import datetime
import os
import threading
import time
import webbrowser

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from ma_client import MortgageAutomatorClient, MortgageAutomatorError
from data import build_dashboard, normalize_loan, is_self_referential_lien, is_real_prior_lien, to_int

load_dotenv()

MA_ENDPOINT = os.getenv("MA_ENDPOINT", "")
MA_ACCOUNT_ID = os.getenv("MA_ACCOUNT_ID", "")
MA_API_KEY = os.getenv("MA_API_KEY", "")
PORT = int(os.getenv("PORT", "5000"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "90"))
# Off by default everywhere. The /debug/* routes dump raw, unfiltered loan
# records (and one even shows the account ID + part of the API auth token) -
# fine on your own machine, never fine on a dashboard anyone on the internet
# can reach. Set ENABLE_DEBUG_ROUTES=1 in your LOCAL .env if you still want
# them while developing; leave it unset wherever this is deployed for the
# team.
ENABLE_DEBUG_ROUTES = os.getenv("ENABLE_DEBUG_ROUTES", "0") == "1"
# Off by default too - this only matters for a hosted deployment, where
# nobody's sitting there to click "Refresh" after the server cold-starts.
# Set AUTO_REFRESH_ON_START=1 in the HOSTED environment's variables (not
# your local .env) so the dashboard starts pulling real data the moment the
# server comes up, instead of showing "no data yet" until someone visits
# and clicks Refresh.
AUTO_REFRESH_ON_START = os.getenv("AUTO_REFRESH_ON_START", "0") == "1"

app = Flask(__name__)

# In-memory cache + progress state. A background thread does the actual
# fetching so the browser can poll /api/progress and show a live loading
# bar instead of the page just hanging until everything's done.
_cache = {"raw_loans": None, "dashboard": None, "error": None, "fetched_at": None}
_progress = {"running": False, "loans_fetched": 0, "page": 0, "message": "Not started"}
_lock = threading.Lock()


def get_client() -> MortgageAutomatorClient:
    return MortgageAutomatorClient(MA_ENDPOINT, MA_ACCOUNT_ID, MA_API_KEY, timeout=REQUEST_TIMEOUT)


def _run_refresh():
    def on_progress(loans_so_far, page_num):
        _progress["loans_fetched"] = loans_so_far
        _progress["page"] = page_num
        _progress["message"] = f"Fetched {loans_so_far} loans so far (page {page_num})..."

    try:
        client = get_client()
        raw_loans = client.list_all_loans(on_progress=on_progress, statuses=[0, 1])  # In Progress + Funded

        # A true (combined) LTV needs two things the bulk /loans/list call
        # never returns (confirmed: asking for either there makes the API
        # return 0 loans, same failure as any field it rejects in bulk) -
        # only available per-loan via /loans/get:
        #   - existing_mortgages: prior liens from OTHER lenders already on
        #     this loan's own property (2nd/3rd position math).
        #   - other_properties: separate ADDITIONAL properties pledged as
        #     extra collateral ("Blanket" in MA's UI) - confirmed this is
        #     NOT limited to subordinate positions (loan 1066682 is a
        #     straight 1st with a blanket-ticked second property; without
        #     it, its LTV reads 137.7% instead of the real 67.97%) - so
        #     every active loan needs this per-loan fetch, not just
        #     subordinate-position ones.
        all_loan_ids = [l.get("loan_id") for l in raw_loans if l.get("loan_id") is not None]

        if all_loan_ids:
            def on_detail_progress(done, total):
                _progress["message"] = f"Fetched {len(raw_loans)} loans. Checking prior liens & blanket collateral for LTV ({done}/{total} loans)..."
            detail_by_id = client.get_loan_detail_extras(all_loan_ids, on_progress=on_detail_progress)
            sample_lien = next((v["existing_mortgages"][0] for v in detail_by_id.values() if v["existing_mortgages"]), None)
            if sample_lien:
                print(f"[MA] sample existing_mortgages entry (to confirm the balance field name): {sample_lien}")
            sample_op = next((v["other_properties"][0] for v in detail_by_id.values() if v["other_properties"]), None)
            if sample_op:
                print(f"[MA] sample other_properties entry (to confirm the value/blanket field names): {sample_op}")
            for l in raw_loans:
                lid = l.get("loan_id")
                if lid in detail_by_id:
                    l["existing_mortgages"] = detail_by_id[lid]["existing_mortgages"]
                    l["other_properties"] = detail_by_id[lid]["other_properties"]

        _cache["raw_loans"] = raw_loans
        _cache["dashboard"] = build_dashboard(raw_loans)
        _cache["error"] = None
        _cache["fetched_at"] = datetime.datetime.now().strftime("%b %d, %Y at %I:%M %p")
        shown = _cache["dashboard"]["kpis"]["total_loans"]
        _progress["message"] = f"Done - downloaded {len(raw_loans)} loans, showing {shown} (In Progress + Funded)."
        print(f"[MA] downloaded {len(raw_loans)} loans, dashboard is showing {shown} (In Progress + Funded only)")
    except MortgageAutomatorError as e:
        _cache["error"] = str(e)
        _progress["message"] = f"Error: {e}"
    except Exception as e:  # network errors, bad endpoint, etc.
        _cache["error"] = f"Unexpected error: {e}"
        _progress["message"] = f"Error: {e}"
    finally:
        _progress["running"] = False


