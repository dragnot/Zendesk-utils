"""Populate the local `zendesk_entities` graph using a two-pass design.

Pass 1: every Zendesk object (ticket, user, agent, organization, category, month)
        is created with a single label `:Entity`, plus all of its properties.
Pass 2: each Entity is promoted to its typed second label (`:Ticket`, `:User`, ...)
        by looking it up via its unique `entity_id` and running `SET e:<Type>`.
Pass 3: relationships are created between the typed entities (same ontology as
        populate_history.py).

Result: each node ends up with two labels, e.g. (:Entity:Ticket).

Run:  python populate_entities.py
"""

import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from zendesk_client import ZendeskClient
from graph_utils import (
    get_local_db, sla_status, sla_over_by,
    category_from_tags, channel_from_tags, create_indexes,
    extract_metric, SLA_MINUTES,
)

load_dotenv()

GRAPH_NAME = "zendesk_entities"
BATCH = 100


# ─── id helpers ───────────────────────────────────────────────────────────────

def tid(ticket_id):  return f"ticket:{ticket_id}"
def uid(user_id):    return f"user:{user_id}"
def aid(agent_id):   return f"agent:{agent_id}"
def oid(org_name):   return f"org:{org_name}"
def cid(cat_name):   return f"category:{cat_name}"
def mid(year_month): return f"month:{year_month}"


# ─── pass 1 row builders ──────────────────────────────────────────────────────

def _build_rows(active_tickets, history_tickets):
    """Return per-kind row dicts ready for Pass 1 UNWIND CREATE."""
    ticket_rows = []
    org_rows    = {}
    user_rows   = {}
    agent_rows  = {}
    cat_rows    = {}
    month_rows  = {}

    rel_ticket_org   = []
    rel_ticket_user  = []
    rel_ticket_agent = []   # {tid, aid, rel}
    rel_ticket_cat   = []
    rel_ticket_month = []
    rel_user_org     = []

    def _push_ticket(t, graph_type):
        ticket_id = int(t["id"])
        priority  = (t.get("priority") or "normal").lower()
        org_name  = t.get("organization_name") or "(no org)"
        ms        = t.get("metric_set", {})

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

        try:
            created_dt = datetime.fromisoformat(t.get("created_at", "").replace("Z", "+00:00"))
            age_days   = (datetime.now(timezone.utc) - created_dt).days
        except Exception:
            age_days = 0

        ticket_rows.append({
            "entity_id":             tid(ticket_id),
            "ticket_id":             ticket_id,
            "subject":               t.get("subject", ""),
            "status":                t.get("status", ""),
            "priority":              priority,
            "category":              category,
            "channel":               channel,
            "graph_type":            graph_type,
            "created_at":            t.get("created_at", ""),
            "solved_at":             solved_at,
            "year_month":            year_month,
            "age_days":              age_days,
            "first_reply_minutes":   first_reply,
            "first_resolution_minutes": first_resolution,
            "full_resolution_minutes": full_resolution,
            "requester_wait_minutes":  req_wait,
            "agent_wait_minutes":      agent_wait,
            "replies":               ms.get("replies", 0),
            "reopens":               ms.get("reopens", 0),
            "sla_threshold_minutes": threshold,
            "sla_status":            s_status,
            "sla_over_by_minutes":   s_over,
        })

        # Org
        if org_name not in org_rows:
            org_rows[org_name] = {"entity_id": oid(org_name), "name": org_name}
        rel_ticket_org.append({"src": tid(ticket_id), "dst": oid(org_name)})

        # Requester
        rid = t.get("requester_id")
        if rid:
            if rid not in user_rows:
                user_rows[rid] = {
                    "entity_id": uid(rid),
                    "user_id":   rid,
                    "name":      t.get("requester_name", ""),
                    "email":     t.get("requester_email", ""),
                }
            rel_ticket_user.append({"src": tid(ticket_id), "dst": uid(rid)})
            if org_name != "(no org)":
                rel_user_org.append({"src": uid(rid), "dst": oid(org_name)})

        # Agent
        a_id = t.get("assignee_id")
        if a_id:
            if a_id not in agent_rows:
                agent_rows[a_id] = {
                    "entity_id": aid(a_id),
                    "user_id":   a_id,
                    "name":      t.get("assignee_name", ""),
                }
            rel = "RESOLVED_BY" if graph_type == "history" else "ASSIGNED_TO"
            rel_ticket_agent.append({"src": tid(ticket_id), "dst": aid(a_id), "rel": rel})

        # Category
        if category not in cat_rows:
            cat_rows[category] = {"entity_id": cid(category), "name": category}
        rel_ticket_cat.append({"src": tid(ticket_id), "dst": cid(category)})

        # Month (history only)
        if graph_type == "history":
            if year_month not in month_rows:
                month_rows[year_month] = {"entity_id": mid(year_month), "year_month": year_month}
            rel_ticket_month.append({"src": tid(ticket_id), "dst": mid(year_month)})

    for t in active_tickets:
        _push_ticket(t, "active")
    for t in history_tickets:
        _push_ticket(t, "history")

    return {
        "tickets":       ticket_rows,
        "organizations": list(org_rows.values()),
        "users":         list(user_rows.values()),
        "agents":        list(agent_rows.values()),
        "categories":    list(cat_rows.values()),
        "months":        list(month_rows.values()),
        "rel_ticket_org":   rel_ticket_org,
        "rel_ticket_user":  rel_ticket_user,
        "rel_ticket_agent": rel_ticket_agent,
        "rel_ticket_cat":   rel_ticket_cat,
        "rel_ticket_month": rel_ticket_month,
        "rel_user_org":     rel_user_org,
    }


