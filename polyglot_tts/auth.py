"""Management token: every state-changing HTTP call needs it.

Speech synthesis (Wyoming, POST /v1/audio/speech), the normalization preview
and all read-only routes stay open — Home Assistant, OpenClaw and scripts keep
working without a token. Everything that changes state (voice upload/delete,
settings, pronunciation dictionary, restart) requires the management token.

Where the token comes from
--------------------------
1. POCKET_TTS_UI_TOKEN, if set (takes precedence).
2. Otherwise a random token is generated on first start and stored in the
   config directory, next to settings.json, as `ui_token` (mode 0600). The log
   names the file, never the value.
3. If that directory is not writable, a token for this run only is generated
   and logged once, so the operator can still log in.

How a client sends it
---------------------
    Authorization: Bearer <token>        (preferred)
    X-UI-Token: <token>                  (legacy header, still accepted)
    ?token=<token>                       (legacy query parameter, still
                                          accepted; do not build such URLs —
                                          they end up in logs and history)

Enforcement is default-deny: `ManagementAuthMiddleware` rejects every request
with a method other than GET/HEAD/OPTIONS unless the route is listed in
`OPEN_UNSAFE_ROUTES`. A route added later is therefore protected without
further work, and the check runs before the request body is read.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import os
import secrets
import stat
import threading
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from . import config_store

_LOGGER = logging.getLogger(__name__)

TOKEN_ENV = "POCKET_TTS_UI_TOKEN"
TOKEN_FILENAME = "ui_token"

# A token read from the file must look like one: printable ASCII without
# whitespace, in this length range. Anything else is replaced by a fresh one.
_MIN_FILE_TOKEN_CHARS = 16
_MAX_TOKEN_CHARS = 512
# Below this length an operator-chosen POCKET_TTS_UI_TOKEN gets a warning.
_WEAK_TOKEN_CHARS = 16

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# State-less POST routes that stay open for synthesis clients.
OPEN_UNSAFE_ROUTES = frozenset(
    {
        ("POST", "/v1/audio/speech"),
        ("POST", "/v1/text/normalize"),
    }
)

UNAUTHORIZED_DETAIL = (
    "Management token required. Send it as 'Authorization: Bearer <token>'. "
    "See docs/WEB_UI.md for where to find it."
)
_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}

_LOCK = threading.Lock()
_file_token: str | None = None  # token read from / written to the token file
_source = "file"  # "file" | "memory" (config dir not writable)
_announced = False


def token_path() -> Path:
    """The generated token lives next to the settings file."""
    return config_store.config_path().parent / TOKEN_FILENAME


def _env_token() -> str | None:
    tok = os.environ.get(TOKEN_ENV, "").strip()
    return tok or None


def _looks_like_token(tok: str) -> bool:
    return _MIN_FILE_TOKEN_CHARS <= len(tok) <= _MAX_TOKEN_CHARS and all(
        33 <= ord(c) <= 126 for c in tok
    )


def _read_token_file(path: Path) -> str | None:
    """Return the stored token, or None if the file is missing or unusable."""
    try:
        with open(path, encoding="utf-8") as fh:
            tok = fh.read(_MAX_TOKEN_CHARS + 2).strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        _LOGGER.warning("Token file %s is not readable (%s) — generating a new one", path, e)
        return None
    if not _looks_like_token(tok):
        _LOGGER.warning("Token file %s does not hold a usable token — generating a new one", path)
        return None
    # Tighten permissions left too open (e.g. a file created by hand).
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            os.chmod(path, 0o600)
            _LOGGER.warning(
                "Token file %s was readable by group/others (mode %o) — set to 0600", path, mode
            )
    except OSError:
        pass
    return tok


def _write_token_file(path: Path, tok: str) -> None:
    """Create the token file atomically with mode 0600.

    The content goes into a private temp file in the same directory (created
    with O_EXCL and mode 0600, so it is never visible with wider permissions)
    and is then renamed over the target.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            os.write(fd, (tok + "\n").encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _load_or_create() -> str:
    """Read the token file, or generate + persist a token. Caller holds _LOCK."""
    global _source
    path = token_path()
    tok = _read_token_file(path)
    if tok:
        _source = "file"
        return tok
    tok = secrets.token_urlsafe(32)
    try:
        _write_token_file(path, tok)
        _source = "file"
        _LOGGER.info("Generated a new management token")
    except OSError as e:
        _source = "memory"
        # The one place the value is logged: without a writable config
        # directory there is no other way for the operator to learn it.
        _LOGGER.warning(
            "Could not store the management token in %s (%s). Token for this "
            "run only: %s — set %s or make the config directory writable "
            "(container user, UID 10001) to get a stable token.",
            path,
            e,
            tok,
            TOKEN_ENV,
        )
    return tok


def get_token() -> str:
    """The token management calls must present. Never empty."""
    global _file_token
    env = _env_token()
    if env:
        return env
    with _LOCK:
        if _file_token is None:
            _file_token = _load_or_create()
        return _file_token


def token_source() -> str:
    """ "env", "file" or "memory" — where the active token comes from."""
    if _env_token():
        return "env"
    get_token()
    return _source


def announce() -> None:
    """Resolve the token and log ONCE per process where to find it."""
    global _announced
    get_token()
    with _LOCK:
        if _announced:
            return
        _announced = True
    source = token_source()
    if source == "env":
        _LOGGER.info("Management token: taken from %s", TOKEN_ENV)
        if len(_env_token() or "") < _WEAK_TOKEN_CHARS:
            _LOGGER.warning(
                "%s is shorter than %d characters — use a long random value",
                TOKEN_ENV,
                _WEAK_TOKEN_CHARS,
            )
    elif source == "file":
        _LOGGER.info(
            "Management token: stored in %s (mode 0600). Voice upload/delete, "
            "settings and restart need it; synthesis does not. Set %s to "
            "choose your own.",
            token_path(),
            TOKEN_ENV,
        )
    # "memory": _load_or_create already logged the one-run token.


def _reset_for_tests() -> None:
    global _file_token, _source, _announced
    with _LOCK:
        _file_token = None
        _source = "file"
        _announced = False


# ── Request checks ──────────────────────────────────────────────────────────


def extract_token(request: Request) -> str | None:
    """Token a client sent: Bearer header, legacy X-UI-Token, legacy ?token=."""
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    legacy = request.headers.get("x-ui-token", "").strip()
    if legacy:
        return legacy
    return request.query_params.get("token") or None


def token_matches(provided: str | None) -> bool:
    """Constant-time comparison against the active token."""
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), get_token().encode("utf-8"))


