"""Populate zendesk_active graph — open + pending tickets with live SLA status.

Graph: zendesk_active  (remote FalkorDB)
Run:   python populate_active.py

Ontology:
  (Ticket)-[:FROM_ORG]------>(Organization)
  (Ticket)-[:REQUESTED_BY]->(User)
  (Ticket)-[:ASSIGNED_TO]-->(Agent)
  (Ticket)-[:IN_CATEGORY]-->(Category)
  (User)-[:BELONGS_TO]----->(Organization)
"""

import os
import sys

from dotenv import load_dotenv

from zendesk_client import ZendeskClient
from graph_utils import (
    get_remote_db, sla_status, sla_over_by, minutes_to_human,
    category_from_tags, channel_from_tags, create_indexes,
    extract_metric, SLA_MINUTES,
)

from populate_history import _build_graph_batched

GRAPH_NAME = "zendesk_active"




def build_active_graph(client: ZendeskClient):
    print("\n[1/4] Fetching all ticket metrics (bulk)...")
    metrics_map = client.get_all_ticket_metrics()

    print("\n[2/4] Fetching open + pending tickets...")
    open_t    = client.get_tickets_with_metrics("open")
    pending_t = client.get_tickets_with_metrics("pending")
    tickets   = open_t + pending_t
    # Attach metrics from bulk map (overrides any empty metric_set)
    for t in tickets:
        t["metric_set"] = metrics_map.get(t["id"], t.get("metric_set", {}))

    print(f"\n[3/4] Enriching {len(tickets)} tickets...")
    tickets = client.enrich_tickets(tickets)

    print(f"\n[4/4] Loading into graph '{GRAPH_NAME}'...")
    db = get_remote_db("active")
    g = db.select_graph(GRAPH_NAME)

    g.query("MATCH (n) DETACH DELETE n")
    create_indexes(g, [
        ("Ticket", "ticket_id"),
        ("Organization", "name"),
        ("User", "user_id"),
        ("Agent", "user_id"),
        ("Category", "name"),
    ])

    n_orgs, n_agents, n_cats, _ = _build_graph_batched(g, tickets, "active")

    print(f"\nGraph '{GRAPH_NAME}' ready: {len(tickets)} tickets, "
          f"{n_orgs} orgs, {n_agents} agents, {n_cats} categories")
    run_active_queries(g)


def run_active_queries(g):
    W = 70
    print("\n" + "=" * W)
    print(f" ACTIVE TICKETS — SLA DASHBOARD  (graph: {GRAPH_NAME})")
    print("=" * W)

    # Scorecard
    print("\n── SLA Scorecard ──────────────────────────────────────────────")
    r = g.query(
        "MATCH (t:Ticket) RETURN t.sla_status AS s, COUNT(t) AS n ORDER BY n DESC"
    )
    total = sum(row[1] for row in r.result_set)
    for row in r.result_set:
        pct = row[1] * 100 // total if total else 0
        print(f"  {row[0]:<16} {row[1]:>3} ({pct}%)")

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
            print(f"  {pri:<8} (SLA≤{thr}): "
                  f"Met={d.get('Met',0)}  "
                  f"Breached={d.get('Breached',0)}  "
                  f"No Response={d.get('No Response',0)}")

    # Breaches
    print("\n── Current SLA Breaches ───────────────────────────────────────")
    r = g.query(
        "MATCH (t:Ticket)-[:FROM_ORG]->(o:Organization), "
        "      (t)-[:ASSIGNED_TO]->(a:Agent) "
        "WHERE t.sla_status = 'Breached' "
        "RETURN t.ticket_id, t.priority, t.sla_over_by_minutes, "
        "       t.first_reply_minutes, o.name, a.name, t.subject "
        "ORDER BY t.sla_over_by_minutes DESC"
    )
    for row in r.result_set:
        over = minutes_to_human(row[2])
        actual = minutes_to_human(row[3])
        print(f"  #{row[0]} [{row[1]}] {str(row[4]):<18} assignee={row[5]}")
        print(f"    first reply: {actual}  over SLA by: {over}")
        print(f"    {str(row[6])[:65]}")

    # By org
    print("\n── Tickets by Organization ────────────────────────────────────")
    r = g.query(
        "MATCH (t:Ticket)-[:FROM_ORG]->(o:Organization) "
        "RETURN o.name, COUNT(t) AS total, "
        "  SUM(CASE WHEN t.sla_status='Breached' THEN 1 ELSE 0 END) AS breached, "
        "  ROUND(AVG(t.first_reply_minutes)) AS avg_reply "
        "ORDER BY total DESC"
    )
    print(f"  {'Org':<22} {'Total':>5} {'Breach':>6} {'Avg Reply':>12}")
    print(f"  {'-'*22} {'-'*5} {'-'*6} {'-'*12}")
    for row in r.result_set:
        avg = minutes_to_human(row[3])
        print(f"  {str(row[0]):<22} {row[1]:>5} {row[2]:>6} {avg:>12}")

    print("\n" + "=" * W)


def main():
    load_dotenv()
    client = ZendeskClient(
        os.getenv("ZENDESK_SUBDOMAIN"),
        os.getenv("ZENDESK_EMAIL"),
        os.getenv("ZENDESK_API_TOKEN"),
    )
    build_active_graph(client)


if __name__ == "__main__":
    main()
