"""World Herb Library cataloging workbench (Flask, localhost, single user).

The tool supports one core workflow: reconciling a private herbal library
against the World Herb Library (WHL), locating existing scans, and preparing
new catalog entries for submission to WHL.

Data sources (all local):
  - whl_catalog.csv          WHL catalogue export (+ output/whl_scraped.json
                             from the website API, + output/whl_corrections.json
                             overlay for the user's edits)
  - output/ch_library.json   the CH private-library spreadsheet, converted
  - output/manual_entries.json  hand-entered books
  - copyright_renewals.csv   offline copyright-renewal check
  - output/ol_search.db      consolidated Open Library editions index
  - output/whl_builds.json   catalog entries being prepared for submission

Run with python3:
    python3 tools/whl_explorer/server.py
then open http://127.0.0.1:5001
"""
from __future__ import annotations

import collections
import contextlib
import hashlib
import importlib.util
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

# Make tools/ importable for the shared helpers.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import capture_pipeline as capture  # noqa: E402
import catalog_checks as checks  # noqa: E402
import cloud_defaults  # noqa: E402
import copyright_registration as copyreg  # noqa: E402
import libcommon as lib  # noqa: E402
import r2_store as r2  # noqa: E402
import store_sync  # noqa: E402
import supabase_auth as sauth  # noqa: E402
import supabase_sync as sbase  # noqa: E402
import ol_client  # noqa: E402
import scan_search  # noqa: E402
import whl_client  # noqa: E402
import whl_scrape  # noqa: E402

# NYPL Catalog of Copyright Entries dataset (optional, for the copyright tag's
# registration half): drop the parsed XML tree under <DATA_ROOT>/nypl_cce/.
copyreg.NYPL_DIR = str(lib.DATA_ROOT / "nypl_cce")

def _flask_app():
    # When frozen (PyInstaller), templates/ and static/ are bundled at the
    # extraction root (sys._MEIPASS), not next to this module — point Flask
    # there. In a normal checkout Flask's default relative lookup is correct.
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        return Flask(__name__,
                     template_folder=str(base / "templates"),
                     static_folder=str(base / "static"))
    return Flask(__name__)


app = _flask_app()
# Jinja compiles index.html once and caches it when debug is off, while static/
# is read from disk on every request. Editing the template therefore served a NEW
# app.js against an OLD DOM until someone restarted the server -- and one missing
# element kills every listener registered after it in the same init function.
# Stat the template instead: one stat per page load, and the whole class of bug
# (twice now: the reason popover, then the OCR Layout button) goes away.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


_TRUSTED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


@app.before_request
def _reject_untrusted_host():
    """Reject DNS-rebinding requests before any local API can run.

    The administrative app is intentionally bound to IPv4 loopback.  Checking
    only credential routes is insufficient because other endpoints expose
    client state, local PDFs, and a fetch proxy.
    """
    host = (request.host or "").partition(":")[0].lower().rstrip(".")
    if host not in _TRUSTED_LOOPBACK_HOSTS:
        abort(403)


@app.after_request
def _static_cache_headers(resp):
    """Long-lived caching for /static so the desktop shell's Chromium keeps its
    HTTP cache — and, for app.js, the V8 compiled-code cache — across launches
    (the shell now reuses a stable sidecar port, so the origin persists).

    Only URLs carrying the ?v= mtime token get `immutable`: their URL changes
    whenever the content does, so a year is safe. Un-tokened static files
    (fonts, the favicon) get a day, so an app update can still refresh them.

    Success responses only: a transient failure (e.g. antivirus briefly locking
    a freshly installed app.js) must not become a year-long cached error for a
    URL whose ?v= token won't change on retry.
    """
    if resp.status_code not in (200, 304):
        return resp
    if request.path.startswith("/static/"):
        if request.args.get("v"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# --- application log ------------------------------------------------------------
# The app had no logging at all: a failing background job left its reason in a
# job dict that only one poller ever read. Everything now lands in a bounded
# in-memory ring, which the Info tab's console reads over /api/log. Nothing is
# written to disk -- this is a console, not an audit trail.

_LOG_CAP = 1000
_log_lock = threading.Lock()
_log_seq = 0
_log_ring: collections.deque = collections.deque(maxlen=_LOG_CAP)

_LOG_LEVELS = {
    logging.DEBUG: "debug", logging.INFO: "info", logging.WARNING: "warn",
    logging.ERROR: "error", logging.CRITICAL: "error",
}


def _log_put(level: str, msg: str, src: str = "server") -> None:
    global _log_seq
    with _log_lock:
        _log_seq += 1
        _log_ring.append({"seq": _log_seq, "ts": time.time(), "level": level,
                          "src": src, "msg": str(msg)[:4000]})


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# 127.0.0.1 - - [09/Jul/2026 23:46:01] "GET /api/builds HTTP/1.1" 200 -
_REQLINE = re.compile(r'^\S+ - - \[[^\]]+\] "(\S+) (.*?) HTTP/[\d.]+" (\d{3})')


class _RingHandler(logging.Handler):
    """Tee every log record into the ring the console reads."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
        except Exception:            # a broken __repr__ must not kill the logger
            return
        src = "http" if record.name.startswith("werkzeug") else "server"
        if src == "http":
            # the console polls /api/log; logging that poll would feed itself
            if "/api/log" in msg:
                return
            msg = _ANSI.sub("", msg)      # werkzeug colours its status codes
            # drop the ip and the timestamp: the console shows its own clock
            m = _REQLINE.match(msg)
            if m:
                msg = f"{m.group(1)} {m.group(2)} -> {m.group(3)}"
        # request lines are debug noise; only their failures deserve attention
        level = _LOG_LEVELS.get(record.levelno, "info")
        if src == "http" and level == "info":
            level = "debug"
        _log_put(level, msg, src)


log = logging.getLogger("whl")


def _init_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    ring = _RingHandler()
    ring.setLevel(logging.DEBUG)
    root.addHandler(ring)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(stream)
    # werkzeug logs one INFO line per request; keep them, demoted to debug
    logging.getLogger("werkzeug").setLevel(logging.INFO)


def _apply_log_level() -> None:
    """Root logger -> DEBUG when Settings > Advanced enables verbose logging,
    else INFO. Read straight off client_state so it applies at startup and
    after every settings push."""
    try:
        s = lib.load_json(lib.CLIENT_STATE_PATH, {}).get("settings", {})
        verbose = bool(s.get("verboseLogging"))
    except Exception:
        verbose = False
    logging.getLogger().setLevel(logging.DEBUG if verbose else logging.INFO)


_init_logging()
_apply_log_level()


@app.errorhandler(Exception)
def _log_unhandled(exc):
    """Any route that raises now says so in the console instead of vanishing."""
    if isinstance(exc, HTTPException):
        if exc.code and exc.code >= 500:
            log.error("%s %s -> %s", request.method, request.path, exc)
        return exc
    log.error("%s %s failed", request.method, request.path, exc_info=exc)
    return jsonify({"ok": False, "error": str(exc)}), 500


# --- activity feed --------------------------------------------------------------
# Append-only, and deliberately NOT part of client_state: that document is
# overwritten wholesale on every PUT, so events folded into it would be clobbered.
# One JSON object per line, oldest first.

ACTIVITY_PATH = lib.DATA_ROOT / "output" / "activity.jsonl"
_activity_lock = threading.Lock()


def _actor() -> str:
    """Who is acting. A signed-in session is the identity; the X-WHL-Actor
    header (the name from Settings) covers working locally, and a background
    job passes its own actor explicitly."""
    ses = (_auth_doc().get("session") or {})
    name = str(ses.get("display_name") or "").strip()
    if not name:
        try:
            name = (request.headers.get("X-WHL-Actor") or "").strip()
        except RuntimeError:        # outside a request context (background jobs)
            name = ""
    return name[:60] or "Unnamed user"


def activity(verb: str, subject: str, n: int = 1, actor: str | None = None,
             detail: str = "") -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "actor": actor or _actor(), "verb": verb, "subject": subject, "n": int(n)}
    if detail:                      # what exactly (book titles etc.), for the
        rec["detail"] = str(detail)[:200]   # feed's expandable per-event view
    try:
        with _activity_lock:
            ACTIVITY_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(ACTIVITY_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:        # the feed is a nicety; never break the write
        log.warning("activity log write failed: %s", exc)
    _push_wake.set()                # the cloud mirror catches up in the background


def _activity_lines() -> list[str]:
    if not ACTIVITY_PATH.is_file():
        return []
    with _activity_lock:
        with open(ACTIVITY_PATH, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readlines()


@app.route("/api/activity")
def api_activity():
    """The newest events, newest first. Signed in, the shared cloud feed is the
    source (it includes everyone, yourself included, because every local event
    is mirrored up); the local file covers signed-out and offline. Bad lines
    are skipped, not fatal."""
    try:
        limit = max(1, min(500, int(request.args.get("limit") or 200)))
    except ValueError:
        limit = 200
    cloud = _cloud_events(limit)
    if cloud is not None:
        # events written since the pusher last ran are only in the local file;
        # the cursor marks exactly what the cloud already has, so prepending
        # the lines beyond it duplicates nothing
        cursor = int(_auth_doc().get("push_cursor") or 0)
        fresh: list[dict] = []
        for line in _activity_lines()[cursor:]:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                fresh.append(rec)
        # merge by time: after an offline stretch the unpushed tail can be
        # OLDER than cloud events other machines wrote in the meantime
        merged = sorted(fresh + cloud,
                        key=lambda r: str(r.get("ts") or ""), reverse=True)
        return jsonify({"ok": True, "cloud": True, "events": merged[:limit]})
    rows: list[dict] = []
    for line in _activity_lines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    rows.reverse()
    return jsonify({"ok": True, "cloud": False, "events": rows})


# --- accounts -------------------------------------------------------------------
# A real Supabase user, signed in from the desktop. The session (plus the
# activity push cursor) lives in DATA_ROOT/output/auth_session.json — device
# state, gitignored like client_state.json. Contributions reach the cloud as
# the authenticated user, so row-level security applies and no policy has to
# trust a plain-text name. The Mistral key is the one account-level credential:
# it syncs through the private profile_secrets row shared with Book Capture.

AUTH_SESSION_PATH = lib.DATA_ROOT / "output" / "auth_session.json"
_auth_lock = threading.RLock()
_push_wake = threading.Event()


def _auth_doc() -> dict:
    # No lock: save_json is atomic (temp file + os.replace), so a read can
    # never see a torn file. Taking _auth_lock here would make _actor() — and
    # with it every activity-logging request — block behind a token refresh's
    # network round-trip. Read-modify-write sequences still take the lock.
    return lib.load_json(AUTH_SESSION_PATH, {}) or {}


def _auth_cfg() -> dict | None:
    """GoTrue wants a public project API key in ``apikey``.

    Nothing configured is NOT unconfigured: the app ships knowing its own
    cloud (cloud_defaults), so accounts work on a fresh install with no keys
    entered. Settings override; a custom project URL with no key of its own
    stays unconfigured rather than pairing with the default key. The owner
    service credential is deliberately ignored here: normal account and phone
    capture traffic must never depend on it."""
    s = _client_settings()
    url = str(s.get("supabaseUrl") or "").strip() or cloud_defaults.SUPABASE_URL
    key = str(s.get("supabaseAnonKey") or "").strip()
    if not key and url == cloud_defaults.SUPABASE_URL:
        key = cloud_defaults.SUPABASE_ANON_KEY
    return {"url": url, "key": key} if url and key else None


def _email_confirm_redirect() -> str:
    """Where the signup-confirmation email link should land: the public
    website's confirmation page, since the desktop has no stable URL of its
    own. Overridable (cloudSiteUrl) for a fork on its own project + site."""
    base = (str(_client_settings().get("cloudSiteUrl") or "").strip().rstrip("/")
            or cloud_defaults.WEBSITE_URL)
    return base + cloud_defaults.EMAIL_CONFIRM_PATH


def _auth_session() -> dict | None:
    """A live session, refreshed when stale. Refresh tokens rotate, so the
    refreshed session is persisted before anything uses it, under the lock —
    two concurrent refreshes with one token can revoke the whole family."""
    cfg = _auth_cfg()
    if not cfg:
        return None
    with _auth_lock:
        doc = _auth_doc()
        ses = doc.get("session")
        if not ses or not ses.get("refresh_token"):
            return None
        if time.time() < float(ses.get("expires_at") or 0) - 90:
            return ses
        try:
            fresh = sauth.refresh(cfg, ses["refresh_token"])
        except sauth.AuthError as exc:
            # Only a definitive rejection kills the session. A transport
            # failure (offline, DNS, captive portal) says nothing about the
            # refresh token — destroying it here would sign the user out
            # every time the laptop slept through a token expiry.
            if exc.status in (400, 401, 403):
                log.warning("session refresh rejected (%s) — signed out", exc)
                doc.pop("session", None)
                lib.save_json(AUTH_SESSION_PATH, doc)
            else:
                log.warning("session refresh unavailable (%s) — will retry", exc)
            return None
        # the stored name came from the profiles row; a refresh only knows
        # user_metadata, which may be blank or stale
        fresh["display_name"] = (ses.get("display_name")
                                 or fresh.get("display_name") or "")
        doc["session"] = fresh
        lib.save_json(AUTH_SESSION_PATH, doc)
        return fresh


def _adopt_profile(cfg: dict, ses: dict) -> dict:
    """The profiles row is the shared display name. Prefer it; seed it from
    signup metadata or the email when it does not exist yet."""
    name = ses.get("display_name") or ""
    try:
        rows = sauth.rest(cfg, ses["access_token"], "GET",
                          f"profiles?id=eq.{ses['user_id']}&select=display_name") or []
        if rows and str(rows[0].get("display_name") or "").strip():
            name = str(rows[0]["display_name"]).strip()
        else:
            name = name or ses.get("email", "").split("@")[0]
            sauth.rest(cfg, ses["access_token"], "POST", "profiles?on_conflict=id",
                       [{"id": ses["user_id"], "display_name": name}],
                       prefer="resolution=merge-duplicates,return=minimal")
    except sauth.AuthError as exc:
        log.warning("profile lookup failed: %s", exc)
        name = name or ses.get("email", "").split("@")[0]
    return dict(ses, display_name=name[:60])


def _store_session(ses: dict) -> None:
    with _auth_lock:
        doc = _auth_doc()
        doc["session"] = ses
        # Share from here on — pushing the backlog would stamp this user's id
        # onto events they may not have made. Applies on the first sign-in AND
        # whenever a different account takes over the machine.
        if "push_cursor" not in doc or doc.get("account_id") != ses["user_id"]:
            doc["push_cursor"] = len(_activity_lines())
        doc["account_id"] = ses["user_id"]
        lib.save_json(AUTH_SESSION_PATH, doc)
    _push_wake.set()


@app.route("/api/auth/status")
def api_auth_status():
    """Who is signed in, from the stored session — no refresh, no network.
    Offline must not read as signed-out: the tokens are still on disk and the
    pusher refreshes them when it actually needs them."""
    cfg = _auth_cfg()
    ses = (_auth_doc().get("session") or {}) if cfg else {}
    return jsonify({"ok": True, "cloud": bool(cfg),
                    "signed_in": bool(ses.get("refresh_token")),
                    "email": ses.get("email", ""),
                    "display_name": ses.get("display_name", "")})


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    cfg = _auth_cfg()
    if not cfg:
        return jsonify({"ok": False, "error": "Supabase is not configured (Settings > Sync)"}), 400
    p = request.get_json(silent=True) or {}
    email = str(p.get("email") or "").strip()
    password = str(p.get("password") or "")
    if not email or not password:
        return jsonify({"ok": False, "error": "email and password are both required"}), 400
    try:
        ses = sauth.sign_in(cfg, email, password)
    except sauth.AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    ses = _adopt_profile(cfg, ses)
    _store_session(ses)
    _sync_profile_mistral_key()
    activity("signed in to", "the cloud", actor=ses["display_name"] or email)
    return jsonify({"ok": True, "email": ses["email"],
                    "display_name": ses["display_name"]})


@app.route("/api/auth/signup", methods=["POST"])
def api_auth_signup():
    cfg = _auth_cfg()
    if not cfg:
        return jsonify({"ok": False, "error": "Supabase is not configured (Settings > Sync)"}), 400
    p = request.get_json(silent=True) or {}
    email = str(p.get("email") or "").strip()
    password = str(p.get("password") or "")
    name = str(p.get("display_name") or "").strip()[:60]
    if not email or not password:
        return jsonify({"ok": False, "error": "email and password are both required"}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "password must be at least 6 characters"}), 400
    try:
        ses = sauth.sign_up(cfg, email, password, name,
                            redirect_to=_email_confirm_redirect())
    except sauth.AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if ses is None:     # project requires email confirmation (the default)
        return jsonify({"ok": True, "confirm": True})
    ses = _adopt_profile(cfg, ses)
    _store_session(ses)
    _sync_profile_mistral_key()
    activity("signed in to", "the cloud", actor=ses["display_name"] or email)
    return jsonify({"ok": True, "email": ses["email"],
                    "display_name": ses["display_name"]})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    cfg = _auth_cfg()
    with _auth_lock:
        doc = _auth_doc()
        ses = doc.pop("session", None)
        # push_cursor stays: signing back in resumes the mirror, no re-push
        lib.save_json(AUTH_SESSION_PATH, doc)
    if cfg and ses and ses.get("access_token"):
        sauth.sign_out(cfg, ses["access_token"])
    return jsonify({"ok": True})


# --- the activity mirror: local jsonl -> cloud events table ----------------------
# The local file is the source of truth; the cursor in auth_session.json marks
# how much of it the cloud already has. Push failures leave the cursor alone,
# so offline work catches up on the next wake. One daemon thread, poked by
# activity() and by sign-in, with a slow heartbeat for retries.

def _push_events_once() -> None:
    cfg = _auth_cfg()
    ses = _auth_session() if cfg else None
    if not cfg or not ses:
        return
    with _auth_lock:
        cursor = int(_auth_doc().get("push_cursor") or 0)
    lines = _activity_lines()
    if cursor > len(lines):        # the file was truncated by hand: resync
        cursor = len(lines)
        with _auth_lock:
            doc = _auth_doc()
            doc["push_cursor"] = cursor
            lib.save_json(AUTH_SESSION_PATH, doc)
    if cursor >= len(lines):
        return
    chunk = lines[cursor:cursor + 200]
    batch = []
    for line in chunk:
        try:
            r = json.loads(line)
        except ValueError:
            continue               # a bad line is skipped, and the cursor passes it
        if not isinstance(r, dict):
            continue               # `[1]` or `"x"` would AttributeError below
        ev = {"actor": str(r.get("actor") or "")[:60], "actor_id": ses["user_id"],
              "verb": str(r.get("verb") or ""), "subject": str(r.get("subject") or "")}
        ev["detail"] = str(r.get("detail") or "")[:200]
        try:
            ev["n"] = int(r.get("n") or 1)
        except (ValueError, TypeError):
            ev["n"] = 1
        ts = str(r.get("ts") or "").strip()
        if ts:
            ev["at"] = ts          # absent -> the column's now() default
        batch.append(ev)
    if batch:
        try:
            sauth.rest(cfg, ses["access_token"], "POST", "events", batch,
                       prefer="return=minimal")
        except sauth.AuthError as exc:
            if exc.status == 400:
                # the DATA was rejected (e.g. a hand-mangled timestamp);
                # retrying the same lines forever would wedge the mirror
                log.warning("event batch rejected (%s) — skipping %d line(s)",
                            exc, len(chunk))
            else:
                raise              # transient: retry next wake, cursor untouched
    with _auth_lock:
        doc = _auth_doc()
        doc["push_cursor"] = cursor + len(chunk)
        lib.save_json(AUTH_SESSION_PATH, doc)
    _cloud_feed_cache["at"] = 0.0  # the feed should see what was just pushed
    if cursor + len(chunk) < len(lines):
        _push_wake.set()


def _push_events_loop() -> None:
    while True:
        _push_wake.wait(timeout=300)
        _push_wake.clear()
        try:
            _push_events_once()
        except Exception as exc:   # offline, RLS, anything — retry on next wake
            log.warning("activity push deferred: %s", exc)


# The cloud read is cached briefly: Home re-renders freely, and the feed does
# not need to be fresher than the pusher that feeds it. Failures are cached
# too, or an unreachable Supabase would cost every feed load a full timeout.
_cloud_feed_cache: dict = {"at": 0.0, "rows": [], "fail_at": 0.0}


def _cloud_events(limit: int) -> list[dict] | None:
    """The shared feed, or None when signed out / offline (caller falls back)."""
    cfg = _auth_cfg()
    ses = _auth_session() if cfg else None
    if not cfg or not ses:
        return None
    now = time.time()
    if now - _cloud_feed_cache["at"] < 15:
        return _cloud_feed_cache["rows"]
    if now - _cloud_feed_cache["fail_at"] < 30:
        return None
    try:
        rows = sauth.rest(cfg, ses["access_token"], "GET",
                          "events?select=at,actor,verb,subject,n,detail"
                          f"&order=at.desc&limit={int(limit)}", timeout=8.0) or []
    except sauth.AuthError as exc:
        log.warning("cloud feed unavailable: %s", exc)
        _cloud_feed_cache["fail_at"] = now
        return None
    out = [{"ts": r.get("at"), "actor": r.get("actor"), "verb": r.get("verb"),
            "subject": r.get("subject"), "n": r.get("n"),
            "detail": r.get("detail") or ""} for r in rows]
    _cloud_feed_cache.update(at=now, rows=out)
    return out


# --- review queue ----------------------------------------------------------------
# An attention mark is a personal flag; a REVIEW is a shared work item raised
# from the Q popover: visible to every contributor, carrying a comment thread
# and an explicit resolution (the Google-Docs pattern, minus accounts).
# kind/ref name the marked item so resolving can clear the underlying mark:
#   key   -> a state.attn map key ("whl:12", "src:<url>", "ol:3" ...)
#   row   -> a checked/manual row id
#   build -> an editor build id

REVIEWS_PATH = lib.OUTPUT_DIR / "reviews.json"
_reviews_lock = threading.Lock()
_REVIEW_KINDS = ("key", "row", "build")


@app.route("/api/reviews")
def api_reviews_list():
    with _reviews_lock:
        reviews = lib.load_json(REVIEWS_PATH, {})
    return jsonify({"ok": True, "reviews": reviews})


@app.route("/api/reviews", methods=["POST"])
def api_reviews_create():
    payload = request.get_json(silent=True) or {}
    kind = str(payload.get("kind") or "")
    ref = str(payload.get("ref") or "").strip()
    if kind not in _REVIEW_KINDS or not ref:
        abort(400)
    key = f"{kind}:{ref}"
    label = str(payload.get("label") or "").strip()[:120]
    reason = str(payload.get("reason") or "").strip()[:500]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    created = False
    with _reviews_lock:
        reviews = lib.load_json(REVIEWS_PATH, {})
        # one OPEN review per item -- flagging again refreshes label/reason
        r = next((x for x in reviews.values()
                  if x.get("key") == key and x.get("status") == "open"), None)
        if r:
            r["label"] = label or r.get("label", "")
            if reason:
                r["reason"] = reason
        else:
            rid = lib.gen_id(set(reviews))
            r = reviews[rid] = {
                "id": rid, "key": key, "kind": kind, "ref": ref,
                "label": label, "reason": reason, "status": "open",
                "created_by": _actor(), "created_at": now,
                "resolved_by": "", "resolved_at": "", "comments": [],
            }
            created = True
        lib.save_json(REVIEWS_PATH, reviews)
    if created:
        activity("opened", "review", detail=label)
    return jsonify({"ok": True, "review": r})


@app.route("/api/reviews/<rid>/comment", methods=["POST"])
def api_reviews_comment(rid: str):
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text") or "").strip()[:1000]
    if not text:
        abort(400)
    with _reviews_lock:
        reviews = lib.load_json(REVIEWS_PATH, {})
        if rid not in reviews:
            abort(404)
        r = reviews[rid]
        r.setdefault("comments", []).append({
            "author": _actor(), "text": text,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        lib.save_json(REVIEWS_PATH, reviews)
    activity("commented on", "review", detail=r.get("label", ""))
    return jsonify({"ok": True, "review": r})


@app.route("/api/reviews/<rid>/resolve", methods=["POST"])
def api_reviews_resolve(rid: str):
    payload = request.get_json(silent=True) or {}
    resolved = bool(payload.get("resolved", True))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _reviews_lock:
        reviews = lib.load_json(REVIEWS_PATH, {})
        if rid not in reviews:
            abort(404)
        r = reviews[rid]
        # reopening must not sidestep the one-open-review-per-item rule
        if not resolved:
            dup = next((x for x in reviews.values()
                        if x is not r and x.get("key") == r.get("key")
                        and x.get("status") == "open"), None)
            if dup:
                return jsonify({"ok": False,
                                "error": "This item already has an open review"}), 409
        r["status"] = "resolved" if resolved else "open"
        r["resolved_by"] = _actor() if resolved else ""
        r["resolved_at"] = now if resolved else ""
        lib.save_json(REVIEWS_PATH, reviews)
    activity("resolved" if resolved else "reopened", "review",
             detail=r.get("label", ""))
    return jsonify({"ok": True, "review": r})


@app.route("/api/log")
def api_log():
    """Console feed: everything after ?since=<seq>. `next` is the new cursor."""
    try:
        since = int(request.args.get("since") or 0)
    except ValueError:
        since = 0
    with _log_lock:
        rows = [r for r in _log_ring if r["seq"] > since]
        last = _log_seq
        # seq starts at 1, so this also flags a since=0 catch-up (the client now
        # polls only while the Info tab is open) whenever the ring has already
        # evicted early lines — the console shows "(truncated)" instead of
        # silently presenting the newest window as the whole log.
        dropped = _log_ring and _log_ring[0]["seq"] > since + 1
    return jsonify({"ok": True, "entries": rows, "next": last, "dropped": bool(dropped)})


# --- the CH private-library catalogue -------------------------------------------

def _categories(row: dict) -> str:
    """Combine the CH Library KEY/KEY_2/KEY_3 category fields, de-duplicated."""
    seen_lower: set[str] = set()
    cats: list[str] = []
    for field in ("key", "key_2", "key_3"):
        val = str(row.get(field, "") or "").strip()
        if val and val.lower() not in seen_lower:
            seen_lower.add(val.lower())
            cats.append(val)
    return ", ".join(cats)


def _ch_row(idx: int, row: dict) -> dict:
    return {
        "idx": idx,
        "title": str(row.get("publication", "") or "").replace("_", " ").strip(),
        "subtitle": "",
        "author": str(row.get("authors", "") or "").strip(),
        "year": str(row.get("year_of_publication", "") or "").strip(),
        "edition": str(row.get("edition", "") or "").strip(),
        "publisher": str(row.get("publisher", "") or "").strip(),
        "city": str(row.get("city_published", "") or "").strip(),
        "pages": str(row.get("page_reference", "") or "").strip(),
        "condition": str(row.get("condition", "") or "").strip(),
        "illustrations": str(row.get("illustrations", "") or "").strip(),
        "price": str(row.get("price", "") or "").strip(),
        "acquired": str(row.get("date", "") or "").strip(),
        "categories": _categories(row),
        "notes": str(row.get("notes", "") or "").strip(),
    }


# --- routes ----------------------------------------------------------------

def _asset_v(filename):
    """Cache-busting token = the static file's mtime, so a changed asset always
    forces a fresh fetch (a plain /static/app.js otherwise serves stale)."""
    try:
        return str(int((Path(app.static_folder) / filename).stat().st_mtime))
    except OSError:
        return "0"


def _app_version():
    """The version shown in the title bar. The Electron shell passes its own
    version via WHL_APP_VERSION; a plain web run falls back to the newest
    changelog heading so the number is never a stale hardcoded string."""
    env_v = (os.environ.get("WHL_APP_VERSION") or "").strip()
    if env_v:
        return env_v
    try:
        text = lib.CHANGELOG_PATH.read_text(encoding="utf-8")
        m = re.search(r"^##\s+(\S+)", text, re.MULTILINE)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "dev"


@app.route("/")
def home():
    return render_template(
        "index.html",
        app_v=_asset_v("app.js"),
        css_v=_asset_v("style.css"),
        app_version=_app_version(),
    )


@app.route("/api/changelog")
def api_changelog():
    """The shared release notes (website/changelog.md), served raw for the
    in-app changelog viewer to render. Missing file -> empty, not an error."""
    try:
        text = lib.CHANGELOG_PATH.read_text(encoding="utf-8")
    except OSError:
        text = ""
    return Response(text, mimetype="text/markdown")   # Flask adds charset=utf-8


@app.route("/api/books")
def api_books():
    """The CH private-library catalogue (output/ch_library.json)."""
    raw = lib.load_json(lib.CH_LIBRARY_JSON_PATH, [])
    books = [_ch_row(i, r) for i, r in enumerate(raw)]
    return jsonify({"books": [b for b in books if b["title"] or b["author"]]})


# --- book builder: catalog entries being prepared for WHL submission -------------

BUILDS_PATH = lib.OUTPUT_DIR / "whl_builds.json"

# The one lock for every whl_builds.json read-modify-write: request handlers,
# analysis/publish background threads, and the cloud sync (passed into
# store_sync.sync_stores) all rewrite the whole file, so an unlocked
# load->mutate->save silently drops whatever another writer changed in
# between. Locks here are threading.Lock — the sidecar is a single process,
# so cross-process coordination is deliberately out of scope.
_builds_lock = threading.Lock()


def _build_updated_at(previous: str = "") -> str:
    """Return a build revision token strictly newer than ``previous``.

    ``updated_at`` doubles as the Editor's optimistic-concurrency token.  A
    wall-clock value rounded to seconds lets two saves in the same second
    share a token, so a stale full-form save can pass the equality check and
    overwrite the first one.  Microseconds make that unlikely; comparing with
    the previous token and advancing by one microsecond makes it impossible
    even when the clock stalls or moves backwards.
    """
    now = datetime.now(timezone.utc)
    try:
        prior = datetime.fromisoformat(str(previous or "").replace("Z", "+00:00"))
        if prior.tzinfo is None:
            prior = prior.replace(tzinfo=timezone.utc)
        else:
            prior = prior.astimezone(timezone.utc)
        if now <= prior:
            now = prior + timedelta(microseconds=1)
    except (TypeError, ValueError):
        pass
    return now.isoformat(timespec="microseconds")


def _mutate_json(path, lock, default, fn):
    """The write path for a mutable JSON store: hold the store's lock across
    load -> fn(doc) -> save so concurrent read-modify-writes serialize instead
    of overwriting each other. fn mutates the document in place; its return
    value passes through. Raising inside fn (abort included) skips the save."""
    with lock:
        doc = lib.load_json(path, default)
        out = fn(doc)
        lib.save_json(path, doc)
        return out


def _builds_apply(bid: str, fields: dict) -> str:
    """Fold field changes into one build against a FRESH read of the store —
    for slow work (folder sync, page deletion) whose snapshot may be minutes
    old: only this build's fields are ours to change (the _publish_run
    precedent)."""
    def apply(builds):
        if bid in builds:
            row = builds[bid]
            row.update(fields)
            row["updated_at"] = _build_updated_at(row.get("updated_at"))
            return row["updated_at"]
        return ""
    return _mutate_json(BUILDS_PATH, _builds_lock, {}, apply)

# The field set mirrors what a WHL catalog entry needs. pdf_source is the
# source URL; pdf_file is the local PDF attached for the actual submission
# (the PRIMARY PDF source); pdf_sources lists SECONDARY PDFs — other scans
# of the same book, each {id, path}, so OCR files can belong to a specific
# scan; ocr_active/ocr_verified/ocr_quality track the entry folder's OCR
# files; title_pages lists PDF pages marked as title pages (metadata
# extraction uses them later); attention flags an entry as needing attention.
_BUILD_FIELDS = ("published_slug",
                 "title", "subtitle", "authors", "year", "publisher",
                 "publisher_city", "edition", "volume", "group_id",
                 "language", "pages",
                 "categories", "category_ids", "description",
                 "pdf_source", "pdf_file",
                 "pdf_sources", "bundle",
                 "source_url", "notes", "status", "rights",
                 "ocr_active", "ocr_verified", "ocr_quality",
                 "title_pages", "thumbnail_source", "attention",
                 "images", "extra", "capture_id")

# The structured exceptions to the str() coercion below. `categories` (flat
# text) is deprecated in favour of category_ids — kept as display fallback.
# `bundle` picks which Analyze artifacts publish with the book; `relevance`
# is also structured but deliberately NOT accepted here — only the
# assessment job writes it.
_BUILD_STRUCTURED_FIELDS = ("pdf_sources", "category_ids", "bundle",
                            "images", "extra", "capture_id")


def _clean_bundle(raw) -> dict:
    """What publishes beyond the PDF: {about, annotations, pages_text,
    translations: [lang]} — everything else the Analyze tab produces stays
    on this machine (and the service_role-only sync)."""
    raw = raw if isinstance(raw, dict) else {}
    langs = []
    for v in raw.get("translations") or []:
        code = re.sub(r"[^a-z\-]", "", str(v or "").lower())[:12]
        if code and code not in langs:
            langs.append(code)
    return {"about": bool(raw.get("about")),
            "annotations": bool(raw.get("annotations")),
            "pages_text": bool(raw.get("pages_text")),
            "translations": langs}


def _clean_pdf_sources(raw) -> list:
    """Secondary PDF sources: a list of {id, path}. Everything else in a
    build is a flat string; this one field is structured, so it gets its
    own sanitizer instead of the str() coercion."""
    out = []
    if isinstance(raw, list):
        for it in raw:
            if not isinstance(it, dict):
                continue
            path = str(it.get("path") or "").strip()
            if not path:
                continue
            sid = re.sub(r"[^\w]", "", str(it.get("id") or ""))[:12]
            if not sid or sid == "primary":   # "primary" is a reserved key
                sid = lib.gen_id()
            out.append({"id": sid, "path": path})
    return out


def _valid_src_key(b: dict, key) -> str:
    """A client-supplied source key checked against the build: 'primary'
    (also the meaning of empty), or a LIVE secondary id. Anything else —
    a removed source, a typo — returns '' so callers refuse to record it."""
    key = str(key or "").strip()
    if not key or key == "primary":
        return "primary"
    if any(s.get("id") == key for s in (b.get("pdf_sources") or [])):
        return key
    return ""

# draft -> ready (verified) -> uploaded (sent to WHL, cleared from Pending)
_BUILD_STATUSES = ("draft", "ready", "uploaded")

# The curator's explicit publication-rights decision (docs/rights.md). Empty
# means undecided, which blocks publishing; only the _RIGHTS_TEXT_OK states
# let the book's own words (page text, translations, notes) go public.
# _RIGHTS_PUBLIC is how each state reads on the site (volumes.copyright_status).
_BUILD_RIGHTS = ("", "public-domain", "cleared", "searchable-only", "no-public-text")
_RIGHTS_TEXT_OK = ("public-domain", "cleared")
_RIGHTS_PUBLIC = {"public-domain": "Public domain", "cleared": "Cleared",
                  "searchable-only": "Search only", "no-public-text": "Restricted"}


@app.route("/api/builds")
def api_builds():
    return jsonify({"builds": lib.load_json(BUILDS_PATH, {})})


@app.route("/api/builds", methods=["POST"])
def api_builds_create():
    payload = request.get_json(silent=True) or {}
    seed = payload.get("build") or {}
    with _builds_lock:
        builds = lib.load_json(BUILDS_PATH, {})
        build = {f: str(seed.get(f, "") or "").strip() for f in _BUILD_FIELDS
                 if f not in _BUILD_STRUCTURED_FIELDS}
        build["pdf_sources"] = _clean_pdf_sources(seed.get("pdf_sources"))
        build["category_ids"] = _clean_category_ids(seed.get("category_ids"),
                                                    lib.load_taxonomy()["nodes"])
        build["bundle"] = _clean_bundle(seed.get("bundle"))
        build["images"] = _clean_images(seed.get("images"))
        build["extra"] = _clean_extra(seed.get("extra"))
        build["capture_id"] = _clean_capture_id(seed.get("capture_id"))
        if build["status"] not in _BUILD_STATUSES:
            build["status"] = "draft"
        if build["rights"] not in _BUILD_RIGHTS:
            return jsonify({"ok": False,
                            "error": f"unknown rights value {build['rights']!r}"}), 400
        build["id"] = lib.gen_id(set(builds))
        build["created_at"] = _build_updated_at()
        build["updated_at"] = build["created_at"]
        builds[build["id"]] = build
        lib.save_json(BUILDS_PATH, builds)
    activity("created", "draft entry", detail=build.get("title", ""))
    return jsonify({"ok": True, "build": build})


@app.route("/api/builds/<build_id>", methods=["PATCH"])
def api_builds_update(build_id: str):
    with _builds_lock:
        builds = lib.load_json(BUILDS_PATH, {})
        if build_id not in builds:
            abort(404)
        payload = request.get_json(silent=True) or {}
        if str(payload.get("rights") or "").strip() not in _BUILD_RIGHTS:
            return jsonify({"ok": False,
                            "error": f"unknown rights value {payload['rights']!r}"}), 400
        b = builds[build_id]
        # optimistic concurrency: an editor that loaded the record before
        # another writer touched it gets the current record back, not a merge
        expect = str(payload.get("expect_updated_at") or "")
        if expect and expect != str(b.get("updated_at") or ""):
            return jsonify({"ok": False, "error": "changed elsewhere",
                            "build": b}), 409
        was = b.get("status")
        for f in _BUILD_FIELDS:
            if f not in payload:
                continue
            if f == "pdf_sources":
                b[f] = _clean_pdf_sources(payload[f])
            elif f == "category_ids":
                b[f] = _clean_category_ids(payload[f], lib.load_taxonomy()["nodes"])
            elif f == "bundle":
                b[f] = _clean_bundle(payload[f])
            elif f == "images":
                b[f] = _clean_images(payload[f])
            elif f == "extra":
                b[f] = _clean_extra(payload[f])
            elif f == "capture_id":
                b[f] = _clean_capture_id(payload[f])
            else:
                b[f] = str(payload[f] or "").strip()
        if b.get("status") not in _BUILD_STATUSES:
            b["status"] = "draft"
        b["updated_at"] = _build_updated_at(b.get("updated_at"))
        lib.save_json(BUILDS_PATH, builds)
    # only the status transition is worth a feed entry; every keystroke is not
    if b["status"] != was and b["status"] in ("ready", "uploaded"):
        activity("uploaded" if b["status"] == "uploaded" else "verified", "book",
                 detail=b.get("title", ""))
    return jsonify({"ok": True, "build": b})


@app.route("/api/builds/<build_id>", methods=["DELETE"])
def api_builds_delete(build_id: str):
    with _builds_lock:
        builds = lib.load_json(BUILDS_PATH, {})
        if build_id not in builds:
            abort(404)
        del builds[build_id]
        lib.save_json(BUILDS_PATH, builds)
    return jsonify({"ok": True})


@app.route("/api/builds/restore", methods=["POST"])
def api_builds_restore():
    """Reinsert a deleted build verbatim (undo support)."""
    payload = request.get_json(silent=True) or {}
    build = payload.get("build") or {}
    bid = str(build.get("id") or "")
    if not bid:
        abort(400)
    build["images"] = _clean_images(build.get("images"))
    build["extra"] = _clean_extra(build.get("extra"))
    build["capture_id"] = _clean_capture_id(build.get("capture_id"))

    def reinsert(builds):
        build["updated_at"] = _build_updated_at(build.get("updated_at"))
        builds[bid] = build
    _mutate_json(BUILDS_PATH, _builds_lock, {}, reinsert)
    return jsonify({"ok": True, "build": build})


# --- staged alternatives: Process-mode candidate field-sets ------------------------
# Process mode stages ALTERNATIVE field values for a catalog entry — produced by
# Normalize, a DeepSeek pass, a rescan, or Smart Scan — for side-by-side review
# before any is applied. Each store entry keys a parent record by ``target``
# ("whl:<idx>", "build:<id>", "manual:<id>", "checked:<source>:<idx>") and holds
# an ordered list of alternative field-sets. Nothing here mutates the real
# record: the client applies a chosen alt through the normal edit endpoints
# ("Mark Primary") and posts the displaced original back here as a "superseded"
# alt, so the swap stays reversible. Device-local — provisional data never syncs
# — and guarded by one lock like the other JSON stores.
STAGED_PATH = lib.OUTPUT_DIR / "staged_alts.json"
_staged_lock = threading.Lock()

_STAGED_SOURCES = ("normalize", "deepseek", "rescan", "smartscan", "superseded")
_STAGED_MAX_ENTRIES = 2000
_STAGED_MAX_ALTS = 12


def _staged_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_staged_fields(raw) -> dict:
    """A flat {field: value} candidate map: string keys, scalar values coerced
    to trimmed strings, capped in count and length so an untrusted client (or a
    chatty model reply routed through here) can't bloat staged_alts.json."""
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if not isinstance(k, str) or len(out) >= 40:
                continue
            if isinstance(v, bool) or v is None:
                continue
            if isinstance(v, (str, int, float)):
                out[k[:40]] = str(v)[:2000]
    return out


def _clean_staged_alt(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    fields = _clean_staged_fields(raw.get("fields"))
    if not fields:
        return None
    src = str(raw.get("source") or "").strip().lower()
    if src not in _STAGED_SOURCES:
        src = "normalize"
    return {
        "id": str(raw.get("id") or lib.gen_id())[:40],
        "source": src,
        "label": str(raw.get("label") or "")[:200],
        "fields": fields,
        "note": str(raw.get("note") or "")[:4000],
        "created_at": str(raw.get("created_at") or _staged_ts())[:40],
    }


def _staged_alt_key(a) -> tuple:
    return (a.get("source"), json.dumps(a.get("fields"), sort_keys=True))


def _staged_append(e, alt) -> None:
    """Append an alt to an entry with the same dedupe + cap as /api/staged/add."""
    key = _staged_alt_key(alt)
    if any(_staged_alt_key(x) == key for x in e["alts"]):
        return
    e["alts"].append(alt)
    if len(e["alts"]) > _STAGED_MAX_ALTS:
        del e["alts"][:-_STAGED_MAX_ALTS]


def _staged_entry(doc, target, kind="", label=""):
    ents = doc.setdefault("entries", {})
    e = ents.get(target)
    if not isinstance(e, dict):
        e = ents[target] = {"target": target, "kind": kind or _target_kind(target),
                            "label": label, "alts": [], "created_at": _staged_ts()}
    if kind:
        e["kind"] = kind
    if label:
        e["label"] = label
    if not isinstance(e.get("alts"), list):
        e["alts"] = []
    return e


def _target_kind(target: str) -> str:
    t = str(target or "")
    if t.startswith("whl:"):
        return "whl"
    if t.startswith("build:"):
        return "build"
    if t.startswith("manual:"):
        return "manual"
    if t.startswith("checked:"):
        return "checked"
    return ""


def _staged_prune(doc):
    ents = doc.get("entries")
    if not isinstance(ents, dict):
        doc["entries"] = {}
        return
    for k in [k for k, e in ents.items()
              if not (isinstance(e, dict) and e.get("alts"))]:
        del ents[k]
    if len(ents) > _STAGED_MAX_ENTRIES:
        keep = sorted(ents.items(),
                      key=lambda kv: str(kv[1].get("updated_at") or kv[1].get("created_at") or ""),
                      reverse=True)[:_STAGED_MAX_ENTRIES]
        doc["entries"] = dict(keep)


def _staged_add(target: str, kind: str, label: str, alt) -> None:
    """Stage one alternative from server-side code (background producers such as
    Smart Scan). Mirrors /api/staged/add's dedupe + per-entry cap."""
    a = _clean_staged_alt(alt)
    if not a:
        return

    def apply(doc):
        e = _staged_entry(doc, target, kind=kind, label=label)
        key = (a["source"], json.dumps(a["fields"], sort_keys=True))
        if not any((x.get("source"), json.dumps(x.get("fields"), sort_keys=True)) == key
                   for x in e["alts"]):
            e["alts"].append(a)
            if len(e["alts"]) > _STAGED_MAX_ALTS:
                del e["alts"][:-_STAGED_MAX_ALTS]
        e["updated_at"] = _staged_ts()
        _staged_prune(doc)
    _mutate_json(STAGED_PATH, _staged_lock, {"entries": {}}, apply)


@app.route("/api/staged")
def api_staged_list():
    doc = lib.load_json(STAGED_PATH, {"entries": {}})
    ents = doc.get("entries") if isinstance(doc.get("entries"), dict) else {}
    return jsonify({"entries": ents})


@app.route("/api/staged/add", methods=["POST"])
def api_staged_add():
    """Stage one or more alternative field-sets on a target. Body:
    {target, kind?, label?, alts:[{source,label?,fields,note?}]} (or a single
    `alt`). A new alt whose (source, fields) duplicates an existing one is
    skipped so re-running Normalize doesn't pile up identical candidates."""
    p = request.get_json(silent=True) or {}
    target = str(p.get("target") or "").strip()
    if not target:
        abort(400)
    raw_alts = p.get("alts")
    if raw_alts is None and p.get("alt") is not None:
        raw_alts = [p.get("alt")]
    if not isinstance(raw_alts, list):
        abort(400)
    cleaned = [a for a in (_clean_staged_alt(x) for x in raw_alts) if a]
    if not cleaned:
        return jsonify({"ok": False, "error": "no valid alternatives"}), 400

    def apply(doc):
        e = _staged_entry(doc, target, kind=str(p.get("kind") or ""),
                          label=str(p.get("label") or ""))
        seen = {(a.get("source"), json.dumps(a.get("fields"), sort_keys=True))
                for a in e["alts"]}
        for a in cleaned:
            key = (a["source"], json.dumps(a["fields"], sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            e["alts"].append(a)
        if len(e["alts"]) > _STAGED_MAX_ALTS:      # keep the most recent
            del e["alts"][:-_STAGED_MAX_ALTS]
        e["updated_at"] = _staged_ts()
        _staged_prune(doc)
        return doc.get("entries", {}).get(target)
    entry = _mutate_json(STAGED_PATH, _staged_lock, {"entries": {}}, apply)
    return jsonify({"ok": True, "entry": entry})


@app.route("/api/staged/swap", methods=["POST"])
def api_staged_swap():
    """Mark Primary: the client has already applied alt <altId> to the real
    record through the normal edit endpoints. Here we remove that alt and, if a
    ``displaced`` field-set is supplied, re-file it as a "superseded" alt so the
    swap is reversible. Body: {target, altId, displaced?:{label?,fields,note?}}."""
    p = request.get_json(silent=True) or {}
    target = str(p.get("target") or "").strip()
    alt_id = str(p.get("altId") or "").strip()
    if not target or not alt_id:
        abort(400)
    displaced = p.get("displaced")
    disp_alt = None
    if isinstance(displaced, dict):
        d = dict(displaced)
        d["source"] = "superseded"
        disp_alt = _clean_staged_alt(d)

    def apply(doc):
        ents = doc.get("entries")
        if not isinstance(ents, dict) or target not in ents:
            return None
        e = ents[target]
        alts = e.get("alts") if isinstance(e.get("alts"), list) else []
        e["alts"] = [a for a in alts if a.get("id") != alt_id]
        if disp_alt:
            _staged_append(e, disp_alt)   # dedupe + cap, like the add paths
        e["updated_at"] = _staged_ts()
        _staged_prune(doc)
        return ents.get(target)
    entry = _mutate_json(STAGED_PATH, _staged_lock, {"entries": {}}, apply)
    return jsonify({"ok": True, "entry": entry})


@app.route("/api/staged/remove", methods=["POST"])
def api_staged_remove():
    """Dismiss a single alt ({target, altId}) or clear a whole entry ({target})."""
    p = request.get_json(silent=True) or {}
    target = str(p.get("target") or "").strip()
    alt_id = str(p.get("altId") or "").strip()
    if not target:
        abort(400)

    def apply(doc):
        ents = doc.get("entries")
        if not isinstance(ents, dict) or target not in ents:
            return None
        if not alt_id:
            del ents[target]
            return None
        e = ents[target]
        if isinstance(e.get("alts"), list):
            e["alts"] = [a for a in e["alts"] if a.get("id") != alt_id]
        e["updated_at"] = _staged_ts()
        _staged_prune(doc)
        return ents.get(target)
    entry = _mutate_json(STAGED_PATH, _staged_lock, {"entries": {}}, apply)
    return jsonify({"ok": True, "entry": entry})


# --- category taxonomy -------------------------------------------------------------
# The vocabulary behind every record's category_ids: a tree of {name, parent}
# nodes in output/categories.json (see docs/library-analyze-design.md §1). The
# old comma-separated `categories` text fields are deprecated — still shown as
# a fallback, no longer edited. Nodes sync across machines through the
# `taxonomy` cloud table (tools/store_sync.py), so every mutation stamps
# updated_at.

_categories_lock = threading.Lock()


def _tax_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tax_sibling_taken(nodes: dict, name: str, parent: str, skip: str = "") -> bool:
    low = name.strip().lower()
    return any(n.get("parent", "") == parent
               and str(n.get("name", "")).strip().lower() == low
               and nid != skip
               for nid, n in nodes.items())


def _tax_descends(nodes: dict, node_id: str, ancestor: str) -> bool:
    """True when ancestor sits on node_id's parent chain (or is node_id)."""
    cur, seen = node_id, set()
    while cur and cur in nodes and cur not in seen:
        if cur == ancestor:
            return True
        seen.add(cur)
        cur = str(nodes[cur].get("parent") or "")
    return cur == ancestor


def _clean_category_ids(raw, nodes: dict | None = None) -> list:
    """A record's category assignment: a de-duplicated list of node ids.
    Like pdf_sources, this is the structured exception to the str() coercion.
    When the taxonomy is at hand, ids that don't resolve are dropped."""
    out, seen = [], set()
    if isinstance(raw, list):
        for v in raw:
            cid = re.sub(r"[^\w]", "", str(v or ""))[:12]
            if not cid or cid in seen:
                continue
            if nodes is not None and cid not in nodes:
                continue
            seen.add(cid)
            out.append(cid)
    return out


def _remap_category_ids(fn) -> int:
    """Apply fn(list) -> list to every category_ids in builds, manual entries
    and checked books; returns how many records changed. Used by node delete
    and merge so assignments never dangle locally."""
    changed = 0

    with _builds_lock:
        builds = lib.load_json(BUILDS_PATH, {})
        dirty = False
        for b in builds.values():
            old = b.get("category_ids") or []
            new = fn(list(old))
            if new != old:
                b["category_ids"] = new
                b["updated_at"] = _build_updated_at(b.get("updated_at"))
                dirty, changed = True, changed + 1
        if dirty:
            lib.save_json(BUILDS_PATH, builds)

    with _manual_lock:
        entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
        dirty = False
        for e in entries.values():
            old = e.get("category_ids") or []
            new = fn(list(old))
            if new != old:
                e["category_ids"] = new
                dirty, changed = True, changed + 1
        if dirty:
            lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)

    with _client_state_lock:
        state = lib.load_json(lib.CLIENT_STATE_PATH, {})
        dirty = False
        for pair in state.get("checked") or []:
            book = (pair[1] or {}).get("book") if len(pair) == 2 else None
            if not isinstance(book, dict):
                continue
            old = book.get("category_ids") or []
            new = fn(list(old))
            if new != old:
                book["category_ids"] = new
                dirty, changed = True, changed + 1
        if dirty:
            state["updated_at"] = _tax_ts()
            lib.save_json(lib.CLIENT_STATE_PATH, state)

    return changed


@app.route("/api/categories")
def api_categories():
    return jsonify({"ok": True, "nodes": lib.load_taxonomy()["nodes"]})


@app.route("/api/categories", methods=["POST"])
def api_categories_create():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()[:80]
    parent = re.sub(r"[^\w]", "", str(payload.get("parent") or ""))[:12]
    if not name:
        return jsonify({"ok": False, "error": "a category needs a name"}), 400
    with _categories_lock:
        doc = lib.load_taxonomy()
        nodes = doc["nodes"]
        if parent and parent not in nodes:
            return jsonify({"ok": False, "error": "no such parent"}), 404
        if _tax_sibling_taken(nodes, name, parent):
            return jsonify({"ok": False, "error": "that name is taken here"}), 409
        cid = lib.gen_id(set(nodes))
        now = _tax_ts()
        nodes[cid] = {"name": name, "parent": parent,
                      "created_at": now, "updated_at": now}
        lib.save_json(lib.CATEGORIES_PATH, doc)
    return jsonify({"ok": True, "id": cid, "node": nodes[cid]})


@app.route("/api/categories/<cid>", methods=["PATCH"])
def api_categories_update(cid: str):
    """Rename and/or re-parent. Re-parenting under a descendant would cut the
    subtree loose from the root, so it is refused."""
    payload = request.get_json(silent=True) or {}
    with _categories_lock:
        doc = lib.load_taxonomy()
        nodes = doc["nodes"]
        if cid not in nodes:
            abort(404)
        node = nodes[cid]
        name = str(payload.get("name") or node["name"]).strip()[:80]
        parent = node.get("parent", "")
        if "parent" in payload:
            parent = re.sub(r"[^\w]", "", str(payload.get("parent") or ""))[:12]
            if parent and parent not in nodes:
                return jsonify({"ok": False, "error": "no such parent"}), 404
            if parent and _tax_descends(nodes, parent, cid):
                return jsonify({"ok": False, "error":
                                "a category cannot move under its own child"}), 400
        if not name:
            return jsonify({"ok": False, "error": "a category needs a name"}), 400
        if _tax_sibling_taken(nodes, name, parent, skip=cid):
            return jsonify({"ok": False, "error": "that name is taken here"}), 409
        node.update(name=name, parent=parent, updated_at=_tax_ts())
        lib.save_json(lib.CATEGORIES_PATH, doc)
    return jsonify({"ok": True, "node": node})


@app.route("/api/categories/<cid>", methods=["DELETE"])
def api_categories_delete(cid: str):
    """Remove a node: children move up to its parent, assignments drop it."""
    with _categories_lock:
        doc = lib.load_taxonomy()
        nodes = doc["nodes"]
        if cid not in nodes:
            abort(404)
        parent = nodes[cid].get("parent", "")
        now = _tax_ts()
        for n in nodes.values():
            if n.get("parent") == cid:
                n["parent"] = parent
                n["updated_at"] = now
        del nodes[cid]
        lib.save_json(lib.CATEGORIES_PATH, doc)
    n = _remap_category_ids(lambda ids: [i for i in ids if i != cid])
    return jsonify({"ok": True, "unassigned": n})


@app.route("/api/categories/merge", methods=["POST"])
def api_categories_merge():
    """Fold one node into another: its children and its assignments move to
    the target, then the node goes away. The cleanup for a vocabulary that
    grew two spellings of the same thing (adopt-legacy produces these)."""
    payload = request.get_json(silent=True) or {}
    src = re.sub(r"[^\w]", "", str(payload.get("from") or ""))[:12]
    dst = re.sub(r"[^\w]", "", str(payload.get("into") or ""))[:12]
    with _categories_lock:
        doc = lib.load_taxonomy()
        nodes = doc["nodes"]
        if src not in nodes or dst not in nodes:
            abort(404)
        if src == dst or _tax_descends(nodes, dst, src):
            return jsonify({"ok": False, "error":
                            "cannot merge a category into its own subtree"}), 400
        now = _tax_ts()
        for n in nodes.values():
            if n.get("parent") == src:
                n["parent"] = dst
                n["updated_at"] = now
        del nodes[src]
        lib.save_json(lib.CATEGORIES_PATH, doc)

    def swap(ids):
        out = [dst if i == src else i for i in ids]
        seen, deduped = set(), []
        for i in out:
            if i not in seen:
                seen.add(i)
                deduped.append(i)
        return deduped

    n = _remap_category_ids(swap)
    activity("merged", "category", detail=f"{n} records moved")
    return jsonify({"ok": True, "reassigned": n})


@app.route("/api/categories/adopt", methods=["POST"])
def api_categories_adopt():
    """Migrate the legacy comma-separated categories text into the taxonomy.

    Scans builds, manual entries and checked books; every distinct label
    becomes a root-level node (matched case-insensitively against existing
    root names) and the records get category_ids. The legacy text is left in
    place — it is display fallback now, and the CH/WHL sources it came from
    are read-only anyway. The tree is expected to be curated afterwards:
    re-parent and merge are what turn a flat harvest into a hierarchy.
    """
    payload = request.get_json(silent=True) or {}
    dry = bool(payload.get("dry_run"))

    def labels_of(text) -> list[str]:
        return [t.strip() for t in str(text or "").split(",") if t.strip()]

    # pass 1: collect every legacy label from records that have no assignment yet
    stores = {
        "builds": lib.load_json(BUILDS_PATH, {}),
        "manual": lib.load_json(lib.MANUAL_ENTRIES_PATH, {}),
        "checked": lib.load_json(lib.CLIENT_STATE_PATH, {}),
    }
    pending: list[tuple[str, str, list[str]]] = []   # (store, key, labels)
    for bid, b in stores["builds"].items():
        if not b.get("category_ids") and b.get("categories"):
            pending.append(("builds", bid, labels_of(b["categories"])))
    for eid, e in stores["manual"].items():
        if not e.get("category_ids") and e.get("categories"):
            pending.append(("manual", eid, labels_of(e["categories"])))
    for pair in stores["checked"].get("checked") or []:
        book = (pair[1] or {}).get("book") if len(pair) == 2 else None
        if isinstance(book, dict) and not book.get("category_ids") \
                and book.get("categories"):
            pending.append(("checked", str(pair[0]), labels_of(book["categories"])))

    wanted = sorted({lab for _, _, labs in pending for lab in labs},
                    key=str.lower)
    with _categories_lock:
        doc = lib.load_taxonomy()
        nodes = doc["nodes"]
        by_name = {str(n.get("name", "")).strip().lower(): nid
                   for nid, n in nodes.items() if not n.get("parent")}
        to_create = [lab for lab in wanted if lab.lower() not in by_name]

        if dry:
            return jsonify({"ok": True, "dry_run": True,
                            "records": len(pending),
                            "labels": wanted, "new": to_create})

        now = _tax_ts()
        for lab in to_create:
            cid = lib.gen_id(set(nodes))
            nodes[cid] = {"name": lab, "parent": "",
                          "created_at": now, "updated_at": now}
            by_name[lab.lower()] = cid
        if to_create:
            lib.save_json(lib.CATEGORIES_PATH, doc)

    # pass 2: assign. Store writes take their own locks; records are re-read
    # so nothing that changed since pass 1 is clobbered.
    ids_for = lambda labs: [by_name[lab.lower()] for lab in labs   # noqa: E731
                            if lab.lower() in by_name]
    assigned = 0
    with _builds_lock:
        builds = lib.load_json(BUILDS_PATH, {})
        for sid, key, labs in pending:
            if sid == "builds" and key in builds \
                    and not builds[key].get("category_ids"):
                builds[key]["category_ids"] = ids_for(labs)
                builds[key]["updated_at"] = _build_updated_at(
                    builds[key].get("updated_at"))
                assigned += 1
        lib.save_json(BUILDS_PATH, builds)
    with _manual_lock:
        entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
        for sid, key, labs in pending:
            if sid == "manual" and key in entries \
                    and not entries[key].get("category_ids"):
                entries[key]["category_ids"] = ids_for(labs)
                assigned += 1
        lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)
    with _client_state_lock:
        state = lib.load_json(lib.CLIENT_STATE_PATH, {})
        by_key = {str(p[0]): p[1] for p in state.get("checked") or []
                  if len(p) == 2 and isinstance(p[1], dict)}
        for sid, key, labs in pending:
            if sid != "checked" or key not in by_key:
                continue
            book = by_key[key].get("book")
            if isinstance(book, dict) and not book.get("category_ids"):
                book["category_ids"] = ids_for(labs)
                assigned += 1
        state["updated_at"] = _tax_ts()
        lib.save_json(lib.CLIENT_STATE_PATH, state)

    activity("adopted", "legacy categories", n=assigned)
    return jsonify({"ok": True, "records": assigned,
                    "created": len(to_create), "labels": wanted})


# --- local PDF serving + browsing (for the builder's SOURCE tab) ------------------
# Single-user localhost tool: the user picks PDFs from anywhere on disk, so
# these endpoints intentionally serve any local *.pdf path.

def _resolve_local(raw: str) -> Path | None:
    p = Path(raw)
    if not p.is_absolute():
        # relative stored paths (downloads/ia/..., output/entries/...) live
        # under the writable data root
        p = lib.DATA_ROOT / p
    try:
        return p.resolve()
    except OSError:
        return None


_remote_pdf_lock = threading.Lock()              # guards the per-URL lock map
_remote_pdf_url_locks: dict[str, threading.Lock] = {}
_REMOTE_PDF_MAX_BYTES = 300 * 1024 * 1024   # size cap on a fetched remote PDF


def _ssrf_guard(url: str) -> None:
    """Refuse URLs whose host resolves to a private / loopback / link-local /
    reserved address, so a client- or data-supplied PDF URL can't make the
    sidecar fetch internal services (SSRF). Not DNS-rebinding-proof — adequate
    for a localhost tool, and the gate before this runs on shared infra."""
    import ipaddress
    import socket
    host = urllib.parse.urlparse(url).hostname or ""
    if not host:
        raise ValueError("no host in URL")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise ValueError(f"cannot resolve host: {exc}")
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            raise ValueError("blocked non-public address")


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-run the SSRF guard on every redirect hop: without this a public URL
    can 3xx-bounce the fetch to an internal address, since urllib follows
    redirects with no host re-check."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not str(newurl).lower().startswith(("http://", "https://")):
            raise urllib.error.HTTPError(
                newurl, code, "redirect to a non-http(s) URL blocked", headers, fp)
        _ssrf_guard(newurl)   # raises ValueError on a blocked hop
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_pdf_opener = urllib.request.build_opener(_GuardedRedirectHandler())


def _remote_pdf_cache(url: str) -> Path:
    """Fetch a remote PDF once into downloads/cache/ and return the path.
    Browsers can't iframe third-party PDFs (X-Frame-Options), so remote
    sources are proxied through here. Raises ValueError on fetch failure.

    Downloads land in a temp file and are renamed into place under a
    PER-URL lock: the viewer fires several concurrent requests for the same
    URL (iframe GET + HEAD size probe + OCR text fetch), and none of them
    may see a half-written file or download it twice — but a long fetch of
    one book (a background smart check pulling a full scan) must not block
    every other remote PDF. A response that isn't a PDF is rejected instead
    of being cached forever."""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("not an http(s) URL")
    import hashlib
    cache_dir = lib.DATA_ROOT / "downloads" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".pdf"
    p = cache_dir / name
    with _remote_pdf_lock:
        url_lock = _remote_pdf_url_locks.setdefault(name, threading.Lock())
    with url_lock:
        if p.exists():
            return p
        _ssrf_guard(url)   # only on an actual fetch — cached hits skip the DNS lookup
        tmp = p.with_suffix(".fetch.tmp")
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": whl_client.USER_AGENT})
            with _pdf_opener.open(req, timeout=90) as resp, \
                    open(tmp, "wb") as fh:
                total = 0
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _REMOTE_PDF_MAX_BYTES:
                        raise ValueError("remote PDF exceeds the size cap")
                    fh.write(chunk)
            with open(tmp, "rb") as fh:
                if fh.read(5) != b"%PDF-":
                    raise ValueError("response is not a PDF")
            tmp.replace(p)
        except ValueError:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise ValueError(f"fetch failed: {exc}")
    return p


@app.route("/api/pdf")
def api_pdf():
    """Stream a PDF — a local path, or a remote ?url= proxied through the
    download cache. ?preview=1&pages=N serves a compressed, truncated
    derivative instead — much faster to load for large scans."""
    raw = (request.args.get("path") or "").strip()
    url = (request.args.get("url") or "").strip()
    if url:
        try:
            p = _remote_pdf_cache(url)
        except ValueError:
            abort(502)
    elif raw:
        p = _resolve_local(raw)
        if p is None or p.suffix.lower() != ".pdf" or not p.is_file():
            abort(404)
    else:
        abort(400)
    if request.args.get("preview"):
        try:
            pages = max(1, min(500, int(request.args.get("pages") or 20)))
        except ValueError:
            pages = 20
        try:
            p = _preview_pdf(p, pages)
        except Exception:
            pass  # fall back to the original
    return send_file(p, mimetype="application/pdf", conditional=True)


@app.route("/api/ai/summarize", methods=["POST"])
def api_ai_summarize():
    """Proxy a summarization request to an OpenAI-compatible chat API.
    The browser cannot call those APIs directly (no CORS), so the client
    sends its configured endpoint/model/key here."""
    p = request.get_json(silent=True) or {}
    base = (p.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    key = (p.get("api_key") or "").strip()
    model = (p.get("model") or "").strip()
    instructions = (p.get("instructions") or "").strip()
    text = (p.get("text") or "").strip()
    if not key or not model:
        return jsonify({"ok": False,
                        "error": "AI model / API key not configured (Settings > AI)"})
    if not text:
        return jsonify({"ok": False, "error": "no source text"})
    system = instructions or (
        "You summarize the OCR text of old books for a library catalog. "
        "Write a concise, factual catalog description in Markdown.")
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "Summarize this book from its OCR text:\n\n"
                                        + text[:60000]},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        summary = data["choices"][0]["message"]["content"]
        return jsonify({"ok": True, "summary": summary})
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return jsonify({"ok": False, "error": f"HTTP {exc.code}: {detail}"})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


# --- entry folders: one directory per pending entry -------------------------------
# output/entries/<build-id>/ holds metadata.json, a compressed + truncated
# primary.pdf (the book's own PDF derivative; preview.pdf is the legacy
# name), and ocr/*.txt files (extracted plus any loaded for comparison),
# each tied to its PDF source via ocr/sources.json.

ENTRIES_DIR = lib.OUTPUT_DIR / "entries"


def _entry_dir(build_id: str) -> Path:
    return ENTRIES_DIR / build_id


def _ocr_name(raw: str) -> str:
    name = re.sub(r"[^\w.\- ]", "_", (raw or "").strip()) or "ocr"
    if not name.lower().endswith(".txt"):
        name += ".txt"
    return name


# ocr/sources.json ties each OCR file to the PDF it came from: {name: key}
# where key is "primary" (the build's pdf_file — the default, so it is
# never written) or a secondary source id from build.pdf_sources.

def _ocr_sources(build_id: str) -> dict:
    return lib.load_json(_entry_dir(build_id) / "ocr" / "sources.json", {})


def _ocr_set_source(build_id: str, name: str, key: str) -> None:
    key = (key or "").strip()
    with _ocr_merge_lock:
        p = _entry_dir(build_id) / "ocr" / "sources.json"
        m = lib.load_json(p, {})
        if not key or key == "primary":
            if name not in m:
                return
            del m[name]              # primary is the default mapping
        elif m.get(name) == key:
            return
        else:
            m[name] = key
        p.parent.mkdir(parents=True, exist_ok=True)
        lib.save_json(p, m)


def _entry_primary_pdf(build_id: str) -> str:
    """The folder's own PDF derivative: primary.pdf, or the legacy
    preview.pdf name from before secondary sources existed."""
    d = _entry_dir(build_id)
    if (d / "primary.pdf").is_file():
        return "primary.pdf"
    if (d / "preview.pdf").is_file():
        return "preview.pdf"
    return ""


# --- artifact provenance: output/entries/<bid>/manifest.json ---------------------
# One row per derived artifact — content hash, producer, and the input hashes
# recorded at job completion (docs/search-design.md D4). Staleness is a hash
# comparison against the inputs as they stand now; artifacts that predate the
# manifest have no row and report stale=None everywhere.

_manifest_lock = threading.Lock()
# Above this size (scan PDFs) size+mtime stand in for the content hash:
# hashing a 130 MB scan on every folder listing is not acceptable, and
# staleness for the text artifacts is the point.
_MANIFEST_HASH_CAP = 32 << 20


def _manifest_path(build_id: str) -> Path:
    return _entry_dir(build_id) / "manifest.json"


def _load_manifest(build_id: str) -> dict:
    doc = lib.load_json(_manifest_path(build_id), None)
    if not isinstance(doc, dict) or not isinstance(doc.get("artifacts"), dict):
        return {"version": 1, "artifacts": {}}
    return doc


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _artifact_fingerprint(path: Path) -> dict | None:
    """{sha256} of the file bytes, {size, mtime} above the cap, None if
    unreadable."""
    try:
        st = path.stat()
        if st.st_size > _MANIFEST_HASH_CAP:
            return {"size": st.st_size, "mtime": st.st_mtime}
        return {"sha256": _file_sha256(path)}
    except OSError:
        return None


def _manifest_input(build_id: str, rel: str, path: Path | None = None) -> dict:
    """An input ref fingerprinted as it reads NOW — call at job start, not
    completion. `rel` is entry-relative; an external file (a source PDF)
    passes `path` and keeps a data-root-relative copy for re-fingerprinting."""
    ref = {"artifact": rel}
    if path is None:
        path = _entry_dir(build_id) / rel
    else:
        try:
            ref["path"] = path.resolve().relative_to(
                lib.DATA_ROOT.resolve()).as_posix()
        except (OSError, ValueError):
            ref["path"] = str(path)
    ref.update(_artifact_fingerprint(path) or {})
    return ref


def _manifest_record(build_id: str, rel: str, produced_by: dict,
                     inputs: list[dict] | None = None) -> None:
    """Upsert one artifact's provenance row, hashing the file as it stands
    on disk. inputs=None keeps the row's recorded inputs (a manual edit
    changes the content, not what it was derived from)."""
    fp = _artifact_fingerprint(_entry_dir(build_id) / rel)
    if fp is None:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def upsert(doc):
        doc.setdefault("version", 1)
        arts = doc.setdefault("artifacts", {})
        old = arts.get(rel) if isinstance(arts.get(rel), dict) else {}
        row = dict(fp)
        row["produced_by"] = dict(produced_by)
        row["inputs"] = ([dict(i) for i in inputs] if inputs is not None
                         else list(old.get("inputs") or []))
        row["created_at"] = old.get("created_at") or now
        row["updated_at"] = now
        arts[rel] = row

    _mutate_json(_manifest_path(build_id), _manifest_lock,
                 {"version": 1, "artifacts": {}}, upsert)


def _manifest_input_current(build_id: str, ref: dict, cache: dict) -> dict | None:
    """The input's fingerprint as it stands now, memoized per request so a
    folder listing hashes each input once however many artifacts share it."""
    p = str(ref.get("path") or "")
    path = _resolve_local(p) if p else \
        _entry_dir(build_id) / str(ref.get("artifact") or "")
    if path is None:
        return None
    key = str(path)
    if key not in cache:
        cache[key] = _artifact_fingerprint(path)
    return cache[key]


def _manifest_inputs_stale(build_id: str, rel: str, doc: dict | None = None,
                           cache: dict | None = None) -> list[str] | None:
    """Names of `rel`'s inputs whose content changed since it was produced;
    None = no manifest row (legacy artifact, staleness unknowable)."""
    row = ((doc if doc is not None else _load_manifest(build_id))
           .get("artifacts") or {}).get(rel)
    if not isinstance(row, dict):
        return None
    if cache is None:
        cache = {}
    out = []
    for ref in row.get("inputs") or []:
        if not isinstance(ref, dict):
            continue
        cur = _manifest_input_current(build_id, ref, cache)
        if cur is None:
            continue          # input gone: unknowable, never "stale"
        if ref.get("sha256") and cur.get("sha256"):
            changed = ref["sha256"] != cur["sha256"]
        elif "size" in ref and "size" in cur:
            changed = ((ref.get("size"), ref.get("mtime"))
                       != (cur.get("size"), cur.get("mtime")))
        else:
            changed = True    # crossed the hash cap: the size class changed
        if changed:
            out.append(str(ref.get("artifact") or ""))
    return out


def _manifest_after_renumber(build_id: str, edited: list[str],
                             moved: list[str]) -> None:
    """A page deletion rewrites the PDF and renumbers its OCR docs, layout
    and translations in one lockstep pass: refresh those rows' fingerprints
    AND their recorded input hashes so they stay current with each other,
    while artifacts outside the pass (summary, analysis, annotations) keep
    their recorded hashes and honestly go stale. The renumber itself is a
    manual edit of the OCR doc + layout (`edited`); `moved` rows keep their
    producer."""
    if not _manifest_path(build_id).is_file():
        return                # legacy entry: stay silent, create nothing
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def apply(doc):
        arts = doc.get("artifacts")
        if not isinstance(arts, dict):
            return
        cache: dict = {}
        for rel in edited + moved:
            row = arts.get(rel)
            if not isinstance(row, dict):
                continue
            fp = _artifact_fingerprint(_entry_dir(build_id) / rel)
            if fp is None:
                continue
            for k in ("sha256", "size", "mtime"):
                row.pop(k, None)
            row.update(fp)
            if rel in edited:
                row["produced_by"] = {"kind": "manual-edit"}
            for ref in row.get("inputs") or []:
                if not isinstance(ref, dict):
                    continue
                cur = _manifest_input_current(build_id, ref, cache)
                if cur is None:
                    continue
                for k in ("sha256", "size", "mtime"):
                    ref.pop(k, None)
                ref.update(cur)
            row["updated_at"] = now

    _mutate_json(_manifest_path(build_id), _manifest_lock,
                 {"version": 1, "artifacts": {}}, apply)


def _ocr_extracted_images(build_id: str) -> list[dict]:
    """Figures an OCR service (Mistral) cut out of a page: [{name, page,
    size}], cross-referencing ocr/layout.json's bbox map against the actual
    files on disk. Used to surface them in the OCR tab's Documents tree
    alongside the compiled .txt output, which _entry_folder_info previously
    listed on its own."""
    d = _entry_dir(build_id) / "ocr" / "images"
    if not d.is_dir():
        return []
    meta = lib.load_json(_entry_dir(build_id) / "ocr" / "layout.json", {})
    images_meta = meta.get("images") or {}
    out = []
    for f in sorted(d.iterdir()):
        if not f.is_file():
            continue
        info = images_meta.get(f.name) or {}
        out.append({"name": f.name, "page": info.get("page"), "size": f.stat().st_size})
    return out


def _captured_images(build: dict | None) -> list[dict]:
    """Captured-photo manifest rows backed by the safe capture-image route."""
    out = []
    root = lib.DATA_ROOT.resolve()
    for raw in _clean_images((build or {}).get("images")):
        resolved = _resolve_local(raw)
        available = False
        size = 0
        if resolved is not None:
            try:
                resolved.relative_to(root)
                available = (resolved.is_file()
                             and resolved.suffix.lower()
                             in (".jpg", ".jpeg", ".png", ".webp"))
                if available:
                    size = resolved.stat().st_size
            except (ValueError, OSError):
                available = False
        out.append({"name": Path(raw).name, "path": raw,
                    "size": size, "available": available})
    return out


def _entry_folder_info(build_id: str, build: dict | None = None) -> dict:
    """Return the book's on-disk artifact manifest for the desktop tree.

    Keep this scan self-contained: it is defined before the Analyze helpers,
    and the folder endpoint is also useful when Analyze has never been opened.
    """
    d = _entry_dir(build_id)
    if build is None:
        build = lib.load_json(BUILDS_PATH, {}).get(build_id) or {}
    manifest = _load_manifest(build_id)
    fp_cache: dict = {}       # inputs hash once per folder call, not per row

    def prov(rel: str) -> dict:
        """{stale, produced_by} for one artifact; stale=None = no row."""
        row = (manifest.get("artifacts") or {}).get(rel)
        if not isinstance(row, dict):
            return {"stale": None, "produced_by": None}
        stale = _manifest_inputs_stale(build_id, rel, manifest, fp_cache)
        return {"stale": bool(stale),
                "produced_by": row.get("produced_by") or {}}

    ocr = []
    if (d / "ocr").is_dir():
        srcmap = _ocr_sources(build_id)
        for f in sorted((d / "ocr").glob("*.txt")):
            ocr.append(dict({"name": f.name, "size": f.stat().st_size,
                             "src": srcmap.get(f.name) or "primary"},
                            **prov(f"ocr/{f.name}")))

    full_text = []
    root_text = d / "full_text.txt"
    if root_text.is_file():
        full_text.append(dict({"name": root_text.name,
                               "artifact": root_text.name,
                               "size": root_text.stat().st_size},
                              **prov(root_text.name)))
    full_text_dir = d / "full_text"
    if full_text_dir.is_dir():
        for f in sorted(full_text_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in (".txt", ".md"):
                full_text.append(dict({"name": f.name,
                                       "artifact": f"full_text/{f.name}",
                                       "size": f.stat().st_size},
                                      **prov(f"full_text/{f.name}")))

    translations = []
    translations_dir = d / "translations"
    if translations_dir.is_dir():
        page_mark = re.compile(r"^--- page \d+ ---$", re.M)
        for f in sorted(translations_dir.glob("*.txt")):
            text = f.read_text(encoding="utf-8", errors="replace")
            markers = page_mark.findall(text)
            translations.append(dict(
                {"name": f.name, "lang": f.stem,
                 "pages": len(markers) or (1 if text.strip() else 0),
                 "size": f.stat().st_size},
                **prov(f"translations/{f.name}")))

    analysis = []
    analysis_dir = d / "analysis"
    if analysis_dir.is_dir():
        for f in sorted(analysis_dir.glob("*.md")):
            analysis.append(dict({"name": f.name, "size": f.stat().st_size},
                                 **prov(f"analysis/{f.name}")))

    def text_artifact(name: str) -> dict:
        f = d / name
        return dict({"exists": f.is_file(),
                     "size": f.stat().st_size if f.is_file() else 0},
                    **prov(name))

    primary = _entry_primary_pdf(build_id)
    return {"exists": d.is_dir(), "path": str(d), "ocr": ocr,
            "images": _ocr_extracted_images(build_id),
            "captured_images": _captured_images(build),
            "preview": bool(primary), "primary_pdf": primary,
            "processed_pdf": "processed.pdf" if (d / "processed.pdf").is_file() else "",
            "full_text": full_text, "translations": translations,
            "analysis": analysis, "summary": text_artifact("summary.md"),
            "about": text_artifact("about.md"),
            "metadata": (d / "metadata.json").is_file()}


def _pdf_extract_text(p: Path, max_pages: int | None = None) -> tuple[int, int, str, int]:
    """(total_pages, shown_pages, text, pages_with_text) of a PDF's text layer.

    PyMuPDF rather than pypdf: it is the same library that rasterises these
    pages and reads their word boxes, so extraction and the Layout view can
    never disagree about what text a page holds.

    pages_with_text is what tells a scanned book from a digitised one. A Google
    scan carries a text layer on its own front matter and nothing else, so the
    extraction looks like it worked while every real page comes back empty.
    """
    import fitz
    doc = fitz.open(str(p))
    try:
        total = doc.page_count
        shown = total if max_pages is None else min(total, max_pages)
        parts, with_text = [], 0
        for i in range(shown):
            text = doc[i].get_text().strip()
            if text:
                with_text += 1
            parts.append(f"--- page {i + 1} ---\n{text}")
    finally:
        doc.close()
    return total, shown, "\n\n".join(parts), with_text


_preview_pdf_lock = threading.Lock()


def _preview_pdf(src: Path, pages: int) -> Path:
    """A compressed, truncated preview derivative, cached by mtime."""
    import hashlib
    cache = lib.DATA_ROOT / "downloads" / "cache" / "previews"
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(
        f"{src}|{src.stat().st_mtime}|{pages}".encode("utf-8")).hexdigest()[:16]
    out = cache / f"{key}.pdf"
    # Serialize concurrent generation of the same preview, exactly as
    # _remote_pdf_cache does for its fetch: the viewer fires several requests
    # for one source at once (iframe GET + size probe + OCR fetch). Without the
    # lock, two of them both find no cached file and both run the write; the
    # loser's atomic replace then targets a file the winner already created and
    # the viewer holds open, which Windows surfaces as PermissionError
    # (Errno 13).
    with _preview_pdf_lock:
        if out.is_file():
            return out
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(str(src))
        writer = PdfWriter()
        for i in range(min(len(reader.pages), pages)):
            page = reader.pages[i]
            try:
                page.compress_content_streams()
            except Exception:
                pass
            writer.add_page(page)
        tmp = out.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            writer.write(fh)
        tmp.replace(out)
    return out


@app.route("/api/builds/<build_id>/folder")
def api_build_folder_info(build_id: str):
    return jsonify(_entry_folder_info(build_id))


@app.route("/api/builds/<build_id>/artifact/<kind>/<path:name>")
def api_build_text_artifact(build_id: str, kind: str, name: str):
    """Read a Full Text or Analysis artifact without permitting traversal."""
    if build_id not in lib.load_json(BUILDS_PATH, {}):
        abort(404)
    entry = _entry_dir(build_id).resolve()
    if not entry.is_relative_to(ENTRIES_DIR.resolve()):
        abort(404)

    if kind == "full_text" and name == "full_text.txt":
        candidate = entry / "full_text.txt"
        allowed_root = entry
    elif kind == "full_text" and name.startswith("full_text/"):
        allowed_root = (entry / "full_text").resolve()
        candidate = entry / name
    elif kind == "full_text":
        # Backward compatibility for clients that used the bare filename for
        # directory artifacts before the manifest gained an explicit path.
        allowed_root = (entry / "full_text").resolve()
        candidate = allowed_root / name
    elif kind == "analysis":
        allowed_root = (entry / "analysis").resolve()
        candidate = allowed_root / name
    elif kind == "verbatim":
        # the pre-correction OCR reading, snapshotted by api_build_ocr_put;
        # read-only by construction — nothing writes here after the snapshot
        allowed_root = (entry / "ocr" / "verbatim").resolve()
        candidate = allowed_root / name
    else:
        abort(404)

    candidate = candidate.resolve()
    if (not candidate.is_relative_to(allowed_root) or not candidate.is_file()
            or candidate.suffix.lower() not in (".txt", ".md")):
        abort(404)
    return jsonify({"ok": True, "kind": kind, "name": name,
                    "text": candidate.read_text(
                        encoding="utf-8", errors="replace")})


@app.route("/api/builds/<build_id>/folder", methods=["POST"])
def api_build_folder_sync(build_id: str):
    """Create/refresh the entry folder: metadata, PDF preview, extracted OCR.
    Body: {pages: N, keep_original: bool}."""
    builds = lib.load_json(BUILDS_PATH, {})
    if build_id not in builds:
        abort(404)
    b = builds[build_id]
    p = request.get_json(silent=True) or {}
    try:
        pages = max(1, min(500, int(p.get("pages") or 20)))
    except (TypeError, ValueError):
        pages = 20
    keep_original = bool(p.get("keep_original", True))
    trim_blank = bool(p.get("trim_blank", False))
    d = _entry_dir(build_id)
    (d / "ocr").mkdir(parents=True, exist_ok=True)
    lib.save_json(d / "metadata.json", b)
    notes = []
    src = None
    preview_ok = False  # THIS sync produced a fresh preview.pdf
    pf = (b.get("pdf_file") or "").strip()
    if pf:
        sp = _resolve_local(pf)
        if sp is not None and sp.is_file():
            src = sp
        else:
            notes.append("pdf_file not found")
    # blank pages are trimmed from the REAL PDF before the preview and
    # extraction are built (backup kept, OCR files renumbered) — skipped
    # for the truncated preview derivative and while an OCR job runs
    if trim_blank and src is not None:
        running = [j for j in _ocr_jobs.values()
                   if j.get("build_id") == build_id
                   and j.get("status") in ("running", "cancelling")]
        is_deriv = False
        try:
            is_deriv = src.resolve().is_relative_to(ENTRIES_DIR.resolve())
        except OSError:
            pass
        if running:
            notes.append("blank-page trim skipped (OCR job running)")
        elif is_deriv:
            notes.append("blank-page trim skipped (preview derivative)")
        else:
            try:
                blanks = _blank_pages(src)
                if blanks:
                    _apply_page_deletion(build_id, builds, src, blanks)
                    b = builds[build_id]
                    # the folder metadata must reflect the remapped
                    # title_pages, not the pre-trim snapshot
                    lib.save_json(d / "metadata.json", b)
                    notes.append(f"trimmed {len(blanks)} blank page(s): "
                                 + ",".join(str(n) for n in blanks))
            except Exception as exc:
                notes.append(f"blank-page trim failed: {exc}")
    if src is not None:
        try:
            prev = _preview_pdf(src, pages)
            import shutil
            shutil.copyfile(prev, d / "primary.pdf")
            preview_ok = True
            # migrate away from the legacy name: anything pointing at the
            # old preview.pdf (a keep_original repoint from an earlier run)
            # moves to primary.pdf BEFORE the stale file goes
            legacy = d / "preview.pdf"
            if legacy.is_file():
                old_rel = legacy.resolve().relative_to(
                    lib.DATA_ROOT.resolve()).as_posix()
                if (b.get("pdf_file") or "").replace("\\", "/") == old_rel:
                    b["pdf_file"] = (d / "primary.pdf").resolve().relative_to(
                        lib.DATA_ROOT.resolve()).as_posix()
                    # this function's snapshot is stale by now (preview render,
                    # extraction): fold in only this build's change
                    b["updated_at"] = _builds_apply(
                        build_id, {"pdf_file": b["pdf_file"]})
                    src = d / "primary.pdf"
                legacy.unlink()
                notes.append("renamed preview.pdf to primary.pdf")
        except Exception as exc:
            notes.append(f"preview failed: {exc}")
        try:
            total, shown, text, with_text = _pdf_extract_text(src)   # every page (the 400 cap was legacy)
            # a Google scan carries text on its front matter and nowhere else:
            # the extraction "succeeds" and is worthless
            if with_text > 1:
                (d / "ocr" / "extracted.txt").write_text(
                    text, encoding="utf-8", errors="replace")
            else:
                notes.append("no text layer (OCR the pages)")
        except Exception as exc:
            notes.append(f"text extraction failed: {exc}")
        # IA originals are temporary artifacts unless configured otherwise.
        # Only a preview produced by THIS sync may cost the original — a
        # leftover preview.pdf from an earlier run does not count.
        if not keep_original and preview_ok:
            try:
                srcr = src.resolve()
                if srcr.is_relative_to(lib.IA_DOWNLOADS_DIR.resolve()):
                    src.unlink()
                    # a trim in this same sync left a full-size backup of
                    # the original — pointless once the original itself is
                    # a disposed temporary artifact
                    src.with_suffix(".bak.pdf").unlink(missing_ok=True)
                    notes.append("original removed (temporary artifact)")
                    # nothing may keep pointing at the deleted file: the
                    # entry folder's own PDF becomes the build's PDF, and
                    # the IA download catalog entry is retired
                    b["pdf_file"] = (d / "primary.pdf").resolve().relative_to(
                        lib.DATA_ROOT.resolve()).as_posix()
                    b["updated_at"] = _builds_apply(
                        build_id, {"pdf_file": b["pdf_file"]})

                    def _drop_stale(catalog):
                        for k in [k for k, v in catalog.items()
                                  if (lib.DATA_ROOT / str(v.get("saved_as") or "?")).resolve()
                                  == srcr]:
                            del catalog[k]
                    _update_ia_catalog(_drop_stale)
            except Exception as exc:
                notes.append(f"original cleanup failed: {exc}")
    out = _entry_folder_info(build_id)
    out.update({"ok": True, "notes": notes, "build": b})
    return jsonify(out)


@app.route("/api/entries")
def api_entries():
    """Folder info for every build that has an entry folder — one pass, so
    the OCR tab's book list doesn't need a request per build."""
    builds = lib.load_json(BUILDS_PATH, {})
    out = {}
    for bid, build in builds.items():
        info = _entry_folder_info(bid, build)
        # Verified entries belong in the OCR workspace even before anything has
        # created their sidecar folder. Their empty document tree is actionable:
        # attach/open the PDF and run OCR from here.
        if info["exists"] or build.get("status") in ("ready", "uploaded"):
            out[bid] = {"ocr": info["ocr"], "images": info["images"],
                        "captured_images": info["captured_images"],
                        "preview": info["preview"],
                        "primary_pdf": info["primary_pdf"],
                        "processed_pdf": info["processed_pdf"],
                        "full_text": info["full_text"],
                        "translations": info["translations"],
                        "analysis": info["analysis"],
                        "summary": info["summary"],
                        "about": info["about"]}
    return jsonify({"entries": out})


@app.route("/api/builds/<build_id>/ocr/<name>")
def api_build_ocr_get(build_id: str, name: str):
    # membership check doubles as path validation for the build_id segment
    if build_id not in lib.load_json(BUILDS_PATH, {}):
        abort(404)
    f = _entry_dir(build_id) / "ocr" / _ocr_name(name)
    if not f.is_file():
        abort(404)
    return jsonify({"ok": True, "name": f.name,
                    "text": f.read_text(encoding="utf-8", errors="replace")})


@app.route("/api/builds/<build_id>/ocr", methods=["POST"])
def api_build_ocr_put(build_id: str):
    """Store an OCR text file on the entry folder. Body: {name, text, src?}
    — src ties the file to a secondary PDF source (default: primary)."""
    builds = lib.load_json(BUILDS_PATH, {})
    if build_id not in builds:
        abort(404)
    p = request.get_json(silent=True) or {}
    name = _ocr_name(p.get("name") or "")
    d = _entry_dir(build_id) / "ocr"
    d.mkdir(parents=True, exist_ok=True)
    target = d / name
    # the verbatim layer: snapshot the reading being overwritten ONCE, before
    # the first manual correction, and never update it after (D4/D8 — the
    # pre-correction reading must stay recoverable)
    if target.is_file():
        verbatim = d / "verbatim" / name
        if not verbatim.is_file():
            import shutil
            verbatim.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, verbatim)
    target.write_text(str(p.get("text") or ""),
                      encoding="utf-8", errors="replace")
    if "src" in p:
        src_key = _valid_src_key(builds[build_id], p.get("src"))
        if src_key:
            _ocr_set_source(build_id, name, src_key)
    _manifest_record(build_id, f"ocr/{name}", {"kind": "manual-edit"})
    _revalidate_note_anchors(build_id, builds[build_id], name)
    return jsonify({"ok": True, "name": name,
                    "folder": _entry_folder_info(build_id)})


@app.route("/api/builds/<build_id>/ocr/images/<name>")
def api_build_ocr_image(build_id: str, name: str):
    """A figure an OCR service cut out of a page (saved by the OCR job)."""
    if build_id not in lib.load_json(BUILDS_PATH, {}):
        abort(404)
    safe = re.sub(r"[^\w.\-]", "_", name or "")
    f = _entry_dir(build_id) / "ocr" / "images" / safe
    if not safe or not f.is_file():
        abort(404)
    return send_file(f, conditional=True)


@app.route("/api/builds/<build_id>/cover-candidate")
def api_build_cover_candidate(build_id: str):
    """The Resources tab's computed "cover" suggestion: the first page of the
    primary PDF that isn't blank, per first_content_page()'s ink+text
    heuristic. {page: N} or {page: null} if there's no PDF yet or nothing
    qualifies — never a hard failure, since this is only ever a suggestion."""
    builds = lib.load_json(BUILDS_PATH, {})
    b = builds.get(build_id)
    if b is None:
        abort(404)
    pdf = _resolve_local(str(b.get("pdf_file") or ""))
    if pdf is None or pdf.suffix.lower() != ".pdf" or not pdf.is_file():
        return jsonify({"ok": True, "page": None})
    try:
        page = first_content_page(pdf)
    except Exception:
        page = None
    return jsonify({"ok": True, "page": page})


@app.route("/api/builds/<build_id>/ocr-layout")
def api_build_ocr_layout(build_id: str):
    """Positions of the extracted figures: ocr/layout.json, {images: {name:
    {page, x, y, w, h}}} with boxes normalised to 0..1 of the page."""
    if build_id not in lib.load_json(BUILDS_PATH, {}):
        abort(404)
    meta = lib.load_json(_entry_dir(build_id) / "ocr" / "layout.json", {})
    # word_pages is per source: {"<src>": [pages...]}, so the client places a
    # facsimile only for the source whose boxes it actually has
    word_pages = {src: sorted(int(k) for k in pages if str(k).isdigit())
                  for src, pages in (meta.get("words") or {}).items()
                  if isinstance(pages, dict)}
    return jsonify({"ok": True, "images": meta.get("images") or {},
                    "word_pages": word_pages})


# --- PDF page rasterization (the OCR tab's side-by-side page view) ---------------

def _pageimg_pdf(raw: str) -> Path:
    p = _resolve_local(raw or "")
    if p is None or p.suffix.lower() != ".pdf" or not p.is_file():
        abort(404)
    return p


# A tiny LRU of open PyMuPDF handles, keyed by (path, mtime). fitz.open reparses
# the whole xref/object table every call, so the interactive reader — which asks
# for one page at a time — reopened a 400 MB scan once per page; one shared handle
# makes it a single parse. fitz Documents are not thread-safe, so _doc_lock is
# held across the render: fine here — rasterization is GIL-bound, only a handful
# of pages render before the on-disk cache serves them, and the background OCR
# job keeps its own handle so it never waits on this.
_doc_cache = collections.OrderedDict()
_doc_lock = threading.Lock()
_DOC_CACHE_MAX = 4


@contextlib.contextmanager
def _pdf_doc(path: Path):
    """Yield a shared, cached fitz.Document for `path`, opened at most once per
    (path, mtime). The lock is held for the whole `with` body, so keep it short:
    read one page and get out."""
    import fitz
    key = (str(path), path.stat().st_mtime)
    with _doc_lock:
        doc = _doc_cache.get(key)
        if doc is None:
            # a re-saved file (new mtime) must not keep serving the stale handle
            for k in [k for k in _doc_cache if k[0] == key[0]]:
                try:
                    _doc_cache.pop(k).close()
                except Exception:
                    pass
            doc = fitz.open(key[0])
            _doc_cache[key] = doc
            while len(_doc_cache) > _DOC_CACHE_MAX:
                _, evicted = _doc_cache.popitem(last=False)
                try:
                    evicted.close()
                except Exception:
                    pass
        else:
            _doc_cache.move_to_end(key)
        yield doc


def _cached_page_file(path: Path, mimetype: str):
    """Serve a rendered page image. A moderate max-age keeps scroll-back fast
    (no 304 per page for a few minutes), but NOT `immutable`: the URL is only
    path+page+width with no mtime, so an in-place page delete/trim that rewrites
    the PDF must still be picked up — once max-age lapses the ETag (conditional=
    True) revalidates and the endpoint serves the freshly-keyed render."""
    resp = send_file(path, mimetype=mimetype, conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


def _pages_cache_dir() -> Path:
    d = lib.DATA_ROOT / "downloads" / "cache" / "pages"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _page_cache_key(path: Path, mtime: float, page: int, width: int) -> str:
    """Content address for one rendered page: the same (path, mtime, page,
    width) always maps to the same cache file, so the background warmer and the
    live endpoint share files with no coordination."""
    return hashlib.sha1(
        f"{path}|{mtime}|{page}|{width}".encode("utf-8")).hexdigest()[:16]


# Warming the page cache in the background. The reader hits /api/pdf/info the
# instant a book opens, so that is where rendering every page at the reader's
# width into the same on-disk cache is kicked off. Ship-1 windowing means the
# viewport only fetches a few pages, but a fast scroll to page 900 then lands on
# a warm file instead of a cold render. Warmed once per (path, mtime, width); a
# re-saved file re-warms under its new mtime. Storage here is deliberately
# unbounded — precomputing the whole book is the point.
_warm_started: set = set()
_warm_lock = threading.Lock()

# The reader paints a low-res thumbnail as a blur-up placeholder, then the sharp
# render on top. Warming the 200 px tier FIRST (≈10 ms/page) means a fast scroll
# has something for every page almost immediately; the 700 px tier follows.
_WARM_THUMB_W = 200
_WARM_FULL_W = 700


def _warm_pages_async(path: Path) -> None:
    try:
        import fitz  # binds `fitz` for the nested renderers; nothing to warm without it
    except ImportError:
        return
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return
    key = (str(path), mtime)
    with _warm_lock:
        if key in _warm_started:
            return
        _warm_started.add(key)

    def render_pass(doc, cache, width):
        for page in range(1, doc.page_count + 1):
            ck = _page_cache_key(path, mtime, page, width)
            if (cache / f"{ck}.jpg").is_file() or (cache / f"{ck}.png").is_file():
                continue
            try:
                pg = doc[page - 1]
                zoom = width / max(1.0, pg.rect.width)
                pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                tmp = cache / f"{ck}.warm.tmp.jpg"
                tmp.write_bytes(pix.tobytes("jpeg", jpg_quality=82))
                tmp.replace(cache / f"{ck}.jpg")
            except Exception:
                pass             # a page that won't encode is left to the live path
            time.sleep(0.005)    # yield so interactive renders stay snappy

    def run():
        cache = _pages_cache_dir()
        try:
            doc = fitz.open(str(path))
        except Exception:
            with _warm_lock:
                _warm_started.discard(key)       # a later open may still work
            return
        try:
            render_pass(doc, cache, _WARM_THUMB_W)   # blur-up tier first: quick
            render_pass(doc, cache, _WARM_FULL_W)    # then the sharp image
        finally:
            doc.close()

    threading.Thread(target=run, daemon=True, name="pdf-warm").start()


@app.route("/api/pdf/words")
def api_pdf_words():
    """Word boxes of one page of a local PDF (?path=&page=N).

    These are the very coordinates the browser's built-in viewer uses to draw
    its selection highlight, read straight off the PDF's own text layer.
    Everything is normalised to 0..1 of the page, so the caller can scale it
    onto a page image rendered at any width. `found` is false for an image-only
    page (a scan never OCR'd into a text layer) -- there is nothing to place.

    The unit is the LINE, and lines are rebuilt by clustering spans on their
    baseline rather than trusting the PDF's own line structure. These scans
    carry an OCR-produced text layer where every word is its own span with its
    own estimated size (4.2pt..10pt across one page of body text), and PyMuPDF
    often reports each such word as its own "line" -- so both per-span sizing
    and per-line medians leave single words rendering a size or two off. The
    median over a baseline cluster is stable against that.

    `y` is the line's baseline and `s` its font size, both as a fraction of page
    height; each span reports `x`/`w` as a fraction of page width. Multiply by
    the rendered pane's box for pixels.
    """
    p = _pageimg_pdf(request.args.get("path"))
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    if importlib.util.find_spec("fitz") is None:
        return jsonify({"ok": False, "error": "PyMuPDF is not installed"}), 501
    from statistics import median
    with _pdf_doc(p) as doc:
        if page > doc.page_count:
            abort(404)
        pg = doc[page - 1]
        pw, ph = float(pg.rect.width), float(pg.rect.height)
        if pw <= 0 or ph <= 0:
            return jsonify({"ok": True, "found": False, "lines": []})
        raw = []
        for block in pg.get_text("dict").get("blocks", []):
            if block.get("type") != 0:      # 0 = text; 1 = image
                continue
            for line in block.get("lines", []):
                for sp in line.get("spans", []):
                    t = sp.get("text") or ""
                    if not t.strip():
                        continue
                    x0, _y0, x1, _y1 = sp["bbox"]
                    size = float(sp.get("size") or 0)
                    if size <= 0:
                        continue
                    raw.append((float(sp.get("origin", (0, 0))[1]), x0, x1, t, size))

        # cluster on the baseline: spans within a fraction of a line's own type
        # size sit on the same line, whatever the PDF claims
        raw.sort()
        clusters: list[list] = []
        for r in raw:
            if clusters and abs(r[0] - clusters[-1][0][0]) <= 0.4 * clusters[-1][0][4]:
                clusters[-1].append(r)
            else:
                clusters.append([r])

        lines = []
        for c in clusters:
            c.sort(key=lambda r: r[1])      # left to right
            lines.append({
                "y": round(median([r[0] for r in c]) / ph, 5),
                "s": round(median([r[4] for r in c]) / ph, 6),
                "spans": [{"t": r[3], "x": round(r[1] / pw, 5),
                           "w": round((r[2] - r[1]) / pw, 5)} for r in c],
            })
    # No text layer? Fall back to this build's stored OCR word boxes, so an
    # image-only scan that has been OCR'd still gets a placed facsimile. The
    # build id is validated (it indexes an entry folder) before it is used.
    source = "pdf"
    if not lines:
        bid = str(request.args.get("build_id") or "").strip()
        builds = lib.load_json(BUILDS_PATH, {})
        if bid and bid in builds:
            # the boxes are stored per source; pick the one this PDF path is
            src = _src_key_for_path(builds[bid], p)
            meta = lib.load_json(_entry_dir(bid) / "ocr" / "layout.json", {})
            pages = (meta.get("words") or {}).get(src) or {}
            ocr_lines = _lines_from_ocr_words(pages.get(str(page)))
            if ocr_lines:
                lines, source = ocr_lines, "ocr"
    return jsonify({"ok": True, "found": bool(lines), "page_w": pw, "page_h": ph,
                    "source": source, "lines": lines})


def _src_key_for_path(build: dict, pdf: Path) -> str:
    """Which source a resolved PDF path is for this build: 'primary' (its
    pdf_file) or a secondary id from pdf_sources. The primary claims the file
    first, so a scan attached as both still counts as primary."""
    try:
        pdfr = pdf.resolve()
    except OSError:
        return "primary"
    primary = _resolve_local(str(build.get("pdf_file") or ""))
    if primary is not None and primary.resolve() == pdfr:
        return "primary"
    for s in (build.get("pdf_sources") or []):
        sp = _resolve_local(str(s.get("path") or ""))
        if sp is not None and sp.resolve() == pdfr:
            return s.get("id") or "primary"
    return "primary"


def _lines_from_ocr_words(words) -> list:
    """Rebuild /api/pdf/words' line structure from stored OCR word boxes
    (already 0..1 of the page). Words that carry the OCR engine's line id (`l`)
    are grouped by it — the engine segments lines far better than box geometry;
    any without one fall back to baseline clustering (baseline = box bottom).
    Per line, baseline = median box bottom and size = median height, so the
    client places these identically to the text-layer path."""
    from statistics import median
    groups: dict = {}
    loose: list = []
    for w in words or []:
        t = str((w or {}).get("t") or "")
        if not t.strip():
            continue
        try:
            x = float(w.get("x") or 0)
            y = float(w.get("y") or 0)
            ww = float(w.get("w") or 0)
            h = float(w.get("h") or 0)
        except (TypeError, ValueError):
            continue
        if h <= 0 or ww <= 0:
            continue
        rec = (y + h, x, x + ww, t, h)      # (baseline, x0, x1, text, size)
        lid = (w or {}).get("l")
        if isinstance(lid, int):
            groups.setdefault(lid, []).append(rec)
        else:
            loose.append(rec)
    # words with no line id: cluster on the baseline like the text-layer path
    loose.sort()
    cur: list = []
    for r in loose:
        if cur and abs(r[0] - cur[-1][0]) <= 0.4 * cur[-1][4]:
            cur.append(r)          # same list object lives in `groups`
        else:
            cur = [r]
            groups[("loose", len(groups))] = cur
    lines = []
    for key in sorted(groups, key=lambda k: median([r[0] for r in groups[k]])):
        c = sorted(groups[key], key=lambda r: r[1])
        lines.append({"y": round(median([r[0] for r in c]), 5),
                      "s": round(median([r[4] for r in c]), 6),
                      "spans": [{"t": r[3], "x": round(r[1], 5),
                                 "w": round(r[2] - r[1], 5)} for r in c]})
    return lines


# page count + dimensions, cached on mtime: the OCR tab asks on every page-view
# render, and pypdf walked the whole xref of a 400 MB scan to answer
_pdf_info_cache: dict[str, tuple[float, dict]] = {}


@app.route("/api/pdf/info")
def api_pdf_info():
    """Page count and per-page [w, h] (PDF points) of a local PDF. The
    dimensions let the client reserve every page's box before its image
    loads, so lazy loading never shifts the scroll position."""
    p = _pageimg_pdf(request.args.get("path"))
    _warm_pages_async(p)          # fill the page cache ahead of the scroll
    key = str(p)
    try:
        mtime = p.stat().st_mtime
        hit = _pdf_info_cache.get(key)
        if hit and hit[0] == mtime:
            return jsonify(hit[1])
        try:
            import fitz
            doc = fitz.open(key)
            try:
                dims = [[round(pg.rect.width, 2), round(pg.rect.height, 2)]
                        for pg in doc]
                out = {"ok": True, "pages": doc.page_count, "dims": dims}
            finally:
                doc.close()
        except ImportError:
            from pypdf import PdfReader
            out = {"ok": True, "pages": len(PdfReader(key).pages)}
        if len(_pdf_info_cache) > 64:
            _pdf_info_cache.clear()
        _pdf_info_cache[key] = (mtime, out)
        return jsonify(out)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def _render_pdf_page(pdf_path: Path, page: int, w: int) -> Path:
    """Render one page of a local PDF to a cached image file, returning its
    path. Cached on disk by path+mtime+page+width (see _page_cache_key). JPEG:
    a scanned page as PNG runs 500 KB+ against ~80 KB, and encodes slower too —
    these are photographs, not line art. Older caches hold .png files; they
    stay valid and are returned as-is.

    Pulled out of the /api/pdf/pageimg route so the publish pipeline
    (thumbnails) can render a page directly, without an HTTP round trip, and
    share the same on-disk cache (and the _pdf_doc handle cache) the OCR tab's
    page view already warms. Raises ImportError if PyMuPDF is unavailable,
    FileNotFoundError if `page` is out of range."""
    import fitz  # PyMuPDF
    cache = _pages_cache_dir()
    key = _page_cache_key(pdf_path, pdf_path.stat().st_mtime, page, w)
    old = cache / f"{key}.png"
    if old.is_file():
        return old
    out = cache / f"{key}.jpg"
    if not out.is_file():
        with _pdf_doc(pdf_path) as doc:
            if page > doc.page_count:
                raise FileNotFoundError(f"page {page} out of range")
            pg = doc[page - 1]
            zoom = w / max(1.0, pg.rect.width)
            pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            tmp = out.with_suffix(f".{page}.tmp.jpg")
            try:
                tmp.write_bytes(pix.tobytes("jpeg", jpg_quality=82))
            except Exception:
                # PyMuPDF without JPEG output: fall back to PNG
                tmp = old.with_suffix(f".{page}.tmp.png")
                pix.save(str(tmp))
                tmp.replace(old)
                return old
            tmp.replace(out)
    return out


@app.route("/api/pdf/pageimg")
def api_pdf_pageimg():
    """One page of a local PDF rendered as an image (?path=&page=N&w=W)."""
    p = _pageimg_pdf(request.args.get("path"))
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    try:
        w = max(200, min(1600, int(request.args.get("w") or 700)))
    except ValueError:
        w = 700
    try:
        out = _render_pdf_page(p, page, w)
    except ImportError:
        return jsonify({"ok": False, "error": "PyMuPDF is not installed"}), 501
    except FileNotFoundError:
        abort(404)
    mime = "image/png" if out.suffix == ".png" else "image/jpeg"
    return _cached_page_file(out, mime)


# --- unified background-job registry ---------------------------------------------
# Every long-running worker (OCR, analyze, publish) enters ONE registry with a
# shared lifecycle: queued | running | cancelling | cancelled | failed | done,
# plus `interrupted` for work a restart cut short. Entries are the SAME dicts
# the per-kind registries hold, so the existing pollers keep their legacy
# fields (`status` strings) while /api/jobs and the snapshot read the
# canonical ones (`state`). The snapshot — an allowlist, never credentials,
# request payloads, or prompt text — lands in DATA_ROOT/output/jobs.json on
# every state transition, so after a restart a poll gets an honest
# "interrupted" answer instead of a 404. Cancellation is cooperative: a
# threading.Event per job (never serialized), checked by each worker at its
# natural boundary (OCR page, analyze chunk, publish stage).

JOBS_PATH = lib.OUTPUT_DIR / "jobs.json"
_JOBS_KEEP = 50                       # newest finished/interrupted entries kept
_JOB_ACTIVE = ("queued", "running", "cancelling")
_JOB_FIELDS = ("id", "kind", "build_id", "label", "state", "status", "done",
               "total", "errors", "error", "note", "created_at", "finished_at")
_JOB_STATES = {"queued": "queued", "running": "running",
               "cancelling": "cancelling", "cancelled": "cancelled",
               "error": "failed", "done": "done", "done (with errors)": "done",
               "interrupted": "interrupted"}
_jobs: dict[str, dict] = {}
_jobs_events: dict[str, threading.Event] = {}   # cancel flags; never serialized
_jobs_lock = threading.Lock()


class _JobCancelled(Exception):
    """Raised inside a worker at a stage boundary after a cancel request."""


def _job_state_of(status) -> str:
    return _JOB_STATES.get(str(status or ""), "running")


def _job_public(job: dict) -> dict:
    return {k: job.get(k) for k in _JOB_FIELDS if k in job}


def _jobs_save_locked() -> None:
    try:
        lib.save_json(JOBS_PATH, {jid: _job_public(j) for jid, j in _jobs.items()})
    except OSError:
        log.warning("could not persist the job registry", exc_info=True)


def _job_book_label(bid: str) -> str:
    b = lib.load_json(BUILDS_PATH, {}).get(str(bid or "")) or {}
    return str(b.get("title") or "").strip() or str(bid or "")


def _jobs_prune_locked() -> None:
    """Drop the oldest finished/interrupted entries beyond the retention cap.
    Active jobs are never pruned."""
    done = sorted((j for j in _jobs.values()
                   if j.get("state") not in _JOB_ACTIVE),
                  key=lambda j: str(j.get("finished_at")
                                    or j.get("created_at") or ""),
                  reverse=True)
    for old in done[_JOBS_KEEP:]:
        _jobs.pop(str(old.get("id")), None)
        _jobs_events.pop(str(old.get("id")), None)


def _job_track(job: dict, kind: str, label: str = "") -> threading.Event:
    """Enter a per-kind job dict into the unified registry (shared dict) and
    return its cancellation event. Insertion prunes the oldest finished
    entries beyond _JOBS_KEEP and persists the snapshot."""
    ev = threading.Event()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _jobs_lock:
        job.setdefault("id", lib.gen_id(set(_jobs)))
        job.setdefault("kind", kind)
        job.setdefault("build_id", "")
        for k, v in (("done", 0), ("total", 0), ("errors", 0), ("note", "")):
            job.setdefault(k, v)
        job.setdefault("status", "running")
        job["label"] = label or str(job.get("label") or "")
        job["state"] = _job_state_of(job["status"])
        job["created_at"] = now
        job["finished_at"] = ""
        _jobs[job["id"]] = job
        _jobs_events[job["id"]] = ev
        _jobs_prune_locked()
        _jobs_save_locked()
    return ev


def _job_transition_locked(job: dict, status: str, **fields) -> None:
    """The transition body for callers that already hold ``_jobs_lock``."""
    job.update(fields)
    job["status"] = status
    job["state"] = _job_state_of(status)
    if job["state"] not in _JOB_ACTIVE and not job.get("finished_at"):
        job["finished_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
    if _jobs.get(str(job.get("id") or "")) is job:
        if job["state"] not in _JOB_ACTIVE:
            _jobs_prune_locked()
        _jobs_save_locked()


def _job_transition(job: dict, status: str, **fields) -> None:
    """Move a job to a lifecycle status (legacy string), stamp the canonical
    state, and persist. Safe on untracked dicts (tests build jobs directly)."""
    with _jobs_lock:
        _job_transition_locked(job, status, **fields)


def _job_checkpoint(job: dict, force: bool = False) -> None:
    """Persist live progress at page/chunk boundaries, throttled to 1 Hz."""
    now = time.monotonic()
    with _jobs_lock:
        if _jobs.get(str(job.get("id") or "")) is not job:
            return
        last = float(job.get("_checkpoint_at") or 0.0)
        if not force and now - last < 1.0:
            return
        job["_checkpoint_at"] = now       # internal; _JOB_FIELDS omits it
        _jobs_save_locked()


def _job_request_cancel(job_id: str, fallback: dict | None = None) -> dict | None:
    """Atomically request cancellation and return a stable job snapshot.

    The active-state check and the ``cancelling`` transition must share the
    registry lock with worker terminal transitions.  Otherwise a worker can
    finish after the check but before the transition, and the request handler
    overwrites ``done`` with a permanently-active ``cancelling`` state.
    ``fallback`` preserves the legacy OCR endpoint's unit-test/untracked-job
    behavior; production OCR jobs are always in the unified registry.
    """
    with _jobs_lock:
        job = _jobs.get(job_id) or fallback
        if job is None:
            return None
        state = job.get("state") or _job_state_of(job.get("status"))
        ev = _jobs_events.get(job_id)
        if state in _JOB_ACTIVE:
            if ev is not None:
                ev.set()
            if job.get("kind") == "ocr" or fallback is not None:
                job["cancel_requested"] = True
            _job_transition_locked(job, "cancelling")
        return dict(job)


def _job_cancelled(job: dict) -> bool:
    ev = _jobs_events.get(str(job.get("id") or ""))
    return ev is not None and ev.is_set()


def _job_interrupt_note(kind: str) -> str:
    k = str(kind or "")
    if k == "ocr" or k.startswith("translate") or k == "annotate":
        return "interrupted by restart — progressive output kept"
    if k == "publish":
        return "interrupted by restart — not applied"
    return "interrupted by restart — output not written"


def _jobs_load() -> None:
    """Rehydrate the persisted registry on startup: whatever was still active
    when the process died becomes `interrupted`, distinguishing resumable
    output (progressively-saved OCR/translation pages) from abandoned work.
    Live entries are never clobbered."""
    try:
        stored = lib.load_json(JOBS_PATH, {})
    except (OSError, ValueError):
        return
    if not isinstance(stored, dict) or not stored:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _jobs_lock:
        for jid, raw in stored.items():
            if not isinstance(raw, dict) or jid in _jobs:
                continue
            job = _job_public(raw)
            job["id"] = str(jid)
            if job.get("state") in _JOB_ACTIVE or not job.get("state"):
                job["status"] = job["state"] = "interrupted"
                job["note"] = _job_interrupt_note(job.get("kind"))
                job["finished_at"] = job.get("finished_at") or now
            _jobs[job["id"]] = job
        _jobs_prune_locked()
        _jobs_save_locked()


_jobs_load()


@app.route("/api/jobs")
def api_jobs():
    """Every registry entry (oldest first) plus read-only rows for the other
    background work — IA downloads and cloud sync — so one poll paints the
    whole queue table."""
    with _jobs_lock:
        rows = [_job_public(j) for j in _jobs.values()]
    rows.sort(key=lambda r: (str(r.get("created_at") or ""),
                             str(r.get("id") or "")))
    with _downloads_lock:
        for ident, d in _downloads.items():
            if d.get("status") == "downloading":
                rows.append({"kind": "download", "label": ident,
                             "state": "running",
                             "done": int(d.get("bytes") or 0),
                             "total": int(d.get("total") or 0)})
    with _cloudsync_lock:
        if _cloudsync.get("running"):
            rows.append({"kind": "cloudsync", "label": "Cloud sync",
                         "state": "running"})
    active = sum(1 for r in rows if r.get("state") in _JOB_ACTIVE)
    return jsonify({"ok": True, "jobs": rows, "active": active})


@app.route("/api/jobs/active")
def api_jobs_active():
    """Count + labels of unfinished work, for the desktop shell's quit guard.
    Cloud sync is deliberately excluded: it converges on its next run."""
    with _jobs_lock:
        act = [_job_public(j) for j in _jobs.values()
               if j.get("state") in _JOB_ACTIVE]
    act.sort(key=lambda r: str(r.get("created_at") or ""))
    jobs = [{"id": r.get("id") or "", "kind": r.get("kind") or "",
             "label": r.get("label") or "", "cancellable": True} for r in act]
    with _downloads_lock:
        for ident, d in _downloads.items():
            if d.get("status") == "downloading":
                jobs.append({"id": "", "kind": "download", "label": ident,
                             "cancellable": False})
    labels = [f"{j['kind']}: {j['label']}" if j["label"] else j["kind"]
              for j in jobs]
    return jsonify({"ok": True, "count": len(jobs), "labels": labels,
                    "jobs": jobs})


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def api_jobs_cancel(job_id: str):
    """Cooperative cancel for any tracked job kind. The worker notices at its
    next boundary (OCR page, analyze chunk, publish stage); already-finished
    jobs return unchanged (idempotent)."""
    job = _job_request_cancel(job_id)
    if job is None:
        abort(404)
    return jsonify({"ok": True, "job": _job_public(job)})


# --- OCR processing jobs -----------------------------------------------------------
# Pages are rasterized (PyMuPDF) and run through the chosen OCR service;
# every finished page is merged into ONE compiled OCR file in the entry
# folder (ocr/compiled.txt) and saved immediately, so results from
# different services land in a single document and nothing is lost if a
# job dies part-way.

_ocr_jobs: dict[str, dict] = {}
_ocr_jobs_lock = threading.Lock()
# serializes every compiled-file merge: concurrent jobs (one POST per digit
# shortcut) must not lose each other's pages in the read-modify-write
_ocr_merge_lock = threading.Lock()

_TESSERACT_DEFAULT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def _ocr_page_png(pdf: Path, page: int, width: int) -> bytes:
    import fitz
    doc = fitz.open(str(pdf))
    try:
        pg = doc[page - 1]
        zoom = width / max(1.0, pg.rect.width)
        pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        return pix.tobytes("png")
    finally:
        doc.close()


def _ocr_tesseract(png: bytes, cfg: dict) -> dict:
    """One Tesseract pass via image_to_data, so the word boxes are kept, not
    just the text: `words` (each box normalised to 0..1 of the page) lets the
    Layout view place an image-only scan the same way a text-layer PDF is
    placed. The transcription is rebuilt from the same data — one OCR run, not
    two — grouping words by Tesseract's block/paragraph/line and breaking
    paragraphs where the block or paragraph changes."""
    import pytesseract
    from pytesseract import Output
    from PIL import Image
    import io as _io
    exe = (cfg.get("tesseract") or "").strip() or _TESSERACT_DEFAULT
    if Path(exe).is_file():
        pytesseract.pytesseract.tesseract_cmd = exe
    img = Image.open(_io.BytesIO(png))
    iw, ih = img.size
    data = pytesseract.image_to_data(img, output_type=Output.DICT)
    words: list[dict] = []
    grouped: dict[tuple, list[str]] = {}
    line_ids: dict[tuple, int] = {}    # (block,par,line) -> reading-order id
    for i in range(len(data.get("text", []))):
        t = (data["text"][i] or "").strip()
        if not t:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 0:            # -1 marks the structural (non-word) rows
            continue
        lk = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lid = line_ids.setdefault(lk, len(line_ids))   # first seen = reading order
        if iw > 0 and ih > 0:
            # `l` is Tesseract's own line grouping — carried through so the
            # facsimile rebuilds real lines instead of re-guessing them from box
            # geometry (descenders make a per-word box bottom a poor baseline)
            words.append({"t": t, "l": lid,
                          "x": round(data["left"][i] / iw, 5),
                          "y": round(data["top"][i] / ih, 5),
                          "w": round(data["width"][i] / iw, 5),
                          "h": round(data["height"][i] / ih, 5)})
        grouped.setdefault(lk, []).append(t)
    parts: list[str] = []
    prev = None
    for key in sorted(grouped):
        if prev is not None and key[:2] != prev[:2]:
            parts.append("")     # blank line between paragraphs/blocks
        parts.append(" ".join(grouped[key]))
        prev = key
    return {"text": "\n".join(parts), "words": words}


def _ocr_max_tokens() -> int:
    """Vision-OCR output cap (Settings > OCR); dense pages need it raised."""
    try:
        return max(1024, min(32000, int(_client_settings().get("ocrMaxTokens") or 8192)))
    except (TypeError, ValueError):
        return 8192


def _ocr_claude(png: bytes, cfg: dict) -> str:
    key = (cfg.get("claude_key") or "").strip()
    if not key:
        raise RuntimeError("Anthropic API key not configured (Settings > OCR)")
    import base64
    model = (cfg.get("claude_model") or "").strip() or "claude-haiku-4-5-20251001"
    body = json.dumps({
        "model": model,
        "max_tokens": _ocr_max_tokens(),
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.b64encode(png).decode("ascii")}},
            {"type": "text", "text":
                "Transcribe ALL text on this scanned book page exactly as "
                "printed, preserving line breaks. Output only the "
                "transcription, no commentary. If the page is blank, "
                "output nothing."},
        ]}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return "".join(blk.get("text", "") for blk in data.get("content", []))


def _ocr_textract(png: bytes, cfg: dict) -> str:
    key = (cfg.get("aws_key") or "").strip()
    secret = (cfg.get("aws_secret") or "").strip()
    if not (key and secret):
        raise RuntimeError("AWS credentials not configured (Settings > OCR)")
    try:
        import boto3
    except ImportError:
        raise RuntimeError("boto3 is not installed (python3 -m pip install boto3)")
    client = boto3.client(
        "textract", region_name=(cfg.get("aws_region") or "us-east-1").strip(),
        aws_access_key_id=key, aws_secret_access_key=secret)
    resp = client.detect_document_text(Document={"Bytes": png})
    blocks = resp.get("Blocks", [])
    # Textract already reports Geometry.BoundingBox as 0..1 of the page, so the
    # Layout word boxes come for free alongside the line transcription. Its LINE
    # blocks carry the line grouping (via CHILD relationships) -> each word's `l`.
    word_line = {}
    lidx = 0
    for b in blocks:
        if b.get("BlockType") != "LINE":
            continue
        for rel in b.get("Relationships") or []:
            if rel.get("Type") == "CHILD":
                for cid in rel.get("Ids") or []:
                    word_line[cid] = lidx
        lidx += 1
    words = []
    for b in blocks:
        if b.get("BlockType") != "WORD":
            continue
        bb = (b.get("Geometry") or {}).get("BoundingBox") or {}
        w = {"t": b.get("Text") or "",
             "x": round(float(bb.get("Left") or 0), 5),
             "y": round(float(bb.get("Top") or 0), 5),
             "w": round(float(bb.get("Width") or 0), 5),
             "h": round(float(bb.get("Height") or 0), 5)}
        if b.get("Id") in word_line:
            w["l"] = word_line[b["Id"]]
        words.append(w)
    text = "\n".join(b["Text"] for b in blocks if b.get("BlockType") == "LINE")
    return {"text": text, "words": words}


def _ocr_mistral(png: bytes, cfg: dict) -> dict:
    """Mistral returns markdown plus the figures it cut out of the page.
    The result dict carries the text and the decoded images with their
    boxes normalised to 0..1 of the page, ready for _ocr_save_page_images."""
    key = (cfg.get("mistral_key") or "").strip()
    if not key:
        raise RuntimeError("Mistral API key not configured (Settings > OCR)")
    import base64
    pages = capture.mistral_ocr_pages(png, key, want_images=True)
    text = "\n\n".join(p.get("markdown", "") for p in pages).strip()
    images = []
    for pg in pages:
        dim = pg.get("dimensions") or {}
        pw, ph = float(dim.get("width") or 0), float(dim.get("height") or 0)
        for im in pg.get("images") or []:
            b64 = str(im.get("image_base64") or "")
            if "," in b64[:64]:                 # strip a data: URL prefix
                b64 = b64.split(",", 1)[1]
            try:
                raw = base64.b64decode(b64)
            except Exception:
                continue
            if not raw:
                continue
            bbox = None
            if pw > 0 and ph > 0:
                x0 = float(im.get("top_left_x") or 0)
                y0 = float(im.get("top_left_y") or 0)
                x1 = float(im.get("bottom_right_x") or 0)
                y1 = float(im.get("bottom_right_y") or 0)
                bbox = {"x": round(x0 / pw, 5), "y": round(y0 / ph, 5),
                        "w": round(max(0.0, x1 - x0) / pw, 5),
                        "h": round(max(0.0, y1 - y0) / ph, 5)}
            images.append({"id": str(im.get("id") or f"img-{len(images)}.jpeg"),
                           "data": raw, "bbox": bbox})
    return {"text": text, "images": images}


_OCR_SERVICES = {
    "tesseract": _ocr_tesseract,
    "claude": _ocr_claude,
    "textract": _ocr_textract,
    "mistral": _ocr_mistral,
}


def _ocr_merge_page(build_id: str, target: str, page: int, text: str) -> None:
    """Merge one page's OCR into the compiled document (page-marker format)
    and save immediately. Serialized: concurrent jobs merge into the same
    file without losing each other's pages."""
    with _ocr_merge_lock:
        f = _entry_dir(build_id) / "ocr" / _ocr_name(target)
        f.parent.mkdir(parents=True, exist_ok=True)
        sections: dict[int, str] = {}
        pre = ""
        if f.is_file():
            raw = f.read_text(encoding="utf-8", errors="replace")
            marks = list(re.finditer(r"^--- page (\d+) ---$", raw, re.M))
            pre = raw[:marks[0].start()].rstrip("\n") if marks else raw.rstrip("\n")
            for i, m in enumerate(marks):
                to = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
                sections[int(m.group(1))] = raw[m.end():to].strip("\n")
        sections[page] = text.strip("\n")
        parts = ([pre] if pre else []) + [
            f"--- page {n} ---\n{sections[n]}" for n in sorted(sections)]
        f.write_text("\n\n".join(parts), encoding="utf-8", errors="replace")


def _ocr_save_page_images(build_id: str, page: int, images: list[dict],
                          text: str, src_key: str = "primary") -> str:
    """Persist the figures an OCR service cut out of one page.

    Files land in the entry folder (ocr/images/p<page>-<id>), their boxes in
    ocr/layout.json, and the markdown's ![id](id) references are rewritten to
    the saved names so every reference stays unique across the compiled file.
    Returns the rewritten text."""
    if not images:
        return text
    d = _entry_dir(build_id) / "ocr" / "images"
    d.mkdir(parents=True, exist_ok=True)
    meta_path = _entry_dir(build_id) / "ocr" / "layout.json"
    with _ocr_merge_lock:
        meta = lib.load_json(meta_path, {})
        meta.setdefault("images", {})
        for im in images:
            safe = re.sub(r"[^\w.\-]", "_", im["id"]) or "img"
            if "." not in safe:
                safe += ".jpeg"
            name = f"p{page}-{safe}"
            (d / name).write_bytes(im["data"])
            meta["images"][name] = dict(
                im["bbox"] or {}, page=page, src_key=src_key or "primary")
            # every ![id](id) in this page's markdown points at the saved file
            text = re.sub(r"(!\[[^\]]*\]\()" + re.escape(im["id"]) + r"(\))",
                          r"\g<1>" + name + r"\g<2>", text)
        lib.save_json(meta_path, meta)
    return text


def _ocr_save_page_words(build_id: str, src_key: str, page: int, words: list) -> None:
    """Persist one page's OCR word boxes to the sidecar (ocr/layout.json,
    {words: {"<src>": {"<page>": [{t,x,y,w,h,l}, ...]}}}). /api/pdf/words reads
    these back for a scan with no text layer, so the Layout facsimile works on
    it too. Keyed by SOURCE like the compiled .txt files, so a secondary scan's
    boxes never clobber the primary's. An empty list DROPS the page — a re-OCR
    with a service that has no boxes (Claude, Mistral) must not leave a stale
    facsimile behind the new transcription."""
    src_key = src_key or "primary"
    meta_path = _entry_dir(build_id) / "ocr" / "layout.json"
    with _ocr_merge_lock:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta = lib.load_json(meta_path, {})
        wmap = meta.setdefault("words", {})
        pages = wmap.setdefault(src_key, {})
        if words:
            pages[str(int(page))] = words
        else:
            pages.pop(str(int(page)), None)
            if not pages:
                wmap.pop(src_key, None)
        lib.save_json(meta_path, meta)


def _ocr_job_run(job_id: str) -> None:
    job = _ocr_jobs[job_id]
    cfg = job["cfg"]
    pdf = Path(job["pdf"])
    for index, item in enumerate(job["pages"]):
        # OCR engines are synchronous calls, so cancellation takes effect at
        # the page boundary. A page already inside Tesseract/API processing is
        # allowed to finish and be saved; everything after it is skipped.
        if _job_cancelled(job):             # unified /api/jobs/<id>/cancel
            job["cancel_requested"] = True
        if job.get("cancel_requested"):
            for pending in job["pages"][index:]:
                if pending["status"] == "queued":
                    pending["status"] = "cancelled"
                    job["cancelled"] += 1
            break
        n, svc = item["page"], item["service"]
        item["status"] = "running"
        try:
            png = _ocr_page_png(pdf, n, job["width"])
            runner = _OCR_SERVICES.get(svc)
            if runner is None:
                raise RuntimeError(f"unsupported service: {svc}")
            result = runner(png, cfg)
            # a runner may return a dict instead of a string: {text, images}
            # (Mistral figures) and/or {text, words} (Tesseract/Textract boxes)
            src_key = job.get("src_key") or "primary"
            if isinstance(result, dict):
                text = str(result.get("text") or "")
                if result.get("images"):
                    text = _ocr_save_page_images(
                        job["build_id"], n, result["images"], text, src_key)
                # save boxes when the service produced them, else clear this
                # page's stale boxes (a figures-only or text-only re-OCR)
                _ocr_save_page_words(job["build_id"], src_key, n,
                                     result.get("words") or [])
            else:
                text = result
                _ocr_save_page_words(job["build_id"], src_key, n, [])
            _ocr_merge_page(job["build_id"], job["target"], n, text)
            item["status"] = "ok"
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            item["status"] = f"error: {detail}"
            job["errors"] += 1
            # Background failures otherwise exist only in the transient job
            # object. Emit them to the ring consumed by the Info tab, with
            # enough context to diagnose the executable, dependency, PDF, or
            # individual bad page without reproducing under a debugger.
            log.error("OCR failed: book=%s page=%s service=%s: %s",
                      job["build_id"], n, svc, detail, exc_info=True)
        job["done"] += 1
        _job_checkpoint(job)
    # provenance at job completion (cancelled-partial included): the compiled
    # doc and its layout sidecar came from THIS source PDF via these engines
    engines = sorted({i["service"] for i in job["pages"]
                      if i["status"] == "ok"})
    if engines:
        produced = {"kind": "ocr", "engine": ", ".join(engines)}
        src_ref = [_manifest_input(
            job["build_id"], f"pdf:{job.get('src_key') or 'primary'}",
            path=pdf)]
        _manifest_record(job["build_id"], f"ocr/{_ocr_name(job['target'])}",
                         produced, src_ref)
        if (_entry_dir(job["build_id"]) / "ocr" / "layout.json").is_file():
            _manifest_record(job["build_id"], "ocr/layout.json",
                             produced, src_ref)
    if job.get("cancel_requested") or _job_cancelled(job):
        # Covers a request received while the final page was processing.
        for pending in job["pages"]:
            if pending["status"] == "queued":
                pending["status"] = "cancelled"
                job["cancelled"] += 1
        _job_transition(job, "cancelled",
                        note=f"{job['done']} page(s) completed and saved; "
                             f"{job['cancelled']} skipped")
        log.info("OCR cancelled: book=%s completed=%s skipped=%s",
                 job["build_id"], job["done"], job["cancelled"])
    else:
        _job_transition(job, "done" if not job["errors"]
                        else "done (with errors)")


@app.route("/api/ocr/run", methods=["POST"])
def api_ocr_run():
    """Queue pages of a build's PDF for OCR.
    Body: {build_id, pdf, pages: [{page, service}], target?, width?,
           tesseract?, claude_key?, claude_model?, aws_key?, aws_secret?,
           aws_region?}."""
    p = request.get_json(silent=True) or {}
    build_id = str(p.get("build_id") or "")
    if build_id not in lib.load_json(BUILDS_PATH, {}):
        abort(404)
    with _page_structure_lock:
        source_revision = _page_structure_revision.get(build_id, 0)
    pdf = _resolve_local(str(p.get("pdf") or ""))
    if pdf is None or not pdf.is_file():
        return jsonify({"ok": False, "error": "PDF not found"})
    pages = [{"page": int(x.get("page")), "service": str(x.get("service") or ""),
              "status": "queued"}
             for x in (p.get("pages") or []) if int(x.get("page", 0)) > 0]
    if not pages:
        return jsonify({"ok": False, "error": "no pages"})
    try:
        width = max(600, min(3000, int(p.get("width") or 1400)))
    except (TypeError, ValueError):
        width = 1400
    job_id = lib.gen_id(set(_ocr_jobs) | set(_jobs))
    # the merged result belongs to the PDF it was read from (?src= key);
    # an unknown/removed key is refused rather than recorded
    src_key = _valid_src_key(lib.load_json(BUILDS_PATH, {}).get(build_id, {}),
                             p.get("src"))
    job = {
        "id": job_id, "build_id": build_id, "pdf": str(pdf),
        "target": str(p.get("target") or "compiled.txt"),
        "src_key": src_key or "primary",     # word boxes are stored per source
        "pages": pages, "done": 0, "total": len(pages), "errors": 0,
        "cancelled": 0,
        "cancel_requested": False, "width": width,
        "status": "running",
        "cfg": _ocr_request_cfg(p),
    }
    if not _ocr_job_start_guarded(job, source_revision, bool(src_key)):
        return jsonify({"ok": False, "error":
                        "page numbering changed — review the pages and retry"}), 409
    return jsonify({"ok": True, "job": _ocr_job_state(job)})


def _ocr_request_cfg(payload: dict) -> dict:
    """Build an OCR worker config from the authoritative local secret store."""
    local = _client_settings()
    cfg = {k: payload.get(k) for k in (
        "tesseract", "claude_key", "claude_model", "aws_key", "aws_secret",
        "aws_region", "mistral_key",
    )}
    for request_key, setting_key in (
        ("mistral_key", "mistralKey"),
        ("claude_key", "ocrClaudeKey"),
        ("aws_key", "ocrAwsKey"),
        ("aws_secret", "ocrAwsSecret"),
    ):
        cfg[request_key] = local.get(setting_key) or cfg.get(request_key)
    return cfg


def _ocr_job_state(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != "cfg"}


@app.route("/api/ocr/job/<job_id>")
def api_ocr_job(job_id: str):
    job = _ocr_jobs.get(job_id)
    if not job:
        # a restart dropped the worker: report the persisted outcome
        # (usually `interrupted`) instead of a 404 the client reads as "lost"
        with _jobs_lock:
            gone = _jobs.get(job_id)
        if gone is None:
            abort(404)
        return jsonify({"ok": True,
                        "job": dict(_job_public(gone), pages=[], cancelled=0)})
    return jsonify({"ok": True, "job": _ocr_job_state(job)})


@app.route("/api/ocr/job/<job_id>/cancel", methods=["POST"])
def api_ocr_job_cancel(job_id: str):
    """Request a cooperative stop after the currently processing page."""
    with _ocr_jobs_lock:
        job = _ocr_jobs.get(job_id)
        if not job:
            abort(404)
    snapshot = _job_request_cancel(job_id, fallback=job)
    return jsonify({"ok": True,
                    "job": {k: v for k, v in snapshot.items() if k != "cfg"}})


# --- PDF page deletion ---------------------------------------------------------------

_page_structure_lock = threading.RLock()
_page_structure_revision: dict[str, int] = {}


def _ocr_job_start_guarded(job: dict, source_revision: int,
                           record_source: bool = False) -> bool:
    """Register/start OCR atomically against page deletion."""
    build_id = str(job.get("build_id") or "")
    with _page_structure_lock:
        if _page_structure_revision.get(build_id, 0) != source_revision:
            return False
        if record_source:
            _ocr_set_source(build_id, _ocr_name(job.get("target") or "compiled.txt"),
                            job.get("src_key") or "primary")
        with _ocr_jobs_lock:
            _ocr_jobs[job["id"]] = job
        _job_track(job, "ocr", label=_job_book_label(build_id))
        threading.Thread(target=_ocr_job_run, args=(job["id"],),
                         daemon=True).start()
        return True


def _page_job_blockers(build_id: str) -> list[dict]:
    """Live jobs whose page snapshot a deletion would invalidate."""
    with _ocr_jobs_lock:
        ocr = [j for j in _ocr_jobs.values()
               if j.get("build_id") == build_id
               and (j.get("state") or _job_state_of(j.get("status")))
               in _JOB_ACTIVE]
    # Defined later in the module, but always present by the time a request or
    # folder-sync worker can call this helper.
    with _an_jobs_lock:
        analyze = [j for j in _an_jobs.values()
                   if j.get("build_id") == build_id
                   and (j.get("state") or _job_state_of(j.get("status")))
                   in _JOB_ACTIVE]
    return ocr + analyze


def _renumber_layout_words(build_id: str, src_key: str, removed: list[int]) -> None:
    """Remap page-keyed word boxes and extracted-image layout metadata.

    Word boxes are source-scoped. Extracted figures belong to the OCR document
    recorded in ``sources.json``; only figures for the PDF being edited move.
    Deleted-page image files stay on disk as recovery artifacts, but disappear
    from layout metadata so they cannot be placed on the wrong page.
    """
    meta_path = _entry_dir(build_id) / "ocr" / "layout.json"
    if not meta_path.is_file():
        return
    removed_set = set(removed)
    with _ocr_merge_lock:
        meta = lib.load_json(meta_path, {})
        dirty = False
        wmap = meta.get("words")
        if isinstance(wmap, dict):
            pages = wmap.get(src_key or "primary")
            if isinstance(pages, dict):
                remapped = {}
                for k, v in pages.items():
                    try:
                        n = int(k)
                    except (TypeError, ValueError):
                        continue
                    if n in removed_set:
                        continue
                    remapped[str(n - sum(1 for r in removed if r < n))] = v
                wmap[src_key or "primary"] = remapped
                dirty = True

        images = meta.get("images")
        if isinstance(images, dict):
            for name, info in list(images.items()):
                if not isinstance(info, dict):
                    continue
                # Older sidecars predate src_key and therefore belong to the
                # historical primary source.
                if str(info.get("src_key") or "primary") != src_key:
                    continue
                try:
                    n = int(info.get("page"))
                except (TypeError, ValueError):
                    continue
                if n in removed_set:
                    images.pop(name, None)
                    image = _entry_dir(build_id) / "ocr" / "images" / name
                    if image.is_file():
                        import shutil
                        backup = image.parent / ".page-delete-backup"
                        backup.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(image, backup / name)
                        image.unlink()
                else:
                    info["page"] = n - sum(1 for r in removed if r < n)
                dirty = True
        if dirty:
            lib.save_json(meta_path, meta)


def _renumber_translation_artifacts(build_id: str, src_key: str,
                                    removed: list[int]) -> list[str]:
    """Keep translated text and its source-hash sidecars page-aligned.
    Returns the entry-relative paths of the renumbered translations."""
    d = _entry_dir(build_id) / "translations"
    renumbered: list[str] = []
    if not d.is_dir():
        return renumbered
    srcmap = _ocr_sources(build_id)
    removed_set = set(removed)
    with _an_write_lock:
        for text_path in sorted(d.glob("*.txt")):
            lang = text_path.stem
            meta_path = _translation_meta_path(build_id, lang)
            meta = _load_translation_meta(build_id, lang)
            source_doc = str(meta.get("src") or "")
            source_key = srcmap.get(source_doc) or "primary"
            if source_key != src_key:
                continue
            renumbered.append(f"translations/{text_path.name}")

            raw = text_path.read_text(encoding="utf-8", errors="replace")
            text_path.with_name(text_path.name + ".bak").write_text(
                raw, encoding="utf-8", errors="replace")
            text_path.write_text(_renumber_marked_text(raw, removed),
                                 encoding="utf-8", errors="replace")

            if not meta_path.is_file():
                continue
            meta_path.with_name(meta_path.name + ".bak").write_text(
                meta_path.read_text(encoding="utf-8", errors="replace"),
                encoding="utf-8", errors="replace")
            remapped = {}
            for key, rec in meta.get("pages", {}).items():
                try:
                    n = int(key)
                except (TypeError, ValueError):
                    remapped[str(key)] = rec
                    continue
                if n in removed_set:
                    continue
                remapped[str(n - sum(1 for r in removed if r < n))] = rec
            meta["pages"] = remapped
            lib.save_json(meta_path, meta)
    return renumbered


def _renumber_marked_text(text: str, removed: list[int]) -> str:
    """Remap "--- page N ---" markers after pages were deleted: sections for
    removed pages are dropped, higher page numbers shift down."""
    marks = list(re.finditer(r"^--- page (\d+) ---$", text, re.M))
    if not marks:
        return text
    removed_set = set(removed)
    pre = text[:marks[0].start()].rstrip("\n")
    parts = [pre] if pre else []
    for i, m in enumerate(marks):
        n = int(m.group(1))
        if n in removed_set:
            continue
        to = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        shift = sum(1 for r in removed if r < n)
        parts.append(f"--- page {n - shift} ---\n" + text[m.end():to].strip("\n"))
    return "\n\n".join(parts)


@app.route("/api/pdf/pages/delete", methods=["POST"])
def api_pdf_pages_delete():
    """Delete pages from a build's PDF — the real file, not a preview.
    Body: {build_id, pdf, pages: [1-based numbers]}.

    The pre-deletion file is kept next to the PDF as <name>.bak.pdf
    (overwritten by the next deletion), the build's OCR files get their
    page markers renumbered, and title_pages is remapped, so everything
    stays aligned with the new page numbering."""
    p = request.get_json(silent=True) or {}
    build_id = str(p.get("build_id") or "")
    builds = lib.load_json(BUILDS_PATH, {})
    if build_id not in builds:
        abort(404)
    # a running OCR job reads page numbers that deletion would shift under
    # its feet — refuse until it finishes
    running = _page_job_blockers(build_id)
    if running:
        return jsonify({"ok": False,
                        "error": "a page-processing job is running for this book — "
                                 "wait for it to finish"})
    pdf = _resolve_local(str(p.get("pdf") or ""))
    if pdf is None or pdf.suffix.lower() != ".pdf" or not pdf.is_file():
        return jsonify({"ok": False, "error": "PDF not found"})
    # the entry-folder preview is a TRUNCATED derivative: deleting pages
    # there would desync the (full-length) OCR renumbering
    try:
        if pdf.resolve().is_relative_to(ENTRIES_DIR.resolve()):
            return jsonify({"ok": False,
                            "error": "this book only has the truncated preview "
                                     "derivative — re-attach the original scan "
                                     "before deleting pages"})
    except OSError:
        pass
    try:
        pages = sorted({int(n) for n in (p.get("pages") or []) if int(n) > 0})
    except (TypeError, ValueError):
        pages = []
    if not pages:
        return jsonify({"ok": False, "error": "no pages selected"})
    try:
        result = _apply_page_deletion(build_id, builds, pdf, pages)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    result["ok"] = True
    return jsonify(result)


def _apply_page_deletion(build_id: str, builds: dict, pdf: Path,
                         pages: list[int]) -> dict:
    """Guard the page structure while every page-keyed derivative is remapped."""
    with _page_structure_lock:
        if _page_job_blockers(build_id):
            raise ValueError("a page-processing job is running for this book")
        result = _apply_page_deletion_locked(build_id, builds, pdf, pages)
        _page_structure_revision[build_id] = (
            _page_structure_revision.get(build_id, 0) + 1)
        return result


def _apply_page_deletion_locked(build_id: str, builds: dict, pdf: Path,
                                pages: list[int]) -> dict:
    """Rewrite the PDF without the given pages (backup kept), renumber the
    build's OCR files, and remap title_pages. Shared by the deletion
    endpoint and blank-page trimming. Raises ValueError on refusal."""
    from pypdf import PdfReader, PdfWriter
    import shutil
    reader = PdfReader(str(pdf))
    total = len(reader.pages)
    keep = [i for i in range(total) if (i + 1) not in set(pages)]
    if not keep:
        raise ValueError("cannot delete every page")
    if len(keep) == total:
        raise ValueError("pages out of range")
    # safety net: the previous version stays recoverable
    shutil.copy2(pdf, pdf.with_suffix(".bak.pdf"))
    writer = PdfWriter()
    for i in keep:
        writer.add_page(reader.pages[i])
    tmp = pdf.with_suffix(".del.tmp")
    with open(tmp, "wb") as fh:
        writer.write(fh)
    tmp.replace(pdf)
    # keep the build's OCR files and title pages aligned with the new
    # numbering (under the merge lock: a job finishing this instant must
    # not interleave with the renumber writes). Only the files that came
    # FROM this PDF renumber — a secondary scan's OCR has its own page
    # numbering and must not shift with the primary's deletions.
    b = builds[build_id]
    src_key = _src_key_for_path(b, pdf)
    srcmap = _ocr_sources(build_id)
    ocr_dir = _entry_dir(build_id) / "ocr"
    renumbered = []
    with _ocr_merge_lock:
        if ocr_dir.is_dir():
            for f in ocr_dir.glob("*.txt"):
                if (srcmap.get(f.name) or "primary") != src_key:
                    continue
                try:
                    raw = f.read_text(encoding="utf-8", errors="replace")
                    # the renumbering is destructive too — a misfired trim
                    # must be recoverable for the text, not just the PDF
                    f.with_name(f.name + ".bak").write_text(
                        raw, encoding="utf-8", errors="replace")
                    out = _renumber_marked_text(raw, pages)
                    f.write_text(out, encoding="utf-8", errors="replace")
                    renumbered.append(f.name)
                except OSError:
                    continue
    # the OCR word-box sidecar is page-keyed per source like the compiled
    # files; keep THIS source's boxes aligned so the placed facsimile never
    # shows a deleted page's words.
    _renumber_layout_words(build_id, src_key, pages)
    # Translations use the same page-marker convention, and their provenance
    # sidecars key each source hash by page. Move both in one protected pass so
    # stale detection and eventual publication never retain an obsolete tail.
    moved = _renumber_translation_artifacts(build_id, src_key, pages)
    _manifest_after_renumber(
        build_id, [f"ocr/{n}" for n in renumbered] + ["ocr/layout.json"],
        moved)
    # title pages are counted on the PRIMARY PDF; a secondary's deletions
    # don't move them
    titles = [] if src_key != "primary" else \
        [int(x) for x in str(b.get("title_pages") or "").split(",")
         if x.strip().isdigit()]
    changed = {}
    if titles:
        remapped = []
        for t in titles:
            if t in set(pages):
                continue
            remapped.append(t - sum(1 for r in pages if r < t))
        changed["title_pages"] = ",".join(str(t) for t in remapped)
    # thumbnail_source references a primary-PDF page the same way title_pages
    # does ("page:<n>") — remap it the same way, or clear it if the referenced
    # page was itself deleted. An "image:<name>" source points at an OCR-
    # extracted figure, not a PDF page, so page deletion never touches it.
    if src_key == "primary":
        m = re.match(r"^page:(\d+)$", str(b.get("thumbnail_source") or ""))
        if m:
            t = int(m.group(1))
            changed["thumbnail_source"] = "" if t in set(pages) else \
                f"page:{t - sum(1 for r in pages if r < t)}"
    if changed:
        # the caller's snapshot predates the slow PDF rewrite above — apply
        # the remap to a fresh read, and keep the returned record in step
        b.update(changed)
        b["updated_at"] = _builds_apply(build_id, changed)
    return {"deleted": pages, "pages": len(keep),
            "renumbered": renumbered,
            "backup": pdf.with_suffix(".bak.pdf").name,
            "build": b}


def _blank_pages(pdf: Path, ink_threshold: float = 0.003) -> list[int]:
    """1-based numbers of visually blank pages. Conservative on purpose —
    a false positive deletes a real page: a page is blank only when BOTH
    (a) the fraction of even-faint ink pixels (gray < 200 at a small
    render) stays under the threshold, and (b) its text layer is empty.
    Faint scans and folio-numbered pages fail one of the two and stay."""
    import fitz
    blank = []
    doc = fitz.open(str(pdf))
    try:
        for i in range(doc.page_count):
            pg = doc[i]
            zoom = 160 / max(1.0, pg.rect.width)
            pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace="gray")
            samples = pix.samples
            inked = sum(1 for v in samples if v < 200)
            if inked / max(1, len(samples)) >= ink_threshold:
                continue
            # ANY text layer keeps the page — folio-only pages ("47") and
            # faint scans usually carry one; true blanks carry none
            if (pg.get_text() or "").strip():
                continue
            blank.append(i + 1)
    finally:
        doc.close()
    return blank


def first_content_page(pdf: Path, ink_threshold: float = 0.003,
                        max_scan: int = 20) -> int | None:
    """1-based number of the first page that ISN'T blank by _blank_pages'
    test — a cheap "cover candidate" heuristic (ink density + text-layer
    presence only; no vision model, that's future work). Deliberately its own
    small scan rather than calling _blank_pages() and diffing the result:
    that function always walks the whole document before returning, which
    defeats the point of an early exit here. Capped at max_scan pages so a
    pathological all-blank document doesn't scan forever; None if nothing
    qualifies within the cap."""
    import fitz
    doc = fitz.open(str(pdf))
    try:
        for i in range(min(doc.page_count, max_scan)):
            pg = doc[i]
            zoom = 160 / max(1.0, pg.rect.width)
            pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace="gray")
            samples = pix.samples
            inked = sum(1 for v in samples if v < 200)
            has_ink = inked / max(1, len(samples)) >= ink_threshold
            has_text = bool((pg.get_text() or "").strip())
            if has_ink or has_text:
                return i + 1
        return None
    finally:
        doc.close()


# --- master list -> Google Sheets sync ----------------------------------------------

@app.route("/api/master/sync", methods=["POST"])
def api_master_sync():
    """Publish the master list (plus manual entries) to a Google Sheet.
    Body: {spreadsheet_id, service_account_file, sheet_name?}. Requires a
    Google service-account JSON key — TODO: verify once the user has one."""
    p = request.get_json(silent=True) or {}
    sheet_id = str(p.get("spreadsheet_id") or "").strip()
    keyfile = (str(p.get("service_account_file") or "").strip()
               or str(_client_settings().get("gsKeyFile") or "").strip())
    sheet_name = str(p.get("sheet_name") or "Master list").strip()
    if not sheet_id or not keyfile:
        return jsonify({"ok": False,
                        "error": "Spreadsheet ID and service-account key file "
                                 "are required (Settings > Sync)"})
    kf = _resolve_local(keyfile)
    if kf is None or not kf.is_file():
        return jsonify({"ok": False, "error": f"key file not found: {keyfile}"})
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build as gbuild
    except ImportError:
        return jsonify({"ok": False,
                        "error": "Google API client not installed (python3 -m "
                                 "pip install google-api-python-client google-auth)"})
    header = ["Title", "Subtitle", "Author", "Year", "Volume", "Edition",
              "Publisher", "City", "Categories", "Notes", "Source"]
    rows = [header]
    for r in lib.load_json(lib.CH_LIBRARY_JSON_PATH, []):
        row = _ch_row(0, r)
        rows.append([row["title"], "", row["author"], row["year"], "",
                     row["edition"], row["publisher"], row["city"],
                     row["categories"], row["notes"], "master"])
    tax = lib.load_taxonomy()["nodes"]
    for e in lib.load_json(lib.MANUAL_ENTRIES_PATH, {}).values():
        # resolved taxonomy paths when assigned; the deprecated text otherwise
        paths = lib.category_paths(tax, e.get("category_ids"))
        cats = lib.categories_text(paths) if paths else e.get("categories", "")
        rows.append([e.get("title", ""), e.get("subtitle", ""),
                     e.get("author", ""), e.get("year", ""),
                     e.get("volume", ""), e.get("edition", ""),
                     e.get("publisher", ""), e.get("city", ""),
                     cats, e.get("notes", ""), "manual"])
    try:
        creds = service_account.Credentials.from_service_account_file(
            str(kf), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        svc = gbuild("sheets", "v4", credentials=creds)
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"{sheet_name}!A1",
            valueInputOption="RAW", body={"values": rows}).execute()
        return jsonify({"ok": True, "rows": len(rows) - 1})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


_PDF_TEXT_CACHE: dict = {}


@app.route("/api/pdf/text")
def api_pdf_text():
    """Extract the text (OCR) layer of a PDF — a local path, or a remote URL
    that is fetched once into downloads/cache/.

    ?save_build=<id> also writes the extraction into that build's entry
    folder as ocr/extracted.txt when it doesn't exist yet — extracted OCR
    is saved automatically the first time a book's PDF is read."""
    raw_path = (request.args.get("path") or "").strip()
    url = (request.args.get("url") or "").strip()
    # pages<=0 means every page (text extraction is cheap); a positive value caps
    # it (default 100 for a quick preview). The old min(…, 500) was the legacy cap.
    try:
        n = int(request.args.get("pages") or 100)
    except ValueError:
        n = 100
    max_pages = None if n <= 0 else min(2000, n)
    if raw_path:
        p = _resolve_local(raw_path)
        if p is None or not p.is_file():
            abort(404)
    elif url:
        try:
            p = _remote_pdf_cache(url)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)})
    else:
        abort(400)
    key = (str(p), p.stat().st_mtime, max_pages)
    out = _PDF_TEXT_CACHE.get(key)
    if out is None:
        try:
            import fitz  # noqa: F401
        except ImportError:
            return jsonify({"ok": False,
                            "error": "PyMuPDF is not installed "
                                     "(python3 -m pip install PyMuPDF)"})
        try:
            total, shown, text, with_text = _pdf_extract_text(p, max_pages)
            out = {"ok": True, "pages": total, "shown": shown, "text": text,
                   "pages_with_text": with_text}
        except Exception as exc:
            out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        _PDF_TEXT_CACHE[key] = out
    # Auto-save into the entry folder (never clobbers an existing file). One
    # page of text is a scanner's cover sheet, not an extraction: don't save it.
    # ?save_name= picks the file (default extracted.txt) and ?src= ties it to
    # a secondary PDF source — extractions of a secondary scan live beside
    # the primary's under their own name.
    bid = (request.args.get("save_build") or "").strip()
    if bid and out.get("ok") and out.get("pages_with_text", 0) > 1:
        builds = lib.load_json(BUILDS_PATH, {})
        if bid in builds:
            name = _ocr_name(request.args.get("save_name") or "extracted.txt")
            f = _entry_dir(bid) / "ocr" / name
            if not f.is_file():
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(out["text"], encoding="utf-8", errors="replace")
                src_key = _valid_src_key(builds[bid], request.args.get("src"))
                if src_key:
                    _ocr_set_source(bid, name, src_key)
                out = dict(out, saved=name)
    return jsonify(out)


def _downloads_dir() -> Path:
    dl = Path.home() / "Downloads"
    return dl if dl.is_dir() else Path.home()


@app.route("/api/pdf/browse")
def api_pdf_browse():
    """List a directory's subdirectories and PDF files (the file picker).
    ?preset=downloads (with no dir) opens the user's Downloads folder; each PDF
    carries its mtime + server 'now' so the client can filter to recently
    downloaded scans."""
    raw = (request.args.get("dir") or "").strip()
    if raw:
        d = _resolve_local(raw)
    elif request.args.get("preset") == "downloads":
        d = _downloads_dir()
    else:
        d = lib.IA_DOWNLOADS_DIR
    if d is None or not d.is_dir():
        d = lib.DATA_ROOT
    dirs: list[dict] = []
    pdfs: list[dict] = []
    try:
        for entry in sorted(d.iterdir(), key=lambda p: p.name.lower()):
            try:
                if entry.is_dir():
                    if not entry.name.startswith("."):
                        dirs.append({"name": entry.name, "path": str(entry)})
                elif entry.suffix.lower() == ".pdf":
                    st = entry.stat()
                    pdfs.append({"name": entry.name, "path": str(entry),
                                 "size": st.st_size, "mtime": st.st_mtime})
            except OSError:
                continue
    except OSError:
        pass
    parent = str(d.parent) if d.parent != d else None
    return jsonify({"dir": str(d), "parent": parent, "dirs": dirs,
                    "pdfs": pdfs, "drives": _drives(),
                    "now": datetime.now(timezone.utc).timestamp()})


_DRIVES_CACHE: list[str] | None = None


def _drives() -> list[str]:
    """Available drive roots; probed once (floppy-era letters are slow)."""
    global _DRIVES_CACHE
    if _DRIVES_CACHE is None:
        _DRIVES_CACHE = [f"{c}:\\" for c in "CDEFGHIJKLMNOPQRSTUVWXYZ"
                         if Path(f"{c}:\\").exists()]
    return _DRIVES_CACHE


# --- manual entries (checked offline on submit) ------------------------------

def _entry_checks(entry: dict) -> dict:
    """Copyright + local-WHL checks; a check failure must not block the save."""
    try:
        return checks.check_entry(
            entry.get("title", ""), entry.get("author", ""), entry.get("year", "")
        )
    except Exception as exc:  # unexpected CSV/parse trouble
        return {"error": f"{type(exc).__name__}: {exc}"}


# --- client/session state (lifted out of browser localStorage) -------------------
# checked books, UI settings, and attention marks used to live only in the
# browser (keyed to the http://127.0.0.1:5001 origin, so a port change would
# orphan them and they never synced). They now round-trip through the server
# doc store, making them port-independent and ready to sync to the cloud.

_CLIENT_STATE_KEYS = ("checked", "settings", "attention")
_client_state_lock = threading.Lock()

# manual_entries.json is rewritten whole by every mutation; now that the cloud
# capture importer writes it from a background thread, every read-modify-write
# must hold this lock or a concurrent save silently drops the other's entry.
_manual_lock = threading.Lock()
_CS_BACKUP_KEEP = 40


def _backup_client_state(state, old_n, new_n):
    """Snapshot the current client_state before a write that shrinks the checked
    list, so a bad sync (e.g. a near-empty client clobbering a full set) is
    always instantly reversible. Best-effort: never let a backup failure break
    the write."""
    try:
        bdir = lib.OUTPUT_DIR / "backups"
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
        lib.save_json(bdir / f"client_state.autobak.{ts}.{old_n}to{new_n}.json", state)
        baks = sorted(bdir.glob("client_state.autobak.*.json"))
        for p in baks[:-_CS_BACKUP_KEEP]:
            try:
                p.unlink()
            except OSError:
                pass
    except Exception:
        pass


@app.route("/api/client_state")
def api_client_state_get():
    return jsonify(lib.load_json(lib.CLIENT_STATE_PATH, {}))


@app.route("/api/client_state", methods=["PUT"])
def api_client_state_put():
    payload = request.get_json(silent=True) or {}
    with _client_state_lock:
        state = lib.load_json(lib.CLIENT_STATE_PATH, {})
        # Safety net: if this write would REDUCE the checked count, back up the
        # current file first. Clients adopt-by-merge on load, so a legitimate
        # shrink is a real uncheck; but this makes even that reversible and
        # catches any client that tries to overwrite a fuller set with less.
        # Guarded with isinstance so a malformed (non-list) payload can never
        # raise here — a bad request degrades, it does not 500.
        new_checked = payload.get("checked")
        if isinstance(new_checked, list):
            old = state.get("checked")
            old_n = len(old) if isinstance(old, list) else 0
            if len(new_checked) < old_n:
                _backup_client_state(state, old_n, len(new_checked))
        old_checked = state.get("checked")
        for k in _CLIENT_STATE_KEYS:
            if k in payload:
                state[k] = payload[k]
        # secrets never persist in the synced client_state (they live in the
        # local secrets store); strip them defensively even if a client sends them
        if isinstance(state.get("settings"), dict):
            for _sk in _SECRET_KEYS:
                state["settings"].pop(_sk, None)
        state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lib.save_json(lib.CLIENT_STATE_PATH, state)
        if "settings" in payload:
            _apply_log_level()   # verbose-logging toggle takes effect immediately
            _apply_lan_state()   # a LAN toggle takes effect without an app restart
    # the checked set is a blob, not a stream of adds -- diff it to get events.
    # Adds and removals are logged separately from the key-set differences, so
    # each event's count always agrees with the titles it names (a PUT that
    # both adds and removes books yields two events, not one net delta).
    if isinstance(new_checked, list) and new_checked is not None:
        n_add, add_titles = _checked_diff(old_checked, new_checked)
        n_rm, rm_titles = _checked_diff(new_checked, old_checked)
        if n_add:
            activity("added", "Checked Books", n_add, detail=add_titles)
        if n_rm:
            activity("removed", "Checked Books", n_rm, detail=rm_titles)
    return jsonify({"ok": True})


def _checked_diff(old, new, cap: int = 3):
    """(count, titles) of books in `new` but not `old` ([key, value] pair
    lists). Purely a nicety for the feed: malformed shapes yield (0, "")."""
    try:
        def as_map(lst):
            return {p[0]: p[1] for p in (lst or [])
                    if isinstance(p, list) and len(p) == 2}
        old_map, new_map = as_map(old), as_map(new)
        added = [k for k in new_map if k not in old_map]
        titles = []
        for k in added:
            v = new_map[k]
            b = v.get("book") if isinstance(v, dict) else None
            t = str(b.get("title") or "").strip() if isinstance(b, dict) else ""
            if t:
                titles.append(t)
        extra = len(titles) - cap
        return len(added), ("; ".join(titles[:cap]) +
                            (f" (+{extra} more)" if extra > 0 else ""))
    except Exception:
        return 0, ""


@app.route("/api/manual")
def api_manual_list():
    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
    out = sorted(entries.values(), key=lambda e: e.get("created_at", ""), reverse=True)
    return jsonify(out)


def _clean_extra(v) -> dict:
    """Arbitrary non-column bibliographic facts, kept as JSON-compatible data.

    Phone extractors are allowed to learn new fields without a desktop release.
    Preserve their complete values here; the Info tab renders this structure,
    while the book table continues to use its explicit column list.
    """
    if not isinstance(v, dict):
        return {}

    def clean(x):
        if isinstance(x, dict):
            return {str(k).strip(): cleaned for k, value in x.items()
                    if str(k).strip() and (cleaned := clean(value)) is not None}
        if isinstance(x, list):
            return [cleaned for value in x
                    if (cleaned := clean(value)) is not None]
        if x is None:
            return None
        if isinstance(x, str):
            value = x.strip()
            return value if value else None
        if isinstance(x, (bool, int, float)):
            return x
        value = str(x).strip()
        return value if value else None

    return {str(k).strip(): cleaned for k, value in v.items()
            if str(k).strip() and (cleaned := clean(value)) is not None}


def _clean_images(v) -> list[str]:
    """Entry image paths: normalized DATA_ROOT-relative image paths only.

    Captured photos are renderer-visible through ``/api/capture/image``. Keep
    absolute paths, traversal segments, URL-like drive prefixes, duplicates,
    and unbounded client payloads out of the persisted build record.
    """
    if not isinstance(v, list):
        return []
    out = []
    for p in v[:200]:
        s = str(p or "").replace("\\", "/").strip()
        parts = s.split("/")
        if (not s or len(s) > 500 or s.startswith("/")
                or not all(part and part not in (".", "..") for part in parts)
                or ":" in parts[0]):
            continue
        if (s.lower().rsplit(".", 1)[-1] in ("jpg", "jpeg", "png", "webp")
                and s not in out):
            out.append(s)
    return out


def _clean_capture_id(v) -> str:
    """The phone capture identifier used for provenance, never as a path."""
    return re.sub(r"[^A-Za-z0-9-]", "", str(v or ""))[:64]


@app.route("/api/manual", methods=["POST"])
def api_manual_add():
    payload = request.get_json(silent=True) or {}
    entry = {f: str(payload.get(f, "") or "").strip() for f in lib.MANUAL_ENTRY_FIELDS}
    if not entry["title"]:
        return jsonify({"ok": False, "error": "TITLE IS REQUIRED"}), 400
    if payload.get("extra"):
        entry["extra"] = _clean_extra(payload.get("extra"))
    if payload.get("images"):
        entry["images"] = _clean_images(payload.get("images"))
    if payload.get("category_ids"):
        entry["category_ids"] = _clean_category_ids(
            payload.get("category_ids"), lib.load_taxonomy()["nodes"])

    entry["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry["checks"] = _entry_checks(entry)
    with _manual_lock:
        entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
        entry["id"] = lib.gen_id(set(entries))
        entries[entry["id"]] = entry
        lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)
    activity("added", "manual entry", detail=entry.get("title", ""))
    return jsonify({"ok": True, "entry": entry})


@app.route("/api/manual/<entry_id>", methods=["PATCH"])
def api_manual_update(entry_id: str):
    """Update fields of a manual entry; metadata changed, so re-run the
    offline checks and drop the stale scan results (the client re-scans).

    "_preserve": true keeps checks/scans/verifications — used for changes
    that don't alter the book's identity (title parsing migration, attaching
    a local scan PDF)."""
    payload = request.get_json(silent=True) or {}
    with _manual_lock:
        entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
        if entry_id not in entries:
            abort(404)
        e = entries[entry_id]
        for f in lib.MANUAL_ENTRY_FIELDS:
            if f in payload:
                e[f] = str(payload[f] or "").strip()
        # non-column metadata: only replaced when explicitly sent (survives edits)
        if "extra" in payload:
            e["extra"] = _clean_extra(payload.get("extra"))
        if "images" in payload:
            e["images"] = _clean_images(payload.get("images"))
        if "category_ids" in payload:
            e["category_ids"] = _clean_category_ids(
                payload.get("category_ids"), lib.load_taxonomy()["nodes"])
        if not e.get("title"):
            return jsonify({"ok": False, "error": "TITLE IS REQUIRED"}), 400
        if payload.get("_edited"):
            e["edited"] = True
        if not payload.get("_preserve"):
            e["checks"] = _entry_checks(e)
            # Metadata changed: stored matches and their verifications are stale.
            e.pop("scans", None)
            e.pop("verify", None)
            e.pop("manual_urls", None)
        lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)
    return jsonify({"ok": True, "entry": e})


@app.route("/api/manual/restore", methods=["POST"])
def api_manual_restore():
    """Reinsert a previously deleted entry verbatim (undo of a delete).

    The client sends back the full entry object it received from this server
    before the deletion, so checks/scans/verifications survive the round trip.
    """
    payload = request.get_json(silent=True) or {}
    entry = payload.get("entry") or {}
    eid = str(entry.get("id") or "")
    if not eid or not str(entry.get("title", "") or "").strip():
        abort(400)
    with _manual_lock:
        entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
        entries[eid] = entry
        lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)
    return jsonify({"ok": True, "entry": entry})


@app.route("/api/manual/<entry_id>", methods=["DELETE"])
def api_manual_delete(entry_id: str):
    with _manual_lock:
        entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
        if entry_id not in entries:
            abort(404)
        title = entries[entry_id].get("title", "")
        del entries[entry_id]
        lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)
    activity("deleted", "manual entry", detail=title)
    return jsonify({"ok": True})


@app.route("/api/manual/<entry_id>/scans", methods=["POST"])
def api_manual_scans(entry_id: str):
    """Run the IA + HathiTrust scan search and persist it on the entry."""
    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
    if entry_id not in entries:
        abort(404)
    e = entries[entry_id]
    scans = scan_search.search_scans(
        e.get("title", ""), e.get("author") or None, e.get("year") or None
    )
    # The scan search is slow (network): the entry may have been edited in
    # the meantime. Re-read and merge only the scans, so this request can't
    # resurrect a stale snapshot of the other fields.
    with _manual_lock:
        entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
        if entry_id not in entries:
            abort(404)
        e = entries[entry_id]
        e["scans"] = scans
        lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)
    return jsonify({"ok": True, "entry": e})


@app.route("/api/manual/<entry_id>/verify", methods=["POST"])
def api_manual_verify(entry_id: str):
    """Record the per-source verification of a matched record.

    Body: {"source": "whl"|"internet_archive"|"hathitrust",
           "state": "approved"|"rejected"|"pending"}.
    'rejected' marks the match as a false positive; 'pending' clears the
    verification.
    """
    payload = request.get_json(silent=True) or {}
    source = str(payload.get("source", "") or "")
    verdict = str(payload.get("state", "") or "")
    if source not in ("whl", "internet_archive", "hathitrust") or \
            verdict not in ("approved", "rejected", "pending"):
        abort(400)
    with _manual_lock:
        entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
        if entry_id not in entries:
            abort(404)
        e = entries[entry_id]
        verify = e.setdefault("verify", {})
        if verdict == "pending":
            verify.pop(source, None)
        else:
            verify[source] = verdict
        if verdict != "rejected":
            # A manually located source only exists alongside a rejected match.
            (e.get("manual_urls") or {}).pop(source, None)
        lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)
    return jsonify({"ok": True, "entry": e})


@app.route("/api/manual/<entry_id>/source", methods=["POST"])
def api_manual_source(entry_id: str):
    """Store the URL of a manually located source for a rejected match.

    Body: {"source": "whl"|"internet_archive"|"hathitrust", "url": "..."};
    an empty url clears it.
    """
    payload = request.get_json(silent=True) or {}
    source = str(payload.get("source", "") or "")
    url = str(payload.get("url", "") or "").strip()
    if source not in ("whl", "internet_archive", "hathitrust"):
        abort(400)
    with _manual_lock:
        entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
        if entry_id not in entries:
            abort(404)
        e = entries[entry_id]
        urls = e.setdefault("manual_urls", {})
        if url:
            urls[source] = url
        else:
            urls.pop(source, None)
        lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)
    return jsonify({"ok": True, "entry": e})


# --- Internet Archive PDF downloads --------------------------------------------

_downloads: dict[str, dict] = {}
_downloads_lock = threading.Lock()

# The IA download catalog is a single shared JSON file written by concurrent
# download threads + the preview/folder-build endpoints; serialize every
# read-modify-write so a save can't drop another writer's entry or be read mid-write.
_ia_catalog_lock = threading.Lock()


def _read_ia_catalog() -> dict:
    with _ia_catalog_lock:
        return lib.load_json(lib.IA_CATALOG_PATH, {})


def _update_ia_catalog(mutate) -> None:
    with _ia_catalog_lock:
        catalog = lib.load_json(lib.IA_CATALOG_PATH, {})
        mutate(catalog)
        lib.save_json(lib.IA_CATALOG_PATH, catalog)


def _ia_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": scan_search.USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pick_pdf(files: list) -> dict | None:
    """Choose the item's PDF derivative ('Text PDF' preferred)."""
    best = None
    for f in files:
        name = str(f.get("name", "") or "")
        if not name.lower().endswith(".pdf"):
            continue
        if (f.get("format") or "").lower() == "text pdf":
            return f
        if best is None:
            best = f
    return best


def _ia_pdf_path(identifier: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", identifier)
    return lib.IA_DOWNLOADS_DIR / f"{safe}.pdf"


def _ia_download_job(identifier: str, book: dict) -> None:
    """Download the item's PDF and write a cataloging entry (runs in a thread)."""
    job = _downloads[identifier]
    log.info("IA download started: %s", identifier)
    try:
        info = _ia_get_json(f"https://archive.org/metadata/{urllib.parse.quote(identifier)}")
        pdf = _pick_pdf(info.get("files") or [])
        if not pdf:
            raise RuntimeError("no PDF derivative on this item")
        name = pdf["name"]
        url = (
            "https://archive.org/download/"
            + urllib.parse.quote(identifier) + "/" + urllib.parse.quote(name)
        )
        lib.IA_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        dest = _ia_pdf_path(identifier)
        tmp = dest.with_suffix(".part")
        req = urllib.request.Request(url, headers={"User-Agent": scan_search.USER_AGENT})
        got = 0
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
            job["total"] = int(resp.headers.get("Content-Length") or 0)
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                job["bytes"] = got
        tmp.replace(dest)

        # A compressed, first-10-pages preview copy drives the fast page viewer
        # (the full PDF stays on disk; the preview is what the client renders).
        preview_rel = ""
        try:
            preview_rel = str(_preview_pdf(dest, 10).relative_to(lib.DATA_ROOT))
            job["preview"] = preview_rel
        except Exception:
            pass

        # Cataloging entry: our book metadata + where the scan came from.
        meta = info.get("metadata") or {}
        entry = {
            "identifier": identifier,
            "source_url": f"https://archive.org/details/{identifier}",
            "pdf_file": name,
            "saved_as": str(dest.relative_to(lib.DATA_ROOT)),
            "preview": preview_rel,
            "size_bytes": got,
            "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ia_title": meta.get("title", ""),
            "ia_creator": meta.get("creator", ""),
            "ia_date": meta.get("date", ""),
            "book": book,
        }
        _update_ia_catalog(lambda c: c.__setitem__(identifier, entry))
        job["status"] = "done"
        job["path"] = str(dest.relative_to(lib.DATA_ROOT))
        log.info("IA download done: %s", identifier)
    except Exception as exc:
        job["status"] = "error"
        job["error"] = f"{type(exc).__name__}: {exc}"
        log.error("IA download failed: %s", identifier, exc_info=exc)


def _download_state(identifier: str) -> dict:
    job = _downloads.get(identifier)
    if job:
        return {"identifier": identifier, **{k: v for k, v in job.items() if k != "thread"}}
    catalog = _read_ia_catalog()
    if identifier in catalog and _ia_pdf_path(identifier).exists():
        return {"identifier": identifier, "status": "done",
                "path": catalog[identifier].get("saved_as", ""),
                "preview": catalog[identifier].get("preview", "")}
    return {"identifier": identifier, "status": "none"}


@app.route("/api/ia/preview/<path:identifier>")
def api_ia_preview(identifier: str):
    """Ensure a compressed first-10-pages preview exists for a downloaded IA PDF
    and return its DATA_ROOT-relative path + page count (generated on demand so
    downloads from earlier builds get a preview too)."""
    pdf = _ia_pdf_path(identifier)
    if not pdf.is_file():
        return jsonify({"ok": False, "error": "not downloaded"}), 404
    try:
        prev = _preview_pdf(pdf, 10)
        rel = str(prev.relative_to(lib.DATA_ROOT))
        from pypdf import PdfReader
        pages = len(PdfReader(str(prev)).pages)
        catalog = _read_ia_catalog()
        if identifier in catalog and catalog[identifier].get("preview") != rel:
            def _set_preview(c):
                if identifier in c:
                    c[identifier]["preview"] = rel
            _update_ia_catalog(_set_preview)
        return jsonify({"ok": True, "preview": rel, "pages": pages})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


@app.route("/api/ia/download", methods=["POST"])
def api_ia_download():
    payload = request.get_json(silent=True) or {}
    identifier = str(payload.get("identifier", "") or "").strip()
    if not identifier:
        abort(400)
    book = payload.get("book") or {}
    with _downloads_lock:
        current = _download_state(identifier)
        if current["status"] in ("downloading", "done"):
            return jsonify(current)
        _downloads[identifier] = {"status": "downloading", "bytes": 0, "total": 0}
        threading.Thread(
            target=_ia_download_job, args=(identifier, book), daemon=True
        ).start()
    return jsonify(_download_state(identifier))


@app.route("/api/ia/download/<path:identifier>")
def api_ia_download_status(identifier: str):
    return jsonify(_download_state(identifier))


@app.route("/api/ia/downloads")
def api_ia_downloads():
    return jsonify(_read_ia_catalog())


# --- Open Library indexes (constrained search + realtime + autocomplete) --------

# Cloud config lives in the client settings blob (synced via /api/client_state),
# so a per-user remote URL and DB source URLs need no separate config file.
# --- credentials: a LOCAL-ONLY secrets store, kept out of the synced (and
# rebinding-reachable) client_state. The dialog reads/writes it through
# /api/secrets (Host-guarded); every server-side credential read goes through
# _client_settings, which overlays these on top of the synced preferences. ------
_SECRET_KEYS = frozenset({
    "aiKey", "embedKey", "mistralKey", "ocrClaudeKey", "ocrAzureKey",
    "ocrAwsKey", "ocrAwsSecret", "supabaseKey", "supabaseAnonKey", "r2KeyId",
    "r2Secret", "gsKeyFile",
})
_SECRETS_PATH = lib.DATA_ROOT / "output" / "secrets.json"
_MISTRAL_PENDING = "_mistralCloudPending"
# The one lock for every secrets.json read-modify-write (settings dialog,
# profile reconciliation, the client_state migration). Single process.
_secrets_lock = threading.Lock()


def _load_secrets() -> dict:
    d = lib.load_json(_SECRETS_PATH, {})
    return d if isinstance(d, dict) else {}


def _save_secrets(d: dict) -> None:
    lib.save_json(_SECRETS_PATH, d)


def _client_settings():
    s = dict((lib.load_json(lib.CLIENT_STATE_PATH, {}) or {}).get("settings") or {})
    for k, v in _load_secrets().items():
        if v:
            s[k] = v                     # secrets override; they never persist here
    return s


def _local_only() -> bool:
    """Block DNS-rebinding: only requests whose Host is the loopback origin the
    app itself is served from may touch the secrets store."""
    host = (request.host or "").split(":")[0].lower()
    return host in ("127.0.0.1", "localhost")


@app.route("/api/secrets", methods=["GET"])
def api_secrets_get():
    if not _local_only():
        return jsonify({"error": "forbidden"}), 403
    # Mistral belongs to the signed-in user's private cloud data. Refresh its
    # local cache when the settings dialog opens; offline falls back cleanly.
    _sync_profile_mistral_key()
    secrets = _load_secrets()
    return jsonify({k: secrets.get(k, "") for k in _SECRET_KEYS})


@app.route("/api/secrets", methods=["PUT"])
def api_secrets_put():
    if not _local_only():
        return jsonify({"error": "forbidden"}), 403
    updates = (request.get_json(silent=True) or {}).get("updates") or {}
    with _secrets_lock:
        secrets = _load_secrets()
        for k, v in updates.items():
            if k not in _SECRET_KEYS:
                continue
            v = str(v or "").strip()
            if v:
                secrets[k] = v
            else:
                secrets.pop(k, None)
            if k == "mistralKey":
                secrets[_MISTRAL_PENDING] = True
        _save_secrets(secrets)
    if "mistralKey" in updates:
        _sync_profile_mistral_key()
    return jsonify({"ok": True})


def _sync_profile_mistral_key() -> str | None:
    """Reconcile the local Mistral cache with this user's profile_secrets row.

    A locally edited value is marked pending until its cloud upsert succeeds;
    otherwise the cloud value wins so Android edits reach the desktop. Returns
    the reconciled key, or None when signed out/offline.
    """
    cfg = _auth_cfg()
    ses = _auth_session() if cfg else None
    if not cfg or not ses:
        return None
    secrets = _load_secrets()
    local = str(secrets.get("mistralKey") or "").strip()
    pending = bool(secrets.get(_MISTRAL_PENDING))
    try:
        rows = sauth.rest(
            cfg, ses["access_token"], "GET",
            f"profile_secrets?id=eq.{ses['user_id']}&select=api_keys",
        ) or []
        keys = dict(rows[0].get("api_keys") or {}) if rows else {}
        if pending or "mistral" not in keys:
            keys["mistral"] = local
            sauth.rest(
                cfg, ses["access_token"], "POST",
                "profile_secrets?on_conflict=id",
                [{"id": ses["user_id"], "api_keys": keys}],
                prefer="resolution=merge-duplicates,return=minimal",
            )
            adopt = None                   # local value pushed; only the flag clears
        else:
            local = adopt = str(keys.get("mistral") or "").strip()
        # the REST round-trips above took time: apply the outcome to a fresh
        # read under the lock, not to the pre-network snapshot
        with _secrets_lock:
            secrets = _load_secrets()
            if adopt is None:
                secrets.pop(_MISTRAL_PENDING, None)
            elif adopt:
                secrets["mistralKey"] = adopt
            else:
                secrets.pop("mistralKey", None)
            _save_secrets(secrets)
        return local
    except sauth.AuthError as exc:
        log.warning("Mistral profile sync deferred: %s", exc)
        return None


def _migrate_secrets_from_client_state() -> None:
    """One-time lift of secret keys out of the synced client_state settings into
    the local-only secrets store, so they stop riding in a rebinding-reachable,
    cloud-synced blob. Idempotent."""
    with _client_state_lock:
        state = lib.load_json(lib.CLIENT_STATE_PATH, {})
        s = state.get("settings")
        if not isinstance(s, dict):
            return
        moved, removed = {}, False
        for k in list(s):
            if k in _SECRET_KEYS:
                v = s.pop(k)
                removed = True
                if v:
                    moved[k] = v
        if moved:
            with _secrets_lock:
                secrets = _load_secrets()
                for k, v in moved.items():
                    secrets.setdefault(k, v)   # never clobber a secrets.json value
                _save_secrets(secrets)
        if removed:
            lib.save_json(lib.CLIENT_STATE_PATH, state)


def _cloud_base():
    url = str(_client_settings().get("cloudSearchUrl") or "").strip().rstrip("/")
    return url or None


def _proxy_ol(kind):
    """Forward the current OL query to the configured cloud instance (a remote
    deployment of this same app). Returns parsed JSON, or None when there is no
    cloud URL or the request fails, so callers fall back to a local result."""
    base = _cloud_base()
    if not base:
        return None
    try:
        qs = urllib.parse.urlencode(list(request.args.items(multi=True)))
        req = urllib.request.Request(f"{base}/api/ol/{kind}?{qs}",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        if isinstance(data, dict):
            data.setdefault("source", "cloud")
        return data
    except Exception:
        return None


@app.route("/api/ol/status")
def api_ol_status():
    st = ol_client.db_stats()
    st["editions"] = ol_client.editions_index_stats()
    st["local"] = ol_client.editions_index_available()
    st["cloud"] = bool(_cloud_base())
    st["mode"] = "local" if st["local"] else ("cloud" if st["cloud"] else "none")
    return jsonify(st)


def _ol_params():
    p = request.args
    try:
        limit = min(int(p.get("limit", 12) or 12), 100)
    except ValueError:
        limit = 12
    return {
        "title": (p.get("title") or "").strip(),
        "author": (p.get("author") or "").strip(),
        "year": (p.get("year") or "").strip(),
        "edition": (p.get("edition") or "").strip(),
        "volume": (p.get("volume") or "").strip(),
        "publisher": (p.get("publisher") or "").strip(),
        "city": (p.get("city") or "").strip(),
        "limit": limit,
    }


@app.route("/api/ol/search")
def api_ol_search():
    params = _ol_params()
    # Local-first: the consolidated editions index answers everything locally.
    if ol_client.editions_index_available():
        return jsonify(ol_client.search_editions(**params))
    # No local index -> the configured cloud instance, then the local works
    # index / live API as a last resort.
    remote = _proxy_ol("search")
    if remote is not None:
        return jsonify(remote)
    return jsonify(ol_client.search_works(
        **params, deep=(request.args.get("deep") or "") in ("1", "true")))


@app.route("/api/ol/realtime")
def api_ol_realtime():
    """Search-as-you-type endpoint for the bottom-pane Open Library table."""
    params = _ol_params()
    if ol_client.editions_index_available():
        verbatim = (request.args.get("title_verbatim") or "") in ("1", "true")
        return jsonify(ol_client.search_editions(**params, title_verbatim=verbatim))
    remote = _proxy_ol("realtime")
    if remote is not None:
        return jsonify(remote)
    out = ol_client.search_works(
        title=params["title"], author=params["author"], year=params["year"],
        edition=params["edition"], volume=params["volume"],
        publisher=params["publisher"], city=params["city"],
        limit=params["limit"], deep=False)
    out["kind"] = "work"
    return jsonify(out)


# --- downloadable databases (offline local search) ------------------------------
# The cloud DBs are downloaded/synced into the writable data root from URLs the
# user configures in Settings. Once present, search resolves locally (offline).

_DB_TARGETS = {
    # name -> (path relative to DATA_ROOT, human label)
    "ol_search": ("output/ol_search.db", "Open Library search index"),
    "ol_works": ("output/ol_works.db", "Open Library works index"),
    "copyright_renewals": ("copyright_renewals.csv", "Copyright renewals"),
    "whl_catalog": ("whl_catalog.csv", "WHL catalog"),
}
_db_jobs = {}          # name -> {status, downloaded, total, error}
_db_lock = threading.Lock()


def _db_local(rel):
    """Where a database actually is, or None if nowhere. LOCAL-FIRST via
    lib.find_db: the ~/.library-tool drop-in folder, the data root, then the copy
    bundled with the app. None only when no copy exists — the sole case a
    download is offered."""
    p = lib.find_db(rel.split("/")[-1], rel)
    return p if p.exists() else None


def _db_urls():
    """Effective download URL per database: a Settings override wins, else the
    baked default (cloud_defaults.DB_URLS[name], or DB_BASE_URL/<file>). Empty
    when neither is set — the database is then local-drop-in only."""
    out = {}
    base = str(getattr(cloud_defaults, "DB_BASE_URL", "") or "").strip().rstrip("/")
    defaults = getattr(cloud_defaults, "DB_URLS", {}) or {}
    for name, (rel, _label) in _DB_TARGETS.items():
        u = str(defaults.get(name) or "").strip()
        if not u and base:
            u = f"{base}/{rel.split('/')[-1]}"
        if u:
            out[name] = u
    s = _client_settings().get("dbUrls")
    if isinstance(s, dict):
        for k, v in s.items():
            if str(v or "").strip():
                out[k] = str(v).strip()
    return out


def _run_db_download(name, url, rel):
    dest = lib.DB_DIR / rel.split("/")[-1]   # downloads land in the drop-in folder
    tmp = dest.with_name(dest.name + ".part")
    log.info("database download started: %s <- %s", name, url)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "whl-explorer"})
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            _db_jobs[name].update(total=total)
            done = 0
            with open(tmp, "wb") as fh:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    _db_jobs[name]["downloaded"] = done
        os.replace(tmp, dest)
        _db_jobs[name] = {"status": "done", "downloaded": done, "total": total}
        log.info("database download done: %s (%d bytes)", name, done)
    except Exception as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        _db_jobs[name] = {"status": "error", "error": str(e)}
        log.error("database download failed: %s", name, exc_info=e)


@app.route("/api/ocr/tesseract")
def api_tesseract_check():
    """Is local (Tesseract) OCR available? The first-run wizard and the OCR
    settings surface this. Checks the configured path, the Windows default, and
    PATH; reports the version when found."""
    bridge_error = _tesseract_bridge_error()
    if bridge_error:
        return jsonify({"ok": True, "installed": False, "path": "", "version": "",
                        "error": bridge_error})
    return _tesseract_status()


def _tesseract_bridge_error() -> str:
    """Return why the Python bridge cannot load, or an empty string."""
    try:
        import pytesseract  # noqa: F401 -- availability is what this probes
    except ImportError as exc:
        return f"Python OCR bridge unavailable: {exc}"
    return ""


def _tesseract_status():
    """Continue the executable probe after the bridge has loaded."""
    import shutil
    import subprocess
    cfg_path = str(_client_settings().get("ocrTesseract") or "").strip()
    candidates = [c for c in (cfg_path, _TESSERACT_DEFAULT) if c]
    found = next((c for c in candidates if Path(c).is_file()), None) \
        or shutil.which("tesseract")
    if not found:
        return jsonify({"ok": True, "installed": False, "path": "", "version": ""})
    version = ""
    try:
        out = subprocess.run([found, "--version"], capture_output=True, text=True,
                             timeout=8)
        version = (out.stdout or out.stderr or "").splitlines()[0].strip()
    except Exception as exc:                 # found the file but couldn't run it
        log.warning("tesseract --version failed: %s", exc)
    return jsonify({"ok": True, "installed": True, "path": found, "version": version})


@app.route("/api/db/status")
def api_db_status():
    urls = _db_urls()
    out = {}
    for name, (rel, label) in _DB_TARGETS.items():
        p = _db_local(rel)
        out[name] = {
            "label": label, "path": rel,
            "filename": rel.split("/")[-1],
            "present": p is not None,
            "size": p.stat().st_size if p else 0,
            "url": str(urls.get(name) or ""),
            "job": _db_jobs.get(name),
        }
    return jsonify({"data_root": str(lib.DATA_ROOT), "db_dir": str(lib.DB_DIR),
                    "targets": out})


@app.route("/api/db/reveal", methods=["POST"])
def api_db_reveal():
    """Open the writable data folder in the OS file manager so a user can drop
    database files straight in — local-first means a file here is used with no
    download and no URL."""
    import subprocess
    target = lib.DB_DIR
    try:
        target.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(target))            # noqa: S606 - a local desktop app
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return jsonify({"ok": True, "path": str(target)})
    except Exception as exc:                      # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc), "path": str(target)}), 500


@app.route("/api/db/download", methods=["POST"])
def api_db_download():
    names = (request.get_json(silent=True) or {}).get("names")
    urls = _db_urls()
    started, skipped = [], []
    for name in (names or list(_DB_TARGETS)):
        if name not in _DB_TARGETS:
            continue
        url = str(urls.get(name) or "").strip()
        if not url:
            skipped.append(name)
            continue
        with _db_lock:
            if (_db_jobs.get(name) or {}).get("status") == "downloading":
                continue
            _db_jobs[name] = {"status": "downloading", "downloaded": 0, "total": 0}
        threading.Thread(target=_run_db_download,
                         args=(name, url, _DB_TARGETS[name][0]), daemon=True).start()
        started.append(name)
    return jsonify({"ok": True, "started": started, "skipped_no_url": skipped})


@app.route("/api/webview")
def api_webview():
    """Proxy a remote URL for the in-app web view: fetch it and re-serve it from
    this origin WITHOUT the X-Frame-Options / frame-ancestors headers that would
    block embedding. HTML gets a <base> so its relative assets resolve to the
    original site; PDFs stream through. The client frames this in a SANDBOXED
    iframe (no allow-same-origin) so the proxied page's scripts run isolated and
    cannot reach the app. (Local/loopback only — never expose publicly: it is an
    open fetch proxy.)"""
    url = (request.args.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        abort(400)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": whl_client.USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            body = resp.read(25 * 1024 * 1024)   # cap: don't proxy huge pages
            charset = resp.headers.get_content_charset() or "utf-8"
    except Exception:
        abort(502)
    if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        return Response(body, content_type="application/pdf")
    if "html" in ctype or not ctype:
        html = body.decode(charset, "replace")
        base = '<base href="' + url.replace('"', "%22") + '">'
        if re.search(r"<head[^>]*>", html, re.I):
            html = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + base, html,
                          count=1, flags=re.I)
        else:
            html = base + html
        return Response(html, content_type="text/html; charset=utf-8")
    return Response(body, content_type=ctype or "application/octet-stream")


@app.route("/api/ia/meta")
def api_ia_meta():
    """An Internet Archive item's metadata + downloadable files, for the in-app
    IA viewer (preview + metadata table + download links)."""
    ident = (request.args.get("id") or "").strip()
    if not ident:
        abort(400)
    try:
        with urllib.request.urlopen(
                "https://archive.org/metadata/" + urllib.parse.quote(ident),
                timeout=25) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        abort(502)
    md = data.get("metadata") or {}
    want = ("pdf", "epub", "text", "djvu")
    dloads, seen = [], set()
    for f in data.get("files") or []:
        name, fmt = f.get("name") or "", f.get("format") or ""
        low = fmt.lower()
        if not name or not any(w in low for w in want) or fmt in seen:
            continue
        seen.add(fmt)   # one entry per format is enough for the picker
        dloads.append({
            "name": name, "format": fmt, "size": f.get("size"),
            "url": (f"https://archive.org/download/{urllib.parse.quote(ident)}"
                    f"/{urllib.parse.quote(name)}"),
        })
    pdf = next((d["url"] for d in dloads if "pdf" in d["format"].lower()), "")
    return jsonify({
        "id": ident, "metadata": md, "downloads": dloads, "pdf": pdf,
        "details": "https://archive.org/details/" + ident,
    })


# --- copyright registration lookup (network; cached per book) ------------------
_REG_CACHE_PATH = lib.DATA_ROOT / "downloads" / "cache" / "copyright_reg.json"
_reg_cache: dict | None = None
_reg_cache_lock = threading.Lock()
_REG_CACHE_VERSION = 2
_REG_NEGATIVE_TTL = 24 * 60 * 60


def _reg_cache_load() -> dict:
    global _reg_cache
    if _reg_cache is None:
        try:
            _reg_cache = json.loads(_REG_CACHE_PATH.read_text("utf-8"))
        except Exception:
            _reg_cache = {}
    return _reg_cache


def _reg_cache_store(cache: dict) -> None:
    try:
        lib.save_json(_REG_CACHE_PATH, cache)   # atomic, unlike write_text
    except Exception:
        pass


def _reg_cache_key(title: str, author: str, year_value, sources) -> str:
    def n(s):
        return " ".join(str(s or "").lower().split())
    source_key = ",".join(s for s in copyreg.SOURCES if s in set(sources))
    return (f"registration-v{_REG_CACHE_VERSION}|{n(title)}|{n(author)}|"
            f"{n(year_value)}|{source_key}")


def _status_cache_key(title: str, author: str, year_value) -> str:
    def n(s):
        return " ".join(str(s or "").lower().split())
    # Preserve the pre-v2 status namespace; only registration results need the
    # parser-version invalidation above.
    return n(title) + "|" + n(author) + "|__status__," + n(year_value)


@app.route("/api/copyright/registration")
def api_copyright_registration():
    """Look up an original copyright REGISTRATION for a book (the left half of
    the split copyright tag). Network + cached; the client passes the enabled
    sources (from settings) as a comma list."""
    title = (request.args.get("title") or "").strip()
    author = (request.args.get("author") or "").strip()
    year = (request.args.get("year") or "").strip()
    requested_sources = set(
        (request.args.get("sources") or "cprs").split(","))
    sources = tuple(s for s in copyreg.SOURCES if s in requested_sources)
    if not title or not sources:
        return jsonify({"found": False, "sources": [], "match": None})
    key = _reg_cache_key(title, author, year, sources)
    now = time.time()
    with _reg_cache_lock:
        cache = _reg_cache_load()
        cached = cache.get(key)
        if isinstance(cached, dict) and isinstance(cached.get("result"), dict):
            result = cached["result"]
            age = now - float(cached.get("cached_at") or 0)
            if result.get("found") or age < _REG_NEGATIVE_TTL:
                return jsonify(result)
    try:
        result = copyreg.registration_lookup(title, author, year, sources)  # network
    except copyreg.RegistrationLookupError as exc:
        return jsonify({
            "found": False, "sources": [], "match": None,
            "error": str(exc), "retryable": True,
        }), 503
    with _reg_cache_lock:
        cache = _reg_cache_load()
        cache[key] = {"cached_at": now, "result": result}
        _reg_cache_store(cache)
    return jsonify(result)


@app.route("/api/copyright/status")
def api_copyright_status():
    """Renewal-based copyright status for a title/author/year (the tag's right
    half). Checked books already carry this in their checks; WHL rows don't, so
    the tag fetches it here. Offline (renewals CSV) + cached."""
    title = (request.args.get("title") or "").strip()
    author = (request.args.get("author") or "").strip()
    year = (request.args.get("year") or "").strip()
    if not title:
        return jsonify({"copyright_status": ""})
    key = _status_cache_key(title, author, year)
    with _reg_cache_lock:
        cache = _reg_cache_load()
        if key in cache:
            return jsonify(cache[key])
    status = checks.copyright_status_for(title, author, year, checks.get_renewals())
    result = {"copyright_status": status}
    with _reg_cache_lock:
        cache = _reg_cache_load()
        cache[key] = result
        _reg_cache_store(cache)
    return jsonify(result)


@app.route("/api/copyright/renewal")
def api_copyright_renewal():
    """Dates for the renewal IDs named in a copyright status, for the tag's
    tooltip. One CSV scan resolves the whole batch, and misses are cached too,
    so an ID is scanned for at most once ever."""
    ids = [s for s in (request.args.get("ids") or "").split(",") if s.strip()][:200]
    out: dict[str, dict] = {}
    missing: list[str] = []
    with _reg_cache_lock:
        cache = _reg_cache_load()
        for rid in ids:
            hit = cache.get("__renewal__|" + rid)
            if hit is None:
                missing.append(rid)
            else:
                out[rid] = hit
    if missing:
        found = checks.renewal_details(missing)   # offline; one scan for the batch
        with _reg_cache_lock:
            cache = _reg_cache_load()
            for rid in missing:
                out[rid] = cache["__renewal__|" + rid] = found.get(rid) or {}
            _reg_cache_store(cache)
    return jsonify(out)


# --- WHL catalogue view (editable via a corrections overlay) --------------------

WHL_CORRECTIONS_PATH = lib.OUTPUT_DIR / "whl_corrections.json"
# The one lock for every whl_corrections.json read-modify-write — the edit
# route AND the cloud sync (passed into store_sync.sync_stores), so a sync
# pass can never drop a correction recorded while it merged. Single process;
# _whl_rows_lock below only guards the merged-rows cache, not this file.
_corrections_lock = threading.Lock()
_whl_rows_cache: list | None = None
_whl_rows_lock = threading.Lock()

# The catalogue export lacks subtitle/description/publisher/pages/language/
# subject (they exist on the WHL website); those columns are filled by the
# scraper (tools/whl_scrape.py) and refined via corrections.
_WHL_EDIT_FIELDS = ("title", "subtitle", "authors", "year", "categories",
                    "description", "publisher", "pages", "language", "subject")


def _load_whl_base() -> list[dict]:
    """whl_catalog.csv rows with stable indexes (cached; the CSV is static)."""
    global _whl_rows_cache
    with _whl_rows_lock:
        if _whl_rows_cache is None:
            rows = []
            path = checks.WHL_CATALOG_CSV
            if path.exists():
                import csv
                with open(path, "r", encoding="utf-8-sig", errors="replace",
                          newline="") as fh:
                    for i, raw in enumerate(csv.DictReader(fh)):
                        rows.append({
                            "idx": i,
                            "title": (raw.get("Title") or "").strip(),
                            "subtitle": "",
                            "authors": (raw.get("Authors") or "").strip(),
                            "year": whl_client._year(raw.get("Year Published")) or "",
                            "categories": (raw.get("Library Categories") or "").strip(),
                            "description": "",
                            "publisher": "",
                            "pages": "",
                            "language": "",
                            "subject": "",
                            "status": (raw.get("Status") or "").strip().lower(),
                            "permalink": (raw.get("Permalink") or "").strip(),
                            "file": (raw.get("Publication File") or "").strip(),
                        })
            _whl_rows_cache = rows
        return _whl_rows_cache


# Fields the scraper fills in when the CSV has nothing better.
_WHL_SCRAPED_FIELDS = ("subtitle", "description", "publisher", "pages",
                       "language", "subject")


def _permalink_slug(permalink: str) -> str:
    if "/catalog/" not in (permalink or ""):
        return ""  # drafts only have ?post_type=...&p= permalinks
    return permalink.rstrip("/").rsplit("/", 1)[-1]


def _merged_whl_rows() -> list[dict]:
    """Base CSV rows + scraped website metadata + the corrections overlay
    (in that precedence order); added rows first."""
    base = [dict(r) for r in _load_whl_base()]
    scraped = whl_scrape.load_scraped()
    if scraped:
        for r in base:
            s = scraped.get(_permalink_slug(r.get("permalink", "")))
            if not s:
                continue
            r["scraped"] = True
            for f in _WHL_SCRAPED_FIELDS:
                if s.get(f):
                    r[f] = s[f]
            # Scraped authors/year are authoritative where the CSV is blank.
            for f in ("authors", "year"):
                if not r.get(f) and s.get(f):
                    r[f] = s[f]
    corr = lib.load_json(WHL_CORRECTIONS_PATH, {})
    for sidx, edits in (corr.get("edits") or {}).items():
        try:
            i = int(sidx)
        except ValueError:
            continue
        if 0 <= i < len(base):
            # Keep the pre-correction values: the client shows the original
            # record while Alt is held over an edited row.
            orig = {}
            for f in _WHL_EDIT_FIELDS:
                if f in edits:
                    orig[f] = base[i].get(f, "")
                    base[i][f] = edits[f]
            base[i]["corrected"] = True
            base[i]["orig"] = orig
            # Which fields carry corrections — undo needs to know whether to
            # restore a previous correction or clear back to the CSV value.
            base[i]["edited_fields"] = [f for f in _WHL_EDIT_FIELDS if f in edits]
    added = []
    for j, a in enumerate(corr.get("added") or []):
        row = {f: a.get(f, "") for f in _WHL_EDIT_FIELDS}
        row.update({"idx": -(j + 1), "status": "added", "permalink": "",
                    "file": "", "added": True})
        added.append(row)
    added.reverse()  # newest first
    return added + base


@app.route("/api/whl_catalog")
def api_whl_catalog():
    return jsonify({"rows": _merged_whl_rows(),
                    "corrections": str(WHL_CORRECTIONS_PATH.name)})


# --- WHL website metadata scrape (background job) --------------------------------

_scrape_job: dict = {"status": "idle"}
_scrape_lock = threading.Lock()


def _run_scrape() -> None:
    try:
        whl_scrape.scrape_all(_scrape_job)
        _scrape_job["status"] = "done"
    except Exception as exc:
        _scrape_job["status"] = "error"
        _scrape_job["error"] = f"{type(exc).__name__}: {exc}"


@app.route("/api/whl_scrape", methods=["POST"])
def api_whl_scrape_start():
    with _scrape_lock:
        if _scrape_job.get("status") == "running":
            return jsonify(_scrape_job)
        _scrape_job.clear()
        _scrape_job.update({"status": "running", "page": 0, "pages": 0, "records": 0})
        threading.Thread(target=_run_scrape, daemon=True).start()
    return jsonify(_scrape_job)


@app.route("/api/whl_scrape/status")
def api_whl_scrape_status():
    out = dict(_scrape_job)
    out["scraped_total"] = len(whl_scrape.load_scraped())
    return jsonify(out)


@app.route("/api/whl_catalog", methods=["POST"])
def api_whl_catalog_edit():
    """Record corrections: {idx, field, value}, {idx, fields: {..}} for a
    multi-field repopulation, or {add: {...}} for a new row.

    The CSV export itself is never modified; changes live in
    output/whl_corrections.json so they are reviewable and revertible.
    """
    payload = request.get_json(silent=True) or {}
    with _corrections_lock:
        corr = lib.load_json(WHL_CORRECTIONS_PATH, {})
        if "add" in payload:
            a = payload.get("add") or {}
            row = {f: str(a.get(f, "") or "").strip() for f in _WHL_EDIT_FIELDS}
            if not row["title"]:
                return jsonify({"ok": False, "error": "TITLE IS REQUIRED"}), 400
            corr.setdefault("added", []).append(row)
            lib.save_json(WHL_CORRECTIONS_PATH, corr)
            return jsonify({"ok": True, "idx": -len(corr["added"])})

        if "remove_added" in payload:  # undo of an add
            try:
                j = -int(payload["remove_added"]) - 1
            except (TypeError, ValueError):
                abort(400)
            added = corr.get("added") or []
            if not (0 <= j < len(added)):
                abort(404)
            added.pop(j)
            lib.save_json(WHL_CORRECTIONS_PATH, corr)
            return jsonify({"ok": True})

        fields = {f: str(v or "").strip() for f, v in (payload.get("fields") or {}).items()
                  if f in _WHL_EDIT_FIELDS}
        if "field" in payload:
            field = str(payload.get("field", "") or "")
            if field not in _WHL_EDIT_FIELDS:
                abort(400)
            fields[field] = str(payload.get("value", "") or "").strip()
        clear = [f for f in (payload.get("clear_fields") or []) if f in _WHL_EDIT_FIELDS]
        if not fields and not clear:
            abort(400)
        try:
            idx = int(payload.get("idx"))
        except (TypeError, ValueError):
            abort(400)
        if idx >= 0:
            if idx >= len(_load_whl_base()):
                abort(404)
            edits = corr.setdefault("edits", {}).setdefault(str(idx), {})
            edits.update(fields)
            for f in clear:  # drop the correction entirely -> CSV value shows again
                edits.pop(f, None)
            if not edits:
                corr["edits"].pop(str(idx), None)
        else:
            added = corr.get("added") or []
            j = -idx - 1
            if j >= len(added):
                abort(404)
            added[j].update(fields)
            for f in clear:
                added[j][f] = ""
        lib.save_json(WHL_CORRECTIONS_PATH, corr)
    return jsonify({"ok": True})


@app.route("/api/ol/editions")
def api_ol_editions():
    work = (request.args.get("work") or "").strip()
    if not work:
        abort(400)
    constraints = {f: (request.args.get(f) or "").strip()
                   for f in ("publisher", "city", "year", "edition", "volume")}
    try:
        info = ol_client.best_edition(work, constraints)
        info["ok"] = True
        return jsonify(info)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


# --- offline checks + scan search for arbitrary books --------------------------

@app.route("/api/check")
def api_check():
    """Offline copyright + local-WHL check for a title/author/year triple."""
    title = (request.args.get("title") or "").strip()
    author = (request.args.get("author") or "").strip()
    year = (request.args.get("year") or "").strip()
    if not title:
        abort(400)
    return jsonify(checks.check_entry(title, author, year))


@app.route("/api/scans")
def api_scans():
    title = (request.args.get("title") or "").strip()
    author = (request.args.get("author") or "").strip()
    year = (request.args.get("year") or "").strip()
    if not title:
        abort(400)
    return jsonify(scan_search.search_scans(title, author or None, year or None))


# --- cloud capture sync (phone -> Supabase -> manual entries) --------------------
# The Android capture app drops photo sets into Supabase; this engine pulls
# pending captures, runs the photo pipeline (perspective/compress/OCR/extract),
# and files each capture as a manual entry with its processed images attached.
# The checked/manual catalog is mirrored one-way into the cloud `books` table.

CAPTURES_DIR = lib.DATA_ROOT / "captures"
_cloudsync_lock = threading.Lock()
_cloudsync = {"running": False, "last_run": "", "last_error": "", "last_result": None}
_autosync_last = 0.0


def _cloud_cfg() -> dict | None:
    """Service-role config for privileged owner publishing and maintenance.

    Phone capture intentionally does not use this path; see ``_capture_cfg``.
    The URL defaults to the shipped project, but this secret never does.
    """
    s = _client_settings()
    url = str(s.get("supabaseUrl") or "").strip() or cloud_defaults.SUPABASE_URL
    key = str(s.get("supabaseKey") or "").strip()
    return {"url": url, "key": key} if url and key else None


def _capture_cfg() -> dict | None:
    """Public project identity plus the signed-in user's current JWT.

    This is the complete phone-sync credential. Supabase uses ``key`` only to
    identify the public app component and ``access_token`` to enforce the
    user's capture/storage RLS policies. A fresh install therefore needs a
    login, never a pasted Supabase key.
    """
    cfg = _auth_cfg()
    ses = _auth_session() if cfg else None
    token = str((ses or {}).get("access_token") or "").strip()
    if not cfg or not token:
        return None
    return dict(cfg, access_token=token)



# --- Analyze: AI summaries, categories, translations, annotations, relevance -----
# DeepSeek by default: with Settings > AI left blank the app talks to
# https://api.deepseek.com with model deepseek-chat, so pasting a key is the
# whole setup. Any OpenAI-compatible endpoint works. Credentials are read
# server-side (_client_settings) because these jobs outlive the page that
# started them. Only verified builds (status ready/uploaded) are analyzable.
#
# Artifacts land in the entry folder — the per-book bundle that already
# mirrors to R2 — and publishing pushes whatever build.bundle includes to the
# anon-readable volume_texts / volume_pages / volume_notes tables. The
# relevance assessment is the deliberate exception: it stays on the build
# record (service_role-only sync) and never enters a published row.

_AI_DEFAULT_BASE = "https://api.deepseek.com"
_AI_DEFAULT_MODEL = "deepseek-chat"


def _ai_cfg() -> dict:
    s = _client_settings()
    return {"base": str(s.get("aiBase") or "").strip() or _AI_DEFAULT_BASE,
            "model": str(s.get("aiModel") or "").strip() or _AI_DEFAULT_MODEL,
            "key": str(s.get("aiKey") or "").strip(),
            "instructions": str(s.get("aiInstructions") or "").strip(),
            # user overrides (Settings > AI): blank temperature keeps each call's own default
            "temperature": s.get("aiTemperature"),
            "timeout": s.get("aiTimeout")}


def _ai_chat(cfg: dict, messages: list, json_mode: bool = False,
             temperature: float = 0.3, timeout: float = 240.0) -> str:
    """One chat-completions call; returns the assistant text. Raises
    RuntimeError with the HTTP body truncated to 300 chars, the same error
    convention every other integration here uses."""
    if not cfg["key"]:
        raise RuntimeError("no AI key — set one in Settings > AI "
                           "(DeepSeek is the default provider)")
    # a set temperature/timeout in Settings > AI overrides the per-call defaults
    _t = cfg.get("temperature")
    if _t not in (None, ""):
        try:
            temperature = max(0.0, min(2.0, float(_t)))
        except (TypeError, ValueError):
            pass
    _to = cfg.get("timeout")
    if _to:
        try:
            timeout = max(10.0, min(1200.0, float(_to)))
        except (TypeError, ValueError):
            pass
    body = {"model": cfg["model"], "messages": messages,
            "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        cfg["base"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['key']}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError) as exc:
        raise RuntimeError("malformed chat response") from exc


def _ai_json(cfg: dict, messages: list, temperature: float = 0.2) -> dict:
    """A JSON-mode call, code fences stripped, parse failure = {}."""
    raw = _ai_chat(cfg, messages, json_mode=True, temperature=temperature)
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {}
    except json.JSONDecodeError:
        return {}


# The catalogue fields a Process/DeepSeek pass is allowed to touch — so the
# model can't slip arbitrary keys into a staged alternative. "categories" is
# deliberately excluded: it is a structured field edited through the chip
# picker, and a free-text value staged here would apply to the deprecated flat
# column, never showing in the UI (the diff would never clear).
_PROC_FIELD_KEYS = frozenset((
    "title", "subtitle", "author", "authors", "year", "publisher",
    "publisher_city", "city", "edition", "volume", "language", "pages",
    "description", "subject"))


@app.route("/api/process/deepseek", methods=["POST"])
def api_process_deepseek():
    """Process-mode "DeepSeek custom instructions" for ONE record: given its
    current fields and the user's instructions, return ONLY the fields that
    should change (same names), for staging as an alternative. The client loops
    this over the selection. Human-in-the-loop: nothing is applied here."""
    p = request.get_json(silent=True) or {}
    fields = p.get("fields")
    if not isinstance(fields, dict):
        abort(400)
    cfg = _ai_cfg()
    if not cfg["key"]:
        return jsonify({"ok": False, "error": "No AI key — set one in Settings > AI"}), 400
    cur = {k: str(v)[:600] for k, v in fields.items()
           if isinstance(k, str) and k in _PROC_FIELD_KEYS
           and isinstance(v, (str, int, float)) and str(v).strip()}
    if not cur:
        return jsonify({"ok": True, "fields": {}})
    sys = ("You are a meticulous bibliographic metadata editor. You are given one "
           "book catalogue record as JSON. Apply the user's instructions and "
           "return ONLY a JSON object of the fields that should CHANGE, using the "
           "exact same field names. Omit unchanged fields. Do not invent facts you "
           "cannot derive from the given values; when unsure, omit the field. "
           "Return {} if nothing should change.")
    run_instr = str(p.get("instructions") or "").strip()
    if cfg["instructions"]:
        sys += "\n\nStanding instructions: " + cfg["instructions"]
    if run_instr:
        sys += "\n\nThis run's instructions: " + run_instr[:2000]
    try:
        out = _ai_json(cfg, [{"role": "system", "content": sys},
                             {"role": "user", "content": "Record:\n" + json.dumps(cur, ensure_ascii=False)}],
                       temperature=0.1)
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)[:200]}), 502
    changed = {}
    for k, v in (out.items() if isinstance(out, dict) else []):
        if isinstance(k, str) and k in _PROC_FIELD_KEYS and isinstance(v, (str, int, float)):
            nv = str(v).strip()
            if nv and nv != str(cur.get(k, "")).strip():
                changed[k] = nv[:2000]
    return jsonify({"ok": True, "fields": changed})


# --- Smart Scan (Process action) -------------------------------------------------
# Reuses the smart-check engine: locate/download a book's own PDF, skip visually
# blank front matter, OCR the first pages with Mistral until a title/imprint page
# and a copyright page have both been seen, then extract fields with DeepSeek.
# The result is staged as a "smartscan" alternative for review — the real record
# is never touched here. Runs on a daemon thread against the unified job registry.
_SS_SCAN_CAP = 15        # pages considered from the front (blanks included)
_SS_OCR_CAP = 8          # pages actually sent to OCR
_SS_WIDTH = 1400         # render width (the OCR queue's default)
_SS_JOBS_KEEP = 20

# extraction vocabulary (capture.FIELDS) -> each record store's field names
_SS_FIELD_MAPS = {
    "whl": {"title": "title", "subtitle": "subtitle", "author": "authors",
            "year": "year", "publisher": "publisher", "language": "language"},
    "build": {"title": "title", "subtitle": "subtitle", "author": "authors",
              "year": "year", "publisher": "publisher", "city": "publisher_city",
              "edition": "edition", "volume": "volume", "language": "language"},
    "checked": {f: f for f in ("title", "subtitle", "author", "publisher",
                               "city", "year", "edition", "volume", "language")},
}
_SS_FIELD_MAPS["manual"] = _SS_FIELD_MAPS["checked"]

# derived (not copied) from the capture prompt so the JSON field contract can't
# drift; a no-op replace still leaves a valid prompt.
_SS_PROMPT = capture._EXTRACT_PROMPT.replace(
    "OCR text from photos of a book's title page and/or copyright page",
    "OCR text from the first pages of a digitized copy of a book "
    "(cover, title page, copyright page, other front matter)")

_SS_COPYRIGHT_RE = re.compile(
    r"copyright|©|all rights reserved|printed in|first published"
    r"|impression|printing|entered according to act", re.I)
_SS_YEAR_RE = re.compile(r"\b(1[4-9]\d{2}|20\d{2})\b")
_SS_IMPRINT_RE = re.compile(
    r"publish|press\b|verlag|editore|editions?\b|librair|imprim"
    r"|printed for|book (?:co|company)|& ?co\b|and company|sons\b|brothers\b", re.I)

_ss_jobs: dict = {}
_ss_jobs_lock = threading.Lock()
_ss_start_lock = threading.Lock()


def _ss_target_kind(target) -> str:
    kind = str(target or "").partition(":")[0]
    return kind if kind in _SS_FIELD_MAPS else ""


def _ss_scan_pages(pdf: Path) -> list[int]:
    """1-based front-matter candidates: the first _SS_SCAN_CAP pages minus
    visually blank ones (ink + text-layer test), so OCR isn't spent on versos."""
    import fitz
    pages = []
    doc = fitz.open(str(pdf))
    try:
        for i in range(min(doc.page_count, _SS_SCAN_CAP)):
            pg = doc[i]
            zoom = 160 / max(1.0, pg.rect.width)
            pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace="gray")
            samples = pix.samples
            inked = sum(1 for v in samples if v < 200)
            if inked / max(1, len(samples)) >= 0.003 or (pg.get_text() or "").strip():
                pages.append(i + 1)
    finally:
        doc.close()
    return pages


def _ss_ocr_page(pdf: Path, page: int, key: str) -> str:
    png = _ocr_page_png(pdf, page, _SS_WIDTH)
    pages = capture.mistral_ocr_pages(png, key)
    return "\n\n".join(p.get("markdown", "") for p in pages).strip()


def _ss_extract(ocr_text: str) -> tuple[dict, str]:
    """OCR text -> extracted bibliographic dict + the model name. DeepSeek when a
    Settings > AI key is set (the phone app's default), else Mistral extraction."""
    cfg = _ai_cfg()
    if cfg["key"]:
        obj = _ai_json(cfg, [{"role": "user", "content": _SS_PROMPT + ocr_text[:12000]}],
                       temperature=0.0)
        return (obj if isinstance(obj, dict) else {}), cfg["model"]
    mkey = str(_client_settings().get("mistralKey") or "").strip()
    if not mkey:
        raise RuntimeError("no AI key and no Mistral key — set one in "
                           "Settings > AI or Settings > OCR")
    return capture.extract_bibliography(ocr_text, mkey), capture.EXTRACT_MODEL


def _ss_map_fields(kind: str, fields: dict) -> dict:
    """Extraction vocabulary -> the target store's names; blanks never map (a
    scan may fill or correct a field, never erase one)."""
    out = {}
    for src, dst in _SS_FIELD_MAPS.get(kind, {}).items():
        v = str(fields.get(src) or "").strip()
        if v:
            out[dst] = v
    return out


def _ss_job_new(target: str, label: str) -> dict:
    job = {"id": lib.gen_id(set(_ss_jobs) | set(_jobs)), "target": target,
           "kind": "smartscan", "done": 0, "total": 0, "errors": 0,
           "status": "running", "error": "", "note": ""}
    with _ss_jobs_lock:
        _ss_jobs[job["id"]] = job
    _job_track(job, "smartscan", label=label)
    return job


def _ss_job_start(target: str, label: str, run) -> dict:
    job = _ss_job_new(target, label)
    threading.Thread(target=run, args=(job,), daemon=True).start()
    return job


def _ss_finish(job: dict, error: str = "") -> None:
    with _ss_jobs_lock:
        job["error"] = error
        status = "error" if error else ("done (with errors)" if job["errors"] else "done")
    _job_transition(job, status)
    with _ss_jobs_lock:
        done = sorted((j for j in _ss_jobs.values() if j.get("state") not in _JOB_ACTIVE),
                      key=lambda j: str(j.get("finished_at") or ""), reverse=True)
        for old in done[_SS_JOBS_KEEP:]:
            _ss_jobs.pop(str(old.get("id")), None)


def _ss_run(job: dict, spec: dict) -> None:
    target = spec["target"]
    kind = _ss_target_kind(target)
    try:
        mkey = str(_client_settings().get("mistralKey") or "").strip()
        if not mkey:
            raise RuntimeError("Mistral API key not configured (Settings > OCR)")
        pdf = spec.get("pdf_path")
        if pdf is None:
            with _ss_jobs_lock:
                job["note"] = "downloading PDF"
            _job_checkpoint(job, force=True)
            pdf = _remote_pdf_cache(spec["url"])   # ValueError on SSRF / size / non-PDF
            with _ss_jobs_lock:
                job["note"] = ""
        pdf = Path(pdf)
        candidates = _ss_scan_pages(pdf)
        if not candidates:
            raise RuntimeError(f"no readable pages in the first {_SS_SCAN_CAP} pages")
        planned = candidates[:_SS_OCR_CAP]
        with _ss_jobs_lock:
            job["total"] = len(planned) + 1        # +1 = the extraction step
        texts: dict[int, str] = {}
        titleish = copyrightish = False
        for i, n in enumerate(planned):
            if _an_cancel_check(job, "cancelled — nothing was written"):
                return
            try:
                text = _ss_ocr_page(pdf, n, mkey)
            except Exception as exc:
                text = ""
                with _ss_jobs_lock:
                    job["errors"] += 1
                    job["note"] = f"page {n}: {type(exc).__name__}"
            if text:
                texts[n] = text
                titleish = titleish or bool(_SS_YEAR_RE.search(text) or _SS_IMPRINT_RE.search(text))
                copyrightish = copyrightish or bool(_SS_COPYRIGHT_RE.search(text))
            with _ss_jobs_lock:
                job["done"] = i + 1
            _job_checkpoint(job)
            # both signals in hand: stop. Also cap the copyright hunt for books
            # that never print "copyright" (pre-1900 / non-English) so we don't
            # burn all _SS_OCR_CAP pages chasing a signal that never fires.
            if titleish and copyrightish and len(texts) >= 2:
                break
            if titleish and len(texts) >= 4:
                break
        ocr_text = "\n\n".join(f"--- page {n} ---\n{texts[n]}" for n in sorted(texts))
        if not ocr_text.strip():
            raise RuntimeError("OCR produced no text from the front matter")
        if _an_cancel_check(job, "cancelled — nothing was written"):
            return
        got, model = _ss_extract(ocr_text)
        got = got if isinstance(got, dict) else {}
        got.pop("extra", None)
        mapped = _ss_map_fields(kind, got)
        # an all-blank extraction must fail, not stage a "nothing changed" record
        if not mapped:
            raise RuntimeError("extraction returned no usable fields — retry")
        if _an_cancel_check(job, "cancelled — nothing was written"):
            return
        _staged_add(target, kind, str(spec.get("label") or ""),
                    {"source": "smartscan", "fields": mapped,
                     "note": f"pages {sorted(texts)} · {model}"})
        with _ss_jobs_lock:
            job["done"] = job["total"]
        activity("smart-scanned", "Book metadata", detail=str(spec.get("label") or target))
        _ss_finish(job)
    except Exception as exc:
        log.error("smart scan failed for %s", target, exc_info=exc)
        _ss_finish(job, f"{type(exc).__name__}: {exc}")


@app.route("/api/process/smartscan/run", methods=["POST"])
def api_process_smartscan_run():
    """Start a Smart Scan for one record. Body: {target, pdf?|url?, label?}.
    Returns the job to poll; a duplicate while one is running joins it."""
    p = request.get_json(silent=True) or {}
    target = str(p.get("target") or "").strip()
    kind = _ss_target_kind(target)
    if not kind or ":" not in target:
        return jsonify({"ok": False, "error": "bad target"}), 400
    label = str(p.get("label") or "").strip()[:120]
    raw_pdf = str(p.get("pdf") or "").strip()
    url = str(p.get("url") or "").strip()
    spec = {"target": target, "label": label, "pdf_path": None, "url": url}
    if raw_pdf:
        lp = _resolve_local(raw_pdf)
        if lp is None or lp.suffix.lower() != ".pdf" or not lp.is_file():
            return jsonify({"ok": False, "error": "PDF not found"}), 404
        spec["pdf_path"] = lp
    elif url:
        if not url.lower().startswith(("http://", "https://")):
            return jsonify({"ok": False, "error": "not an http(s) URL"}), 400
    else:
        return jsonify({"ok": False, "error": "pdf or url required"}), 400
    with _ss_start_lock:
        with _jobs_lock:
            for j in _jobs.values():
                if (j.get("kind") == "smartscan" and j.get("target") == target
                        and j.get("state") in _JOB_ACTIVE):
                    return jsonify({"ok": True, "already": True, "job": _job_public(j)})
        job = _ss_job_start(target, label, lambda jb: _ss_run(jb, spec))
    return jsonify({"ok": True, "job": dict(job)})


@app.route("/api/process/smartscan/job/<job_id>")
def api_process_smartscan_job(job_id: str):
    with _ss_jobs_lock:
        job = _ss_jobs.get(job_id)
        if job is not None:
            return jsonify(dict(job))
    with _jobs_lock:
        gone = _jobs.get(job_id)
    if gone is None:
        abort(404)
    return jsonify(_job_public(gone))


_PAGE_MARK = re.compile(r"^--- page (\d+) ---$", re.M)


def _an_pages(text: str) -> dict[int, str]:
    """Split an OCR doc on its page markers (the client's exact convention);
    an unmarked doc is one page."""
    marks = list(_PAGE_MARK.finditer(text))
    if not marks:
        return {1: text.strip()} if text.strip() else {}
    out = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[int(m.group(1))] = text[m.end():end].strip()
    return out


def _analyze_doc(bid: str, b: dict) -> tuple[str, str]:
    """(name, text) of the build's best OCR document: the verified one, the
    active one, then the conventional names."""
    d = _entry_dir(bid) / "ocr"
    candidates = [b.get("ocr_verified"), b.get("ocr_active"),
                  "compiled.txt", "extracted.txt"]
    for name in candidates:
        name = _ocr_name(name) if name else ""
        if name and (d / name).is_file():
            return name, (d / name).read_text(encoding="utf-8", errors="replace")
    return "", ""


def _analyze_doc_snapshot(bid: str, b: dict,
                          requested: str = "") -> tuple[str, str, int]:
    """Read one OCR source together with the page-structure revision.

    A guarded job start later verifies the revision under the same lock. Thus
    deletion either sees the registered job and refuses, or completes first
    and causes the stale snapshot to be rejected before a worker starts.
    """
    with _page_structure_lock:
        name, text = "", ""
        requested = str(requested or "").strip()
        if requested and _ocr_name(requested) == requested:
            ocr_root = (_entry_dir(bid) / "ocr").resolve()
            candidate = (ocr_root / requested).resolve()
            if candidate.is_relative_to(ocr_root) and candidate.is_file():
                name = requested
                text = candidate.read_text(encoding="utf-8", errors="replace")
        if not text:
            name, text = _analyze_doc(bid, b)
        return name, text, _page_structure_revision.get(bid, 0)


def _an_meta_line(b: dict) -> str:
    bits = [b.get("title") or "?"]
    if b.get("subtitle"):
        bits.append(b["subtitle"])
    if b.get("authors"):
        bits.append(f"by {b['authors']}")
    for k in ("year", "publisher", "publisher_city", "edition", "volume", "language"):
        if b.get(k):
            bits.append(str(b[k]))
    return " — ".join(bits)


def _an_gate(bid: str):
    """The build, or an error response: Analyze works on verified entries."""
    builds = lib.load_json(BUILDS_PATH, {})
    b = builds.get(bid)
    if not b:
        return None, (jsonify({"ok": False, "error": "no such entry"}), 404)
    if b.get("status") not in ("ready", "uploaded"):
        return None, (jsonify({
            "ok": False,
            "error": "only verified entries can be analyzed — mark it "
                     "verified in the Editor first"}), 400)
    return b, None


# --- analyze artifacts: about.md, summary.md, annotations.json, translations/ ----

_an_notes_lock = threading.Lock()


def _read_entry_text(bid: str, rel: str) -> str:
    p = _entry_dir(bid) / rel
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


def _write_entry_text(bid: str, rel: str, text: str) -> None:
    p = _entry_dir(bid) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _save_analyze_summary(bid: str, text: str) -> None:
    """Persist an Analyze summary as both an artifact and catalog metadata.

    The Editor's description is the public catalog description. Keeping it in
    the build record makes Analyze output visible there immediately and ensures
    publication does not depend on a second manual copy/paste step.
    """
    summary = str(text or "").strip()
    _write_entry_text(bid, "summary.md", summary + ("\n" if summary else ""))
    with _builds_lock:
        builds = lib.load_json(BUILDS_PATH, {})
        if bid not in builds:
            return
        builds[bid]["description"] = summary
        builds[bid]["updated_at"] = _build_updated_at(
            builds[bid].get("updated_at"))
        lib.save_json(BUILDS_PATH, builds)


def _save_analyze_about(bid: str, text: str) -> None:
    """Persist the About article and mirror it into Editor Description.

    The article remains a publishable per-book artifact while the build record
    is the Editor's source of truth. Lock the read-modify-write so a completed
    background analysis cannot discard an unrelated Editor or publish update.
    """
    about = str(text or "").strip()
    _write_entry_text(bid, "about.md", about + ("\n" if about else ""))
    with _builds_lock:
        builds = lib.load_json(BUILDS_PATH, {})
        if bid not in builds:
            return
        builds[bid]["description"] = about
        builds[bid]["updated_at"] = _build_updated_at(
            builds[bid].get("updated_at"))
        lib.save_json(BUILDS_PATH, builds)


def _load_annotations(bid: str) -> dict:
    doc = lib.load_json(_entry_dir(bid) / "annotations.json", None)
    if not isinstance(doc, dict) or not isinstance(doc.get("notes"), list):
        return {"version": 1, "notes": []}
    return doc


def _revalidate_note_anchors(bid: str, b: dict, saved: str) -> None:
    """After an OCR text edit, re-check that each note's quote still exists on
    its page. A quote that no longer matches is flagged, never deleted — the
    curator decides what a rewritten page means for the note."""
    doc_name, text = _analyze_doc(bid, b)
    if saved != doc_name:
        return                        # not the document the notes anchor to
    src_pages = _an_pages(text)
    with _an_notes_lock:
        doc = _load_annotations(bid)
        changed = False
        for n in doc["notes"]:
            quote = str(n.get("quote") or "")
            if not quote:
                continue
            flat = re.sub(r"\s+", " ", src_pages.get(n.get("page"), "")).lower()
            anchor = ("ok" if re.sub(r"\s+", " ", quote).lower() in flat
                      else "orphaned")
            if n.get("anchor") != anchor:
                n["anchor"] = anchor
                changed = True
        if changed:
            lib.save_json(_entry_dir(bid) / "annotations.json", doc)


def _lang_code(raw: str) -> str:
    return re.sub(r"[^a-z\-]", "", str(raw or "").lower())[:12]


def _page_sha(text: str) -> str:
    return hashlib.sha1(
        re.sub(r"\s+", " ", text.strip()).encode("utf-8")).hexdigest()


def _translation_meta_path(bid: str, lang: str):
    return _entry_dir(bid) / "translations" / f"{lang}.meta.json"


def _load_translation_meta(bid: str, lang: str) -> dict:
    doc = lib.load_json(_translation_meta_path(bid, lang), None)
    if not isinstance(doc, dict) or not isinstance(doc.get("pages"), dict):
        return {"version": 1, "src": "", "model": "", "pages": {}}
    return doc


def _stale_translation_pages(meta: dict, src_pages: dict[int, str]) -> list[int]:
    """Pages whose recorded source hash no longer matches the OCR text. Pages
    translated before hashes were recorded can't be judged — never "stale"."""
    out = []
    for key, rec in meta.get("pages", {}).items():
        try:
            n = int(key)
        except (TypeError, ValueError):
            continue
        if (n in src_pages and isinstance(rec, dict) and rec.get("sha1")
                and rec["sha1"] != _page_sha(src_pages[n])):
            out.append(n)
    return sorted(out)


def _translations_info(bid: str) -> list[dict]:
    d = _entry_dir(bid) / "translations"
    out = []
    if d.is_dir():
        b = lib.load_json(BUILDS_PATH, {}).get(bid) or {}
        src_pages = _an_pages(_analyze_doc(bid, b)[1])
        for f in sorted(d.glob("*.txt")):
            pages = _an_pages(f.read_text(encoding="utf-8", errors="replace"))
            meta = _load_translation_meta(bid, f.stem)
            out.append({"lang": f.stem, "pages": len(pages),
                        "size": f.stat().st_size,
                        "stale": len(_stale_translation_pages(meta, src_pages)),
                        "untracked": sum(1 for n in pages
                                         if str(n) not in meta["pages"])})
    return out


@app.route("/api/builds/<bid>/about", methods=["GET", "PUT"])
def api_build_about(bid: str):
    if request.method == "GET":
        return jsonify({"ok": True, "text": _read_entry_text(bid, "about.md")})
    b, err = _an_gate(bid)
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    with _an_write_lock:
        _save_analyze_about(bid, str(payload.get("text") or ""))
    _manifest_record(bid, "about.md", {"kind": "manual-edit"})
    return jsonify({"ok": True})


@app.route("/api/builds/<bid>/summary")
def api_build_summary(bid: str):
    return jsonify({"ok": True, "text": _read_entry_text(bid, "summary.md")})


@app.route("/api/builds/<bid>/annotations", methods=["GET", "PUT"])
def api_build_annotations(bid: str):
    """GET the note list; PUT curation changes: {update: {id, status?, body?,
    kind?}} or {remove: id}. Wholesale replacement is deliberately absent —
    notes are curated one by one."""
    if request.method == "GET":
        return jsonify({"ok": True, "doc": _load_annotations(bid)})
    b, err = _an_gate(bid)
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    with _an_notes_lock:
        doc = _load_annotations(bid)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if payload.get("remove"):
            doc["notes"] = [n for n in doc["notes"]
                            if n.get("id") != payload["remove"]]
        upd = payload.get("update") or {}
        if upd.get("id"):
            for n in doc["notes"]:
                if n.get("id") != upd["id"]:
                    continue
                if upd.get("status") in ("suggested", "approved", "rejected"):
                    n["status"] = upd["status"]
                if "body" in upd:
                    n["body"] = str(upd["body"] or "").strip()
                if "kind" in upd:
                    n["kind"] = str(upd["kind"] or "").strip()[:24]
                n["updated_at"] = now
        lib.save_json(_entry_dir(bid) / "annotations.json", doc)
    # curation changed the content by hand; the recorded inputs (the OCR doc
    # the notes anchor to) are kept so staleness stays judgeable
    _manifest_record(bid, "annotations.json", {"kind": "manual-edit"})
    return jsonify({"ok": True, "doc": doc})


@app.route("/api/builds/<bid>/translations")
def api_build_translations(bid: str):
    return jsonify({"ok": True, "translations": _translations_info(bid)})


@app.route("/api/builds/<bid>/translations/<lang>", methods=["GET", "DELETE"])
def api_build_translation(bid: str, lang: str):
    lang = _lang_code(lang)
    p = _entry_dir(bid) / "translations" / f"{lang}.txt"
    if request.method == "GET":
        if not p.is_file():
            abort(404)
        return jsonify({"ok": True, "lang": lang,
                        "text": p.read_text(encoding="utf-8", errors="replace")})
    b, err = _an_gate(bid)
    if err:
        return err
    with _an_write_lock:
        if p.is_file():
            p.unlink()
        m = _translation_meta_path(bid, lang)
        if m.is_file():
            m.unlink()
    return jsonify({"ok": True})


# --- analyze jobs: one daemon thread each, polled like OCR jobs ------------------

_an_jobs: dict = {}
_an_jobs_lock = threading.Lock()
_an_write_lock = threading.Lock()


class _AnalyzeSourceChanged(Exception):
    """The page structure changed between route validation and job start."""


def _an_job_new(bid: str, kind: str, total: int) -> dict:
    job = {"id": lib.gen_id(set(_an_jobs) | set(_jobs)), "build_id": bid,
           "kind": kind, "done": 0, "total": total, "errors": 0,
           "status": "running", "error": "", "note": ""}
    with _an_jobs_lock:
        _an_jobs[job["id"]] = job
    _job_track(job, kind, label=_job_book_label(bid))
    return job


def _an_job_start(bid: str, kind: str, total: int, target,
                  decorate=None) -> dict:
    """Create and start a background Analyze job.

    ``decorate`` may attach immutable request metadata after the id is known
    but before the worker can run. Existing callers need no decoration.
    """
    with _page_structure_lock:
        job = _an_job_new(bid, kind, total)
        if decorate is not None:
            with _an_jobs_lock:
                decorate(job)
        threading.Thread(target=target, args=(job,), daemon=True).start()
        return job


def _an_job_start_guarded(bid: str, source_revision: int, kind: str,
                          total: int, target, decorate=None) -> dict:
    """Start only if the OCR snapshot still has its original page numbering."""
    with _page_structure_lock:
        if _page_structure_revision.get(bid, 0) != source_revision:
            raise _AnalyzeSourceChanged()
        return _an_job_start(bid, kind, total, target, decorate)


def _an_finish(job: dict, error: str = "") -> None:
    with _an_jobs_lock:
        job["error"] = error
        status = "error" if error else (
            "done (with errors)" if job["errors"] else "done")
    _job_transition(job, status)


def _an_cancel_check(job: dict, note: str) -> bool:
    """True when a cancel was requested — the run() closure returns at once.
    Whatever the loop already saved stays on disk; `note` says what that is."""
    if not _job_cancelled(job):
        return False
    _job_transition(job, "cancelled", note=note)
    return True


@app.route("/api/analyze/job/<job_id>")
def api_analyze_job(job_id: str):
    with _an_jobs_lock:
        job = _an_jobs.get(job_id)
        if job is not None:
            return jsonify(dict(job))
    # a restart dropped the worker: the persisted registry still knows it
    with _jobs_lock:
        gone = _jobs.get(job_id)
    if gone is None:
        abort(404)
    return jsonify(_job_public(gone))


# chunk pages to a character budget; DeepSeek's context is generous but a
# 400-page herbal is not one call
def _an_chunks(pages: dict[int, str], budget: int = 22000) -> list[tuple[list[int], str]]:
    chunks, nums, buf, size = [], [], [], 0
    for n in sorted(pages):
        t = pages[n]
        if size + len(t) > budget and buf:
            chunks.append((nums, "\n\n".join(buf)))
            nums, buf, size = [], [], 0
        nums = nums + [n]
        buf.append(f"[page {n}]\n{t}")
        size += len(t)
    if buf:
        chunks.append((nums, "\n\n".join(buf)))
    return chunks


@app.route("/api/analyze/pages", methods=["POST"])
def api_analyze_pages():
    """Analyze selected OCR pages and save the result as a book artifact.

    Body: ``{build_id, pages: [1, ...], doc?, engine?}``. ``engine`` is
    descriptive job metadata; requests continue to use the one configured
    OpenAI-compatible provider returned by :func:`_ai_cfg`.
    """
    p = request.get_json(silent=True) or {}
    bid = str(p.get("build_id") or "").strip()
    b, err = _an_gate(bid)
    if err:
        return err

    raw_pages = p.get("pages")
    if (not isinstance(raw_pages, list) or not raw_pages
            or any(isinstance(n, bool) or not isinstance(n, int) or n < 1
                   for n in raw_pages)):
        return jsonify({"ok": False, "error":
                        "pages must be a non-empty list of positive integers"}), 400
    wanted = sorted(set(raw_pages))

    requested_doc = str(p.get("doc") or "").strip()
    doc_name, text, source_revision = _analyze_doc_snapshot(
        bid, b, requested_doc)

    all_pages = _an_pages(text)
    missing = [n for n in wanted if n not in all_pages]
    if not all_pages:
        return jsonify({"ok": False, "error":
                        "no OCR text for this entry - extract or run OCR first"}), 400
    if missing:
        return jsonify({"ok": False, "error":
                        "OCR text does not contain page(s): "
                        + ", ".join(str(n) for n in missing)}), 400
    selected = {n: all_pages[n] for n in wanted}
    chunks = _an_chunks(selected)
    cfg = _ai_cfg()
    engine = str(p.get("engine") or cfg["model"]).strip()[:100] or cfg["model"]
    meta = _an_meta_line(b)
    src_input = _manifest_input(bid, f"ocr/{doc_name}")   # hashed at job start

    if wanted == list(range(wanted[0], wanted[-1] + 1)):
        page_slug = (str(wanted[0]) if len(wanted) == 1
                     else f"{wanted[0]}-{wanted[-1]}")
    elif len(wanted) <= 12:
        page_slug = "_".join(str(n) for n in wanted)
    else:
        page_slug = f"{wanted[0]}-{wanted[-1]}-{len(wanted)}pages"

    def decorate(job):
        safe_id = re.sub(r"[^\w-]", "_", str(job["id"]))
        job.update(pages=wanted, engine=engine, doc=doc_name,
                   artifact=f"page-analysis-{page_slug}-{safe_id}.md")

    def run(job):
        try:
            results = []
            for i, (nums, chunk) in enumerate(chunks):
                if _an_cancel_check(job, f"{job['done']}/{job['total']} chunks "
                                    "analyzed — artifact not written"):
                    return
                result = _ai_chat(cfg, [
                    {"role": "system", "content":
                     "You are analyzing selected pages of a historical "
                     "botanical or medical work for a rare-books cataloguer. "
                     "Identify subjects, named people and works, structure, "
                     "notable claims, and OCR uncertainties. Be concise, "
                     "factual, and do not invent missing context. Reply in "
                     "Markdown."},
                    {"role": "user", "content":
                     f"Work: {meta}\nSelected pages {nums}:\n\n{chunk}"},
                ])
                results.append((nums, result.strip()))
                with _an_jobs_lock:
                    job["done"] = i + 1
                _job_checkpoint(job)

            parts = [f"# Page analysis: {b.get('title') or 'Untitled'}",
                     f"_OCR source: {doc_name}; engine: {engine}_"]
            for nums, result in results:
                label = (str(nums[0]) if len(nums) == 1
                         else f"{nums[0]}-{nums[-1]}")
                parts.extend((f"## Pages {label}", result))
            with _an_write_lock:
                _write_entry_text(
                    bid, f"analysis/{job['artifact']}",
                    "\n\n".join(parts).strip() + "\n")
            _manifest_record(bid, f"analysis/{job['artifact']}",
                             {"kind": "page-analysis", "model": cfg["model"]},
                             [src_input])
            activity("analyzed pages", "book", detail=b.get("title", ""))
            _an_finish(job)
        except Exception as exc:
            log.error("page analysis failed for %s", bid, exc_info=exc)
            _an_finish(job, f"{type(exc).__name__}: {exc}")

    try:
        job = _an_job_start_guarded(
            bid, source_revision, "page-analysis", len(chunks), run, decorate)
    except _AnalyzeSourceChanged:
        return jsonify({"ok": False, "error":
                        "page numbering changed — review the pages and retry"}), 409
    return jsonify({"ok": True, "job": job["id"], "pages": wanted,
                    "engine": engine, "doc": doc_name,
                    "artifact": job["artifact"]})


@app.route("/api/analyze/summarize", methods=["POST"])
def api_analyze_summarize():
    p = request.get_json(silent=True) or {}
    bid = str(p.get("build_id") or "").strip()
    b, err = _an_gate(bid)
    if err:
        return err
    name, text, source_revision = _analyze_doc_snapshot(bid, b)
    pages = _an_pages(text)
    if not pages:
        return jsonify({"ok": False, "error":
                        "no OCR text for this entry — extract or run OCR first"}), 400
    cfg = _ai_cfg()
    chunks = _an_chunks(pages)
    meta = _an_meta_line(b)
    src_input = _manifest_input(bid, f"ocr/{name}")   # hashed at job start

    def run(job):
        try:
            notes = []
            for i, (nums, chunk) in enumerate(chunks):
                if _an_cancel_check(job, f"{job['done']}/{job['total']} sections "
                                    "read — no summary written"):
                    return
                out = _ai_chat(cfg, [
                    {"role": "system", "content":
                     "You are a rare-books cataloguer summarizing a historical "
                     "botanical/medical work from its OCR text. Note the "
                     "subjects covered, structure, notable content, and any "
                     "period context. OCR noise is expected; read through it. "
                     "Be factual and specific. Reply with dense notes."},
                    {"role": "user", "content":
                     f"Work: {meta}\nPages {nums[0]}–{nums[-1]} of the text:\n\n{chunk}"},
                ])
                notes.append(out)
                with _an_jobs_lock:
                    job["done"] = i + 1
                _job_checkpoint(job)
            if _an_cancel_check(job, f"{job['done']}/{job['total']} sections "
                                "read — no summary written"):
                return
            final = _ai_chat(cfg, [
                {"role": "system", "content":
                 "Combine these section notes into one summary of the work "
                 "for a library catalogue: 300-500 words of Markdown — an "
                 "opening paragraph on what the work is, then its scope and "
                 "structure, then notable content. Factual, no invention, "
                 "no header line."},
                {"role": "user", "content":
                 f"Work: {meta}\n\n" + "\n\n---\n\n".join(notes)},
            ])
            with _an_write_lock:
                _save_analyze_summary(bid, final)
            _manifest_record(bid, "summary.md",
                             {"kind": "summarize", "model": cfg["model"]},
                             [src_input])
            activity("summarized", "book", detail=b.get("title", ""))
            _an_finish(job)
        except Exception as exc:
            log.error("summarize failed for %s", bid, exc_info=exc)
            _an_finish(job, f"{type(exc).__name__}: {exc}")

    try:
        job = _an_job_start_guarded(
            bid, source_revision, "summarize", len(chunks) + 1, run)
    except _AnalyzeSourceChanged:
        return jsonify({"ok": False, "error":
                        "page numbering changed — review the pages and retry"}), 409
    return jsonify({"ok": True, "job": job["id"], "chunks": len(chunks),
                    "doc": name})


@app.route("/api/analyze/about", methods=["POST"])
def api_analyze_about():
    """Draft the public About article from the summary + metadata. Refuses to
    overwrite an existing article unless told to — it may be hand-edited."""
    p = request.get_json(silent=True) or {}
    bid = str(p.get("build_id") or "").strip()
    b, err = _an_gate(bid)
    if err:
        return err
    summary = _read_entry_text(bid, "summary.md")
    if not summary.strip():
        return jsonify({"ok": False, "error":
                        "no summary yet — generate one first"}), 400
    if _read_entry_text(bid, "about.md").strip() and not p.get("overwrite"):
        return jsonify({"ok": False, "error": "an About article already "
                        "exists — pass overwrite to replace it"}), 409
    cfg = _ai_cfg()
    meta = _an_meta_line(b)
    # the About draft reads the summary, not the OCR doc — staleness chains:
    # OCR edit -> summary stale -> regenerated summary -> About stale
    src_input = _manifest_input(bid, "summary.md")

    def run(job):
        try:
            if _an_cancel_check(job, "cancelled before the draft call — "
                                "nothing written"):
                return
            out = _ai_chat(cfg, [
                {"role": "system", "content":
                 "Write the About article for a volume in a public digital "
                 "library of rare botanical works. Neutral, archival tone; "
                 "200-400 words of Markdown; describe what the work is, its "
                 "context and significance, and what a reader will find. No "
                 "title header, no invented facts, no bibliographic citation "
                 "block."},
                {"role": "user", "content": f"Work: {meta}\n\nCataloguer's "
                 f"summary:\n{summary}"},
            ], temperature=0.4)
            with _an_write_lock:
                _save_analyze_about(bid, out)
            _manifest_record(bid, "about.md",
                             {"kind": "about", "model": cfg["model"]},
                             [src_input])
            _an_finish(job)
        except Exception as exc:
            _an_finish(job, f"{type(exc).__name__}: {exc}")

    job = _an_job_start(bid, "about", 1, run)
    return jsonify({"ok": True, "job": job["id"]})


@app.route("/api/analyze/categories", methods=["POST"])
def api_analyze_categories():
    """Suggest taxonomy paths for a work; synchronous (one call). Existing
    paths resolve to node ids so the client can assign them directly; novel
    paths come back as proposals."""
    p = request.get_json(silent=True) or {}
    bid = str(p.get("build_id") or "").strip()
    b, err = _an_gate(bid)
    if err:
        return err
    nodes = lib.load_taxonomy()["nodes"]
    vocab = sorted(" › ".join(lib.category_path(nodes, nid))
                   for nid in nodes)
    summary = _read_entry_text(bid, "summary.md")
    _, text = _analyze_doc(bid, b)
    excerpt = summary or "\n".join(list(_an_pages(text).values())[:6])[:8000]
    try:
        out = _ai_json(_ai_cfg(), [
            {"role": "system", "content":
             "You classify works for a digital library of rare botanical and "
             "medical books. Its category taxonomy is hierarchical; paths are "
             "written 'Parent › Child'. Suggest 1-5 categories for the work: "
             "prefer existing paths verbatim; propose a new path only when "
             "nothing fits, reusing an existing parent where possible. Reply "
             "as JSON: {\"suggestions\": [{\"path\": [\"Botany\",\"Herbals\"], "
             "\"reason\": \"...\"}]}"},
            {"role": "user", "content":
             "Existing taxonomy paths:\n" + ("\n".join(vocab) or "(empty)") +
             f"\n\nWork: {_an_meta_line(b)}\n\nWhat is known of its content:\n"
             + excerpt[:9000]},
        ])
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    # resolve each suggested path against the tree, walking name by name
    def resolve(path: list) -> str:
        cur = ""
        for name in path:
            low = str(name or "").strip().lower()
            nxt = next((nid for nid, n in nodes.items()
                        if (n.get("parent") or "") == cur
                        and str(n.get("name", "")).strip().lower() == low), None)
            if not nxt:
                return ""
            cur = nxt
        return cur

    suggestions = []
    for s in (out.get("suggestions") or [])[:8]:
        path = [str(x).strip()[:80] for x in (s.get("path") or []) if str(x).strip()]
        if not path:
            continue
        nid = resolve(path)
        suggestions.append({"path": path, "id": nid, "exists": bool(nid),
                            "reason": str(s.get("reason") or "")[:300]})
    return jsonify({"ok": True, "suggestions": suggestions})


@app.route("/api/analyze/translate", methods=["POST"])
def api_analyze_translate():
    p = request.get_json(silent=True) or {}
    bid = str(p.get("build_id") or "").strip()
    lang = _lang_code(p.get("lang"))
    b, err = _an_gate(bid)
    if err:
        return err
    if not lang:
        return jsonify({"ok": False, "error": "no target language"}), 400
    name, text, source_revision = _analyze_doc_snapshot(bid, b)
    pages = _an_pages(text)
    if not pages:
        return jsonify({"ok": False, "error":
                        "no OCR text for this entry — extract or run OCR first"}), 400
    rel = f"translations/{lang}.txt"
    done_pages = _an_pages(_read_entry_text(bid, rel))
    want = p.get("pages")
    if isinstance(want, list):
        # explicit re-translation: the client names the pages, current or not
        try:
            want = sorted({int(n) for n in want})
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "pages must be integers"}), 400
        todo = [n for n in want if pages.get(n, "").strip()]
        if not todo:
            return jsonify({"ok": False, "error":
                            "none of those pages have source text"}), 400
    elif p.get("mode") == "stale":
        # bring the translation current: pages whose source text changed since
        # they were translated, plus pages never translated at all
        stale = _stale_translation_pages(_load_translation_meta(bid, lang), pages)
        todo = sorted(set(stale) | {n for n in pages if pages[n].strip()
                                    and not done_pages.get(n, "").strip()})
        if not todo:
            return jsonify({"ok": False, "error":
                            "no outdated or missing pages — the translation "
                            "is current"}), 400
    else:
        todo = [n for n in sorted(pages) if pages[n].strip()
                and not done_pages.get(n, "").strip()]
        if not todo:
            return jsonify({"ok": False, "error":
                            "every page with text is already translated"}), 400
    cfg = _ai_cfg()
    meta = _an_meta_line(b)
    src_input = _manifest_input(bid, f"ocr/{name}")   # hashed at job start
    # file-level provenance at job completion; the per-page meta sidecar
    # stays authoritative for page detail
    record = lambda: _manifest_record(     # noqa: E731
        bid, rel, {"kind": "translate", "model": cfg["model"]}, [src_input])

    def run(job):
        try:
            for i, n in enumerate(todo):
                if _an_cancel_check(job, f"{job['done'] - job['errors']} of "
                                    f"{len(todo)} pages translated — "
                                    "saved pages kept"):
                    if job["done"] > job["errors"]:
                        record()           # partial completion, pages saved
                    return
                try:
                    out = _ai_chat(cfg, [
                        {"role": "system", "content":
                         f"Translate this page of a historical work into "
                         f"{lang}. Preserve paragraph breaks and the sense of "
                         f"period language; do not annotate, do not add "
                         f"anything. OCR noise is expected — translate "
                         f"through it. Reply with the translation only."},
                        {"role": "user", "content":
                         f"Work: {meta}\nPage {n}:\n\n{pages[n][:14000]}"},
                    ], temperature=0.2)
                except RuntimeError as exc:
                    with _an_jobs_lock:
                        job["errors"] += 1
                        job["note"] = f"page {n}: {exc}"
                    continue
                finally:
                    with _an_jobs_lock:
                        job["done"] = i + 1
                    _job_checkpoint(job)
                # progressive save under a lock: a partial job loses nothing
                with _an_write_lock:
                    cur = _an_pages(_read_entry_text(bid, rel))
                    cur[n] = out.strip()
                    doc = "\n\n".join(f"--- page {k} ---\n{cur[k]}"
                                      for k in sorted(cur))
                    _write_entry_text(bid, rel, doc + "\n")
                    # provenance sidecar: which source text (by hash) and
                    # model each page was translated from — staleness is a
                    # hash comparison later
                    tm = _load_translation_meta(bid, lang)
                    tm["src"], tm["model"] = name, cfg["model"]
                    tm["pages"][str(n)] = {
                        "sha1": _page_sha(pages[n]),
                        "at": datetime.now(timezone.utc).isoformat(
                            timespec="seconds")}
                    lib.save_json(_translation_meta_path(bid, lang), tm)
            record()
            activity("translated", "book", n=len(todo) - job["errors"],
                     detail=f"{b.get('title', '')} -> {lang}")
            _an_finish(job)
        except Exception as exc:
            log.error("translate failed for %s", bid, exc_info=exc)
            _an_finish(job, f"{type(exc).__name__}: {exc}")

    try:
        job = _an_job_start_guarded(
            bid, source_revision, f"translate:{lang}", len(todo), run)
    except _AnalyzeSourceChanged:
        return jsonify({"ok": False, "error":
                        "page numbering changed — review the pages and retry"}), 409
    return jsonify({"ok": True, "job": job["id"], "pages": len(todo)})


@app.route("/api/analyze/annotate", methods=["POST"])
def api_analyze_annotate():
    p = request.get_json(silent=True) or {}
    bid = str(p.get("build_id") or "").strip()
    b, err = _an_gate(bid)
    if err:
        return err
    name, text, source_revision = _analyze_doc_snapshot(bid, b)
    pages = _an_pages(text)
    if not pages:
        return jsonify({"ok": False, "error":
                        "no OCR text for this entry — extract or run OCR first"}), 400
    cfg = _ai_cfg()
    chunks = _an_chunks(pages, budget=14000)
    meta = _an_meta_line(b)
    src_input = _manifest_input(bid, f"ocr/{name}")   # hashed at job start
    record = lambda: _manifest_record(     # noqa: E731
        bid, "annotations.json",
        {"kind": "annotate", "model": cfg["model"]}, [src_input])

    def run(job):
        try:
            added = 0
            for i, (nums, chunk) in enumerate(chunks):
                if _an_cancel_check(job, f"{job['done']}/{job['total']} chunks "
                                    "annotated — saved notes kept"):
                    if added:
                        record()           # partial completion, notes saved
                    return
                try:
                    out = _ai_json(cfg, [
                        {"role": "system", "content":
                         "You annotate a historical botanical/medical work "
                         "for modern readers. Propose margin notes anchored "
                         "to short verbatim quotes: explain archaic terms, "
                         "identify plants (modern binomials), people, places, "
                         "preparations, and give context worth a note. Only "
                         "genuinely noteworthy passages — 0 to 4 notes per "
                         "page. Reply as JSON: {\"notes\": [{\"page\": N, "
                         "\"quote\": \"exact words from the page\", \"kind\": "
                         "\"term|plant|person|place|context\", \"note\": "
                         "\"...\"}]}"},
                        {"role": "user", "content":
                         f"Work: {meta}\n\n{chunk}"},
                    ])
                except RuntimeError as exc:
                    with _an_jobs_lock:
                        job["errors"] += 1
                        job["note"] = str(exc)
                    continue
                finally:
                    with _an_jobs_lock:
                        job["done"] = i + 1
                    _job_checkpoint(job)
                fresh = []
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                for raw in (out.get("notes") or [])[:40]:
                    try:
                        page = int(raw.get("page"))
                    except (TypeError, ValueError):
                        continue
                    if page not in pages:
                        continue
                    quote = str(raw.get("quote") or "").strip()[:300]
                    body = str(raw.get("note") or "").strip()[:1000]
                    if not body:
                        continue
                    # an anchor that isn't on the page is noise, not an anchor
                    flat = re.sub(r"\s+", " ", pages[page]).lower()
                    if quote and re.sub(r"\s+", " ", quote).lower() not in flat:
                        quote = ""
                    fresh.append({"id": lib.gen_id(), "page": page,
                                  "quote": quote,
                                  "kind": str(raw.get("kind") or "context")[:24],
                                  "body": body, "status": "suggested",
                                  "source": f"ai:{cfg['model']}",
                                  "created_at": now, "updated_at": now})
                if fresh:
                    with _an_notes_lock:
                        doc = _load_annotations(bid)
                        # no duplicate suggestions on re-runs: same page+body
                        seen = {(n.get("page"), n.get("body")) for n in doc["notes"]}
                        doc["notes"].extend(
                            n for n in fresh
                            if (n["page"], n["body"]) not in seen)
                        lib.save_json(_entry_dir(bid) / "annotations.json", doc)
                    added += len(fresh)
            record()
            activity("annotated", "book", n=added, detail=b.get("title", ""))
            _an_finish(job)
        except Exception as exc:
            log.error("annotate failed for %s", bid, exc_info=exc)
            _an_finish(job, f"{type(exc).__name__}: {exc}")

    try:
        job = _an_job_start_guarded(
            bid, source_revision, "annotate", len(chunks), run)
    except _AnalyzeSourceChanged:
        return jsonify({"ok": False, "error":
                        "page numbering changed — review the pages and retry"}), 409
    return jsonify({"ok": True, "job": job["id"], "chunks": len(chunks)})


@app.route("/api/analyze/relevance", methods=["POST"])
def api_analyze_relevance():
    """Assess the work against the user's custom criteria (defined in the
    Analyze tab, stored in settings.relevanceCriteria). The result lands on
    the build record — an internal metric, never published (_volume_row is an
    allowlist and does not carry it)."""
    p = request.get_json(silent=True) or {}
    bid = str(p.get("build_id") or "").strip()
    b, err = _an_gate(bid)
    if err:
        return err
    criteria = [c for c in (_client_settings().get("relevanceCriteria") or [])
                if isinstance(c, dict) and str(c.get("name") or "").strip()]
    if not criteria:
        return jsonify({"ok": False, "error":
                        "no relevance criteria defined yet"}), 400
    summary = _read_entry_text(bid, "summary.md")
    _, text, source_revision = _analyze_doc_snapshot(bid, b)
    excerpt = summary or "\n".join(list(_an_pages(text).values())[:6])[:8000]
    if not excerpt.strip():
        return jsonify({"ok": False, "error":
                        "no summary or OCR text to assess from"}), 400
    cfg = _ai_cfg()
    crit_lines = "\n".join(
        f"- {c['name']}: {str(c.get('description') or '').strip()}"
        for c in criteria)
    meta = _an_meta_line(b)

    def run(job):
        try:
            if _an_cancel_check(job, "cancelled before assessment — "
                                "nothing written"):
                return
            out = _ai_json(cfg, [
                {"role": "system", "content":
                 "Assess how relevant a historical work is to a private "
                 "collection, against the collector's own criteria. Score "
                 "each criterion 0-10 with a one-sentence rationale, then an "
                 "overall 0-10. Be honest — a poor fit scores low. Reply as "
                 "JSON: {\"criteria\": [{\"name\": \"...\", \"score\": N, "
                 "\"rationale\": \"...\"}], \"overall\": N, "
                 "\"summary\": \"one sentence\"}"},
                {"role": "user", "content":
                 f"Criteria:\n{crit_lines}\n\nWork: {meta}\n\n"
                 f"What is known of it:\n{excerpt[:9000]}"},
            ])
            clamp = lambda v: max(0, min(10, int(v)))   # noqa: E731
            scored, by_name = [], {str(c.get("name", "")).strip().lower(): c
                                   for c in (out.get("criteria") or [])
                                   if isinstance(c, dict)}
            for c in criteria:
                got = by_name.get(c["name"].strip().lower(), {})
                try:
                    score = clamp(got.get("score"))
                except (TypeError, ValueError):
                    score = 0
                scored.append({"id": str(c.get("id") or ""), "name": c["name"],
                               "score": score,
                               "rationale": str(got.get("rationale") or "")[:400]})
            try:
                overall = clamp(out.get("overall"))
            except (TypeError, ValueError):
                overall = 0
            result = {"assessed_at": datetime.now(timezone.utc)
                      .isoformat(timespec="seconds"),
                      "model": cfg["model"], "overall": overall,
                      "summary": str(out.get("summary") or "")[:400],
                      "criteria": scored}
            with _builds_lock:
                fresh = lib.load_json(BUILDS_PATH, {})
                row = fresh.get(bid)
                if row is not None:
                    row["relevance"] = result
                    row["updated_at"] = _build_updated_at(row.get("updated_at"))
                    lib.save_json(BUILDS_PATH, fresh)
            _an_finish(job)
        except Exception as exc:
            log.error("relevance failed for %s", bid, exc_info=exc)
            _an_finish(job, f"{type(exc).__name__}: {exc}")

    try:
        job = _an_job_start_guarded(
            bid, source_revision, "relevance", 1, run)
    except _AnalyzeSourceChanged:
        return jsonify({"ok": False, "error":
                        "page numbering changed — review the pages and retry"}), 409
    return jsonify({"ok": True, "job": job["id"]})


# --- smart check: extract real metadata from a book's own PDF --------------------
# Any book record with a reachable PDF can be "smart checked": the PDF is
# fetched (remote URLs land in the downloads/cache temp store), its front
# matter is OCRed page by page with Mistral until a title page and a copyright
# page have been seen, and the OCR text goes to the configured AI provider
# (DeepSeek by default — the same Mistral -> DeepSeek chain as a phone
# capture) for strict-JSON bibliographic extraction. The result is held as a
# PENDING overlay in output/smart_checks.json: nothing touches the book's real
# metadata until the client explicitly bakes it in, and retired records move
# to a capped `resolved` list so every extraction stays auditable. The store
# is deliberately device-local (not in the store_sync map) — provisional data
# has no business syncing; bakes travel through the normal record stores.

SMART_CHECKS_PATH = lib.OUTPUT_DIR / "smart_checks.json"
_sc_lock = threading.Lock()          # every smart_checks.json read-modify-write
_sc_jobs: dict = {}
_sc_jobs_lock = threading.Lock()
_sc_start_lock = threading.Lock()    # dedupe-scan + job insert, atomically

_SC_SCAN_CAP = 15        # pages considered from the front (blanks included)
_SC_OCR_CAP = 8          # pages actually sent to OCR
_SC_WIDTH = 1400         # render width; the OCR queue's default
_SC_RESOLVED_KEEP = 400  # audit-trail records kept after bake/dismiss
_SC_PENDING_CAP = 200    # un-baked overlays kept; oldest are dropped first
_SC_JOBS_KEEP = 20       # finished entries kept in the per-kind registry

# extraction vocabulary (capture.FIELDS) -> each record store's field names
_SC_FIELD_MAPS = {
    "whl": {"title": "title", "subtitle": "subtitle", "author": "authors",
            "year": "year", "publisher": "publisher", "language": "language"},
    "build": {"title": "title", "subtitle": "subtitle", "author": "authors",
              "year": "year", "publisher": "publisher",
              "city": "publisher_city", "edition": "edition",
              "volume": "volume", "language": "language"},
    "checked": {f: f for f in ("title", "subtitle", "author", "publisher",
                               "city", "year", "edition", "volume",
                               "language")},
}
_SC_FIELD_MAPS["manual"] = _SC_FIELD_MAPS["checked"]

# The phone capture's extraction prompt, reframed for digitized front matter.
# Derived (not copied) so the strict-JSON field contract can never drift from
# capture_pipeline / the Android app; if the upstream wording changes, the
# replace no-ops and the prompt is still valid.
_SC_PROMPT = capture._EXTRACT_PROMPT.replace(
    "OCR text from photos of a book's title page and/or copyright page",
    "OCR text from the first pages of a digitized copy of a book "
    "(cover, title page, copyright page, other front matter)")

# cheap textual signals deciding when the front-matter scan can stop early
_SC_COPYRIGHT_RE = re.compile(
    r"copyright|©|all rights reserved|printed in|first published"
    r"|impression|printing|entered according to act", re.I)
_SC_YEAR_RE = re.compile(r"\b(1[4-9]\d{2}|20\d{2})\b")
_SC_IMPRINT_RE = re.compile(
    r"publish|press\b|verlag|editore|editions?\b|librair|imprim"
    r"|printed for|book (?:co|company)|& ?co\b|and company|sons\b|brothers\b",
    re.I)


def _sc_parse_target(raw) -> tuple[str, str]:
    """A target names one book record: '<kind>:<ident>' where kind is one of
    whl / build / manual / checked (idents may themselves contain colons —
    checked keys are 'source:idx'). Raises ValueError on anything else."""
    t = str(raw or "").strip()
    kind, _, ident = t.partition(":")
    if kind not in _SC_FIELD_MAPS or not ident:
        raise ValueError("bad target")
    return kind, ident


def _sc_scan_pages(pdf: Path) -> list[int]:
    """1-based front-matter candidates: the first _SC_SCAN_CAP pages minus
    visually blank ones (_blank_pages' ink + text-layer test), so OCR calls
    aren't spent on empty versos."""
    import fitz
    pages = []
    doc = fitz.open(str(pdf))
    try:
        for i in range(min(doc.page_count, _SC_SCAN_CAP)):
            pg = doc[i]
            zoom = 160 / max(1.0, pg.rect.width)
            pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                                colorspace="gray")
            samples = pix.samples
            inked = sum(1 for v in samples if v < 200)
            if (inked / max(1, len(samples)) >= 0.003
                    or (pg.get_text() or "").strip()):
                pages.append(i + 1)
    finally:
        doc.close()
    return pages


def _sc_ocr_page(pdf: Path, page: int, key: str) -> str:
    """Render one page and OCR it with Mistral — the OCR queue's exact chain."""
    png = _ocr_page_png(pdf, page, _SC_WIDTH)
    pages = capture.mistral_ocr_pages(png, key)
    return "\n\n".join(p.get("markdown", "") for p in pages).strip()


def _sc_extract(ocr_text: str) -> tuple[dict, str]:
    """OCR text -> normalized bibliography + the model that produced it.
    DeepSeek (Settings > AI) when a key is set — mirroring the phone app's
    'DeepSeek by default' — else Mistral's own extraction."""
    cfg = _ai_cfg()
    if cfg["key"]:
        obj = _ai_json(cfg, [{"role": "user",
                              "content": _SC_PROMPT + ocr_text[:12000]}],
                       temperature=0.0)
        return capture.normalize_bibliography(obj), cfg["model"]
    mkey = str(_client_settings().get("mistralKey") or "").strip()
    if not mkey:
        raise RuntimeError("no AI key and no Mistral key — set one in "
                           "Settings > AI or Settings > OCR")
    return capture.extract_bibliography(ocr_text, mkey), capture.EXTRACT_MODEL


def _sc_map_fields(kind: str, fields: dict) -> dict:
    """Extraction vocabulary -> the target store's. Blank values never map:
    a smart check may fill or correct a field, never erase one."""
    out = {}
    for src, dst in _SC_FIELD_MAPS[kind].items():
        v = str(fields.get(src) or "").strip()
        if v:
            out[dst] = v
    return out


def _sc_store_locked() -> dict:
    doc = lib.load_json(SMART_CHECKS_PATH, {})
    if not isinstance(doc, dict):
        doc = {}
    if not isinstance(doc.get("pending"), dict):
        doc["pending"] = {}
    if not isinstance(doc.get("resolved"), list):
        doc["resolved"] = []
    return doc


def _sc_mutate(fn):
    with _sc_lock:
        doc = _sc_store_locked()
        out = fn(doc)
        lib.save_json(SMART_CHECKS_PATH, doc)
    return out


def _sc_job_new(target: str, label: str) -> dict:
    job = {"id": lib.gen_id(set(_sc_jobs) | set(_jobs)), "target": target,
           "build_id": "", "kind": "smartcheck", "done": 0, "total": 0,
           "errors": 0, "status": "running", "error": "", "note": ""}
    kind, ident = _sc_parse_target(target)
    if kind == "build":
        job["build_id"] = ident
    with _sc_jobs_lock:
        _sc_jobs[job["id"]] = job
    _job_track(job, "smartcheck", label=label)
    return job


def _sc_job_start(target: str, label: str, run) -> dict:
    """Create and start a smart-check job (a seam tests replace to run the
    worker inline)."""
    job = _sc_job_new(target, label)
    threading.Thread(target=run, args=(job,), daemon=True).start()
    return job


def _sc_finish(job: dict, error: str = "") -> None:
    with _sc_jobs_lock:
        job["error"] = error
        status = "error" if error else (
            "done (with errors)" if job["errors"] else "done")
    _job_transition(job, status)
    # drop older finished entries from the per-kind registry — the unified
    # registry keeps the durable snapshot, and polls fall back to it
    with _sc_jobs_lock:
        done = sorted((j for j in _sc_jobs.values()
                       if j.get("state") not in _JOB_ACTIVE),
                      key=lambda j: str(j.get("finished_at") or ""),
                      reverse=True)
        for old in done[_SC_JOBS_KEEP:]:
            _sc_jobs.pop(str(old.get("id")), None)


def _sc_run(job: dict, spec: dict) -> None:
    """The whole smart check for one book on its own daemon thread: resolve
    the PDF -> OCR front-matter pages until both a title-page signal and a
    copyright signal have been seen -> extract fields -> file the result as
    a PENDING record. The book's real metadata is never touched here."""
    target = spec["target"]
    kind, _ident = _sc_parse_target(target)
    try:
        mkey = str(_client_settings().get("mistralKey") or "").strip()
        if not mkey:
            raise RuntimeError("Mistral API key not configured (Settings > OCR)")
        pdf = spec.get("pdf_path")
        if pdf is None:
            # note only — a transition would clobber a 'cancelling' status
            with _sc_jobs_lock:
                job["note"] = "downloading PDF"
            _job_checkpoint(job, force=True)
            pdf = _remote_pdf_cache(spec["url"])   # ValueError on failure
            with _sc_jobs_lock:
                job["note"] = ""
        pdf = Path(pdf)
        candidates = _sc_scan_pages(pdf)
        if not candidates:
            raise RuntimeError(
                f"no readable pages in the first {_SC_SCAN_CAP} pages")
        planned = candidates[:_SC_OCR_CAP]
        with _sc_jobs_lock:
            job["total"] = len(planned) + 1        # +1 = the extraction step
            job["note"] = ""
        texts: dict[int, str] = {}
        titleish = copyrightish = False
        for i, n in enumerate(planned):
            if _an_cancel_check(job, "cancelled — nothing was written"):
                return
            try:
                text = _sc_ocr_page(pdf, n, mkey)
            except Exception as exc:
                text = ""
                with _sc_jobs_lock:
                    job["errors"] += 1
                    job["note"] = f"page {n}: {type(exc).__name__}"
            if text:
                texts[n] = text
                titleish = titleish or bool(_SC_YEAR_RE.search(text)
                                            or _SC_IMPRINT_RE.search(text))
                copyrightish = copyrightish or bool(
                    _SC_COPYRIGHT_RE.search(text))
            with _sc_jobs_lock:
                job["done"] = i + 1
            _job_checkpoint(job)
            # both signals in hand: the imprint is covered, stop spending
            if titleish and copyrightish and len(texts) >= 2:
                break
        ocr_text = "\n\n".join(f"--- page {n} ---\n{texts[n]}"
                               for n in sorted(texts))
        if not ocr_text.strip():
            raise RuntimeError("OCR produced no text from the front matter")
        if _an_cancel_check(job, "cancelled — nothing was written"):
            return
        got, model = _sc_extract(ocr_text)
        extra = got.pop("extra", {}) or {}
        # an unparseable/empty AI reply must fail loudly — filing an all-blank
        # record would render as "the PDF agrees with this record"
        if not extra and not any(str(v or "").strip() for v in got.values()):
            raise RuntimeError("extraction returned no fields — the AI reply "
                               "could not be parsed; try again")
        if _an_cancel_check(job, "cancelled — nothing was written"):
            return
        record = {
            "target": target, "kind": kind,
            "label": str(spec.get("label") or ""),
            "fields": got, "extra": extra,
            "mapped": _sc_map_fields(kind, got),
            "ocr_text": ocr_text[:60000],
            "pages_ocred": sorted(texts),
            "pdf": spec.get("pdf_ref") or {},
            "engine": {"ocr": capture.OCR_MODEL, "extract": model},
            "created_at": datetime.now(timezone.utc)
                          .isoformat(timespec="seconds"),
            "job_id": job["id"],
        }
        def put(doc):
            pend = doc["pending"]
            pend[target] = record
            if len(pend) > _SC_PENDING_CAP:      # oldest overlays age out
                for k in sorted(pend, key=lambda k: str(
                        (pend[k] or {}).get("created_at") or ""))[
                        :len(pend) - _SC_PENDING_CAP]:
                    pend.pop(k, None)
        _sc_mutate(put)
        with _sc_jobs_lock:
            job["done"] = job["total"]
        activity("smart-checked", "Book metadata",
                 detail=record["label"] or target)
        _sc_finish(job)
    except Exception as exc:
        log.error("smart check failed for %s", target, exc_info=exc)
        _sc_finish(job, f"{type(exc).__name__}: {exc}")


@app.route("/api/smartcheck/run", methods=["POST"])
def api_smartcheck_run():
    """Start a smart check for one book record.

    Body: ``{target, pdf?, url?, label?}`` — ``target`` is
    ``whl:<idx> | build:<id> | manual:<id> | checked:<key>``; ``pdf`` is a
    local path (DATA_ROOT-relative or absolute), ``url`` a remote PDF.
    Returns the job to poll; a second request for a book whose check is
    still running returns that same job (``already: true``).
    """
    p = request.get_json(silent=True) or {}
    try:
        kind, ident = _sc_parse_target(p.get("target"))
    except ValueError:
        return jsonify({"ok": False, "error": "bad target"}), 400
    target = f"{kind}:{ident}"
    label = str(p.get("label") or "").strip()[:120]
    # server-owned targets must exist; checked books live in client_state
    if kind == "build":
        b = lib.load_json(BUILDS_PATH, {}).get(ident)
        if not isinstance(b, dict):
            return jsonify({"ok": False, "error": "unknown build"}), 404
        label = label or str(b.get("title") or "")
    elif kind == "manual":
        entry = lib.load_json(lib.MANUAL_ENTRIES_PATH, {}).get(ident)
        if not isinstance(entry, dict):
            return jsonify({"ok": False, "error": "unknown manual entry"}), 404
        label = label or str(entry.get("title") or "")
    elif kind == "whl":
        try:
            widx = int(ident)
        except ValueError:
            return jsonify({"ok": False, "error": "bad WHL index"}), 400
        if widx >= 0 and widx >= len(_load_whl_base()):
            return jsonify({"ok": False, "error": "unknown WHL row"}), 404
        if widx < 0:      # negative idx = a row added through corrections
            corr = lib.load_json(WHL_CORRECTIONS_PATH, {})
            if -widx > len(corr.get("added") or []):
                return jsonify({"ok": False, "error": "unknown WHL row"}), 404
    elif kind == "checked" and ":" not in ident:
        return jsonify({"ok": False, "error": "bad checked key"}), 400
    raw_pdf = str(p.get("pdf") or "").strip()
    url = str(p.get("url") or "").strip()
    spec = {"target": target, "label": label, "pdf_path": None, "url": url,
            "pdf_ref": {}}
    if raw_pdf:
        lp = _resolve_local(raw_pdf)
        if lp is None or lp.suffix.lower() != ".pdf" or not lp.is_file():
            return jsonify({"ok": False, "error": "PDF not found"}), 404
        spec["pdf_path"] = lp
        spec["pdf_ref"] = {"path": raw_pdf}
    elif url:
        if not url.lower().startswith(("http://", "https://")):
            return jsonify({"ok": False, "error": "not an http(s) URL"}), 400
        spec["pdf_ref"] = {"url": url}     # fetched on the worker thread
    else:
        return jsonify({"ok": False, "error": "pdf or url required"}), 400
    # one live job per book — a duplicate click joins the running check.
    # _sc_start_lock makes the scan-then-insert atomic against a concurrent
    # POST for the same target (the insert re-takes _jobs_lock internally).
    with _sc_start_lock:
        with _jobs_lock:
            for j in _jobs.values():
                if (j.get("kind") == "smartcheck" and j.get("target") == target
                        and j.get("state") in _JOB_ACTIVE):
                    return jsonify({"ok": True, "already": True,
                                    "job": dict(_job_public(j), target=target)})
        job = _sc_job_start(target, label, lambda jb: _sc_run(jb, spec))
    return jsonify({"ok": True, "job": dict(job)})


@app.route("/api/smartcheck/job/<job_id>")
def api_smartcheck_job(job_id: str):
    with _sc_jobs_lock:
        job = _sc_jobs.get(job_id)
        if job is not None:
            return jsonify(dict(job))
    # a restart dropped the worker: the persisted registry still knows it
    with _jobs_lock:
        gone = _jobs.get(job_id)
    if gone is None:
        abort(404)
    return jsonify(_job_public(gone))


@app.route("/api/smartcheck")
def api_smartcheck_list():
    """Every pending (un-baked) smart-check record, keyed by target.

    Records whose build/manual target no longer exists are retired here as
    ``orphaned`` — a deleted book must not leave an immortal overlay behind
    (its wand is the only dismiss surface, and it's gone with the row)."""
    builds = manuals = None

    def prune(doc):
        nonlocal builds, manuals
        dead = []
        for t in doc["pending"]:
            k, _, ident = str(t).partition(":")
            if k == "build":
                if builds is None:
                    builds = lib.load_json(BUILDS_PATH, {})
                if ident not in builds:
                    dead.append(t)
            elif k == "manual":
                if manuals is None:
                    manuals = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
                if ident not in manuals:
                    dead.append(t)
        for t in dead:
            rec = doc["pending"].pop(t)
            if isinstance(rec, dict):
                rec["resolved"] = {
                    "action": "orphaned", "applied": {},
                    "at": datetime.now(timezone.utc)
                          .isoformat(timespec="seconds")}
                doc["resolved"].append(rec)
        del doc["resolved"][:-_SC_RESOLVED_KEEP]
        return dict(doc["pending"])

    return jsonify({"ok": True, "pending": _sc_mutate(prune)})


@app.route("/api/smartcheck/resolve", methods=["POST"])
def api_smartcheck_resolve():
    """Retire a pending record: ``baked`` (the client applied it to the book
    through the normal edit endpoints) or ``dismissed``. Retired records move
    to the store's capped ``resolved`` list — the audit trail of what was
    extracted, what was applied, and when."""
    p = request.get_json(silent=True) or {}
    target = str(p.get("target") or "").strip()
    action = str(p.get("action") or "").strip()
    if action not in ("baked", "dismissed"):
        return jsonify({"ok": False, "error": "bad action"}), 400
    applied = p.get("applied") if isinstance(p.get("applied"), dict) else {}

    def fn(doc):
        rec = doc["pending"].pop(target, None)
        if rec is None:
            return None
        rec["resolved"] = {
            "action": action, "applied": applied,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        doc["resolved"].append(rec)
        del doc["resolved"][:-_SC_RESOLVED_KEEP]
        return rec

    rec = _sc_mutate(fn)
    if rec is None:
        return jsonify({"ok": False, "error": "no pending smart check"}), 404
    if action == "baked":
        activity("baked", "Smart check",
                 detail=str(rec.get("label") or target))
    return jsonify({"ok": True})


# --- publishing a volume to the cloud library ---------------------------------
# The Editor's old "Upload to WHL" only flipped a status field; there was no WHL
# write API and nothing ever left the machine. A volume now goes to object
# storage, and its metadata to Supabase, where the website reads it.
#
# Two stores, because the free Supabase tier is 1 GB and one local scan is
# 129 MB: R2 when configured (metadata still in Supabase), otherwise the
# Supabase `volumes` bucket. The volumes row records whichever URL resulted, so
# the reader never needs to know which was used.

_publish_lock = threading.Lock()
_publish: dict = {"running": False, "build": "", "stage": "idle", "sent": 0,
                  "total": 0, "error": "", "url": "", "slug": "", "note": "",
                  "job": ""}


def _r2_cfg() -> dict:
    s = _client_settings()
    return {"account": str(s.get("r2Account") or "").strip(),
            "bucket": str(s.get("r2Bucket") or "").strip(),
            "key_id": str(s.get("r2KeyId") or "").strip(),
            "secret": str(s.get("r2Secret") or "").strip(),
            "public_base": str(s.get("r2PublicBase") or "").strip()}


def _volume_row(b: dict, slug: str, url: str, path: str, size: int, actor: str,
                 thumb_url: str = "", thumb_path: str = "") -> dict:
    num = lambda v: int(v) if str(v or "").strip().isdigit() else None   # noqa: E731
    # Categories publish as resolved taxonomy paths; the flat text column is
    # their rendering (and stays inside the fts index). Builds that predate
    # the taxonomy fall back to their legacy free text. Note the allowlist
    # nature of this row: internal fields (relevance, bundle) never enter it.
    paths = lib.category_paths(lib.load_taxonomy()["nodes"],
                               b.get("category_ids"))
    cats = lib.categories_text(paths) if paths else (b.get("categories") or "")
    return {"slug": slug, "title": b.get("title") or "",
            "subtitle": b.get("subtitle") or "", "authors": b.get("authors") or "",
            "year": num(b.get("year")), "publisher": b.get("publisher") or "",
            "publisher_city": b.get("publisher_city") or "",
            "edition": b.get("edition") or "", "volume": b.get("volume") or "",
            "group_id": b.get("group_id") or "",
            "language": b.get("language") or "",
            "pages": num(b.get("pages")), "categories": cats,
            "category_paths": paths,
            "description": b.get("description") or "",
            "source_url": b.get("source_url") or b.get("pdf_source") or "",
            "copyright_status": _RIGHTS_PUBLIC.get(b.get("rights") or "", ""),
            "pdf_url": url, "pdf_path": path, "pdf_bytes": size,
            "thumbnail_url": thumb_url, "thumbnail_path": thumb_path,
            "uploaded_by_name": actor,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _bundle_artifacts(bid: str, b: dict) -> dict:
    """Everything build.bundle says should publish, read once: the About
    text, page texts (original and per-language translations), approved
    notes — plus the assets manifest the volumes row carries so the site
    knows what exists without probing."""
    bundle = _clean_bundle(b.get("bundle"))
    out = {"about": "", "pages": {}, "notes": [], "assets": {}}
    if bundle["about"]:
        out["about"] = _read_entry_text(bid, "about.md").strip()
        if out["about"]:
            out["assets"]["about"] = True
    if bundle["pages_text"]:
        _, text = _analyze_doc(bid, b)
        pages = {n: t for n, t in _an_pages(text).items() if t.strip()}
        if pages:
            out["pages"][""] = pages
            out["assets"]["pages"] = len(pages)
    langs = {}
    for lang in bundle["translations"]:
        pages = {n: t for n, t in _an_pages(
            _read_entry_text(bid, f"translations/{lang}.txt")).items()
            if t.strip()}
        if pages:
            out["pages"][lang] = pages
            langs[lang] = len(pages)
    if langs:
        out["assets"]["translations"] = langs
    if bundle["annotations"]:
        out["notes"] = [n for n in _load_annotations(bid)["notes"]
                        if n.get("status") == "approved"]
        if out["notes"]:
            out["assets"]["notes"] = len(out["notes"])
    return out


def _rights_artifacts(bid: str, b: dict) -> tuple[dict, bool]:
    """The bundle the rights decision actually allows out (art, withheld).
    The About article is the curator's own writing and always publishes; the
    book's words — page text, translations, notes (verbatim quotes) — only
    with a permitting decision. _publish_bundle's pruning then removes any
    text rows a previous publish sent."""
    art = _bundle_artifacts(bid, b)
    if b.get("rights") in _RIGHTS_TEXT_OK:
        return art, False
    withheld = bool(art["pages"] or art["notes"])
    art["pages"], art["notes"] = {}, []
    art["assets"] = {k: v for k, v in art["assets"].items()
                     if k not in ("pages", "translations", "notes")}
    return art, withheld


def _publish_preview_thumb(bid: str, b: dict) -> str:
    """A local thumbnail URL for an uploaded build when cloud data is absent."""
    source = str(b.get("thumbnail_source") or "")
    page = re.match(r"^page:(\d+)$", source)
    pdf = str(b.get("pdf_file") or "").strip()
    if page and pdf:
        return ("/api/pdf/pageimg?path=" + urllib.parse.quote(pdf, safe="")
                + "&page=" + page.group(1) + "&w=640")
    image = re.match(r"^image:(.+)$", source)
    if image:
        return (f"/api/builds/{urllib.parse.quote(bid, safe='')}/ocr/images/"
                + urllib.parse.quote(image.group(1), safe=""))
    return ""


def _local_preview_assets(bid: str, b: dict) -> dict:
    """Cheap availability hints for the catalogue tree's offline fallback.

    The full publish bundler parses every OCR/translation page. A catalogue
    refresh must not do that for every uploaded book merely to draw badges.
    """
    bundle = _clean_bundle(b.get("bundle"))
    assets = {}
    if bundle["about"] and _read_entry_text(bid, "about.md").strip():
        assets["about"] = True
    if bundle["annotations"]:
        count = sum(1 for n in _load_annotations(bid)["notes"]
                    if n.get("status") == "approved")
        if count:
            assets["notes"] = count
    return assets


def _local_publish_rows(builds: dict | None = None) -> list[dict]:
    """Website-shaped rows for builds this checkout remembers publishing."""
    builds = builds if isinstance(builds, dict) else \
        lib.load_json(BUILDS_PATH, {})
    rows = []
    for bid, b in builds.items():
        slug = str(b.get("published_slug") or "").strip()
        if b.get("status") != "uploaded" or not slug:
            continue
        pdf = _resolve_local(str(b.get("pdf_file") or ""))
        size = pdf.stat().st_size if pdf and pdf.is_file() else 0
        row = _volume_row(b, slug, "", "", size, "")
        row["updated_at"] = b.get("updated_at") or row["updated_at"]
        row["assets"] = _local_preview_assets(str(bid), b)
        row["local_build_id"] = str(bid)
        row["preview_thumbnail"] = _publish_preview_thumb(str(bid), b)
        rows.append(row)
    return rows


def _public_volume_rows(cfg: dict) -> list[dict]:
    """Read the complete public catalogue in bounded PostgREST pages."""
    rows = []
    page_size = 1000
    offset = 0
    while True:
        batch = sbase._rest(
            cfg, "GET", "volumes?select=*&order=title.asc,slug.asc"
            f"&limit={page_size}&offset={offset}") or []
        if not isinstance(batch, list):
            raise RuntimeError("online catalogue returned an invalid response")
        rows.extend(x for x in batch if isinstance(x, dict))
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def _public_preview_urls(cfg: dict, row: dict) -> dict:
    """Resolve the site's dual URL/path asset fields for a local preview."""
    item = dict(row)
    base = str(cfg.get("url") or "").rstrip("/")
    storage = f"{base}/storage/v1/object/public/volumes/"
    for url_key, path_key in (("pdf_url", "pdf_path"),
                              ("thumbnail_url", "thumbnail_path")):
        path = str(item.get(path_key) or "").strip()
        if base and path and not item.get(url_key):
            item[url_key] = storage + urllib.parse.quote(path, safe="/")
    return item


@app.route("/api/publish/catalog")
def api_publish_catalog():
    """The website catalogue, or local uploads when it cannot be reached.

    This deliberately uses the public project key, never the owner-only
    service credential. If the network/catalogue is unavailable, builds that
    this checkout successfully uploaded still provide a useful offline tree.
    """
    warning = ""
    cloud_rows = []
    cloud_ok = False
    cfg = _auth_cfg()
    if cfg:
        try:
            cloud_rows = [_public_preview_urls(cfg, row)
                          for row in _public_volume_rows(cfg)]
            cloud_ok = True
            if cloud_rows and any("group_id" not in row or "volume" not in row
                                  for row in cloud_rows):
                warning = ("Online catalogue schema is missing book-set metadata; "
                           "set grouping is temporarily unavailable.")
        except Exception as exc:
            warning = f"Online catalogue unavailable: {exc}"
    else:
        warning = "Online catalogue unavailable; showing local uploads."

    # A successful public read is authoritative, including an empty result.
    # Mixing in local builds here could preview stale edits or an upload that
    # has since been unpublished. Local rows are strictly an offline fallback.
    entries = cloud_rows if cloud_ok else _local_publish_rows()
    entries = [row for row in entries if str(row.get("slug") or "").strip()]
    entries.sort(key=lambda r: (str(r.get("title") or "").casefold(),
                                str(r.get("volume") or ""),
                                str(r.get("slug") or "")))
    source = "cloud" if cloud_ok else "local"
    site_url = (str(_client_settings().get("cloudSiteUrl") or "").strip().rstrip("/")
                or cloud_defaults.WEBSITE_URL)
    return jsonify({"ok": True, "entries": entries, "source": source,
                    "warning": warning, "site_url": site_url})


@app.route("/api/publish/preview/<slug>")
def api_publish_preview(slug: str):
    """Publishable About text and notes for one catalogue volume."""
    slug = str(slug or "").strip()[:240]
    builds = lib.load_json(BUILDS_PATH, {})
    match = next(((bid, b) for bid, b in builds.items()
                  if b.get("status") == "uploaded"
                  and str(b.get("published_slug") or "") == slug), None)
    about, notes, warning, source = "", [], "", "local"
    cloud_ok = False
    cfg = _auth_cfg()
    if cfg and slug:
        q = urllib.parse.quote(slug, safe="")
        try:
            texts = sbase._rest(
                cfg, "GET", f"volume_texts?slug=eq.{q}&kind=eq.about"
                "&select=body,lang&order=lang.asc&limit=1") or []
            remote_notes = sbase._rest(
                cfg, "GET", f"volume_notes?slug=eq.{q}"
                "&select=note_id,page,quote,kind,body"
                "&order=page.asc,note_id.asc") or []
            if not isinstance(texts, list) or not isinstance(remote_notes, list):
                raise RuntimeError("online preview returned an invalid response")
            if texts:
                about = str(texts[0].get("body") or "")
            notes = [n for n in remote_notes if isinstance(n, dict)]
            cloud_ok = True
            source = "cloud"
        except Exception as exc:
            warning = f"Online preview details unavailable: {exc}"
    # Empty cloud rows are authoritative. Only consult a local bundle if the
    # public catalogue was unavailable, never to fill a successful response.
    if match and not cloud_ok:
        bid, build = match
        bundle = _clean_bundle(build.get("bundle"))
        if bundle["about"]:
            about = _read_entry_text(str(bid), "about.md").strip()
        if bundle["annotations"]:
            notes = [{"note_id": str(n.get("id") or ""),
                      "page": int(n.get("page") or 0),
                      "quote": str(n.get("quote") or ""),
                      "kind": str(n.get("kind") or ""),
                      "body": str(n.get("body") or "")}
                     for n in _load_annotations(str(bid))["notes"]
                     if n.get("status") == "approved"]
    return jsonify({"ok": True, "about": about, "notes": notes,
                    "source": source, "warning": warning})


# The website's textsearch.js folds the same glyphs client-side; the two maps
# must stay identical (tests/test_page_search.py runs the same vectors as
# tests/textsearch.test.js). U+FB05 is the long-s + t ligature, so it lands
# on "st" like every other long s.
_SEARCH_LIGATURES = {
    "\u017f": "s",                                     # long s
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl",
    "\ufb03": "ffi", "\ufb04": "ffl",
    "\ufb05": "st", "\ufb06": "st",
    "\u00e6": "ae", "\u0153": "oe",                 # ae / oe ligature vowels
}
# JavaScript's \s, character for character, so the desktop's normalization
# collapses exactly the runs the client's does (Python's str.isspace()
# differs at the margins: \x1c-\x1f and \x85 in, U+FEFF out).
_SEARCH_SPACE = frozenset(
    "\t\n\v\f\r \u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff")


def _search_fold_char(c: str) -> str:
    """One character -> its folded form: lowercase, ligature-expanded, then
    NFD with the combining marks stripped ("\u00fa" -> "u"). May emit
    0..3 characters."""
    lower = c.lower()
    base = _SEARCH_LIGATURES.get(lower, lower)
    return "".join(ch for ch in unicodedata.normalize("NFD", base)
                   if not "\u0300" <= ch <= "\u036f")


def _search_normalize(text: str | None) -> str:
    """volume_pages.search_body — the normalized search layer (issue #139).

    A faithful port of buildSearchIndex in website/assets/textsearch.js,
    minus the offset map: lowercase, long s and ligatures expanded,
    diacritics stripped, hyphenated line breaks joined, whitespace
    collapsed. The desktop owns this normalization; the verbatim `body` is
    a separate layer and is never altered.
    """
    s = str(text or "")
    n = len(s)
    out: list[str] = []
    i = 0
    while i < n:
        c = s[i]
        # A hyphen carried across a line break is a typesetting artefact,
        # not a character of the word: "phy-" + newline + "sick" folds to
        # "physick". The soft hyphen (U+00AD) and the dedicated hyphen
        # (U+2010) count too.
        if c in "-\u00ad\u2010":
            j = i + 1
            while j < n and s[j] in " \t\r":
                j += 1
            if j < n and s[j] == "\n":
                j += 1
                while j < n and s[j] in _SEARCH_SPACE:
                    j += 1
                i = j
                continue
            if c == "\u00ad":     # a soft hyphen is invisible anywhere
                i += 1
                continue
        if c in _SEARCH_SPACE:
            j = i + 1
            while j < n and s[j] in _SEARCH_SPACE:
                j += 1
            if out and out[-1] != " ":   # collapse runs, never lead
                out.append(" ")
            i = j
            continue
        folded = _search_fold_char(c)
        if folded:                       # 0 chars: a bare combining mark
            out.append(folded)
        i += 1
    if out and out[-1] == " ":           # never trail either
        out.pop()
    return "".join(out)


def _publish_bundle(cloud: dict, slug: str, art: dict) -> None:
    """Upsert the bundle's artifacts and prune what left it, so a republish
    converges on exactly what the bundle says. Runs after the volumes row —
    an artifact failure leaves a published book missing extras, not an
    orphaned object."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    q = urllib.parse.quote

    if art["about"]:
        sbase.upsert_rows(cloud, "volume_texts", "slug,kind,lang",
                          [{"slug": slug, "kind": "about", "lang": "",
                            "body": art["about"], "updated_at": now}])
    else:
        sbase.delete_rows(cloud, "volume_texts",
                          f"slug=eq.{q(slug)}&kind=eq.about")

    kept = sorted(art["pages"])
    if kept:
        # body is the verbatim reading; search_body is the normalized layer
        # docs/cloud/migrations/003_page_search.sql indexes (issue #139).
        rows = [{"slug": slug, "lang": lang, "page": n, "body": t,
                 "search_body": _search_normalize(t), "updated_at": now}
                for lang in kept for n, t in sorted(art["pages"][lang].items())]
        try:
            sbase.upsert_rows(cloud, "volume_pages", "slug,lang,page", rows)
        except sbase.SyncError as exc:
            # A live project behind on docs/cloud/migrations lacks the search
            # column; the page text still deserves to publish (the same
            # degradation as _publish_run's volumes upsert).
            if "search_body" not in str(exc):
                raise
            for row in rows:
                row.pop("search_body", None)
            sbase.upsert_rows(cloud, "volume_pages", "slug,lang,page", rows)
            log.warning("volume_pages.search_body is missing on the cloud "
                        "project — apply docs/cloud/migrations/"
                        "003_page_search.sql (page text published unindexed)")
        langs_in = ",".join(f'"{lang}"' for lang in kept)
        sbase.delete_rows(cloud, "volume_pages",
                          f"slug=eq.{q(slug)}&lang=not.in.({q(langs_in)})")
        for lang in kept:
            top = max(art["pages"][lang])
            sbase.delete_rows(
                cloud, "volume_pages",
                f"slug=eq.{q(slug)}&lang=eq.{q(lang)}&page=gt.{top}")
    else:
        sbase.delete_rows(cloud, "volume_pages", f"slug=eq.{q(slug)}")

    if art["notes"]:
        rows = [{"slug": slug, "note_id": str(n.get("id") or ""),
                 "page": int(n.get("page") or 0),
                 "quote": str(n.get("quote") or ""),
                 "kind": str(n.get("kind") or ""),
                 "body": str(n.get("body") or ""), "updated_at": now}
                for n in art["notes"] if n.get("id")]
        sbase.upsert_rows(cloud, "volume_notes", "slug,note_id", rows)
        ids_in = ",".join(f'"{r["note_id"]}"' for r in rows)
        sbase.delete_rows(cloud, "volume_notes",
                          f"slug=eq.{q(slug)}&note_id=not.in.({q(ids_in)})")
    else:
        sbase.delete_rows(cloud, "volume_notes", f"slug=eq.{q(slug)}")


def _publish_slug(cloud: dict, b: dict) -> str:
    """A slug that belongs to THIS build, for good.

    Publishing wrote to `slugify(title, year)` with no uniqueness check, so two
    distinct builds sharing a title and a year -- two scans of the same edition,
    which this catalogue really has -- resolved to one object key and one unique
    `slug` row. The second publish overwrote the first book's PDF and, via
    on_conflict=slug merge-duplicates, its metadata too. Silently.

    The build now remembers the slug it took, so a re-publish overwrites itself
    and only itself; a new build steps around whatever is already there.
    """
    if (b.get("published_slug") or "").strip():
        return b["published_slug"].strip()
    base = lib.slugify(b.get("title") or "", b.get("year"))
    try:
        rows = sbase._rest(cloud, "GET",
                           f"volumes?select=slug&slug=like.{urllib.parse.quote(base)}*") or []
        taken = {r["slug"] for r in rows}
    except Exception:
        taken = set()                      # a lookup failure must not block a first publish
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def _unpublish_object(cloud: dict, slug: str, path: str, r2_name: str = "") -> None:
    """Best-effort removal of an object whose catalogue row never landed.
    r2_name overrides the default "<slug>.pdf" R2 key -- needed for anything
    that isn't the primary PDF (a secondary scan, a thumbnail)."""
    try:
        if path:
            sbase.delete_objects(cloud, "volumes", [path])
        else:
            r2.delete(_r2_cfg(), f"volumes/{r2_name or slug + '.pdf'}")
        log.warning("rolled back orphaned object for %s", slug)
    except Exception as exc:
        log.error("could not roll back orphaned object for %s: %s", slug, exc)


def _publish_run(bid: str, actor: str, job: dict | None = None) -> None:
    def stage(name, cancellable=True, **kw):
        # cancellation is checked at stage boundaries only: past "recording"
        # the volumes row is public, so aborting would not be a clean cancel
        if cancellable and job is not None and _job_cancelled(job):
            raise _JobCancelled()
        with _publish_lock:
            _publish.update(stage=name, **kw)
        if job is not None and job.get("state") in _JOB_ACTIVE:
            job["note"] = name
            _job_checkpoint(job, force=True)
    try:
        builds = lib.load_json(BUILDS_PATH, {})
        b = builds.get(bid) or {}
        pdf = _resolve_local(b.get("pdf_file") or "")
        if pdf is None or not pdf.is_file():
            raise RuntimeError("this entry has no local PDF attached")
        cloud = _cloud_cfg()
        if not cloud:
            raise RuntimeError("Supabase is not configured (Settings > Sync)")

        size = pdf.stat().st_size
        slug = _publish_slug(cloud, b)
        name = f"{slug}.pdf"
        stage("uploading", sent=0, total=size)

        def progress(sent, total):
            with _publish_lock:
                _publish["sent"] = sent
            if job is not None:                 # unified table sees bytes
                job["done"], job["total"] = int(sent), int(total)
                _job_checkpoint(job)

        r2cfg = _r2_cfg()
        if r2.configured(r2cfg):
            # an R2 bucket may also hold installers, so namespace the volumes
            url = r2.put_file(r2cfg, f"volumes/{name}", pdf, "application/pdf",
                              on_progress=progress)
            path = ""
        else:
            # the Supabase bucket IS "volumes" -- prefixing again would nest it
            data = pdf.read_bytes()          # storage has no streaming upload
            progress(size, size)
            sbase.upload_object(cloud, "volumes", name, data, "application/pdf")
            url, path = sbase.public_url(cloud, "volumes", name), name

        # Secondary scans ride along, named after the book like the primary.
        # They live under scans/ — <slug>-2.pdf in the SAME namespace would
        # collide with _publish_slug's "-N" disambiguation of other books
        # (two scans of "Herbal 1600" already produce slugs herbal-1600 and
        # herbal-1600-2). Objects only; the volumes row keeps pointing at
        # the primary. Any failure past this point takes every uploaded
        # object back down — a public object with no catalog row is a leak.
        extras = []
        thumb_url = thumb_path = ""   # defined before the try: an early failure
        try:                          # in the loop below must still see these
            for i, s in enumerate(b.get("pdf_sources") or [], start=2):
                sp = _resolve_local(str(s.get("path") or ""))
                if sp is None or not sp.is_file():
                    continue
                name_i = f"scans/{slug}-{i}.pdf"
                stage(f"uploading {name_i}", sent=0, total=sp.stat().st_size)
                if r2.configured(r2cfg):
                    r2.put_file(r2cfg, f"volumes/{name_i}", sp,
                                "application/pdf", on_progress=progress)
                    extras.append((name_i, ""))
                else:
                    sbase.upload_object(cloud, "volumes", name_i,
                                        sp.read_bytes(), "application/pdf")
                    extras.append((name_i, name_i))

            # Thumbnail: whatever the Editor's Resources tab picked
            # (thumbnail_source, "page:<n>" or "image:<name>"), or — if
            # nothing was picked, or the pick no longer resolves — the same
            # first-non-blank-page heuristic the Resources tab offers as its
            # own "cover candidate" suggestion. A book with no usable page at
            # all just publishes without a thumbnail; that's never a publish
            # failure, a thumbnail is a nice-to-have.
            stage("thumbnail")
            thumb_local = None
            tsrc = str(b.get("thumbnail_source") or "")
            m = re.match(r"^page:(\d+)$", tsrc)
            if m:
                try:
                    thumb_local = _render_pdf_page(pdf, int(m.group(1)), 640)
                except Exception:
                    thumb_local = None
            else:
                m2 = re.match(r"^image:(.+)$", tsrc)
                if m2:
                    cand = (_entry_dir(bid) / "ocr" / "images" /
                            re.sub(r"[^\w.\-]", "_", m2.group(1)))
                    if cand.is_file():
                        thumb_local = cand
            if thumb_local is None:
                try:
                    cover_page = first_content_page(pdf)
                    if cover_page:
                        thumb_local = _render_pdf_page(pdf, cover_page, 640)
                except Exception:
                    thumb_local = None
            thumb_name = f"{slug}-thumb.jpg"
            if thumb_local is not None:
                try:
                    if r2.configured(r2cfg):
                        thumb_url = r2.put_file(r2cfg, f"volumes/{thumb_name}",
                                                thumb_local, "image/jpeg")
                    else:
                        sbase.upload_object(cloud, "volumes", thumb_name,
                                            thumb_local.read_bytes(), "image/jpeg")
                        thumb_url = sbase.public_url(cloud, "volumes", thumb_name)
                        thumb_path = thumb_name
                except Exception as exc:
                    log.warning("thumbnail upload failed for %s: %s", slug, exc)
                    thumb_url = thumb_path = ""

            stage("recording")
            art, withheld = _rights_artifacts(bid, b)
            row = dict(_volume_row(b, slug, url, path, size, actor,
                                    thumb_url, thumb_path),
                       assets=art["assets"])
            try:
                sbase.upsert_volume(cloud, row)
            except sbase.SyncError as exc:
                # A live project behind on docs/cloud/migrations lacks the new
                # columns; the book still deserves to publish.
                missing = ("category_paths", "assets", "thumbnail_url", "thumbnail_path",
                           "volume", "group_id", "copyright_status")
                if not any(k in str(exc) for k in missing):
                    raise
                for k in missing:
                    row.pop(k, None)
                sbase.upsert_volume(cloud, row)
                log.warning("optional volumes metadata is missing on the cloud "
                            "project — apply the pending docs/cloud/migrations")
        except Exception:
            _unpublish_object(cloud, slug, path)
            for name_i, path_i in extras:
                _unpublish_object(cloud, name_i[:-4], path_i)
            if thumb_url or thumb_path:
                _unpublish_object(cloud, f"{slug}-thumb", thumb_path,
                                  r2_name=f"{slug}-thumb.jpg")
            raise

        # The bundle's artifacts go after the row: a failure here leaves the
        # book published without its extras (retryable by republishing), not
        # an orphaned public object. Past this point a cancel would strand a
        # published row, so the remaining stages ignore the request.
        stage("bundle", cancellable=False)
        try:
            _publish_bundle(cloud, slug, art)
        except sbase.SyncError as exc:
            if not any(t in str(exc) for t in
                       ("volume_texts", "volume_pages", "volume_notes")):
                raise
            log.warning("artifact tables missing on the cloud project — "
                        "apply the pending docs/cloud/migrations (%s)", exc)

        # re-read: the upload took minutes, and another writer may have touched
        # builds meanwhile. Only this build's fields are ours to change.
        with _builds_lock:
            fresh = lib.load_json(BUILDS_PATH, {})
            row = fresh.get(bid)
            if row is not None:
                row["status"] = "uploaded"
                row["published_slug"] = slug
                row["updated_at"] = _build_updated_at(row.get("updated_at"))
                lib.save_json(BUILDS_PATH, fresh)
        activity("published", "book", actor=actor or None)
        log.info("published volume %s (%.0f MB) -> %s", slug, size / 1e6, url)
        if withheld:
            log.info("text withheld for %s (rights: %s)", slug, b.get("rights"))
        note = "page text and notes withheld (rights)" if withheld else ""
        stage("done", cancellable=False, url=url, slug=slug, error="", note=note)
        if job is not None:
            _job_transition(job, "done", note=note)
    except _JobCancelled:
        # every uploaded object was rolled back by the block that raised
        log.info("publish cancelled for build %s", bid)
        with _publish_lock:
            _publish.update(stage="cancelled", error="")
        if job is not None:
            _job_transition(job, "cancelled", note="cancelled — uploaded "
                            "objects rolled back; nothing published")
    except Exception as exc:
        log.error("publish failed for build %s", bid, exc_info=exc)
        stage("error", cancellable=False, error=f"{type(exc).__name__}: {exc}")
        if job is not None:
            _job_transition(job, "error",
                            error=f"{type(exc).__name__}: {exc}", note="")
    finally:
        with _publish_lock:
            _publish["running"] = False


@app.route("/api/volumes/publish", methods=["POST"])
def api_volumes_publish():
    p = request.get_json(silent=True) or {}
    bid = str(p.get("build_id") or "").strip()
    builds = lib.load_json(BUILDS_PATH, {})
    if bid not in builds:
        abort(404)
    if builds[bid].get("status") not in ("ready", "uploaded"):
        return jsonify({"ok": False, "error": "only verified entries can be published"}), 400
    if builds[bid].get("rights") not in _BUILD_RIGHTS[1:]:
        return jsonify({"ok": False, "error": "no rights decision — set Rights in "
                        "the Editor before publishing"}), 400
    if not _cloud_cfg():
        return jsonify({"ok": False, "error": "Supabase is not configured (Settings > Sync)"}), 400
    with _publish_lock:
        if _publish["running"]:
            return jsonify({"ok": False, "error": "a publish is already running"}), 409
        _publish.update(running=True, build=bid, stage="starting", sent=0, total=0,
                        error="", url="", slug="", note="", job="")
    job = {"id": lib.gen_id(set(_jobs)), "build_id": bid, "kind": "publish",
           "status": "running"}
    _job_track(job, "publish", label=_job_book_label(bid))
    with _publish_lock:
        _publish["job"] = job["id"]
    threading.Thread(target=_publish_run, args=(bid, _actor(), job),
                     daemon=True).start()
    return jsonify({"ok": True, "job": job["id"]})


@app.route("/api/volumes/publish/status")
def api_volumes_publish_status():
    with _publish_lock:
        return jsonify(dict(_publish, store="r2" if r2.configured(_r2_cfg()) else "supabase"))


# --- Knowledge passages: segmentation, curation, and the search index (#140) -----
# Structure-aware child passages over the OCR text, curated in the Workbench
# Knowledge phase and published as versioned index rows beside — never inside —
# the archive entry (docs/search-design.md D5/D6/D7). The artifact is
# entries/<bid>/passages.json; the cloud side is index_versions + passages in
# docs/cloud/migrations/004_passages_index.sql.

# The starting recipe (#142 benchmarks it, docs/search-design.md §7): child
# passages of ~150-350 whitespace tokens, grouped into parent sections of
# ~600-1200. Token counts are a whitespace approximation on purpose — the
# corpus predates every tokenizer, and the bounds are targets, not truth.
_PASSAGE_RECIPE = {"child_min": 150, "child_max": 350,
                   "parent_min": 600, "parent_max": 1200}
_INDEX_RIGHTS_OK = ("public-domain", "cleared", "searchable-only")
_EMBED_BATCH = 64          # passages per /embeddings call
_INDEX_CHUNK = 100         # passage rows per cloud insert (cancel checkpoints)
_passages_lock = threading.Lock()

# Sentence boundaries: [.!?] (optionally followed by ONE closing quote or
# bracket), whitespace, then something that opens a sentence — a capital, a
# digit, or an opening quote/bracket. A simple heuristic on purpose:
# abbreviations and initials will over-split occasionally, which merely
# places a passage boundary early — it never corrupts text, because
# segmentation only chooses cut points and every slice comes verbatim from
# the source (D8: the stored layers are sacred). Only the whitespace is
# consumed, so slicing at match ends loses no characters.
_SENTENCE_BOUND = re.compile(
    "(?:(?<=[.!?])|(?<=[.!?][\"')\\]»”]))\\s+"
    "(?=[(\\[\"'«“]*[A-Z0-9])")


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """[start, end) sentence slices of `text`, separators attached left, so
    text[a:b].strip() is the verbatim sentence."""
    cuts = [m.end() for m in _SENTENCE_BOUND.finditer(text)]
    spans, start = [], 0
    for c in cuts:
        spans.append((start, c))
        start = c
    if start < len(text):
        spans.append((start, len(text)))
    return [s for s in spans if text[s[0]:s[1]].strip()]


def _passage_id(seed: str, text: str) -> str:
    """Stable, slug-independent passage identity: sha1 of the content plus a
    seed (the generation ordinal, or the source ids of a manual split or
    merge), so identical input yields identical ids on any machine."""
    return hashlib.sha1(f"{seed}\n{text}".encode("utf-8")).hexdigest()[:16]


def _passage_recipe(raw) -> dict:
    """A sanitized recipe: the defaults overlaid with any sane integers."""
    r = dict(_PASSAGE_RECIPE)
    if isinstance(raw, dict):
        for k in r:
            try:
                r[k] = max(20, min(5000, int(raw.get(k, r[k]))))
            except (TypeError, ValueError):
                continue
    r["child_max"] = max(r["child_max"], r["child_min"])
    r["parent_max"] = max(r["parent_max"], r["parent_min"])
    return r


def _segment_passages(pages: dict[int, str], recipe: dict) -> list[dict]:
    """Structure-aware segmentation: page text -> child passages.

    Structure before size: paragraphs (blank-line blocks) are the atoms;
    they pack into child passages within the recipe's token bounds and may
    span page boundaries (that is the point — pages split sentences). A
    sentence is never broken: a paragraph that alone exceeds child_max
    splits into sentence runs first, and a single oversized sentence stays
    whole. Deterministic for identical input.

    Each passage: {id, parent_id, page_from, page_to, text (verbatim
    excerpt, blocks joined by blank lines), body (=_search_normalize(text))}.
    """
    r = _passage_recipe(recipe)
    paras: list[tuple[int, str]] = []
    for n in sorted(pages):
        for block in re.split(r"\n\s*\n", str(pages[n] or "")):
            block = block.strip()
            if block:
                paras.append((n, block))

    # atomic units: (page, text, tokens) — paragraphs, or sentence runs of an
    # oversized paragraph, sliced verbatim at sentence boundaries
    units: list[tuple[int, str, int]] = []
    for page, text in paras:
        toks = len(text.split())
        if toks <= r["child_max"]:
            units.append((page, text, toks))
            continue
        spans = _sentence_spans(text)
        run_from = None
        run_toks = 0
        for a, bnd in spans:
            st = len(text[a:bnd].split())
            if run_from is not None and run_toks + st > r["child_max"]:
                piece = text[run_from:a].strip()
                units.append((page, piece, run_toks))
                run_from, run_toks = None, 0
            if run_from is None:
                run_from = a
            run_toks += st
        if run_from is not None:
            piece = text[run_from:].strip()
            units.append((page, piece, run_toks))

    # pack units into children: child_max is a hard cap, child_min a target —
    # a passage flushes when the next unit would push past the cap, so only a
    # trailing passage (or a single oversized sentence) may sit outside the
    # bounds
    children: list[dict] = []
    cur: list[tuple[int, str, int]] = []
    cur_toks = 0

    def flush() -> None:
        nonlocal cur, cur_toks
        if cur:
            children.append({"page_from": cur[0][0], "page_to": cur[-1][0],
                             "text": "\n\n".join(u[1] for u in cur)})
        cur, cur_toks = [], 0

    for u in units:
        if cur and cur_toks + u[2] > r["child_max"]:
            flush()
        cur.append(u)
        cur_toks += u[2]
    flush()

    for i, c in enumerate(children):
        c["id"] = _passage_id(str(i), c["text"])
        c["body"] = _search_normalize(c["text"])

    # parent sections: consecutive children grouped to the parent bounds
    # (same rule — hard cap, a trailing group may run small); parent_id is
    # derived from the group's first child so it is just as deterministic
    start, ptoks = 0, 0
    for i, c in enumerate(children):
        ct = len(c["text"].split())
        if i > start and ptoks + ct > r["parent_max"]:
            for x in children[start:i]:
                x["parent_id"] = "p" + children[start]["id"]
            start, ptoks = i, 0
        ptoks += ct
    for x in children[start:]:
        x["parent_id"] = "p" + children[start]["id"]

    return [{"id": c["id"], "parent_id": c["parent_id"],
             "page_from": c["page_from"], "page_to": c["page_to"],
             "text": c["text"], "body": c["body"]} for c in children]


def _passages_path(bid: str) -> Path:
    return _entry_dir(bid) / "passages.json"


def _load_passages(bid: str) -> dict | None:
    doc = lib.load_json(_passages_path(bid), None)
    if not isinstance(doc, dict) or not isinstance(doc.get("passages"), list):
        return None
    return doc


def _write_passages(bid: str, doc_name: str, src_input: dict, recipe: dict,
                    pages: dict[int, str]) -> dict:
    """Segment and write entries/<bid>/passages.json, keeping still-valid
    exclusions (ids are content-based, so unchanged passages keep theirs),
    and record provenance — produced_by kind "segment", input = the OCR doc
    fingerprinted at job start — so #135 staleness covers the artifact."""
    passages = _segment_passages(pages, recipe)
    keep = {p["id"] for p in passages}
    with _passages_lock:
        old = _load_passages(bid) or {}
        doc = {"version": 1, "recipe": _passage_recipe(recipe),
               "generated_from": {"doc": doc_name,
                                  "sha256": str(src_input.get("sha256") or "")},
               "passages": passages,
               "excluded": [i for i in old.get("excluded") or [] if i in keep]}
        lib.save_json(_passages_path(bid), doc)
    _manifest_record(bid, "passages.json",
                     {"kind": "segment", "recipe": _passage_recipe(recipe)},
                     [src_input])
    return doc


def _passages_state(bid: str, b: dict) -> dict:
    """The honest Passages state: {exists, count, excluded, stale, doc,
    sha256, recipe}. Stale is manifest staleness (#135) OR the current OCR
    doc's hash differing from generated_from — the direct comparison also
    covers artifacts without a manifest row and a switched active document."""
    name, _text = _analyze_doc(bid, b)
    cur = ""
    if name and (_entry_dir(bid) / "ocr" / name).is_file():
        cur = _file_sha256(_entry_dir(bid) / "ocr" / name)
    with _passages_lock:
        doc = _load_passages(bid)
    if doc is None:
        return {"exists": False, "count": 0, "excluded": 0, "stale": False,
                "doc": name, "sha256": cur, "recipe": _passage_recipe(None)}
    gen = doc.get("generated_from") or {}
    stale = bool(_manifest_inputs_stale(bid, "passages.json")) or \
        bool(cur and str(gen.get("sha256") or "") != cur)
    return {"exists": True, "count": len(doc.get("passages") or []),
            "excluded": len(doc.get("excluded") or []), "stale": stale,
            "doc": name, "sha256": cur,
            "recipe": _passage_recipe(doc.get("recipe"))}


def _apply_passage_edits(doc: dict, p: dict) -> str:
    """Apply {exclude, include, split, merge} to a passages doc in place;
    returns an error string, or ''. Split and merge recompute ids from their
    source ids + content, so curation stays deterministic too."""
    passages = doc.get("passages") or []
    ids = {str(x.get("id")): i for i, x in enumerate(passages)}
    excluded = set(str(i) for i in doc.get("excluded") or [])

    for pid in p.get("exclude") or []:
        if str(pid) not in ids:
            return f"unknown passage id: {pid}"
        excluded.add(str(pid))
    for pid in p.get("include") or []:
        excluded.discard(str(pid))

    split = p.get("split") or {}
    if split.get("id"):
        i = ids.get(str(split["id"]))
        if i is None:
            return "unknown passage id: " + str(split["id"])
        orig = passages[i]
        spans = _sentence_spans(orig["text"])
        if len(spans) < 2:
            return "cannot split — the passage is a single sentence"
        cut = spans[len(spans) // 2][0]        # the middle sentence boundary
        halves = []
        for tag, text in (("a", orig["text"][:cut].strip()),
                          ("b", orig["text"][cut:].strip())):
            halves.append({
                "id": _passage_id(f"{orig['id']}:{tag}", text),
                "parent_id": orig.get("parent_id") or "",
                # without an offset map the cut's page is unknowable (D2),
                # so both halves keep the honest full range
                "page_from": orig.get("page_from"),
                "page_to": orig.get("page_to"),
                "text": text, "body": _search_normalize(text)})
        passages[i:i + 1] = halves
        if orig["id"] in excluded:             # an excluded passage splits excluded
            excluded.discard(orig["id"])
            excluded.update(h["id"] for h in halves)

    merge = p.get("merge") or {}
    if merge.get("id"):
        ids = {str(x.get("id")): i for i, x in enumerate(passages)}
        i = ids.get(str(merge["id"]))
        if i is None:
            return "unknown passage id: " + str(merge["id"])
        if (i + 1 >= len(passages)
                or (passages[i + 1].get("parent_id") or "")
                != (passages[i].get("parent_id") or "")):
            return "cannot merge — no next passage in the same section"
        a, nxt = passages[i], passages[i + 1]
        text = a["text"].rstrip() + "\n\n" + nxt["text"].lstrip()
        merged = {"id": _passage_id(f"{a['id']}+{nxt['id']}", text),
                  "parent_id": a.get("parent_id") or "",
                  "page_from": a.get("page_from"),
                  "page_to": nxt.get("page_to"),
                  "text": text, "body": _search_normalize(text)}
        passages[i:i + 2] = [merged]
        both = a["id"] in excluded and nxt["id"] in excluded
        excluded.discard(a["id"])
        excluded.discard(nxt["id"])
        if both:                               # merged stays out only if both were
            excluded.add(merged["id"])

    doc["passages"] = passages
    doc["excluded"] = sorted(excluded)
    return ""


@app.route("/api/knowledge/segment", methods=["POST"])
def api_knowledge_segment():
    """Segment the OCR text into passages. Body: {build_id, recipe?}. A
    tracked job — the work is fast, but the registry gives progress and the
    jobs drawer a row like every other background step."""
    p = request.get_json(silent=True) or {}
    bid = str(p.get("build_id") or "").strip()
    b, err = _an_gate(bid)
    if err:
        return err
    recipe = _passage_recipe(p.get("recipe"))
    doc_name, text, source_revision = _analyze_doc_snapshot(bid, b)
    pages = _an_pages(text)
    if not pages:
        return jsonify({"ok": False, "error":
                        "no OCR text for this entry — extract or run OCR first"}), 400
    src_input = _manifest_input(bid, f"ocr/{doc_name}")   # hashed at job start

    def run(job):
        try:
            if _an_cancel_check(job, "cancelled — passages not written"):
                return
            doc = _write_passages(bid, doc_name, src_input, recipe, pages)
            with _an_jobs_lock:
                job["done"] = 1
                job["note"] = f"{len(doc['passages'])} passages"
            activity("segmented passages", "book", detail=b.get("title", ""))
            _an_finish(job)
        except Exception as exc:
            log.error("segmentation failed for %s", bid, exc_info=exc)
            _an_finish(job, f"{type(exc).__name__}: {exc}")

    try:
        job = _an_job_start_guarded(bid, source_revision, "segment", 1, run)
    except _AnalyzeSourceChanged:
        return jsonify({"ok": False, "error":
                        "page numbering changed — review the pages and retry"}), 409
    return jsonify({"ok": True, "job": job["id"], "doc": doc_name})


@app.route("/api/builds/<bid>/passages", methods=["GET", "PATCH"])
def api_build_passages(bid: str):
    """GET: the artifact plus its state line. PATCH {exclude, include,
    split, merge}: curation — a manual edit, re-recorded as such so the
    recorded inputs (and staleness) stay judgeable."""
    if request.method == "GET":
        b = lib.load_json(BUILDS_PATH, {}).get(bid) or {}
        with _passages_lock:
            doc = _load_passages(bid)
        return jsonify({"ok": True, "doc": doc,
                        "state": _passages_state(bid, b)})
    b, err = _an_gate(bid)
    if err:
        return err
    p = request.get_json(silent=True) or {}
    with _passages_lock:
        doc = _load_passages(bid)
        if doc is None:
            return jsonify({"ok": False, "error":
                            "no passages yet — generate them first"}), 404
        problem = _apply_passage_edits(doc, p)
        if problem:
            return jsonify({"ok": False, "error": problem}), 400
        lib.save_json(_passages_path(bid), doc)
    _manifest_record(bid, "passages.json", {"kind": "manual-edit"})
    return jsonify({"ok": True, "doc": doc,
                    "state": _passages_state(bid, b)})


def _embed_cfg() -> dict:
    """The embeddings provider (Settings > AI): any OpenAI-compatible POST
    /embeddings endpoint. Configured = base AND model set; the key is
    optional so self-hosted endpoints work. Absent -> lexical-only indexes
    with model '' (docs/search-design.md D7)."""
    s = _client_settings()
    return {"base": str(s.get("embedBase") or "").strip(),
            "model": str(s.get("embedModel") or "").strip(),
            "key": str(s.get("embedKey") or "").strip()}


def _embed_texts(cfg: dict, texts: list[str]) -> list[list[float]]:
    """One /embeddings call for a batch; returns vectors in input order.
    Same error convention as _ai_chat: RuntimeError with the body truncated."""
    headers = {"Content-Type": "application/json"}
    if cfg["key"]:
        headers["Authorization"] = f"Bearer {cfg['key']}"
    req = urllib.request.Request(
        cfg["base"].rstrip("/") + "/embeddings",
        data=json.dumps({"model": cfg["model"], "input": texts}).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120.0) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
    rows = data.get("data")
    if not isinstance(rows, list) or len(rows) != len(texts):
        raise RuntimeError("malformed embeddings response")
    rows = sorted(rows, key=lambda r: int(r.get("index") or 0))
    return [list(r.get("embedding") or []) for r in rows]


def _vector_literal(vec: list) -> str:
    """pgvector input as a string ('[0.1,0.2,...]') so PostgREST casts
    text -> vector at any dimension."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _index_sync_error(exc) -> str:
    """A SyncError message, naming the migration when the tables are absent
    (the _publish_bundle degradation convention)."""
    msg = str(exc)
    if "index_versions" in msg or "passages" in msg:
        return ("index tables missing on the cloud project — apply "
                "docs/cloud/migrations/004_passages_index.sql")
    return msg


def _index_version_delete(cloud: dict, version_id: str) -> None:
    """Best-effort removal of a version row; the FK cascade removes whatever
    passages already landed. A half version must never survive."""
    if not version_id:
        return
    try:
        sbase.delete_rows(cloud, "index_versions",
                          "id=eq." + urllib.parse.quote(str(version_id)))
    except Exception as exc:
        log.error("could not remove index version %s: %s", version_id, exc)


@app.route("/api/knowledge/index/publish", methods=["POST"])
def api_knowledge_index_publish():
    """Build and publish a search-index version. Body: {build_id}.

    Requires the archive entry published first (the index attaches to its
    catalogue row) and a permitting rights decision. searchable-only IS
    permitting here (docs/rights.md): passage bodies are never anon-readable
    and the RPC returns only snippets. Segments first when passages.json is
    missing or outdated; embeds through the configured provider or degrades
    to lexical-only. Cancellable between batches; a cancelled or failed
    build deletes its partial index_versions row (cascade cleans passages).
    """
    p = request.get_json(silent=True) or {}
    bid = str(p.get("build_id") or "").strip()
    b, err = _an_gate(bid)
    if err:
        return err
    slug = str(b.get("published_slug") or "").strip()
    if b.get("status") != "uploaded" or not slug:
        return jsonify({"ok": False, "error":
                        "publish the archive entry first — the search index "
                        "attaches to its catalogue row"}), 400
    rights = str(b.get("rights") or "")
    if rights not in _INDEX_RIGHTS_OK:
        msg = ("no rights decision — set Rights in the Editor first"
               if not rights else
               "the rights decision “No public text” blocks a "
               "search index (docs/rights.md)")
        return jsonify({"ok": False, "error": msg}), 400
    cloud = _cloud_cfg()
    if not cloud:
        return jsonify({"ok": False, "error":
                        "Supabase is not configured (Settings > Sync)"}), 400
    doc_name, text, source_revision = _analyze_doc_snapshot(bid, b)
    pages = _an_pages(text)
    if not pages:
        return jsonify({"ok": False, "error":
                        "no OCR text for this entry — extract or run OCR first"}), 400
    ecfg = _embed_cfg()
    embed = bool(ecfg["base"] and ecfg["model"])
    src_input = _manifest_input(bid, f"ocr/{doc_name}")   # hashed at job start
    src_sha = str(src_input.get("sha256") or "")

    def run(job):
        version_id = ""
        try:
            if _an_cancel_check(job, "cancelled — nothing published"):
                return
            # 1. passages: reuse the artifact when it matches the OCR doc as
            # it stands, (re)segment otherwise — corrected text re-indexes
            # without republishing the PDF
            with _passages_lock:
                doc = _load_passages(bid)
            gen = (doc or {}).get("generated_from") or {}
            if doc is None or str(gen.get("sha256") or "") != src_sha:
                doc = _write_passages(
                    bid, doc_name, src_input,
                    _passage_recipe((doc or {}).get("recipe")), pages)
            excluded = set(doc.get("excluded") or [])
            included = [x for x in doc.get("passages") or []
                        if x.get("id") not in excluded]
            if not included:
                raise RuntimeError("every passage is excluded — nothing to index")
            batches = ((len(included) + _EMBED_BATCH - 1) // _EMBED_BATCH
                       if embed else 0)
            chunks = (len(included) + _INDEX_CHUNK - 1) // _INDEX_CHUNK
            with _an_jobs_lock:
                job["total"] = 1 + batches + chunks
                job["done"] = 1
            _job_checkpoint(job, force=True)

            # 2. embeddings (optional): the normalized body is what the index
            # searches, so it is what embeds
            vectors: dict[str, str] = {}
            if embed:
                for i in range(0, len(included), _EMBED_BATCH):
                    if _an_cancel_check(job, "cancelled — nothing published"):
                        return
                    batch = included[i:i + _EMBED_BATCH]
                    embs = _embed_texts(
                        ecfg, [x.get("body") or x.get("text") or ""
                               for x in batch])
                    for x, e in zip(batch, embs):
                        vectors[x["id"]] = _vector_literal(e)
                    with _an_jobs_lock:
                        job["done"] += 1
                    _job_checkpoint(job)

            # 3. the version row first (its passages reference it), the
            # passages in bounded chunks after. Cancel checks sit between
            # chunks; _JobCancelled or any failure past this point deletes
            # the row so a half version never serves. (A hard process kill
            # can still strand one — Roll back removes it.)
            config = {"recipe": doc.get("recipe") or {}, "normalize": 1,
                      "model": ecfg["model"] if embed else ""}
            stats = {"passages": len(included), "embedded": len(vectors),
                     "excluded": len(excluded)}
            rows = sbase._rest(
                cloud, "POST", "index_versions",
                [{"slug": slug, "channel": "stable", "config": config,
                  "source_hash": src_sha, "stats": stats}],
                prefer="return=representation")
            if not (isinstance(rows, list) and rows
                    and isinstance(rows[0], dict) and rows[0].get("id")):
                raise RuntimeError("index_versions insert returned no row")
            version_id = str(rows[0]["id"])
            prows = [{"index_id": version_id, "slug": slug,
                      "passage_id": x["id"],
                      "parent_id": x.get("parent_id") or "",
                      "page_from": x.get("page_from"),
                      "page_to": x.get("page_to"),
                      "body": x.get("body") or "",
                      "embedding": vectors.get(x["id"])}
                     for x in included]
            for i in range(0, len(prows), _INDEX_CHUNK):
                if _job_cancelled(job):
                    raise _JobCancelled()
                sbase.upsert_rows(cloud, "passages",
                                  "index_id,slug,passage_id",
                                  prows[i:i + _INDEX_CHUNK])
                with _an_jobs_lock:
                    job["done"] += 1
                _job_checkpoint(job)
            with _an_jobs_lock:
                job["note"] = (f"{len(included)} passages"
                               + (f", {len(vectors)} embedded" if vectors
                                  else " (lexical-only)"))
            activity("published search index", "book",
                     detail=b.get("title", ""))
            log.info("published index version %s for %s (%d passages, "
                     "%d embedded)", version_id, slug, len(included),
                     len(vectors))
            _an_finish(job)
        except _JobCancelled:
            _index_version_delete(cloud, version_id)
            _job_transition(job, "cancelled", note="cancelled — the partial "
                            "index version was removed; the previous one "
                            "still serves")
        except sbase.SyncError as exc:
            _index_version_delete(cloud, version_id)
            log.error("index publish failed for %s: %s", bid, exc)
            _an_finish(job, _index_sync_error(exc))
        except Exception as exc:
            _index_version_delete(cloud, version_id)
            log.error("index publish failed for %s", bid, exc_info=exc)
            _an_finish(job, f"{type(exc).__name__}: {exc}")

    try:
        job = _an_job_start_guarded(bid, source_revision, "index-publish",
                                    1, run)
    except _AnalyzeSourceChanged:
        return jsonify({"ok": False, "error":
                        "page numbering changed — review the pages and retry"}), 409
    return jsonify({"ok": True, "job": job["id"], "slug": slug,
                    "model": ecfg["model"] if embed else ""})


@app.route("/api/knowledge/index/rollback", methods=["POST"])
def api_knowledge_index_rollback():
    """Delete the newest index version for the build's slug; the previous
    one becomes latest by built_at (the releases pattern — archive rows are
    never touched). Body: {build_id}."""
    p = request.get_json(silent=True) or {}
    bid = str(p.get("build_id") or "").strip()
    b, err = _an_gate(bid)
    if err:
        return err
    slug = str(b.get("published_slug") or "").strip()
    if not slug:
        return jsonify({"ok": False, "error":
                        "this entry has never published"}), 400
    cloud = _cloud_cfg()
    if not cloud:
        return jsonify({"ok": False, "error":
                        "Supabase is not configured (Settings > Sync)"}), 400
    q = urllib.parse.quote(slug, safe="")
    try:
        rows = sbase._rest(
            cloud, "GET", f"index_versions?slug=eq.{q}"
            "&select=id,built_at&order=built_at.desc,id.desc&limit=2") or []
        if not rows:
            return jsonify({"ok": False, "error":
                            "no index versions to roll back"}), 400
        newest = str(rows[0].get("id") or "")
        sbase.delete_rows(cloud, "index_versions",
                          "id=eq." + urllib.parse.quote(newest))
    except sbase.SyncError as exc:
        return jsonify({"ok": False, "error": _index_sync_error(exc)}), 502
    activity("rolled back search index", "book", detail=b.get("title", ""))
    return jsonify({"ok": True, "removed": newest,
                    "remaining": max(0, len(rows) - 1)})


@app.route("/api/knowledge/index/status")
def api_knowledge_index_status():
    """Everything the Publish phase's Search index card needs in one call:
    the local passages state plus the slug's version list, newest first."""
    bid = str(request.args.get("build_id") or "").strip()
    b = lib.load_json(BUILDS_PATH, {}).get(bid)
    if not b:
        abort(404)
    slug = str(b.get("published_slug") or "").strip()
    versions, warning = [], ""
    cloud = _cloud_cfg()
    if not cloud:
        warning = "Supabase is not configured (Settings > Sync)"
    elif slug:
        q = urllib.parse.quote(slug, safe="")
        try:
            versions = [v for v in (sbase._rest(
                cloud, "GET", f"index_versions?slug=eq.{q}"
                "&select=id,channel,config,stats,source_hash,built_at"
                "&order=built_at.desc,id.desc") or []) if isinstance(v, dict)]
        except sbase.SyncError as exc:
            warning = _index_sync_error(exc)
    return jsonify({"ok": True, "state": _passages_state(bid, b),
                    "versions": versions, "slug": slug,
                    "published": b.get("status") == "uploaded",
                    "rights": str(b.get("rights") or ""), "warning": warning})


def _capture_note(cap: dict, errors: list[str]) -> str:
    bits = ["Captured via phone"]
    if cap.get("device"):
        bits.append(f"({cap['device']})")
    if cap.get("created_at"):
        bits.append(str(cap["created_at"])[:19])
    note = " ".join(bits)
    if cap.get("contributor"):
        note += f"\nContributor: {str(cap['contributor'])[:80]}"
    if cap.get("note"):
        note += "\n" + str(cap["note"])
    if errors:
        note += "\n" + "; ".join(errors)
    return note


def _phone_result(cap: dict, raw_photos: list[bytes], photo_paths: list) -> dict | None:
    """Reuse the OCR + fields the phone (BookCapture 2.0+) extracted in the
    background, so import doesn't pay for a second OCR pass. Returns a
    process_capture-shaped dict, or None when the phone sent nothing usable
    (older app, or no API key on the phone) — the caller falls back to the full
    desktop pipeline.

    The images are still perspective-corrected + standardized here: that step
    needs OpenCV, which the phone deliberately does not ship, so the stored copy
    is the desktop's better one — only the text/fields are the phone's.
    """
    ocr = cap.get("ocr")
    meta = cap.get("meta")
    if isinstance(ocr, str):
        try:
            ocr = json.loads(ocr)
        except json.JSONDecodeError:
            ocr = None
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = None
    ocr = ocr if isinstance(ocr, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    def has_value(value):
        if isinstance(value, (dict, list)):
            return bool(value)
        return bool(str(value or "").strip())

    has_metadata = any(has_value(value) for value in meta.values())
    if not (ocr or has_metadata):
        return None                       # phone had no keys / extracted nothing

    photos, errors = [], []
    for i, raw in enumerate(raw_photos, 1):
        try:
            photos.append(capture.process_photo(raw))
        except Exception as exc:          # noqa: BLE001 - never lose a photo to a bad warp
            photos.append(raw)
            errors.append(f"photo {i}: processing failed ({type(exc).__name__})")
    # OCR text in page order, keyed by the object path's basename
    parts = []
    for i, p in enumerate(photo_paths, 1):
        text = str(ocr.get(str(p).rsplit("/", 1)[-1]) or "").strip()
        if text:
            parts.append(f"--- Photo {i} ---\n{text}")
    fields = {f: str(meta.get(f) or "").strip() for f in capture.FIELDS}
    # The phone normally puts non-column facts under `extra`. Be liberal when
    # reading older/newer extractors: preserve every unknown top-level key too,
    # so adding metadata on Android never silently loses it on desktop.
    extra = dict(meta.get("extra")) if isinstance(meta.get("extra"), dict) else {}
    for key, value in meta.items():
        if key not in capture.FIELDS and key != "extra" and has_value(value):
            extra.setdefault(key, value)
    return {"photos": photos, "ocr_text": "\n\n".join(parts),
            "fields": fields, "extra": extra, "errors": errors}


def ingest_capture(cap: dict, raw_photos: list[bytes], mistral_key: str,
                   photo_paths: list | None = None):
    """Raw photo bytes + a capture dict -> a manual entry, with NO cloud
    involved. Shared by the cloud sync (photos downloaded first) and the LAN
    endpoint (photos arrive in the request). Prefers the phone's OCR/fields (via
    _phone_result), else the full desktop pipeline. Returns (entry_id, errors),
    or (None, None) when it's a duplicate (idempotent on capture_id)."""
    cap_id = re.sub(r"[^A-Za-z0-9-]", "", str(cap.get("id") or ""))[:64]
    if not cap_id:
        return None, None
    with _manual_lock:
        entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
        if any(e.get("capture_id") == cap_id for e in entries.values()):
            return None, None                     # already here: idempotent
    # prefer the phone's OCR/fields (BookCapture 2.0+) to skip a second OCR pass;
    # fall back to the full desktop pipeline when the phone sent nothing
    result = _phone_result(cap, raw_photos, photo_paths or []) \
        or capture.process_capture(raw_photos, mistral_key)

    cdir = CAPTURES_DIR / cap_id
    cdir.mkdir(parents=True, exist_ok=True)
    images = []
    for i, jpg in enumerate(result["photos"], 1):
        (cdir / f"photo_{i}.jpg").write_bytes(jpg)
        images.append(f"captures/{cap_id}/photo_{i}.jpg")
    for i, raw in enumerate(raw_photos, 1):       # originals: re-OCR stays possible
        (cdir / f"orig_{i}.jpg").write_bytes(raw)
    if result["ocr_text"]:
        (cdir / "ocr.txt").write_text(result["ocr_text"], "utf-8", errors="replace")

    fields = result["fields"]
    entry = {f: "" for f in lib.MANUAL_ENTRY_FIELDS}
    for f in ("title", "subtitle", "author", "publisher", "city",
              "year", "edition", "volume", "language"):
        entry[f] = str(fields.get(f) or "").strip()
    if not entry["title"]:                        # never lose a capture
        entry["title"] = f"(untitled capture {cap_id[:8]})"
    entry["notes"] = _capture_note(cap, result["errors"])
    entry["extra"] = _clean_extra(result["extra"])
    entry["images"] = images
    entry["capture_id"] = cap_id
    entry["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry["checks"] = _entry_checks(entry)
    with _manual_lock:
        entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
        entry["id"] = lib.gen_id(set(entries))
        entries[entry["id"]] = entry
        lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)

    # a phone capture is attributed to whoever photographed it (the signed-in
    # contributor), not to whoever ran the sync; device name is the fallback
    actor = str(cap.get("contributor") or cap.get("device") or "phone")[:60]
    activity("captured", "book", actor=actor, detail=entry.get("title", ""))
    return entry["id"], result["errors"]


def _import_capture(cfg: dict, cap: dict, mistral_key: str,
                    delete_remote: bool) -> str:
    """One pending CLOUD capture -> a manual entry. Returns 'imported'|'skipped'."""
    cap_id = re.sub(r"[^A-Za-z0-9-]", "", str(cap.get("id") or ""))[:64]
    if not cap_id:
        return "skipped"
    photo_paths = cap.get("photos") or []
    if isinstance(photo_paths, str):
        try:
            photo_paths = json.loads(photo_paths)
        except json.JSONDecodeError:
            photo_paths = []
    with _manual_lock:
        entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
        dup = any(e.get("capture_id") == cap_id for e in entries.values())
    if dup:
        sbase.mark_capture(cfg, cap["id"], "imported")   # already here: idempotent
        if delete_remote:                                # a lost mark left these behind
            try:
                sbase.delete_photos(cfg, photo_paths)
            except sbase.SyncError:
                pass
        return "skipped"
    try:
        raw_photos = [sbase.download_photo(cfg, p) for p in photo_paths]
    except sbase.SyncError as exc:
        if "HTTP 404" in str(exc) or "HTTP 400" in str(exc):
            # the photos are gone — this row can never import; stop retrying it
            sbase.mark_capture(cfg, cap["id"], "error")
        raise
    new_id, errors = ingest_capture(cap, raw_photos, mistral_key, photo_paths)

    sbase.mark_capture(cfg, cap["id"], "imported")
    # keep the cloud copies when OCR/extraction had trouble — the originals
    # are local too, but leaving the remote set makes recovery foolproof
    if delete_remote and not errors:
        try:
            sbase.delete_photos(cfg, photo_paths)
        except sbase.SyncError:
            pass                                   # cleanup is best-effort
    return "imported" if new_id else "skipped"


def _books_mirror_rows() -> list[dict]:
    """The whole catalog (checked + manual) as cloud `books` upsert rows."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    state = lib.load_json(lib.CLIENT_STATE_PATH, {}) or {}
    for pair in (state.get("checked") or []):
        try:
            key, val = pair[0], pair[1]
        except (TypeError, IndexError):
            continue
        book = (val or {}).get("book") or {}
        if book:
            rows.append({"key": str(key), "data": book, "updated_at": now})
    for eid, e in (lib.load_json(lib.MANUAL_ENTRIES_PATH, {}) or {}).items():
        data = {f: e.get(f, "") for f in lib.MANUAL_ENTRY_FIELDS}
        for k in ("extra", "images"):
            if e.get(k):
                data[k] = e[k]
        rows.append({"key": f"manual:{eid}", "data": data, "updated_at": now})
    return rows


def _cloud_sync_run() -> dict:
    """Import this user's pending phone captures, with optional owner work.

    Capture ingest runs with the signed-in user's JWT and RLS. If an owner has
    separately configured a service credential, the same pass also pushes the
    catalog mirror, merges the owner working stores, and mirrors entry folders.

    Everything after the flag is claimed runs inside try/finally, and ANY
    exception lands in `result` — the flag can never stay stuck on, and a
    failed pass can't masquerade as the previous run's outcome."""
    owner_cfg = _cloud_cfg()
    capture_cfg = _capture_cfg() or owner_cfg  # legacy owner-only installs
    if not capture_cfg:
        return {"ok": False,
                "error": "Sign in to your Library Tool account to sync phone captures"}
    with _cloudsync_lock:
        if _cloudsync["running"]:
            return {"ok": False, "error": "sync already running"}
        _cloudsync["running"] = True
    imported = skipped = 0
    errors: list[str] = []
    result: dict = {"ok": False, "error": "sync crashed", "errors": errors}
    log.info("cloud sync started")
    try:
        s = _client_settings()
        mistral_key = str(s.get("mistralKey") or "").strip()
        delete_remote = s.get("cloudDeleteRemote") is not False
        for cap in sbase.list_pending_captures(capture_cfg):
            try:
                if _import_capture(capture_cfg, cap, mistral_key, delete_remote) == "imported":
                    imported += 1
                else:
                    skipped += 1
            except Exception as exc:      # one bad capture must not stop the rest
                errors.append(f"capture {str(cap.get('id'))[:8]}: {exc}")
        pushed = 0
        stores: dict = {}
        entries_res: dict = {}
        if owner_cfg:
            try:
                pushed = sbase.push_books(owner_cfg, _books_mirror_rows())
            except sbase.SyncError as exc:
                errors.append(f"books mirror: {exc}")
            # the working stores that left git in 87a9bf2 (two-way, per record;
            # store_sync guards against an emptier side clobbering a fuller one)
            stores = store_sync.sync_stores(owner_cfg, locks={
                "builds": _builds_lock, "ia_catalog": _ia_catalog_lock,
                "corrections": _corrections_lock,
                "taxonomy": _categories_lock})
            for name, res in stores.items():
                if res.get("error"):
                    errors.append(f"{name}: {res['error']}")
                if res.get("guard"):      # a wipe was caught: worth surfacing
                    errors.append(f"{name}: {res['guard']}")
            r2cfg = _r2_cfg()
            if r2.configured(r2cfg):
                try:
                    entries_res = store_sync.sync_entry_files(r2cfg)
                except Exception as exc:
                    errors.append(f"entry files: {exc}")
            else:
                entries_res = {"skipped": "R2 not configured"}
        else:
            entries_res = {"skipped": "owner sync not configured"}
        result = {"ok": not errors, "imported": imported, "skipped": skipped,
                  "books_pushed": pushed, "stores": stores,
                  "entries": entries_res, "errors": errors,
                  "owner_sync": bool(owner_cfg)}
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                  "imported": imported, "errors": errors}
        log.error("cloud sync crashed", exc_info=exc)
    finally:
        with _cloudsync_lock:
            _cloudsync["running"] = False
            _cloudsync["last_run"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds")
            _cloudsync["last_result"] = result
            _cloudsync["last_error"] = (result.get("error", "")
                                        or "; ".join(result.get("errors") or []))
        if result.get("ok"):
            stores = result.get("stores") or {}
            log.info("cloud sync done: %d imported, %d skipped, %d books pushed, "
                     "stores %d up / %d down, entry files %d up / %d down",
                     result.get("imported", 0), result.get("skipped", 0),
                     result.get("books_pushed", 0),
                     sum(r.get("pushed", 0) + r.get("tombstoned", 0)
                         for r in stores.values()),
                     sum(r.get("pulled", 0) + r.get("deleted", 0)
                         for r in stores.values()),
                     (result.get("entries") or {}).get("pushed", 0),
                     (result.get("entries") or {}).get("pulled", 0))
        elif result.get("error") != "sync crashed":
            log.warning("cloud sync finished with errors: %s",
                        result.get("error") or "; ".join(errors))
    return result


def _cloud_autosync_loop() -> None:
    """Background interval sync; the interval is re-read every tick so a
    settings change applies without a restart (0 = off)."""
    global _autosync_last
    while True:
        time.sleep(30)
        # the WHOLE tick is guarded: a torn settings read (or anything else)
        # must never kill this thread — it would silently stop syncing forever
        try:
            minutes = int(_client_settings().get("cloudSyncMinutes") or 0)
            if minutes <= 0 or not (_capture_cfg() or _cloud_cfg()):
                continue
            if time.time() - _autosync_last >= minutes * 60:
                _autosync_last = time.time()
                _cloud_sync_run()
        except Exception:
            pass


@app.route("/api/cloudsync/run", methods=["POST"])
def api_cloudsync_run():
    """Manual sync trigger (the Catalogs-tab button). Runs in the background;
    poll /api/cloudsync/status for the outcome."""
    if not (_capture_cfg() or _cloud_cfg()):
        return jsonify({"ok": False,
                        "error": "Sign in to your Library Tool account to sync phone captures"})
    with _cloudsync_lock:
        already = _cloudsync["running"]
    if not already:
        threading.Thread(target=_cloud_sync_run, daemon=True).start()
    return jsonify({"ok": True, "started": not already})


@app.route("/api/cloudsync/status")
def api_cloudsync_status():
    with _cloudsync_lock:
        out = dict(_cloudsync)
    out["configured"] = bool(_capture_cfg() or _cloud_cfg())
    return jsonify(out)


@app.route("/api/cloudsync/test")
def api_cloudsync_test():
    cfg = _capture_cfg() or _cloud_cfg()
    if not cfg:
        return jsonify({"ok": False,
                        "error": "Sign in to your Library Tool account to test phone sync"})
    return jsonify(sbase.test_connection(cfg))


@app.route("/api/capture/image")
def api_capture_image():
    """Serve an entry-associated image (DATA_ROOT-relative path)."""
    p = _resolve_local(request.args.get("path") or "")
    if (p is None or not p.is_file()
            or p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp")):
        abort(404)
    try:
        p.relative_to(lib.DATA_ROOT.resolve())     # images never leave the data root
    except ValueError:
        abort(404)
    mime = "image/png" if p.suffix.lower() == ".png" else \
        "image/webp" if p.suffix.lower() == ".webp" else "image/jpeg"
    return send_file(str(p), mimetype=mime, conditional=True)


def _relativize_data_path(raw: str) -> str:
    """An absolute path that lives under the writable data root is rewritten
    to a DATA_ROOT-relative posix path so it survives the app being moved
    (packaging, a new machine). Paths outside the data root — scans the user
    attached from elsewhere on disk — are left untouched."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    p = Path(raw)
    if not p.is_absolute():
        return raw
    try:
        rel = p.resolve().relative_to(lib.DATA_ROOT.resolve())
    except (ValueError, OSError):
        return raw
    return rel.as_posix()


def _migrate_stored_paths() -> None:
    """One-time (idempotent) migration: absolute pdf_file / local_pdf paths
    stored under the data root become relative, making existing user data
    portable across relocations."""
    with _builds_lock:
        builds = lib.load_json(BUILDS_PATH, {})
        changed = False
        for b in builds.values():
            rel = _relativize_data_path(b.get("pdf_file", ""))
            if rel != (b.get("pdf_file") or ""):
                b["pdf_file"] = rel
                b["updated_at"] = _build_updated_at(b.get("updated_at"))
                changed = True
        if changed:
            lib.save_json(BUILDS_PATH, builds)

    with _manual_lock:
        entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
        changed = False
        for e in entries.values():
            rel = _relativize_data_path(e.get("local_pdf", ""))
            if rel != (e.get("local_pdf") or ""):
                e["local_pdf"] = rel
                changed = True
        if changed:
            lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)


# --- LAN capture (offline phone -> desktop, bypassing the cloud) --------------
# A SECOND HTTP listener bound to 0.0.0.0 serving ONLY /lan/ping and
# /lan/capture. The admin sidecar (this module's `app`) stays loopback-only and
# is never exposed. Off unless the user opts in (client setting `lanCapture`, or
# the WHL_LAN_ENABLE env from the desktop shell); a pairing token guards the
# capture route. Photos arrive in the request and feed the SAME ingest the cloud
# sync uses, so a LAN capture lands as an identical manual entry — no internet.

_LAN_TOKEN_PATH = lib.DATA_ROOT / "lan_token.txt"


def _lan_token() -> str:
    """The pairing token the phone must present; generated once, kept locally."""
    import secrets
    try:
        tok = _LAN_TOKEN_PATH.read_text("utf-8").strip()
    except OSError:
        tok = ""
    if not tok:
        tok = secrets.token_urlsafe(18)
        try:
            _LAN_TOKEN_PATH.write_text(tok, "utf-8")
        except OSError:
            pass
    return tok


def _lan_settings() -> tuple[bool, int]:
    """(enabled, port). Env overrides the client setting so the desktop shell
    can flip it on without a UI round-trip; default off, port 8899."""
    env_en = os.environ.get("WHL_LAN_ENABLE")
    s = _client_settings()
    enabled = (env_en == "1") if env_en is not None else bool(s.get("lanCapture"))
    try:
        port = int(os.environ.get("WHL_LAN_PORT") or s.get("lanPort") or 8899)
    except (TypeError, ValueError):
        port = 8899
    return enabled, port


def _lan_ips() -> list[str]:
    """This host's LAN IPv4 addresses, so the desktop can tell the phone where
    to point (manual pairing)."""
    import socket
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


lan_app = Flask("whl_lan")


@lan_app.get("/lan/ping")
def _lan_ping():
    # unauthenticated liveness so the phone can confirm it reached the right host
    import socket
    return jsonify(app="whl-capture", device=socket.gethostname() or "desktop")


@lan_app.post("/lan/capture")
def _lan_capture():
    import hmac
    if not hmac.compare_digest(request.headers.get("X-WHL-Token", ""), _lan_token()):
        abort(401)
    try:
        cap = json.loads(request.form.get("meta", "{}"))
    except json.JSONDecodeError:
        abort(400)
    if not isinstance(cap, dict):
        abort(400)
    files = request.files.getlist("photo")
    names = [f.filename or "" for f in files]
    photos = [p for p in (f.read() for f in files) if p]
    if not photos:
        abort(400)
    mistral_key = str(_client_settings().get("mistralKey") or "").strip()
    try:
        # pass the filenames so _phone_result can key the phone's OCR by name
        entry_id, _errors = ingest_capture(cap, photos, mistral_key, names)
    except Exception as exc:                       # noqa: BLE001 — report, don't 500-crash
        log.exception("LAN capture ingest failed")
        return jsonify(error=str(exc)[:200]), 500
    if entry_id is None:
        return jsonify(status="duplicate"), 200    # idempotent: a retried POST
    return jsonify(status="imported", id=entry_id), 200


@app.get("/api/lan_info")
def _api_lan_info():
    """Loopback-only: the desktop UI reads this to show the pairing details."""
    enabled, port = _lan_settings()
    return jsonify(enabled=enabled, port=port, token=_lan_token(), ips=_lan_ips())


_lan_lock = threading.Lock()
_lan_server: dict = {"srv": None, "thread": None, "port": None}


def _apply_lan_state() -> None:
    """Start, stop, or restart the LAN listener to match the current setting.
    Idempotent — safe to call from startup AND from the settings-save path, so
    the desktop toggle takes effect live, no app restart."""
    enabled, port = _lan_settings()
    with _lan_lock:
        running = _lan_server["srv"] is not None
        if enabled and running and _lan_server["port"] == port:
            return                                     # already in the desired state
        if not enabled and not running:
            return
        if running:                                    # stop (disabled or port changed)
            try:
                _lan_server["srv"].shutdown()
            except Exception:
                pass
            _lan_server.update(srv=None, thread=None, port=None)
        if not enabled:
            log.info("LAN capture off")
            return
        from werkzeug.serving import make_server
        try:
            srv = make_server("0.0.0.0", port, lan_app, threaded=True)
        except OSError as exc:
            log.warning("LAN capture listener not started (%s)", exc)
            return
        t = threading.Thread(target=srv.serve_forever, daemon=True, name="lan-capture")
        t.start()
        _lan_server.update(srv=srv, thread=t, port=port)
        log.info("LAN capture on 0.0.0.0:%d  token=%s  ips=%s",
                 port, _lan_token(), ", ".join(_lan_ips()) or "?")


if __name__ == "__main__":
    # Make existing user data portable (absolute -> data-root-relative paths).
    _migrate_stored_paths()
    # Warm the offline check indexes (the renewals CSV is ~40 MB) so the first
    # manual-entry submission doesn't stall while they load, and the drive
    # list so the first file-browser open is instant. Deferred a few seconds:
    # the CSV parse is CPU-bound and would otherwise contend (GIL) with the
    # first page load + client_state GET exactly when the window is opening.
    # The getters stay lazy, so an early submission just loads inline as before.
    def _warm_slow_indexes():
        time.sleep(6)
        checks.get_renewals()
        checks.get_whl_catalog()
        _drives()
    threading.Thread(target=_warm_slow_indexes, daemon=True).start()
    # Cloud capture autosync (interval read from settings each tick; 0 = off).
    threading.Thread(target=_cloud_autosync_loop, daemon=True).start()
    # LAN capture listener (offline phone -> desktop); off unless opted in.
    _apply_lan_state()
    # Activity mirror: local jsonl -> cloud events, as the signed-in user.
    threading.Thread(target=_push_events_loop, daemon=True, name="event-push").start()
    # WHL_PORT lets a second instance run on another port (a distinct origin,
    # so its localStorage/client-state can't collide with the main one) — used
    # to test against a throwaway WHL_DATA_ROOT without touching live state.
    _migrate_secrets_from_client_state()   # lift any secrets off the synced blob
    _sync_profile_mistral_key()            # user cloud data; cached for offline use
    port = int(os.environ.get("WHL_PORT") or 5001)
    log.info("Library Tool on 127.0.0.1:%d - DATA_ROOT=%s", port, lib.DATA_ROOT)
    app.run(host="127.0.0.1", port=port, debug=False)
