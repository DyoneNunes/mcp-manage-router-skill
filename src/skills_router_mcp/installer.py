"""CLI `skills-router`: setup portátil, migração com backup, pins e restauração.

Tudo o que é específico da máquina (caminhos, skills fixadas, clientes) é
descoberto em runtime ou gravado em ``~/.ai-skills/config.json`` — nunca no
repositório.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import indexer

SERVER_NAME = "skills-router"
ORCHESTRATOR = "skill-orchestrator"
SELF_SKILL = "skills-router-mcp"   # pacote-guia de instalação: fica nativo para não sumir no meio do setup

# Ativação automática. Uma skill é só uma descrição que o modelo pode ignorar; em teste, só um
# lembrete injetado a cada mensagem (hook) ou uma regra global fez o agente buscar antes de agir.
ACTIVATION_MARK = "[skills-router]"
ACTIVATION_TEXT = (
    f"{ACTIVATION_MARK} If this is a development, architecture, infrastructure or automation task, "
    "your FIRST action must be search_installed_skills (MCP skills-router) with 2-5 English keywords; "
    "load matches with get_skill_content. Skip for small talk."
)
HOOK_COMMAND = "echo " + shlex.quote(ACTIVATION_TEXT)
RULE_START = "<!-- skills-router:start (gerenciado por `skills-router init`; removido por `skills-router restore`) -->"
RULE_END = "<!-- skills-router:end -->"
RULE_BLOCK = (
    f"{RULE_START}\n"
    "## Skills router\n\n"
    "Before any development, architecture, infrastructure or automation task, your first action is\n"
    "`search_installed_skills` (MCP server `skills-router`) with 2-5 English keywords; then load only\n"
    "the matching skills with `get_skill_content`. Skip it for small talk and factual questions.\n"
    f"{RULE_END}\n"
)
_RULE_RE = re.compile(r"\n*<!-- skills-router:start.*?<!-- skills-router:end -->\n?", re.S)


@dataclass(frozen=True)
class Client:
    key: str
    label: str
    markers: tuple[str, ...]          # caminhos (relativos à home) que indicam instalação
    binaries: tuple[str, ...]
    skills_dirs: tuple[str, ...]      # pastas de skills (relativas à home)
    mcp: tuple[str, str] | None       # (formato, arquivo relativo à home)
    activation: tuple[str, str] | None = None   # ("claude_hook" | "rules", arquivo relativo à home)


CLIENTS: tuple[Client, ...] = (
    Client("claude", "Claude Code", (".claude",), ("claude",), (".claude/skills",),
           ("claude", ".claude.json"), ("claude_hook", ".claude/settings.json")),
    Client("gemini", "Gemini CLI", (".gemini",), ("gemini",), (".gemini/skills",),
           ("mcpServers", ".gemini/settings.json"), ("rules", ".gemini/GEMINI.md")),
    Client("cursor", "Cursor", (".cursor",), ("cursor", "cursor-agent"), (".cursor/skills",),
           ("mcpServers", ".cursor/mcp.json")),   # regras globais do Cursor ficam na UI: ativação manual
    Client("codex", "Codex CLI", (".codex",), ("codex",), (".codex/skills",),
           ("codex", ".codex/config.toml"), ("rules", ".codex/AGENTS.md")),
    Client("opencode", "opencode", (".config/opencode",), ("opencode",), (".config/opencode/skills",),
           ("opencode", ".config/opencode/opencode.json"), ("rules", ".config/opencode/AGENTS.md")),
    Client("antigravity", "Antigravity", (".gemini/config/mcp_config.json",), (),
           (".gemini/config/skills",), ("mcpServers", ".gemini/config/mcp_config.json"),
           ("rules", ".gemini/GEMINI.md")),
    Client("agents", "~/.agents (compartilhado)", (".agents/skills",), (), (".agents/skills",), None),
)


def detect_clients(only: list[str] | None = None) -> list[Client]:
    user_home = Path.home()
    found = []
    for client in CLIENTS:
        if only and client.key not in only:
            continue
        if any((user_home / m).exists() for m in client.markers) or any(shutil.which(b) for b in client.binaries):
            found.append(client)
    return found


# --------------------------------------------------------------------------- #
# Config local da máquina
# --------------------------------------------------------------------------- #
def config_path() -> Path:
    return indexer.home() / "config.json"


def load_config() -> dict:
    try:
        cfg = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("pinned", [])
    cfg.setdefault("expose", "pinned")
    return cfg


def template_dir() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in (here / "templates", here.parents[1] / "templates"):
        if (candidate / ORCHESTRATOR / "SKILL.md").is_file():
            return candidate
    raise FileNotFoundError("Template skill-orchestrator não encontrado no pacote.")


def server_command(override: str | None = None) -> list[str]:
    """Comando que os clientes usarão para subir o servidor MCP nesta máquina."""
    override = override or os.environ.get("SKILLS_ROUTER_COMMAND")
    if override:
        return shlex.split(override)
    repo = Path(__file__).resolve().parents[2]
    if (repo / "pyproject.toml").is_file() and (repo / "src").is_dir():  # checkout do repositório
        return [shutil.which("uv") or "uv", "run", "--directory", str(repo), SERVER_NAME, "serve"]
    exe = shutil.which(SERVER_NAME)
    if exe and "/cache/" not in exe and "archive-v" not in exe:  # instalado (uv tool / pip)
        return [exe, "serve"]
    print("! Execução efêmera (uvx): instale com `uv tool install` ou passe --server-command.",
          file=sys.stderr)
    return [exe or SERVER_NAME, "serve"]


# --------------------------------------------------------------------------- #
# Operações com dry-run e manifesto de backup
# --------------------------------------------------------------------------- #
class Ops:
    def __init__(self, dry: bool):
        self.dry = dry
        stamp = time.strftime("%Y%m%d-%H%M%S")
        root, n = indexer.home() / "backups", 1
        self.backup_root = root / stamp
        while os.path.lexists(self.backup_root):   # dois inits no mesmo segundo não dividem a pasta
            n += 1
            self.backup_root = root / f"{stamp}-{n}"
        self.manifest: dict = {"moves": [], "skills_dirs": [], "registered": [], "config_backups": [],
                               "activation": []}
        self.warnings: list[str] = []

    def log(self, msg: str) -> None:
        print(("[dry-run] " if self.dry else "") + msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"! {msg}")

    def _backup_path(self, original: Path) -> Path:
        try:
            rel = original.relative_to(Path.home())
        except ValueError:
            rel = Path(*original.parts[1:])
        return self.backup_root / "files" / rel

    def _secure_backup_root(self) -> None:
        """Backups guardam cópias de configs com credenciais: só o dono entra."""
        for folder in (self.backup_root.parent, self.backup_root):
            folder.mkdir(parents=True, exist_ok=True)
            os.chmod(folder, 0o700)

    def mkdir(self, path: Path) -> None:
        if not path.is_dir() and not self.dry:
            path.mkdir(parents=True, exist_ok=True)

    def copytree(self, src: Path, dst: Path) -> None:
        if not self.dry:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst, symlinks=True)

    def move_to_backup(self, original: Path) -> None:
        dest = self._backup_path(original)
        if not self.dry:
            self._secure_backup_root()
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(original), str(dest))
        self.manifest["moves"].append({"from": str(original), "to": str(dest)})

    def backup_file(self, original: Path) -> None:
        if not original.is_file():
            return
        dest = self._backup_path(original)
        if not self.dry:
            self._secure_backup_root()
            dest.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(dest):
                os.unlink(dest)
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(original.read_bytes())
        self.manifest["config_backups"].append({"from": str(original), "to": str(dest)})

    def save_manifest(self) -> None:
        touched = any(self.manifest[k] for k in ("moves", "registered", "config_backups", "activation"))
        if self.dry or not touched:
            return
        self._secure_backup_root()
        (self.backup_root / "manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=1), encoding="utf-8")


def _link_target(link: Path) -> Path:
    return Path(os.path.abspath(os.path.join(link.parent, os.readlink(link))))


def _is_managed(link: Path) -> bool:
    """Symlink criado por nós: aponta para dentro de ~/.ai-skills."""
    if not link.is_symlink():
        return False
    root = str(indexer.home()) + os.sep
    return str(_link_target(link)).startswith(root)


_digests: dict[str, str] = {}


def tree_digest(path: Path) -> str:
    key = str(path)
    if key not in _digests:
        h = hashlib.sha256()
        for f in sorted(path.rglob("*")):
            rel = f.relative_to(path)
            if ".git" in rel.parts or "__pycache__" in rel.parts:
                continue
            if f.is_symlink():
                h.update(f"{rel}\0->{os.readlink(f)}\n".encode())
            elif f.is_file():
                h.update(f"{rel}\0".encode())
                h.update(f.read_bytes())
        _digests[key] = h.hexdigest()
    return _digests[key]


# --------------------------------------------------------------------------- #
# Migração: importa skills dos clientes para a fonte única e guarda os originais
# --------------------------------------------------------------------------- #
def migrate(ops: Ops, clients: list[Client]) -> tuple[dict, set[str]]:
    """Devolve (estatísticas, pendentes): nomes importados + caminhos movidos (p/ o dry-run)."""
    central = indexer.skills_dir()
    stats = {"imported": 0, "duplicates": 0, "aliases": 0, "dangling": 0, "kept": [], "conflicts": []}
    known: dict[str, Path] = {}       # nome -> origem já importada nesta execução
    to_move: list[Path] = []

    for client in clients:
        for rel in client.skills_dirs:
            folder = Path.home() / rel
            if folder.is_symlink():
                if not _is_managed(folder):
                    ops.warn(f"{folder} é um symlink externo; não foi alterado.")
                continue
            if not folder.is_dir():
                continue
            for entry in sorted(folder.iterdir()):
                if entry.name.startswith(".") or _is_managed(entry):
                    continue
                target = Path(os.path.realpath(entry))
                if not (target / "SKILL.md").is_file():
                    if entry.is_symlink() and not target.exists():
                        stats["dangling"] += 1
                        to_move.append(entry)
                    else:
                        stats["kept"].append(str(entry))   # não é skill: fica onde está
                    continue
                nested = target.parent.name == "skills" and (target.parent.parent / "SKILL.md").is_file()
                if entry.is_symlink() and nested:
                    stats["aliases"] += 1                  # atalho p/ sub-skill de um pacote
                    to_move.append(entry)
                    continue
                dest = central / entry.name
                previous = known.get(entry.name) or (dest if dest.is_dir() else None)
                if previous is None:
                    ops.copytree(target, dest)
                    known[entry.name] = target
                    stats["imported"] += 1
                elif previous == target or tree_digest(previous) == tree_digest(target):
                    stats["duplicates"] += 1
                else:
                    keep = indexer.home() / "conflicts" / client.key / entry.name
                    if not keep.exists():
                        ops.copytree(target, keep)
                    stats["conflicts"].append({"name": entry.name, "kept": str(previous), "other": str(keep)})
                    ops.warn(f"Conflito em '{entry.name}': mantida a 1ª versão; a de {client.label} foi para {keep}")
                to_move.append(entry)

    for entry in to_move:
        ops.move_to_backup(entry)
    ops.log(f"Skills importadas: {stats['imported']} | duplicadas idênticas: {stats['duplicates']} | "
            f"atalhos de pacote: {stats['aliases']} | links quebrados: {stats['dangling']} | "
            f"conflitos: {len(stats['conflicts'])}")
    if to_move:
        ops.log(f"Originais movidos para backup: {len(to_move)} -> {ops.backup_root}")
    for kept in stats["kept"]:
        ops.log(f"Mantido no lugar (não é skill): {kept}")
    return stats, set(known) | {str(p) for p in to_move}


# --------------------------------------------------------------------------- #
# Exposição: o que cada cliente enxerga nativamente
# --------------------------------------------------------------------------- #
def _resolve_skill_dir(name: str, pending: set[str]) -> Path | None:
    direct = indexer.skills_dir() / name
    if name in pending:               # dry-run: ainda vai ser importada
        return direct
    if os.path.lexists(direct / "SKILL.md"):
        try:
            indexer.resolve_skill_file(direct / "SKILL.md")
        except ValueError:
            return None               # recusada pelo indexador: não expor aos clientes
        return direct
    data = indexer.load_index()
    entry = indexer.find_skill(name, data) if data else None
    if entry is None:
        return None
    try:   # o índice é um arquivo local: nunca criar link para fora do diretório oficial
        return indexer.resolve_skill_file(entry["path"]).parent
    except ValueError:
        return None


def sync_links(ops: Ops, clients: list[Client], cfg: dict, pending: set[str] = frozenset()) -> None:
    central = indexer.skills_dir()
    exposed = list(dict.fromkeys([ORCHESTRATOR, *cfg["pinned"]]))
    for client in clients:
        for rel in client.skills_dirs:
            folder = Path.home() / rel
            if rel not in ops.manifest["skills_dirs"]:
                ops.manifest["skills_dirs"].append(rel)

            if cfg["expose"] == "all":
                if folder.is_symlink() and _is_managed(folder):
                    continue
                leftovers = list(folder.iterdir()) if folder.is_dir() else []
                leftovers = [p for p in leftovers if not _is_managed(p)]
                if not leftovers and not folder.is_symlink():
                    ops.log(f"{client.label}: {folder} -> {central}")
                    if not ops.dry:
                        if folder.is_dir():
                            shutil.rmtree(folder)
                        folder.parent.mkdir(parents=True, exist_ok=True)
                        folder.symlink_to(central, target_is_directory=True)
                    continue
                ops.warn(f"{folder} tem itens que não são skills; usando modo 'pinned' nessa pasta.")

            if folder.is_symlink():
                continue
            ops.mkdir(folder)
            wanted = {}
            for name in exposed:
                target = _resolve_skill_dir(name, pending | {ORCHESTRATOR})
                if target is None:
                    ops.warn(f"Skill fixada '{name}' não existe em {central}.")
                else:
                    wanted[name] = target
            if folder.is_dir():   # remove links nossos que deixaram de estar fixados
                for entry in folder.iterdir():
                    if _is_managed(entry) and entry.name not in wanted:
                        ops.log(f"{client.label}: remove link {entry.name}")
                        if not ops.dry:
                            entry.unlink()
            for name, target in wanted.items():
                link = folder / name
                if link.is_symlink() and _link_target(link) == target:
                    continue
                if link.is_symlink() and _is_managed(link):
                    if not ops.dry:
                        link.unlink()
                elif os.path.lexists(link) and not (ops.dry and str(link) in pending):
                    ops.warn(f"{link} já existe e não é gerenciado; link não criado.")
                    continue
                if not ops.dry:
                    link.symlink_to(target, target_is_directory=True)
            ops.log(f"{client.label}: {rel} expõe {', '.join(wanted)}")


# --------------------------------------------------------------------------- #
# Registro do servidor MCP nas configs globais
# --------------------------------------------------------------------------- #
def _write_text(path: Path, text: str) -> None:
    path = Path(os.path.realpath(path))   # config que é symlink (dotfiles): mantém o link
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    # Temporário exclusivo e 0600: configs de clientes costumam guardar tokens de outros servidores.
    indexer.atomic_write(path, text, mode)


def _load_json_dict(ops: Ops, path: Path, hint: str) -> dict | None:
    """Conteúdo de um JSON de config ({} se ausente/vazio), ou None (com aviso) se não for JSON puro."""
    if not (path.is_file() and path.read_text(encoding="utf-8").strip()):
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        ops.warn(f"{path} não é JSON puro; {hint}")
        return None
    if not isinstance(data, dict):
        ops.warn(f"{path} não é um objeto JSON; {hint}")
        return None
    return data


def _edit_json(ops: Ops, path: Path, section: str, value: dict | None) -> bool | None:
    """Define (ou remove, se value=None) section[SERVER_NAME] num JSON.

    True se mudou, False se já estava assim, None se não deu para editar (aviso já emitido).
    """
    data = _load_json_dict(ops, path, "registre o servidor manualmente.")
    if data is None:
        return None
    servers = data.get(section)
    if not isinstance(servers, dict):
        servers = {}
    if value is None:
        if SERVER_NAME not in servers:
            return False
        del servers[SERVER_NAME]
    else:
        if servers.get(SERVER_NAME) == value:
            return False
        servers[SERVER_NAME] = value
    data[section] = servers
    if not ops.dry:
        if value is not None:
            ops.backup_file(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return True


_CODEX_BLOCK = re.compile(rf"(?ms)^\[mcp_servers\.{re.escape(SERVER_NAME)}\][^\n]*\n.*?(?=^\[|\Z)")


def _edit_codex(ops: Ops, path: Path, cmd: list[str] | None) -> bool:
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    stripped = _CODEX_BLOCK.sub("", text)
    new = stripped
    if cmd is not None:
        block = (f"[mcp_servers.{SERVER_NAME}]\ncommand = {json.dumps(cmd[0])}\n"
                 f"args = {json.dumps(cmd[1:])}\n")
        new = stripped.rstrip("\n") + ("\n\n" if stripped.strip() else "") + block
    if new == text:
        return False
    if not ops.dry:
        if cmd is not None:
            ops.backup_file(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text(path, new)
    return True


def _edit_claude(ops: Ops, path: Path, cmd: list[str] | None) -> bool | None:
    """Prefere a CLI oficial (o ~/.claude.json é regravado pelo Claude Code em uso)."""
    exe = shutil.which("claude")
    if not exe:
        value = None if cmd is None else {"type": "stdio", "command": cmd[0], "args": cmd[1:]}
        return _edit_json(ops, path, "mcpServers", value)
    if ops.dry:
        return True
    subprocess.run([exe, "mcp", "remove", "--scope", "user", SERVER_NAME],
                   capture_output=True, text=True, timeout=60)
    if cmd is None:
        return True
    ops.backup_file(path)
    done = subprocess.run([exe, "mcp", "add", "--scope", "user", SERVER_NAME, "--", *cmd],
                          capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        ops.warn(f"`claude mcp add` falhou: {(done.stderr or done.stdout).strip()[:200]}")
        return None
    return True


def set_registration(ops: Ops, kind: str, path: Path, cmd: list[str] | None) -> bool | None:
    if kind == "claude":
        return _edit_claude(ops, path, cmd)
    if kind == "codex":
        return _edit_codex(ops, path, cmd)
    if kind == "opencode":
        value = None if cmd is None else {"type": "local", "command": cmd, "enabled": True}
        return _edit_json(ops, path, "mcp", value)
    value = None if cmd is None else {"command": cmd[0], "args": cmd[1:]}
    return _edit_json(ops, path, "mcpServers", value)


def register_all(ops: Ops, clients: list[Client], cmd: list[str]) -> None:
    for client in clients:
        if client.mcp is None:
            continue
        kind, rel = client.mcp
        path = Path.home() / rel
        try:
            changed = set_registration(ops, kind, path, cmd)
        except (OSError, subprocess.SubprocessError) as exc:
            ops.warn(f"{client.label}: falha ao registrar MCP em {path}: {exc}")
            continue
        if changed is None:   # não editável (JSONC, CLI falhou): o aviso já explicou
            continue
        if changed:
            ops.manifest["registered"].append({"client": client.key, "kind": kind, "path": str(path)})
        ops.log(f"{client.label}: MCP '{SERVER_NAME}' {'registrado' if changed else 'já estava registrado'} em ~/{rel}")


# --------------------------------------------------------------------------- #
# Ativação automática: hook (Claude Code) ou bloco de regra global (demais clientes)
# --------------------------------------------------------------------------- #
def _is_our_hook_group(group) -> bool:
    hooks = group.get("hooks") if isinstance(group, dict) else None
    return isinstance(hooks, list) and any(
        isinstance(h, dict) and ACTIVATION_MARK in str(h.get("command", "")) for h in hooks)


def _edit_claude_hook(ops: Ops, path: Path, enable: bool) -> bool | None:
    """Liga/desliga o hook UserPromptSubmit em ~/.claude/settings.json sem tocar nos demais."""
    data = _load_json_dict(ops, path, "adicione o hook de ativação manualmente (veja o SKILL.md).")
    if data is None:
        return None
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        ops.warn(f"{path}: 'hooks' tem formato inesperado; adicione o hook manualmente.")
        return None
    groups = hooks.get("UserPromptSubmit", [])
    if not isinstance(groups, list):
        ops.warn(f"{path}: 'hooks.UserPromptSubmit' tem formato inesperado; adicione o hook manualmente.")
        return None
    new = [g for g in groups if not _is_our_hook_group(g)]
    if enable:
        new.append({"hooks": [{"type": "command", "command": HOOK_COMMAND}]})
    if new == groups:
        return False
    if new:
        hooks["UserPromptSubmit"] = new
    else:
        hooks.pop("UserPromptSubmit", None)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    if not ops.dry:
        if enable:
            ops.backup_file(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return True


def _edit_rules(ops: Ops, path: Path, enable: bool) -> bool:
    """Acrescenta/remove o bloco gerenciado num arquivo de regras global (GEMINI.md, AGENTS.md)."""
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    found = _RULE_RE.search(text)
    if not enable:
        if not found:
            return False           # arquivo do usuário sem bloco nosso: nunca é reescrito
        new = _RULE_RE.sub("\n", text).strip("\n")
        new = new + "\n" if new else ""
    else:
        if found and found.group(0).strip("\n") == RULE_BLOCK.strip("\n"):
            return False
        base = (_RULE_RE.sub("\n", text) if found else text).rstrip("\n")
        new = base + ("\n\n" if base else "") + RULE_BLOCK
    if not ops.dry:
        if enable:
            ops.backup_file(path)
        if not new.strip():
            path.unlink(missing_ok=True)   # o arquivo só tinha o nosso bloco
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_text(path, new)
    return True


def set_activation(ops: Ops, kind: str, path: Path, enable: bool) -> bool | None:
    if kind == "claude_hook":
        return _edit_claude_hook(ops, path, enable)
    return _edit_rules(ops, path, enable)


def activation_targets(clients) -> list[tuple[Client, str, Path]]:
    """(cliente, tipo, arquivo) sem repetir arquivo (Gemini CLI e Antigravity dividem o GEMINI.md)."""
    seen, out = set(), []
    for client in clients:
        if client.activation is None:
            continue
        kind, rel = client.activation
        path = Path.home() / rel
        if path not in seen:
            seen.add(path)
            out.append((client, kind, path))
    return out


def activate_all(ops: Ops, clients: list[Client]) -> None:
    for client, kind, path in activation_targets(clients):
        try:
            changed = set_activation(ops, kind, path, True)
        except OSError as exc:
            ops.warn(f"{client.label}: falha ao configurar a ativação em {path}: {exc}")
            continue
        if changed is None:
            continue
        if changed:
            ops.manifest["activation"].append({"client": client.key, "kind": kind, "path": str(path)})
        what = "hook UserPromptSubmit" if kind == "claude_hook" else "regra global"
        rel = path.relative_to(Path.home())
        ops.log(f"{client.label}: ativação automática ({what}) {'configurada' if changed else 'já estava configurada'} em ~/{rel}")
    if any(c.key == "cursor" for c in clients):
        ops.warn("Cursor: as regras globais ficam na interface. Para a ativação automática, cole a regra "
                 "do SKILL.md em Settings → Rules → User Rules.")


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #
def cmd_init(args) -> int:
    ops = Ops(args.dry_run)
    cfg = load_config()
    if args.pin:
        cfg["pinned"] = list(dict.fromkeys([*cfg["pinned"], *args.pin]))
    if args.expose:
        cfg["expose"] = args.expose
    clients = detect_clients(args.clients.split(",") if args.clients else None)
    cfg["clients"] = [c.key for c in clients]

    ops.log(f"Runtime: {indexer.home()}")
    ops.log(f"Clientes detectados: {', '.join(c.label for c in clients) or 'nenhum'}")
    ops.mkdir(indexer.skills_dir())
    ops.mkdir(indexer.drafts_dir())

    source = template_dir() / ORCHESTRATOR / "SKILL.md"
    dest = indexer.skills_dir() / ORCHESTRATOR / "SKILL.md"
    if not dest.is_file() or dest.read_bytes() != source.read_bytes():
        ops.log(f"Instala a meta-skill em {dest}")
        if not ops.dry:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)

    _, pending = migrate(ops, clients)
    # O pacote-guia (skills-router-mcp) acabou de ser importado: sem o pin ele sumiria da pasta do
    # cliente no meio do setup e os caminhos relativos dele quebrariam. Só na primeira vez, para
    # respeitar um `unpin` posterior.
    self_present = SELF_SKILL in pending or (indexer.skills_dir() / SELF_SKILL / "SKILL.md").is_file()
    if self_present and SELF_SKILL not in cfg["pinned"] and not cfg.get("self_pinned"):
        cfg["pinned"].append(SELF_SKILL)
        cfg["self_pinned"] = True
        ops.log(f"Mantém '{SELF_SKILL}' nativa para o guia de instalação continuar acessível "
                f"(depois do setup: `skills-router unpin {SELF_SKILL}`).")
    # Skills extras que acompanham o pacote: só entram se ainda não existirem,
    # para nunca sobrescrever uma versão que o usuário já tenha ou tenha editado.
    for extra in sorted(template_dir().iterdir()):
        target = indexer.skills_dir() / extra.name
        if extra.name == ORCHESTRATOR or not (extra / "SKILL.md").is_file():
            continue
        if not target.exists() and extra.name not in pending:
            ops.log(f"Instala a skill incluída '{extra.name}'")
            ops.copytree(extra, target)
    if not ops.dry:
        indexer.build_index()
    sync_links(ops, clients, cfg, pending)
    cmd = server_command(args.server_command)
    ops.log(f"Comando do servidor MCP: {' '.join(cmd)}")
    register_all(ops, clients, cmd)
    if args.no_activation:
        ops.log("Ativação automática não configurada (--no-activation).")
    else:
        activate_all(ops, clients)

    if not ops.dry:
        config_path().write_text(json.dumps(cfg, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        data = indexer.build_index()
        ops.save_manifest()
        ops.log(f"Índice: {len(data['skills'])} skills em {indexer.index_path()}")
        for item in data.get("unsafe_ignored", []):
            ops.warn(f"Skill NÃO indexada ({item['reason']}): {item['path']}")
    if ops.warnings:
        print(f"\n{len(ops.warnings)} aviso(s) acima merecem revisão.")
    print("\nNada foi alterado (dry-run)." if ops.dry else "\nPronto. Reinicie os clientes para carregar o MCP.")
    return 0


def _resync(cfg: dict) -> None:
    config_path().write_text(json.dumps(cfg, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    indexer.ensure_index()
    sync_links(Ops(False), detect_clients(cfg.get("clients")), cfg)


def cmd_pin(args) -> int:
    cfg = load_config()
    for name in args.names:
        if _resolve_skill_dir(name, set()) is None:
            print(f"! Skill '{name}' não encontrada ou recusada por segurança; ignorada.")
        elif name not in cfg["pinned"]:
            cfg["pinned"].append(name)
    _resync(cfg)
    return 0


def cmd_unpin(args) -> int:
    cfg = load_config()
    cfg["pinned"] = [n for n in cfg["pinned"] if n not in args.names]
    _resync(cfg)
    return 0


def _restore_manifest(ops: Ops, manifest_path: Path, root: Path) -> int | None:
    """Desfaz um manifesto. Devolve itens devolvidos ao lugar, ou None se o backup foi recusado."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = None
    lists = ("skills_dirs", "registered", "moves")
    if not isinstance(manifest, dict) or not all(isinstance(manifest.get(k, []), list) for k in lists):
        ops.warn(f"Manifesto inválido, ignorado: {manifest_path}")
        return None
    backup_dir = manifest_path.parent
    # O backup tem de ser uma pasta real, direto em ~/.ai-skills/backups, com `files/` real.
    real_root = os.path.realpath(root)
    for folder in (backup_dir, backup_dir / "files"):
        expected = os.path.join(real_root, os.path.relpath(folder, root))
        if folder.is_symlink() or (folder.exists() and os.path.realpath(folder) != expected):
            ops.warn(f"Backup recusado: {folder} é um symlink ou aponta para fora de {root}.")
            return None
    ops.log(f"Restaurando a partir de {backup_dir}")

    # O manifesto é um arquivo local editável: só aceitamos o que o init poderia ter gravado.
    known_dirs = {rel for c in CLIENTS for rel in c.skills_dirs}
    known_mcp = {(c.mcp[0], str(Path.home() / c.mcp[1])) for c in CLIENTS if c.mcp}
    real_backup = os.path.realpath(backup_dir / "files")
    client_dirs = {str(Path.home() / rel) for rel in known_dirs}

    def valid_move(move) -> bool:
        """O init só move filhos diretos das pastas de skills dos clientes para o backup."""
        if not isinstance(move, dict) or not all(isinstance(move.get(k), str) for k in ("from", "to")):
            return False
        src, dst = move["to"], move["from"]
        if os.path.abspath(src) != src or os.path.abspath(dst) != dst:
            return False
        if os.path.dirname(dst) not in client_dirs or os.path.islink(os.path.dirname(dst)):
            return False                                        # nunca gravar através de symlink
        real_parent = os.path.realpath(os.path.dirname(src))   # pega symlink dentro do backup
        return real_parent == real_backup or real_parent.startswith(real_backup + os.sep)

    for rel in manifest.get("skills_dirs", []):
        if not isinstance(rel, str) or rel not in known_dirs:
            ops.warn(f"Pasta de skills desconhecida no manifesto, ignorada: {rel}")
            continue
        folder = Path.home() / rel
        if folder.is_symlink() and _is_managed(folder):
            ops.log(f"Remove link {folder}")
            if not ops.dry:
                folder.unlink()
        elif folder.is_dir():
            for entry in folder.iterdir():
                if _is_managed(entry):
                    if not ops.dry:
                        entry.unlink()
    for reg in manifest.get("registered", []):
        valid = isinstance(reg, dict) and isinstance(reg.get("kind"), str) and isinstance(reg.get("path"), str)
        if not valid or (reg["kind"], reg["path"]) not in known_mcp:
            ops.warn(f"Registro MCP desconhecido no manifesto, ignorado: {reg!r:.80}")
            continue
        if set_registration(ops, reg["kind"], Path(reg["path"]), None):
            ops.log(f"MCP removido de {reg['path']}")
    restored = 0
    for move in reversed(manifest.get("moves", [])):
        if not valid_move(move):
            ops.warn(f"Entrada de manifesto fora do padrão do init, ignorada: {move!r:.100}")
            continue
        src, dst = Path(move["to"]), Path(move["from"])
        if os.path.lexists(dst) or not os.path.lexists(src):
            ops.warn(f"Não restaurado (destino ocupado ou backup ausente): {dst}")
            continue
        if not ops.dry:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        restored += 1
    if not ops.dry:   # um manifesto desfeito não é desfeito de novo num próximo restore
        manifest_path.rename(manifest_path.with_name("manifest.restored.json"))
    return restored


