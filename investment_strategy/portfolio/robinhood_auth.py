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

CONSENT DEADLINE (Sep 10 2026 incident): the redirect catcher used to loop
``while not result: handle_request()`` with only a per-request socket timeout, so a
login nobody approved blocked its caller forever — a re-auth started 14:55 CT sat
2h44m holding the callback port and wedged the resident session that launched it
(which then false-fired the away-mode fallback). ``wait_for_code`` now runs against a
wall-clock deadline (ROBINHOOD_LOGIN_TIMEOUT_S / ``login --timeout``, default 600s)
and raises ``OAuthConsentTimeout``; the ``login`` command turns that into exit code
3 with the runbook line ("consent stalled — leaving Robinhood DEGRADED") and never
touches the token file, so the bot keeps trading on Alpaca without RH context and
the operator decides whether to retry (ops/away_mode.md: stop after two stalls).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..config import Config

log = logging.getLogger("robinhood")

#: Wall-clock budget for the operator to approve the consent screen. 10 minutes is
#: generous for a phone push + browser click and short enough that a stalled login
#: cannot wedge the session that launched it (the Sep 10 login sat 2h44m).
DEFAULT_LOGIN_TIMEOUT_S = 600.0
#: Env override for the deadline (read at the CLI boundary — `login` is a one-shot
#: operator command, not a bot-runtime path, so it is not a Config field).
LOGIN_TIMEOUT_ENV = "ROBINHOOD_LOGIN_TIMEOUT_S"
#: `login` exit code when consent never arrived (1 = other failure, 2 = not configured).
EXIT_CONSENT_STALLED = 3
#: Upper bound on one handle_request() wait so the deadline is re-checked regularly.
_POLL_S = 5.0
#: How often the wait prints a "still waiting" line (greppable progress for an
#: operator tailing a backgrounded login).
_WAIT_PROGRESS_S = 60.0


class OAuthConsentTimeout(TimeoutError):
    """Nobody approved the Robinhood consent screen before the login deadline."""


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


def build_provider(
    cfg: Config, *, interactive: bool, login_timeout_s: float = DEFAULT_LOGIN_TIMEOUT_S
):
    """An ``OAuthClientProvider`` (an httpx.Auth) wired to our file storage.

    interactive=True installs browser + localhost-callback handlers for the one-time
    login. interactive=False omits them: refreshes still work unattended, but if the
    refresh token is dead the SDK raises instead of silently popping a browser during
    a trading loop — the reader catches that and logs "re-run login".

    login_timeout_s bounds the interactive wait for the consent redirect (see the
    module docstring); it is ignored when interactive=False.
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
        print(
            f"Waiting for the redirect on {_redirect_uri(cfg)} "
            f"(giving up after {login_timeout_s:.0f}s) …"
        )
        code, state = await asyncio.to_thread(callback.wait_for_code, login_timeout_s)
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
    end the wait early — but only until a wall-clock deadline, after which it raises
    ``OAuthConsentTimeout`` (a ``TimeoutError``) and releases the port.
    """

    def __init__(self, port: int):
        self.port = port
        self.result: dict[str, str] = {}

    def wait_for_code(
        self, timeout: float = DEFAULT_LOGIN_TIMEOUT_S
    ) -> tuple[str, str | None]:
        """Block until the redirect lands or ``timeout`` seconds of wall clock pass.

        Raises ``OAuthConsentTimeout`` on expiry and ``RuntimeError`` if Robinhood
        redirected with ``?error=``. The port is released on every exit path.
        """
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
        # socketserver's `timeout` bounds ONE handle_request() and returns silently on
        # expiry, so a loop keyed only on it never ends (Sep 10 2026: 2h44m wedge).
        # Track a monotonic deadline and re-derive the per-call timeout every pass —
        # a favicon/prefetch hit consumes budget instead of restarting the clock.
        started = time.monotonic()
        deadline = started + max(0.0, float(timeout))
        next_progress = started + _WAIT_PROGRESS_S
        try:
            while not self.result:
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    break
                if now >= next_progress:
                    print(
                        f"Still waiting for the Robinhood redirect "
                        f"({remaining:.0f}s left before giving up) …"
                    )
                    next_progress = now + _WAIT_PROGRESS_S
                httpd.timeout = min(_POLL_S, remaining)
                httpd.handle_request()  # one request; loop past favicon/prefetch
        finally:
            httpd.server_close()  # release the port on success, error AND timeout

        if "error" in self.result:
            raise RuntimeError(f"Robinhood denied authorization: {self.result['error']}")
        if "code" not in self.result:
            raise OAuthConsentTimeout(
                f"No Robinhood OAuth redirect within {float(timeout):.0f}s — "
                "consent was never approved."
            )
        return self.result["code"], self.result.get("state") or None


