# Mower Finder — Setup Guide

A web tool that automatically finds used Toro & John Deere walking greens mowers across the internet, flagging **bulk sellers (3+ units)** for priority outreach. Backed by your Supabase database for permanent, reliable storage.

**What you'll end up with:** A private URL (like `usedreelmowers-finder.streamlit.app`) you can bookmark and open from any device. Click "Run Search" and it scans Google, eBay, GovDeals, and Reddit for fresh leads — all stored in your Supabase database.

**Cost:** $0/month if you skip Google, or about $50/month for full Google coverage via SerpAPI. Streamlit Cloud and Supabase are free.

**Time to set up:** 20–25 minutes. You don't need to know how to code.

---

## What you'll need before starting

- [x] Your existing Supabase account (you already have this)
- [ ] A GitHub account — free, 2 min to create if you don't have one
- [ ] A Streamlit Cloud account — free, signs in with GitHub
- [ ] (Optional) A SerpAPI key for Google search — $0 free tier or $50/mo

---

## Part 1 — Prepare Supabase (5 min)

You can either **add these tables to your existing Supabase project** or **create a new project just for this tool**. Adding to the existing one is quicker; separate projects are a bit cleaner. Your call. The table names start with `mower_` so they won't collide with anything from other projects.

### Create the tables

1. Open your Supabase dashboard and pick the project you want to use.
2. In the left sidebar, click **SQL Editor**.
3. Click **New query** (top right).
4. Open the `schema.sql` file from your downloaded files, select all, copy, and paste into the SQL Editor.
5. Click the green **Run** button (or press Ctrl/Cmd + Enter).
6. You should see **"Success. No rows returned."** at the bottom. That's it — your tables exist.

### Grab your credentials

1. In the same Supabase project, click the **gear icon (⚙️)** in the bottom left → **Project Settings**.
2. Click **API** in the settings menu.
3. You need two things from this page — keep this tab open:
   - **Project URL** — under "Project URL", looks like `https://xxxxxxxx.supabase.co`
   - **service_role secret** — scroll to "Project API keys", find the row labeled `service_role`, click the 👁️ eye icon to reveal it, then the copy icon.

⚠️ **The `service_role` key is powerful** — treat it like a password. You're going to paste it into Streamlit Cloud's encrypted secrets (never into GitHub, never into a chat). If it's ever exposed, you can rotate it from this same page.

---

## Part 2 — Put the code on GitHub (5 min)

1. Open **https://github.com**. Sign up if you haven't.
2. Click the **+** in the top right → **New repository**.
3. Name: `mower-finder`. Leave as **Public**. Click **Create repository**.
4. On the next page click the blue link **uploading an existing file**.
5. Drag these four files into the upload area:
   - `app.py`
   - `requirements.txt`
   - `schema.sql`
   - `README.md`
6. Then also drag the **`.streamlit`** folder (contains `config.toml`). If GitHub's web upload doesn't take folders, skip this — it only controls the color theme.
7. Scroll down and click **Commit changes**.

---

## Part 3 — Deploy to Streamlit Cloud (5 min)

1. Open **https://share.streamlit.io** and click **Sign up** → **Continue with GitHub**. Authorize when prompted.
2. On the Streamlit Cloud dashboard, click **Create app** (or **New app**).
3. Choose **Deploy a public app from GitHub**.
4. Fill in:
   - **Repository**: `your-username/mower-finder`
   - **Branch**: `main`
   - **Main file path**: `app.py`
   - **App URL** (bottom): pick something memorable like `usedreelmowers-finder`
5. **Before clicking Deploy**, click **Advanced settings** and look for the **Secrets** box. Paste exactly this (replacing the placeholders with your real values from Part 1):

   ```
   SUPABASE_URL = "https://xxxxxxxx.supabase.co"
   SUPABASE_SERVICE_KEY = "eyJhbGc...the_long_service_role_key..."
   SERPAPI_KEY = ""
   ```

   Keep the quotes. You can leave `SERPAPI_KEY` blank for now — the app works without Google search.

6. Click **Save** on the secrets box, then click **Deploy**.

Wait 2–3 minutes while Streamlit builds. The app will load automatically. If the secrets were set correctly, you should see the normal UI. If something's off, the app will show a setup screen telling you exactly what's missing.

---

## Part 4 (optional) — Add SerpAPI for Google search

Skip this if you want to stay at $0. You can come back anytime.

1. Sign up at **https://serpapi.com** (free tier: 100 searches/month).
2. Copy your API key from the dashboard.
3. Back in Streamlit Cloud, click the **⋮** menu (bottom right of your app) → **Settings** → **Secrets**.
4. Replace the empty `SERPAPI_KEY = ""` with your real key:
   ```
   SERPAPI_KEY = "your_real_key_here"
   ```
5. Click **Save**. The app reloads.

---

## You're done! How to use it

1. **Bookmark your app URL** — open it from anywhere, any device.
2. Click **🚀 Run Search Now** in the sidebar. A full search takes 2–5 minutes.
3. Review the **📋 Leads** tab. Start with **🔥 Bulk only** — highest-value leads first.
4. Click **Open ↗** on any lead to see the original listing.
5. When you contact a seller, change the **Status** dropdown from *new* to *contacted*. Your pipeline stays organized.
6. Run searches every few days. The tool remembers everything, so you only see fresh leads.

## View or query your data directly in Supabase

Because your leads live in Supabase, you can:
- Open **Table Editor** in your Supabase dashboard → click `mower_listings` to see everything in a spreadsheet.
- Open **SQL Editor** to run custom queries like "top 10 locations for bulk Toro listings in the last 90 days."
- Build automations later (Zapier, n8n, etc.) that read from the same database.

## Troubleshooting

**"Tables not created yet" on the setup screen**
Go back to Part 1 → re-run `schema.sql` in the Supabase SQL Editor. Make sure you're on the correct Supabase project.

**"SUPABASE_URL or SUPABASE_SERVICE_KEY not set"**
In Streamlit Cloud: ⋮ menu → Settings → Secrets. Make sure both lines are present with quotes and correct values. Save and the app reloads.

**"Error installing requirements" during deployment**
In GitHub, click `requirements.txt` → pencil (✏️) → remove the `>=` version bits so it reads just `streamlit`, `requests`, `beautifulsoup4`, `pandas`, `supabase`. Commit. Streamlit auto-redeploys.

**Google source stays disabled after adding the key**
In Streamlit Cloud, click **⋮** → **Reboot app**. Sometimes secrets need a manual reboot to take effect.

**I want to edit the code**
Go to your GitHub repo, click the file, pencil (✏️), make changes, commit. Streamlit auto-redeploys within a minute.

## What it costs

| Service | Cost | What you get |
|---|---|---|
| Streamlit Cloud | Free | App hosting, public URL |
| GitHub | Free | Code storage |
| Supabase | Free | Postgres database (~50k rows, way more than you need) |
| SerpAPI | Free (100 searches/mo) or $50/mo (5,000 searches) | Google search coverage |
| eBay / GovDeals / Reddit | Free | No API key needed |

Skip SerpAPI to stay completely free — you still get three of the four sources.