def cmd_restore(args) -> int:
    """Sem --backup, desfaz TODOS os inits (do mais novo ao mais antigo): rodar o init de novo
    para corrigir algo não pode prender no backup o que o primeiro init moveu."""
    root = indexer.home() / "backups"
    if args.backup:
        candidates = [root / args.backup / "manifest.json"]
    else:
        candidates = sorted((p for p in root.glob("*/manifest.json")), reverse=True) if root.is_dir() else []
    candidates = [c for c in candidates if c.is_file()]
    if not candidates:
        print("Nenhum backup com manifesto pendente encontrado.")
        return 1
    ops = Ops(args.dry_run)
    restored = 0
    for manifest_path in candidates:
        result = _restore_manifest(ops, manifest_path, root)
        restored += result or 0
    # A ativação é identificada pelo marcador, então sai de todos os alvos conhecidos.
    for client, kind, path in activation_targets(CLIENTS):
        try:
            if set_activation(ops, kind, path, False):
                ops.log(f"Ativação automática removida de {path}")
        except OSError as exc:
            ops.warn(f"{client.label}: falha ao remover a ativação de {path}: {exc}")
    ops.log(f"Itens devolvidos ao lugar original: {restored}")
    print(f"As cópias em {indexer.skills_dir()} foram mantidas.")
    return 0


