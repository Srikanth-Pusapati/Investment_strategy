"""Tests for the Robinhood OAuth handshake plumbing.

Pure logic + local sockets, no Robinhood network calls: we exercise the file-backed
token storage (round-trip + 0600 perms), the has_tokens gate, the provider wiring
(redirect_uri / scope / public-client method), and the localhost redirect catcher
(parses ?code&state, ignores favicon, surfaces ?error, and — since the Sep 10 2026
2h44m wedge — gives up at a wall-clock deadline). The `login` command's handling of
that deadline (exit 3, runbook line, token file untouched) is covered with the async
handshake stubbed out. The live OAuth handshake against Robinhood is manual
(`robinhood_auth login`) and not covered here.

No pytest fixtures on purpose: the tests must also run under the plain `_run_all()`
runner below, so env/stdout capture use unittest.mock + contextlib instead.

Runnable two ways:
    .venv/bin/python tests/test_robinhood_auth.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import stat
import sys
import threading
import time
import urllib.request
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from investment_strategy.portfolio import robinhood_auth as ra


def _cfg(tmpdir, port=8791):
    return SimpleNamespace(
        robinhood_enabled=True,
        robinhood_mcp_url="https://agent.robinhood.com/mcp/trading",
        robinhood_mcp_token="",
        robinhood_oauth_file=os.path.join(tmpdir, "robinhood_oauth.json"),
        robinhood_scope="internal",
        robinhood_callback_port=port,
        robinhood_client_name="Test Bot",
    )


def _tmpdir():
    import tempfile

    return tempfile.mkdtemp(prefix="rh_auth_test_")


def test_token_storage_roundtrip_and_perms():
    from mcp.shared.auth import OAuthToken

    cfg = _cfg(_tmpdir())
    store = ra.FileTokenStorage(cfg.robinhood_oauth_file)

    assert asyncio.run(store.get_tokens()) is None
    tok = OAuthToken(access_token="A", refresh_token="R", token_type="Bearer", expires_in=3600)
    asyncio.run(store.set_tokens(tok))

    got = asyncio.run(store.get_tokens())
    assert got is not None and got.access_token == "A" and got.refresh_token == "R"
    # tokens are credentials -> owner-only file
    mode = stat.S_IMODE(os.stat(cfg.robinhood_oauth_file).st_mode)
    assert mode == 0o600, oct(mode)


def test_has_tokens_gate():
    cfg = _cfg(_tmpdir())
    assert ra.has_tokens(cfg) is False           # no file yet
    from mcp.shared.auth import OAuthToken

    store = ra.FileTokenStorage(cfg.robinhood_oauth_file)
    asyncio.run(store.set_tokens(OAuthToken(access_token="A", token_type="Bearer")))
    assert ra.has_tokens(cfg) is True


def test_client_info_roundtrip_survives_tokens():
    """Persisting client_info must not clobber tokens and vice-versa."""
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    cfg = _cfg(_tmpdir())
    store = ra.FileTokenStorage(cfg.robinhood_oauth_file)
    ci = OAuthClientInformationFull(
        client_id="cid-123",
        redirect_uris=["http://localhost:8791/callback"],  # type: ignore[list-item]
        token_endpoint_auth_method="none",
    )
    asyncio.run(store.set_client_info(ci))
    asyncio.run(store.set_tokens(OAuthToken(access_token="A", token_type="Bearer")))
    assert asyncio.run(store.get_client_info()).client_id == "cid-123"
    assert asyncio.run(store.get_tokens()).access_token == "A"  # not clobbered


def test_provider_metadata_is_public_pkce_client():
    cfg = _cfg(_tmpdir())
    provider = ra.build_provider(cfg, interactive=False)
    meta = provider.context.client_metadata
    assert str(meta.redirect_uris[0]) == "http://localhost:8791/callback"
    assert meta.token_endpoint_auth_method == "none"       # public client
    assert "refresh_token" in meta.grant_types             # auto-refresh
    assert meta.scope == "internal"


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 (localhost test)
        return r.status, r.read().decode()


def test_callback_server_captures_code_and_ignores_favicon():
    port = 8793
    server = ra._CallbackServer(port)
    result: dict = {}

    def run():
        result["code"], result["state"] = server.wait_for_code(timeout=10)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    # a stray favicon hit must NOT end the wait
    try:
        _get(f"http://127.0.0.1:{port}/favicon.ico")
    except Exception:
        pass
    status, body = _get(f"http://127.0.0.1:{port}/callback?code=THECODE&state=xyz")
    t.join(timeout=5)
    assert status == 200 and "complete" in body.lower()
    assert result == {"code": "THECODE", "state": "xyz"}


def test_callback_server_surfaces_error():
    port = 8794
    server = ra._CallbackServer(port)
    err: dict = {}

    def run():
        try:
            server.wait_for_code(timeout=10)
        except RuntimeError as e:
            err["msg"] = str(e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    _get(f"http://127.0.0.1:{port}/callback?error=access_denied")
    t.join(timeout=5)
    assert "access_denied" in err.get("msg", "")


# --------------------------------------------------------------------------- #
# Consent deadline (Sep 10 2026: login blocked 2h44m because the redirect wait had
# only a per-request socket timeout and looped forever). These pin the fix.
# --------------------------------------------------------------------------- #
def test_callback_server_times_out_without_redirect():
    """No request ever arrives -> OAuthConsentTimeout (a TimeoutError) at the deadline,
    and the port is released (a second server can bind it immediately)."""
    port = 8795
    server = ra._CallbackServer(port)
    t0 = time.monotonic()
    try:
        server.wait_for_code(timeout=1.0)
    except TimeoutError as e:  # the item's contract: a TimeoutError, not a hang
        assert isinstance(e, ra.OAuthConsentTimeout)
        assert "consent" in str(e).lower()
    else:
        raise AssertionError("wait_for_code returned without a redirect")
    elapsed = time.monotonic() - t0
    assert 0.9 <= elapsed < 3.0, f"deadline not honoured: {elapsed:.2f}s"
    assert server.result == {}  # nothing captured
    # port released on the timeout path: re-binding must succeed (bind failure would
    # surface as OSError, not the TimeoutError we expect from the fresh short wait)
    try:
        ra._CallbackServer(port).wait_for_code(timeout=0.2)
    except ra.OAuthConsentTimeout:
        pass


def test_callback_server_favicon_hit_does_not_extend_deadline():
    """An incidental hit (favicon/prefetch) consumes budget instead of restarting the
    clock — with a per-request timeout the old loop would wait another full period."""
    port = 8796
    server = ra._CallbackServer(port)
    err: dict = {}
    t0 = time.monotonic()

    def run():
        try:
            server.wait_for_code(timeout=1.0)
        except ra.OAuthConsentTimeout as e:
            err["exc"] = e
        err["elapsed"] = time.monotonic() - t0

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.4)
    try:
        _get(f"http://127.0.0.1:{port}/favicon.ico")
    except Exception:  # 404 is the expected answer
        pass
    t.join(timeout=5)
    assert not t.is_alive(), "wait_for_code still blocked after the deadline"
    assert "exc" in err, "favicon hit ended the wait without a TimeoutError"
    assert err["elapsed"] < 2.0, f"favicon hit extended the wait: {err['elapsed']:.2f}s"


def test_callback_server_code_before_deadline_still_succeeds():
    """The deadline must not break the happy path: a code arriving inside the window
    is returned immediately and the wait does not run to the deadline."""
    port = 8797
    server = ra._CallbackServer(port)
    result: dict = {}
    t0 = time.monotonic()

    def run():
        result["code"], result["state"] = server.wait_for_code(timeout=5.0)
        result["elapsed"] = time.monotonic() - t0

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.3)
    status, body = _get(f"http://127.0.0.1:{port}/callback?code=EARLY&state=s1")
    t.join(timeout=5)
    assert status == 200 and "complete" in body.lower()
    assert result["code"] == "EARLY" and result["state"] == "s1"
    assert result["elapsed"] < 3.0  # returned on the redirect, not at the 5s deadline


def _seed_token_file(cfg) -> tuple[bytes, float]:
    from mcp.shared.auth import OAuthToken

    store = ra.FileTokenStorage(cfg.robinhood_oauth_file)
    asyncio.run(store.set_tokens(OAuthToken(access_token="OLD", refresh_token="R",
                                            token_type="Bearer")))
    st = os.stat(cfg.robinhood_oauth_file)
    return open(cfg.robinhood_oauth_file, "rb").read(), st.st_mtime_ns


def _run_login(cfg, raising, **kw):
    """Run ra.login with _login_async stubbed to raise `raising`; return (rc, stdout)."""
    async def stub(_cfg, *, timeout_s):
        raise raising

    out = io.StringIO()
    with mock.patch.object(ra, "_login_async", stub), contextlib.redirect_stdout(out):
        rc = ra.login(cfg, **kw)
    return rc, out.getvalue()


def test_login_consent_stalled_exits_3_and_leaves_token_file_alone():
    """anyio wraps the callback's exception in an ExceptionGroup; login must still
    recognise the stall, print the runbook line, exit 3 and not touch the token file."""
    cfg = _cfg(_tmpdir())
    before, mtime = _seed_token_file(cfg)
    wrapped = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [ra.OAuthConsentTimeout("No Robinhood OAuth redirect within 1s")],
    )
    rc, out = _run_login(cfg, wrapped, timeout_s=1.0)
    assert rc == ra.EXIT_CONSENT_STALLED == 3
    assert "consent stalled — leaving Robinhood DEGRADED; the bot trades without RH context" in out
    assert "Token file untouched" in out
    assert "Login failed" not in out  # the generic path did not also fire
    assert open(cfg.robinhood_oauth_file, "rb").read() == before
    assert os.stat(cfg.robinhood_oauth_file).st_mtime_ns == mtime
    assert ra.has_tokens(cfg) is True  # OLD tokens still there for the reader


def test_login_detects_stall_through_nested_groups_and_cause_chains():
    """Two task groups (streamablehttp_client + ClientSession) and an SDK re-raise
    with `from` must all still map to exit 3."""
    cfg = _cfg(_tmpdir())
    inner = ra.OAuthConsentTimeout("stalled")
    try:
        raise RuntimeError("OAuth flow error") from inner
    except RuntimeError as chained:
        nested = ExceptionGroup("outer", [ExceptionGroup("inner", [chained])])
    rc, out = _run_login(cfg, nested, timeout_s=2.0)
    assert rc == 3 and "DEGRADED" in out

    bare_rc, _ = _run_login(cfg, ra.OAuthConsentTimeout("bare"), timeout_s=2.0)
    assert bare_rc == 3


def test_login_other_errors_still_exit_1():
    cfg = _cfg(_tmpdir())
    rc, out = _run_login(cfg, RuntimeError("Robinhood denied authorization: access_denied"))
    assert rc == 1 and "Login failed" in out and "DEGRADED" not in out
    # a plain TimeoutError that is NOT the consent stall (e.g. some socket timeout
    # surfacing raw) stays on the generic path — we only claim the stall we can prove
    rc2, _ = _run_login(cfg, ExceptionGroup("g", [TimeoutError("socket")]))
    assert rc2 == 1


def test_login_timeout_resolves_env_then_default():
    cfg = _cfg(_tmpdir())
    seen: dict = {}

    async def capture(_cfg, *, timeout_s):
        seen["timeout_s"] = timeout_s
        return ["get_equity_positions"]

    with mock.patch.object(ra, "_login_async", capture), contextlib.redirect_stdout(io.StringIO()):
        with mock.patch.dict(os.environ, {ra.LOGIN_TIMEOUT_ENV: "42"}):
            assert ra.login(cfg) == 0 and seen["timeout_s"] == 42.0
        with mock.patch.dict(os.environ, {ra.LOGIN_TIMEOUT_ENV: ""}):
            assert ra.login(cfg) == 0 and seen["timeout_s"] == ra.DEFAULT_LOGIN_TIMEOUT_S == 600.0
        with mock.patch.dict(os.environ, {ra.LOGIN_TIMEOUT_ENV: "nope"}):
            assert ra.login(cfg) == 0 and seen["timeout_s"] == 600.0  # garbage -> default
        with mock.patch.dict(os.environ, {ra.LOGIN_TIMEOUT_ENV: "0"}):
            assert ra.login(cfg) == 0 and seen["timeout_s"] == 600.0  # 0s would never wait
        with mock.patch.dict(os.environ, {ra.LOGIN_TIMEOUT_ENV: "42"}):
            assert ra.login(cfg, timeout_s=7) == 0 and seen["timeout_s"] == 7  # explicit wins


def test_login_explicit_non_positive_timeout_resolves_env_then_default():
    # Review finding (run-7 C1): `login --timeout 0` bypassed the <= 0 guard
    # (only None went through login_timeout_from_env) and reached
    # wait_for_code, which raised the consent timeout at once — exit 3, no wait.
    cfg = _cfg(_tmpdir())
    seen: dict = {}

    async def capture(_cfg, *, timeout_s):
        seen["timeout_s"] = timeout_s
        return ["get_equity_positions"]

    with mock.patch.object(ra, "_login_async", capture), contextlib.redirect_stdout(io.StringIO()):
        with mock.patch.dict(os.environ, {ra.LOGIN_TIMEOUT_ENV: ""}):
            assert ra.login(cfg, timeout_s=0) == 0 and seen["timeout_s"] == 600.0
            assert ra.login(cfg, timeout_s=-5) == 0 and seen["timeout_s"] == 600.0
        with mock.patch.dict(os.environ, {ra.LOGIN_TIMEOUT_ENV: "42"}):
            assert ra.login(cfg, timeout_s=0.0) == 0 and seen["timeout_s"] == 42.0
            assert ra.login(cfg, timeout_s=7) == 0 and seen["timeout_s"] == 7  # positive still wins


def test_main_timeout_flag_reaches_login():
    cfg = _cfg(_tmpdir())
    seen: dict = {}

    def fake_login(_cfg, *, timeout_s=None):
        seen["timeout_s"] = timeout_s
        return 0

    with mock.patch("investment_strategy.config.load_config", return_value=cfg), \
            mock.patch.object(ra, "login", fake_login):
        assert ra.main(["login", "--timeout", "7.5"]) == 0
        assert seen["timeout_s"] == 7.5
        assert ra.main(["login"]) == 0
        assert seen["timeout_s"] is None  # -> login() resolves env/default itself


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