def start_refresh_if_needed():
    """Kick off a background refresh if one isn't already running."""
    with _lock:
        if _progress["running"]:
            return False
        _progress["running"] = True
        _progress["loans_fetched"] = 0
        _progress["page"] = 0
        _progress["message"] = "Starting..."
    threading.Thread(target=_run_refresh, daemon=True).start()
    return True


@app.before_request
def _block_debug_routes_unless_enabled():
    if request.path.startswith("/debug/") and not ENABLE_DEBUG_ROUTES:
        return jsonify({"error": "debug routes are disabled on this deployment"}), 404


if AUTO_REFRESH_ON_START:
    start_refresh_if_needed()


@app.route("/")
def dashboard():
    if _cache["dashboard"] is None and _cache["error"] is None:
        start_refresh_if_needed()
    return render_template(
        "dashboard.html",
        data=_cache["dashboard"],
        error=_cache["error"],
        fetched_at=_cache["fetched_at"],
        loading=_progress["running"] or (_cache["dashboard"] is None and _cache["error"] is None),
    )


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    started = start_refresh_if_needed()
    return jsonify({"started": started, "progress": _progress})


@app.route("/api/progress")
def api_progress():
    return jsonify({
        "running": _progress["running"],
        "loans_fetched": _progress["loans_fetched"],
        "page": _progress["page"],
        "message": _progress["message"],
        "done": (not _progress["running"]) and (_cache["dashboard"] is not None or _cache["error"] is not None),
        "error": _cache["error"],
    })


@app.route("/debug/sample-loan")
def debug_sample_loan():
    """One raw loan record, straight from the API (first page only), to help map fields."""
    try:
        client = get_client()
        raw_loans = client.list_all_loans(max_pages=1)
    except MortgageAutomatorError as e:
        return jsonify({"error": str(e)})
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"})
    if not raw_loans:
        return jsonify({"error": "No loans returned."})
    return jsonify(raw_loans[0])


@app.route("/debug/raw-list")
def debug_raw_list():
    """
    The exact, unprocessed JSON that comes back from one /loans/list call,
    plus what we sent, so we can see exactly what MA's API is saying
    without anything being extracted or filtered out first.
    """
    try:
        client = get_client()
        headers_preview = {
            "url": f"{client.endpoint}/loans/list",
            "ACCOUNT-ID": client.account_id,
            "API-AUTH_first_10_chars": client._auth_token("loans", "list")[:10] + "...",
            "timestamp_used": client._timestamp(),
        }
        raw_response = client.call(
            "loans", "list", payload={"response_field_whitelist": ["loan_id", "loan_title", "status"]},
            params={"start": 0},
        )
        return jsonify({"request_info": headers_preview, "raw_response": raw_response})
    except MortgageAutomatorError as e:
        return jsonify({"error": str(e)})
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"})


