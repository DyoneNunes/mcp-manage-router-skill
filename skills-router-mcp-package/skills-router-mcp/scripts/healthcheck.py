#!/usr/bin/env python3
"""Read-only health check for the skills-router MCP setup. Prints one JSON report.

Standard library only. No network access. Safe to run repeatedly.

What it reads
  * The Python interpreter version and the PATH lookup for `uv`, `uvx` and `skills-router`.
  * The runtime folder: $AI_SKILLS_HOME, or ~/.ai-skills when unset. That is the only
    environment variable the script reads by name (the standard library also uses HOME
    to expand `~` and PATH to locate executables).
  * SKILL.md files inside <runtime>/skills (frontmatter only, capped at 512 KB each).
  * The skill folders of supported clients (~/.claude/skills, ~/.gemini/skills,
    ~/.cursor/skills, ...) and of the project directory (.claude/skills, .cursor/skills, ...):
    directory listings and symlink targets only.

What it does only when you pass a flag
  --configs   Opens each client's MCP config file and reports whether a `skills-router`
              entry exists and which kind of launcher it uses (installed binary, uvx, uv
              run, ephemeral cache path). Also reports whether automatic activation is in
              place: the `hooks` key of ~/.claude/settings.json and the managed block in
              GEMINI.md / AGENTS.md. Nothing else from those files is printed or kept.
  --probe     Starts the MCP server as a local child process over stdio, runs the MCP
              `initialize` + `tools/list` handshake and stops it. The server may refresh
              <runtime>/index.json on start-up, exactly as it does when a client launches it.
              The script itself opens no socket; if you pass a `uvx --from git+...` command,
              uv may fetch that source, exactly as the client would.

What it never does
  Network calls of its own, reading .env files, credentials, SSH keys or any other
  environment variable, writing or deleting files.

Exit codes: 0 = ok, 1 = warnings, 2 = at least one failed check.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

VERSION = "1.2.0"
SERVER_NAME = "skills-router"
REPO_URL = "git+https://github.com/DyoneNunes/mcp-manage-router-skill"
ORCHESTRATOR = "skill-orchestrator"
EXPECTED_TOOLS = (
    "search_installed_skills",
    "get_skill_content",
    "rebuild_index",
    "propose_skill_draft",
    "promote_draft",
)
MIN_SERVER_PYTHON = (3, 10)
INDEX_VERSION = 1
MAX_SKILL_BYTES = 512 * 1024
MAX_DESCRIPTION = 1024
MAX_EXAMPLES = 15
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
FRONTMATTER_RE = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
KEY_RE = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$")
TAGS_RE = re.compile(r"(?m)^[ \t]*(tags|keywords):[ \t]*\S|^[ \t]*(tags|keywords):[ \t]*\n[ \t]+-")

# (label, skills folder relative to $HOME, MCP config relative to $HOME, config format)
CLIENTS = (
    ("Claude Code", ".claude/skills", ".claude.json", "json:mcpServers"),
    ("Gemini CLI", ".gemini/skills", ".gemini/settings.json", "json:mcpServers"),
    ("Cursor", ".cursor/skills", ".cursor/mcp.json", "json:mcpServers"),
    ("Codex CLI", ".codex/skills", ".codex/config.toml", "toml"),
    ("opencode", ".config/opencode/skills", ".config/opencode/opencode.json", "json:mcp"),
    ("Antigravity", ".gemini/config/skills", ".gemini/config/mcp_config.json", "json:mcpServers"),
    ("Shared ~/.agents", ".agents/skills", None, None),
)
PROJECT_SKILL_DIRS = (".claude/skills", ".cursor/skills", ".gemini/skills", ".agents/skills")
PROJECT_CONFIGS = ((".cursor/mcp.json", "json:mcpServers"), (".mcp.json", "json:mcpServers"),
                   (".gemini/settings.json", "json:mcpServers"))
# Clients without a skills folder, registered by hand (config relative to $HOME).
EXTRA_CONFIGS = (
    ("Claude Desktop", "Library/Application Support/Claude/claude_desktop_config.json", "json:mcpServers"),
    ("Claude Desktop", ".config/Claude/claude_desktop_config.json", "json:mcpServers"),
)
# Automatic activation written by `skills-router init`: (label, client marker, file, trigger).
ACTIVATION_TARGETS = (
    ("Claude Code", ".claude", ".claude/settings.json", "hook"),
    ("Gemini CLI / Antigravity", ".gemini", ".gemini/GEMINI.md", "rules"),
    ("Codex CLI", ".codex", ".codex/AGENTS.md", "rules"),
    ("opencode", ".config/opencode", ".config/opencode/AGENTS.md", "rules"),
)
ACTIVATION_MARK = "[skills-router]"
RULE_START = "<!-- skills-router:start"
FULL_SHA_RE = re.compile(r"@[0-9a-f]{40}$")
TOML_BLOCK_RE = re.compile(rf"(?ms)^\[mcp_servers\.\"?{re.escape(SERVER_NAME)}\"?\][^\n]*\n(.*?)(?=^\[|\Z)")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def tilde(path) -> str:
    """Show paths relative to ~ so the report does not leak the account name."""
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def runtime_home() -> Path:
    custom = os.environ.get("AI_SKILLS_HOME")
    return Path(custom).expanduser() if custom else Path.home() / ".ai-skills"


def check(cid: str, status: str, detail: str, **data) -> dict:
    item = {"id": cid, "status": status, "detail": detail}
    if data:
        item["data"] = data
    return item


def candidates(root: Path):
    """Same discovery rule as the server: <skill>/SKILL.md and <pkg>/skills/<sub>/SKILL.md."""
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        yield entry / "SKILL.md"
        nested = entry / "skills"
        if nested.is_dir():
            try:
                subs = sorted(nested.iterdir())
            except OSError:
                continue
            for sub in subs:
                if sub.is_dir():
                    yield sub / "SKILL.md"


def loose_frontmatter(text: str) -> dict | None:
    """Minimal YAML-ish reader for top-level keys (name, description, tags). None = no block."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return None
    parts: dict = {}
    key = None
    for line in match.group(1).splitlines():
        found = KEY_RE.match(line)
        if found:
            key, value = found.group(1), found.group(2).strip()
            parts[key] = [] if value in (">", ">-", "|", "|-", "") else [value.strip("\"'")]
        elif key and line.startswith((" ", "\t")):
            parts[key].append(line.strip())
    meta = {k: " ".join(v).strip() for k, v in parts.items()}
    meta["_has_tags"] = bool(TAGS_RE.search(match.group(1)))   # top-level or metadata.tags
    return meta


