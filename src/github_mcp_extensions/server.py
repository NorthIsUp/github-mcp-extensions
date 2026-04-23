"""
github_extensions MCP server — fills gaps in the standard GitHub MCP's
PR review workflow.

Tools:
  get_review_comments     Enhanced: returns thread node_id, comment id, suggestion metadata
  apply_suggestion        Apply a single ```suggestion from a review comment
  apply_suggestions_batch Apply multiple suggestions in one commit
  dismiss_review          Dismiss a review (e.g. after addressing feedback)
  add_reaction            React to a review comment with an emoji
  edit_review_comment     Edit the body of a review comment
  request_reviewers       Add or remove reviewers on a PR
  resolve_review_thread   Mark a review thread as resolved
  unresolve_review_thread Mark a resolved review thread as unresolved
  dismiss_finding         Dismiss a Copilot/code-scanning finding (resolves thread)
  dismiss_code_quality_finding
                          Dismiss a github-code-quality[bot] finding via the
                          private PR endpoint (session-cookie auth)

Usage with Claude Code / MCP clients:

  {
    "mcpServers": {
      "github_extensions": {
        "command": "uvx",
        "args": ["github-mcp-extensions"],
        "env": {
          "GITHUB_PERSONAL_ACCESS_TOKEN": "...",
          "GITHUB_API_URL": "https://api.github.com"
        }
      }
    }
  }

Set GITHUB_API_URL to point at the same proxy as the standard GitHub MCP if you need
to share auth/org access (e.g. "http://127.0.0.1:35420").
"""

from __future__ import annotations

import re
from base64 import b64decode, b64encode
from dataclasses import dataclass, field
from typing import Annotated, Literal
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP

from .github_api import GitHubAPI
from .github_web import GitHubWebSession
from .models import (
    AddReactionResult,
    ApplySuggestionResult,
    ApplySuggestionsBatchResult,
    CommentResult,
    ContentCommitResponse,
    DismissCodeQualityFindingResult,
    DismissReviewResponse,
    DismissReviewResult,
    EditCommentResponse,
    EditReviewCommentResult,
    FileContentResponse,
    GetReviewCommentsResult,
    GqlReviewThreadsResponse,
    PullRequestResponse,
    ReactionResponse,
    RequestReviewersResponse,
    RequestReviewersResult,
    ResolveReviewThreadResult,
    ReviewInfo,
    ThreadResult,
    UnresolveReviewThreadResult,
)
from .suggestion_utils import (
    apply_multiple_suggestions,
    apply_suggestion_to_content,
    commit_file_changes,
    fetch_and_parse_suggestion,
    parse_suggestion_from_body,
)

# ── Shared parameter type aliases ───────────────────────────────────

_CommentIdParam = Annotated[
    int | str,
    "Review comment ID. Accepted forms: "
    "(1) integer e.g. 3076443930 — from get_review_comments → comment.id; "
    "(2) 'r<n>' e.g. 'r3076443930' — the anchor suffix in any GitHub comment URL; "
    "(3) full GitHub comment URL e.g. 'https://github.com/org/repo/pull/1#discussion_r3076443930' "
    "— owner, repo, and pull_number are inferred automatically from the URL.",
]

_ThreadIdParam = Annotated[
    str,
    "Review thread identifier. Accepted forms: "
    "(1) thread node ID e.g. 'PRRT_kwDO…' — from get_review_comments → thread_node_id (preferred, zero extra API calls); "
    "(2) comment node ID e.g. 'PRRC_kwDO…' — from get_review_comments → comment.node_id (parent thread looked up via GraphQL); "
    "(3) full GitHub comment URL e.g. 'https://github.com/org/repo/pull/1#discussion_r3076443930' "
    "(comment fetched via REST to get node ID, then parent thread looked up via GraphQL).",
]

# ── Server + API client ─────────────────────────────────────────────

mcp = FastMCP("github_extensions")

_github: GitHubAPI | None = None


def _get_github() -> GitHubAPI:
    """Lazy-init the GitHub client (defers token check until first tool call)."""
    global _github
    if _github is None:
        _github = GitHubAPI()
    return _github


# ── ID normalisation helpers ─────────────────────────────────────────

def _norm(owner: str, repo: str) -> tuple[str, str]:
    """Lowercase owner and repo — GitHub is case-insensitive but GraphQL is not."""
    return owner.lower(), repo.lower()


