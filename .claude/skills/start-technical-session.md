---
name: Start Technical Session
description: MANDATORY first action for any coding, debugging, review, refactor, exploration, or "look at my repo" request in this project. Installs and builds the code-review-graph knowledge graph (if missing or stale) and enforces graph-first navigation. ALWAYS invoke this skill at the very start of a technical session before running Grep/Glob/Read, before writing code, before answering "how does X work", before proposing a fix. Even if the user's phrasing is casual ("can you take a look at…", "fix the scraper", "what's wrong with…", "add a feature for…"), trigger this skill. The user has explicitly asked that code-review-graph be the default navigation tool — bypassing it wastes tokens and loses structural context (callers, tests, impact radius) that file scanning cannot recover.
---

## Start Technical Session

This skill runs at the start of every technical session (coding, debugging, reviewing, exploring, refactoring) to make sure the code-review-graph knowledge graph is installed, fresh, and actually consulted BEFORE any keyword search or file read.

Why this matters: the graph understands structure (who calls who, what tests exist, what breaks if you change this) in a way that Grep/Read cannot. Querying it is ~10× cheaper in tokens than scanning files, and it prevents the "I read 500 lines to find 3 relevant lines" failure mode we've been stuck in.

### Phase 1 — Ensure the graph exists and is fresh

Run these three commands in order. If any of them errors, report the error to the user and stop — do not fall back to Grep/Read until the graph is live.

```bash
# 1. Install (idempotent — pip is a no-op if already installed)
pip install code-review-graph --upgrade --quiet

# 2. Auto-configure for this platform (idempotent; writes MCP config if missing)
code-review-graph install

# 3. Build or rebuild the graph for the current working directory
code-review-graph build
```

If the user is on a machine where `pip` is not on PATH, try `python3 -m pip install code-review-graph`. If `pipx install code-review-graph` is the user's preference (check their CLAUDE.md), use that instead.

The build step is the one that takes noticeable time (10–90 s depending on repo size). The install hooks (`code-review-graph install`) also wires a pre-commit hook that auto-updates the graph on every commit, so once the initial build is done the graph stays fresh without further work.

### Phase 2 — Consult the graph FIRST

Before ANY of the following, query the graph:

- Searching for a function, class, or symbol → `semantic_search_nodes` (not Grep)
- Understanding how something is used → `query_graph` with `callers_of` / `callees_of` / `imports_of` / `tests_for` (not Read + inference)
- Understanding impact of a change → `get_impact_radius` (not manual import tracing)
- Reviewing a diff or change → `detect_changes` + `get_review_context` (not reading each modified file end-to-end)
- Orienting in an unfamiliar codebase → `get_architecture_overview` + `list_communities` (not directory listing)
- Checking test coverage → `query_graph` with `tests_for` (not searching tests/)
- Planning a refactor → `refactor_tool` (dead code detection, rename preview)

Only fall back to Grep / Glob / Read when the graph genuinely doesn't cover what's needed (e.g. non-code content like YAML config values, log files, data files).

### Phase 3 — Token-efficient querying

Always pair graph calls with these defaults to avoid blowing context:

1. Start with `get_minimal_context(task="<one-line description of what you're trying to do>")`. This gives a curated subset of the graph scoped to the task — usually enough on its own.
2. Pass `detail_level="minimal"` on every call. Escalate to `"standard"` only when minimal is insufficient.
3. Budget: ≤5 graph tool calls + ≤800 output tokens for any single review/debug/refactor task. If you're about to exceed that, step back and ask the user for clarification — it usually means the task is broader than you thought.

### Phase 4 — When the graph says something surprising

The graph auto-updates on every commit via the installed hook, but uncommitted changes are invisible to it. If a query result conflicts with what you see in the working tree, trust the working tree, and mention the staleness to the user. If the user has uncommitted changes that matter to the task, suggest `code-review-graph build` again before proceeding.

### When NOT to run this skill

- Pure conversation with no code involvement ("what is the capital of France?")
- Non-code file operations (renaming documents, editing .docx, creating a spreadsheet)
- Tasks where the user has explicitly asked you to work without the graph

Everything else — including "just take a quick look at this" — runs this skill first.

### Failure modes

- **"Command not found: code-review-graph"** → pip install failed silently or pip is not on PATH. Try `python3 -m pip install --user code-review-graph` and add `~/.local/bin` to PATH.
- **"Could not build graph"** → usually means no recognizable source files in the CWD. Confirm with the user which directory is the codebase root and rerun `code-review-graph build` from there.
- **MCP tools not appearing after `code-review-graph install`** → Cowork / Claude Code may need a restart to pick up the new MCP server. Tell the user to restart their session, then re-invoke this skill.

### One-shot verification

After Phase 1 runs clean, do a single probe to confirm the MCP is actually live:

```
semantic_search_nodes(query="<pick a term obviously in the codebase, e.g. 'main'>", detail_level="minimal", limit=3)
```

If that returns nodes, the graph is wired and you can proceed. If it errors or returns empty, escalate to the user — don't silently fall back to Grep.