# ─── graph operations ─────────────────────────────────────────────────────────

def _unwind(g, cypher, rows):
    for i in range(0, len(rows), BATCH):
        g.query(cypher, params={"rows": rows[i:i + BATCH]})


def _pass1_create_entities(g, data):
    """Create every node with only the :Entity label, carrying all properties."""
    print("\n[Pass 1] Creating all nodes as :Entity ...")

    print(f"  → {len(data['tickets']):>4} tickets")
    _unwind(g,
        "UNWIND $rows AS r CREATE (:Entity {"
        "entity_id:r.entity_id, kind:'ticket',"
        "ticket_id:r.ticket_id, subject:r.subject, status:r.status, priority:r.priority,"
        "category:r.category, channel:r.channel, graph_type:r.graph_type,"
        "created_at:r.created_at, solved_at:r.solved_at, year_month:r.year_month,"
        "age_days:r.age_days,"
        "first_reply_minutes:r.first_reply_minutes,"
        "first_resolution_minutes:r.first_resolution_minutes,"
        "full_resolution_minutes:r.full_resolution_minutes,"
        "requester_wait_minutes:r.requester_wait_minutes,"
        "agent_wait_minutes:r.agent_wait_minutes,"
        "replies:r.replies, reopens:r.reopens,"
        "sla_threshold_minutes:r.sla_threshold_minutes,"
        "sla_status:r.sla_status, sla_over_by_minutes:r.sla_over_by_minutes})",
        data["tickets"])

    print(f"  → {len(data['organizations']):>4} organizations")
    _unwind(g,
        "UNWIND $rows AS r CREATE (:Entity {entity_id:r.entity_id, kind:'organization', name:r.name})",
        data["organizations"])

    print(f"  → {len(data['users']):>4} users")
    _unwind(g,
        "UNWIND $rows AS r CREATE (:Entity {entity_id:r.entity_id, kind:'user', "
        "user_id:r.user_id, name:r.name, email:r.email})",
        data["users"])

    print(f"  → {len(data['agents']):>4} agents")
    _unwind(g,
        "UNWIND $rows AS r CREATE (:Entity {entity_id:r.entity_id, kind:'agent', "
        "user_id:r.user_id, name:r.name})",
        data["agents"])

    print(f"  → {len(data['categories']):>4} categories")
    _unwind(g,
        "UNWIND $rows AS r CREATE (:Entity {entity_id:r.entity_id, kind:'category', name:r.name})",
        data["categories"])

    print(f"  → {len(data['months']):>4} months")
    _unwind(g,
        "UNWIND $rows AS r CREATE (:Entity {entity_id:r.entity_id, kind:'month', year_month:r.year_month})",
        data["months"])


