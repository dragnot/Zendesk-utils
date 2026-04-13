"""Populate local FalkorDB with fresh Zendesk data — active + history graphs.

Graphs created on localhost:6379:
  zendesk_active  — open + pending tickets (live SLA status)
  zendesk_history — solved + closed tickets (trend analytics)

Run:  python populate_local.py
"""

import os
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from zendesk_client import ZendeskClient
from graph_utils import (
    get_local_db, sla_status, sla_over_by, minutes_to_human,
    category_from_tags, channel_from_tags, create_indexes,
    extract_metric, SLA_MINUTES,
)
from populate_history import _build_graph_batched

W = 70


# ─── helpers ──────────────────────────────────────────────────────────────────

def _prep_tickets(tickets: list[dict], metrics_map: dict) -> list[dict]:
    """Attach bulk metrics to each ticket."""
    for t in tickets:
        t["metric_set"] = metrics_map.get(t["id"], t.get("metric_set", {}))
    return tickets


def _load_graph(g, tickets: list[dict], graph_type: str):
    g.query("MATCH (n) DETACH DELETE n")
    create_indexes(g, [
        ("Ticket", "ticket_id"),
        ("Organization", "name"),
        ("User", "user_id"),
        ("Agent", "user_id"),
        ("Category", "name"),
        ("Month", "year_month"),
    ])
    return _build_graph_batched(g, tickets, graph_type)


# ─── SLA report ───────────────────────────────────────────────────────────────

