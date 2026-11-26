# Agensi.io listing

Copy for the marketplace listing of `skills-router-mcp`. The package is
`mcp-manage-router-skill.zip`, built from `skills-router-mcp-package/` (see the end of this file).

| Field | Value |
|---|---|
| Logo (512×512) | `skills-router-mcp-logo.png` (source: `docs/assets/skill-logo.svg`) |
| Demo video | `docs/demo/skills-router-mcp-demo.mp4` (render: `docs/demo/render_demo.py`) |
| Categories | Agents & Orchestration (primary), LLM Operations, Developer Tools |

## Summary (max 300 characters)

> Sets up a local MCP router that keeps all your agent skills in one library and loads only the one each task needs. Works with Claude Code, Gemini CLI, Codex, Cursor and opencode. Less context bloat, with backups and one-command restore.

## Compatibility

> Works with Claude Code, Gemini CLI, Antigravity, Codex CLI, opencode and Cursor (set up automatically), plus Claude Desktop and any MCP stdio client (manual setup). Requires Python 3.10+, uv and git. Tested on Linux.

**Agents (set up automatically by `init`)**
- Claude Code: MCP server + automatic search hook
- Gemini CLI and Antigravity: MCP server + global rule
- Codex CLI: MCP server + global rule
- opencode: MCP server + global rule
- Cursor: MCP server; paste one rule into User Rules

**Manual setup**
- Claude Desktop: copy-paste config and rule (included)
- Any other MCP client that runs stdio servers

**Skill format:** standard `SKILL.md` folders with YAML frontmatter (the Agent Skills format).

**Requirements:** Python 3.10+, `uv` and `git`, and network access on first run only.

**OS:** tested on Linux. macOS config paths are supported. Windows is untested.

## Full description

## Stop paying for skills your agent isn't using

Most agents load the name and description of every installed skill into context at the start of every session. With a few dozen skills that's noise; with a few hundred it's tens of thousands of tokens, gone before you type anything, and it grows with every skill you add.

**Skills Router MCP** replaces that catalog with a local MCP server that searches your library on demand. The agent sees five small tools instead of hundreds of descriptions, searches when a task needs a skill, and loads only the skill it picks.

## Measured, not estimated

On a real library of 319 skills:

| Per session, before any skill loads | Native catalog | With the router | Saved |
|---|---:|---:|---:|
| No task needs a skill | ~33,800 tokens | ~530 tokens | 98% |
| Two searches | ~33,800 tokens | ~1,450 tokens | 96% |
| Two searches + 5 pinned skills | ~33,800 tokens | ~2,260 tokens | 93% |

The method, formulas and caveats are in `reference/architecture.md`. For a session with two searches, the router costs less than the native catalog once you have about 14 skills.

## What this skill does

This is an installation and operations guide that your agent follows step by step:

- **Sets up the server** for Claude Code, Gemini CLI, Antigravity, Codex CLI, opencode and Cursor, plus manual steps for Claude Desktop. The release is pinned to an exact commit, so it keeps working offline after the first download.
- **Moves your existing skills** from every client into one shared library (`~/.ai-skills`). It removes identical copies and sets aside same-name skills whose contents differ, for you to choose which one to keep.
- **Makes the agent search on its own**: a prompt hook in Claude Code, rule files for Gemini, Codex and opencode, and copy-paste rules for Cursor and Claude Desktop.
- **Keeps the skills you always want loaded** with `--pin`.
- **Teaches the agent to write metadata that ranks well**: a frontmatter template, a tag taxonomy and a precision check you run on your own requests.
- **Diagnoses problems** with a bundled health check (`scripts/healthcheck.py`) and a troubleshooting table that maps each symptom to its fix.

## Safe by design

- `--dry-run` shows every change before anything is written.
- Every moved file and edited config is backed up first. `skills-router restore` undoes the whole install in one command.
- New skills the agent writes stay as drafts until you approve them. Optionally, approval can be limited to the terminal.
- The health check uses only the Python standard library. It opens no network connection and reads no `.env` files, tokens or SSH keys.

## What's in the package

- `SKILL.md`: the setup, lazy-loading protocol and troubleshooting workflow.
- `reference/client-setup.md`: per-client wiring, manual config snippets, CLI reference and full removal.
- `reference/architecture.md`: the ranking model, metadata template, taxonomy and benchmark.
- `reference/troubleshooting.md`: every installer and server message, with its cause and fix.
- `scripts/healthcheck.py`: JSON diagnostics for the runtime, index, client configs and server startup.

## Requirements

- Python 3.10+, `uv` and `git`.
- Tested on Linux. macOS config paths are supported; Windows is untested.
- Network access on the first run only.

## Good to know

- Search is keyword-based (TF-IDF style), not semantic, so a skill is only as findable as the words in its name, tags and description. The guide shows how to write those well.
- Hooks are stronger than rule files: in rule-based clients the agent searches reliably but not always first.
- The router and this package are also on GitHub, free and source-available (MIT + Commons Clause).

## Rebuilding the package

```bash
rm -f mcp-manage-router-skill.zip
cd skills-router-mcp-package && zip -X -r ../mcp-manage-router-skill.zip skills-router-mcp -x '*__pycache__*' '*.pyc'
```