@dataclass
class _CommentRef:
    """Parsed comment reference — all context extractable from a GitHub comment URL."""
    comment_id: int
    owner: str | None = field(default=None)
    repo: str | None = field(default=None)
    pull_number: int | None = field(default=None)


# Captures: owner(1) repo(2) pull_number(3) comment_db_id(4)
_GH_COMMENT_URL_RE = re.compile(
    r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)#discussion_r(\d+)$"
)


def _parse_comment_ref(comment_id: int | str) -> _CommentRef:
    """Parse any comment ID form into a _CommentRef, extracting owner/repo/PR when a URL is given."""
    if isinstance(comment_id, int):
        return _CommentRef(comment_id=comment_id)
    s = str(comment_id).strip()
    if m := _GH_COMMENT_URL_RE.match(s):
        return _CommentRef(
            comment_id=int(m.group(4)),
            owner=m.group(1).lower(),
            repo=m.group(2).lower(),
            pull_number=int(m.group(3)),
        )
    if m := re.match(r"^r(\d+)$", s):
        return _CommentRef(comment_id=int(m.group(1)))
    if s.isdigit():
        return _CommentRef(comment_id=int(s))
    raise ValueError(
        f"Cannot parse comment ID from {s!r}. "
        "Expected an integer, 'r<n>', or a GitHub comment URL."
    )


def _parse_comment_id(comment_id: int | str) -> int:
    return _parse_comment_ref(comment_id).comment_id


_THREAD_FROM_COMMENT_QUERY = """
query GetThreadFromComment($nodeId: ID!) {
  node(id: $nodeId) {
    ... on PullRequestReviewComment {
      pullRequestThread { id }
    }
  }
}
"""


async def _resolve_to_thread_id(github: GitHubAPI, id_or_url: str) -> str:
    """Accept a thread node ID, comment node ID, or GitHub comment URL and return PRRT_….

    Supported forms:
      - PRRT_kwDO…                                    thread node ID — used directly
      - PRRC_kwDO…                                    comment node ID — parent thread looked up
      - https://github.com/…/pull/N#discussion_rN     comment URL — REST + GraphQL lookup
    """
    if id_or_url.startswith("PRRT_"):
        return id_or_url
    if id_or_url.startswith("PRRC_"):
        data = await github.graphql_raw(_THREAD_FROM_COMMENT_QUERY, {"nodeId": id_or_url})
        try:
            return data["node"]["pullRequestThread"]["id"]
        except (KeyError, TypeError):
            raise ValueError(f"Could not find parent thread for comment node {id_or_url!r}")
    if m := _GH_COMMENT_URL_RE.match(id_or_url):
        owner, repo, comment_db_id = m.group(1).lower(), m.group(2).lower(), m.group(4)
        comment_data = await github.rest_raw(
            "GET", f"/repos/{owner}/{repo}/pulls/comments/{comment_db_id}"
        )
        node_id = comment_data["node_id"]
        data = await github.graphql_raw(_THREAD_FROM_COMMENT_QUERY, {"nodeId": node_id})
        try:
            return data["node"]["pullRequestThread"]["id"]
        except (KeyError, TypeError):
            raise ValueError(f"Could not find parent thread for comment URL {id_or_url!r}")
    raise ValueError(
        f"Unrecognised ID format {id_or_url!r}. "
        "Expected a thread node ID (PRRT_…), comment node ID (PRRC_…), "
        "or GitHub comment URL (https://github.com/…/pull/N#discussion_rN)."
    )


# ── GraphQL query for review threads ────────────────────────────────

_REVIEW_THREADS_QUERY = """
query GetReviewThreads(
  $owner: String!
  $repo: String!
  $number: Int!
  $first: Int!
  $after: String
) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: $first, after: $after) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          isCollapsed
          path
          line
          startLine
          originalLine
          originalStartLine
          diffSide
          subjectType
          comments(first: 100) {
            totalCount
            nodes {
              id
              databaseId
              body
              path
              author { login }
              createdAt
              updatedAt
              url
              state
              pullRequestReview {
                id
                databaseId
                state
              }
            }
          }
        }
      }
    }
  }
}
"""


