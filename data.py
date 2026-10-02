"""
Turns raw /loans/list JSON into the numbers the dashboard shows.

A note on field names: Mortgage Automator's Loan object is huge (400+
fields across nested Property / Mortgage / Address sub-objects) and a
few values shown on your Tableau dashboard - like a running "current
balance" or "maturity date" - aren't spelled out as a single documented
field in the Lender API PDF. This file pulls from the fields that ARE
documented (loan status, mortgage amount/total, property value, position,
city, province, property type) and falls back gracefully if a field is
missing. If you open http://127.0.0.1:5000/debug/sample-loan after
running the app, you'll see one full raw loan record - use that to find
the exact key your account returns for anything that looks off, and
adjust the `get_in` / `*_KEYS` lists below to match.
"""
from collections import defaultdict
from datetime import datetime, date

STATUS_MAP = {0: "In Progress", 1: "Funded", 2: "Discharged", 3: "Terminated"}
POSITION_MAP = {1: "First", 2: "Second", 3: "Third", 4: "Fourth", 5: "Fifth", 6: "Sixth", 7: "Seventh"}
PROPERTY_TYPE_MAP = {
    0: "Other", 1: "Detached", 2: "Semi-Detached", 3: "Row/Town House",
    4: "Commercial", 6: "Land", 7: "Mobile", 8: "Stacked",
    9: "Apartment", 11: "Condo",
}

# Only these statuses are shown on the dashboard (0 = In Progress, 1 = Funded).
INCLUDED_STATUSES = {0, 1}

# Candidate key names to try, in order, for values that aren't nailed down
# by the docs. Edit these lists if your account's real field names differ
# (check /debug/sample-loan or /debug/one-loan to confirm).
BALANCE_KEYS = ["current_balance", "balance", "outstanding_balance", "principal_balance"]
INITIAL_AMOUNT_KEYS = ["amount", "total"]
MATURITY_KEYS = ["maturity_date", "maturity", "renewal_date"]
# NOTE: mortgage.total looked like it might be "combined mortgage total
# including prior liens" but /debug/one-loan on a real account showed it's
# NOT that - it came back 0 on a normal funded 1st mortgage, and (per the
# 150%+ portfolio LTV it produced when trusted) holds something unrelated
# on other loans - possibly a lifetime total-of-payments figure, not a
# balance. Don't use it for LTV or as a dollar amount anywhere.
#
# The real source for "what's already registered on this property ahead of
# this loan" is the loan's own `existing_mortgages` list (confirmed via
# /debug/one-loan - a top-level field, a list of prior-lien records, one
# per existing mortgage). Each entry's balance-like field:
EXISTING_MORTGAGE_BALANCE_KEYS = ["balance", "current_balance", "amount", "outstanding_balance"]
# Confirmed via /debug/ltv-check on a real 2nd position loan: its
# existing_mortgages list included an entry held by "Shelter Lending" itself
# (our own account) at the SAME type/position as the loan's own position -
# i.e. a title search naturally turns up the loan's own registered charge
# alongside genuinely prior/external ones. Counting that entry on top of the
# loan's own balance double-counts our own money and was exactly what
# inflated the portfolio LTV to 74%+ (vs. the real 62%). An internally-held
# entry only counts as a real prior lien if it's SENIOR (a lower type number)
# to this loan's own position - e.g. a "Blanket 1st & 2nd" loan where we
# genuinely hold both positions - never at the same or a junior position.
OWN_LENDER_NAME_FRAGMENT = "shelterlending"
# Some accounts' API returns a ready-made LTV percentage right on the
# mortgage object (get_mortgage's own docs mention it: "...maturity date,
# and LTV"). When it's there, trust it over anything we'd compute -
# Mortgage Automator knows whether it's combined with prior liens on a
# 2nd/3rd position loan, we don't have to guess.
LTV_KEYS = ["ltv", "loan_to_value", "combined_ltv", "cltv", "ltv_percent"]
# Province wasn't showing up right - some accounts nest it under a different
# key than "province"/"state", or use a short code vs. a full name. Widened
# the candidate list; if it's still off after this, check /debug/sample-loan
# for the real key under property.address and add it here.
PROVINCE_KEYS = ["province", "state", "province_code", "state_province", "region", "administrative_area", "province_name"]
# "Other Properties" in Mortgage Automator's UI (the "Blanket" tab) - extra
# properties pledged as ADDITIONAL collateral on a loan, separate from
# existing_mortgages (which is prior liens from other lenders on this loan's
# OWN property). Confirmed via /debug/other-properties on loan 1066682
# (Lacharity): a straight 1st position loan, own property worth $570,000
# against a $785,000 balance (137.7% on its own), PLUS a blanket-ticked
# second property worth $585,000 - real combined LTV is 785,000/1,155,000 =
# 67.97%, matching Mortgage Automator's own report exactly. This is why a
# first mortgage can legitimately need this too, not just 2nd/3rd/blanket -
# "blanket" is a per-property flag on the loan, not tied to its own position.
OTHER_PROPERTY_VALUE_KEYS = ["value", "est_value", "property_value", "appraised_value"]
# Each other_property can itself carry a `positions` list (same shape as a
# loan's own existing_mortgages) - a lien already registered against THAT
# property, which reduces how much of its value is actually free to secure
# THIS loan. Reuses EXISTING_MORTGAGE_BALANCE_KEYS for the balance field and
# the same self-held/discharged exclusions as is_self_referential_lien.

