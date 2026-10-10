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
the client makes (the MCP session, discovery, registration, the token exchange and every refresh) goes
through `GuardedTransport` (https, or http on this machine; no login part; checked on every redirect
hop, and the SDK follows a redirect only within one origin), and every connection through
`PinnedBackend`, which resolves the name once, refuses it unless every address is public (loopback only,
and only loopback, for a server whose own url is on this machine), and connects to an address it
checked: a name that answers differently a moment later (DNS rebinding) is never looked up again. So the
server's metadata cannot point her at 127.0.0.1, the LAN or a cloud metadata address. A server on the LAN
is not reachable this way. The sign-in page itself is opened in the user's browser, not fetched here, and
must be https too (or http on this machine).
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

import httpcore2  # httpx2's connections: PinnedBackend makes them
import httpx2   # the MCP SDK's HTTP client (a dependency of `mcp`)

from . import paths

log = logging.getLogger("strawberryd.remote")
# Both log a request line or a session id at INFO; neither is anything the journal needs.
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("mcp.client.streamable_http").setLevel(logging.WARNING)

LOGIN_TIMEOUT_S = 300.0     # how long `strawberry tools login` waits for the browser
REFRESH_EARLY_S = 60.0      # refresh this long before the access token expires
CONNECT_S = 5.0             # the HTTP connect timeout (Server's connect_timeout_s bounds the whole connect)
ADDRESS_S = 1.5             # the least one address of several gets before the next is tried
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


class BlockedConnect(BlockedAddress, OSError):
    """BlockedAddress raised inside a connection (PinnedBackend): an OSError too, so httpcore2 wraps it as a
    ConnectError like any refused connection, and the message still names the host and why."""


async def resolve(host: str, port: int) -> list[str]:
    """The addresses a name resolves to, from the event loop's resolver (a thread), within CONNECT_S. Tests
    replace it."""
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), CONNECT_S)
    return [str(info[4][0]) for info in infos]


class PinnedBackend(httpcore2.AsyncNetworkBackend):
    """The one place a connection is made: the name is resolved here, once, every address checked, and the
    TCP connection goes to one of the checked addresses. The request keeps the name, so TLS still sends it
    as SNI and checks the certificate against it, and Host still carries it: only the connect is pinned. An
    answer that changes after the check (DNS rebinding) is never used, because nothing resolves the name
    again. Allowed: public addresses only; loopback ones only for a server whose own url is on this machine
    (a local server, or the tests'), and then only loopback."""

    def __init__(self, home: str, inner: Any = None) -> None:
        self.home = (home or "").strip("[]").lower()
        self.inner = inner or httpcore2.AnyIOBackend()

    async def vetted(self, host: str, port: int) -> list[str]:
        from .adapters.web import public_address   # local: the adapters import the core

        host = (host or "").strip("[]").lower()
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            try:
                addresses = await resolve(host, port)
            except (OSError, asyncio.TimeoutError, UnicodeError, ValueError):
                raise BlockedConnect(f"{host}: its name could not be looked up") from None
        addresses = list(dict.fromkeys(a.split("%", 1)[0] for a in addresses))
        if not addresses:
            raise BlockedConnect(f"{host}: its name could not be looked up")
        local = is_loopback(self.home) and (is_loopback(host) or host == self.home)
        if local:
            if not all(is_loopback(a) for a in addresses):
                raise BlockedConnect(f"{host}: an address off this machine for a server on it")
        elif not all(public_address(a) for a in addresses):
            raise BlockedConnect(f"{host}: {'a private address' if len(addresses) == 1 else 'it points to a private address'}")
        return addresses

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                          socket_options: Any = None) -> Any:
        """Each checked address in turn, IPv4 first: a network with an IPv6 route that goes nowhere would
        otherwise spend the whole timeout on the first AAAA answer. Each try gets a share of the timeout, at
        least ADDRESS_S, so a dead address cannot use it all."""
        addresses = sorted(await self.vetted(host, port), key=lambda a: ipaddress.ip_address(a).version)
        failure: Exception | None = None
        for i, address in enumerate(addresses):
            share = None if timeout is None else max(ADDRESS_S, timeout / (len(addresses) - i)) if i < len(addresses) - 1 else timeout
            try:
                return await self.inner.connect_tcp(address, port, timeout=share, local_address=local_address,
                                                    socket_options=socket_options)
            except (httpcore2.ConnectError, httpcore2.ConnectTimeout, OSError) as exc:
                failure = exc
        raise failure if failure is not None else BlockedConnect(f"{host}: no address")

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: Any = None) -> Any:
        raise BlockedConnect("a unix socket: never used here")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


class GuardedTransport(httpx2.AsyncBaseTransport):
    """Every request of one server's HTTP client: http(s) only, plain http only on this machine, no login part
    in the address (checked here, per request and per redirect hop), and every connection made by
    PinnedBackend (checked there, on the address actually connected to). No proxy from the environment: the
    connection goes where it was checked. Raises BlockedAddress naming the host, never the path or query."""

    def __init__(self, home: str, backend: Any = None) -> None:
        self.home = (urlsplit(home).hostname or "").lower()
        self.backend = backend or PinnedBackend(self.home)
        self.inner = httpx2.AsyncHTTPTransport(trust_env=False)
        # The transport's own pool, rebuilt on the pinned backend (httpx2 takes no backend argument).
        self.inner._pool = httpcore2.AsyncConnectionPool(
            ssl_context=httpx2_ssl_context(), max_connections=20, max_keepalive_connections=10, keepalive_expiry=5.0,
            http1=True, http2=False, network_backend=self.backend)

    @staticmethod
    def problem(scheme: str, host: str, userinfo: bytes | str) -> str | None:
        if userinfo:
            return "a login part in the address"
        if scheme not in ("http", "https"):
            return "not http(s)"
        if scheme == "http" and not is_loopback(host):
            return "plain http off this machine"
        return None

    async def handle_async_request(self, request: Any) -> Any:
        url = request.url
        why = self.problem(url.scheme, url.host, url.userinfo)
        if why:
            raise BlockedAddress(f"{url.host}: {why}")
        try:
            return await self.inner.handle_async_request(request)
        except httpx2.ConnectError as exc:
            blocked = _blocked_cause(exc)
            if blocked is not None:
                raise BlockedAddress(str(blocked)) from None
            raise

    async def aclose(self) -> None:
        await self.inner.aclose()


def _blocked_cause(exc: BaseException) -> BaseException | None:
    seen = 0
    while exc is not None and seen < 8:
        if isinstance(exc, BlockedAddress):
            return exc
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return None


def httpx2_ssl_context() -> Any:
    """The system's trust store, as httpx2 makes it, without the environment's certificate overrides."""
    from httpx2._config import create_ssl_context

    return create_ssl_context(verify=True, trust_env=False)


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
