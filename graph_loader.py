"""Load unsolved Zendesk tickets from CSV into a FalkorDB graph.

Graph model:
  (Ticket)-[:FROM_ORG]->(Organization)
  (Ticket)-[:REQUESTED_BY]->(Requester)
  (Ticket)-[:ASSIGNED_TO]->(Assignee)
  (Ticket)-[:IN_CATEGORY]->(Category)
  (Requester)-[:BELONGS_TO]->(Organization)

Usage:
  python graph_loader.py                            # loads latest dashboard CSV
  python graph_loader.py output/my_tickets.csv      # loads a specific CSV
"""

import csv
import sys
import glob
import os

from falkordb import FalkorDB

GRAPH_NAME = "zendesk_tickets"


def find_latest_csv() -> str:
    """Find the most recent dashboard CSV in the output directory."""
    pattern = os.path.join("output", "dashboard_unsolved_tickets_*.csv")
    files = sorted(glob.glob(pattern))
    if not files:
        print("No dashboard CSV found. Run: python main.py")
        sys.exit(1)
    return files[-1]


def load_tickets(csv_path: str) -> list[dict]:
    with open(csv_path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_graph(tickets: list[dict], host: str = "localhost", port: int = 6379):
    db = FalkorDB(host=host, port=port)
    g = db.select_graph(GRAPH_NAME)

    # Clear previous data
    print(f"Clearing graph '{GRAPH_NAME}'...")
    g.query("MATCH (n) DETACH DELETE n")

    # Create indexes for query performance
    try:
        g.query("CREATE INDEX FOR (t:Ticket) ON (t.ticket_id)")
        g.query("CREATE INDEX FOR (o:Organization) ON (o.name)")
        g.query("CREATE INDEX FOR (c:Category) ON (c.name)")
    except Exception:
        pass  # indexes may already exist

    print(f"Loading {len(tickets)} tickets into graph...")

    # Track created nodes to avoid duplicates
    orgs_created = set()
    assignees_created = set()
    categories_created = set()

    for t in tickets:
        ticket_id = int(t["id"])
        org_name = t.get("organization_name", "(no org)")
        assignee = t.get("assignee_name", "")
        requester = t.get("requester_name", "")
        requester_email = t.get("requester_email", "")
        category = t.get("category", "Other")
        age = int(t["age_days"]) if t.get("age_days") else 0
        days_over = int(t["days_over_sla"]) if t.get("days_over_sla") else 0
        sla_target = int(t["sla_target_days"]) if t.get("sla_target_days") else 7

        # Create Ticket node
        g.query(
            "CREATE (:Ticket {"
            "  ticket_id: $id, subject: $subject, status: $status,"
            "  priority: $priority, sla_status: $sla, sla_target_days: $sla_target,"
            "  channel: $channel, age_days: $age, age_bucket: $bucket,"
            "  days_over_sla: $over, created_date: $created,"
            "  created_week: $week, created_year_month: $ym"
            "})",
            params={
                "id": ticket_id,
                "subject": t.get("subject", ""),
                "status": t.get("status", ""),
                "priority": t.get("priority", ""),
                "sla": t.get("sla_status", ""),
                "sla_target": sla_target,
                "channel": t.get("channel", ""),
                "age": age,
                "bucket": t.get("age_bucket", ""),
                "over": days_over,
                "created": t.get("created_date", ""),
                "week": t.get("created_week", ""),
                "ym": t.get("created_year_month", ""),
            },
        )

        # Organization
        if org_name not in orgs_created:
            g.query(
                "CREATE (:Organization {name: $name})",
                params={"name": org_name},
            )
            orgs_created.add(org_name)

        g.query(
            "MATCH (t:Ticket {ticket_id: $id}), (o:Organization {name: $org}) "
            "CREATE (t)-[:FROM_ORG]->(o)",
            params={"id": ticket_id, "org": org_name},
        )

        # Requester
        g.query(
            "CREATE (:Requester {name: $name, email: $email})",
            params={"name": requester, "email": requester_email},
        )
        g.query(
            "MATCH (t:Ticket {ticket_id: $id}), (r:Requester {email: $email}) "
            "CREATE (t)-[:REQUESTED_BY]->(r)",
            params={"id": ticket_id, "email": requester_email},
        )
        # Link requester to org
        if org_name != "(no org)":
            g.query(
                "MATCH (r:Requester {email: $email}), (o:Organization {name: $org}) "
                "MERGE (r)-[:BELONGS_TO]->(o)",
                params={"email": requester_email, "org": org_name},
            )

        # Assignee
        if assignee:
            if assignee not in assignees_created:
                g.query(
                    "CREATE (:Assignee {name: $name})",
                    params={"name": assignee},
                )
                assignees_created.add(assignee)

            g.query(
                "MATCH (t:Ticket {ticket_id: $id}), (a:Assignee {name: $name}) "
                "CREATE (t)-[:ASSIGNED_TO]->(a)",
                params={"id": ticket_id, "name": assignee},
            )

        # Category
        if category not in categories_created:
            g.query(
                "CREATE (:Category {name: $name})",
                params={"name": category},
            )
            categories_created.add(category)

        g.query(
            "MATCH (t:Ticket {ticket_id: $id}), (c:Category {name: $name}) "
            "CREATE (t)-[:IN_CATEGORY]->(c)",
            params={"id": ticket_id, "name": category},
        )

    print(f"Graph '{GRAPH_NAME}' loaded: {len(tickets)} tickets, "
          f"{len(orgs_created)} orgs, {len(assignees_created)} assignees, "
          f"{len(categories_created)} categories")

    run_sample_queries(g)


def run_sample_queries(g):
    print("\n" + "=" * 60)
    print("SAMPLE QUERIES")
    print("=" * 60)

    # 1. SLA breaches by org
    print("\n--- SLA Breaches by Organization ---")
    result = g.query(
        "MATCH (t:Ticket)-[:FROM_ORG]->(o:Organization) "
        "WHERE t.sla_status = 'Breached' "
        "RETURN o.name AS org, COUNT(t) AS breached, MAX(t.days_over_sla) AS worst_overage "
        "ORDER BY breached DESC"
    )
    for row in result.result_set:
        print(f"  {row[0]}: {row[1]} breached (worst: {row[2]}d over SLA)")

    # 2. Tickets by category and channel
    print("\n--- Tickets by Category × Channel ---")
    result = g.query(
        "MATCH (t:Ticket)-[:IN_CATEGORY]->(c:Category) "
        "RETURN c.name AS category, t.channel AS channel, COUNT(t) AS count "
        "ORDER BY count DESC"
    )
    for row in result.result_set:
        print(f"  {row[0]} ({row[1]}): {row[2]}")

    # 3. Orgs with most tickets + avg age
    print("\n--- Top Organizations by Ticket Volume ---")
    result = g.query(
        "MATCH (t:Ticket)-[:FROM_ORG]->(o:Organization) "
        "RETURN o.name AS org, COUNT(t) AS tickets, "
        "  ROUND(AVG(t.age_days)) AS avg_age, "
        "  COLLECT(t.sla_status) AS sla_statuses "
        "ORDER BY tickets DESC LIMIT 10"
    )
    for row in result.result_set:
        print(f"  {row[0]}: {row[1]} tickets, avg {row[2]}d, SLA: {row[3]}")

    # 4. Hotspot: orgs where requesters filed multiple tickets
    print("\n--- Repeat Requesters (multi-ticket) ---")
    result = g.query(
        "MATCH (t:Ticket)-[:REQUESTED_BY]->(r:Requester)-[:BELONGS_TO]->(o:Organization) "
        "WITH r, o, COUNT(t) AS tickets "
        "WHERE tickets > 1 "
        "RETURN r.name AS requester, r.email AS email, o.name AS org, tickets "
        "ORDER BY tickets DESC"
    )
    for row in result.result_set:
        print(f"  {row[0]} ({row[2]}): {row[3]} tickets")

    # 5. SLA risk path: high priority + old
    print("\n--- Critical Path: High Priority Breached ---")
    result = g.query(
        "MATCH (t:Ticket)-[:FROM_ORG]->(o:Organization), "
        "      (t)-[:ASSIGNED_TO]->(a:Assignee) "
        "WHERE t.priority = 'high' AND t.sla_status = 'Breached' "
        "RETURN t.ticket_id AS id, t.subject AS subject, o.name AS org, "
        "       a.name AS assignee, t.age_days AS age, t.days_over_sla AS over_sla "
        "ORDER BY over_sla DESC"
    )
    for row in result.result_set:
        print(f"  #{row[0]} ({row[3]}) {row[2]} — {row[4]}d old, {row[5]}d over SLA")
        print(f"    {row[1][:70]}")

    print("\n" + "=" * 60)
    print("Run custom queries with: python -c \"")
    print("  from falkordb import FalkorDB")
    print("  g = FalkorDB().select_graph('zendesk_tickets')")
    print(f"  result = g.query('MATCH (t:Ticket) RETURN COUNT(t)')\"")
    print("=" * 60)


if __name__ == "__main__":
    csv_path = sys.argv[1] if len(sys.argv) > 1 else find_latest_csv()
    print(f"Loading from: {csv_path}")
    tickets = load_tickets(csv_path)
    build_graph(tickets)
