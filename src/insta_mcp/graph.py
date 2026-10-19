"""Thin async client for the Instagram Graph API (Instagram Login flavour)."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from .token_store import TokenStore

# httpx logs full request URLs at INFO, and the token travels in the query string.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class GraphError(RuntimeError):
    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        super().__init__(f"Graph API {status}")

    def to_dict(self) -> dict[str, Any]:
        err = self.payload.get("error", {}) if isinstance(self.payload, dict) else {}
        return {
            "error": True,
            "status": self.status,
            "message": err.get("message") or str(self.payload)[:500],
            "code": err.get("code"),
            "subcode": err.get("error_subcode"),
        }


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


class Graph:
    def __init__(self, tokens: TokenStore, host: str, version: str, client: httpx.AsyncClient):
        self.tokens = tokens
        self.host = host
        self.base = f"https://{host}/{version}"
        self.http = client

    async def _token(self) -> str:
        await self.tokens.ensure_fresh(self.http)
        return self.tokens.token

    @staticmethod
    def _unwrap(r: httpx.Response) -> Any:
        try:
            body = r.json()
        except ValueError:
            body = r.text
        if r.status_code >= 400:
            raise GraphError(r.status_code, body)
        return body

    async def get(self, path: str, **params: Any) -> Any:
        q = _clean(params)
        q["access_token"] = await self._token()
        return self._unwrap(await self.http.get(f"{self.base}/{path.lstrip('/')}", params=q))

    async def post(self, path: str, **fields: Any) -> Any:
        token = await self._token()
        return self._unwrap(
            await self.http.post(
                f"{self.base}/{path.lstrip('/')}",
                params={"access_token": token},
                data=_clean(fields),
            )
        )

    async def post_json(self, path: str, payload: dict[str, Any]) -> Any:
        token = await self._token()
        return self._unwrap(
            await self.http.post(
                f"{self.base}/{path.lstrip('/')}",
                params={"access_token": token},
                json=payload,
            )
        )

    async def paginate(self, path: str, max_pages: int = 3, **params: Any) -> list[Any]:
        """Follow paging.next, but only on the Graph host we were configured for."""
        res = await self.get(path, **params)
        results: list[Any] = list(res.get("data", []))
        pages = 1
        while pages < max_pages:
            nxt = res.get("paging", {}).get("next")
            if not nxt or urlparse(nxt).hostname != self.host:
                break
            res = self._unwrap(await self.http.get(nxt))
            results.extend(res.get("data", []))
            pages += 1
        return results
