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
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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


_init_logging()


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
# trust a plain-text name. Settings and API keys never leave the machine.

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
    """GoTrue wants a project API key in `apikey`; the anon key is the right
    one, but the service key works too, so use whichever Settings holds.

    Nothing configured is NOT unconfigured: the app ships knowing its own
    cloud (cloud_defaults), so accounts work on a fresh install with no keys
    entered. Settings override; a custom project URL with no key of its own
    stays unconfigured rather than pairing with the default key."""
    s = _client_settings()
    url = str(s.get("supabaseUrl") or "").strip() or cloud_defaults.SUPABASE_URL
    key = (str(s.get("supabaseAnonKey") or "").strip()
           or str(s.get("supabaseKey") or "").strip())
    if not key and url == cloud_defaults.SUPABASE_URL:
        key = cloud_defaults.SUPABASE_ANON_KEY
    return {"url": url, "key": key} if url and key else None


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
        ses = sauth.sign_up(cfg, email, password, name)
    except sauth.AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if ses is None:     # project requires email confirmation (the default)
        return jsonify({"ok": True, "confirm": True})
    ses = _adopt_profile(cfg, ses)
    _store_session(ses)
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
                          "events?select=at,actor,verb,subject,n"
                          f"&order=at.desc&limit={int(limit)}", timeout=8.0) or []
    except sauth.AuthError as exc:
        log.warning("cloud feed unavailable: %s", exc)
        _cloud_feed_cache["fail_at"] = now
        return None
    out = [{"ts": r.get("at"), "actor": r.get("actor"), "verb": r.get("verb"),
            "subject": r.get("subject"), "n": r.get("n")} for r in rows]
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
        dropped = since > 0 and _log_ring and _log_ring[0]["seq"] > since + 1
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

# The field set mirrors what a WHL catalog entry needs. pdf_source is the
# source URL; pdf_file is the local PDF attached for the actual submission
# (the PRIMARY PDF source); pdf_sources lists SECONDARY PDFs — other scans
# of the same book, each {id, path}, so OCR files can belong to a specific
# scan; ocr_active/ocr_verified/ocr_quality track the entry folder's OCR
# files; title_pages lists PDF pages marked as title pages (metadata
# extraction uses them later); attention flags an entry as needing attention.
_BUILD_FIELDS = ("published_slug",
                 "title", "subtitle", "authors", "year", "publisher",
                 "publisher_city", "edition", "language", "pages",
                 "categories", "category_ids", "description",
                 "pdf_source", "pdf_file",
                 "pdf_sources",
                 "source_url", "notes", "status",
                 "ocr_active", "ocr_verified", "ocr_quality",
                 "title_pages", "attention")

# The structured exceptions to the str() coercion below. `categories` (flat
# text) is deprecated in favour of category_ids — kept as display fallback.
_BUILD_LIST_FIELDS = ("pdf_sources", "category_ids")


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


@app.route("/api/builds")
def api_builds():
    return jsonify({"builds": lib.load_json(BUILDS_PATH, {})})


