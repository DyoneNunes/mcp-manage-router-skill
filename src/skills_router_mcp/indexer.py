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
import stat
import subprocess
import tempfile
import time
import unicodedata
from collections import Counter
from pathlib import Path

import yaml

INDEX_VERSION = 1
MAX_SKILL_BYTES = 512 * 1024      # teto de leitura de um SKILL.md
MAX_FRONTMATTER_BYTES = 16 * 1024  # frontmatter maior que isso não passa pelo parser YAML
MAX_YAML_DEPTH = 32                # aninhamento de [ ] / { } aceito no frontmatter
MAX_TAGS = 20
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
class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader que recusa aliases: frontmatter de skill não precisa deles, e é por
    eles (inclusive via merge key `<<`) que bombas de expansão travam o parser."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.events.AliasEvent):
            raise yaml.YAMLError("aliases YAML não são aceitos em frontmatter de skill")
        return super().compose_node(parent, index)


def _too_deep(raw: str) -> bool:
    depth = 0
    for ch in raw:
        if ch in "[{":
            depth += 1
            if depth > MAX_YAML_DEPTH:
                return True
        elif ch in "]}" and depth:
            depth -= 1
    return False


def _loose_parse(raw: str) -> dict:
    """Fallback linear para frontmatter que não é YAML válido (ex.: ':' sem aspas)."""
    parts: dict[str, list[str]] = {}
    key = None
    for line in raw.splitlines():
        m = _LOOSE_KEY.match(line)
        if m:
            key, value = m.group(1), m.group(2).strip()
            parts[key] = [] if value in (">", ">-", "|", "|-") else [value.strip("\"'")]
        elif key and line.startswith((" ", "\t")):
            parts[key].append(line.strip())
    return {k: " ".join(v).strip() for k, v in parts.items()}


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Devolve (metadados, corpo). Nunca levanta exceção por YAML ruim."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    raw, body = m.group(1), text[m.end():]
    if len(raw) > MAX_FRONTMATTER_BYTES or _too_deep(raw):
        return _loose_parse(raw[:MAX_FRONTMATTER_BYTES]), body
    try:
        meta = yaml.load(raw, Loader=_NoAliasLoader)  # noqa: S506 - subclasse de SafeLoader
    except Exception:  # noqa: BLE001 - YAML inválido, data impossível, aninhamento absurdo...
        meta = _loose_parse(raw)
    return (meta if isinstance(meta, dict) else {}), body


def _first_paragraph(body: str, limit: int = 300) -> str:
    for block in re.split(r"\n\s*\n", body):
        block = block.strip()
        if block and not block.startswith(("#", "```", "---", "<", "|", "!")):
            return " ".join(block.split())[:limit]
    return ""


