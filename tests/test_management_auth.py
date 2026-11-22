"""Management password: every state-changing HTTP route needs it, synthesis
and read-only routes do not. Also covers where the password comes from (env
variable, first-run setup in the browser, stored as a hash), changing it, the
rate limit, upload and settings validation, and the forms of the web UI.

No model is loaded: the app runs against a PolyglotCore with a dummy model,
and synthesis is exercised through the chime marker (no inference).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from polyglot_tts import auth, config_store
from polyglot_tts.core import CHIME_MARKER, PolyglotCore
from polyglot_tts.http_server import build_app
from polyglot_tts.ui_server import mount_ui

WEB_DIR = Path(auth.__file__).parent / "web"
ENV_TOKEN = "test-token-0123456789abcdef"
PASSWORD = "correct horse 42!"
NEW_PASSWORD = "battery-staple-2026"


class _FakeModel:
    sample_rate = 24000
    temp = 0.3


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    """Isolated config dir + clean env; restores everything afterwards."""
    for key in (*config_store.EDITABLE_KEYS, auth.TOKEN_ENV, auth.SETUP_WINDOW_ENV):
        monkeypatch.delenv(key, raising=False)  # also restores on teardown
    monkeypatch.setattr(auth, "_SCRYPT_N", 2**10)  # cheap hashes keep the suite fast
    d = tmp_path / "config"
    monkeypatch.setenv("POCKET_TTS_CONFIG_FILE", str(d / "settings.json"))
    auth._reset_for_tests()
    yield d
    auth._reset_for_tests()


@pytest.fixture
def voices_dir(tmp_path):
    d = tmp_path / "voices-extra"
    d.mkdir()
    return d


@pytest.fixture
def core(voices_dir):
    return PolyglotCore(
        models={"german_24l": _FakeModel()},
        voice_states={},
        default_voice="eve",
        advertised_bcp47=["de"],
        voices_extra_dir=voices_dir,
    )


@pytest.fixture
def app(cfg_dir, core, voices_dir, monkeypatch):
    monkeypatch.setenv(auth.TOKEN_ENV, ENV_TOKEN)
    application = build_app(core, voices_dir)
    mount_ui(application, core)
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


def _client(core, voices_dir) -> TestClient:
    application = build_app(core, voices_dir)
    mount_ui(application, core)
    return TestClient(application)


@pytest.fixture
def fresh(cfg_dir, core, voices_dir):
    """A server as on its first start: no env password, none stored yet."""
    return _client(core, voices_dir)


@pytest.fixture
def clock(monkeypatch):
    """Replaces the clock of the auth module. Request it BEFORE `fresh` /
    `client` so the setup window starts on this clock."""
    t = {"now": 50_000.0}
    monkeypatch.setattr(auth, "_now", lambda: t["now"])
    return t


def _bearer(token: str = ENV_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _wav_upload(filename: str = "sample.wav", data: bytes = b"RIFF0000WAVE"):
    return {"file": (filename, data, "audio/wav")}


@pytest.fixture
def no_restart(monkeypatch):
    """The restart route re-execs the process from a thread — never in tests."""
    started = []

    class _Thread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            started.append(True)

    # Only ui_server's view of `threading` — the real module must keep working
    # (the auth check itself runs in a worker thread).
    from polyglot_tts import ui_server

    monkeypatch.setattr(ui_server, "threading", SimpleNamespace(Thread=_Thread))
    return started


MUTATING = [
    ("POST", "/v1/audio/voices"),
    ("DELETE", "/v1/audio/voices/somevoice"),
    ("POST", "/api/ui/config"),
    ("POST", "/api/ui/replacements"),
    ("POST", "/api/ui/restart"),
    ("POST", "/api/ui/password"),
]


# ── Where the password comes from ───────────────────────────────────────────


def test_first_start_is_closed_until_a_password_is_set(fresh, cfg_dir, no_restart):
    """Nothing configured: management is refused (never open), the page is
    told to offer the setup form, synthesis still works."""
    status = fresh.get("/api/ui/status").json()
    assert status["auth_mode"] == "setup" and status["auth_enabled"] is True
    assert 0 < status["setup_seconds_left"] <= 600
    assert status["min_password_length"] == 10
    for method, path in MUTATING:
        for headers in ({}, _bearer("any-value-at-all-123")):
            r = fresh.request(method, path, headers=headers)
            assert r.status_code == 401, (method, path)
            assert "No management password" in r.json()["detail"]
    assert fresh.get("/api/ui/session", headers=_bearer("x" * 12)).status_code == 401
    assert no_restart == []
    assert list(cfg_dir.iterdir()) == []  # nothing stored, no probe file left
    r = fresh.post("/v1/audio/speech", json={"input": CHIME_MARKER, "response_format": "wav"})
    assert r.status_code == 200


def test_setup_happy_path(fresh, cfg_dir, no_restart):
    r = fresh.post("/api/ui/setup", json={"password": PASSWORD})
    assert r.status_code == 200 and r.json() == {"authenticated": True}

    # stored as a salted hash, private, written atomically (no temp file left)
    path = cfg_dir / "ui_password.json"
    assert path == auth.password_path()
    assert [p.name for p in cfg_dir.iterdir()] == ["ui_password.json"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_text()
    record = json.loads(raw)
    assert record["algo"] == "scrypt" and len(bytes.fromhex(record["salt"])) >= 16
    assert len(bytes.fromhex(record["hash"])) == 32
    assert PASSWORD not in raw and "password" not in record

    status = fresh.get("/api/ui/status").json()
    assert status["auth_mode"] == "password" and status["setup_seconds_left"] == 0

    # the password is the Bearer value for the UI and for API clients
    assert fresh.get("/api/ui/session", headers=_bearer(PASSWORD)).status_code == 200
    assert fresh.get("/api/ui/session", headers={"X-UI-Token": PASSWORD}).status_code == 200
    assert fresh.get("/api/ui/session", headers=_bearer(PASSWORD + "x")).status_code == 401
    assert fresh.get("/api/ui/session").status_code == 401
    assert fresh.post("/api/ui/restart").status_code == 401
    assert fresh.post("/api/ui/restart", headers=_bearer(PASSWORD)).status_code == 200
    assert no_restart == [True]

    # setup works exactly once
    r = fresh.post("/api/ui/setup", json={"password": "another-password-1"})
    assert r.status_code == 403 and "already" in r.json()["detail"]
    assert fresh.get("/api/ui/session", headers=_bearer("another-password-1")).status_code == 401
    assert json.loads(path.read_text()) == record


def test_password_survives_restart_and_is_never_logged(fresh, cfg_dir, caplog):
    caplog.set_level(logging.DEBUG)
    assert fresh.post("/api/ui/setup", json={"password": PASSWORD}).status_code == 200
    record = json.loads((cfg_dir / "ui_password.json").read_text())
    auth._reset_for_tests()  # "restart": module state gone, file stays
    auth.announce()
    auth.announce()
    assert auth.mode() == "password"
    assert auth.verify(PASSWORD) == "ok" and auth.verify(PASSWORD + "x") == "bad"
    assert caplog.text.count("Management password: set") == 1  # announced once
    assert str(cfg_dir / "ui_password.json") in caplog.text
    for secret in (PASSWORD, record["hash"], record["salt"]):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    "body",
    [
        {"password": "too-short"},  # 9 characters
        {"password": ""},
        {"password": "pässwort-mit-umlaut"},  # not plain ASCII
        {"password": "emoji-password-🔑🔑"},
        {"password": " leading-space-pw"},
        {"password": "trailing-space-pw "},
        {"password": "tab\tin-the-password"},
        {"password": "x" * 300},
        {"password": 12345678901234},
        {"password": None},
        {"passwort": PASSWORD},
        {},
    ],
)
def test_setup_rejects_unusable_passwords(fresh, cfg_dir, body):
    r = fresh.post("/api/ui/setup", json=body)
    assert r.status_code == 400, r.text
    assert fresh.get("/api/ui/status").json()["auth_mode"] == "setup"
    assert list(cfg_dir.iterdir()) == []


def test_setup_body_is_checked_and_size_limited(fresh, cfg_dir):
    as_json = {"Content-Type": "application/json"}
    assert fresh.post("/api/ui/setup", content=b"{not json", headers=as_json).status_code == 400
    # only application/json: the content types a cross-site form can send are refused
    body = json.dumps({"password": PASSWORD})
    for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
        r = fresh.post("/api/ui/setup", content=body, headers={"Content-Type": content_type})
        assert r.status_code == 415, content_type
    assert fresh.post("/api/ui/setup", content=body).status_code == 415
    assert fresh.post("/api/ui/setup", json=["a", "list"]).status_code == 400
    big = {"password": PASSWORD, "padding": "x" * 10_000}
    assert fresh.post("/api/ui/setup", json=big).status_code == 413
    assert list(cfg_dir.iterdir()) == []


def test_setup_closes_after_the_window(clock, fresh, cfg_dir, no_restart):
    clock["now"] += 599
    assert fresh.get("/api/ui/status").json()["auth_mode"] == "setup"
    clock["now"] += 2  # 10 minutes after start are over
    status = fresh.get("/api/ui/status").json()
    assert status["auth_mode"] == "closed" and status["setup_seconds_left"] == 0
    r = fresh.post("/api/ui/setup", json={"password": PASSWORD})
    assert r.status_code == 403
    assert "Restart" in r.json()["detail"] and auth.TOKEN_ENV in r.json()["detail"]
    assert list(cfg_dir.iterdir()) == []
    # still closed, not open: no password can be valid
    for method, path in MUTATING:
        assert fresh.request(method, path, headers=_bearer(PASSWORD)).status_code == 401
    assert no_restart == []


def test_setup_works_until_the_last_moment_of_the_window(clock, fresh):
    clock["now"] += 599
    assert fresh.post("/api/ui/setup", json={"password": PASSWORD}).status_code == 200


def test_a_restart_reopens_the_setup_window(clock, fresh):
    clock["now"] += 3600
    assert auth.mode() == "closed"
    auth._reset_for_tests()  # new process …
    auth.announce()  # … builds the app again
    assert auth.mode() == "setup" and auth.setup_seconds_left() == 600
    assert fresh.post("/api/ui/setup", json={"password": PASSWORD}).status_code == 200


def test_setup_window_length_comes_from_a_constant_with_env_override(monkeypatch):
    monkeypatch.delenv(auth.SETUP_WINDOW_ENV, raising=False)
    assert auth.SETUP_WINDOW_MINUTES == 10
    assert auth.setup_window_seconds() == 600
    for raw, seconds in (("1", 60), ("0.5", 30), ("0", 0), ("30", 1800)):
        monkeypatch.setenv(auth.SETUP_WINDOW_ENV, raw)
        assert auth.setup_window_seconds() == seconds, raw
    for bad in ("abc", "-5", "nan", "inf", "99999999", ""):
        monkeypatch.setenv(auth.SETUP_WINDOW_ENV, bad)
        assert auth.setup_window_seconds() == 600, bad


def test_setup_can_be_disabled(cfg_dir, core, voices_dir, monkeypatch, caplog):
    monkeypatch.setenv(auth.SETUP_WINDOW_ENV, "0")
    caplog.set_level(logging.WARNING)
    c = _client(core, voices_dir)
    assert c.get("/api/ui/status").json()["auth_mode"] == "closed"
    r = c.post("/api/ui/setup", json={"password": PASSWORD})
    assert r.status_code == 403 and "disabled" in r.json()["detail"]
    assert c.post("/api/ui/restart", headers=_bearer(PASSWORD)).status_code == 401
    assert "in-browser setup is disabled" in caplog.text


def test_setup_is_refused_when_the_env_password_is_set(client, cfg_dir):
    assert client.get("/api/ui/status").json()["auth_mode"] == "env"
    r = client.post("/api/ui/setup", json={"password": PASSWORD})
    assert r.status_code == 403 and auth.TOKEN_ENV in r.json()["detail"]
    assert not (cfg_dir / "ui_password.json").exists()
    assert client.get("/api/ui/session", headers=_bearer(PASSWORD)).status_code == 401


def test_env_password_still_works_and_takes_precedence(fresh, monkeypatch, caplog):
    assert fresh.post("/api/ui/setup", json={"password": PASSWORD}).status_code == 200
    monkeypatch.setenv(auth.TOKEN_ENV, ENV_TOKEN)
    assert auth.mode() == "env"
    assert fresh.get("/api/ui/session", headers=_bearer(ENV_TOKEN)).status_code == 200
    assert fresh.get("/api/ui/session", headers=_bearer(PASSWORD)).status_code == 401
    # and without it the stored password is back in charge
    monkeypatch.delenv(auth.TOKEN_ENV)
    assert fresh.get("/api/ui/session", headers=_bearer(PASSWORD)).status_code == 200


def test_env_password_is_announced_without_its_value(cfg_dir, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(auth.TOKEN_ENV, ENV_TOKEN)
    auth.announce()
    assert auth.mode() == "env" and auth.verify(ENV_TOKEN) == "ok"
    assert ENV_TOKEN not in caplog.text and "shorter than" not in caplog.text
    assert not cfg_dir.exists() or list(cfg_dir.iterdir()) == []
    auth._reset_for_tests()
    monkeypatch.setenv(auth.TOKEN_ENV, "short")
    auth.announce()
    assert "shorter than" in caplog.text
    assert auth.verify("short") == "ok"  # still honoured: operator's choice


# ── Change password ─────────────────────────────────────────────────────────


def _change(client, bearer, current, new):
    return client.post(
        "/api/ui/password",
        headers=_bearer(bearer) if bearer else {},
        json={"current_password": current, "new_password": new},
    )


def test_change_password(fresh, cfg_dir):
    assert fresh.post("/api/ui/setup", json={"password": PASSWORD}).status_code == 200
    path = cfg_dir / "ui_password.json"
    before = json.loads(path.read_text())

    r = _change(fresh, PASSWORD, PASSWORD, NEW_PASSWORD)
    assert r.status_code == 200 and r.json() == {"changed": True}

    after = json.loads(path.read_text())
    assert after["salt"] != before["salt"] and after["hash"] != before["hash"]
    assert NEW_PASSWORD not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in cfg_dir.iterdir()] == ["ui_password.json"]
    assert fresh.get("/api/ui/session", headers=_bearer(PASSWORD)).status_code == 401
    assert fresh.get("/api/ui/session", headers=_bearer(NEW_PASSWORD)).status_code == 200
    auth._reset_for_tests()  # and it is the new one after a restart
    assert auth.verify(NEW_PASSWORD) == "ok" and auth.verify(PASSWORD) == "bad"


def test_change_password_with_wrong_current_password(fresh, cfg_dir):
    assert fresh.post("/api/ui/setup", json={"password": PASSWORD}).status_code == 200
    before = (cfg_dir / "ui_password.json").read_text()
    for current in ("wrong-current-pw", "", None, 12345):
        r = _change(fresh, PASSWORD, current, NEW_PASSWORD)
        assert r.status_code == 403, current
        assert "current password" in r.json()["detail"]
    assert (cfg_dir / "ui_password.json").read_text() == before
    assert fresh.get("/api/ui/session", headers=_bearer(PASSWORD)).status_code == 200
    assert fresh.get("/api/ui/session", headers=_bearer(NEW_PASSWORD)).status_code == 401


def test_change_password_requires_being_logged_in(fresh, cfg_dir):
    assert fresh.post("/api/ui/setup", json={"password": PASSWORD}).status_code == 200
    before = (cfg_dir / "ui_password.json").read_text()
    # knowing the current password in the body is not enough without the login
    assert _change(fresh, None, PASSWORD, NEW_PASSWORD).status_code == 401
    assert _change(fresh, "wrong-bearer-pw", PASSWORD, NEW_PASSWORD).status_code == 401
    assert (cfg_dir / "ui_password.json").read_text() == before


@pytest.mark.parametrize("new", ["too-short", "pässwort-mit-umlaut", " space-in-front", None])
def test_change_password_rejects_unusable_new_password(fresh, new):
    assert fresh.post("/api/ui/setup", json={"password": PASSWORD}).status_code == 200
    assert _change(fresh, PASSWORD, PASSWORD, new).status_code == 400
    assert fresh.get("/api/ui/session", headers=_bearer(PASSWORD)).status_code == 200


def test_change_password_is_not_offered_for_the_env_password(client, cfg_dir):
    r = _change(client, ENV_TOKEN, ENV_TOKEN, NEW_PASSWORD)
    assert r.status_code == 409 and auth.TOKEN_ENV in r.json()["detail"]
    assert not (cfg_dir / "ui_password.json").exists()
    assert client.get("/api/ui/session", headers=_bearer()).status_code == 200


# ── Forgotten password, damaged file, unwritable directory ──────────────────


def test_forgotten_password_delete_the_file_and_restart(fresh, cfg_dir):
    assert fresh.post("/api/ui/setup", json={"password": PASSWORD}).status_code == 200
    (cfg_dir / "ui_password.json").unlink()
    auth._reset_for_tests()  # restart
    auth.announce()
    assert fresh.get("/api/ui/status").json()["auth_mode"] == "setup"
    assert fresh.get("/api/ui/session", headers=_bearer(PASSWORD)).status_code == 401
    assert fresh.post("/api/ui/setup", json={"password": NEW_PASSWORD}).status_code == 200
    assert fresh.get("/api/ui/session", headers=_bearer(NEW_PASSWORD)).status_code == 200


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "[]",
        '{"v": 1}',
        '{"v": 2, "algo": "scrypt", "n": 16384, "r": 8, "p": 1, "salt": "00"}',
        # absurd cost parameters must not be computed
        '{"v": 1, "algo": "scrypt", "n": 1073741824, "r": 8, "p": 1, '
        '"salt": "0011223344556677", "hash": "00112233445566778899aabbccddeeff"}',
        '{"v": 1, "algo": "plain", "salt": "0011223344556677", '
        '"hash": "00112233445566778899aabbccddeeff"}',
    ],
)
def test_unusable_password_file_fails_closed(cfg_dir, core, voices_dir, caplog, content):
    """A damaged hash file must neither be accepted nor reopen the setup."""
    cfg_dir.mkdir(parents=True)
    path = cfg_dir / "ui_password.json"
    path.write_text(content)
    caplog.set_level(logging.ERROR)
    c = _client(core, voices_dir)
    assert c.get("/api/ui/status").json()["auth_mode"] == "closed"
    r = c.post("/api/ui/setup", json={"password": PASSWORD})
    assert r.status_code == 403 and "unusable" in r.json()["detail"]
    assert c.post("/api/ui/restart", headers=_bearer(PASSWORD)).status_code == 401
    assert path.read_text() == content
    assert "Delete it and restart" in caplog.text


def test_wide_permissions_on_the_password_file_are_tightened(fresh, cfg_dir):
    assert fresh.post("/api/ui/setup", json={"password": PASSWORD}).status_code == 200
    path = cfg_dir / "ui_password.json"
    os.chmod(path, 0o644)
    auth._reset_for_tests()
    assert auth.verify(PASSWORD) == "ok"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_unwritable_config_dir_falls_back_to_a_one_run_token(
    tmp_path, monkeypatch, caplog, core, voices_dir
):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")  # parent of the config "dir" is a file
    monkeypatch.delenv(auth.TOKEN_ENV, raising=False)
    monkeypatch.setenv("POCKET_TTS_CONFIG_FILE", str(blocker / "cfg" / "settings.json"))
    auth._reset_for_tests()
    caplog.set_level(logging.INFO)
    try:
        c = _client(core, voices_dir)
        auth.announce()
        assert auth.mode() == "memory"
        (token,) = [w for r in caplog.records for w in r.getMessage().split() if len(w) == 43]
        assert caplog.text.count(token) == 1  # shown exactly once
        assert c.get("/api/ui/session", headers=_bearer(token)).status_code == 200
        assert c.get("/api/ui/session", headers=_bearer("wrong-token-xyz")).status_code == 401
        assert c.get("/api/ui/status").json()["auth_mode"] == "memory"
        r = c.post("/api/ui/setup", json={"password": PASSWORD})
        assert r.status_code == 403 and "not writable" in r.json()["detail"]
    finally:
        auth._reset_for_tests()


# ── Hashing and comparison ──────────────────────────────────────────────────


def test_passwords_are_hashed_with_scrypt_and_a_fresh_salt(monkeypatch):
    monkeypatch.setattr(auth, "_SCRYPT_N", 2**14)  # the shipped cost
    a, b = auth._new_record(PASSWORD), auth._new_record(PASSWORD)
    assert a["algo"] == "scrypt" and (a["n"], a["r"], a["p"]) == (16384, 8, 1)
    assert a["salt"] != b["salt"] and a["hash"] != b["hash"]
    assert auth._record_usable(a)
    assert auth._derive(PASSWORD, a).hex() == a["hash"]
    assert auth._derive(PASSWORD + "x", a).hex() != a["hash"]


def test_stored_password_is_checked_by_hashing_in_constant_time(fresh, monkeypatch):
    assert fresh.post("/api/ui/setup", json={"password": PASSWORD}).status_code == 200
    auth._reset_for_tests()  # no cached verification
    derived, compared = [], []
    real_derive, real_compare = auth._derive, auth.hmac.compare_digest

    def spy_derive(password, record):
        derived.append(password)
        return real_derive(password, record)

    def spy_compare(a, b):
        compared.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(auth, "_derive", spy_derive)
    monkeypatch.setattr(auth.hmac, "compare_digest", spy_compare)
    assert auth.verify("wrong-password-1") == "bad"
    assert auth.verify(PASSWORD) == "ok"
    assert derived == ["wrong-password-1", PASSWORD]
    assert len(compared) == 2 and all(len(a) == len(b) == 32 for a, b in compared)


def test_oversized_credential_is_refused_without_hashing(fresh, monkeypatch):
    assert fresh.post("/api/ui/setup", json={"password": PASSWORD}).status_code == 200
    auth._reset_for_tests()
    monkeypatch.setattr(auth, "_derive", lambda *_: pytest.fail("hashed an oversized value"))
    assert auth.verify("x" * 5000) == "bad"


def test_env_password_comparison_is_constant_time(cfg_dir, monkeypatch):
    monkeypatch.setenv(auth.TOKEN_ENV, ENV_TOKEN)
    calls = []
    real = auth.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    assert auth.verify(ENV_TOKEN) == "ok"
    assert auth.verify(ENV_TOKEN + "x") == "bad"
    assert auth.verify("tökén-with-non-ascii") == "bad"
    assert len(calls) == 3
    assert auth.verify("") == "bad" and auth.verify(None) == "bad"


# ── Rate limit ──────────────────────────────────────────────────────────────


def test_failed_logins_are_rate_limited(clock, fresh, monkeypatch):
    assert fresh.post("/api/ui/setup", json={"password": PASSWORD}).status_code == 200
    for i in range(auth.MAX_FAILED_ATTEMPTS):
        r = fresh.get("/api/ui/session", headers=_bearer(f"wrong-guess-{i:03d}"))
        assert r.status_code == 401, i

    r = fresh.get("/api/ui/session", headers=_bearer("wrong-guess-xyz"))
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
    assert fresh.post("/api/ui/restart", headers=_bearer("wrong-guess-xyz")).status_code == 429

    # while limited, nothing is evaluated for an address that never logged in —
    # not even the right password, and no hash is computed
    real_derive, hashed = auth._derive, []

    def counting_derive(password, record):
        hashed.append(password)
        return real_derive(password, record)

    monkeypatch.setattr(auth, "_derive", counting_derive)
    assert auth.verify("wrong-guess-abc", "10.9.8.7") == "limited"
    assert auth.verify(PASSWORD, "10.9.8.7") == "limited"
    # the browser that is logged in (it did the setup) is not locked out
    assert fresh.get("/api/ui/session", headers=_bearer(PASSWORD)).status_code == 200
    # a missing credential is not an attempt and stays a plain 401
    assert fresh.get("/api/ui/session").status_code == 401

    assert hashed == []

    clock["now"] += auth.ATTEMPT_WINDOW_S + 1
    assert auth.verify(PASSWORD, "10.9.8.7") == "ok"


def test_rate_limit_also_covers_the_env_password(clock, client):
    for i in range(auth.MAX_FAILED_ATTEMPTS):
        assert client.get("/api/ui/session", headers=_bearer(f"guess-{i}")).status_code == 401
    assert client.get("/api/ui/session", headers=_bearer("guess-x")).status_code == 429
    assert client.get("/api/ui/session", headers=_bearer()).status_code == 429
    clock["now"] += auth.ATTEMPT_WINDOW_S + 1
    assert client.get("/api/ui/session", headers=_bearer()).status_code == 200


def test_setup_calls_are_rate_limited(clock, fresh, cfg_dir):
    for _ in range(auth.MAX_SETUP_ATTEMPTS):
        assert fresh.post("/api/ui/setup", json={"password": "short"}).status_code == 400
    r = fresh.post("/api/ui/setup", json={"password": PASSWORD})
    assert r.status_code == 429
    assert list(cfg_dir.iterdir()) == []
    clock["now"] += auth.ATTEMPT_WINDOW_S + 1
    assert fresh.post("/api/ui/setup", json={"password": PASSWORD}).status_code == 200


# ── Mutating routes: 401 without the password, served with it ──────────────────


@pytest.mark.parametrize(("method", "path"), MUTATING)
def test_mutating_route_rejects_missing_and_wrong_token(client, no_restart, method, path):
    for headers in (
        {},
        _bearer("wrong-token"),
        {"Authorization": f"Basic {ENV_TOKEN}"},
        {"Authorization": ENV_TOKEN},
        {"X-UI-Token": "wrong"},
    ):
        r = client.request(method, path, headers=headers)
        assert r.status_code == 401, (headers, r.text)
        assert r.headers["www-authenticate"] == "Bearer"
        assert ENV_TOKEN not in r.text
    assert client.request(method, path, params={"token": "wrong"}).status_code == 401
    assert no_restart == []


def test_mutating_routes_work_with_token(client, core, voices_dir, cfg_dir, no_restart):
    h = _bearer()
    r = client.post("/v1/audio/voices", headers=h, files=_wav_upload(), data={"name": "myvoice"})
    assert r.status_code == 201, r.text
    assert (voices_dir / "myvoice.wav").is_file()

    assert client.delete("/v1/audio/voices/myvoice", headers=h).status_code == 204
    assert not (voices_dir / "myvoice.wav").exists()

    r = client.post(
        "/api/ui/config", headers=h, json={"updates": {"POCKET_TTS_MIN_LID_CHARS": "12"}}
    )
    assert r.status_code == 200 and r.json()["saved"] is True

    r = client.post(
        "/api/ui/replacements", headers=h, json={"replacements": {"HA": "Home Assistant"}}
    )
    assert r.status_code == 200 and r.json()["count"] == 1

    r = client.post("/api/ui/restart", headers=h)
    assert r.status_code == 200 and no_restart == [True]


def test_every_registered_unsafe_route_needs_the_token(app, client, no_restart):
    """Walk the route table: whatever is not GET/HEAD/OPTIONS and not on the
    open list must answer 401 without a token — also for routes added later."""
    checked = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path.replace("{name}", "x")
        for method in route.methods - auth.SAFE_METHODS:
            if (method, route.path) in auth.OPEN_UNSAFE_ROUTES:
                continue
            assert client.request(method, path).status_code == 401, (method, path)
            checked.add((method, route.path))
    assert {
        ("POST", "/v1/audio/voices"),
        ("DELETE", "/v1/audio/voices/{name}"),
        ("POST", "/api/ui/config"),
        ("POST", "/api/ui/replacements"),
        ("POST", "/api/ui/restart"),
        ("POST", "/api/ui/password"),
    } <= checked
    # the only open POSTs: synthesis, preview, and the self-guarded setup
    open_posts = {
        ("POST", "/v1/audio/speech"),
        ("POST", "/v1/text/normalize"),
        ("POST", "/api/ui/setup"),
    }
    assert open_posts == auth.OPEN_UNSAFE_ROUTES
    assert no_restart == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/ui/restart/"),  # trailing slash
        ("POST", "//api/ui/restart"),  # doubled slash
        ("POST", "/api/ui/config/"),
        ("POST", "/v1/audio/voices/"),
        ("DELETE", "/v1/audio/voices/x/"),
        ("PUT", "/api/ui/config"),  # other verbs
        ("PATCH", "/v1/audio/voices/x"),
        ("DELETE", "/api/ui/replacements"),
        ("POST", "/ui/static/app.js"),  # mounted sub-app
        ("POST", "/no/such/route"),
        ("POST", "/v1/audio/speech/extra"),  # only the exact open paths are open
        ("POST", "/api/ui/setup/extra"),
        ("PUT", "/api/ui/setup"),
        ("DELETE", "/api/ui/password"),
        ("DELETE", "/v1/audio/speech"),
    ],
)
def test_default_deny_for_unsafe_methods(client, no_restart, method, path):
    assert client.request(method, path).status_code == 401
    assert no_restart == []


def test_safe_methods_never_reach_a_mutating_handler(client, voices_dir, no_restart):
    (voices_dir / "keep.wav").write_bytes(b"x")
    for method in ("GET", "HEAD", "OPTIONS"):
        for path in ("/api/ui/restart", "/v1/audio/voices/keep", "/api/ui/restart/"):
            r = client.request(method, path)
            assert r.status_code in (404, 405), (method, path, r.status_code)
    assert no_restart == []
    assert (voices_dir / "keep.wav").exists()


def test_rejection_happens_before_the_body_is_read(app):
    """A client without a token must not be able to make the server spool an
    upload: the 401 is sent without consuming the request body."""
    sent = []

    async def receive():
        raise AssertionError("request body was read before the auth check")

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/audio/voices",
        "raw_path": b"/v1/audio/voices",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"multipart/form-data; boundary=x"),
            (b"content-length", b"52428800"),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 401


def test_accepted_ways_to_send_the_token(client):
    url = "/api/ui/session"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=_bearer()).status_code == 200
    assert client.get(url, headers={"Authorization": f"bearer {ENV_TOKEN}"}).status_code == 200
    # legacy mechanisms of the old optional UI token keep working
    assert client.get(url, headers={"X-UI-Token": ENV_TOKEN}).status_code == 200
    assert client.get(url, params={"token": ENV_TOKEN}).status_code == 200
    assert client.get(url, headers=_bearer(ENV_TOKEN[:-1])).status_code == 401
    assert client.get(url, headers=_bearer()).json() == {"authenticated": True}


# ── Synthesis and read-only routes stay open ────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/health",
        "/v1/audio/voices",
        "/v1/audio/languages",
        "/ui",
        "/ui/static/app.js",
        "/ui/static/style.css",
        "/api/ui/status",
        "/api/ui/config",
        "/api/ui/replacements",
    ],
)
def test_read_routes_need_no_token(client, path):
    assert client.get(path).status_code == 200


def test_synthesis_needs_no_token(client):
    r = client.post("/v1/audio/speech", json={"input": CHIME_MARKER, "response_format": "wav"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF"
    # a client that used the trailing-slash form still gets the router redirect
    r = client.post(
        "/v1/audio/speech/",
        follow_redirects=False,
        json={"input": CHIME_MARKER, "response_format": "wav"},
    )
    assert r.status_code == 307


def test_normalize_preview_needs_no_token(client):
    r = client.post("/v1/text/normalize", json={"input": "Hallo", "language": "de"})
    assert r.status_code == 200 and "normalized" in r.json()


# ── The token never shows up in a response ──────────────────────────────────


def test_token_is_not_exposed_by_any_open_route(client):
    for path in (
        "/",
        "/health",
        "/ui",
        "/ui/static/app.js",
        "/ui/static/index.html",
        "/api/ui/status",
        "/api/ui/config",
        "/api/ui/replacements",
        "/v1/audio/voices",
        "/openapi.json",
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        assert ENV_TOKEN not in r.text, path
    assert auth.TOKEN_ENV not in client.get("/api/ui/config").json()["config"]
    status = client.get("/api/ui/status").json()
    assert status["auth_enabled"] is True and status["auth_mode"] == "env"


def test_token_cannot_be_set_through_settings(client, cfg_dir):
    assert auth.TOKEN_ENV not in config_store.EDITABLE_KEYS
    r = client.post(
        "/api/ui/config",
        headers=_bearer(),
        json={"updates": {auth.TOKEN_ENV: "attacker-chosen-token-1234"}},
    )
    assert r.status_code == 200
    assert auth.verify(ENV_TOKEN) == "ok"
    assert auth.verify("attacker-chosen-token-1234") == "bad"
    assert auth.TOKEN_ENV not in config_store._read_file()


# ── Upload hardening ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "filename",
    [
        "shell.php",
        "run.sh",
        "page.html",
        "voice.wav.error",
        "cfg.yaml",
        "state.safetensors",
        "recording.webm",
        "noext",
        "archive.tar.gz",
    ],
)
def test_upload_rejects_non_audio_suffix(client, voices_dir, filename):
    r = client.post(
        "/v1/audio/voices", headers=_bearer(), files=_wav_upload(filename), data={"name": "v1"}
    )
    # "noext" has no suffix and is stored as .wav, as before
    if filename == "noext":
        assert r.status_code == 201
        assert [p.name for p in voices_dir.iterdir()] == ["v1.wav"]
    else:
        assert r.status_code == 400, r.text
        assert list(voices_dir.iterdir()) == []


@pytest.mark.parametrize(
    "suffix", [".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".WAV"]
)
def test_upload_accepts_audio_suffixes(client, voices_dir, suffix):
    r = client.post(
        "/v1/audio/voices",
        headers=_bearer(),
        files=_wav_upload("in" + suffix),
        data={"name": "v1", "language": "de"},
    )
    assert r.status_code == 201, r.text
    assert (voices_dir / f"v1.de{suffix.lower()}").is_file()


def test_upload_keeps_name_validation_and_size_cap(client, voices_dir, monkeypatch):
    for bad in ("../evil", "..", ".hidden", "a/b", "a\\b", "x y"):
        r = client.post(
            "/v1/audio/voices", headers=_bearer(), files=_wav_upload(), data={"name": bad}
        )
        assert r.status_code == 400, bad
    monkeypatch.setattr("polyglot_tts.http_server.MAX_VOICE_UPLOAD_BYTES", 10)
    r = client.post(
        "/v1/audio/voices",
        headers=_bearer(),
        files=_wav_upload(data=b"x" * 64),
        data={"name": "big"},
    )
    assert r.status_code == 413
    assert list(voices_dir.iterdir()) == []


def test_upload_caps_the_number_of_voices(client, voices_dir, monkeypatch):
    monkeypatch.setattr("polyglot_tts.http_server.MAX_EXTRA_VOICES", 2)
    for name in ("one", "two"):
        assert (
            client.post(
                "/v1/audio/voices", headers=_bearer(), files=_wav_upload(), data={"name": name}
            ).status_code
            == 201
        )
    r = client.post(
        "/v1/audio/voices", headers=_bearer(), files=_wav_upload(), data={"name": "three"}
    )
    assert r.status_code == 409
    assert not (voices_dir / "three.wav").exists()
    # another language file for an existing voice is not a new voice
    r = client.post(
        "/v1/audio/voices",
        headers=_bearer(),
        files=_wav_upload(),
        data={"name": "one", "language": "de"},
    )
    assert r.status_code == 201


def test_unauthenticated_upload_writes_nothing(client, voices_dir):
    r = client.post("/v1/audio/voices", files=_wav_upload(), data={"name": "v1"})
    assert r.status_code == 401
    assert list(voices_dir.iterdir()) == []


# ── Settings: languages and default voice are names, never locations ────────

BAD_LANGUAGES = [
    "../../../../tmp/evil",
    "/tmp/evil",
    "..",
    ".",
    "german_24l/../../x",
    "german_24l.yaml",
    "hf://someone/repo/cfg",
    "https://example.invalid/cfg",
    "german_24l,../x",
    "klingon_24l",
    "german 24l",
    "-german",
    "a" * 80,
    "C:\\evil",
    "german_24l\x00",
]


@pytest.mark.parametrize("value", BAD_LANGUAGES)
def test_config_rejects_bad_language_values(client, cfg_dir, value):
    r = client.post(
        "/api/ui/config", headers=_bearer(), json={"updates": {"POCKET_TTS_LANGUAGES": value}}
    )
    assert r.status_code == 400, r.text
    assert "POCKET_TTS_LANGUAGES" not in os.environ
    assert not (cfg_dir / "settings.json").exists()


def test_config_accepts_known_languages(client, cfg_dir):
    r = client.post(
        "/api/ui/config",
        headers=_bearer(),
        json={"updates": {"POCKET_TTS_LANGUAGES": " german_24l , french_24l,german "}},
    )
    assert r.status_code == 200, r.text
    assert config_store._read_file()["POCKET_TTS_LANGUAGES"] == "german_24l,french_24l"
    assert r.json()["restart_required_for"] == ["POCKET_TTS_LANGUAGES"]


BAD_VOICES = [
    "http://127.0.0.1:9/voice.wav",
    "https://example.invalid/v.safetensors",
    "hf://someone/repo/voice.wav",
    "/etc/hostname",
    "../voices/x.wav",
    "file:///etc/hostname",
    "no_such_voice",
    "eve.safetensors",
    "eve\n",
]


@pytest.mark.parametrize("value", BAD_VOICES)
def test_config_rejects_unknown_default_voice(client, core, cfg_dir, value):
    r = client.post(
        "/api/ui/config", headers=_bearer(), json={"updates": {"POCKET_TTS_VOICE": value}}
    )
    assert r.status_code == 400, r.text
    assert core.default_voice == "eve"
    assert "POCKET_TTS_VOICE" not in os.environ
    assert not (cfg_dir / "settings.json").exists()


def test_config_accepts_builtin_and_loaded_voices(client, core):
    r = client.post(
        "/api/ui/config", headers=_bearer(), json={"updates": {"POCKET_TTS_VOICE": "alba"}}
    )
    assert r.status_code == 200 and core.default_voice == "alba"
    core.add_voice("My Cloned.Voice", {"german_24l": object()})
    r = client.post(
        "/api/ui/config",
        headers=_bearer(),
        json={"updates": {"POCKET_TTS_VOICE": "My Cloned.Voice"}},
    )
    assert r.status_code == 200 and core.default_voice == "My Cloned.Voice"


@pytest.mark.parametrize(
    "updates",
    [
        {"POCKET_TTS_DEVICE": "x" * 5000},
        {"POCKET_TTS_DEVICE": "cu\x00da"},
        {"HF_TOKEN": "hf_abc\r\nX-Injected: 1"},
        {"POCKET_TTS_TEMP": {"nested": "object"}},
        {"POCKET_TTS_TEMP": ["list"]},
    ],
)
def test_config_rejects_malformed_values(client, cfg_dir, updates):
    r = client.post("/api/ui/config", headers=_bearer(), json={"updates": updates})
    assert r.status_code == 400, r.text
    assert not (cfg_dir / "settings.json").exists()


def test_config_rejects_malformed_bodies(client):
    h = _bearer()
    assert client.post("/api/ui/config", headers=h, json=["not", "an", "object"]).status_code == 400
    assert client.post("/api/ui/config", headers=h, content=b"{not json").status_code == 400
    assert client.post("/api/ui/replacements", headers=h, json=[1, 2]).status_code == 400


# ── The same rules at startup (settings file or environment) ────────────────


def test_startup_drops_bad_language_entries(cfg_dir, caplog):
    from polyglot_tts import dispatcher

    caplog.set_level(logging.ERROR)
    got = dispatcher._resolve_checkpoints("../../tmp/evil, german_24l ,hf://x/y/z,french_24l")
    assert got == ["german_24l", "french_24l"]
    assert os.environ["POCKET_TTS_LANGUAGES"] == "german_24l,french_24l"
    assert "Ignoring POCKET_TTS_LANGUAGES entry" in caplog.text


def test_startup_falls_back_to_defaults_instead_of_failing(cfg_dir, caplog):
    from polyglot_tts import dispatcher

    caplog.set_level(logging.ERROR)
    for bad in ("/tmp/evil", "../x,..", "", " , "):
        assert dispatcher._resolve_checkpoints(bad) == dispatcher.DEFAULT_LANGUAGES.split(",")
    assert "loading the defaults" in caplog.text


def test_startup_keeps_a_valid_language_setting_untouched(cfg_dir):
    from polyglot_tts import dispatcher

    assert dispatcher._resolve_checkpoints("english_2026-10,german") == [
        "english_2026-10",
        "german",
    ]
    assert "POCKET_TTS_LANGUAGES" not in os.environ


def test_startup_default_voice_must_be_a_known_name(cfg_dir, caplog):
    from polyglot_tts import dispatcher

    caplog.set_level(logging.ERROR)
    loaded = {"EL_Custom": {}}
    assert dispatcher._resolve_default_voice("EL_Custom", loaded) == "EL_Custom"
    assert dispatcher._resolve_default_voice("alba", loaded) == "alba"
    assert "POCKET_TTS_VOICE" not in os.environ and caplog.text == ""
    for bad in BAD_VOICES + [""]:
        assert dispatcher._resolve_default_voice(bad, loaded) == "eve", bad
    assert os.environ["POCKET_TTS_VOICE"] == "eve"
    assert "names only" in caplog.text


def test_tampered_settings_file_cannot_break_or_redirect_startup(cfg_dir, caplog):
    """A settings file written by an older version (or by hand) goes through
    the same checks when it is read back."""
    import json

    from polyglot_tts import dispatcher

    cfg_dir.mkdir(parents=True)
    (cfg_dir / "settings.json").write_text(
        json.dumps(
            {
                "POCKET_TTS_LANGUAGES": "../../../tmp/evil",
                "POCKET_TTS_VOICE": "https://example.invalid/voice.wav",
                "POCKET_TTS_DEVICE": "cu\u0000da",  # cannot go into os.environ
                "POCKET_TTS_UI_TOKEN": "attacker-chosen-token-1234",
                "PATH": "/tmp/evil",
            }
        )
    )
    path_before = os.environ.get("PATH")
    caplog.set_level(logging.ERROR)
    config_store.apply_overlay()  # must not raise
    assert os.environ.get("PATH") == path_before
    assert auth.TOKEN_ENV not in os.environ
    assert "POCKET_TTS_DEVICE" not in os.environ
    checkpoints = dispatcher._resolve_checkpoints(os.environ["POCKET_TTS_LANGUAGES"])
    assert checkpoints == dispatcher.DEFAULT_LANGUAGES.split(",")
    assert dispatcher._resolve_default_voice(os.environ["POCKET_TTS_VOICE"], {}) == "eve"


def test_filter_checkpoints_only_passes_bare_known_names():
    ok, bad = config_store.filter_checkpoints(
        ["german_24l", " english_2026-10 ", "", "../german_24l", "german_24l/", "nope"]
    )
    assert ok == ["german_24l", "english_2026-10"]
    assert bad == ["../german_24l", "german_24l/", "nope"]


# ── Web UI: login flow present, page hardened ───────────────────────────────


def test_ui_page_is_open_and_carries_protective_headers(client):
    r = client.get("/ui")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert (
        "script-src 'self'" in csp
        and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    )
    assert "frame-ancestors 'none'" in csp
    assert r.headers["referrer-policy"] == "no-referrer"
    # the static copy of the page gets the same policy
    assert "content-security-policy" in client.get("/ui/static/index.html").headers


def test_ui_assets_contain_login_setup_and_change_forms():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    for element_id in (
        "login-btn",
        "logout-btn",
        "auth-state",
        "auth-panel",
        "login-form",
        "setup-form",
        "change-form",
        "setup-closed",
        "settings-login-btn",
        "settings-logout-btn",
    ):
        assert f'id="{element_id}"' in html, element_id
    assert "<script>" not in html and "onclick=" not in html  # CSP: no inline script
    # real forms that submit; never by GET (a password must not end up in a URL)
    assert html.count("<form ") == 3 == html.count('method="post"')
    assert html.count('<button type="submit">') == 3
    # hints for password managers: a fixed user name next to every password
    assert html.count('autocomplete="username"') == 3
    assert html.count('value="admin"') == 3
    assert html.count('autocomplete="current-password"') == 2  # login, change
    assert html.count('autocomplete="new-password"') == 4  # setup + change, each twice
    assert html.count('minlength="10"') == 4
    # every password field is a password field with a label
    for field in (
        "login-password",
        "setup-password",
        "setup-repeat",
        "change-current",
        "change-new",
        "change-repeat",
    ):
        assert f'<label for="{field}">' in html, field
        tag = html[html.index(f'<input id="{field}"') :].split("/>", 1)[0]
        assert 'type="password"' in tag, field
    assert html.count("data-toggle=") == 3  # show / hide
    assert "Management token" not in html and "management token" not in html

    assert '"Bearer " + ' in js and "Authorization" in js
    assert "localStorage" in js and "polyglot_ui_token" in js
    for route in ("/api/ui/session", "/api/ui/setup", "/api/ui/password"):
        assert route in js, route
    assert "requireLogin()" in js and "setLoggedOut" in js
    assert "res.status === 401" in js
    assert 'addEventListener("submit"' in js and "preventDefault()" in js
    # the page reads a legacy ?token= once but never builds such a URL
    assert "token=" not in js.replace("?token=…", "")
    assert "X-UI-Token" not in js


def test_ui_stylesheet_has_finger_sized_form_controls():
    css = (WEB_DIR / "style.css").read_text(encoding="utf-8")
    assert "min-height: 44px" in css and "font-size: 16px" in css
    assert ".auth-form" in css and "flex-direction: column" in css


def test_ui_escapes_server_values_before_inserting_markup():
    """Voice names and last-synthesis fields come from any client on the
    network; the page that holds the token must insert them as text."""
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "function esc(" in js
    for expr in (
        "esc(t.voice)",
        "esc(t.language)",
        "esc(s.default_voice)",
        "esc(v.name)",
        "esc(j.detail || r.status)",
    ):
        assert expr in js, expr
    for raw in ("${t.voice}", "${v.name}", "${s.default_voice}", "${s.device}"):
        assert raw not in js, raw
