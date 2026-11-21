"""Leitura de frontmatter, geração do index.json e busca por relevância.

Nada aqui conhece o dono da máquina: todos os caminhos derivam de
``$AI_SKILLS_HOME`` (padrão ``~/.ai-skills``) e a identidade do autor de um
rascunho é lida da configuração local (git / usuário do sistema) em runtime.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import math
import os
import re
import subprocess
import time
import unicodedata
from collections import Counter
from pathlib import Path

import yaml

INDEX_VERSION = 1
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

_FRONTMATTER = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_LOOSE_KEY = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$")

_STOPWORDS = frozenset(
    """a an and are as at be by for from how in is it of on or that the this to use
    using when with you your um uma o os as de da do das dos e em para por com que
    como no na nos nas ao se ou meu minha quero preciso fazer criar""".split()
)

_W_NAME, _W_TAG, _W_DESC, _W_MULTI = 3.0, 2.5, 1.0, 0.5


# --------------------------------------------------------------------------- #
# Caminhos e identidade local
# --------------------------------------------------------------------------- #
def home() -> Path:
    custom = os.environ.get("AI_SKILLS_HOME")
    return Path(custom).expanduser() if custom else Path.home() / ".ai-skills"


def skills_dir() -> Path:
    return home() / "skills"


def drafts_dir() -> Path:
    return home() / "drafts"


def index_path() -> Path:
    return home() / "index.json"


def owner() -> str:
    """Dono da máquina, descoberto na hora (git global; senão, usuário do SO)."""
    try:
        out = subprocess.run(
            ["git", "config", "--global", "user.name"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        if out:
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - ambientes sem passwd
        return "unknown"


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise ValueError(
            f"Nome de skill inválido: {name!r}. Use minúsculas, dígitos e hífens (máx. 64)."
        )
    return name


# --------------------------------------------------------------------------- #
# Frontmatter
# --------------------------------------------------------------------------- #
def _loose_parse(raw: str) -> dict:
    """Fallback para frontmatter que não é YAML válido (ex.: ':' sem aspas)."""
    meta: dict[str, str] = {}
    key = None
    for line in raw.splitlines():
        m = _LOOSE_KEY.match(line)
        if m:
            key, value = m.group(1), m.group(2).strip()
            meta[key] = "" if value in (">", ">-", "|", "|-") else value.strip("\"'")
        elif key and line.startswith((" ", "\t")):
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Devolve (metadados, corpo). Nunca levanta exceção por YAML ruim."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    raw, body = m.group(1), text[m.end():]
    try:
        meta = yaml.safe_load(raw)
    except yaml.YAMLError:
        meta = _loose_parse(raw)
    return (meta if isinstance(meta, dict) else {}), body


def _first_paragraph(body: str, limit: int = 300) -> str:
    for block in re.split(r"\n\s*\n", body):
        block = block.strip()
        if block and not block.startswith(("#", "```", "---", "<", "|", "!")):
            return " ".join(block.split())[:limit]
    return ""


def _tags(meta: dict) -> list[str]:
    nested = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    raw = meta.get("tags") or meta.get("keywords") or nested.get("tags") or []
    if isinstance(raw, str):
        raw = re.split(r"[,;]", raw)
    if not isinstance(raw, list):
        return []
    return [str(t).strip() for t in raw if str(t).strip()]


def read_skill(skill_md: Path) -> dict:
    """Metadados leves de um SKILL.md: name, description, tags, path."""
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    meta, body = parse_frontmatter(text)
    name = str(meta.get("name") or skill_md.parent.name).strip()
    description = meta.get("description")
    description = " ".join(str(description).split()) if description else _first_paragraph(body)
    return {
        "name": name,
        "description": description[:1024],
        "tags": _tags(meta),
        "path": str(skill_md),
    }


# --------------------------------------------------------------------------- #
# Indexação
# --------------------------------------------------------------------------- #
def iter_skill_files(root: Path | None = None):
    """SKILL.md de cada skill: ``<skill>/SKILL.md`` e ``<pacote>/skills/<sub>/SKILL.md``."""
    root = root or skills_dir()
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if (entry / "SKILL.md").is_file():
            yield entry / "SKILL.md"
        nested = entry / "skills"
        if nested.is_dir():
            for sub in sorted(nested.iterdir()):
                if sub.is_dir() and (sub / "SKILL.md").is_file():
                    yield sub / "SKILL.md"


def fingerprint(root: Path | None = None) -> str:
    """Muda sempre que um SKILL.md é criado, removido ou editado."""
    digest = hashlib.sha1()
    for f in iter_skill_files(root):
        st = f.stat()
        digest.update(f"{f}\0{st.st_mtime_ns}\0{st.st_size}\n".encode())
    return digest.hexdigest()


def build_index() -> dict:
    skills, seen, duplicates = [], set(), []
    for f in iter_skill_files():
        entry = read_skill(f)
        key = entry["name"].lower()
        if key in seen:
            duplicates.append(entry["path"])
            continue
        seen.add(key)
        skills.append(entry)
    data = {
        "version": INDEX_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "fingerprint": fingerprint(),
        "skills": skills,
    }
    if duplicates:
        data["duplicates_ignored"] = duplicates
    target = index_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, target)
    return data


def load_index() -> dict | None:
    try:
        data = json.loads(index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
        return None
    return data


def ensure_index() -> tuple[dict, bool]:
    """Devolve (índice, reconstruído?). Reindexa só se algum SKILL.md mudou."""
    data = load_index()
    if data is not None and data.get("fingerprint") == fingerprint():
        return data, False
    return build_index(), True


def find_skill(name: str, data: dict | None = None) -> dict | None:
    data = data or ensure_index()[0]
    wanted = (name or "").strip().lower()
    for entry in data["skills"]:
        if entry["name"].lower() == wanted:
            return entry
    for entry in data["skills"]:  # nome do diretório como apelido
        if Path(entry["path"]).parent.name.lower() == wanted:
            return entry
    return None


# --------------------------------------------------------------------------- #
# Busca
# --------------------------------------------------------------------------- #
def tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return [t for t in re.findall(r"[a-z0-9]+", text) if len(t) > 1 and t not in _STOPWORDS]


def _prefix_hit(token: str, field: set[str]) -> bool:
    if len(token) < 4:
        return False
    return any(len(f) >= 4 and (f.startswith(token) or token.startswith(f)) for f in field)


def _weight(token: str, name_t: set[str], tag_t: set[str], desc_t: set[str]) -> float:
    hits = [w for w, field in ((_W_NAME, name_t), (_W_TAG, tag_t), (_W_DESC, desc_t)) if token in field]
    if hits:
        return max(hits) + (_W_MULTI if len(hits) > 1 else 0.0)
    partial = [w for w, field in ((_W_NAME, name_t), (_W_TAG, tag_t), (_W_DESC, desc_t))
               if _prefix_hit(token, field)]
    return max(partial) * 0.5 if partial else 0.0


def search(query: str, skills: list[dict], limit: int = 5) -> list[dict]:
    """Ranking TF-IDF leve sobre name/tags/description. Score normalizado em 0..1."""
    terms = list(dict.fromkeys(tokenize(query)))
    if not terms or not skills:
        return []
    docs = [
        (set(tokenize(s["name"])), set(tokenize(" ".join(s.get("tags", [])))), set(tokenize(s["description"])))
        for s in skills
    ]
    df: Counter[str] = Counter()
    for name_t, tag_t, desc_t in docs:
        df.update(name_t | tag_t | desc_t)
    n = len(docs)
    idf = {t: math.log(1 + n / (1 + df[t])) for t in terms}
    best = sum(idf[t] * (_W_NAME + _W_MULTI) for t in terms)
    phrase = "-".join(terms)

    ranked = []
    for skill, (name_t, tag_t, desc_t) in zip(skills, docs):
        score = sum(idf[t] * _weight(t, name_t, tag_t, desc_t) for t in terms)
        if score <= 0:
            continue
        score /= best
        if len(terms) > 1 and phrase in "-".join(tokenize(skill["name"])):
            score *= 1.15
        ranked.append((min(score, 1.0), skill))
    ranked.sort(key=lambda item: (-item[0], item[1]["name"]))

    limit = max(1, min(int(limit), 25))
    return [
        {
            "name": s["name"],
            "description": s["description"] if len(s["description"]) <= 240 else s["description"][:237] + "...",
            "tags": s.get("tags", []),
            "path": s["path"],
            "score": round(score, 3),
        }
        for score, s in ranked[:limit]
    ]