def cmd_status(_args) -> int:
    cfg = load_config()
    data, _ = indexer.ensure_index()
    drafts = [p.name for p in indexer.drafts_dir().iterdir()] if indexer.drafts_dir().is_dir() else []
    print(f"Runtime:  {indexer.home()}")
    print(f"Dono:     {indexer.owner()}  (lido da configuração local)")
    print(f"Skills:   {len(data['skills'])} indexadas | rascunhos: {len(drafts)}")
    print(f"Fixadas:  {', '.join(cfg['pinned']) or '-'}  (modo: {cfg['expose']})")
    for client in detect_clients():
        for rel in client.skills_dirs:
            folder = Path.home() / rel
            links = sorted(p.name for p in folder.iterdir() if _is_managed(p)) if folder.is_dir() else []
            print(f"  {client.label:<26} ~/{rel}: {', '.join(links) or '(sem links)'}")
    return 0


def cmd_search(args) -> int:
    data, _ = indexer.ensure_index()
    for hit in indexer.search(" ".join(args.query), data["skills"], args.limit):
        print(f"{hit['score']:.3f}  {hit['name']:<40} {hit['description'][:90]}")
    return 0


def cmd_reindex(_args) -> int:
    data = indexer.build_index()
    print(f"{len(data['skills'])} skills indexadas em {indexer.index_path()}")
    for item in data.get("unsafe_ignored", []):
        print(f"! Skill NÃO indexada ({item['reason']}): {item['path']}")
    return 0


