"""Fetch last 30 days of tickets + comments, compute first-response SLA,
export per-ticket CSVs, and load everything into a dedicated FalkorDB graph.

SLA thresholds (first agent response):
  urgent  → 30 minutes
  high    → 1 hour  (60 minutes)
  normal  → 1 day   (1440 minutes)
  low     → 3 days  (4320 minutes)

Graph model:
  (Ticket)-[:HAS_COMMENT]->(Comment)
  (Ticket)-[:FROM_ORG]   ->(Organization)
  (Ticket)-[:ASSIGNED_TO]->(Agent)
  (Ticket)-[:REQUESTED_BY]->(User)
  (Comment)-[:BY]        ->(User)

Usage:
  python sla_graph.py            # last 30 days
  python sla_graph.py --days 14  # last 14 days
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from falkordb import FalkorDB

from zendesk_client import ZendeskClient

# ── SLA thresholds in minutes ────────────────────────────────────────────────
SLA_MINUTES = {
    "urgent": 30,
    "high":   60,
    "normal": 1440,
    "low":    4320,
    "none":   1440,
}

GRAPH_NAME = "zendesk_sla_30d"
OUTPUT_DIR = os.path.join("output", "tickets")


# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def minutes_between(a: str, b: str) -> float:
    return (parse_dt(b) - parse_dt(a)).total_seconds() / 60


def sla_status(priority: str, first_response_minutes) -> str:
    if first_response_minutes is None:
        return "No Response"
    threshold = SLA_MINUTES.get(priority, SLA_MINUTES["normal"])
    return "Met" if first_response_minutes <= threshold else "Breached"


def minutes_to_human(minutes) -> str:
    if minutes is None:
        return "—"
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 1440:
        return f"{minutes // 60}h {minutes % 60}m"
    return f"{minutes // 1440}d {(minutes % 1440) // 60}h"


def category_from_tags(tags_lower: str) -> str:
    if "bug" in tags_lower:
        return "Bug"
    if "how-to" in tags_lower or "usage_guidance" in tags_lower:
        return "How-To"
    if "billing" in tags_lower or "subscription" in tags_lower:
        return "Billing"
    if "configuration" in tags_lower or "setup" in tags_lower:
        return "Config"
    if "availability" in tags_lower or "incident" in tags_lower:
        return "Incident"
    if "performance" in tags_lower or "latency" in tags_lower:
        return "Performance"
    return "Other"


# ── Fetch pipeline ────────────────────────────────────────────────────────────

def fetch_all_data(client: ZendeskClient, days: int):
    """Fetch tickets + enrich + fetch all comments. Returns enriched tickets
    where each ticket has a 'comments' list and SLA fields."""

    print(f"\n[1/4] Fetching tickets from last {days} days...")
    tickets = client.get_tickets_last_n_days(days)
    tickets = client.enrich_tickets(tickets)

    print(f"\n[2/4] Fetching comments for {len(tickets)} tickets...")
    # Collect all unique author IDs across all comments
    all_author_ids = set()
    for i, t in enumerate(tickets, 1):
        tid = t["id"]
        comments = client.get_ticket_comments(tid)
        t["comments"] = comments
        for c in comments:
            all_author_ids.add(c["author_id"])
        if i % 10 == 0:
            print(f"  ...{i}/{len(tickets)} tickets processed")
        time.sleep(0.1)  # gentle rate limit

    print(f"\n[3/4] Resolving {len(all_author_ids)} comment authors...")
    user_roles = client.get_user_roles(list(all_author_ids))
    user_names = {uid: u.get("name", "") for uid, u in
                  client.get_users(list(all_author_ids)).items()}
    user_emails = {uid: u.get("email", "") for uid, u in
                   client.get_users(list(all_author_ids)).items()}

    print(f"\n[4/4] Computing SLA first-response metrics...")
    for t in tickets:
        requester_id = t.get("requester_id")
        priority = (t.get("priority") or "normal").lower()
        created_at = t.get("created_at", "")
        threshold_min = SLA_MINUTES.get(priority, SLA_MINUTES["normal"])

        # Find first PUBLIC comment by a non-end-user who isn't the requester
        first_response = None
        for c in t["comments"]:
            if not c.get("public"):
                continue
            author_id = c["author_id"]
            if author_id == requester_id:
                continue
            role = user_roles.get(author_id, "end-user")
            if role in ("admin", "agent"):
                first_response = c
                break

        if first_response:
            minutes = minutes_between(created_at, first_response["created_at"])
            t["first_response_minutes"] = round(minutes, 1)
            t["first_response_at"] = first_response["created_at"]
            t["first_response_by"] = user_names.get(first_response["author_id"], "")
        else:
            t["first_response_minutes"] = None
            t["first_response_at"] = None
            t["first_response_by"] = None

        t["sla_threshold_minutes"] = threshold_min
        t["sla_status"] = sla_status(priority, t["first_response_minutes"])
        t["sla_over_by_minutes"] = (
            max(0, round(t["first_response_minutes"] - threshold_min, 1))
            if t["first_response_minutes"] is not None else None
        )

        # Annotate each comment with author info
        for c in t["comments"]:
            aid = c["author_id"]
            c["author_name"] = user_names.get(aid, "")
            c["author_email"] = user_emails.get(aid, "")
            c["author_role"] = user_roles.get(aid, "end-user")

        # Category
        tags = " ".join(t.get("tags", []) if isinstance(t.get("tags"), list)
                        else (t.get("tags") or "").split("; ")).lower()
        t["category"] = category_from_tags(tags)

    return tickets


# ── CSV export ────────────────────────────────────────────────────────────────

def export_per_ticket_csvs(tickets: list[dict]):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    TICKET_FIELDS = [
        "id", "subject", "status", "priority", "category",
        "organization_name", "requester_name", "requester_email",
        "assignee_name", "created_at", "updated_at",
        "sla_status", "sla_threshold_minutes", "first_response_minutes",
        "first_response_at", "first_response_by", "sla_over_by_minutes",
        "comment_count",
    ]
    COMMENT_FIELDS = [
        "seq", "created_at", "author_name", "author_email", "author_role",
        "public", "body",
    ]

    print(f"\nExporting per-ticket CSVs to {OUTPUT_DIR}/...")
    for t in tickets:
        tid = t["id"]
        path = os.path.join(OUTPUT_DIR, f"ticket_{tid}.csv")
        comments = t.get("comments", [])

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)

            # Header block
            writer.writerow(["=== TICKET SUMMARY ==="])
            writer.writerow(TICKET_FIELDS)
            writer.writerow([
                t.get("id"), t.get("subject", ""), t.get("status", ""),
                t.get("priority", ""), t.get("category", ""),
                t.get("organization_name", ""), t.get("requester_name", ""),
                t.get("requester_email", ""), t.get("assignee_name", ""),
                t.get("created_at", ""), t.get("updated_at", ""),
                t.get("sla_status", ""), t.get("sla_threshold_minutes", ""),
                t.get("first_response_minutes", ""), t.get("first_response_at", ""),
                t.get("first_response_by", ""), t.get("sla_over_by_minutes", ""),
                len(comments),
            ])

            writer.writerow([])
            writer.writerow(["=== COMMENTS ==="])
            writer.writerow(COMMENT_FIELDS)
            for seq, c in enumerate(comments, 1):
                body = (c.get("plain_body") or c.get("body") or "").replace("\n", " ")[:500]
                writer.writerow([
                    seq, c.get("created_at", ""), c.get("author_name", ""),
                    c.get("author_email", ""), c.get("author_role", ""),
                    c.get("public", ""), body,
                ])

    print(f"Exported {len(tickets)} ticket CSVs.")


# ── Graph loader ──────────────────────────────────────────────────────────────

def build_sla_graph(tickets: list[dict]):
    db = FalkorDB(host="localhost", port=6379)
    g = db.select_graph(GRAPH_NAME)

    print(f"\nClearing graph '{GRAPH_NAME}'...")
    g.query("MATCH (n) DETACH DELETE n")

    # Indexes
    for idx in [
        "CREATE INDEX FOR (t:Ticket) ON (t.ticket_id)",
        "CREATE INDEX FOR (u:User) ON (u.user_id)",
        "CREATE INDEX FOR (o:Organization) ON (o.name)",
    ]:
        try:
            g.query(idx)
        except Exception:
            pass

    print(f"Loading {len(tickets)} tickets + comments into graph '{GRAPH_NAME}'...")

    orgs_created = set()
    users_created = set()

    for t in tickets:
        tid = int(t["id"])
        priority = (t.get("priority") or "normal").lower()
        org_name = t.get("organization_name") or "(no org)"

        # ── Ticket node ──────────────────────────────────────────────────
        g.query(
            "CREATE (:Ticket {"
            "  ticket_id:$id, subject:$subject, status:$status, priority:$priority,"
            "  category:$category, channel:$channel,"
            "  created_at:$created, updated_at:$updated,"
            "  sla_status:$sla_status, sla_threshold_minutes:$threshold,"
            "  first_response_minutes:$frm, first_response_at:$fra,"
            "  first_response_by:$frb, sla_over_by_minutes:$over,"
            "  comment_count:$cc"
            "})",
            params={
                "id": tid,
                "subject": t.get("subject", ""),
                "status": t.get("status", ""),
                "priority": priority,
                "category": t.get("category", "Other"),
                "channel": "Chat" if "chat" in " ".join(
                    t.get("tags", []) if isinstance(t.get("tags"), list)
                    else (t.get("tags") or "").split("; ")
                ).lower() else "Email",
                "created": t.get("created_at", ""),
                "updated": t.get("updated_at", ""),
                "sla_status": t.get("sla_status", "No Response"),
                "threshold": t.get("sla_threshold_minutes", 1440),
                "frm": t.get("first_response_minutes"),
                "fra": t.get("first_response_at"),
                "frb": t.get("first_response_by"),
                "over": t.get("sla_over_by_minutes"),
                "cc": len(t.get("comments", [])),
            },
        )

        # ── Organization ─────────────────────────────────────────────────
        if org_name not in orgs_created:
            g.query("CREATE (:Organization {name:$name})", params={"name": org_name})
            orgs_created.add(org_name)
        g.query(
            "MATCH (t:Ticket {ticket_id:$id}),(o:Organization {name:$org}) "
            "CREATE (t)-[:FROM_ORG]->(o)",
            params={"id": tid, "org": org_name},
        )

        # ── Users (requester, assignee, comment authors) ──────────────
        def ensure_user(uid, name, email, role):
            if uid and uid not in users_created:
                g.query(
                    "CREATE (:User {user_id:$uid, name:$name, email:$email, role:$role})",
                    params={"uid": uid, "name": name or "", "email": email or "", "role": role or ""},
                )
                users_created.add(uid)

        ensure_user(t.get("requester_id"), t.get("requester_name"), t.get("requester_email"), "end-user")
        if t.get("requester_id"):
            g.query(
                "MATCH (t:Ticket {ticket_id:$id}),(u:User {user_id:$uid}) "
                "CREATE (t)-[:REQUESTED_BY]->(u)",
                params={"id": tid, "uid": t["requester_id"]},
            )

        if t.get("assignee_id"):
            ensure_user(t["assignee_id"], t.get("assignee_name"), None, "agent")
            g.query(
                "MATCH (t:Ticket {ticket_id:$id}),(u:User {user_id:$uid}) "
                "MERGE (t)-[:ASSIGNED_TO]->(u)",
                params={"id": tid, "uid": t["assignee_id"]},
            )

        # ── Comments ─────────────────────────────────────────────────────
        for seq, c in enumerate(t.get("comments", []), 1):
            cid = c["id"]
            author_id = c["author_id"]
            ensure_user(author_id, c.get("author_name"), c.get("author_email"), c.get("author_role"))

            body = (c.get("plain_body") or c.get("body") or "").replace("\n", " ")[:500]
            g.query(
                "CREATE (:Comment {"
                "  comment_id:$cid, ticket_id:$tid, seq:$seq,"
                "  created_at:$created, public:$public, body:$body"
                "})",
                params={
                    "cid": cid, "tid": tid, "seq": seq,
                    "created": c.get("created_at", ""),
                    "public": c.get("public", True),
                    "body": body,
                },
            )
            g.query(
                "MATCH (t:Ticket {ticket_id:$tid}),(c:Comment {comment_id:$cid}) "
                "CREATE (t)-[:HAS_COMMENT]->(c)",
                params={"tid": tid, "cid": cid},
            )
            g.query(
                "MATCH (c:Comment {comment_id:$cid}),(u:User {user_id:$uid}) "
                "CREATE (c)-[:BY]->(u)",
                params={"cid": cid, "uid": author_id},
            )

    total_comments = sum(len(t.get("comments", [])) for t in tickets)
    print(f"Graph '{GRAPH_NAME}' ready: {len(tickets)} tickets, "
          f"{total_comments} comments, {len(orgs_created)} orgs, {len(users_created)} users")


# ── SLA insight queries ───────────────────────────────────────────────────────

def run_sla_queries(g):
    W = 70
    print("\n" + "=" * W)
    print(" SLA INSIGHTS")
    print("=" * W)

    # 1. Overall SLA scorecard
    print("\n── Overall SLA Scorecard ──────────────────────────────────────")
    r = g.query(
        "MATCH (t:Ticket) "
        "RETURN t.sla_status AS status, COUNT(t) AS count "
        "ORDER BY count DESC"
    )
    total = sum(row[1] for row in r.result_set)
    for row in r.result_set:
        pct = row[1] * 100 // total if total else 0
        bar = "█" * (pct // 3)
        print(f"  {row[0]:<14} {row[1]:>3} ({pct:>2}%)  {bar}")

    # 2. SLA by priority
    print("\n── SLA by Priority ────────────────────────────────────────────")
    r = g.query(
        "MATCH (t:Ticket) "
        "RETURN t.priority AS priority, t.sla_status AS sla, COUNT(t) AS count "
        "ORDER BY priority, sla"
    )
    from collections import defaultdict
    by_pri = defaultdict(dict)
    for row in r.result_set:
        by_pri[row[0]][row[1]] = row[2]
    for pri in ["urgent", "high", "normal", "low", "none"]:
        if pri in by_pri:
            d = by_pri[pri]
            threshold = SLA_MINUTES.get(pri, 1440)
            thresh_str = minutes_to_human(threshold)
            print(f"  {pri:<8} (SLA={thresh_str}): "
                  f"Met={d.get('Met',0)}, "
                  f"Breached={d.get('Breached',0)}, "
                  f"NoResponse={d.get('No Response',0)}")

    # 3. SLA by organization
    print("\n── SLA by Organization ────────────────────────────────────────")
    r = g.query(
        "MATCH (t:Ticket)-[:FROM_ORG]->(o:Organization) "
        "RETURN o.name AS org, "
        "  COUNT(t) AS total, "
        "  SUM(CASE WHEN t.sla_status='Met' THEN 1 ELSE 0 END) AS met, "
        "  SUM(CASE WHEN t.sla_status='Breached' THEN 1 ELSE 0 END) AS breached, "
        "  ROUND(AVG(t.first_response_minutes)) AS avg_response_min "
        "ORDER BY breached DESC, total DESC"
    )
    print(f"  {'Org':<22} {'Total':>5} {'Met':>5} {'Breached':>8} {'Avg 1st Response':>18}")
    print(f"  {'-'*22} {'-'*5} {'-'*5} {'-'*8} {'-'*18}")
    for row in r.result_set:
        avg = minutes_to_human(row[4]) if row[4] else "—"
        print(f"  {str(row[0]):<22} {row[1]:>5} {row[2]:>5} {row[3]:>8} {avg:>18}")

    # 4. Worst SLA breaches
    print("\n── Worst SLA Breaches (by over-SLA minutes) ───────────────────")
    r = g.query(
        "MATCH (t:Ticket)-[:FROM_ORG]->(o:Organization) "
        "WHERE t.sla_status = 'Breached' "
        "RETURN t.ticket_id, t.priority, t.sla_over_by_minutes, "
        "       t.first_response_minutes, t.sla_threshold_minutes, "
        "       o.name, t.subject "
        "ORDER BY t.sla_over_by_minutes DESC LIMIT 15"
    )
    for row in r.result_set:
        over = minutes_to_human(row[2])
        actual = minutes_to_human(row[3])
        target = minutes_to_human(row[4])
        print(f"  #{row[0]} [{row[1]}] {str(row[5]):<18} "
              f"responded in {actual} (target {target}, over by {over})")
        print(f"    {str(row[6])[:65]}")

    # 5. No response tickets
    print("\n── No Agent Response Yet ──────────────────────────────────────")
    r = g.query(
        "MATCH (t:Ticket)-[:FROM_ORG]->(o:Organization) "
        "WHERE t.sla_status = 'No Response' "
        "RETURN t.ticket_id, t.priority, t.status, o.name, t.subject "
        "ORDER BY t.priority"
    )
    if r.result_set:
        for row in r.result_set:
            print(f"  #{row[0]} [{row[1]}] {row[2]:<9} {str(row[3]):<18} {str(row[4])[:55]}")
    else:
        print("  None — all tickets have at least one agent response ✓")

    # 6. Avg first response by assignee
    print("\n── Avg First Response by Assignee ─────────────────────────────")
    r = g.query(
        "MATCH (t:Ticket)-[:ASSIGNED_TO]->(u:User) "
        "WHERE t.first_response_minutes IS NOT NULL "
        "RETURN u.name AS agent, COUNT(t) AS tickets, "
        "  ROUND(AVG(t.first_response_minutes)) AS avg_min, "
        "  SUM(CASE WHEN t.sla_status='Breached' THEN 1 ELSE 0 END) AS breached "
        "ORDER BY avg_min"
    )
    for row in r.result_set:
        avg = minutes_to_human(row[2])
        print(f"  {str(row[0]):<25} {row[1]:>3} tickets  avg={avg}  breached={row[3]}")

    print("\n" + "=" * W)
    print(f"Graph: '{GRAPH_NAME}' — run your own queries with redis-cli or Python")
    print(f"Per-ticket CSVs: {OUTPUT_DIR}/ticket_<id>.csv")
    print("=" * W)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch tickets+comments, compute SLA, load into FalkorDB.")
    parser.add_argument("--days", type=int, default=30, help="Number of days to look back (default: 30)")
    args = parser.parse_args()

    load_dotenv()
    subdomain = os.getenv("ZENDESK_SUBDOMAIN")
    email = os.getenv("ZENDESK_EMAIL")
    api_token = os.getenv("ZENDESK_API_TOKEN")
    if not all([subdomain, email, api_token]):
        print("Error: Missing credentials in .env")
        sys.exit(1)

    client = ZendeskClient(subdomain, email, api_token)
    tickets = fetch_all_data(client, args.days)

    export_per_ticket_csvs(tickets)

    build_sla_graph(tickets)

    db = FalkorDB(host="localhost", port=6379)
    g = db.select_graph(GRAPH_NAME)
    run_sla_queries(g)


if __name__ == "__main__":
    main()