@app.route("/debug/test-fields")
def debug_test_fields():
    """Ask for each field on its own to see which one makes the API return 0 loans."""
    fields = ["loan_id", "loan_title", "status", "substatus", "closing_date", "mortgage", "property"]
    results = {}
    try:
        client = get_client()
        for field in fields:
            data = client.call("loans", "list",
                               payload={"response_field_whitelist": ["loan_id", field]},
                               params={"start": 0})
            got = len((data.get("result") or {}).get("loans", []))
            results[field] = f"{got} loans returned" + ("" if got else "  <-- PROBLEM")
            time.sleep(1.2)
    except MortgageAutomatorError as e:
        results["error"] = str(e)
    except Exception as e:
        results["error"] = f"Unexpected error: {e}"
    return jsonify(results)


@app.route("/debug/one-loan")
def debug_one_loan():
    """Pull ONE full loan (no field list) and show how its fields are named,
    so we can find where this account keeps the mortgage info."""
    try:
        client = get_client()
        first = client.call("loans", "list",
                            payload={"response_field_whitelist": ["loan_id", "status"]},
                            params={"start": 0})
        loans = (first.get("result") or {}).get("loans", [])
        # prefer a Funded loan, since those have mortgage data filled in
        pick = next((l for l in loans if str(l.get("status")) == "1"), loans[0] if loans else None)
        if not pick:
            return jsonify({"error": "No loans returned."})
        loan_id = pick["loan_id"]
        time.sleep(1.5)
        full = client.call("loans", "get", payload={"loan_id": loan_id}, params={"loan_id": loan_id})
        loan = full.get("result", full)
        if isinstance(loan, list):
            loan = loan[0] if loan else {}
        if isinstance(loan, dict) and isinstance(loan.get("loan"), dict):
            loan = loan["loan"]
    except MortgageAutomatorError as e:
        return jsonify({"error": str(e)})
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"})

    def describe(v):
        if isinstance(v, dict):
            return f"object with {len(v)} fields"
        if isinstance(v, list):
            return f"list of {len(v)}"
        return type(v).__name__

    top_level = {k: describe(v) for k, v in loan.items()} if isinstance(loan, dict) else {}
    mortgage_like = {k: v for k, v in loan.items() if "mortg" in k.lower()} if isinstance(loan, dict) else {}
    return jsonify({
        "loan_id": loan_id,
        "mortgage_related_fields": mortgage_like,
        "all_top_level_fields": top_level,
    })


@app.route("/debug/many-loans")
def debug_many_loans():
    """The SAME fetch the real dashboard uses (statuses=[0,1], same
    whitelist) - not a fresh unfiltered call - so what we inspect here is
    exactly what build_dashboard() actually sees. Shows the full mortgage
    object (not just a filtered subset) for a couple of 2nd/3rd position
    loans plus a summary line per loan, to see the real existing_mortgages
    shape and find the true current-balance field."""
    try:
        client = get_client()
        raw_loans = client.list_all_loans(max_pages=2, statuses=[0, 1])
    except MortgageAutomatorError as e:
        return jsonify({"error": str(e)})
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"})

    summary = []
    full_examples = []
    for l in raw_loans:
        mortgage = l.get("mortgage") or {}
        prop = l.get("property") or {}
        summary.append({
            "loan_id": l.get("loan_id"),
            "status": l.get("status"),
            "position": mortgage.get("position"),
            "mortgage_amount": mortgage.get("amount"),
            "mortgage_total": mortgage.get("total"),
            "existing_mortgages_present": l.get("existing_mortgages") is not None,
            "existing_mortgages_len": len(l["existing_mortgages"]) if isinstance(l.get("existing_mortgages"), list) else None,
            "property_value": prop.get("value"),
        })
        # grab full, unfiltered detail on the first few 2nd/3rd position loans
        if mortgage.get("position") in (2, 3, "2", "3") and len(full_examples) < 3:
            full_examples.append(l)

    return jsonify({
        "loans_fetched": len(raw_loans),
        "summary": summary[:40],
        "full_examples_2nd_3rd_position": full_examples,
    })


