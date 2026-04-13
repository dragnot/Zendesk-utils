# Zendesk Utility

A collection of tools for fetching, analysing, and visualising Zendesk support tickets using Python and FalkorDB.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your credentials
```

## Scripts

| Script | Description |
|---|---|
| `sla_report.py` | Morning SLA report — prints breaches/at-risk tickets, saves to `output/`, optional Telegram notify |
| `export_csv.py` | Export tickets to a pivot-friendly CSV (interactive status picker) |
| `audit.py` | Query Zendesk audit logs for admin actions |
| `populate_active.py` | Populate **remote** `zendesk_active` FalkorDB graph (open + pending) |
| `populate_history.py` | Populate **remote** `zendesk_history` FalkorDB graph (solved + closed) |
| `populate_local.py` | Populate **local** FalkorDB with both active + history graphs |
| `graph_loader.py` | Load a CSV export into a local FalkorDB graph |
| `sla_graph.py` | Fetch last N days of tickets + SLA metrics and load into FalkorDB |

## Usage

```bash
# SLA morning report (print only)
python sla_report.py

# SLA morning report + send to Telegram
python sla_report.py --notify

# Export tickets to CSV (interactive)
python export_csv.py

# Export a specific status directly
python export_csv.py open

# Query audit logs (last 7 days)
python audit.py

# Audit logs — specific filter
python audit.py --source trigger --action create --days 30

# Populate remote graphs
python populate_active.py
python populate_history.py

# Populate local FalkorDB (both graphs)
python populate_local.py

# Load a CSV into local FalkorDB graph
python graph_loader.py output/my_export.csv

# Fetch last 30 days SLA data into FalkorDB
python sla_graph.py --days 30
```

## Configuration

### Environment Variables (`.env`)

| Variable | Description |
|---|---|
| `ZENDESK_SUBDOMAIN` | Your Zendesk subdomain (e.g. `mycompany`) |
| `ZENDESK_EMAIL` | Admin email address |
| `ZENDESK_API_TOKEN` | API token from Zendesk Admin Center |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (optional, for `--notify`) |
| `TELEGRAM_CHAT_ID` | Telegram chat/channel ID (optional) |
| `FALKORDB_LOCAL_HOST` | Local FalkorDB host (default: `localhost`) |
| `FALKORDB_LOCAL_PORT` | Local FalkorDB port (default: `6379`) |
| `FALKORDB_ACTIVE_HOST` | Remote FalkorDB host for active graph |
| `FALKORDB_ACTIVE_PORT` | Remote FalkorDB port for active graph |
| `FALKORDB_ACTIVE_USERNAME` | Remote FalkorDB username for active graph |
| `FALKORDB_ACTIVE_PASSWORD` | Remote FalkorDB password for active graph |
| `FALKORDB_HISTORY_HOST` | Remote FalkorDB host for history graph |
| `FALKORDB_HISTORY_PORT` | Remote FalkorDB port for history graph |
| `FALKORDB_HISTORY_USERNAME` | Remote FalkorDB username for history graph |
| `FALKORDB_HISTORY_PASSWORD` | Remote FalkorDB password for history graph |

### SLA Thresholds (`config.py`)

| Priority | First-reply SLA |
|---|---|
| urgent | 30 minutes |
| high | 1 hour |
| normal | 24 hours |
| low | 3 days |

Edit `config.py` to adjust thresholds or the at-risk warning window (`WARNING_MINUTES`).

## Cron Example

Run the SLA report every morning at 8am:

```
0 8 * * * cd /path/to/zendesk-util && venv/bin/python sla_report.py --notify >> output/cron.log 2>&1
```
