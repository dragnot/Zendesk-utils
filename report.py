"""Build the SLA morning report text."""

from datetime import datetime, timezone
from config import SLA_MINUTES, WARNING_MINUTES
from sla import classify, fmt_minutes

W = 72


def _bar(pct: int, width: int = 20) -> str:
    filled = round(pct * width / 100)
    return "█" * filled + "░" * (width - filled)


def build(tickets: list[dict], metrics: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = []

    rows = []
    for t in tickets:
        ms  = metrics.get(t["id"], {})
        clf = classify(t, ms)
        rows.append((t, clf))

    breached = [(t, c) for t, c in rows if c["status"] == "Breached"]
    at_risk  = [(t, c) for t, c in rows
                if c["status"] == "Pending" and c["minutes_left"] is not None
                and c["minutes_left"] <= WARNING_MINUTES]
    on_track = [(t, c) for t, c in rows
                if c["status"] == "Pending" and (
                    c["minutes_left"] is None or c["minutes_left"] > WARNING_MINUTES)]
    met      = [(t, c) for t, c in rows if c["status"] == "Met"]

    total = len(rows)

    lines += [
        "=" * W,
        f"  ZENDESK SLA MORNING REPORT  —  {now}",
        f"  Active tickets: {total}   "
        f"Breached: {len(breached)}   "
        f"At Risk: {len(at_risk)}   "
        f"Met/On Track: {len(met) + len(on_track)}",
        "=" * W,
    ]

    lines.append(f"\n🔴  BREACHED ({len(breached)} tickets)")
    lines.append("─" * W)
    if not breached:
        lines.append("  None 🎉")
    else:
        for t, c in sorted(breached, key=lambda x: x[1]["minutes_left"]):
            _ticket_block(lines, t, c, label="OVERDUE BY",
                          value=fmt_minutes(c["minutes_left"]))

    warn_h = round(WARNING_MINUTES / 60, 1)
    lines.append(f"\n🟡  AT RISK — breaching within {warn_h}h ({len(at_risk)} tickets)")
    lines.append("─" * W)
    if not at_risk:
        lines.append("  None ✅")
    else:
        for t, c in sorted(at_risk, key=lambda x: x[1]["minutes_left"]):
            _ticket_block(lines, t, c, label="TIME LEFT",
                          value=fmt_minutes(c["minutes_left"]))

    lines.append(f"\n📊  SCORECARD")
    lines.append("─" * W)
    for label, subset, emoji in [
        ("Met (replied within SLA)", met,      "✅"),
        ("On Track (no reply yet, safe)", on_track, "🟢"),
        ("At Risk",  at_risk,   "🟡"),
        ("Breached", breached,  "🔴"),
    ]:
        n   = len(subset)
        pct = round(100 * n / total) if total else 0
        lines.append(f"  {emoji} {label:<32} {n:>3}  {_bar(pct)} {pct}%")

    lines.append(f"\n📋  PRIORITY BREAKDOWN (active tickets)")
    lines.append("─" * W)
    pri_totals: dict = {}
    for t, c in rows:
        p = (t.get("priority") or "normal").lower()
        if p not in pri_totals:
            pri_totals[p] = {"total": 0, "breached": 0, "at_risk": 0}
        pri_totals[p]["total"] += 1
        if c["status"] == "Breached":
            pri_totals[p]["breached"] += 1
        elif c["status"] == "Pending" and c.get("minutes_left", 9999) <= WARNING_MINUTES:
            pri_totals[p]["at_risk"] += 1

    for p in ("urgent", "high", "normal", "low"):
        if p not in pri_totals:
            continue
        d   = pri_totals[p]
        thr = fmt_minutes(SLA_MINUTES.get(p, 1440))
        lines.append(f"  {p:<8} SLA≤{thr:<10}  total={d['total']:>3}  "
                     f"breached={d['breached']}  at_risk={d['at_risk']}")

    lines.append("\n" + "=" * W)
    lines.append("  To send via Telegram:")
    lines.append("    python sla_report.py --notify")
    lines.append("=" * W + "\n")

    return "\n".join(lines)


def _ticket_block(lines, t, c, label, value):
    priority = (t.get("priority") or "normal").upper()
    org      = t.get("organization_name") or "(no org)"
    assignee = t.get("assignee_name") or "unassigned"
    subj     = (t.get("subject") or "")[:60]
    thr      = fmt_minutes(c["threshold"])
    lines.append(
        f"  #{t['id']}  [{priority}]  {label}: {value}  (SLA={thr})"
    )
    lines.append(f"    Org: {org:<20}  Assignee: {assignee}")
    lines.append(f"    {subj}")
    lines.append("")