@app.route("/debug/ltv-aggregate")
def debug_ltv_aggregate():
    """Uses the already-fetched, already-lien-enriched loans sitting in
    memory from the last Refresh (no new API calls, instant) to check a
    per-position (First/Second/Third) weighted-average LTV, to compare
    directly against Mortgage Automator's own report (57.65% / 66.80% /
    58.79%), plus how often an existing_mortgages entry got kept as a real
    (senior) prior lien vs excluded - either as our own self-referential
    charge, or (per loan 1058201 / "First Circle") a same-or-junior-position
    entry held by someone else that isn't actually a prior lien either."""
    raw_loans = _cache.get("raw_loans")
    if not raw_loans:
        return jsonify({"error": "No cached loans yet - run a Refresh on the main dashboard first."})

    by_position = {}
    self_ref_excluded = 0
    not_senior_excluded = 0
    external_kept = 0
    zero_liens = 0
    multi_lien_loans = 0
    for l in raw_loans:
        if str(l.get("status")) != "1":  # Funded only, matches the dashboard's "active" set
            continue
        n = normalize_loan(l)
        pos = n["position"]
        pos_id = n["position_id"] if "position_id" in n else to_int((l.get("mortgage") or {}).get("position"))
        existing = l.get("existing_mortgages")
        existing = existing if isinstance(existing, list) else []
        if len(existing) > 1:
            multi_lien_loans += 1
        if not existing:
            zero_liens += 1
        for em in existing:
            if not isinstance(em, dict):
                continue
            if is_real_prior_lien(em, pos_id):
                external_kept += 1
            elif is_self_referential_lien(em, pos_id):
                self_ref_excluded += 1
            else:
                not_senior_excluded += 1  # a same/junior-position entry held by someone else - e.g. loan 1058201's "First Circle" (not a real prior lien, just not our own charge either)
        if n["ltv"] is None or not n["balance"]:
            continue
        bucket = by_position.setdefault(pos, {"count": 0, "balance": 0.0, "weighted_ltv_sum": 0.0})
        bucket["count"] += 1
        bucket["balance"] += n["balance"]
        bucket["weighted_ltv_sum"] += n["balance"] * n["ltv"]

    summary = {}
    for pos, b in by_position.items():
        summary[pos] = {
            "count": b["count"],
            "balance": round(b["balance"], 2),
            "weighted_avg_ltv": round(b["weighted_ltv_sum"] / b["balance"], 2) if b["balance"] else None,
        }

    return jsonify({
        "real_report_for_comparison": {"First": 57.65, "Second": 66.80, "Third": 58.79},
        "our_weighted_avg_ltv_by_position": summary,
        "existing_mortgage_entries_kept_as_real_prior_lien": external_kept,
        "existing_mortgage_entries_excluded_as_self_referential": self_ref_excluded,
        "existing_mortgage_entries_excluded_as_not_senior": not_senior_excluded,
        "funded_loans_with_zero_existing_mortgages": zero_liens,
        "funded_loans_with_more_than_one_existing_mortgage": multi_lien_loans,
    })


