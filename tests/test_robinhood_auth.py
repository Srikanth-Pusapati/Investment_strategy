"""Tests for the Robinhood OAuth handshake plumbing.

Pure logic + local sockets, no Robinhood network calls: we exercise the file-backed
token storage (round-trip + 0600 perms), the has_tokens gate, the provider wiring
(redirect_uri / scope / public-client method), and the localhost redirect catcher
(parses ?code&state, ignores favicon, surfaces ?error). The live OAuth handshake
against Robinhood is manual (`robinhood_auth login`) and not covered here.

Runnable two ways:
    .venv/bin/python tests/test_robinhood_auth.py
    .venv/bin/pytest tests/
"""
from __future__ import annotations

import asyncio
import os
import stat
import sys
import threading
import urllib.request
from types import SimpleNamespace

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
