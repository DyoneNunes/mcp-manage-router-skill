"""Servidor MCP `skills-router`: busca, leitura sob demanda e quarentena de rascunhos."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import yaml
from fastmcp import FastMCP

from . import indexer

mcp = FastMCP(
    "skills-router",
    instructions=(
        "Roteador de skills locais. Antes de tarefas de desenvolvimento, arquitetura ou "
        "automação, chame search_installed_skills e carregue com get_skill_content apenas "
        "as skills escolhidas. Novas skills só entram como rascunho (propose_skill_draft) e "
        "só são promovidas (promote_draft) com aprovação explícita do humano."
    ),
)

_MAX_RESOURCES = 60


def _resources(skill_dir: Path) -> list[str]:
    """Arquivos de apoio da skill (só nomes; o conteúdo é lido sob demanda)."""
    found = []
    for p in sorted(skill_dir.rglob("*")):
        rel = p.relative_to(skill_dir)
        if any(part.startswith(".") or part in ("__pycache__", "node_modules") for part in rel.parts):
            continue
        if p.is_file() and p.name != "SKILL.md":
            found.append(str(rel))
            if len(found) >= _MAX_RESOURCES:
                found.append("...")
                break
    return found


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
    path = Path(entry["path"])
    return {
        "name": entry["name"],
        "path": str(path),
        "directory": str(path.parent),
        "resources": _resources(path.parent),
        "content": path.read_text(encoding="utf-8", errors="replace"),
    }


@mcp.tool()
def rebuild_index() -> dict:
    """Força a reindexação completa do diretório oficial de skills."""
    data = indexer.build_index()
    return {
        "status": "rebuilt",
        "skills_indexed": len(data["skills"]),
        "duplicates_ignored": data.get("duplicates_ignored", []),
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

    extra, body = indexer.parse_frontmatter(content or "")
    if not body.strip():
        raise ValueError("O conteúdo da skill está vazio.")

    meta = {"name": name, "description": description}
    meta.update({k: v for k, v in extra.items() if k not in meta})
    info = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    info.setdefault("author", indexer.owner())
    info.setdefault("created", time.strftime("%Y-%m-%d"))
    meta["metadata"] = info

    target_dir = indexer.drafts_dir() / name
    overwritten = (target_dir / "SKILL.md").exists()
    target_dir.mkdir(parents=True, exist_ok=True)
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=100).strip()
    (target_dir / "SKILL.md").write_text(f"---\n{front}\n---\n\n{body.strip()}\n", encoding="utf-8")

    return {
        "status": "draft_saved",
        "path": str(target_dir / "SKILL.md"),
        "overwritten_previous_draft": overwritten,
        "name_already_installed": (indexer.skills_dir() / name).exists(),
        "next_step": "Mostre o rascunho ao humano. Só chame promote_draft após aprovação explícita.",
    }


@mcp.tool()
def promote_draft(skill_name: str) -> dict:
    """Move drafts/<skill_name> para skills/ e reindexa. Exige aprovação humana explícita."""
    name = indexer.validate_name(skill_name)
    return promote(name)


def promote(name: str) -> dict:
    source = indexer.drafts_dir() / name
    target = indexer.skills_dir() / name
    if not (source / "SKILL.md").is_file():
        raise ValueError(f"Não existe rascunho {name!r} em {indexer.drafts_dir()}.")
    if target.exists():
        raise ValueError(f"Já existe uma skill oficial chamada {name!r}; promoção recusada.")
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
