"""A fake recall: a notes MCP server over streamable HTTP (JSON answers, no session) behind a fake OAuth
authorization server (protected-resource and authorization-server metadata, dynamic client registration,
an authorization endpoint that approves at once, a token endpoint with PKCE and refresh tokens), all on
one loopback port. For tests/test_adapter_recall.py, and by hand for an end-to-end run:

    uv run python -m tests.fake_recall --port 8799

Its notes are made up. Nothing here reaches any real service.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from aiohttp import web

NOTES = [
    {"id": "n-orbs-1", "title": "Orbs: decisions", "project_id": "p-straw", "project_name": "Strawberry",
     "updated_at": "2026-10-07T18:20:00Z", "status": "current",
     "body": "---\ntype: decision\ntags: [orbs]\n---\n# Orbs: decisions\n\n## Decided\n\n"
             "- The orbs stay pure visuals: no buttons, no menus, no approvals on them.\n"
             "- Approvals, menus and stop live in the crab widget.\n"
             "- The Brain UI is only for what happens under the hood.\n\n## Why\n\n"
             "Two places for one approval confused everyone in the trial week.\n"},
    {"id": "n-orbs-2", "title": "Orbs: colour ideas", "project_id": "p-straw", "project_name": "Strawberry",
     "updated_at": "2026-10-02T09:00:00Z", "status": "draft",
     "body": "# Orbs: colour ideas\n\nAmber for thinking, teal for listening. Not decided yet.\n"},
    {"id": "n-orbs-secret", "title": "Orbs budget (other team)", "project_id": "p-other", "project_name": "Finance",
     "updated_at": "2026-10-05T10:00:00Z", "status": "current",
     "body": "# Orbs budget\n\nThe orbs budget is 4000 euros. SECRET-FINANCE-CANARY\n"},
    {"id": "n-garden", "title": "Garden plan", "project_id": "p-home", "project_name": "Home",
     "updated_at": "2026-09-30T08:00:00Z", "status": "current",
     "body": "# Garden\n\nTomatoes on the left, beans on the right.\n"},
]

TOOLS = [
    {"name": "search", "description": "Search notes by meaning and keywords. " * 6,
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"},
                                                      "project_id": {"type": "string"}}, "required": ["query"]}},
    {"name": "read_note", "description": "Read a note by id.",
     "inputSchema": {"type": "object", "properties": {"note_id": {"type": "string"}}, "required": ["note_id"]}},
    {"name": "create_note", "description": "Create a note.",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}}}},
    {"name": "update_note", "description": "Update a note.",
     "inputSchema": {"type": "object", "properties": {"note_id": {"type": "string"}, "body": {"type": "string"}}}},
    {"name": "delete", "description": "Delete a note.", "annotations": {"destructiveHint": True},
     "inputSchema": {"type": "object", "properties": {"note_id": {"type": "string"}}}},
    {"name": "move", "description": "Move a note.",
     "inputSchema": {"type": "object", "properties": {"note_id": {"type": "string"}, "project_id": {"type": "string"}}}},
]


def challenge_of(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class FakeRecall:
    def __init__(self, notes: list[dict[str, Any]] | None = None, auth: bool = True, access_ttl: int = 3600,
                 refresh: bool = True) -> None:
        self.notes = [dict(n) for n in (notes if notes is not None else NOTES)]
        self.auth = auth
        self.access_ttl = access_ttl
        self.refresh = refresh
        self.base = ""
        self.clients: dict[str, dict[str, Any]] = {}
        self.codes: dict[str, dict[str, Any]] = {}
        self.access: dict[str, float] = {}       # token -> expiry
        self.refresh_tokens: dict[str, str] = {}  # refresh token -> client id
        self.tamper_state = False                 # /authorize answers with another state
        self.hostile_metadata = ""                # an authorization server address to put in the metadata
        self.calls: list[tuple[str, dict]] = []   # every tools/call (name, arguments)
        self.token_requests: list[dict[str, str]] = []
        self.registrations = 0
        self.unauthorized = 0
        self.runner: web.AppRunner | None = None

    # ------------------------------------------------------------------ the app

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/.well-known/oauth-protected-resource", self.prm)
        app.router.add_get("/.well-known/oauth-protected-resource/mcp", self.prm)
        app.router.add_get("/.well-known/oauth-authorization-server", self.asm)
        app.router.add_get("/.well-known/oauth-authorization-server/mcp", self.asm)
        app.router.add_post("/register", self.register)
        app.router.add_get("/authorize", self.authorize)
        app.router.add_post("/token", self.token)
        app.router.add_post("/mcp", self.mcp)
        app.router.add_get("/mcp", self.no_stream)
        app.router.add_delete("/mcp", self.no_stream)
        return app

    async def start(self, port: int = 0) -> str:
        self.runner = web.AppRunner(self.app(), access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", port)
        await site.start()
        bound = site._server.sockets[0].getsockname()[1]   # type: ignore[union-attr]
        self.base = f"http://127.0.0.1:{bound}"
        return self.base + "/mcp"

    async def close(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()

    # ------------------------------------------------------------------ OAuth

    async def prm(self, request: web.Request) -> web.Response:
        return web.json_response({"resource": self.base + "/mcp",
                                  "authorization_servers": [self.hostile_metadata or self.base]})

    async def asm(self, request: web.Request) -> web.Response:
        base = self.base
        return web.json_response({
            "issuer": base, "authorization_endpoint": base + "/authorize", "token_endpoint": base + "/token",
            "registration_endpoint": base + "/register", "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"]})

    async def register(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.registrations += 1
        client_id = "client-" + secrets.token_hex(6)
        self.clients[client_id] = body
        return web.json_response({"client_id": client_id, "client_id_issued_at": int(time.time()),
                                  "redirect_uris": body.get("redirect_uris"), "token_endpoint_auth_method": "none",
                                  "grant_types": body.get("grant_types"), "response_types": ["code"],
                                  "client_name": body.get("client_name")}, status=201)

    async def authorize(self, request: web.Request) -> web.Response:
        q = request.query
        client = self.clients.get(q.get("client_id", ""))
        if client is None or q.get("redirect_uri") not in (client.get("redirect_uris") or []):
            return web.Response(status=400, text="unknown client or redirect")
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            return web.Response(status=400, text="PKCE required")
        code = "code-" + secrets.token_hex(8)
        self.codes[code] = {"client_id": q["client_id"], "challenge": q["code_challenge"],
                            "redirect_uri": q["redirect_uri"]}
        state = "not-the-state" if self.tamper_state else q.get("state", "")
        raise web.HTTPFound(f"{q['redirect_uri']}?{urlencode({'code': code, 'state': state})}")

    def _issue(self, client_id: str) -> dict[str, Any]:
        access = "at-" + secrets.token_hex(12)
        self.access[access] = time.time() + self.access_ttl
        out: dict[str, Any] = {"access_token": access, "token_type": "Bearer", "expires_in": self.access_ttl}
        if self.refresh:
            refresh = "rt-" + secrets.token_hex(12)
            self.refresh_tokens[refresh] = client_id
            out["refresh_token"] = refresh
        return out

    async def token(self, request: web.Request) -> web.Response:
        form = dict(await request.post())
        self.token_requests.append({k: str(v) for k, v in form.items()})
        if form.get("grant_type") == "authorization_code":
            entry = self.codes.pop(str(form.get("code", "")), None)
            if (entry is None or entry["client_id"] != form.get("client_id")
                    or entry["redirect_uri"] != form.get("redirect_uri")
                    or challenge_of(str(form.get("code_verifier", ""))) != entry["challenge"]):
                return web.json_response({"error": "invalid_grant"}, status=400)
            return web.json_response(self._issue(entry["client_id"]))
        if form.get("grant_type") == "refresh_token":
            client = self.refresh_tokens.pop(str(form.get("refresh_token", "")), None)   # rotated: used once
            if client is None or client != form.get("client_id"):
                return web.json_response({"error": "invalid_grant"}, status=400)
            return web.json_response(self._issue(client))
        return web.json_response({"error": "unsupported_grant_type"}, status=400)

    def revoke_all(self) -> None:
        self.access.clear()
        self.refresh_tokens.clear()

    def expire_access(self) -> None:
        for token in self.access:
            self.access[token] = 0.0

    # ------------------------------------------------------------------ MCP

    async def no_stream(self, request: web.Request) -> web.Response:
        return web.Response(status=405)

    def _authorized(self, request: web.Request) -> bool:
        if not self.auth:
            return True
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        return token in self.access and self.access[token] > time.time()

    async def mcp(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            self.unauthorized += 1
            return web.json_response(
                {"error": "invalid_token"}, status=401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{self.base}/.well-known/oauth-protected-resource/mcp"'})
        message = await request.json()
        method, params, ident = message.get("method", ""), message.get("params") or {}, message.get("id")
        if ident is None:
            return web.Response(status=202)
        if method == "initialize":
            result: Any = {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                           "serverInfo": {"name": "fake-recall", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = self.call(params.get("name", ""), params.get("arguments") or {})
        elif method == "ping":
            result = {}
        else:
            return web.json_response({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "no such method"}})
        return web.json_response({"jsonrpc": "2.0", "id": ident, "result": result})

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, dict(arguments)))

        def text(value: Any, error: bool = False) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": json.dumps(value)}], "isError": error}

        if name == "search":
            words = [w for w in str(arguments.get("query", "")).lower().split() if len(w) > 2]
            hits = [n for n in self.notes
                    if any(w.rstrip("s") in (n["title"] + " " + n["body"]).lower() for w in words)]
            limit = int(arguments.get("limit") or 10)
            return text({"results": [{"id": n["id"], "title": n["title"], "project_id": n.get("project_id"),
                                      "project_name": n.get("project_name"), "snippet": n["body"][:120],
                                      "status": n.get("status"), "updated_at": n.get("updated_at"),
                                      "url": f"https://notes.example/{n['id']}"} for n in hits[:limit]]})
        if name == "read_note":
            for note in self.notes:
                if note["id"] == arguments.get("note_id"):
                    return text({k: v for k, v in note.items()})
            return text({"error": "Note not found"}, error=True)
        return text({"ok": True, "did": name})   # a write: the tests assert it is never reached


async def serve(port: int) -> None:
    fake = FakeRecall()
    url = await fake.start(port)
    print(f"fake recall at {url}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8799)
    asyncio.run(serve(parser.parse_args().port))
