# Architecture and Metadata Guide

How `skills-router` indexes and ranks skills, how to write frontmatter that routes
accurately across hundreds of skills, and what the router saves in context.

Every rule here comes from the server's implementation (`indexer.py`, `server.py`). Where a
number is a measurement, the method is given so you can reproduce it on your own library.

## 1. Pipeline

```
client session                       skills-router (stdio)            ~/.ai-skills
──────────────                       ─────────────────────            ────────────
skill-orchestrator (native)
        │ search_installed_skills ──► ensure_index() ───────────────► fingerprint of every SKILL.md
        │                              │  changed? ──► build_index() ─► index.json (name, description, tags, path)
        │ ◄── name/description/tags/score (no file content)
        │ get_skill_content ─────────► resolve_skill_file() ─────────► skills/<name>/SKILL.md (one file)
        │ ◄── content + directory + resources list
        │ propose_skill_draft ───────► drafts/<name>/SKILL.md (quarantine)
        └ promote_draft (approved) ──► drafts/<name> → skills/<name>, rebuild index
```

The fingerprint is a SHA-1 over each `SKILL.md` path, modification time and size, so adding,
removing or editing a `SKILL.md` triggers a rebuild on the next call. Editing only a
supporting file does not, and does not need to: the index stores frontmatter only.

## 2. What the indexer reads

Discovery is fixed at two depths inside `~/.ai-skills/skills` (or `$AI_SKILLS_HOME/skills`):

```
skills/<skill>/SKILL.md                 standalone skill
skills/<pkg>/SKILL.md                   package overview (indexed too)
skills/<pkg>/skills/<sub>/SKILL.md      sub-skill of a package
```

Folders whose name starts with `.` are skipped. Each candidate must resolve, after following
symlinks, to a regular file inside `skills/` with exactly one hard link. Anything else is
recorded under `unsafe_ignored` and never indexed.

| Field | Source, in order | Limit |
|---|---|---|
| `name` | frontmatter `name`, else the folder name | 128 chars. Must be unique, case-insensitive; the first in sorted folder order wins. |
| `description` | frontmatter `description`, else the first plain paragraph of the body | 1024 chars indexed; search results show 240. |
| `tags` | `tags`, else `keywords`, else `metadata.tags`; a YAML list or a comma/semicolon string | 20 tags, 50 chars each. Only scalars are kept. |
| `path` | absolute path to the `SKILL.md` | — |

Parsing limits: a `SKILL.md` is read up to 512 KB. A frontmatter block over 16 KB, nested
more than 32 levels, using YAML aliases (`&a` / `*a` / `<<`) or failing to parse goes
through a line-based fallback that keeps only simple `key: value` pairs. The body is never
searched.

## 3. Ranking model

The query and each skill's three fields are tokenized the same way: Unicode NFKD, lowercase,
accents stripped, split on anything outside `[a-z0-9]`, tokens of one character dropped,
stopwords dropped. The stopword list covers common English and Portuguese function words
plus `use`, `using`, `when`, `fazer`, `criar`, `quero`, `preciso`.

For each distinct query term `t`:

```
idf(t)    = ln(1 + N / (1 + df(t)))            N = skills indexed, df = skills containing t
weight(t) = max(field weights where t is an exact token)      name 3.0 · tags 2.5 · description 1.0
            + 0.5 if t matches in more than one field
          | 0.5 × max(field weights with a prefix match)       only if no exact match;
                                                              both tokens ≥ 4 chars, one starts with the other
score     = Σ idf(t)·weight(t) / Σ idf(t)·3.5
          × 1.15 if the query has 2+ terms and "t1-t2-..." appears inside the skill's name tokens
          clamped to 1.0; ties broken by name
```

What follows from it:

1. **The name is worth three times the description.** Put the primary subject in the name.
2. **Name plus tag is a perfect hit.** A term found in both scores 3.5, the normalizer, so
   the core subject belongs in the name and the tags.
3. **Repetition does nothing.** Fields are sets of tokens. Use the words saved on
   repetition for synonyms.
4. **Rare words carry the ranking.** IDF rewards terms few skills share. Words like
   `helper`, `tool`, `guide`, `workflow` appear everywhere and add little.