@app.route("/debug/ltv-outliers")
def debug_ltv_outliers():
    """/debug/ltv-aggregate shows First at 70.44% (real 57.65%) - a First
    position loan NEVER touches existing_mortgages (that fetch is skipped
    for position 1 entirely), so whatever's wrong there is just balance vs.
    property_value, nothing to do with prior liens. Since the portfolio LTV
    is BALANCE-WEIGHTED, a handful of large loans with a bad per-loan LTV
    can drag the whole average up even if most loans are fine. This does
    its own FRESH bulk-only fetch (list_all_loans, no per-loan lien calls -
    ~30-40s, not the full multi-minute refresh) and lists every First and
    Second position Funded loan sorted by LTV descending, so we can see the
    real distribution and spot exactly which loans are pulling it up."""
    from data import normalize_loan
    try:
        client = get_client()
        raw_loans = client.list_all_loans(statuses=[0, 1])
    except MortgageAutomatorError as e:
        return jsonify({"error": str(e)})
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"})

    rows = {"First": [], "Second": [], "Third": []}
    for l in raw_loans:
        if str(l.get("status")) != "1":
            continue
        n = normalize_loan(l)
        if n["position"] not in rows or n["ltv"] is None or not n["balance"]:
            continue
        rows[n["position"]].append({
            "loan_id": n["loan_id"],
            "loan_title": n["loan_title"],
            "balance": n["balance"],
            "property_value": n["property_value"],
            "ltv": n["ltv"],
        })

    out = {}
    for pos, items in rows.items():
        items.sort(key=lambda r: r["ltv"], reverse=True)
        count = len(items)
        total_balance = sum(r["balance"] for r in items)
        weighted_ltv = round(sum(r["balance"] * r["ltv"] for r in items) / total_balance, 2) if total_balance else None
        out[pos] = {
            "count": count,
            "weighted_avg_ltv_no_liens": weighted_ltv,
            "top_15_highest_ltv": items[:15],
        }

    return jsonify(out)


@app.route("/debug/balance-check")
def debug_balance_check():
    """Checks a hypothesis for the ~1.4% Current Loan Balance overshoot
    ($85.5M vs. the real $84,333,541.15): some loans have an undisbursed
    holdback (construction/reno reserve) still sitting in mortgage.amount,
    which isn't part of the actually-outstanding balance yet. Sums the
    Funded book both ways (raw amount vs. amount-minus-holdback) so we can
    see which one lands on the real number - no lien-fetch phase, so this
    is much faster than a full refresh."""
    try:
        client = get_client()
        raw_loans = client.list_all_loans(statuses=[0, 1])
    except MortgageAutomatorError as e:
        return jsonify({"error": str(e)})
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"})

    funded = [l for l in raw_loans if str(l.get("status")) == "1"]
    raw_sum = 0.0
    holdback_adjusted_sum = 0.0
    with_holdback = 0
    for l in funded:
        m = l.get("mortgage") or {}
        amount = m.get("amount") or 0
        holdback = m.get("holdback_amount") or 0
        raw_sum += amount
        holdback_adjusted_sum += (amount - holdback)
        if holdback:
            with_holdback += 1

    return jsonify({
        "funded_loan_count": len(funded),
        "target_from_ma_report": 84333541.15,
        "sum_of_mortgage_amount": round(raw_sum, 2),
        "sum_of_amount_minus_holdback": round(holdback_adjusted_sum, 2),
        "loans_with_nonzero_holdback": with_holdback,
    })