def _find_consent_timeout(exc: BaseException) -> OAuthConsentTimeout | None:
    """Locate an ``OAuthConsentTimeout`` inside whatever reached ``asyncio.run``.

    The callback runs inside the SDK's anyio task groups (streamablehttp_client and
    ClientSession), so anyio 4.x delivers it wrapped in one or more
    ``ExceptionGroup``s; the SDK may also chain it as ``__cause__``/``__context__``.
    Walks all three, cycle-safe, so the CLI can name the stall precisely instead of
    printing a generic "Login failed: unhandled errors in a TaskGroup".
    """
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, OAuthConsentTimeout):
            return cur
        if isinstance(cur, BaseExceptionGroup):
            stack.extend(cur.exceptions)
        for linked in (cur.__cause__, cur.__context__):
            if linked is not None:
                stack.append(linked)
    return None


def login_timeout_from_env(default: float = DEFAULT_LOGIN_TIMEOUT_S) -> float:
    """Resolve the consent deadline from ROBINHOOD_LOGIN_TIMEOUT_S (blank/garbage/
    non-positive → ``default``, with a warning so a typo can't silently mean 0s)."""
    raw = os.getenv(LOGIN_TIMEOUT_ENV, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        val = -1.0
    if val <= 0:
        log.warning(
            "%s=%r is not a positive number of seconds; using %.0fs",
            LOGIN_TIMEOUT_ENV, raw, default,
        )
        return default
    return val


# --------------------------------------------------------------------------- #
# The one-time login handshake
# --------------------------------------------------------------------------- #
async def _login_async(
    cfg: Config, *, timeout_s: float = DEFAULT_LOGIN_TIMEOUT_S
) -> list[str]:
    """Drive the full OAuth flow by opening an authenticated MCP session.

    Connecting triggers the SDK's lazy handshake on the first 401; on success the
    tokens are already persisted by FileTokenStorage. We then list tools to prove the
    token works and to surface the exact read tool names for ROBINHOOD_POSITIONS_TOOL.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    provider = build_provider(cfg, interactive=True, login_timeout_s=timeout_s)
    async with streamablehttp_client(cfg.robinhood_mcp_url, auth=provider) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = await session.list_tools()
            return [t.name for t in tools.tools]


def login(cfg: Config, *, timeout_s: float | None = None) -> int:
    """Interactive entry point. Returns a process exit code.

    0 = authorized, 1 = handshake failed, 2 = not configured, 3 = consent stalled
    (no redirect within ``timeout_s``; token file untouched, Robinhood left DEGRADED).
    ``timeout_s=None`` — or an explicit non-positive value — resolves
    ROBINHOOD_LOGIN_TIMEOUT_S (default 600); a 0 s deadline would never wait.
    """
    if not cfg.robinhood_mcp_url:
        print("ROBINHOOD_MCP_URL is empty — nothing to authorize against.")
        return 2
    if timeout_s is not None and timeout_s <= 0:
        # `--timeout 0` used to reach wait_for_code and raise the consent
        # timeout at once (exit 3, no wait at all) — the same guard the env
        # path applies belongs here too.
        log.warning(
            "--timeout %r is not a positive number of seconds; using %s / default",
            timeout_s, LOGIN_TIMEOUT_ENV,
        )
        timeout_s = None
    if timeout_s is None:
        timeout_s = login_timeout_from_env()
    print("Robinhood Agentic MCP — OAuth login")
    print(f"  endpoint : {cfg.robinhood_mcp_url}")
    print(f"  scope    : {cfg.robinhood_scope or '(server default)'}")
    print(f"  tokens   : {cfg.robinhood_oauth_file}")
    print(f"  deadline : {timeout_s:.0f}s for consent ({LOGIN_TIMEOUT_ENV} / --timeout)")
    print(
        "\nAuthorize the DEDICATED agentic account you fund for the bot — NOT your "
        "main portfolio. The token is trade-capable; we only ever call read tools.\n"
    )
    try:
        tools = asyncio.run(_login_async(cfg, timeout_s=timeout_s))
    except Exception as e:
        stalled = _find_consent_timeout(e)
        if stalled is not None:
            # Runbook (ops/away_mode.md): a stalled consent is DEGRADED, not broken.
            # Nothing was written — the token file only changes on a successful
            # exchange — so the reader's existing latch/self-heal state is intact.
            log.warning("Robinhood OAuth login: consent stalled after %.0fs", timeout_s)
            print(
                f"\nNo Robinhood redirect within {timeout_s:.0f}s: consent stalled — "
                "leaving Robinhood DEGRADED; the bot trades without RH context."
            )
            print(
                f"Token file untouched ({cfg.robinhood_oauth_file}). Re-run login when "
                "you can approve the consent screen; per ops/away_mode.md, after two "
                "stalls stop and leave it DEGRADED."
            )
            return EXIT_CONSENT_STALLED
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
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "login only: give up waiting for the consent redirect after this many "
            f"seconds (default: ${LOGIN_TIMEOUT_ENV} or {DEFAULT_LOGIN_TIMEOUT_S:.0f}); "
            f"exit code {EXIT_CONSENT_STALLED} on expiry, token file untouched."
        ),
    )
    args = parser.parse_args(argv)
    cfg = load_config()  # also loads .env, so the env override below sees it
    if args.command == "login":
        return login(cfg, timeout_s=args.timeout)
    return {"status": status, "logout": logout}[args.command](cfg)


if __name__ == "__main__":
    raise SystemExit(main())
