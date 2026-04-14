"""
Shared utilities for parsing and applying GitHub suggestion blocks.
"""

from __future__ import annotations

import asyncio
import re
from base64 import b64decode
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .github_api import GitHubAPI

# ── Types ────────────────────────────────────────────────────────────

@dataclass
class ParsedSuggestion:
    comment_id: int
    path: str
    start_line: int
    end_line: int
    replacement: str  # The replacement text (lines joined with \n)
    original_comment: dict[str, Any]


@dataclass
class FileChange:
    path: str
    content: str


# ── Parsing ──────────────────────────────────────────────────────────

_SUGGESTION_RE = re.compile(r"```suggestion\s*\n([\s\S]*?)```")


def parse_suggestion_from_body(body: str) -> str | None:
    """Extract the first ```suggestion block content from a comment body."""
    m = _SUGGESTION_RE.search(body)
    if not m:
        return None
    content = m.group(1)
    # Remove exactly one trailing newline (markdown artefact)
    if content.endswith("\n"):
        content = content[:-1]
    return content


# ── Fetch + Parse ────────────────────────────────────────────────────

async def fetch_and_parse_suggestion(
    github: GitHubAPI,
    owner: str,
    repo: str,
    comment_id: int,
) -> ParsedSuggestion:
    comment = await github.rest("GET", f"/repos/{owner}/{repo}/pulls/comments/{comment_id}")

    replacement = parse_suggestion_from_body(comment["body"])
    if replacement is None:
        raise ValueError(f"Comment {comment_id} does not contain a ```suggestion block.")

    end_line = comment.get("line")
    if end_line is None:
        raise ValueError(f"Comment {comment_id} has no line position — cannot apply suggestion.")

    start_line = comment.get("start_line") or end_line

    return ParsedSuggestion(
        comment_id=comment_id,
        path=comment["path"],
        start_line=start_line,
        end_line=end_line,
        replacement=replacement,
        original_comment=comment,
    )


# ── Apply to file content ───────────────────────────────────────────

def apply_suggestion_to_content(
    content: str,
    start_line: int,
    end_line: int,
    replacement: str,
) -> str:
    """Replace lines start_line..end_line (1-indexed, inclusive) with replacement."""
    lines = content.split("\n")
    before = lines[: start_line - 1]
    after = lines[end_line:]
    replacement_lines = replacement.split("\n") if replacement else []
    return "\n".join([*before, *replacement_lines, *after])


# ── Batch: group + apply (sorted descending to preserve offsets) ────

async def apply_multiple_suggestions(
    github: GitHubAPI,
    owner: str,
    repo: str,
    pull_number: int,
    comment_ids: list[int],
) -> list[FileChange]:
    """Fetch, parse, and apply multiple suggestions. Returns modified file contents."""
    # 1. Fetch all suggestions concurrently
    suggestions = await asyncio.gather(
        *(fetch_and_parse_suggestion(github, owner, repo, cid) for cid in comment_ids)
    )

    # 2. Get PR head ref
    pr = await github.rest("GET", f"/repos/{owner}/{repo}/pulls/{pull_number}")
    head_ref = pr["head"]["ref"]

    # 3. Group by file path
    by_file: dict[str, list[ParsedSuggestion]] = {}
    for s in suggestions:
        by_file.setdefault(s.path, []).append(s)

    # 4. For each file, fetch content and apply suggestions bottom-up
    changes: list[FileChange] = []

    for path, file_suggestions in by_file.items():
        # Sort descending so earlier replacements don't shift later ones
        file_suggestions.sort(key=lambda s: s.start_line, reverse=True)

        # Check for overlapping ranges
        for i in range(len(file_suggestions) - 1):
            current = file_suggestions[i]
            next_s = file_suggestions[i + 1]
            if next_s.end_line >= current.start_line:
                raise ValueError(
                    f"Suggestions on {path} overlap: comment {next_s.comment_id} "
                    f"(lines {next_s.start_line}-{next_s.end_line}) and comment "
                    f"{current.comment_id} (lines {current.start_line}-{current.end_line})."
                )

        # Fetch current file content
        file_data = await github.rest(
            "GET", f"/repos/{owner}/{repo}/contents/{quote(path, safe='')}?ref={quote(head_ref, safe='')}"
        )
        file_content = b64decode(file_data["content"]).decode("utf-8")

        # Apply each suggestion (already sorted descending)
        for s in file_suggestions:
            file_content = apply_suggestion_to_content(
                file_content, s.start_line, s.end_line, s.replacement
            )

        changes.append(FileChange(path=path, content=file_content))

    return changes


# ── Commit via Git Data API (single commit, multiple files) ─────────

async def commit_file_changes(
    github: GitHubAPI,
    owner: str,
    repo: str,
    branch: str,
    message: str,
    changes: list[FileChange],
) -> dict[str, str]:
    """Create a single commit with multiple file changes. Returns {sha, url}."""
    # Get current ref
    ref = await github.rest("GET", f"/repos/{owner}/{repo}/git/ref/heads/{quote(branch, safe='')}")
    base_sha = ref["object"]["sha"]

    # Get base commit's tree
    base_commit = await github.rest("GET", f"/repos/{owner}/{repo}/git/commits/{base_sha}")

    # Create blobs for each changed file
    tree_items = []
    for change in changes:
        blob = await github.rest(
            "POST",
            f"/repos/{owner}/{repo}/git/blobs",
            json={"content": change.content, "encoding": "utf-8"},
        )
        tree_items.append({
            "path": change.path,
            "mode": "100644",
            "type": "blob",
            "sha": blob["sha"],
        })

    # Create new tree
    tree = await github.rest(
        "POST",
        f"/repos/{owner}/{repo}/git/trees",
        json={"base_tree": base_commit["tree"]["sha"], "tree": tree_items},
    )

    # Create commit
    commit = await github.rest(
        "POST",
        f"/repos/{owner}/{repo}/git/commits",
        json={"message": message, "tree": tree["sha"], "parents": [base_sha]},
    )

    # Update ref
    await github.rest(
        "PATCH",
        f"/repos/{owner}/{repo}/git/refs/heads/{quote(branch, safe='')}",
        json={"sha": commit["sha"]},
    )

    return {"sha": commit["sha"], "url": commit.get("html_url", "")}