# Fixed display order for the "ordinal" breakdown panels - anything not in
# this list (an unmapped position, say) is appended after, sorted alphabetically.
POSITION_ORDER = ["First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh"]
LTV_BAND_ORDER = ["Under 60%", "60-70%", "70-80%", "80%+", "Unknown"]
RATE_BAND_ORDER = ["Under 6%", "6-8%", "8-10%", "10-12%", "12%+", "Unknown"]

# A single-hue, light -> dark ramp used to shade the *ordered* band panels so
# the highest band (worst LTV, highest rate, most-subordinate position) reads
# as the heaviest bar. Flat panels (property type, substatus, location) stay
# one flat color - bar length alone carries the magnitude there.
SEQUENTIAL_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#2a78d6", "#1c5cab", "#104281"]
FLAT_SHADE = SEQUENTIAL_RAMP[4]
STATUS_COLORS = {"In Progress": "#2a78d6", "Funded": "#eb6834"}


def get_in(d, *path, default=None):
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def is_self_referential_lien(em, this_position):
    """True if an existing_mortgages entry is (very likely) just this same
    loan's own registered charge turning up in its own title search, rather
    than a genuinely separate prior lien - so it shouldn't be added on top
    of the loan's own balance when combining for LTV. Kept as its own check
    (rather than folded into is_real_prior_lien) because it's still useful
    on its own for /debug reporting, even though is_real_prior_lien's
    seniority check below is what actually gates the LTV math now."""
    if not isinstance(em, dict):
        return False
    holder = "".join(ch for ch in str(em.get("holder") or "").lower() if ch.isalnum())
    if OWN_LENDER_NAME_FRAGMENT not in holder:
        return False  # a real external lender
    em_type = to_int(em.get("type"))
    if isinstance(em_type, int) and isinstance(this_position, int) and em_type < this_position:
        return False  # genuinely senior and internally-held (e.g. a Blanket 1st & 2nd) - counts
    return True  # same/junior position and held by us - almost certainly itself


