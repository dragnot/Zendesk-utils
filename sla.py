"""SLA calculation logic."""

from datetime import datetime, timezone
from config import SLA_MINUTES


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _extract_reply(metric_set: dict):
    """Return first reply time in minutes, or None."""
    rpm = metric_set.get("reply_time_in_minutes")
    if not rpm:
        return None
    v = rpm.get("calendar")
    return float(v) if v is not None else None


def classify(ticket: dict, metric_set: dict) -> dict:
    """
    Return a dict with SLA classification for the ticket:
      status        : "Breached" | "Pending" | "Met"
      minutes_left  : minutes until breach (negative = already over)
      first_reply   : first reply time in minutes (None if no reply yet)
      threshold     : SLA threshold in minutes
    """
    priority  = (ticket.get("priority") or "normal").lower()
    threshold = SLA_MINUTES.get(priority, SLA_MINUTES["normal"])
    created   = _parse(ticket["created_at"])
    age_min   = ((_now_utc() - created).total_seconds()) / 60

    first_reply = _extract_reply(metric_set)

    if first_reply is not None:
        if first_reply <= threshold:
            return dict(status="Met", minutes_left=None,
                        first_reply=first_reply, threshold=threshold)
        else:
            over = first_reply - threshold
            return dict(status="Breached", minutes_left=-over,
                        first_reply=first_reply, threshold=threshold)
    else:
        minutes_left = threshold - age_min
        if minutes_left < 0:
            return dict(status="Breached", minutes_left=minutes_left,
                        first_reply=None, threshold=threshold)
        return dict(status="Pending",
                    minutes_left=minutes_left,
                    first_reply=None, threshold=threshold)


def fmt_minutes(minutes) -> str:
    if minutes is None:
        return "—"
    minutes = abs(int(minutes))
    d, rem = divmod(minutes, 1440)
    h, m   = divmod(rem, 60)
    parts  = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts) or "0m"
