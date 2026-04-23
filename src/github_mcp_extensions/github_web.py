"""
Private github.com (not api.github.com) client for PR UI endpoints that
aren't exposed on the REST or GraphQL surface.

Currently only used by dismiss_code_quality_finding — the 'Dismiss finding'
button on github-code-quality[bot] review comments calls

    PUT https://github.com/{owner}/{repo}/pull/{pr}/automated_review_comments/{id}/dismiss

which is authenticated by the user's browser session cookie, not a PAT.

Credential source: GH_WEB_SESSION_COOKIE env var. Paste the full Cookie
header from an authenticated github.com tab (DevTools → Network → any
request → Request Headers → Cookie). It must contain at minimum
_gh_sess, user_session, and dotcom_user. Treat as a sensitive secret —
it expires and must be refreshed when it does.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx


_NONCE_RE = re.compile(
    r"v2:[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
)


class GitHubWebSessionError(RuntimeError):
    """Raised for any failure that should surface to the MCP caller without
    leaking cookie material in the message."""


def _scrub(text: str, cookie: str) -> str:
    """Remove the cookie value from an error string so it can be surfaced safely."""
    if not cookie:
        return text
    return text.replace(cookie, "<redacted>")


class GitHubWebSession:
    """httpx.AsyncClient wrapper for authenticated github.com (web) endpoints."""

    BASE_URL = "https://github.com"

    def __init__(self, cookie: str | None = None) -> None:
        cookie = cookie or os.environ.get("GH_WEB_SESSION_COOKIE", "")
        if not cookie:
            raise GitHubWebSessionError(
                "Missing GH_WEB_SESSION_COOKIE. This tool calls a private "
                "github.com endpoint that authenticates via browser session "
                "cookies — a PAT is not sufficient. Paste your github.com "
                "Cookie header (must include _gh_sess, user_session, "
                "dotcom_user) into the GH_WEB_SESSION_COOKIE env var. "
                "Treat it as sensitive; it expires."
            )
        self._cookie = cookie.strip()
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Cookie": self._cookie,
                "User-Agent": "github-mcp-extensions",
            },
            timeout=30.0,
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GitHubWebSession:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def fetch_pr_nonce(self, owner: str, repo: str, pull_number: int) -> str:
        """GET the PR page and extract the X-Fetch-Nonce embedded in the React payload."""
        path = f"/{owner}/{repo}/pull/{pull_number}"
        try:
            resp = await self._client.get(path, headers={"Accept": "text/html"})
        except httpx.HTTPError as e:
            raise GitHubWebSessionError(
                _scrub(f"Failed to fetch PR page to scrape nonce: {e}", self._cookie)
            ) from None

        if resp.status_code == 302 and "/login" in resp.headers.get("location", ""):
            raise GitHubWebSessionError(
                "Session cookie is expired or invalid — github.com redirected "
                "to /login. Refresh GH_WEB_SESSION_COOKIE from an authenticated "
                "browser tab."
            )
        if resp.status_code >= 400:
            raise GitHubWebSessionError(
                f"PR page fetch returned {resp.status_code}. "
                "The PR may not exist, or your session lacks access to this repo."
            )

        m = _NONCE_RE.search(resp.text)
        if not m:
            raise GitHubWebSessionError(
                "Could not locate X-Fetch-Nonce on the PR page. GitHub may "
                "have changed the page layout; this tool relies on a private "
                "endpoint and may need updating."
            )
        return m.group(0)

    async def dismiss_code_quality_finding(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        finding_id: int,
        reason: str,
        resolution_note: str = "",
        nonce: str | None = None,
    ) -> None:
        """PUT …/automated_review_comments/{id}/dismiss. Returns None on 204."""
        nonce = nonce or await self.fetch_pr_nonce(owner, repo, pull_number)

        path = (
            f"/{owner}/{repo}/pull/{pull_number}"
            f"/automated_review_comments/{finding_id}/dismiss"
        )
        referer = f"{self.BASE_URL}/{owner}/{repo}/pull/{pull_number}"

        try:
            resp = await self._client.put(
                path,
                json={"reason": reason, "resolution_note": resolution_note},
                headers={
                    "Content-Type": "application/json",
                    "Origin": self.BASE_URL,
                    "Referer": referer,
                    "GitHub-Verified-Fetch": "true",
                    "X-Fetch-Nonce": nonce,
                    "X-Requested-With": "XMLHttpRequest",
                    "GitHub-Is-React": "true",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as e:
            raise GitHubWebSessionError(
                _scrub(f"Dismiss request failed: {e}", self._cookie)
            ) from None

        if resp.status_code == 204:
            return
        if resp.status_code in (401, 403):
            raise GitHubWebSessionError(
                f"Dismiss rejected ({resp.status_code}). Session cookie may be "
                "expired or lacks write access to this PR. For SAML/SSO orgs, "
                "ensure you have an active authenticated browser session."
            )
        if resp.status_code == 404:
            raise GitHubWebSessionError(
                f"Dismiss returned 404 — finding_id {finding_id} not found on "
                f"{owner}/{repo}#{pull_number}. Note: finding_id is the "
                "automated_review_comments id, NOT the PR review comment id."
            )
        raise GitHubWebSessionError(
            f"Dismiss returned unexpected status {resp.status_code}: "
            f"{resp.text[:200]!r}"
        )
