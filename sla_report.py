"""Entry point for the SLA morning report — run manually or via cron.

Usage:
    python sla_report.py              # generate report, print + save to output/
    python sla_report.py --notify     # also send to Telegram (when configured)

Cron (8am daily):
    0 8 * * * cd /path/to/zendesk-util && python sla_report.py >> output/cron.log 2>&1
"""

import os
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from zendesk_client import ZendeskClient
from report import build
from config import OUTPUT_DIR


def save_report(text: str) -> Path:
    Path(OUTPUT_DIR).mkdir(exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = Path(OUTPUT_DIR) / f"sla_report_{date_str}.txt"
    path.write_text(text)
    return path


def send_telegram(text: str):
    import requests
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("⚠️  Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        return
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"```\n{chunk}\n```",
                  "parse_mode": "Markdown"},
            timeout=15,
        )
        r.raise_for_status()
    print("✅ Sent to Telegram")


def main():
    parser = argparse.ArgumentParser(description="Zendesk SLA morning report")
    parser.add_argument("--notify", action="store_true",
                        help="Send report to Telegram after generating")
    args = parser.parse_args()

    client = ZendeskClient(
        subdomain=os.getenv("ZENDESK_SUBDOMAIN", ""),
        email=os.getenv("ZENDESK_EMAIL", ""),
        api_token=os.getenv("ZENDESK_API_TOKEN", ""),
    )

    print("Fetching tickets...")
    tickets = client.get_active_tickets()
    print(f"  {len(tickets)} active tickets")

    print("Fetching metrics...")
    metrics = client.get_bulk_metrics()
    print(f"  {len(metrics)} metric records")

    print("Enriching...")
    tickets = client.enrich_tickets(tickets)

    report_text = build(tickets, metrics)

    print(report_text)

    path = save_report(report_text)
    print(f"📄 Report saved → {path}")

    if args.notify:
        send_telegram(report_text)


if __name__ == "__main__":
    main()