# ═══════════════════════════════════════════════════════════════════════
# Tool 1: get_review_comments
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool()
async def get_review_comments(
    owner: Annotated[str, "Repository owner"],
    repo: Annotated[str, "Repository name"],
    pull_number: Annotated[int, "Pull request number"],
    per_page: Annotated[int, "Threads per page (1-100, default 30)"] = 30,
    after: Annotated[str | None, "Cursor for pagination (endCursor from previous page)"] = None,
) -> GetReviewCommentsResult:
    """Get review threads on a pull request with full IDs.

    Returns thread node_id (PRRT_… for resolve_review_thread), comment numeric
    id (for add_reply / apply_suggestion), and parsed suggestion metadata.
    Superset of the standard GitHub MCP's get_review_comments.
    """
    owner, repo = _norm(owner, repo)
    github = _get_github()
    data = await github.graphql(
        GqlReviewThreadsResponse,
        _REVIEW_THREADS_QUERY,
        {
            "owner": owner,
            "repo": repo,
            "number": pull_number,
            "first": min(max(per_page, 1), 100),
            "after": after,
        },
    )

    threads = data.repository.pullRequest.reviewThreads

    result_threads = []
    for thread in threads.nodes:
        comments = []
        for c in thread.comments.nodes:
            suggestion = parse_suggestion_from_body(c.body)
            review = (
                ReviewInfo(id=c.pullRequestReview.databaseId, node_id=c.pullRequestReview.id, state=c.pullRequestReview.state)
                if c.pullRequestReview else None
            )
            comments.append(CommentResult(
                id=c.databaseId,
                node_id=c.id,
                body=c.body,
                path=c.path,
                author=c.author.login if c.author else None,
                created_at=c.createdAt,
                updated_at=c.updatedAt,
                url=c.url,
                state=c.state,
                has_suggestion=suggestion is not None,
                suggestion=suggestion,
                review=review,
            ))

        result_threads.append(ThreadResult(
            thread_node_id=thread.id,
            is_resolved=thread.isResolved,
            is_outdated=thread.isOutdated,
            is_collapsed=thread.isCollapsed,
            path=thread.path,
            line=thread.line,
            start_line=thread.startLine,
            original_line=thread.originalLine,
            original_start_line=thread.originalStartLine,
            diff_side=thread.diffSide,
            subject_type=thread.subjectType,
            comment_count=thread.comments.totalCount,
            comments=comments,
        ))

    return GetReviewCommentsResult(
        total_thread_count=threads.totalCount,
        has_next_page=threads.pageInfo.hasNextPage,
        end_cursor=threads.pageInfo.endCursor,
        threads=result_threads,
    )


# ═══════════════════════════════════════════════════════════════════════
# Tool 2: apply_suggestion
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool()
async def apply_suggestion(
    comment_id: _CommentIdParam,
    owner: Annotated[str | None, "Repository owner — inferred automatically when comment_id is a full GitHub URL"] = None,
    repo: Annotated[str | None, "Repository name — inferred automatically when comment_id is a full GitHub URL"] = None,
    pull_number: Annotated[int | None, "PR number — inferred automatically when comment_id is a full GitHub URL"] = None,
    commit_message: Annotated[str | None, "Custom commit message (optional)"] = None,
) -> ApplySuggestionResult:
    """Apply a single code suggestion — equivalent to GitHub's 'Commit suggestion' button.

    Pass a full GitHub comment URL as comment_id and owner/repo/pull_number are
    inferred automatically. Reads the ```suggestion block, modifies the file,
    and creates a commit on the PR branch.
    """
    ref = _parse_comment_ref(comment_id)
    owner = (owner or ref.owner or "").lower() or None
    repo = (repo or ref.repo or "").lower() or None
    pull_number = pull_number or ref.pull_number
    if not owner or not repo or not pull_number:
        raise ValueError(
            "owner, repo, and pull_number are required when comment_id is not a full GitHub comment URL."
        )
    comment_id = ref.comment_id
    github = _get_github()

    # 1. Parse suggestion from the comment
    suggestion = await fetch_and_parse_suggestion(github, owner, repo, comment_id)

    # 2. Get PR head branch
    pr = await github.rest(PullRequestResponse, "GET", f"/repos/{owner}/{repo}/pulls/{pull_number}")
    head_ref = pr.head.ref

    # 3. Fetch current file content
    file_data = await github.rest(
        FileContentResponse,
        "GET",
        f"/repos/{owner}/{repo}/contents/{quote(suggestion.path, safe='')}?ref={quote(head_ref, safe='')}",
    )
    original_content = b64decode(file_data.content).decode("utf-8")

    # 4. Apply the suggestion
    new_content = apply_suggestion_to_content(
        original_content, suggestion.start_line, suggestion.end_line, suggestion.replacement
    )

    # 5. Commit via Contents API
    message = commit_message or (
        f"Apply suggestion from code review\n\n"
        f"Applied suggestion from comment {comment_id} on {suggestion.path}"
    )

    result = await github.rest(
        ContentCommitResponse,
        "PUT",
        f"/repos/{owner}/{repo}/contents/{quote(suggestion.path, safe='')}",
        json={
            "message": message,
            "content": b64encode(new_content.encode("utf-8")).decode("ascii"),
            "sha": file_data.sha,
            "branch": head_ref,
        },
    )

    return ApplySuggestionResult(
        path=suggestion.path,
        lines=f"{suggestion.start_line}-{suggestion.end_line}",
        commit_sha=result.commit.sha,
        commit_url=result.commit.html_url,
    )


