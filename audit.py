"""Query Zendesk audit logs for admin actions.

Usage:
  python audit.py                                    # all admin actions (last 7 days)
  python audit.py --source view --action create      # views created
  python audit.py --source trigger --action update   # triggers updated
  python audit.py --source user --action create      # users created
  python audit.py --source rule                      # all rule changes (macros, triggers, automations)
  python audit.py --days 30                           # last 30 days
  python audit.py --source macro                     # all macro changes

Common source_type values:
  user, rule, trigger, macro, automation, view, ticket,
  group, organization, brand, apitoken, sla_policy
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from zendesk_client import ZendeskClient


def fetch_audit_logs(client: ZendeskClient, source_type: str = None,
                     action: str = None, days: int = 7) -> list[dict]:
    """Fetch audit logs with optional filters."""
    url = f"{client.base_url}/audit_logs.json"
    params = {}

    if source_type:
        params["filter[source_type]"] = source_type
    if action:
        params["filter[action]"] = action

    since = datetime.now(timezone.utc) - timedelta(days=days)
    params["filter[created_at][]"] = [
        since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    ]

    all_logs = []
    page = 1

    while url:
        response = client.session.get(url, params=params)

        if response.status_code == 403:
            print("Error: Audit logs require an Enterprise plan.")
            sys.exit(1)

        response.raise_for_status()
        data = response.json()
        logs = data.get("audit_logs", [])
        all_logs.extend(logs)
        print(f"  Page {page}: fetched {len(logs)} entries ({len(all_logs)} total)")

        url = data.get("next_page")
        params = None
        page += 1

    return all_logs


def display_logs(logs: list[dict]):
    if not logs:
        print("\nNo audit log entries found for this filter.")
        return

    print(f"\n{'='*80}")
    print(f"{'TIME':<22} {'ACTION':<10} {'SOURCE TYPE':<16} {'ACTOR':<25} {'DESCRIPTION'}")
    print(f"{'='*80}")

    for log in logs:
        created = log.get("created_at", "")[:19].replace("T", " ")
        action = log.get("action", "")
        source_type = log.get("source_type", "")
        actor_name = log.get("actor_name", "System")
        source_label = log.get("source_label", "")

        # Truncate description for readability
        desc = source_label[:50] if source_label else ""
        print(f"  {created:<20} {action:<10} {source_type:<16} {actor_name:<25} {desc}")

    print(f"\nTotal: {len(logs)} entries")


def main():
    parser = argparse.ArgumentParser(description="Query Zendesk audit logs.")
    parser.add_argument("--source", "-s", help="Filter by source_type (e.g. view, trigger, user, rule, macro)")
    parser.add_argument("--action", "-a", help="Filter by action (create, update, destroy, login, exported)")
    parser.add_argument("--days", "-d", type=int, default=7, help="Look back N days (default: 7)")
    args = parser.parse_args()

    load_dotenv()
    subdomain = os.getenv("ZENDESK_SUBDOMAIN")
    email = os.getenv("ZENDESK_EMAIL")
    api_token = os.getenv("ZENDESK_API_TOKEN")

    if not all([subdomain, email, api_token]):
        print("Error: Missing credentials in .env")
        sys.exit(1)

    client = ZendeskClient(subdomain, email, api_token)

    filter_desc = []
    if args.source:
        filter_desc.append(f"source={args.source}")
    if args.action:
        filter_desc.append(f"action={args.action}")
    filter_desc.append(f"last {args.days} days")
    print(f"Fetching audit logs ({', '.join(filter_desc)})...")

    logs = fetch_audit_logs(client, source_type=args.source, action=args.action, days=args.days)
    display_logs(logs)


if __name__ == "__main__":
    main()
