"""
Lightweight GitHub API client supporting both REST v3 and GraphQL v4.

Reads GITHUB_PERSONAL_ACCESS_TOKEN (matching the standard GitHub MCP)
or falls back to GITHUB_TOKEN.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class GitHubAPI:
    def __init__(self) -> None:
        token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN") or os.environ.get(
            "GITHUB_TOKEN"
        )
        if not token:
            raise RuntimeError(
                "Missing GitHub token. Set GITHUB_PERSONAL_ACCESS_TOKEN or GITHUB_TOKEN."
            )

        base_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    # ── REST ────────────────────────────────────────────────────────

    async def rest(
        self,
        method: str,
        path: str,
        json: Any = None,
    ) -> Any:
        resp = await self._client.request(method, path, json=json)
        resp.raise_for_status()
        if resp.status_code == 204:
            return None
        return resp.json()

    # ── GraphQL ─────────────────────────────────────────────────────

    async def graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> Any:
        resp = await self._client.post(
            "/graphql",
            json={"query": query, "variables": variables or {}},
        )
        resp.raise_for_status()
        body = resp.json()
        if errors := body.get("errors"):
            msgs = "; ".join(e["message"] for e in errors)
            raise RuntimeError(f"GitHub GraphQL error: {msgs}")
        if body.get("data") is None:
            raise RuntimeError("GitHub GraphQL: empty response (no data)")
        return body["data"]

    async def close(self) -> None:
        await self._client.aclose()
