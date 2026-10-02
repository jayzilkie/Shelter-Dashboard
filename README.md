# Mortgage Automator Live Dashboard

A small local Flask app that pulls live loan data from the Mortgage
Automator Lender API and shows it in a Portfolio Overview style
dashboard in your browser.

## Setup

1. Install Python 3.10+ if you don't have it.
2. In this folder, install the dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Open `.env` and fill in:
   - `MA_ENDPOINT` — your account's API base URL (see note below)
   - `MA_ACCOUNT_ID` — your Lender API account ID
   - `MA_API_KEY` — your Lender API key

## Run it

```
python app.py
```

A browser tab opens automatically at `http://127.0.0.1:5000` with your
live data. Click **Refresh** any time to pull the latest.

## About the endpoint URL

Mortgage Automator's API docs use `{ENDPOINT}` as a placeholder and say
to sign into your account for the real, account-specific value. Log
into your MA account, open the API docs page, and it'll show your
actual base URL — drop that into `MA_ENDPOINT`.

## About the auth headers

Mortgage Automator authenticates requests with two headers:
- `ACCOUNT-ID`: your account ID, sent as-is
- `API-AUTH`: `SHA1("{account_id}-{api_key}-{entity}/{action}-{timestamp}")`,
  where timestamp is the current UTC hour as `YYYY-MM-DD-HH`

This is implemented in `ma_client.py`. If you get an authentication
error, the most likely culprit is the separator between entity and
action in that hash (the docs call it an "entity/action combo" but
don't give a worked real-world example) — see the comment at the top
of `ma_client.py` for where to tweak it.

## About "current balance" and LTV

The public API documentation doesn't spell out a single dedicated
field for a loan's running current balance (the Loans object has 400+
fields across nested Property/Mortgage/Address objects, and a few
things your Tableau-style dashboard shows may come from a different
report or from MA's internal calculations). This app currently:
- Uses the mortgage's initial amount/total as a stand-in for balance
  if no dedicated balance field is present
- Computes LTV as balance ÷ property value

Once you run the app, visit `http://127.0.0.1:5000/debug/sample-loan`
to see one full raw loan record straight from your account. If the
real field name for current balance is different, add it to
`BALANCE_KEYS` at the top of `data.py` and refresh.

## Files

- `app.py` — Flask app, in-memory cache, auto-opens the browser
- `ma_client.py` — API client (auth, pagination, generic call)
- `data.py` — turns raw loan JSON into dashboard numbers (edit this to
  adjust field mappings)
- `templates/dashboard.html` — the page itself
