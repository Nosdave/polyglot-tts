"""Management password: every state-changing HTTP call needs it.

Speech synthesis (Wyoming, POST /v1/audio/speech), the normalization preview
and all read-only routes stay open — Home Assistant, OpenClaw and scripts keep
working without credentials. Everything that changes state (voice
upload/delete, settings, pronunciation dictionary, restart) requires the
management password.

Where the password comes from
-----------------------------
1. POCKET_TTS_UI_TOKEN, if set (takes precedence; compared as given).
2. Otherwise the password set in the web UI. Only a salted hash (scrypt) is
   stored, as `ui_password.json` (mode 0600) next to settings.json.
3. While neither exists, the web UI can set the password once — but only
   during the setup window, the first minutes after the server has started
   (SETUP_WINDOW_MINUTES, override with POCKET_TTS_UI_SETUP_MINUTES, 0 = off).
   Outside the window, and for every management route until a password
   exists, the answer is 401/403: never open.
4. If the config directory is not writable, no hash can be stored: a token
   for this run only is generated and logged once.

Forgotten password: delete `ui_password.json` and restart — that reopens the
setup window.

How a client sends it
---------------------
    Authorization: Bearer <password>     (preferred)
    X-UI-Token: <password>               (legacy header, still accepted)
    ?token=<password>                    (legacy query parameter, still
                                          accepted; do not build such URLs —
                                          they end up in logs and history)

Enforcement is default-deny: `ManagementAuthMiddleware` rejects every request
with a method other than GET/HEAD/OPTIONS unless the route is listed in
`OPEN_UNSAFE_ROUTES`. A route added later is therefore protected without
further work, and the check runs before the request body is read.

Failed attempts are limited process-wide (MAX_FAILED_ATTEMPTS per
ATTEMPT_WINDOW_S). While the limit is hit, credentials are not evaluated at
all (429) — except the valid one coming from an address that has already
authenticated, so a logged-in browser is not locked out by someone else's
guessing.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from . import config_store

_LOGGER = logging.getLogger(__name__)

TOKEN_ENV = "POCKET_TTS_UI_TOKEN"
PASSWORD_FILENAME = "ui_password.json"

# In-browser first-run setup is possible for this long after the server has
# started, and only while no password exists. 0 disables it.
SETUP_WINDOW_MINUTES = 10.0
SETUP_WINDOW_ENV = "POCKET_TTS_UI_SETUP_MINUTES"
_MAX_SETUP_WINDOW_MINUTES = 24 * 60.0

MIN_PASSWORD_CHARS = 10
MAX_PASSWORD_CHARS = 256
# Below this length an operator-chosen POCKET_TTS_UI_TOKEN gets a warning.
_WEAK_TOKEN_CHARS = MIN_PASSWORD_CHARS

# Process-wide limit on failed password checks, and on setup calls.
MAX_FAILED_ATTEMPTS = 10
MAX_SETUP_ATTEMPTS = 10
ATTEMPT_WINDOW_S = 60.0

# scrypt parameters for new hashes (stored with each hash, so they can be
# raised later without invalidating existing files). ~16 MiB, tens of ms.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_PBKDF2_ITERATIONS = 600_000  # only used where hashlib has no scrypt
_KEY_BYTES = 32
_SALT_BYTES = 16

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# POST routes that need no password: synthesis clients, and the first-run
# setup, which guards itself (no password yet + setup window + rate limit).
OPEN_UNSAFE_ROUTES = frozenset(
    {
        ("POST", "/v1/audio/speech"),
        ("POST", "/v1/text/normalize"),
        ("POST", "/api/ui/setup"),
    }
)

UNAUTHORIZED_DETAIL = (
    "Management password required. Send it as 'Authorization: Bearer <password>'. "
    "See docs/WEB_UI.md."
)
NOT_SET_DETAIL = (
    "No management password has been set yet. Open the web UI to set one "
    "(possible for a few minutes after a server start), or set "
    f"{TOKEN_ENV}. Until then management calls are refused."
)
LIMITED_DETAIL = "Too many failed attempts. Wait a minute and try again."
_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}
_MAX_TRUSTED_CLIENTS = 64


class AuthError(Exception):
    """A setup / change-password request that cannot be carried out."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