def _pass2_promote_labels(g, data):
    """For each kind, look up entities by entity_id and SET the typed label."""
    print("\n[Pass 2] Promoting Entities to typed labels (SET e:<Type>) ...")

    promotions = [
        ("Ticket",       [{"id": r["entity_id"]} for r in data["tickets"]]),
        ("Organization", [{"id": r["entity_id"]} for r in data["organizations"]]),
        ("User",         [{"id": r["entity_id"]} for r in data["users"]]),
        ("Agent",        [{"id": r["entity_id"]} for r in data["agents"]]),
        ("Category",     [{"id": r["entity_id"]} for r in data["categories"]]),
        ("Month",        [{"id": r["entity_id"]} for r in data["months"]]),
    ]

    for label, rows in promotions:
        if not rows:
            continue
        print(f"  → SET e:{label} on {len(rows):>4} entities")
        _unwind(g,
            f"UNWIND $rows AS r MATCH (e:Entity {{entity_id:r.id}}) SET e:{label}",
            rows)


def _pass3_relationships(g, data):
    print("\n[Pass 3] Creating relationships ...")

    print(f"  → Ticket-[:FROM_ORG]->Organization      ({len(data['rel_ticket_org'])})")
    _unwind(g,
        "UNWIND $rows AS r "
        "MATCH (s:Entity {entity_id:r.src}),(d:Entity {entity_id:r.dst}) "
        "CREATE (s)-[:FROM_ORG]->(d)",
        data["rel_ticket_org"])

    print(f"  → Ticket-[:REQUESTED_BY]->User          ({len(data['rel_ticket_user'])})")
    _unwind(g,
        "UNWIND $rows AS r "
        "MATCH (s:Entity {entity_id:r.src}),(d:Entity {entity_id:r.dst}) "
        "CREATE (s)-[:REQUESTED_BY]->(d)",
        data["rel_ticket_user"])

    resolved = [r for r in data["rel_ticket_agent"] if r["rel"] == "RESOLVED_BY"]
    assigned = [r for r in data["rel_ticket_agent"] if r["rel"] == "ASSIGNED_TO"]
    if resolved:
        print(f"  → Ticket-[:RESOLVED_BY]->Agent          ({len(resolved)})")
        _unwind(g,
            "UNWIND $rows AS r "
            "MATCH (s:Entity {entity_id:r.src}),(d:Entity {entity_id:r.dst}) "
            "CREATE (s)-[:RESOLVED_BY]->(d)",
            resolved)
    if assigned:
        print(f"  → Ticket-[:ASSIGNED_TO]->Agent          ({len(assigned)})")
        _unwind(g,
            "UNWIND $rows AS r "
            "MATCH (s:Entity {entity_id:r.src}),(d:Entity {entity_id:r.dst}) "
            "CREATE (s)-[:ASSIGNED_TO]->(d)",
            assigned)

    print(f"  → Ticket-[:IN_CATEGORY]->Category       ({len(data['rel_ticket_cat'])})")
    _unwind(g,
        "UNWIND $rows AS r "
        "MATCH (s:Entity {entity_id:r.src}),(d:Entity {entity_id:r.dst}) "
        "CREATE (s)-[:IN_CATEGORY]->(d)",
        data["rel_ticket_cat"])

    if data["rel_ticket_month"]:
        print(f"  → Ticket-[:CLOSED_IN]->Month            ({len(data['rel_ticket_month'])})")
        _unwind(g,
            "UNWIND $rows AS r "
            "MATCH (s:Entity {entity_id:r.src}),(d:Entity {entity_id:r.dst}) "
            "CREATE (s)-[:CLOSED_IN]->(d)",
            data["rel_ticket_month"])

    seen = set()
    dedup_user_org = []
    for r in data["rel_user_org"]:
        key = (r["src"], r["dst"])
        if key not in seen:
            seen.add(key)
            dedup_user_org.append(r)
    if dedup_user_org:
        print(f"  → User-[:BELONGS_TO]->Organization      ({len(dedup_user_org)})")
        _unwind(g,
            "UNWIND $rows AS r "
            "MATCH (s:Entity {entity_id:r.src}),(d:Entity {entity_id:r.dst}) "
            "MERGE (s)-[:BELONGS_TO]->(d)",
            dedup_user_org)