def cmd_drafts(_args) -> int:
    folder = indexer.drafts_dir()
    names = sorted(p.name for p in folder.iterdir() if (p / "SKILL.md").is_file()) if folder.is_dir() else []
    print("\n".join(names) if names else "Nenhum rascunho aguardando aprovação.")
    return 0


def cmd_promote(args) -> int:
    from .server import promote
    try:
        result = promote(indexer.validate_name(args.name))
    except ValueError as exc:
        print(f"! {exc}")
        return 1
    print(f"Promovida: {result['path']}")
    return 0


def cmd_serve(_args) -> int:
    from .server import main as serve
    serve()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skills-router", description="MCP Server + Meta-Skill de roteamento de skills.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="cria ~/.ai-skills, migra skills (com backup), cria links e registra o MCP")
    p.add_argument("--dry-run", action="store_true", help="mostra o que seria feito, sem alterar nada")
    p.add_argument("--pin", action="append", default=[], metavar="SKILL", help="skill que continua nativa nos clientes (repetível)")
    p.add_argument("--expose", choices=("pinned", "all"), help="pinned: só orquestrador + fixadas (padrão); all: pasta inteira via symlink")
    p.add_argument("--clients", help="lista separada por vírgula: " + ",".join(c.key for c in CLIENTS))
    p.add_argument("--server-command", help="comando completo para subir o servidor MCP")
    p.add_argument("--no-activation", action="store_true",
                   help="não instala o hook (Claude Code) nem as regras globais que fazem o agente buscar sozinho")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("serve", help="sobe o servidor MCP (stdio)")
    p.set_defaults(func=cmd_serve)
    p = sub.add_parser("status", help="resumo do ambiente local")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("search", help="testa a busca pelo terminal")
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_search)
    p = sub.add_parser("reindex", help="reconstrói o index.json")
    p.set_defaults(func=cmd_reindex)
    p = sub.add_parser("pin", help="expõe skills nativamente nos clientes")
    p.add_argument("names", nargs="+")
    p.set_defaults(func=cmd_pin)
    p = sub.add_parser("unpin", help="deixa de expor skills nativamente")
    p.add_argument("names", nargs="+")
    p.set_defaults(func=cmd_unpin)
    p = sub.add_parser("drafts", help="lista rascunhos em quarentena")
    p.set_defaults(func=cmd_drafts)
    p = sub.add_parser("promote", help="promove um rascunho para skills/ (aprovação humana)")
    p.add_argument("name")
    p.set_defaults(func=cmd_promote)
    p = sub.add_parser("restore", help="desfaz o init: remove links/registro e devolve os originais")
    p.add_argument("--backup", help="desfaz só esta pasta de ~/.ai-skills/backups (padrão: todas as pendentes, da mais nova à mais antiga)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
