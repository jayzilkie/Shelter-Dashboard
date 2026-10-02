"""
Thin client for the Mortgage Automator Lender API.

Auth, per Mortgage Automator's docs:
  - Every request needs two headers: ACCOUNT-ID and API-AUTH
  - API-AUTH = SHA1("{account_id}-{api_key}-{entity}/{action}-{timestamp}")
  - timestamp format is YYYY-MM-DD-HH (UTC, hour is 24h)

NOTE on the entity/action combo: MA's docs describe it as an
"entity/action combo" but don't spell out the exact separator with a
real-world example. This client joins them as "entity/action" (e.g.
"loans/list"). If your account rejects requests with an auth error,
that's the first thing to double check with MA support - you may need
to change ENTITY_ACTION_SEPARATOR below to "-" or something else.
"""
import hashlib
import time
from datetime import datetime, timezone

import requests

ENTITY_ACTION_SEPARATOR = "/"


class MortgageAutomatorError(Exception):
    pass


class MortgageAutomatorClient:
    def __init__(self, endpoint: str, account_id: str, api_key: str, timeout: int = 90):
        if not endpoint or not account_id or not api_key:
            raise MortgageAutomatorError(
                "Missing MA_ENDPOINT, MA_ACCOUNT_ID or MA_API_KEY - check your .env file."
            )
        self.endpoint = endpoint.rstrip("/")
        self.account_id = str(account_id)
        self.api_key = api_key
        self.timeout = timeout

    def _timestamp(self) -> str:
        now = datetime.now(timezone.utc)
        return f"{now.year:04d}-{now.month:02d}-{now.day:02d}-{now.hour:02d}"

    def _auth_token(self, entity: str, action: str) -> str:
        combo = f"{entity}{ENTITY_ACTION_SEPARATOR}{action}"
        raw = f"{self.account_id}-{self.api_key}-{combo}-{self._timestamp()}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def call(self, entity: str, action: str, payload: dict | None = None, version: int | None = None,
             params: dict | None = None) -> dict:
        """Call {ENDPOINT}/{entity}/{action} (or /v{version}/{entity}/{action}).

        `payload` is sent as the JSON body. `params` is sent as URL query
        parameters - MA's docs put filtering/sorting/pagination fields
        (like `start`) in the URL, not the JSON body.
        """
        payload = payload or {}
        path = f"{entity}/{action}" if version is None else f"v{version}/{entity}/{action}"
        url = f"{self.endpoint}/{path}"

        headers = {
            "Content-Type": "application/json",
            "ACCOUNT-ID": self.account_id,
            "API-AUTH": self._auth_token(entity, action),
        }

        resp = None
        last_exc = None
        for attempt in range(4):
            try:
                resp = requests.post(url, json=payload, headers=headers, params=params, timeout=self.timeout)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_exc = e
                time.sleep(2 * (attempt + 1))
                continue

            if resp.status_code in (502, 503, 504):
                wait_s = 10 * (attempt + 1)
                print(f"[MA] Server timeout ({resp.status_code}) on {entity}/{action}, waiting {wait_s}s before retry {attempt + 1}/4...")
                last_exc = MortgageAutomatorError(f"HTTP {resp.status_code} from server")
                time.sleep(wait_s)
                resp = None
                continue

            if resp.status_code == 429:
                # Rate limited. Cloudflare sometimes sends Retry-After: 0
                # while still actively blocking, so enforce a real minimum
                # wait that grows with each attempt rather than trusting 0.
                header_wait = int(resp.headers.get("Retry-After", 0) or 0)
                wait_s = max(header_wait, 20 * (attempt + 1))
                print(f"[MA] Rate limited (429) on {entity}/{action}, waiting {wait_s}s before retry {attempt + 1}/4...")
                last_exc = MortgageAutomatorError(f"Rate limited (429), waited {wait_s}s")
                time.sleep(wait_s)
                resp = None
                continue
            break

        if resp is None:
            raise MortgageAutomatorError(
                f"{entity}/{action} failed after 4 attempts (timeouts/rate limits/server errors): {last_exc}"
            )
        if resp.status_code != 200:
            raise MortgageAutomatorError(
                f"{entity}/{action} failed: HTTP {resp.status_code} - {resp.text[:500]}"
            )
        try:
            return resp.json()
        except ValueError:
            raise MortgageAutomatorError(f"{entity}/{action} returned non-JSON: {resp.text[:500]}")

    # Different ways MA's API might accept a status filter. Each one is tried
    # until MA actually respects it. ("url" = query string, "body" = JSON.)
    STATUS_FILTER_STYLES = [
        ("url", "status"),
        ("url", "status[]"),
        ("url", "filter[status]"),
        ("url", "filters[status]"),
        ("url", "loan_status"),
        ("body", "status"),
        ("body", "filters"),
        ("body", "filter"),
    ]

    def _filter_args(self, style, status_id):
        """Return (extra_url_params, extra_body) for one filter style."""
        where, key = style
        if where == "url":
            return {key: status_id}, {}
        if key in ("filters", "filter"):
            return {}, {key: {"status": status_id}}
        return {}, {key: status_id}

    def list_all_loans(self, page_size_hint: int = 50, max_pages: int = 250, extra_filters: dict | None = None,
                        on_progress=None, statuses=None):
        """Paginate through /loans/list using the 'next' cursor.

        statuses: optional list of status IDs (e.g. [0, 1] for In Progress +
        Funded). The script finds a filter style MA respects and downloads
        only those loans. If none work, it downloads everything.
        """
        self._whitelist = ["loan_id", "loan_title", "status", "substatus", "closing_date", "mortgage", "property"]
        self._total_so_far = 0
        self._page_counter = 0

        style = self.find_status_filter(statuses[0]) if statuses else None
        if style:
            print(f"[MA] status filter works using {style} - downloading only statuses {statuses}")
            loans = []
            for st in statuses:
                url_extra, body_extra = self._filter_args(style, st)
                params = dict(extra_filters or {})
                params.update(url_extra)
                loans.extend(self._fetch_pages(params, max_pages, on_progress, body_extra))
            print(f"[MA] done: fetched {len(loans)} loans total.")
            return loans

        if statuses:
            print("[MA] no status filter style worked - downloading all loans instead (dashboard will still filter).")
        loans = self._fetch_pages(extra_filters, max_pages, on_progress)
        print(f"[MA] done: fetched {len(loans)} loans total.")
        return loans

    def find_status_filter(self, status_id):
        """Try each filter style and return the first one MA actually applies."""
        wl = {"response_field_whitelist": ["loan_id", "status"]}
        try:
            base = self.call("loans", "list", payload=wl, params={"start": 0})
        except MortgageAutomatorError as e:
            print(f"[MA] couldn't get unfiltered count: {e}")
            return None
        total = base.get("count") or 0
        print(f"[MA] testing status filters (unfiltered count = {total})...")

        for style in self.STATUS_FILTER_STYLES:
            time.sleep(3)
            url_extra, body_extra = self._filter_args(style, status_id)
            try:
                data = self.call("loans", "list", payload={**wl, **body_extra},
                                 params={"start": 0, **url_extra})
            except MortgageAutomatorError as e:
                print(f"[MA]   {style}: error ({str(e)[:80]})")
                continue
            batch = (data.get("result") or {}).get("loans", [])
            count = data.get("count") or 0
            try:
                all_match = bool(batch) and all(int(l.get("status")) == int(status_id) for l in batch)
            except (TypeError, ValueError):
                all_match = False
            if all_match and (not total or count < total):
                print(f"[MA]   {style}: WORKS (count {count})")
                return style
            print(f"[MA]   {style}: ignored")
        return None

    def _fetch_pages(self, extra_filters, max_pages, on_progress, extra_body=None):
        extra_body = extra_body or {}
        loans = []
        start = 0
        for page_num in range(max_pages):
            params = {"start": start}
            if extra_filters:
                params.update(extra_filters)
            data = self.call("loans", "list", payload={"response_field_whitelist": self._whitelist, **extra_body}, params=params)
            batch = (data.get("result") or {}).get("loans", [])

            # Some accounts return 0 loans when the whitelist includes a field
            # they don't accept. Asking for everything instead times out (504)
            # on big accounts, so find and drop only the bad field(s).
            if not batch and page_num == 0 and "mortgage" in self._whitelist:
                print("[MA] got 0 loans on page 1 - checking which field is causing it...")
                self._whitelist = self.find_working_fields(self._whitelist, params)
                print(f"[MA] using fields: {self._whitelist}")
                data = self.call("loans", "list", payload={"response_field_whitelist": self._whitelist, **extra_body}, params=params)
                batch = (data.get("result") or {}).get("loans", [])

            loans.extend(batch)
            self._total_so_far += len(batch)
            self._page_counter += 1

            next_start = data.get("next")
            print(f"[MA] page {self._page_counter}: got {len(batch)} loans (total so far: {self._total_so_far}), next={next_start}")
            if on_progress:
                on_progress(self._total_so_far, self._page_counter)

            if next_start is None:
                break
            if next_start == start:
                print(f"[MA] warning: 'next' ({next_start}) didn't advance past 'start' ({start}) - stopping to avoid a loop.")
                break
            start = next_start
            time.sleep(3)
        return loans

    def find_working_fields(self, fields, params):
        """Test each field on its own and keep only the ones that return loans."""
        working = ["loan_id"]
        for field in fields:
            if field == "loan_id":
                continue
            time.sleep(3)
            data = self.call("loans", "list",
                             payload={"response_field_whitelist": ["loan_id", field]},
                             params=params)
            got = len((data.get("result") or {}).get("loans", []))
            if got:
                print(f"[MA]   {field}: OK")
                working.append(field)
            else:
                print(f"[MA]   {field}: returns 0 loans - REMOVED")
        return working

    def get_loan(self, loan_id: int) -> dict:
        data = self.call("loans", "get", {"loan_id": loan_id})
        return data.get("result", data)

    def get_loan_detail_extras(self, loan_ids: list, on_progress=None) -> dict:
        """Two fields that only come back on a full per-loan /loans/get call,
        never on the bulk /loans/list (asking for either there makes MA's
        API return 0 loans, same failure mode as any field it rejects in
        bulk) - fetched together here since they're both in the same
        response, one call per loan:

          - existing_mortgages: prior liens already registered on THIS
            loan's own property, from OTHER lenders.
          - other_properties: separate ADDITIONAL properties pledged as
            extra collateral on this loan (Mortgage Automator's "Other
            Properties" / "Blanket" tab). Confirmed on loan 1066682
            (Lacharity, a straight 1st position loan): its own property is
            worth $570,000 against a $785,000 balance (137.7% on its own),
            but it also has a blanket-ticked second property worth $585,000
            pledged alongside it - the real combined LTV is 785,000 /
            (570,000+585,000) = 67.97%, matching Mortgage Automator's own
            report exactly. This is NOT limited to 2nd/3rd position loans -
            a straight 1st can carry blanket collateral too - so (unlike
            the old existing_mortgages-only fetch) this is called for
            EVERY active loan, not just subordinate-position ones.

        Returns {loan_id: {"existing_mortgages": [...], "other_properties": [...]}}.
        A single loan_id failing (timeout, bad id, etc.) is skipped rather
        than aborting the whole batch - better to ship correct data for the
        loans we could reach than to fail everyone's LTV over one bad
        record.
        """
        out = {}
        for i, loan_id in enumerate(loan_ids):
            try:
                full = self.call("loans", "get", payload={"loan_id": loan_id}, params={"loan_id": loan_id})
                loan = full.get("result", full)
                if isinstance(loan, list):
                    loan = loan[0] if loan else {}
                if isinstance(loan, dict) and isinstance(loan.get("loan"), dict):
                    loan = loan["loan"]
                existing = loan.get("existing_mortgages") if isinstance(loan, dict) else None
                other_props = loan.get("other_properties") if isinstance(loan, dict) else None
                out[loan_id] = {
                    "existing_mortgages": existing if isinstance(existing, list) else [],
                    "other_properties": other_props if isinstance(other_props, list) else [],
                }
            except MortgageAutomatorError as e:
                print(f"[MA] couldn't fetch loan detail for loan {loan_id}: {e}")
            if on_progress:
                on_progress(i + 1, len(loan_ids))
            time.sleep(1.2)
        return out