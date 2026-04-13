import time

import requests
from requests.auth import HTTPBasicAuth

VALID_STATUSES = ("open", "pending", "solved", "all")


class ZendeskClient:
    """Client for interacting with the Zendesk REST API v2."""

    def __init__(self, subdomain: str, email: str, api_token: str):
        self.base_url = f"https://{subdomain}.zendesk.com/api/v2"
        self.auth = HTTPBasicAuth(f"{email}/token", api_token)
        self.session = requests.Session()
        self.session.auth = self.auth
        self.session.headers.update({"Content-Type": "application/json"})

    def get_users(self, user_ids: list[int]) -> dict[int, dict]:
        """Batch-fetch users by ID. Returns {user_id: user_dict}."""
        if not user_ids:
            return {}

        users = {}
        # show_many accepts up to 100 IDs per request
        for i in range(0, len(user_ids), 100):
            batch = user_ids[i : i + 100]
            ids_param = ",".join(str(uid) for uid in batch)
            url = f"{self.base_url}/users/show_many.json?ids={ids_param}"
            response = self.session.get(url)
            response.raise_for_status()
            for user in response.json().get("users", []):
                users[user["id"]] = user
            time.sleep(0.5)

        return users

    def get_organizations(self, org_ids: list[int]) -> dict[int, dict]:
        """Batch-fetch organizations by ID. Returns {org_id: org_dict}."""
        if not org_ids:
            return {}

        orgs = {}
        for i in range(0, len(org_ids), 100):
            batch = org_ids[i : i + 100]
            ids_param = ",".join(str(oid) for oid in batch)
            url = f"{self.base_url}/organizations/show_many.json?ids={ids_param}"
            response = self.session.get(url)
            response.raise_for_status()
            for org in response.json().get("organizations", []):
                orgs[org["id"]] = org
            time.sleep(0.5)

        return orgs

    def enrich_tickets(self, tickets: list[dict]) -> list[dict]:
        """Add requester_email, requester_name, assignee_name, and organization_name to tickets."""
        user_ids = set()
        org_ids = set()
        for t in tickets:
            if t.get("requester_id"):
                user_ids.add(t["requester_id"])
            if t.get("assignee_id"):
                user_ids.add(t["assignee_id"])
            if t.get("organization_id"):
                org_ids.add(t["organization_id"])

        print(f"Resolving {len(user_ids)} users and {len(org_ids)} organizations...")
        users = self.get_users(list(user_ids))
        orgs = self.get_organizations(list(org_ids))

        for t in tickets:
            requester = users.get(t.get("requester_id"), {})
            assignee = users.get(t.get("assignee_id"), {})
            org = orgs.get(t.get("organization_id"), {})

            t["requester_name"] = requester.get("name", "")
            t["requester_email"] = requester.get("email", "")
            t["assignee_name"] = assignee.get("name", "")
            t["organization_name"] = org.get("name", "")

        return tickets

    def get_all_ticket_metrics(self) -> dict:
        """Fetch all ticket metrics in bulk. Returns {ticket_id: metric_dict}.
        Uses /api/v2/ticket_metrics (paginated) — no per-ticket calls needed.
        """
        url = f"{self.base_url}/ticket_metrics.json"
        params = {"per_page": 100}
        metrics = {}
        page = 1

        while url:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            for ms in data.get("ticket_metrics", []):
                metrics[ms["ticket_id"]] = ms
            print(f"  Metrics page {page}: {len(data.get('ticket_metrics', []))} records "
                  f"({len(metrics)}/{data.get('count', '?')} total)")
            url = data.get("next_page")
            params = None
            page += 1
            if url:
                time.sleep(0.3)

        print(f"Loaded metrics for {len(metrics)} tickets.")
        return metrics

    def get_tickets_with_metrics(self, status: str = "open",
                                  days: int = None) -> list[dict]:
        """Fetch tickets with metric_sets sideloaded (no extra API calls).

        Args:
            status: 'open', 'pending', 'solved', 'closed', or 'all'
            days:   if set, restrict to tickets created in last N days
        """
        if days:
            from datetime import datetime, timedelta, timezone
            since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
            query = f"type:ticket created>{since}"
            if status != "all":
                query += f" status:{status}"
        else:
            query = "type:ticket" if status == "all" else f"type:ticket status:{status}"

        url = f"{self.base_url}/search.json"
        params = {
            "query": query,
            "sort_by": "created_at",
            "sort_order": "asc",
            "per_page": 100,
            "include": "metric_sets",
        }

        all_tickets = []
        metrics_map = {}
        page = 1

        while url:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            results = data.get("results", [])
            all_tickets.extend(results)

            # metric_sets come back as a sideload list keyed by ticket_id
            for ms in data.get("metric_sets", []):
                metrics_map[ms["ticket_id"]] = ms

            total = data.get("count", "?")
            print(f"  Page {page}: {len(results)} tickets ({len(all_tickets)}/{total})")
            url = data.get("next_page")
            params = None
            page += 1
            if url:
                time.sleep(0.5)

        # Attach metric_set directly onto each ticket dict
        for t in all_tickets:
            t["metric_set"] = metrics_map.get(t["id"], {})

        print(f"Fetched {len(all_tickets)} tickets (status={status}).")
        return all_tickets

    def get_tickets_last_n_days(self, days: int = 30) -> list[dict]:
        """Fetch all tickets created in the last N days."""
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        query = f"type:ticket created>{since}"

        url = f"{self.base_url}/search.json"
        params = {
            "query": query,
            "sort_by": "created_at",
            "sort_order": "asc",
            "per_page": 100,
        }

        all_tickets = []
        page = 1
        while url:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])
            all_tickets.extend(results)
            total = data.get("count", "?")
            print(f"  Page {page}: fetched {len(results)} tickets ({len(all_tickets)}/{total} total)")
            url = data.get("next_page")
            params = None
            page += 1
            if url:
                time.sleep(0.5)

        print(f"Fetched {len(all_tickets)} tickets from the last {days} days.")
        return all_tickets

    def get_ticket_comments(self, ticket_id: int) -> list[dict]:
        """Fetch all comments for a ticket."""
        url = f"{self.base_url}/tickets/{ticket_id}/comments.json"
        params = {"per_page": 100}
        all_comments = []
        while url:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            all_comments.extend(data.get("comments", []))
            url = data.get("next_page")
            params = None
        return all_comments

    def get_user_roles(self, user_ids: list[int]) -> dict[int, str]:
        """Batch-fetch user roles by ID. Returns {user_id: role}."""
        users = self.get_users(user_ids)
        return {uid: u.get("role", "end-user") for uid, u in users.items()}

    def get_active_tickets(self) -> list[dict]:
        """Fetch all open + pending tickets via search API (deduped)."""
        tickets = []
        seen: set[int] = set()
        for status in ("open", "pending"):
            url = f"{self.base_url}/search.json"
            params: dict = {"query": f"type:ticket status:{status}", "per_page": 100}
            while url:
                response = self.session.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                for t in data.get("results", []):
                    if t["id"] not in seen:
                        seen.add(t["id"])
                        tickets.append(t)
                url = data.get("next_page")
                params = None
        return tickets

    def get_bulk_metrics(self) -> dict[int, dict]:
        """Return dict of ticket_id → metric_set for all tickets."""
        metrics: dict[int, dict] = {}
        url = f"{self.base_url}/ticket_metrics.json"
        params: dict = {"per_page": 100}
        while url:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            for m in data.get("ticket_metrics", []):
                metrics[m["ticket_id"]] = m
            url = data.get("next_page")
            params = None
        return metrics

    def get_tickets(self, status: str = "open") -> list[dict]:
        """Fetch all tickets matching the given status, with automatic pagination.

        Args:
            status: One of 'open', 'pending', 'solved', or 'all'.

        Returns:
            List of ticket dictionaries.
        """
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status '{status}'. Must be one of: {VALID_STATUSES}")

        if status == "all":
            query = "type:ticket"
        else:
            query = f"type:ticket status:{status}"

        url = f"{self.base_url}/search.json"
        params = {
            "query": query,
            "sort_by": "created_at",
            "sort_order": "desc",
            "per_page": 100,
        }

        all_tickets = []
        page = 1

        while url:
            response = self.session.get(url, params=params)
            response.raise_for_status()

            data = response.json()
            results = data.get("results", [])
            all_tickets.extend(results)

            total = data.get("count", "?")
            print(f"  Page {page}: fetched {len(results)} tickets ({len(all_tickets)}/{total} total)")

            url = data.get("next_page")
            params = None  # next_page URL already includes query params
            page += 1

            # Respect Zendesk rate limits
            if url:
                time.sleep(0.5)

        print(f"Fetched {len(all_tickets)} {status} tickets total.")
        return all_tickets
