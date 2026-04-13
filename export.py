import csv
import os
from datetime import datetime

MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

QUARTER_MAP = {1: "Q1", 2: "Q1", 3: "Q1", 4: "Q2", 5: "Q2", 6: "Q2",
               7: "Q3", 8: "Q3", 9: "Q3", 10: "Q4", 11: "Q4", 12: "Q4"}


def _parse_date_fields(iso_str: str) -> dict:
    """Break an ISO timestamp into pivot-friendly columns."""
    if not iso_str:
        return {"_year": "", "_month": "", "_month_name": "", "_quarter": "",
                "_day_of_week": "", "_date": "", "_year_month": ""}
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return {"_year": "", "_month": "", "_month_name": "", "_quarter": "",
                "_day_of_week": "", "_date": "", "_year_month": ""}
    return {
        "_year": dt.year,
        "_month": dt.month,
        "_month_name": MONTH_NAMES[dt.month],
        "_quarter": QUARTER_MAP[dt.month],
        "_day_of_week": DAY_NAMES[dt.weekday()],
        "_date": dt.strftime("%Y-%m-%d"),
        "_year_month": dt.strftime("%Y-%m"),
    }


SLA_THRESHOLDS = {
    "high":   {"at_risk": 1, "breached": 3},
    "normal": {"at_risk": 5, "breached": 7},
    "low":    {"at_risk": 10, "breached": 14},
    "none":   {"at_risk": 5, "breached": 7},
}


def _sla_status(priority: str, age_days: int) -> str:
    """Determine SLA status based on priority and ticket age."""
    thresholds = SLA_THRESHOLDS.get(priority, SLA_THRESHOLDS["normal"])
    if age_days > thresholds["breached"]:
        return "Breached"
    elif age_days > thresholds["at_risk"]:
        return "At Risk"
    return "On Track"


def _sla_target_days(priority: str) -> int:
    """Return the SLA target in days for a given priority."""
    return SLA_THRESHOLDS.get(priority, SLA_THRESHOLDS["normal"])["breached"]


def export_tickets_to_csv(tickets: list[dict], status: str = "open", output_dir: str = "output") -> str:
    """Export tickets to a pivot-friendly CSV file.

    Args:
        tickets: List of ticket dictionaries from the Zendesk API.
        status: The ticket status filter used (for the filename).
        output_dir: Directory to write the CSV file to.

    Returns:
        Path to the created CSV file.
    """
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"{status}_tickets_{timestamp}.csv")

    fields = [
        "id",
        "subject",
        "status",
        "priority",
        "sla_status",
        "sla_target_days",
        "channel",
        "category",
        "organization_name",
        "requester_name",
        "requester_email",
        "assignee_name",
        "age_days",
        "age_bucket",
        "days_over_sla",
        "created_date",
        "created_year_month",
        "created_month_name",
        "created_quarter",
        "created_week",
        "created_day_of_week",
        "updated_date",
        "tags",
        "description",
    ]

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()

        for ticket in tickets:
            row = {field: ticket.get(field, "") for field in fields}

            tags_raw = ticket.get("tags", "")
            if isinstance(tags_raw, list):
                tags_str = "; ".join(tags_raw)
                tags_lower = " ".join(tags_raw).lower()
            else:
                tags_str = tags_raw
                tags_lower = tags_raw.lower()
            row["tags"] = tags_str

            # Channel: chat vs email
            row["channel"] = "Chat" if "chat" in tags_lower else "Email"

            # Category derived from tags
            if "bug" in tags_lower:
                row["category"] = "Bug"
            elif "how-to" in tags_lower or "usage_guidance" in tags_lower:
                row["category"] = "How-To"
            elif "billing" in tags_lower or "subscription" in tags_lower:
                row["category"] = "Billing"
            elif "configuration" in tags_lower or "setup" in tags_lower:
                row["category"] = "Config"
            elif "availability" in tags_lower or "incident" in tags_lower:
                row["category"] = "Incident"
            elif "performance" in tags_lower or "latency" in tags_lower:
                row["category"] = "Performance"
            else:
                row["category"] = "Other"

            # Org name — fill blanks for cleaner pivots
            if not row.get("organization_name"):
                row["organization_name"] = "(no org)"

            # Date breakdowns
            created = _parse_date_fields(ticket.get("created_at", ""))
            row["created_date"] = created["_date"]
            row["created_year_month"] = created["_year_month"]
            row["created_month_name"] = created["_month_name"]
            row["created_quarter"] = created["_quarter"]
            row["created_day_of_week"] = created["_day_of_week"]
            # ISO week number for weekly grouping
            if created["_date"]:
                try:
                    row["created_week"] = datetime.strptime(created["_date"], "%Y-%m-%d").strftime("%Y-W%W")
                except ValueError:
                    row["created_week"] = ""

            updated = _parse_date_fields(ticket.get("updated_at", ""))
            row["updated_date"] = updated["_date"]

            # Age in days + bucket
            if created["_date"] and updated["_date"]:
                try:
                    d1 = datetime.strptime(created["_date"], "%Y-%m-%d")
                    d2 = datetime.strptime(updated["_date"], "%Y-%m-%d")
                    age = (d2 - d1).days
                    row["age_days"] = age
                    if age <= 3:
                        row["age_bucket"] = "0-3 days"
                    elif age <= 7:
                        row["age_bucket"] = "4-7 days"
                    elif age <= 14:
                        row["age_bucket"] = "8-14 days"
                    else:
                        row["age_bucket"] = "15+ days"
                except ValueError:
                    row["age_days"] = ""
                    row["age_bucket"] = ""

            # Normalize empty priority
            if not row.get("priority"):
                row["priority"] = "none"

            # SLA analysis
            age = row.get("age_days", 0)
            if isinstance(age, str):
                age = int(age) if age else 0
            priority = row["priority"]
            row["sla_status"] = _sla_status(priority, age)
            row["sla_target_days"] = _sla_target_days(priority)
            row["days_over_sla"] = max(0, age - _sla_target_days(priority))

            writer.writerow(row)

    print(f"Exported {len(tickets)} tickets to {filepath}")
    return filepath
