"""Instagram access token, kept on disk and refreshed before it expires.

Tokens from "API setup with Instagram login" live for 60 days and can be
refreshed once they are at least 24 hours old. We refresh them weekly, so a
server that runs at least once a week never lets the token lapse.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

DAY = 86_400
REFRESH_AFTER = 7 * DAY  # refresh weekly
MIN_AGE = DAY  # Meta refuses to refresh tokens younger than 24h
DEFAULT_LIFETIME = 60 * DAY


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


class TokenStore:
    def __init__(self, path: Path, initial_token: str | None, graph_host: str):
        self.path = path
        self.graph_host = graph_host
        self._lock = asyncio.Lock()
        self._state = self._load(initial_token)

    # ---------- persistence ----------

    def _load(self, initial_token: str | None) -> dict:
        state: dict | None = None
        if self.path.exists():
            state = json.loads(self.path.read_text(encoding="utf-8"))
        seed = _fingerprint(initial_token) if initial_token else None
        # A new IG_ACCESS_TOKEN in the environment replaces whatever is on disk.
        if initial_token and (state is None or state.get("seed") != seed):
            now = int(time.time())
            state = {
                "access_token": initial_token,
                "obtained_at": now,
                "expires_at": now + DEFAULT_LIFETIME,
                "seed": seed,
            }
            self._save(state)
        if state is None:
            raise RuntimeError("No Instagram token: set IG_ACCESS_TOKEN for the first start")
        return state

    def _save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, self.path)

    # ---------- public ----------

    @property
    def token(self) -> str:
        return self._state["access_token"]

    def status(self) -> dict:
        now = int(time.time())
        return {
            "expires_in_days": round((self._state["expires_at"] - now) / DAY, 1),
            "last_refresh_days_ago": round((now - self._state["obtained_at"]) / DAY, 1),
        }

    async def ensure_fresh(self, client: httpx.AsyncClient) -> None:
        age = time.time() - self._state["obtained_at"]
        if age < max(REFRESH_AFTER, MIN_AGE):
            return
        async with self._lock:
            if time.time() - self._state["obtained_at"] < REFRESH_AFTER:
                return  # another task refreshed while we waited
            await self._refresh(client)

    async def _refresh(self, client: httpx.AsyncClient) -> None:
        r = await client.get(
            f"https://{self.graph_host}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": self.token},
        )
        if r.status_code >= 400:
            # Keep the current token; it is still valid for weeks. Log without the URL.
            log.error("Instagram token refresh failed (HTTP %s)", r.status_code)
            return
        body = r.json()
        now = int(time.time())
        self._state = {
            **self._state,
            "access_token": body["access_token"],
            "obtained_at": now,
            "expires_at": now + int(body.get("expires_in", DEFAULT_LIFETIME)),
        }
        self._save(self._state)
        log.info("Instagram token refreshed, valid for %s days", self.status()["expires_in_days"])
