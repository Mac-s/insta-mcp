import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from insta_mcp.config import Settings
from insta_mcp.oauth import redirect_allowed
from insta_mcp.server import build_server, parse_time

PUBLIC = "https://insta.example.com"
PASSWORD = "correct-horse-battery-staple"
G = "https://graph.instagram.com/v21.0"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        public_url=PUBLIC,
        admin_password=PASSWORD,
        static_token=None,
        ig_initial_token="IGTEST",
        graph_host="graph.instagram.com",
        graph_version="v21.0",
        data_dir=tmp_path,
        host="127.0.0.1",
        port=8000,
    )


def _out(res):
    """FastMCP returns (content, structured); lists are wrapped under "result"."""
    if isinstance(res, tuple):
        structured = res[1]
        return structured.get("result", structured)
    return res


def test_redirect_allowlist():
    assert redirect_allowed("https://claude.ai/api/mcp/auth_callback")
    assert redirect_allowed("http://localhost:3118/callback")
    assert redirect_allowed("http://127.0.0.1:9999/callback")
    assert not redirect_allowed("https://evil.example.com/callback")
    assert not redirect_allowed("http://claude.ai/api/mcp/auth_callback")


def test_parse_time_formats():
    a = parse_time("2026-10-19T08:39:51+0000")
    b = parse_time("2026-10-19T08:39:51Z")
    assert a == b


async def _oauth_token(
    app_client: httpx.AsyncClient, password: str = PASSWORD
) -> tuple[httpx.Response, str]:
    reg = await app_client.post("/register", json={
        "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": "Claude",
    })
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    auth = await app_client.get("/authorize", params={
        "response_type": "code", "client_id": client_id,
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "code_challenge": challenge.decode(), "code_challenge_method": "S256",
        "state": "xyz", "scope": "mcp",
    })
    assert auth.status_code == 302, auth.text
    login_url = auth.headers["location"]
    assert login_url.startswith(f"{PUBLIC}/login?txn=")
    txn = parse_qs(urlparse(login_url).query)["txn"][0]

    page = await app_client.get(f"/login?txn={txn}")
    assert page.status_code == 200 and "claude.ai" in page.text

    done = await app_client.post("/login", data={"txn": txn, "password": password})
    if done.status_code != 302:
        return done, client_id
    redirect = urlparse(done.headers["location"])
    q = parse_qs(redirect.query)
    assert q["state"] == ["xyz"]
    tok = await app_client.post("/token", data={
        "grant_type": "authorization_code", "code": q["code"][0],
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "client_id": client_id, "code_verifier": verifier,
    })
    return tok, client_id


async def test_mcp_requires_auth_and_oauth_flow_works(settings):
    app = build_server(settings).streamable_http_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC) as c:
            r = await c.post("/mcp", json={})
            assert r.status_code == 401
            assert "resource_metadata" in r.headers.get("www-authenticate", "")

            meta = await c.get("/.well-known/oauth-protected-resource/mcp")
            assert meta.status_code == 200, meta.text
            assert meta.json()["resource"].rstrip("/") == f"{PUBLIC}/mcp"

            tok, client_id = await _oauth_token(c)
            assert tok.status_code == 200, tok.text
            access = tok.json()["access_token"]
            refresh = tok.json()["refresh_token"]

            init = await c.post("/mcp", headers={
                "Authorization": f"Bearer {access}",
                "Accept": "application/json, text/event-stream",
            }, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}},
            })
            assert init.status_code == 200, init.text

            # Refresh tokens rotate: the old one is refused after use.
            body = {"grant_type": "refresh_token", "refresh_token": refresh,
                    "client_id": client_id}
            first = await c.post("/token", data=body)
            assert first.status_code == 200, first.text
            second = await c.post("/token", data=body)
            assert second.status_code == 400


async def test_wrong_password_is_refused(settings):
    app = build_server(settings).streamable_http_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC) as c:
            r, _ = await _oauth_token(c, password="nope")
            assert r.status_code == 401


async def test_foreign_redirect_registration_refused(settings):
    app = build_server(settings).streamable_http_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC) as c:
            reg = await c.post("/register", json={
                "redirect_uris": ["https://evil.example.com/cb"],
                "token_endpoint_auth_method": "none",
            })
            assert reg.status_code == 400


