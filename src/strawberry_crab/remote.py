"""MCP servers over streamable HTTP, signed in with OAuth (WIRING.md §8b, ADAPTERS.md).

    [tools.servers.recall]
    topic = "notes"
    url = "https://recall.example.com/mcp"

A server with a `url` in place of a `command` is reached over MCP's streamable HTTP transport (the SDK's
client). A server that wants a login answers 401; the user signs in once, by hand:

    strawberry tools login recall      # a browser, a redirect to this machine, the tokens saved
    strawberry tools logout recall     # the tokens deleted

`login` runs the SDK's OAuth client (discovery, dynamic client registration, PKCE, the state check)
with a listener on 127.0.0.1 and a random port for the redirect, and saves what the daemon needs to use
and refresh the tokens in `<state>/tokens/<server>.json`, readable by the user alone (0600 in a 0700
directory). The daemon never logs in: `TokenAuth` sends the access token, refreshes it shortly before it
expires or when the server answers 401, and when that is refused (or there are no tokens and the server
wants some) the server is unavailable with one plain line, "recall needs a login: run `strawberry tools
login recall`", and she says she can't reach it. A refresh that fails for any other reason (the network,
a 5xx) keeps the tokens for the next try.

Tokens are never logged or printed, and never reach /health, /config or the Brain UI: they live in that
file and in this process's memory. The redirect listener has no access log (its query is the code).

Addresses. The server's own URL is https, or http on this machine only (`url_problem`). Every request
the client makes goes through `GuardedTransport`: the server's own host, or any other host (the
authorization server, the token endpoint, the registration endpoint its metadata names) on https only
and only when every address it resolves to is public (adapters/web.py `resolves_public`, the page
reader's check), so metadata cannot point her at 127.0.0.1, the LAN or a cloud metadata address. The SDK
follows a redirect only within one origin. The sign-in page itself is opened in the user's browser, not
fetched here, and must be https too (or http on this machine).
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import os
import re
import socket
import time
import webbrowser
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlsplit

import httpx2   # the MCP SDK's HTTP client (a dependency of `mcp`)

from . import paths

log = logging.getLogger("strawberryd.remote")
# Both log a request line or a session id at INFO; neither is anything the journal needs.
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("mcp.client.streamable_http").setLevel(logging.WARNING)

LOGIN_TIMEOUT_S = 300.0     # how long `strawberry tools login` waits for the browser
REFRESH_EARLY_S = 60.0      # refresh this long before the access token expires
PUBLIC_FOR_S = 300.0        # how long a host's public-address check holds
CONNECT_S = 5.0             # the HTTP connect timeout (Server's connect_timeout_s bounds the whole connect)
SERVER_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class NeedsLogin(RuntimeError):
    """The server wants a login and there is no usable token: the user runs `strawberry tools login`."""


class BlockedAddress(RuntimeError):
    """A request to an address that is never called from here (GuardedTransport)."""


class LoginFailed(RuntimeError):
    """`strawberry tools login` did not end with tokens; the message is for the user."""


def login_line(server: str) -> str:
    return f"{server} needs a login: run `strawberry tools login {server}`"


def is_loopback(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def url_problem(url: Any) -> str | None:
    """Why a server's `url` (or a sign-in page) is not used, or None: https, or http on this machine only;
    no login part, one plain address."""
    if not isinstance(url, str) or not url:
        return "no address"
    if any(c.isspace() for c in url) or "\\" in url:
        return "not one plain address"
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port   # noqa: B018 - raises on a port that is not one
    except ValueError:
        return "not an address"
    if parts.scheme not in ("http", "https") or not host:
        return "not an http(s) address"
    if parts.username is not None or parts.password is not None:
        return "a login part in the address"
    if parts.scheme == "http" and not is_loopback(host):
        return "plain http only on this machine (127.0.0.1); use https"
    return None


# ------------------------------------------------------------------------------------------------- the tokens


def token_file(server: str) -> Path:
    if not SERVER_NAME.match(server or ""):
        raise ValueError("a server name with letters, digits, '.', '_' or '-' only")
    return paths.state_dir() / "tokens" / f"{server}.json"


class TokenStore:
    """`<state>/tokens/<server>.json`: the tokens and what refreshing them takes (the token endpoint, the
    client id, the resource). Written atomically, 0600 in a 0700 directory; never logged."""

    def __init__(self, server: str) -> None:
        self.server = server
        self.path = token_file(server)

    def load(self) -> dict[str, Any] | None:
        try:
            if os.name == "posix" and self.path.stat().st_mode & 0o077:
                os.chmod(self.path, 0o600)    # someone widened it: only the user reads tokens
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.warning("tools: %s: its token file could not be read; log in again", self.server)
            return None
        return data if isinstance(data, dict) else None

    def save(self, data: dict[str, Any]) -> None:
        paths.private_dir(self.path.parent)
        paths.write_atomic(self.path, json.dumps(data, indent=1), private=True)

    def delete(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False

    def access_token(self) -> str:
        tokens = (self.load() or {}).get("tokens")
        token = tokens.get("access_token") if isinstance(tokens, dict) else None
        return token if isinstance(token, str) else ""


def _tokens(data: dict[str, Any] | None) -> dict[str, Any]:
    tokens = (data or {}).get("tokens")
    return tokens if isinstance(tokens, dict) and isinstance(tokens.get("access_token"), str) else {}


# --------------------------------------------------------------------------------------------- the addresses


class GuardedTransport(httpx2.AsyncBaseTransport):
    """Every request of one server's HTTP client: its own host; another host only over https and only when
    every address the name resolves to is public. Raises BlockedAddress (naming the host, never the path or
    the query) for anything else."""

    def __init__(self, home: str, inner: Any = None) -> None:
        self.home = (urlsplit(home).hostname or "").lower()
        self.inner = inner or httpx2.AsyncHTTPTransport()
        self.public: dict[str, float] = {}     # host -> when its check runs out

    async def problem(self, scheme: str, host: str, port: int | None, userinfo: bytes | str) -> str | None:
        from .adapters.web import public_address, resolves_public   # local: the adapters import the core

        host = (host or "").lower()
        if userinfo:
            return "a login part in the address"
        if scheme not in ("http", "https"):
            return "not http(s)"
        if scheme == "http" and not is_loopback(host):
            return "plain http off this machine"
        if host == self.home:
            return None
        if is_loopback(host):
            return None if is_loopback(self.home) else "an address on this machine"
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            literal = None
        if literal is not None:
            return None if public_address(str(literal)) else "a private address"
        if self.public.get(host, 0.0) > time.monotonic():
            return None
        why = await resolves_public(f"https://{host}:{port or 443}/")
        if why:
            return why
        self.public[host] = time.monotonic() + PUBLIC_FOR_S
        return None

    async def handle_async_request(self, request: Any) -> Any:
        url = request.url
        why = await self.problem(url.scheme, url.host, url.port, url.userinfo)
        if why:
            raise BlockedAddress(f"{url.host}: {why}")
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def http_client(url: str, auth: Any) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(auth=auth, transport=GuardedTransport(url),
                              timeout=httpx2.Timeout(30.0, connect=CONNECT_S, read=300.0))


# ------------------------------------------------------------------------------------------ the daemon's auth


class TokenAuth(httpx2.Auth):
    """The daemon's side: the stored access token on every request, a refresh shortly before it expires or
    once on a 401, and on a refusal the tokens dropped and `dead` set to the login line (no exception inside
    the transport: the request goes on, the server answers 401, and the call fails as any call does).
    One refresh at a time, so a rotating refresh token is used once."""

    def __init__(self, server: str, store: TokenStore, on_dead: Callable[[str], None] | None = None) -> None:
        self.server = server
        self.store = store
        self.on_dead = on_dead
        self._lock: asyncio.Lock | None = None

    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def sync_auth_flow(self, request: Any):   # pragma: no cover - the client is async only
        raise RuntimeError("TokenAuth is for an async client")

    async def async_auth_flow(self, request: Any):
        refused = False                 # a refresh was refused in this flow: the login is over, said once
        async with self.lock():
            data = self.store.load()
            if _tokens(data) and self._expiring(data) and self._refresh_token(data):
                response = yield self._refresh_request(data)
                data = await self._after_refresh(data, response)
                refused = data is None
        sent = self._authorize(request, data)
        response = yield request
        if response.status_code != 401 or refused:
            return
        if not sent:
            self._give_up("it wants a login")
            return
        async with self.lock():
            latest = self.store.load()
            if _tokens(latest).get("access_token") not in (None, sent):
                data = latest           # refreshed by another request meanwhile
            elif self._refresh_token(data):
                refresh = yield self._refresh_request(data)
                data = await self._after_refresh(data, refresh)
                if data is None:
                    return              # refused: the 401 stands, and _after_refresh said why
            else:
                self._give_up("the server refused the token, and there is no refresh token")
                return
        if not self._authorize(request, data):
            self._give_up("the server refused the token")
            return
        response = yield request
        if response.status_code == 401:
            self._give_up("the server refused a fresh token")

    def _authorize(self, request: Any, data: dict[str, Any] | None) -> str:
        token = _tokens(data).get("access_token", "")
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        elif "Authorization" in request.headers:
            del request.headers["Authorization"]
        return token

    @staticmethod
    def _expiring(data: dict[str, Any] | None) -> bool:
        expires = (data or {}).get("expires_at")
        return isinstance(expires, (int, float)) and time.time() > expires - REFRESH_EARLY_S

    @staticmethod
    def _refresh_token(data: dict[str, Any] | None) -> str:
        token = _tokens(data).get("refresh_token")
        client = (data or {}).get("client") or {}
        usable = isinstance(token, str) and token and isinstance((data or {}).get("token_endpoint"), str)
        return token if usable and isinstance(client, dict) and client.get("client_id") else ""

    def _refresh_request(self, data: dict[str, Any]) -> httpx2.Request:
        client = data.get("client") or {}
        form = {"grant_type": "refresh_token", "refresh_token": self._refresh_token(data),
                "client_id": str(client.get("client_id"))}
        if isinstance(data.get("resource"), str):
            form["resource"] = data["resource"]
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        method, secret = client.get("token_endpoint_auth_method"), client.get("client_secret")
        if method == "client_secret_basic" and isinstance(secret, str):
            pair = f"{quote(str(client['client_id']), safe='')}:{quote(secret, safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        elif method == "client_secret_post" and isinstance(secret, str):
            form["client_secret"] = secret
        return httpx2.Request("POST", data["token_endpoint"], data=form, headers=headers)

    async def _after_refresh(self, data: dict[str, Any], response: Any) -> dict[str, Any] | None:
        """The stored data with the new tokens, saved; None when the refresh was refused (400, 401: the
        login is over); the old data, untouched, when it failed for another reason (the next try may work)."""
        await response.aread()
        if response.status_code in (400, 401):
            self._give_up("the refresh was refused")
            return None
        try:
            fresh = response.json() if response.status_code == 200 else None
        except ValueError:
            fresh = None
        if not isinstance(fresh, dict) or not isinstance(fresh.get("access_token"), str):
            log.warning("tools: %s: refreshing its login failed (HTTP %d); trying again on the next use", self.server,
                        response.status_code)
            return data
        old = _tokens(data)
        tokens = {"access_token": fresh["access_token"], "token_type": fresh.get("token_type") or "Bearer",
                  "refresh_token": fresh.get("refresh_token") or old.get("refresh_token"),
                  "scope": fresh.get("scope") or old.get("scope")}
        updated = {**data, "tokens": {k: v for k, v in tokens.items() if v},
                   "expires_at": _expires_at(fresh.get("expires_in")), "refreshed_at": time.time()}
        self.store.save(updated)
        log.info("tools: %s: login refreshed", self.server)
        return updated

    def _give_up(self, why: str) -> None:
        """The login is over: drop the tokens (keep how to reach the server) and say so once."""
        data = self.store.load()
        if data and "tokens" in data:
            data.pop("tokens", None)
            data.pop("expires_at", None)
            self.store.save(data)
        log.warning("tools: %s: %s; %s", self.server, why, login_line(self.server))
        if self.on_dead is not None:
            self.on_dead(login_line(self.server))


def _expires_at(expires_in: Any) -> float | None:
    return time.time() + float(expires_in) if isinstance(expires_in, (int, float)) and expires_in > 0 else None


class RemoteConnector:
    """`tools.Connector` for one server with a `url`: the SDK's streamable HTTP client on a guarded httpx2
    client with `TokenAuth`. `dead` is the login line once the server wants a login it does not have; the
    toolbox shows it as the server's error (Server.failed) instead of the SDK's own words."""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name = name
        self.url = config.get("url", "")
        self.dead = ""

    def _died(self, line: str) -> None:
        self.dead = line

    async def __call__(self, stack: AsyncExitStack, config: dict[str, Any]) -> Any:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        why = url_problem(self.url)
        if why:
            raise BlockedAddress(f"its url is not used: {why}")
        store = TokenStore(self.name)
        saved = store.load()
        if saved is not None and not _tokens(saved):
            # A login that ended (a refused refresh): nothing to try until the user logs in again.
            self.dead = login_line(self.name)
            raise NeedsLogin(self.dead)
        # No login saved at all: tried without one, so a server that wants none works; its 401 says "log in".
        self.dead = ""
        client = await stack.enter_async_context(http_client(self.url, TokenAuth(self.name, store, self._died)))
        read, write, *_ = await stack.enter_async_context(streamable_http_client(self.url, http_client=client))
        session = await stack.enter_async_context(ClientSession(read, write))
        try:
            await session.initialize()
        except Exception:
            if self.dead:
                raise NeedsLogin(self.dead) from None
            raise
        return session


# -------------------------------------------------------------------------------------- strawberry tools login


PAGE = ("<!doctype html><meta charset=utf-8><title>Strawberry</title>"
        "<p style='font:16px sans-serif;margin:3em'>{text}</p>")


class Callback:
    """The redirect's listener: 127.0.0.1, a random port, GET /callback once. No access log (the query is
    the authorization code)."""

    def __init__(self) -> None:
        self.future: asyncio.Future | None = None
        self.runner: Any = None
        self.uri = ""

    async def start(self) -> str:
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/callback", self._handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        await web.SockSite(self.runner, sock).start()
        self.future = asyncio.get_running_loop().create_future()
        self.uri = f"http://127.0.0.1:{sock.getsockname()[1]}/callback"
        return self.uri

    async def _handle(self, request: Any) -> Any:
        from aiohttp import web
        from mcp.shared.auth import AuthorizationCodeResult

        query = request.query
        assert self.future is not None
        if self.future.done():
            return web.Response(text=PAGE.format(text="This sign-in is already over; you can close this tab."),
                                content_type="text/html")
        if "error" in query:
            code = re.sub(r"[^\w.-]", "", query.get("error", ""))[:60] or "refused"
            self.future.set_exception(LoginFailed(f"the server said no ({code})"))
            return web.Response(text=PAGE.format(text="Not signed in. You can close this tab."), content_type="text/html")
        if not query.get("code"):
            return web.Response(status=400, text="no code")
        self.future.set_result(AuthorizationCodeResult(code=query["code"], state=query.get("state"),
                                                       iss=query.get("iss")))
        return web.Response(text=PAGE.format(text="Signed in. You can close this tab and go back to the terminal."),
                            content_type="text/html")

    async def wait(self, timeout_s: float) -> Any:
        assert self.future is not None
        try:
            return await asyncio.wait_for(asyncio.shield(self.future), timeout_s)
        except asyncio.TimeoutError:
            raise LoginFailed(f"no sign-in within {timeout_s / 60:.0f} minutes") from None

    async def close(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()


class _Memory:
    """The SDK's TokenStorage for one login, in memory; `login` saves what it got."""

    def __init__(self) -> None:
        self.tokens: Any = None
        self.client: Any = None

    async def get_tokens(self) -> Any:
        return self.tokens

    async def set_tokens(self, tokens: Any) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> Any:
        return self.client

    async def set_client_info(self, client_info: Any) -> None:
        self.client = client_info


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaves(inner)]
    return [exc]


