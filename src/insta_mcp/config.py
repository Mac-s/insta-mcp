"""Settings read from the environment.

Every secret can be given either directly (``NAME=value``) or as a file path
(``NAME_FILE=/run/secrets/name``), which is what Docker secrets provide.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _read(name: str, default: str | None = None) -> str | None:
    path = os.environ.get(f"{name}_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8").strip() or default
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _require(name: str) -> str:
    value = _read(name)
    if not value:
        raise RuntimeError(f"{name} (or {name}_FILE) must be set")
    return value


@dataclass(frozen=True)
class Settings:
    public_url: str
    admin_password: str
    static_token: str | None
    ig_initial_token: str | None
    graph_host: str
    graph_version: str
    data_dir: Path
    host: str
    port: int

    @property
    def mcp_url(self) -> str:
        return f"{self.public_url}/mcp"

    @property
    def public_host(self) -> str:
        return urlparse(self.public_url).netloc


def load_settings() -> Settings:
    public_url = _require("PUBLIC_URL").rstrip("/")
    if not public_url.startswith("https://") and not public_url.startswith("http://localhost"):
        raise RuntimeError("PUBLIC_URL must be an https:// URL")
    password = _require("MCP_ADMIN_PASSWORD")
    if len(password) < 16:
        raise RuntimeError("MCP_ADMIN_PASSWORD must be at least 16 characters")
    static = _read("MCP_STATIC_TOKEN")
    if static is not None and len(static) < 32:
        raise RuntimeError("MCP_STATIC_TOKEN must be at least 32 characters")
    return Settings(
        public_url=public_url,
        admin_password=password,
        static_token=static,
        ig_initial_token=_read("IG_ACCESS_TOKEN"),
        graph_host=_read("IG_GRAPH_HOST", "graph.instagram.com") or "graph.instagram.com",
        graph_version=_read("IG_GRAPH_VERSION", "v21.0") or "v21.0",
        data_dir=Path(_read("DATA_DIR", "/data") or "/data"),
        host=_read("HOST", "0.0.0.0") or "0.0.0.0",
        port=int(_read("PORT", "8000") or "8000"),
    )
