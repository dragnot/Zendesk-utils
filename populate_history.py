"""Populate zendesk_history graph — all solved + closed tickets with full resolution metrics.

Graph: zendesk_history  (remote FalkorDB)
Run:   python populate_history.py

Ontology:
  (Ticket)-[:FROM_ORG]------>(Organization)
  (Ticket)-[:REQUESTED_BY]->(User)
  (Ticket)-[:RESOLVED_BY]-->(Agent)
  (Ticket)-[:IN_CATEGORY]-->(Category)
  (Ticket)-[:CLOSED_IN]---->(Month)
  (User)-[:BELONGS_TO]----->(Organization)
"""

import os
import sys
from collections import defaultdict

from dotenv import load_dotenv

from zendesk_client import ZendeskClient
from graph_utils import (
    get_remote_db, sla_status, sla_over_by, minutes_to_human,
    category_from_tags, channel_from_tags, create_indexes,
    extract_metric, SLA_MINUTES,
)

GRAPH_NAME = "zendesk_history"


def _build_graph_batched(g, tickets: list[dict], graph_type: str = "history"):
    """Load all tickets into the graph using UNWIND batching for speed.
    graph_type: 'history' (solved/closed) or 'active' (open/pending)
    """
    from datetime import datetime, timezone

    # ── Pre-process all tickets into flat dicts ───────────────────────────
    ticket_rows = []
    org_rows    = {}   # name -> row
    user_rows   = {}   # user_id -> row
    agent_rows  = {}   # user_id -> row
    cat_rows    = {}   # name -> row
    month_rows  = {}   # year_month -> row (history only)

    rel_ticket_org   = []
    rel_ticket_user  = []
    rel_ticket_agent = []
    rel_ticket_cat   = []
    rel_ticket_month = []
    rel_user_org     = []

    for t in tickets:
        tid      = int(t["id"])
        priority = (t.get("priority") or "normal").lower()
        org_name = t.get("organization_name") or "(no org)"
        ms       = t.get("metric_set", {})

        first_reply      = extract_metric(ms, "reply_time_in_minutes")
        first_resolution = extract_metric(ms, "first_resolution_time_in_minutes")
        full_resolution  = extract_metric(ms, "full_resolution_time_in_minutes")
        req_wait         = extract_metric(ms, "requester_wait_time_in_minutes")
        agent_wait       = extract_metric(ms, "agent_wait_time_in_minutes")
        solved_at        = ms.get("solved_at") or t.get("updated_at", "")
        threshold        = SLA_MINUTES.get(priority, 1440)
        s_status         = sla_status(priority, first_reply)
        s_over           = sla_over_by(priority, first_reply)
        category         = category_from_tags(t.get("tags", []))
        channel          = channel_from_tags(t.get("tags", []))
        ts               = solved_at or t.get("created_at", "")
        year_month       = ts[:7] if ts else "unknown"

        if graph_type == "active":
            created_at = t.get("created_at", "")
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - created_dt).days
            except Exception:
                age_days = 0
            ticket_rows.append({
                "id": tid, "subject": t.get("subject", ""),
                "status": t.get("status", ""), "priority": priority,
                "category": category, "channel": channel,
                "created": created_at, "age": age_days,
                "frm": first_reply, "rwm": req_wait,
                "replies": ms.get("replies", 0), "reopens": ms.get("reopens", 0),
                "threshold": threshold, "sla_status": s_status, "sla_over": s_over,
            })
        else:
            ticket_rows.append({
                "id": tid, "subject": t.get("subject", ""),
                "status": t.get("status", ""), "priority": priority,
                "category": category, "channel": channel,
                "created": t.get("created_at", ""), "solved": solved_at,
                "ym": year_month,
                "frm": first_reply, "frsm": first_resolution,
                "fullm": full_resolution, "rwm": req_wait, "awm": agent_wait,
                "replies": ms.get("replies", 0), "reopens": ms.get("reopens", 0),
                "threshold": threshold, "sla_status": s_status, "sla_over": s_over,
            })
            if year_month not in month_rows:
                month_rows[year_month] = {"ym": year_month}
            rel_ticket_month.append({"tid": tid, "ym": year_month})

        if org_name not in org_rows:
            org_rows[org_name] = {"name": org_name}
        rel_ticket_org.append({"tid": tid, "org": org_name})

        rid = t.get("requester_id")
        if rid:
            if rid not in user_rows:
                user_rows[rid] = {"uid": rid, "name": t.get("requester_name", ""),
                                  "email": t.get("requester_email", "")}
            rel_ticket_user.append({"tid": tid, "uid": rid})
            if org_name != "(no org)":
                rel_user_org.append({"uid": rid, "org": org_name})

        aid = t.get("assignee_id")
        if aid:
            if aid not in agent_rows:
                agent_rows[aid] = {"uid": aid, "name": t.get("assignee_name", "")}
            rel = "RESOLVED_BY" if graph_type == "history" else "ASSIGNED_TO"
            rel_ticket_agent.append({"tid": tid, "uid": aid, "rel": rel})

        if category not in cat_rows:
            cat_rows[category] = {"name": category}
        rel_ticket_cat.append({"tid": tid, "cat": category})

    # ── Batch CREATE nodes with UNWIND ────────────────────────────────────
    BATCH = 100

    def unwind_create(cypher_tpl: str, rows: list, batch_size: int = BATCH):
        for i in range(0, len(rows), batch_size):
            g.query(cypher_tpl, params={"rows": rows[i:i + batch_size]})

    print("  Creating Ticket nodes...")
    if graph_type == "active":
        unwind_create(
            "UNWIND $rows AS r CREATE (:Ticket {"
            "ticket_id:r.id, subject:r.subject, status:r.status, priority:r.priority,"
            "category:r.category, channel:r.channel, created_at:r.created, age_days:r.age,"
            "first_reply_minutes:r.frm, requester_wait_minutes:r.rwm,"
            "replies:r.replies, reopens:r.reopens,"
            "sla_threshold_minutes:r.threshold, sla_status:r.sla_status,"
            "sla_over_by_minutes:r.sla_over})",
            ticket_rows,
        )
    else:
        unwind_create(
            "UNWIND $rows AS r CREATE (:Ticket {"
            "ticket_id:r.id, subject:r.subject, status:r.status, priority:r.priority,"
            "category:r.category, channel:r.channel,"
            "created_at:r.created, solved_at:r.solved, year_month:r.ym,"
            "first_reply_minutes:r.frm, first_resolution_minutes:r.frsm,"
            "full_resolution_minutes:r.fullm, requester_wait_minutes:r.rwm,"
            "agent_wait_minutes:r.awm, replies:r.replies, reopens:r.reopens,"
            "sla_threshold_minutes:r.threshold, sla_status:r.sla_status,"
            "sla_over_by_minutes:r.sla_over})",
            ticket_rows,
        )

    print("  Creating Organization nodes...")
    unwind_create(
        "UNWIND $rows AS r CREATE (:Organization {name:r.name})",
        list(org_rows.values()),
    )

    print("  Creating User nodes...")
    unwind_create(
        "UNWIND $rows AS r CREATE (:User {user_id:r.uid, name:r.name, email:r.email})",
        list(user_rows.values()),
    )

    print("  Creating Agent nodes...")
    unwind_create(
        "UNWIND $rows AS r CREATE (:Agent {user_id:r.uid, name:r.name})",
        list(agent_rows.values()),
    )

    print("  Creating Category nodes...")
    unwind_create(
        "UNWIND $rows AS r CREATE (:Category {name:r.name})",
        list(cat_rows.values()),
    )

    if graph_type == "history" and month_rows:
        print("  Creating Month nodes...")
        unwind_create(
            "UNWIND $rows AS r CREATE (:Month {year_month:r.ym})",
            list(month_rows.values()),
        )

    # ── Batch CREATE relationships ────────────────────────────────────────
    print("  Creating Ticket→Org relationships...")
    unwind_create(
        "UNWIND $rows AS r "
        "MATCH (t:Ticket {ticket_id:r.tid}),(o:Organization {name:r.org}) "
        "CREATE (t)-[:FROM_ORG]->(o)",
        rel_ticket_org,
    )

    print("  Creating Ticket→User relationships...")
    unwind_create(
        "UNWIND $rows AS r "
        "MATCH (t:Ticket {ticket_id:r.tid}),(u:User {user_id:r.uid}) "
        "CREATE (t)-[:REQUESTED_BY]->(u)",
        rel_ticket_user,
    )

    print("  Creating Ticket→Agent relationships...")
    # Split by rel type
    resolved = [r for r in rel_ticket_agent if r["rel"] == "RESOLVED_BY"]
    assigned = [r for r in rel_ticket_agent if r["rel"] == "ASSIGNED_TO"]
    if resolved:
        unwind_create(
            "UNWIND $rows AS r "
            "MATCH (t:Ticket {ticket_id:r.tid}),(a:Agent {user_id:r.uid}) "
            "CREATE (t)-[:RESOLVED_BY]->(a)",
            resolved,
        )
    if assigned:
        unwind_create(
            "UNWIND $rows AS r "
            "MATCH (t:Ticket {ticket_id:r.tid}),(a:Agent {user_id:r.uid}) "
            "CREATE (t)-[:ASSIGNED_TO]->(a)",
            assigned,
        )

    print("  Creating Ticket→Category relationships...")
    unwind_create(
        "UNWIND $rows AS r "
        "MATCH (t:Ticket {ticket_id:r.tid}),(c:Category {name:r.cat}) "
        "CREATE (t)-[:IN_CATEGORY]->(c)",
        rel_ticket_cat,
    )

    if graph_type == "history" and rel_ticket_month:
        print("  Creating Ticket→Month relationships...")
        unwind_create(
            "UNWIND $rows AS r "
            "MATCH (t:Ticket {ticket_id:r.tid}),(m:Month {year_month:r.ym}) "
            "CREATE (t)-[:CLOSED_IN]->(m)",
            rel_ticket_month,
        )

    print("  Creating User→Org relationships...")
    # Deduplicate user-org pairs
    seen_uo = set()
    dedup_user_org = []
    for r in rel_user_org:
        key = (r["uid"], r["org"])
        if key not in seen_uo:
            seen_uo.add(key)
            dedup_user_org.append(r)
    unwind_create(
        "UNWIND $rows AS r "
        "MATCH (u:User {user_id:r.uid}),(o:Organization {name:r.org}) "
        "MERGE (u)-[:BELONGS_TO]->(o)",
        dedup_user_org,
    )

    return len(org_rows), len(agent_rows), len(cat_rows), len(month_rows)