# ═══════════════════════════════════════════════════════════════════════
# Tool 3: apply_suggestions_batch
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool()
async def apply_suggestions_batch(
    comment_ids: Annotated[list[_CommentIdParam], "List of review comment IDs — see apply_suggestion for accepted forms. When all entries are full GitHub URLs, owner/repo/pull_number are inferred from the first URL."],
    owner: Annotated[str | None, "Repository owner — inferred automatically when comment_ids are full GitHub URLs"] = None,
    repo: Annotated[str | None, "Repository name — inferred automatically when comment_ids are full GitHub URLs"] = None,
    pull_number: Annotated[int | None, "PR number — inferred automatically when comment_ids are full GitHub URLs"] = None,
    commit_message: Annotated[str | None, "Custom commit message (optional)"] = None,
) -> ApplySuggestionsBatchResult:
    """Apply multiple code suggestions in a single commit — equivalent to GitHub's 'Add suggestion to batch' then commit.

    Pass full GitHub comment URLs and owner/repo/pull_number are inferred
    automatically. Reads each ```suggestion block, modifies the affected files,
    and creates one atomic commit on the PR branch via the Git Data API.
    """
    refs = [_parse_comment_ref(c) for c in comment_ids]
    first = refs[0]
    owner = (owner or first.owner or "").lower() or None
    repo = (repo or first.repo or "").lower() or None
    pull_number = pull_number or first.pull_number
    if not owner or not repo or not pull_number:
        raise ValueError(
            "owner, repo, and pull_number are required when comment_ids are not full GitHub comment URLs."
        )
    comment_ids_int = [r.comment_id for r in refs]
    github = _get_github()

    # 1. Get PR to find the head branch (also validates the PR exists)
    pr = await github.rest(PullRequestResponse, "GET", f"/repos/{owner}/{repo}/pulls/{pull_number}")
    head_ref = pr.head.ref

    # 2. Fetch, parse, and apply all suggestions to file contents
    changes = await apply_multiple_suggestions(github, owner, repo, pull_number, comment_ids_int)

    # 3. Commit all changes atomically
    count = len(comment_ids_int)
    message = commit_message or f"Apply {count} suggestion{'s' if count > 1 else ''} from code review"

    commit = await commit_file_changes(github, owner, repo, head_ref, message, changes)

    return ApplySuggestionsBatchResult(
        suggestion_count=count,
        files_changed=[c.path for c in changes],
        commit_sha=commit.sha,
        commit_url=commit.url,
    )


# ═══════════════════════════════════════════════════════════════════════
# Tool 4: dismiss_review
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool()
async def dismiss_review(
    owner: Annotated[str, "Repository owner"],
    repo: Annotated[str, "Repository name"],
    pull_number: Annotated[int, "Pull request number"],
    review_id: Annotated[int, "Numeric REST ID of the review to dismiss (from get_review_comments → comment.review.id)"],
    message: Annotated[str, "Reason for dismissing the review"],
) -> DismissReviewResult:
    """Dismiss a pull request review.

    Use after addressing feedback to clear a 'changes requested' review.
    Requires write access to the repository.
    """
    owner, repo = _norm(owner, repo)
    github = _get_github()
    result = await github.rest(
        DismissReviewResponse,
        "PUT",
        f"/repos/{owner}/{repo}/pulls/{pull_number}/reviews/{review_id}/dismissals",
        json={"message": message, "event": "DISMISS"},
    )

    return DismissReviewResult(
        review_id=result.id,
        state=result.state,
        reviewer=result.user.login if result.user else None,
        message=message,
    )