5. **Prefixes match at half weight.** `deploy` finds `deployment` and `test` finds `testing`,
   but `k8s` never finds `kubernetes`. Add abbreviations and synonyms as tags.
6. **Hyphenated names get a phrase boost.** The query `landing page` boosts `landing-page`
   and `landing-page-design` by 15%.
7. **Scores are relative to the query.** A 0.4 on a three-term query and a 0.4 on a
   one-term query mean different things. Compare hits within one response.

## 4. Frontmatter that routes well

Template:

```yaml
---
name: fastapi-postgres-migrations
description: >-
  Plans and runs Alembic schema migrations for FastAPI services on PostgreSQL: autogenerate
  revisions, review destructive operations, zero-downtime column changes, rollback scripts.
  Use when the user mentions Alembic, database migrations, schema changes, adding or
  renaming columns, or a failed migration in a FastAPI or SQLAlchemy project.
tags: [fastapi, postgres, postgresql, alembic, sqlalchemy, migration, database, backend]
---
```

Rules:

- **name**: kebab-case, `^[a-z0-9][a-z0-9-]{0,63}$` (the only form accepted for drafts),
  two to four words, subject first: `<stack>-<object>[-<action>]`. Avoid names that are only
  generic words (`helper`, `utils`, `my-skill`).
- **description**: two sentences. The first says what the skill produces, using the nouns
  a user would type. The second starts with "Use when" and lists triggers: tool names, file
  types, error messages, user phrasings. Aim for 250 to 700 characters; past 1024 the rest
  is dropped.
- **tags**: 4 to 10 lowercase tokens from a controlled vocabulary (section 5). Include
  abbreviations and synonyms the name and description lack.
- Keep the frontmatter plain YAML: no anchors, no aliases, no deep nesting.

Weak versus strong:

| Weak | Strong |
|---|---|
| `name: db-helper` | `name: fastapi-postgres-migrations` |
| `description: Helps with database stuff.` | The two-sentence description above |
| no tags | `tags: [postgres, postgresql, alembic, migration, ...]` |

The weak version can only match `db` and `helper`. `db` is two characters, so it is never
prefix-matched. The strong version matches `postgres`, `alembic`, `migration`, `schema`,
`column`, `rollback` and more, most of them in two fields.

## 5. Taxonomy for hundreds of skills

Tags are searched as plain tokens, so a taxonomy is a shared vocabulary, not a hierarchy.
Give every skill one or more tags from each facet that applies:

| Facet | Purpose | Examples |
|---|---|---|
| Stack | Languages, frameworks, products | `python`, `fastapi`, `react`, `terraform`, `aws`, `stripe` |
| Domain | Area of work | `backend`, `frontend`, `infra`, `security`, `data`, `marketing`, `legal` |
| Artifact | What gets produced | `pdf`, `docx`, `dockerfile`, `landing-page`, `report`, `migration` |
| Activity | Verb of the task | `audit`, `test`, `deploy`, `refactor`, `review`, `debug` |
| Alias | Abbreviations and other languages | `k8s`, `pg`, `js`, words your team types in its own language |

Practices that keep precision high as the library grows:

1. **Keep the vocabulary in one file** (for example `~/.ai-skills/TAGS.md`; files outside
   skill folders are not indexed). Add a tag there before using it, and prefer an existing
   tag over a near-synonym.
2. **Name families.** Share a prefix across related skills (`aws-lambda-deploy`,
   `aws-iam-audit`). The prefix becomes a strong name token for broad queries, and the
   suffix separates them.
3. **One subject per skill.** If a description needs "and also", split the skill. Two
   narrow skills each rank higher for their own queries than one broad skill does for either.
4. **Packages for suites.** Group related skills as `<pkg>/skills/<sub>/`. Give the package's
   own `SKILL.md` a description that lists its sub-skills, so a broad query finds the
   overview and a narrow query finds the sub-skill.
5. **Pin sparingly.** Each pinned skill costs its description in every session. Pin only
   what the user invokes by name from the `/` menu, about five at most.
6. **Keep SKILL.md small.** `get_skill_content` returns the whole file. Move long examples
   and tables into `reference/` files, which are listed in `resources` and read only on demand.

