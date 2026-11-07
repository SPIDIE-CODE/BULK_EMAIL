#!/usr/bin/env python3
"""Consent-first Brevo email dashboard. Python stdlib only."""
import base64
import csv
import hmac
import io
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parent


def load_env_file(path):
    """Load simple KEY=VALUE config without overwriting process environment."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


# Supports both common filenames. The OS/container environment takes precedence.
load_env_file(ROOT / "server.env")
load_env_file(ROOT / ".env")

DB_PATH = Path(os.environ.get("DATABASE_PATH", ROOT / "data" / "dashboard.sqlite3"))
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "127.0.0.1")
BASE_URL = os.environ.get("PUBLIC_BASE_URL", f"http://localhost:{PORT}").rstrip("/")
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
BREVO_SENDER_EMAIL = os.environ.get("BREVO_SENDER_EMAIL", "")
BREVO_SENDER_NAME = os.environ.get("BREVO_SENDER_NAME", "Email Team")
BREVO_LIST_ID = os.environ.get("BREVO_LIST_ID", "")
BREVO_WELCOME_TEMPLATE_ID = os.environ.get("BREVO_WELCOME_TEMPLATE_ID", "")
LIVE_SENDS_ENABLED = os.environ.get("LIVE_SENDS_ENABLED", "false").lower() == "true"
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
WEBHOOK_TOKEN = os.environ.get("BREVO_WEBHOOK_TOKEN", "")
DB_LOCK = threading.RLock()
SIGNUP_ATTEMPTS = {}
SIGNUP_LIMIT = 30
SIGNUP_WINDOW = 900


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS subscribers (
          id INTEGER PRIMARY KEY, email TEXT UNIQUE COLLATE NOCASE NOT NULL,
          name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
          consent_source TEXT NOT NULL DEFAULT '', consent_at TEXT, created_at TEXT NOT NULL,
          confirm_token TEXT UNIQUE, unsubscribe_token TEXT UNIQUE, unsubscribed_at TEXT, brevo_id TEXT
        );
        CREATE TABLE IF NOT EXISTS campaigns (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, subject TEXT NOT NULL,
          body TEXT NOT NULL, scheduled_at TEXT, status TEXT NOT NULL DEFAULT 'draft',
          created_at TEXT NOT NULL, sent_at TEXT, recipient_count INTEGER DEFAULT 0,
          delivered_count INTEGER DEFAULT 0, open_count INTEGER DEFAULT 0,
          click_count INTEGER DEFAULT 0, bounce_count INTEGER DEFAULT 0,
          test_mode INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, campaign_id INTEGER, email TEXT NOT NULL,
          event_type TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        cols = {r[1] for r in c.execute("PRAGMA table_info(subscribers)")}
        if "unsubscribe_token" not in cols:
            c.execute("ALTER TABLE subscribers ADD COLUMN unsubscribe_token TEXT")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS subscribers_unsubscribe_token ON subscribers(unsubscribe_token)")
        for row in c.execute("SELECT id FROM subscribers WHERE unsubscribe_token IS NULL").fetchall():
            c.execute("UPDATE subscribers SET unsubscribe_token=? WHERE id=?", (secrets.token_urlsafe(32), row["id"]))
        if not c.execute("SELECT 1 FROM campaigns LIMIT 1").fetchone():
            c.execute("INSERT INTO campaigns(name,subject,body,status,created_at,test_mode) VALUES(?,?,?,?,?,1)",
                      ("Welcome series", "Welcome to our updates", "<h1>Thanks for joining</h1><p>We’re glad you’re here.</p><p><a href=\"{{unsubscribe_url}}\">Unsubscribe</a></p>", "draft", now()))


def brevo_request(path, payload=None, method=None):
    if not BREVO_API_KEY:
        raise RuntimeError("BREVO_API_KEY is not configured")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request("https://api.brevo.com/v3/" + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"api-key": BREVO_API_KEY, "accept": "application/json", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Brevo API returned HTTP {e.code}") from e


def send_email(to_email, subject, html, text=None, tags=None):
    payload = {"sender": {"email": BREVO_SENDER_EMAIL, "name": BREVO_SENDER_NAME},
        "to": [{"email": to_email}], "subject": subject, "htmlContent": html,
        "textContent": text or "Please view this email in an HTML-capable email client."}
    if tags: payload["tags"] = tags
    return brevo_request("smtp/email", payload)


def add_event(c, campaign_id, email, event_type, details=""):
    c.execute("INSERT INTO events(campaign_id,email,event_type,details,created_at) VALUES(?,?,?,?,?)",
              (campaign_id, email, event_type, details, now()))


def send_welcome(email):
    if not LIVE_SENDS_ENABLED:
        return "test"
    if not BREVO_WELCOME_TEMPLATE_ID:
        # Uses a simple, fixed welcome message. Campaign template editor is independent.
        send_email(email, "Welcome", f'<h1>Welcome!</h1><p>Thanks for confirming your subscription.</p><p><a href="{unsubscribe_url(email)}">Unsubscribe</a></p>')
    else:
        brevo_request("smtp/email", {"sender": {"email": BREVO_SENDER_EMAIL, "name": BREVO_SENDER_NAME},
            "to": [{"email": email}], "templateId": int(BREVO_WELCOME_TEMPLATE_ID),
            "params": {"UNSUBSCRIBE_URL": unsubscribe_url(email)}})
    return "live"


def live_ready():
    return bool(BREVO_API_KEY and BREVO_SENDER_EMAIL and BREVO_LIST_ID.isdigit())


def unsubscribe_url(email):
    with db() as c:
        row = c.execute("SELECT unsubscribe_token FROM subscribers WHERE email=?", (email,)).fetchone()
    return f"{BASE_URL}/unsubscribe?token={quote(row['unsubscribe_token'])}" if row and row["unsubscribe_token"] else BASE_URL + "/unsubscribe"


def sync_confirmed_contact(email):
    if BREVO_LIST_ID:
        brevo_request("contacts", {"email": email, "listIds": [int(BREVO_LIST_ID)], "updateEnabled": True})


def sync_brevo_unsubscribe(email):
    if BREVO_API_KEY:
        brevo_request("contacts/" + quote(email, safe=""), {"emailBlacklisted": True}, method="PUT")


def process_campaign(campaign_id):
    with DB_LOCK, db() as c:
        campaign = c.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not campaign or campaign["status"] != "scheduled":
            return
        if LIVE_SENDS_ENABLED and not live_ready():
            c.execute("UPDATE campaigns SET status='blocked',sent_at=? WHERE id=?", (now(), campaign_id))
            add_event(c, campaign_id, "system", "blocked", "Live mode is enabled but Brevo credentials, sender, or numeric list ID are incomplete")
            return
        # Claim the campaign before sending so concurrent requests cannot dispatch it twice.
        c.execute("UPDATE campaigns SET status='sending' WHERE id=?", (campaign_id,))
        recipients = c.execute("SELECT email FROM subscribers WHERE status='confirmed' ORDER BY id").fetchall()
        subject, body = campaign["subject"], campaign["body"]
    sent = failures = 0
    for r in recipients:
        email = r["email"]
        with db() as c:
            if c.execute("SELECT 1 FROM subscribers WHERE email=? AND status='confirmed'", (email,)).fetchone() is None:
                continue
        if LIVE_SENDS_ENABLED:
            try:
                unsub = unsubscribe_url(email)
                html = body.replace("{{unsubscribe_url}}", unsub)
                if "{{unsubscribe_url}}" not in body:
                    html += f'<hr><p style="font-size:12px;color:#777">You are receiving this because you confirmed your subscription. <a href="{unsub}">Unsubscribe</a></p>'
                send_email(email, subject, html, tags=[f"letterform-campaign-{campaign_id}"])
                with db() as c: add_event(c, campaign_id, email, "sent")
                sent += 1
            except Exception as e:
                with db() as c: add_event(c, campaign_id, email, "error", str(e)[:200])
                failures += 1
        else:
            with db() as c: add_event(c, campaign_id, email, "test_queued", "No message sent; test mode")
            sent += 1
    with db() as c:
        status = ("failed" if sent == 0 and failures else "partial" if failures else "empty" if sent == 0 else "sent") if LIVE_SENDS_ENABLED else "test_complete"
        c.execute("UPDATE campaigns SET status=?,sent_at=?,recipient_count=?,test_mode=? WHERE id=?",
                  (status, now(), sent, 0 if LIVE_SENDS_ENABLED else 1, campaign_id))


class Handler(BaseHTTPRequestHandler):
    server_version = "ConsentMail/1.0"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def _auth(self):
        if not DASHBOARD_PASSWORD:
            return True
        expected = "Basic " + base64.b64encode(("admin:" + DASHBOARD_PASSWORD).encode()).decode()
        if hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            return True
        self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="Email Dashboard"'); self.end_headers()
        return False

    def _send(self, code, payload, content_type="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else (json.dumps(payload).encode() if content_type.startswith("application/json") else payload.encode())
        self.send_response(code); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin"); self.send_header("Cache-Control", "no-store")
        if content_type.startswith("text/html"): self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'")
        self.end_headers(); self.wfile.write(data)

    def _body(self):
        n = min(int(self.headers.get("Content-Length", "0")), 2_000_000)
        return self.rfile.read(n)

    def _json(self):
        try: return json.loads(self._body() or b"{}")
        except Exception: return {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/unsubscribe":
            token = parse_qs(u.query).get("token", [""])[0]
            with db() as c: row = c.execute("SELECT 1 FROM subscribers WHERE unsubscribe_token=?", (token,)).fetchone() if token else None
            if not row: return self._send(400, "<h1>Invalid unsubscribe link</h1>", "text/html; charset=utf-8")
            page = f'<h1>Manage email preferences</h1><p>Confirm that you want to unsubscribe from these email updates.</p><form method="post" action="/unsubscribe"><input type="hidden" name="token" value="{token}"><button type="submit">Confirm unsubscribe</button></form>'
            return self._send(200, page, "text/html; charset=utf-8")
        if u.path == "/confirm":
            token = parse_qs(u.query).get("token", [""])[0]
            with db() as c: row = c.execute("SELECT 1 FROM subscribers WHERE confirm_token=? AND status='pending'", (token,)).fetchone() if token else None
            if not row: return self._send(400, "<h1>Link expired or invalid</h1>", "text/html; charset=utf-8")
            page = f'<h1>Confirm your subscription</h1><p>Please confirm that you want to receive email updates.</p><form method="post" action="/confirm"><input type="hidden" name="token" value="{token}"><button type="submit">Confirm subscription</button></form>'
            return self._send(200, page, "text/html; charset=utf-8")
        if u.path == "/health": return self._send(200, {"ok": True})
        if u.path == "/subscribe":
            return self._send(200, (ROOT / "templates" / "subscribe.html").read_bytes(), "text/html; charset=utf-8")
        if not self._auth(): return
        if u.path == "/" or u.path == "/index.html":
            return self._send(200, (ROOT / "templates" / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/status":
            return self._send(200, {"connected": bool(BREVO_API_KEY), "liveReady": live_ready(), "liveEnabled": LIVE_SENDS_ENABLED,
                "senderConfigured": bool(BREVO_SENDER_EMAIL), "passwordProtected": bool(DASHBOARD_PASSWORD),
                "baseUrl": BASE_URL, "listConfigured": bool(BREVO_LIST_ID)})
        if u.path == "/api/overview":
            with db() as c:
                counts = {x["status"]: x["n"] for x in c.execute("SELECT status,count(*) n FROM subscribers GROUP BY status")}
                stats = c.execute("SELECT count(*) campaigns, sum(CASE WHEN status IN ('sent','test_complete') THEN 1 ELSE 0 END) sent, sum(open_count) opens, sum(click_count) clicks FROM campaigns").fetchone()
                events = [dict(x) for x in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 12")]
            return self._send(200, {"counts": counts, "stats": dict(stats), "events": events})
        if u.path == "/api/subscribers":
            with db() as c: rows = [dict(x) for x in c.execute("SELECT id,email,name,status,consent_source,consent_at,created_at,unsubscribed_at FROM subscribers ORDER BY id DESC LIMIT 1000")]
            return self._send(200, rows)
        if u.path.startswith("/api/subscribers/") and u.path.endswith("/confirmation-link"):
            try: sid = int(u.path.split("/")[3])
            except Exception: return self._send(400, {"error": "Invalid subscriber id"})
            with db() as c: row = c.execute("SELECT confirm_token,status FROM subscribers WHERE id=?", (sid,)).fetchone()
            if not row or row["status"] != "pending" or not row["confirm_token"]: return self._send(404, {"error": "No pending confirmation link"})
            return self._send(200, {"url": f"{BASE_URL}/confirm?token={row['confirm_token']}"})
        if u.path == "/api/campaigns":
            with db() as c: rows = [dict(x) for x in c.execute("SELECT * FROM campaigns ORDER BY id DESC")]
            return self._send(200, rows)
        return self._send(404, {"error": "Not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/confirm":
            token = parse_qs(self._body().decode(errors="replace")).get("token", [""])[0]
            with DB_LOCK, db() as c:
                row = c.execute("SELECT * FROM subscribers WHERE confirm_token=? AND status='pending'", (token,)).fetchone() if token else None
                if not row: return self._send(400, "<h1>Link expired or invalid</h1>", "text/html; charset=utf-8")
                c.execute("UPDATE subscribers SET status='confirmed',consent_at=?,confirm_token=NULL WHERE id=?", (now(), row["id"]))
                c.execute("INSERT INTO events(email,event_type,details,created_at) VALUES(?,?,?,?)", (row["email"], "confirmed", "Double opt-in confirmed", now()))
                email = row["email"]
            try:
                if LIVE_SENDS_ENABLED: sync_confirmed_contact(email)
                outcome = send_welcome(email)
                with db() as c: add_event(c, None, email, "welcome_" + outcome)
            except Exception as e:
                with db() as c: add_event(c, None, email, "welcome_error", str(e)[:200])
            return self._send(200, "<h1>Subscription confirmed</h1><p>Thanks. Your email preferences are saved.</p>", "text/html; charset=utf-8")
        if u.path == "/unsubscribe":
            token = parse_qs(self._body().decode(errors="replace")).get("token", [""])[0]
            with DB_LOCK, db() as c:
                row = c.execute("SELECT email FROM subscribers WHERE unsubscribe_token=?", (token,)).fetchone() if token else None
                email = row["email"] if row else ""
                if email: c.execute("UPDATE subscribers SET status='unsubscribed',unsubscribed_at=? WHERE email=?", (now(), email))
            if not email: return self._send(400, "<h1>Invalid unsubscribe link</h1>", "text/html; charset=utf-8")
            if LIVE_SENDS_ENABLED:
                try: sync_brevo_unsubscribe(email)
                except Exception as e:
                    with db() as c: add_event(c, None, email, "brevo_unsubscribe_sync_error", str(e)[:200])
            return self._send(200, "<h1>You’re unsubscribed</h1><p>Your preference has been saved.</p>", "text/html; charset=utf-8")
        if u.path != "/api/signup" and not self._auth(): return
        data = self._json()
        if u.path == "/api/signup":
            ip = self.client_address[0] if getattr(self, "client_address", None) else "local"
            timestamp = time.time()
            recent = [t for t in SIGNUP_ATTEMPTS.get(ip, []) if timestamp - t < SIGNUP_WINDOW]
            if len(recent) >= SIGNUP_LIMIT:
                return self._send(429, {"error": "Too many signup attempts. Please try again later."})
            recent.append(timestamp); SIGNUP_ATTEMPTS[ip] = recent
            email = str(data.get("email", "")).strip().lower()
            name = str(data.get("name", "")).strip()[:120]
            if not data.get("consent") or "@" not in email or len(email) > 254:
                return self._send(400, {"error": "Enter a valid email and check the explicit consent box."})
            if LIVE_SENDS_ENABLED and not live_ready():
                return self._send(503, {"error": "Live sending needs BREVO_API_KEY, BREVO_SENDER_EMAIL, and BREVO_LIST_ID configured on the server."})
            token = secrets.token_urlsafe(32)
            with DB_LOCK, db() as c:
                if c.execute("SELECT 1 FROM subscribers WHERE email=?", (email,)).fetchone():
                    return self._send(409, {"error": "This email is already in the list or suppression table."})
                c.execute("INSERT INTO subscribers(email,name,status,consent_source,created_at,confirm_token,unsubscribe_token) VALUES(?,?,?,?,?,?,?)",
                    (email, name, "pending", "Dashboard signup", now(), token, secrets.token_urlsafe(32)))
                c.execute("INSERT INTO events(email,event_type,details,created_at) VALUES(?,?,?,?)", (email, "signup_pending", "Explicit consent recorded; confirmation required", now()))
            confirm_url = f"{BASE_URL}/confirm?token={token}"
            if LIVE_SENDS_ENABLED:
                try:
                    send_email(email, "Confirm your subscription", f'<h1>Please confirm</h1><p><a href="{confirm_url}">Confirm my subscription</a></p>')
                except Exception as e:
                    with db() as c: add_event(c, None, email, "doi_error", str(e)[:200])
                    return self._send(502, {"error": "The signup is saved as pending, but Brevo could not send the confirmation email. Check server logs."})
                return self._send(201, {"message": "Confirmation email sent. This address remains pending until confirmed."})
            return self._send(201, {"message": "Saved as pending in test mode. No email was sent.", "confirmationUrl": confirm_url})
        if u.path == "/api/import":
            if LIVE_SENDS_ENABLED and not live_ready():
                return self._send(503, {"error": "Live sending needs BREVO_API_KEY, BREVO_SENDER_EMAIL, and BREVO_LIST_ID configured on the server."})
            csv_text = str(data.get("csv", ""))
            if not data.get("consentAttested"):
                return self._send(400, {"error": "Confirm that every CSV contact explicitly opted in to these updates before importing."})
            try:
                reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
                if not reader.fieldnames: raise ValueError("CSV needs a header row with an email column.")
                if any(not isinstance(x, str) for x in reader.fieldnames):
                    raise ValueError("CSV header row contains an empty column name.")
                fields = {x.strip().lower(): x for x in reader.fieldnames}
                email_col = fields.get("email") or fields.get("email address")
                if not email_col: raise ValueError("CSV must have an email or email address column.")
                name_col = fields.get("name") or fields.get("first name") or fields.get("firstname")
                added = skipped = invalid = 0
                new_contacts = []
                with DB_LOCK, db() as c:
                    for row in reader:
                        email = (row.get(email_col) or "").strip().lower()
                        if "@" not in email or "." not in email.rsplit("@", 1)[-1] or len(email) > 254:
                            invalid += 1; continue
                        if c.execute("SELECT 1 FROM subscribers WHERE email=?", (email,)).fetchone(): skipped += 1; continue
                        token = secrets.token_urlsafe(32)
                        c.execute("INSERT INTO subscribers(email,name,status,consent_source,created_at,confirm_token,unsubscribe_token) VALUES(?,?,?,?,?,?,?)",
                                  (email, (row.get(name_col) or "").strip()[:120] if name_col else "", "pending", "CSV import: permission attested; confirmation required", now(), token, secrets.token_urlsafe(32)))
                        add_event(c, None, email, "import_pending", "Imported as pending; no marketing until confirmation")
                        new_contacts.append((email, token))
                        added += 1
                if LIVE_SENDS_ENABLED:
                    for email, token in new_contacts:
                        try:
                            confirm_url = f"{BASE_URL}/confirm?token={token}"
                            send_email(email, "Confirm your subscription", f'<h1>Please confirm</h1><p>You were added to our email updates after permission was attested by the list owner.</p><p><a href="{confirm_url}">Confirm my subscription</a></p>')
                            with db() as c: add_event(c, None, email, "doi_sent", "CSV import double opt-in")
                        except Exception as e:
                            with db() as c: add_event(c, None, email, "doi_error", str(e)[:200])
                return self._send(200, {"added": added, "duplicates": skipped, "invalid": invalid, "note": "Imported rows remain pending; CSV import alone is not consent."})
            except (ValueError, csv.Error) as e: return self._send(400, {"error": str(e)})
        if u.path == "/api/campaigns":
            name, subject, body = [str(data.get(k, "")).strip() for k in ("name", "subject", "body")]
            if not name or not subject or not body: return self._send(400, {"error": "Name, subject, and email body are required."})
            with db() as c:
                cur = c.execute("INSERT INTO campaigns(name,subject,body,status,created_at,test_mode) VALUES(?,?,?,?,?,?)",
                    (name[:120], subject[:200], body, "draft", now(), 0 if LIVE_SENDS_ENABLED else 1))
                cid = cur.lastrowid
            return self._send(201, {"id": cid})
        if u.path.startswith("/api/campaigns/") and u.path.endswith("/schedule"):
            if LIVE_SENDS_ENABLED and not live_ready():
                return self._send(503, {"error": "Live sending needs BREVO_API_KEY, BREVO_SENDER_EMAIL, and BREVO_LIST_ID configured on the server."})
            try: cid = int(u.path.split("/")[3])
            except Exception: return self._send(400, {"error": "Invalid campaign id"})
            scheduled = str(data.get("scheduledAt", ""))
            try:
                dt = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                if dt.timestamp() < time.time() + 60: raise ValueError()
            except Exception: return self._send(400, {"error": "Choose a future schedule time, at least one minute from now."})
            with db() as c:
                changed = c.execute("UPDATE campaigns SET status='scheduled',scheduled_at=?,test_mode=? WHERE id=? AND status IN ('draft','scheduled')", (dt.astimezone(timezone.utc).isoformat(timespec="seconds"), 0 if LIVE_SENDS_ENABLED else 1, cid)).rowcount
            if not changed: return self._send(404, {"error": "Draft campaign not found."})
            return self._send(200, {"scheduled": True, "mode": "live" if LIVE_SENDS_ENABLED else "test"})
        if u.path == "/api/webhooks/brevo":
            # Separate secret header is configured in the Brevo webhook definition.
            if not WEBHOOK_TOKEN or not hmac.compare_digest(self.headers.get("X-Webhook-Token", ""), WEBHOOK_TOKEN): return self._send(401, {"error": "Unauthorized webhook"})
            event = str(data.get("event", "unknown")).lower(); email = str(data.get("email", "")).strip().lower()
            mapping = {"unsubscribe": "unsubscribed", "unsubscribed": "unsubscribed", "hard_bounce": "bounced", "soft_bounce": "bounced", "spam": "unsubscribed", "complaint": "unsubscribed"}
            cid = data.get("campaign_id") or data.get("tag")
            if isinstance(cid, str) and cid.startswith("["):
                try: cid = json.loads(cid)
                except json.JSONDecodeError: pass
            if isinstance(cid, list): cid = cid[0] if cid else None
            if isinstance(cid, str) and cid.startswith("letterform-campaign-"):
                cid = cid.rsplit("-", 1)[-1]
            try: cid = int(cid) if cid else None
            except (TypeError, ValueError): cid = None
            with DB_LOCK, db() as c:
                if email and event in mapping:
                    c.execute("UPDATE subscribers SET status=?,unsubscribed_at=? WHERE email=?", (mapping[event], now(), email))
                if email:
                    add_event(c, cid, email, event[:60], "Brevo webhook")
                    counters = {"delivered": "delivered_count", "opened": "open_count", "unique_opened": "open_count", "click": "click_count", "clicked": "click_count", "hard_bounce": "bounce_count", "soft_bounce": "bounce_count"}
                    col = counters.get(event)
                    if col and cid:
                        c.execute(f"UPDATE campaigns SET {col}={col}+1 WHERE id=?", (cid,))
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "Not found"})

    def do_PUT(self):
        if not self._auth(): return
        u = urlparse(self.path); data = self._json()
        if u.path.startswith("/api/campaigns/"):
            try: cid = int(u.path.split("/")[3])
            except Exception: return self._send(400, {"error": "Invalid campaign id"})
            name, subject, body = [str(data.get(k, "")).strip() for k in ("name", "subject", "body")]
            with db() as c:
                changed = c.execute("UPDATE campaigns SET name=?,subject=?,body=? WHERE id=? AND status IN ('draft','scheduled')", (name[:120], subject[:200], body, cid)).rowcount
            return self._send(200 if changed else 404, {"saved": bool(changed)})
        return self._send(404, {"error": "Not found"})

    def do_DELETE(self):
        if not self._auth(): return
        u = urlparse(self.path)
        if u.path.startswith("/api/campaigns/"):
            try: cid = int(u.path.split("/")[3])
            except Exception: return self._send(400, {"error": "Invalid campaign id"})
            with db() as c: changed = c.execute("DELETE FROM campaigns WHERE id=? AND status='draft'", (cid,)).rowcount
            return self._send(200 if changed else 409, {"deleted": bool(changed)})
        return self._send(404, {"error": "Not found"})


def scheduler():
    while True:
        try:
            with DB_LOCK, db() as c:
                rows = c.execute("SELECT id FROM campaigns WHERE status='scheduled' AND scheduled_at<=?", (now(),)).fetchall()
            for row in rows: process_campaign(row["id"])
        except Exception as e: print("scheduler error:", e)
        time.sleep(15)


if __name__ == "__main__":
    init_db()
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"ConsentMail dashboard at http://{HOST}:{PORT} (live sends {'enabled' if LIVE_SENDS_ENABLED else 'disabled'})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


