"""A deliberately small OAuth 2.1 authorization server for a single owner.

Claude connects to remote MCP servers with OAuth: it registers itself (DCR),
sends the owner's browser to /authorize, and exchanges the code (PKCE) for
tokens. Here "signing in" means typing MCP_ADMIN_PASSWORD on our own page.

Only token hashes are stored on disk. Codes and pending logins live in memory.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

SCOPE = "mcp"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86_400
CODE_TTL = 300
LOGIN_TTL = 600
MAX_FAILURES = 5
LOCKOUT = 900


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def redirect_allowed(uri: str) -> bool:
    """Claude's hosted callback, or a loopback redirect (Claude Code), nothing else."""
    if uri == CLAUDE_CALLBACK:
        return True
    p = urlparse(uri)
    return p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1")


class OwnerOAuthProvider:
    def __init__(self, data_dir: Path, issuer_url: str, password: str, static_token: str | None):
        self.path = data_dir / "oauth.json"
        self.issuer_url = issuer_url
        self._password = password
        self._static = static_token
        self._pending: dict[str, tuple[str, AuthorizationParams, float]] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._failures: list[float] = []
        self._db = self._load()

    # ---------- persistence ----------

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"clients": {}, "access": {}, "refresh": {}}

    def _save(self) -> None:
        now = time.time()
        for kind in ("access", "refresh"):
            self._db[kind] = {
                k: v for k, v in self._db[kind].items() if (v.get("expires_at") or now + 1) > now
            }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._db, f)
        os.replace(tmp, self.path)

    # ---------- clients (DCR) ----------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self._db["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not uris or not all(redirect_allowed(u) for u in uris):
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="Only Claude's callback and loopback redirects are accepted",
            )
        self._db["clients"][client_info.client_id] = client_info.model_dump(mode="json")
        self._save()

    # ---------- authorization ----------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        txn = secrets.token_urlsafe(32)
        self._pending[txn] = (client.client_id, params, time.time() + LOGIN_TTL)
        return f"{self.issuer_url}/login?txn={txn}"

    def _locked(self) -> bool:
        cutoff = time.time() - LOCKOUT
        self._failures = [t for t in self._failures if t > cutoff]
        return len(self._failures) >= MAX_FAILURES

    async def login_page(self, request: Request) -> Response:
        headers = {"X-Frame-Options": "DENY", "Cache-Control": "no-store"}
        if request.method == "GET":
            txn = request.query_params.get("txn", "")
            return self._render(txn, error=None, headers=headers)

        form = await request.form()
        txn = str(form.get("txn", ""))
        pending = self._pending.get(txn)
        if not pending or pending[2] < time.time():
            self._pending.pop(txn, None)
            return HTMLResponse(_page("Lien expiré", "<p>Relance la connexion depuis Claude.</p>"),
                                status_code=400, headers=headers)
        if self._locked():
            return self._render(txn, "Trop d'essais. Réessaie dans 15 minutes.", headers, 429)
        if not hmac.compare_digest(str(form.get("password", "")).encode(), self._password.encode()):
            self._failures.append(time.time())
            return self._render(txn, "Mot de passe incorrect.", headers, 401)

        client_id, params, _ = self._pending.pop(txn)
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [SCOPE],
            expires_at=time.time() + CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject="owner",
        )
        target = construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)
        return RedirectResponse(target, status_code=302, headers=headers)

    def _render(self, txn: str, error: str | None, headers: dict, status: int = 200) -> Response:
        pending = self._pending.get(txn)
        if not pending or pending[2] < time.time():
            return HTMLResponse(_page("Lien expiré", "<p>Relance la connexion depuis Claude.</p>"),
                                status_code=400, headers=headers)
        dest = html.escape(urlparse(str(pending[1].redirect_uri)).hostname or "?")
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        body = f"""
<p>Une application demande l'accès à ton compte Instagram via <b>insta-mcp</b>.</p>
<p>Elle sera renvoyée vers : <b>{dest}</b></p>
{err}
<form method="post" action="/login">
  <input type="hidden" name="txn" value="{html.escape(txn)}">
  <label for="pw">Mot de passe administrateur</label>
  <input id="pw" type="password" name="password" autocomplete="current-password" required autofocus>
  <button type="submit">Autoriser</button>
</form>"""
        page = _page("Connexion à insta-mcp", body)
        return HTMLResponse(page, status_code=status, headers=headers)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if not code or code.client_id != client.client_id or code.expires_at < time.time():
            return None
        return code

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        now = int(time.time())
        rec = {"client_id": client_id, "scopes": scopes, "resource": resource}
        self._db["access"][_h(access)] = {**rec, "expires_at": now + ACCESS_TTL}
        self._db["refresh"][_h(refresh)] = {**rec, "expires_at": now + REFRESH_TTL}
        self._save()
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError(error="invalid_grant", error_description="code already used")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    # ---------- refresh ----------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        rec = self._db["refresh"].get(_h(refresh_token))
        if not rec or rec["client_id"] != client.client_id or rec["expires_at"] < time.time():
            return None
        return RefreshToken(token=refresh_token, **rec)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # Rotate: the old refresh token stops working as soon as a new one exists.
        if self._db["refresh"].pop(_h(refresh_token.token), None) is None:
            raise TokenError(error="invalid_grant", error_description="refresh token revoked")
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    # ---------- verification / revocation ----------

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self._static and hmac.compare_digest(token.encode(), self._static.encode()):
            return AccessToken(token=token, client_id="static", scopes=[SCOPE], subject="owner")
        rec = self._db["access"].get(_h(token))
        if not rec or rec["expires_at"] < time.time():
            return None
        return AccessToken(token=token, subject="owner", **rec)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._db["access"].pop(_h(token.token), None)
        self._db["refresh"].pop(_h(token.token), None)
        self._save()


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:26rem;margin:4rem auto;
  padding:0 1rem;color:#1d1f24}}
form{{display:flex;flex-direction:column;gap:.6rem;margin-top:1.2rem}}
input{{padding:.6rem;font-size:1rem}} button{{padding:.7rem;font-size:1rem;cursor:pointer}}
.err{{color:#b3261e;font-weight:600}}
</style></head><body><h1>{html.escape(title)}</h1>{body}</body></html>"""