def is_managed(link: Path, runtime: Path) -> bool:
    if not link.is_symlink():
        return False
    target = os.path.abspath(os.path.join(link.parent, os.readlink(link)))
    return target.startswith(str(runtime) + os.sep)


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def check_python() -> dict:
    ver = sys.version_info[:3]
    text = ".".join(map(str, ver))
    if ver[:2] >= MIN_SERVER_PYTHON:
        return check("python", "ok", f"Python {text} satisfies the server requirement (>= 3.10).",
                     version=text, executable=tilde(sys.executable))
    return check("python", "warn",
                 f"Python {text} is older than 3.10. The server needs 3.10+; `uv` can provision it.",
                 version=text, executable=tilde(sys.executable))


def check_tools_on_path() -> list[dict]:
    out = []
    uv = shutil.which("uv")
    out.append(check("uv", "ok" if uv else "warn",
                     f"uv found at {tilde(uv)}." if uv else
                     "uv not found on PATH. Install it from https://docs.astral.sh/uv/ before setup.",
                     path=tilde(uv) if uv else None))
    uvx = shutil.which("uvx")
    out.append(check("uvx", "ok" if uvx else "warn",
                     f"uvx found at {tilde(uvx)} (zero-config launch available)." if uvx else
                     "uvx not found on PATH; it ships with uv. Zero-config launch needs it.",
                     path=tilde(uvx) if uvx else None))
    exe = shutil.which(SERVER_NAME)
    if exe and ("/cache/" in exe or "archive-v" in exe):
        out.append(check("server_binary", "warn",
                         f"{SERVER_NAME} resolves to an ephemeral uv cache path ({tilde(exe)}). "
                         "Never register this path; register `uvx --from <repo> skills-router serve` "
                         "or run `uv tool install`.", path=tilde(exe)))
    elif exe:
        out.append(check("server_binary", "ok", f"{SERVER_NAME} installed at {tilde(exe)}.",
                         path=tilde(exe)))
    else:
        out.append(check("server_binary", "ok" if uv else "warn",
                         f"`{SERVER_NAME}` is not on PATH. Expected when clients launch it with "
                         f"`uvx --from {REPO_URL} skills-router serve` or "
                         "`uv run --directory <checkout> skills-router serve`.",
                         path=None))
    return out


