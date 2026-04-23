"""Smoke tests for the MCP server — verifies tools load and models validate."""

from __future__ import annotations

import pytest

from github_mcp_extensions.models import (
    AddReactionResult,
    ApplySuggestionResult,
    ApplySuggestionsBatchResult,
    CommentResult,
    DismissCodeQualityFindingResult,
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
from github_mcp_extensions.server import _GH_COMMENT_URL_RE, _norm, _parse_comment_id, _parse_comment_ref
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
    "dismiss_finding",
    "dismiss_code_quality_finding",
]


def test_server_loads_all_tools():
    from github_mcp_extensions.server import mcp

    tool_names = [t.name for t in mcp._tool_manager._tools.values()]
    for expected in EXPECTED_TOOLS:
        assert expected in tool_names, f"Tool {expected!r} not registered"


def test_server_name():
    from github_mcp_extensions.server import mcp

    assert mcp.name == "github_extensions"


# ── ID normalisation ────────────────────────────────────────────────


def test_norm_lowercases():
    assert _norm("NorthIsUp", "Clara_V1") == ("northisup", "clara_v1")


def test_norm_already_lower():
    assert _norm("org", "repo") == ("org", "repo")


def test_parse_comment_id_integer():
    assert _parse_comment_id(123) == 123


def test_parse_comment_id_r_prefix():
    assert _parse_comment_id("r3076443930") == 3076443930


def test_parse_comment_id_plain_string():
    assert _parse_comment_id("456") == 456


def test_parse_comment_id_url():
    url = "https://github.com/org/repo/pull/1#discussion_r3076443930"
    assert _parse_comment_id(url) == 3076443930


def test_parse_comment_id_invalid():
    with pytest.raises(ValueError):
        _parse_comment_id("not-an-id")


def test_gh_comment_url_re_matches():
    url = "https://github.com/teamclara/Clara_V1/pull/656#discussion_r3076443930"
    m = _GH_COMMENT_URL_RE.match(url)
    assert m is not None
    assert m.group(1) == "teamclara"
    assert m.group(2) == "Clara_V1"
    assert m.group(3) == "656"
    assert m.group(4) == "3076443930"


def test_gh_comment_url_re_no_match_without_anchor():
    url = "https://github.com/org/repo/pull/1"
    assert _GH_COMMENT_URL_RE.match(url) is None


def test_parse_comment_ref_from_url():
    url = "https://github.com/teamclara/Clara_V1/pull/656#discussion_r3076443930"
    ref = _parse_comment_ref(url)
    assert ref.comment_id == 3076443930
    assert ref.owner == "teamclara"
    assert ref.repo == "clara_v1"
    assert ref.pull_number == 656


def test_parse_comment_ref_from_integer():
    ref = _parse_comment_ref(123)
    assert ref.comment_id == 123
    assert ref.owner is None
    assert ref.pull_number is None


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
    assert DismissCodeQualityFindingResult(
        finding_id=13882761, reason="WONT_FIX", resolution_note=""
    ).dismissed is True


# ── github_web (session cookie client) ──────────────────────────────


def test_github_web_requires_cookie(monkeypatch):
    from github_mcp_extensions.github_web import GitHubWebSession, GitHubWebSessionError

    monkeypatch.delenv("GH_WEB_SESSION_COOKIE", raising=False)
    with pytest.raises(GitHubWebSessionError, match="GH_WEB_SESSION_COOKIE"):
        GitHubWebSession()


def test_github_web_nonce_regex_finds_uuid_format():
    from github_mcp_extensions.github_web import _NONCE_RE

    html = (
        '<html><script>window.payload={"fetchNonce":'
        '"v2:d773f67b-4069-ecfb-133f-1ec362f98711"};</script></html>'
    )
    m = _NONCE_RE.search(html)
    assert m is not None
    assert m.group(0) == "v2:d773f67b-4069-ecfb-133f-1ec362f98711"


def test_github_web_nonce_regex_no_match_on_plain_page():
    from github_mcp_extensions.github_web import _NONCE_RE

    assert _NONCE_RE.search("<html>no nonce here</html>") is None


def test_github_web_scrub_redacts_cookie():
    from github_mcp_extensions.github_web import _scrub

    cookie = "_gh_sess=SECRET; user_session=ALSO_SECRET"
    msg = f"httpx failed with cookie: {cookie}"
    assert cookie not in _scrub(msg, cookie)
    assert "<redacted>" in _scrub(msg, cookie)
