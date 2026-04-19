# Basic Memory + Claude Desktop Setup

This guide wires [Basic Memory](https://github.com/basicmachines-co/basic-memory) onto the directory that `content-brain` writes research reports to, then exposes it as an MCP server to Claude Desktop so you can query your brain in chat.

**Prerequisite:** Phase 2 code is merged (the CLI writes to `~/basic-memory/research/` after each research run).

**Not dependent on Basic Memory.** The brain is just markdown on disk. Basic Memory is one possible indexer. You can substitute Obsidian, Logseq, or plain grep — the CLI doesn't care.

---

## 1. Install Basic Memory

Recommended (uv-managed tool install, auto-isolated):

```bash
uv tool install basic-memory
```

Verify:

```bash
basic-memory --version
```

> If you don't have `uv`: `brew install uv` on macOS or `curl -LsSf https://astral.sh/uv/install.sh | sh`.

## 2. Confirm the Vault Path

Basic Memory's default vault is `~/basic-memory/`, which matches `content-brain`'s default `BRAIN_PATH`. Verify the directory exists and has your research:

```bash
ls ~/basic-memory/research/
```

You should see one markdown file per research run, e.g. `2026-04-19-fastapi-dependency-injection-patterns.md`.

If you want the brain somewhere else, set `BRAIN_PATH` in `.env` and point Basic Memory at the same path via a named project (see §7).

## 3. Create the Project + Initial Index

Basic Memory 0.20+ uses named projects. Register `~/basic-memory/` as the default project and build the search index:

```bash
basic-memory project add main ~/basic-memory
basic-memory project default main
echo "y" | basic-memory reset --reindex
```

The `reset --reindex` step drops any stale index state and rebuilds from the markdown files on disk. It also downloads the `bge-small-en-v1.5` embedding model on first run (~90 MB, local, free).

**Ongoing updates happen automatically.** Every CLI research run calls `basic-memory reindex` after writing the new note, so Claude Desktop sees it immediately. To disable this behavior, set `BRAIN_AUTOINDEX=0` in your environment.

## 4. Wire the MCP Server Into Claude Desktop

Edit (or create) the Claude Desktop config:

```bash
~/Library/Application\ Support/Claude/claude_desktop_config.json
```

Add the `basic-memory` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "basic-memory": {
      "command": "uvx",
      "args": ["basic-memory", "mcp"]
    }
  }
}
```

If you already have other MCP servers configured, merge — don't overwrite.

**Restart Claude Desktop.** The MCP connection is established at app startup, not on the fly.

## 5. Verify in Chat

Open Claude Desktop and try:

> What research notes do I have about FastAPI dependency injection?

Claude should call Basic Memory tools, cite the file, and quote content. If it says it has no access, the MCP server isn't connected — check the config JSON for typos and restart the app.

Useful verification prompts:

- `"Find notes about <topic I researched>"`
- `"Summarize the last 3 research reports"`
- `"What sources did I cite in the <slug> note?"`

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| Claude says "no MCP tools available" | Restart Claude Desktop after editing the config. |
| `uvx: command not found` in Claude logs | Install `uv` (see §1). Claude Desktop inherits your shell PATH only if launched from terminal; otherwise you may need to use an absolute path like `/opt/homebrew/bin/uvx` in the config. |
| Notes don't appear in search | Run `basic-memory reindex` (or `basic-memory reset --reindex` if the DB feels stale), then check `basic-memory doctor`. |
| Auto-reindex seems to hang | Set `BRAIN_AUTOINDEX=0` in `.env` and reindex manually on your own cadence. |
| Watch mode stops after reboot | Move it under `launchd` or a process supervisor. Not scripted here — manual for now. |

## 7. Optional: Custom Vault Path

If you don't want `~/basic-memory/`, set your own:

```bash
# .env (content-brain)
BRAIN_PATH=~/work/notes
```

Then create a named Basic Memory project pointing there:

```bash
basic-memory project add my-brain ~/work/notes
basic-memory project default my-brain
```

And update the MCP config to target it:

```json
{
  "mcpServers": {
    "basic-memory": {
      "command": "uvx",
      "args": ["basic-memory", "mcp", "--project", "my-brain"]
    }
  }
}
```

## 8. What You've Built

You now have:

1. **A growing research archive** — every CLI run auto-saves to `~/basic-memory/research/` with frontmatter (topic, date, source URLs, source count, config snapshot).
2. **Semantic search over that archive** via Basic Memory.
3. **Chat-with-your-brain** via Claude Desktop — no custom UI needed.

This satisfies Phase 2's exit criteria. Phase 3 (approval queue) can now read and write to the same brain.
