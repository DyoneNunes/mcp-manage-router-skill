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


@dataclass(frozen=True)
class Client:
    key: str
    label: str
    markers: tuple[str, ...]          # caminhos (relativos à home) que indicam instalação
    binaries: tuple[str, ...]
    skills_dirs: tuple[str, ...]      # pastas de skills (relativas à home)
    mcp: tuple[str, str] | None       # (formato, arquivo relativo à home)


CLIENTS: tuple[Client, ...] = (
    Client("claude", "Claude Code", (".claude",), ("claude",), (".claude/skills",),
           ("claude", ".claude.json")),
    Client("gemini", "Gemini CLI", (".gemini",), ("gemini",), (".gemini/skills",),
           ("mcpServers", ".gemini/settings.json")),
    Client("cursor", "Cursor", (".cursor",), ("cursor", "cursor-agent"), (".cursor/skills",),
           ("mcpServers", ".cursor/mcp.json")),
    Client("codex", "Codex CLI", (".codex",), ("codex",), (".codex/skills",),
           ("codex", ".codex/config.toml")),
    Client("opencode", "opencode", (".config/opencode",), ("opencode",), (".config/opencode/skills",),
           ("opencode", ".config/opencode/opencode.json")),
    Client("antigravity", "Antigravity", (".gemini/config/mcp_config.json",), (),
           (".gemini/config/skills",), ("mcpServers", ".gemini/config/mcp_config.json")),
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
        self.backup_root = indexer.home() / "backups" / time.strftime("%Y%m%d-%H%M%S")
        self.manifest: dict = {"moves": [], "skills_dirs": [], "registered": [], "config_backups": []}
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
        touched = any(self.manifest[k] for k in ("moves", "registered", "config_backups"))
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


def _edit_json(ops: Ops, path: Path, section: str, value: dict | None) -> bool:
    """Define (ou remove, se value=None) section[SERVER_NAME] num JSON. True se mudou."""
    data: dict = {}
    if path.is_file() and path.read_text(encoding="utf-8").strip():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            ops.warn(f"{path} não é JSON puro; registre o servidor manualmente.")
            return False
        if not isinstance(data, dict):
            return False
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


def _edit_claude(ops: Ops, path: Path, cmd: list[str] | None) -> bool:
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
        return False
    return True


def set_registration(ops: Ops, kind: str, path: Path, cmd: list[str] | None) -> bool:
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
        if changed:
            ops.manifest["registered"].append({"client": client.key, "kind": kind, "path": str(path)})
        ops.log(f"{client.label}: MCP '{SERVER_NAME}' {'registrado' if changed else 'já estava registrado'} em ~/{rel}")


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


def cmd_restore(args) -> int:
    root = indexer.home() / "backups"
    candidates = sorted(p for p in root.glob("*/manifest.json")) if root.is_dir() else []
    if args.backup:
        candidates = [root / args.backup / "manifest.json"]
    if not candidates or not candidates[-1].is_file():
        print("Nenhum backup com manifesto encontrado.")
        return 1
    try:
        manifest = json.loads(candidates[-1].read_text(encoding="utf-8"))
    except ValueError:
        manifest = None
    lists = ("skills_dirs", "registered", "moves")
    if not isinstance(manifest, dict) or not all(isinstance(manifest.get(k, []), list) for k in lists):
        print("Manifesto de backup inválido; nada foi restaurado.")
        return 1
    ops = Ops(args.dry_run)
    backup_dir = candidates[-1].parent
    # O backup tem de ser uma pasta real, direto em ~/.ai-skills/backups, com `files/` real.
    real_root = os.path.realpath(root)
    for folder in (backup_dir, backup_dir / "files"):
        expected = os.path.join(real_root, os.path.relpath(folder, root))
        if folder.is_symlink() or (folder.exists() and os.path.realpath(folder) != expected):
            print(f"Backup recusado: {folder} é um symlink ou aponta para fora de {root}.")
            return 1
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
        set_registration(ops, reg["kind"], Path(reg["path"]), None)
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
    p.add_argument("--backup", help="nome da pasta em ~/.ai-skills/backups (padrão: o mais recente)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