def _scalar(value) -> str:
    """Só escalares viram texto. Listas/dicts (ex.: bomba de aliases YAML) são ignorados."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return str(value)


def _tags(meta: dict) -> list[str]:
    nested = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    raw = meta.get("tags") or meta.get("keywords") or nested.get("tags") or []
    if isinstance(raw, str):
        raw = re.split(r"[,;]", raw)
    if not isinstance(raw, list):
        return []
    tags = (_scalar(t).strip()[:50] for t in raw[:MAX_TAGS * 2])
    return [t for t in tags if t][:MAX_TAGS]


def read_text_capped(path: Path, limit: int = MAX_SKILL_BYTES) -> tuple[str, bool]:
    """Lê no máximo `limit` bytes. Devolve (texto, truncado?)."""
    with open(path, "rb") as fh:
        raw = fh.read(limit + 1)
    return raw[:limit].decode("utf-8", errors="replace"), len(raw) > limit


def resolve_skill_file(path: str | Path) -> Path:
    """Valida que `path` é um SKILL.md legítimo do diretório oficial e devolve o arquivo real.

    Contenção estrita: depois de resolver todos os symlinks, o arquivo precisa estar
    dentro do diretório oficial, ser um arquivo regular e não ter hardlinks. Isso barra
    índice adulterado, SKILL.md/pasta/pasta intermediária que apontem para fora, FIFOs e
    hardlinks para arquivos alheios. Symlinks internos ao diretório continuam valendo.
    """
    p = Path(path)
    try:
        rel = p.relative_to(skills_dir())
    except ValueError:
        raise ValueError("caminho fora do diretório oficial de skills") from None
    if p.name != "SKILL.md" or ".." in rel.parts or len(rel.parts) not in (2, 4):
        raise ValueError("caminho de skill inválido")
    real_root = os.path.realpath(skills_dir())
    real = os.path.realpath(p)
    if not real.startswith(real_root + os.sep):
        raise ValueError("aponta (via symlink) para fora do diretório oficial de skills")
    try:
        info = os.stat(real)
    except OSError:
        raise ValueError("arquivo inacessível ou symlink quebrado") from None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("não é um arquivo regular")
    if info.st_nlink > 1:
        raise ValueError("tem hardlinks; copie o arquivo para dentro da skill")
    return Path(real)


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """Grava por arquivo temporário exclusivo (0600) + rename.

    Nunca escreve *através* do destino: se ele for symlink, hardlink ou FIFO, é
    substituído, e o alvo original fica intacto.
    """
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            os.fchmod(fh.fileno(), mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_skill(skill_md: Path) -> dict:
    """Metadados leves de um SKILL.md: name, description, tags, path."""
    text, _ = read_text_capped(skill_md)
    meta, body = parse_frontmatter(text)
    name = (_scalar(meta.get("name")) or skill_md.parent.name).strip()[:128]
    description = " ".join(_scalar(meta.get("description")).split()) or _first_paragraph(body)
    return {
        "name": name,
        "description": description[:1024],
        "tags": _tags(meta),
        "path": str(skill_md),
    }


# --------------------------------------------------------------------------- #
# Indexação
# --------------------------------------------------------------------------- #
def _candidates(root: Path):
    for entry in sorted(root.iterdir()):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        yield entry / "SKILL.md"
        nested = entry / "skills"
        if nested.is_dir():
            for sub in sorted(nested.iterdir()):
                if sub.is_dir():
                    yield sub / "SKILL.md"


def iter_skill_files(root: Path | None = None, rejected: list | None = None):
    """SKILL.md de cada skill: ``<skill>/SKILL.md`` e ``<pacote>/skills/<sub>/SKILL.md``.

    No diretório oficial, cada arquivo passa por `resolve_skill_file`; os recusados não
    são indexados e, se `rejected` for dado, entram nele com o motivo.
    """
    root = root or skills_dir()
    if not root.is_dir():
        return
    official = root == skills_dir()
    for candidate in _candidates(root):
        if not os.path.lexists(candidate):
            continue
        if official:
            try:
                resolve_skill_file(candidate)
            except ValueError as exc:
                if rejected is not None:
                    rejected.append({"path": str(candidate), "reason": str(exc)})
                continue
        elif not candidate.is_file():
            continue
        yield candidate


def fingerprint(root: Path | None = None) -> str:
    """Muda sempre que um SKILL.md é criado, removido ou editado."""
    digest = hashlib.sha1()
    for f in iter_skill_files(root):
        st = f.stat()
        digest.update(f"{f}\0{st.st_mtime_ns}\0{st.st_size}\n".encode())
    return digest.hexdigest()


def build_index() -> dict:
    skills, seen, duplicates, rejected = [], set(), [], []
    for f in iter_skill_files(rejected=rejected):
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
    if rejected:
        data["unsafe_ignored"] = rejected
    target = index_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, json.dumps(data, ensure_ascii=False, indent=1), 0o644)
    return data


def _valid_entry(entry) -> bool:
    return (
        isinstance(entry, dict)
        and all(isinstance(entry.get(k), str) for k in ("name", "description", "path"))
        and isinstance(entry.get("tags"), list)
        and all(isinstance(t, str) for t in entry["tags"])
    )


def load_index() -> dict | None:
    try:
        data = json.loads(index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
        return None
    skills = data.get("skills")
    if not isinstance(skills, list) or not all(_valid_entry(e) for e in skills):
        return None   # índice corrompido ou adulterado: será reconstruído
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
