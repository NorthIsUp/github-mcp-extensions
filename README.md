# github-mcp-extensions

Extended GitHub MCP server that fills gaps in the standard GitHub MCP's PR review workflow.

Designed to run **alongside** the standard GitHub MCP — it doesn't replace it, it adds the missing tools. Tools appear as `mcp__github_extensions__*` in your MCP client.

## The problem

The standard GitHub MCP server doesn't cover the full PR review workflow. You can't apply code suggestions, dismiss reviews, react to comments, edit review comments, or manage reviewers. This server adds those missing pieces.

## Install

```
pip install github-mcp-extensions
```

Or run directly:

```
uvx github-mcp-extensions
```

## Configuration

Add to your MCP client config (Claude Code, Claude Desktop, etc.):

```json
{
  "mcpServers": {
    "github_extensions": {
      "command": "uvx",
      "args": ["github-mcp-extensions"],
      "env": {
        "GITHUB_TOKEN": "ghp_..."
      }
    }
  }
}
```

Or from git:

```json
{
  "mcpServers": {
    "github_extensions": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/NorthIsUp/github-mcp-extended", "github-mcp-extensions"],
      "env": {
        "GITHUB_TOKEN": "ghp_..."
      }
    }
  }
}
```

The token needs `repo` scope (same as the standard GitHub MCP).

## Tools

### `get_review_comments`

Enhanced version of the standard MCP's `get_review_comments`. Uses GraphQL to return the IDs that write tools actually need.

| Parameter | Type | Description |
|---|---|---|
| `owner` | `str` | Repository owner |
| `repo` | `str` | Repository name |
| `pull_number` | `int` | Pull request number |
| `per_page` | `int` | Threads per page (1-100, default 30) |
| `after` | `str?` | Cursor for pagination |

**Returns** for each thread:
- `thread_node_id` — GraphQL ID (`PRRT_…`) for `resolve_review_thread`
- `is_resolved`, `is_outdated`, `is_collapsed`
- `path`, `line`, `start_line`, `diff_side`, `subject_type`

And for each comment:
- `id` — numeric REST ID for `add_reply`, `apply_suggestion`, `add_reaction`
- `node_id` — GraphQL ID
- `has_suggestion` — whether the body contains a `\`\`\`suggestion` block
- `suggestion` — the parsed suggestion content (if present)
- `review.id` — numeric REST ID of the parent review (for `dismiss_review`)

### `apply_suggestion`

Apply a single `\`\`\`suggestion` block from a review comment. Parses the suggestion, modifies the file, and commits to the PR branch.

| Parameter | Type | Description |
|---|---|---|
| `owner` | `str` | Repository owner |
| `repo` | `str` | Repository name |
| `pull_number` | `int` | Pull request number |
| `comment_id` | `int` | Numeric REST ID of the comment with the suggestion |
| `commit_message` | `str?` | Custom commit message |

### `apply_suggestions_batch`

Apply multiple suggestions in a **single commit** using the Git Data API (trees + blobs). Avoids the N suggestions → N commits problem.

| Parameter | Type | Description |
|---|---|---|
| `owner` | `str` | Repository owner |
| `repo` | `str` | Repository name |
| `pull_number` | `int` | Pull request number |
| `comment_ids` | `list[int]` | Numeric REST IDs of comments with suggestions |
| `commit_message` | `str?` | Custom commit message |

Validates that suggestions don't overlap. Applies changes bottom-up within each file to preserve line offsets.

### `dismiss_review`

Dismiss a pull request review (e.g. a "changes requested" review after addressing feedback).

| Parameter | Type | Description |
|---|---|---|
| `owner` | `str` | Repository owner |
| `repo` | `str` | Repository name |
| `pull_number` | `int` | Pull request number |
| `review_id` | `int` | Numeric REST ID of the review (from `get_review_comments → comment.review.id`) |
| `message` | `str` | Reason for dismissing |

### `add_reaction`

React to a review comment with an emoji. Lightweight acknowledgment without a full reply.

| Parameter | Type | Description |
|---|---|---|
| `owner` | `str` | Repository owner |
| `repo` | `str` | Repository name |
| `comment_id` | `int` | Numeric REST ID of the review comment |
| `reaction` | `str` | One of: `+1`, `-1`, `laugh`, `confused`, `heart`, `hooray`, `rocket`, `eyes` |

### `edit_review_comment`

Edit the body of an existing review comment.

| Parameter | Type | Description |
|---|---|---|
| `owner` | `str` | Repository owner |
| `repo` | `str` | Repository name |
| `comment_id` | `int` | Numeric REST ID of the review comment |
| `body` | `str` | New comment body (markdown) |

### `request_reviewers`

Add or remove reviewers (users and/or teams) on a pull request.

| Parameter | Type | Description |
|---|---|---|
| `owner` | `str` | Repository owner |
| `repo` | `str` | Repository name |
| `pull_number` | `int` | Pull request number |
| `action` | `str` | `"add"` or `"remove"` |
| `reviewers` | `list[str]?` | GitHub usernames |
| `team_reviewers` | `list[str]?` | Team slugs |

## How it fits with the standard GitHub MCP

| Action | Standard MCP | This server |
|---|---|---|
| Read review threads | `pull_request_read(method="get_review_comments")` — missing IDs | `get_review_comments` — full IDs + suggestion metadata |
| Reply to comment | `add_reply_to_pull_request_comment` — needs numeric `id` | _(use standard MCP, get `id` from our `get_review_comments`)_ |
| Resolve thread | `resolve_review_thread` — needs `PRRT_…` node_id | _(use standard MCP, get `thread_node_id` from our `get_review_comments`)_ |
| Apply suggestion | not available | `apply_suggestion` / `apply_suggestions_batch` |
| Dismiss review | not available | `dismiss_review` |
| React to comment | not available | `add_reaction` |
| Edit comment | not available | `edit_review_comment` |
| Manage reviewers | not available | `request_reviewers` |

## Typical workflow

```
1. get_review_comments(owner, repo, pull_number)
   → threads with thread_node_id, comment ids, suggestions

2. For each suggestion you want to accept:
   apply_suggestion(owner, repo, pull_number, comment_id)
   — or batch them:
   apply_suggestions_batch(owner, repo, pull_number, [id1, id2, id3])

3. React to acknowledged comments:
   add_reaction(owner, repo, comment_id, "+1")

4. Resolve addressed threads (via standard GitHub MCP):
   resolve_review_thread(thread_node_id)

5. Dismiss the review if all feedback addressed:
   dismiss_review(owner, repo, pull_number, review_id, "All suggestions applied")
```

## Development

```bash
git clone https://github.com/NorthIsUp/github-mcp-extended
cd github-mcp-extended
pip install -e .

# Run directly
GITHUB_TOKEN=ghp_... github-mcp-extensions

# Or via MCP dev server
GITHUB_TOKEN=ghp_... uv run mcp dev src/github_mcp_extensions/server.py
```

## Environment variables

| Variable | Description |
|---|---|
| `GITHUB_TOKEN` | GitHub token (most common, GitHub Actions default) |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | Alternative (matches standard GitHub MCP) |
| `GH_TOKEN` | Alternative (matches gh CLI) |
| `GITHUB_API_URL` | API base URL (default: `https://api.github.com`). Set for GitHub Enterprise. |