def build_history_graph(client: ZendeskClient):
    print("\n[1/4] Fetching all ticket metrics (bulk)...")
    metrics_map = client.get_all_ticket_metrics()

    print("\n[2/4] Fetching solved + closed tickets...")
    solved_t = client.get_tickets_with_metrics("solved")
    closed_t = client.get_tickets_with_metrics("closed")
    tickets  = solved_t + closed_t
    # Attach metrics from bulk map
    for t in tickets:
        t["metric_set"] = metrics_map.get(t["id"], t.get("metric_set", {}))
    print(f"Total: {len(tickets)} historical tickets")

    print(f"\n[3/4] Enriching {len(tickets)} tickets...")
    tickets = client.enrich_tickets(tickets)

    print(f"\n[4/4] Loading into graph '{GRAPH_NAME}'...")
    db = get_remote_db("history")
    g = db.select_graph(GRAPH_NAME)

    g.query("MATCH (n) DETACH DELETE n")
    create_indexes(g, [
        ("Ticket", "ticket_id"),
        ("Organization", "name"),
        ("User", "user_id"),
        ("Agent", "user_id"),
        ("Category", "name"),
        ("Month", "year_month"),
    ])

    n_orgs, n_agents, n_cats, n_months = _build_graph_batched(g, tickets, "history")

    print(f"\nGraph '{GRAPH_NAME}' ready: {len(tickets)} tickets, "
          f"{n_orgs} orgs, {n_agents} agents, "
          f"{n_months} months, {n_cats} categories")
    run_history_queries(g)