# ═══════════════════════════════════════════════════════════════════════
# Tool 5: add_reaction
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool()
async def add_reaction(
    owner: Annotated[str, "Repository owner"],
    repo: Annotated[str, "Repository name"],
    comment_id: _CommentIdParam,
    reaction: Annotated[
        Literal["+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes"],
        "Reaction emoji",
    ],
) -> AddReactionResult:
    """Add an emoji reaction to a pull request review comment.

    Use for lightweight acknowledgment (thumbs up, etc.) without posting a full reply.
    """
    owner, repo = _norm(owner, repo)
    comment_id = _parse_comment_id(comment_id)
    github = _get_github()
    result = await github.rest(
        ReactionResponse,
        "POST",
        f"/repos/{owner}/{repo}/pulls/comments/{comment_id}/reactions",
        json={"content": reaction},
    )

    return AddReactionResult(
        reaction_id=result.id,
        content=result.content,
        user=result.user.login if result.user else None,
    )


# ═══════════════════════════════════════════════════════════════════════
# Tool 6: edit_review_comment
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool()
async def edit_review_comment(
    owner: Annotated[str, "Repository owner"],
    repo: Annotated[str, "Repository name"],
    comment_id: _CommentIdParam,
    body: Annotated[str, "New comment body (markdown)"],
) -> EditReviewCommentResult:
    """Edit the body of an existing pull request review comment.

    Useful for updating a reply after fixing the issue it referenced.
    """
    owner, repo = _norm(owner, repo)
    comment_id = _parse_comment_id(comment_id)
    github = _get_github()
    result = await github.rest(
        EditCommentResponse,
        "PATCH",
        f"/repos/{owner}/{repo}/pulls/comments/{comment_id}",
        json={"body": body},
    )

    return EditReviewCommentResult(
        comment_id=result.id,
        body=result.body,
        updated_at=result.updated_at,
        url=result.html_url,
    )


# ═══════════════════════════════════════════════════════════════════════
# Tool 7: request_reviewers
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool()
async def request_reviewers(
    owner: Annotated[str, "Repository owner"],
    repo: Annotated[str, "Repository name"],
    pull_number: Annotated[int, "Pull request number"],
    action: Annotated[Literal["add", "remove"], "Whether to add or remove reviewers"],
    reviewers: Annotated[list[str] | None, "GitHub usernames to add/remove as reviewers"] = None,
    team_reviewers: Annotated[list[str] | None, "Team slugs to add/remove as reviewers"] = None,
) -> RequestReviewersResult:
    """Add or remove reviewers (users and/or teams) on a pull request."""
    owner, repo = _norm(owner, repo)
    github = _get_github()

    if not reviewers and not team_reviewers:
        raise ValueError("At least one of reviewers or team_reviewers must be provided.")

    path = f"/repos/{owner}/{repo}/pulls/{pull_number}/requested_reviewers"
    body: dict[str, list[str]] = {}
    if reviewers:
        body["reviewers"] = reviewers
    if team_reviewers:
        body["team_reviewers"] = team_reviewers

    method = "POST" if action == "add" else "DELETE"
    result = await github.rest(RequestReviewersResponse, method, path, json=body)

    return RequestReviewersResult(
        action=action,
        requested_reviewers=[r.login for r in result.requested_reviewers],
        requested_teams=[t.slug for t in result.requested_teams],
    )


# ═══════════════════════════════════════════════════════════════════════
# Tool 8: resolve_review_thread
# ═══════════════════════════════════════════════════════════════════════

_RESOLVE_THREAD_MUTATION = """
mutation ResolveReviewThread($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread {
      id
      isResolved
    }
  }
}
"""

_UNRESOLVE_THREAD_MUTATION = """
mutation UnresolveReviewThread($threadId: ID!) {
  unresolveReviewThread(input: {threadId: $threadId}) {
    thread {
      id
      isResolved
    }
  }
}
"""


@mcp.tool()
async def resolve_review_thread(
    thread_id: _ThreadIdParam,
) -> ResolveReviewThreadResult:
    """Mark a pull request review thread as resolved.

    Accepts three forms of ID (in order of efficiency):
      1. PRRT_… thread node ID — from get_review_comments → thread_node_id. Zero extra calls.
      2. PRRC_… comment node ID — from get_review_comments → comment.node_id. One GraphQL lookup.
      3. GitHub comment URL — https://github.com/org/repo/pull/N#discussion_rN. One REST + one GraphQL lookup.
    """
    github = _get_github()
    thread_id = await _resolve_to_thread_id(github, thread_id)
    data = await github.graphql_raw(_RESOLVE_THREAD_MUTATION, {"threadId": thread_id})
    return ResolveReviewThreadResult(
        thread_node_id=data["resolveReviewThread"]["thread"]["id"],
    )