@app.route("/api/builds", methods=["POST"])
def api_builds_create():
    payload = request.get_json(silent=True) or {}
    seed = payload.get("build") or {}
    builds = lib.load_json(BUILDS_PATH, {})
    build = {f: str(seed.get(f, "") or "").strip() for f in _BUILD_FIELDS
             if f not in _BUILD_LIST_FIELDS}
    build["pdf_sources"] = _clean_pdf_sources(seed.get("pdf_sources"))
    build["category_ids"] = _clean_category_ids(seed.get("category_ids"),
                                                lib.load_taxonomy()["nodes"])
    if build["status"] not in _BUILD_STATUSES:
        build["status"] = "draft"
    build["id"] = lib.gen_id(set(builds))
    build["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    build["updated_at"] = build["created_at"]
    builds[build["id"]] = build
    lib.save_json(BUILDS_PATH, builds)
    activity("created", "draft entry", detail=build.get("title", ""))
    return jsonify({"ok": True, "build": build})


@app.route("/api/builds/<build_id>", methods=["PATCH"])
def api_builds_update(build_id: str):
    builds = lib.load_json(BUILDS_PATH, {})
    if build_id not in builds:
        abort(404)
    payload = request.get_json(silent=True) or {}
    b = builds[build_id]
    was = b.get("status")
    for f in _BUILD_FIELDS:
        if f not in payload:
            continue
        if f == "pdf_sources":
            b[f] = _clean_pdf_sources(payload[f])
        elif f == "category_ids":
            b[f] = _clean_category_ids(payload[f], lib.load_taxonomy()["nodes"])
        else:
            b[f] = str(payload[f] or "").strip()
    if b.get("status") not in _BUILD_STATUSES:
        b["status"] = "draft"
    b["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lib.save_json(BUILDS_PATH, builds)
    # only the status transition is worth a feed entry; every keystroke is not
    if b["status"] != was and b["status"] in ("ready", "uploaded"):
        activity("uploaded" if b["status"] == "uploaded" else "verified", "book",
                 detail=b.get("title", ""))
    return jsonify({"ok": True, "build": b})


@app.route("/api/builds/<build_id>", methods=["DELETE"])
def api_builds_delete(build_id: str):
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
    builds = lib.load_json(BUILDS_PATH, {})
    builds[bid] = build
    lib.save_json(BUILDS_PATH, builds)
    return jsonify({"ok": True, "build": build})


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
                b["updated_at"] = _tax_ts()
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
    ids_for = lambda labs: [by_name[l.lower()] for l in labs   # noqa: E731
                            if l.lower() in by_name]
    assigned = 0
    with _builds_lock:
        builds = lib.load_json(BUILDS_PATH, {})
        for sid, key, labs in pending:
            if sid == "builds" and key in builds \
                    and not builds[key].get("category_ids"):
                builds[key]["category_ids"] = ids_for(labs)
                builds[key]["updated_at"] = _tax_ts()
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


_remote_pdf_lock = threading.Lock()


def _remote_pdf_cache(url: str) -> Path:
    """Fetch a remote PDF once into downloads/cache/ and return the path.
    Browsers can't iframe third-party PDFs (X-Frame-Options), so remote
    sources are proxied through here. Raises ValueError on fetch failure.

    Downloads land in a temp file and are renamed into place under a lock:
    the viewer fires several concurrent requests for the same URL (iframe
    GET + HEAD size probe + OCR text fetch), and none of them may see a
    half-written file. A response that isn't a PDF is rejected instead of
    being cached forever."""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("not an http(s) URL")
    import hashlib
    cache_dir = lib.DATA_ROOT / "downloads" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_dir / (hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".pdf")
    with _remote_pdf_lock:
        if p.exists():
            return p
        tmp = p.with_suffix(".fetch.tmp")
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": whl_client.USER_AGENT})
            with urllib.request.urlopen(req, timeout=90) as resp, \
                    open(tmp, "wb") as fh:
                import shutil
                shutil.copyfileobj(resp, fh)
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


def _entry_folder_info(build_id: str) -> dict:
    d = _entry_dir(build_id)
    ocr = []
    if (d / "ocr").is_dir():
        srcmap = _ocr_sources(build_id)
        for f in sorted((d / "ocr").glob("*.txt")):
            ocr.append({"name": f.name, "size": f.stat().st_size,
                        "src": srcmap.get(f.name) or "primary"})
    primary = _entry_primary_pdf(build_id)
    return {"exists": d.is_dir(), "path": str(d), "ocr": ocr,
            "preview": bool(primary), "primary_pdf": primary,
            "metadata": (d / "metadata.json").is_file()}


def _pdf_extract_text(p: Path, max_pages: int) -> tuple[int, int, str, int]:
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
        shown = min(total, max_pages)
        parts, with_text = [], 0
        for i in range(shown):
            text = doc[i].get_text().strip()
            if text:
                with_text += 1
            parts.append(f"--- page {i + 1} ---\n{text}")
    finally:
        doc.close()
    return total, shown, "\n\n".join(parts), with_text


def _preview_pdf(src: Path, pages: int) -> Path:
    """A compressed, truncated preview derivative, cached by mtime."""
    import hashlib
    cache = lib.DATA_ROOT / "downloads" / "cache" / "previews"
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(
        f"{src}|{src.stat().st_mtime}|{pages}".encode("utf-8")).hexdigest()[:16]
    out = cache / f"{key}.pdf"
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
                   and j.get("status") == "running"]
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
                    lib.save_json(BUILDS_PATH, builds)
                    src = d / "primary.pdf"
                legacy.unlink()
                notes.append("renamed preview.pdf to primary.pdf")
        except Exception as exc:
            notes.append(f"preview failed: {exc}")
        try:
            total, shown, text, with_text = _pdf_extract_text(src, 400)
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
                    b["updated_at"] = datetime.now(timezone.utc).isoformat(
                        timespec="seconds")
                    lib.save_json(BUILDS_PATH, builds)

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
    for bid in builds:
        info = _entry_folder_info(bid)
        if info["exists"]:
            out[bid] = {"ocr": info["ocr"], "preview": info["preview"],
                        "primary_pdf": info["primary_pdf"]}
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
    (d / name).write_text(str(p.get("text") or ""),
                          encoding="utf-8", errors="replace")
    if "src" in p:
        src_key = _valid_src_key(builds[build_id], p.get("src"))
        if src_key:
            _ocr_set_source(build_id, name, src_key)
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
    try:
        import fitz  # PyMuPDF: renders these pages already, so no new dependency
    except ImportError:
        return jsonify({"ok": False, "error": "PyMuPDF is not installed"}), 501
    from statistics import median
    doc = fitz.open(str(p))
    try:
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
    finally:
        doc.close()
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


@app.route("/api/pdf/pageimg")
def api_pdf_pageimg():
    """One page of a local PDF rendered as an image (?path=&page=N&w=W).
    Rendered via PyMuPDF and cached on disk by path+mtime+page+width.
    JPEG: a scanned page as PNG runs 500 KB+ against ~80 KB, and encodes
    slower too — these are photographs, not line art. Older caches hold
    .png files; they stay valid and are served as-is."""
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
        import fitz  # PyMuPDF
    except ImportError:
        return jsonify({"ok": False, "error": "PyMuPDF is not installed"}), 501
    import hashlib
    cache = lib.DATA_ROOT / "downloads" / "cache" / "pages"
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(
        f"{p}|{p.stat().st_mtime}|{page}|{w}".encode("utf-8")).hexdigest()[:16]
    old = cache / f"{key}.png"
    if old.is_file():
        return send_file(old, mimetype="image/png", conditional=True)
    out = cache / f"{key}.jpg"
    if not out.is_file():
        doc = fitz.open(str(p))
        try:
            if page > doc.page_count:
                abort(404)
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
                return send_file(old, mimetype="image/png", conditional=True)
            tmp.replace(out)
        finally:
            doc.close()
    return send_file(out, mimetype="image/jpeg", conditional=True)


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


def _ocr_claude(png: bytes, cfg: dict) -> str:
    key = (cfg.get("claude_key") or "").strip()
    if not key:
        raise RuntimeError("Anthropic API key not configured (Settings > OCR)")
    import base64
    model = (cfg.get("claude_model") or "").strip() or "claude-haiku-4-5-20251001"
    body = json.dumps({
        "model": model,
        "max_tokens": 8192,
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
                          text: str) -> str:
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
            meta["images"][name] = dict(im["bbox"] or {}, page=page)
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
    for item in job["pages"]:
        n, svc = item["page"], item["service"]
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
                        job["build_id"], n, result["images"], text)
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
            item["status"] = f"error: {exc}"
            job["errors"] += 1
        job["done"] += 1
    job["status"] = "done" if not job["errors"] else "done (with errors)"


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
    job_id = lib.gen_id(set(_ocr_jobs))
    # the merged result belongs to the PDF it was read from (?src= key);
    # an unknown/removed key is refused rather than recorded
    src_key = _valid_src_key(lib.load_json(BUILDS_PATH, {}).get(build_id, {}),
                             p.get("src"))
    if src_key:
        _ocr_set_source(build_id,
                        _ocr_name(str(p.get("target") or "compiled.txt")),
                        src_key)
    job = {
        "id": job_id, "build_id": build_id, "pdf": str(pdf),
        "target": str(p.get("target") or "compiled.txt"),
        "src_key": src_key or "primary",     # word boxes are stored per source
        "pages": pages, "done": 0, "errors": 0, "width": width,
        "status": "running",
        "cfg": {k: p.get(k) for k in ("tesseract", "claude_key", "claude_model",
                                      "aws_key", "aws_secret", "aws_region",
                                      "mistral_key")},
    }
    with _ocr_jobs_lock:
        _ocr_jobs[job_id] = job
    threading.Thread(target=_ocr_job_run, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True, "job": _ocr_job_state(job)})


