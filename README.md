# Workforce & Machine Live Dashboard

A web-based, password-protected dashboard that tracks daily workforce numbers and
machine status, backed by **PostgreSQL** (with a SQLite fallback for quick local testing).

## Features
- Password-protected web access (session-based login)
- Live dashboard that auto-refreshes every 15 seconds
- Tracks per-date records:
  - Total workforce, Metex, CSK, TopQuality, Best Care, Prestige staff
  - Running machines (count + names) and Out-of-order machines (count + names)
- Easy update form — create or edit any date's record
- History table of recent dates
- Responsive design — works on both mobile and PC

## Requirements
- Python 3.9+
- PostgreSQL (for production) — or nothing extra for SQLite testing

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Configure environment (copy and edit):
   ```
   cp .env.example .env
   ```
   Edit `.env` and set:
   - `DATABASE_URL` — your PostgreSQL connection string, e.g.
     `postgresql://postgres:PASSWORD@localhost:5432/workforce`
     (The app automatically selects the `psycopg` v3 driver.)
   - `DASHBOARD_PASSWORD` — the password users enter to view the dashboard
   - `SECRET_KEY` — a random string for session security

   > For a quick test without PostgreSQL, set
   > `DATABASE_URL=sqlite:///workforce.db` in `.env`.

   **PostgreSQL driver note:** `requirements.txt` installs `psycopg[binary]` (v3),
   which ships prebuilt wheels and needs no `pg_config`. If you prefer the classic
   `psycopg2-binary`, comment/uncomment the relevant line in `requirements.txt`
   (it requires PostgreSQL client libraries to build from source on some systems).

3. Run the app:
   ```
   python app.py
   ```

4. Open in your browser:
   ```
   http://localhost:5000
   ```

## PostgreSQL setup (production)
```sql
CREATE DATABASE workforce;
-- The app auto-creates the tables on first run (db.create_all()).
-- Connection string format: postgresql://USER:PASSWORD@HOST:5432/workforce
```

## Usage
- Log in with the password from `.env`.
- Use the date picker to view/edit any day; "Today" jumps to the current date.
- Fill the form and click **Save / Update** to store that date's snapshot.
- The dashboard and history update live for all viewers.

## Running on other PCs (remote access via Cloudflare Tunnel)

The app already binds to `0.0.0.0` on port `5000`, so it can be reached from
other machines. To let people open it from **anywhere over the internet**
without touching your router or buying a domain, use a Cloudflare Tunnel.

> **Security:** `DEBUG` defaults to `False`. Never set `DEBUG=True` when the app
> is reachable from the internet — the Werkzeug debugger allows remote code
> execution. Keep it off for remote access.

1. Install dependencies and start the server locally:
   ```
   pip install -r requirements.txt
   python app.py
   ```
   It listens on `http://localhost:5000`.

2. Download `cloudflared` for your OS:
   https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

3. In a second terminal, start the tunnel to the local server:
   ```
   cloudflared tunnel --url http://localhost:5000
   ```

4. Cloudflared prints a public HTTPS address, e.g.:
   ```
   https://random-name.trycloudflare.com
   ```

5. Share that address (and the `DASHBOARD_PASSWORD` from your `.env`) with
   anyone. They open it in any browser, on any PC or phone, and log in.

**Notes:**
- Your PC must stay on and the tunnel running for the link to work.
- The random address changes every time you restart the tunnel. For a fixed
  address, create a named tunnel (free Cloudflare account required).
- All viewers share the same database (`workforce.db` on this PC), so edits
  appear live for everyone.

## Deploying to the cloud (later, when final)

When the app is final, it can be deployed to a free host (e.g. Render.com) for a
permanent address. That step is deferred until the project is ready.

## Project structure
```
workforce-dashboard/
├── app.py              # Flask app (auth, API, DB models)
├── requirements.txt
├── .env.example        # config template
├── templates/
│   ├── login.html      # password login page
│   └── dashboard.html  # main dashboard + update form
└── README.md