@app.route("/debug/ltv-check")
def debug_ltv_check():
    """Full detail (not just a boolean/count) on a handful of real
    subordinate-position loans: this loan's own balance, EVERY field on
    each existing_mortgages entry (unfiltered - not just the ones our
    EXISTING_MORTGAGE_BALANCE_KEYS guess matches), property value, and what
    our own code computes from that. Used to check the 74.2% overshoot -
    whether we're reading the right field out of existing_mortgages, or
    counting something we shouldn't (a mortgage that's already discharged,
    one of our own other-position loans on the same property, etc.)."""
    from data import normalize_loan, first_present, EXISTING_MORTGAGE_BALANCE_KEYS
    try:
        client = get_client()
        raw_loans = client.list_all_loans(max_pages=1, statuses=[0, 1])
    except MortgageAutomatorError as e:
        return jsonify({"error": str(e)})
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"})

    subordinate = []
    for l in raw_loans:
        pos = (l.get("mortgage") or {}).get("position")
        try:
            pos = int(pos)
        except (TypeError, ValueError):
            pos = None
        if pos and pos != 1:
            subordinate.append(l)
        if len(subordinate) >= 6:
            break

    out = []
    for l in subordinate:
        loan_id = l.get("loan_id")
        try:
            full = client.call("loans", "get", payload={"loan_id": loan_id}, params={"loan_id": loan_id})
        except MortgageAutomatorError as e:
            out.append({"loan_id": loan_id, "error": str(e)})
            continue
        loan = full.get("result", full)
        if isinstance(loan, list):
            loan = loan[0] if loan else {}
        if isinstance(loan, dict) and isinstance(loan.get("loan"), dict):
            loan = loan["loan"]
        mortgage = loan.get("mortgage") or {}
        prop = loan.get("property") or {}
        existing = loan.get("existing_mortgages")
        matched_values = [first_present(em, EXISTING_MORTGAGE_BALANCE_KEYS) for em in (existing or []) if isinstance(em, dict)]
        merged = {**l, "existing_mortgages": existing}
        computed = normalize_loan(merged)
        out.append({
            "loan_id": loan_id,
            "position": mortgage.get("position"),
            "own_amount": mortgage.get("amount"),
            "property_value": prop.get("value"),
            "existing_mortgages_raw": existing,
            "existing_mortgages_matched_balance_field_values": matched_values,
            "our_computed_balance": computed["balance"],
            "our_computed_mortgage_total": computed["mortgage_total"],
            "our_computed_ltv": computed["ltv"],
        })
        time.sleep(1.2)

    return jsonify(out)


@app.route("/debug/existing-mortgages-test")
def debug_existing_mortgages_test():
    """Single cheap test: does adding 'existing_mortgages' to the bulk
    /loans/list whitelist actually populate it (vs. only being available
    per-loan via /loans/get)? The normal fetch never asks for this field at
    all - this checks whether it even could."""
    try:
        client = get_client()
        data = client.call(
            "loans", "list",
            payload={"response_field_whitelist": ["loan_id", "status", "mortgage", "existing_mortgages"]},
            params={"start": 0},
        )
    except MortgageAutomatorError as e:
        return jsonify({"error": str(e)})
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"})
    batch = (data.get("result") or {}).get("loans", [])
    out = []
    for l in batch:
        mortgage = l.get("mortgage") or {}
        out.append({
            "loan_id": l.get("loan_id"),
            "status": l.get("status"),
            "position": mortgage.get("position"),
            "existing_mortgages": l.get("existing_mortgages"),
        })
    return jsonify({"count_returned": len(batch), "loans": out})