def _ocr_job_state(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != "cfg"}


@app.route("/api/ocr/job/<job_id>")
def api_ocr_job(job_id: str):
    job = _ocr_jobs.get(job_id)
    if not job:
        abort(404)
    return jsonify({"ok": True, "job": _ocr_job_state(job)})


# --- PDF page deletion ---------------------------------------------------------------

def _renumber_layout_words(build_id: str, src_key: str, removed: list[int]) -> None:
    """Drop the deleted pages' word boxes from ONE source's sidecar map and
    shift the rest down, matching _renumber_marked_text on the compiled files.
    Source-scoped like that renumber, so a deletion on one scan never disturbs
    another's boxes. Its own lock, so call it OUTSIDE the caller's
    _ocr_merge_lock block (non-reentrant)."""
    meta_path = _entry_dir(build_id) / "ocr" / "layout.json"
    if not meta_path.is_file():
        return
    removed_set = set(removed)
    with _ocr_merge_lock:
        meta = lib.load_json(meta_path, {})
        wmap = meta.get("words")
        if not isinstance(wmap, dict):
            return
        pages = wmap.get(src_key or "primary")
        if not isinstance(pages, dict):
            return
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
        lib.save_json(meta_path, meta)


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
    running = [j for j in _ocr_jobs.values()
               if j.get("build_id") == build_id and j.get("status") == "running"]
    if running:
        return jsonify({"ok": False,
                        "error": "an OCR job is running for this book — "
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
    # title pages are counted on the PRIMARY PDF; a secondary's deletions
    # don't move them
    titles = [] if src_key != "primary" else \
        [int(x) for x in str(b.get("title_pages") or "").split(",")
         if x.strip().isdigit()]
    if titles:
        remapped = []
        for t in titles:
            if t in set(pages):
                continue
            remapped.append(t - sum(1 for r in pages if r < t))
        b["title_pages"] = ",".join(str(t) for t in remapped)
        lib.save_json(BUILDS_PATH, builds)
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


# --- master list -> Google Sheets sync ----------------------------------------------

@app.route("/api/master/sync", methods=["POST"])
def api_master_sync():
    """Publish the master list (plus manual entries) to a Google Sheet.
    Body: {spreadsheet_id, service_account_file, sheet_name?}. Requires a
    Google service-account JSON key — TODO: verify once the user has one."""
    p = request.get_json(silent=True) or {}
    sheet_id = str(p.get("spreadsheet_id") or "").strip()
    keyfile = str(p.get("service_account_file") or "").strip()
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
    try:
        max_pages = max(1, min(500, int(request.args.get("pages") or 100)))
    except ValueError:
        max_pages = 100
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
        state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lib.save_json(lib.CLIENT_STATE_PATH, state)
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
    """Arbitrary non-column bibliographic facts: a flat str->str dict."""
    if not isinstance(v, dict):
        return {}
    return {str(k).strip()[:64]: str(x)[:500] for k, x in v.items()
            if str(k).strip() and str(x or "").strip()}


def _clean_images(v) -> list[str]:
    """Entry image paths: DATA_ROOT-relative, image suffixes only."""
    if not isinstance(v, list):
        return []
    out = []
    for p in v:
        s = str(p or "").replace("\\", "/").strip().lstrip("/")
        if not s or ".." in s:
            continue
        if s.lower().rsplit(".", 1)[-1] in ("jpg", "jpeg", "png", "webp"):
            out.append(s)
    return out


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
def _client_settings():
    return (lib.load_json(lib.CLIENT_STATE_PATH, {}) or {}).get("settings") or {}


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


def _db_urls():
    urls = _client_settings().get("dbUrls")
    return urls if isinstance(urls, dict) else {}


def _run_db_download(name, url, rel):
    dest = lib.DATA_ROOT / rel
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
        p = lib.DATA_ROOT / rel
        out[name] = {
            "label": label, "path": rel,
            "present": p.exists(),
            "size": p.stat().st_size if p.exists() else 0,
            "url": str(urls.get(name) or ""),
            "job": _db_jobs.get(name),
        }
    return jsonify({"data_root": str(lib.DATA_ROOT), "targets": out})


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
        _REG_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _REG_CACHE_PATH.write_text(json.dumps(cache), "utf-8")
    except Exception:
        pass


def _reg_cache_key(title: str, author: str, sources) -> str:
    def n(s):
        return " ".join(str(s or "").lower().split())
    return n(title) + "|" + n(author) + "|" + ",".join(sources)


@app.route("/api/copyright/registration")
def api_copyright_registration():
    """Look up an original copyright REGISTRATION for a book (the left half of
    the split copyright tag). Network + cached; the client passes the enabled
    sources (from settings) as a comma list."""
    title = (request.args.get("title") or "").strip()
    author = (request.args.get("author") or "").strip()
    year = (request.args.get("year") or "").strip()
    sources = tuple(s for s in (request.args.get("sources") or "cprs").split(",")
                    if s in copyreg.SOURCES)
    if not title or not sources:
        return jsonify({"found": False, "sources": [], "match": None})
    key = _reg_cache_key(title, author, sources)
    with _reg_cache_lock:
        cache = _reg_cache_load()
        if key in cache:
            return jsonify(cache[key])
    result = copyreg.registration_lookup(title, author, year, sources)  # network
    with _reg_cache_lock:
        cache = _reg_cache_load()
        cache[key] = result
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
    key = _reg_cache_key(title, author, ("__status__", year))
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
    """The service-role config for the owner pipelines (captures, publish,
    store sync). The URL defaults to the shipped project; the service key is
    a secret and must always come from Settings."""
    s = _client_settings()
    url = str(s.get("supabaseUrl") or "").strip() or cloud_defaults.SUPABASE_URL
    key = str(s.get("supabaseKey") or "").strip()
    return {"url": url, "key": key} if url and key else None



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
_builds_lock = threading.Lock()      # whl_builds.json is read-modify-written
_publish: dict = {"running": False, "build": "", "stage": "idle", "sent": 0,
                  "total": 0, "error": "", "url": "", "slug": ""}


def _r2_cfg() -> dict:
    s = _client_settings()
    return {"account": str(s.get("r2Account") or "").strip(),
            "bucket": str(s.get("r2Bucket") or "").strip(),
            "key_id": str(s.get("r2KeyId") or "").strip(),
            "secret": str(s.get("r2Secret") or "").strip(),
            "public_base": str(s.get("r2PublicBase") or "").strip()}


def _volume_row(b: dict, slug: str, url: str, path: str, size: int, actor: str) -> dict:
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
            "edition": b.get("edition") or "", "language": b.get("language") or "",
            "pages": num(b.get("pages")), "categories": cats,
            "category_paths": paths,
            "description": b.get("description") or "",
            "source_url": b.get("source_url") or b.get("pdf_source") or "",
            "pdf_url": url, "pdf_path": path, "pdf_bytes": size,
            "uploaded_by_name": actor,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


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


def _unpublish_object(cloud: dict, slug: str, path: str) -> None:
    """Best-effort removal of an object whose catalogue row never landed."""
    try:
        if path:
            sbase.delete_objects(cloud, "volumes", [path])
        else:
            r2.delete(_r2_cfg(), f"volumes/{slug}.pdf")
        log.warning("rolled back orphaned object for %s", slug)
    except Exception as exc:
        log.error("could not roll back orphaned object for %s: %s", slug, exc)


def _publish_run(bid: str, actor: str) -> None:
    def stage(name, **kw):
        with _publish_lock:
            _publish.update(stage=name, **kw)
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
        try:
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

            stage("recording")
            row = _volume_row(b, slug, url, path, size, actor)
            try:
                sbase.upsert_volume(cloud, row)
            except sbase.SyncError as exc:
                # A live project that hasn't re-run schema.sql lacks the
                # category_paths column; the book still deserves to publish.
                if "category_paths" not in str(exc):
                    raise
                row.pop("category_paths", None)
                sbase.upsert_volume(cloud, row)
                log.warning("volumes.category_paths missing on the cloud "
                            "project — re-run docs/cloud/schema.sql")
        except Exception:
            _unpublish_object(cloud, slug, path)
            for name_i, path_i in extras:
                _unpublish_object(cloud, name_i[:-4], path_i)
            raise

        # re-read: the upload took minutes, and another writer may have touched
        # builds meanwhile. Only this build's fields are ours to change.
        with _builds_lock:
            fresh = lib.load_json(BUILDS_PATH, {})
            row = fresh.get(bid)
            if row is not None:
                row["status"] = "uploaded"
                row["published_slug"] = slug
                row["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                lib.save_json(BUILDS_PATH, fresh)
        activity("published", "book", actor=actor or None)
        log.info("published volume %s (%.0f MB) -> %s", slug, size / 1e6, url)
        stage("done", url=url, slug=slug, error="")
    except Exception as exc:
        log.error("publish failed for build %s", bid, exc_info=exc)
        stage("error", error=f"{type(exc).__name__}: {exc}")
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
    if not _cloud_cfg():
        return jsonify({"ok": False, "error": "Supabase is not configured (Settings > Sync)"}), 400
    with _publish_lock:
        if _publish["running"]:
            return jsonify({"ok": False, "error": "a publish is already running"}), 409
        _publish.update(running=True, build=bid, stage="starting", sent=0, total=0,
                        error="", url="", slug="")
    threading.Thread(target=_publish_run, args=(bid, _actor()), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/volumes/publish/status")
def api_volumes_publish_status():
    with _publish_lock:
        return jsonify(dict(_publish, store="r2" if r2.configured(_r2_cfg()) else "supabase"))

def _capture_note(cap: dict, errors: list[str]) -> str:
    bits = ["Captured via phone"]
    if cap.get("device"):
        bits.append(f"({cap['device']})")
    if cap.get("created_at"):
        bits.append(str(cap["created_at"])[:19])
    note = " ".join(bits)
    if cap.get("note"):
        note += "\n" + str(cap["note"])
    if errors:
        note += "\n" + "; ".join(errors)
    return note


def _import_capture(cfg: dict, cap: dict, mistral_key: str,
                    delete_remote: bool) -> str:
    """One pending capture -> a manual entry. Returns 'imported' | 'skipped'."""
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
    result = capture.process_capture(raw_photos, mistral_key)

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

    sbase.mark_capture(cfg, cap["id"], "imported")
    # a phone capture is attributed to the device, not to whoever ran the sync
    activity("captured", "book", actor=str(cap.get("device") or "phone")[:60],
             detail=entry.get("title", ""))
    # keep the cloud copies when OCR/extraction had trouble — the originals
    # are local too, but leaving the remote set makes recovery foolproof
    if delete_remote and not result["errors"]:
        try:
            sbase.delete_photos(cfg, photo_paths)
        except sbase.SyncError:
            pass                                   # cleanup is best-effort
    return "imported"


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
    """One full sync pass: import pending captures, push the books mirror.

    Everything after the flag is claimed runs inside try/finally, and ANY
    exception lands in `result` — the flag can never stay stuck on, and a
    failed pass can't masquerade as the previous run's outcome."""
    cfg = _cloud_cfg()
    if not cfg:
        return {"ok": False, "error": "Supabase URL/key not configured"}
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
        for cap in sbase.list_pending_captures(cfg):
            try:
                if _import_capture(cfg, cap, mistral_key, delete_remote) == "imported":
                    imported += 1
                else:
                    skipped += 1
            except Exception as exc:      # one bad capture must not stop the rest
                errors.append(f"capture {str(cap.get('id'))[:8]}: {exc}")
        pushed = 0
        try:
            pushed = sbase.push_books(cfg, _books_mirror_rows())
        except sbase.SyncError as exc:
            errors.append(f"books mirror: {exc}")
        result = {"ok": not errors, "imported": imported, "skipped": skipped,
                  "books_pushed": pushed, "errors": errors}
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
            log.info("cloud sync done: %d imported, %d skipped, %d books pushed",
                     result.get("imported", 0), result.get("skipped", 0),
                     result.get("books_pushed", 0))
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
            if minutes <= 0 or not _cloud_cfg():
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
    if not _cloud_cfg():
        return jsonify({"ok": False, "error": "Supabase URL/key not configured"})
    with _cloudsync_lock:
        already = _cloudsync["running"]
    if not already:
        threading.Thread(target=_cloud_sync_run, daemon=True).start()
    return jsonify({"ok": True, "started": not already})


@app.route("/api/cloudsync/status")
def api_cloudsync_status():
    with _cloudsync_lock:
        out = dict(_cloudsync)
    out["configured"] = bool(_cloud_cfg())
    return jsonify(out)


@app.route("/api/cloudsync/test")
def api_cloudsync_test():
    cfg = _cloud_cfg()
    if not cfg:
        return jsonify({"ok": False, "error": "Supabase URL/key not configured"})
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
    builds = lib.load_json(BUILDS_PATH, {})
    changed = False
    for b in builds.values():
        rel = _relativize_data_path(b.get("pdf_file", ""))
        if rel != (b.get("pdf_file") or ""):
            b["pdf_file"] = rel
            changed = True
    if changed:
        lib.save_json(BUILDS_PATH, builds)

    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
    changed = False
    for e in entries.values():
        rel = _relativize_data_path(e.get("local_pdf", ""))
        if rel != (e.get("local_pdf") or ""):
            e["local_pdf"] = rel
            changed = True
    if changed:
        lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)


if __name__ == "__main__":
    # Make existing user data portable (absolute -> data-root-relative paths).
    _migrate_stored_paths()
    # Warm the offline check indexes (the renewals CSV is ~40 MB) so the first
    # manual-entry submission doesn't stall while they load, and the drive
    # list so the first file-browser open is instant.
    threading.Thread(
        target=lambda: (checks.get_renewals(), checks.get_whl_catalog(),
                        _drives()),
        daemon=True,
    ).start()
    # Cloud capture autosync (interval read from settings each tick; 0 = off).
    threading.Thread(target=_cloud_autosync_loop, daemon=True).start()
    # Activity mirror: local jsonl -> cloud events, as the signed-in user.
    threading.Thread(target=_push_events_loop, daemon=True, name="event-push").start()
    # WHL_PORT lets a second instance run on another port (a distinct origin,
    # so its localStorage/client-state can't collide with the main one) — used
    # to test against a throwaway WHL_DATA_ROOT without touching live state.
    port = int(os.environ.get("WHL_PORT") or 5001)
    log.info("Library Tool on 127.0.0.1:%d - DATA_ROOT=%s", port, lib.DATA_ROOT)
    app.run(host="127.0.0.1", port=port, debug=False)
