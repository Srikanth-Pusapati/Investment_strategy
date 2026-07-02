"""OAuth 2.1 (PKCE) handshake for Robinhood's official Agentic Trading MCP.

Robinhood's agentic MCP is OAuth-protected. Rather than hand you a token, RH runs a
standard OAuth 2.1 authorization-code + PKCE flow with Dynamic Client Registration:

    protected-resource metadata  ->  authorization-server metadata
        auth endpoint   https://robinhood.com/oauth
        token endpoint  https://api.robinhood.com/oauth2/token/
        register (DCR)  https://agent.robinhood.com/oauth/trading/register
        public client (token_endpoint_auth_method="none"), PKCE S256, refresh_token

The MCP Python SDK's ``OAuthClientProvider`` implements every step; we supply only
(1) a file-backed ``TokenStorage`` so the tokens survive restarts and (2) a tiny
localhost web server to catch the redirect. Run the handshake once:

    python -m investment_strategy.portfolio.robinhood_auth login

It opens your browser, you log into the DEDICATED agentic Robinhood account and
approve, and the access + refresh tokens land in ROBINHOOD_OAUTH_FILE (default
state/robinhood_oauth.json, gitignored). RobinhoodReader then loads them and the
SDK auto-refreshes on expiry — no pasted ROBINHOOD_MCP_TOKEN needed.

READ-ONLY NOTE: Robinhood advertises a single OAuth scope ("internal"); there is no
OAuth-level read-vs-trade split. The token IS trade-capable. Our read-only guarantee
therefore lives in the CLIENT (we only ever call read tools) and in you authorizing a
separate, funded agentic account — NOT in the token's scope. Guard this file like a
credential.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..config import Config

log = logging.getLogger("robinhood")


# --------------------------------------------------------------------------- #
# Token persistence
# --------------------------------------------------------------------------- #
class FileTokenStorage:
    """MCP ``TokenStorage`` backed by a single 0600 JSON file.

    Holds both the dynamically-registered client info and the OAuth tokens so the
    whole handshake only happens once; subsequent runs reuse the client_id and just
    refresh. Implemented structurally (not by subclassing) so importing this module
    never hard-fails if the SDK isn't installed — the methods match the protocol.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    # -- raw file I/O ------------------------------------------------------- #
    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)  # tokens are credentials — owner-only
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # -- TokenStorage protocol --------------------------------------------- #
    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(exclude_none=True)
        self._write(data)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(exclude_none=True, mode="json")
        self._write(data)


