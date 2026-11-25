"""Servidor MCP `skills-router`: busca, leitura sob demanda e quarentena de rascunhos."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import yaml
from fastmcp import FastMCP

from . import indexer

mcp = FastMCP(
    "skills-router",
    instructions=(
        "Local skills router. On any development, architecture, infrastructure or automation "
        "task, your FIRST action is search_installed_skills with 2-5 English keywords; then load "
        "only the chosen skills with get_skill_content. Skip it for small talk and factual "
        "questions. New skills enter only as drafts (propose_skill_draft) and are promoted "
        "(promote_draft) only after the human explicitly approves that draft."
    ),
)

_MAX_RESOURCES = 60
_MAX_DRAFT_BYTES = 256 * 1024
_SKIP_DIRS = ("__pycache__", "node_modules")


def _resources(skill_dir: Path) -> list[str]:
    """Arquivos de apoio da skill (só nomes; o conteúdo é lido sob demanda).

    Não segue symlinks: um link para fora da skill não é listado nem percorrido.
    """
    found: list[str] = []
    for base, dirs, files in os.walk(skill_dir, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS
                         and not os.path.islink(os.path.join(base, d)))
        for name in sorted(files):
            full = os.path.join(base, name)
            if name.startswith(".") or name == "SKILL.md" or os.path.islink(full):
                continue
            found.append(os.path.relpath(full, skill_dir))
            if len(found) >= _MAX_RESOURCES:
                return found + ["..."]
    return found


def _require_plain_runtime_dir(folder: Path) -> None:
    """drafts/ e skills/ têm de ser pastas reais dentro do runtime, nunca symlinks."""
    expected = os.path.join(os.path.realpath(indexer.home()), folder.name)
    if folder.is_symlink() or os.path.realpath(folder) != expected:
        raise ValueError(f"{folder} é um symlink ou está fora do runtime; operação recusada.")


def _draft_problem(folder: Path) -> str | None:
    """Motivo para recusar um rascunho, ou None. Só entram arquivos regulares e próprios."""
    if folder.is_symlink():
        return "a pasta do rascunho é um symlink"
    for base, dirs, files in os.walk(folder, followlinks=False):
        for name in dirs + files:
            full = os.path.join(base, name)
            info = os.lstat(full)
            if os.path.islink(full):
                return f"contém symlink ({name})"
            if name in files and (not os.path.isfile(full) or info.st_nlink > 1):
                return f"contém arquivo especial ou com hardlink ({name})"
            if name == "SKILL.md" and base != str(folder):
                return "contém outras skills aninhadas"
    return None


def _promotion_locked() -> bool:
    """Fail-closed: só `mcp` (ou variável ausente) libera a promoção pela tool."""
    return os.environ.get("SKILLS_ROUTER_PROMOTE", "mcp").strip().lower() != "mcp"


@mcp.tool()
def search_installed_skills(query: str, limit: int = 5) -> dict:
    """Busca skills instaladas por relevância e devolve só metadados resumidos + score.

    Não carrega nenhum SKILL.md. Use palavras-chave da stack/tarefa (os metadados
    costumam estar em inglês) e faça mais de uma busca se a tarefa cruzar domínios.
    """
    data, _ = indexer.ensure_index()
    results = indexer.search(query, data["skills"], limit)
    return {"query": query, "total_indexed": len(data["skills"]), "results": results}


@mcp.tool()
def get_skill_content(skill_name: str) -> dict:
    """Lê o SKILL.md completo de UMA skill escolhida (use após search_installed_skills)."""
    data, _ = indexer.ensure_index()
    entry = indexer.find_skill(skill_name, data)
    if entry is None:
        hints = [r["name"] for r in indexer.search(skill_name, data["skills"], 3)]
        raise ValueError(
            f"Skill {skill_name!r} não encontrada."
            + (f" Parecidas: {', '.join(hints)}." if hints else " Use search_installed_skills.")
        )
    path = indexer.resolve_skill_file(entry["path"])   # recusa índice adulterado e symlinks
    content, truncated = indexer.read_text_capped(path)
    return {
        "name": entry["name"],
        "path": str(path),
        "directory": str(path.parent),
        "resources": _resources(path.parent),
        "content": content,
        "truncated": truncated,
    }


@mcp.tool()
def rebuild_index() -> dict:
    """Força a reindexação completa do diretório oficial de skills."""
    data = indexer.build_index()
    return {
        "status": "rebuilt",
        "skills_indexed": len(data["skills"]),
        "duplicates_ignored": data.get("duplicates_ignored", []),
        "unsafe_ignored": data.get("unsafe_ignored", []),
        "index_path": str(indexer.index_path()),
    }


@mcp.tool()
def propose_skill_draft(name: str, description: str, content: str) -> dict:
    """Salva um RASCUNHO de skill em drafts/<name>/SKILL.md (nunca em skills/).

    Só chame depois de o humano confirmar explicitamente que quer o rascunho.
    `content` é o corpo em Markdown; um frontmatter enviado junto é mesclado.
    """
    name = indexer.validate_name(name)
    description = " ".join((description or "").split())
    if not description:
        raise ValueError("A descrição é obrigatória.")
    if len(description) > 1024:
        raise ValueError("A descrição deve ter no máximo 1024 caracteres.")

    if len((content or "").encode("utf-8")) > _MAX_DRAFT_BYTES:
        raise ValueError(f"Conteúdo grande demais (máx. {_MAX_DRAFT_BYTES // 1024} KB).")

    extra, body = indexer.parse_frontmatter(content or "")
    if not body.strip():
        raise ValueError("O conteúdo da skill está vazio.")

    meta = {"name": name, "description": description}
    meta.update({k: v for k, v in extra.items() if k not in meta})
    info = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    info["author"] = indexer.owner()              # nunca vem do conteúdo proposto
    info["created"] = time.strftime("%Y-%m-%d")
    meta["metadata"] = info

    indexer.drafts_dir().mkdir(parents=True, exist_ok=True)
    _require_plain_runtime_dir(indexer.drafts_dir())
    target_dir = indexer.drafts_dir() / name
    if target_dir.is_symlink():
        raise ValueError("O destino do rascunho é um symlink; gravação recusada.")
    overwritten = os.path.lexists(target_dir / "SKILL.md")
    target_dir.mkdir(exist_ok=True)
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=100).strip()
    indexer.atomic_write(target_dir / "SKILL.md", f"---\n{front}\n---\n\n{body.strip()}\n", 0o644)

    return {
        "status": "draft_saved",
        "path": str(target_dir / "SKILL.md"),
        "overwritten_previous_draft": overwritten,
        "name_already_installed": _installed(name),
        "next_step": "Mostre o rascunho ao humano. Só chame promote_draft após aprovação explícita.",
    }


@mcp.tool()
def promote_draft(skill_name: str) -> dict:
    """Move drafts/<skill_name> para skills/ e reindexa. Exige aprovação humana explícita."""
    name = indexer.validate_name(skill_name)
    if _promotion_locked():
        raise ValueError(
            "Promoção via MCP desativada nesta máquina (SKILLS_ROUTER_PROMOTE). "
            f"Peça ao humano para rodar: skills-router promote {name}"
        )
    return promote(name)


def _installed(name: str) -> bool:
    """Já existe skill com esse nome — pela pasta OU pelo `name` do frontmatter?"""
    if os.path.lexists(indexer.skills_dir() / name):
        return True
    return indexer.find_skill(name, indexer.ensure_index()[0]) is not None


def promote(name: str) -> dict:
    _require_plain_runtime_dir(indexer.drafts_dir())
    indexer.skills_dir().mkdir(parents=True, exist_ok=True)
    _require_plain_runtime_dir(indexer.skills_dir())
    source = indexer.drafts_dir() / name
    target = indexer.skills_dir() / name
    if source.is_symlink() or not (source / "SKILL.md").is_file():
        raise ValueError(f"Não existe rascunho {name!r} em {indexer.drafts_dir()}.")
    if _installed(name):
        raise ValueError(f"Já existe uma skill oficial chamada {name!r}; promoção recusada.")
    problem = _draft_problem(source)
    if problem:
        raise ValueError(f"Rascunho recusado: {problem}.")
    declared = indexer.read_skill(source / "SKILL.md")["name"]
    if declared != name:
        raise ValueError(f"O frontmatter declara name={declared!r}, diferente da pasta {name!r}.")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    data = indexer.build_index()
    return {"status": "promoted", "path": str(target / "SKILL.md"), "skills_indexed": len(data["skills"])}


def main() -> None:
    indexer.skills_dir().mkdir(parents=True, exist_ok=True)
    indexer.drafts_dir().mkdir(parents=True, exist_ok=True)
    indexer.ensure_index()  # auto-indexação: só refaz se algum SKILL.md mudou
    mcp.run()


if __name__ == "__main__":
    main()
