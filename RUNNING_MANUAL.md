# Letterform Bulk Mail - Run Manual

This guide starts the dashboard locally, tests it without sending email, and explains how to enable Brevo sending when the account and permission-based list are ready.

## 1. Prepare the project

1. Download and extract `bulk-mail-github-ready.zip`.
2. Open a terminal in the extracted `bulk-mail` folder (the folder containing `server.py`).
3. Install Python 3.10 or newer if needed.

The project uses Python's standard library. `requirements.txt` is included for a repeatable setup, but it has no external packages to install.

## 2. Create the environment file

Copy `.env.example` to `server.env`:

Linux or macOS:

```sh
cp .env.example server.env
chmod 600 server.env
```

Windows PowerShell:

```powershell
Copy-Item .env.example server.env
```

Open `server.env` in a text editor. For local testing, set at least a private dashboard password and leave live sending off:

```env
HOST=127.0.0.1
PORT=8080
DASHBOARD_PASSWORD=replace-with-a-long-random-password
PUBLIC_BASE_URL=http://localhost:8080
LIVE_SENDS_ENABLED=false
```

Keep `server.env` private. Do not upload it to GitHub or paste its contents into chat. The server automatically reads `server.env` and `.env` beside `server.py`; operating-system environment values override file values.

## 3. Start the app

Create and activate a virtual environment (recommended):

Linux or macOS:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python server.py
```

Windows PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python server.py
```

Keep the terminal open while using the app. Stop the server with `Ctrl+C`.

## 4. Open the dashboard and check it

- Dashboard: <http://localhost:8080>
- Public signup form: <http://localhost:8080/subscribe>
- Server health check: <http://localhost:8080/health>

If `DASHBOARD_PASSWORD` is set, the browser prompts for credentials. Use username `admin` and the password from `server.env`.

The health check should return `{"ok": true}`. The dashboard should load and show **Test mode**. Create a signup using an address you control. In test mode, no confirmation email is sent: the dashboard gives you a local confirmation link. Open it and press **Confirm subscription**. Create and schedule a campaign to verify that a dry run appears in activity. Test runs do not send emails.

## 5. Connect a Brevo account

In Brevo, verify your sender address and authenticate the sending domain. Create a Brevo API key with the permissions this app needs and a contact list for confirmed subscribers. Use a new key if one was included in any previously shared ZIP or Git history; revoke the exposed key first.

Add these values to `server.env`:

```env
BREVO_API_KEY=your_new_brevo_api_key
BREVO_SENDER_EMAIL=updates@example.com
BREVO_SENDER_NAME=Your Organization
BREVO_LIST_ID=123
BREVO_WEBHOOK_TOKEN=use-a-separate-long-random-token
LIVE_SENDS_ENABLED=false
```

`BREVO_LIST_ID` must be the numeric ID of your Brevo contact list. Keep `LIVE_SENDS_ENABLED=false` while reviewing configuration. Restart the server after changing settings. The dashboard's Settings page reports which values it detects.

## 6. Start live sending only after review

Before enabling live mode:

1. Deploy the app at a public HTTPS address and set `PUBLIC_BASE_URL` to that address. Confirmation and unsubscribe links must be reachable by subscribers.
2. Confirm the sender and domain are verified in Brevo.
3. Use only contacts who explicitly opted in. Do not import scraped directory contacts.
4. Review the subscription privacy notice, email copy, recipient list, and unsubscribe handling.
5. Confirm API key, sender email, and numeric list ID appear correctly in the dashboard.

Then change `LIVE_SENDS_ENABLED=true` in `server.env` and restart the server. Live signups send double opt-in confirmation emails. Confirmed contacts sync to Brevo. Scheduled campaigns send only to confirmed contacts that have not been suppressed. CSV imports remain pending and require confirmation; in live mode the app sends those contacts a confirmation email.

Create a campaign in the dashboard, include your HTML content, choose a future schedule time, and save it. Keep the server running at the scheduled time. Every campaign gets an unsubscribe link; unsubscribe actions update local suppression and, in live mode, Brevo suppression.

## 7. Optional: Docker deployment

Run behind a TLS reverse proxy. Do not expose the Python server directly to the public internet; it does not terminate TLS or provide general request rate limiting.

Build the image from the project directory:

```sh
docker build -t letterform .
```

Create a protected deployment env file with production settings. Then run:

```sh
docker run -d --name letterform \
  -p 127.0.0.1:8080:8080 \
  -v letterform-data:/data \
  --env-file /secure/path/letterform.env \
  -e HOST=0.0.0.0 -e PORT=8080 -e DATABASE_PATH=/data/dashboard.sqlite3 \
  letterform
```

Set `PUBLIC_BASE_URL=https://your-domain.example` in the deployment env file. Preserve the `/data` volume for SQLite data. After changing app code, rebuild and recreate the container.

## 8. Optional: push the clean project to GitHub

The clean ZIP omits local Git history, caches, data, and secrets. Create an empty GitHub repository, extract the ZIP, then run these commands from the extracted project directory:

```sh
git init -b main
git add .
git commit -m "Initial bulk mail application"
git remote add origin YOUR_GITHUB_REPOSITORY_URL
git push -u origin main
```

Check `git status` before pushing. `.env` and `server.env` are ignored, but never force-add them.

## Troubleshooting

- **Dashboard says it cannot reach the server:** start `python server.py`, then open the dashboard from the same host and port. Check <http://localhost:8080/health>.
- **Dashboard prompts repeatedly for a password:** verify `DASHBOARD_PASSWORD`, then restart the server and authenticate as `admin`.
- **Brevo reports disconnected:** confirm `BREVO_API_KEY` is in the `server.env` beside the running `server.py`, then restart. Never print the key to logs.
- **Live sending is blocked:** set a sender email and numeric list ID as well as the API key. A key by itself is not enough. Keep live mode off until all three are correct.
- **Confirmation links point to localhost:** set `PUBLIC_BASE_URL` to the deployed HTTPS origin, then restart.
- **Campaign does not run at its scheduled time:** keep the Python process or Docker container running; the local scheduler runs inside that process.
- **A campaign fails:** inspect the dashboard activity and the server terminal for Brevo API errors. Confirm the sender is verified and that the API key has permission for contacts and transactional email.

The app does not send real marketing email in test mode. Never use it with a list unless you can substantiate subscriber consent.
