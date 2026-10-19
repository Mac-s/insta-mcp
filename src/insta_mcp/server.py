"""MCP tools for one Instagram professional account.

Scope is deliberately narrow: read and answer comments and DMs, read insights.
No publishing (Postiz does that) and no deleting.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

import httpx
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .config import Settings
from .graph import Graph, GraphError
from .oauth import SCOPE, OwnerOAuthProvider
from .token_store import TokenStore

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

MEDIA_FIELDS = "id,caption,media_type,permalink,timestamp,comments_count,like_count"
COMMENT_FIELDS = (
    "id,text,timestamp,username,from,user,like_count,hidden,"
    "replies{id,text,timestamp,username,from,user}"
)
MESSAGE_FIELDS = "id,created_time,from,to,message"
DM_MAX_CHARS = 1000


def parse_time(value: str) -> datetime:
    """Accept ISO 8601 ('2026-10-19T08:00:00Z') and Graph's '+0000' offsets."""
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        dt = datetime.strptime(v, "%Y-%m-%dT%H:%M:%S%z")
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def build_server(settings: Settings, http: httpx.AsyncClient | None = None) -> FastMCP:
    http = http or httpx.AsyncClient(timeout=30.0)
    tokens = TokenStore(settings.data_dir / "ig_token.json", settings.ig_initial_token,
                        settings.graph_host)
    graph = Graph(tokens, settings.graph_host, settings.graph_version, http)
    provider = OwnerOAuthProvider(settings.data_dir, settings.public_url,
                                  settings.admin_password, settings.static_token)

    mcp = FastMCP(
        "insta-mcp",
        instructions=(
            "Outils pour le compte Instagram professionnel du propriétaire : lire et répondre "
            "aux commentaires et messages privés, lire les statistiques. N'envoie une réponse "
            "qu'avec l'accord du propriétaire : validation au cas par cas, ou autorisation "
            "permanente qu'il a donnée pour un type de réponse."
        ),
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=settings.mcp_url,
            validate_token_resource=False,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[settings.public_host, "localhost:*", "127.0.0.1:*"],
            allowed_origins=[settings.public_url, "https://claude.ai"],
        ),
    )
    mcp.custom_route("/login", methods=["GET", "POST"], include_in_schema=False)(
        provider.login_page
    )

    me: dict[str, Any] = {}

    async def whoami() -> dict[str, Any]:
        if not me:
            me.update(await graph.get("me", fields="user_id,username,id"))
        return me

    def is_me(author: dict[str, Any] | None, username: str | None, ident: dict[str, Any]) -> bool:
        ids = {str(ident.get("user_id")), str(ident.get("id"))}
        if author and str(author.get("id")) in ids:
            return True
        return bool(username) and username == ident.get("username")

    def tool(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except GraphError as e:
                return e.to_dict()
            except (ValueError, TypeError) as e:
                return {"error": True, "message": str(e)}

        return mcp.tool()(wrapper)  # type: ignore[return-value]

    # ---------- account ----------

    @tool
    async def account_status() -> dict[str, Any]:
        """Profil du compte (nom, abonnés, nombre de posts) et état du jeton Instagram."""
        profile = await graph.get("me", fields="username,name,followers_count,media_count")
        return {"profile": profile, "instagram_token": tokens.status()}

    @tool
    async def list_recent_media(limit: int = 10) -> list[dict[str, Any]]:
        """Derniers posts et Reels, avec leur nombre de commentaires."""
        res = await graph.get("me/media", fields=MEDIA_FIELDS, limit=min(limit, 50))
        return res.get("data", [])

    # ---------- comments ----------

    @tool
    async def list_comments(media_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Commentaires d'un post, avec leurs réponses. Le nom de l'auteur peut être absent."""
        return await graph.paginate(
            f"{media_id}/comments", fields=COMMENT_FIELDS, limit=min(limit, 50)
        )

    @tool
    async def list_new_comments(since: str, media_limit: int = 10) -> list[dict[str, Any]]:
        """Commentaires et réponses reçus depuis `since` (ISO 8601) sur les `media_limit`
        derniers posts, hors ceux du compte lui-même. Le plus ancien en premier."""
        cutoff = parse_time(since)
        ident = await whoami()
        media = (await graph.get("me/media", fields=MEDIA_FIELDS, limit=min(media_limit, 50)))
        found: list[dict[str, Any]] = []
        for m in media.get("data", []):
            if not m.get("comments_count"):
                continue
            for c in await graph.paginate(f"{m['id']}/comments", fields=COMMENT_FIELDS, limit=50):
                thread = [c] + [{**r, "parent_id": c["id"]} for r in
                                (c.get("replies") or {}).get("data", [])]
                for item in thread:
                    if parse_time(item["timestamp"]) <= cutoff:
                        continue
                    # Graph only returns `user` when the comment was written by the
                    # account itself; `username`/`from` are often missing on our own
                    # replies (e.g. OpenReply's automatic ones).
                    if item.get("user") or is_me(item.get("from"), item.get("username"), ident):
                        continue
                    found.append({
                        "comment_id": item["id"],
                        "parent_id": item.get("parent_id"),
                        "text": item.get("text", ""),
                        "author": item.get("username") or (item.get("from") or {}).get("username"),
                        "timestamp": item["timestamp"],
                        "media_id": m["id"],
                        "media_permalink": m.get("permalink"),
                        "media_caption": (m.get("caption") or "")[:120],
                    })
        return sorted(found, key=lambda x: parse_time(x["timestamp"]))

    @tool
    async def reply_to_comment(comment_id: str, message: str) -> dict[str, Any]:
        """Répond publiquement à un commentaire. À n'appeler qu'avec l'accord du propriétaire (au cas par cas ou permanent)."""
        if not message.strip():
            raise ValueError("message vide")
        return await graph.post(f"{comment_id}/replies", message=message)

    @tool
    async def hide_comment(comment_id: str, hide: bool = True) -> dict[str, Any]:
        """Masque (ou ré-affiche) un commentaire, par exemple un spam. Réversible."""
        return await graph.post(comment_id, hide=str(hide).lower())

    # ---------- direct messages ----------

    @tool
    async def list_conversations(limit: int = 20) -> list[dict[str, Any]]:
        """Conversations privées, la plus récente en premier."""
        res = await graph.get(
            "me/conversations", platform="instagram",
            fields="id,updated_time,participants", limit=min(limit, 50),
        )
        return res.get("data", [])

    @tool
    async def get_conversation(conversation_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Messages d'une conversation, du plus récent au plus ancien."""
        res = await graph.get(
            conversation_id, fields=f"messages.limit({min(limit, 50)}){{{MESSAGE_FIELDS}}}"
        )
        return (res.get("messages") or {}).get("data", [])

    @tool
    async def list_new_messages(since: str, conversation_limit: int = 20) -> list[dict[str, Any]]:
        """Messages privés reçus depuis `since` (ISO 8601), regroupés par conversation.
        `reply_to` est l'identifiant à passer à send_dm. Les messages vides sont des pièces
        jointes ou des réactions."""
        cutoff = parse_time(since)
        ident = await whoami()
        convs = await graph.get(
            "me/conversations", platform="instagram",
            fields="id,updated_time,participants", limit=min(conversation_limit, 50),
        )
        out: list[dict[str, Any]] = []
        for conv in convs.get("data", []):
            if parse_time(conv["updated_time"]) <= cutoff:
                continue
            others = [p for p in (conv.get("participants") or {}).get("data", [])
                      if not is_me(p, p.get("username"), ident)]
            res = await graph.get(conv["id"], fields=f"messages.limit(25){{{MESSAGE_FIELDS}}}")
            new = [
                {"text": m.get("message") or "[pièce jointe ou réaction]",
                 "created_time": m["created_time"]}
                for m in (res.get("messages") or {}).get("data", [])
                if parse_time(m["created_time"]) > cutoff
                and not is_me(m.get("from"), (m.get("from") or {}).get("username"), ident)
            ]
            if new:
                out.append({
                    "conversation_id": conv["id"],
                    "from": others[0].get("username") if others else None,
                    "reply_to": others[0].get("id") if others else None,
                    "messages": sorted(new, key=lambda x: parse_time(x["created_time"])),
                })
        return out

    @tool
    async def send_dm(recipient_id: str, text: str) -> dict[str, Any]:
        """Envoie une réponse en message privé (fenêtre de 24 h après le dernier message de la
        personne). À n'appeler qu'avec l'accord du propriétaire (au cas par cas ou permanent)."""
        if not text.strip():
            raise ValueError("message vide")
        if len(text) > DM_MAX_CHARS:
            raise ValueError(f"message trop long ({len(text)} > {DM_MAX_CHARS} caractères)")
        return await graph.post_json(
            "me/messages", {"recipient": {"id": recipient_id}, "message": {"text": text}}
        )

    # ---------- insights ----------

    @tool
    async def get_account_insights(
        metrics: str = "reach,views,accounts_engaged,total_interactions",
        period: str = "day",
        metric_type: str | None = "total_value",
        since: int | None = None,
        until: int | None = None,
    ) -> dict[str, Any]:
        """Statistiques du compte. `since`/`until` en timestamps Unix."""
        return await graph.get(
            "me/insights", metric=metrics, period=period,
            metric_type=metric_type, since=since, until=until,
        )

    @tool
    async def get_media_insights(
        media_id: str, metrics: str = "reach,views,saved,shares,total_interactions"
    ) -> dict[str, Any]:
        """Statistiques d'un post ou d'un Reel (les métriques valides varient selon le type)."""
        return await graph.get(f"{media_id}/insights", metric=metrics)

    return mcp