def _sla_report(active_tickets: list[dict], history_tickets: list[dict]):
    print("\n" + "=" * W)
    print("  ZENDESK SLA REPORT  —  " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    print("=" * W)

    all_tickets = active_tickets + history_tickets

    # ── Scorecard ────────────────────────────────────────────────────────────
    def score(tickets):
        met = breached = no_resp = 0
        for t in tickets:
            ms = t.get("metric_set", {})
            fr = extract_metric(ms, "reply_time_in_minutes")
            p  = (t.get("priority") or "normal").lower()
            s  = sla_status(p, fr)
            if s == "Met":          met      += 1
            elif s == "Breached":   breached += 1
            else:                   no_resp  += 1
        return met, breached, no_resp

    def pct(n, total): return f"{round(100*n/total)}%" if total else "—"

    for label, tickets in [("ACTIVE (open/pending)", active_tickets),
                            ("HISTORY (solved/closed)", history_tickets),
                            ("COMBINED", all_tickets)]:
        met, breached, no_resp = score(tickets)
        total = len(tickets)
        print(f"\n── {label} ({'total: ' + str(total)}) " + "─" * max(0, W - len(label) - 18))
        print(f"  {'Met':<16} {met:>4}  ({pct(met, total)})")
        print(f"  {'Breached':<16} {breached:>4}  ({pct(breached, total)})")
        print(f"  {'No Response':<16} {no_resp:>4}  ({pct(no_resp, total)})")

    # ── Active breaches detail ────────────────────────────────────────────────
    print(f"\n── Current SLA Breaches (active tickets) {'─' * 29}")
    breaches = []
    for t in active_tickets:
        ms = t.get("metric_set", {})
        fr = extract_metric(ms, "reply_time_in_minutes")
        p  = (t.get("priority") or "normal").lower()
        if sla_status(p, fr) == "Breached":
            over = sla_over_by(p, fr)
            breaches.append((over or 0, t, fr))
    breaches.sort(key=lambda x: -x[0])

    if not breaches:
        print("  🎉 No active SLA breaches!")
    for over, t, fr in breaches:
        org   = t.get("organization_name") or "(no org)"
        agent = t.get("assignee_name") or "unassigned"
        p     = (t.get("priority") or "normal").lower()
        subj  = (t.get("subject") or "")[:55]
        print(f"  #{t['id']} [{p}] {org:<20} assignee={agent}")
        print(f"    first reply: {minutes_to_human(fr)}  over SLA by: {minutes_to_human(over)}")
        print(f"    {subj}")

    # ── By priority ───────────────────────────────────────────────────────────
    print(f"\n── SLA by Priority (active) {'─' * 41}")
    pri_data: dict = {}
    for t in active_tickets:
        ms = t.get("metric_set", {})
        fr = extract_metric(ms, "reply_time_in_minutes")
        p  = (t.get("priority") or "normal").lower()
        s  = sla_status(p, fr)
        if p not in pri_data:
            pri_data[p] = {"Met": 0, "Breached": 0, "No Response": 0, "threshold": SLA_MINUTES.get(p, 1440)}
        pri_data[p][s] += 1

    for p, d in sorted(pri_data.items()):
        thr = minutes_to_human(d["threshold"])
        print(f"  {p:<8} (SLA≤{thr}): Met={d['Met']}  Breached={d['Breached']}  No Response={d['No Response']}")

    # ── By org (active) ───────────────────────────────────────────────────────
    print(f"\n── Active Tickets by Organization {'─' * 36}")
    org_data: dict = {}
    for t in active_tickets:
        ms    = t.get("metric_set", {})
        fr    = extract_metric(ms, "reply_time_in_minutes")
        p     = (t.get("priority") or "normal").lower()
        org   = t.get("organization_name") or "(no org)"
        s     = sla_status(p, fr)
        if org not in org_data:
            org_data[org] = {"total": 0, "breach": 0, "replies": []}
        org_data[org]["total"] += 1
        if s == "Breached":
            org_data[org]["breach"] += 1
        if fr is not None:
            org_data[org]["replies"].append(fr)

    print(f"  {'Org':<22} {'Total':>5} {'Breach':>6}  {'Avg Reply':>12}")
    print(f"  {'-'*22} {'-'*5} {'-'*6}  {'-'*12}")
    for org, d in sorted(org_data.items(), key=lambda x: -x[1]["total"]):
        avg = minutes_to_human(sum(d["replies"])/len(d["replies"])) if d["replies"] else "—"
        print(f"  {org:<22} {d['total']:>5} {d['breach']:>6.1f}  {avg:>12}")

    # ── Monthly trend (history) ───────────────────────────────────────────────
    print(f"\n── Monthly Ticket Volume (history) {'─' * 34}")
    month_data: dict = {}
    for t in history_tickets:
        ca = t.get("created_at", "")
        try:
            ym = ca[:7]
        except Exception:
            ym = "unknown"
        ms = t.get("metric_set", {})
        fr = extract_metric(ms, "reply_time_in_minutes")
        p  = (t.get("priority") or "normal").lower()
        s  = sla_status(p, fr)
        if ym not in month_data:
            month_data[ym] = {"total": 0, "met": 0}
        month_data[ym]["total"] += 1
        if s == "Met":
            month_data[ym]["met"] += 1

    for ym in sorted(month_data):
        d     = month_data[ym]
        mp    = pct(d["met"], d["total"])
        bar   = "█" * max(1, d["total"] // 5)
        print(f"  {ym}  {d['total']:>4} tickets  SLA met {mp:>4}  {bar}")

    print("\n" + "=" * W)


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    client = ZendeskClient(
        subdomain=os.getenv("ZENDESK_SUBDOMAIN", ""),
        email=os.getenv("ZENDESK_EMAIL", ""),
        api_token=os.getenv("ZENDESK_API_TOKEN", ""),
    )

    print("[1/5] Fetching all ticket metrics (bulk)...")
    metrics_map = client.get_all_ticket_metrics()

    print("\n[2/5] Fetching active tickets (open + pending)...")
    active_tickets = _prep_tickets(
        client.get_tickets_with_metrics("open") + client.get_tickets_with_metrics("pending"),
        metrics_map,
    )
    print(f"      {len(active_tickets)} active tickets")

    print("\n[3/5] Fetching history tickets (solved + closed)...")
    history_tickets = _prep_tickets(
        client.get_tickets_with_metrics("solved") + client.get_tickets_with_metrics("closed"),
        metrics_map,
    )
    print(f"      {len(history_tickets)} history tickets")

    print("\n[4/5] Enriching all tickets...")
    active_tickets  = client.enrich_tickets(active_tickets)
    history_tickets = client.enrich_tickets(history_tickets)

    print("\n[5/5] Loading into local FalkorDB...")
    db = get_local_db()

    print("  → zendesk_active...")
    g_active = db.select_graph("zendesk_active")
    n_orgs, n_agents, n_cats, _ = _load_graph(g_active, active_tickets, "active")
    print(f"     {len(active_tickets)} tickets, {n_orgs} orgs, {n_agents} agents, {n_cats} categories")

    print("  → zendesk_history...")
    g_history = db.select_graph("zendesk_history")
    n_orgs, n_agents, n_cats, n_months = _load_graph(g_history, history_tickets, "history")
    print(f"     {len(history_tickets)} tickets, {n_orgs} orgs, {n_agents} agents, {n_months} months, {n_cats} categories")

    _sla_report(active_tickets, history_tickets)


if __name__ == "__main__":
    main()