@app.route("/debug/value-check")
def debug_value_check():
    """The per-position weighted LTV (First 70.44%, Second 78.33%) is
    running well above Mortgage Automator's own report (57.65% / 66.80%) -
    and First-position loans never touch existing_mortgages at all, so
    prior-lien double counting can't be the (whole) explanation. This
    checks the other half of the LTV formula: is `property.value` (what we
    divide by) actually the right current-value field, or does the real
    figure live somewhere else on the loan (e.g. the loan's own top-level
    `projected_value`, which isn't in the bulk whitelist so we've never
    looked at it)? Pulls a handful of real First/Second position loans via
    /loans/get (full, unfiltered) and shows every value-like field side by
    side with what LTV each one implies."""
    from data import normalize_loan
    try:
        client = get_client()
        raw_loans = client.list_all_loans(max_pages=3, statuses=[0, 1])
    except MortgageAutomatorError as e:
        return jsonify({"error": str(e)})
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"})

    sample = []
    for l in raw_loans:
        pos = to_int((l.get("mortgage") or {}).get("position"))
        firsts = sum(1 for s in sample if to_int((s.get("mortgage") or {}).get("position")) == 1)
        seconds = sum(1 for s in sample if to_int((s.get("mortgage") or {}).get("position")) == 2)
        if pos == 1 and firsts < 4:
            sample.append(l)
        elif pos == 2 and seconds < 4:
            sample.append(l)
        if len(sample) >= 8:
            break

    out = []
    for l in sample:
        loan_id = l.get("loan_id")
        try:
            full = client.call("loans", "get", payload={"loan_id": loan_id}, params={"loan_id": loan_id})
        except MortgageAutomatorError as e:
            out.append({"loan_id": loan_id, "error": str(e)})
            continue
        loan = full.get("result", full)
        if isinstance(loan, list):
            loan = loan[0] if loan else {}
        if isinstance(loan, dict) and isinstance(loan.get("loan"), dict):
            loan = loan["loan"]
        mortgage = loan.get("mortgage") or {}
        prop = loan.get("property") or {}
        computed = normalize_loan(loan)
        property_value = prop.get("value")
        projected_value = loan.get("projected_value")
        land_value = loan.get("land_value") if loan.get("land_value") is not None else prop.get("land_value")
        purchase_price = prop.get("purchase_price")
        ltv_using_projected = None
        if computed["balance"] and projected_value:
            try:
                ltv_using_projected = round(computed["balance"] / projected_value * 100, 2)
            except (TypeError, ZeroDivisionError):
                ltv_using_projected = None
        out.append({
            "loan_id": loan_id,
            "position": mortgage.get("position"),
            "mortgage_amount": mortgage.get("amount"),
            "holdback_amount": mortgage.get("holdback_amount"),
            "our_computed_balance": computed["balance"],
            "property_value_field": property_value,
            "projected_value_field": projected_value,
            "land_value_field": land_value,
            "purchase_price_field": purchase_price,
            "our_computed_ltv_using_property_value": computed["ltv"],
            "ltv_using_projected_value_instead": ltv_using_projected,
        })
        time.sleep(1.2)

    return jsonify(out)


@app.route("/debug/other-properties")
def debug_other_properties():
    """Jayson flagged (on loan 1066682 'Lacharity') that our 137.7% LTV is
    wrong because we're only dividing by this loan's OWN property value -
    we're missing 'Other Properties' (additional collateral pledged on the
    same loan, ticked 'Blanket' in Mortgage Automator's UI), which brings
    the real LTV down to 67.97%. existing_mortgages (prior liens from OTHER
    lenders on the SAME property) is a different field from this - this
    checks the raw shape of `other_properties` so we can get the field
    names right (est. value, debts, the blanket flag) before fixing the
    combined-LTV formula in data.py. Pass ?loan_id=X for a different loan;
    defaults to the Lacharity loan."""
    loan_id = request.args.get("loan_id", "1066682")
    try:
        loan_id = int(loan_id)
    except (TypeError, ValueError):
        return jsonify({"error": f"bad loan_id: {loan_id!r}"})
    try:
        client = get_client()
        full = client.call("loans", "get", payload={"loan_id": loan_id}, params={"loan_id": loan_id})
    except MortgageAutomatorError as e:
        return jsonify({"error": str(e)})
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"})
    loan = full.get("result", full)
    if isinstance(loan, list):
        loan = loan[0] if loan else {}
    if isinstance(loan, dict) and isinstance(loan.get("loan"), dict):
        loan = loan["loan"]
    mortgage = loan.get("mortgage") or {}
    prop = loan.get("property") or {}
    return jsonify({
        "loan_id": loan_id,
        "loan_title": loan.get("loan_title"),
        "position": mortgage.get("position"),
        "mortgage_amount": mortgage.get("amount"),
        "holdback_amount": mortgage.get("holdback_amount"),
        "own_property_value": prop.get("value"),
        "other_properties_raw": loan.get("other_properties"),
        "existing_mortgages_raw": loan.get("existing_mortgages"),
    })


def open_browser():
    webbrowser.open(f"http://127.0.0.1:{PORT}")


if __name__ == "__main__":
    threading.Timer(1.0, open_browser).start()
    app.run(host="127.0.0.1", port=PORT, debug=False)
