# Sharing the Shelter Dashboard with your team (free, live, on the internet)

This gets your dashboard onto a real URL your team can open from anywhere,
using Render's free tier. It stays live and keeps auto-refreshing itself -
nobody needs to touch your computer.

Two things worth knowing up front, since there's no login on this link:

- **Anyone who has the link can see the full portfolio** - balances, LTVs,
  borrower names. Only share the URL directly with your team (don't post it
  somewhere public or searchable). If you change your mind and want a simple
  shared password on it later, that's a quick add - just ask.
- **Free tier sleeps after ~15 minutes of no visits.** The first person to
  open it after it's been quiet will wait ~1 minute for it to wake up, then
  it needs its own ~12-16 minute data refresh before showing real numbers
  (same as your local app after a restart) - a connection pulling in the
  cloud, not here. If that's annoying day-to-day, Render's cheapest
  always-on tier is ~$7/month and removes the sleep entirely.

## What's already done

I've updated your project with everything a hosted deployment needs:

- **`Procfile`** - tells Render how to start the app (`gunicorn`, 1 worker -
  important, see note below).
- **`requirements.txt`** - added `gunicorn` (the production web server;
  the `python app.py` dev server you use locally isn't meant for real
  traffic).
- **`.gitignore`** - makes sure your `.env` (with your live API key) never
  gets uploaded anywhere.
- **`app.py`** - added two settings, both **off unless you turn them on**:
  - `ENABLE_DEBUG_ROUTES` - keeps the `/debug/...` pages (which show raw
    account/loan internals) switched off on the public version. Leave this
    unset on Render. If you ever want them locally, add
    `ENABLE_DEBUG_ROUTES=1` to your own `.env`.
  - `AUTO_REFRESH_ON_START` - makes the server start pulling real data the
    moment it boots, instead of waiting for someone to click Refresh.
    **You'll turn this on for Render** (step 4 below), but leave it out of
    your local `.env`.

**Why `--workers 1` in the Procfile:** the dashboard keeps its loan data in
memory, refreshed by one background thread. More than one worker process
would each keep their own separate copy and each hit Mortgage Automator's
API on their own - duplicate load, and different people could see different
numbers depending which worker answered their request. One worker keeps it
simple and correct; `--threads 4` still lets it handle several people
browsing at once.

## Step 1 - Put the project on GitHub

Render deploys from a Git repository, so the code needs a home there first.
A **private** repo is what you want (keeps the code itself off public
search, though remember: your `.env` secrets never get uploaded anyway,
private or not).

1. If you don't already have one, create a free account at
   [github.com](https://github.com).
2. On GitHub, click **New repository**. Name it something like
   `shelter-dashboard`, set it to **Private**, and don't check any of the
   "initialize with..." boxes. Click **Create repository**.
3. GitHub will show you a repo URL like
   `https://github.com/<your-username>/shelter-dashboard.git` - copy it.
4. Open a terminal/command prompt **in your `Shelter Dashboard` folder**
   and run:

   ```
   git init
   git add .
   git commit -m "Shelter Dashboard"
   git branch -M main
   git remote add origin https://github.com/<your-username>/shelter-dashboard.git
   git push -u origin main
   ```

   (If `git` isn't recognized, install it from
   [git-scm.com](https://git-scm.com/downloads) first - it'll prompt you to
   sign in to GitHub the first time you push.)

5. Double-check on GitHub's website that `.env` does **not** appear in the
   uploaded file list. It shouldn't, thanks to `.gitignore` - but it's worth
   a 10-second look before moving on, since that file holds your live API
   key.

## Step 2 - Create a Render account and connect the repo

1. Go to [render.com](https://render.com) and sign up (free - "Sign up with
   GitHub" is the fastest way, since it can see your new repo immediately).
2. Click **New +** → **Web Service**.
3. Choose **Build and deploy from a Git repository**, then select the
   `shelter-dashboard` repo you just pushed.

## Step 3 - Configure the service

Render will mostly auto-detect this, but confirm these settings:

| Setting | Value |
|---|---|
| Name | `shelter-dashboard` (or anything you like - this becomes part of the URL) |
| Runtime | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn app:app --workers 1 --threads 4 --timeout 120` |
| Instance Type | **Free** |

## Step 4 - Add your environment variables

Still on that same setup page, find **Environment Variables** and add:

| Key | Value |
|---|---|
| `MA_ENDPOINT` | (copy from your local `.env`) |
| `MA_ACCOUNT_ID` | (copy from your local `.env`) |
| `MA_API_KEY` | (copy from your local `.env`) |
| `AUTO_REFRESH_ON_START` | `1` |

Type these in directly from your own `.env` file - don't paste them
anywhere else (like into a chat with me) along the way.

Leave `ENABLE_DEBUG_ROUTES` out entirely, so it stays off.

## Step 5 - Deploy

Click **Create Web Service**. Render will build and start it - the first
build takes a few minutes. Once it says "Live", you'll have a URL like:

```
https://shelter-dashboard.onrender.com
```

That's it - send that link to your team. It'll start pulling real loan data
right away (since `AUTO_REFRESH_ON_START` is on); give it the same ~12-16
minutes an initial refresh always takes before the numbers settle in.

## Keeping it updated later

Any time I (or you) change `app.py`, `data.py`, `ma_client.py`, or the
templates, you'll push the change to GitHub and Render redeploys
automatically:

```
git add .
git commit -m "describe the change"
git push
```
