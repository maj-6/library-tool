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

import base64
import collections
import contextlib
import functools
import hashlib
import importlib.util
import json
import logging
import math
import os
import re
import shutil
import stat
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

import desktop_transport
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.exceptions import HTTPException

# Make the installable engine package and transitional tools/ modules
# importable when this file is launched directly from a source checkout.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tools"))
import capture_pipeline as capture  # noqa: E402
import catalog_checks as checks  # noqa: E402
import cloud_defaults  # noqa: E402
import layout_roles  # noqa: E402
import libformat  # noqa: E402
import replica_service  # noqa: E402
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
from librarytool.adapters.lib_archive import (  # noqa: E402
    ExistingItemLibArchivePlanner,
    LibArchiveLimits,
)
from librarytool.adapters.filesystem.recoverable_write_set import (  # noqa: E402
    RecoverableWriteSet,
)
from librarytool.adapters.filesystem.whl_catalogue_codec import (  # noqa: E402
    WhlCatalogueItemCodec,
)
from librarytool.adapters.windows.secret_store import (  # noqa: E402
    SecretCredentialNotConfiguredError,
    SecretIdRegistry,
    SecretStoreHealth,
    WindowsDpapiSecretStoreRepository,
)
from librarytool_http import (  # noqa: E402
    create_provider_discovery_blueprint,
    create_text_layer_blueprint,
)
from librarytool.composition.filesystem import (  # noqa: E402
    CatalogueBindings,
    FilesystemEnginePaths,
    InterchangeBindings,
    ItemLifecycleBindings,
    ReplicaBindings,
    RepresentationBindings,
    SecretStoreBindings,
    TranslationBindings,
)
from librarytool.composition.first_party import (  # noqa: E402
    first_party_module_contributions,
)
from librarytool.composition.host import (  # noqa: E402
    FilesystemEngineConfig,
    FilesystemEngineSession,
    FilesystemHostBindings,
    JobHistoryBindings,
    open_filesystem_engine,
)
from librarytool.engine.contracts import (  # noqa: E402
    ItemDescriptor,
    LayoutFamilyQuery,
    PageKey,
    RecompileRegionPagesCommand,
    ReplaceRegionPageCommand,
    ReviewRegionProposalCommand,
)
from librarytool.engine.errors import (  # noqa: E402
    ConflictError as EngineConflictError,
    EngineError,
    NotFoundError as EngineNotFoundError,
    PreconditionRequiredError as EnginePreconditionRequiredError,
    RepositoryError as EngineRepositoryError,
    ValidationError as EngineValidationError,
)
from librarytool.engine.jobs import (  # noqa: E402
    ACTIVE_JOB_STATES,
    PUBLIC_JOB_FIELDS,
    JobManager,
)
from librarytool.engine.item_commands import (  # noqa: E402
    CreateItemCommand,
    ItemCommandService,
    ItemDraft,
    ItemPatch,
    ItemRecordSnapshot,
    UpdateItemCommand,
)
from librarytool.engine.item_lifecycle import (  # noqa: E402
    DeleteItemCommand as LifecycleDeleteItemCommand,
    ItemLifecycleResult,
    ItemLifecycleService,
    RestoreItemCommand,
)
from librarytool.engine.items import ItemQueryService  # noqa: E402
from librarytool.engine.interchange import (  # noqa: E402
    ImportLibCommand,
    LibInterchangeService,
    OpenLibCommand,
    OpenLibService,
)
from librarytool.engine.replica import ReplicaApplicationService  # noqa: E402
from librarytool.engine.secret_ids import (  # noqa: E402
    LEGACY_SECRET_IDS,
    LEGACY_SECRET_KEYS,
)
from librarytool.engine.secret_store import (  # noqa: E402
    ClearSecretCommand,
    ReplaceSecretCommand,
    SecretStatus,
    SecretStoreService,
)
from librarytool.engine.representation_commands import (  # noqa: E402
    AttachRepresentationCommand,
    DetachRepresentationCommand,
    RepresentationAggregateSnapshot,
    RepresentationAttachmentDraft,
    RepresentationCommandService,
    RepresentationRecordSnapshot,
)
from librarytool.engine.runtime import (  # noqa: E402
    ITEM_LIFECYCLE_SERVICE,
    LIB_OPEN_SERVICE,
    REPRESENTATION_COMMAND_SERVICE,
    SECRET_STORE_SERVICE,
    LibraryEngine,
)
from librarytool.engine.text_layers import TextLayerService  # noqa: E402
from librarytool.engine.translation_contracts import (  # noqa: E402
    ReplaceTranslationPageCommand,
    TranslationSourceCanvas,
    TranslationSourceSnapshot,
)
from librarytool.engine.translations import (  # noqa: E402
    TranslationProvenanceService,
    TranslationService,
)
from librarytool.profiles import WhlBookItemCommandPolicy  # noqa: E402

_DESKTOP_CAPABILITY_HEADER = desktop_transport.CAPABILITY_HEADER
_DESKTOP_MODE = desktop_transport.CONFIG.mode
_DESKTOP_CAPABILITY_DIGEST = desktop_transport.CONFIG.capability_digest
_DESKTOP_PORT = desktop_transport.CONFIG.port
_DESKTOP_EXPECTED_HOST = desktop_transport.CONFIG.expected_host
_DESKTOP_EXPECTED_ORIGIN = desktop_transport.CONFIG.expected_origin

# Importing the compatibility transport must not claim a workspace. The first
# request, or the explicit __main__ startup barrier, opens one complete session
# and publishes these aliases together for processors that have not crossed the
# engine boundary yet.
_engine_session: FilesystemEngineSession | None = None
_engine_write_set: RecoverableWriteSet | None = None
_job_manager: JobManager | None = None
_translation_provenance: TranslationProvenanceService | None = None
_jobs: dict | None = None
_jobs_events: dict | None = None
_jobs_lock: threading.Lock | None = None

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
app.register_blueprint(
    create_provider_discovery_blueprint(lambda: _library_engine())
)
app.register_blueprint(create_text_layer_blueprint(lambda: _library_engine()))
# Jinja compiles index.html once and caches it when debug is off, while static/
# is read from disk on every request. Editing the template therefore served a NEW
# app.js against an OLD DOM until someone restarted the server -- and one missing
# element kills every listener registered after it in the same init function.
# Stat the template instead: one stat per page load, and the whole class of bug
# (twice now: the reason popover, then the OCR Layout button) goes away.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


_TRUSTED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
_DESKTOP_CSP = "; ".join((
    "default-src 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self' data:",
    "img-src 'self' data: blob: https:",
    "connect-src 'self'",
    "frame-src 'self' blob:",
    "form-action 'self'",
    "frame-ancestors 'none'",
))
_RESOURCE_CSP = "; ".join((
    "default-src 'none'",
    "base-uri 'none'",
    "style-src 'unsafe-inline'",
    "img-src 'self' data:",
    "frame-ancestors 'none'",
))


@app.before_request
def _reject_untrusted_host():
    """Reject DNS-rebinding requests before any local API can run.

    The administrative app is intentionally bound to IPv4 loopback.  Checking
    only credential routes is insufficient because other endpoints expose
    client state, local PDFs, and a fetch proxy.
    """
    request_host = (request.host or "").lower()
    if _DESKTOP_EXPECTED_HOST is not None:
        if request_host != _DESKTOP_EXPECTED_HOST:
            abort(403)
        return
    host = request_host.partition(":")[0].rstrip(".")
    if host not in _TRUSTED_LOOPBACK_HOSTS:
        abort(403)


@app.before_request
def _enforce_api_origin_and_desktop_capability():
    """Authenticate desktop API traffic before any engine or route executes."""
    if not request.path.startswith("/api/"):
        return
    origin = request.headers.get("Origin")
    if origin:
        expected = _DESKTOP_EXPECTED_ORIGIN or f"http://{request.host}"
        if not desktop_transport.origin_matches(origin, expected):
            abort(403)
    if (_DESKTOP_CAPABILITY_DIGEST is not None and
            not desktop_transport.capability_matches(
                request.headers.get(_DESKTOP_CAPABILITY_HEADER),
                _DESKTOP_CAPABILITY_DIGEST)):
        abort(401)


@app.before_request
def _ensure_engine_before_request():
    """Settle recovery and composition before dispatching any local API."""

    _ensure_engine_session()


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
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    resp.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), display-capture=(), "
        "usb=(), serial=(), bluetooth=()",
    )
    if _DESKTOP_CAPABILITY_DIGEST is not None and request.path.startswith("/api/"):
        # A custom request header does not participate in ordinary cache keys.
        # Never let a response authenticated for the app frame be reused by an
        # unauthenticated same-origin frame without contacting this guard again.
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Pragma"] = "no-cache"
    if resp.mimetype == "text/html":
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        csp = _DESKTOP_CSP if request.path == "/" else _RESOURCE_CSP
        resp.headers.setdefault("Content-Security-Policy", csp)
    if resp.status_code in (200, 304) and request.path.startswith("/static/"):
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
    """Return configured public auth identity without leasing a custom key.

    Nothing configured is NOT unconfigured: the app ships knowing its own
    cloud (cloud_defaults), so accounts work on a fresh install with no keys
    entered. Settings override; a custom project URL with no key of its own
    stays unconfigured rather than pairing with the default key. The owner
    service credential is deliberately ignored here: normal account and phone
    capture traffic must never depend on it.

    The bundled anon key is a public application identifier and may remain in
    this value. A custom key is represented only by configured status; its
    plaintext is added by :func:`_auth_execution_cfg` while provider calls run.
    """
    s = _client_settings()
    url = str(s.get("supabaseUrl") or "").strip() or cloud_defaults.SUPABASE_URL
    if not url:
        return None
    if url == cloud_defaults.SUPABASE_URL:
        return {"url": url, "key": cloud_defaults.SUPABASE_ANON_KEY}
    if _secret_is_configured("supabaseAnonKey"):
        return {"url": url}
    return None


@contextlib.contextmanager
def _auth_execution_cfg():
    """Lease a custom project key for one Supabase request family."""

    public = _auth_cfg()
    if not public:
        yield None
        return
    if "key" in public:
        cfg = dict(public)
        try:
            yield cfg
        finally:
            # The bundled value is public, but clearing the execution copy
            # keeps the same lifecycle as a custom protected value.
            cfg.pop("key", None)
        return
    with _lease_secret("supabaseAnonKey") as key:
        cfg = {**public, "key": key}
        try:
            yield cfg
        finally:
            cfg.pop("key", None)


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
    if not _auth_cfg():
        return None
    with _auth_lock:
        doc = _auth_doc()
        ses = doc.get("session")
        if not ses or not ses.get("refresh_token"):
            return None
        if time.time() < float(ses.get("expires_at") or 0) - 90:
            return ses
        try:
            with _auth_execution_cfg() as cfg:
                if not cfg:
                    return None
                fresh = sauth.refresh(cfg, ses["refresh_token"])
        except RuntimeError:
            log.warning("session refresh unavailable â€” protected auth key unavailable")
            return None
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
    if not _auth_cfg():
        return jsonify({"ok": False, "error": "Supabase is not configured (Settings > Integrations)"}), 400
    p = request.get_json(silent=True) or {}
    email = str(p.get("email") or "").strip()
    password = str(p.get("password") or "")
    if not email or not password:
        return jsonify({"ok": False, "error": "email and password are both required"}), 400
    try:
        with _auth_execution_cfg() as cfg:
            if not cfg:
                raise RuntimeError("protected auth configuration unavailable")
            ses = sauth.sign_in(cfg, email, password)
            ses = _adopt_profile(cfg, ses)
    except RuntimeError:
        return jsonify({"ok": False,
                        "error": "Protected credential storage is unavailable"}), 503
    except sauth.AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    _store_session(ses)
    _sync_profile_mistral_key()
    activity("signed in to", "the cloud", actor=ses["display_name"] or email)
    return jsonify({"ok": True, "email": ses["email"],
                    "display_name": ses["display_name"]})


@app.route("/api/auth/signup", methods=["POST"])
def api_auth_signup():
    if not _auth_cfg():
        return jsonify({"ok": False, "error": "Supabase is not configured (Settings > Integrations)"}), 400
    p = request.get_json(silent=True) or {}
    email = str(p.get("email") or "").strip()
    password = str(p.get("password") or "")
    name = str(p.get("display_name") or "").strip()[:60]
    if not email or not password:
        return jsonify({"ok": False, "error": "email and password are both required"}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "password must be at least 6 characters"}), 400
    try:
        with _auth_execution_cfg() as cfg:
            if not cfg:
                raise RuntimeError("protected auth configuration unavailable")
            ses = sauth.sign_up(cfg, email, password, name,
                                redirect_to=_email_confirm_redirect())
            if ses is not None:
                ses = _adopt_profile(cfg, ses)
    except RuntimeError:
        return jsonify({"ok": False,
                        "error": "Protected credential storage is unavailable"}), 503
    except sauth.AuthError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if ses is None:     # project requires email confirmation (the default)
        return jsonify({"ok": True, "confirm": True})
    _store_session(ses)
    _sync_profile_mistral_key()
    activity("signed in to", "the cloud", actor=ses["display_name"] or email)
    return jsonify({"ok": True, "email": ses["email"],
                    "display_name": ses["display_name"]})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    configured = bool(_auth_cfg())
    with _auth_lock:
        doc = _auth_doc()
        ses = doc.pop("session", None)
        # push_cursor stays: signing back in resumes the mirror, no re-push
        lib.save_json(AUTH_SESSION_PATH, doc)
    if configured and ses and ses.get("access_token"):
        try:
            with _auth_execution_cfg() as cfg:
                if cfg:
                    sauth.sign_out(cfg, ses["access_token"])
        except RuntimeError:
            log.warning("remote sign-out skipped â€” protected auth key unavailable")
    return jsonify({"ok": True})


# --- shared collections ---------------------------------------------------------
# A collection row is current, editable state shared with Book Capture.  A
# captured book's scan_collection / scan_from values are deliberately *not*
# edited here: they are a snapshot stored on the entry (see ingest_capture).
# Keeping the two paths separate is what makes a rename safe -- the row changes,
# while books already scanned under the old name continue to say so.

_COLLECTION_SELECT = ("id,name,from_place,created_by,updated_at,deleted,"
                      "merged_into")
_COLLECTION_FIELD_MAX = 80
COLLECTION_ALIASES_PATH = lib.OUTPUT_DIR / "collection_aliases.json"
_collection_alias_lock = threading.Lock()
_COLLECTION_ALIAS_VERSION = 2
_COLLECTION_PAGE_SIZE = 500


def _collection_text(value, *, required: bool = False) -> str:
    """Normalize the two phone-sized editable collection fields."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()[:_COLLECTION_FIELD_MAX]
    if required and not text:
        raise ValueError("collection name is required")
    return text


def _collection_payload() -> dict:
    """Return a JSON object for a mutation, rejecting scalar/array bodies."""
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


def _collection_id(value) -> str:
    """Return a canonical UUID, rejecting PostgREST-filter metacharacters."""
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("invalid collection id") from None


def _resolve_collection_alias_in(aliases: dict, collection_id: str) -> str:
    current = collection_id
    seen = set()
    while current and current not in seen:
        seen.add(current)
        nxt = str(aliases.get(current) or "").strip()
        if not nxt:
            break
        current = nxt
    return current


def _load_collection_aliases_unlocked() -> dict[str, str]:
    """Read only durable-marker aliases written by this implementation.

    The pre-``merged_into`` prototype wrote a bare ``{old: survivor}`` map.
    Those aliases cannot be proven authoritative when a normal tombstone is
    later resurrected by LWW, so deliberately ignore that legacy shape.
    """
    doc = lib.load_json(COLLECTION_ALIASES_PATH, {}) or {}
    if not isinstance(doc, dict) or doc.get("version") != _COLLECTION_ALIAS_VERSION:
        return {}
    aliases = doc.get("aliases")
    if not isinstance(aliases, dict):
        return {}
    return {str(key): str(value) for key, value in aliases.items()
            if str(key).strip() and str(value).strip()}


def _save_collection_aliases_unlocked(aliases: dict[str, str]) -> None:
    lib.save_json(COLLECTION_ALIASES_PATH, {
        "version": _COLLECTION_ALIAS_VERSION,
        "aliases": aliases,
    })


def _resolve_collection_alias(collection_id: str) -> str:
    """Follow the local old->survivor chain for late capture imports."""
    with _collection_alias_lock:
        aliases = _load_collection_aliases_unlocked()
    return _resolve_collection_alias_in(aliases, collection_id)


def _collection_alias_snapshot() -> dict[str, str]:
    """Return a stable copy for API clients after an authoritative refresh."""
    with _collection_alias_lock:
        return dict(_load_collection_aliases_unlocked())


def _remember_collection_alias(old_id: str, survivor_id: str) -> None:
    """Cache one authoritative ``merged_into`` edge, flattened and durable."""
    with _collection_alias_lock:
        aliases = _load_collection_aliases_unlocked()
        target = _resolve_collection_alias_in(aliases, survivor_id)
        existing = _resolve_collection_alias_in(aliases, old_id)
        if target == old_id:
            raise ValueError("collection merge would create an alias cycle")
        if existing != old_id and existing != target:
            raise ValueError("collection is already merged into another identity")
        aliases[old_id] = target
        # If A->B existed and B just merged into C, flatten A directly to C.
        for key in list(aliases):
            aliases[key] = _resolve_collection_alias_in(aliases, str(aliases[key]))
        _save_collection_aliases_unlocked(aliases)


def _replace_collection_aliases(rows: list[dict]) -> dict[str, str]:
    """Merge a full cloud marker snapshot into the irreversible alias cache."""
    fetched: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("deleted"):
            continue
        old_id = str(row.get("id") or "").strip()
        target = str(row.get("merged_into") or "").strip()
        if old_id and target and old_id != target:
            fetched[old_id] = target
    with _collection_alias_lock:
        # Fetch happens without holding this lock. A local RPC may commit and
        # cache a new marker before this older snapshot is installed; unioning
        # preserves it. Merge markers are irreversible, so every v2 cached edge
        # remains authoritative. Fetched direct edges win before flattening.
        aliases = _load_collection_aliases_unlocked()
        aliases.update(fetched)
        for key in list(aliases):
            aliases[key] = _resolve_collection_alias_in(aliases, aliases[key])
        _save_collection_aliases_unlocked(aliases)
    return aliases


def _refresh_collection_aliases(cfg: dict, token: str) -> list[dict]:
    """Fetch every current/tombstoned row and refresh durable merge aliases."""
    rows: list[dict] = []
    cursor = ""
    while True:
        after = (f"&id=gt.{urllib.parse.quote(cursor, safe='')}"
                 if cursor else "")
        page = sauth.rest(
            cfg, token, "GET",
            f"collections?select={_COLLECTION_SELECT}&order=id.asc"
            f"&limit={_COLLECTION_PAGE_SIZE}{after}",
            timeout=8.0,
        )
        if not isinstance(page, list):
            raise sauth.AuthError("collections pagination returned malformed data")
        if not page:
            break
        page_ids = [str(row.get("id") or "") if isinstance(row, dict) else ""
                    for row in page]
        if (any(not value for value in page_ids)
                or page_ids != sorted(page_ids)
                or len(set(page_ids)) != len(page_ids)
                or (cursor and page_ids[0] <= cursor)):
            raise sauth.AuthError("collections pagination did not advance")
        rows.extend(page)
        next_cursor = page_ids[-1]
        if cursor and next_cursor <= cursor:
            raise sauth.AuthError("collections pagination did not advance")
        cursor = next_cursor
    aliases = _replace_collection_aliases(rows)
    # A merge may have been performed from another desktop. Refresh is the
    # convergence boundary for already-imported local rows as well as future
    # captures; snapshot name/origin strings remain untouched.
    _repoint_collection_aliases(aliases)
    return rows


def _canonicalize_collection_link(entry) -> bool:
    """Heal one entry/book through merge aliases, changing only its link id."""
    if not isinstance(entry, dict):
        return False
    extra = entry.get("extra")
    if not isinstance(extra, dict):
        return False
    old_id = str(extra.get("scan_collection_id") or "").strip()
    if not old_id:
        return False
    current = _resolve_collection_alias(old_id)
    if not current or current == old_id:
        return False
    entry["extra"] = dict(extra, scan_collection_id=current)
    return True


def _collection_json(row: dict) -> dict:
    """Map the SQL-safe from_place name to the API/phone's `from`."""
    return {
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or ""),
        "from": str(row.get("from_place") or ""),
        "created_by": str(row.get("created_by") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "deleted": bool(row.get("deleted")),
        "merged_into": str(row.get("merged_into") or ""),
    }


def _collection_auth() -> tuple[dict, dict] | None:
    cfg = _auth_cfg()
    ses = _auth_session() if cfg else None
    if not cfg or not ses or not ses.get("access_token"):
        return None
    return cfg, ses


def _collection_error(exc: sauth.AuthError):
    # A protocol rejection is useful as-is; an offline/transport failure is a
    # temporary service outage, not an authentication failure.
    status = exc.status if exc.status and 400 <= exc.status < 500 else 503
    return jsonify({"ok": False, "error": str(exc)}), status


def _next_collection_timestamp(expected: str = "") -> str:
    """A logical revision one microsecond newer than a matched predecessor.

    The CAS baseline came from the database and is the only clock trusted here.
    Using this workstation's possibly-future wall clock would poison LWW for
    every other device. Creation and transactional merge use database time.
    """
    raw = str(expected or "").strip()
    if raw:
        try:
            prior = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if prior.tzinfo is None:
                prior = prior.replace(tzinfo=timezone.utc)
            else:
                prior = prior.astimezone(timezone.utc)
            return (prior + timedelta(microseconds=1)).isoformat()
        except (ValueError, OverflowError):
            pass
    return datetime.now(timezone.utc).isoformat()


def _collection_current(cfg: dict, token: str, cid: str) -> dict | None:
    rows = sauth.rest(
        cfg, token, "GET",
        f"collections?id=eq.{urllib.parse.quote(cid, safe='')}"
        f"&select={_COLLECTION_SELECT}&limit=1",
        timeout=8.0,
    ) or []
    return rows[0] if isinstance(rows, list) and rows else None


def _adopt_collection_marker(row: dict | None) -> dict[str, str]:
    """Cache/heal a durable marker learned on an optimistic-write conflict."""
    if not isinstance(row, dict) or not row.get("deleted"):
        return {}
    old_id = str(row.get("id") or "").strip()
    target = str(row.get("merged_into") or "").strip()
    if not old_id or not target:
        return {}
    _remember_collection_alias(old_id, target)
    aliases = _collection_alias_snapshot()
    _repoint_collection_aliases(aliases)
    return aliases


def _collection_miss(cfg: dict, token: str, cid: str):
    """Distinguish a deleted/missing row from an optimistic-write conflict."""
    try:
        current = _collection_current(cfg, token, cid)
    except sauth.AuthError as exc:
        return _collection_error(exc)
    if current:
        body = {"ok": False, "error": "collection changed on another device",
                "conflict": True, "current": _collection_json(current)}
        if aliases := _adopt_collection_marker(current):
            body["aliases"] = aliases
        return jsonify(body), 409
    return jsonify({"ok": False, "error": "collection not found"}), 404


@app.route("/api/collections")
def api_collections_list():
    """Read active shared collection rows as the signed-in contributor.

    Signed-out is a normal desktop mode, so GET returns an empty cloud list
    rather than making the whole catalogue look broken.  Entry snapshots are
    still available locally and the client surfaces them as unlinked records.
    """
    auth = _collection_auth()
    if not auth:
        return jsonify({"ok": True, "signed_in": False, "collections": [],
                        "aliases": _collection_alias_snapshot()})
    cfg, ses = auth
    try:
        # Fetch tombstones too: merged_into is the durable cross-desktop source
        # for the offline alias cache. Only active rows are returned to the UI.
        rows = _refresh_collection_aliases(cfg, ses["access_token"])
    except sauth.AuthError as exc:
        return _collection_error(exc)
    return jsonify({"ok": True, "signed_in": True,
                    "collections": [_collection_json(r) for r in rows
                                    if not r.get("deleted")],
                    "aliases": _collection_alias_snapshot()})


@app.route("/api/collections", methods=["POST"])
def api_collections_add():
    """Create a desktop-originated collection with a device-generated UUID."""
    auth = _collection_auth()
    if not auth:
        return jsonify({"ok": False, "error": "sign in to edit collections"}), 401
    try:
        payload = _collection_payload()
        name = _collection_text(payload.get("name"), required=True)
        from_place = _collection_text(payload.get("from"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    cfg, ses = auth
    row = {"id": str(uuid.uuid4()), "name": name, "from_place": from_place,
           "created_by": ses.get("user_id"), "deleted": False}
    try:
        saved = sauth.rest(
            cfg, ses["access_token"], "POST", "collections", [row],
            prefer="return=representation",
        ) or []
    except sauth.AuthError as exc:
        return _collection_error(exc)
    if not isinstance(saved, list) or not saved:
        return jsonify({"ok": False,
                        "error": "collection create returned no row"}), 502
    actual = saved[0]
    activity("created", "collection", detail=name)
    return jsonify({"ok": True, "collection": _collection_json(actual)})


@app.route("/api/collections/<collection_id>", methods=["PATCH"])
def api_collections_update(collection_id: str):
    """Rename/re-origin current collection state, never captured books."""
    auth = _collection_auth()
    if not auth:
        return jsonify({"ok": False, "error": "sign in to edit collections"}), 401
    try:
        payload = _collection_payload()
        cid = _collection_id(collection_id)
        expected = str(payload.get("expected_updated_at") or "").strip()
        if not expected:
            raise ValueError("expected_updated_at is required")
        changes = {}
        if "name" in payload:
            changes["name"] = _collection_text(payload.get("name"), required=True)
        if "from" in payload:
            changes["from_place"] = _collection_text(payload.get("from"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not changes:
        return jsonify({"ok": False, "error": "name or from is required"}), 400
    changes["updated_at"] = _next_collection_timestamp(expected)
    cfg, ses = auth
    try:
        saved = sauth.rest(
            cfg, ses["access_token"], "PATCH",
            f"collections?id=eq.{urllib.parse.quote(cid, safe='')}"
            f"&updated_at=eq.{urllib.parse.quote(expected, safe='')}"
            "&deleted=eq.false",
            changes, prefer="return=representation",
        ) or []
    except sauth.AuthError as exc:
        return _collection_error(exc)
    if not isinstance(saved, list) or not saved:
        return _collection_miss(cfg, ses["access_token"], cid)
    activity("updated", "collection", detail=str(saved[0].get("name") or ""))
    return jsonify({"ok": True, "collection": _collection_json(saved[0])})


@app.route("/api/collections/<collection_id>", methods=["DELETE"])
def api_collections_delete(collection_id: str):
    """Soft-delete a collection so entry references can never be orphaned."""
    auth = _collection_auth()
    if not auth:
        return jsonify({"ok": False, "error": "sign in to edit collections"}), 401
    try:
        payload = _collection_payload()
        cid = _collection_id(collection_id)
        expected = str(payload.get("expected_updated_at") or "").strip()
        if not expected:
            raise ValueError("expected_updated_at is required")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    changes = {"deleted": True,
               "updated_at": _next_collection_timestamp(expected)}
    cfg, ses = auth
    try:
        saved = sauth.rest(
            cfg, ses["access_token"], "PATCH",
            f"collections?id=eq.{urllib.parse.quote(cid, safe='')}"
            f"&updated_at=eq.{urllib.parse.quote(expected, safe='')}"
            "&deleted=eq.false",
            changes, prefer="return=representation",
        ) or []
    except sauth.AuthError as exc:
        return _collection_error(exc)
    if not isinstance(saved, list) or not saved:
        return _collection_miss(cfg, ses["access_token"], cid)
    activity("deleted", "collection", detail=str(saved[0].get("name") or ""))
    return jsonify({"ok": True, "collection": _collection_json(saved[0])})


def _repoint_collection_aliases(aliases: dict[str, str]) -> int:
    """Canonicalize all local identities in one pass over each JSON store."""
    if not aliases:
        return 0
    changed = 0
    with _manual_lock:
        entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {}) or {}
        dirty = False
        for entry in entries.values():
            extra = entry.get("extra") if isinstance(entry, dict) else None
            old_id = (str(extra.get("scan_collection_id") or "")
                      if isinstance(extra, dict) else "")
            new_id = aliases.get(old_id, old_id)
            if old_id and new_id != old_id:
                entry["extra"] = dict(extra, scan_collection_id=new_id)
                changed += 1
                dirty = True
        if dirty:
            lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)
    with _client_state_lock:
        state = lib.load_json(lib.CLIENT_STATE_PATH, {}) or {}
        dirty = False
        checked = state.get("checked")
        if isinstance(checked, list):
            for pair in checked:
                value = pair[1] if isinstance(pair, list) and len(pair) == 2 else None
                book = value.get("book") if isinstance(value, dict) else None
                extra = book.get("extra") if isinstance(book, dict) else None
                old_id = (str(extra.get("scan_collection_id") or "")
                          if isinstance(extra, dict) else "")
                new_id = aliases.get(old_id, old_id)
                if old_id and new_id != old_id:
                    book["extra"] = dict(extra, scan_collection_id=new_id)
                    changed += 1
                    dirty = True
        if dirty:
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            lib.save_json(lib.CLIENT_STATE_PATH, state)
    return changed


def _repoint_collection_entries(old_id: str, new_id: str) -> int:
    """Repoint one merged identity while preserving name/origin snapshots."""
    return _repoint_collection_aliases({old_id: new_id})


def _collection_merge_conflict(cfg: dict, token: str, survivor_id: str,
                               duplicate_id: str, survivor_expected: str,
                               duplicate_expected: str):
    """Explain a transactional RPC miss using the now-current locked rows."""
    try:
        survivor = _collection_current(cfg, token, survivor_id)
        duplicate = _collection_current(cfg, token, duplicate_id)
    except sauth.AuthError as exc:
        return _collection_error(exc)
    if not survivor:
        return jsonify({"ok": False,
                        "error": "surviving collection not found"}), 404
    if not duplicate:
        return jsonify({"ok": False,
                        "error": "duplicate collection not found"}), 404
    if duplicate.get("deleted") and duplicate.get("merged_into"):
        target = str(duplicate.get("merged_into") or "")
        message = ("collection is already merged into the selected identity"
                   if target == survivor_id else
                   "collection is already merged into another identity")
        return jsonify({"ok": False, "error": message, "conflict": True,
                        "current": _collection_json(duplicate),
                        "aliases": _adopt_collection_marker(duplicate)}), 409
    if duplicate.get("deleted"):
        return jsonify({"ok": False,
                        "error": "duplicate collection was deleted, not merged",
                        "conflict": True,
                        "current": _collection_json(duplicate)}), 409
    if survivor.get("deleted"):
        return jsonify({"ok": False,
                        "error": "surviving collection is no longer active",
                        "conflict": True,
                        "current": _collection_json(survivor)}), 409
    if str(survivor.get("updated_at") or "") != survivor_expected:
        return jsonify({"ok": False,
                        "error": "surviving collection changed on another device",
                        "conflict": True,
                        "current": _collection_json(survivor)}), 409
    if str(duplicate.get("updated_at") or "") != duplicate_expected:
        return jsonify({"ok": False,
                        "error": "duplicate collection changed on another device",
                        "conflict": True,
                        "current": _collection_json(duplicate)}), 409
    return jsonify({"ok": False, "error": "collection merge was not applied",
                    "conflict": True}), 409


@app.route("/api/collections/merge", methods=["POST"])
def api_collections_merge():
    """Atomically merge identities through the revision-checking cloud RPC."""
    auth = _collection_auth()
    if not auth:
        return jsonify({"ok": False, "error": "sign in to edit collections"}), 401
    try:
        payload = _collection_payload()
        survivor_id = _collection_id(payload.get("survivor_id"))
        duplicate_id = _collection_id(payload.get("duplicate_id"))
        if survivor_id == duplicate_id:
            raise ValueError("choose two different collections")
        survivor_expected = str(payload.get("survivor_updated_at") or "").strip()
        duplicate_expected = str(payload.get("duplicate_updated_at") or "").strip()
        if not survivor_expected or not duplicate_expected:
            raise ValueError("both collection revisions are required")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    cfg, ses = auth
    token = ses["access_token"]
    try:
        result = sauth.rest(cfg, token, "POST", "rpc/merge_collections", {
            "p_survivor_id": survivor_id,
            "p_duplicate_id": duplicate_id,
            "p_survivor_updated_at": survivor_expected,
            "p_duplicate_updated_at": duplicate_expected,
        })
    except sauth.AuthError as exc:
        return _collection_error(exc)
    if isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict):
        result = result[0]
    if not isinstance(result, dict):
        return _collection_merge_conflict(
            cfg, token, survivor_id, duplicate_id,
            survivor_expected, duplicate_expected)
    survivor = result.get("survivor")
    duplicate = result.get("duplicate")
    if (not isinstance(survivor, dict) or not isinstance(duplicate, dict)
            or str(survivor.get("id") or "") != survivor_id
            or str(duplicate.get("id") or "") != duplicate_id
            or not duplicate.get("deleted")
            or str(duplicate.get("merged_into") or "") != survivor_id):
        return jsonify({"ok": False,
                        "error": "collection merge returned an invalid marker"}), 502

    # The RPC locked and revision-checked both rows, then durably recorded the
    # exact identity edge. Cache it for offline LAN imports before touching
    # local entries. If the survivor was itself merged later, cache that marker
    # first so all local links converge directly on the final identity.
    if survivor.get("deleted") and survivor.get("merged_into"):
        _remember_collection_alias(
            survivor_id, str(survivor.get("merged_into")))
    _remember_collection_alias(duplicate_id, survivor_id)
    resolved_survivor = _resolve_collection_alias(duplicate_id)
    repointed = _repoint_collection_entries(duplicate_id, resolved_survivor)
    continued = bool(result.get("continued"))
    if not continued:
        activity("merged", "collection", detail=(
            f"{duplicate.get('name') or duplicate_id} into "
            f"{survivor.get('name') or survivor_id}"
        ))
    return jsonify({"ok": True, "survivor": _collection_json(survivor),
                    "deleted": _collection_json(duplicate),
                    "resolved_survivor_id": resolved_survivor,
                    "repointed": repointed, "continued": continued})


# --- the activity mirror: local jsonl -> cloud events table ----------------------
# The local file is the source of truth; the cursor in auth_session.json marks
# how much of it the cloud already has. Push failures leave the cursor alone,
# so offline work catches up on the next wake. One daemon thread, poked by
# activity() and by sign-in, with a slow heartbeat for retries.

def _push_events_once() -> None:
    ses = _auth_session() if _auth_cfg() else None
    if not ses:
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
            with _auth_execution_cfg() as cfg:
                if not cfg:
                    return
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
    ses = _auth_session() if _auth_cfg() else None
    if not ses:
        return None
    now = time.time()
    if now - _cloud_feed_cache["at"] < 15:
        return _cloud_feed_cache["rows"]
    if now - _cloud_feed_cache["fail_at"] < 30:
        return None
    try:
        with _auth_execution_cfg() as cfg:
            if not cfg:
                return None
            rows = sauth.rest(
                cfg, ses["access_token"], "GET",
                "events?select=at,actor,verb,subject,n,detail"
                f"&order=at.desc&limit={int(limit)}", timeout=8.0) or []
    except (RuntimeError, sauth.AuthError) as exc:
        log.warning("cloud feed unavailable: %s", exc)
        _cloud_feed_cache["fail_at"] = now
        return None
    out = [{"ts": r.get("at"), "actor": r.get("actor"), "verb": r.get("verb"),
            "subject": r.get("subject"), "n": r.get("n"),
            "detail": r.get("detail") or ""} for r in rows]
    _cloud_feed_cache.update(at=now, rows=out)
    return out


# A contributor's real metadata: account age from the profiles row plus the
# real span of their activity from the shared events table. Both tables open
# select to any signed-in user (RLS `using (true)`), so no owner credential is
# involved. Cached briefly per name — the popup can reopen freely. Signed
# out / offline yields None and the client keeps its feed-derived summary.
_profile_cache: dict = {}   # display_name -> {"at": float, "data": dict | None}


def _cloud_profile(name: str) -> dict | None:
    ses = _auth_session() if _auth_cfg() else None
    if not ses:
        return None
    now = time.time()
    hit = _profile_cache.get(name)
    if hit and now - hit["at"] < (60 if hit["data"] is not None else 30):
        return hit["data"]
    if len(_profile_cache) > 200:      # a name is caller-supplied; keep it bounded
        _profile_cache.clear()
    tok = ses["access_token"]
    q = urllib.parse.quote(name, safe="")          # the value only; keep &=/ out
    data = {"display_name": name, "found": False,
            "member_since": None, "last_active": None}
    try:
        with _auth_execution_cfg() as cfg:
            if not cfg:
                return None
            # display_name is not unique; the earliest-created row is the
            # canonical account (a namesake cannot backdate membership).
            prof = sauth.rest(
                cfg, tok, "GET", f"profiles?display_name=eq.{q}"
                "&select=display_name,created_at"
                "&order=created_at.asc&limit=1", timeout=8.0) or []
            if prof:
                data["found"] = True
                data["member_since"] = prof[0].get("created_at")
            last = sauth.rest(
                cfg, tok, "GET",
                f"events?actor=eq.{q}&select=at&order=at.desc&limit=1",
                timeout=8.0) or []
            if last:
                data["last_active"] = last[0].get("at")
    except (RuntimeError, sauth.AuthError) as exc:
        log.warning("profile lookup unavailable: %s", exc)
        _profile_cache[name] = {"at": now, "data": None}   # back off on failure
        return None
    _profile_cache[name] = {"at": now, "data": data}
    return data


@app.route("/api/profile")
def api_profile():
    """Real account metadata for one contributor, looked up by the display name
    the feed shows: when their account was created and the true span of their
    recorded activity. Cloud-only — signed out / offline returns cloud:false and
    the client falls back to the activity feed it already holds."""
    name = str(request.args.get("name") or "").strip()[:60]
    if not name:
        return jsonify({"ok": False, "error": "name is required"}), 400
    data = _cloud_profile(name)
    if data is None:
        return jsonify({"ok": True, "cloud": False, "found": False})
    return jsonify({"ok": True, "cloud": True, **data})


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

    def write_review():
        created = False
        with _reviews_lock:
            reviews = lib.load_json(REVIEWS_PATH, {})
            # one OPEN review per item -- flagging again refreshes label/reason
            review = next((x for x in reviews.values()
                           if x.get("key") == key and x.get("status") == "open"),
                          None)
            if review:
                review["label"] = label or review.get("label", "")
                if reason:
                    review["reason"] = reason
            else:
                rid = lib.gen_id(set(reviews))
                review = reviews[rid] = {
                    "id": rid, "key": key, "kind": kind, "ref": ref,
                    "label": label, "reason": reason, "status": "open",
                    "created_by": _actor(), "created_at": now,
                    "resolved_by": "", "resolved_at": "", "comments": [],
                }
                created = True
            lib.save_json(REVIEWS_PATH, reviews)
        return review, created

    page_ref = _page_remark_ref_parts(ref) if kind == "key" else None
    if page_ref:
        # Serialize with page deletion, then reject a popover/sidebar created
        # against an older page grid. Otherwise a late review could attach to
        # the physical page that shifted into the saved number.
        with _page_structure_lock:
            build = lib.load_json(BUILDS_PATH, {}).get(page_ref[0])
            revision = str(payload.get("page_revision") or "")
            current_revision = str(build.get("updated_at") or "unversioned") \
                if build else ""
            if (not build or not revision or
                    revision != current_revision or
                    not _valid_src_key(build, page_ref[1])):
                return jsonify({"ok": False,
                                "error": "page changed; reopen it before review"}), 409
            r, created = write_review()
    else:
        r, created = write_review()
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


@app.get("/healthz")
def healthz():
    """Uncredentialed loopback readiness probe; contains no application data."""
    return jsonify({"ok": True})


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
        engine_v=_asset_v("engine-client.js"),
        css_v=_asset_v("style.css"),
        app_version=_app_version(),
    )


@app.route("/api/v1/capabilities")
def api_engine_capabilities():
    """Installed module/workbench discovery for current and future clients."""
    return jsonify({"ok": True,
                    **_library_engine().discovery_document()})


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


@contextlib.contextmanager
def _live_item_write_scope(item_id: str):
    """Serialize one legacy item write against aggregate lifecycle deletion.

    Transitional request handlers still publish several entry sidecars
    directly instead of through an engine repository.  They must therefore
    join the engine's recoverable workspace lease themselves.  Catalogue
    membership is re-read *inside* that lease; after the brief catalogue lock
    is released, the lease stays held for the caller's complete mutation.
    Aggregate deletion takes the same lease, so either this write finishes
    first and deletion removes its result, or deletion wins and this scope
    refuses to recreate the managed tree.
    """

    item_id = str(item_id or "").strip()
    if not item_id:
        abort(404)
    with _ensure_engine_session().write_set.workspace_lease():
        with _builds_lock:
            builds = lib.load_json(BUILDS_PATH, {})
            item = builds.get(item_id) if isinstance(builds, dict) else None
        if not isinstance(item, dict):
            abort(404)
        yield item


def _live_item_write_endpoint(fn):
    """Apply :func:`_live_item_write_scope` to a path-scoped mutation.

    Mixed GET/write endpoints retain their existing read path.  Flask passes
    path parameters by keyword, and the two historical spellings are both
    supported while those routes are migrated into engine modules.
    """

    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return fn(*args, **kwargs)
        item_id = kwargs.get("build_id") or kwargs.get("bid")
        if item_id is None and args:
            item_id = args[0]
        with _live_item_write_scope(str(item_id or "")):
            return fn(*args, **kwargs)

    return guarded


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


def _builds_apply(
        bid: str, fields: dict, *, expected_revision: str | None = None) -> str:
    """Fold field changes into one build against a FRESH read of the store —
    for slow work (folder sync, page deletion) whose snapshot may be minutes
    old: only this build's fields are ours to change (the _publish_run
    precedent)."""
    overlap = sorted(set(fields) & _BUILD_REPRESENTATION_FIELDS)
    if overlap:
        raise ValueError(
            "representation fields require the representation command service: "
            + ", ".join(overlap)
        )

    def apply(builds):
        if bid in builds:
            row = builds[bid]
            current_revision = _engine_build_record_revision(bid, row)
            if (
                expected_revision is not None
                and current_revision != expected_revision
            ):
                raise EngineConflictError(
                    "the item changed while background work was running",
                    code="item_revision_conflict",
                    details={
                        "item_id": bid,
                        "expected_revision": expected_revision,
                        "current_revision": current_revision,
                    },
                )
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

_BUILD_REPRESENTATION_FIELDS = frozenset({
    "pdf_file", "pdf_sources", "representation_manifest",
})
_BUILD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

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
    aliases = {"primary"}
    if isinstance(raw, list):
        for it in raw:
            if not isinstance(it, dict):
                continue
            path = str(it.get("path") or "").strip()
            if not path:
                continue
            sid = re.sub(
                r"[^A-Za-z0-9_-]", "", str(it.get("id") or "")
            )[:31]
            alias = sid.casefold()
            if not sid or alias in aliases:
                sid = lib.gen_id(set(aliases))
                alias = sid.casefold()
            aliases.add(alias)
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
    # This transitional projection contains local source paths. It remains for
    # legacy callers only and must never enter a browser or intermediary cache.
    response = jsonify({"builds": lib.load_json(BUILDS_PATH, {})})
    response.cache_control.no_store = True
    return response


def _create_build(seed: dict) -> tuple[dict | None, str]:
    """Mint one build record from a seed through the standard field cleaning:
    (build, "") on success, (None, error) on refusal. The core of POST
    /api/builds, shared with the .lib open flow so a book minted from a
    manifest passes exactly the same scrubbing as a hand-created one."""
    with _builds_lock:
        builds = lib.load_json(BUILDS_PATH, {})
        build = {f: str(seed.get(f, "") or "").strip() for f in _BUILD_FIELDS
                 if f not in _BUILD_STRUCTURED_FIELDS}
        # Source attachment is a separate versioned aggregate. The browser
        # creates catalogue state first, then attaches any seed source through
        # the representation command service under dual CAS preconditions.
        build["pdf_file"] = ""
        build["pdf_sources"] = []
        build["category_ids"] = _clean_category_ids(seed.get("category_ids"),
                                                    lib.load_taxonomy()["nodes"])
        build["bundle"] = _clean_bundle(seed.get("bundle"))
        build["images"] = _clean_images(seed.get("images"))
        build["extra"] = _clean_extra(seed.get("extra"))
        build["capture_id"] = _clean_capture_id(seed.get("capture_id"))
        if build["status"] not in _BUILD_STATUSES:
            build["status"] = "draft"
        if build["rights"] not in _BUILD_RIGHTS:
            return None, f"unknown rights value {build['rights']!r}"
        build["id"] = lib.gen_id(set(builds))
        build["created_at"] = _build_updated_at()
        build["updated_at"] = build["created_at"]
        build["representation_manifest"] = {
            "version": 1,
            "sources": {},
            "detached": [],
        }
        builds[build["id"]] = build
        lib.save_json(BUILDS_PATH, builds)
    activity("created", "draft entry", detail=build.get("title", ""))
    return build, ""


@app.route("/api/builds", methods=["POST"])
def api_builds_create():
    payload = request.get_json(silent=True) or {}
    seed = payload.get("build") or {}
    managed_sources = sorted(
        set(seed) & _BUILD_REPRESENTATION_FIELDS
    ) if isinstance(seed, Mapping) else []
    if managed_sources:
        return jsonify({
            "ok": False,
            "error": (
                "Create catalogue state first, then attach PDF sources "
                "through the representation command resource"
            ),
            "code": "representation_command_required",
            "fields": managed_sources,
        }), 409
    build, err = _create_build(seed)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "build": build})


@app.route("/api/builds/<build_id>", methods=["PATCH"])
def api_builds_update(build_id: str):
    with _builds_lock:
        builds = lib.load_json(BUILDS_PATH, {})
        if build_id not in builds:
            abort(404)
        payload = request.get_json(silent=True) or {}
        managed_sources = sorted(set(payload) & _BUILD_REPRESENTATION_FIELDS)
        if managed_sources:
            return jsonify({
                "ok": False,
                "error": (
                    "PDF sources must be changed through the representation "
                    "command resource"
                ),
                "code": "representation_command_required",
                "fields": managed_sources,
            }), 409
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
    """Compatibility facade over aggregate lifecycle deletion.

    Older renderers know only this path and expect a ``trash_id``.  Keep that
    response spelling, but make the value the engine-owned lifecycle tombstone
    id; catalogue-only deletion is no longer an authority in this process.
    Callers may opt into replay-safe semantics by carrying the modern CAS and
    idempotency headers.  Headerless legacy callers receive a coherent
    preflight followed by the same conditional engine command.
    """

    try:
        lifecycle = _item_lifecycle_engine()
        _item_lifecycle_require_empty_body()
        modern_headers = (
            request.headers.get("Idempotency-Key") is not None,
            request.headers.get("If-Record-Match") is not None,
            request.headers.get("If-Managed-Tree-Match") is not None,
        )
        if any(modern_headers):
            # Exact replay is possible only with the complete original
            # command identity. Never mix server-derived state or a fresh
            # operation id with a caller-supplied subset.
            item_revision = _item_lifecycle_match(
                "If-Record-Match",
                required_code="item_revision_required",
                invalid_code="invalid_item_revision",
                details={"item_id": build_id},
            )
            tree_revision = _item_lifecycle_match(
                "If-Managed-Tree-Match",
                required_code="managed_tree_revision_required",
                invalid_code="invalid_managed_tree_revision",
                details={"item_id": build_id},
            )
            operation_id = _item_command_operation_id(item_id=build_id)
        else:
            state = lifecycle.inspect(build_id)
            item_revision = state.item.revision
            tree_revision = state.managed_tree.revision
            operation_id = f"legacy-item-delete-{uuid.uuid4().hex}"
        result = lifecycle.delete(LifecycleDeleteItemCommand(
            item_id=build_id,
            expected_item_revision=item_revision,
            expected_managed_tree_revision=tree_revision,
            operation_id=operation_id,
        ))
    except EngineError as exc:
        return _engine_error_response(exc)

    receipt = result.receipt
    tombstone = receipt.tombstone
    response = jsonify({
        "ok": True,
        "schema": "librarytool.legacy-item-delete/1",
        "deprecated": True,
        "trash_id": tombstone.tombstone_id,
        "tombstone_id": tombstone.tombstone_id,
        **result.as_dict(),
    })
    response.cache_control.no_store = True
    response.headers["Location"] = url_for(
        "api_v1_item_tombstone", tombstone_id=tombstone.tombstone_id,
    )
    response.headers["X-Record-Revision"] = receipt.deleted_item_revision
    response.headers["X-Managed-Tree-Revision"] = (
        receipt.managed_tree_revision
    )
    response.headers["X-Tombstone-Revision"] = tombstone.revision
    return response


def _legacy_active_item_tombstone(item_id: str):
    """Return the one active aggregate tombstone for a legacy item id."""

    return next((
        tombstone
        for tombstone in _item_lifecycle_engine().list_tombstones()
        if tombstone.item_id == item_id and tombstone.state == "deleted"
    ), None)


def _legacy_restore_lifecycle_tombstone(
    tombstone_id: str,
) -> tuple[dict, ItemLifecycleResult | None]:
    """Restore a server-owned tombstone for compatibility transports.

    The old transport has no tombstone CAS field.  Reading the current public
    snapshot and passing that revision into the engine preserves collision and
    race safety; an optional Idempotency-Key still gives callers exact replay
    when they also use the versioned resource for retry.
    """

    lifecycle = _item_lifecycle_engine()
    tombstone = lifecycle.get_tombstone(tombstone_id)
    result = None
    modern_headers = (
        request.headers.get("Idempotency-Key") is not None,
        request.headers.get("If-Tombstone-Match") is not None,
    )
    if any(modern_headers):
        # On response-loss retry the public tombstone may already say
        # ``restored``. Send the caller's original command back through the
        # service so its durable receipt—not this facade—decides replay versus
        # operation-id or revision conflict. A partial command is rejected by
        # the standard header readers below.
        expected_revision = _item_lifecycle_match(
            "If-Tombstone-Match",
            required_code="tombstone_revision_required",
            invalid_code="invalid_tombstone_revision",
            details={"tombstone_id": tombstone_id},
        )
        operation_id = _item_command_operation_id()
        result = lifecycle.restore(RestoreItemCommand(
            tombstone_id=tombstone_id,
            expected_tombstone_revision=expected_revision,
            operation_id=operation_id,
        ))
        tombstone = result.receipt.tombstone
    elif tombstone.state == "deleted":
        result = lifecycle.restore(RestoreItemCommand(
            tombstone_id=tombstone_id,
            expected_tombstone_revision=tombstone.revision,
            operation_id=f"legacy-item-restore-{uuid.uuid4().hex}",
        ))
        tombstone = result.receipt.tombstone

    # The legacy response still embeds a build projection. Read it under the
    # same aggregate isolation so a racing lifecycle delete cannot remove it
    # between the committed restore and this compatibility projection.
    with lifecycle.deletion_index_guard():
        builds = lib.load_json(BUILDS_PATH, {})
        build = (
            builds.get(tombstone.item_id)
            if isinstance(builds, dict) else None
        )
        live_revision = (
            _engine_build_record_revision(tombstone.item_id, build)
            if isinstance(build, Mapping) else ""
        )
    if not isinstance(build, dict):
        raise EngineRepositoryError(
            "the restored item is absent from the catalogue",
            code="item_restore_publication_missing",
            details={"item_id": tombstone.item_id},
        )
    if result is None and live_revision != tombstone.restored_item_revision:
        raise EngineConflictError(
            "the restored item changed after the lifecycle command",
            code="item_restore_replay_conflict",
            details={
                "item_id": tombstone.item_id,
                "expected_item_revision": tombstone.restored_item_revision,
                "current_item_revision": live_revision,
            },
        )
    body = {
        "ok": True,
        "schema": "librarytool.legacy-item-restore/1",
        "deprecated": True,
        "restored": ["record.json"],
        "skipped": [],
        "replayed": result is None or result.replayed,
        "build": dict(build),
        "tombstone_id": tombstone.tombstone_id,
        "tombstone": tombstone.as_dict(),
    }
    if result is not None:
        body["receipt"] = result.receipt.as_public_dict()
    return body, result


@app.route("/api/builds/restore", methods=["POST"])
def api_builds_restore():
    """Compatibility restore that delegates only to an aggregate tombstone."""
    payload = request.get_json(silent=True) or {}
    raw = payload.get("build") or {}
    if not isinstance(raw, Mapping):
        return jsonify({"ok": False, "error": "build must be an object"}), 400
    managed_sources = sorted(set(raw) & _BUILD_REPRESENTATION_FIELDS)
    if managed_sources:
        return jsonify({
            "ok": False,
            "error": (
                "Restore catalogue state first, then attach PDF sources "
                "through the representation command resource"
            ),
            "code": "representation_command_required",
            "fields": managed_sources,
        }), 409
    bid = str(raw.get("id") or "")
    if not bid or not _BUILD_ID_RE.fullmatch(bid):
        abort(400)
    try:
        builds = lib.load_json(BUILDS_PATH, {})
        if isinstance(builds, dict) and bid in builds:
            return jsonify({
                "ok": False,
                "error": "the build already exists",
                "code": "item_already_exists",
            }), 409
        tombstone = _legacy_active_item_tombstone(bid)
        if tombstone is None:
            response = jsonify({
                "ok": False,
                "error": (
                    "catalogue-only restore is retired; restore an engine "
                    "item tombstone instead"
                ),
                "code": "legacy_item_restore_retired",
                "replacement": "/api/v1/item-tombstones/<id>/restore",
            })
            response.cache_control.no_store = True
            return response, 410
        body, result = _legacy_restore_lifecycle_tombstone(
            tombstone.tombstone_id
        )
    except EngineError as exc:
        return _engine_error_response(exc)
    response = jsonify(body)
    response.cache_control.no_store = True
    if result is not None:
        response.headers["X-Record-Revision"] = (
            result.receipt.restored_item_revision
        )
        response.headers["X-Tombstone-Revision"] = (
            result.receipt.tombstone.revision
        )
    return response


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
    ``displaced`` field-set is supplied, re-file it so the swap is reversible.
    Body: {target, altId, displaced?:{id?,source?,label?,fields,note?}} —
    displaced.source defaults to "superseded"; a valid source passes through so
    Ctrl+Z can re-file the un-applied alt under its original source, and a
    supplied id is kept so undo/redo address stable alt ids across swaps."""
    p = request.get_json(silent=True) or {}
    target = str(p.get("target") or "").strip()
    alt_id = str(p.get("altId") or "").strip()
    if not target or not alt_id:
        abort(400)
    displaced = p.get("displaced")
    disp_alt = None
    if isinstance(displaced, dict):
        d = dict(displaced)
        if str(d.get("source") or "") not in _STAGED_SOURCES:
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
_REMOTE_PDF_FREE_RESERVE = 128 * 1024 * 1024


def _remote_pdf_cache_path(url: str) -> Path:
    """Stable on-disk cache name shared by the viewer and Smart Scan."""
    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".pdf"
    return lib.DATA_ROOT / "downloads" / "cache" / name


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


def _fetch_remote_pdf(url: str, dest: Path, max_bytes: int | None) -> None:
    """Stream one remote PDF to ``dest`` and validate its signature.

    ``max_bytes`` is used by the general viewer cache. Smart Scan passes
    ``None`` because its working copy is short-lived; it still streams fixed
    chunks rather than holding the response in memory, and preserves a disk
    reserve so a bogus/unbounded response cannot fill the data drive.
    """
    import shutil
    _ssrf_guard(url)
    req = urllib.request.Request(url, headers={"User-Agent": whl_client.USER_AGENT})
    try:
        with _pdf_opener.open(req, timeout=90) as resp, open(dest, "wb") as fh:
            try:
                expected = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                expected = 0
            if max_bytes is not None and expected > max_bytes:
                raise ValueError("remote PDF exceeds the size cap")
            free = shutil.disk_usage(dest.parent).free
            if expected and expected > max(0, free - _REMOTE_PDF_FREE_RESERVE):
                raise ValueError("not enough disk space to stage remote PDF")
            total = 0
            next_disk_check = 16 * 1024 * 1024
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise ValueError("remote PDF exceeds the size cap")
                if total >= next_disk_check:
                    if shutil.disk_usage(dest.parent).free < _REMOTE_PDF_FREE_RESERVE:
                        raise ValueError("not enough disk space to stage remote PDF")
                    next_disk_check += 16 * 1024 * 1024
                fh.write(chunk)
        with open(dest, "rb") as fh:
            if fh.read(5) != b"%PDF-":
                raise ValueError("response is not a PDF")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"fetch failed: {exc}") from exc


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
    p = _remote_pdf_cache_path(url)
    p.parent.mkdir(parents=True, exist_ok=True)
    name = p.name
    with _remote_pdf_lock:
        url_lock = _remote_pdf_url_locks.setdefault(name, threading.Lock())
    with url_lock:
        if p.exists():
            return p
        _ssrf_guard(url)   # only on an actual fetch — cached hits skip the DNS lookup
        tmp = p.with_suffix(".fetch.tmp")
        try:
            _fetch_remote_pdf(url, tmp, _REMOTE_PDF_MAX_BYTES)
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
    sends its configured endpoint/model here. The key is leased server-side."""
    p = request.get_json(silent=True) or {}
    base = (p.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    model = (p.get("model") or "").strip()
    instructions = (p.get("instructions") or "").strip()
    text = (p.get("text") or "").strip()
    if not _secret_is_configured("aiKey") or not model:
        return jsonify({"ok": False,
                        "error": "AI model not configured (Settings > AI); API key (Settings > Credentials)"})
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
    try:
        with _lease_secret("aiKey") as key:
            req = urllib.request.Request(
                base + "/chat/completions", data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + key})
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        summary = data["choices"][0]["message"]["content"]
        return jsonify({"ok": True, "summary": summary})
    except urllib.error.HTTPError as exc:
        return jsonify({"ok": False, "error": f"provider returned HTTP {exc.code}"})
    except Exception:
        return jsonify({"ok": False, "error": "AI provider unavailable"})


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
@_live_item_write_endpoint
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
    trim_tid = ""       # trash row for a blank-page trim done by THIS sync
    page_remap = None
    pf = (b.get("pdf_file") or "").strip()
    if pf:
        sp = _resolve_local(pf)
        if sp is not None and sp.is_file():
            src = sp
        else:
            notes.append("pdf_file not found")
    # blank pages are trimmed from the REAL PDF before the preview and
    # extraction are built (removed pages go to the trash, OCR renumbered) — skipped
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
                    deletion = _apply_page_deletion(
                        build_id, builds, src, blanks,
                        expected_revision=str(
                            b.get("updated_at") or "unversioned"))
                    page_remap = deletion.get("page_remap")
                    trim_tid = str(deletion.get("trash_id") or "")
                    notes.extend(
                        f"page deletion warning: {warning}"
                        for warning in deletion.get("warnings") or [])
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
            primary = d / "primary.pdf"
            try:
                primary_is_source = (
                    primary.is_file() and src.resolve() == primary.resolve()
                )
            except OSError:
                primary_is_source = False
            if not primary_is_source:
                prev = _preview_pdf(src, pages)
                import shutil
                shutil.copyfile(prev, primary)
                preview_ok = True
            # migrate away from the legacy name: anything pointing at the
            # old preview.pdf (a keep_original repoint from an earlier run)
            # moves to primary.pdf BEFORE the stale file goes
            legacy = d / "preview.pdf"
            if legacy.is_file():
                old_rel = legacy.resolve().relative_to(
                    lib.DATA_ROOT.resolve()).as_posix()
                if (b.get("pdf_file") or "").replace("\\", "/") == old_rel:
                    primary_rel = (
                        primary.resolve()
                        .relative_to(lib.DATA_ROOT.resolve()).as_posix()
                    )
                    _engine_refresh_representation_reference(
                        build_id,
                        "primary",
                        primary_rel,
                        operation_scope="folder-preview-repoint",
                        expected_item_revision=(
                            _engine_build_record_revision(build_id, b)
                        ),
                    )
                    b = lib.load_json(BUILDS_PATH, {}).get(build_id, b)
                    lib.save_json(d / "metadata.json", b)
                    src = primary
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
                    primary_rel = (
                        (d / "primary.pdf").resolve()
                        .relative_to(lib.DATA_ROOT.resolve()).as_posix()
                    )
                    # Publish the usable replacement before removing the
                    # temporary original. If dual CAS or validation refuses,
                    # the original remains attached and intact.
                    _engine_refresh_representation_reference(
                        build_id,
                        "primary",
                        primary_rel,
                        operation_scope="folder-temporary-repoint",
                        expected_item_revision=(
                            _engine_build_record_revision(build_id, b)
                        ),
                    )
                    b = lib.load_json(BUILDS_PATH, {}).get(build_id, b)
                    lib.save_json(d / "metadata.json", b)
                    src.unlink()
                    # a trim in this same sync trashed the pages it removed,
                    # and the row points at the file just deleted. The pages
                    # themselves are still worth keeping (download-only), but
                    # the full pre-image is now dead weight against the cap.
                    if trim_tid:
                        _trash_retire(trim_tid, "the original was a temporary "
                                      "download and has been removed")
                    notes.append("original removed (temporary artifact)")
                    # nothing may keep pointing at the deleted file: the
                    # entry folder's own PDF becomes the build's PDF, and
                    # the IA download catalog entry is retired
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
    if page_remap:
        out["page_remap"] = page_remap
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
@_live_item_write_endpoint
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
    lib.save_text(target, str(p.get("text") or ""), errors="replace")
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
    # facsimile only for the source whose boxes it actually has; word_docs
    # ({"<src>": {"<page>": name}}) says which compiled file each page's box
    # text came from, so viewing any OTHER doc flows that doc's text instead
    word_pages = {src: sorted(int(k) for k in pages if str(k).isdigit())
                  for src, pages in (meta.get("words") or {}).items()
                  if isinstance(pages, dict)}
    # region_pages mirrors word_pages: which pages have a typed-region record
    # (fetched individually via /ocr-regions — the records carry full text);
    # region_states carries each page's review flag for the workbench strip
    region_pages = {src: sorted(int(k) for k in pages if str(k).isdigit())
                    for src, pages in (meta.get("regions") or {}).items()
                    if isinstance(pages, dict)}
    region_states = {
        src: {k: rec.get("state") for k, rec in pages.items()
              if isinstance(rec, dict) and rec.get("state")}
        for src, pages in (meta.get("regions") or {}).items()
        if isinstance(pages, dict)}
    region_proposal_pages = {
        src: sorted(int(k) for k, rec in pages.items()
                    if str(k).isdigit() and isinstance(rec, dict))
        for src, pages in (meta.get("region_proposals") or {}).items()
        if isinstance(pages, dict)}
    region_stale_pages = {
        src: sorted(int(k) for k, rec in pages.items()
                    if str(k).isdigit() and isinstance(rec, dict)
                    and isinstance(rec.get("stale"), dict))
        for src, pages in (meta.get("regions") or {}).items()
        if isinstance(pages, dict)}
    region_compile_pending_pages = {
        src: sorted(int(k) for k, rec in pages.items()
                    if str(k).isdigit() and isinstance(rec, dict))
        for src, pages in (meta.get("region_compile_pending") or {}).items()
        if isinstance(pages, dict)}
    return jsonify({"ok": True, "images": meta.get("images") or {},
                    "word_pages": word_pages,
                    "word_docs": meta.get("words_doc") or {},
                    "region_pages": region_pages,
                    "region_states": {k: v for k, v in region_states.items() if v},
                    "region_proposal_pages": {
                        k: v for k, v in region_proposal_pages.items() if v},
                    "region_stale_pages": {
                        k: v for k, v in region_stale_pages.items() if v},
                    "region_compile_pending_pages": {
                        k: v for k, v in region_compile_pending_pages.items()
                        if v}})


# Transitional HTTP and representation adapters still use this vocabulary.
# The reusable row codec is now its single owner.
_ENGINE_ITEM_COMMAND_MANAGED_FIELDS = WhlCatalogueItemCodec.managed_fields
_ENGINE_REPRESENTATION_ID = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_ENGINE_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _engine_valid_record_revision(value) -> bool:
    return WhlCatalogueItemCodec.valid_record_revision(value)


def _engine_build_record_revision(item_id: str, raw: Mapping) -> str:
    return _ENGINE_ITEM_CODEC.record_revision(item_id, raw)


def _engine_validate_bundle(value) -> None:
    _ENGINE_ITEM_CODEC.validate_bundle(value)


def _engine_validate_catalogue_metadata(
        metadata: Mapping, *, strict_fields=frozenset()) -> None:
    _ENGINE_ITEM_CODEC.validate_catalogue_metadata(
        metadata,
        strict_fields=strict_fields,
    )


def _engine_validate_managed_build_fields(item_id: str, raw: Mapping) -> None:
    _ENGINE_ITEM_CODEC.validate_managed_record(item_id, raw)


def _engine_representation_manifest(raw: Mapping) -> dict:
    """Return one strict detached attachment manifest from a build row."""
    value = raw.get("representation_manifest")
    if value is None:
        return {"version": 1, "sources": {}, "detached": []}
    if not isinstance(value, Mapping) or set(value) != {
        "version", "sources", "detached",
    }:
        raise TypeError("build representation_manifest is invalid")
    if value.get("version") != 1 or not isinstance(value.get("sources"), Mapping):
        raise ValueError("build representation_manifest version is invalid")
    detached = value.get("detached")
    if not isinstance(detached, (list, tuple)) or any(
        not isinstance(source_id, str)
        or not _ENGINE_REPRESENTATION_ID.fullmatch(source_id)
        or (source_id.casefold() == "primary" and source_id != "primary")
        for source_id in detached
    ):
        raise TypeError("build detached representation ids are invalid")
    folded_detached = [source_id.casefold() for source_id in detached]
    if len(folded_detached) != len(set(folded_detached)):
        raise ValueError("build detached representation ids are duplicated")
    sources = {}
    aliases = set()
    fields = {
        "role", "media_type", "label", "acquisition",
        "content_sha256", "size", "source_stat", "metadata",
    }
    for source_id, record in value["sources"].items():
        if (
            not isinstance(source_id, str)
            or not _ENGINE_REPRESENTATION_ID.fullmatch(source_id)
            or (source_id.casefold() == "primary" and source_id != "primary")
            or source_id.casefold() in aliases
        ):
            raise ValueError("build representation manifest ids are invalid")
        aliases.add(source_id.casefold())
        if not isinstance(record, Mapping) or set(record) != fields:
            raise TypeError("build representation manifest source is invalid")
        if (
            not isinstance(record["role"], str)
            or not isinstance(record["media_type"], str)
            or not isinstance(record["label"], str)
            or record["acquisition"] not in {"reference", "copy"}
            or not isinstance(record["content_sha256"], str)
            or not _ENGINE_SHA256.fullmatch(record["content_sha256"])
            or isinstance(record["size"], bool)
            or not isinstance(record["size"], int)
            or record["size"] < 0
            or not isinstance(record["source_stat"], Mapping)
            or set(record["source_stat"]) != {
                "size", "mtime_ns", "ctime_ns", "device", "inode",
            }
            or any(
                isinstance(record["source_stat"].get(field), bool)
                or not isinstance(record["source_stat"].get(field), int)
                for field in (
                    "size", "mtime_ns", "ctime_ns", "device", "inode",
                )
            )
            or record["source_stat"].get("size") < 0
            or record["source_stat"].get("size") != record["size"]
            or not isinstance(record["metadata"], Mapping)
        ):
            raise ValueError("build representation manifest source is invalid")
        # JSON round-trip detaches legacy mutable containers and rejects data
        # that cannot survive the catalogue's storage contract.
        sources[source_id] = json.loads(json.dumps(
            record, ensure_ascii=False, allow_nan=False,
        ))
    if aliases & set(folded_detached):
        raise ValueError("a representation cannot be attached and detached")
    return {
        "version": 1,
        "sources": sources,
        "detached": list(detached),
    }


def _engine_item_command_decode(
        item_id: str, raw: Mapping) -> ItemRecordSnapshot:
    return _ENGINE_ITEM_CODEC.decode(item_id, raw)


def _engine_item_command_encode(
        item_id: str, draft: ItemDraft,
        previous: Mapping | None) -> Mapping:
    return _ENGINE_ITEM_CODEC.encode(item_id, draft, previous)


def _engine_advance_restored_record(
    item_id: str,
    raw: Mapping,
) -> Mapping:
    return _ENGINE_ITEM_CODEC.advance_restored_record(item_id, raw)


def _engine_open_lib_draft(metadata: Mapping) -> ItemDraft:
    """Project hostile ``.lib`` metadata into one safe catalogue draft.

    Archive decoding remains in the framework-neutral planner.  This injected
    production policy owns the transitional catalogue's bibliographic field
    vocabulary and its publication-rights default, so another host or module
    can choose a different metadata model without changing the open service.
    """

    if not isinstance(metadata, Mapping):
        raise TypeError("Replica manifest metadata must be an object")

    def text(field: str) -> str:
        return str(metadata.get(field) or "").strip()

    # ``published_slug`` is intentionally omitted even though old exporters
    # may include it: it is server-managed publication state, not descriptive
    # metadata that a foreign package may assign to a new local item.
    bibliographic = {
        field: text(field)
        for field in _LIB_META_FIELDS
        if field not in {"title", "published_slug"}
    }
    # Preserve the complete transitional build shape without accepting any of
    # these operational values from the archive.  The eventual neutral
    # catalogue schema can drop this projection without changing OpenLibService.
    bibliographic.update({
        "group_id": "",
        "categories": "",
        "category_ids": [],
        "description": "",
        "pdf_source": "",
        "bundle": _clean_bundle({}),
        "notes": "",
        "rights": "",
        "attention": "",
    })
    return ItemDraft(
        title=text("title"),
        metadata=bibliographic,
    )


def _engine_representation_locator(item_id: str, source_id: str) -> str:
    """Opaque engine resource identity; never serialize an attached path."""
    item = urllib.parse.quote(str(item_id), safe="")
    source = urllib.parse.quote(str(source_id), safe="")
    return f"urn:librarytool:item:{item}:representation:{source}"


def _engine_file_stat(value) -> dict:
    """Portable identity/change fingerprint for one attached local file."""
    return {
        "size": int(value.st_size),
        "mtime_ns": int(value.st_mtime_ns),
        "ctime_ns": int(value.st_ctime_ns),
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
    }


def _engine_cross_interface_file_metadata(value) -> tuple[int, int, int]:
    """Metadata safe to compare between path-stat and descriptor-stat."""

    return (
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _engine_source_snapshot(item_id: str, source_id: str, raw: str,
                            *, role: str, label: str,
                            manifest: Mapping | None = None) -> dict:
    """Project one attached PDF into a stable, path-safe representation."""
    manifest = manifest if isinstance(manifest, Mapping) else {}
    path = _resolve_local(raw) if raw else None
    stat = None
    if path is not None:
        try:
            if path.is_file():
                value = path.stat()
                stat = _engine_file_stat(value)
        except OSError:
            stat = None
    fingerprint = json.dumps(
        {"path": raw, "stat": stat, "manifest": manifest},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    acquisition = str(manifest.get("acquisition") or "reference")
    content_sha256 = str(manifest.get("content_sha256") or "")
    stored_size = manifest.get("size")
    size = (stored_size if isinstance(stored_size, int)
            and not isinstance(stored_size, bool) and stored_size >= 0
            else stat.get("size") if stat else None)
    metadata = manifest.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    attached_stat = manifest.get("source_stat")
    tracked = bool(manifest.get("content_sha256"))
    unchanged = (
        isinstance(attached_stat, Mapping)
        and stat is not None
        and all(attached_stat.get(field) == stat[field] for field in stat)
    )
    content_state = (
        "missing" if stat is None else
        "unchanged" if tracked and unchanged else
        "drifted" if tracked else
        "untracked"
    )
    return {
        "id": source_id,
        "revision": "sr-" + hashlib.sha256(fingerprint).hexdigest()[:24],
        "role": str(manifest.get("role") or role),
        "media_type": str(manifest.get("media_type") or "application/pdf"),
        "locator": _engine_representation_locator(item_id, source_id),
        "label": str(manifest.get("label") or label),
        # Bundled OCR/Replica executors require a readable local attachment.
        # A remote catalogue URL remains metadata, not a usable source.
        # A referenced file whose stat no longer matches the bytes hashed at
        # attachment is not advertised as a usable source. Its stored digest
        # still identifies the attached pre-image; replacing the reference
        # explicitly refreshes both digest and stat fingerprint.
        "available": bool(stat) and content_state != "drifted",
        "disposition": "copied" if acquisition == "copy" else "referenced",
        "content_state": content_state,
        "content_sha256": content_sha256,
        "size": size,
        "metadata": dict(metadata),
    }


def _engine_item_representations(item_id: str, build: dict) -> list[dict]:
    rows = []
    manifest = _engine_representation_manifest(build)
    source_manifest = manifest["sources"]
    detached = {value.casefold() for value in manifest["detached"]}
    primary = str(build.get("pdf_file") or "").strip()
    if not primary and "primary" not in detached:
        entry_pdf = _entry_primary_pdf(item_id)
        if entry_pdf:
            primary = str(_entry_dir(item_id) / entry_pdf)
    if primary:
        rows.append(_engine_source_snapshot(
            item_id, "primary", primary, role="primary", label="Primary source",
            manifest=source_manifest.get("primary"),
        ))
    seen = {"primary"}
    for source in build.get("pdf_sources") or []:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("id") or "").strip()
        raw = str(source.get("path") or "").strip()
        if not source_id or source_id in seen or not raw:
            continue
        seen.add(source_id)
        rows.append(_engine_source_snapshot(
            item_id, source_id, raw, role="alternate",
            label=str(source.get("label") or "Alternate source"),
            manifest=source_manifest.get(source_id),
        ))
    return rows


def _engine_representation_aggregate(
        item_id: str, build: Mapping) -> RepresentationAggregateSnapshot:
    """Decode one transitional row into the safe mutation aggregate."""
    _engine_validate_managed_build_fields(item_id, build)
    source_ids = [
        str(source.get("id") or "")
        for source in build.get("pdf_sources") or []
        if isinstance(source, Mapping)
    ]
    aliases = [value.casefold() for value in source_ids]
    if (
        any(not _ENGINE_REPRESENTATION_ID.fullmatch(value)
            or value.casefold() == "primary" for value in source_ids)
        or len(aliases) != len(set(aliases))
    ):
        raise ValueError("build PDF source ids are invalid or duplicated")
    snapshots = []
    for row in _engine_item_representations(item_id, dict(build)):
        snapshots.append(RepresentationRecordSnapshot(
            representation_id=row["id"],
            revision=row["revision"],
            role=row["role"],
            media_type=row["media_type"],
            locator=row["locator"],
            label=row["label"],
            available=row["available"],
            disposition=row["disposition"],
            content_state=row["content_state"],
            content_sha256=row["content_sha256"],
            size=row["size"],
            metadata=row["metadata"],
        ))
    return RepresentationAggregateSnapshot(
        item_id=item_id,
        item_revision=_engine_build_record_revision(item_id, build),
        representations=tuple(snapshots),
    )


def _engine_representation_source_path(source_token: str) -> Path:
    path = _resolve_local(source_token)
    if path is None or not path.is_file():
        raise EngineValidationError(
            "the representation source is not a readable local file",
            code="representation_source_not_found",
        )
    if path.suffix.casefold() != ".pdf":
        raise EngineValidationError(
            "this adapter currently accepts PDF representations only",
            code="unsupported_representation_media_type",
            details={"media_type": "application/pdf"},
        )
    return path


def _engine_representation_manifest_record(
        draft: RepresentationAttachmentDraft, path: Path) -> dict:
    from pypdf import PdfReader

    try:
        named_before = path.stat()
        with path.open("rb") as stream:
            opened_before = os.fstat(stream.fileno())
            before = _engine_file_stat(opened_before)
            if (
                not stat.S_ISREG(named_before.st_mode)
                or not stat.S_ISREG(opened_before.st_mode)
                or not os.path.samestat(opened_before, named_before)
                or _engine_cross_interface_file_metadata(opened_before)
                != _engine_cross_interface_file_metadata(named_before)
            ):
                raise EngineConflictError(
                    "the representation source changed while it was being attached",
                    code="representation_source_changed",
                    retryable=True,
                )
            if stream.read(5) != b"%PDF-":
                raise EngineValidationError(
                    "the representation source is not a PDF",
                    code="invalid_representation_source",
                )
            stream.seek(0)
            try:
                reader = PdfReader(stream, strict=False)
                if reader.is_encrypted or len(reader.pages) < 1:
                    raise ValueError("the PDF has no readable pages")
            except Exception as exc:
                raise EngineValidationError(
                    "the representation source is not a readable PDF",
                    code="invalid_representation_source",
                ) from exc
            stream.seek(0)
            digest_state = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                digest_state.update(chunk)
            digest = digest_state.hexdigest()
            opened_after = os.fstat(stream.fileno())
            after = _engine_file_stat(opened_after)
            named_after = path.stat()
            path_after = _engine_file_stat(named_after)
    except EngineError:
        raise
    except OSError as exc:
        raise EngineValidationError(
            "the representation source changed while it was being attached",
            code="representation_source_changed",
            retryable=True,
        ) from exc
    if (
        before != after
        or _engine_file_stat(named_before) != path_after
        or not stat.S_ISREG(opened_after.st_mode)
        or not stat.S_ISREG(named_after.st_mode)
        or not os.path.samestat(opened_after, named_after)
        or _engine_cross_interface_file_metadata(opened_after)
        != _engine_cross_interface_file_metadata(named_after)
    ):
        raise EngineConflictError(
            "the representation source changed while it was being attached",
            code="representation_source_changed",
            retryable=True,
        )
    if (
        draft.expected_content_sha256
        and digest != draft.expected_content_sha256
    ):
        raise EngineConflictError(
            "the representation source digest does not match",
            code="representation_source_digest_mismatch",
            details={"expected_sha256": draft.expected_content_sha256},
        )
    if (
        draft.expected_size is not None
        and path_after["size"] != draft.expected_size
    ):
        raise EngineConflictError(
            "the representation source size does not match",
            code="representation_source_size_mismatch",
            details={"expected_size": draft.expected_size},
        )
    return {
        "role": draft.role,
        "media_type": draft.media_type,
        "label": draft.label,
        "acquisition": draft.acquisition,
        "content_sha256": digest,
        "size": path_after["size"],
        # Drift checks later compare path-stat to path-stat.  Persist the final
        # named observation, never descriptor-only metadata such as Windows'
        # cross-interface ctime value.
        "source_stat": path_after,
        "metadata": json.loads(json.dumps(
            draft.as_dict()["metadata"], ensure_ascii=False, allow_nan=False,
        )),
    }


def _engine_representation_paths(build: Mapping) -> dict[str, Path]:
    rows = {"primary": str(build.get("pdf_file") or "").strip()}
    for source in build.get("pdf_sources") or []:
        if isinstance(source, Mapping):
            rows[str(source.get("id") or "")] = str(
                source.get("path") or "").strip()
    resolved = {}
    for source_id, raw in rows.items():
        path = _resolve_local(raw) if raw else None
        if source_id and path is not None:
            try:
                resolved[source_id] = path.resolve(strict=False)
            except OSError:
                continue
    return resolved


def _engine_representation_put_record(
        item_id: str, build: Mapping,
        draft: RepresentationAttachmentDraft) -> Mapping:
    """Attach one local reference while retaining its path server-side."""
    _engine_validate_managed_build_fields(item_id, build)
    if draft.media_type != "application/pdf":
        raise EngineValidationError(
            "this adapter currently accepts PDF representations only",
            code="unsupported_representation_media_type",
            details={"media_type": draft.media_type},
        )
    if draft.acquisition != "reference":
        raise EngineValidationError(
            "managed asset copying is not installed",
            code="unsupported_representation_acquisition",
            details={"acquisition": draft.acquisition},
        )
    source_id = draft.representation_id
    if (
        not _ENGINE_REPRESENTATION_ID.fullmatch(source_id)
        or (source_id.casefold() == "primary" and source_id != "primary")
    ):
        raise EngineValidationError(
            "the representation id is incompatible with this catalogue",
            code="invalid_representation_id",
        )
    if (
        source_id == "primary" and draft.role != "primary"
    ) or (
        source_id != "primary" and draft.role == "primary"
    ):
        raise EngineValidationError(
            "the representation role conflicts with its identity",
            code="invalid_representation_role",
        )
    path = _engine_representation_source_path(draft.source_token)
    resolved = path.resolve(strict=True)
    for existing_id, existing_path in _engine_representation_paths(build).items():
        if existing_id != source_id and os.path.normcase(existing_path) == os.path.normcase(resolved):
            raise EngineConflictError(
                "the source is already attached to this item",
                code="representation_source_already_attached",
                details={"item_id": item_id, "representation_id": existing_id},
            )

    result = dict(build)
    if source_id == "primary":
        result["pdf_file"] = draft.source_token
    else:
        sources = [
            dict(source) for source in result.get("pdf_sources") or []
            if isinstance(source, Mapping) and source.get("id") != source_id
        ]
        sources.append({"id": source_id, "path": draft.source_token})
        result["pdf_sources"] = sources

    manifest = _engine_representation_manifest(build)
    manifest["sources"][source_id] = _engine_representation_manifest_record(
        draft, path
    )
    manifest["detached"] = [
        value for value in manifest["detached"] if value != source_id
    ]
    result["representation_manifest"] = manifest
    result["updated_at"] = _build_updated_at(build.get("updated_at"))
    return result


def _engine_representation_detach_record(
        item_id: str, build: Mapping, source_id: str) -> Mapping:
    """Detach catalogue state without deleting an external source file."""
    _engine_validate_managed_build_fields(item_id, build)
    result = dict(build)
    if source_id == "primary":
        result["pdf_file"] = ""
    else:
        result["pdf_sources"] = [
            dict(source) for source in result.get("pdf_sources") or []
            if isinstance(source, Mapping) and source.get("id") != source_id
        ]
    manifest = _engine_representation_manifest(build)
    manifest["sources"].pop(source_id, None)
    if source_id not in manifest["detached"]:
        manifest["detached"].append(source_id)
    result["representation_manifest"] = manifest
    result["updated_at"] = _build_updated_at(build.get("updated_at"))
    return result


def _engine_refresh_representation_reference(
        item_id: str, source_id: str, source_token: str,
        *, operation_scope: str, expected_item_revision: str):
    """Attach or replace one internal reference through the same authority.

    Transitional workflows such as folder repointing and page rewrite/restore
    still operate on legacy local files. Once their file operation succeeds,
    this helper refreshes the authoritative checksum/stat manifest under the
    representation service's normal dual-CAS and recoverable receipt boundary.
    """
    item = _item_engine().get_item(item_id)
    if item.record_revision != expected_item_revision:
        raise EngineConflictError(
            "the item changed while source work was running",
            code="item_revision_conflict",
            details={
                "item_id": item_id,
                "expected_revision": expected_item_revision,
                "current_revision": item.record_revision,
            },
        )
    current = next(
        (
            value for value in item.representations
            if value.representation_id == source_id
        ),
        None,
    )
    role = "primary" if source_id == "primary" else (
        current.role if current is not None else "alternate"
    )
    label = (
        current.label if current is not None and current.label else
        "Primary source" if source_id == "primary" else "Alternate source"
    )
    draft = RepresentationAttachmentDraft(
        representation_id=source_id,
        source_token=str(source_token).strip(),
        acquisition="reference",
        expected_content_sha256="",
        expected_size=None,
        role=role,
        media_type="application/pdf",
        label=label,
        metadata=(current.metadata if current is not None else {}),
    )
    return _representation_command_engine().attach(
        AttachRepresentationCommand(
            item_id=item_id,
            expected_item_revision=expected_item_revision,
            expected_representation_revision=(
                current.revision if current is not None else None
            ),
            draft=draft,
            operation_id=(
                f"{operation_scope[:64]}-{uuid.uuid4().hex}"
            ),
        )
    )


def _engine_artifact_id(item_id: str, kind: str, name: str,
                        layer: str = "", source_id: str = "") -> str:
    identity = (
        f"{item_id}\n{kind}\n{layer}\n{source_id}\n{name}"
    ).encode("utf-8")
    return "art-" + hashlib.sha256(identity).hexdigest()[:20]


def _engine_artifact_row(item_id: str, build: dict, *, kind: str,
                         name: str, row: dict, layer: str = "",
                         source_id: str = "") -> dict:
    stale = row.get("stale") if isinstance(row.get("stale"), bool) else None
    provenance = row.get("produced_by")
    if not isinstance(provenance, dict):
        provenance = {}
    artifact_identity = str(row.get("artifact") or name)
    result = {
        "id": _engine_artifact_id(
            item_id, kind, artifact_identity, layer, source_id,
        ),
        "kind": kind,
        "name": name,
        "layer": layer,
        "media_type": (
            "text/markdown" if name.lower().endswith(".md") else
            "text/plain" if name.lower().endswith(".txt") else
            "application/pdf" if name.lower().endswith(".pdf") else
            "application/octet-stream"
        ),
        "available": bool(row.get("exists", True)),
        "stale": stale,
        "size": row.get("size"),
        "provenance": provenance,
    }
    if source_id:
        result["source_representation_id"] = source_id
        if stale is False:
            raw = str(build.get("pdf_file") or "") if source_id == "primary" else next(
                (str(source.get("path") or "")
                 for source in build.get("pdf_sources") or []
                 if isinstance(source, dict)
                 and str(source.get("id") or "") == source_id),
                "",
            )
            if raw:
                result["source_revision"] = _engine_source_snapshot(
                    item_id, source_id, raw,
                    role="primary" if source_id == "primary" else "alternate",
                    label="",
                )["revision"]
    metadata = {
        key: row[key] for key in ("page", "pages") if row.get(key) is not None
    }
    if metadata:
        result["metadata"] = metadata
    return result


def _engine_item_artifacts(item_id: str, build: dict) -> list[dict]:
    """Flatten today's entry-folder summary into portable artifact refs."""
    info = _entry_folder_info(item_id, build)
    rows: list[dict] = []
    for row in info.get("ocr") or []:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        source_id = str(row.get("src") or "primary")
        rows.append(_engine_artifact_row(
            item_id, build, kind="ocr", name=str(row["name"]), row=row,
            source_id=source_id,
        ))
    for row in info.get("full_text") or []:
        if isinstance(row, dict) and row.get("name"):
            rows.append(_engine_artifact_row(
                item_id, build, kind="full-text", name=str(row["name"]), row=row,
            ))
    for row in info.get("translations") or []:
        if isinstance(row, dict) and row.get("name"):
            rows.append(_engine_artifact_row(
                item_id, build, kind="translation", name=str(row["name"]),
                layer=str(row.get("lang") or ""), row=row,
            ))
    for row in info.get("analysis") or []:
        if isinstance(row, dict) and row.get("name"):
            rows.append(_engine_artifact_row(
                item_id, build, kind="analysis", name=str(row["name"]), row=row,
            ))
    for kind in ("summary", "about"):
        row = info.get(kind)
        if isinstance(row, dict) and row.get("exists"):
            rows.append(_engine_artifact_row(
                item_id, build, kind=kind, name=f"{kind}.md", row=row,
            ))
    for row in info.get("images") or []:
        if isinstance(row, dict) and row.get("name"):
            rows.append(_engine_artifact_row(
                item_id, build, kind="figure", name=str(row["name"]), row=row,
            ))
    processed = str(info.get("processed_pdf") or "")
    if processed:
        path = _entry_dir(item_id) / processed
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        rows.append(_engine_artifact_row(
            item_id, build, kind="processed-source", name=processed,
            row={"size": size},
        ))
    return rows


def _engine_item_snapshot() -> dict[str, dict]:
    """Read the transitional build store as portable engine item records."""
    builds = lib.load_json(BUILDS_PATH, {})
    if not isinstance(builds, dict):
        raise ValueError("the build catalogue is not an object")
    out = {}
    for item_id, raw in builds.items():
        if not isinstance(raw, dict):
            # Let the repository boundary report the malformed record rather
            # than allowing a partial catalogue to look authoritative.
            out[str(item_id)] = raw
            continue
        build = dict(raw)
        metadata = {
            key: value for key, value in build.items()
            if key not in _ENGINE_ITEM_COMMAND_MANAGED_FIELDS
        }
        out[str(item_id)] = {
            "id": str(item_id),
            "kind": "book",
            "title": str(build.get("title") or ""),
            "revision": _engine_build_record_revision(str(item_id), build),
            "updated_at": str(build.get("updated_at") or ""),
            "metadata": metadata,
            "representations": _engine_item_representations(str(item_id), build),
            "artifacts": _engine_item_artifacts(str(item_id), build),
        }
    return out


class _EngineItemRepository:
    """Current build catalogue exposed through the engine item port."""

    def get(self, item_id: str):
        build = lib.load_json(BUILDS_PATH, {}).get(item_id)
        if not isinstance(build, dict):
            return None
        sources = tuple(
            str(source.get("id")) for source in (build.get("pdf_sources") or [])
            if isinstance(source, dict) and source.get("id")
        )
        return ItemDescriptor(item_id=item_id, sources=sources,
                              metadata=dict(build))


class _EngineReplicaPolicies:
    """Compatibility adapter for pure policies not yet moved under src/."""

    content_revision = staticmethod(replica_service.content_revision)
    proposal_revision = staticmethod(replica_service.proposal_revision)
    duplicate_rids = staticmethod(replica_service.duplicate_rids)
    clean_rid = staticmethod(libformat.clean_rid)

    @staticmethod
    def sanitize_region_items(items, *, source_type: str):
        return libformat.sanitize_page_items(
            list(items or []), src_type=source_type)

    @staticmethod
    def sanitize_dims(dims):
        return libformat.sanitize_dims(dims) or {}

    @staticmethod
    def sanitize_ext(ext):
        return libformat.sanitize_ext(ext)

    @staticmethod
    def sanitize_document_name(value: str) -> str:
        return _ocr_name(value)

    @staticmethod
    def normalize_language(value: str) -> str:
        return _lang_code(value)

    accept_region_proposal = staticmethod(
        replica_service.accept_region_proposal)
    dismiss_region_proposal = staticmethod(
        replica_service.dismiss_region_proposal)

    @staticmethod
    def compose_text(items, *, layer: str = "text") -> str:
        return layout_roles.compose_text(list(items or []), layer=layer)

    @staticmethod
    def propose_layout_families(pages, **options):
        return replica_service.propose_layout_families(pages, **options)


class _EngineTextLayerRepository:
    """Derived text adapter; the engine owns when it may be invoked."""

    @staticmethod
    def merge_page(item_id: str, document: str, page: int, text: str) -> None:
        _ocr_merge_page(item_id, document, page, text)

    @staticmethod
    def bind_document_source(item_id: str, document: str,
                             source_id: str) -> None:
        _ocr_set_source(item_id, document, source_id)


_library_engine_guard = threading.Lock()
_library_engine_instance = None


def _interchange_source_ids(item_id: str) -> tuple[str, ...] | None:
    build = lib.load_json(BUILDS_PATH, {}).get(item_id)
    if not isinstance(build, dict):
        return None
    values = ["primary"]
    values.extend(
        str(source.get("id") or "")
        for source in (build.get("pdf_sources") or [])
        if isinstance(source, dict) and source.get("id")
    )
    return tuple(dict.fromkeys(values))


_TRANSLATION_LAYER_PREFIX = "ocr."
_TRANSLATION_SOURCE_PAGE = re.compile(
    r"^--- page ([0-9]+) ---\r?$", re.MULTILINE)
_TRANSLATION_PORTABLE_ID = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _translation_layer_id(document: str) -> str:
    """Encode an OCR filename as a reversible, portable engine layer ID."""
    if (not isinstance(document, str) or not document
            or _ocr_name(document) != document
            or "/" in document or "\\" in document):
        raise EngineRepositoryError(
            "the authoritative OCR document name is invalid",
            code="invalid_translation_source_identity",
            details={"document": str(document or "")},
        )
    try:
        encoded = base64.urlsafe_b64encode(
            document.encode("utf-8")).decode("ascii").rstrip("=")
    except (UnicodeError, ValueError) as exc:
        raise EngineRepositoryError(
            "the authoritative OCR document name cannot be encoded",
            code="invalid_translation_source_identity",
        ) from exc
    layer_id = _TRANSLATION_LAYER_PREFIX + encoded
    if not _TRANSLATION_PORTABLE_ID.fullmatch(layer_id):
        raise EngineRepositoryError(
            "the authoritative OCR document name is too long",
            code="invalid_translation_source_identity",
            details={"document": document},
        )
    return layer_id


def _translation_document_name(layer_id: str) -> str:
    """Decode the exact legacy OCR filename represented by ``layer_id``."""
    if (not isinstance(layer_id, str)
            or not layer_id.startswith(_TRANSLATION_LAYER_PREFIX)):
        raise EngineRepositoryError(
            "the translation source layer is not an OCR document",
            code="invalid_translation_source_identity",
            details={"layer_id": str(layer_id or "")},
        )
    encoded = layer_id[len(_TRANSLATION_LAYER_PREFIX):]
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(
            encoded + padding, altchars=b"-_", validate=True)
        document = raw.decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise EngineRepositoryError(
            "the translation source layer cannot be decoded",
            code="invalid_translation_source_identity",
            details={"layer_id": layer_id},
        ) from exc
    if _translation_layer_id(document) != layer_id:
        raise EngineRepositoryError(
            "the translation source layer is not canonical",
            code="invalid_translation_source_identity",
            details={"layer_id": layer_id},
        )
    return document


def _translation_unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def _translation_source_map(item_id: str) -> dict[str, str]:
    """Read the OCR-to-PDF binding without permissive JSON fallbacks."""
    path = _entry_dir(item_id) / "ocr" / "sources.json"
    if not os.path.lexists(path):
        return {}
    lexical = Path(os.path.abspath(path))
    try:
        resolved = lexical.resolve()
    except OSError as exc:
        raise EngineRepositoryError(
            "the OCR source map path cannot be resolved",
            code="invalid_translation_source_snapshot",
            details={"item_id": item_id, "artifact": "ocr/sources.json"},
        ) from exc
    if lexical != resolved or lexical.is_symlink():
        raise EngineRepositoryError(
            "the OCR source map path is unsafe",
            code="invalid_translation_source_snapshot",
            details={"item_id": item_id, "artifact": "ocr/sources.json"},
        )
    if not resolved.is_file():
        raise EngineRepositoryError(
            "the OCR source map is not a regular file",
            code="invalid_translation_source_snapshot",
            details={"item_id": item_id, "artifact": "ocr/sources.json"},
        )
    try:
        value = json.loads(
            resolved.read_bytes().decode("utf-8"),
            object_pairs_hook=_translation_unique_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number {token}")),
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise EngineRepositoryError(
            "the OCR source map is malformed",
            code="invalid_translation_source_snapshot",
            details={"item_id": item_id, "artifact": "ocr/sources.json"},
        ) from exc
    if not isinstance(value, dict):
        raise EngineRepositoryError(
            "the OCR source map is not an object",
            code="invalid_translation_source_snapshot",
            details={"item_id": item_id, "artifact": "ocr/sources.json"},
        )
    result = {}
    owners = {}
    for document, source_id in value.items():
        if (not isinstance(document, str) or _ocr_name(document) != document
                or not isinstance(source_id, str)
                or not _TRANSLATION_PORTABLE_ID.fullmatch(source_id)):
            raise EngineRepositoryError(
                "the OCR source map contains an invalid binding",
                code="invalid_translation_source_snapshot",
                details={"item_id": item_id,
                         "artifact": "ocr/sources.json"},
            )
        folded = document.casefold()
        if folded in owners and owners[folded] != document:
            raise EngineRepositoryError(
                "the OCR source map contains aliased document names",
                code="invalid_translation_source_snapshot",
                details={"item_id": item_id,
                         "artifact": "ocr/sources.json",
                         "documents": [owners[folded], document]},
            )
        owners[folded] = document
        result[document] = source_id
    return result


def _translation_source_pages(item_id: str, document: str,
                              payload: bytes) -> tuple[TranslationSourceCanvas, ...]:
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise EngineRepositoryError(
            "the authoritative OCR document is not UTF-8",
            code="invalid_translation_source_snapshot",
            details={"item_id": item_id, "document": document},
        ) from exc
    markers = list(_TRANSLATION_SOURCE_PAGE.finditer(text))
    if not markers:
        stripped = text.strip()
        pages = {1: stripped} if stripped else {}
    else:
        # ``_ocr_merge_page`` intentionally preserves arbitrary legacy
        # preamble.  It has never belonged to a numbered page, so match
        # ``_an_pages`` and ignore it while validating every actual marker.
        pages = {}
        for index, marker in enumerate(markers):
            raw_page = marker.group(1)
            if len(raw_page) > 123 or raw_page.startswith("0"):
                raise EngineRepositoryError(
                    "the authoritative OCR document has ambiguous page markers",
                    code="invalid_translation_source_snapshot",
                    details={"item_id": item_id, "document": document,
                             "page": raw_page[:128]},
                )
            page = int(raw_page)
            if page < 1 or str(page) != raw_page or page in pages:
                raise EngineRepositoryError(
                    "the authoritative OCR document has ambiguous page markers",
                    code="invalid_translation_source_snapshot",
                    details={"item_id": item_id, "document": document,
                             "page": raw_page},
                )
            end = (markers[index + 1].start()
                   if index + 1 < len(markers) else len(text))
            pages[page] = text[marker.end():end].strip()
    return tuple(
        TranslationSourceCanvas(
            selector=f"page:{page}", order=order,
            label=str(page), text=page_text,
        )
        for order, (page, page_text) in enumerate(sorted(pages.items()))
    )


def _translation_source_snapshot(
        item_id: str, reference: str) -> TranslationSourceSnapshot | None:
    """Return one strict, authoritative OCR snapshot for the engine adapter."""
    builds = lib.load_json(BUILDS_PATH, {})
    build = builds.get(item_id) if isinstance(builds, dict) else None
    if not isinstance(build, dict):
        return None

    document = str(reference or "")
    if not document:
        ocr_dir = _entry_dir(item_id) / "ocr"
        for candidate in (build.get("ocr_verified"), build.get("ocr_active"),
                          "compiled.txt", "extracted.txt"):
            candidate = _ocr_name(candidate) if candidate else ""
            if candidate and os.path.lexists(ocr_dir / candidate):
                document = candidate
                break
    if not document:
        return None
    layer_id = _translation_layer_id(document)

    ocr_root = Path(os.path.abspath(_entry_dir(item_id) / "ocr"))
    candidate = Path(os.path.abspath(ocr_root / document))
    if not os.path.lexists(candidate):
        return None
    try:
        resolved_root = ocr_root.resolve()
        resolved = candidate.resolve()
    except OSError as exc:
        raise EngineRepositoryError(
            "the authoritative OCR document path cannot be resolved",
            code="translation_source_read_failed",
            details={"item_id": item_id, "document": document},
        ) from exc
    if (candidate != resolved or resolved.parent != resolved_root
            or candidate.is_symlink()):
        raise EngineRepositoryError(
            "the authoritative OCR document path is unsafe",
            code="invalid_translation_source_snapshot",
            details={"item_id": item_id, "document": document},
        )
    if not resolved.is_file():
        raise EngineRepositoryError(
            "the authoritative OCR document is not a regular file",
            code="invalid_translation_source_snapshot",
            details={"item_id": item_id, "document": document},
        )

    sources = _translation_source_map(item_id)
    if document not in sources:
        alias = next(
            (name for name in sources if name.casefold() == document.casefold()),
            "",
        )
        if alias:
            raise EngineRepositoryError(
                "the OCR source map aliases the selected document name",
                code="invalid_translation_source_snapshot",
                details={"item_id": item_id, "document": document,
                         "mapped_document": alias},
            )
    source_id = sources.get(document, "primary")
    live_sources = set(_interchange_source_ids(item_id) or ())
    if (not _TRANSLATION_PORTABLE_ID.fullmatch(source_id)
            or source_id not in live_sources):
        raise EngineRepositoryError(
            "the OCR document names an unavailable representation",
            code="invalid_translation_source_snapshot",
            details={"item_id": item_id, "document": document,
                     "representation_id": source_id},
        )
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise EngineRepositoryError(
            "the authoritative OCR document cannot be read",
            code="translation_source_read_failed",
            details={"item_id": item_id, "document": document},
            retryable=True,
        ) from exc
    try:
        return TranslationSourceSnapshot(
            item_id=item_id,
            layer_id=layer_id,
            representation_id=source_id,
            canvases=_translation_source_pages(item_id, document, payload),
        )
    except (TypeError, ValueError) as exc:
        raise EngineRepositoryError(
            "the authoritative OCR document cannot be represented safely",
            code="invalid_translation_source_snapshot",
            details={"item_id": item_id, "document": document},
        ) from exc


def _translation_item_exists(item_id: str) -> bool:
    builds = lib.load_json(BUILDS_PATH, {})
    return isinstance(builds, dict) and isinstance(builds.get(item_id), dict)


@contextlib.contextmanager
def _engine_workspace_locks(_item_id: str):
    """Bridge legacy writers into the workspace lease's lock order."""

    with _page_structure_lock:
        with _ocr_merge_lock:
            with _an_write_lock:
                with _manifest_lock:
                    with _builds_lock:
                        yield


@contextlib.contextmanager
def _engine_recovery_locks():
    """Take every transitional storage lock inside the recovery lease."""

    with _engine_workspace_locks(""):
        yield


def _engine_item_category_ids() -> tuple[str, ...]:
    """Adapt the legacy taxonomy document to the neutral profile port."""

    try:
        taxonomy = lib.load_taxonomy()
    except Exception as exc:
        raise EngineRepositoryError(
            "the category catalogue is unavailable",
            code="category_repository_unavailable",
            details={"cause_type": type(exc).__name__},
            retryable=True,
        ) from exc
    nodes = taxonomy.get("nodes") if isinstance(taxonomy, Mapping) else None
    if not isinstance(nodes, Mapping):
        raise EngineRepositoryError(
            "the category catalogue is unavailable",
            code="category_repository_unavailable",
            retryable=True,
        )
    return tuple(nodes)


_ENGINE_ITEM_CODEC = WhlCatalogueItemCodec(
    advance_revision=_build_updated_at,
    category_ids_for=_engine_item_category_ids,
    validate_representation_manifest=_engine_representation_manifest,
)


def _engine_host_bindings() -> FilesystemHostBindings:
    """Return borrowed production callbacks for the neutral host opener."""

    policies = _EngineReplicaPolicies()
    interchange_planner = ExistingItemLibArchivePlanner(
        parse_format=libformat.parse_format,
        supported_major=libformat.SUPPORTED_MAJOR,
        sanitize_items=libformat.sanitize_page_items,
        sanitize_dims=libformat.sanitize_dims,
        sanitize_document_name=_ocr_name,
        sanitize_styles=libformat.sanitize_styles,
        sanitize_ext=libformat.sanitize_ext,
        sanitize_figure=libformat.sanitize_figure,
        clean_region_id=libformat.clean_rid,
        is_template_name=lambda name: bool(_RW_TPL_RE.fullmatch(name)),
        is_protected=replica_service.is_protected,
        compose_text=layout_roles.compose_text,
        normalize_language=_lang_code,
        limits=LibArchiveLimits(
            max_archive_bytes=libformat.MAX_BYTES,
            max_inflated_bytes=libformat.MAX_INFLATED,
            max_json_bytes=libformat.MAX_JSON,
            max_figure_bytes=libformat.MAX_FIGURE,
            max_pages=libformat.MAX_PAGES,
            max_items_per_page=libformat.MAX_ITEMS,
        ),
    )
    descriptors = _EngineItemRepository()
    return FilesystemHostBindings(
        catalogue=CatalogueBindings(
            load_snapshot=_engine_item_snapshot,
            descriptors=descriptors,
            decode_record=_engine_item_command_decode,
            encode_record=_engine_item_command_encode,
            allocate_item_id=lambda existing: lib.gen_id(set(existing)),
            lock_context_for=lambda: _builds_lock,
            representations=RepresentationBindings(
                decode_aggregate=_engine_representation_aggregate,
                put_record=_engine_representation_put_record,
                detach_record=_engine_representation_detach_record,
            ),
            lifecycle=ItemLifecycleBindings(
                advance_restored_record=_engine_advance_restored_record,
            ),
            item_command_policy=WhlBookItemCommandPolicy(
                _engine_item_category_ids
            ),
        ),
        replica=ReplicaBindings(
            policies=policies,
            text_repository=_EngineTextLayerRepository(),
            read_json=lambda path: lib.load_json(path, {}),
            write_json=lib.save_json,
            lock_context_for=lambda _item_id: _ocr_merge_lock,
        ),
        interchange=InterchangeBindings(
            planner=interchange_planner,
            source_ids_for=_interchange_source_ids,
            clean_region_id=libformat.clean_rid,
            normalize_language=_lang_code,
            sanitize_document_name=_ocr_name,
            open_item_draft_for=_engine_open_lib_draft,
        ),
        translation=TranslationBindings(
            item_exists_for=_translation_item_exists,
            source_snapshot_for=_translation_source_snapshot,
            source_reference_for=lambda source: _translation_document_name(
                source.layer_id
            ),
        ),
        workspace_lock_context_for=_engine_workspace_locks,
        recovery_lock_context=_engine_recovery_locks,
        jobs=JobHistoryBindings(
            id_factory=lambda existing: lib.gen_id(existing),
        ),
        secrets=_secret_store_bindings(),
    )


def _open_engine_session(
    workspace_root: Path | None = None,
) -> FilesystemEngineSession:
    """Open production resources after all compatibility seams exist."""

    root = Path(workspace_root or lib.OUTPUT_DIR)
    return open_filesystem_engine(
        config=FilesystemEngineConfig(
            workspace_root=root,
            paths=FilesystemEnginePaths(
                catalogue=BUILDS_PATH,
                entries=ENTRIES_DIR,
            ),
            job_history=Path("jobs.json"),
            job_keep=_JOBS_KEEP,
        ),
        bindings=_engine_host_bindings(),
        contribute_modules=first_party_module_contributions,
    )


def _ensure_engine_session() -> FilesystemEngineSession:
    """Atomically open and publish the process-lifetime engine session."""

    global _engine_session
    global _engine_write_set
    global _job_manager
    global _translation_provenance
    global _jobs
    global _jobs_events
    global _jobs_lock
    global _library_engine_instance

    session = _engine_session
    opened_here = False
    if session is not None and not session.closed:
        return session
    with _library_engine_guard:
        session = _engine_session
        if session is None or session.closed:
            # Credentials are cut over before the engine graph is composed or
            # any process-lifetime alias can become visible.  A failed legacy
            # migration therefore cannot leave request handlers or workers
            # running against plaintext fallback state.
            _prepare_protected_secret_store()
            opened = _open_engine_session()
            # Resolve every property before publishing the session. If an
            # opener ever returns an invalid/closed object, no partial set of
            # compatibility aliases becomes visible.
            try:
                engine = opened.engine
                write_set = opened.write_set
                jobs = opened.jobs
                provenance = opened.provenance
                records = jobs.records
                events = jobs.cancel_events
                lock = jobs.lock
            except BaseException:
                try:
                    opened.close()
                except Exception:
                    pass
                raise

            _engine_write_set = write_set
            _job_manager = jobs
            _translation_provenance = provenance
            _jobs = records
            _jobs_events = events
            _jobs_lock = lock
            _library_engine_instance = engine
            _engine_session = opened
            session = opened
            opened_here = True
    cleanup = globals().get("_ocr_cleanup_staged_figure_orphans")
    if opened_here and callable(cleanup):
        builds = lib.load_json(BUILDS_PATH, {})
        for item_id in builds if isinstance(builds, dict) else ():
            try:
                cleanup(str(item_id))
            except Exception:
                log.warning(
                    "Could not clean staged Replica figures: book=%s",
                    item_id,
                )
    return session


def _close_engine_session() -> None:
    """Unpublish and close the transport session after workers have stopped.

    The current executable has no worker supervisor and therefore does not call
    this automatically. Embedders that control every borrowed worker must call
    it before in-process app disposal or ``importlib.reload(server)``.
    """

    global _engine_session
    global _engine_write_set
    global _job_manager
    global _translation_provenance
    global _jobs
    global _jobs_events
    global _jobs_lock
    global _library_engine_instance

    with _library_engine_guard:
        session = _engine_session
        if session is None:
            return
        try:
            session.close()
        finally:
            _engine_session = None
            _engine_write_set = None
            _job_manager = None
            _translation_provenance = None
            _jobs = None
            _jobs_events = None
            _jobs_lock = None
            _library_engine_instance = None


def _library_engine() -> LibraryEngine:
    """Return the engine owned by the process-lifetime filesystem session."""

    return _ensure_engine_session().engine


def _replica_engine() -> ReplicaApplicationService:
    replica = _library_engine().replica
    if replica is None:  # manifest/runtime mismatch is a server fault
        raise RuntimeError("the Replica engine module is unavailable")
    return replica


def _interchange_engine() -> LibInterchangeService:
    interchange = _library_engine().interchange
    if interchange is None:
        raise RuntimeError("the interchange engine module is unavailable")
    return interchange


def _lib_open_engine() -> OpenLibService:
    """Return the optional composite new-item Replica service."""

    return _library_engine().require_service(LIB_OPEN_SERVICE)


def _region_view_response(view) -> dict:
    out = {"ok": True, "found": bool(view.found),
           "revision": view.revision}
    if view.found:
        out.update({"doc": view.doc, "dims": dict(view.dims),
                    "state": view.state, "stale": dict(view.stale),
                    "ext": dict(view.ext),
                    "items": [dict(item) for item in view.items]})
    if view.proposal is not None:
        out["proposal"] = dict(view.proposal)
    if view.compile_pending is not None:
        out["compile_pending"] = dict(view.compile_pending)
    return out


def _engine_error_status(exc: EngineError) -> int:
    if isinstance(exc, EngineNotFoundError):
        return 404
    if isinstance(exc, EnginePreconditionRequiredError):
        return 428
    if isinstance(exc, EngineConflictError):
        return 409
    if isinstance(exc, EngineValidationError):
        return 400
    if exc.retryable and exc.code.endswith("_unavailable"):
        return 503
    return 500


def _engine_error_response(exc: EngineError, *, current=None):
    body = {"ok": False, "error": exc.message, "code": exc.code,
            "retryable": exc.retryable}
    if exc.details:
        body["details"] = dict(exc.details)
        # Compatibility fields used by the existing workbench and import
        # tests stay top-level while structured clients use details.
        for key in ("duplicate_rids", "untracked"):
            if key in exc.details:
                body[key] = exc.details[key]
        if isinstance(exc.details.get("warnings"), list):
            body["warnings"] = [
                {
                    "loc": str(warning.get("location") or ""),
                    "msg": str(warning.get("message") or ""),
                }
                for warning in exc.details["warnings"]
                if isinstance(warning, dict)
            ]
    if isinstance(exc, (EngineConflictError,
                        EnginePreconditionRequiredError)):
        body["conflict"] = exc.code
    if current is not None:
        canonical = _region_view_response(current)
        canonical.update(body)
        body = canonical
        if current.proposal is not None:
            body["proposal_revision"] = replica_service.proposal_revision(
                current.proposal)
    response = jsonify(body)
    if current is not None:
        response.set_etag(current.revision)
    return response, _engine_error_status(exc)


def _region_record(meta: dict, src: str, page: int) -> dict | None:
    rec = ((meta.get("regions") or {}).get(src) or {}).get(str(page))
    return rec if isinstance(rec, dict) else None


def _region_match_token(payload: dict) -> str:
    """If-Match wins; the JSON token keeps non-HTTP clients convenient."""
    raw = str(request.headers.get("If-Match") or
              payload.get("expect_revision") or "").strip()
    if raw.startswith("W/"):
        raw = raw[2:].strip()
    return raw.strip('"')


def _json_with_etag(body: dict):
    response = jsonify(body)
    response.set_etag(body["revision"])
    return response


def _item_engine() -> ItemQueryService:
    items = _library_engine().items
    if items is None:
        raise EngineError(
            "the item query module is unavailable",
            code="item_query_unavailable", retryable=True,
        )
    return items


def _item_command_engine() -> ItemCommandService:
    commands = _library_engine().item_commands
    if commands is None:
        raise EngineError(
            "the item command module is unavailable",
            code="item_command_unavailable", retryable=True,
        )
    return commands


def _item_lifecycle_engine() -> ItemLifecycleService:
    lifecycle = _library_engine().get_service(ITEM_LIFECYCLE_SERVICE)
    if lifecycle is None:
        raise EngineError(
            "the item lifecycle module is unavailable",
            code="item_lifecycle_unavailable",
            retryable=True,
        )
    return lifecycle


def _representation_command_engine() -> RepresentationCommandService:
    commands = _library_engine().get_service(REPRESENTATION_COMMAND_SERVICE)
    if commands is None:
        raise EngineError(
            "the representation command module is unavailable",
            code="representation_command_unavailable", retryable=True,
        )
    return commands


_ITEM_COMMAND_MAX_BYTES = 1024 * 1024


def _item_command_json(envelope: str) -> Mapping:
    """Read one bounded, duplicate-free canonical command envelope."""
    length = request.content_length
    if length is not None and length > _ITEM_COMMAND_MAX_BYTES:
        raise EngineValidationError(
            "the item mutation document is too large",
            code="item_mutation_too_large",
            details={"maximum_bytes": _ITEM_COMMAND_MAX_BYTES},
        )
    if request.mimetype != "application/json":
        raise EngineValidationError(
            "the item mutation must use application/json",
            code="invalid_item_mutation_document",
            details={"content_type": str(request.content_type or "")},
        )
    encoded = request.stream.read(_ITEM_COMMAND_MAX_BYTES + 1)
    if len(encoded) > _ITEM_COMMAND_MAX_BYTES:
        raise EngineValidationError(
            "the item mutation document is too large",
            code="item_mutation_too_large",
            details={"maximum_bytes": _ITEM_COMMAND_MAX_BYTES},
        )

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON object key")
            value[key] = item
        return value

    try:
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")),
        )
    except (RecursionError, UnicodeError, TypeError, ValueError) as exc:
        raise EngineValidationError(
            "the item mutation document is invalid JSON",
            code="invalid_item_mutation_document",
            details={"cause_type": type(exc).__name__},
        ) from exc
    if not isinstance(payload, Mapping) or set(payload) != {envelope}:
        raise EngineValidationError(
            f"the item mutation must contain exactly {envelope!r}",
            code="invalid_item_mutation_envelope",
            details={"field": envelope},
        )
    return payload[envelope]


def _item_command_operation_id(*, item_id: str = "") -> str:
    operation_id = request.headers.get("Idempotency-Key")
    if operation_id is None or operation_id == "":
        details = {"header": "Idempotency-Key"}
        if item_id:
            details["item_id"] = item_id
        raise EnginePreconditionRequiredError(
            "an idempotency key is required",
            code="idempotency_key_required",
            details=details,
        )
    return operation_id


def _item_command_record_match(item_id: str) -> str:
    raw = request.headers.get("If-Record-Match")
    if raw is None or raw == "":
        raise EnginePreconditionRequiredError(
            "an item revision is required",
            code="item_revision_required",
            details={"header": "If-Record-Match", "item_id": item_id},
        )
    value = raw.strip()
    if (
        raw != value
        or value.startswith("W/")
        or len(value) < 3
        or value[0] != '"'
        or value[-1] != '"'
        or "," in value
        or not _engine_valid_record_revision(value[1:-1])
    ):
        raise EngineValidationError(
            "If-Record-Match must contain one strong quoted item revision",
            code="invalid_item_revision",
            details={"header": "If-Record-Match", "item_id": item_id},
        )
    return value[1:-1]


def _representation_command_match(
        item_id: str, representation_id: str, *, required: bool) -> str | None:
    raw = request.headers.get("If-Representation-Match")
    if raw is None:
        if required:
            raise EnginePreconditionRequiredError(
                "a representation revision is required",
                code="representation_revision_required",
                details={
                    "header": "If-Representation-Match",
                    "item_id": item_id,
                    "representation_id": representation_id,
                },
            )
        return None
    value = raw.strip()
    if (
        raw != value
        or value.startswith("W/")
        or len(value) < 3
        or value[0] != '"'
        or value[-1] != '"'
        or "," in value
        or not _engine_valid_record_revision(value[1:-1])
    ):
        raise EngineValidationError(
            "If-Representation-Match must contain one strong quoted revision",
            code="invalid_representation_revision",
            details={
                "header": "If-Representation-Match",
                "item_id": item_id,
                "representation_id": representation_id,
            },
        )
    return value[1:-1]


def _representation_command_draft(
        representation_id: str) -> RepresentationAttachmentDraft:
    raw = _item_command_json("representation")
    fields = {
        "source_token", "acquisition", "role", "media_type", "label",
        "metadata", "expected_content_sha256", "expected_size",
    }
    if (
        not isinstance(raw, Mapping)
        or set(raw) != fields
        or any(not isinstance(raw.get(field), str) for field in fields - {
            "metadata", "expected_size",
        })
        or not isinstance(raw.get("metadata"), Mapping)
        or (raw.get("expected_size") is not None
            and (isinstance(raw.get("expected_size"), bool)
                 or not isinstance(raw.get("expected_size"), int)))
    ):
        raise EngineValidationError(
            "the representation attachment does not match its schema",
            code="invalid_representation_attachment",
        )
    try:
        return RepresentationAttachmentDraft(
            representation_id=representation_id,
            source_token=raw["source_token"],
            acquisition=raw["acquisition"],
            expected_content_sha256=raw["expected_content_sha256"],
            expected_size=raw["expected_size"],
            role=raw["role"],
            media_type=raw["media_type"],
            label=raw["label"],
            metadata=raw["metadata"],
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise EngineValidationError(
            "the representation attachment is invalid",
            code="invalid_representation_attachment",
            details={"cause_type": type(exc).__name__},
        ) from exc


def _representation_command_response(result, *, created: bool = False):
    response = jsonify({
        "ok": True,
        "schema": "librarytool.representation-mutation-receipt/1",
        **result.as_dict(),
    })
    response.headers["X-Record-Revision"] = (
        result.receipt.after_item_revision
    )
    if result.receipt.after is not None:
        response.headers["X-Representation-Revision"] = (
            result.receipt.after.revision
        )
    response.cache_control.no_store = True
    return response, 201 if created and not result.replayed else 200


def _item_command_managed_fields(*values) -> None:
    fields = sorted({
        key for value in values for key in value
        if key in _ENGINE_ITEM_COMMAND_MANAGED_FIELDS
    })
    if fields:
        raise EngineValidationError(
            "server-managed item fields cannot be changed here",
            code="managed_item_fields_not_writable",
            details={"fields": fields},
        )


def _item_command_draft() -> ItemDraft:
    raw = _item_command_json("item")
    if (
        not isinstance(raw, Mapping)
        or set(raw) != {"kind", "title", "metadata", "representations"}
        or not isinstance(raw.get("kind"), str)
        or not isinstance(raw.get("title"), str)
        or not isinstance(raw.get("metadata"), Mapping)
        or not isinstance(raw.get("representations"), list)
    ):
        raise EngineValidationError(
            "the item draft does not match its canonical schema",
            code="invalid_item_draft",
        )
    try:
        draft = ItemDraft.from_dict(raw)
    except (RecursionError, TypeError, ValueError) as exc:
        raise EngineValidationError(
            "the item draft does not match its canonical schema",
            code="invalid_item_draft",
            details={"cause_type": type(exc).__name__},
        ) from exc
    if draft.kind != "book":
        raise EngineValidationError(
            "this catalogue supports book items only",
            code="unsupported_item_kind",
            details={"kind": draft.kind},
        )
    if draft.representations:
        raise EngineValidationError(
            "representation attachment is a separate operation",
            code="representation_mutation_not_supported",
        )
    _item_command_managed_fields(draft.metadata)
    try:
        if draft.title != draft.title.strip():
            raise ValueError("title has outer whitespace")
        _engine_validate_catalogue_metadata(
            draft.metadata, strict_fields=frozenset(draft.metadata))
    except (RecursionError, TypeError, ValueError) as exc:
        raise EngineValidationError(
            "the item metadata is invalid",
            code="invalid_item_metadata",
            details={"cause_type": type(exc).__name__},
        ) from exc
    return draft


def _item_command_patch() -> ItemPatch:
    raw = _item_command_json("patch")
    fields = {"title", "metadata_set", "metadata_remove", "representations"}
    if (
        not isinstance(raw, Mapping)
        or set(raw) != fields
        or (raw.get("title") is not None
            and not isinstance(raw.get("title"), str))
        or not isinstance(raw.get("metadata_set"), Mapping)
        or not isinstance(raw.get("metadata_remove"), list)
    ):
        raise EngineValidationError(
            "the item patch does not match its canonical schema",
            code="invalid_item_patch",
        )
    if raw.get("representations") is not None:
        raise EngineValidationError(
            "representation attachment is a separate operation",
            code="representation_mutation_not_supported",
        )
    try:
        patch = ItemPatch(
            title=raw["title"],
            metadata_set=raw["metadata_set"],
            metadata_remove=tuple(raw["metadata_remove"]),
            representations=None,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise EngineValidationError(
            "the item patch does not match its canonical schema",
            code="invalid_item_patch",
            details={"cause_type": type(exc).__name__},
        ) from exc
    _item_command_managed_fields(
        patch.metadata_set, patch.metadata_remove)
    try:
        if patch.title is not None and patch.title != patch.title.strip():
            raise ValueError("title has outer whitespace")
        _engine_validate_catalogue_metadata(
            patch.metadata_set,
            strict_fields=frozenset(patch.metadata_set),
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise EngineValidationError(
            "the item metadata patch is invalid",
            code="invalid_item_metadata",
            details={"cause_type": type(exc).__name__},
        ) from exc
    return patch


def _item_command_response(result, *, created: bool = False):
    response = jsonify({
        "ok": True,
        "schema": "librarytool.item-mutation-receipt/1",
        **result.as_dict(),
    })
    response.headers["X-Record-Revision"] = result.receipt.after_revision
    response.cache_control.no_store = True
    return response, 201 if created and not result.replayed else 200


def _translation_engine() -> TranslationService:
    translations = _library_engine().translations
    if translations is None:
        raise EngineError(
            "the translation module is unavailable",
            code="translation_module_unavailable", retryable=True,
        )
    return translations


def _translation_revision_token(payload: dict, header: str, field: str) -> str:
    """Read one strong header validator or portable JSON revision field."""
    if header in request.headers:
        raw = request.headers.get(header)
        value = raw.strip() if isinstance(raw, str) else ""
        if value.startswith("W/") or not (
                len(value) >= 2 and value[0] == '"' and value[-1] == '"'
                and '"' not in value[1:-1]
                and "\\" not in value[1:-1]):
            raise EngineValidationError(
                f"{header} must contain one strong quoted revision",
                code="invalid_translation_page_update",
                details={"header": header, "field": field},
            )
        value = value[1:-1]
    else:
        raw = payload.get(field)
        if raw is None or raw == "":
            return ""
        if not isinstance(raw, str):
            raise EngineValidationError(
                f"{field} must be a portable revision",
                code="invalid_translation_page_update",
                details={"header": header, "field": field},
            )
        value = raw
    if not _TRANSLATION_PORTABLE_ID.fullmatch(value):
        raise EngineValidationError(
            f"{field} must be a portable revision",
            code="invalid_translation_page_update",
            details={"header": header, "field": field},
        )
    return value


def _translation_preconditions(payload: dict) -> tuple[str, str]:
    document_revision = _translation_revision_token(
        payload, "If-Document-Match", "expected_document_revision")
    source_revision = _translation_revision_token(
        payload, "If-Source-Match", "expected_source_revision")
    missing = []
    if not document_revision:
        missing.append({"header": "If-Document-Match",
                        "field": "expected_document_revision"})
    if not source_revision:
        missing.append({"header": "If-Source-Match",
                        "field": "expected_source_revision"})
    if missing:
        raise EnginePreconditionRequiredError(
            "translation document and source preconditions are required",
            code="translation_preconditions_required",
            details={"required": missing},
        )
    return document_revision, source_revision


def _translation_json_response(body: dict, *, view_revision: str,
                               document_revision: str = "",
                               source_revision: str = "",
                               conditional: bool = False):
    response = jsonify(body)
    response.set_etag(view_revision)
    response.cache_control.no_cache = True
    if document_revision:
        response.headers["X-Document-Revision"] = document_revision
    if source_revision:
        response.headers["X-Source-Revision"] = source_revision
    return response.make_conditional(request) if conditional else response


def _item_projection() -> str:
    projection = str(request.args.get("projection") or "").strip()
    if projection not in {"", "build-workbench"}:
        raise EngineValidationError(
            "the requested item projection is not supported",
            code="invalid_item_projection",
            details={"projection": projection},
        )
    return projection


def _item_response_revision(prefix: str, value) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return prefix + "-" + hashlib.sha256(canonical).hexdigest()[:24]


def _item_json_response(
        body: dict, revision: str, *, contains_compatibility: bool = False):
    """Return a revalidatable engine snapshot with a strong aggregate ETag."""
    response = jsonify({**body, "revision": revision})
    response.set_etag(revision)
    if contains_compatibility:
        # The explicit transitional projection can contain local filesystem
        # paths and must never enter a browser or intermediary cache.
        response.cache_control.no_store = True
        return response
    response.cache_control.no_cache = True
    return response.make_conditional(request)


def _item_lifecycle_revision_token(value: str) -> bool:
    """Return whether *value* is safe as an opaque lifecycle validator."""

    return (
        isinstance(value, str)
        and 0 < len(value) <= 512
        and value == value.strip()
        and '"' not in value
        and "\\" not in value
        and all(
            ord(character) > 32
            and ord(character) != 127
            and not 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        )
    )


def _item_lifecycle_match(
    header: str,
    *,
    required_code: str,
    invalid_code: str,
    details: Mapping,
) -> str:
    """Read exactly one strong, quoted lifecycle revision header."""

    raw = request.headers.get(header)
    error_details = {"header": header, **dict(details)}
    if raw is None or raw == "":
        raise EnginePreconditionRequiredError(
            f"{header} is required",
            code=required_code,
            details=error_details,
        )
    value = raw.strip()
    token = value[1:-1] if len(value) >= 2 else ""
    if (
        raw != value
        or value.startswith("W/")
        or len(value) < 3
        or value[0] != '"'
        or value[-1] != '"'
        or not _item_lifecycle_revision_token(token)
    ):
        raise EngineValidationError(
            f"{header} must contain one strong quoted revision",
            code=invalid_code,
            details=error_details,
        )
    return token


def _item_lifecycle_require_empty_body() -> None:
    """Lifecycle commands are header-only so their replay identity is exact."""

    content_length = request.content_length
    has_transfer_encoding = bool(request.headers.get("Transfer-Encoding"))
    has_unframed_data = (
        content_length in (None, 0)
        and not has_transfer_encoding
        and bool(request.get_data(cache=False))
    )
    if (
        content_length not in (None, 0)
        or has_transfer_encoding
        or has_unframed_data
    ):
        raise EngineValidationError(
            "item lifecycle commands do not accept a request body",
            code="item_lifecycle_body_not_allowed",
        )


def _item_lifecycle_receipt_response(result):
    receipt = result.receipt
    response = jsonify({
        "ok": True,
        "schema": "librarytool.item-lifecycle-receipt/1",
        **result.as_dict(),
    })
    response.cache_control.no_store = True
    response.headers["X-Record-Revision"] = (
        receipt.restored_item_revision
        if receipt.action == "restore"
        else receipt.deleted_item_revision
    )
    response.headers["X-Managed-Tree-Revision"] = (
        receipt.managed_tree_revision
    )
    response.headers["X-Tombstone-Revision"] = receipt.tombstone.revision
    if receipt.action == "restore":
        response.headers["Location"] = url_for(
            "api_v1_item", item_id=receipt.item_id
        )
        status = 200 if result.replayed else 201
    else:
        response.headers["Location"] = url_for(
            "api_v1_item_tombstone",
            tombstone_id=receipt.tombstone.tombstone_id,
        )
        status = 200
    return response, status


def _project_build_compatibility(items: list[dict]) -> None:
    """Attach the old build shape only for the transitional web workbench.

    The default versioned API never includes local PDF/image paths. Existing
    browser code can explicitly request this projection while it migrates one
    feature at a time; alternate clients have no reason to depend on it.
    """
    builds = lib.load_json(BUILDS_PATH, {})
    if not isinstance(builds, dict):
        return
    for item in items:
        build = builds.get(item.get("id"))
        if isinstance(build, dict):
            item["compatibility"] = {
                "schema": "librarytool.build-record/1",
                "build": dict(build),
            }


@app.route("/api/v1/items")
def api_v1_items():
    """Portable catalogue snapshots for every installed client framework."""
    try:
        projection = _item_projection()
        items = [view.as_dict() for view in _item_engine().list_items()]
        if projection == "build-workbench":
            _project_build_compatibility(items)
    except EngineError as exc:
        return _engine_error_response(exc)
    revision = _item_response_revision("ic", items)
    return _item_json_response({
        "ok": True, "schema": "librarytool.items/1", "items": items,
    }, revision, contains_compatibility=(projection == "build-workbench"))


@app.route("/api/v1/items", methods=["POST"])
def api_v1_items_create():
    """Create one catalogue-only book through the durable command engine."""
    try:
        operation_id = _item_command_operation_id()
        draft = _item_command_draft()
        result = _item_command_engine().create(
            CreateItemCommand(draft=draft, operation_id=operation_id))
    except EngineError as exc:
        return _engine_error_response(exc)
    return _item_command_response(result, created=True)


@app.route("/api/v1/items/<item_id>")
def api_v1_item(item_id: str):
    try:
        projection = _item_projection()
        item = _item_engine().get_item(item_id).as_dict()
        if projection == "build-workbench":
            _project_build_compatibility([item])
    except EngineError as exc:
        return _engine_error_response(exc)
    revision = (
        item["revision"] if not projection else
        _item_response_revision("ip", item)
    )
    response = _item_json_response({
        "ok": True, "schema": "librarytool.item/1", "item": item,
    }, revision, contains_compatibility=(projection == "build-workbench"))
    response.headers["X-Record-Revision"] = item["record_revision"]
    return response


@app.route("/api/v1/items/<item_id>/lifecycle")
def api_v1_item_lifecycle(item_id: str):
    """Expose one coherent dual-CAS preflight for recoverable deletion."""

    try:
        state = _item_lifecycle_engine().inspect(item_id)
    except EngineError as exc:
        return _engine_error_response(exc)
    item_revision = state.item.revision
    managed_tree_revision = state.managed_tree.revision
    revision = _item_response_revision("il", state.as_dict())
    response = jsonify({
        "ok": True,
        "schema": "librarytool.item-lifecycle-state/1",
        "state": "live",
        "item_id": state.item.item_id,
        "item_revision": item_revision,
        "managed_tree_revision": managed_tree_revision,
        "revision": revision,
    })
    response.set_etag(revision)
    response.headers["X-Record-Revision"] = item_revision
    response.headers["X-Managed-Tree-Revision"] = managed_tree_revision
    response.cache_control.no_cache = True
    return response.make_conditional(request)


@app.route("/api/v1/items/<item_id>", methods=["PATCH"])
def api_v1_item_update(item_id: str):
    """Patch portable catalogue metadata under idempotency and item CAS."""
    try:
        operation_id = _item_command_operation_id(item_id=item_id)
        expected_revision = _item_command_record_match(item_id)
        patch = _item_command_patch()
        result = _item_command_engine().update(UpdateItemCommand(
            item_id=item_id,
            expected_revision=expected_revision,
            patch=patch,
            operation_id=operation_id,
        ))
    except EngineError as exc:
        return _engine_error_response(exc)
    return _item_command_response(result)


@app.route("/api/v1/items/<item_id>", methods=["DELETE"])
def api_v1_item_delete(item_id: str):
    """Recoverably delete an item and its engine-managed tree under dual CAS."""

    try:
        lifecycle = _item_lifecycle_engine()
        _item_lifecycle_require_empty_body()
        operation_id = _item_command_operation_id(item_id=item_id)
        item_revision = _item_lifecycle_match(
            "If-Record-Match",
            required_code="item_revision_required",
            invalid_code="invalid_item_revision",
            details={"item_id": item_id},
        )
        managed_tree_revision = _item_lifecycle_match(
            "If-Managed-Tree-Match",
            required_code="managed_tree_revision_required",
            invalid_code="invalid_managed_tree_revision",
            details={"item_id": item_id},
        )
        result = lifecycle.delete(LifecycleDeleteItemCommand(
            item_id=item_id,
            expected_item_revision=item_revision,
            expected_managed_tree_revision=managed_tree_revision,
            operation_id=operation_id,
        ))
    except EngineError as exc:
        return _engine_error_response(exc)
    return _item_lifecycle_receipt_response(result)


@app.route("/api/v1/item-tombstones")
def api_v1_item_tombstones():
    """List transport-safe lifecycle tombstones in engine order."""

    try:
        lifecycle = _item_lifecycle_engine()
        state_values = request.args.getlist("state")
        unknown_filters = sorted(set(request.args) - {"state"})
        if (
            len(state_values) > 1
            or unknown_filters
            or (state_values and state_values[0] not in {"deleted", "restored"})
        ):
            raise EngineValidationError(
                "the item tombstone filter is invalid",
                code="invalid_item_tombstone_filter",
                details={
                    "state": state_values,
                    "unknown": unknown_filters,
                },
            )
        wanted_state = state_values[0] if state_values else ""
        tombstones = [
            tombstone.as_dict()
            for tombstone in lifecycle.list_tombstones()
            if not wanted_state or tombstone.state == wanted_state
        ]
    except EngineError as exc:
        return _engine_error_response(exc)
    response = jsonify({
        "ok": True,
        "schema": "librarytool.item-tombstone-list/1",
        "tombstones": tombstones,
    })
    response.cache_control.no_cache = True
    return response


@app.route("/api/v1/item-tombstones/<tombstone_id>")
def api_v1_item_tombstone(tombstone_id: str):
    """Read one public tombstone without its private recovery envelope."""

    try:
        tombstone = _item_lifecycle_engine().get_tombstone(tombstone_id)
    except EngineError as exc:
        return _engine_error_response(exc)
    response = jsonify({
        "ok": True,
        "schema": "librarytool.item-tombstone/1",
        "tombstone": tombstone.as_dict(),
    })
    response.set_etag(tombstone.revision)
    response.headers["X-Tombstone-Revision"] = tombstone.revision
    response.cache_control.no_cache = True
    return response.make_conditional(request)


@app.route(
    "/api/v1/item-tombstones/<tombstone_id>/restore",
    methods=["POST"],
)
def api_v1_item_tombstone_restore(tombstone_id: str):
    """Restore a deleted aggregate under tombstone CAS and idempotency."""

    try:
        lifecycle = _item_lifecycle_engine()
        _item_lifecycle_require_empty_body()
        operation_id = _item_command_operation_id()
        tombstone_revision = _item_lifecycle_match(
            "If-Tombstone-Match",
            required_code="tombstone_revision_required",
            invalid_code="invalid_tombstone_revision",
            details={"tombstone_id": tombstone_id},
        )
        result = lifecycle.restore(RestoreItemCommand(
            tombstone_id=tombstone_id,
            expected_tombstone_revision=tombstone_revision,
            operation_id=operation_id,
        ))
    except EngineError as exc:
        return _engine_error_response(exc)
    return _item_lifecycle_receipt_response(result)


@app.route("/api/v1/items/<item_id>/representations")
def api_v1_item_representations(item_id: str):
    try:
        rows = [value.as_dict() for value in
                _item_engine().list_representations(item_id)]
    except EngineError as exc:
        return _engine_error_response(exc)
    revision = _item_response_revision("rc", rows)
    return _item_json_response({
        "ok": True, "schema": "librarytool.representations/1",
        "item_id": item_id, "representations": rows,
    }, revision)


@app.route(
    "/api/v1/items/<item_id>/representations/<representation_id>",
    methods=["PUT"],
)
def api_v1_item_representation_put(item_id: str, representation_id: str):
    """Attach a new source, or replace one under dual CAS preconditions."""
    try:
        operation_id = _item_command_operation_id(item_id=item_id)
        expected_item_revision = _item_command_record_match(item_id)
        expected_representation_revision = _representation_command_match(
            item_id, representation_id, required=False,
        )
        draft = _representation_command_draft(representation_id)
        result = _representation_command_engine().attach(
            AttachRepresentationCommand(
                item_id=item_id,
                expected_item_revision=expected_item_revision,
                expected_representation_revision=(
                    expected_representation_revision
                ),
                draft=draft,
                operation_id=operation_id,
            )
        )
    except EngineError as exc:
        return _engine_error_response(exc)
    return _representation_command_response(
        result, created=expected_representation_revision is None,
    )


@app.route(
    "/api/v1/items/<item_id>/representations/<representation_id>",
    methods=["DELETE"],
)
def api_v1_item_representation_delete(item_id: str, representation_id: str):
    """Detach one representation without deleting an external source file."""
    try:
        operation_id = _item_command_operation_id(item_id=item_id)
        expected_item_revision = _item_command_record_match(item_id)
        expected_representation_revision = _representation_command_match(
            item_id, representation_id, required=True,
        )
        assert expected_representation_revision is not None
        result = _representation_command_engine().detach(
            DetachRepresentationCommand(
                item_id=item_id,
                representation_id=representation_id,
                expected_item_revision=expected_item_revision,
                expected_representation_revision=(
                    expected_representation_revision
                ),
                operation_id=operation_id,
            )
        )
    except EngineError as exc:
        return _engine_error_response(exc)
    return _representation_command_response(result)


@app.route("/api/v1/items/<item_id>/artifacts")
def api_v1_item_artifacts(item_id: str):
    try:
        rows = [value.as_dict() for value in
                _item_engine().list_artifacts(item_id)]
    except EngineError as exc:
        return _engine_error_response(exc)
    revision = _item_response_revision("ac", rows)
    return _item_json_response({
        "ok": True, "schema": "librarytool.artifacts/1",
        "item_id": item_id, "artifacts": rows,
    }, revision)


@app.route("/api/v1/items/<item_id>/readiness")
def api_v1_item_readiness(item_id: str):
    try:
        state = _item_engine().readiness(item_id).as_dict()
    except EngineError as exc:
        return _engine_error_response(exc)
    return _item_json_response({
        "ok": True, "schema": "librarytool.workbench-state/1",
        "item_id": item_id, "state": state,
    }, state["revision"])


@app.route("/api/v1/items/<item_id>/translations")
def api_v1_item_translations(item_id: str):
    """List provider-neutral translation summaries for one item."""
    try:
        rows = [value.as_dict()
                for value in _translation_engine().list(item_id)]
    except EngineError as exc:
        return _engine_error_response(exc)
    revision = _item_response_revision("tlc", rows)
    return _translation_json_response({
        "ok": True,
        "schema": "librarytool.translation-summaries/1",
        "item_id": item_id,
        "translations": rows,
        "revision": revision,
    }, view_revision=revision, conditional=True)


@app.route("/api/v1/items/<item_id>/translations/<translation_id>")
def api_v1_item_translation(item_id: str, translation_id: str):
    """Read a coherent translation document and authoritative source view."""
    try:
        view = _translation_engine().get(item_id, translation_id)
    except EngineError as exc:
        return _engine_error_response(exc)
    return _translation_json_response({
        "ok": True,
        "schema": "librarytool.translation/1",
        "translation": view.as_dict(),
    }, view_revision=view.view_revision,
        document_revision=view.document_revision,
        source_revision=view.source.revision,
        conditional=True)


@app.route(
    "/api/v1/items/<item_id>/translations/<translation_id>/pages/<selector>",
    methods=["PUT"],
)
def api_v1_item_translation_page(
        item_id: str, translation_id: str, selector: str):
    """Replace one human translation page under document and source CAS."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _engine_error_response(EngineValidationError(
            "a translation page update must be a JSON object",
            code="invalid_translation_page_update",
        ))
    if "text" not in payload or not isinstance(payload.get("text"), str):
        return _engine_error_response(EngineValidationError(
            "translation page text must be a string",
            code="invalid_translation_page_text",
            details={"field": "text"},
        ))
    try:
        document_revision, source_revision = _translation_preconditions(payload)
        try:
            command = ReplaceTranslationPageCommand(
                item_id=item_id,
                translation_id=translation_id,
                selector=selector,
                text=payload["text"],
                expected_document_revision=document_revision,
                expected_source_revision=source_revision,
            )
        except (TypeError, ValueError) as exc:
            raise EngineValidationError(
                "the translation page update is invalid",
                code="invalid_translation_page_update",
                details={"item_id": item_id,
                         "translation_id": translation_id,
                         "selector": selector},
            ) from exc
        view = _translation_engine().replace_page(command)
    except EngineError as exc:
        return _engine_error_response(exc)
    return _translation_json_response({
        "ok": True,
        "schema": "librarytool.translation/1",
        "translation": view.as_dict(),
    }, view_revision=view.view_revision,
        document_revision=view.document_revision,
        source_revision=view.source.revision)


@app.route("/api/builds/<build_id>/ocr-regions")
def api_build_ocr_regions(build_id: str):
    """One page's typed regions — the Phase 1 substrate the region facsimile
    and the Replica workbench read: {ok, found, doc, dims, items: [{id, role,
    box, order, text}]}. ?src= and ?page= select the record."""
    src = request.args.get("src") or "primary"
    try:
        page = int(request.args.get("page") or 0)
    except (TypeError, ValueError, OverflowError):
        page = 0
    try:
        view = _replica_engine().get_region_page(
            PageKey(build_id, src, page))
    except EngineError as exc:
        return _engine_error_response(exc)
    return _json_with_etag(_region_view_response(view))


# The sanitizers live in tools/libformat.py now — the single implementation
# the .lib Python API and these routes share, so a file that round-trips
# through the API can never come out a shape the app rejects. These aliases
# keep the in-module names (and their exact call sites) unchanged.
_RW_MAX_ITEMS = libformat.MAX_ITEMS
_RW_ROLE_RE = libformat.ROLE_RE
_rw_sanitize_items = libformat.sanitize_page_items
_rw_sanitize_dims = libformat.sanitize_dims


@app.route("/api/builds/<build_id>/ocr-regions", methods=["PUT"])
def api_build_ocr_regions_put(build_id: str):
    """Replace one page's typed regions with the Replica workbench's edited
    set. Body: {src?, page, doc?, dims?, items: [{role, box{x,y,w,h}, order,
    text}]}. Items are sanitized (roles kebab-case, boxes clamped to the
    page, text capped), re-ordered, and re-idd; an empty list drops the
    page's record. src_type becomes "human" — the set is human-curated from
    here on."""
    p = request.get_json(silent=True) or {}
    src = str(p.get("src") or "primary")
    try:
        # OverflowError: json.loads accepts the non-standard Infinity
        # literal, and int(inf) raises it rather than ValueError
        page = int(p.get("page") or 0)
    except (TypeError, ValueError, OverflowError):
        page = 0
    expected = _region_match_token(p)
    raw = p.get("items")
    if not isinstance(raw, list) or len(raw) > _RW_MAX_ITEMS:
        return jsonify({"ok": False, "error": "bad items"}), 400
    command = ReplaceRegionPageCommand(
        key=PageKey(build_id, src, page),
        expected_revision=expected,
        items=raw,
        doc=str(p.get("doc") or "compiled.txt"),
        dims=p.get("dims") if isinstance(p.get("dims"), dict) else {},
        state="verified" if p.get("state") == "verified" else "",
        preserve_ext="ext" not in p,
        ext=p.get("ext") if isinstance(p.get("ext"), dict) else {},
    )
    try:
        view = _replica_engine().replace_region_page(command)
    except EngineError as exc:
        current = None
        if isinstance(exc, EngineConflictError):
            try:
                current = _replica_engine().get_region_page(command.key)
            except EngineError:
                pass
        return _engine_error_response(exc, current=current)
    body = _region_view_response(view)
    body["count"] = len(view.items)
    return _json_with_etag(body)


def _proposal_match_token(payload: dict) -> str:
    raw = str(request.headers.get("If-Proposal-Match") or
              payload.get("expect_proposal_revision") or "").strip()
    if raw.startswith("W/"):
        raw = raw[2:].strip()
    return raw.strip('"')


@app.route("/api/builds/<build_id>/ocr-region-proposals", methods=["POST"])
def api_build_ocr_region_proposals(build_id: str):
    """Apply or dismiss one machine proposal with two-resource CAS."""
    p = request.get_json(silent=True) or {}
    src = str(p.get("src") or "primary")
    try:
        page = int(p.get("page") or 0)
    except (TypeError, ValueError, OverflowError):
        page = 0
    action = str(p.get("action") or "").strip().lower()
    expected = _region_match_token(p)
    expected_proposal = _proposal_match_token(p)
    command = ReviewRegionProposalCommand(
        key=PageKey(build_id, src, page),
        action=action,
        expected_region_revision=expected,
        expected_proposal_revision=expected_proposal,
    )
    staged_figures: set[str] = set()
    if action == "dismiss":
        try:
            before = _replica_engine().get_region_page(command.key)
            staged_figures = _ocr_staged_figure_names(before.proposal)
        except EngineError:
            pass
    try:
        result = _replica_engine().review_region_proposal(command)
    except EngineError as exc:
        current = None
        if isinstance(exc, EngineConflictError):
            try:
                current = _replica_engine().get_region_page(command.key)
            except EngineError:
                pass
        return _engine_error_response(exc, current=current)
    if action == "dismiss":
        # The atomic manifest commit above makes the crops unreachable. The
        # physical unlink is idempotent; startup GC finishes it after a crash
        # between these two operations.
        _ocr_remove_staged_figures(build_id, staged_figures)
    _ocr_cleanup_staged_figure_orphans(build_id)
    body = _region_view_response(result.page)
    body.update({"action": ("applied" if result.action.value == "apply"
                             else "dismissed"),
                 "compiled": result.compiled})
    if result.derived_failure is not None:
        body["warning"] = (
            "compiled text rebuild pending: " +
            result.derived_failure.message)
    response = _json_with_etag(body)
    return (response, 202) if not result.compiled else response


@app.route("/api/builds/<build_id>/ocr-regions/recompile", methods=["POST"])
def api_build_ocr_regions_recompile(build_id: str):
    """Rewrite compiled body text from the saved regions. Every region page
    of the source (or just ?page) recomposes — furniture (marginalia, heads,
    catchwords) stays out of the flow, figure refs stay in — and merges into
    the compiled file its record names. A pending accepted text-only proposal
    can also rebuild a page whose old region layer was explicitly removed."""
    p = request.get_json(silent=True) or {}
    src = str(p.get("src") or "primary")
    # an absent page means "every region page"; a PRESENT but garbage page
    # must refuse — mapping it to the no-filter case would rewrite the whole
    # compiled file when the caller asked to touch exactly one page
    only = None
    if "page" in p:
        try:
            only = int(p["page"])
        except (TypeError, ValueError, OverflowError):
            only = 0
    # layer "norm" composes the normalized reading into one explicit target —
    # the modern-edition text; the default layer writes each page's
    # diplomatic body into the file its record names. The default target is
    # per SOURCE (normalized.txt / normalized-<src>.txt): both sources of a
    # two-scan build merge by bare page number, so sharing one file would
    # silently interleave two different books' pages into a chimera.
    layer = "norm" if str(p.get("layer") or "") in ("norm", "normalized") \
        else "text"
    try:
        result = _replica_engine().recompile_region_pages(
            RecompileRegionPagesCommand(
                item_id=build_id,
                source_id=src,
                layer=layer,
                page=only,
                target=str(p.get("target") or ""),
            ))
    except EngineError as exc:
        return _engine_error_response(exc)
    return jsonify({"ok": True, "pages": result.pages,
                    "docs": list(result.documents)})


# --- layout templates: recto/verso grids applied across a book -------------------

_RW_TPL_RE = re.compile(r"^[\w\- ]{1,24}$")


def _pdf_layer_words(pdf, page: int) -> list:
    """One page's text-layer words via fitz (pixel corners -> 0..1 fractions,
    block/line -> a line id), shaped like the stored OCR word boxes. Best-
    effort — no text layer just means empty regions to fill by hand."""
    if pdf is None or importlib.util.find_spec("fitz") is None:
        return []
    try:
        with _pdf_doc(pdf) as doc:
            if page > doc.page_count:
                return []
            pg = doc[page - 1]
            pw, ph = float(pg.rect.width), float(pg.rect.height)
            if pw <= 0 or ph <= 0:
                return []
            out = []
            lids: dict = {}
            for x0, y0, x1, y1, text, blk, ln, _wn in pg.get_text("words"):
                lid = lids.setdefault((blk, ln), len(lids))
                out.append({"t": text, "l": lid,
                            "x": x0 / pw, "y": y0 / ph,
                            "w": max(0.0, (x1 - x0) / pw),
                            "h": max(0.0, (y1 - y0) / ph)})
            return out
    except Exception:
        return []


@app.route("/api/builds/<build_id>/ocr-templates", methods=["GET", "PUT", "DELETE"])
@_live_item_write_endpoint
def api_build_ocr_templates(build_id: str):
    """Layout templates — hand-press books are grid-stable, so one corrected
    exemplar page (a recto, a verso) becomes a reusable region grid.
    GET ?src= lists them; PUT {src?, name, from_page} snapshots the SAVED
    region record of from_page (geometry + roles, no text); DELETE {src?,
    name} removes one. Stored in layout.json under templates.<src>.<name>."""
    b = lib.load_json(BUILDS_PATH, {}).get(build_id)
    if b is None:
        abort(404)
    meta_path = _entry_dir(build_id) / "ocr" / "layout.json"
    if request.method == "GET":
        src = _valid_src_key(b, request.args.get("src"))
        tpls = ((lib.load_json(meta_path, {}).get("templates") or {})
                .get(src or "primary") or {})
        return jsonify({"ok": True, "templates": [
            {"name": name, "items": len(t.get("items") or []),
             "from_page": t.get("from_page")}
            for name, t in sorted(tpls.items()) if isinstance(t, dict)]})
    p = request.get_json(silent=True) or {}
    src = _valid_src_key(b, p.get("src"))
    if not src:
        return jsonify({"ok": False, "error": "unknown source"}), 400
    name = str(p.get("name") or "").strip()
    if not _RW_TPL_RE.match(name):
        return jsonify({"ok": False, "error": "bad template name"}), 400
    with _ocr_merge_lock:
        meta = lib.load_json(meta_path, {})
        tmap = meta.setdefault("templates", {}).setdefault(src, {})
        if request.method == "DELETE":
            tmap.pop(name, None)
            if not tmap:
                meta["templates"].pop(src, None)
            if not meta["templates"]:
                meta.pop("templates", None)
            lib.save_json(meta_path, meta)
            return jsonify({"ok": True})
        try:
            from_page = int(p.get("from_page") or 0)
        except (TypeError, ValueError, OverflowError):
            from_page = 0
        rec = ((meta.get("regions") or {}).get(src) or {}).get(str(from_page))
        if not isinstance(rec, dict) or not rec.get("items"):
            return jsonify({"ok": False, "error":
                            "that page has no saved regions"}), 400
        tmap[name] = {
            "from_page": from_page,
            "doc": rec.get("doc") or "",
            "dims": rec.get("dims") or {},
            "items": [{"role": it.get("role") or "body",
                       "order": it.get("order") or i,
                       "box": it.get("box") or {}}
                      for i, it in enumerate(rec.get("items") or [])],
        }
        lib.save_json(meta_path, meta)
    return jsonify({"ok": True, "items": len(tmap[name]["items"])})


@app.route("/api/builds/<build_id>/ocr-templates/apply", methods=["POST"])
@_live_item_write_endpoint
def api_build_ocr_templates_apply(build_id: str):
    """Stamp a template's region grid onto a page range. Body: {src?, name,
    pages: [ints], overwrite?: bool, clip?: bool (default true)}. Pages that
    already carry a region record are skipped unless overwrite. With clip,
    each stamped region's text pre-fills from the word boxes inside it
    (stored OCR boxes, else the PDF text layer) — a template plus word
    geometry drafts the whole page; the human corrects instead of typing."""
    b = lib.load_json(BUILDS_PATH, {}).get(build_id)
    if b is None:
        abort(404)
    p = request.get_json(silent=True) or {}
    src = _valid_src_key(b, p.get("src"))
    if not src:
        return jsonify({"ok": False, "error": "unknown source"}), 400
    name = str(p.get("name") or "").strip()
    meta_path = _entry_dir(build_id) / "ocr" / "layout.json"
    meta = lib.load_json(meta_path, {})
    tpl = ((meta.get("templates") or {}).get(src) or {}).get(name)
    if not isinstance(tpl, dict) or not tpl.get("items"):
        return jsonify({"ok": False, "error": "unknown template"}), 400
    raw_pages = p.get("pages")
    if not isinstance(raw_pages, list) or not raw_pages or len(raw_pages) > 500:
        return jsonify({"ok": False, "error": "bad pages"}), 400
    pages = []
    for x in raw_pages:
        try:
            n = int(x)
        except (TypeError, ValueError, OverflowError):
            return jsonify({"ok": False, "error": "bad pages"}), 400
        if n < 1:
            return jsonify({"ok": False, "error": "bad pages"}), 400
        pages.append(n)
    overwrite = bool(p.get("overwrite"))
    clip = p.get("clip") is not False
    pdf = None
    if clip:
        raw = str(b.get("pdf_file") or "") if src == "primary" else next(
            (s.get("path") for s in (b.get("pdf_sources") or [])
             if s.get("id") == src), "")
        pdf = _resolve_local(raw or "")
    # Two phases. Preparation runs UNLOCKED — text-layer clipping opens the
    # PDF and must not stall OCR jobs — against a words snapshot read once
    # (the old shape re-parsed the whole sidecar per page, 500 times). The
    # skip-unless-overwrite decision is then re-made under the merge lock
    # against FRESH state in one read-modify-write: a page the user saved
    # while the loop ran must not get stamped over, and a snapshot-based
    # skip check could do exactly that.
    words_map = (meta.get("words") or {}).get(src) or {}
    existing0 = (meta.get("regions") or {}).get(src) or {}
    tpl_items = sorted(tpl["items"], key=lambda x: x.get("order") or 0)
    doc = _ocr_name(str(tpl.get("doc") or "compiled.txt"))
    prepared: dict[int, list] = {}
    pre_skipped, clipped = [], []
    for n in sorted(set(pages)):
        if not overwrite and str(n) in existing0:
            pre_skipped.append(n)
            continue
        words = []
        if clip:
            words = words_map.get(str(n))
            if not (isinstance(words, list) and words):
                words = _pdf_layer_words(pdf, n)
        items = []
        for i, t in enumerate(tpl_items):
            box = t.get("box") or {}
            text = layout_roles.clip_words_to_box(words, box) if words else ""
            items.append({"id": f"r{i}", "role": t.get("role") or "body",
                          "src_type": "template", "order": i,
                          "box": box, "text": text})
        items = libformat.ensure_rids(items)
        if any(it["text"] for it in items):
            clipped.append(n)
        prepared[n] = items
    applied, skipped, protected = [], list(pre_skipped), []
    with _ocr_merge_lock:
        cur = lib.load_json(meta_path, {})
        pmap = cur.setdefault("regions", {}).setdefault(src, {})
        for n, items in prepared.items():
            existing = pmap.get(str(n))
            if replica_service.is_protected(existing):
                protected.append(n)
                continue
            if not overwrite and existing is not None:
                skipped.append(n)
                continue
            pmap[str(n)] = {"doc": doc, "dims": tpl.get("dims") or {},
                            "items": items, "origin": "template"}
            applied.append(n)
        lib.save_json(meta_path, cur)
    clipped = [n for n in clipped if n in applied]
    return jsonify({"ok": True, "applied": sorted(applied),
                    "skipped": sorted(skipped),
                    "protected": sorted(protected),
                    "clipped": sorted(clipped)})


@app.route("/api/builds/<build_id>/ocr-templates/outliers", methods=["POST"])
def api_build_ocr_templates_outliers(build_id: str):
    """Score every region page of the source against a template: the pages
    where the grid broke (plates, chapter openings, errata) are the ones
    worth a human look. Body: {src?, name, threshold?: 0.5}. Returns {scores:
    {page: 0..1}, outliers: [pages below threshold]}."""
    b = lib.load_json(BUILDS_PATH, {}).get(build_id)
    if b is None:
        abort(404)
    p = request.get_json(silent=True) or {}
    src = _valid_src_key(b, p.get("src"))
    if not src:
        return jsonify({"ok": False, "error": "unknown source"}), 400
    name = str(p.get("name") or "").strip()
    meta = lib.load_json(_entry_dir(build_id) / "ocr" / "layout.json", {})
    tpl = ((meta.get("templates") or {}).get(src) or {}).get(name)
    if not isinstance(tpl, dict) or not tpl.get("items"):
        return jsonify({"ok": False, "error": "unknown template"}), 400
    try:
        threshold = min(1.0, max(0.0, float(p.get("threshold") or 0.5)))
    except (TypeError, ValueError, OverflowError):
        threshold = 0.5
    pages = (meta.get("regions") or {}).get(src) or {}
    scores, outliers = {}, []
    for k in sorted((k for k in pages if str(k).isdigit()), key=int):
        rec = pages[k]
        if not isinstance(rec, dict):
            continue
        s = layout_roles.template_score(tpl["items"], rec.get("items") or [])
        scores[k] = round(s, 3)
        if s < threshold:
            outliers.append(int(k))
    return jsonify({"ok": True, "scores": scores, "outliers": outliers})


@app.route("/api/builds/<build_id>/ocr-layout-families/propose",
           methods=["POST"])
def api_build_ocr_layout_families_propose(build_id: str):
    """Propose recurring page-layout families without changing the book.

    This thin transport adapter intentionally delegates all geometry and
    grouping policy to the framework-neutral Replica service. The result is a
    review proposal; applying family/template changes remains a separate
    revisioned command.
    """
    p = request.get_json(silent=True) or {}
    src = str(p.get("src") or "primary")
    options = {key: p[key] for key in (
        "similarity_threshold", "min_family_size",
        "low_confidence_threshold", "max_families",
        "max_regions_per_page") if key in p}
    try:
        result = _replica_engine().propose_layout_families(
            LayoutFamilyQuery(build_id, src, options))
    except EngineError as exc:
        return _engine_error_response(exc)
    return jsonify({"ok": True, "capability": result.capability,
                    "proposal": dict(result.proposal)})


# --- .lib export: the Replica working store, sealed ------------------------------

# The default role -> modern-type mapping a .lib carries until the style
# board exists to edit one. OFL faces only — a .lib may someday embed its
# fonts, and only open-licensed families can travel. Sizes are relative to
# the body (em), which the region boxes then scale to the page.
_LIB_STYLESHEET = {
    "body": {"family": "EB Garamond", "size_em": 1.0, "align": "justify"},
    "marginalia": {"family": "EB Garamond", "size_em": 0.78, "style": "italic"},
    "footnote": {"family": "EB Garamond", "size_em": 0.82},
    "title": {"family": "EB Garamond", "size_em": 1.25,
              "variant": "small-caps"},
    "header": {"family": "EB Garamond", "size_em": 0.85,
               "variant": "small-caps", "align": "center"},
    "footer": {"family": "EB Garamond", "size_em": 0.85, "align": "center"},
    "caption": {"family": "EB Garamond", "size_em": 0.85, "style": "italic",
                "align": "center"},
    "page-number": {"family": "EB Garamond", "size_em": 0.85},
    "catch-word": {"family": "EB Garamond", "size_em": 0.85,
                   "align": "right"},
    "signature-mark": {"family": "EB Garamond", "size_em": 0.85,
                       "align": "center"},
    # large capitals render at their box height; rubricated red by default —
    # an illuminated look the style board's color/bg fields can build on
    "drop-capital": {"family": "EB Garamond", "color": "#8b1a1a"},
    # the "page" pseudo-role: the facsimile's paper and ink
    "page": {"bg": "#fdfcf8", "color": "#1c1a17"},
}

_LIB_META_FIELDS = ("published_slug", "title", "subtitle", "authors", "year",
                    "publisher", "publisher_city", "edition", "volume",
                    "language", "pages", "source_url")


_RW_HEX_RE = libformat.HEX_RE
_rw_sanitize_styles = libformat.sanitize_styles


def _replica_style_path(build_id: str):
    return _entry_dir(build_id) / "ocr" / "replica-style.json"


def _replica_styles(build_id: str) -> tuple[dict, bool]:
    """The book's role->type stylesheet: (styles, custom). Falls back to the
    OFL seed when the style board has never saved one."""
    doc = lib.load_json(_replica_style_path(build_id), None)
    if isinstance(doc, dict) and isinstance(doc.get("styles"), dict) \
            and doc["styles"]:
        return doc["styles"], True
    return _LIB_STYLESHEET, False


# lib/2 identity + self-description sidecars, all under ocr/ beside layout.json.
# book_id is minted once and persisted so re-exports of the same book carry the
# same id; the per-book instructions and manifest ext travel with every export
# (the Replica-tab editor for the instructions field is a later step).
def _lib_id_path(build_id: str):
    return _entry_dir(build_id) / "ocr" / "lib-id.json"


def _lib_book_id(build_id: str) -> str:
    """The book's stable UUID (docs/lib-format.md §2.4), minted on first
    export. Imported identities are persisted; older local builds use a
    deterministic fallback so a read-only export never mutates the store."""
    doc = lib.load_json(_lib_id_path(build_id), None)
    if isinstance(doc, dict) and re.fullmatch(
            r"b-[0-9a-f]{32}", str(doc.get("book_id") or "")):
        return str(doc["book_id"])
    return "b-" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"https://librarytool.local/items/{build_id}").hex


def _lib_store_book_id(build_id: str, book_id: str) -> None:
    """Persist an imported identity; callers hold ``_ocr_merge_lock``."""
    if not re.fullmatch(r"b-[0-9a-f]{32}", str(book_id or "")):
        return
    _lib_id_path(build_id).parent.mkdir(parents=True, exist_ok=True)
    lib.save_json(_lib_id_path(build_id), {"book_id": book_id})


def _lib_book_instructions(build_id: str) -> str:
    """The per-book "instructions for editors/AI" text that travels in every
    export (§2.2). An optional sidecar for now — the Replica-tab field that
    edits it is a later step; absent means an empty string."""
    p = _entry_dir(build_id) / "ocr" / "lib-instructions.md"
    if p.is_file():
        return p.read_text(encoding="utf-8", errors="replace")[:20000]
    return ""


def _lib_manifest_ext_path(build_id: str):
    return _entry_dir(build_id) / "ocr" / "lib-ext.json"


def _lib_manifest_ext(build_id: str) -> dict:
    """The manifest-level `ext` passthrough (§2.4). An importer stores whatever
    third-party namespace a `.lib` carried here; export re-emits it verbatim."""
    doc = lib.load_json(_lib_manifest_ext_path(build_id), None)
    return libformat.sanitize_ext(doc) if isinstance(doc, dict) else {}


def _lib_translation_members(build_id: str, page_nums) -> dict:
    """Each stored translation as a translations/<bcp47>.json member: page-
    aligned text keyed by page (docs/lib-format.md §2.5). The app stores
    page-level translations, so the per-page rid map carries one reserved
    "_page" key holding the whole page's text — a shape that stays valid if
    per-region translations arrive later. Only the exported pages are
    included, so the member lines up with pages/."""
    out = {}
    tdir = _entry_dir(build_id) / "translations"
    if not tdir.is_dir():
        return out
    keep = set(page_nums)
    for f in sorted(tdir.glob("*.txt")):
        lang = _lang_code(f.stem)
        if not lang:
            continue
        pages = _an_pages(f.read_text(encoding="utf-8", errors="replace"))
        pmap = {str(n): {"_page": pages[n]} for n in sorted(pages)
                if n in keep and pages[n].strip()}
        if pmap:
            out[lang] = {"lang": lang, "pages": pmap}
    return out


def _lib_read_translation_members(z, warn) -> dict:
    """Parse each translations/<bcp47>.json member into {lang: {page: text}}.
    A page value is either the whole-page string (our reserved "_page" key) or
    a rid->text map, which we join in key order — so a per-region translation
    written by an external tool still lands as page text in the store."""
    out: dict = {}
    for name in z.namelist():
        # one shared member grammar with export/read_lib/validate; a
        # translations/ member that misses it is warned, not silently dropped
        if not name.startswith("translations/") or name.endswith("/"):
            continue
        tm = libformat._TRANS_MEMBER.fullmatch(name)
        if not tm:
            warn(name, "translation skipped: member name is not a "
                       "translations/<bcp47>.json tag")
            continue
        lang = tm.group(1).lower()
        if z.getinfo(name).file_size > _LIB_MAX_JSON:
            warn(name, "translation skipped: JSON member exceeds the size cap")
            continue
        try:
            td = json.loads(z.read(name))
        except (ValueError, KeyError):
            warn(name, "translation skipped: not valid JSON")
            continue
        pages_in = td.get("pages") if isinstance(td, dict) else None
        if not isinstance(pages_in, dict):
            warn(name, "translation skipped: no pages map")
            continue
        collected: dict = {}
        for pk, pv in pages_in.items():
            try:
                pn = int(pk)
            except (TypeError, ValueError):
                continue
            if not 1 <= pn <= 99999:
                continue
            if isinstance(pv, str):
                text = pv
            elif isinstance(pv, dict):
                if isinstance(pv.get("_page"), str):
                    text = pv["_page"]
                else:
                    text = "\n\n".join(str(pv[k]) for k in sorted(pv)
                                       if isinstance(pv[k], str) and pv[k].strip())
            else:
                text = ""
            text = text.strip()[:20000]
            if text:
                collected[pn] = text
        if collected:
            out.setdefault(lang, {}).update(collected)
    return out


def _lib_apply_translations(build_id: str, trans_in: dict,
                            overwrite: bool) -> list:
    """Merge imported translations into the entry's page-marked .txt store,
    page by page — a page already translated is kept unless overwrite. Returns
    the languages touched. Written under the analyze store's lock, the same one
    the live translate job holds."""
    added = []
    for lang, pmap in trans_in.items():
        rel = f"translations/{lang}.txt"
        touched = False
        touched_pages = []
        with _an_write_lock:
            cur = _an_pages(_read_entry_text(build_id, rel))
            for pn, text in pmap.items():
                if overwrite or not cur.get(pn, "").strip():
                    cur[pn] = text
                    touched = True
                    touched_pages.append(pn)
            if touched:
                doc = "\n\n".join(f"--- page {k} ---\n{cur[k]}"
                                  for k in sorted(cur))
                _write_entry_text(build_id, rel, doc + "\n")
                # .lib translation members do not carry this store's source
                # hash/model contract. Never let imported text inherit stale
                # local provenance and appear falsely current.
                meta_path = _translation_meta_path(build_id, lang)
                tm = _load_translation_meta(build_id, lang)
                for pn in touched_pages:
                    tm["pages"].pop(str(pn), None)
                if tm["pages"]:
                    lib.save_json(meta_path, tm)
                else:
                    meta_path.unlink(missing_ok=True)
        if touched:
            added.append(lang)
    return added


@app.route("/api/builds/<build_id>/replica-style",
           methods=["GET", "PUT", "DELETE"])
@_live_item_write_endpoint
def api_build_replica_style(build_id: str):
    """The book-level role -> modern-type mapping the re-typeset preview and
    the .lib export use. GET returns the stored sheet (custom: true) or the
    OFL seed (custom: false); PUT {styles: {role: {family, size_em, leading,
    style, variant, align}}} validates and stores; DELETE resets to the
    seed. Typography belongs to the BOOK, not a scan, so there is no src."""
    if build_id not in lib.load_json(BUILDS_PATH, {}):
        abort(404)
    path = _replica_style_path(build_id)
    if request.method == "GET":
        styles, custom = _replica_styles(build_id)
        return jsonify({"ok": True, "styles": styles, "custom": custom})
    if request.method == "DELETE":
        with _ocr_merge_lock:
            path.unlink(missing_ok=True)
        return jsonify({"ok": True})
    p = request.get_json(silent=True) or {}
    raw = p.get("styles")
    if not isinstance(raw, dict) or not raw or len(raw) > 40:
        return jsonify({"ok": False, "error": "bad styles"}), 400
    styles = _rw_sanitize_styles(raw)
    if not styles:
        return jsonify({"ok": False, "error": "no valid styles"}), 400
    # under the merge lock like every other sidecar write: the .lib import's
    # check-and-write shares it, so neither side clobbers the other
    with _ocr_merge_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        lib.save_json(path, {"version": 1, "styles": styles})
    return jsonify({"ok": True, "count": len(styles)})


@app.route("/api/builds/<build_id>/replica-instructions",
           methods=["GET", "PUT"])
@_live_item_write_endpoint
def api_build_replica_instructions(build_id: str):
    """The per-book "instructions for editors / AI" text (docs/lib-format.md
    §2.2) — free guidance that every .lib export embeds in its manifest and
    INSTRUCTIONS.md, e.g. "Latin plant names stay untranslated". Stored as
    ocr/lib-instructions.md beside the other book-level sidecars. GET returns
    {text}; PUT {text} stores it, and an empty text removes the sidecar."""
    if build_id not in lib.load_json(BUILDS_PATH, {}):
        abort(404)
    if request.method == "GET":
        return jsonify({"ok": True, "text": _lib_book_instructions(build_id)})
    p = request.get_json(silent=True) or {}
    text = str(p.get("text") or "")[:20000]
    dest = _entry_dir(build_id) / "ocr" / "lib-instructions.md"
    with _ocr_merge_lock:
        if text.strip():
            lib.save_text(dest, text)
        else:
            dest.unlink(missing_ok=True)
    return jsonify({"ok": True, "chars": len(text) if text.strip() else 0})


@app.route("/api/builds/<build_id>/replica-export")
def api_build_replica_export(build_id: str):
    """Seal one source's Replica working store into a .lib — a plain zip:

        book.json      format tag, bibliographic snapshot, role stylesheet,
                       layout templates, figure inventory, page list
        pages/N.json   one file per region page: doc, dims, review state,
                       items with diplomatic text + normalized layer
        assets/img/*   the extracted figure crops the regions reference

    The entry folder stays the working store — this is its portable,
    diffable snapshot for interchange and the coming re-typeset tooling.
    ?src= picks the scan (default primary)."""
    b = lib.load_json(BUILDS_PATH, {}).get(build_id)
    if b is None:
        abort(404)
    src = _valid_src_key(b, request.args.get("src"))
    if not src:
        return jsonify({"ok": False, "error": "unknown source"}), 400
    layout_path = _entry_dir(build_id) / "ocr" / "layout.json"
    img_dir = _entry_dir(build_id) / "ocr" / "images"
    # Capture one immutable export snapshot. The archive is assembled after
    # releasing the locks, but every component below belongs to this revision.
    with _ocr_merge_lock:
        meta = lib.load_json(layout_path, {})
        stored_pages = (meta.get("regions") or {}).get(src) or {}
        page_nums = sorted(int(k) for k in stored_pages
                           if str(k).isdigit() and
                           isinstance(stored_pages[k], dict))
        used_rids: set[str] = set()
        pages = {}
        for n in page_nums:
            rec = dict(stored_pages[str(n)])
            rec["items"] = replica_service.stable_export_items(
                rec.get("items") or [], f"{build_id}:{src}:{n}", used_rids)
            pages[str(n)] = rec
        per_book = _lib_book_instructions(build_id)
        snapshot_styles = _replica_styles(build_id)[0]
        snapshot_templates = (meta.get("templates") or {}).get(src) or {}
        snapshot_ext = _lib_manifest_ext(build_id)
        snapshot_book_id = _lib_book_id(build_id)
        figures = {}
        figure_assets = {}
        for name, info in (meta.get("images") or {}).items():
            if not isinstance(info, dict):
                continue
            if str(info.get("src_key") or "primary") != src:
                continue
            safe = re.sub(r"[^\w.\-]", "_", str(name))
            if not safe or safe != str(name):
                continue
            figure = {k: info.get(k) for k in
                      ("page", "x", "y", "w", "h")}
            if info.get("rework_of"):
                figure["rework_of"] = str(info["rework_of"])
            fext = libformat.sanitize_ext(info.get("ext"))
            if fext:
                figure["ext"] = fext
            f = img_dir / safe
            if f.is_file():
                figures[safe] = figure
                figure_assets[safe] = f.read_bytes()
        with _an_write_lock:
            translations = _lib_translation_members(build_id, page_nums)
    if not page_nums:
        return jsonify({"ok": False, "error":
                        "no region pages to export — seed or draw some in"
                        " the Replica tab first"}), 400
    import io
    import zipfile
    book = {
        "format_version": libformat.FORMAT_VERSION,
        "generator": f"library-tool/{_app_version()}",
        "book_id": snapshot_book_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": src,
        "meta": {k: b.get(k) for k in _LIB_META_FIELDS if b.get(k)},
        "capabilities": list(libformat.CAPABILITIES),
        "roles": libformat.ROLE_VOCAB,          # the vocabulary AS DATA
        "instructions": {"general_ref": "INSTRUCTIONS.md", "book": per_book},
        "stylesheet": snapshot_styles,
        "templates": snapshot_templates,
        "figures": figures,
        "pages": page_nums,
        "ext": snapshot_ext,
    }
    buf = io.BytesIO()
    try:
        # allow_nan=False: a hand-edited sidecar smuggling NaN/Infinity
        # through load_json must fail HERE, loudly — not export an archive
        # whose pages/N.json no strict parser will read
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("book.json", json.dumps(book, indent=1,
                                               ensure_ascii=False,
                                               allow_nan=False))
            # the self-description a lib/2 file ships: the LLM contract and a
            # machine-checkable schema, both generated from the live vocabulary
            z.writestr("INSTRUCTIONS.md",
                       libformat.render_instructions(book["meta"], per_book))
            z.writestr("schema.json", json.dumps(libformat.SCHEMA, indent=1))
            for n in page_nums:
                rec = pages.get(str(n))
                if not isinstance(rec, dict):
                    continue
                page_obj = {
                    "page": n,
                    "doc": rec.get("doc") or "",
                    "dims": rec.get("dims") or {},
                    "state": rec.get("state") or "",
                    # New records already carry stable RIDs. Legacy gaps were
                    # filled deterministically in this immutable snapshot,
                    # without turning a download GET into a store mutation.
                    "items": rec.get("items") or [],
                }
                pext = libformat.sanitize_ext(rec.get("ext"))
                if pext:
                    page_obj["ext"] = pext
                z.writestr(f"pages/{n}.json", json.dumps(
                    page_obj, indent=1, ensure_ascii=False, allow_nan=False))
            for lang, member in translations.items():
                z.writestr(f"translations/{lang}.json", json.dumps(
                    member, ensure_ascii=False, allow_nan=False))
            for name in figures:
                z.writestr(f"assets/img/{name}", figure_assets[name])
    except ValueError:
        return jsonify({"ok": False, "error":
                        "the layout sidecar contains non-finite numbers — "
                        "re-save the affected pages first"}), 400
    buf.seek(0)
    stem = re.sub(r"[^\w\-]+", "-",
                  str(b.get("published_slug") or b.get("title")
                      or build_id)).strip("-").lower()[:60] or build_id
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{stem}.lib")


# --- illustration rework: image generation over the figure crops -----------------

_IMG_GEN_PROMPT = (
    "Redraw this illustration from an old printed book as a clean modern "
    "edition illustration. Preserve the composition, subject, and period "
    "character; render as crisp line art suitable for print, free of paper "
    "texture, stains, and show-through.")


def _img_gen_cfg() -> dict:
    """BYO-key image generation settings (key: Settings > Credentials). Providers:
    "openai" (images/edits, default model gpt-image-1) or "gemini"
    (generateContent with an inline image, default gemini-2.5-flash-image)."""
    s = _client_settings()
    provider = str(s.get("imgGenProvider") or "openai").strip().lower()
    if provider not in ("openai", "gemini"):
        provider = "openai"
    model = str(s.get("imgGenModel") or "").strip() or (
        "gpt-image-1" if provider == "openai" else "gemini-2.5-flash-image")
    return {"provider": provider, "model": model}


def _img_gen_with_key(cfg: dict, image: bytes, mime: str, prompt: str,
                      timeout: float = 180.0) -> bytes:
    """One image in, one generated image out, or RuntimeError. NOTE: written
    to the providers' documented shapes but not yet exercised against live
    keys — same standing as the cloud OCR processor before its key existed."""
    import base64
    if cfg["provider"] == "gemini":
        payload = {"contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": mime,
                             "data": base64.b64encode(image).decode("ascii")}},
        ]}]}
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(cfg['model'])}:generateContent",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "x-goog-api-key": cfg["key"]})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        for cand in data.get("candidates") or []:
            for part in ((cand.get("content") or {}).get("parts") or []):
                blob = part.get("inline_data") or part.get("inlineData")
                if blob and blob.get("data"):
                    return base64.b64decode(blob["data"])
        raise RuntimeError("the model returned no image")
    # openai images/edits: multipart with the source image
    boundary = "whl-" + uuid.uuid4().hex
    ext = "jpg" if mime == "image/jpeg" else "png"
    parts = []
    for k, v in (("model", cfg["model"]), ("prompt", prompt), ("n", "1")):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f"name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8"))
    parts.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
         f"filename=\"figure.{ext}\"\r\nContent-Type: {mime}\r\n\r\n")
        .encode("utf-8") + image + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(
        "https://api.openai.com/v1/images/edits", data=b"".join(parts),
        headers={"Authorization": f"Bearer {cfg['key']}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    import base64 as _b64
    for item in data.get("data") or []:
        if item.get("b64_json"):
            return _b64.b64decode(item["b64_json"])
    raise RuntimeError("the model returned no image")


def _img_gen(cfg: dict, image: bytes, mime: str, prompt: str,
             timeout: float = 180.0) -> bytes:
    with _lease_secret("imgGenKey") as key:
        execution_cfg = {**cfg, "key": key}
        try:
            return _img_gen_with_key(
                execution_cfg, image, mime, prompt, timeout=timeout)
        finally:
            execution_cfg.pop("key", None)


@app.route("/api/builds/<build_id>/rework-figure", methods=["POST"])
def api_build_rework_figure(build_id: str):
    """Rework one extracted figure through the configured image model:
    {src?, figure: <name>, prompt?: extra art direction}. The generated
    image saves beside the original as rework-<name>.png with the same
    page/bbox and a rework_of pointer — the re-typeset preview prefers it,
    the original is never touched. Works on any figure region, including a
    hand-drawn box over an initial to commission an illuminated capital."""
    b = lib.load_json(BUILDS_PATH, {}).get(build_id)
    if b is None:
        abort(404)
    p = request.get_json(silent=True) or {}
    src = _valid_src_key(b, p.get("src"))
    if not src:
        return jsonify({"ok": False, "error": "unknown source"}), 400
    name = str(p.get("figure") or "")
    if not re.fullmatch(r"[\w.\-]{1,120}", name):
        return jsonify({"ok": False, "error": "bad figure name"}), 400
    meta_path = _entry_dir(build_id) / "ocr" / "layout.json"
    info = (lib.load_json(meta_path, {}).get("images") or {}).get(name)
    if not isinstance(info, dict) or \
            str(info.get("src_key") or "primary") != src:
        return jsonify({"ok": False, "error": "unknown figure"}), 400
    # rework the ORIGINAL, never a rework: chains would mint paid calls the
    # preview can't even show (its shadow map is one level deep)
    if info.get("rework_of") or name.startswith("rework-"):
        return jsonify({"ok": False, "error":
                        "that is already a rework — rework the original"}), 400
    f = _entry_dir(build_id) / "ocr" / "images" / name
    if not f.is_file():
        return jsonify({"ok": False, "error": "figure file missing"}), 400
    cfg = _img_gen_cfg()
    if not _secret_is_configured("imgGenKey"):
        return jsonify({"ok": False, "error":
                        "no image-generation key configured "
                        "(image model key: Settings > Credentials)"}), 400
    prompt = _IMG_GEN_PROMPT
    extra = str(p.get("prompt") or "").strip()[:2000]
    if extra:
        prompt += " " + extra
    raw = f.read_bytes()
    mime = "image/jpeg" if f.suffix.lower() in (".jpg", ".jpeg") \
        else "image/png"
    try:
        out = _img_gen(cfg, raw, mime, prompt)
    except Exception as exc:
        return jsonify({"ok": False, "error":
                        f"image generation failed: {exc}"}), 502
    # the FULL original name (extension included) keys the rework, so
    # p3-fig.jpeg and p3-fig.png can never collide onto one output
    out_name = f"rework-{name}.png"
    if len(out_name) > 120:
        return jsonify({"ok": False, "error": "figure name too long"}), 400
    # Never hold the workspace lease across the paid remote call.  Publication
    # is a separate, short transaction: deletion may have won while the model
    # ran, and another editor may have replaced either the crop or its layout
    # metadata.  Re-read both under the merge lock before writing anything.
    with _live_item_write_scope(build_id):
        with _ocr_merge_lock:
            meta = lib.load_json(meta_path, {})
            current_info = (meta.get("images") or {}).get(name)
            target_is_current = (
                isinstance(current_info, dict)
                and str(current_info.get("src_key") or "primary") == src
                and not current_info.get("rework_of")
                and not name.startswith("rework-")
                and current_info == info
            )
            try:
                image_is_current = f.is_file() and f.read_bytes() == raw
            except OSError:
                image_is_current = False
            if not target_is_current or not image_is_current:
                return jsonify({
                    "ok": False,
                    "error": (
                        "the source figure changed while the model was running "
                        "\u2014 review it and retry"
                    ),
                }), 409
            lib.save_bytes(f.parent / out_name, out)
            entry = {
                k: current_info.get(k) for k in ("page", "x", "y", "w", "h")
            }
            entry.update({"src_key": src, "rework_of": name})
            meta.setdefault("images", {})[out_name] = entry
            lib.save_json(meta_path, meta)
    return jsonify({"ok": True, "name": out_name, "bytes": len(out)})


# --- print / PDF export: the re-typeset preview, paginated ----------------------

# A4 printable area at 12mm margins; a page box scales to fit, ratio kept
_PRINT_W_MM = 186.0
_PRINT_H_MM = 273.0


@app.route("/api/builds/<build_id>/replica-print")
def api_build_replica_print(build_id: str):
    """The modernized facsimile as a print document: every region page of
    the source re-typeset at the book's styles, one sheet per page, sized
    for A4 — open it and print to PDF (Chromium's fuller paged-media
    machinery isn't needed: each sheet is one absolutely-positioned box
    with a page break after it, which prints faithfully everywhere).
    ?src= picks the scan; ?layer= flows the diplomatic text (default),
    "norm", or a page-aligned translation language."""
    b = lib.load_json(BUILDS_PATH, {}).get(build_id)
    if b is None:
        abort(404)
    src = _valid_src_key(b, request.args.get("src"))
    if not src:
        return jsonify({"ok": False, "error": "unknown source"}), 400
    layer = str(request.args.get("layer") or "").strip()[:12]
    meta = lib.load_json(_entry_dir(build_id) / "ocr" / "layout.json", {})
    pages = (meta.get("regions") or {}).get(src) or {}
    page_nums = sorted(int(k) for k in pages
                       if str(k).isdigit() and isinstance(pages[k], dict))
    if not page_nums:
        return jsonify({"ok": False, "error": "no region pages to print"}), 400
    styles, _custom = _replica_styles(build_id)
    pg_style = styles.get("page") or {}
    paper = pg_style.get("bg") or "#fdfcf8"
    ink = pg_style.get("color") or "#1c1a17"
    # reworked art shadows its original, exactly like the preview
    rework = {str(i.get("rework_of")): n
              for n, i in (meta.get("images") or {}).items()
              if isinstance(i, dict) and i.get("rework_of")}
    trans_pages = None
    if layer and layer != "norm":
        lang = _lang_code(layer)
        trans_pages = _an_pages(_read_entry_text(
            build_id, f"translations/{lang}.txt")) if lang else {}

    import html as _html

    def esc(s):
        return _html.escape(str(s or ""), quote=True)

    def css_color(v, fallback):
        return v if isinstance(v, str) and _RW_HEX_RE.match(v) else fallback

    sheets = []
    for n in page_nums:
        rec = pages[str(n)]
        items = sorted((i for i in rec.get("items") or []
                        if isinstance(i, dict)),
                       key=lambda i: i.get("order") or 0)
        dims = rec.get("dims") or {}
        try:
            ratio = float(dims.get("w") or 0) / float(dims.get("h") or 0)
        except (TypeError, ValueError, ZeroDivisionError):
            ratio = 0.0
        if not 0.1 < ratio < 10:
            ratio = 0.72
        sheet_w = min(_PRINT_W_MM, _PRINT_H_MM * ratio)
        sheet_h = sheet_w / ratio
        # the base type size: median content-fit of the body regions, so
        # size_em ratios play against the page's own scale (same rule as
        # the on-screen preview)
        fits = []
        for it in items:
            if it.get("role") != "body" or not str(it.get("text") or "").strip():
                continue
            lines = max(1, len([ln for ln in str(it["text"]).split("\n")
                                if ln.strip()]))
            fits.append((it.get("box") or {}).get("h", 0) * sheet_h
                        / lines * 0.78)
        fits.sort()
        base = fits[len(fits) // 2] if fits else sheet_h * 0.018
        texts = {}
        if trans_pages is not None:
            bodies = [it for it in items if it.get("role") == "body"]
            dist = TextLayerService.distribute(
                trans_pages.get(n) or "",
                [max(1, len(str(it.get("text") or ""))) for it in bodies])
            for it, t in zip(bodies, dist):
                texts[id(it)] = t
        cells = []
        for it in items:
            box = it.get("box") or {}
            try:
                x = float(box.get("x") or 0) * sheet_w
                y = float(box.get("y") or 0) * sheet_h
                w = float(box.get("w") or 0) * sheet_w
                h = float(box.get("h") or 0) * sheet_h
            except (TypeError, ValueError):
                continue
            role = str(it.get("role") or "body")
            st = styles.get(role) or styles.get("body") or {}
            pos = (f"left:{x:.2f}mm;top:{y:.2f}mm;"
                   f"width:{w:.2f}mm;height:{h:.2f}mm;")
            text = str(it.get("text") or "")
            fig = role == "figure" and re.search(
                r"!\[[^\]\n]*\]\(([\w.\- ]+)\)", text)
            if fig:
                name = rework.get(fig.group(1)) or fig.group(1)
                cells.append(
                    f'<div class="rg" style="{pos}">'
                    f'<img src="/api/builds/{urllib.parse.quote(build_id)}'
                    f'/ocr/images/{urllib.parse.quote(name)}" alt=""></div>')
                continue
            if layer == "norm":
                shown = str(it.get("norm") or "").strip() or text
            elif trans_pages is not None and id(it) in texts:
                shown = texts[id(it)]
            else:
                shown = text
            decl = [pos]
            # single-quoted in CSS: the style attribute itself is double-
            # quoted, and a double quote inside would terminate it
            fam = str(st.get("family") or "").replace('"', "").replace("'", "")
            decl.append(f"font-family:'{fam}',serif;" if fam
                        else "font-family:serif;")
            if role == "drop-capital":
                decl.append(f"font-size:{max(2.0, h * 0.9):.2f}mm;"
                            "line-height:1;text-align:center;")
            else:
                size = base * float(st.get("size_em") or 1)
                decl.append(f"font-size:{max(1.5, size):.2f}mm;")
                decl.append(f"line-height:{st.get('leading') or 1.25};")
            if st.get("style") == "italic":
                decl.append("font-style:italic;")
            if st.get("variant") == "small-caps":
                decl.append("font-variant:small-caps;")
            if st.get("align"):
                decl.append(f"text-align:{st['align']};")
            if st.get("color"):
                decl.append(f"color:{css_color(st['color'], ink)};")
            if st.get("bg"):
                decl.append(f"background:{css_color(st['bg'], 'none')};")
            cells.append(f'<div class="rg" style="{"".join(decl)}">'
                         f"{esc(shown)}</div>")
        sheets.append(
            f'<div class="sheet" style="width:{sheet_w:.1f}mm;'
            f'height:{sheet_h:.1f}mm;">{"".join(cells)}'
            f'<div class="folio">{n}</div></div>')

    title = esc(b.get("title") or build_id)
    doc = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{title} — replica</title><style>"
        "@page { size: A4; margin: 12mm; }"
        "html, body { margin: 0; padding: 0; }"
        f"body {{ background: #666; color: {css_color(ink, '#1c1a17')}; }}"
        ".sheet { position: relative; overflow: hidden; margin: 4mm auto;"
        f" background: {css_color(paper, '#fdfcf8')};"
        " page-break-after: always; break-after: page; }"
        ".rg { position: absolute; overflow: hidden;"
        " white-space: pre-wrap; }"
        ".rg img { width: 100%; height: 100%; object-fit: contain; }"
        ".folio { position: absolute; right: 2mm; bottom: 1mm;"
        " font: 2.4mm sans-serif; opacity: .35; }"
        "@media print { body { background: none; }"
        " .sheet { margin: 0 auto; } .folio { display: none; } }"
        f"</style></head><body>{''.join(sheets)}</body></html>")
    return Response(doc, mimetype="text/html")


# the .lib size caps live in libformat now (one place both the API and this
# route enforce); the aliases keep the call sites below unchanged
_LIB_MAX_BYTES = libformat.MAX_BYTES
_LIB_MAX_FIGURE = libformat.MAX_FIGURE
_LIB_MAX_PAGES = libformat.MAX_PAGES
_LIB_MAX_JSON = libformat.MAX_JSON            # per JSON member, decompressed
_LIB_MAX_INFLATED = libformat.MAX_INFLATED    # total page-JSON budget


def _lib_import_archive(build_id: str, src: str, raw: bytes,
                        overwrite: bool) -> tuple[dict, int]:
    """The .lib import core: unpack an archive's pages/templates/figures/
    stylesheet/translations into a build's working store, returning
    (receipt, http_status). Everything passes the same sanitizers as the
    live endpoints — a .lib is somebody else's file. Shared by the
    replica-import route and the desktop "open a .lib" flow."""
    import io
    import zipfile
    # Every JSON member is read at its DECLARED decompressed size, which
    # zipfile also truncates to — so rejecting large declarations is a real
    # cap, and a small .lib cannot deflate-bomb the sidecar into an OOM.
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        if z.getinfo("book.json").file_size > _LIB_MAX_JSON:
            return {"ok": False, "error": "book.json too large"}, 400
        book = json.loads(z.read("book.json"))
    except (zipfile.BadZipFile, KeyError, ValueError):
        return {"ok": False, "error": "not a .lib archive"}, 400
    # lib/1 (the bare "format": "lib/1" marker) and lib/2 (format_version
    # "2.x") both import; lib/1 upgrades on ingest (rids minted, defaults
    # filled). A higher MAJOR breaks — refuse it with a clear message rather
    # than silently dropping everything the newer format added.
    fmt = libformat.parse_format(book)
    if fmt is None:
        return {"ok": False, "error": "unsupported .lib format"}, 400
    if fmt[0] > libformat.SUPPORTED_MAJOR:
        return {"ok": False, "error":
                f"this .lib needs a newer Library Tool "
                f"(format {fmt[0]}.{fmt[1]})"}, 400
    incoming_book_id = str(book.get("book_id") or "")
    if fmt[0] >= 2 and not re.fullmatch(r"b-[0-9a-f]{32}", incoming_book_id):
        return {"ok": False, "error": "the .lib has no valid stable book_id"}, 400
    local_id_doc = lib.load_json(_lib_id_path(build_id), None)
    local_book_id = str(local_id_doc.get("book_id") or "") \
        if isinstance(local_id_doc, dict) else ""
    if (incoming_book_id and local_book_id and
            incoming_book_id != local_book_id):
        return {"ok": False, "error":
                "this .lib belongs to a different book",
                "conflict": "book_identity_mismatch"}, 409

    # Nothing is dropped silently: every coercion and skip below is named in
    # the receipt's warnings[] with its location and reason (§2.6).
    warnings: list = []

    def warn(loc, msg):
        warnings.append({"loc": loc, "msg": msg})

    # pages: every well-formed pages/N.json, sanitized like a live PUT
    incoming: dict[int, dict] = {}
    incoming_rids: dict[str, str] = {}
    budget = _LIB_MAX_INFLATED
    for name in z.namelist():
        m = re.fullmatch(r"pages/(\d{1,5})\.json", name)
        if not m:
            continue
        n = int(m.group(1))
        if not 1 <= n <= 99999:
            warn(name, "page skipped: page number out of range")
            continue
        if len(incoming) >= _LIB_MAX_PAGES:
            warn(name, f"page skipped: over the {_LIB_MAX_PAGES}-page cap")
            continue
        declared = z.getinfo(name).file_size
        if declared > _LIB_MAX_JSON or declared > budget:
            warn(name, "page skipped: JSON member exceeds the size cap")
            continue
        budget -= declared
        try:
            rec = json.loads(z.read(name))
        except (ValueError, KeyError):
            warn(name, "page skipped: not valid JSON")
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("items"), list):
            warn(name, "page skipped: no items array")
            continue
        raw_items = rec["items"]
        if len(raw_items) > _RW_MAX_ITEMS:
            warn(name, f"page had more than {_RW_MAX_ITEMS} regions; "
                       "the surplus was dropped")
        for index, item in enumerate(raw_items[:_RW_MAX_ITEMS]):
            rid = libformat.clean_rid(
                item.get("rid") if isinstance(item, dict) else "")
            if not rid:
                continue
            if rid in incoming_rids:
                return {"ok": False, "error":
                        f"duplicate region identity {rid!r}",
                        "warnings": warnings + [{"loc": f"{name}[{index}]",
                        "msg": "the same rid is already used at " +
                               incoming_rids[rid]}]}, 400
            incoming_rids[rid] = f"{name}[{index}]"
        items = _rw_sanitize_items(raw_items[:_RW_MAX_ITEMS],
                                   src_type="import", warn=warn, loc=name)
        if not items:
            warn(name, "page skipped: no usable regions")
            continue
        incoming[n] = {
            "doc": _ocr_name(str(rec.get("doc") or "compiled.txt")),
            "dims": _rw_sanitize_dims(rec.get("dims")) or {},
            "items": items,
        }
        pext = libformat.sanitize_ext(rec.get("ext"), f"{name}.ext", warn)
        if pext:
            incoming[n]["ext"] = pext
        st = rec.get("state")
        if st == "verified":
            incoming[n]["imported_state"] = "verified"
            warn(name, "verified state imported as advisory; local review is required")
        elif st:
            warn(name, f"state {st!r} dropped: only 'verified' is recognized")
    if not incoming:
        return {"ok": False, "error": "no usable pages",
                "warnings": warnings}, 400

    # book.json's sections are attacker-shaped: coerce every one before
    # touching disk, so a malformed archive fails clean instead of 500ing
    # halfway through a commit
    tpl_src = book.get("templates")
    tpl_in = {}
    if isinstance(tpl_src, dict):
        for name, t in tpl_src.items():
            name = str(name).strip()
            loc = f"templates/{name}"
            if not _RW_TPL_RE.match(name):
                warn("book.json/templates",
                     f"template {name!r} dropped: not a valid template name")
                continue
            if not isinstance(t, dict):
                warn(loc, "template dropped: not an object")
                continue
            items = _rw_sanitize_items(t.get("items") or [],
                                       src_type="template", warn=warn, loc=loc)
            if not items:
                warn(loc, "template dropped: no usable regions after sanitize")
                continue
            tpl_in[name] = {"from_page": 0, "doc": _ocr_name(
                str(t.get("doc") or "compiled.txt")),
                "dims": _rw_sanitize_dims(t.get("dims")) or {},
                "items": [{"role": i["role"], "order": i["order"],
                           "box": i["box"]} for i in items]}

    figures_src = book.get("figures") if isinstance(book.get("figures"),
                                                    dict) else {}
    raw_styles = book.get("stylesheet")
    if isinstance(raw_styles, dict) and len(raw_styles) > 40:
        warn("book.json/stylesheet", "stylesheet dropped: more than 40 roles")
    styles = _rw_sanitize_styles(raw_styles) \
        if isinstance(raw_styles, dict) and len(raw_styles) <= 40 else {}
    manifest_ext = libformat.sanitize_ext(book.get("ext"), "book.json.ext", warn)
    # translations/<bcp47>.json members route into the entry's translation
    # store (page-marked .txt), additive — never touching text or norm
    trans_in = _lib_read_translation_members(z, warn)

    meta_path = _entry_dir(build_id) / "ocr" / "layout.json"
    applied, skipped, protected, tpls_added = [], [], [], []
    sheet = "none"
    with _ocr_merge_lock:
        meta = lib.load_json(meta_path, {})
        pmap = meta.setdefault("regions", {}).setdefault(src, {})
        for n, rec in sorted(incoming.items()):
            existing = pmap.get(str(n))
            if replica_service.is_protected(existing):
                protected.append(n)
                warn(f"pages/{n}.json", "page skipped: the destination is "
                     "human-edited or verified")
                continue
            if not overwrite and existing is not None:
                skipped.append(n)
                warn(f"pages/{n}.json", "page skipped: the destination "
                     "already has this page (import with overwrite to replace)")
                continue
            pmap[str(n)] = rec
            applied.append(n)
        tmap = meta.setdefault("templates", {}).setdefault(src, {})
        for name, t in tpl_in.items():
            if not overwrite and name in tmap:
                warn(f"templates/{name}", "template skipped: the destination "
                     "already has this template (import with overwrite to "
                     "replace)")
                continue
            tmap[name] = t
            tpls_added.append(name)
        imap = meta.setdefault("images", {})
        figures_added = 0
        img_dir = _entry_dir(build_id) / "ocr" / "images"
        for name in z.namelist():
            # the shared hardened pattern rejects dot-only names ("."/".."):
            # a bare ".." member resolves to a directory and dest.write_bytes
            # on it would raise mid-commit, orphaning the figures already written
            m = libformat._ASSET_MEMBER.fullmatch(name)
            if not m:
                continue
            safe = m.group(1)
            dest = img_dir / safe
            entry = libformat.sanitize_figure(figures_src.get(safe), src,
                                              warn=warn, loc=f"assets/img/{safe}")
            if dest.exists() or safe in imap:
                # §2.6: a colliding figure is replaced only when the incoming
                # entry deliberately reworks it — rework_of must NAME the
                # colliding member ("the original member or itself", both ==
                # safe); an accidental collision always skips, now with a warning
                if overwrite and entry.get("rework_of") == safe:
                    pass
                elif overwrite:
                    warn(f"assets/img/{safe}", "figure skipped: name collides "
                         "and the entry carries no rework_of")
                    continue
                else:
                    warn(f"assets/img/{safe}",
                         "figure skipped: a figure by that name already exists")
                    continue
            info = z.getinfo(name)
            if info.file_size > _LIB_MAX_FIGURE:
                warn(f"assets/img/{safe}",
                     "figure skipped: image exceeds the size cap")
                continue
            img_dir.mkdir(parents=True, exist_ok=True)
            lib.save_bytes(dest, z.read(name))
            imap[safe] = entry
            figures_added += 1
        lib.save_json(meta_path, meta)
        # the stylesheet check-and-write shares the lock: a style-board Save
        # landing between "no custom sheet" and this save must not be
        # silently clobbered
        if styles:
            if overwrite or not _replica_styles(build_id)[1]:
                lib.save_json(_replica_style_path(build_id),
                              {"version": 1, "styles": styles})
                sheet = "imported"
            else:
                sheet = "kept"
        # the manifest ext passthrough persists so a later export re-emits it
        if manifest_ext:
            if overwrite or not _lib_manifest_ext(build_id):
                lib.save_json(_lib_manifest_ext_path(build_id), manifest_ext)
            else:
                warn("book.json/ext", "ext kept: destination already has one "
                     "(import with overwrite to replace)")
        if incoming_book_id and not local_book_id:
            _lib_store_book_id(build_id, incoming_book_id)

    # translations write under their own lock (the analyze store), so they
    # land after the region commit rather than nesting the two locks
    applied_set = set(applied)
    trans_apply = {lang: {pn: text for pn, text in pmap.items()
                          if pn in applied_set}
                   for lang, pmap in trans_in.items()}
    trans_apply = {lang: pmap for lang, pmap in trans_apply.items() if pmap}
    trans_added = _lib_apply_translations(build_id, trans_apply, overwrite)

    return {"ok": True, "format_version": "%d.%d" % fmt,
            "pages_applied": applied, "pages_skipped": skipped,
            "pages_protected": protected,
            "templates_added": tpls_added,
            "figures_added": figures_added, "stylesheet": sheet,
            "translations_added": trans_added, "warnings": warnings}, 200


@app.route("/api/builds/<build_id>/replica-import", methods=["POST"])
def api_build_replica_import(build_id: str):
    """The other half of .lib interchange: unpack an exported archive into
    this build's working store. Multipart field "lib"; ?src= picks the scan
    the pages land under, ?overwrite=1 lets imported pages/templates replace
    existing ones (default: skip, like template apply). Planning and durable
    publication live behind the headless engine interchange boundary."""
    b = lib.load_json(BUILDS_PATH, {}).get(build_id)
    if b is None:
        abort(404)
    src = _valid_src_key(b, request.args.get("src"))
    if not src:
        return jsonify({"ok": False, "error": "unknown source"}), 400
    overwrite = str(request.args.get("overwrite") or "") in ("1", "true")
    f = request.files.get("lib")
    if f is None:
        return jsonify({"ok": False, "error": "no file"}), 400
    if request.content_length and request.content_length > _LIB_MAX_BYTES:
        return jsonify({"ok": False, "error": "file too large"}), 400
    raw = f.read(_LIB_MAX_BYTES + 1)
    if len(raw) > _LIB_MAX_BYTES:
        return jsonify({"ok": False, "error": "file too large"}), 400
    operation_id = str(request.headers.get("Idempotency-Key") or "").strip()
    if not operation_id:
        operation_id = uuid.uuid4().hex
    try:
        receipt = _interchange_engine().import_lib(ImportLibCommand(
            item_id=build_id,
            source_id=src,
            archive=raw,
            overwrite=overwrite,
            operation_id=operation_id,
        ))
    except EngineError as exc:
        return _engine_error_response(exc)
    return jsonify({
        "ok": True,
        "format_version": receipt.format_version,
        "pages_applied": list(receipt.pages_applied),
        "pages_skipped": list(receipt.pages_skipped),
        "pages_protected": list(receipt.pages_protected),
        "templates_added": list(receipt.templates_added),
        "figures_added": len(receipt.figures_added),
        "stylesheet": receipt.stylesheet_disposition,
        "translations_added": list(receipt.translations_added),
        "warnings": [
            {"loc": warning.location, "msg": warning.message}
            for warning in receipt.warnings
        ],
    })


@app.route(
    "/api/v1/items/<item_id>/replica/lib-imports", methods=["POST"]
)
def api_v1_replica_lib_import(item_id: str):
    """Import a Replica package through the stable engine transport.

    The operation key is mandatory on the versioned resource because a caller
    may safely retry after losing the response.  The compatibility route above
    still mints a key for older clients, but new workbenches receive the full
    durable receipt rather than its historical UI projection.
    """
    operation_id = str(request.headers.get("Idempotency-Key") or "").strip()
    if not operation_id:
        return _engine_error_response(EnginePreconditionRequiredError(
            "an idempotency key is required",
            code="idempotency_key_required",
            details={"header": "Idempotency-Key", "item_id": item_id},
        ))

    source_id = str(request.args.get("source_id") or "").strip()
    raw_overwrite = request.args.get("overwrite")
    overwrite_value = str(raw_overwrite or "").strip().lower()
    if overwrite_value in ("", "0", "false"):
        overwrite = False
    elif overwrite_value in ("1", "true"):
        overwrite = True
    else:
        return _engine_error_response(EngineValidationError(
            "overwrite must be a boolean query value",
            code="invalid_overwrite",
            details={"overwrite": str(raw_overwrite)},
        ))

    upload = request.files.get("lib")
    if upload is None:
        return _engine_error_response(EngineValidationError(
            "a Replica package is required",
            code="lib_archive_required",
            details={"field": "lib"},
        ))
    archive = upload.read(_LIB_MAX_BYTES + 1)
    if len(archive) > _LIB_MAX_BYTES:
        return _engine_error_response(EngineValidationError(
            "the Replica package is too large",
            code="lib_archive_too_large",
            details={"maximum_bytes": _LIB_MAX_BYTES},
        ))

    try:
        receipt = _interchange_engine().import_lib(ImportLibCommand(
            item_id=item_id,
            source_id=source_id,
            archive=archive,
            overwrite=overwrite,
            operation_id=operation_id,
        ))
    except EngineError as exc:
        return _engine_error_response(exc)
    return jsonify({
        "ok": True,
        "schema": "librarytool.lib-import-receipt/1",
        "receipt": receipt.as_dict(),
    })


@app.route("/api/lib/open", methods=["POST"])
def api_lib_open():
    """Adapt the desktop shell's trusted local-path flow to ``open_lib``.

    Filesystem access belongs to this loopback transport. Allocation, metadata
    projection, import, durable replay, and rollback belong to one engine unit
    of work, so no catalogue shell or partial entry can escape a failure.
    """
    p = request.get_json(silent=True) or {}
    raw_path = str(p.get("path") or "")
    fp = Path(raw_path) if raw_path else None
    if fp is None or not fp.is_absolute():
        return jsonify({"ok": False, "error": "path must be absolute"}), 400
    if fp.suffix.lower() not in (".lib", ".zip") or not fp.is_file():
        return jsonify({"ok": False, "error": "not a .lib file"}), 400
    try:
        if fp.stat().st_size > libformat.MAX_BYTES:
            return jsonify({"ok": False, "error": "file too large"}), 400
        raw = fp.read_bytes()
    except OSError as exc:
        return jsonify({"ok": False,
                        "error": f"could not read the file: {exc}"}), 400
    operation_id = str(
        request.headers.get("Idempotency-Key") or uuid.uuid4().hex
    ).strip()
    try:
        result = _lib_open_engine().open_lib(OpenLibCommand(
            archive=raw,
            operation_id=operation_id,
            source_path=str(fp),
        ))
    except EngineError as exc:
        return _engine_error_response(exc)

    receipt = result.import_receipt
    projected = {
        "ok": True,
        "format_version": receipt.format_version,
        "pages_applied": list(receipt.pages_applied),
        "pages_skipped": list(receipt.pages_skipped),
        "pages_protected": list(receipt.pages_protected),
        "templates_added": list(receipt.templates_added),
        "figures_added": len(receipt.figures_added),
        "stylesheet": receipt.stylesheet_disposition,
        "translations_added": list(receipt.translations_added),
        "warnings": [
            {"loc": warning.location, "msg": warning.message}
            for warning in receipt.warnings
        ],
    }
    if not result.replayed:
        activity("created", "draft entry", detail=result.item.title)
    return jsonify({
        "ok": True,
        "build_id": result.item_id,
        "receipt": projected,
    })


@app.route("/api/v1/lib-opens", methods=["POST"])
def api_v1_lib_open():
    """Create an item from an uploaded Replica package with safe replay."""

    operation_id = str(request.headers.get("Idempotency-Key") or "").strip()
    if not operation_id:
        return _engine_error_response(EnginePreconditionRequiredError(
            "an idempotency key is required",
            code="idempotency_key_required",
            details={"header": "Idempotency-Key"},
        ))
    upload = request.files.get("lib")
    if upload is None:
        return _engine_error_response(EngineValidationError(
            "a Replica package is required",
            code="lib_archive_required",
            details={"field": "lib"},
        ))
    archive = upload.read(_LIB_MAX_BYTES + 1)
    if len(archive) > _LIB_MAX_BYTES:
        return _engine_error_response(EngineValidationError(
            "the Replica package is too large",
            code="lib_archive_too_large",
            details={"maximum_bytes": _LIB_MAX_BYTES},
        ))
    try:
        result = _lib_open_engine().open_lib(OpenLibCommand(
            archive=archive,
            operation_id=operation_id,
        ))
    except EngineError as exc:
        return _engine_error_response(exc)
    response = jsonify({
        "ok": True,
        "schema": "librarytool.open-lib-receipt/1",
        **result.as_dict(),
    })
    response.headers["X-Record-Revision"] = result.item.revision
    return response, 200 if result.replayed else 201


@app.route("/api/lib/validate", methods=["POST"])
def api_lib_validate():
    """Lint a .lib with no side effects — the same sanitize/lint pass the
    import runs, but nothing is written. Multipart field "lib"; not tied to any
    build. External tools and CI check a file here before shipping it. Returns
    {ok, format_version, pages, warnings[], errors[]}; ok is true when there
    are no errors (warnings are advisory)."""
    f = request.files.get("lib")
    if f is None:
        return jsonify({"ok": False, "error": "no file"}), 400
    if request.content_length and request.content_length > libformat.MAX_BYTES:
        return jsonify({"ok": False, "error": "file too large"}), 400
    raw = f.read(libformat.MAX_BYTES + 1)
    if len(raw) > libformat.MAX_BYTES:
        return jsonify({"ok": False, "error": "file too large"}), 400
    try:
        doc = libformat.read_lib(raw)
    except libformat.LibError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    issues = libformat.validate(doc)
    errors = [i.as_dict() for i in issues if i.level == "error"]
    warnings = [i.as_dict() for i in issues if i.level == "warning"]
    return jsonify({"ok": not errors, "format_version": doc.format_version,
                    "pages": len(doc.pages), "warnings": warnings,
                    "errors": errors})


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


def _pdf_doc_cache_key(path: Path | str) -> str:
    """Case-normalized absolute path used when evicting cached PDF handles."""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _evict_pdf_doc(path: Path | str) -> None:
    """Close every cached PyMuPDF handle for ``path`` before it is replaced.

    Windows refuses to unlink a PDF while MuPDF still has it open. Interactive
    Smart Scan previews use this cache, so their disposable full-PDF source
    must be evicted before cleanup rather than relying on Unix unlink semantics.
    """
    target = _pdf_doc_cache_key(path)
    with _doc_lock:
        for key in list(_doc_cache):
            if _pdf_doc_cache_key(key[0]) != target:
                continue
            doc = _doc_cache.pop(key)
            try:
                doc.close()
            except Exception:
                pass


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


_pdf_paper_cache: dict[tuple[str, float, int], dict] = {}


def _paper_sample_pages(page_count: int, sample_count: int) -> list[int]:
    """Zero-based, evenly-spaced pages for paper sampling.

    Covers and endpapers are poor representatives of the book's paper, so omit
    the first and last page when the document is long enough to do so without
    crowding the requested sample count.
    """
    if page_count <= 0:
        return []
    sample_count = max(1, min(sample_count, page_count))
    if page_count <= sample_count:
        return list(range(page_count))
    if sample_count == 1:
        return [page_count // 2]
    omit_ends = page_count >= sample_count + 2
    lo, hi = (1, page_count - 2) if omit_ends else (0, page_count - 1)
    return [round(lo + i * (hi - lo) / (sample_count - 1))
            for i in range(sample_count)]


def _pixmap_margin_rgb(pix) -> tuple[int, int, int] | None:
    """Median RGB in a clean ring just inside a rasterized page's margins.

    The outermost few percent are skipped because scanned books often carry a
    black scanner border there. Channel medians ignore the small amount of ink,
    foxing, page numbers, and marginalia that can remain in the ring.
    """
    from statistics import median

    w, h, n = int(pix.width), int(pix.height), int(pix.n)
    if w < 8 or h < 8 or n < 3:
        return None
    inset_x, inset_y = max(1, round(w * 0.035)), max(1, round(h * 0.035))
    band_x, band_y = max(inset_x + 1, round(w * 0.14)), \
        max(inset_y + 1, round(h * 0.14))
    step = max(1, min(w, h) // 100)
    raw = pix.samples
    stride = int(pix.stride)
    channels = ([], [], [])
    for y in range(inset_y, h - inset_y, step):
        in_y_band = y < band_y or y >= h - band_y
        row = y * stride
        for x in range(inset_x, w - inset_x, step):
            if not in_y_band and band_x <= x < w - band_x:
                continue
            off = row + x * n
            r, g, b = raw[off], raw[off + 1], raw[off + 2]
            if max(r, g, b) < 64:     # scanner border or ink, not paper
                continue
            channels[0].append(r)
            channels[1].append(g)
            channels[2].append(b)
    if not channels[0]:
        return None
    return tuple(round(median(values)) for values in channels)


def _lighten_pdf_paper_color(color: str, amount: float) -> str:
    """Mix a #rrggbb paper sample toward white by ``amount`` percent."""
    amount = max(0.0, min(100.0, float(amount))) / 100.0
    vals = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    mixed = [round(v + (255 - v) * amount) for v in vals]
    return "#" + "".join(f"{v:02x}" for v in mixed)


def _sample_pdf_paper_color(path: Path, sample_count: int) -> dict:
    """Return a cached representative margin color for a local PDF."""
    import fitz
    from statistics import median

    mtime = path.stat().st_mtime
    key = (str(path), mtime, sample_count)
    hit = _pdf_paper_cache.get(key)
    if hit is not None:
        return hit
    colors: list[tuple[int, int, int]] = []
    sampled: list[int] = []
    with _pdf_doc(path) as doc:
        page_indexes = _paper_sample_pages(doc.page_count, sample_count)
        for index in page_indexes:
            pg = doc[index]
            zoom = 160 / max(1.0, float(pg.rect.width))
            pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                                colorspace=fitz.csRGB, alpha=False)
            color = _pixmap_margin_rgb(pix)
            if color is not None:
                colors.append(color)
                sampled.append(index + 1)
    if not colors:
        raise ValueError("No paper-colored margin pixels found")
    rgb = tuple(round(median([c[i] for c in colors])) for i in range(3))
    out = {"base_color": "#" + "".join(f"{v:02x}" for v in rgb),
           "sampled_pages": sampled}
    if len(_pdf_paper_cache) > 64:
        _pdf_paper_cache.clear()
    _pdf_paper_cache[key] = out
    return out


@app.route("/api/pdf/paper-color")
def api_pdf_paper_color():
    """Representative paper color sampled from several PDF page margins."""
    p = _pageimg_pdf(request.args.get("path"))
    try:
        samples = max(1, min(12, int(request.args.get("samples") or 5)))
    except ValueError:
        samples = 5
    try:
        lighten = max(0.0, min(100.0,
                               float(request.args.get("lighten") or 0)))
    except ValueError:
        lighten = 0.0
    if importlib.util.find_spec("fitz") is None:
        return jsonify({"ok": False, "error": "PyMuPDF is not installed"}), 501
    try:
        sampled = _sample_pdf_paper_color(p, samples)
        return jsonify({"ok": True, **sampled,
                        "color": _lighten_pdf_paper_color(
                            sampled["base_color"], lighten),
                        "lighten": lighten})
    except Exception as exc:
        return jsonify({"ok": False,
                        "error": f"{type(exc).__name__}: {exc}"}), 500


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


def _attached_src_key_for_path(build: dict, pdf: Path) -> str:
    """Return the exact attached source for ``pdf``, or ``""`` if unmatched.

    Destructive page operations must use this strict form: treating an
    arbitrary local PDF as the primary source would delete that unrelated file
    while renumbering this build's OCR and remarks.
    """
    try:
        pdfr = pdf.resolve()
    except OSError:
        return ""
    primary = _resolve_local(str(build.get("pdf_file") or ""))
    try:
        if primary is not None and primary.resolve() == pdfr:
            return "primary"
    except OSError:
        pass
    for s in (build.get("pdf_sources") or []):
        if not isinstance(s, dict):
            continue
        sp = _resolve_local(str(s.get("path") or ""))
        try:
            if sp is not None and sp.resolve() == pdfr:
                return str(s.get("id") or "")
        except OSError:
            continue
    return ""


def _src_key_for_path(build: dict, pdf: Path) -> str:
    """Which source a resolved PDF path is for this build.

    Legacy read-only callers treat an unmatched path as primary. Destructive
    callers use ``_attached_src_key_for_path`` and reject an unmatched path.
    """
    attached = _attached_src_key_for_path(build, pdf)
    if attached:
        return attached
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
_JOB_ACTIVE = ACTIVE_JOB_STATES
_JOB_FIELDS = PUBLIC_JOB_FIELDS
class _JobCancelled(Exception):
    """Raised inside a worker at a stage boundary after a cancel request."""


def _job_state_of(status) -> str:
    return _job_manager.state_of(status)


def _job_public(job: dict) -> dict:
    return _job_manager.public(job)


class _ItemJobStartRejected(Exception):
    """The catalogue item disappeared before its worker was registered."""


def _job_track(job: dict, kind: str, label: str = "") -> threading.Event:
    """Enter a per-kind job dict into the unified registry (shared dict) and
    return its cancellation event. This is the low-level compatibility seam;
    production item-scoped starts must use ``_job_track_item_guarded`` so item
    deletion and worker registration share one outer gate. Insertion prunes
    the oldest finished entries beyond _JOBS_KEEP and persists the snapshot."""
    return _job_manager.track(job, kind, label=label)


def _job_track_item_guarded(
        job: dict, kind: str, item_id: str,
        label: str = "") -> threading.Event:
    """Register one item worker atomically against lifecycle deletion.

    The order is deliberately page/lifecycle gate -> catalogue -> JobManager.
    Lifecycle deletion takes the same outer gate before reserving the item in
    the JobManager, so either the worker becomes visible to its active-job
    guard or deletion wins and this late start is refused.  The catalogue lock
    stays held through registration as a compatibility backstop for the old
    catalogue-only delete route, which does not yet take the outer gate.

    Callers may already hold ``_page_structure_lock`` (it is reentrant), but
    must not hold the non-reentrant ``_builds_lock``.
    """
    item_id = str(item_id or "").strip()
    if not item_id:
        raise _ItemJobStartRejected("an item-scoped job needs an item id")
    with _page_structure_lock:
        with _builds_lock:
            builds = lib.load_json(BUILDS_PATH, {})
            item = builds.get(item_id) if isinstance(builds, dict) else None
            if not isinstance(item, dict):
                raise _ItemJobStartRejected(
                    "the item disappeared before the job could start"
                )
            job["build_id"] = item_id
            raw_subject = job.get("subject")
            subject = dict(raw_subject) if isinstance(raw_subject, Mapping) else {}
            subject["item_id"] = item_id
            job["subject"] = subject
            resolved_label = (
                str(label or "").strip()
                or str(item.get("title") or "").strip()
                or item_id
            )
            # Keep both locks until JobManager.track has made the active job
            # observable. Releasing either one first recreates a delete/start
            # time-of-check/time-of-use window.
            return _job_track(job, kind, label=resolved_label)


def _job_transition_locked(job: dict, status: str, **fields) -> None:
    """The transition body for callers that already hold ``_jobs_lock``."""
    _job_manager.transition_locked(job, status, **fields)


def _job_transition(job: dict, status: str, **fields) -> None:
    """Move a job to a lifecycle status (legacy string), stamp the canonical
    state, and persist. Safe on untracked dicts (tests build jobs directly)."""
    _job_manager.transition(job, status, **fields)


def _job_checkpoint(job: dict, force: bool = False) -> None:
    """Persist live progress at page/chunk boundaries, throttled to 1 Hz."""
    _job_manager.checkpoint(job, force=force)


def _job_request_cancel(job_id: str, fallback: dict | None = None) -> dict | None:
    """Atomically request cancellation and return a stable job snapshot.

    The active-state check and the ``cancelling`` transition must share the
    registry lock with worker terminal transitions.  Otherwise a worker can
    finish after the check but before the transition, and the request handler
    overwrites ``done`` with a permanently-active ``cancelling`` state.
    ``fallback`` preserves the legacy OCR endpoint's unit-test/untracked-job
    behavior; production OCR jobs are always in the unified registry.
    """
    return _job_manager.request_cancel(job_id, fallback=fallback)


def _job_cancelled(job: dict) -> bool:
    return _job_manager.is_cancelled(job)


def _job_interrupt_note(kind: str) -> str:
    return _job_manager.interruption_note(kind)


def _jobs_load() -> None:
    """Rehydrate the persisted registry on startup: whatever was still active
    when the process died becomes `interrupted`, distinguishing resumable
    output (progressively-saved OCR/translation pages) from abandoned work.
    Live entries are never clobbered."""
    _job_manager.rehydrate()


def _jobs_engine() -> JobManager:
    jobs = _library_engine().jobs
    if jobs is None:
        raise RuntimeError("the background-job engine module is unavailable")
    return jobs


def _csv_query_values(name: str) -> tuple[str, ...]:
    values = []
    for raw in request.args.getlist(name):
        values.extend(part.strip() for part in str(raw).split(",") if part.strip())
    return tuple(values)


@app.route("/api/v1/jobs")
def api_v1_jobs():
    """Versioned, framework-neutral query over canonical engine jobs."""
    states = _csv_query_values("state")
    if states == ("active",):
        states = _JOB_ACTIVE
    views = _jobs_engine().list_views(
        states=states,
        kinds=_csv_query_values("kind"),
        item_id=str(request.args.get("item_id") or ""),
    )
    rows = [view.as_dict() for view in views]
    return jsonify({"ok": True, "jobs": rows,
                    "active": sum(row["state"] in _JOB_ACTIVE for row in rows)})


@app.route("/api/v1/jobs/<job_id>")
def api_v1_job(job_id: str):
    view = _jobs_engine().view(job_id)
    if view is None:
        abort(404)
    return jsonify({"ok": True, "job": view.as_dict()})


@app.route("/api/v1/jobs/<job_id>/cancel", methods=["POST"])
def api_v1_job_cancel(job_id: str):
    job = _jobs_engine().request_cancel(job_id)
    if job is None:
        abort(404)
    return jsonify({"ok": True, "job": _jobs_engine().view_of(job).as_dict()})


@app.route("/api/v1/job-events")
def api_v1_job_events():
    """Cursor polling over normalized lifecycle changes.

    The event log is intentionally transport-neutral and bounded. A future SSE
    or WebSocket adapter can stream the same values without changing workers.
    """
    try:
        after = max(0, int(request.args.get("after") or 0))
        limit = max(1, min(500, int(request.args.get("limit") or 200)))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid job event cursor"}), 400
    manager = _jobs_engine()
    events = manager.events_after(after, limit=limit)
    cursor = events[-1].sequence if events else manager.event_sequence
    return jsonify({"ok": True,
                    "events": [event.as_dict() for event in events],
                    "cursor": cursor})


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
# Serializes the page-scoped deduplication check with registration of a new
# Replica detection job.  General OCR batches remain intentionally independent;
# this lock only prevents two clients from paying for the same region proposal
# on the same page at the same time.
_replica_detection_start_lock = threading.Lock()
# serializes every compiled-file merge: concurrent jobs (one POST per digit
# shortcut) must not lose each other's pages in the read-modify-write
_ocr_merge_lock = threading.RLock()

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
        raise RuntimeError("Anthropic API key not configured (Settings > Credentials)")
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
        raise RuntimeError("AWS credentials not configured (Settings > Credentials)")
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
    """Mistral returns markdown, the figures it cut out of the page, and
    (OCR-4, include_blocks) typed text blocks in reading order. The result
    carries: `text` — the body flow composed from role-classified regions,
    with page furniture (marginalia, running heads, catchwords…) lifted out
    so compiled text, translations, and volume_pages stay clean; `images` —
    decoded figures with 0..1 boxes for _ocr_save_page_images; `regions` —
    the typed-region sidecar records; `dims` — the raster size/dpi the API
    reported (NOT the physical page — it describes our own rasterization).
    When blocks are missing or carry under 70% of the markdown's characters
    (a segmentation failure), the text falls back to the full markdown so
    nothing is silently lost."""
    key = (cfg.get("mistral_key") or "").strip()
    if not key:
        raise RuntimeError("Mistral API key not configured (Settings > Credentials)")
    import base64
    pages = capture.mistral_ocr_pages(png, key, want_images=True,
                                      want_blocks=True)
    markdown = "\n\n".join(p.get("markdown", "") for p in pages).strip()
    regions: list[dict] = []
    dims = None
    images = []
    for pg in pages:
        dim = pg.get("dimensions") or {}
        pw, ph = float(dim.get("width") or 0), float(dim.get("height") or 0)
        if dims is None and (pw > 0 or ph > 0):
            dims = {"w": int(pw), "h": int(ph),
                    "dpi": int(dim.get("dpi") or 0)}
        regions.extend(layout_roles.regions_from_blocks(pg.get("blocks"), dim))
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
    if regions and layout_roles.coverage(regions, markdown) >= 0.7:
        text = layout_roles.compose_text(regions)
    else:
        text = markdown
    return {"text": text, "images": images, "regions": regions, "dims": dims}


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
        lib.save_text(f, "\n\n".join(parts), errors="replace")


_OCR_PROPOSAL_IMAGE_PREFIX = "proposal-"


def _ocr_figure_source_token(src_key: str) -> str:
    source = str(src_key or "primary")
    safe = re.sub(r"[^\w.-]", "_", source).strip("._-") or "source"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:10]
    return f"{safe[:32]}-{digest}"


def _ocr_figure_leaf(raw: object) -> str:
    safe = re.sub(r"[^\w.\-]", "_", str(raw or "")) or "img"
    if "." not in safe:
        safe += ".jpeg"
    suffix = Path(safe).suffix[:16]
    stem = safe[:-len(suffix)] if suffix else safe
    return f"{stem[:96]}{suffix}"


def _ocr_proposal_identity(*, src_key: str, page: int, provider: str,
                           base_revision: str, doc: str, text: str,
                           dims: Mapping | None, regions: list,
                           images: list[dict]) -> str:
    image_rows = []
    for image in images or []:
        if not isinstance(image, Mapping):
            continue
        raw = image.get("data")
        payload = bytes(raw) if isinstance(raw, (bytes, bytearray)) else b""
        image_rows.append({
            "id": str(image.get("id") or ""),
            "bbox": image.get("bbox") if isinstance(
                image.get("bbox"), Mapping) else {},
            "sha256": hashlib.sha256(payload).hexdigest(),
        })
    command = {
        "source_id": str(src_key or "primary"),
        "page": int(page),
        "provider": str(provider or "unknown"),
        "base_revision": str(base_revision or ""),
        "doc": str(doc or ""),
        "text": str(text or ""),
        "dims": dict(dims) if isinstance(dims, Mapping) else {},
        "regions": regions or [],
        "images": image_rows,
    }
    canonical = json.dumps(
        command, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=str,
    ).encode("utf-8")
    return "rdp-" + hashlib.sha256(canonical).hexdigest()[:32]


def _ocr_write_page_images(build_id: str, page: int, images: list[dict],
                           text: str, src_key: str,
                           regions: list | None = None,
                           *, proposal_id: str = "") -> tuple[
                               str, dict[str, dict], set[str]]:
    """Write immutable crops and return their unpublished manifest.

    The caller owns ``_ocr_merge_lock`` and decides whether the returned
    manifest belongs in canonical ``images`` or in a protected-page proposal.
    Newly-created paths are returned so a failed manifest commit can clean up
    without deleting an asset that belonged to an earlier identical retry.
    """
    if not images:
        return text, {}, set()
    directory = _entry_dir(build_id) / "ocr" / "images"
    directory.mkdir(parents=True, exist_ok=True)
    source = str(src_key or "primary")
    source_token = _ocr_figure_source_token(source)
    created: set[str] = set()
    manifest: dict[str, dict] = {}
    used: set[str] = set()
    for index, image in enumerate(images):
        if not isinstance(image, Mapping):
            continue
        raw = image.get("data")
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            continue
        leaf = _ocr_figure_leaf(image.get("id"))
        if proposal_id:
            name = (f"{_OCR_PROPOSAL_IMAGE_PREFIX}{source_token}-p{int(page)}-"
                    f"{proposal_id[4:]}-{leaf}")
        else:
            source_prefix = "" if source == "primary" else f"s-{source_token}-"
            name = f"{source_prefix}p{int(page)}-{leaf}"
        if name in used:
            suffix = Path(name).suffix
            stem = name[:-len(suffix)] if suffix else name
            name = f"{stem}-{index + 1}{suffix}"
        used.add(name)
        path = directory / name
        existed = path.is_file()
        lib.save_bytes(path, bytes(raw))
        if not existed:
            created.add(name)
        info = dict(image.get("bbox") or {}) if isinstance(
            image.get("bbox"), Mapping) else {}
        info.update({
            "page": int(page),
            "src_key": source,
            "sha256": hashlib.sha256(bytes(raw)).hexdigest(),
        })
        if proposal_id:
            info["proposal_id"] = proposal_id
        manifest[name] = info
        image_id = str(image.get("id") or "")
        if image_id:
            pattern = re.compile(
                r"(!\[[^\]]*\]\()" + re.escape(image_id) + r"(\))")
            text = pattern.sub(r"\g<1>" + name + r"\g<2>", text)
            for region in regions or []:
                if isinstance(region, dict) and region.get("text"):
                    region["text"] = pattern.sub(
                        r"\g<1>" + name + r"\g<2>", str(region["text"]))
    return text, manifest, created


def _ocr_staged_figure_names(proposal: Mapping | None) -> set[str]:
    figures = proposal.get("staged_figures") if isinstance(
        proposal, Mapping) else None
    if not isinstance(figures, Mapping):
        return set()
    return {
        str(name) for name in figures
        if str(name).startswith(_OCR_PROPOSAL_IMAGE_PREFIX)
        and re.sub(r"[^\w.\-]", "_", str(name)) == str(name)
    }


def _ocr_remove_staged_figures(build_id: str, names: set[str]) -> None:
    directory = _entry_dir(build_id) / "ocr" / "images"
    for name in names:
        if not name.startswith(_OCR_PROPOSAL_IMAGE_PREFIX):
            continue
        try:
            (directory / name).unlink(missing_ok=True)
        except OSError:
            log.warning("Could not remove staged Replica figure: book=%s name=%s",
                        build_id, name)


def _ocr_cleanup_staged_figure_orphans(build_id: str) -> None:
    """Remove proposal crops that no manifest can make visible after restart."""
    directory = _entry_dir(build_id) / "ocr" / "images"
    if not directory.is_dir():
        return
    with _ocr_merge_lock:
        meta = lib.load_json(_entry_dir(build_id) / "ocr" / "layout.json", {})
        referenced = {
            str(name) for name in (meta.get("images") or {})
            if str(name).startswith(_OCR_PROPOSAL_IMAGE_PREFIX)
        }
        for pages in (meta.get("region_proposals") or {}).values():
            if not isinstance(pages, Mapping):
                continue
            for proposal in pages.values():
                referenced.update(_ocr_staged_figure_names(proposal))
        orphans = {
            path.name for path in directory.glob(
                f"{_OCR_PROPOSAL_IMAGE_PREFIX}*")
            if path.is_file() and path.name not in referenced
        }
    _ocr_remove_staged_figures(build_id, orphans)


def _ocr_save_page_images(build_id: str, page: int, images: list[dict],
                          text: str, src_key: str = "primary",
                          regions: list | None = None) -> str:
    """Persist the figures an OCR service cut out of one page.

    Files land in the entry folder (ocr/images/p<page>-<id>), their boxes in
    ocr/layout.json, and the markdown's ![id](id) references are rewritten to
    the saved names so every reference stays unique across the compiled file.
    Returns the rewritten text. `regions` get the same rewrite in place —
    their text fields carry the same raw ids, and a region record that names
    figures the compiled doc calls something else is a record that lies."""
    if not images:
        return text
    meta_path = _entry_dir(build_id) / "ocr" / "layout.json"
    with _ocr_merge_lock:
        meta = lib.load_json(meta_path, {})
        text, figures, created = _ocr_write_page_images(
            build_id, page, images, text, src_key, regions)
        try:
            meta.setdefault("images", {}).update(figures)
            lib.save_json(meta_path, meta)
        except Exception:
            _ocr_remove_staged_figures(build_id, created)
            raise
    return text


def _ocr_save_page_words(build_id: str, src_key: str, page: int, words: list,
                         doc: str = "") -> None:
    """Persist one page's OCR word boxes to the sidecar (ocr/layout.json,
    {words: {"<src>": {"<page>": [{t,x,y,w,h,l}, ...]}}}). /api/pdf/words reads
    these back for a scan with no text layer, so the Layout facsimile works on
    it too. Keyed by SOURCE like the compiled .txt files, so a secondary scan's
    boxes never clobber the primary's. An empty list DROPS the page — the
    engine that owns the geometry saw a blank page. Only geometry-speaking
    engines may call this (see _ocr_job_run): a text-only re-OCR must not
    destroy boxes another engine paid to produce — the box positions stay
    valid for the unchanged page image even when the transcription moves on,
    and cross-engine work (clip-from-words, region seeding) depends on them
    surviving.

    `doc` records WHICH compiled file's transcription the boxes carry
    (words_doc, same src/page keying): the words' `t` values are that run's
    text, and the client only places the facsimile when the doc being viewed
    is the one the boxes belong to — any other doc flows its own text."""
    src_key = src_key or "primary"
    meta_path = _entry_dir(build_id) / "ocr" / "layout.json"
    with _ocr_merge_lock:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta = lib.load_json(meta_path, {})
        wmap = meta.setdefault("words", {})
        dmap = meta.setdefault("words_doc", {})
        pages = wmap.setdefault(src_key, {})
        docs = dmap.setdefault(src_key, {})
        if words:
            pages[str(int(page))] = words
            if doc:
                docs[str(int(page))] = doc
        else:
            pages.pop(str(int(page)), None)
            docs.pop(str(int(page)), None)
            if not pages:
                wmap.pop(src_key, None)
        if not docs:
            dmap.pop(src_key, None)
        if not dmap:
            meta.pop("words_doc", None)
        lib.save_json(meta_path, meta)


def _ocr_save_page_regions(build_id: str, src_key: str, page: int,
                           regions: list, dims: dict | None,
                           doc: str = "", state: str = "",
                           *, protect_existing: bool = False,
                           provider: str = "",
                           proposed_text: str = "") -> str:
    """Persist one page's typed regions (ocr/layout.json, {regions: {"<src>":
    {"<page>": {doc, dims, items: [{id, role, box, order, text}]}}}}), boxes
    0..1 like the word sidecar. Machine output may replace an untouched
    machine draft; human/imported/verified work receives a proposal instead.
    `doc` names the compiled file, `dims` describes the provider raster, and
    `state` is the local review flag."""
    src_key = src_key or "primary"
    meta_path = _entry_dir(build_id) / "ocr" / "layout.json"
    with _ocr_merge_lock:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta = lib.load_json(meta_path, {})
        current = _region_record(meta, src_key, page)
        if protect_existing and replica_service.is_protected(current):
            current["stale"] = replica_service.stale_marker(provider)
            proposal = replica_service.make_proposal(
                doc=doc, dims=dims or {}, items=regions or [],
                provider=provider,
                base_revision=replica_service.content_revision(current),
                text=proposed_text)
            meta.setdefault("region_proposals", {}).setdefault(
                src_key, {})[str(int(page))] = proposal
            lib.save_json(meta_path, meta)
            return "proposed"

        regions = libformat.ensure_rids(regions)
        rmap = meta.setdefault("regions", {})
        pages = rmap.setdefault(src_key, {})
        if regions:
            rec = {"doc": doc, "dims": dims or {}, "items": regions,
                   "origin": "machine" if protect_existing else "internal"}
            if state:
                rec["state"] = state
            pages[str(int(page))] = rec
            action = "saved"
        else:
            pages.pop(str(int(page)), None)
            if not pages:
                rmap.pop(src_key, None)
            action = "dropped"
        if not rmap:
            meta.pop("regions", None)
        proposals = meta.get("region_proposals") or {}
        proposal_pages = proposals.get(src_key) or {}
        proposal_pages.pop(str(int(page)), None)
        if not proposal_pages:
            proposals.pop(src_key, None)
        if not proposals:
            meta.pop("region_proposals", None)
        lib.save_json(meta_path, meta)
        return action


def _ocr_save_page_detection(build_id: str, src_key: str, page: int,
                             *, images: list[dict], text: str,
                             regions: list, dims: Mapping | None,
                             doc: str, provider: str) -> tuple[str, str]:
    """Commit one region-producing OCR result without touching protected work.

    Figure bytes are first written under a deterministic proposal identity.
    Their metadata remains inside the proposal until the engine applies it;
    canonical ``images`` and the protected page therefore cannot change merely
    because detection ran.  Unprotected pages retain the existing immediate
    save behavior, with secondary-source names now source-qualified.
    """
    source = str(src_key or "primary")
    page_number = int(page)
    document = _ocr_name(doc)
    proposed_regions = [dict(region) for region in (regions or [])
                        if isinstance(region, Mapping)]
    meta_path = _entry_dir(build_id) / "ocr" / "layout.json"
    previous_staged: set[str] = set()
    current_staged: set[str] = set()
    created: set[str] = set()
    with _ocr_merge_lock:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta = lib.load_json(meta_path, {})
        current = _region_record(meta, source, page_number)
        proposals = meta.get("region_proposals") or {}
        proposal_pages = proposals.get(source) or {}
        old_proposal = proposal_pages.get(str(page_number)) if isinstance(
            proposal_pages, Mapping) else None
        previous_staged = _ocr_staged_figure_names(old_proposal)
        protected = replica_service.is_protected(current)
        proposal_id = ""
        if protected:
            current["stale"] = replica_service.stale_marker(provider)
            base_revision = replica_service.content_revision(current)
            proposal_id = _ocr_proposal_identity(
                src_key=source, page=page_number, provider=provider,
                base_revision=base_revision,
                doc=document, text=text, dims=dims,
                regions=proposed_regions, images=images,
            )
        try:
            rewritten, figures, created = _ocr_write_page_images(
                build_id, page_number, images or [], str(text or ""), source,
                proposed_regions, proposal_id=proposal_id)
            if protected:
                proposal = replica_service.make_proposal(
                    doc=document,
                    dims=dict(dims) if isinstance(dims, Mapping) else {},
                    items=proposed_regions,
                    provider=provider,
                    base_revision=base_revision,
                    text=rewritten,
                    proposal_id=proposal_id,
                    staged_figures=figures,
                )
                meta.setdefault("region_proposals", {}).setdefault(
                    source, {})[str(page_number)] = proposal
                current_staged = set(figures)
                action = "proposed"
            else:
                if figures:
                    meta.setdefault("images", {}).update(figures)
                clean_regions = libformat.ensure_rids(proposed_regions)
                region_map = meta.setdefault("regions", {})
                pages = region_map.setdefault(source, {})
                if clean_regions:
                    pages[str(page_number)] = {
                        "doc": document,
                        "dims": (dict(dims) if isinstance(dims, Mapping)
                                 else {}),
                        "items": clean_regions,
                        "origin": "machine",
                    }
                    action = "saved"
                else:
                    pages.pop(str(page_number), None)
                    if not pages:
                        region_map.pop(source, None)
                    if not region_map:
                        meta.pop("regions", None)
                    action = "dropped"
                proposal_map = meta.get("region_proposals") or {}
                pending = proposal_map.get(source) or {}
                if isinstance(pending, dict):
                    pending.pop(str(page_number), None)
                    if not pending:
                        proposal_map.pop(source, None)
                if not proposal_map:
                    meta.pop("region_proposals", None)
            lib.save_json(meta_path, meta)
        except Exception:
            _ocr_remove_staged_figures(build_id, created)
            raise
    _ocr_remove_staged_figures(
        build_id, previous_staged - current_staged)
    return rewritten, action


def _ocr_drop_page_regions_for_doc(build_id: str, src_key: str, page: int,
                                   doc: str, *, provider: str = "",
                                   proposed_text: str = "") -> str:
    """Drop a page's region record IF it claims to carry `doc`'s text.

    A run without region output supersedes an untouched machine record. For
    protected work it stores the new text as a proposal and leaves canonical
    regions and compiled text alone. A different target leaves it unchanged."""
    src_key = src_key or "primary"
    meta_path = _entry_dir(build_id) / "ocr" / "layout.json"
    with _ocr_merge_lock:
        meta = lib.load_json(meta_path, {})
        rmap = meta.get("regions")
        if not isinstance(rmap, dict):
            return "unchanged"
        pages = rmap.get(src_key)
        rec = pages.get(str(int(page))) if isinstance(pages, dict) else None
        if not (isinstance(rec, dict) and rec.get("doc") == doc):
            return "unchanged"
        if replica_service.is_protected(rec):
            rec["stale"] = replica_service.stale_marker(provider)
            proposal = replica_service.make_proposal(
                doc=doc, dims=rec.get("dims") or {}, items=[],
                provider=provider,
                base_revision=replica_service.content_revision(rec),
                reason="source-text-changed-without-layout",
                text=proposed_text)
            meta.setdefault("region_proposals", {}).setdefault(
                src_key, {})[str(int(page))] = proposal
            lib.save_json(meta_path, meta)
            return "proposed"
        pages.pop(str(int(page)), None)
        if not pages:
            rmap.pop(src_key, None)
        if not rmap:
            meta.pop("regions", None)
        lib.save_json(meta_path, meta)
        return "dropped"


def _ocr_job_run(job_id: str) -> None:
    job = _ocr_jobs[job_id]
    cfg = job["cfg"]
    pdf = Path(job["pdf"])
    replica_detection = job.get("kind") == "replica.detect-regions"
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
            # Long-lived job dictionaries contain only nonsecret provider
            # settings. Lease exactly the credential(s) required by this one
            # page invocation and drop the temporary config immediately.
            with _ocr_execution_cfg(svc, cfg) as execution_cfg:
                result = runner(png, execution_cfg)
            # a runner may return a dict instead of a string: {text, images}
            # (Mistral figures) and/or {text, words} (Tesseract/Textract boxes)
            src_key = job.get("src_key") or "primary"
            region_action = "unchanged"
            if isinstance(result, dict):
                text = str(result.get("text") or "")
                # Region-producing results are committed as one page-level
                # decision. This is what keeps figure crops and their metadata
                # inside a protected-page proposal instead of publishing them
                # before the protection check runs.
                if "regions" in result:
                    text, region_action = _ocr_save_page_detection(
                        job["build_id"], src_key, n,
                        images=result.get("images") or [], text=text,
                        regions=result.get("regions") or [],
                        dims=result.get("dims"),
                        doc=_ocr_name(job["target"]), provider=svc)
                elif result.get("images"):
                    text = _ocr_save_page_images(
                        job["build_id"], n, result["images"], text,
                        src_key, regions=result.get("regions"))
                # a "words" key marks a geometry-speaking engine, and its
                # value is authoritative ([] = blank page). Engines without
                # one (Mistral, Claude) replace the text but must leave the
                # boxes alone — Tesseract geometry stays valid for the same
                # page image no matter which engine wrote the transcription.
                if "words" in result:
                    _ocr_save_page_words(job["build_id"], src_key, n,
                                         result.get("words") or [],
                                         doc=_ocr_name(job["target"]))
                # likewise a "regions" key marks the region-producing path
                # (Mistral blocks); its value is authoritative — [] clears
                # the page's regions along with the transcription they held.
                # Region-silent engines instead DROP a record claiming this
                # target's text: unlike word geometry, region text is
                # superseded by the new transcription.
                if "regions" not in result:
                    region_action = _ocr_drop_page_regions_for_doc(
                        job["build_id"], src_key, n,
                        _ocr_name(job["target"]), provider=svc,
                        proposed_text=text)
            else:
                text = str(result or "")
                region_action = _ocr_drop_page_regions_for_doc(
                    job["build_id"], src_key, n,
                    _ocr_name(job["target"]), provider=svc,
                    proposed_text=text)
            # A proposal is deliberately non-destructive: accepting it is the
            # command that may replace canonical regions and derived text.
            if region_action == "proposed":
                item["proposal"] = True
            else:
                _ocr_merge_page(job["build_id"], job["target"], n, text)
            if replica_detection:
                output_kind = ("replica.region-proposal"
                               if region_action == "proposed"
                               else "replica.region-page")
                quoted_item = urllib.parse.quote(str(job["build_id"]), safe="")
                quoted_source = urllib.parse.quote(str(src_key), safe="")
                ref = (f"librarytool://item/{quoted_item}/replica/"
                       f"{quoted_source}/pages/{int(n)}")
                if region_action == "proposed":
                    ref += "/proposal"
                job["outputs"] = [
                    {"kind": output_kind, "ref": ref, "partial": False}
                ]
                job["note"] = ("proposal ready" if region_action == "proposed"
                               else "regions updated")
            item["status"] = "ok"
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            item["status"] = f"error: {detail}"
            job["errors"] += 1
            if replica_detection:
                job["error"] = detail
            # Background failures otherwise exist only in the transient job
            # object. Emit them to the ring consumed by the Info tab, with
            # enough context to diagnose the executable, dependency, PDF, or
            # individual bad page without reproducing under a debugger.
            log.error("OCR failed: book=%s page=%s service=%s: %s",
                      job["build_id"], n, svc, detail, exc_info=True)
        job["done"] += 1
        if replica_detection:
            job["progress"] = {
                "completed": job["done"], "total": job["total"],
                "unit": "page", "phase": "detecting-regions",
            }
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
    elif replica_detection and job["errors"]:
        detail = str(job.get("error") or "region detection failed")
        _job_transition(
            job, "error", error=detail,
            failure={"code": "region_detection_failed", "message": detail,
                     "retryable": True})
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
    """Build a credential-free OCR job config.

    Credential-shaped renderer fields are deliberately ignored; only the
    protected provider lease can authorize execution.
    """
    settings = _client_settings()
    return {
        "tesseract": payload.get("tesseract") or settings.get("ocrTesseract"),
        "claude_model": payload.get("claude_model")
        or settings.get("ocrClaudeModel"),
        "aws_region": payload.get("aws_region") or settings.get("ocrAwsRegion"),
    }


@contextlib.contextmanager
def _ocr_execution_cfg(service: str, base_cfg: Mapping):
    cfg = dict(base_cfg)
    with contextlib.ExitStack() as stack:
        if service == "mistral":
            cfg["mistral_key"] = stack.enter_context(
                _lease_secret("mistralKey"))
        elif service == "claude":
            cfg["claude_key"] = stack.enter_context(
                _lease_secret("ocrClaudeKey"))
        elif service == "textract":
            cfg["aws_key"] = stack.enter_context(
                _lease_secret("ocrAwsKey"))
            cfg["aws_secret"] = stack.enter_context(
                _lease_secret("ocrAwsSecret"))
        try:
            yield cfg
        finally:
            for key in ("mistral_key", "claude_key", "aws_key", "aws_secret"):
                cfg.pop(key, None)


def _replica_detection_pdf(build: dict, source_id: str) -> Path | None:
    """Resolve one attached source without trusting a renderer-supplied path."""
    raw = str(build.get("pdf_file") or "") if source_id == "primary" else next(
        (str(source.get("path") or "")
         for source in (build.get("pdf_sources") or [])
         if isinstance(source, dict) and source.get("id") == source_id), "")
    pdf = _resolve_local(raw)
    return pdf if pdf is not None and pdf.is_file() else None


def _replica_detection_active(build_id: str, source_id: str,
                              page: int) -> dict | None:
    """The live detection for this exact page, copied under the registry lock."""
    with _jobs_lock:
        for job in _jobs.values():
            subject = job.get("subject") or {}
            if (job.get("kind") == "replica.detect-regions"
                    and job.get("state") in _JOB_ACTIVE
                    and str(subject.get("item_id") or job.get("build_id") or "")
                    == build_id
                    and str(subject.get("source_id") or "primary") == source_id
                    and str(subject.get("page") or "") == str(page)):
                return dict(job)
    return None


def _replica_detection_command_sha256(*, build_id: str, source_id: str,
                                      page: int, provider: str,
                                      expected_revision: str) -> str:
    command = {
        "capability": "replica.region-detection.start@1",
        "item_id": str(build_id),
        "source_id": str(source_id),
        "page": int(page),
        "provider": str(provider),
        "expected_region_revision": str(expected_revision),
    }
    canonical = json.dumps(
        command, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _replica_detection_replay_response(receipt, provider: str):
    return jsonify({
        "ok": True,
        "already": True,
        "provider": provider,
        "job": receipt.job.as_dict(),
        "receipt": receipt.as_public_dict(),
    })


@app.route("/api/v1/items/<build_id>/replica/region-detection-jobs",
           methods=["POST"])
def api_v1_replica_region_detection_job(build_id: str):
    """Start or join non-destructive automatic region detection for one page.

    The browser identifies the item/source/page and its current region
    revision; the engine resolves the attached PDF, provider credentials, OCR
    target, raster width, and page-structure revision.  This deliberately
    reuses the proven OCR worker while exposing a Replica-specific job identity
    that any current or future workbench can observe through ``/api/v1/jobs``.
    """
    payload = request.get_json(silent=True) or {}
    raw_page = payload.get("page")
    try:
        page = int(raw_page)
    except (TypeError, ValueError, OverflowError):
        page = 0
    if isinstance(raw_page, bool) or page < 1:
        return _engine_error_response(EngineValidationError(
            "page must be a positive integer", code="invalid_page",
            details={"item_id": build_id, "page": raw_page}))

    source_id = str(payload.get("source_id") or "primary")
    provider = str(payload.get("provider") or "automatic").strip().lower()
    if provider not in ("automatic", "mistral"):
        return _engine_error_response(EngineValidationError(
            "the requested region-detection provider is unavailable",
            code="region_detection_provider_unavailable",
            details={"provider": provider}))
    provider = "mistral"  # the only installed provider that returns blocks

    try:
        current = _replica_engine().get_region_page(
            PageKey(build_id, source_id, page))
    except EngineError as exc:
        return _engine_error_response(exc)
    expected = _region_match_token(payload)
    if not expected:
        return _engine_error_response(EnginePreconditionRequiredError(
            "a region revision is required",
            code="region_revision_required",
            details={"item_id": build_id, "source_id": source_id,
                     "page": page}), current=current)
    raw_idempotency_key = payload.get("idempotency_key")
    if not isinstance(raw_idempotency_key, str) or not raw_idempotency_key:
        return _engine_error_response(EnginePreconditionRequiredError(
            "an idempotency key is required",
            code="idempotency_key_required",
            details={"item_id": build_id, "source_id": source_id,
                     "page": page}), current=current)
    try:
        idempotency_key = _jobs_engine().validate_operation_id(
            raw_idempotency_key)
    except EngineError as exc:
        return _engine_error_response(exc, current=current)
    source_id = current.key.source_id
    command_sha256 = _replica_detection_command_sha256(
        build_id=build_id, source_id=source_id, page=page,
        provider=provider, expected_revision=expected)
    try:
        receipt = _jobs_engine().command_receipt(
            idempotency_key, command_sha256,
            kind="replica.detect-regions")
    except EngineError as exc:
        return _engine_error_response(exc, current=current)
    # Replay precedes the live CAS check. An exact retry after a successful
    # unprotected run necessarily carries the old page revision, but it must
    # still return the paid-for terminal job instead of starting or billing it
    # again.
    if receipt is not None:
        return _replica_detection_replay_response(receipt, provider)
    if expected != current.revision:
        return _engine_error_response(EngineConflictError(
            "the region page changed before detection could start",
            code="region_revision_conflict",
            details={"item_id": build_id, "source_id": source_id,
                     "page": page, "expected_revision": expected,
                     "current_revision": current.revision}), current=current)

    build = lib.load_json(BUILDS_PATH, {}).get(build_id)
    # get_region_page already checks item/source identity; retain the explicit
    # dictionary guard because the attached-path resolver needs build metadata.
    if not isinstance(build, dict):
        return _engine_error_response(EngineNotFoundError(
            "the item does not exist", code="item_not_found",
            details={"item_id": build_id}))
    source_id = _valid_src_key(build, source_id)
    pdf = _replica_detection_pdf(build, source_id) if source_id else None
    if pdf is None:
        return _engine_error_response(EngineValidationError(
            "the selected source has no attached local PDF",
            code="replica_source_pdf_unavailable",
            details={"item_id": build_id, "source_id": source_id or ""}))

    cfg = _ocr_request_cfg({})
    if not _secret_is_configured("mistralKey"):
        return _engine_error_response(EngineValidationError(
            "Mistral OCR is not configured",
            code="region_detection_provider_not_configured",
            details={"provider": provider}))
    try:
        width = max(600, min(3000, int(
            _client_settings().get("ocrImageWidth") or 1400)))
    except (TypeError, ValueError, OverflowError):
        width = 1400

    with _page_structure_lock:
        source_revision = _page_structure_revision.get(build_id, 0)
    input_revisions = {
        "region": expected,
        "page_structure": source_revision,
    }
    with _replica_detection_start_lock:
        try:
            receipt = _jobs_engine().command_receipt(
                idempotency_key, command_sha256,
                kind="replica.detect-regions")
        except EngineError as exc:
            return _engine_error_response(exc, current=current)
        if receipt is not None:
            return _replica_detection_replay_response(receipt, provider)
        existing = _replica_detection_active(build_id, source_id, page)
        if existing is not None:
            existing_region = str(
                (existing.get("input_revisions") or {}).get("region") or "")
            if existing_region and existing_region != expected:
                return _engine_error_response(EngineConflictError(
                    "region detection is already running from another revision",
                    code="region_detection_already_running",
                    details={"item_id": build_id, "source_id": source_id,
                             "page": page,
                             "running_revision": existing_region,
                             "expected_revision": expected}), current=current)
            view = _jobs_engine().view(str(existing.get("id") or ""))
            if view is not None:
                # A second observer may carry its own command identity. It
                # does not acquire a receipt for work it did not start, but it
                # can still join the identical live job instead of presenting
                # an unexplained page-level conflict.
                return jsonify({"ok": True, "already": True,
                                "provider": provider,
                                "job": view.as_dict()})

        job_id = lib.gen_id(set(_ocr_jobs) | set(_jobs))
        target = ("compiled.txt" if source_id == "primary"
                  else f"compiled-{source_id}.txt")
        job = {
            "id": job_id,
            "kind": "replica.detect-regions",
            "build_id": build_id,
            "pdf": str(pdf),
            "target": target,
            "src_key": source_id,
            "pages": [{"page": page, "service": provider,
                       "status": "queued"}],
            "done": 0, "total": 1, "errors": 0, "cancelled": 0,
            "cancel_requested": False, "width": width,
            "status": "running", "cfg": cfg,
            "subject": {"item_id": build_id, "source_id": source_id,
                        "page": page},
            "progress": {"completed": 0, "total": 1,
                         "unit": "page", "phase": "detecting-regions"},
            "input_revisions": input_revisions,
            "outputs": [], "provider": provider,
            "operation_id": idempotency_key,
            "command_sha256": command_sha256,
        }
        if not _ocr_job_start_guarded(job, source_revision, record_source=True):
            return _engine_error_response(EngineConflictError(
                "page numbering changed before detection could start",
                code="page_structure_conflict",
                details={"item_id": build_id, "source_id": source_id,
                         "page": page}), current=current)

    view = _jobs_engine().view(job_id)
    if view is None:  # registration and view publication are one start step
        raise RuntimeError("region-detection job was not registered")
    receipt = _jobs_engine().command_receipt(
        idempotency_key, command_sha256, kind="replica.detect-regions")
    if receipt is None:
        raise RuntimeError("region-detection command receipt was not persisted")
    return jsonify({"ok": True, "already": False, "provider": provider,
                    "job": view.as_dict(),
                    "receipt": receipt.as_public_dict()})


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


# --- trash (recoverable deletes) ------------------------------------------------------
# A delete that can be undone days later, not just seconds later. The client's
# pushOp undo covers the immediate mistake; this covers "I noticed on Tuesday".
#
# An item is a self-contained PAYLOAD (one directory) plus enough hints to put it
# back. There is deliberately no operation log, no journal and no inverse-of-a-
# renumber function anywhere: collateral files are snapshotted WHOLE, so writing
# them back is automatically correct. An item that cannot be restored cleanly
# says so and offers its payload as a download instead of guessing.
#
# Placement matters: output/trash/ sits beside output/backups/ and NOT under
# output/entries/, because store_sync mirrors that tree to R2 file-by-file — a
# trash inside it would upload the very bytes the user just deleted.
TRASH_DIR = lib.OUTPUT_DIR / "trash"
TRASH_PATH = TRASH_DIR / "index.json"
_trash_lock = threading.Lock()

_TRASH_KEEP = 200                    # items
_TRASH_KEEP_DAYS = 30
_TRASH_MAX_BYTES = 2 << 30           # 2 GiB
_TRASH_FLOOR_MINUTES = 60            # never prune something this fresh
_TRASH_FULL_COPY_MAX = 64 << 20      # PDFs at/under this also keep a full pre-image
# payload prefixes that restore writes back over the live file. Anything
# outside this set is download-only, so both sides must agree on it: a snapshot
# stamped here but not written back (or vice versa) is a silent half-restore.
_TRASH_WRITEBACK = ("ocr/", "translations/")
# ids with a restore in flight. A restore does its file work OUTSIDE the index
# lock (it rewrites a whole PDF), so without this a concurrent Empty-trash could
# delete the payload mid-read and leave the book with restored pages but
# still-renumbered OCR — reported as a success. Guarded by _trash_lock.
_trash_restoring: set[str] = set()


def _trash_dir_bytes(d: Path) -> int:
    total = 0
    for p in d.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _trash_open() -> tuple[str, Path]:
    """Reserve an item id + its payload directory WITHOUT touching the index.

    Split from the commit because a payload accrues across a long destructive
    function (the PDF rewrite, then the OCR snapshots under another lock) and
    the index row must be written LAST — a crash then leaves an orphan
    directory the next prune sweeps, never a row pointing at nothing.
    """
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    index = lib.load_json(TRASH_PATH, {"version": 1, "items": {}})
    tid = lib.gen_id(set(index.get("items") or {}))
    (TRASH_DIR / tid).mkdir(parents=True, exist_ok=True)
    return tid, TRASH_DIR / tid


def _trash_prune_locked(doc: dict) -> None:
    """Enforce the caps, newest-first, inside the caller's index mutation.

    Pruned synchronously by the inserter (the pattern _jobs_prune_locked uses):
    this project has no scheduler, and a background sweeper would be machinery
    out of proportion to the problem.
    """
    items = doc.get("items") or {}
    now = datetime.now(timezone.utc)
    floor = now - timedelta(minutes=_TRASH_FLOOR_MINUTES)
    cutoff = now - timedelta(days=_TRASH_KEEP_DAYS)

    def created(rec):
        try:
            return datetime.fromisoformat(str(rec.get("created") or ""))
        except ValueError:
            return now

    ordered = sorted(items.values(), key=created, reverse=True)
    running = 0
    doomed = []
    for i, rec in enumerate(ordered):
        when = created(rec)
        running += int(rec.get("bytes") or 0)
        if when > floor:
            continue                      # too fresh to prune, whatever the caps
        if str(rec.get("id") or "") in _trash_restoring:
            continue                      # a restore is reading this payload
        if (i >= _TRASH_KEEP or when < cutoff or running > _TRASH_MAX_BYTES):
            doomed.append(str(rec.get("id") or ""))
    for tid in doomed:
        shutil.rmtree(TRASH_DIR / tid, ignore_errors=True)
        items.pop(tid, None)
    # orphan sweep: directories whose row never landed (the crash window above)
    try:
        for d in TRASH_DIR.iterdir():
            if not d.is_dir() or d.name in items or d.name in _trash_restoring:
                continue
            if datetime.fromtimestamp(d.stat().st_mtime, timezone.utc) > floor:
                continue                  # may be a commit still in flight
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def _trash_commit(tid: str, kind: str, label: str, origin: dict,
                  restore: dict, payload_kind: str, files: list[str]) -> str:
    """Register a payload written by _trash_open. Innermost lock, held only
    for the index write — never while copying payload bytes."""
    rec = {
        "id": tid, "kind": kind, "label": label,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "origin": origin, "payload_kind": payload_kind,
        "bytes": _trash_dir_bytes(TRASH_DIR / tid),
        "files": files, "restore": restore, "restored_at": "", "note": "",
    }

    def apply(doc):
        doc.setdefault("version", 1)
        doc.setdefault("items", {})[tid] = rec
        _trash_prune_locked(doc)
        return tid

    return _mutate_json(TRASH_PATH, _trash_lock, {"version": 1, "items": {}}, apply)


def _trash_amend(tid: str, files: list[str], stamps: dict,
                 restore: dict | None = None) -> None:
    """Enrich an already-registered row with payload written after the commit.

    The row is registered the moment the destructive step lands, so it can
    never be missing while the user's data is already gone; the collateral
    snapshots taken afterwards are folded in here. Re-sums bytes so the caps
    stay honest.
    """
    def apply(doc):
        rec = (doc.get("items") or {}).get(tid)
        if rec is None:
            return
        rec["files"] = list(files)
        rec.setdefault("restore", {})["stamps"] = stamps
        rec["restore"].update(restore or {})
        rec["bytes"] = _trash_dir_bytes(TRASH_DIR / tid)
        # the commit priced this row before its collateral existed, so the cap
        # was tested against an undercount — re-test it now that the real size
        # is known. The freshness floor keeps this from pruning the row itself.
        _trash_prune_locked(doc)

    _mutate_json(TRASH_PATH, _trash_lock, {"version": 1, "items": {}}, apply)


def _trash_retire(tid: str, why: str) -> None:
    """Downgrade a row to download-only: the thing it would restore INTO is
    gone, so keep the payload the user might still want (the pages) and drop
    the pre-image that only restore could have used. Re-sums bytes so a dead
    row stops holding the cap hostage."""
    def apply(doc):
        rec = (doc.get("items") or {}).get(tid)
        if rec is None:
            return
        (TRASH_DIR / tid / "original.pdf").unlink(missing_ok=True)
        rec["files"] = [f for f in (rec.get("files") or []) if f != "original.pdf"]
        rec["payload_kind"] = "pages"
        rec["restorable"] = False
        rec["note"] = why
        rec["bytes"] = _trash_dir_bytes(TRASH_DIR / tid)

    _mutate_json(TRASH_PATH, _trash_lock, {"version": 1, "items": {}}, apply)


def _trash_rel_ok(rel: str) -> bool:
    """Cheap string screen before any path join — a hand-edited index.json
    must not be able to walk out of the payload directory."""
    r = str(rel or "")
    return bool(r) and ".." not in r and ":" not in r and "\\" not in r \
        and not r.startswith("/")


def _trash_payload_path(tid: str, rel: str) -> Path | None:
    """Resolve a payload file, or None. Containment is checked twice: the
    string screen above, then the resolved is_relative_to test the rest of the
    server uses for entry files."""
    if not _trash_rel_ok(tid) or not _trash_rel_ok(rel):
        return None
    root = (TRASH_DIR / tid).resolve()
    cand = (root / rel).resolve()
    if not cand.is_relative_to(root) or not cand.is_file():
        return None
    return cand


def _trash_put(kind: str, label: str, origin: dict, restore: dict,
               files: dict[str, bytes | str]) -> str:
    """One-shot helper for adopters whose payload is small and known up front:
    _trash_put("build", "Entry: Herbal", {...}, {}, {"record.json": blob}).
    The page-delete path uses open/commit instead because its payload accrues."""
    tid, d = _trash_open()
    written = []
    for rel, data in files.items():
        if not _trash_rel_ok(rel):
            continue
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(str(data), encoding="utf-8")
        written.append(rel)
    return _trash_commit(tid, kind, label, origin, restore, "json", written)


@app.route("/api/trash")
def api_trash():
    """List trashed items, newest first, plus the retention summary the Info
    tab footer shows. Plain read: save_json is atomic tmp+replace, so a reader
    never sees a torn document and needs no lock."""
    doc = lib.load_json(TRASH_PATH, {"version": 1, "items": {}})
    items = []
    for raw in (doc.get("items") or {}).values():
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        if item.get("kind") == "build" and not item.get("restored_at"):
            # These rows predate aggregate lifecycle storage. Their raw record
            # remains downloadable, but reinserting it would bypass the
            # engine-owned item/tree tombstone and can resurrect stale state.
            item["restorable"] = False
            item["note"] = (
                "legacy catalogue-only recovery; download the record"
            )
        items.append(item)
    items.sort(key=lambda r: str(r.get("created") or ""), reverse=True)
    return jsonify({"ok": True, "items": items, "summary": {
        "count": len(items),
        "bytes": sum(int(r.get("bytes") or 0) for r in items),
        "cap_bytes": _TRASH_MAX_BYTES, "keep_days": _TRASH_KEEP_DAYS,
        "keep": _TRASH_KEEP,
    }})


@app.route("/api/trash/<tid>/payload/<path:rel>")
def api_trash_payload(tid: str, rel: str):
    """Download one payload file — the fallback when a restore is refused, so
    the user still gets their pages back by hand."""
    src = _trash_payload_path(tid, rel)
    if src is None:
        abort(404)
    return send_file(str(src), as_attachment=True, download_name=Path(rel).name)


@app.route("/api/trash/forget", methods=["POST"])
def api_trash_forget():
    """Discard one item, or everything. The payload is the safety net, so the
    client confirms only the bulk case."""
    p = request.get_json(silent=True) or {}
    wipe_all = bool(p.get("all"))
    tid = str(p.get("id") or "")
    if not wipe_all and not _trash_rel_ok(tid):
        return jsonify({"ok": False, "error": "bad id"}), 400

    busy = []

    def apply(doc):
        items = doc.setdefault("items", {})
        found = list(items) if wipe_all else ([tid] if tid in items else [])
        # never pull a payload out from under a restore that is mid-rewrite
        targets = []
        for t in found:
            (busy if t in _trash_restoring else targets).append(t)
        for t in targets:
            shutil.rmtree(TRASH_DIR / t, ignore_errors=True)
            items.pop(t, None)
        return len(targets)

    n = _mutate_json(TRASH_PATH, _trash_lock, {"version": 1, "items": {}}, apply)
    return jsonify({"ok": True, "forgotten": n, "busy": len(busy)})


def _trash_restore_pdf_pages(item: dict) -> tuple[dict, int]:
    """Restore page payloads only while their aggregate item remains live."""

    origin = item.get("origin") or {}
    build_id = str(origin.get("build_id") or "")
    builds = lib.load_json(BUILDS_PATH, {})
    if build_id not in builds:
        return {"ok": False, "error": "that entry no longer exists"}, 409
    with _live_item_write_scope(build_id):
        return _trash_restore_pdf_pages_guarded(item)


def _trash_restore_pdf_pages_guarded(item: dict) -> tuple[dict, int]:
    """Put deleted pages back. Refuses rather than guesses: the recorded page
    numbers are only meaningful against the exact post-delete PDF, and
    appending them to the end would not be a restore at all."""
    from pypdf import PdfReader, PdfWriter
    origin = item.get("origin") or {}
    rest = item.get("restore") or {}
    build_id = str(origin.get("build_id") or "")
    builds = lib.load_json(BUILDS_PATH, {})
    if build_id not in builds:
        return {"ok": False, "error": "that entry no longer exists"}, 409
    refresh_expected_revision = _engine_build_record_revision(
        build_id, builds[build_id]
    )
    pdf = _resolve_local(str(origin.get("pdf") or ""))
    if pdf is None or not pdf.is_file() or pdf.suffix.lower() != ".pdf":
        return {"ok": False, "error": "the original PDF is no longer there"}, 409
    # restoring into the truncated preview would desync OCR the same way
    # deleting from it would — the deletion path refuses it too
    if pdf.resolve().is_relative_to(ENTRIES_DIR.resolve()):
        return {"ok": False,
                "error": "this book now points at the truncated preview"}, 409
    payload = _trash_payload_path(str(item.get("id") or ""), "pages.pdf")
    if payload is None:
        return {"ok": False, "error": "the trashed pages are gone"}, 410

    pages = [int(x) for x in (rest.get("pages") or [])]
    with _page_structure_lock:
        blockers = _page_job_blockers(build_id)
        if blockers:
            return {"ok": False, "error": "an OCR job is running for this book"}, 409
        expected_digest = str(rest.get("pdf_after_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            return {"ok": False, "error":
                    "this recovery record predates exact PDF lineage checks — "
                    "download the pages instead and re-insert them by hand"}, 409
        try:
            current_digest = _file_sha256(pdf)
        except OSError:
            return {"ok": False, "error":
                    "the current PDF could not be verified — download the "
                    "pages instead and re-insert them by hand"}, 409
        if current_digest != expected_digest:
            return {"ok": False, "error":
                    "this PDF has changed since the delete — download the pages "
                    "instead and re-insert them by hand"}, 409
        current = PdfReader(str(pdf))
        if len(current.pages) != int(rest.get("pages_after") or -1):
            return {"ok": False, "error":
                    "this PDF has changed since the delete — download the pages "
                    "instead and re-insert them by hand"}, 409
        held = PdfReader(str(payload))
        # walk the ORIGINAL positions: take a held page whenever the counter
        # lands on a recorded number, otherwise the next surviving page
        writer = PdfWriter()
        held_i = cur_i = 0
        for n in range(1, int(rest.get("pages_before") or 0) + 1):
            if n in set(pages) and held_i < len(held.pages):
                writer.add_page(held.pages[held_i])
                held_i += 1
            elif cur_i < len(current.pages):
                writer.add_page(current.pages[cur_i])
                cur_i += 1
        tmp = pdf.with_suffix(".restore.tmp")
        with open(tmp, "wb") as fh:
            writer.write(fh)
        tmp.replace(pdf)
        # Bump BEFORE the collateral work. The page structure genuinely changed
        # as of the line above, so the OCR guard has to see it even if every
        # step below fails. Everything after this point is best-effort and
        # reports through `skipped`: raising instead would leave the book
        # half-restored AND permanently unrestorable, because the page-count
        # guard above would reject every retry.
        _page_structure_revision[build_id] = (
            _page_structure_revision.get(build_id, 0) + 1)

        # write back only the snapshots whose live file is untouched since the
        # delete — silently overwriting an edit would be its own data loss
        restored, skipped = ["pages.pdf"], []
        stamps = rest.get("stamps") or {}
        for rel in (item.get("files") or []):
            if not rel.startswith(_TRASH_WRITEBACK):
                continue
            src = _trash_payload_path(str(item.get("id") or ""), rel)
            live = _entry_dir(build_id) / rel
            if src is None:
                skipped.append({"file": rel, "reason": "payload missing"})
                continue
            # `want` is [size, mtime] for a file the delete left in place, or
            # [] for one it removed outright (a deleted page's figure). The
            # empty list is a real signal, not a missing stamp: if something
            # now occupies that path, it arrived AFTER the delete — a re-run of
            # figure extraction, say — and writing over it would be exactly the
            # silent clobber this guard exists to prevent.
            want = stamps.get(rel)
            try:
                st = live.stat()
                same = bool(want) and int(want[0]) == st.st_size and \
                    abs(float(want[1]) - st.st_mtime) < 0.001
            except OSError:
                same = not want          # absent now, absent then -> safe to write
            if want is not None and not same:
                skipped.append({"file": rel, "reason": "edited since the delete"})
                continue
            try:
                live.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, live)
            except OSError:
                skipped.append({"file": rel, "reason": "could not be written back"})
                continue
            restored.append(rel)

        # Put the page marks and review threads back on their original pages.
        # The delete moved them; a restore that rebuilt the pages and the text
        # but left these one page off would be a half-restore reported as a
        # success. Dropped marks come from the payload, threads from their own
        # tombstones.
        att = _trash_payload_path(str(item.get("id") or ""), "attention.json")
        for note in _unremap_page_attention_references(
                build_id, str(origin.get("src_key") or "primary"), pages,
                int(rest.get("pages_before") or 0),
                lib.load_json(att, {}) if att else {}):
            skipped.append({"file": "page marks", "reason": note})

        # Build fields get the same edited-since guard as the snapshots. Only
        # the keys the delete actually rewrote carry an "_after" value, so a
        # field it never touched is never written — and one the user has since
        # changed by hand is reported rather than clobbered.
        live_b = lib.load_json(BUILDS_PATH, {}).get(build_id) or {}
        changed = {}
        for key in ("title_pages", "thumbnail_source"):
            after = rest.get(f"{key}_after")
            if after is None:
                continue
            if str(live_b.get(key) or "") != str(after):
                skipped.append({"file": key, "reason": "edited since the delete"})
                continue
            changed[key] = str(rest.get(f"{key}_before") or "")
        # ALWAYS advance the build revision, even with nothing to write. A
        # restore changes the page grid exactly as a delete does, and the grid
        # is what page_revision guards — leaving the token untouched (which it
        # was whenever `changed` came out empty: a secondary source, or a
        # primary with no title pages) let a client holding the post-delete
        # token delete against numbering that had silently reverted, hitting a
        # different physical page with no conflict raised. The delete path
        # carries the same invariant via `if changed or build_persisted`.
        try:
            refresh_expected_revision = _builds_apply(
                build_id,
                changed,
                expected_revision=refresh_expected_revision,
            )
        except (EngineError, OSError):
            skipped.append({"file": "entry record",
                            "reason": "could not be written back"})
    try:
        _engine_refresh_representation_reference(
            build_id,
            str(origin.get("src_key") or "primary"),
            str(origin.get("pdf") or pdf),
            operation_scope="page-restore-source-refresh",
            expected_item_revision=refresh_expected_revision,
        )
    except EngineError as exc:
        log.warning(
            "could not refresh representation after page restore: %s",
            exc.code,
        )
        skipped.append({
            "file": "source integrity metadata",
            "reason": "could not be refreshed",
        })

    # the caller (api_trash_restore) owns marking the row restored
    return {"ok": True, "restored": restored, "skipped": skipped,
            "pages": int(rest.get("pages_before") or 0)}, 200


def _trash_restore_record(item: dict) -> tuple[dict, int]:
    """Put a deleted build / manual entry back. Refuses rather than clobbers if
    something now occupies the id — the user may have re-created it by hand."""
    kind = str(item.get("kind") or "")
    origin = item.get("origin") or {}
    src = _trash_payload_path(str(item.get("id") or ""), "record.json")
    if src is None:
        return {"ok": False, "error": "the trashed record is gone"}, 410
    try:
        record = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "error": "the trashed record is unreadable"}, 410
    if kind == "build":
        return {
            "ok": False,
            "error": (
                "this legacy catalogue-only recovery cannot safely restore "
                "an aggregate item; download its record instead"
            ),
            "code": "legacy_item_restore_retired",
        }, 410
    rid, path, lock = (str(origin.get("entry_id") or ""),
                       lib.MANUAL_ENTRIES_PATH, _manual_lock)
    if not rid:
        return {"ok": False, "error": "the trashed record has no id"}, 410

    def apply(doc):
        if rid in doc:
            # Retrying a restore after its response was lost is a replay, not
            # an overwrite. Any intervening edit still conflicts.
            return "replayed" if doc[rid] == record else "conflict"
        if kind == "manual_entry":
            _canonicalize_collection_link(record)
        doc[rid] = record
        return "restored"

    outcome = _mutate_json(path, lock, {}, apply)
    if outcome == "conflict":
        return {"ok": False,
                "error": "something with that id exists again — restoring would "
                         "overwrite it"}, 409
    body = {
        "ok": True,
        "restored": ["record.json"],
        "skipped": [],
        "replayed": outcome == "replayed",
    }
    body["build" if kind == "build" else "entry"] = record
    return body, 200


def _trash_restore_translation(item: dict) -> tuple[dict, int]:
    """Restore translation payloads only while their item remains live."""

    origin = item.get("origin") or {}
    bid = str(origin.get("build_id") or "")
    builds = lib.load_json(BUILDS_PATH, {})
    if bid not in builds:
        return {"ok": False, "error": "that entry no longer exists"}, 409
    with _live_item_write_scope(bid):
        return _trash_restore_translation_guarded(item)


def _trash_restore_translation_guarded(item: dict) -> tuple[dict, int]:
    """Write a deleted translation (and its provenance sidecar) back, unless a
    newer one is already there."""
    origin = item.get("origin") or {}
    bid = str(origin.get("build_id") or "")
    lang = str(origin.get("lang") or "")
    if not bid or not lang:
        return {"ok": False, "error": "the trashed translation has no target"}, 410
    dest_dir = _entry_dir(bid) / "translations"
    restored, skipped = [], []
    with _an_write_lock:
        for rel in (item.get("files") or []):
            src = _trash_payload_path(str(item.get("id") or ""), rel)
            if src is None:
                skipped.append({"file": rel, "reason": "payload missing"})
                continue
            dest = dest_dir / Path(rel).name
            if dest.exists():
                skipped.append({"file": rel, "reason": "a newer translation is there"})
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            restored.append(rel)
    if not restored:
        # restoring nothing is not a success — either something newer is in the
        # way, or the payload is gone. Saying "ok" would be a silent no-op.
        blocked = any(s.get("reason", "").startswith("a newer") for s in skipped)
        return {"ok": False, "error":
                "a newer translation is already in place" if blocked
                else "nothing left to restore"}, 409 if blocked else 410
    return {"ok": True, "restored": restored, "skipped": skipped}, 200


@app.route("/api/trash/restore", methods=["POST"])
def api_trash_restore():
    """Restore one item. The body carries ONLY an id — never a path, never a
    destination; everything else is derived from the stored row."""
    p = request.get_json(silent=True) or {}
    tid = str(p.get("id") or "")
    if not _trash_rel_ok(tid):
        return jsonify({"ok": False, "error": "bad id"}), 400
    doc = lib.load_json(TRASH_PATH, {"version": 1, "items": {}})
    item = (doc.get("items") or {}).get(tid)
    if not item:
        # Since aggregate deletion was introduced, the compatibility DELETE
        # route returns a lifecycle tombstone id as its old ``trash_id``.
        # Preserve old undo clients by resolving that handle through the
        # lifecycle service instead of recreating catalogue state here.
        try:
            body, result = _legacy_restore_lifecycle_tombstone(tid)
        except EngineNotFoundError:
            return jsonify({"ok": False, "error": "no such item"}), 404
        except EngineError as exc:
            return _engine_error_response(exc)
        response = jsonify(body)
        response.cache_control.no_store = True
        if result is not None:
            response.headers["X-Record-Revision"] = (
                result.receipt.restored_item_revision
            )
            response.headers["X-Tombstone-Revision"] = (
                result.receipt.tombstone.revision
            )
        return response
    if item.get("restorable") is False:
        return jsonify({"ok": False, "error": str(item.get("note") or "")
                        or "this item can no longer be restored"}), 409
    kind = str(item.get("kind") or "")
    if kind not in ("pdf_pages", "build", "manual_entry", "translation"):
        return jsonify({"ok": False,
                        "error": f"restoring '{kind}' is not supported yet"}), 501
    # claim the payload for the duration: a concurrent forget/prune would
    # otherwise be free to delete it out from under the read below
    with _trash_lock:
        _trash_restoring.add(tid)
    try:
        if kind == "pdf_pages":
            body, code = _trash_restore_pdf_pages(item)
        elif kind == "translation":
            body, code = _trash_restore_translation(item)
        else:
            body, code = _trash_restore_record(item)
    finally:
        with _trash_lock:
            _trash_restoring.discard(tid)
    if body.get("ok"):
        # one owner for the row update, whatever the kind: the pane greys a
        # restored item rather than dropping it, so the restore stays visible
        def mark(doc):
            rec = (doc.get("items") or {}).get(tid)
            if rec is None:
                return
            rec["restored_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds")
            kept = len(body.get("skipped") or [])
            if kept:
                rec["note"] = f"{kept} file(s) kept as they were"
        _mutate_json(TRASH_PATH, _trash_lock, {"version": 1, "items": {}}, mark)
    return jsonify(body), code


# --- PDF page deletion ---------------------------------------------------------------

_page_structure_lock = threading.RLock()
_page_structure_revision: dict[str, int] = {}


class _PageStructureConflict(ValueError):
    """A destructive request targets a stale page grid or detached PDF."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _page_deletion_target(build_id: str, pdf: Path,
                          expected_revision: str,
                          reserve: bool) -> tuple[dict, str]:
    """Validate the current grid/source and optionally reserve its next token."""
    expected_revision = str(expected_revision or "")
    with _builds_lock:
        current = lib.load_json(BUILDS_PATH, {})
        build = current.get(build_id)
        if not isinstance(build, dict):
            raise _PageStructureConflict(
                "stale_page_revision",
                "book changed; reopen it before deleting pages")
        revision = str(build.get("updated_at") or "unversioned")
        if not expected_revision or expected_revision != revision:
            raise _PageStructureConflict(
                "stale_page_revision",
                "page changed; reopen it before deleting pages")
        src_key = _attached_src_key_for_path(build, pdf)
        if not src_key:
            raise _PageStructureConflict(
                "pdf_not_attached",
                "PDF attachment changed; reopen the book before deleting pages")
        if reserve:
            build["updated_at"] = _build_updated_at(build.get("updated_at"))
            lib.save_json(BUILDS_PATH, current)
        return build, src_key


def _validate_page_deletion(build_id: str, pdf: Path,
                            expected_revision: str) -> tuple[dict, str]:
    """Reject stale revisions and detached paths before inspecting the PDF."""
    return _page_deletion_target(
        build_id, pdf, expected_revision, reserve=False)


def _reserve_page_deletion(build_id: str, pdf: Path,
                           expected_revision: str) -> tuple[dict, str]:
    """Revalidate and durably advance the page-grid token before PDF commit.

    This runs while ``_page_structure_lock`` is held. The compare-and-bump is
    one ``_builds_lock`` transaction, so a concurrent metadata save cannot
    slip between validation and reservation. Advancing before ``replace`` is
    deliberately conservative: a later pre-commit failure may force the user
    to reopen the page, but a committed deletion can never leave an old review
    token valid merely because the post-commit metadata merge failed.
    """
    return _page_deletion_target(
        build_id, pdf, expected_revision, reserve=True)


def _ocr_job_start_guarded(job: dict, source_revision: int,
                           record_source: bool = False) -> bool:
    """Register/start OCR atomically against item and page deletion."""
    build_id = str(job.get("build_id") or "")
    with _page_structure_lock:
        if _page_structure_revision.get(build_id, 0) != source_revision:
            return False
        # Region detection records its document/source binding before the
        # unified registration below. Confirm the item under the catalogue
        # lock first so a start that waited behind lifecycle deletion cannot
        # recreate files for an item that is already gone. The final guarded
        # registration rechecks the same fact after this collateral write.
        with _builds_lock:
            builds = lib.load_json(BUILDS_PATH, {})
            if not isinstance(builds, dict) or not isinstance(
                    builds.get(build_id), dict):
                return False
        if record_source:
            _ocr_set_source(build_id, _ocr_name(job.get("target") or "compiled.txt"),
                            job.get("src_key") or "primary")
        with _ocr_jobs_lock:
            _ocr_jobs[job["id"]] = job
        # Preserve a semantic consumer kind (for example
        # ``replica.detect-regions``) while legacy OCR batches continue to
        # default to ``ocr``.  The generic jobs API and future workbenches must
        # not need to infer the producer from mutable OCR implementation data.
        try:
            _job_track_item_guarded(
                job, str(job.get("kind") or "ocr"), build_id,
            )
        except _ItemJobStartRejected:
            with _ocr_jobs_lock:
                _ocr_jobs.pop(job["id"], None)
            return False
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


def _renumber_layout_words(build_id: str, src_key: str, removed: list[int],
                           snapshot: Path | None = None,
                           snapshot_out: list[str] | None = None) -> None:
    """Remap page-keyed word boxes and extracted-image layout metadata.

    Word boxes are source-scoped. Extracted figures belong to the OCR document
    recorded in ``sources.json``; only figures for the PDF being edited move.
    A deleted page's figures leave the layout metadata (so they cannot be
    placed on the wrong page) and their files go to the trash item under
    ``ocr/images/`` — the same relative path they live at, which is what lets
    restore write them back with no special case. They used to be copied to an
    ``ocr/images/.page-delete-backup`` dead-drop that nothing ever read: after
    a restore put layout.json back, every figure it named was still sitting in
    there, so the facsimile drew a page of missing images.
    """
    meta_path = _entry_dir(build_id) / "ocr" / "layout.json"
    if not meta_path.is_file():
        return
    removed_set = set(removed)

    def remap(pages: dict) -> dict:
        out = {}
        for k, v in pages.items():
            try:
                n = int(k)
            except (TypeError, ValueError):
                continue
            if n in removed_set:
                continue
            out[str(n - sum(1 for r in removed if r < n))] = v
        return out

    with _ocr_merge_lock:
        meta = lib.load_json(meta_path, {})
        dirty = False
        # Every source/page map in the Replica aggregate moves together.  A
        # proposal or pending compile left on its old number would otherwise
        # be offered/applied to a different physical page after deletion.
        for key in ("words", "words_doc", "regions",
                    "region_proposals", "region_compile_pending"):
            wmap = meta.get(key)
            if not isinstance(wmap, dict):
                continue
            pages = wmap.get(src_key or "primary")
            if not isinstance(pages, dict):
                continue
            wmap[src_key or "primary"] = remap(pages)
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
                    if image.is_file() and snapshot is not None:
                        # unlink only once the copy is safely in the trash —
                        # a figure left in place is a harmless orphan, one
                        # deleted without a snapshot is gone
                        try:
                            dst = snapshot / "ocr" / "images" / name
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(image, dst)
                        except OSError:
                            continue
                        if snapshot_out is not None:
                            snapshot_out.append(f"ocr/images/{name}")
                        image.unlink(missing_ok=True)
                else:
                    info["page"] = n - sum(1 for r in removed if r < n)
                dirty = True
        if dirty:
            lib.save_json(meta_path, meta)


def _renumber_translation_artifacts(build_id: str, src_key: str,
                                    removed: list[int],
                                    snapshot: Path | None = None,
                                    snapshot_out: list[str] | None = None
                                    ) -> list[str]:
    """Keep translated text and its source-hash sidecars page-aligned.
    Returns the entry-relative paths of the renumbered translations.

    `snapshot` is a trash payload directory: each file is copied there VERBATIM
    before it is rewritten, and its "translations/<name>" key appended to
    `snapshot_out`. The copy happens here rather than in the caller because
    only this function knows which translations belong to `src_key` — a
    translation of a different source doesn't renumber and must not be
    snapshotted, or restore would write back a file the delete never touched.
    """
    d = _entry_dir(build_id) / "translations"
    renumbered: list[str] = []
    if not d.is_dir():
        return renumbered
    srcmap = _ocr_sources(build_id)
    removed_set = set(removed)

    def _snap(p: Path) -> None:
        """Verbatim pre-image into the trash payload. Best-effort: a snapshot
        that can't be written must not abort the alignment pass — restore just
        reports that file as skipped instead."""
        if snapshot is None:
            return
        try:
            (snapshot / "translations").mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, snapshot / "translations" / p.name)
            if snapshot_out is not None:
                snapshot_out.append(f"translations/{p.name}")
        except OSError:
            pass

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
            _snap(text_path)

            raw = text_path.read_text(encoding="utf-8", errors="replace")
            text_path.write_text(_renumber_marked_text(raw, removed),
                                 encoding="utf-8", errors="replace")

            if not meta_path.is_file():
                continue
            _snap(meta_path)
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


def _page_remark_ref_parts(ref: str) -> tuple[str, str, int, str, str] | None:
    """Decode a JS ``page:`` remark ref while retaining its exact encoding."""
    raw = str(ref or "")
    if not raw.startswith("page:"):
        return None
    parts = raw[5:].split("|")
    if len(parts) != 3:
        return None
    if any(re.search(r"%(?![0-9A-Fa-f]{2})", part) for part in parts):
        return None
    try:
        build_id = urllib.parse.unquote(parts[0], errors="strict")
        source_id = urllib.parse.unquote(parts[1], errors="strict")
        page_text = urllib.parse.unquote(parts[2], errors="strict")
        page = int(page_text)
    except (UnicodeDecodeError, ValueError):
        return None
    if not build_id or not source_id or page < 1 or str(page) != page_text:
        return None
    return build_id, source_id, page, parts[0], parts[1]


def _remapped_page_remark_ref(ref: str, build_id: str, source_id: str,
                              removed: list[int]) -> tuple[bool, str | None]:
    """Return (matched, shifted-ref); ``None`` means its page was deleted."""
    parsed = _page_remark_ref_parts(ref)
    if not parsed or parsed[:2] != (str(build_id), str(source_id or "primary")):
        return False, ref
    _, _, page, encoded_build, encoded_source = parsed
    removed_set = set(removed)
    if page in removed_set:
        return True, None
    shifted = page - sum(1 for old in removed if old < page)
    return True, f"page:{encoded_build}|{encoded_source}|{shifted}"


_PAGE_REMARK_LABEL_RE = re.compile(r"(\s+\u00b7\s*page\s+)(\d+)$", re.IGNORECASE)


def _remapped_page_remark_value(value, old_page: int, new_page: int):
    """Keep a generated fallback label aligned with its remapped page key."""
    if not isinstance(value, dict) or not isinstance(value.get("label"), str):
        return value
    label = value["label"]
    match = _PAGE_REMARK_LABEL_RE.search(label)
    if not match or int(match.group(2)) != old_page:
        return value
    updated = dict(value)
    updated["label"] = label[:match.start(2)] + str(new_page)
    return updated


def _remap_page_attention_references(build_id: str, source_id: str,
                                     removed: list[int],
                                     dropped: dict | None = None) -> list[str]:
    """Keep personal page marks and shared threads bound to physical pages.

    Deleted personal marks disappear with their target. Deleted shared threads
    become unique, unroutable tombstones so their comments remain reachable
    without ever attaching to the page that shifted into the old number.

    ``dropped`` collects the personal marks this drops, keyed by bucket, so a
    restore can put them back: a surviving mark can be shifted back
    arithmetically, but one whose page was deleted is popped with no record and
    would be gone for good. Threads need no snapshot — the tombstone carries
    its own original ref (see _unremap_page_attention_references).
    """
    removed = sorted({int(page) for page in removed if int(page) > 0})
    if not removed:
        return []
    warnings = []

    try:
        with _client_state_lock:
            client = lib.load_json(lib.CLIENT_STATE_PATH, {})
            attention = client.get("attention")
            settings = client.get("settings")
            meta = settings.get("remarksMeta") if isinstance(settings, dict) else None
            dirty = False

            def remap_map(values: dict | None, bucket: str = "") -> bool:
                if not isinstance(values, dict):
                    return False
                moves: list[tuple[str, str | None, object]] = []
                for key, value in list(values.items()):
                    old_parts = _page_remark_ref_parts(key)
                    matched, new_key = _remapped_page_remark_ref(
                        key, build_id, source_id, removed)
                    if matched and new_key != key:
                        if old_parts and new_key:
                            new_parts = _page_remark_ref_parts(new_key)
                            if new_parts:
                                value = _remapped_page_remark_value(
                                    value, old_parts[2], new_parts[2])
                        moves.append((key, new_key, value))
                for old_key, new_key, value in moves:
                    # a mark whose page is going away is popped with no record;
                    # keep a copy so the trash can put it back
                    if new_key is None and dropped is not None and bucket:
                        dropped.setdefault(bucket, {})[old_key] = \
                            values.get(old_key)
                    values.pop(old_key, None)
                for _, new_key, value in moves:
                    if new_key:
                        values[new_key] = value
                return bool(moves)

            dirty = remap_map(attention, "attention") or dirty
            dirty = remap_map(meta, "meta") or dirty
            if dirty:
                client["updated_at"] = datetime.now(timezone.utc).isoformat(
                    timespec="seconds")
                lib.save_json(lib.CLIENT_STATE_PATH, client)
    except Exception as exc:
        log.warning("could not remap page attention state: %s", exc)
        warnings.append("personal attention marks could not be renumbered")

    try:
        with _reviews_lock:
            reviews = lib.load_json(REVIEWS_PATH, {})
            dirty = False
            for review in reviews.values():
                if not isinstance(review, dict) or review.get("kind") != "key":
                    continue
                old_ref = str(review.get("ref") or "")
                parsed = _page_remark_ref_parts(old_ref)
                matched, new_ref = _remapped_page_remark_ref(
                    old_ref, build_id, source_id, removed)
                if not matched or new_ref == old_ref:
                    continue
                old_page = parsed[2] if parsed else 0
                if new_ref is None:
                    # Include the immutable review id so deleting page N again
                    # later cannot merge two unrelated historical threads.
                    new_ref = "page-deleted:" + old_ref[5:] + "|" + \
                        urllib.parse.quote(str(review.get("id") or uuid.uuid4()), safe="")
                    if not str(review.get("label") or "").endswith(" · removed"):
                        review["label"] = str(review.get("label") or "") + " · removed"
                    # Record WHICH threads this particular delete tombstoned.
                    # A tombstone is identified only by (build, source, page),
                    # so without this a restore would resurrect every thread
                    # ever tombstoned on that page number — undoing the very
                    # separation the review id above exists to create.
                    if dropped is not None:
                        dropped.setdefault("reviews", []).append(
                            str(review.get("id") or ""))
                else:
                    new_page = _page_remark_ref_parts(new_ref)[2]
                    label = str(review.get("label") or "")
                    review["label"] = re.sub(
                        r"(\s·\s[Pp]age\s+)" + str(old_page) +
                        r"(?=\s·\sSource\b|$)",
                        lambda match: match.group(1) + str(new_page), label)
                review["ref"] = new_ref
                review["key"] = "key:" + new_ref
                dirty = True
            if dirty:
                lib.save_json(REVIEWS_PATH, reviews)
    except Exception as exc:
        log.warning("could not remap page review references: %s", exc)
        warnings.append("shared review threads could not be renumbered")
    return warnings


def _unremap_page_attention_references(build_id: str, source_id: str,
                                       removed: list[int], pages_before: int,
                                       dropped: dict | None = None) -> list[str]:
    """Undo _remap_page_attention_references after a page restore.

    Without this a restore rebuilt the PDF and its text but left every mark and
    thread past the deletion point one page off, and reported full success.

    Two halves. A reference that SURVIVED the delete shifts back
    arithmetically: the page now numbered N was originally the Nth survivor, so
    the map is just the survivor list. A reference whose page was DELETED needs
    its pre-delete form — for a personal mark that comes from the trash payload
    (it was popped with no record), and for a thread from the tombstone itself,
    which was built as "page-deleted:<original ref tail>|<review id>".
    """
    removed_set = {int(p) for p in removed if int(p) > 0}
    survivors = [p for p in range(1, int(pages_before) + 1)
                 if p not in removed_set]
    mine = {str(x) for x in ((dropped or {}).get("reviews") or [])}
    warnings: list[str] = []

    def original(page: int) -> int | None:
        return survivors[page - 1] if 1 <= page <= len(survivors) else None

    try:
        with _client_state_lock:
            client = lib.load_json(lib.CLIENT_STATE_PATH, {})
            settings = client.get("settings")
            buckets = {
                "attention": client.get("attention"),
                "meta": settings.get("remarksMeta")
                if isinstance(settings, dict) else None,
            }
            dirty = False
            for name, values in buckets.items():
                if not isinstance(values, dict):
                    # the live map can vanish (a client PUT replaces `settings`
                    # wholesale). Skipping here silently discarded the payload
                    # and still reported success, so materialize it instead —
                    # but only when there is something of ours to put back.
                    if not ((dropped or {}).get(name) or {}):
                        continue
                    values = {}
                    if name == "attention":
                        client["attention"] = values
                    else:
                        # `settings` can be missing outright, not just missing
                        # its remarksMeta — materialize both rather than
                        # leaving the same hole open for the other bucket
                        if not isinstance(settings, dict):
                            settings = client.setdefault("settings", {})
                        settings["remarksMeta"] = values
                moves = []
                for key, value in list(values.items()):
                    parts = _page_remark_ref_parts(key)
                    if not parts or parts[:2] != (str(build_id),
                                                  str(source_id or "primary")):
                        continue
                    was = original(parts[2])
                    if was is None or was == parts[2]:
                        continue
                    moves.append((key, f"page:{parts[3]}|{parts[4]}|{was}",
                                  _remapped_page_remark_value(
                                      value, parts[2], was)))
                # A key ABOVE the post-delete page count has no pre-delete
                # original, so it is never moved — but it is still a valid
                # TARGET for another mark shifting back, and writing over it
                # would delete a mark while reporting success. (Reachable when
                # a stale client flushes a pre-delete attention map, since the
                # PUT replaces the whole blob.) Leave both alone and say so.
                # Blocking CASCADES: dropping a move leaves that mark at its
                # old key, which turns the key into an occupied one, which can
                # in turn block the mark shifting into it. Iterate to a
                # fixpoint — stopping after one pass still destroyed a mark,
                # while reporting that a different one had been preserved.
                staying = set(values) - {old for old, _, _ in moves}
                blocked = []
                spreading = True
                while spreading:
                    spreading = False
                    for m in list(moves):
                        if m[1] in staying:
                            moves.remove(m)
                            blocked.append(m)
                            staying.add(m[0])
                            spreading = True
                if blocked:
                    warnings.append(
                        f"{len(blocked)} attention mark(s) kept their current "
                        "page — something else already sits where they belong")
                for old_key, _, _ in moves:
                    values.pop(old_key, None)
                for _, new_key, value in moves:
                    values[new_key] = value
                # then the marks the delete dropped outright, straight back at
                # their original keys — they were never shifted, just removed
                for key, value in ((dropped or {}).get(name) or {}).items():
                    values.setdefault(key, value)
                    dirty = True
                dirty = bool(moves) or dirty
            if dirty:
                client["updated_at"] = datetime.now(timezone.utc).isoformat(
                    timespec="seconds")
                lib.save_json(lib.CLIENT_STATE_PATH, client)
    except Exception as exc:
        log.warning("could not restore page attention state: %s", exc)
        warnings.append("personal attention marks could not be put back")

    try:
        with _reviews_lock:
            reviews = lib.load_json(REVIEWS_PATH, {})
            dirty = False
            for review in reviews.values():
                if not isinstance(review, dict) or review.get("kind") != "key":
                    continue
                ref = str(review.get("ref") or "")
                new_ref = None
                if ref.startswith("page-deleted:"):
                    # "page-deleted:<build>|<source>|<page>|<quoted review id>"
                    tail = ref[len("page-deleted:"):].rsplit("|", 1)[0]
                    cand = "page:" + tail
                    parts = _page_remark_ref_parts(cand)
                    # Only the threads THIS delete tombstoned. A tombstone is
                    # otherwise identified just by (build, source, page), so
                    # deleting page 3 twice and restoring the second row would
                    # also resurrect the first row's thread — stranding it on
                    # another page's content and leaving two threads sharing a
                    # key, where re-flagging the page silently edits whichever
                    # one dict order happens to yield.
                    if parts and parts[:2] == (str(build_id),
                                               str(source_id or "primary")) \
                            and parts[2] in removed_set \
                            and str(review.get("id") or "") in mine:
                        new_ref = cand
                        label = str(review.get("label") or "")
                        if label.endswith(" · removed"):
                            review["label"] = label[:-len(" · removed")]
                else:
                    parts = _page_remark_ref_parts(ref)
                    if parts and parts[:2] == (str(build_id),
                                               str(source_id or "primary")):
                        was = original(parts[2])
                        if was is not None and was != parts[2]:
                            new_ref = f"page:{parts[3]}|{parts[4]}|{was}"
                            review["label"] = re.sub(
                                r"(\s·\s[Pp]age\s+)" + str(parts[2]) +
                                r"(?=\s·\sSource\b|$)",
                                lambda m: m.group(1) + str(was),
                                str(review.get("label") or ""))
                if not new_ref:
                    continue
                review["ref"] = new_ref
                review["key"] = "key:" + new_ref
                dirty = True
            if dirty:
                lib.save_json(REVIEWS_PATH, reviews)
    except Exception as exc:
        log.warning("could not restore page review references: %s", exc)
        warnings.append("shared review threads could not be put back")
    return warnings


@app.route("/api/pdf/pages/delete", methods=["POST"])
def api_pdf_pages_delete():
    """Delete pages from a build's PDF — the real file, not a preview.
    Body: {build_id, pdf, pages: [1-based numbers], page_revision}.

    The removed pages (plus whole copies of every collateral file the
    renumbering rewrites) go to the trash under output/trash/, listed and
    restorable from the Info tab. The build's OCR files get their page
    markers renumbered and title_pages is remapped, so everything stays
    aligned with the new page numbering."""
    p = request.get_json(silent=True) or {}
    build_id = str(p.get("build_id") or "")
    expected_revision = str(p.get("page_revision") or "")
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
        result = _apply_page_deletion(
            build_id, builds, pdf, pages,
            expected_revision=expected_revision)
    except _PageStructureConflict as exc:
        return jsonify({"ok": False, "error": str(exc),
                        "conflict": exc.code}), 409
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    result["ok"] = True
    return jsonify(result)


def _apply_page_deletion(build_id: str, builds: dict, pdf: Path,
                         pages: list[int],
                         expected_revision: str | None = None) -> dict:
    """Guard the page structure while every page-keyed derivative is remapped."""
    with _page_structure_lock:
        if expected_revision is not None:
            # Reject a stale grid or detached path before even inspecting a
            # caller-selected file. The reservation inside the locked rewrite
            # rechecks both after PDF validation, closing the metadata-race gap.
            _validate_page_deletion(
                build_id, pdf, expected_revision)
        if _page_job_blockers(build_id):
            raise ValueError("a page-processing job is running for this book")
        result = _apply_page_deletion_locked(
            build_id, builds, pdf, pages,
            expected_revision=expected_revision)
    refresh = result.pop("_representation_refresh", None)
    if refresh is not None:
        source_id, source_token, expected_item_revision = refresh
        try:
            _engine_refresh_representation_reference(
                build_id,
                source_id,
                source_token,
                operation_scope="page-delete-source-refresh",
                expected_item_revision=expected_item_revision,
            )
            current = lib.load_json(BUILDS_PATH, {}).get(build_id)
            if isinstance(current, dict):
                builds[build_id] = current
                result["build"] = current
        except EngineError as exc:
            log.warning(
                "could not refresh representation after page deletion: %s",
                exc.code,
            )
            warnings = result.setdefault("warnings", [])
            warnings.append(
                "source integrity metadata could not be refreshed"
            )
            result["partial"] = True
    return result


def _apply_page_deletion_locked(build_id: str, builds: dict, pdf: Path,
                                pages: list[int],
                                expected_revision: str | None = None) -> dict:
    """Rewrite the PDF without the given pages, renumber the build's OCR
    files, and remap title_pages. Shared by the deletion endpoint and
    blank-page trimming. Raises ValueError only before the live PDF
    replacement; later derivative failures return partial warnings.

    Everything this destroys goes to the trash first (see _trash_open): the
    dropped pages as their own small PDF, plus WHOLE copies of each collateral
    file the renumbering rewrites. Whole copies are what let restore be a
    verbatim write-back instead of an inverse-renumber function, and they
    replace the ad-hoc .bak siblings this function used to leave behind.
    """
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(pdf))
    total = len(reader.pages)
    keep = [i for i in range(total) if (i + 1) not in set(pages)]
    if not keep:
        raise ValueError("cannot delete every page")
    if len(keep) == total:
        raise ValueError("pages out of range")
    # Resolve every source-scoped input before the live PDF changes. Failures
    # here remain ordinary refusals; after tmp.replace(pdf), the deletion has
    # committed and derivative failures must be reported as partial success.
    if expected_revision is None:
        # Direct helper callers predate the HTTP concurrency contract. Keep
        # their in-memory/test behavior, while every destructive request path
        # supplies a revision and takes the strict reservation path below.
        b = builds[build_id]
        src_key = _src_key_for_path(b, pdf)
        build_persisted = build_id in lib.load_json(BUILDS_PATH, {})
    else:
        b, src_key = _reserve_page_deletion(
            build_id, pdf, expected_revision)
        builds[build_id] = b
        build_persisted = True
    reserved_item_revision = _engine_build_record_revision(build_id, b)
    actual_pages = [page for page in pages if page <= total]
    srcmap = _ocr_sources(build_id)
    ocr_dir = _entry_dir(build_id) / "ocr"
    # Trash the pre-image BEFORE the rewrite, in place of the old .bak.pdf
    # sibling. The payload is the DROPPED pages (the exact complement of
    # `keep`), so a 130MB scan losing 3 pages costs a few MB — that is what
    # makes a 30-day window affordable. Small PDFs also keep a byte-identical
    # original, which is what the .bak.pdf used to provide at every size.
    tid, tdir = _trash_open()
    tfiles: list[str] = []
    drop_writer = PdfWriter()
    for i in range(total):
        if (i + 1) in set(pages):
            drop_writer.add_page(reader.pages[i])
    with open(tdir / "pages.pdf", "wb") as fh:
        drop_writer.write(fh)
    tfiles.append("pages.pdf")
    payload_kind = "pages"
    try:
        if pdf.stat().st_size <= _TRASH_FULL_COPY_MAX:
            shutil.copy2(pdf, tdir / "original.pdf")
            tfiles.append("original.pdf")
            payload_kind = "full"
    except OSError:
        pass
    writer = PdfWriter()
    for i in keep:
        writer.add_page(reader.pages[i])
    tmp = pdf.with_suffix(".del.tmp")
    with open(tmp, "wb") as fh:
        writer.write(fh)
    # Hash the exact successor bytes before replacing the live file. Restore
    # must pin lineage, not merely page count: an unrelated PDF can have the
    # same number of pages. A hashing failure here is still pre-commit and
    # therefore leaves the source untouched.
    post_delete_sha256 = _file_sha256(tmp)
    # Record the PDF that is actually being edited. "primary" is also
    # _src_key_for_path's catch-all for a resolve error or no match at all, so
    # verify rather than assume: pointing the row at pdf_file for a file that
    # is not it makes restore splice these pages into the wrong scan and write
    # the result over it. Prefer the STORED path string so the row survives a
    # DATA_ROOT move.
    src_path = str(pdf)
    if src_key == "primary":
        prim = _resolve_local(str(b.get("pdf_file") or ""))
        try:
            if prim is not None and prim.resolve() == pdf.resolve():
                src_path = str(b.get("pdf_file") or "")
        except OSError:
            pass
    else:
        for s in (b.get("pdf_sources") or []):
            if str(s.get("id") or "") == src_key and s.get("path"):
                src_path = str(s.get("path"))
                break
    tmp.replace(pdf)
    # The live page grid has committed. Advance the process-local job token
    # immediately, before any best-effort derivative work can fail, so a job
    # prepared against the old numbering can never start afterward.
    _page_structure_revision[build_id] = (
        _page_structure_revision.get(build_id, 0) + 1)
    # REGISTER THE TRASH ROW NOW, not at the end. The pages are gone from disk
    # as of the replace above; if anything below raises, the row must already
    # exist or the payload becomes an unregistered orphan that the next prune
    # silently removes — losing the very pages this feature exists to keep.
    # The collateral snapshots are folded in by _trash_amend in the finally.
    label = (f"{len(pages)} page{'' if len(pages) == 1 else 's'} from "
             f"{b.get('title') or build_id}")
    _trash_commit(
        tid, "pdf_pages", label,
        {"build_id": build_id, "pdf": src_path, "src_key": src_key},
        {"pages": pages, "pages_before": total, "pages_after": len(keep),
         "pdf_after_sha256": post_delete_sha256,
         "title_pages_before": str(b.get("title_pages") or ""),
         "thumbnail_source_before": str(b.get("thumbnail_source") or "")},
        payload_kind, list(tfiles))
    # keep the build's OCR files and title pages aligned with the new
    # numbering (under the merge lock: a job finishing this instant must
    # not interleave with the renumber writes). Only the files that came
    # FROM this PDF renumber — a secondary scan's OCR has its own page
    # numbering and must not shift with the primary's deletions.
    # ``pages`` historically reports the caller's request (including a mixed
    # out-of-range number). Structural references must use only pages that
    # actually existed, or a stale high page mark could shift spuriously.
    renumbered = []
    changed = {}
    stamps = {}
    warnings = []
    try:
        failed_ocr = []
        try:
            with _ocr_merge_lock:
                if ocr_dir.is_dir():
                    for f in ocr_dir.glob("*.txt"):
                        if (srcmap.get(f.name) or "primary") != src_key:
                            continue
                        try:
                            raw = f.read_text(encoding="utf-8", errors="replace")
                            # the renumbering is destructive too — snapshot the
                            # WHOLE pre-edit text into the same trash item, so
                            # restoring is a verbatim write-back and never an
                            # inverse renumber (this replaces the .txt.bak)
                            snap = tdir / "ocr" / f.name
                            snap.parent.mkdir(parents=True, exist_ok=True)
                            snap.write_text(raw, encoding="utf-8",
                                            errors="replace")
                            tfiles.append(f"ocr/{f.name}")
                            out = _renumber_marked_text(raw, pages)
                            f.write_text(out, encoding="utf-8", errors="replace")
                            renumbered.append(f.name)
                        except OSError as exc:
                            log.warning("could not renumber OCR text %s: %s", f, exc)
                            failed_ocr.append(f.name)
        except Exception as exc:
            log.warning("could not enumerate OCR text for page renumbering: %s", exc)
            warnings.append("OCR text documents could not be checked for renumbering")
        if failed_ocr:
            warnings.append("OCR text could not be renumbered: " +
                            ", ".join(sorted(failed_ocr)))
        # the OCR word-box sidecar is page-keyed per source like the compiled
        # files; keep THIS source's boxes aligned so the placed facsimile never
        # shows a deleted page's words. This is the ONE leg of the flow that had
        # no backup of any kind before the trash — snapshot it whole first.
        layout_path = ocr_dir / "layout.json"
        if layout_path.is_file():
            try:
                snap = tdir / "ocr" / "layout.json"
                snap.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(layout_path, snap)
                tfiles.append("ocr/layout.json")
            except OSError:
                pass
        layout_ok = False
        try:
            _renumber_layout_words(build_id, src_key, pages,
                                   snapshot=tdir, snapshot_out=tfiles)
            layout_ok = True
        except Exception as exc:
            log.warning("could not renumber OCR page layout: %s", exc)
            warnings.append("OCR page layout could not be renumbered")
        # Translations use the same page-marker convention, and their provenance
        # sidecars key each source hash by page. Move both in one protected pass
        # so stale detection and eventual publication never retain an obsolete
        # tail. Each affected file is snapshotted WHOLE on its way through:
        # without that, a restore rebuilt the PDF and the OCR but left every
        # translated page past the deletion shifted by one, and said "ok" with
        # nothing in `skipped`.
        moved = []
        try:
            moved = _renumber_translation_artifacts(
                build_id, src_key, pages, snapshot=tdir, snapshot_out=tfiles)
        except Exception as exc:
            log.warning("could not renumber translation artifacts: %s", exc)
            warnings.append("translations could not be fully renumbered")
        edited = [f"ocr/{name}" for name in renumbered]
        if layout_ok:
            edited.append("ocr/layout.json")
        try:
            _manifest_after_renumber(build_id, edited, moved)
        except Exception as exc:
            log.warning("could not refresh artifact manifest after page deletion: %s",
                        exc)
            warnings.append("artifact provenance could not be refreshed")
        # ``pages`` historically reports the caller's request (including a mixed
        # out-of-range number). Structural references must use only pages that
        # actually existed, or a stale high page mark could shift spuriously.
        # Runs AFTER the file renumbering: it is the one derivative step with no
        # try/except of its own, so going last means a failure here cannot leave
        # the others half-done, and everything snapshotted before it is already
        # in the trash row by the time the finally folds it in.
        att_dropped: dict = {}
        warnings.extend(_remap_page_attention_references(
            build_id, src_key, actual_pages, dropped=att_dropped))
        if att_dropped:
            # marks the remap popped outright; restore cannot derive these
            try:
                lib.save_json(tdir / "attention.json", att_dropped)
                tfiles.append("attention.json")
            except OSError:
                warnings.append("attention marks could not be kept for undo")
        # title pages are counted on the PRIMARY PDF; a secondary's deletions
        # don't move them. The pre-remap values are already in the row committed
        # above (b has not been rewritten yet at this point).
        titles = [] if src_key != "primary" else \
            [int(x) for x in str(b.get("title_pages") or "").split(",")
             if x.strip().isdigit()]
        if titles:
            remapped = []
            for t_page in titles:
                if t_page in set(pages):
                    continue
                remapped.append(t_page - sum(1 for r in pages if r < t_page))
            changed["title_pages"] = ",".join(str(x) for x in remapped)
        # thumbnail_source references a primary-PDF page the same way title_pages
        # does ("page:<n>") — remap it the same way, or clear it if the referenced
        # page was itself deleted. An "image:<name>" source points at an OCR-
        # extracted figure, not a PDF page, so page deletion never touches it.
        if src_key == "primary":
            m = re.match(r"^page:(\d+)$", str(b.get("thumbnail_source") or ""))
            if m:
                t_page = int(m.group(1))
                changed["thumbnail_source"] = "" if t_page in set(pages) else \
                    f"page:{t_page - sum(1 for r in pages if r < t_page)}"
        # A page-grid change is itself a build revision even when no title-page or
        # thumbnail field moved. Page review creation uses this token to reject a
        # stale popover that finishes after the deletion remap.
        b.update(changed)
        if changed or build_persisted:
            try:
                revision = _builds_apply(
                    build_id,
                    changed,
                    expected_revision=reserved_item_revision,
                )
                if revision:
                    b["updated_at"] = revision
                else:
                    warnings.append("build metadata could not be saved")
                    changed = {}
            except Exception as exc:
                log.warning("could not persist build metadata after page deletion: %s",
                            exc)
                warnings.append("build metadata could not be saved")
                # the *_after values recorded from `changed` are restore's
                # "is this still what the delete left?" test. If the save never
                # landed, the live field still holds the PRE-delete value, so
                # recording an _after that only ever existed in memory would
                # make every restore report a phantom "edited since the delete".
                changed = {}
    finally:
        # Fold the collateral snapshots into the row registered above, stamping
        # each written-back-able one with the POST-delete (size, mtime) of its
        # live file so restore can tell "untouched since" from "the user edited
        # this afterwards". Same idea for the build fields via `changed`. In the
        # finally so the row is never left describing less than the payload
        # holds: restore iterates `files`, so a frozen list would write nothing
        # back and still report ok with an EMPTY skipped list.
        for rel in tfiles:
            if not rel.startswith(_TRASH_WRITEBACK):
                continue
            live = _entry_dir(build_id) / rel
            try:
                st = live.stat()
                stamps[rel] = [st.st_size, st.st_mtime]
            except OSError:
                stamps[rel] = []      # absent now: see restore's guard
        _trash_amend(tid, tfiles, stamps,
                     {f"{k}_after": v for k, v in changed.items()})
    result = {"deleted": pages, "pages": len(keep),
              "renumbered": renumbered,
              "trash_id": tid,
              "page_remap": {"source": src_key, "deleted": actual_pages},
              "build": b}
    if build_persisted:
        result["_representation_refresh"] = (
            src_key,
            src_path,
            _engine_build_record_revision(build_id, b),
        )
    if warnings:
        result["partial"] = True
        result["warnings"] = warnings
    return result


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
    Body: {spreadsheet_id, sheet_name?}. Requires a
    Google service-account JSON key — TODO: verify once the user has one."""
    p = request.get_json(silent=True) or {}
    sheet_id = str(p.get("spreadsheet_id") or "").strip()
    sheet_name = str(p.get("sheet_name") or "Master list").strip()
    if not sheet_id or not _secret_is_configured("gsKeyFile"):
        return jsonify({"ok": False,
                        "error": "Spreadsheet ID and service-account key file "
                                 "are required (Settings > Integrations; key "
                                 "file under Credentials)"})
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
        with _lease_secret("gsKeyFile") as keyfile:
            kf = _resolve_local(keyfile)
            if kf is None or not kf.is_file():
                return jsonify({"ok": False,
                                "error": "service-account key file not found"})
            creds = service_account.Credentials.from_service_account_file(
                str(kf), scopes=["https://www.googleapis.com/auth/spreadsheets"])
            svc = gbuild("sheets", "v4", credentials=creds)
            svc.spreadsheets().values().update(
                spreadsheetId=sheet_id, range=f"{sheet_name}!A1",
                valueInputOption="RAW", body={"values": rows}).execute()
        return jsonify({"ok": True, "rows": len(rows) - 1})
    except Exception:
        return jsonify({"ok": False, "error": "Google Sheets sync failed"})


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
            # Extraction may be slow (and a remote URL may be involved), so
            # take the lifecycle lease only for final publication.  Re-read
            # membership inside the lease before creating the entry tree.
            with _live_item_write_scope(bid) as live_build:
                name = _ocr_name(
                    request.args.get("save_name") or "extracted.txt"
                )
                f = _entry_dir(bid) / "ocr" / name
                if not f.is_file():
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(
                        out["text"], encoding="utf-8", errors="replace"
                    )
                    src_key = _valid_src_key(
                        live_build, request.args.get("src")
                    )
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
    state = lib.load_json(lib.CLIENT_STATE_PATH, {})
    # Startup normally migrates secrets out of this file, but keep GET safe for
    # legacy state and non-standard server entry points that have not run that
    # migration yet. Work on a detached copy; the migration remains responsible
    # for repairing the file on disk.
    if isinstance(state, dict) and isinstance(state.get("settings"), dict):
        state = dict(state)
        state["settings"] = dict(state["settings"])
        for _sk in _SECRET_KEYS:
            state["settings"].pop(_sk, None)
    return jsonify(state)


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
            # A stale tab/offline cache may predate a collection merge. Heal
            # every incoming book at the whole-blob write boundary so a loser
            # id cannot be reintroduced after the merge repoint completed.
            for pair in new_checked:
                value = pair[1] if isinstance(pair, list) and len(pair) == 2 else None
                book = value.get("book") if isinstance(value, dict) else None
                _canonicalize_collection_link(book)
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


def _manual_extra_patch(value, existing=None) -> dict:
    """Clean generic metadata without letting it edit capture provenance.

    Phone ingest writes the snapshot once, and the private merge helper may
    repoint only its id. Generic manual-entry create/PATCH callers can neither
    forge nor remove Collection/From/id, even if they bypass the renderer.
    """
    cleaned = _clean_extra(value)
    prior = existing if isinstance(existing, dict) else {}
    for key in PHONE_PROVENANCE_KEYS:
        cleaned.pop(key, None)
        if key in prior:
            cleaned[key] = prior[key]
    return cleaned


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
        entry["extra"] = _manual_extra_patch(payload.get("extra"))
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
            e["extra"] = _manual_extra_patch(payload.get("extra"), e.get("extra"))
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
        _canonicalize_collection_link(entry)
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
        record = entries[entry_id]
        title = record.get("title", "")
        del entries[entry_id]
        lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)
    _trash_put("manual_entry", f"Manual entry: {title or entry_id}",
               {"entry_id": entry_id}, {},
               {"record.json": json.dumps(record, ensure_ascii=False)})
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

# Cloud config lives in client_state, but credential material does not.  The
# public engine service below exposes only fixed-length masked status and
# conditional mutation receipts.  Plaintext access is a separate, sidecar-
# private lease held only around a provider invocation.
_SECRET_IDS = LEGACY_SECRET_IDS
_SECRET_KEYS = LEGACY_SECRET_KEYS
_SECRET_INITIAL_REVISIONS = {
    secret_id: f"absent-v1-{legacy_key.lower()}"
    for legacy_key, secret_id in _SECRET_IDS.items()
}
_SECRET_REGISTRY = SecretIdRegistry(_SECRET_INITIAL_REVISIONS)
_SECRET_STORE_ID = "librarytool.desktop.current-user.v1"
_PROTECTED_SECRETS_PATH = lib.OUTPUT_DIR / "secrets.dpapi"

# Read-only legacy inputs.  This file is never written again.
_SECRETS_PATH = lib.OUTPUT_DIR / "secrets.json"
_MISTRAL_PENDING = "_mistralCloudPending"
_SECRET_SYNC_STATE_PATH = lib.OUTPUT_DIR / "secret_sync_state.json"
_MISTRAL_SYNC_SCHEMA = "librarytool.mistral-profile-sync/2"
_MISTRAL_SYNC_KEY = "mistral"
_MISTRAL_STABLE_PHASES = frozenset({"synced", "pending", "unowned", "blocked"})
_MISTRAL_OWNED_PHASES = frozenset({"synced", "pending"})
_LEGACY_RENDERER_SECRET_HEADER = "X-WHL-Secret-Source"
_LEGACY_RENDERER_SECRET_SOURCE = "legacy-renderer-local-storage-v1"
_secrets_lock = threading.RLock()
_secret_cutover_lock = threading.RLock()
_mistral_sync_lock = threading.RLock()
_secret_repository: WindowsDpapiSecretStoreRepository | None = None
_secret_cutover_complete = False


class ProtectedSecretCutoverError(RuntimeError):
    """Sanitized startup failure; never includes a path or credential."""


def _new_secret_repository() -> WindowsDpapiSecretStoreRepository:
    return WindowsDpapiSecretStoreRepository(
        _PROTECTED_SECRETS_PATH,
        registry=_SECRET_REGISTRY,
        store_id=_SECRET_STORE_ID,
    )


def _secret_store_bindings() -> SecretStoreBindings:
    global _secret_repository
    if _secret_repository is None:
        _secret_repository = _new_secret_repository()
    return SecretStoreBindings(_secret_repository)


def _legacy_secret_document() -> dict:
    value = lib.load_json(_SECRETS_PATH, {})
    return value if isinstance(value, dict) else {}


def _secret_sync_state() -> dict:
    value = lib.load_json(_SECRET_SYNC_STATE_PATH, {})
    return value if isinstance(value, dict) else {}


def _mistral_account_id(value) -> str | None:
    """Return one safe opaque Supabase user id, or no account identity."""

    if not isinstance(value, str):
        return None
    account_id = value.strip()
    if (
        not account_id
        or len(account_id) > 255
        or any(ord(character) < 32 or ord(character) == 127
               for character in account_id)
    ):
        return None
    return account_id


def _active_mistral_account_id() -> str | None:
    """Read the stored account identity without refreshing or using a token."""

    session = _auth_doc().get("session")
    if not isinstance(session, Mapping) or not session.get("refresh_token"):
        return None
    return _mistral_account_id(session.get("user_id"))


def _mistral_stable_record(
        phase: str, revision: str, *, owner_user_id: str | None = None) -> dict:
    record = {"phase": phase, "revision": revision}
    if owner_user_id is not None:
        record["owner_user_id"] = owner_user_id
    return record


def _valid_mistral_stable_record(value) -> dict | None:
    if not isinstance(value, Mapping):
        return None
    phase = value.get("phase")
    revision = value.get("revision")
    if phase not in _MISTRAL_STABLE_PHASES or not isinstance(revision, str) \
            or not revision:
        return None
    owner = _mistral_account_id(value.get("owner_user_id"))
    if phase in _MISTRAL_OWNED_PHASES:
        if owner is None or set(value) != {"phase", "revision", "owner_user_id"}:
            return None
        return _mistral_stable_record(
            phase, revision, owner_user_id=owner)
    if phase == "unowned":
        if set(value) != {"phase", "revision"}:
            return None
        return _mistral_stable_record(phase, revision)
    # A blocked record may retain the last known owner for diagnostics and a
    # deliberate replacement, but it never authorizes a lease or an upload.
    if set(value) not in (
        {"phase", "revision"},
        {"phase", "revision", "owner_user_id"},
    ):
        return None
    return _mistral_stable_record(
        phase, revision, owner_user_id=owner) if owner else \
        _mistral_stable_record(phase, revision)


def _save_mistral_sync_record(record: dict | None) -> None:
    """Atomically persist redacted Mistral ownership/sync metadata."""

    state = _secret_sync_state()
    state.pop("mistral_pending", None)  # v1 marker had no safe account owner
    state.pop(_MISTRAL_PENDING, None)
    if record is None:
        state.pop(_MISTRAL_SYNC_KEY, None)
        state.pop("schema", None)
    else:
        state["schema"] = _MISTRAL_SYNC_SCHEMA
        state[_MISTRAL_SYNC_KEY] = record
    if state:
        lib.save_json(_SECRET_SYNC_STATE_PATH, state)
    else:
        _SECRET_SYNC_STATE_PATH.unlink(missing_ok=True)


def _mistral_record_from_document() -> dict | None:
    state = _secret_sync_state()
    if state.get("schema") != _MISTRAL_SYNC_SCHEMA:
        return None
    record = state.get(_MISTRAL_SYNC_KEY)
    if isinstance(record, Mapping):
        return dict(record)
    return None


def _mistral_prepared_record(
        *, action: str, operation_id: str, before_revision: str,
        target_phase: str, target_owner_user_id: str | None,
        previous: dict | None) -> dict:
    return {
        "phase": "prepared",
        "action": action,
        "operation_id": operation_id,
        "before_revision": before_revision,
        "target_phase": target_phase,
        "target_owner_user_id": target_owner_user_id,
        "previous": previous,
    }


def _recover_mistral_sync_record(status: SecretStatus) -> dict | None:
    """Resolve a v2 write-ahead record against the protected CAS revision.

    The journal is written before a vault mutation.  On restart, an unchanged
    revision proves the mutation did not commit and restores the prior stable
    record.  A changed revision plus the expected configured/cleared shape
    proves the protected mutation crossed its commit boundary, so it remains
    pending for the recorded owner.  Anything ambiguous is blocked and can
    neither be leased nor uploaded.
    """

    raw_state = _secret_sync_state()
    raw = _mistral_record_from_document()
    if isinstance(raw, Mapping) and raw.get("phase") == "prepared":
        before_revision = raw.get("before_revision")
        action = raw.get("action")
        operation_id = raw.get("operation_id")
        target_phase = raw.get("target_phase")
        target_owner = _mistral_account_id(raw.get("target_owner_user_id"))
        previous = _valid_mistral_stable_record(raw.get("previous"))
        valid = (
            isinstance(before_revision, str) and bool(before_revision)
            and action in ("replace", "clear")
            and isinstance(operation_id, str) and bool(operation_id)
            and target_phase in ("pending", "synced", "unowned")
            and (target_phase == "unowned") == (target_owner is None)
        )
        if valid and status.revision == before_revision:
            record = previous
        elif valid and status.configured == (action == "replace"):
            record = _mistral_stable_record(
                target_phase,
                status.revision,
                owner_user_id=target_owner,
            )
        else:
            record = _mistral_stable_record(
                "blocked", status.revision, owner_user_id=target_owner)
        _save_mistral_sync_record(record)
        return record

    record = _valid_mistral_stable_record(raw)
    if record is not None:
        if record["revision"] == status.revision:
            if record["phase"] != "unowned" or status.configured:
                return record
            _save_mistral_sync_record(None)
            return None
        # A vault revision changed outside the write-ahead protocol. Do not
        # guess which account owns the resulting credential.
        blocked = _mistral_stable_record("blocked", status.revision)
        _save_mistral_sync_record(blocked)
        return blocked

    # v1 had only a global pending bit; a configured value therefore has no
    # trustworthy account owner. Preserve it as protected-but-unowned and
    # never upload it automatically. Empty legacy state can be discarded.
    legacy_state = bool(raw_state.get("mistral_pending")
                        or raw_state.get(_MISTRAL_PENDING))
    if status.configured:
        unowned = _mistral_stable_record("unowned", status.revision)
        _save_mistral_sync_record(unowned)
        return unowned
    if legacy_state or raw_state:
        _save_mistral_sync_record(None)
    return None


def _client_settings():
    """Return nonsecret preferences only.

    The filter is defense in depth for an old or malicious renderer.  Normal
    client-state PUT already strips these fields before publication.
    """
    raw = (lib.load_json(lib.CLIENT_STATE_PATH, {}) or {}).get("settings") or {}
    if not isinstance(raw, Mapping):
        return {}
    return {key: value for key, value in raw.items() if key not in _SECRET_KEYS}


def _legacy_secret_snapshot() -> tuple[dict[str, str], bool, dict, dict]:
    """Collect legacy values under both source locks with old precedence."""

    with _client_state_lock:
        with _secrets_lock:
            state = lib.load_json(lib.CLIENT_STATE_PATH, {})
            if not isinstance(state, dict):
                state = {}
            settings = state.get("settings")
            settings = settings if isinstance(settings, dict) else {}
            legacy = _legacy_secret_document()
            values: dict[str, str] = {}
            for key in _SECRET_KEYS:
                # The retired runtime overlaid only a usable secrets.json
                # value.  A present null/false/blank legacy field must not
                # suppress a valid client_state credential before both sources
                # are sanitized.
                legacy_value = str(legacy.get(key) or "").strip()
                settings_value = str(settings.get(key) or "").strip()
                value = legacy_value or settings_value
                if value:
                    values[key] = value
            return values, bool(legacy.get(_MISTRAL_PENDING)), state, legacy


def _verify_migrated_secret(
        repository: WindowsDpapiSecretStoreRepository,
        legacy_key: str, expected: str) -> None:
    secret_id = _SECRET_IDS[legacy_key]
    try:
        with repository.credential_leases.lease(secret_id) as leased:
            if leased.reveal() != expected:
                raise ProtectedSecretCutoverError(
                    "protected secret verification did not match legacy state")
    except ProtectedSecretCutoverError:
        raise
    except Exception:
        raise ProtectedSecretCutoverError(
            "protected secret verification failed") from None


def _sanitize_legacy_secret_sources(
        state: dict, *, repository: WindowsDpapiSecretStoreRepository,
        had_legacy_mistral_state: bool) -> None:
    """Remove plaintext only after every protected value was reopened."""

    # A legacy key/pending bit had no account ownership. Protect the value, but
    # never reinterpret that global marker as permission to upload it to the
    # next account that happens to sign in.
    if had_legacy_mistral_state:
        status = SecretStoreService(repository).get_status(
            _SECRET_IDS["mistralKey"])
        with _secrets_lock:
            existing = _valid_mistral_stable_record(
                _mistral_record_from_document())
            if existing is None and status.configured:
                _save_mistral_sync_record(
                    _mistral_stable_record("unowned", status.revision))
            elif existing is None:
                _save_mistral_sync_record(None)
    with _client_state_lock:
        fresh = lib.load_json(lib.CLIENT_STATE_PATH, {})
        if not isinstance(fresh, dict):
            fresh = state
        settings = fresh.get("settings")
        removed = False
        if isinstance(settings, dict):
            for key in _SECRET_KEYS:
                # Presence, not truthiness, decides whether the sanitized
                # document must be persisted.  JSON null and blank legacy
                # fields are still plaintext-secret schema and must disappear
                # physically from disk.
                if key in settings:
                    del settings[key]
                    removed = True
        if removed:
            lib.save_json(lib.CLIENT_STATE_PATH, fresh)
    with _secrets_lock:
        fresh_legacy = _legacy_secret_document()
        for key in (*_SECRET_KEYS, _MISTRAL_PENDING):
            fresh_legacy.pop(key, None)
        if fresh_legacy:
            # Preserve unknown nonsecret compatibility metadata, but never a
            # registered credential.  Normal installations remove the file.
            lib.save_json(_SECRETS_PATH, fresh_legacy)
        else:
            _SECRETS_PATH.unlink(missing_ok=True)


def _migrate_legacy_plaintext_secrets(
        repository: WindowsDpapiSecretStoreRepository,
        *, reopen_repository=None,
) -> WindowsDpapiSecretStoreRepository:
    values, pending, state, legacy = _legacy_secret_snapshot()
    settings = state.get("settings")
    settings = settings if isinstance(settings, Mapping) else {}
    has_legacy_fields = any(
        key in legacy or key in settings
        for key in _SECRET_KEYS
    ) or _MISTRAL_PENDING in legacy
    if not has_legacy_fields:
        return repository
    if values:
        health = repository.health.get_health()
        if health.state != "ready" or not health.writable:
            raise ProtectedSecretCutoverError(
                "protected secret storage is unavailable for legacy migration")
        service = SecretStoreService(repository)
        for legacy_key, credential in sorted(values.items()):
            secret_id = _SECRET_IDS[legacy_key]
            status = service.get_status(secret_id)
            if status.configured:
                # This is either a restart after a committed migration or an
                # independently newer protected value.  Never overwrite it.
                _verify_migrated_secret(repository, legacy_key, credential)
                continue
            service.replace(ReplaceSecretCommand(
                secret_id=secret_id,
                expected_revision=status.revision,
                credential=credential,
                operation_id=f"legacy-cutover-v1-{legacy_key.lower()}",
            ))

        # Reconstruct the complete adapter and decrypt each migrated value.
        # Adapter commit verification covers bytes; this covers reopen/user
        # scope and exact credential semantics before plaintext is sanitized.
        try:
            reopened = (reopen_repository or _new_secret_repository)()
            reopened_health = reopened.health.get_health()
        except Exception:
            raise ProtectedSecretCutoverError(
                "protected secret storage could not be reopened") from None
        if reopened_health.state != "ready":
            raise ProtectedSecretCutoverError(
                "protected secret storage could not be reopened")
        for legacy_key, credential in sorted(values.items()):
            _verify_migrated_secret(reopened, legacy_key, credential)
        repository = reopened

    _sanitize_legacy_secret_sources(
        state,
        repository=repository,
        had_legacy_mistral_state=(
            pending
            or "mistralKey" in legacy
            or "mistralKey" in settings
        ),
    )
    return repository


def _prepare_protected_secret_store() -> None:
    """Complete the one-time cutover before engine/session publication."""

    global _secret_repository
    global _secret_cutover_complete
    if _secret_cutover_complete:
        return
    with _secret_cutover_lock:
        if _secret_cutover_complete:
            return
        repository = _secret_repository or _new_secret_repository()
        repository = _migrate_legacy_plaintext_secrets(repository)
        _secret_repository = repository
        _secret_cutover_complete = True


def _secret_service() -> SecretStoreService:
    service = _library_engine().get_service(SECRET_STORE_SERVICE)
    if not isinstance(service, SecretStoreService):
        raise EngineRepositoryError(
            "protected secret storage is unavailable",
            code="secret_repository_unavailable", retryable=True)
    return service


def _secret_health() -> SecretStoreHealth:
    repository = _secret_repository
    if repository is None:
        return SecretStoreHealth("unavailable", has_vault=None, writable=False)
    return repository.health.get_health()


@contextlib.contextmanager
def _lease_secret(legacy_key: str):
    """Lease one registered credential for the duration of provider work."""

    secret_id = _SECRET_IDS[legacy_key]
    repository = _secret_repository
    if repository is None:
        raise RuntimeError("protected credential storage is unavailable")
    try:
        if legacy_key == "mistralKey":
            # Mistral is account data, not a machine-global provider key. Take
            # one credential snapshot only while ownership, active session,
            # metadata revision, and protected revision all agree. Do not hold
            # the metadata lock across the provider's network operation.
            with _secrets_lock:
                status = _secret_service().get_status(secret_id)
                record = _recover_mistral_sync_record(status)
                account_id = _active_mistral_account_id()
                owned_access = bool(
                    account_id is not None
                    and record
                    and record.get("phase") in _MISTRAL_OWNED_PHASES
                    and record.get("owner_user_id") == account_id
                )
                local_unowned_access = bool(
                    account_id is None
                    and record
                    and record.get("phase") == "unowned"
                )
                if (
                    not (owned_access or local_unowned_access)
                    or record.get("revision") != status.revision
                ):
                    raise RuntimeError(
                        "the required provider credential is not configured")
                with repository.credential_leases.lease(secret_id) as leased:
                    if leased.revision != status.revision:
                        raise RuntimeError(
                            "protected credential storage is unavailable")
                    credential = leased.reveal()
            try:
                yield credential
            finally:
                credential = ""
            return
        with repository.credential_leases.lease(secret_id) as leased:
            yield leased.reveal()
    except SecretCredentialNotConfiguredError:
        raise RuntimeError("the required provider credential is not configured") from None
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("protected credential storage is unavailable") from None


def _secret_is_configured(legacy_key: str) -> bool:
    try:
        status = _secret_service().get_status(_SECRET_IDS[legacy_key])
        if legacy_key != "mistralKey" or not status.configured:
            return status.configured
        with _secrets_lock:
            record = _recover_mistral_sync_record(status)
            account_id = _active_mistral_account_id()
            return bool(
                record
                and record.get("revision") == status.revision
                and (
                    record.get("phase") in _MISTRAL_OWNED_PHASES
                    and record.get("owner_user_id") == account_id
                    and account_id is not None
                    or record.get("phase") == "unowned"
                    and account_id is None
                )
            )
    except EngineError:
        return False


def _public_secret_status(status: SecretStatus) -> SecretStatus:
    """Hide another account's Mistral presence from the active renderer."""

    if status.secret_id != _SECRET_IDS["mistralKey"] or not status.configured:
        return status
    with _secrets_lock:
        record = _recover_mistral_sync_record(status)
        account_id = _active_mistral_account_id()
        if (
            record
            and record.get("revision") == status.revision
            and (
                record.get("phase") in _MISTRAL_OWNED_PHASES
                and record.get("owner_user_id") == account_id
                and account_id is not None
                or record.get("phase") == "unowned"
                and account_id is None
            )
        ):
            return status
    return SecretStatus(status.secret_id, False, status.revision)


def _secret_health_document() -> dict:
    health = _secret_health()
    return {
        "available": health.state == "ready",
        "state": health.state,
        "writable": health.writable,
    }


def _secret_match(secret_id: str) -> str:
    raw = request.headers.get("If-Match")
    if raw is None or raw == "":
        raise EnginePreconditionRequiredError(
            "a secret status revision is required",
            code="secret_revision_required",
            details={"header": "If-Match", "secret_id": secret_id})
    value = raw.strip()
    if (raw != value or value.startswith("W/") or len(value) < 3
            or value[0] != '"' or value[-1] != '"' or "," in value):
        raise EngineValidationError(
            "If-Match must contain one strong quoted secret revision",
            code="invalid_secret_revision",
            details={"header": "If-Match", "secret_id": secret_id})
    return value[1:-1]


def _secret_operation_id() -> str:
    operation_id = request.headers.get("Idempotency-Key")
    if operation_id is None or operation_id == "":
        raise EnginePreconditionRequiredError(
            "an idempotency key is required",
            code="operation_id_required",
            details={"header": "Idempotency-Key"})
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", operation_id):
        raise EngineValidationError(
            "the idempotency key is invalid",
            code="invalid_operation_id",
            details={"header": "Idempotency-Key"})
    return operation_id


def _legacy_renderer_secret_import() -> bool:
    source = request.headers.get(_LEGACY_RENDERER_SECRET_HEADER)
    if source is None:
        return False
    if source != _LEGACY_RENDERER_SECRET_SOURCE:
        raise EngineValidationError(
            "the secret mutation source is invalid",
            code="invalid_secret_mutation_source",
            details={"header": _LEGACY_RENDERER_SECRET_HEADER})
    return True


_SECRET_MUTATION_MAX_BYTES = 512 * 1024


def _secret_replacement_credential() -> str:
    """Read one bounded, duplicate-free credential replacement document."""

    length = request.content_length
    if length is not None and length > _SECRET_MUTATION_MAX_BYTES:
        raise EngineValidationError(
            "the secret replacement document is too large",
            code="secret_mutation_too_large",
            details={"maximum_bytes": _SECRET_MUTATION_MAX_BYTES},
        )
    if request.mimetype != "application/json":
        raise EngineValidationError(
            "the secret replacement must use application/json",
            code="invalid_secret_mutation_document",
        )
    encoded = request.stream.read(_SECRET_MUTATION_MAX_BYTES + 1)
    if len(encoded) > _SECRET_MUTATION_MAX_BYTES:
        raise EngineValidationError(
            "the secret replacement document is too large",
            code="secret_mutation_too_large",
            details={"maximum_bytes": _SECRET_MUTATION_MAX_BYTES},
        )

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON object key")
            value[key] = item
        return value

    try:
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")),
        )
    except (RecursionError, UnicodeError, TypeError, ValueError) as exc:
        raise EngineValidationError(
            "the secret replacement document is invalid",
            code="invalid_secret_mutation_document",
            details={"cause_type": type(exc).__name__},
        ) from exc
    if not isinstance(payload, Mapping) or set(payload) != {"credential"}:
        raise EngineValidationError(
            "the secret replacement document is invalid",
            code="invalid_secret_mutation_document",
        )
    return payload["credential"]


@app.route("/api/secrets", methods=["GET", "PUT", "POST", "PATCH", "DELETE"])
def api_secrets_retired():
    return jsonify({
        "ok": False,
        "code": "plaintext_secret_api_retired",
        "replacement": "/api/v1/secrets",
    }), 410


@app.get("/api/v1/secrets")
def api_v1_secret_statuses():
    health = _secret_health_document()
    statuses = []
    if health["available"]:
        try:
            statuses = [
                _public_secret_status(
                    _secret_service().get_status(secret_id)).as_dict()
                for secret_id in sorted(_SECRET_REGISTRY.ids)
            ]
        except EngineError as exc:
            return _engine_error_response(exc)
    return jsonify({
        "ok": True,
        "schema": "librarytool.secret-status-list/1",
        "health": health,
        "secrets": statuses,
    })


@app.get("/api/v1/secrets/<path:secret_id>")
def api_v1_secret_status(secret_id: str):
    if not _secret_health_document()["available"]:
        return _engine_error_response(EngineRepositoryError(
            "protected secret storage is unavailable",
            code="secret_repository_unavailable", retryable=True))
    try:
        status = _public_secret_status(
            _secret_service().get_status(secret_id))
    except EngineError as exc:
        return _engine_error_response(exc)
    response = jsonify({
        "ok": True,
        "schema": "librarytool.secret-status/1",
        "status": status.as_dict(),
    })
    response.set_etag(status.revision)
    return response


def _mistral_mutation_authorized(
        record: dict | None, status: SecretStatus, *, action: str,
        owner_user_id: str | None, legacy_renderer_import: bool) -> tuple[str, str | None]:
    """Authorize one local mutation and return its durable target ownership."""

    if legacy_renderer_import:
        if action != "replace":
            raise EngineValidationError(
                "legacy credential import requires replacement",
                code="invalid_secret_mutation_source")
        if record and record.get("phase") != "unowned":
            raise EngineConflictError(
                "an account-owned protected credential already exists",
                code="mistral_credential_owned")
        return "unowned", None

    phase = record.get("phase") if record else None
    prior_owner = record.get("owner_user_id") if record else None
    if owner_user_id is None:
        if phase == "pending" or phase == "synced" and status.configured:
            raise EngineConflictError(
                "the protected Mistral credential belongs to a signed-out account",
                code="mistral_credential_owned")
        if phase == "blocked":
            raise EngineConflictError(
                "protected Mistral ownership is unresolved",
                code="mistral_credential_owned")
        # Signed-out Library Tool remains fully useful: a user may configure a
        # device-local Mistral key. It is explicitly unowned, can be leased only
        # while no account is active, and is never profile-synchronized.
        return "unowned", None
    if phase == "pending" and prior_owner != owner_user_id:
        raise EngineConflictError(
            "another account has a pending Mistral credential change",
            code="mistral_pending_for_another_account")
    if action == "clear" and phase == "unowned":
        # The account-scoped status intentionally hides this device-local
        # legacy value.  Treating its clear as an account mutation would then
        # upload an empty value and erase an unrelated remote credential.
        raise EngineConflictError(
            "the protected Mistral credential is not owned by this account",
            code="mistral_credential_unowned")
    if (
        action == "clear"
        and phase in _MISTRAL_OWNED_PHASES
        and prior_owner != owner_user_id
    ):
        raise EngineConflictError(
            "the protected Mistral credential belongs to another account",
            code="mistral_credential_owned")
    return "pending", owner_user_id


def _commit_mistral_mutation(
        command: ReplaceSecretCommand | ClearSecretCommand, *, action: str,
        target_phase: str, target_owner_user_id: str | None):
    """Commit one vault mutation behind a recoverable write-ahead record.

    Caller-visible CAS/idempotency remain the engine service's responsibility.
    The sidecar journal adds only account ownership and remote-sync intent.
    """

    with _secrets_lock:
        service = _secret_service()
        before = service.get_status(command.secret_id)
        previous = _recover_mistral_sync_record(before)
        prepared = _mistral_prepared_record(
            action=action,
            operation_id=command.operation_id,
            before_revision=before.revision,
            target_phase=target_phase,
            target_owner_user_id=target_owner_user_id,
            previous=previous,
        )
        _save_mistral_sync_record(prepared)
        try:
            if action == "replace":
                assert isinstance(command, ReplaceSecretCommand)
                result = service.replace(command)
            else:
                assert isinstance(command, ClearSecretCommand)
                result = service.clear(command)
        except Exception:
            # Settle a definite pre-commit failure immediately. If even status
            # recovery is unavailable, preserve the prepared journal so the
            # next process can resolve it without guessing.
            try:
                fresh = service.get_status(command.secret_id)
                _recover_mistral_sync_record(fresh)
            except Exception:
                pass
            raise
        if result.replayed:
            # Exact replay proves this call did not mutate the current vault.
            # Never transfer ownership using a historical receipt, especially
            # when a different account presents an old operation id.
            _save_mistral_sync_record(previous)
            return result
        _save_mistral_sync_record(_mistral_stable_record(
            target_phase,
            result.receipt.after.revision,
            owner_user_id=target_owner_user_id,
        ))
        return result


def _mistral_sync_pending_for_active_account() -> bool:
    with _secrets_lock:
        status = _secret_service().get_status(_SECRET_IDS["mistralKey"])
        record = _recover_mistral_sync_record(status)
        account_id = _active_mistral_account_id()
        return bool(
            account_id
            and record
            and record.get("phase") == "pending"
            and record.get("owner_user_id") == account_id
            and record.get("revision") == status.revision
        )


def _mutate_mistral_from_request(
        command: ReplaceSecretCommand | ClearSecretCommand, *, action: str,
        legacy_renderer_import: bool = False):
    with _secrets_lock:
        status = _secret_service().get_status(command.secret_id)
        record = _recover_mistral_sync_record(status)
        target_phase, target_owner = _mistral_mutation_authorized(
            record,
            status,
            action=action,
            owner_user_id=_active_mistral_account_id(),
            legacy_renderer_import=legacy_renderer_import,
        )
        return _commit_mistral_mutation(
            command,
            action=action,
            target_phase=target_phase,
            target_owner_user_id=target_owner,
        )


@app.put("/api/v1/secrets/<path:secret_id>")
def api_v1_secret_replace(secret_id: str):
    try:
        expected = _secret_match(secret_id)
        operation_id = _secret_operation_id()
        legacy_renderer_import = _legacy_renderer_secret_import()
        credential = _secret_replacement_credential()
        command = ReplaceSecretCommand(
            secret_id=secret_id,
            expected_revision=expected,
            credential=credential,
            operation_id=operation_id,
        )
        if secret_id == _SECRET_IDS["mistralKey"]:
            result = _mutate_mistral_from_request(
                command,
                action="replace",
                legacy_renderer_import=legacy_renderer_import,
            )
        else:
            if legacy_renderer_import:
                # The marker changes account semantics only for Mistral. It is
                # accepted for other legacy renderer credentials so one client
                # migration path can handle the complete registry.
                pass
            result = _secret_service().replace(command)
    except (TypeError, ValueError) as exc:
        return _engine_error_response(EngineValidationError(
            "the secret replacement document is invalid",
            code="invalid_secret_mutation_document",
            details={"cause_type": type(exc).__name__}))
    except EngineError as exc:
        return _engine_error_response(exc)
    if (
        secret_id == _SECRET_IDS["mistralKey"]
        and not legacy_renderer_import
        and (not result.replayed or _mistral_sync_pending_for_active_account())
    ):
        # Local commit succeeded. Remote failure is deliberately non-fatal: the
        # protected write-ahead state remains pending and startup/login retries.
        _sync_profile_mistral_key()
    return jsonify({
        "ok": True,
        "schema": "librarytool.secret-mutation-receipt/1",
        **result.as_dict(),
    })


@app.delete("/api/v1/secrets/<path:secret_id>")
def api_v1_secret_clear(secret_id: str):
    try:
        if _legacy_renderer_secret_import():
            raise EngineValidationError(
                "legacy credential import requires replacement",
                code="invalid_secret_mutation_source")
        command = ClearSecretCommand(
            secret_id=secret_id,
            expected_revision=_secret_match(secret_id),
            operation_id=_secret_operation_id(),
        )
        if secret_id == _SECRET_IDS["mistralKey"]:
            result = _mutate_mistral_from_request(command, action="clear")
        else:
            result = _secret_service().clear(command)
    except EngineError as exc:
        return _engine_error_response(exc)
    if (
        secret_id == _SECRET_IDS["mistralKey"]
        and (not result.replayed or _mistral_sync_pending_for_active_account())
    ):
        _sync_profile_mistral_key()
    return jsonify({
        "ok": True,
        "schema": "librarytool.secret-mutation-receipt/1",
        **result.as_dict(),
    })


def _profile_secret_rows(value) -> list:
    if not isinstance(value, list) or any(
            not isinstance(row, Mapping) for row in value):
        raise sauth.AuthError("profile secret response was invalid")
    return value


def _profile_secret_keys(rows: list) -> dict:
    if not rows:
        return {}
    raw = rows[0].get("api_keys")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise sauth.AuthError("profile secret response was invalid")
    return dict(raw)


def _mistral_credential_snapshot(
        status: SecretStatus, record: dict, owner_user_id: str) -> str:
    repository = _secret_repository
    if repository is None:
        raise RuntimeError("protected credential storage is unavailable")
    if (
        record.get("owner_user_id") != owner_user_id
        or record.get("phase") not in _MISTRAL_OWNED_PHASES
        or record.get("revision") != status.revision
    ):
        raise RuntimeError("the required provider credential is not configured")
    if not status.configured:
        return ""
    with repository.credential_leases.lease(status.secret_id) as leased:
        if leased.revision != status.revision:
            raise RuntimeError("protected credential storage is unavailable")
        return leased.reveal()


def _write_profile_mistral(
        cfg: dict, ses: dict, local: str) -> None:
    """CAS-merge one pending local value without erasing other providers."""

    owner_user_id = _mistral_account_id(ses.get("user_id"))
    if owner_user_id is None:
        raise sauth.AuthError("profile account identity was invalid")
    encoded_owner = urllib.parse.quote(owner_user_id, safe="")
    for _attempt in range(4):
        rows = _profile_secret_rows(sauth.rest(
            cfg, ses["access_token"], "GET",
            f"profile_secrets?id=eq.{encoded_owner}"
            "&select=api_keys,updated_at",
        ) or [])
        keys = _profile_secret_keys(rows)
        keys["mistral"] = local
        written_at = datetime.now(timezone.utc).isoformat()
        if rows:
            previous = str(rows[0].get("updated_at") or "").strip()
            revision = ("updated_at=eq." + urllib.parse.quote(previous, safe="")
                        if previous else "updated_at=is.null")
            wrote = sauth.rest(
                cfg, ses["access_token"], "PATCH",
                f"profile_secrets?id=eq.{encoded_owner}&{revision}",
                {"api_keys": keys, "updated_at": written_at},
                prefer="return=representation",
            ) or []
        else:
            wrote = sauth.rest(
                cfg, ses["access_token"], "POST",
                "profile_secrets?on_conflict=id",
                [{"id": owner_user_id, "api_keys": keys,
                  "updated_at": written_at}],
                prefer="resolution=ignore-duplicates,return=representation",
            ) or []
        if wrote:
            return
    raise sauth.AuthError("profile changed on another device; retrying")


def _adopt_profile_mistral_locked(
        *, owner_user_id: str, remote: str, status: SecretStatus,
        local: str | None) -> None:
    """Apply a cloud snapshot after the caller revalidated local ownership."""

    secret_id = _SECRET_IDS["mistralKey"]
    if local is not None and remote == local:
        _save_mistral_sync_record(_mistral_stable_record(
            "synced", status.revision, owner_user_id=owner_user_id))
        return
    operation_id = "mistral-profile-adopt-" + uuid.uuid4().hex
    if remote:
        command = ReplaceSecretCommand(
            secret_id=secret_id,
            expected_revision=status.revision,
            credential=remote,
            operation_id=operation_id,
        )
        _commit_mistral_mutation(
            command,
            action="replace",
            target_phase="synced",
            target_owner_user_id=owner_user_id,
        )
    elif status.configured:
        command = ClearSecretCommand(
            secret_id=secret_id,
            expected_revision=status.revision,
            operation_id=operation_id,
        )
        _commit_mistral_mutation(
            command,
            action="clear",
            target_phase="synced",
            target_owner_user_id=owner_user_id,
        )
    else:
        _save_mistral_sync_record(_mistral_stable_record(
            "synced", status.revision, owner_user_id=owner_user_id))


def _sync_profile_mistral_key_with_cfg(cfg: dict, ses: dict) -> str | None:
    """Reconcile only the active account's owned protected Mistral value."""

    secret_id = _SECRET_IDS["mistralKey"]
    owner_user_id = _mistral_account_id(ses.get("user_id"))
    if owner_user_id is None or not ses.get("access_token"):
        return None
    try:
        # Serialize full remote reconciliations. A second login waits for an
        # older account's in-flight upload, then re-evaluates current session
        # and ownership instead of racing it.
        with _mistral_sync_lock:
            if _active_mistral_account_id() != owner_user_id:
                return None
            with _secrets_lock:
                before = _secret_service().get_status(secret_id)
                record = _recover_mistral_sync_record(before)
                phase = record.get("phase") if record else None
                record_owner = record.get("owner_user_id") if record else None
                if phase in ("unowned", "blocked"):
                    # An ownerless legacy cache must be explicitly re-entered;
                    # never upload it to whichever user logs in next.
                    return None
                if phase == "pending" and record_owner != owner_user_id:
                    return None
                pending = phase == "pending"
                if record_owner == owner_user_id:
                    local = _mistral_credential_snapshot(
                        before, record, owner_user_id)
                else:
                    # Switching from a fully synced prior owner is safe, but
                    # their credential must never leave the protected store.
                    local = None
                snapshot_record = dict(record) if record else None

            if pending:
                assert local is not None
                _write_profile_mistral(cfg, ses, local)
                with _secrets_lock:
                    fresh = _secret_service().get_status(secret_id)
                    fresh_record = _recover_mistral_sync_record(fresh)
                    if (
                        fresh.revision == before.revision
                        and fresh_record == snapshot_record
                    ):
                        _save_mistral_sync_record(_mistral_stable_record(
                            "synced", fresh.revision,
                            owner_user_id=owner_user_id))
                return local

            rows = _profile_secret_rows(sauth.rest(
                cfg, ses["access_token"], "GET",
                "profile_secrets?id=eq."
                f"{urllib.parse.quote(owner_user_id, safe='')}"
                "&select=api_keys,updated_at",
            ) or [])
            keys = _profile_secret_keys(rows)
            raw_remote = keys.get("mistral") if "mistral" in keys else ""
            if not isinstance(raw_remote, str):
                raise sauth.AuthError("profile secret response was invalid")
            remote = raw_remote.strip()
            with _secrets_lock:
                fresh = _secret_service().get_status(secret_id)
                fresh_record = _recover_mistral_sync_record(fresh)
                if (
                    _active_mistral_account_id() != owner_user_id
                    or fresh.revision != before.revision
                    or fresh_record != snapshot_record
                ):
                    return None
                _adopt_profile_mistral_locked(
                    owner_user_id=owner_user_id,
                    remote=remote,
                    status=fresh,
                    local=local,
                )
            return remote
    except (EngineError, OSError, RuntimeError, sauth.AuthError) as exc:
        log.warning("Mistral profile sync deferred: %s", exc)
        return None


def _sync_profile_mistral_key() -> str | None:
    """Reconcile Mistral without retaining either protected provider key."""

    ses = _auth_session() if _auth_cfg() else None
    if not ses:
        return None
    try:
        with _auth_execution_cfg() as cfg:
            if not cfg:
                return None
            return _sync_profile_mistral_key_with_cfg(cfg, ses)
    except RuntimeError:
        log.warning("Mistral profile sync deferred: protected auth unavailable")
        return None


def _migrate_secrets_from_client_state() -> None:
    """Compatibility entry point for explicit migration tests/embedders."""
    global _secret_repository
    with _secret_cutover_lock:
        repository = _secret_repository or _new_secret_repository()
        _secret_repository = _migrate_legacy_plaintext_secrets(repository)


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
_DB_METADATA = {
    "ol_search": {
        "description": (
            "Edition-level offline search over titles, authors, publishers, "
            "places, dates, editions, and volumes."
        ),
        "format": "SQLite 3 with FTS5",
        "origin": "Open Library editions, authors, and works data dumps",
        "origin_url": "https://openlibrary.org/developers/dumps",
        "entry_unit": "editions",
        "count_table": "ed",
    },
    "ol_works": {
        "description": (
            "Work-level offline title index used as the fallback when the "
            "edition search index is unavailable."
        ),
        "format": "SQLite 3 with FTS5",
        "origin": "Open Library works data dump",
        "origin_url": "https://openlibrary.org/developers/dumps",
        "entry_unit": "works",
        "count_table": "works",
    },
    "copyright_renewals": {
        "description": (
            "Offline renewal records used to estimate United States copyright "
            "status for works from the renewal era."
        ),
        "format": "UTF-8 CSV",
        "origin": "Catalog of Copyright Entries renewal records",
        "origin_url": "https://exhibits.stanford.edu/copyrightrenewals",
        "entry_unit": "renewal records",
    },
    "whl_catalog": {
        "description": (
            "World Herb Library catalog export used to identify books that are "
            "already published or awaiting publication."
        ),
        "format": "UTF-8 CSV",
        "origin": "World Herb Library catalog export",
        "origin_url": "https://worldherblibrary.org/catalog/",
        "entry_unit": "catalog entries",
    },
}
_db_jobs = {}          # name -> {status, downloaded, total, error}
_db_lock = threading.Lock()
_db_count_cache: dict[tuple[str, str, int, int], int] = {}


def _db_local(rel):
    """Where a database actually is, or None if nowhere. LOCAL-FIRST via
    lib.find_db: the ~/.library-tool drop-in folder, the data root, then the copy
    bundled with the app. None only when no copy exists — the sole case a
    download is offered."""
    p = lib.find_db(rel.split("/")[-1], rel)
    return p if p.exists() else None


def _db_entry_count(name: str, path: Path) -> int:
    """Count a resource once per file revision.

    The Open Library builders assign contiguous integer primary keys, so
    ``max(id)`` is their exact record count and avoids scanning multi-gigabyte
    tables. CSV files need one real parse because quoted fields may contain
    newlines; the path/size/mtime cache makes later status polls constant-time.
    """
    stat = path.stat()
    key = (name, str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    cached = _db_count_cache.get(key)
    if cached is not None:
        return cached

    meta = _DB_METADATA.get(name) or {}
    table = meta.get("count_table")
    if table:
        import sqlite3

        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            count = int(con.execute(f"SELECT max(id) FROM {table}").fetchone()[0] or 0)
        finally:
            con.close()
    else:
        import csv

        encoding = "utf-8-sig" if name == "whl_catalog" else "utf-8"
        with open(path, "r", encoding=encoding, errors="replace", newline="") as fh:
            count = sum(1 for _ in csv.reader(fh))
        count = max(0, count - 1)       # exclude the header row

    if len(_db_count_cache) > 32:
        _db_count_cache.clear()
    _db_count_cache[key] = count
    return count


def _db_location(path: Path, rel: str) -> str:
    """Human-readable provenance for the resolved local copy."""
    resolved = path.resolve()
    if resolved.parent == lib.DB_DIR.expanduser().resolve():
        return "User database folder"
    bundled = [lib.APP_ROOT / rel, lib.APP_ROOT / rel.split("/")[-1]]
    if any(resolved == candidate.resolve() for candidate in bundled):
        return "Bundled with Library Tool"
    try:
        resolved.relative_to(lib.DATA_ROOT.resolve())
        return "Library Tool data folder"
    except ValueError:
        return "Local file"


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
        meta = _DB_METADATA.get(name) or {}
        item = {
            "label": label, "path": rel,
            "filename": rel.split("/")[-1],
            "present": p is not None,
            "loaded": p is not None,
            "size": p.stat().st_size if p else 0,
            "url": str(urls.get(name) or ""),
            "job": _db_jobs.get(name),
            "description": str(meta.get("description") or ""),
            "format": str(meta.get("format") or ""),
            "origin": str(meta.get("origin") or ""),
            "origin_url": str(meta.get("origin_url") or ""),
            "entry_unit": str(meta.get("entry_unit") or "entries"),
            "entries": None,
            "updated_at": "",
            "resolved_path": "",
            "location": "",
        }
        if p:
            stat = p.stat()
            item.update({
                "updated_at": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                "resolved_path": str(p),
                "location": _db_location(p, rel),
            })
            try:
                item["entries"] = _db_entry_count(name, p)
            except Exception as exc:
                item["metadata_error"] = f"{type(exc).__name__}: {exc}"
                log.warning("database metadata failed for %s: %s", name, exc)
        out[name] = item
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
    """Refuse the retired same-origin remote-content proxy in every run mode."""
    return jsonify({"ok": False, "error": "embedded_remote_content_disabled"}), 410


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


def _cloud_public_cfg() -> dict:
    """Nonsecret owner-cloud settings; never includes the service role key."""
    s = _client_settings()
    url = str(s.get("supabaseUrl") or "").strip() or cloud_defaults.SUPABASE_URL
    return {"url": url}


def _cloud_cfg() -> dict | None:
    """Status-only owner-cloud configuration and provider test seam.

    Production values never contain ``key``.  Tests may inject a complete
    short-lived provider config by replacing this function; the lease wrapper
    still confines that copy to one execution.
    """
    cfg = _cloud_public_cfg()
    return cfg if cfg["url"] and _secret_is_configured("supabaseKey") else None


def _cloud_configured() -> bool:
    return bool(_cloud_cfg())


@contextlib.contextmanager
def _lease_cloud_cfg():
    public = _cloud_cfg()
    if not public:
        yield None
        return
    if "key" in public:  # explicitly injected complete provider config
        cfg = dict(public)
        try:
            yield cfg
        finally:
            cfg.pop("key", None)
        return
    with _lease_secret("supabaseKey") as key:
        cfg = {**public, "key": key}
        try:
            yield cfg
        finally:
            cfg.pop("key", None)


def _capture_configured() -> bool:
    """Whether phone sync has auth identity and a persisted user session."""

    session = (_auth_doc().get("session") or {}) if _auth_cfg() else {}
    return bool(session.get("refresh_token"))


@contextlib.contextmanager
def _lease_capture_cfg():
    """Hold auth-key and user-token copies only for one capture sync run."""

    ses = _auth_session() if _auth_cfg() else None
    token = str((ses or {}).get("access_token") or "").strip()
    if not token:
        yield None
        return
    with _auth_execution_cfg() as auth_cfg:
        if not auth_cfg:
            yield None
            return
        cfg = {**auth_cfg, "access_token": token}
        try:
            yield cfg
        finally:
            cfg.pop("key", None)
            cfg.pop("access_token", None)



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
            "instructions": str(s.get("aiInstructions") or "").strip(),
            "smart_scan_instructions":
                str(s.get("smartScanInstructions") or "").strip(),
            # user overrides (Settings > AI): blank temperature keeps each call's own default
            "temperature": s.get("aiTemperature"),
            "timeout": s.get("aiTimeout")}


def _ai_chat(cfg: dict, messages: list, json_mode: bool = False,
             temperature: float = 0.3, timeout: float = 240.0) -> str:
    """One chat-completions call; returns the assistant text. Raises
    RuntimeError with the HTTP body truncated to 300 chars, the same error
    convention every other integration here uses."""
    if not _secret_is_configured("aiKey"):
        raise RuntimeError("no AI key — set one in Settings > Credentials "
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
    with _lease_secret("aiKey") as key:
        req = urllib.request.Request(
            cfg["base"].rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"provider returned HTTP {exc.code}") from None
        except OSError:
            raise RuntimeError("the AI provider is unavailable") from None
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
    if not _secret_is_configured("aiKey"):
        return jsonify({"ok": False, "error": "No AI key — set one in Settings > Credentials"}), 400
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
# The Process-mode surface over the smart-check ENGINE (the pure pipeline
# helpers below: _sc_scan_pages / _sc_ocr_page / _sc_extract / _sc_map_fields):
# locate/download a book's own PDF, skip visually blank front matter, OCR the
# first pages with Mistral until a title/imprint page and a copyright page have
# both been seen, then extract fields with DeepSeek. The result is staged as a
# "smartscan" alternative for review — the real record is never touched here.
# Runs on a daemon thread against the unified job registry. (The wand-overlay
# smart-check UI this engine originally shipped with is retired; Process mode
# is the one front-end.)
_SS_JOBS_KEEP = 20
_SS_TEMP_DIR = lib.DATA_ROOT / "downloads" / "smartscan" / "temp"
_SS_SELECTED_DIR = lib.DATA_ROOT / "downloads" / "smartscan" / "selected"
_SS_TEMP_MAX_AGE = 24 * 60 * 60

_ss_jobs: dict = {}
_ss_jobs_lock = threading.Lock()
_ss_start_lock = threading.Lock()
_ss_pages_lock = threading.Lock()


def _ss_target_kind(target) -> str:
    kind = str(target or "").partition(":")[0]
    return kind if kind in _SC_FIELD_MAPS else ""


def _ss_local_pdf(raw) -> Path | None:
    p = _resolve_local(str(raw or "").strip())
    return (p if p is not None and p.suffix.lower() == ".pdf" and p.is_file()
            else None)


def _ss_target_local_pdf(target: str) -> Path | None:
    """Best local PDF already attached to a Smart Scan target, if any.

    The client normally sends this path, but resolving it again server-side
    prevents a stale UI row from downloading a source that is already local.
    """
    kind, _, ident = str(target or "").partition(":")
    candidates = []
    if kind == "build":
        rec = (lib.load_json(BUILDS_PATH, {}) or {}).get(ident) or {}
        candidates.extend((rec.get("pdf_file"), rec.get("local_pdf")))
    elif kind == "manual":
        rec = (lib.load_json(lib.MANUAL_ENTRIES_PATH, {}) or {}).get(ident) or {}
        candidates.extend((rec.get("local_pdf"), rec.get("pdf_file")))
    elif kind == "checked":
        checked = (lib.load_json(lib.CLIENT_STATE_PATH, {}) or {}).get("checked") or []
        for pair in checked if isinstance(checked, list) else []:
            if (isinstance(pair, (list, tuple)) and len(pair) == 2
                    and str(pair[0]) == ident and isinstance(pair[1], dict)):
                rec = pair[1]
                book = rec.get("book") if isinstance(rec.get("book"), dict) else {}
                candidates.extend((rec.get("local_pdf"), book.get("local_pdf"),
                                   book.get("localPdf"), book.get("pdf_file")))
                break
    for raw in candidates:
        p = _ss_local_pdf(raw)
        if p is not None:
            return p
    return None


def _ss_existing_remote_pdf(url: str) -> tuple[Path | None, str]:
    """Find a reusable local copy for a remote URL without network access."""
    cached = _remote_pdf_cache_path(url)
    if cached.is_file():
        return cached, "cache"
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [urllib.parse.unquote(p) for p in parsed.path.split("/") if p]
    if (host == "archive.org" or host.endswith(".archive.org")) and len(parts) >= 2:
        try:
            marker = parts.index("download")
            identifier = parts[marker + 1]
        except (ValueError, IndexError):
            identifier = ""
        if identifier:
            catalog = _read_ia_catalog()
            saved = catalog.get(identifier) if isinstance(catalog, dict) else None
            if isinstance(saved, dict):
                p = _ss_local_pdf(saved.get("saved_as"))
                if p is not None:
                    return p, "internet-archive"
            p = _ia_pdf_path(identifier)
            if p.is_file():
                return p, "internet-archive"
    return None, ""


def _ss_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _ss_is_temp_pdf(path: Path) -> bool:
    return _ss_is_under(path, _SS_TEMP_DIR)


def _ss_is_selected_pdf(path: Path) -> bool:
    return _ss_is_under(path, _SS_SELECTED_DIR)


def _ss_unlink_temp_pdf(path: Path | str) -> None:
    """Delete only a Smart Scan temp PDF, closing any preview handle first."""
    pdf = Path(path)
    if not _ss_is_temp_pdf(pdf):
        return
    _evict_pdf_doc(pdf)
    pdf.unlink(missing_ok=True)


def _ss_cleanup_stale_temp() -> None:
    """Best-effort cleanup for a manual-selection dialog abandoned mid-flow."""
    if not _SS_TEMP_DIR.is_dir():
        return
    cutoff = time.time() - _SS_TEMP_MAX_AGE
    for p in _SS_TEMP_DIR.glob("*.pdf"):
        try:
            if p.stat().st_mtime < cutoff:
                _ss_unlink_temp_pdf(p)
        except OSError:
            pass


def _ss_download_remote_temp(url: str) -> Path:
    """Stream an uncapped Smart Scan source to a disposable local file."""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("not an http(s) URL")
    _SS_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    _ss_cleanup_stale_temp()
    out = _SS_TEMP_DIR / f"{uuid.uuid4().hex}.pdf"
    try:
        _fetch_remote_pdf(url, out, None)
        return out
    except Exception:
        _ss_unlink_temp_pdf(out)
        raise


def _ss_resolve_pdf(spec: dict) -> tuple[Path, bool, str]:
    """Resolve local-first, returning (path, caller_must_delete, source)."""
    explicit = _ss_local_pdf(spec.get("pdf_path"))
    if explicit is not None:
        return explicit, _ss_is_temp_pdf(explicit), (
            "prepared" if _ss_is_temp_pdf(explicit) else "local")
    attached = _ss_target_local_pdf(str(spec.get("target") or ""))
    if attached is not None:
        return attached, False, "local"
    url = str(spec.get("url") or "").strip()
    if not url:
        raise ValueError("pdf or url required")
    existing, source = _ss_existing_remote_pdf(url)
    if existing is not None:
        return existing, False, source
    return _ss_download_remote_temp(url), True, "download"


def _ss_pdf_page_count(pdf: Path) -> int:
    import fitz
    doc = fitz.open(str(pdf))
    try:
        return int(doc.page_count)
    finally:
        doc.close()


def _ss_api_path(pdf: Path) -> str:
    try:
        return pdf.resolve().relative_to(lib.DATA_ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        return str(pdf.resolve())


def _ss_selected_pdf(source: Path, pages: list[int]) -> Path:
    """Retain a compact PDF containing exactly the selected 1-based pages."""
    _SS_SELECTED_DIR.mkdir(parents=True, exist_ok=True)
    stat = source.stat()
    key = hashlib.sha1(
        f"{source.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{pages}".encode("utf-8")
    ).hexdigest()[:20]
    out = _SS_SELECTED_DIR / f"{key}.pdf"
    with _ss_pages_lock:
        if out.is_file():
            return out
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(str(source))
        writer = PdfWriter()
        for n in pages:
            page = reader.pages[n - 1]
            try:
                page.compress_content_streams()
            except Exception:
                pass
            writer.add_page(page)
        tmp = out.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "wb") as fh:
                writer.write(fh)
            tmp.replace(out)
        finally:
            tmp.unlink(missing_ok=True)
    return out


def _ss_request_spec(payload: dict) -> tuple[dict | None, tuple[dict, int] | None]:
    target = str(payload.get("target") or "").strip()
    _target_kind, separator, target_id = target.partition(":")
    if not _ss_target_kind(target) or not separator or not target_id:
        return None, ({"ok": False, "error": "bad target"}, 400)
    raw_pdf = str(payload.get("pdf") or "").strip()
    url = str(payload.get("url") or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        return None, ({"ok": False, "error": "not an http(s) URL"}, 400)
    pdf = _ss_local_pdf(raw_pdf) if raw_pdf else None
    if raw_pdf and pdf is None and not url:
        return None, ({"ok": False, "error": "PDF not found"}, 404)
    if pdf is None and not url and _ss_target_local_pdf(target) is None:
        return None, ({"ok": False, "error": "pdf or url required"}, 400)
    return {"target": target,
            "label": str(payload.get("label") or "").strip()[:120],
            "volume": str(payload.get("volume") or "").strip()[:40],
            "pdf_path": pdf, "url": url,
            "instructions": str(payload.get("instructions") or "").strip()[:4000]}, None


def _ss_job_new(target: str, label: str, volume: str = "") -> dict:
    target_kind, _, target_id = str(target or "").partition(":")
    if target_kind == "build" and not target_id:
        raise _ItemJobStartRejected("a build Smart Scan needs an item id")
    build_id = target_id if target_kind == "build" else ""
    job = {"id": lib.gen_id(set(_ss_jobs) | set(_jobs)), "target": target,
           "kind": "smartscan", "done": 0, "total": 0, "errors": 0,
           "status": "running", "error": "", "note": "", "volume": volume}
    if build_id:
        job["build_id"] = build_id
        job["subject"] = {"item_id": build_id}
    with _ss_jobs_lock:
        _ss_jobs[job["id"]] = job
    try:
        if build_id:
            _job_track_item_guarded(job, "smartscan", build_id, label=label)
        else:
            _job_track(job, "smartscan", label=label)
    except _ItemJobStartRejected:
        with _ss_jobs_lock:
            _ss_jobs.pop(job["id"], None)
        raise
    return job


def _ss_job_start(target: str, label: str, volume: str, run) -> dict:
    job = _ss_job_new(target, label, volume)
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
    pdf = None
    owned_pdf = False
    try:
        if not _secret_is_configured("mistralKey"):
            raise RuntimeError("Mistral API key not configured (Settings > Credentials)")
        pdf = spec.get("pdf_path")
        if pdf is None:
            with _ss_jobs_lock:
                job["note"] = "resolving PDF"
            _job_checkpoint(job, force=True)
        pdf, owned_pdf, _source = _ss_resolve_pdf(spec)
        if spec.get("pdf_path") is None:
            with _ss_jobs_lock:
                job["note"] = ""
        pdf = Path(pdf)
        selected_pdf = _ss_is_selected_pdf(pdf)
        candidates = (list(range(1, _ss_pdf_page_count(pdf) + 1))
                      if selected_pdf else _sc_scan_pages(pdf))
        if not candidates:
            raise RuntimeError(f"no readable pages in the first {_SC_SCAN_CAP} pages")
        # A user explicitly chose every page in a retained selection PDF, so
        # honor all of them. Automatic scans keep the cost-bounded OCR cap.
        planned = candidates if selected_pdf else candidates[:_SC_OCR_CAP]
        with _ss_jobs_lock:
            job["total"] = len(planned) + 1        # +1 = the extraction step
        texts: dict[int, str] = {}
        titleish = copyrightish = False
        for i, n in enumerate(planned):
            if _an_cancel_check(job, "cancelled — nothing was written"):
                return
            try:
                with _lease_secret("mistralKey") as mkey:
                    text = _sc_ocr_page(pdf, n, mkey)
            except Exception as exc:
                text = ""
                with _ss_jobs_lock:
                    job["errors"] += 1
                    job["note"] = f"page {n}: {type(exc).__name__}"
            if text:
                texts[n] = text
                titleish = titleish or bool(_SC_YEAR_RE.search(text) or _SC_IMPRINT_RE.search(text))
                copyrightish = copyrightish or bool(_SC_COPYRIGHT_RE.search(text))
            with _ss_jobs_lock:
                job["done"] = i + 1
            _job_checkpoint(job)
            # both signals in hand: stop. Also cap the copyright hunt for books
            # that never print "copyright" (pre-1900 / non-English) so we don't
            # burn all _SC_OCR_CAP pages chasing a signal that never fires.
            if not selected_pdf:
                if titleish and copyrightish and len(texts) >= 2:
                    break
                if titleish and len(texts) >= 4:
                    break
        ocr_text = "\n\n".join(f"--- page {n} ---\n{texts[n]}" for n in sorted(texts))
        if not ocr_text.strip():
            raise RuntimeError("OCR produced no text from the front matter")
        if _an_cancel_check(job, "cancelled — nothing was written"):
            return
        got, model = _sc_extract(ocr_text, spec.get("instructions") or None)
        got = got if isinstance(got, dict) else {}
        got.pop("extra", None)
        mapped = _sc_map_fields(kind, got)
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
    finally:
        if owned_pdf and pdf is not None:
            try:
                _ss_unlink_temp_pdf(pdf)
            except OSError:
                pass


@app.route("/api/process/smartscan/prepare", methods=["POST"])
def api_process_smartscan_prepare():
    """Resolve one source for manual page marking and report its page count.

    Body: ``{target, pdf?, url?, label?}``. Local/attached/cached copies win;
    an uncached remote source is streamed to a disposable local PDF. The
    returned ``pdf`` is directly usable by /api/pdf/pageimg and by the normal
    Smart Scan run endpoint.
    """
    spec, error = _ss_request_spec(request.get_json(silent=True) or {})
    if error:
        body, status = error
        return jsonify(body), status
    pdf = None
    owned = False
    try:
        pdf, owned, source = _ss_resolve_pdf(spec)
        pages = _ss_pdf_page_count(pdf)
        if pages < 1:
            raise ValueError("PDF has no pages")
        return jsonify({"ok": True, "pdf": _ss_api_path(pdf), "pages": pages,
                        "temporary": owned, "reused": not owned,
                        "source": source})
    except Exception as exc:
        if owned and pdf is not None:
            _ss_unlink_temp_pdf(pdf)
        return jsonify({"ok": False,
                        "error": f"{type(exc).__name__}: {exc}"}), 400


@app.route("/api/process/smartscan/select-pages", methods=["POST"])
def api_process_smartscan_select_pages():
    """Save exactly the chosen 1-based pages as a retained local Smart Scan PDF.

    Body: ``{target, pdf?, url?, pages:[...]}``; ``pdf`` is normally the path
    returned by /prepare. The response path can be passed unchanged as ``pdf``
    to /run. A prepared full remote source is deleted after a successful save.
    """
    payload = request.get_json(silent=True) or {}
    raw_pages = payload.get("pages")
    if not isinstance(raw_pages, list):
        return jsonify({"ok": False, "error": "pages must be a list"}), 400
    try:
        pages = sorted({int(n) for n in raw_pages})
    except (TypeError, ValueError):
        pages = []
    if not pages or pages[0] < 1:
        return jsonify({"ok": False, "error": "select at least one page"}), 400
    spec, error = _ss_request_spec(payload)
    if error:
        body, status = error
        return jsonify(body), status
    pdf = None
    owned = False
    source = ""
    success = False
    try:
        pdf, owned, source = _ss_resolve_pdf(spec)
        total = _ss_pdf_page_count(pdf)
        if pages[-1] > total:
            return jsonify({"ok": False,
                            "error": f"page {pages[-1]} exceeds PDF page count {total}"}), 400
        out = _ss_selected_pdf(pdf, pages)
        success = True
        return jsonify({"ok": True, "pdf": _ss_api_path(out),
                        "pages": pages, "page_count": len(pages),
                        "source_pages": total})
    except Exception as exc:
        return jsonify({"ok": False,
                        "error": f"{type(exc).__name__}: {exc}"}), 400
    finally:
        # A prepared path stays available after validation errors so the user
        # can fix the selection. A direct remote attempt has no caller to reuse
        # it, and every successful subset makes the full temp copy unnecessary.
        if owned and pdf is not None and (success or source == "download"):
            try:
                _ss_unlink_temp_pdf(pdf)
            except OSError:
                pass


@app.route("/api/process/smartscan/run", methods=["POST"])
def api_process_smartscan_run():
    """Start a Smart Scan for one record. Body: {target, pdf?|url?, label?}.
    Returns the job to poll; a duplicate while one is running joins it."""
    spec, error = _ss_request_spec(request.get_json(silent=True) or {})
    if error:
        body, status = error
        return jsonify(body), status
    target = spec["target"]
    label = spec["label"]
    with _ss_start_lock:
        with _jobs_lock:
            for j in _jobs.values():
                if (j.get("kind") == "smartscan" and j.get("target") == target
                        and j.get("state") in _JOB_ACTIVE):
                    return jsonify({"ok": True, "already": True, "job": _job_public(j)})
        try:
            job = _ss_job_start(target, label, spec["volume"],
                                lambda jb: _ss_run(jb, spec))
        except _ItemJobStartRejected:
            return jsonify({"ok": False, "error":
                            "that entry changed before Smart Scan could start"}), 409
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
    lib.save_text(p, text)


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
    """Legacy SHA-1 used by version-1 translation metadata."""
    return _translation_provenance.legacy_source_hash(text)


def _page_source_hash(text: str) -> str:
    """Version-2 source hash: normalize wrapping, preserve paragraphs."""
    return _translation_provenance.source_hash(text)


def _translation_meta_path(bid: str, lang: str):
    return _entry_dir(bid) / "translations" / f"{lang}.meta.json"


def _load_translation_meta(bid: str, lang: str) -> dict:
    doc = lib.load_json(_translation_meta_path(bid, lang), None)
    if not isinstance(doc, dict) or not isinstance(doc.get("pages"), dict):
        return {"version": 1, "src": "", "model": "", "pages": {}}
    return doc


def _stale_translation_pages(meta: dict, src_pages: dict[int, str],
                             src: str = "") -> list[int]:
    """Tracked pages whose source layer or content revision changed."""
    return list(_translation_provenance.stale_pages(meta, src_pages, src))


def _translations_info(bid: str) -> list[dict]:
    d = _entry_dir(bid) / "translations"
    out = []
    if d.is_dir():
        b = lib.load_json(BUILDS_PATH, {}).get(bid) or {}
        src_name, src_text = _analyze_doc(bid, b)
        src_pages = _an_pages(src_text)
        for f in sorted(d.glob("*.txt")):
            pages = _an_pages(f.read_text(encoding="utf-8", errors="replace"))
            meta = _load_translation_meta(bid, f.stem)
            translation_status = _translation_provenance.status(
                meta, src_pages, pages, source_layer=src_name)
            info = {"lang": f.stem, "pages": len(pages),
                    "size": f.stat().st_size,
                    "stale": len(translation_status.stale),
                    "untracked": len(translation_status.untracked)}
            missing = len(translation_status.missing)
            orphaned = len(translation_status.orphaned)
            if missing:
                info["missing"] = missing
            if orphaned:
                info["orphaned"] = orphaned
            if meta.get("src") and meta.get("src") != src_name:
                info["source_mismatch"] = True
            out.append(info)
    return out


@app.route("/api/builds/<bid>/about", methods=["GET", "PUT"])
@_live_item_write_endpoint
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
@_live_item_write_endpoint
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
        # regenerating a translation means re-running a paid model over the
        # whole book, so keep the text (and its provenance) recoverable
        payload = {}
        if p.is_file():
            payload[f"{lang}.txt"] = p.read_text(encoding="utf-8", errors="replace")
        m = _translation_meta_path(bid, lang)
        if m.is_file():
            payload[m.name] = m.read_text(encoding="utf-8", errors="replace")
        if p.is_file():
            p.unlink()
        if m.is_file():
            m.unlink()
    if payload:
        _trash_put("translation",
                   f"{lang} translation of {b.get('title') or bid}",
                   {"build_id": bid, "lang": lang}, {},
                   payload)
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
    try:
        _job_track_item_guarded(job, kind, bid)
    except _ItemJobStartRejected as exc:
        with _an_jobs_lock:
            _an_jobs.pop(job["id"], None)
        raise _AnalyzeSourceChanged() from exc
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

    try:
        job = _an_job_start(bid, "about", 1, run)
    except _AnalyzeSourceChanged:
        return jsonify({"ok": False, "error":
                        "the item changed before analysis could start"}), 409
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
        tm = _load_translation_meta(bid, lang)
        stale = _stale_translation_pages(tm, pages, name)
        tracked = {int(k) for k, rec in tm["pages"].items()
                   if str(k).isdigit() and isinstance(rec, dict) and
                   (rec.get("source_hash") or rec.get("sha1"))}
        untracked = sorted(n for n in done_pages if n not in tracked)
        todo = sorted(set(stale) | {n for n in pages if pages[n].strip()
                                    and not done_pages.get(n, "").strip()})
        if not todo:
            if untracked:
                return jsonify({"ok": False, "error":
                                "the translation has untracked human/imported "
                                "pages; choose pages explicitly to replace them",
                                "untracked": untracked}), 409
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
                    tm["version"] = 2
                    tm["src"], tm["model"] = name, cfg["model"]
                    tm["pages"][str(n)] = _translation_provenance.page_record(
                        pages[n],
                        source_layer=name,
                        model=cfg["model"],
                        at=datetime.now(timezone.utc).isoformat(
                            timespec="seconds"),
                    )
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


# --- smart-check engine: extract real metadata from a book's own PDF -------------
# The pure pipeline behind Process mode's Smart Scan (see _ss_run above): scan
# the PDF's front matter skipping visually blank pages, OCR page by page with
# Mistral until a title page and a copyright page have been seen, and send the
# OCR text to the configured AI provider (DeepSeek by default — the same
# Mistral -> DeepSeek chain as a phone capture) for strict-JSON bibliographic
# extraction. Only stateless helpers live here; results are staged as Process
# alternatives (staged_alts.json), and the retired wand-overlay UI's own
# store/endpoints are gone.

_SC_SCAN_CAP = 15        # pages considered from the front (blanks included)
_SC_OCR_CAP = 8          # pages actually sent to OCR
_SC_WIDTH = 1400         # render width; the OCR queue's default

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


def _sc_extract(ocr_text: str, instructions: str | None = None) -> tuple[dict, str]:
    """OCR text -> normalized bibliography + the model that produced it.
    DeepSeek (Settings > AI) when a key is set — mirroring the phone app's
    'DeepSeek by default' — else Mistral's own extraction."""
    cfg = _ai_cfg()
    if _secret_is_configured("aiKey"):
        prompt = _SC_PROMPT
        custom = str(instructions if instructions is not None else
                     cfg.get("smart_scan_instructions") or "").strip()
        if custom:
            prompt += ("\n\nAdditional Smart Scan instructions from the user:\n"
                       + custom[:4000])
        obj = _ai_json(cfg, [{"role": "user",
                              "content": prompt + "\n\n" + ocr_text[:12000]}],
                       temperature=0.0)
        return capture.normalize_bibliography(obj), cfg["model"]
    if not _secret_is_configured("mistralKey"):
        raise RuntimeError("no AI key and no Mistral key — set one in "
                           "Settings > Credentials")
    with _lease_secret("mistralKey") as mkey:
        extracted = capture.extract_bibliography(ocr_text, mkey)
    return extracted, capture.EXTRACT_MODEL


def _sc_map_fields(kind: str, fields: dict) -> dict:
    """Extraction vocabulary -> the target store's. Blank values never map:
    a smart check may fill or correct a field, never erase one."""
    out = {}
    for src, dst in _SC_FIELD_MAPS[kind].items():
        v = str(fields.get(src) or "").strip()
        if v:
            out[dst] = v
    return out




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


def _r2_public_cfg() -> dict:
    s = _client_settings()
    return {"account": str(s.get("r2Account") or "").strip(),
            "bucket": str(s.get("r2Bucket") or "").strip(),
            "public_base": str(s.get("r2PublicBase") or "").strip()}


def _r2_configured() -> bool:
    cfg = _r2_public_cfg()
    return bool(cfg["account"] and cfg["bucket"] and
                _secret_is_configured("r2KeyId") and
                _secret_is_configured("r2Secret"))


@contextlib.contextmanager
def _lease_r2_cfg():
    public = _r2_public_cfg()
    if not _r2_configured():
        yield {**public, "key_id": "", "secret": ""}
        return
    with contextlib.ExitStack() as stack:
        cfg = {
            **public,
            "key_id": stack.enter_context(_lease_secret("r2KeyId")),
            "secret": stack.enter_context(_lease_secret("r2Secret")),
        }
        try:
            yield cfg
        finally:
            cfg.pop("key_id", None)
            cfg.pop("secret", None)


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


def _catalogue_workbench_links(rows: list[dict], builds: dict) -> list[dict]:
    """Attach the local Workbench identity without changing public metadata.

    ``volumes.slug`` and a build's ``published_slug`` are the current identity
    spine.  A slug must resolve to exactly one local build: silently choosing
    between duplicates could open the wrong editable record.
    """
    by_slug: dict[str, list[str]] = {}
    for bid, build in builds.items():
        if not isinstance(build, dict):
            continue
        slug = str(build.get("published_slug") or "").strip()
        if slug:
            by_slug.setdefault(slug, []).append(str(bid))

    linked = []
    for source in rows:
        row = dict(source)
        # Never trust or preserve a similarly named public column: this value
        # describes this checkout and comes only from its local build store.
        row.pop("local_build_id", None)
        matches = by_slug.get(str(row.get("slug") or "").strip(), [])
        if len(matches) == 1:
            row["local_build_id"] = matches[0]
        linked.append(row)
    return linked


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
    if _auth_cfg():
        try:
            with _auth_execution_cfg() as cfg:
                if not cfg:
                    raise RuntimeError("protected auth configuration unavailable")
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
    # Never fill its catalogue metadata from local edits or add a local-only
    # volume that may since have been unpublished.  The one local sidecar is
    # local_build_id: a navigation link back through slug identity, not public
    # catalogue data. Local rows remain strictly an offline fallback.
    builds = lib.load_json(BUILDS_PATH, {})
    entries = (_catalogue_workbench_links(cloud_rows, builds) if cloud_ok
               else _local_publish_rows(builds))
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
    if _auth_cfg() and slug:
        q = urllib.parse.quote(slug, safe="")
        try:
            with _auth_execution_cfg() as cfg:
                if not cfg:
                    raise RuntimeError("protected auth configuration unavailable")
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


def _unpublish_object(cloud: dict, r2cfg: dict, slug: str, path: str,
                      r2_name: str = "") -> None:
    """Best-effort removal of an object whose catalogue row never landed.
    r2_name overrides the default "<slug>.pdf" R2 key -- needed for anything
    that isn't the primary PDF (a secondary scan, a thumbnail)."""
    try:
        if path:
            sbase.delete_objects(cloud, "volumes", [path])
        else:
            r2.delete(r2cfg, f"volumes/{r2_name or slug + '.pdf'}")
        log.warning("rolled back orphaned object for %s", slug)
    except Exception as exc:
        log.error("could not roll back orphaned object for %s: %s", slug, exc)


def _publish_run_with_configs(bid: str, actor: str, cloud: dict, r2cfg: dict,
                              job: dict | None = None) -> None:
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
            _unpublish_object(cloud, r2cfg, slug, path)
            for name_i, path_i in extras:
                _unpublish_object(cloud, r2cfg, name_i[:-4], path_i)
            if thumb_url or thumb_path:
                _unpublish_object(cloud, r2cfg, f"{slug}-thumb", thumb_path,
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
        activity("published", "book", actor=actor or None, detail=b.get("title", ""))
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


def _publish_run(bid: str, actor: str, job: dict | None = None) -> None:
    """Lease publishing credentials inside the worker, never its job record."""
    try:
        with _lease_cloud_cfg() as cloud:
            if not cloud:
                raise RuntimeError(
                    "Supabase is not configured (Settings > Credentials)")
            with _lease_r2_cfg() as r2cfg:
                _publish_run_with_configs(bid, actor, cloud, r2cfg, job)
    except Exception as exc:
        log.error("publish credential setup failed for build %s", bid,
                  exc_info=exc)
        with _publish_lock:
            _publish.update(stage="error", running=False,
                            error=f"{type(exc).__name__}: {exc}")
        if job is not None:
            _job_transition(job, "error", error=str(exc), note="")


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
    if not _cloud_configured():
        return jsonify({"ok": False, "error": "Supabase is not configured (Settings > Credentials)"}), 400
    with _publish_lock:
        if _publish["running"]:
            return jsonify({"ok": False, "error": "a publish is already running"}), 409
        _publish.update(running=True, build=bid, stage="starting", sent=0, total=0,
                        error="", url="", slug="", note="", job="")
    job = {"id": lib.gen_id(set(_jobs)), "build_id": bid, "kind": "publish",
           "status": "running"}
    try:
        _job_track_item_guarded(job, "publish", bid)
    except _ItemJobStartRejected:
        with _publish_lock:
            _publish["running"] = False
            _publish["error"] = "the item changed before publishing could start"
        return jsonify({"ok": False, "error": _publish["error"]}), 409
    with _publish_lock:
        _publish["job"] = job["id"]
    threading.Thread(target=_publish_run, args=(bid, _actor(), job),
                     daemon=True).start()
    return jsonify({"ok": True, "job": job["id"]})


@app.route("/api/volumes/publish/status")
def api_volumes_publish_status():
    with _publish_lock:
        return jsonify(dict(_publish,
                            store="r2" if _r2_configured() else "supabase"))


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
@_live_item_write_endpoint
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
            "model": str(s.get("embedModel") or "").strip()}


def _embed_texts(cfg: dict, texts: list[str]) -> list[list[float]]:
    """One /embeddings call for a batch; returns vectors in input order.
    Same error convention as _ai_chat: RuntimeError with the body truncated."""
    lease = (_lease_secret("embedKey") if _secret_is_configured("embedKey")
             else contextlib.nullcontext(""))
    with lease as key:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(
            cfg["base"].rstrip("/") + "/embeddings",
            data=json.dumps({"model": cfg["model"], "input": texts}).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120.0) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"provider returned HTTP {exc.code}") from None
        except OSError:
            raise RuntimeError("the embedding provider is unavailable") from None
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
    if not _cloud_configured():
        return jsonify({"ok": False, "error":
                        "Supabase is not configured (Settings > Credentials)"}), 400
    doc_name, text, source_revision = _analyze_doc_snapshot(bid, b)
    pages = _an_pages(text)
    if not pages:
        return jsonify({"ok": False, "error":
                        "no OCR text for this entry — extract or run OCR first"}), 400
    ecfg = _embed_cfg()
    embed = bool(ecfg["base"] and ecfg["model"])
    src_input = _manifest_input(bid, f"ocr/{doc_name}")   # hashed at job start
    src_sha = str(src_input.get("sha256") or "")

    def run_with_cloud(job, cloud):
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
            # #142 promotion visibility: the latest evaluation results ride
            # on the version row, so the Search-index card shows them where
            # promotion (publish / roll back) is decided
            with _eval_lock:
                ev = _load_eval(bid).get("last_run") or {}
            overall = (ev.get("local") or {}).get("overall") or {}
            if overall.get("judged") or overall.get("unanswerable"):
                stats["eval"] = dict(overall, at=str(ev.get("at") or ""))
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

    def run(job):
        with _lease_cloud_cfg() as cloud:
            if not cloud:
                raise RuntimeError("protected cloud credential is unavailable")
            return run_with_cloud(job, cloud)

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
    if not _cloud_configured():
        return jsonify({"ok": False, "error":
                        "Supabase is not configured (Settings > Credentials)"}), 400
    q = urllib.parse.quote(slug, safe="")
    try:
        with _lease_cloud_cfg() as cloud:
            if not cloud:
                raise RuntimeError("protected cloud credential is unavailable")
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
    # Passive status must not nag about cloud config: only publishing needs the
    # owner service key, and the publish action reports that itself. Here we just
    # list any already-published versions, warning only if that lookup fails.
    if slug and _cloud_configured():
        q = urllib.parse.quote(slug, safe="")
        try:
            with _lease_cloud_cfg() as cloud:
                versions = [v for v in (sbase._rest(
                    cloud, "GET", f"index_versions?slug=eq.{q}"
                    "&select=id,channel,config,stats,source_hash,built_at"
                    "&order=built_at.desc,id.desc") or [])
                    if isinstance(v, dict)] if cloud else []
        except sbase.SyncError as exc:
            warning = _index_sync_error(exc)
    return jsonify({"ok": True, "state": _passages_state(bid, b),
                    "versions": versions, "slug": slug,
                    "published": b.get("status") == "uploaded",
                    "rights": str(b.get("rights") or ""), "warning": warning})


# --- Knowledge: Test + Ask (#142/#143) -------------------------------------------
# One retrieval core serves both: the Test view judges it (per-volume
# evaluation sets, metrics that gate index promotion — docs/search-design.md
# D9), and Ask builds on it (evidence first, then an optional cited answer).
# Both are curator-side desktop surfaces; the public site gets nothing here
# until an execution point with quotas exists (D7).

_EVAL_KINDS = ("exact-phrase", "archaic-modern", "factual", "thematic",
               "tables", "cross-page", "multilingual", "unanswerable")
_EVAL_K = 10               # the metrics cutoff: Recall@10 / nDCG@10 / MRR
# An unanswerable query PASSES when no passage scores ABOVE this floor.
# 1.0 is the score of one query term found exactly once at full coverage
# (see _score_passages): genuine coverage of a multi-term query lands well
# above it, incidental partial overlap lands below. The known edge: a
# single-term query whose term appears exactly once scores exactly 1.0 and
# still passes — repeats push it over.
_EVAL_UNANSWERABLE_FLOOR = 1.0
_SNIPPET_WORDS = 24        # ts_headline's MaxWords, so both arms read alike
_ASK_K = 10                # evidence rows behind an answer
_eval_lock = threading.Lock()

_SCORE_WORD = re.compile(r"[a-z0-9]+")


def _score_terms(text: str) -> list[str]:
    """Folded search words of arbitrary text: _search_normalize (the exact
    layer the index searches — long s, ligatures, diacritics), then the
    alphanumeric runs, so 'phyſick,' and 'physick' meet as one term."""
    return _SCORE_WORD.findall(_search_normalize(text))


def _evidence_snippet(body: str, qterms: list[str]) -> str:
    """A «»-marked window of the normalized body around the densest cluster
    of query-term hits — the local counterpart of the RPC's ts_headline
    (StartSel=«, StopSel=», MaxWords=24), shared by Test and Ask so every
    evidence row reads the same."""
    words = body.split()
    qset = set(qterms)
    hits = [any(w in qset for w in _SCORE_WORD.findall(tok)) for tok in words]
    if any(hits):
        # densest fixed-size window by prefix sums; ties keep the earliest
        ps = [0]
        for h in hits:
            ps.append(ps[-1] + int(h))
        best, best_n = 0, -1
        for i in range(max(1, len(words) - _SNIPPET_WORDS + 1)):
            n = ps[min(i + _SNIPPET_WORDS, len(words))] - ps[i]
            if n > best_n:
                best, best_n = i, n
    else:
        best = 0
    seg = words[best:best + _SNIPPET_WORDS]
    mark = lambda m: f"«{m.group(0)}»" if m.group(0) in qset else m.group(0)  # noqa: E731
    out = " ".join(_SCORE_WORD.sub(mark, tok) for tok in seg)
    if best > 0:
        out = "… " + out
    if best + _SNIPPET_WORDS < len(words):
        out += " …"
    return out


def _score_passages(passages: list[dict], query: str, k: int = _EVAL_K,
                    excluded=None) -> list[dict]:
    """Rank non-excluded passages for a query with a transparent lexical
    scorer, over the same folded layer as the published index.

    Scoring is deliberately simple and inspectable: per query term found,
    1 + ln(tf) (repeats help, with diminishing returns), the sum weighted
    by query coverage (matched terms / query terms) so passages containing
    the whole query outrank scattered partial hits, doubled when the exact
    folded phrase occurs. This APPROXIMATES the cloud FTS (websearch +
    ts_rank over the same normalized bodies); it does not replicate
    ts_rank's proximity weighting — which is exactly why the Test view can
    show both arms side by side and judge them by the same metrics.

    Returns [{passage_id, page_from, page_to, score, snippet, text}],
    best first, ties broken by passage id for a stable order.
    """
    qterms = list(dict.fromkeys(_score_terms(query)))
    if not qterms:
        return []
    phrase = " ".join(_score_terms(query))     # order + repeats kept
    skip = set(excluded or ())
    scored = []
    for p in passages:
        if p.get("id") in skip:
            continue
        body = p.get("body") or _search_normalize(p.get("text"))
        words = _SCORE_WORD.findall(body)
        if not words:
            continue
        counts = collections.Counter(words)
        hit = [t for t in qterms if counts[t]]
        if not hit:
            continue
        score = (sum(1.0 + math.log(counts[t]) for t in hit)
                 * (len(hit) / len(qterms)))
        if len(qterms) > 1 and f" {phrase} " in f" {' '.join(words)} ":
            score *= 2.0                       # exact-phrase boost
        scored.append((score, p, body))
    scored.sort(key=lambda s: (-s[0], s[1]["id"]))
    return [{"passage_id": p["id"], "page_from": p.get("page_from"),
             "page_to": p.get("page_to"), "score": round(score, 4),
             "snippet": _evidence_snippet(body, qterms),
             "text": p.get("text") or ""}
            for score, p, body in scored[:max(1, int(k))]]


def _rpc_search_passages(cloud: dict, slug: str, query: str,
                         k: int = _EVAL_K) -> list[dict]:
    """The published arm: the same search_passages RPC the website calls,
    over the LATEST stable index version — so the curator compares the
    working passages against what actually serves."""
    rows = sbase._rest(cloud, "POST", "rpc/search_passages",
                       {"p_slug": slug, "p_query": query,
                        "p_limit": max(1, int(k))})
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and r.get("passage_id"):
            out.append({"passage_id": str(r["passage_id"]),
                        "page_from": r.get("page_from"),
                        "page_to": r.get("page_to"),
                        "score": float(r.get("rank") or 0.0),
                        "snippet": str(r.get("snippet") or "")})
    return out


def _index_version_count(cloud: dict, slug: str) -> int:
    rows = sbase._rest(
        cloud, "GET", "index_versions?slug=eq."
        + urllib.parse.quote(slug, safe="") + "&select=id")
    return len(rows) if isinstance(rows, list) else 0


# --- the per-volume evaluation set: entries/<bid>/eval.json (#142) ----------------

def _eval_path(bid: str) -> Path:
    return _entry_dir(bid) / "eval.json"


def _load_eval(bid: str) -> dict:
    doc = lib.load_json(_eval_path(bid), None)
    if not isinstance(doc, dict) or not isinstance(doc.get("queries"), list):
        return {"version": 1, "queries": []}
    return doc


def _eval_metrics(ranked: list[str], relevant: set[str], k: int) -> dict:
    """Recall@k, nDCG@k, MRR@k against binary judgments. Results the curator
    never judged count as not relevant — the standard convention, and the
    honest one: an unjudged hit earns credit only after someone looks at it.
    nDCG uses binary gains 1/log2(pos+1); the ideal ranking is all relevant
    passages first."""
    hits = [1 if pid in relevant else 0 for pid in ranked[:k]]
    recall = round(sum(hits) / len(relevant), 4) if relevant else 0.0
    dcg = sum(h / math.log2(i + 2) for i, h in enumerate(hits))
    idcg = sum(1 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    mrr = 0.0
    for i, h in enumerate(hits):
        if h:
            mrr = round(1.0 / (i + 1), 4)
            break
    return {"recall": recall, "ndcg": round(dcg / idcg, 4) if idcg else 0.0,
            "mrr": mrr}


def _eval_query_run(q: dict, results: list[dict], k: int,
                    floor: float | None) -> dict:
    """One query's scored outcome. Judged queries (>= 1 relevant judgment)
    get the three metrics; unanswerable queries pass/fail on the floor
    (floor=None means the published arm, where rank scales differ across
    ts_rank and RRF — there, returning nothing at all is the only honest
    pass); everything else reports unjudged and stays out of the means."""
    top = float(results[0]["score"]) if results else 0.0
    if q.get("kind") == "unanswerable":
        ok = (not results) if floor is None else (top <= floor)
        return {"kind": "unanswerable", "pass": bool(ok),
                "top": round(top, 4)}
    relevant = {pid for pid, v in (q.get("judgments") or {}).items() if v}
    if not relevant:
        return {"judged": False}
    out = _eval_metrics([r["passage_id"] for r in results], relevant, k)
    out["relevant"] = len(relevant)
    return out


def _eval_overall(per_query: dict) -> dict:
    """Means over the judged queries plus the separate tallies: unanswerable
    pass counts and the unjudged remainder. No judged queries -> null
    metrics, never a fake zero."""
    judged = [r for r in per_query.values() if "recall" in r]
    un = [r for r in per_query.values() if r.get("kind") == "unanswerable"]
    mean = lambda key: round(sum(r[key] for r in judged) / len(judged), 4)  # noqa: E731
    return {"recall": mean("recall") if judged else None,
            "ndcg": mean("ndcg") if judged else None,
            "mrr": mean("mrr") if judged else None,
            "judged": len(judged),
            "unanswerable_pass": sum(1 for r in un if r["pass"]),
            "unanswerable": len(un),
            "unjudged": sum(1 for r in per_query.values()
                            if r.get("judged") is False)}


def _eval_summary_note(overall: dict) -> str:
    bits = []
    if overall.get("judged"):
        bits.append(f"R@{_EVAL_K} {overall['recall']:.2f} · "
                    f"nDCG {overall['ndcg']:.2f} · MRR {overall['mrr']:.2f} "
                    f"({overall['judged']} judged)")
    if overall.get("unanswerable"):
        bits.append(f"unanswerable {overall['unanswerable_pass']}"
                    f"/{overall['unanswerable']}")
    if overall.get("unjudged"):
        bits.append(f"{overall['unjudged']} unjudged")
    return " · ".join(bits) or "no queries scored"


@app.route("/api/builds/<bid>/eval", methods=["GET", "PUT"])
@_live_item_write_endpoint
def api_build_eval(bid: str):
    """GET the evaluation set; PUT curation, one operation at a time (the
    annotations idiom): {add: {text, kind}}, {update: {id, text?, kind?}},
    {remove: id}, {judge: {id, passage_id, rel: 1|0|null}} (null clears).

    Judgments are keyed by content-hash passage ids, so they survive
    regeneration for every passage whose text did not change; a judged id
    the current set no longer contains simply never surfaces in results —
    and a positive one honestly drags Recall down until re-judged, which
    is exactly the re-score-against-new-versions semantics of #142."""
    if request.method == "GET":
        with _eval_lock:
            return jsonify({"ok": True, "doc": _load_eval(bid)})
    b, err = _an_gate(bid)
    if err:
        return err
    p = request.get_json(silent=True) or {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    judged = False
    with _eval_lock:
        doc = _load_eval(bid)
        queries = doc["queries"]
        by_id = {str(q.get("id")): q for q in queries}

        add = p.get("add") or {}
        if add:
            text = str(add.get("text") or "").strip()[:500]
            kind = str(add.get("kind") or "").strip()
            if not text:
                return jsonify({"ok": False, "error": "a query needs text"}), 400
            if kind not in _EVAL_KINDS:
                return jsonify({"ok": False, "error": "unknown query kind"}), 400
            queries.append({"id": lib.gen_id(set(by_id)), "text": text,
                            "kind": kind, "judgments": {}, "updated_at": now})

        if p.get("remove"):
            doc["queries"] = queries = [
                q for q in queries if q.get("id") != p["remove"]]
        by_id = {str(q.get("id")): q for q in queries}

        upd = p.get("update") or {}
        if upd.get("id"):
            q = by_id.get(str(upd["id"]))
            if q is None:
                return jsonify({"ok": False, "error": "unknown query id"}), 400
            if "text" in upd:
                text = str(upd.get("text") or "").strip()[:500]
                if not text:
                    return jsonify({"ok": False,
                                    "error": "a query needs text"}), 400
                q["text"] = text
            if "kind" in upd:
                if upd["kind"] not in _EVAL_KINDS:
                    return jsonify({"ok": False,
                                    "error": "unknown query kind"}), 400
                q["kind"] = upd["kind"]
            q["updated_at"] = now

        j = p.get("judge") or {}
        if j.get("id"):
            q = by_id.get(str(j["id"]))
            if q is None:
                return jsonify({"ok": False, "error": "unknown query id"}), 400
            pid = str(j.get("passage_id") or "").strip()
            if not pid:
                return jsonify({"ok": False, "error": "no passage id"}), 400
            rel = j.get("rel")
            marks = q.setdefault("judgments", {})
            if rel is None:
                marks.pop(pid, None)
            elif rel in (0, 1, False, True):
                marks[pid] = 1 if rel else 0
            else:
                return jsonify({"ok": False,
                                "error": "rel must be 1, 0 or null"}), 400
            q["updated_at"] = now
            judged = True

        lib.save_json(_eval_path(bid), doc)
    # provenance (#135): judging binds the set to the passages as they stand,
    # so the input is passages.json fingerprinted at judgment time; query
    # edits keep the recorded inputs (they change intent, not derivation)
    _manifest_record(bid, "eval.json", {"kind": "eval"},
                     [_manifest_input(bid, "passages.json")] if judged else None)
    return jsonify({"ok": True, "doc": doc})


@app.route("/api/knowledge/eval/run", methods=["POST"])
def api_knowledge_eval_run():
    """Score every evaluation query and cache the results. Body: {build_id}.
    A tracked job (one tick per query): the local arm always runs; when the
    cloud is configured and the book has published, the search_passages RPC
    runs beside it so working passages and the published index get separate,
    comparable metrics. Results land in eval.json under last_run."""
    p = request.get_json(silent=True) or {}
    bid = str(p.get("build_id") or "").strip()
    b, err = _an_gate(bid)
    if err:
        return err
    with _eval_lock:
        queries = [dict(q) for q in _load_eval(bid)["queries"]]
    if not queries:
        return jsonify({"ok": False, "error":
                        "no evaluation queries yet — add some first"}), 400
    with _passages_lock:
        doc = _load_passages(bid)
    if doc is None:
        return jsonify({"ok": False, "error":
                        "no passages yet — generate them first"}), 400
    passages = doc.get("passages") or []
    excluded = doc.get("excluded") or []
    slug = str(b.get("published_slug") or "").strip()
    psg_input = _manifest_input(bid, "passages.json")   # hashed at job start

    def run_with_cloud(job, cloud):
        try:
            local_q: dict = {}
            pub_q: dict = {}
            warning = ""
            version = 0
            if cloud and slug:
                try:
                    version = _index_version_count(cloud, slug)
                except sbase.SyncError as exc:
                    warning = _index_sync_error(exc)
            for q in queries:
                if _an_cancel_check(job, "cancelled — metrics not saved"):
                    return
                results = _score_passages(passages, q["text"], _EVAL_K,
                                          excluded)
                local_q[q["id"]] = _eval_query_run(
                    q, results, _EVAL_K, _EVAL_UNANSWERABLE_FLOOR)
                if version and not warning:
                    try:
                        rows = _rpc_search_passages(cloud, slug, q["text"],
                                                    _EVAL_K)
                        pub_q[q["id"]] = _eval_query_run(q, rows, _EVAL_K,
                                                         None)
                    except sbase.SyncError as exc:
                        warning = _index_sync_error(exc)
                with _an_jobs_lock:
                    job["done"] += 1
                _job_checkpoint(job)
            overall = _eval_overall(local_q)
            last_run = {"at": datetime.now(timezone.utc).isoformat(
                            timespec="seconds"),
                        "k": _EVAL_K, "floor": _EVAL_UNANSWERABLE_FLOOR,
                        "local": {"overall": overall, "queries": local_q}}
            if pub_q:
                last_run["published"] = {"version": version,
                                         "overall": _eval_overall(pub_q),
                                         "queries": pub_q}
            if warning:
                last_run["warning"] = warning
            with _eval_lock:
                cur = _load_eval(bid)
                cur["last_run"] = last_run
                lib.save_json(_eval_path(bid), cur)
            _manifest_record(bid, "eval.json", {"kind": "eval"}, [psg_input])
            with _an_jobs_lock:
                job["note"] = _eval_summary_note(overall)
            activity("ran retrieval evaluation", "book",
                     detail=b.get("title", ""))
            _an_finish(job)
        except Exception as exc:
            log.error("eval run failed for %s", bid, exc_info=exc)
            _an_finish(job, f"{type(exc).__name__}: {exc}")

    def run(job):
        with _lease_cloud_cfg() as cloud:
            return run_with_cloud(job, cloud)

    try:
        job = _an_job_start(bid, "eval-run", len(queries), run)
    except _AnalyzeSourceChanged:
        return jsonify({"ok": False, "error":
                        "the item changed before evaluation could start"}), 409
    return jsonify({"ok": True, "job": job["id"]})


# --- Ask this book (#143): evidence first, then an optional cited answer ----------

_ASK_ABSTAIN = "The archive does not contain enough evidence to answer this."
_ASK_NOTE = ("Model-generated from the passages above — not the book's "
             "text, not medical advice.")


def _ask_system_prompt(year) -> str:
    """The fixed Ask contract: grounded, cited, abstaining, historically
    framed, never medical advice. The edition year is the only variable."""
    edition = f"this {year} edition" if str(year or "").strip() else \
        "this edition"
    return (
        "You answer questions about one historical book using ONLY the "
        "numbered passages supplied below — no outside knowledge, no "
        "guesses. After each claim, cite the page of the passage that "
        "supports it inline as [p<page>], for example [p12]. If the "
        f"passages do not support an answer, reply exactly "
        f"\"{_ASK_ABSTAIN}\" and nothing else. Frame every historical "
        f"claim as the edition's statement — \"{edition} states…\" — "
        "never as present-day fact. The book's remedies are historical "
        "text: never present them as modern medical advice, and if the "
        "question asks how to use a remedy today, refuse that framing "
        "and describe only what the edition says.")


@app.route("/api/knowledge/ask", methods=["POST"])
def api_knowledge_ask():
    """Retrieval for Ask and for the Test view's live query runs. Body:
    {build_id, question, published?}. Synchronous — this is a local rank
    over passages.json, plus (published: true) the RPC arm for the
    working-vs-published comparison."""
    p = request.get_json(silent=True) or {}
    bid = str(p.get("build_id") or "").strip()
    b, err = _an_gate(bid)
    if err:
        return err
    question = str(p.get("question") or "").strip()
    if not question:
        return jsonify({"ok": False, "error": "ask a question first"}), 400
    with _passages_lock:
        doc = _load_passages(bid)
    if doc is None:
        return jsonify({"ok": False, "error":
                        "no passages yet — generate them first"}), 404
    results = _score_passages(doc.get("passages") or [], question, _ASK_K,
                              doc.get("excluded") or [])
    out = {"ok": True, "results": results,
           "floor": _EVAL_UNANSWERABLE_FLOOR, "published": None,
           "warning": ""}
    if p.get("published"):
        slug = str(b.get("published_slug") or "").strip()
        if _cloud_configured() and slug:
            try:
                with _lease_cloud_cfg() as cloud:
                    if cloud:
                        out["published"] = {
                            "version": _index_version_count(cloud, slug),
                            "results": _rpc_search_passages(
                                cloud, slug, question, _ASK_K)}
            except sbase.SyncError as exc:
                out["warning"] = _index_sync_error(exc)
    return jsonify(out)


@app.route("/api/knowledge/ask/answer", methods=["POST"])
def api_knowledge_ask_answer():
    """Draft a cited answer from already-retrieved passages. Body:
    {build_id, question, passage_ids}. The answer is TRANSIENT by design —
    it is returned, rendered, and never written to disk: AI-derived claims
    stay visibly derived and never publish (docs/search-design.md §7), and
    a stored answer would rot silently as the text is corrected."""
    p = request.get_json(silent=True) or {}
    bid = str(p.get("build_id") or "").strip()
    b, err = _an_gate(bid)
    if err:
        return err
    question = str(p.get("question") or "").strip()
    if not question:
        return jsonify({"ok": False, "error": "ask a question first"}), 400
    cfg = _ai_cfg()
    if not _secret_is_configured("aiKey"):
        # the _ai_chat no-key message, surfaced before any work happens
        return jsonify({"ok": False, "error":
                        "no AI key — set one in Settings > Credentials "
                        "(DeepSeek is the default provider)"}), 409
    with _passages_lock:
        doc = _load_passages(bid)
    by_id = {str(x.get("id")): x for x in (doc or {}).get("passages") or []}
    wanted = [str(i) for i in p.get("passage_ids") or []]
    picked = [by_id[i] for i in wanted if i in by_id][:12]
    if not picked:
        return jsonify({"ok": False, "error":
                        "no known passages to answer from — run the "
                        "question first"}), 400
    lines = []
    for i, x in enumerate(picked, 1):
        a, z = x.get("page_from"), x.get("page_to")
        label = f"page {a}" if a == z else f"pages {a}–{z}"
        lines.append(f"[{i}] ({label})\n{x.get('text') or ''}")
    user = (f"Question: {question}\n\nPassages from "
            f"{_an_meta_line(b)}:\n\n" + "\n\n".join(lines))
    try:
        answer = _ai_chat(cfg, [
            {"role": "system", "content": _ask_system_prompt(b.get("year"))},
            {"role": "user", "content": user},
        ], temperature=0.2)
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify({"ok": True, "answer": answer.strip(),
                    "abstained": answer.strip() == _ASK_ABSTAIN})


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


# Context Book Capture attaches to EVERY capture: which collection a book was
# scanned into, and where that batch came from. It rides in `meta` to reach an
# entry's `extra` without a schema change, but it is passthrough provenance, not
# extraction output — so it must never make an un-extracted capture look
# extracted, or a phone with no API key would skip the desktop's own OCR below
# and file a blank entry. The `scan_` prefix also keeps it from colliding with a
# model-extracted `collection`.
PHONE_PROVENANCE_KEYS = frozenset({
    "scan_collection_id", "scan_collection", "scan_from",
})
PHONE_PHOTO_ASSETS_KEY = "_capture_photo_assets"
PHONE_CAPTURE_NOTES_KEY = "_capture_notes"
PHONE_INTERNAL_META_KEYS = frozenset({
    PHONE_PHOTO_ASSETS_KEY, PHONE_CAPTURE_NOTES_KEY,
})
PHONE_PHOTO_ASSETS_SCHEMA = "org.whl.bookcapture.photo-assets"
PHONE_CAPTURE_NOTES_SCHEMA = "org.whl.bookcapture.capture-notes"
PHONE_CAPTURE_NOTE_FIELDS = frozenset({
    "price", "pages", "condition", "illustrations", "remark",
})
_PHONE_ASSET_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")
_PHONE_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _phone_asset_token(value: str) -> bool:
    return value not in {".", ".."} and bool(_PHONE_ASSET_TOKEN.fullmatch(value))


def _capture_provenance(cap: dict) -> dict:
    """The phone's scan provenance, whichever import path ran.

    It travels in `meta`, but only _phone_result reads `meta` — when the phone
    sent no extraction and the desktop OCRs the capture itself, that result
    carries none of it. So it is merged back in explicitly, or a capture from a
    phone without an API key would arrive with no record of where it came from.
    """
    meta = cap.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = None
    if not isinstance(meta, dict):
        return {}
    return {key: value.strip() for key in PHONE_PROVENANCE_KEYS
            if isinstance((value := meta.get(key)), str) and value.strip()}


def _capture_photo_assets(cap: dict) -> dict:
    """Return the versioned Android photo contract without catalog pollution.

    LAN sends the contract at the capture envelope's top level. The existing
    cloud table has no dedicated column yet, so Android mirrors it through one
    reserved metadata key. Neither representation is bibliographic metadata.
    Validate the current v1 identity/lineage fields strictly while preserving
    optional v1 fields verbatim. A future version must be explicitly supported
    before it can replace this evidence on disk.
    """
    supplied = "photo_assets" in cap
    payload = cap.get("photo_assets")
    meta = cap.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = None
    if not isinstance(payload, dict) and isinstance(meta, dict) and \
            PHONE_PHOTO_ASSETS_KEY in meta:
        supplied = True
        payload = meta.get(PHONE_PHOTO_ASSETS_KEY)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            raise ValueError("advertised photo asset contract is not valid JSON")
    if not supplied:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("advertised photo asset contract is not an object")

    def invalid(reason: str):
        raise ValueError(f"invalid photo asset contract: {reason}")

    version = payload.get("version")
    assets = payload.get("assets")
    capture_id = str(payload.get("capture_id") or "")
    if (payload.get("schema") != PHONE_PHOTO_ASSETS_SCHEMA or
            isinstance(version, bool) or version != 1 or
            capture_id != str(cap.get("id") or "") or
            not _phone_asset_token(capture_id) or
            not isinstance(assets, list) or not assets):
        invalid("schema, version, capture id, or asset list")
    clean_assets = []
    ids, orders, capture_files = set(), set(), set()
    for asset in assets:
        if not isinstance(asset, dict):
            invalid("asset is not an object")
        asset_id = str(asset.get("asset_id") or "")
        order = asset.get("capture_order")
        capture_file = str(asset.get("capture_file") or "")
        original = asset.get("original")
        display = asset.get("display")
        if (not _phone_asset_token(asset_id) or
                isinstance(order, bool) or not isinstance(order, int) or order < 1 or
                not re.fullmatch(r"photo_\d+\.jpg", capture_file) or
                not isinstance(original, dict) or not isinstance(display, dict)):
            invalid("asset identity, order, filename, or lineage")
        original_ref = str(original.get("reference") or "")
        display_ref = str(display.get("reference") or "")
        original_sha = str(original.get("sha256") or "").lower()
        display_sha = str(display.get("sha256") or "").lower()
        if (not _phone_asset_token(original_ref) or
                not _phone_asset_token(display_ref) or
                not _PHONE_SHA256.fullmatch(original_sha) or
                not _PHONE_SHA256.fullmatch(display_sha) or
                asset_id in ids or order in orders or capture_file in capture_files):
            invalid("asset reference, checksum, or uniqueness")
        ids.add(asset_id)
        orders.add(order)
        capture_files.add(capture_file)
        clean_assets.append(asset)
    if orders != set(range(1, len(clean_assets) + 1)):
        invalid("capture order is not dense")
    transport = payload.get("transport")
    if (not isinstance(transport, dict) or
            transport.get("representation") != "original" or
            transport.get("version") != 1):
        invalid("original transport marker is missing or unsupported")
    preserved = dict(payload)
    preserved["assets"] = clean_assets
    return preserved


def _capture_notes(cap: dict) -> dict:
    """Validate Android notes without making their transport bibliographic."""
    meta = cap.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = None
    payload = meta.get(PHONE_CAPTURE_NOTES_KEY) if isinstance(meta, dict) else None
    if payload is None:
        payload = cap.get("capture_notes")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {}
    if not isinstance(payload, dict):
        return {}
    version = payload.get("version")
    capture_id = str(payload.get("capture_id") or "")
    if (payload.get("schema") != PHONE_CAPTURE_NOTES_SCHEMA or
            isinstance(version, bool) or not isinstance(version, int) or version != 1 or
            capture_id != str(cap.get("id") or "") or
            not _phone_asset_token(capture_id) or
            not isinstance(payload.get("notes"), list)):
        return {}

    clean_notes = []
    ids = set()
    for note in payload["notes"]:
        if not isinstance(note, dict):
            return {}
        note_id = str(note.get("id") or "")
        status = str(note.get("status") or "")
        provider = str(note.get("provider") or "").strip()
        model = str(note.get("model") or "").strip()
        transcript = note.get("transcript")
        unclassified = note.get("unclassified_text")
        started = note.get("started_at_ms")
        updated = note.get("updated_at_ms")
        completed = note.get("completed_at_ms")
        rows = note.get("rows")
        valid_completion = (
            status == "completed" and
            isinstance(completed, int) and not isinstance(completed, bool) and
            isinstance(started, int) and not isinstance(started, bool) and
            isinstance(updated, int) and not isinstance(updated, bool) and
            started <= completed <= updated
        ) or (status == "in_progress" and completed is None)
        if (not _phone_asset_token(note_id) or note_id in ids or
                status not in {"in_progress", "completed"} or
                not provider or not model or
                not isinstance(transcript, str) or
                not isinstance(unclassified, str) or
                isinstance(started, bool) or not isinstance(started, int) or started < 0 or
                isinstance(updated, bool) or not isinstance(updated, int) or updated < started or
                not isinstance(rows, list) or not valid_completion):
            return {}
        clean_rows = []
        for row in rows:
            if not isinstance(row, dict):
                return {}
            field = str(row.get("field") or "").strip().lower()
            value = row.get("value")
            if not _phone_asset_token(field) or not isinstance(value, str):
                return {}
            clean_row = dict(row)
            clean_row["field"] = field
            clean_row["value"] = value
            clean_rows.append(clean_row)
        ids.add(note_id)
        clean_note = dict(note)
        clean_note.update({
            "id": note_id,
            "status": status,
            "provider": provider,
            "model": model,
            "rows": clean_rows,
        })
        clean_notes.append(clean_note)
    preserved = dict(payload)
    preserved["notes"] = clean_notes
    return preserved


def _capture_note_extra(notes: dict) -> dict:
    """Latest non-blank spoken value for each recognized catalogue fact."""
    extra = {}
    for note in notes.get("notes") or []:
        for row in note.get("rows") or []:
            field = str(row.get("field") or "").strip().lower()
            value = str(row.get("value") or "").strip()
            if field in PHONE_CAPTURE_NOTE_FIELDS and value:
                extra[field] = value
    return extra


def _transported_original_assets(photo_contract: dict, raw_photos: list[bytes],
                                 photo_paths: list) -> list[dict]:
    """Bind each transport part to its declared immutable Android source."""
    assets = list(photo_contract.get("assets") or [])
    if len(assets) != len(raw_photos):
        raise ValueError("photo asset count does not match transported originals")
    by_name = {str(asset["capture_file"]): asset for asset in assets}
    if photo_paths:
        names = [str(path).replace("\\", "/").rsplit("/", 1)[-1]
                 for path in photo_paths]
        if len(names) != len(raw_photos) or len(set(names)) != len(names):
            raise ValueError("transported original names are incomplete or duplicated")
        try:
            ordered = [by_name[name] for name in names]
        except KeyError as exc:
            raise ValueError(
                "transported original name is absent from photo contract"
            ) from exc
    else:
        ordered = sorted(assets, key=lambda asset: asset["capture_order"])
    for asset, raw in zip(ordered, raw_photos):
        actual = hashlib.sha256(raw).hexdigest()
        expected = str(asset["original"]["sha256"]).lower()
        if actual != expected:
            display_hash = str(asset["display"].get("sha256") or "").lower()
            if actual == display_hash:
                raise ValueError(
                    "transported bytes are a display derivative, not the camera "
                    f"original, for {asset['capture_file']}"
                )
            raise ValueError(
                f"transported original checksum mismatch for {asset['capture_file']}"
            )
    return ordered


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

    has_metadata = any(
        has_value(value)
        for key, value in meta.items()
        if key not in PHONE_PROVENANCE_KEYS and key not in PHONE_INTERNAL_META_KEYS
    )
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
    # Only the flat wire keys are authoritative capture provenance. A model or
    # malformed client must not smuggle reserved scan_* values through nested
    # generic metadata; ingest_capture merges the validated flat strings later.
    for key in PHONE_PROVENANCE_KEYS | PHONE_INTERNAL_META_KEYS:
        extra.pop(key, None)
    for key, value in meta.items():
        if (key not in capture.FIELDS and key != "extra"
                and key not in PHONE_PROVENANCE_KEYS
                and key not in PHONE_INTERNAL_META_KEYS and has_value(value)):
            extra.setdefault(key, value)
    # The current manual catalogue has no dedicated spine-title column. Keep
    # the extraction in extensible metadata at the phone-result boundary.
    spine_title = fields.get("spine_title", "")
    if spine_title:
        extra.setdefault("spine_title", spine_title)
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
    photo_contract = _capture_photo_assets(cap)
    transported_assets = (_transported_original_assets(
        photo_contract, raw_photos, photo_paths or []) if photo_contract else [])
    # prefer the phone's OCR/fields (BookCapture 2.0+) to skip a second OCR pass;
    # fall back to the full desktop pipeline when the phone sent nothing
    result = _phone_result(cap, raw_photos, photo_paths or []) \
        or capture.process_capture(raw_photos, mistral_key)
    # Notes are parsed only after OCR source selection so their reserved
    # transport key cannot make an otherwise unprocessed capture look extracted.
    capture_notes = _capture_notes(cap)

    cdir = CAPTURES_DIR / cap_id
    cdir.mkdir(parents=True, exist_ok=True)
    if capture_notes:
        (cdir / "capture_notes.json").write_text(
            json.dumps(capture_notes, indent=2, ensure_ascii=False) + "\n",
            "utf-8",
        )
    images = []
    for i, jpg in enumerate(result["photos"], 1):
        (cdir / f"photo_{i}.jpg").write_bytes(jpg)
        images.append(f"captures/{cap_id}/photo_{i}.jpg")
    for i, raw in enumerate(raw_photos, 1):       # originals: re-OCR stays possible
        (cdir / f"orig_{i}.jpg").write_bytes(raw)
    if photo_contract:
        import_assets = []
        for index, raw in enumerate(raw_photos, 1):
            derivative = result["photos"][index - 1]
            source_asset = transported_assets[index - 1]
            import_assets.append({
                "order": index - 1,
                "asset_id": source_asset["asset_id"],
                "raw_ref": f"orig_{index}.jpg",
                "display_ref": f"photo_{index}.jpg",
                "source_checksum": hashlib.sha256(raw).hexdigest(),
                "derivative_checksum": hashlib.sha256(derivative).hexdigest(),
                "transport_representation": "original",
                "recipe": "desktop_perspective_standardize_v1",
                "lifecycle": "failed" if any(
                    error.startswith(f"photo {index}:") for error in result["errors"]
                ) else "completed",
            })
        photo_contract = dict(photo_contract)
        photo_contract["desktop_import"] = {
            "version": 1,
            "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "assets": import_assets,
        }
        (cdir / "photo_assets.json").write_text(
            json.dumps(photo_contract, indent=2, ensure_ascii=False) + "\n",
            "utf-8",
        )
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
    capture_extra = dict(result["extra"])
    spine_title = str(fields.get("spine_title") or "").strip()
    if spine_title:
        capture_extra.setdefault("spine_title", spine_title)
    # Explicit spoken fields outrank OCR guesses. The full transcript remains
    # a capture sidecar rather than being copied into catalogue metadata.
    capture_extra.update(_capture_note_extra(capture_notes))
    # provenance last: it is the phone's own record of where the book came
    # from, so it outranks anything an extractor happened to call the same thing
    provenance = _capture_provenance(cap)
    if provenance.get("scan_collection_id"):
        # A queued capture may arrive after its collection identity was merged.
        # Resolve only the link; name/origin remain the capture-time snapshot.
        provenance["scan_collection_id"] = _resolve_collection_alias(
            provenance["scan_collection_id"])
    entry["extra"] = _clean_extra({**capture_extra, **provenance})
    entry["images"] = images
    entry["capture_id"] = cap_id
    entry["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry["checks"] = _entry_checks(entry)
    with _manual_lock:
        # Close the ingest/merge interleaving: a merge alias can be committed
        # while photo processing is in flight, after the first resolution
        # above but before this entry exists for the merge repoint walk.
        extra = entry.get("extra")
        if isinstance(extra, dict) and extra.get("scan_collection_id"):
            entry["extra"] = dict(extra, scan_collection_id=
                                  _resolve_collection_alias(
                                      str(extra["scan_collection_id"])))
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


@contextlib.contextmanager
def _cloud_sync_item_policy_guard():
    """Yield the tombstone policy while lifecycle isolation remains held."""

    lifecycle = _item_lifecycle_engine()
    with lifecycle.deletion_index_guard() as deletion_index:
        yield deletion_index.allows


def _cloud_sync_run_with_configs(owner_cfg: dict | None,
                                 capture_cfg: dict | None) -> dict:
    """Import this user's pending phone captures, with optional owner work.

    Capture ingest runs with the signed-in user's JWT and RLS. If an owner has
    separately configured a service credential, the same pass also pushes the
    catalog mirror, merges the owner working stores, and mirrors entry folders.

    Everything after the flag is claimed runs inside try/finally, and ANY
    exception lands in `result` — the flag can never stay stuck on, and a
    failed pass can't masquerade as the previous run's outcome."""
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
        delete_remote = s.get("cloudDeleteRemote") is not False
        # A different desktop may have merged an identity since this process
        # last opened the Collections window. Refresh durable merge markers
        # before importing queued captures so their ids converge immediately.
        # If this authoritative read fails, stop before filing captures with a
        # potentially stale identity; the next sync can retry without loss.
        collection_token = (str(capture_cfg.get("access_token") or "").strip()
                            or str(capture_cfg.get("key") or "").strip())
        _refresh_collection_aliases(capture_cfg, collection_token)
        for cap in sbase.list_pending_captures(capture_cfg):
            try:
                lease = (_lease_secret("mistralKey")
                         if _secret_is_configured("mistralKey")
                         else contextlib.nullcontext(""))
                with lease as mistral_key:
                    outcome = _import_capture(
                        capture_cfg, cap, mistral_key, delete_remote)
                if outcome == "imported":
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
            stores = store_sync.sync_stores(
                owner_cfg,
                locks={
                    "ia_catalog": _ia_catalog_lock,
                    "corrections": _corrections_lock,
                    "taxonomy": _categories_lock,
                },
                item_policy_guard=_cloud_sync_item_policy_guard,
            )
            for name, res in stores.items():
                if res.get("error"):
                    errors.append(f"{name}: {res['error']}")
                if res.get("guard"):      # a wipe was caught: worth surfacing
                    errors.append(f"{name}: {res['guard']}")
            with _lease_r2_cfg() as r2cfg:
                if r2.configured(r2cfg):
                    try:
                        entries_res = store_sync.sync_entry_files(
                            r2cfg,
                            item_policy_guard=_cloud_sync_item_policy_guard,
                        )
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


def _cloud_sync_run() -> dict:
    """Acquire owner credentials inside the sync execution, never a job."""
    with contextlib.ExitStack() as stack:
        capture_cfg = stack.enter_context(_lease_capture_cfg())
        owner_cfg = stack.enter_context(_lease_cloud_cfg())
        return _cloud_sync_run_with_configs(
            owner_cfg, capture_cfg or owner_cfg)


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
            if minutes <= 0 or not (_capture_configured() or _cloud_configured()):
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
    if not (_capture_configured() or _cloud_configured()):
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
    out["configured"] = bool(_capture_configured() or _cloud_configured())
    return jsonify(out)


@app.route("/api/cloudsync/test")
def api_cloudsync_test():
    if _capture_configured():
        with _lease_capture_cfg() as capture_cfg:
            if capture_cfg:
                return jsonify(sbase.test_connection(capture_cfg))
            return jsonify({"ok": False,
                            "error": "protected auth credential is unavailable"})
    if not _cloud_configured():
        return jsonify({"ok": False,
                        "error": "Sign in to your Library Tool account to test phone sync"})
    with _lease_cloud_cfg() as owner_cfg:
        if not owner_cfg:
            return jsonify({"ok": False,
                            "error": "protected cloud credential is unavailable"})
        return jsonify(sbase.test_connection(owner_cfg))


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


@lan_app.post("/lan/pair")
def _lan_pair():
    """Authenticated, side-effect-free proof that the configured token opens
    the capture service. Echoing a caller nonce prevents a cached/replayed
    liveness response from being mistaken for successful pairing."""
    import hmac
    if not hmac.compare_digest(request.headers.get("X-WHL-Token", ""), _lan_token()):
        abort(401)
    payload = request.get_json(silent=True) or {}
    nonce = str(payload.get("nonce") or "")
    if not (16 <= len(nonce) <= 128):
        abort(400)
    return jsonify(app="whl-capture", authorized=True, nonce=nonce)


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
    try:
        # pass the filenames so _phone_result can key the phone's OCR by name
        with _lease_secret("mistralKey") as mistral_key:
            entry_id, _errors = ingest_capture(cap, photos, mistral_key, names)
    except Exception as exc:                       # noqa: BLE001 — report, don't 500-crash
        log.exception("LAN capture ingest failed")
        return jsonify(error=str(exc)[:200]), 500
    capture_id = str(cap.get("id") or "")
    if entry_id is None:
        # Idempotent retry. Echo the submitted capture id so Android can prove
        # that this receipt belongs to the entry it is about to move to sent/.
        return jsonify(app="whl-capture", status="duplicate",
                       id=capture_id), 200
    # ``ingest_capture`` returns the desktop's generated manual-entry id, but
    # Android's delivery contract is keyed by the phone capture id. Echo that
    # submitted id in ``id`` and expose the local id separately for diagnostics.
    return jsonify(app="whl-capture", status="imported", id=capture_id,
                   entry_id=entry_id), 200


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
        log.info("LAN capture on 0.0.0.0:%d  ips=%s",
                 port, ", ".join(_lan_ips()) or "?")


if __name__ == "__main__":
    # Explicit startup completes protected migration before recovery and every
    # worker. Imported WSGI/test hosts cross the same barrier on first request.
    _ensure_engine_session()
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
    # Protected migration completed inside _ensure_engine_session, before any
    # worker above was allowed to start. Profile reconciliation now operates
    # directly on that protected store.
    _sync_profile_mistral_key()
    port = int(os.environ.get("WHL_PORT") or 5001)
    log.info("Library Tool on 127.0.0.1:%d - DATA_ROOT=%s", port, lib.DATA_ROOT)
    app.run(host="127.0.0.1", port=port, debug=False)