# ═══════════════════════════════════════════════════════════════════════
# Tool 9: unresolve_review_thread
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool()
async def unresolve_review_thread(
    thread_id: _ThreadIdParam,
) -> UnresolveReviewThreadResult:
    """Mark a previously resolved pull request review thread as unresolved.

    Accepts three forms of ID (in order of efficiency):
      1. PRRT_… thread node ID — from get_review_comments → thread_node_id. Zero extra calls.
      2. PRRC_… comment node ID — from get_review_comments → comment.node_id. One GraphQL lookup.
      3. GitHub comment URL — https://github.com/org/repo/pull/N#discussion_rN. One REST + one GraphQL lookup.
    """
    github = _get_github()
    thread_id = await _resolve_to_thread_id(github, thread_id)
    data = await github.graphql_raw(_UNRESOLVE_THREAD_MUTATION, {"threadId": thread_id})
    return UnresolveReviewThreadResult(
        thread_node_id=data["unresolveReviewThread"]["thread"]["id"],
    )


# ═══════════════════════════════════════════════════════════════════════
# Tool 10: dismiss_finding
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool()
async def dismiss_finding(
    thread_id: _ThreadIdParam,
) -> ResolveReviewThreadResult:
    """Dismiss a Copilot or code-scanning finding on a pull request.

    Equivalent to the 'Dismiss finding' button in GitHub's PR review UI —
    marks the thread as resolved. Accepts the same ID forms as resolve_review_thread:
      1. PRRT_… thread node ID — from get_review_comments → thread_node_id
      2. PRRC_… comment node ID — from get_review_comments → comment.node_id
      3. GitHub comment URL — https://github.com/org/repo/pull/N#discussion_rN
    """
    github = _get_github()
    thread_id = await _resolve_to_thread_id(github, thread_id)
    data = await github.graphql_raw(_RESOLVE_THREAD_MUTATION, {"threadId": thread_id})
    return ResolveReviewThreadResult(
        thread_node_id=data["resolveReviewThread"]["thread"]["id"],
    )


# ═══════════════════════════════════════════════════════════════════════
# Tool 11: dismiss_code_quality_finding
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool()
async def dismiss_code_quality_finding(
    owner: Annotated[str, "Repository owner"],
    repo: Annotated[str, "Repository name"],
    pull_number: Annotated[int, "Pull request number"],
    finding_id: Annotated[
        int,
        "automated_review_comments id — NOT the PR review comment id. "
        "Discoverable in the DOM of the PR page on the 'Dismiss finding' "
        "button, or in the URL of the dismiss request in DevTools → Network.",
    ],
    reason: Annotated[
        Literal["FALSE_POSITIVE", "USED_IN_TESTS", "WONT_FIX"],
        "Dismissal reason — matches the three radios in the GitHub UI dialog.",
    ],
    resolution_note: Annotated[str, "Optional free-text explanation (may be empty)"] = "",
) -> DismissCodeQualityFindingResult:
    """Dismiss a github-code-quality[bot] finding on a pull request.

    Equivalent to clicking 'Dismiss finding' in the PR UI. Uses a private
    github.com endpoint (not api.github.com) that authenticates via browser
    session cookies — a PAT / GITHUB_TOKEN is not sufficient.

    `resolve_review_thread` is NOT a substitute: it toggles isResolved on
    the GraphQL thread but does not update the bot's backing state, so the
    bot re-posts the same finding on the next commit.

    Required env var:
      GH_WEB_SESSION_COOKIE — the full Cookie header from an authenticated
      github.com browser tab (must include _gh_sess, user_session,
      dotcom_user). Sensitive and short-lived; refresh when it expires.
      For SAML/SSO orgs, ensure the cookie is from a session where SSO has
      been activated for the org.

    Implementation note: this relies on an undocumented, private endpoint.
    GitHub may change the route, headers, or auth at any time. Treat this
    as a bridge until GitHub ships a first-class API.
    """
    owner, repo = _norm(owner, repo)
    async with GitHubWebSession() as web:
        await web.dismiss_code_quality_finding(
            owner=owner,
            repo=repo,
            pull_number=pull_number,
            finding_id=finding_id,
            reason=reason,
            resolution_note=resolution_note,
        )
    return DismissCodeQualityFindingResult(
        finding_id=finding_id,
        reason=reason,
        resolution_note=resolution_note,
    )


# ── Entry point ─────────────────────────────────────────────────────


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
