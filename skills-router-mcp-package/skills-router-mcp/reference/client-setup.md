# Client Setup Reference

Reference for `SKILL.md` Step 1: where `init` writes per client, manual registration snippets,
`init` flags and steps, server environment variables, the CLI and a full removal. `<url>`,
`/abs/uvx` and the launch options A, A', B and C are defined in SKILL.md 1.2 and 1.5. Ask the
user before editing any file named here.

## Where each client is wired

| Client | Skills folder | MCP registration written by `init` | Activation written by `init` |
|---|---|---|---|
| Claude Code | `~/.claude/skills` | `claude mcp add --scope user skills-router -- <cmd>` (or `~/.claude.json` → `mcpServers`) | hook in `~/.claude/settings.json` |
| Gemini CLI | `~/.gemini/skills` | `~/.gemini/settings.json` → `mcpServers` | rule block in `~/.gemini/GEMINI.md` |
| Antigravity | `~/.gemini/config/skills` | `~/.gemini/config/mcp_config.json` → `mcpServers` | rule block in `~/.gemini/GEMINI.md` |
| Codex CLI | `~/.codex/skills` | `~/.codex/config.toml` → `[mcp_servers.skills-router]` | rule block in `~/.codex/AGENTS.md` |
| opencode | `~/.config/opencode/skills` | `~/.config/opencode/opencode.json` → `mcp` | rule block in `~/.config/opencode/AGENTS.md` |
| Cursor | `~/.cursor/skills` | `~/.cursor/mcp.json` → `mcpServers` | none: manual (SKILL.md 1.6) |
| Claude Desktop | none | none: manual (SKILL.md 1.5) | none: manual (SKILL.md 1.6) |
| Shared `~/.agents/skills` | `~/.agents/skills` (read by agents that use that convention) | none | none |

## Manual registration

Use these when `init` warned (for example `não é JSON puro`), for Claude Desktop, or for project
scope. Ask before editing. Merge into the existing object; never replace the file. Replace
`/abs/uvx` with the output of `command -v uvx` and `<url>` as in SKILL.md 1.2.

Claude Code:

```bash
claude mcp add --scope user skills-router -- /abs/uvx --from <url> skills-router serve
```

Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS,
`~/.config/Claude/claude_desktop_config.json` on Linux), Cursor (`~/.cursor/mcp.json`, or
`.cursor/mcp.json` in a project), Gemini CLI (`~/.gemini/settings.json`) and Antigravity
(`~/.gemini/config/mcp_config.json`):

```json
{
  "mcpServers": {
    "skills-router": {
      "command": "/abs/uvx",
      "args": ["--from", "<url>", "skills-router", "serve"]
    }
  }
}
```

For B use `"command": "/abs/uv"` and
`"args": ["run", "--directory", "/abs/checkout", "skills-router", "serve"]`. For C use the
output of `command -v skills-router` with `"args": ["serve"]`. If the file has comments
(JSONC), keep them and add only the `skills-router` entry.

Codex CLI (`~/.codex/config.toml`):

```toml
[mcp_servers.skills-router]
command = "/abs/uvx"
args = ["--from", "<url>", "skills-router", "serve"]
```

opencode (`~/.config/opencode/opencode.json`):

```json
{ "mcp": { "skills-router": { "type": "local", "enabled": true, "command": ["/abs/uvx", "--from", "<url>", "skills-router", "serve"] } } }
```

Manual registration alone does not install the orchestrator or import existing skills; the
server only creates the empty `~/.ai-skills/skills` and `drafts` on its first start. Run `init`
once (SKILL.md 1.3) for the rest.

## `init` flags and steps

| Flag | Effect |
|---|---|
| `--dry-run` | Report only; change nothing. |
| `--pin NAME` | Keep a skill native in every client (repeatable). Saved in `~/.ai-skills/config.json`. |
| `--clients LIST` | Only these keys: `claude`, `gemini`, `cursor`, `codex`, `opencode`, `antigravity`, `agents`. |
| `--server-command "..."` | Exact command clients run to start the server. Overrides `SKILLS_ROUTER_COMMAND`. |
| `--no-activation` | Skip the hook and rule files of SKILL.md 1.6. The agent then searches only when it decides to. |
| `--expose all` | Replace each client folder with a symlink to `~/.ai-skills/skills` (only folders that held nothing but skills; others stay in pinned mode). Unifies folders but saves no context. |

What `init` does, in order:

1. Creates `~/.ai-skills/{skills,drafts}` and installs `skill-orchestrator`.
2. Imports each client's skills once (identical copies are deduplicated; a same-name skill with
   different content goes to `~/.ai-skills/conflicts/<client>/<name>`).
3. Moves the originals to `~/.ai-skills/backups/<timestamp>/files/` and writes a `manifest.json`.
4. Leaves in each client folder only symlinks to the orchestrator, the `skills-router-mcp` guide and pinned skills.
5. Registers the MCP server in each client config, backing up each file first.
6. Configures automatic activation (SKILL.md 1.6) unless `--no-activation`.
7. Installs bundled skills the user does not have yet (currently `license-designer`) and
   builds `index.json`.

## Server environment

Set these in the client's MCP entry (`"env": {...}` or `claude mcp add -e KEY=VALUE`), because
the client, not the shell, launches the server:

| Variable | Effect |
|---|---|
| `AI_SKILLS_HOME` | Runtime folder instead of `~/.ai-skills`. Set the same value for the CLI. |
| `SKILLS_ROUTER_PROMOTE=cli` | The `promote_draft` tool refuses; promotion only via `skills-router promote NAME` in a terminal. Any value other than `mcp` (case-insensitive) locks it. |
| `SKILLS_ROUTER_COMMAND` | Read by `init` only: server command to register when `--server-command` is absent. |

## CLI reference

Prefix each command as in SKILL.md 1.2: `uvx --from <url>` (A), `uv run` inside the clone (B), nothing (C).

```bash
skills-router init [--dry-run] [--pin NAME]... [--clients LIST] [--server-command CMD] [--no-activation] [--expose pinned|all]
skills-router status              # runtime, indexed count, pins, links per client
skills-router search fastapi postgres --limit 5    # output: <score>  <name>  <description>
skills-router pin NAME... | unpin NAME...
skills-router drafts              # drafts awaiting approval
skills-router promote NAME        # human-run promotion
skills-router reindex             # rebuild index.json, list rejected skills
skills-router restore [--backup YYYYMMDD-HHMMSS] [--dry-run]
skills-router serve               # MCP server over stdio
```

## Full removal

`skills-router restore` (SKILL.md 1.10) does not touch what was added by hand. For a full removal, also:

1. Delete the `skills-router` entries you added in SKILL.md 1.5 (Claude Desktop, project scope, JSONC
   files) and the rule text pasted in Cursor or Claude Desktop (SKILL.md 1.6).
2. Remove the launcher: `uv tool uninstall mcp-manage-router-skill` (C), delete the clone (B),
   or `uv cache clean` (A, optional).
3. `~/.ai-skills/skills` holds the only copy of promoted drafts and of bundled skills such as
   `license-designer`. Copy any the user wants to keep, then delete `~/.ai-skills`.