def _why(exc: BaseException) -> str:
    """A login failure in a line for the user: never a token, a code or a state."""
    from mcp.client.auth import OAuthFlowError, OAuthRegistrationError, OAuthTokenError

    leaves = _leaves(exc)
    for leaf in leaves:
        if isinstance(leaf, (LoginFailed, BlockedAddress)):
            return str(leaf)
    for leaf in leaves:
        if isinstance(leaf, OAuthRegistrationError):
            return "the server would not register Strawberry as an app (dynamic client registration)"
        if isinstance(leaf, OAuthTokenError):
            return "the server did not accept the sign-in code"
        if isinstance(leaf, OAuthFlowError):
            text = str(leaf)
            if "State parameter mismatch" in text:
                return "the sign-in answer was not for this request (the state did not match)"
            if "issuer" in text.lower():
                return "the sign-in answer came from another server (issuer)"
            return "the sign-in did not go through"
    leaf = leaves[0] if leaves else exc
    return f"could not reach the server ({type(leaf).__name__})"


async def login(name: str, config: dict[str, Any], open_browser: bool = True, say: Callable[[str], None] = print,
                timeout_s: float = LOGIN_TIMEOUT_S, opener: Callable[[str], Any] | None = None) -> int:
    """`strawberry tools login <server>`: sign in in the browser and save the tokens. 0 when saved (or when
    the server wants no login), 1 when it did not go through, 2 for a server this cannot log in to."""
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import OAuthClientMetadata

    url = config.get("url")
    if not url:
        say(f"{name} is not a server with a url (only those log in)")
        return 2
    why = url_problem(url)
    if why:
        say(f"{name}: its url is not used ({why})")
        return 2
    try:
        store = TokenStore(name)
    except ValueError as exc:
        say(f"{name}: {exc}")
        return 2
    callback = Callback()
    redirect_uri = await callback.start()
    memory = _Memory()
    opener = opener or webbrowser.open

    async def redirect(address: str) -> None:
        problem = url_problem(address)
        if problem:
            raise BlockedAddress(f"the sign-in page is not opened: {problem}")
        say(f"Sign in to {name} in your browser. If it does not open, open this address:")
        say(address)
        if open_browser:
            try:
                opener(address)
            except Exception:   # no browser here: the address is printed
                pass

    provider = OAuthClientProvider(
        server_url=url,
        client_metadata=OAuthClientMetadata(client_name="Strawberry", redirect_uris=[redirect_uri],
                                            grant_types=["authorization_code", "refresh_token"],
                                            response_types=["code"], token_endpoint_auth_method="none"),
        storage=memory, redirect_handler=redirect, callback_handler=lambda: callback.wait(timeout_s))
    quiet = logging.getLogger("mcp")
    was = quiet.propagate, list(quiet.handlers)
    quiet.propagate, quiet.handlers = False, [logging.NullHandler()]   # its OAuth errors carry a state: one line here
    tools = 0
    try:
        async with http_client(url, provider) as client:
            async with streamable_http_client(url, http_client=client) as (read, write, *_):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = len((await session.list_tools()).tools)
    except BaseException as exc:   # the SDK's task groups raise groups; KeyboardInterrupt goes on below
        if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        say(f"{name}: not logged in: {_why(exc)}")
        return 1
    finally:
        quiet.propagate, quiet.handlers = was
        await callback.close()
    if memory.tokens is None:
        say(f"{name} answered without a login: nothing to save ({tools} tools)")
        return 0
    context = provider.context
    endpoint = (str(context.oauth_metadata.token_endpoint) if context.oauth_metadata and context.oauth_metadata.token_endpoint
                else urljoin(context.get_authorization_base_url(url), "/token"))
    client_info = memory.client or context.client_info
    client = {key: getattr(client_info, key, None) for key in ("client_id", "client_secret", "token_endpoint_auth_method")}
    tokens = memory.tokens
    store.save({
        "server_url": url,
        "token_endpoint": endpoint,
        "resource": context.get_resource_url() if context.should_include_resource_param(context.protocol_version) else None,
        "client": {k: v for k, v in client.items() if v},
        "tokens": {k: v for k, v in {"access_token": tokens.access_token, "token_type": tokens.token_type,
                                     "refresh_token": tokens.refresh_token, "scope": tokens.scope}.items() if v},
        "expires_at": _expires_at(tokens.expires_in),
        "saved_at": time.time(),
    })
    say(f"Logged in to {name}: {tools} tools. The tokens are in {store.path} (readable by you only); "
        f"`strawberry tools logout {name}` deletes them. She uses it from the next sentence that needs it.")
    return 0


def logout(name: str, say: Callable[[str], None] = print) -> int:
    try:
        store = TokenStore(name)
    except ValueError as exc:
        say(f"{name}: {exc}")
        return 2
    if store.delete():
        say(f"Logged out of {name}: its tokens are deleted. (The server may still list Strawberry as an app.)")
    else:
        say(f"{name} had no saved login.")
    return 0
