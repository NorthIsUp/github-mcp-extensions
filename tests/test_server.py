"""Smoke tests for the MCP server — verifies tools load and models validate."""

from __future__ import annotations

import pytest

from github_mcp_extensions.models import (
    AddReactionResult,
    ApplySuggestionResult,
    ApplySuggestionsBatchResult,
    CommentResult,
    DismissReviewResult,
    EditReviewCommentResult,
    FileChange,
    GetReviewCommentsResult,
    GitHubUser,
    ParsedSuggestion,
    RequestReviewersResult,
    ResolveReviewThreadResult,
    ReviewCommentResponse,
    ReviewInfo,
    ThreadResult,
    UnresolveReviewThreadResult,
)
from github_mcp_extensions.suggestion_utils import (
    apply_suggestion_to_content,
    parse_suggestion_from_body,
)


# ── Tool registration ───────────────────────────────────────────────

EXPECTED_TOOLS = [
    "get_review_comments",
    "apply_suggestion",
    "apply_suggestions_batch",
    "dismiss_review",
    "add_reaction",
    "edit_review_comment",
    "request_reviewers",
    "resolve_review_thread",
    "unresolve_review_thread",
]


def test_server_loads_all_tools():
    from github_mcp_extensions.server import mcp

    tool_names = [t.name for t in mcp._tool_manager._tools.values()]
    for expected in EXPECTED_TOOLS:
        assert expected in tool_names, f"Tool {expected!r} not registered"


def test_server_name():
    from github_mcp_extensions.server import mcp

    assert mcp.name == "github_extensions"


# ── Suggestion parsing ──────────────────────────────────────────────


def test_parse_suggestion_from_body():
    body = "Some review text\n```suggestion\nreplacement code\n```\nMore text"
    assert parse_suggestion_from_body(body) == "replacement code"


def test_parse_suggestion_multiline():
    body = "```suggestion\nline1\nline2\nline3\n```"
    assert parse_suggestion_from_body(body) == "line1\nline2\nline3"


def test_parse_suggestion_empty():
    body = "```suggestion\n\n```"
    assert parse_suggestion_from_body(body) == ""


def test_parse_suggestion_missing():
    body = "Just a regular comment with no suggestion"
    assert parse_suggestion_from_body(body) is None


def test_parse_suggestion_with_language_hint():
    body = "```suggestion\n  return None\n```"
    assert parse_suggestion_from_body(body) == "  return None"


# ── Applying suggestions to content ─────────────────────────────────


def test_apply_single_line():
    content = "line1\nline2\nline3\nline4"
    result = apply_suggestion_to_content(content, 2, 2, "replaced")
    assert result == "line1\nreplaced\nline3\nline4"


def test_apply_multi_line():
    content = "line1\nline2\nline3\nline4\nline5"
    result = apply_suggestion_to_content(content, 2, 4, "new_line")
    assert result == "line1\nnew_line\nline5"


def test_apply_delete_lines():
    content = "line1\nline2\nline3\nline4"
    result = apply_suggestion_to_content(content, 2, 3, "")
    assert result == "line1\nline4"


def test_apply_expand_lines():
    content = "line1\nline2\nline3"
    result = apply_suggestion_to_content(content, 2, 2, "new_a\nnew_b\nnew_c")
    assert result == "line1\nnew_a\nnew_b\nnew_c\nline3"


def test_apply_first_line():
    content = "line1\nline2\nline3"
    result = apply_suggestion_to_content(content, 1, 1, "replaced")
    assert result == "replaced\nline2\nline3"


def test_apply_last_line():
    content = "line1\nline2\nline3"
    result = apply_suggestion_to_content(content, 3, 3, "replaced")
    assert result == "line1\nline2\nreplaced"


# ── Model validation ────────────────────────────────────────────────


def test_review_comment_response_parses_minimal():
    data = {
        "id": 123,
        "node_id": "PRRC_abc",
        "body": "test",
        "path": "foo.py",
        "line": 10,
        "extra_field": "ignored",
    }
    r = ReviewCommentResponse.model_validate(data)
    assert r.id == 123
    assert r.path == "foo.py"
    assert r.start_line is None


def test_github_user_parses():
    u = GitHubUser.model_validate({"login": "octocat", "id": 1, "extra": True})
    assert u.login == "octocat"


def test_apply_suggestion_result_serializes():
    r = ApplySuggestionResult(
        path="src/foo.py",
        lines="5-7",
        commit_sha="abc123",
        commit_url="https://github.com/...",
    )
    d = r.model_dump()
    assert d["applied"] is True
    assert d["path"] == "src/foo.py"


def test_get_review_comments_result_serializes():
    r = GetReviewCommentsResult(
        total_thread_count=1,
        has_next_page=False,
        end_cursor=None,
        threads=[
            ThreadResult(
                thread_node_id="PRRT_abc",
                is_resolved=False,
                is_outdated=False,
                is_collapsed=False,
                path="foo.py",
                line=10,
                start_line=None,
                original_line=10,
                original_start_line=None,
                diff_side="RIGHT",
                subject_type="LINE",
                comment_count=1,
                comments=[
                    CommentResult(
                        id=456,
                        node_id="PRRC_def",
                        body="```suggestion\nnew code\n```",
                        path="foo.py",
                        author="reviewer",
                        created_at="2024-01-01T00:00:00Z",
                        updated_at="2024-01-01T00:00:00Z",
                        url="https://github.com/...",
                        state="SUBMITTED",
                        has_suggestion=True,
                        suggestion="new code",
                        review=ReviewInfo(id=789, node_id="PRR_ghi", state="CHANGES_REQUESTED"),
                    )
                ],
            )
        ],
    )
    d = r.model_dump()
    assert d["threads"][0]["thread_node_id"] == "PRRT_abc"
    assert d["threads"][0]["comments"][0]["has_suggestion"] is True


def test_file_change_model():
    fc = FileChange(path="a.py", content="print('hi')")
    assert fc.path == "a.py"


def test_parsed_suggestion_model():
    comment = ReviewCommentResponse(
        id=1, node_id="n", body="b", path="p.py", line=5
    )
    ps = ParsedSuggestion(
        comment_id=1,
        path="p.py",
        start_line=5,
        end_line=5,
        replacement="new",
        original_comment=comment,
    )
    assert ps.end_line == 5


def test_all_result_models_have_defaults():
    """Verify result models with bool flags default to True."""
    assert ApplySuggestionResult(path="a", lines="1-1", commit_sha="x", commit_url="y").applied is True
    assert ApplySuggestionsBatchResult(suggestion_count=1, files_changed=["a"], commit_sha="x", commit_url="y").applied is True
    assert DismissReviewResult(review_id=1, state="DISMISSED", reviewer=None, message="m").dismissed is True
    assert AddReactionResult(reaction_id=1, content="+1", user=None).added is True
    assert EditReviewCommentResult(comment_id=1, body="b", updated_at="t", url="u").edited is True
    assert ResolveReviewThreadResult(thread_node_id="PRRT_abc").resolved is True
    assert UnresolveReviewThreadResult(thread_node_id="PRRT_abc").unresolved is True