### Measure routing precision

Keep a list of real requests and the skill each should find, then check top-3 hits with the
CLI. Set `SR` to the way you run the CLI: `skills-router` (persistent install),
`uv run skills-router` (inside a checkout) or `uvx --from <url> skills-router` (zero-config).
Each output line is `<score>  <name>  <description>`:

```bash
SR="skills-router"
while IFS='|' read -r query expected; do
  if $SR search $query --limit 3 2>/dev/null | awk '{print $2}' | grep -qx "$expected"; then echo "hit   $query"
  else echo "MISS  $query -> $expected"; fi
done <<'EOF'
alembic migration rollback|fastapi-postgres-migrations
landing page saas|landing-page-design
EOF
```

Fix each miss by adding the missing words to the expected skill's name, tags or
description, not by changing the query. Run `python3 <skill-dir>/scripts/healthcheck.py` afterwards:
its `skill_lint` check reports missing frontmatter, short or truncated descriptions,
duplicate names, and symlinked or hard-linked files the server will reject.

## 6. Context savings benchmark

### Method

Measured on 2026-11-25 against one real library of 319 indexed skills (average description
399 characters; only 4 skills declared tags). Token counts use the common approximation of
4 characters per token, so treat them as estimates.

- **Native catalog**: every skill rendered as `- name: description` (what a client injects
  when it lists all installed skills).
- **Router fixed cost**: the orchestrator's catalog line plus the JSON of the five tool
  definitions (name, description, input schema).
- **Search**: the JSON response of `search_installed_skills` with `limit=5`, averaged over
  10 representative queries.
- **Load**: size of the `SKILL.md` that `get_skill_content` returns. This cost is the same
  with or without the router, because a native skill also loads its body when used.

### Results

| Item | Characters | ≈ Tokens |
|---|---:|---:|
| Native catalog, 319 skills | 135,183 | 33,800 |
| Router: orchestrator line | 322 | 80 |
| Router: five tool schemas | 1,804 | 450 |
| Router fixed total | 2,126 | **530** |
| Router fixed total + 5 pinned skills | 5,379 | 1,340 |
| One search response (mean of 10; max 2,078 chars) | 1,834 | 460 |
| One loaded SKILL.md (median / mean) | 6,225 / 8,742 | 1,560 / 2,190 |

Per session, before any skill body loads:

| Scenario | Native | Router | Saved |
|---|---:|---:|---:|
| No task needs a skill | 33,800 | 530 | 98% |
| Two searches | 33,800 | 1,450 | 96% |
| Two searches, 5 pinned skills | 33,800 | 2,260 | 93% |

### Scaling

```
native  ≈ N × (len(name) + len(description) + 4) / 4      tokens, every session
router  ≈ 530 + 460 × searches + pinned descriptions       tokens
```

At this library's average of about 106 tokens per skill, the router costs less once the
library passes about 14 skills for a session with two searches, and the gap grows linearly
with every skill added.

### Caveats

- Some clients cap or truncate the native skill listing, which lowers the native figure. The
  router's advantage is that cost no longer grows with the library at all.
- The router adds a round trip per search, and its ranking is keyword-based, not semantic:
  a skill is only as findable as the words in its name, tags and description.

### What the scores looked like

The same 10 queries show why metadata quality matters more than library size:

| Query | Top hit | Score | Reading |
|---|---|---:|---|
| `license` | `license-designer` | 1.00 | Term in the name: perfect hit. |
| `security audit` | `security-audit`, `security-audit-pipeline` | 1.00 | Phrase match in both names. |
| `landing page saas` | `landing-page` | 0.71 | Strong; the phrase boost applies. |
| `git commit` | `commit-push` | 0.63 | Name hit on one term. |
| `python packaging uv` | `uv` | 0.44 | Correct; a short name with few words limits the score. |
| `react testing` | `mobile-native` | 0.20 | Noise: the library has no React testing skill. |
| `fastapi postgres migration` | unrelated | 0.09 | Noise: nothing covers this stack. |

In this library, hits under about 0.2 were noise and real matches scored 0.3 or more.
Recalibrate on your own library with the precision check in section 5.