def check_runtime(runtime: Path) -> list[dict]:
    out = []
    skills, drafts = runtime / "skills", runtime / "drafts"
    if not runtime.is_dir():
        return [check("runtime", "fail",
                      f"Runtime folder {tilde(runtime)} does not exist. Run `skills-router init`.",
                      path=tilde(runtime))]
    problems = []
    for folder in (skills, drafts):
        if folder.is_symlink():
            problems.append(f"{folder.name}/ is a symlink (drafts and promotion will be refused)")
        elif not folder.is_dir():
            problems.append(f"{folder.name}/ is missing")
    out.append(check("runtime", "warn" if problems else "ok",
                     "; ".join(problems) if problems else f"Runtime folder {tilde(runtime)} is in place.",
                     path=tilde(runtime)))
    orch = skills / ORCHESTRATOR / "SKILL.md"
    out.append(check("orchestrator", "ok" if orch.is_file() else "fail",
                     "Meta-skill skill-orchestrator is installed." if orch.is_file() else
                     f"{tilde(orch)} is missing. Re-run `skills-router init`.", path=tilde(orch)))
    return out


def check_index(runtime: Path, on_disk: int) -> dict:
    path = runtime / "index.json"
    if not path.is_file():
        return check("index", "warn", "index.json not built yet. It is created on the first search "
                     "or by `skills-router reindex`.", path=tilde(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return check("index", "warn", f"index.json is unreadable ({exc.__class__.__name__}); the "
                     "server rebuilds it automatically on the next call.", path=tilde(path))
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION or not isinstance(data.get("skills"), list):
        return check("index", "warn", "index.json has an unexpected format; it will be rebuilt.",
                     path=tilde(path))
    indexed = len(data["skills"])
    unsafe = data.get("unsafe_ignored") or []
    dups = data.get("duplicates_ignored") or []
    status = "warn" if unsafe or dups else "ok"
    detail = f"{indexed} skills indexed (generated {data.get('generated_at', '?')})."
    if unsafe:
        detail += f" {len(unsafe)} rejected for safety (see skill_lint)."
    if dups:
        detail += f" {len(dups)} duplicate names ignored."
    if on_disk != indexed + len(unsafe) + len(dups):
        detail += (f" Disk has {on_disk} SKILL.md candidates, so the index is stale; it refreshes "
                   "on the next search.")
    return check("index", status, detail, path=tilde(path), indexed=indexed, on_disk=on_disk,
                 unsafe_ignored=len(unsafe), duplicates_ignored=len(dups))


def lint_skills(runtime: Path) -> tuple[dict, int]:
    skills = runtime / "skills"
    real_root = os.path.realpath(skills)
    issues: dict[str, list[str]] = {}
    seen: dict[str, str] = {}
    total = with_tags = 0

    def add(kind: str, path: Path, extra: str = "") -> None:
        issues.setdefault(kind, []).append(tilde(path) + (f" ({extra})" if extra else ""))

    for cand in candidates(skills) if skills.is_dir() else ():
        if not os.path.lexists(cand):
            continue
        total += 1
        real = os.path.realpath(cand)
        if not real.startswith(real_root + os.sep):
            add("symlink_escapes_runtime", cand, "rejected by the server; copy the skill instead")
            continue
        try:
            info = os.stat(real)
        except OSError:
            add("broken_symlink", cand)
            continue
        if not stat.S_ISREG(info.st_mode):
            add("not_a_regular_file", cand)
            continue
        if info.st_nlink > 1:
            add("hardlinked", cand, "rejected by the server; replace with a plain copy")
            continue
        if info.st_size > MAX_SKILL_BYTES:
            add("oversized", cand, f"{info.st_size // 1024} KB; content is truncated at 512 KB")
        with open(real, "rb") as fh:
            text = fh.read(MAX_SKILL_BYTES).decode("utf-8", errors="replace")
        meta = loose_frontmatter(text)
        if meta is None:
            add("no_frontmatter", cand, "folder name and first paragraph are used instead")
            name = cand.parent.name
            if name.lower() in seen:
                add("duplicate_name", cand, f"'{name}' also at {seen[name.lower()]}; only the first is indexed")
            else:
                seen[name.lower()] = tilde(cand)
            continue
        name = (meta.get("name") or cand.parent.name).strip()
        if not meta.get("name"):
            add("missing_name", cand, f"folder name '{cand.parent.name}' is used")
        elif not NAME_RE.match(name):
            add("name_not_kebab_case", cand, name)
        desc = meta.get("description", "")
        if not desc:
            add("missing_description", cand, "first paragraph of the body is used instead")
        elif len(desc) > MAX_DESCRIPTION:
            add("description_truncated", cand, f"{len(desc)} chars > 1024")
        elif len(desc) < 60:
            add("description_too_short", cand, f"{len(desc)} chars; add trigger phrases")
        if meta.get("_has_tags"):
            with_tags += 1
        key = name.lower()
        if key in seen:
            add("duplicate_name", cand, f"'{name}' also at {seen[key]}; only the first is indexed")
        else:
            seen[key] = tilde(cand)

    blocking = {"symlink_escapes_runtime", "broken_symlink", "not_a_regular_file", "hardlinked",
                "duplicate_name"}
    status = "warn" if blocking & issues.keys() else "ok"
    summary = {k: {"count": len(v), "examples": v[:MAX_EXAMPLES]} for k, v in sorted(issues.items())}
    detail = (f"{total} SKILL.md files scanned, {with_tags} declare tags."
              + (f" Issues: {', '.join(f'{k}={len(v)}' for k, v in sorted(issues.items()))}." if issues else
                 " No issues."))
    return check("skill_lint", status, detail, scanned=total, with_tags=with_tags, issues=summary), total


def check_client_dirs(runtime: Path, project: Path) -> dict:
    rows, native_total, missing_orch = [], 0, []
    folders = [(label, Path.home() / rel, "user") for label, rel, _, _ in CLIENTS]
    folders += [(f"project {rel}", project / rel, "project") for rel in PROJECT_SKILL_DIRS]
    for label, folder, scope in folders:
        if not os.path.lexists(folder):
            continue
        row = {"client": label, "path": tilde(folder), "scope": scope}
        if folder.is_symlink():
            row["mode"] = "expose-all symlink" if is_managed(folder, runtime) else "external symlink (left untouched)"
            rows.append(row)
            continue
        managed, native = [], []
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if is_managed(entry, runtime):
                managed.append(entry.name)
            elif (entry / "SKILL.md").is_file():
                native.append(entry.name)
        row.update(linked=managed, native_unrouted=native)
        native_total += len(native)
        if scope == "user" and ORCHESTRATOR not in managed:
            missing_orch.append(label)
        rows.append(row)
    status = "warn" if missing_orch else "ok"
    detail = f"{len(rows)} skill folders found."
    if native_total:
        detail += (f" {native_total} skills still load natively in every session (not routed); "
                   "project-level skills are never indexed by the router.")
    if missing_orch:
        detail += f" skill-orchestrator is not linked for: {', '.join(missing_orch)}."
    return check("client_skill_dirs", status, detail, folders=rows)


def registered_argv(path: Path, fmt: str):
    """argv of the skills-router entry: a list, [] if present but unreadable, False if absent,
    None if the file is unparsable. Only that one entry is inspected."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if fmt == "toml":
        block = TOML_BLOCK_RE.search(text)
        if not block:
            return False
        cmd = re.search(r'(?m)^command[ \t]*=[ \t]*"((?:[^"\\]|\\.)*)"', block.group(1))
        args = re.search(r"(?ms)^args[ \t]*=[ \t]*(\[.*?\])", block.group(1))
        try:
            argv = [json.loads(f'"{cmd.group(1)}"')] if cmd else []
            argv += json.loads(args.group(1)) if args else []
        except ValueError:
            return []
        return [a for a in argv if isinstance(a, str)]
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        return None
    section = data.get(fmt.split(":", 1)[1]) if isinstance(data, dict) else None
    if not isinstance(section, dict) or SERVER_NAME not in section:
        return False
    entry = section[SERVER_NAME] if isinstance(section[SERVER_NAME], dict) else {}
    cmd, args = entry.get("command"), entry.get("args") or []
    argv = list(cmd) if isinstance(cmd, list) else ([cmd] if isinstance(cmd, str) else [])
    argv += args if isinstance(args, list) else []
    return [a for a in argv if isinstance(a, str)]


def launcher(argv: list[str]) -> str:
    """Classify how a client starts the server, without echoing the command itself."""
    if not argv:
        return "unknown"
    if any("archive-v" in a or "/cache/uv/" in a for a in argv):
        return "ephemeral-cache-path"
    base = os.path.basename(argv[0])
    if base == "uvx" or (base == "uv" and argv[1:3] == ["tool", "run"]):
        source = argv[argv.index("--from") + 1] if "--from" in argv[:-1] else ""
        if source.startswith("git+"):
            return "uvx-git-pinned" if FULL_SHA_RE.search(source) else "uvx-git-unpinned"
        return "uvx"
    if base == "uv" and "run" in argv:
        return "uv-run-checkout"
    if base == SERVER_NAME:
        return "installed-binary"
    return "other"


def check_configs(project: Path) -> dict:
    rows, found, broken, notes = [], 0, 0, set()
    targets = [(label, Path.home() / rel, fmt) for label, _, rel, fmt in CLIENTS if rel]
    targets += [(label, Path.home() / rel, fmt) for label, rel, fmt in EXTRA_CONFIGS]
    targets += [(f"project {rel}", project / rel, fmt) for rel, fmt in PROJECT_CONFIGS]
    for label, path, fmt in targets:
        if not path.is_file():
            continue
        argv = registered_argv(path, fmt)
        row = {"client": label, "path": tilde(path)}
        if argv is None:
            row["registered"] = "unparsable (JSONC or invalid)"
        elif argv is False:
            row["registered"] = False
        else:
            found += 1
            kind = launcher(argv)
            row.update(registered=True, launcher=kind,
                       absolute_command=bool(argv) and os.path.isabs(argv[0]))
            if kind == "ephemeral-cache-path":
                broken += 1
            elif kind == "uvx-git-unpinned":
                notes.add("uvx-git-unpinned fetches the repository on every start and fails offline; "
                          "pin a full commit SHA (@<40-char sha>) to reuse the cached build.")
            if argv and not os.path.isabs(argv[0]) and label.startswith(("Claude Desktop", "Cursor")):
                notes.add("GUI clients may not inherit the shell PATH; use an absolute command path.")
        rows.append(row)
    status = "warn" if broken or not found else "ok"
    detail = (f"skills-router is registered in {found} of {len(rows)} config files found." if rows else
              "No client MCP config files found.")
    if broken:
        detail += (f" {broken} entr{'y' if broken == 1 else 'ies'} point to an ephemeral uv cache path "
                   "that breaks when the cache is cleaned; re-register.")
    if notes:
        detail += " " + " ".join(sorted(notes))
    return check("client_configs", status, detail, configs=rows)


def hook_present(path: Path) -> bool | None:
    """Is the skills-router UserPromptSubmit hook in a Claude settings file? Only `hooks` is read."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        return None
    hooks = data.get("hooks") if isinstance(data, dict) else None
    groups = hooks.get("UserPromptSubmit") if isinstance(hooks, dict) else None
    for group in groups if isinstance(groups, list) else []:
        for hook in (group.get("hooks") or []) if isinstance(group, dict) else []:
            if isinstance(hook, dict) and ACTIVATION_MARK in str(hook.get("command", "")):
                return True
    return False


def check_activation() -> dict:
    rows, missing = [], []
    for label, marker, rel, trigger in ACTIVATION_TARGETS:
        if not (Path.home() / marker).exists():
            continue
        path = Path.home() / rel
        if trigger == "hook":
            present = hook_present(path)
        else:
            try:
                present = path.is_file() and RULE_START in path.read_text(encoding="utf-8")
            except OSError:
                present = False
        rows.append({"client": label, "trigger": trigger, "path": tilde(path),
                     "configured": present if present is not None else "unparsable"})
        if present is not True:
            missing.append(label)
    if (Path.home() / ".cursor").exists():
        rows.append({"client": "Cursor", "trigger": "manual: Settings > Rules > User Rules",
                     "configured": "not checkable"})
    detail = (f"Automatic activation is missing for: {', '.join(missing)}. Re-run `skills-router init` "
              "without --no-activation, or add it by hand (SKILL.md 1.6)." if missing else
              "Automatic activation is configured for every detected client that supports it.")
    return check("activation", "warn" if missing else "ok", detail, clients=rows)


def probe_server(cmd: list[str], timeout: float) -> dict:
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
    except OSError as exc:
        return check("mcp_probe", "fail", f"Could not start `{' '.join(cmd)}`: {exc}.")
    lines: queue.Queue = queue.Queue()
    errors: deque = deque(maxlen=8)

    def pump_stdout() -> None:
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)   # EOF: the server exited

    def pump_stderr() -> None:
        for line in proc.stderr:
            errors.append(line.rstrip())

    threading.Thread(target=pump_stdout, daemon=True).start()
    threading.Thread(target=pump_stderr, daemon=True).start()

    def send(message: dict) -> None:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def wait_for(msg_id: int) -> dict | None:
        while True:
            try:
                line = lines.get(timeout=timeout)
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue   # stray non-JSON output on stdout
            if isinstance(msg, dict) and msg.get("id") == msg_id:
                return msg

    def no_answer() -> dict:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        tail = list(errors)[-4:]
        exited = proc.poll() is not None
        detail = ("Server exited before answering `initialize`." if exited else
                  "Server did not answer `initialize` in time (first uvx runs build the package; "
                  "retry with --timeout 90).")
        if any("Git operation failed" in e or "Failed to fetch" in e for e in tail):
            detail += (" uv could not fetch the git source: check the network, or pin a full commit "
                       "SHA so uvx reuses its cached build offline.")
        return check("mcp_probe", "fail", detail, command=[tilde(c) for c in cmd], stderr_tail=tail)

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "skills-router-healthcheck", "version": VERSION}}})
        init = wait_for(1)
        if not init or "result" not in init:
            return no_answer()
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = wait_for(2)
        tools = sorted(t.get("name") for t in ((listed or {}).get("result") or {}).get("tools", []))
        missing = [t for t in EXPECTED_TOOLS if t not in tools]
        server = (init["result"].get("serverInfo") or {})
        return check("mcp_probe", "fail" if missing else "ok",
                     f"Missing tools: {', '.join(missing)}." if missing else
                     f"Handshake ok with {server.get('name', '?')}; all {len(EXPECTED_TOOLS)} tools exposed.",
                     command=[tilde(c) for c in cmd], server=server.get("name"), tools=tools)
    except (BrokenPipeError, OSError) as exc:
        return check("mcp_probe", "fail", f"Server exited during the handshake: {exc}.",
                     command=[tilde(c) for c in cmd], stderr_tail=list(errors)[-4:])
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only diagnostics for the skills-router MCP setup.")
    parser.add_argument("--configs", action="store_true",
                        help="report whether client MCP configs contain a skills-router entry")
    parser.add_argument("--probe", action="store_true",
                        help="start the server locally and run the MCP stdio handshake")
    parser.add_argument("--server-command",
                        help='command used by --probe (default: "skills-router serve" from PATH)')
    parser.add_argument("--project-dir", default=".", help="project root for project-level folders")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds to wait per probe step")
    args = parser.parse_args(argv)

    runtime = runtime_home()
    project = Path(args.project_dir).expanduser().resolve()
    checks = [check_python(), *check_tools_on_path(), *check_runtime(runtime)]
    lint, on_disk = lint_skills(runtime)
    checks += [check_index(runtime, on_disk), lint, check_client_dirs(runtime, project)]
    if args.configs:
        checks.append(check_configs(project))
        checks.append(check_activation())
    if args.probe:
        if args.server_command:
            cmd = shlex.split(args.server_command)
        elif shutil.which(SERVER_NAME):
            cmd = [SERVER_NAME, "serve"]
        else:
            cmd = None
        checks.append(probe_server(cmd, args.timeout) if cmd else check(
            "mcp_probe", "fail", "No server command on PATH. Pass --server-command "
            f'"uvx --from {REPO_URL} skills-router serve" or '
            '"uv run --directory <checkout> skills-router serve".'))

    rank = {"ok": 0, "warn": 1, "fail": 2}
    worst = max((rank[c["status"]] for c in checks), default=0)
    report = {
        "tool": "skills-router-healthcheck",
        "version": VERSION,
        "status": ("ok", "warn", "fail")[worst],
        "runtime": tilde(runtime),
        "summary": {s: sum(c["status"] == s for c in checks) for s in rank},
        "checks": checks,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return worst


if __name__ == "__main__":
    sys.exit(main())