def is_real_prior_lien(em, this_position):
    """True only if an existing_mortgages entry is genuinely SENIOR to (already
    registered ahead of) this loan's own position - the only kind that
    belongs added on top of this loan's own balance for combined/CLTV math.
    This matters regardless of WHO holds it, not just whether it's held by
    us - confirmed via loan 1058201 (Westcor Lands, a straight First position
    loan): its existing_mortgages list included a same-position ("type": 1)
    entry from an unrelated lender, "First Circle", at $1,735,000. That's not
    a real prior encumbrance - nothing can be senior to a First position loan
    by definition - it's almost certainly the mortgage THIS loan paid out /
    refinanced, still showing up in the title search. Treating every
    externally-held entry as "always counts" (the old logic) added that
    $1.7M on top of a $1,131,000 balance against a $945,000 property, LTV
    160.56% instead of the real ~63%, and was the dominant reason First
    position loans ran ~19 points above Mortgage Automator's own report
    portfolio-wide. A same-or-junior-position entry is excluded whether it's
    held by us (is_self_referential_lien's old job) or by someone else - an
    entry whose type/position can't be confirmed is excluded too, rather
    than risk inflating LTV off data we can't verify is actually senior."""
    if not isinstance(em, dict):
        return False
    em_type = to_int(em.get("type"))
    if not (isinstance(em_type, int) and isinstance(this_position, int)):
        return False
    return em_type < this_position


def other_property_contribution(op):
    """How much of an other_properties entry's value is actually free to
    secure THIS loan: its value, minus any ACTIVE (non-discharged), real
    external lien already registered against THAT property - mirroring
    is_self_referential_lien's logic but reading the field names this
    nested `positions` list actually uses (confirmed via
    /debug/other-properties): `mortgageposition` instead of `type`, and a
    `discharged` flag (a paid-off prior lien doesn't reduce its equity).
    Only counts at all if the "Blanket" checkbox (`is_blanket`) was ticked -
    an other_properties entry can exist purely as asset/net-worth info
    without being pledged as collateral on this loan."""
    if not isinstance(op, dict) or not to_int(op.get("is_blanket")):
        return 0.0
    value = to_float(first_present(op, OTHER_PROPERTY_VALUE_KEYS)) or 0.0
    positions = op.get("positions")
    positions = positions if isinstance(positions, list) else []
    debt = 0.0
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        if to_int(pos.get("discharged")):
            continue  # paid off - no longer a real encumbrance
        holder = "".join(ch for ch in str(pos.get("holder") or "").lower() if ch.isalnum())
        if OWN_LENDER_NAME_FRAGMENT in holder:
            continue  # our own charge on that property - not an external debt
        debt += to_float(first_present(pos, EXISTING_MORTGAGE_BALANCE_KEYS)) or 0.0
    return max(value - debt, 0.0)