@respx.mock
async def test_list_new_messages_filters_own_and_old(settings):
    respx.get(f"{G}/me").mock(return_value=httpx.Response(200, json={
        "user_id": "17840000000000001", "username": "my_shop", "id": "999"}))
    respx.get(f"{G}/me/conversations").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "CONV1", "updated_time": "2026-10-19T09:00:00+0000", "participants": {"data": [
            {"username": "my_shop", "id": "17840000000000001"},
            {"username": "a_customer", "id": "1000000000000002"}]}},
        {"id": "CONV2", "updated_time": "2026-10-01T09:00:00+0000", "participants": {"data": []}},
    ]}))
    respx.get(f"{G}/CONV1").mock(return_value=httpx.Response(200, json={"messages": {"data": [
        {"id": "m3", "created_time": "2026-10-19T09:00:00+0000", "message": "Merci !",
         "from": {"id": "17840000000000001", "username": "my_shop"}},
        {"id": "m2", "created_time": "2026-10-19T08:30:00+0000", "message": "Tu vois ce message ?",
         "from": {"id": "1000000000000002", "username": "a_customer"}},
        {"id": "m1", "created_time": "2026-10-10T08:30:00+0000", "message": "vieux",
         "from": {"id": "1000000000000002", "username": "a_customer"}},
    ]}}))

    mcp = build_server(settings)
    result = await mcp.call_tool("list_new_messages", {"since": "2026-10-18T00:00:00Z"})
    data = _out(result)
    assert data == [{
        "conversation_id": "CONV1", "from": "a_customer", "reply_to": "1000000000000002",
        "messages": [{"text": "Tu vois ce message ?", "created_time": "2026-10-19T08:30:00+0000"}],
    }]


@respx.mock
async def test_list_new_comments_skips_own_replies_without_username(settings):
    respx.get(f"{G}/me").mock(return_value=httpx.Response(200, json={
        "user_id": "17840000000000001", "username": "my_shop", "id": "999"}))
    respx.get(f"{G}/me/media").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "M1", "comments_count": 3, "permalink": "https://x/p/1", "caption": "c"}]}))
    respx.get(f"{G}/M1/comments").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "C1", "text": "Prix ?", "timestamp": "2026-10-19T09:00:00+0000",
         "username": "a_customer", "from": {"id": "1000000000000002", "username": "a_customer"},
         "replies": {"data": [
             {"id": "R1", "text": "Check tes DM !", "timestamp": "2026-10-19T09:00:05+0000",
              "user": {"id": "17840000000000001"}},
             {"id": "R2", "text": "Lien en DM", "timestamp": "2026-10-19T09:00:06+0000"},
             {"id": "R3", "text": "Merci !", "timestamp": "2026-10-19T09:01:00+0000"}]}},
        # Graph also lists the account's own reply at the top level, with `from`.
        {"id": "R2", "text": "Lien en DM", "timestamp": "2026-10-19T09:00:06+0000",
         "username": "my_shop", "from": {"id": "17840000000000001", "username": "my_shop"}},
    ]}))
    data = _out(await build_server(settings).call_tool(
        "list_new_comments", {"since": "2026-10-18T00:00:00Z"}))
    assert [c["comment_id"] for c in data] == ["C1", "R3"]


@respx.mock
async def test_send_dm_rejects_overlong_and_posts_json(settings):
    route = respx.post(f"{G}/me/messages").mock(
        return_value=httpx.Response(200, json={"message_id": "x"}))
    mcp = build_server(settings)
    res = await mcp.call_tool("send_dm", {"recipient_id": "1", "text": "a" * 1001})
    out = _out(res)
    assert out["error"] is True and not route.called

    res = await mcp.call_tool("send_dm", {"recipient_id": "1", "text": "Salut"})
    out = _out(res)
    assert out == {"message_id": "x"}
    assert route.calls.last.request.url.params["access_token"] == "IGTEST"


def test_dangerous_tools_are_absent(settings):
    import asyncio
    names = {t.name for t in asyncio.run(build_server(settings).list_tools())}
    assert "send_dm" in names and "reply_to_comment" in names
    assert not {n for n in names if n.startswith(("publish", "delete"))}
