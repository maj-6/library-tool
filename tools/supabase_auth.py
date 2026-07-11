"""Supabase Auth (GoTrue) for the desktop app: sign in, persist, refresh.

The desktop signs a real user in with email + password and keeps the session
in a JSON file under DATA_ROOT, so contributions pushed to the cloud carry the
user's identity and pass row-level security as `authenticated` -- no policy has
to trust a plain-text name.

Same conventions as supabase_sync.py: plain urllib, a cfg dict of
{"url": ..., "key": ...}, readable AuthError messages. The `key` here is the
project API key GoTrue wants in the `apikey` header -- the anon key is the
right one, but the service key also works, so callers pass whichever the
settings hold. The session tokens returned are the USER's, not the project's.

One subtlety worth the comment: Supabase rotates the refresh token on every
refresh, and reusing an old one outside the grace window revokes the whole
session family. So `refresh()` results must be persisted immediately, and
callers serialize refreshes behind a lock.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 20.0


class AuthError(Exception):
    """`status` carries the HTTP status of a protocol rejection (400 wrong
    password, 401/403 revoked token, ...). Transport failures — offline, DNS,
    a captive portal answering HTML — leave it None. Callers MUST distinguish
    the two: a rejection means the credential is dead; a transport failure
    means nothing at all about the credential, so destroying a session over
    one signs the user out every time the laptop sleeps through a token
    expiry."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def _cfg(cfg: dict) -> tuple[str, dict]:
    url = str(cfg.get("url") or "").strip().rstrip("/")
    key = str(cfg.get("key") or "").strip()
    if not url or not key:
        raise AuthError("Supabase URL / key not configured")
    return url, {"apikey": key, "Content-Type": "application/json"}


def _post(cfg: dict, path: str, payload: dict, bearer: str = "") -> dict:
    url, headers = _cfg(cfg)
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(f"{url}/auth/v1/{path}",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except urllib.error.HTTPError as exc:
        raise AuthError(_readable(exc), status=exc.code)
    except Exception as exc:
        raise AuthError(f"{type(exc).__name__}: {exc}")


def _readable(exc: urllib.error.HTTPError) -> str:
    """GoTrue errors are JSON with a msg under one of several keys; surface the
    text, not the envelope, because it lands in the login dialog verbatim."""
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
        msg = (body.get("msg") or body.get("message")
               or body.get("error_description") or body.get("error") or "")
        if msg:
            return str(msg)
    except Exception:
        pass
    return f"HTTP {exc.code}"


def _session(body: dict) -> dict:
    """Normalize a GoTrue token response into what we persist."""
    user = body.get("user") or {}
    meta = user.get("user_metadata") or {}
    return {
        "access_token": body.get("access_token") or "",
        "refresh_token": body.get("refresh_token") or "",
        # absolute deadline; GoTrue also sends relative expires_in
        "expires_at": int(body.get("expires_at")
                          or time.time() + float(body.get("expires_in") or 3600)),
        "user_id": user.get("id") or "",
        "email": user.get("email") or "",
        "display_name": str(meta.get("display_name") or "").strip(),
    }


def sign_in(cfg: dict, email: str, password: str) -> dict:
    body = _post(cfg, "token?grant_type=password",
                 {"email": email, "password": password})
    if not body.get("access_token"):
        raise AuthError("no session returned")
    return _session(body)


def sign_up(cfg: dict, email: str, password: str, display_name: str,
            redirect_to: str = "") -> dict | None:
    """Create an account. Returns a session, or None when the project requires
    email confirmation first (the default) -- the caller tells the user to go
    click the link and sign in afterwards.

    `redirect_to` is where the confirmation link sends the browser after GoTrue
    verifies the token. Passed as a query param on /signup; GoTrue honours it
    only when it matches the project's allow-listed Redirect URLs, otherwise it
    falls back to the Site URL. Without either pointing somewhere real, the
    default (localhost:3000) is what refuses the connection."""
    path = "signup"
    if redirect_to:
        path += "?redirect_to=" + urllib.parse.quote(redirect_to, safe="")
    body = _post(cfg, path, {
        "email": email, "password": password,
        "data": {"display_name": display_name},
    })
    if body.get("access_token"):
        return _session(body)
    # Enumeration protection: for an ALREADY-REGISTERED email GoTrue answers
    # 200 with an obfuscated user whose identities list is empty -- shaped
    # exactly like confirmation-required. Without this check the user is told
    # to watch for an email that will never come.
    user = body.get("user") or body
    if isinstance(user, dict) and user.get("identities") == []:
        raise AuthError("this email may already have an account — try signing in",
                        status=400)
    return None


def refresh(cfg: dict, refresh_token: str) -> dict:
    body = _post(cfg, "token?grant_type=refresh_token",
                 {"refresh_token": refresh_token})
    if not body.get("access_token"):
        raise AuthError("refresh returned no session")
    return _session(body)


def sign_out(cfg: dict, access_token: str) -> None:
    """Revoke the session server-side. Best-effort: the local file is deleted
    either way, and an already-dead token is not worth an error dialog."""
    try:
        _post(cfg, "logout", {}, bearer=access_token)
    except AuthError:
        pass


# --- authed PostgREST, for the tables RLS opens to `authenticated` ---------------

def rest(cfg: dict, access_token: str, method: str, path: str,
         payload=None, prefer: str = "", timeout: float = TIMEOUT) -> list | dict | None:
    """One REST call as the signed-in user (Bearer = user JWT, apikey = project)."""
    url, headers = _cfg(cfg)
    headers["Authorization"] = f"Bearer {access_token}"
    if prefer:
        headers["Prefer"] = prefer
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"{url}/rest/v1/{path}", data=body,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8", "replace")) if raw else None
    except urllib.error.HTTPError as exc:
        raise AuthError(_readable(exc), status=exc.code)
    except Exception as exc:
        raise AuthError(f"{type(exc).__name__}: {exc}")