def has_tokens(cfg: Config) -> bool:
    """True if an OAuth handshake has already been completed (tokens on disk)."""
    try:
        return bool(json.loads(Path(cfg.robinhood_oauth_file).read_text()).get("tokens"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False


# --------------------------------------------------------------------------- #
# Provider construction
# --------------------------------------------------------------------------- #
def _redirect_uri(cfg: Config) -> str:
    return f"http://localhost:{cfg.robinhood_callback_port}/callback"


def build_provider(cfg: Config, *, interactive: bool):
    """An ``OAuthClientProvider`` (an httpx.Auth) wired to our file storage.

    interactive=True installs browser + localhost-callback handlers for the one-time
    login. interactive=False omits them: refreshes still work unattended, but if the
    refresh token is dead the SDK raises instead of silently popping a browser during
    a trading loop — the reader catches that and logs "re-run login".
    """
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    metadata = OAuthClientMetadata(
        client_name=cfg.robinhood_client_name,
        redirect_uris=[_redirect_uri(cfg)],  # type: ignore[list-item]
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",  # public client (PKCE only)
        scope=cfg.robinhood_scope or None,
    )
    storage = FileTokenStorage(cfg.robinhood_oauth_file)

    if not interactive:
        return OAuthClientProvider(
            server_url=cfg.robinhood_mcp_url,
            client_metadata=metadata,
            storage=storage,
        )

    callback = _CallbackServer(cfg.robinhood_callback_port)

    async def redirect_handler(authorization_url: str) -> None:
        print("\nOpening your browser to authorize Robinhood…")
        print("If it doesn't open, paste this URL manually:\n")
        print(f"  {authorization_url}\n")
        try:
            webbrowser.open(authorization_url)
        except Exception:  # headless / no browser — manual paste still works
            pass

    async def callback_handler() -> tuple[str, str | None]:
        print(f"Waiting for the redirect on {_redirect_uri(cfg)} …")
        code, state = await asyncio.to_thread(callback.wait_for_code)
        return code, state

    return OAuthClientProvider(
        server_url=cfg.robinhood_mcp_url,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


# --------------------------------------------------------------------------- #
# Localhost redirect catcher
# --------------------------------------------------------------------------- #
class _CallbackServer:
    """Serves exactly the OAuth redirect on localhost, capturing ?code&state.

    Ignores incidental hits (e.g. /favicon.ico) and keeps serving until the real
    callback with a ``code`` (or an ``error``) arrives, so a browser prefetch can't
    end the wait early.
    """

    def __init__(self, port: int):
        self.port = port
        self.result: dict[str, str] = {}

    def wait_for_code(self, timeout: float = 300.0) -> tuple[str, str | None]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (stdlib naming)
                params = parse_qs(urlparse(self.path).query)
                code = params.get("code", [None])[0]
                error = params.get("error", [None])[0]
                if not code and not error:
                    self.send_response(404)
                    self.end_headers()
                    return
                if error:
                    outer.result["error"] = error
                    body = f"Authorization failed: {error}. You can close this tab."
                else:
                    outer.result["code"] = code  # type: ignore[assignment]
                    outer.result["state"] = params.get("state", [""])[0]
                    body = "Robinhood authorization complete — return to the terminal."
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    f"<html><body style='font:16px sans-serif;padding:3rem'>"
                    f"{body}</body></html>".encode()
                )

            def log_message(self, *_args):  # silence stdlib request logging
                pass

        httpd = HTTPServer(("127.0.0.1", self.port), Handler)
        httpd.timeout = timeout
        try:
            while not self.result:
                httpd.handle_request()  # one request; loop past favicon/prefetch
        finally:
            httpd.server_close()

        if "error" in self.result:
            raise RuntimeError(f"Robinhood denied authorization: {self.result['error']}")
        if "code" not in self.result:
            raise TimeoutError("Timed out waiting for the Robinhood OAuth redirect.")
        return self.result["code"], self.result.get("state") or None


# --------------------------------------------------------------------------- #
# The one-time login handshake
# --------------------------------------------------------------------------- #
async def _login_async(cfg: Config) -> list[str]:
    """Drive the full OAuth flow by opening an authenticated MCP session.

    Connecting triggers the SDK's lazy handshake on the first 401; on success the
    tokens are already persisted by FileTokenStorage. We then list tools to prove the
    token works and to surface the exact read tool names for ROBINHOOD_POSITIONS_TOOL.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    provider = build_provider(cfg, interactive=True)
    async with streamablehttp_client(cfg.robinhood_mcp_url, auth=provider) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = await session.list_tools()
            return [t.name for t in tools.tools]


def login(cfg: Config) -> int:
    """Interactive entry point. Returns a process exit code."""
    if not cfg.robinhood_mcp_url:
        print("ROBINHOOD_MCP_URL is empty — nothing to authorize against.")
        return 2
    print("Robinhood Agentic MCP — OAuth login")
    print(f"  endpoint : {cfg.robinhood_mcp_url}")
    print(f"  scope    : {cfg.robinhood_scope or '(server default)'}")
    print(f"  tokens   : {cfg.robinhood_oauth_file}")
    print(
        "\nAuthorize the DEDICATED agentic account you fund for the bot — NOT your "
        "main portfolio. The token is trade-capable; we only ever call read tools.\n"
    )
    try:
        tools = asyncio.run(_login_async(cfg))
    except Exception as e:
        log.exception("Robinhood OAuth login failed")
        print(f"\nLogin failed: {e}")
        return 1
    print("\n✅ Authorized. Tokens saved (access + refresh).")
    print(f"Available MCP tools ({len(tools)}):")
    for name in sorted(tools):
        print(f"  - {name}")
    print(
        "\nSet ROBINHOOD_POSITIONS_TOOL in .env to the holdings/positions tool above "
        "to import your agentic account's holdings as context."
    )
    return 0


def status(cfg: Config) -> int:
    """Print whether a handshake has been completed and where tokens live."""
    ok = has_tokens(cfg)
    print(f"Robinhood OAuth: {'authorized' if ok else 'NOT authorized'}")
    print(f"  token file : {cfg.robinhood_oauth_file}")
    print(f"  redirect   : {_redirect_uri(cfg)}")
    if not ok:
        print("\nRun:  python -m investment_strategy.portfolio.robinhood_auth login")
    return 0 if ok else 1


def logout(cfg: Config) -> int:
    """Delete the persisted tokens/client registration (forces a fresh login)."""
    try:
        Path(cfg.robinhood_oauth_file).unlink()
        print(f"Removed {cfg.robinhood_oauth_file}. Re-run `login` to reauthorize.")
    except FileNotFoundError:
        print("Nothing to remove — no token file present.")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    from ..config import load_config

    parser = argparse.ArgumentParser(
        prog="python -m investment_strategy.portfolio.robinhood_auth",
        description="OAuth handshake for Robinhood's Agentic Trading MCP.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="login",
        choices=["login", "status", "logout"],
        help="login (default): run the handshake; status: check; logout: delete tokens.",
    )
    args = parser.parse_args(argv)
    cfg = load_config()
    return {"login": login, "status": status, "logout": logout}[args.command](cfg)


if __name__ == "__main__":
    raise SystemExit(main())