def first_present(d, keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if d.get(k) not in (None, ""):
            return d.get(k)
    return default


def to_int(value):
    """Some accounts return IDs as text ("1") instead of numbers (1)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def to_float(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_date(value):
    """Best-effort parse of whatever date format this account returns."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return date.fromtimestamp(value)
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    return None


def normalize_loan(raw: dict) -> dict:
    """Flatten one raw loan record into the fields the dashboard needs."""
    mortgage = raw.get("mortgage") or raw.get("mortgages") or {}
    if isinstance(mortgage, list):
        mortgage = mortgage[0] if mortgage else {}
    prop = raw.get("property") or {}
    address = prop.get("address") or {}

    status_id = to_int(raw.get("status"))
    position_id = to_int(mortgage.get("position"))
    property_type_id = to_int(prop.get("property_type"))

    property_value = to_float(prop.get("value"))
    initial_amount = to_float(first_present(mortgage, INITIAL_AMOUNT_KEYS))
    balance = to_float(first_present(raw, BALANCE_KEYS) or first_present(mortgage, BALANCE_KEYS))
    if balance is None:
        balance = initial_amount
    # Confirmed via /debug/balance-check on the real account: mortgage.amount
    # is the full committed facility, but some loans (construction/reno)
    # hold part of that back in a holdback reserve that hasn't actually been
    # advanced to the borrower yet - subtracting it moved the portfolio
    # total roughly halfway from $85.52M to the real $84.33M. initial_amount
    # (used for Fundings YTD etc.) stays the full committed figure; only the
    # "current balance" used for the KPI and LTV drops the undisbursed part.
    holdback = to_float(mortgage.get("holdback_amount"))
    if balance is not None and holdback:
        balance = balance - holdback
    interest_rate = to_float(mortgage.get("interest"))

    existing_mortgages = raw.get("existing_mortgages")
    if not isinstance(existing_mortgages, list):
        existing_mortgages = []
    real_prior_liens = [
        em for em in existing_mortgages
        if isinstance(em, dict) and is_real_prior_lien(em, position_id)
    ]
    existing_mortgage_count = len(real_prior_liens)
    prior_liens_total = sum(
        (to_float(first_present(em, EXISTING_MORTGAGE_BALANCE_KEYS)) or 0)
        for em in real_prior_liens
    )
    # "Mortgage total" here means the full combined encumbrance on the
    # property: this loan's own balance plus whatever prior mortgages are
    # already registered ahead of it (empty for a plain 1st position loan,
    # so mortgage_total == balance in that case).
    mortgage_total = (balance or 0) + prior_liens_total if (balance is not None or prior_liens_total) else None

    # "Other Properties" / blanket additional collateral: extra properties
    # pledged alongside this loan's own property (see OTHER_PROPERTY_VALUE_KEYS
    # comment above). Each contributes its value net of any real external debt
    # already against it - other_property_contribution() does that math and
    # returns 0 for any entry that isn't actually blanket-ticked.
    other_properties = raw.get("other_properties")
    if not isinstance(other_properties, list):
        other_properties = []
    additional_security = sum(
        other_property_contribution(op) for op in other_properties if isinstance(op, dict)
    )
    security_value = (property_value or 0) + additional_security

    # LTV: on a 2nd/3rd position (or blanket) loan, "LTV" means that combined
    # total divided by combined security value (this loan's own property PLUS
    # any blanket-pledged other properties) - not just this loan's own
    # property on its own, which understates security (and so overstates LTV)
    # for any loan with additional blanket collateral. Prefer a
    # directly-reported ltv/cltv field if the account's API returns one.
    ltv = to_float(first_present(mortgage, LTV_KEYS) or first_present(raw, LTV_KEYS))
    if ltv is None:
        ltv_basis = mortgage_total if mortgage_total is not None else balance
        if security_value and ltv_basis:
            try:
                ltv = round((ltv_basis / security_value) * 100, 2)
            except (TypeError, ZeroDivisionError, ValueError):
                ltv = None

    maturity_raw = first_present(mortgage, MATURITY_KEYS) or first_present(raw, MATURITY_KEYS)
    maturity_date = parse_date(maturity_raw)

    substatus = raw.get("substatus")
    substatus_label = str(substatus) if substatus not in (None, "") else "None"

    return {
        "loan_id": raw.get("loan_id"),
        "loan_title": raw.get("loan_title"),
        "status_id": status_id,
        "status": STATUS_MAP.get(status_id, f"Unknown ({status_id})"),
        "substatus": substatus_label,
        "funding_date": get_in(mortgage, "funding_date"),
        "closing_date": raw.get("closing_date"),
        "maturity_date": maturity_date.isoformat() if maturity_date else None,
        "balance": balance,
        "initial_amount": initial_amount,
        "mortgage_total": mortgage_total,
        "existing_mortgage_count": existing_mortgage_count,
        "property_value": property_value,
        "additional_security_value": additional_security,
        "security_value": security_value,
        "interest_rate": interest_rate,
        "term_months": mortgage.get("term"),
        "position_id": position_id,
        "position": POSITION_MAP.get(position_id, f"Unknown ({position_id})" if position_id else "Unknown"),
        "property_type_id": property_type_id,
        "property_type": PROPERTY_TYPE_MAP.get(property_type_id, f"Other ({property_type_id})" if property_type_id else "Unknown"),
        "city": (address.get("city") or "").strip() or None,
        "province": (lambda p: str(p).strip() or None)(first_present(address, PROVINCE_KEYS) or ""),
        "ltv": ltv,
    }


def ltv_band(ltv):
    if ltv is None:
        return "Unknown"
    if ltv < 60:
        return "Under 60%"
    if ltv < 70:
        return "60-70%"
    if ltv < 80:
        return "70-80%"
    return "80%+"


def rate_band(rate):
    if rate is None:
        return "Unknown"
    if rate < 6:
        return "Under 6%"
    if rate < 8:
        return "6-8%"
    if rate < 10:
        return "8-10%"
    if rate < 12:
        return "10-12%"
    return "12%+"


def shade_for(index, total):
    """Pick a step of SEQUENTIAL_RAMP for position `index` of `total` ordered bars."""
    if total <= 1:
        return SEQUENTIAL_RAMP[-1]
    pos = index / (total - 1)
    i = round(pos * (len(SEQUENTIAL_RAMP) - 1))
    return SEQUENTIAL_RAMP[i]


def to_sorted_list(d):
    return sorted(
        [{"label": k, **v} for k, v in d.items()],
        key=lambda x: x["balance"],
        reverse=True,
    )


def to_flat_list(d):
    """Unordered breakdown (property type, substatus, location): sorted by
    balance, one flat color - the bar length alone carries the magnitude."""
    rows = to_sorted_list(d)
    for row in rows:
        row["shade"] = FLAT_SHADE
    return rows


def to_status_list(d):
    rows = to_sorted_list(d)
    for row in rows:
        row["shade"] = STATUS_COLORS.get(row["label"], FLAT_SHADE)
    return rows


MATURITY_BUCKET_ORDER = ["Overdue", "0-30 days", "31-60 days", "61-90 days", "91-120 days"]


def maturity_bucket(days):
    if days < 0:
        return "Overdue"
    if days <= 30:
        return "0-30 days"
    if days <= 60:
        return "31-60 days"
    if days <= 90:
        return "61-90 days"
    return "91-120 days"


def to_maturity_bucket_list(d):
    """Like to_ordered_list, but "Overdue" gets the critical color and the
    rest of the ramp runs soonest-due = heaviest (reverse of the usual
    light -> dark direction, since here the *first* band is the urgent one)."""
    rows = [{"label": k, **d[k]} for k in MATURITY_BUCKET_ORDER if k in d]
    band_rows = [r for r in rows if r["label"] != "Overdue"]
    total = len(band_rows)
    i = 0
    for row in rows:
        if row["label"] == "Overdue":
            row["shade"] = "var(--critical)"
        else:
            row["shade"] = shade_for(total - 1 - i, total)
            i += 1
    return rows


def month_add(d, n):
    """First-of-month date `n` months after the first of `d`'s month."""
    total = d.month - 1 + n
    year = d.year + total // 12
    month = total % 12 + 1
    return date(year, month, 1)


def renewals_month_buckets(today, months=12):
    """Ordered (label, start, end) tuples for the rolling window used by the
    Renewals tab chart: the current calendar month plus the following
    `months - 1` months."""
    start = date(today.year, today.month, 1)
    buckets = []
    for i in range(months):
        b_start = month_add(start, i)
        b_end = month_add(start, i + 1)
        buckets.append((b_start.strftime("%b %Y"), b_start, b_end))
    return buckets


def renewals_month_label(m_date, today, buckets):
    """Bucket a maturity date into "Overdue", one of the 12 rolling months,
    or ">12 months" - a coarser, month-level cousin of maturity_bucket()
    used for the Renewals tab's by-month chart."""
    if m_date < date(today.year, today.month, 1):
        return "Overdue"
    for label, b_start, b_end in buckets:
        if b_start <= m_date < b_end:
            return label
    return ">12 months"


def to_ordered_list(d, order):
    """Like to_sorted_list, but in a fixed logical order (bands/positions)
    with anything unrecognized appended after, sorted alphabetically. Shades
    step light -> dark across the displayed order - except "Unknown", which
    always gets a neutral gray rather than inheriting the ramp's darkest
    (worst-looking) step just because it's sorted last."""
    known = [{"label": k, **d[k]} for k in order if k in d]
    leftover = sorted(
        [{"label": k, **v} for k, v in d.items() if k not in order],
        key=lambda x: x["label"],
    )
    rows = known + leftover
    band_rows = [r for r in rows if not r["label"].startswith("Unknown")]
    total = len(band_rows)
    i = 0
    for row in rows:
        if row["label"].startswith("Unknown"):
            row["shade"] = "var(--muted)"
        else:
            row["shade"] = shade_for(i, total)
            i += 1
    return rows


def build_dashboard(raw_loans: list) -> dict:
    loans = [normalize_loan(l) for l in raw_loans]
    loans = [l for l in loans if l["status_id"] in INCLUDED_STATUSES]

    active = [l for l in loans if l["status_id"] == 1]  # Funded
    total_balance = sum(l["balance"] or 0 for l in active)
    total_committed = sum((l["initial_amount"] or l["balance"] or 0) for l in active)

    rated = [l for l in active if l["balance"] and l["interest_rate"] is not None]
    wtd_avg_rate = (
        round(sum(l["balance"] * l["interest_rate"] for l in rated) / sum(l["balance"] for l in rated), 3)
        if rated else None
    )

    # Portfolio LTV = balance-weighted average of each loan's own LTV (which,
    # per the fix in normalize_loan, is already the combined/CLTV figure for
    # 2nd+ position loans). A straight sum(balance)/sum(property_value)
    # pools every loan's own slice of value instead of the full encumbrance,
    # which understates LTV more the more 2nd/3rd/blanket loans a book has -
    # this weighted-average-of-per-loan-LTV approach is what matches Mortgage
    # Automator's own "Funded Loans" report (verified: reproduces its 62.10%
    # portfolio total from its position-level LTV/balance breakdown).
    ltv_priced = [l for l in active if l["ltv"] is not None and l["balance"]]
    total_balance_priced = sum(l["balance"] for l in ltv_priced)
    portfolio_ltv = (
        round(sum(l["balance"] * l["ltv"] for l in ltv_priced) / total_balance_priced, 2)
        if total_balance_priced else None
    )

    by_status = defaultdict(lambda: {"count": 0, "balance": 0})
    by_position = defaultdict(lambda: {"count": 0, "balance": 0})
    by_property_type = defaultdict(lambda: {"count": 0, "balance": 0})
    by_ltv_band = defaultdict(lambda: {"count": 0, "balance": 0})
    by_rate_band = defaultdict(lambda: {"count": 0, "balance": 0})
    by_substatus = defaultdict(lambda: {"count": 0, "balance": 0})
    by_location = defaultdict(lambda: {"count": 0, "balance": 0})

    for l in loans:
        bal = l["balance"] or 0
        by_status[l["status"]]["count"] += 1
        by_status[l["status"]]["balance"] += bal

    for l in active:
        bal = l["balance"] or 0
        by_position[l["position"]]["count"] += 1
        by_position[l["position"]]["balance"] += bal
        by_property_type[l["property_type"]]["count"] += 1
        by_property_type[l["property_type"]]["balance"] += bal
        by_ltv_band[ltv_band(l["ltv"])]["count"] += 1
        by_ltv_band[ltv_band(l["ltv"])]["balance"] += bal
        by_rate_band[rate_band(l["interest_rate"])]["count"] += 1
        by_rate_band[rate_band(l["interest_rate"])]["balance"] += bal
        by_substatus[l["substatus"]]["count"] += 1
        by_substatus[l["substatus"]]["balance"] += bal
        if l["city"] and l["province"]:
            loc_key = f"{l['city']}, {l['province']}"
        elif l["city"]:
            loc_key = l["city"]
        elif l["province"]:
            loc_key = l["province"]
        else:
            loc_key = "Unknown"
        by_location[loc_key]["count"] += 1
        by_location[loc_key]["balance"] += bal

    today = date.today()
    maturities = []
    for l in active:
        if not l["maturity_date"]:
            continue
        try:
            m_date = date.fromisoformat(l["maturity_date"])
        except ValueError:
            continue
        maturities.append({**l, "maturity_date_obj": m_date, "days_until": (m_date - today).days})
    maturities.sort(key=lambda l: l["maturity_date_obj"])
    upcoming = [l for l in maturities if l["days_until"] <= 120]
    upcoming_out = [{k: v for k, v in l.items() if k != "maturity_date_obj"} for l in upcoming[:30]]

    by_maturity_bucket = defaultdict(lambda: {"count": 0, "balance": 0})
    for l in upcoming:
        bucket = maturity_bucket(l["days_until"])
        by_maturity_bucket[bucket]["count"] += 1
        by_maturity_bucket[bucket]["balance"] += l["balance"] or 0

    # Full Renewals tab: every active loan with a maturity date, not just the
    # next 120 days, so it can double as a renewals worklist rather than just
    # a near-term alert. Each row is tagged with the same month bucket used
    # for the chart, so the list can be grouped by it.
    month_buckets = renewals_month_buckets(today, months=12)
    for l in maturities:
        l["month_label"] = renewals_month_label(l["maturity_date_obj"], today, month_buckets)

    renewals_list = [{k: v for k, v in l.items() if k != "maturity_date_obj"} for l in maturities]
    renewals_list = renewals_list[:500]  # keep the page responsive on very large portfolios

    by_renewal_month = defaultdict(lambda: {"count": 0, "balance": 0})
    for l in maturities:
        by_renewal_month[l["month_label"]]["count"] += 1
        by_renewal_month[l["month_label"]]["balance"] += l["balance"] or 0

    renewal_month_order = ["Overdue"] + [b[0] for b in month_buckets] + [">12 months"]
    renewal_month_rows = [{"label": k, **by_renewal_month[k]} for k in renewal_month_order if k in by_renewal_month]
    for row in renewal_month_rows:
        if row["label"] == "Overdue":
            row["shade"] = "var(--critical)"
        elif row["label"] == ">12 months":
            row["shade"] = "var(--muted)"
        else:
            row["shade"] = "var(--navy)"

    # Headline KPI for the tab: everything in view (overdue + the 12-month
    # window), leaving out the ">12 months" catch-all.
    in_window = [l for l in maturities if l["month_label"] != ">12 months"]
    renewals_kpi_count = len(in_window)
    renewals_kpi_balance = sum(l["balance"] or 0 for l in in_window)

    # Fundings YTD: active loans funded since Jan 1 of the current year.
    fundings_ytd = 0
    fundings_ytd_count = 0
    for l in active:
        f_date = parse_date(l["funding_date"])
        if f_date and f_date.year == today.year and f_date <= today:
            fundings_ytd += l["initial_amount"] or l["balance"] or 0
            fundings_ytd_count += 1

    return {
        "kpis": {
            "active_loans": len(active),
            "current_balance": total_balance,
            "avg_balance": round(total_balance / len(active), 2) if active else 0,
            "total_loans": len(loans),
            "total_committed": total_committed,
            "wtd_avg_rate": wtd_avg_rate,
            "portfolio_ltv": portfolio_ltv,
            "maturing_90d_count": len([l for l in upcoming if l["days_until"] <= 90]),
            "maturing_90d_balance": sum((l["balance"] or 0) for l in upcoming if l["days_until"] <= 90),
            "has_maturity_data": any(l["maturity_date"] for l in active),
            "fundings_ytd": fundings_ytd,
            "fundings_ytd_count": fundings_ytd_count,
            "renewals_count": renewals_kpi_count,
            "renewals_balance": renewals_kpi_balance,
        },
        "by_status": to_status_list(by_status),
        "by_position": to_ordered_list(by_position, POSITION_ORDER),
        "by_property_type": to_flat_list(by_property_type),
        "by_ltv_band": to_ordered_list(by_ltv_band, LTV_BAND_ORDER),
        "by_rate_band": to_ordered_list(by_rate_band, RATE_BAND_ORDER),
        "by_substatus": to_flat_list(by_substatus),
        "by_location": to_flat_list(by_location)[:20],
        "by_maturity_bucket": to_maturity_bucket_list(by_maturity_bucket),
        "upcoming_maturities": upcoming_out,
        "renewals_list": renewals_list,
        "renewals_by_month": renewal_month_rows,
        "loans": loans,
    }
