# Letterform · Brevo email automation

A small, self-hosted email dashboard for permission-based lists. It uses Python's standard library and SQLite; there are no package installs. The default mode is safe test mode: scheduled campaigns create test activity records and **send no email**.

The HTML files live in `templates/` to keep the project organized. The HTTP server is implemented with Python's standard library rather than Flask, so the PDF's Flask-only `render_template` instructions do not apply. `requirements.txt` is included for standard project tooling and intentionally has no third-party packages.

For beginner-friendly local, Brevo, Docker, and GitHub instructions, see [RUNNING_MANUAL.md](RUNNING_MANUAL.md).

## Features

- Explicit signup consent followed by double opt-in confirmation. Only confirmed contacts are eligible for campaigns.
- Public, mobile-friendly subscription form at `/subscribe`; signup confirmations are rate-limited by source IP.
- CSV validation (`email` or `email address` required; optional `name`), invalid-row reporting, duplicate prevention, and an import attestation. Imports stay pending and require confirmation.
- Campaign HTML editor, preview, future scheduling, test runs, and delivery/event reports.
- Per-recipient unsubscribe URLs, suppression checks immediately before delivery, local unsubscribe handling, and Brevo blacklist synchronization for live sends.
- Brevo event webhook endpoint with a separately configured token.
- API credentials are server environment variables. Dashboard Basic Auth can be enabled with `DASHBOARD_PASSWORD`.

## Run locally

Requires Python 3.10 or newer.

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
# Set DASHBOARD_PASSWORD to a long random value. Leave LIVE_SENDS_ENABLED=false.
python3 server.py
```

The server automatically reads `server.env` and `.env` beside `server.py` (the process environment takes precedence). Keep those files private and out of version control.

Open `http://localhost:8080` for the dashboard and `http://localhost:8080/subscribe` for the public signup form. With `DASHBOARD_PASSWORD` configured, the browser will request username `admin` and that password for the dashboard. Public subscribers can complete signup without admin credentials; they remain pending until they click through the confirmation page. SQLite data is stored under `./data/` by default. Set `DATABASE_PATH` to change it.

Project layout:

```text
bulk-mail/
├── templates/
│   ├── index.html
│   └── subscribe.html
├── server.py
├── requirements.txt
├── .env.example
├── Dockerfile
└── .gitignore
```

## Push a clean copy to GitHub

The `bulk-mail-github-ready.zip` package omits Git history, caches, local databases, and secret environment files. Extract it, create an empty GitHub repository, then from the extracted project directory run:

```sh
git init -b main
git add .
git commit -m "Initial bulk mail application"
git remote add origin YOUR_GITHUB_REPOSITORY_URL
git push -u origin main
```

Never add `.env` or `server.env` to Git. If a real API key was included in an uploaded archive or committed previously, revoke it in Brevo and generate a replacement before deployment.

## Connect Brevo

1. Verify your sender/domain and create a Brevo API key and a list for confirmed subscribers.
2. Set `BREVO_API_KEY`, `BREVO_SENDER_EMAIL`, `BREVO_SENDER_NAME`, and `BREVO_LIST_ID` in the server environment. Set `PUBLIC_BASE_URL` to the public HTTPS URL for this app.
3. Keep `LIVE_SENDS_ENABLED=false` while reviewing the dashboard and testing with sample data. In test mode, signups return a local confirmation link and campaign schedules are dry runs.
4. Set `LIVE_SENDS_ENABLED=true` only after connecting an audience that explicitly opted in, configuring a privacy notice, and verifying the sender. Live mode sends signup confirmation messages, syncs confirmed contacts to the configured Brevo list, sends the welcome automation, sends scheduled campaigns, and blacklists local unsubscribes in Brevo.

The confirmation URL is handled by this app so the local consent state stays in sync. `BREVO_WELCOME_TEMPLATE_ID` is optional; if set, it is used for the post-confirmation welcome email. Otherwise the server sends a minimal built-in welcome message.

## Webhooks and unsubscribe handling

Set an independent random `BREVO_WEBHOOK_TOKEN` and configure the Brevo webhook to call:

```text
https://your-domain.example/api/webhooks/brevo
```

The webhook handler requires `X-Webhook-Token` to match the server secret. Send event objects containing `event`, `email`, and optionally `campaign_id` (or a numeric `tag`). It records delivery/open/click events and locally suppresses unsubscribe, spam, and bounce events. Test webhook handling before relying on it; if your Brevo webhook configuration cannot set a custom header, put the endpoint behind a reverse proxy that adds one, or adapt the auth mechanism to your deployment.

The unsubscribe URL linked from each campaign opens a confirmation page; a person confirms with one more click so email security scanners do not unsubscribe them by prefetching the link. The app then suppresses the address locally. With live sends enabled, it also asks Brevo to set the contact's `emailBlacklisted` flag. Campaigns re-check the local subscriber status immediately before each send.

## Deploy with Docker

Build and run behind a TLS reverse proxy. Do not expose the Python server directly to the public internet: it does not terminate TLS or provide general request rate limiting. Signup attempts have a basic in-process IP limit; configure proper proxy-level rate limits for production.

```sh
docker build -t letterform .
chmod 600 /secure/path/letterform.env
docker run -d --name letterform \
  -p 127.0.0.1:8080:8080 \
  -v letterform-data:/data \
  --env-file /secure/path/letterform.env \
  -e HOST=0.0.0.0 -e PORT=8080 -e DATABASE_PATH=/data/dashboard.sqlite3 \
  letterform
```

Put production values in the protected env file or inject them through your host's secret manager. Include `DASHBOARD_PASSWORD`, `PUBLIC_BASE_URL`, Brevo keys, and `LIVE_SENDS_ENABLED=false`. Mount `/data` persistently and back up the SQLite database. Before enabling live mode, verify your Brevo account and domain, test opt-in and unsubscribe flows, and use only contacts whose permission you can substantiate. This app does not scrape directories or provide scraped-contact import workflows.

## Notes

- The UI's delivery metrics depend on webhook events. Configure an authenticated webhook and check that events arrive before treating the reports as authoritative.
- The app intentionally does not offer a UI switch to turn on live sending. That control is server-side so an accidental click cannot turn test data into a real campaign.
- For a larger team or high-volume sends, use a production database, a durable job queue, stronger account authentication, and monitoring before expanding this starter deployment.
# BULK_EMAIL
# BULK_EMAIL
