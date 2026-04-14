"""
Lightweight GitHub API client supporting both REST v3 and GraphQL v4.

Reads GITHUB_PERSONAL_ACCESS_TOKEN (matching the standard GitHub MCP)
or falls back to GITHUB_TOKEN.
"""

from __future__ import annotations

import os
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

T = TypeVar("T", bound=BaseModel)


class GitHubAPI(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    token: str = Field(default="")
    base_url: str = Field(default="")
    _client: httpx.AsyncClient | None = None

    @model_validator(mode="before")
    @classmethod
    def _resolve_env(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get("token"):
                data["token"] = (
                    os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
                    or os.environ.get("GITHUB_TOKEN")
                    or ""
                )
            if not data.get("base_url"):
                data["base_url"] = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        return data

    @model_validator(mode="after")
    def _validate_token(self) -> GitHubAPI:
        if not self.token:
            raise ValueError(
                "Missing GitHub token. Set GITHUB_PERSONAL_ACCESS_TOKEN or GITHUB_TOKEN."
            )
        return self

    def model_post_init(self, __context: Any) -> None:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    @property
    def client(self) -> httpx.AsyncClient:
        assert self._client is not None
        return self._client

    # ── REST (untyped) ──────────────────────────────────────────────

    async def rest_raw(
        self,
        method: str,
        path: str,
        json: Any = None,
    ) -> Any:
        resp = await self.client.request(method, path, json=json)
        resp.raise_for_status()
        if resp.status_code == 204:
            return None
        return resp.json()

    # ── REST (typed) ────────────────────────────────────────────────

    async def rest(
        self,
        model: type[T],
        method: str,
        path: str,
        json: Any = None,
    ) -> T:
        data = await self.rest_raw(method, path, json=json)
        return model.model_validate(data)

    # ── GraphQL (untyped) ───────────────────────────────────────────

    async def graphql_raw(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> Any:
        resp = await self.client.post(
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

    # ── GraphQL (typed) ─────────────────────────────────────────────

    async def graphql(
        self,
        model: type[T],
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> T:
        data = await self.graphql_raw(query, variables)
        return model.model_validate(data)

    async def close(self) -> None:
        await self.client.aclose()
