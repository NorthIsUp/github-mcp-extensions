"""
Pydantic models for GitHub API responses and tool results.

All API response models use extra="ignore" so only the fields we need
are parsed — the rest of the GitHub payload is silently dropped.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


# ── Base ─────────────────────────────────────────────────────────────

class _GH(BaseModel):
    """Base for GitHub API response models — ignores extra fields."""
    model_config = ConfigDict(extra="ignore")


# ═══════════════════════════════════════════════════════════════════════
# GitHub REST API response models
# ═══════════════════════════════════════════════════════════════════════


class GitHubUser(_GH):
    login: str


class GitHubTeam(_GH):
    slug: str


# ── Review comment (GET /repos/{o}/{r}/pulls/comments/{id}) ─────────

class ReviewCommentResponse(_GH):
    id: int
    node_id: str
    body: str
    path: str
    line: int | None = None
    start_line: int | None = None
    side: str = "RIGHT"
    original_line: int | None = None
    original_start_line: int | None = None
    commit_id: str = ""
    html_url: str = ""


# ── Pull request (GET /repos/{o}/{r}/pulls/{n}) ─────────────────────

class PullRequestHead(_GH):
    ref: str
    sha: str


class PullRequestResponse(_GH):
    head: PullRequestHead


# ── File contents (GET /repos/{o}/{r}/contents/{path}) ──────────────

class FileContentResponse(_GH):
    content: str
    sha: str
    encoding: str = "base64"


# ── Contents API commit (PUT /repos/{o}/{r}/contents/{path}) ────────

class CommitInfo(_GH):
    sha: str
    html_url: str = ""


class ContentCommitResponse(_GH):
    commit: CommitInfo


# ── Git Data API ────────────────────────────────────────────────────

class GitObject(_GH):
    sha: str


class GitRefResponse(_GH):
    object: GitObject = Field(alias="object")


class GitTree(_GH):
    sha: str


class GitCommitResponse(_GH):
    sha: str
    html_url: str = ""
    tree: GitTree


class GitBlobResponse(_GH):
    sha: str


class GitTreeResponse(_GH):
    sha: str


# ── Dismiss review (PUT …/reviews/{id}/dismissals) ──────────────────

class DismissReviewResponse(_GH):
    id: int
    state: str
    user: GitHubUser | None = None


# ── Reaction (POST …/pulls/comments/{id}/reactions) ─────────────────

class ReactionResponse(_GH):
    id: int
    content: str
    user: GitHubUser | None = None


# ── Edit comment (PATCH …/pulls/comments/{id}) ──────────────────────

class EditCommentResponse(_GH):
    id: int
    body: str
    updated_at: str
    html_url: str


# ── Request reviewers (POST/DELETE …/requested_reviewers) ────────────

class RequestReviewersResponse(_GH):
    requested_reviewers: list[GitHubUser] = []
    requested_teams: list[GitHubTeam] = []


# ═══════════════════════════════════════════════════════════════════════
# GraphQL response models (for get_review_comments)
# ═══════════════════════════════════════════════════════════════════════


class GqlAuthor(BaseModel):
    login: str


class GqlReview(BaseModel):
    id: str
    databaseId: int
    state: str


class GqlComment(BaseModel):
    id: str
    databaseId: int
    body: str
    path: str
    author: GqlAuthor | None = None
    createdAt: str
    updatedAt: str
    url: str
    state: str
    pullRequestReview: GqlReview | None = None


class GqlComments(BaseModel):
    totalCount: int
    nodes: list[GqlComment]


class GqlThread(BaseModel):
    id: str
    isResolved: bool
    isOutdated: bool
    isCollapsed: bool
    path: str
    line: int | None = None
    startLine: int | None = None
    originalLine: int | None = None
    originalStartLine: int | None = None
    diffSide: str
    subjectType: str
    comments: GqlComments


class GqlPageInfo(BaseModel):
    hasNextPage: bool
    endCursor: str | None = None


class GqlReviewThreads(BaseModel):
    totalCount: int
    pageInfo: GqlPageInfo
    nodes: list[GqlThread]


class GqlPullRequest(BaseModel):
    reviewThreads: GqlReviewThreads


class GqlRepository(BaseModel):
    pullRequest: GqlPullRequest


class GqlReviewThreadsResponse(BaseModel):
    repository: GqlRepository


# ═══════════════════════════════════════════════════════════════════════
# Internal models (suggestion parsing)
# ═══════════════════════════════════════════════════════════════════════


class ParsedSuggestion(BaseModel):
    comment_id: int
    path: str
    start_line: int
    end_line: int
    replacement: str
    original_comment: ReviewCommentResponse


class FileChange(BaseModel):
    path: str
    content: str


class CommitResult(BaseModel):
    sha: str
    url: str


# ═══════════════════════════════════════════════════════════════════════
# Tool result models (serialized back to the MCP client)
# ═══════════════════════════════════════════════════════════════════════


class ReviewInfo(BaseModel):
    id: int
    node_id: str
    state: str


class CommentResult(BaseModel):
    id: int
    node_id: str
    body: str
    path: str
    author: str | None
    created_at: str
    updated_at: str
    url: str
    state: str
    has_suggestion: bool
    suggestion: str | None
    review: ReviewInfo | None


class ThreadResult(BaseModel):
    thread_node_id: str
    is_resolved: bool
    is_outdated: bool
    is_collapsed: bool
    path: str
    line: int | None
    start_line: int | None
    original_line: int | None
    original_start_line: int | None
    diff_side: str
    subject_type: str
    comment_count: int
    comments: list[CommentResult]


class GetReviewCommentsResult(BaseModel):
    total_thread_count: int
    has_next_page: bool
    end_cursor: str | None
    threads: list[ThreadResult]


class ApplySuggestionResult(BaseModel):
    applied: bool = True
    path: str
    lines: str
    commit_sha: str
    commit_url: str


class ApplySuggestionsBatchResult(BaseModel):
    applied: bool = True
    suggestion_count: int
    files_changed: list[str]
    commit_sha: str
    commit_url: str


class DismissReviewResult(BaseModel):
    dismissed: bool = True
    review_id: int
    state: str
    reviewer: str | None
    message: str


class AddReactionResult(BaseModel):
    added: bool = True
    reaction_id: int
    content: str
    user: str | None


class EditReviewCommentResult(BaseModel):
    edited: bool = True
    comment_id: int
    body: str
    updated_at: str
    url: str


class RequestReviewersResult(BaseModel):
    action: str
    requested_reviewers: list[str]
    requested_teams: list[str]


class ResolveReviewThreadResult(BaseModel):
    resolved: bool = True
    thread_node_id: str


class UnresolveReviewThreadResult(BaseModel):
    unresolved: bool = True
    thread_node_id: str