_LOCK = threading.Lock()
_loaded = False
_record: dict | None = None  # parsed hash file
_record_invalid = False  # a hash file exists but cannot be used
_memory_token: str | None = None  # one-run token (config dir not writable)
_window_start: float | None = None
_announced = False
_mac_key = secrets.token_bytes(32)
_good_mac: bytes | None = None  # keyed digest of the password verified last
_trusted: OrderedDict[str, None] = OrderedDict()  # addresses that logged in
_failures: deque[float] = deque()
_setup_calls: deque[float] = deque()


def _now() -> float:
    return time.monotonic()


def password_path() -> Path:
    """The password hash lives next to the settings file."""
    return config_store.config_path().parent / PASSWORD_FILENAME


def _env_token() -> str | None:
    tok = os.environ.get(TOKEN_ENV, "").strip()
    return tok or None


# ── Hashing ─────────────────────────────────────────────────────────────────


def _new_record(password: str) -> dict:
    salt = secrets.token_bytes(_SALT_BYTES)
    if hasattr(hashlib, "scrypt"):
        record = {"v": 1, "algo": "scrypt", "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P}
    else:  # pragma: no cover — OpenSSL without scrypt
        record = {"v": 1, "algo": "pbkdf2_sha256", "iterations": _PBKDF2_ITERATIONS}
    record["salt"] = salt.hex()
    record["hash"] = _derive(password, record).hex()
    return record


def _derive(password: str, record: dict) -> bytes:
    data = password.encode("utf-8")
    salt = bytes.fromhex(record["salt"])
    if record["algo"] == "scrypt":
        n, r, p = int(record["n"]), int(record["r"]), int(record["p"])
        return hashlib.scrypt(
            data, salt=salt, n=n, r=r, p=p, dklen=_KEY_BYTES, maxmem=256 * n * r + 2**20
        )
    if record["algo"] == "pbkdf2_sha256":
        return hashlib.pbkdf2_hmac("sha256", data, salt, int(record["iterations"]), _KEY_BYTES)
    raise ValueError(f"unknown hash algorithm {record['algo']!r}")


def _record_usable(record) -> bool:
    """Shape and bounds of a stored record (bounds keep a tampered file from
    turning every check into a huge computation)."""
    try:
        if not isinstance(record, dict) or record.get("v") != 1:
            return False
        if len(bytes.fromhex(record["salt"])) < 8 or len(bytes.fromhex(record["hash"])) < 16:
            return False
        if record["algo"] == "scrypt":
            n, r, p = int(record["n"]), int(record["r"]), int(record["p"])
            return 2 <= n <= 2**17 and n & (n - 1) == 0 and 1 <= r <= 8 and 1 <= p <= 4
        if record["algo"] == "pbkdf2_sha256":
            return 1000 <= int(record["iterations"]) <= 10_000_000
    except (KeyError, TypeError, ValueError):
        pass
    return False


def _mac(value: str) -> bytes:
    return hmac.new(_mac_key, value.encode("utf-8"), hashlib.sha256).digest()


def _equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ── Files ───────────────────────────────────────────────────────────────────


def _write_private_file(path: Path, text: str) -> None:
    """Create/replace a file atomically with mode 0600.

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
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_record(path: Path) -> tuple[dict | None, bool]:
    """(record, invalid). invalid = a file is there but cannot be used."""
    try:
        with open(path, encoding="utf-8") as fh:
            record = json.loads(fh.read(8192))
    except (FileNotFoundError, NotADirectoryError):
        return None, False
    except (OSError, ValueError) as e:
        if isinstance(e, PermissionError) and not os.access(path.parent, os.X_OK):
            # The directory itself is off limits: nothing can be stored there,
            # which the caller handles as "not writable".
            return None, False
        _LOGGER.error("Password file %s cannot be read (%s)", path, e)
        return None, True
    if not _record_usable(record):
        _LOGGER.error("Password file %s is not a valid password record", path)
        return None, True
    # Tighten permissions left too open (e.g. a file restored by hand).
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            os.chmod(path, 0o600)
            _LOGGER.warning("Password file %s had mode %o — set to 0600", path, mode)
    except OSError:
        pass
    return record, False


def _config_dir_writable() -> bool:
    probe = password_path().with_name(f".{PASSWORD_FILENAME}.probe.{secrets.token_hex(4)}")
    try:
        _write_private_file(probe, "")
        os.unlink(probe)
        return True
    except OSError:
        return False


def _ensure_loaded() -> None:
    """Read the stored state once per process. Caller holds _LOCK."""
    global _loaded, _record, _record_invalid, _memory_token, _window_start
    if _loaded:
        return
    _loaded = True
    if _window_start is None:
        _window_start = _now()
    _record, _record_invalid = _read_record(password_path())
    if _record is None and not _record_invalid and not _env_token() and not _config_dir_writable():
        _memory_token = secrets.token_urlsafe(32)
        # The one place a credential is logged: without a writable config
        # directory no password can be stored, and there is no other way for
        # the operator to learn the token.
        _LOGGER.warning(
            "The config directory %s is not writable, so no management password "
            "can be stored. One-time password for this run only: %s — set %s, or "
            "make the directory writable (container user, UID 10001) and restart "
            "to set a password in the web UI.",
            password_path().parent,
            _memory_token,
            TOKEN_ENV,
        )


# ── State ───────────────────────────────────────────────────────────────────


def setup_window_seconds() -> float:
    """Length of the setup window; 0 = in-browser setup disabled."""
    raw = os.environ.get(SETUP_WINDOW_ENV, "").strip()
    minutes = SETUP_WINDOW_MINUTES
    if raw:
        try:
            minutes = float(raw.replace(",", "."))
        except ValueError:
            minutes = SETUP_WINDOW_MINUTES
        if not 0 <= minutes <= _MAX_SETUP_WINDOW_MINUTES:  # also catches nan
            minutes = SETUP_WINDOW_MINUTES
    return minutes * 60.0


def _setup_seconds_left_locked() -> float:
    if _window_start is None:
        return 0.0
    return max(0.0, _window_start + setup_window_seconds() - _now())


def _mode_locked() -> str:
    if _env_token():
        return "env"
    if _record is not None:
        return "password"
    if _memory_token is not None:
        return "memory"
    if _record_invalid:
        return "closed"
    return "setup" if _setup_seconds_left_locked() > 0 else "closed"


def mode() -> str:
    """Where the password comes from / what state the server is in.

    "env"      POCKET_TTS_UI_TOKEN is set
    "password" a password was set in the web UI (hash file)
    "setup"    no password yet, the setup window is open
    "closed"   no password yet, setup not possible (window over or disabled,
               or the hash file is unusable) — management stays refused
    "memory"   config directory not writable: one-run token from the log
    """
    with _LOCK:
        _ensure_loaded()
        return _mode_locked()


def setup_seconds_left() -> int:
    with _LOCK:
        _ensure_loaded()
        return int(_setup_seconds_left_locked()) if _mode_locked() == "setup" else 0


def announce() -> None:
    """Start the setup window and log ONCE per process what applies."""
    global _announced, _window_start
    with _LOCK:
        if _announced:
            return
        _announced = True
        _window_start = _now()
        _ensure_loaded()
        current = _mode_locked()
        invalid = _record_invalid
    path = password_path()
    if current == "env":
        _LOGGER.info("Management password: taken from %s", TOKEN_ENV)
        if len(_env_token() or "") < _WEAK_TOKEN_CHARS:
            _LOGGER.warning(
                "%s is shorter than %d characters — use a longer value",
                TOKEN_ENV,
                _WEAK_TOKEN_CHARS,
            )
    elif current == "password":
        _LOGGER.info(
            "Management password: set (hash in %s). Forgotten? Delete that file "
            "and restart to set a new one.",
            path,
        )
    elif current == "setup":
        _LOGGER.warning(
            "No management password set yet. Open the web UI (/ui) within %d "
            "minute(s) to set one — whoever gets there first sets it — or set %s. "
            "Until a password exists, management calls are refused.",
            round(setup_window_seconds() / 60),
            TOKEN_ENV,
        )
    elif current == "closed" and invalid:
        _LOGGER.error(
            "Management calls are refused: %s is unusable. Delete it and restart "
            "to set a new password, or set %s.",
            path,
            TOKEN_ENV,
        )
    elif current == "closed":
        _LOGGER.warning(
            "No management password set and in-browser setup is disabled (%s=0). "
            "Management calls are refused until %s is set.",
            SETUP_WINDOW_ENV,
            TOKEN_ENV,
        )
    # "memory": _ensure_loaded already logged the one-run token.


def _reset_for_tests() -> None:
    global _loaded, _record, _record_invalid, _memory_token, _window_start
    global _announced, _good_mac
    with _LOCK:
        _loaded = False
        _record = None
        _record_invalid = False
        _memory_token = None
        _window_start = None
        _announced = False
        _good_mac = None
        _trusted.clear()
        _failures.clear()
        _setup_calls.clear()


# ── Checking a presented password ───────────────────────────────────────────


def _prune(events: deque[float]) -> None:
    cutoff = _now() - ATTEMPT_WINDOW_S
    while events and events[0] <= cutoff:
        events.popleft()


def _blocked_locked() -> bool:
    _prune(_failures)
    return len(_failures) >= MAX_FAILED_ATTEMPTS


def _trust_locked(client: str | None) -> None:
    if not client:
        return
    _trusted[client] = None
    _trusted.move_to_end(client)
    while len(_trusted) > _MAX_TRUSTED_CLIENTS:
        _trusted.popitem(last=False)


def verify(presented: str | None, client: str | None = None) -> str:
    """Check a presented password.

    Returns "ok", "bad" (wrong or missing), "unset" (no password exists yet,
    nothing can be valid) or "limited" (too many failures — not evaluated).
    Comparisons are constant-time; a stored hash is checked by hashing the
    presented value. Blocking work (scrypt) happens outside the lock.
    """
    global _good_mac
    with _LOCK:
        _ensure_loaded()
        current = _mode_locked()
        if current in ("setup", "closed"):
            return "unset"
        if not presented:
            return "bad"
        if len(presented) > 4 * MAX_PASSWORD_CHARS:
            _failures.append(_now())  # nothing that long can be valid; don't hash it
            return "bad"
        # Cheap, definitive where the reference is at hand; None = needs hashing.
        fast: bool | None
        if current == "env":
            fast = _equal(presented, _env_token() or "")
        elif current == "memory":
            fast = _equal(presented, _memory_token or "")
        else:
            fast = (
                True
                if _good_mac is not None and hmac.compare_digest(_mac(presented), _good_mac)
                else None
            )
        blocked = _blocked_locked()
        if fast is True and (not blocked or client in _trusted):
            _trust_locked(client)
            return "ok"
        if blocked:
            return "limited"
        record = _record
    ok = fast
    if ok is None:
        ok = hmac.compare_digest(_derive(presented, record), bytes.fromhex(record["hash"]))
    with _LOCK:
        if ok and (fast is not None or record is _record):
            if fast is None:
                _good_mac = _mac(presented)
            _trust_locked(client)
            return "ok"
        _failures.append(_now())
        return "bad"


def validate_new_password(password) -> str:
    """Rules for a password set through the UI. Raises AuthError(400)."""
    if not isinstance(password, str):
        raise AuthError(400, "Password must be a string.")
    if len(password) < MIN_PASSWORD_CHARS:
        raise AuthError(400, f"Password must have at least {MIN_PASSWORD_CHARS} characters.")
    if len(password) > MAX_PASSWORD_CHARS:
        raise AuthError(400, f"Password must have at most {MAX_PASSWORD_CHARS} characters.")
    # The password travels in an HTTP header: browsers and command-line
    # clients only agree on the encoding for plain ASCII.
    if any(not 32 <= ord(c) <= 126 for c in password) or password != password.strip():
        raise AuthError(
            400,
            "Use letters, digits and common symbols only (no umlauts, accents "
            "or emoji), without a space at the start or end.",
        )
    return password


def _store_password_locked(password: str, client: str | None) -> None:
    global _record, _record_invalid, _good_mac
    record = _new_record(password)
    try:
        _write_private_file(password_path(), json.dumps(record) + "\n")
    except OSError as e:
        raise AuthError(
            500,
            f"Could not store the password in {password_path().parent}: {e}. The "
            "config volume must be writable by the container user (UID 10001).",
        ) from e
    _record = record
    _record_invalid = False
    _good_mac = _mac(password)
    _trusted.clear()
    _failures.clear()
    _trust_locked(client)


def set_initial_password(password, client: str | None = None) -> None:
    """First-run setup. Only while no password exists and the window is open."""
    with _LOCK:
        _ensure_loaded()
        _prune(_setup_calls)
        if len(_setup_calls) >= MAX_SETUP_ATTEMPTS:
            raise AuthError(429, LIMITED_DETAIL)
        _setup_calls.append(_now())
        current = _mode_locked()
        if current == "env":
            raise AuthError(403, f"The password is set through {TOKEN_ENV}; it cannot be set here.")
        if current == "password":
            raise AuthError(403, "A password has already been set. Log in instead.")
        if current == "memory":
            raise AuthError(
                403,
                "The config directory is not writable, so no password can be stored. "
                f"Use the one-time password from the server log or set {TOKEN_ENV}.",
            )
        if current == "closed":
            if _record_invalid:
                hint = "The stored password file is unusable — delete it and restart the server."
            elif setup_window_seconds() <= 0:
                hint = f"Setting a password in the browser is disabled — set {TOKEN_ENV}."
            else:
                hint = (
                    "The setup window is over. Restart the server to reopen it for "
                    f"{round(setup_window_seconds() / 60)} minute(s), or set {TOKEN_ENV}."
                )
            raise AuthError(403, hint)
        validate_new_password(password)
        _store_password_locked(password, client)
    _LOGGER.warning("Management password set through the web UI (first-run setup)")


def change_password(current_password, new_password, client: str | None = None) -> None:
    """Replace the stored password. The caller is already authenticated; the
    current password is asked again so an unattended browser is not enough."""
    state = mode()
    if state == "env":
        raise AuthError(409, f"The password is set through {TOKEN_ENV}; change it there.")
    if state != "password":
        raise AuthError(409, "No password is stored on this server, so none can be changed.")
    validate_new_password(new_password)
    result = verify(current_password if isinstance(current_password, str) else None, client)
    if result == "limited":
        raise AuthError(429, LIMITED_DETAIL)
    if result != "ok":
        raise AuthError(403, "The current password is wrong.")
    with _LOCK:
        _store_password_locked(new_password, client)
    _LOGGER.warning("Management password changed through the web UI")


# ── Request checks ──────────────────────────────────────────────────────────


def extract_token(request: Request) -> str | None:
    """Password a client sent: Bearer header, legacy X-UI-Token, legacy ?token=."""
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    legacy = request.headers.get("x-ui-token", "").strip()
    if legacy:
        return legacy
    return request.query_params.get("token") or None


def client_host(request: Request) -> str | None:
    return request.client.host if request.client else None


def authorize(request: Request) -> str:
    """verify() for the credential of a request."""
    return verify(extract_token(request), client_host(request))


_REFUSALS = {
    "bad": (401, UNAUTHORIZED_DETAIL, _UNAUTHORIZED_HEADERS),
    "unset": (401, NOT_SET_DETAIL, _UNAUTHORIZED_HEADERS),
    "limited": (429, LIMITED_DETAIL, {"Retry-After": str(int(ATTEMPT_WINDOW_S))}),
}


def require_token(request: Request) -> None:
    """FastAPI dependency for management routes. Deliberately a plain `def`:
    FastAPI runs it in a worker thread, so hashing never blocks the event loop."""
    result = authorize(request)
    if result != "ok":
        status, detail, headers = _REFUSALS[result]
        raise HTTPException(status, detail, headers=headers)


def is_open_request(method: str, path: str) -> bool:
    """True if this request needs no password under the default-deny rule."""
    method = method.upper()
    if method in SAFE_METHODS:
        return True
    # "/v1/audio/speech/" only gets the router's redirect to the canonical
    # path, so it may pass like the canonical one.
    return (method, path.rstrip("/") or "/") in OPEN_UNSAFE_ROUTES


class ManagementAuthMiddleware:
    """Reject unsafe-method requests without the valid password (default-deny).

    Pure ASGI, so the refusal is sent before any of the request body is read —
    an unauthenticated client cannot make the server spool an upload.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        needs_password = scope["type"] == "http" and not is_open_request(
            scope.get("method", ""), scope.get("path", "")
        )
        if needs_password:
            # In a thread: checking a stored hash takes tens of milliseconds.
            result = await asyncio.to_thread(authorize, Request(scope))
            if result != "ok":
                status, detail, headers = _REFUSALS[result]
                response = JSONResponse({"detail": detail}, status_code=status, headers=headers)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
