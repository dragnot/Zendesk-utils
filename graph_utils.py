"""Shared utilities for FalkorDB graph population scripts."""

import os
from typing import Optional
from falkordb import FalkorDB

from config import SLA_MINUTES  # single source of truth


def get_local_db() -> FalkorDB:
    """Connect to local FalkorDB instance (localhost:6379)."""
    return FalkorDB(
        host=os.getenv("FALKORDB_LOCAL_HOST", "localhost"),
        port=int(os.getenv("FALKORDB_LOCAL_PORT", 6379)),
    )


def get_remote_db(graph_type: str = "active") -> FalkorDB:
    """Connect to remote FalkorDB using env vars.

    graph_type: "active"  → FALKORDB_ACTIVE_*  (open/pending tickets)
                "history" → FALKORDB_HISTORY_*  (solved/closed tickets)
    """
    prefix = f"FALKORDB_{graph_type.upper()}"
    return FalkorDB(
        host=os.getenv(f"{prefix}_HOST", "localhost"),
        port=int(os.getenv(f"{prefix}_PORT", 6379)),
        username=os.getenv(f"{prefix}_USERNAME") or None,
        password=os.getenv(f"{prefix}_PASSWORD") or None,
    )


def sla_status(priority: str, first_reply_minutes) -> str:
    if first_reply_minutes is None:
        return "No Response"
    threshold = SLA_MINUTES.get((priority or "normal").lower(), 1440)
    return "Met" if first_reply_minutes <= threshold else "Breached"


def sla_over_by(priority: str, first_reply_minutes) -> Optional[float]:
    if first_reply_minutes is None:
        return None
    threshold = SLA_MINUTES.get((priority or "normal").lower(), 1440)
    over = round(first_reply_minutes - threshold, 1)
    return max(0.0, over)


def minutes_to_human(minutes) -> str:
    if minutes is None:
        return "—"
    m = int(minutes)
    if m < 60:
        return f"{m}m"
    if m < 1440:
        return f"{m // 60}h {m % 60}m"
    return f"{m // 1440}d {(m % 1440) // 60}h"


def category_from_tags(tags) -> str:
    if isinstance(tags, list):
        tags_str = " ".join(tags).lower()
    else:
        tags_str = (tags or "").lower()
    if "bug" in tags_str:
        return "Bug"
    if "how-to" in tags_str or "usage_guidance" in tags_str:
        return "How-To"
    if "billing" in tags_str or "subscription" in tags_str:
        return "Billing"
    if "configuration" in tags_str or "setup" in tags_str:
        return "Config"
    if "availability" in tags_str or "incident" in tags_str:
        return "Incident"
    if "performance" in tags_str or "latency" in tags_str:
        return "Performance"
    return "Other"


def channel_from_tags(tags) -> str:
    if isinstance(tags, list):
        tags_str = " ".join(tags).lower()
    else:
        tags_str = (tags or "").lower()
    return "Chat" if "chat" in tags_str else "Email"


def create_indexes(g, labels_props: list[tuple[str, str]]):
    for label, prop in labels_props:
        try:
            g.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except Exception:
            pass


def extract_metric(ms: dict, field: str) -> Optional[float]:
    """Extract calendar-based metric value, return None if missing."""
    val = ms.get(field, {})
    if isinstance(val, dict):
        return val.get("calendar")
    return val if val else None