# ─── main ─────────────────────────────────────────────────────────────────────

CACHE_PATH = "output/entities_cache.json"


def _fetch_and_cache():
    """Fetch from Zendesk, build row dicts, persist to disk for later passes."""
    import json
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)

    client = ZendeskClient(
        os.getenv("ZENDESK_SUBDOMAIN", ""),
        os.getenv("ZENDESK_EMAIL", ""),
        os.getenv("ZENDESK_API_TOKEN", ""),
    )

    print("[fetch 1/3] Bulk ticket metrics...")
    metrics_map = client.get_all_ticket_metrics()

    print("\n[fetch 2/3] Active + history tickets...")
    active = (client.get_tickets_with_metrics("open")
              + client.get_tickets_with_metrics("pending"))
    history = (client.get_tickets_with_metrics("solved")
               + client.get_tickets_with_metrics("closed"))
    for t in active + history:
        t["metric_set"] = metrics_map.get(t["id"], t.get("metric_set", {}))
    print(f"           {len(active)} active + {len(history)} history "
          f"= {len(active)+len(history)} tickets")

    print("\n[fetch 3/3] Enriching with users + organizations...")
    active  = client.enrich_tickets(active)
    history = client.enrich_tickets(history)

    data = _build_rows(active, history)
    with open(CACHE_PATH, "w") as f:
        json.dump(data, f)
    print(f"\nCached row data → {CACHE_PATH}")
    return data


def _load_cache():
    import json
    if not os.path.exists(CACHE_PATH):
        raise SystemExit(
            f"\nNo cache found at {CACHE_PATH}.\n"
            f"Run `python populate_entities.py pass1` first to fetch + cache + create Entities."
        )
    with open(CACHE_PATH) as f:
        return json.load(f)


def _verify(g):
    print("\n── Verification ───────────────────────────────────────────────")
    r = g.query("MATCH (n) RETURN labels(n) AS lbls, count(n) AS n ORDER BY n DESC")
    for row in r.result_set:
        print(f"  {str(row[0]):<32} {row[1]}")
    r = g.query("MATCH (e:Entity) RETURN count(e)")
    print(f"\n  Total :Entity nodes: {r.result_set[0][0]}")


# ─── CLI subcommands ──────────────────────────────────────────────────────────

def cmd_pass1():
    """Fetch Zendesk + cache + wipe graph + create all nodes as :Entity only."""
    data = _fetch_and_cache()

    print("\n[Pass 1] Wiping graph and creating :Entity nodes...")
    db = get_local_db()
    g  = db.select_graph(GRAPH_NAME)
    g.query("MATCH (n) DETACH DELETE n")
    create_indexes(g, [("Entity", "entity_id")])
    _pass1_create_entities(g, data)
    _verify(g)


def cmd_pass2():
    """Promote each Entity to its typed second label via entity_id."""
    data = _load_cache()
    db = get_local_db()
    g  = db.select_graph(GRAPH_NAME)
    _pass2_promote_labels(g, data)
    _verify(g)


def cmd_pass3():
    """Create relationships between typed entities."""
    data = _load_cache()
    db = get_local_db()
    g  = db.select_graph(GRAPH_NAME)
    _pass3_relationships(g, data)
    _verify(g)


def cmd_all():
    """Run all three passes in sequence (original behaviour)."""
    cmd_pass1()
    cmd_pass2()
    cmd_pass3()


COMMANDS = {
    "pass1": cmd_pass1,
    "pass2": cmd_pass2,
    "pass3": cmd_pass3,
    "all":   cmd_all,
}


def main():
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd not in COMMANDS:
        raise SystemExit(
            f"Unknown command '{cmd}'. Valid: {', '.join(COMMANDS)}"
        )
    print(f"=== populate_entities.py: running '{cmd}' ===")
    COMMANDS[cmd]()


if __name__ == "__main__":
    main()