def run_history_queries(g):
    W = 70
    print("\n" + "=" * W)
    print(f" HISTORICAL TICKETS — SLA ANALYTICS  (graph: {GRAPH_NAME})")
    print("=" * W)

    # Overall SLA
    print("\n── Overall SLA Compliance ─────────────────────────────────────")
    r = g.query(
        "MATCH (t:Ticket) RETURN t.sla_status AS s, COUNT(t) AS n ORDER BY n DESC"
    )
    total = sum(row[1] for row in r.result_set)
    for row in r.result_set:
        pct = row[1] * 100 // total if total else 0
        bar = "█" * (pct // 4)
        print(f"  {row[0]:<16} {row[1]:>4} ({pct:>2}%)  {bar}")

    # SLA by priority
    print("\n── SLA by Priority ────────────────────────────────────────────")
    r = g.query(
        "MATCH (t:Ticket) "
        "RETURN t.priority, t.sla_status, COUNT(t) "
        "ORDER BY t.priority, t.sla_status"
    )
    from collections import defaultdict
    by_pri = defaultdict(dict)
    for row in r.result_set:
        by_pri[row[0]][row[1]] = row[2]
    for pri in ["urgent", "high", "normal", "low", "none"]:
        if pri in by_pri:
            d = by_pri[pri]
            thr = minutes_to_human(SLA_MINUTES.get(pri, 1440))
            tot_p = sum(d.values())
            met_pct = d.get("Met", 0) * 100 // tot_p if tot_p else 0
            print(f"  {pri:<8} (SLA≤{thr}): "
                  f"Met={d.get('Met',0)} ({met_pct}%)  "
                  f"Breached={d.get('Breached',0)}  "
                  f"No Response={d.get('No Response',0)}")

    # Avg resolution time by priority
    print("\n── Avg Resolution Time by Priority ────────────────────────────")
    r = g.query(
        "MATCH (t:Ticket) WHERE t.full_resolution_minutes IS NOT NULL "
        "RETURN t.priority, ROUND(AVG(t.full_resolution_minutes)) AS avg_res, "
        "  ROUND(AVG(t.first_reply_minutes)) AS avg_reply, COUNT(t) "
        "ORDER BY avg_res"
    )
    for row in r.result_set:
        print(f"  {str(row[0]):<8}  avg_resolution={minutes_to_human(row[1])}  "
              f"avg_first_reply={minutes_to_human(row[2])}  n={row[3]}")

    # SLA compliance by org (top 15)
    print("\n── SLA Compliance by Organization (top 15) ───────────────────")
    r = g.query(
        "MATCH (t:Ticket)-[:FROM_ORG]->(o:Organization) "
        "RETURN o.name, COUNT(t) AS total, "
        "  SUM(CASE WHEN t.sla_status='Met' THEN 1 ELSE 0 END) AS met, "
        "  SUM(CASE WHEN t.sla_status='Breached' THEN 1 ELSE 0 END) AS breached, "
        "  ROUND(AVG(t.full_resolution_minutes)) AS avg_res "
        "ORDER BY total DESC LIMIT 15"
    )
    print(f"  {'Org':<22} {'Total':>5} {'Met%':>5} {'Breach':>6} {'Avg Resolution':>16}")
    print(f"  {'-'*22} {'-'*5} {'-'*5} {'-'*6} {'-'*16}")
    for row in r.result_set:
        total_o = row[1] or 1
        met_pct = int(row[2]) * 100 // total_o
        avg_res = minutes_to_human(row[4])
        print(f"  {str(row[0]):<22} {row[1]:>5} {met_pct:>4}% {row[3]:>6} {avg_res:>16}")

    # Monthly trend
    print("\n── Monthly Ticket Volume & SLA ────────────────────────────────")
    r = g.query(
        "MATCH (t:Ticket)-[:CLOSED_IN]->(m:Month) "
        "RETURN m.year_month AS ym, COUNT(t) AS total, "
        "  SUM(CASE WHEN t.sla_status='Met' THEN 1 ELSE 0 END) AS met, "
        "  ROUND(AVG(t.full_resolution_minutes)) AS avg_res "
        "ORDER BY ym"
    )
    for row in r.result_set:
        total_m = row[1] or 1
        pct = int(row[2]) * 100 // total_m
        bar = "█" * (row[1] // 5)
        avg_res = minutes_to_human(row[3])
        print(f"  {row[0]}  {row[1]:>4} tickets  SLA met {pct:>2}%  avg_res={avg_res}  {bar}")

    # Agent performance
    print("\n── Agent Resolution Performance ───────────────────────────────")
    r = g.query(
        "MATCH (t:Ticket)-[:RESOLVED_BY]->(a:Agent) "
        "WHERE t.full_resolution_minutes IS NOT NULL "
        "RETURN a.name, COUNT(t) AS tickets, "
        "  ROUND(AVG(t.full_resolution_minutes)) AS avg_res, "
        "  ROUND(AVG(t.first_reply_minutes)) AS avg_reply, "
        "  SUM(CASE WHEN t.sla_status='Met' THEN 1 ELSE 0 END) AS met "
        "ORDER BY tickets DESC"
    )
    for row in r.result_set:
        total_a = row[1] or 1
        met_pct = int(row[4]) * 100 // total_a
        print(f"  {str(row[0]):<25} {row[1]:>4} tickets  "
              f"avg_reply={minutes_to_human(row[3])}  "
              f"avg_res={minutes_to_human(row[2])}  "
              f"SLA met={met_pct}%")

    # Category breakdown
    print("\n── Category Breakdown ─────────────────────────────────────────")
    r = g.query(
        "MATCH (t:Ticket)-[:IN_CATEGORY]->(c:Category) "
        "RETURN c.name, COUNT(t) AS n, "
        "  ROUND(AVG(t.full_resolution_minutes)) AS avg_res "
        "ORDER BY n DESC"
    )
    for row in r.result_set:
        print(f"  {str(row[0]):<14}  {row[1]:>4} tickets  avg_res={minutes_to_human(row[2])}")

    print("\n" + "=" * W)


def main():
    load_dotenv()
    client = ZendeskClient(
        os.getenv("ZENDESK_SUBDOMAIN"),
        os.getenv("ZENDESK_EMAIL"),
        os.getenv("ZENDESK_API_TOKEN"),
    )
    build_history_graph(client)


if __name__ == "__main__":
    main()