def is_authorized(request: Request) -> bool:
    return token_matches(extract_token(request))


def require_token(request: Request) -> None:
    """FastAPI dependency for management routes. 401 without a valid token."""
    if not is_authorized(request):
        raise HTTPException(401, UNAUTHORIZED_DETAIL, headers=_UNAUTHORIZED_HEADERS)


def is_open_request(method: str, path: str) -> bool:
    """True if this request needs no token under the default-deny rule."""
    method = method.upper()
    if method in SAFE_METHODS:
        return True
    # "/v1/audio/speech/" only gets the router's redirect to the canonical
    # path, so it may pass like the canonical one.
    return (method, path.rstrip("/") or "/") in OPEN_UNSAFE_ROUTES


class ManagementAuthMiddleware:
    """Reject unsafe-method requests without a valid token (default-deny).

    Pure ASGI, so the 401 is sent before any of the request body is read —
    an unauthenticated client cannot make the server spool an upload.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        needs_token = scope["type"] == "http" and not is_open_request(
            scope.get("method", ""), scope.get("path", "")
        )
        if needs_token and not is_authorized(Request(scope)):
            response = JSONResponse(
                {"detail": UNAUTHORIZED_DETAIL}, status_code=401, headers=_UNAUTHORIZED_HEADERS
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
