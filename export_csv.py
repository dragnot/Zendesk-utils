import argparse
import os
import sys

from dotenv import load_dotenv

from zendesk_client import ZendeskClient, VALID_STATUSES
from export import export_tickets_to_csv


def main():
    parser = argparse.ArgumentParser(description="Fetch Zendesk tickets and export to CSV.")
    parser.add_argument(
        "status",
        nargs="?",
        default=None,
        choices=VALID_STATUSES,
        help="Ticket status to fetch: open, pending, solved, or all (default: interactive prompt)",
    )
    args = parser.parse_args()

    load_dotenv()

    subdomain = os.getenv("ZENDESK_SUBDOMAIN")
    email = os.getenv("ZENDESK_EMAIL")
    api_token = os.getenv("ZENDESK_API_TOKEN")

    if not all([subdomain, email, api_token]):
        print("Error: Missing Zendesk credentials in .env file.")
        print("Required: ZENDESK_SUBDOMAIN, ZENDESK_EMAIL, ZENDESK_API_TOKEN")
        sys.exit(1)

    status = args.status
    if status is None:
        print("Which tickets do you want to fetch?")
        for i, s in enumerate(VALID_STATUSES, 1):
            print(f"  {i}. {s}")
        choice = input("Enter choice (1-4): ").strip()
        try:
            status = VALID_STATUSES[int(choice) - 1]
        except (ValueError, IndexError):
            print("Invalid choice.")
            sys.exit(1)

    print(f"\nFetching {status} tickets...")
    client = ZendeskClient(subdomain, email, api_token)

    try:
        tickets = client.get_tickets(status=status)
        if tickets:
            tickets = client.enrich_tickets(tickets)
    except Exception as e:
        print(f"Error fetching tickets: {e}")
        sys.exit(1)

    if not tickets:
        print(f"No {status} tickets found.")
        return

    filepath = export_tickets_to_csv(tickets, status=status)
    print(f"\nDone! {len(tickets)} {status} tickets exported to {filepath}")


if __name__ == "__main__":
    main()
