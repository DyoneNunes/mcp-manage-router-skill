# Troubleshooting Reference

Symptoms and messages for `SKILL.md` Step 3, each mapped to its cause and fix. Server and
installer messages are in Portuguese: match on the quoted fragment. Section numbers such as
SKILL.md 1.5 refer to the main guide, and A, A', B and C are its launch options (SKILL.md 1.2).

## Agent does not search on its own

| Symptom | Cause | Fix |
|---|---|---|
| Tools are listed, but the agent answers without `search_installed_skills` | No activation trigger for that client | Check `activation` in `--configs`; re-run `init` without `--no-activation`, or follow the manual steps in SKILL.md 1.6. |
| Searches only after exploring the repository | Rule file present, but rules are weaker than a hook | Expected for rule-based clients. The user can say "search skills first". |
| Searches on small talk | Over-eager model | Harmless; the search returns nothing relevant. |

## Server will not connect

| Symptom | Cause | Fix |
|---|---|---|
| Tools missing after setup | Client not restarted | Restart the client. Run `--probe` to confirm the server itself starts. |
| Works in a terminal, not in Claude Desktop or Cursor | GUI app cannot see the shell PATH | Use the absolute path of `uvx`, `uv` or `skills-router` in `command`. |
| `--probe` says `uv could not fetch the git source` | Unpinned (A') or tag-pinned (`@v1.0.0`) uvx source and no network | Restore the network, or pin the release's full commit SHA (SKILL.md 1.2). |
| `--probe` times out on first run | uv is cloning and building the package | Re-run with `--timeout 90`. |
| `launcher: ephemeral-cache-path` | `init` ran under uvx without `--server-command` | Re-run `init` with `--server-command` (SKILL.md 1.3). |
| Every client broke at once | Checkout registered via `uv run --directory` was moved or deleted | Switch to A or C and re-run `init`, or fix `--directory`. |
| `` `claude mcp add` falhou `` | Claude CLI error | Run the `claude mcp add` line from SKILL.md 1.5 and read its error. |
| `não é JSON puro; registre o servidor manualmente` | Config has comments (JSONC) or is invalid | Merge the snippet from SKILL.md 1.5 by hand. `--configs` shows `unparsable`. |

## Skill not found or not indexed

| Symptom | Cause | Fix |
|---|---|---|
| Search returns nothing | Portuguese, single-letter or stopword-only query | Use English nouns: tool, framework, file type, deliverable. |
| Skill exists but never ranks | Vague `description`, no tags, generic name | Rewrite the frontmatter as in `reference/architecture.md`. |
| `Skill 'x' não encontrada. Parecidas: ...` | Name typo | Use one of the suggested names or search. Folder names also work as aliases. |
| `aponta (via symlink) para fora do diretório oficial` | Skill folder or `SKILL.md` symlinked from outside `skills/` | Copy the skill into `~/.ai-skills/skills/`. Symlinks inside it still work. |
| `tem hardlinks; copie o arquivo` | Hard-linked `SKILL.md` (some dotfile and sync tools do this) | Replace it with a plain copy: `cp SKILL.md tmp && mv tmp SKILL.md`. |
| `não é um arquivo regular` / `symlink quebrado` | FIFO, device or dangling link | Replace with a real file. |
| Two skills, one invisible | Same `name` (case-insensitive); the first in sorted folder order wins | Rename one. `rebuild_index()` lists `duplicates_ignored`. |
| Sub-skill not indexed | Wrong depth | Only `<skill>/SKILL.md` and `<pkg>/skills/<sub>/SKILL.md` are indexed. |
| Hidden folder ignored | Top-level folder names starting with `.` are skipped | Rename the folder. |
| Project skills (`.claude/skills` in a repo) not found | The router indexes only `~/.ai-skills/skills` | Move them there or keep them native. The health check lists them under `native_unrouted`. |
| Description looks wrong | Frontmatter missing, invalid YAML, over 16 KB, nested over 32 levels, or uses YAML aliases | The server falls back to a line parser or the first paragraph. Fix the YAML; never use `&anchor` / `*alias`. |
| `content` cut short | `SKILL.md` over 512 KB, so `truncated: true` | Move detail into supporting files under `reference/`. |

## Installer and folders

| Symptom | Cause | Fix |
|---|---|---|
| `é um symlink externo; não foi alterado` | Client skills folder is a symlink to dotfiles | Expected. Migrate those skills by hand or replace the link with a real folder. |
| `já existe e não é gerenciado; link não criado` | A real folder has the pinned skill's name | Move that folder into `~/.ai-skills/skills` and re-run `skills-router pin NAME`. |
| `Skill fixada 'x' não existe` | Pinned name not in the index | Check the spelling with `skills-router search x`. |
| `Conflito em 'x'` | Same name, different content in two clients | First version kept; the other is in `~/.ai-skills/conflicts/<client>/x`. Ask the user which wins. |
| `Mantido no lugar (não é skill)` | Folder without `SKILL.md` | Expected; never moved. |
| `Cursor: as regras globais ficam na interface` | Cursor has no global rule file | Manual step in SKILL.md 1.6. |

## Drafts and promotion

| Message | Fix |
|---|---|
| `Nome de skill inválido` | Use `^[a-z0-9][a-z0-9-]{0,63}$`. |
| `A descrição é obrigatória` / `no máximo 1024 caracteres` | Provide a description up to 1024 chars. |
| `Conteúdo grande demais` / `O conteúdo da skill está vazio` | Body must be non-empty and at most 256 KB. |
| `Não existe rascunho` | No `drafts/<name>/SKILL.md`. List drafts with `skills-router drafts`. |
| `Promoção via MCP desativada nesta máquina` | `SKILLS_ROUTER_PROMOTE` locks it. The user runs `skills-router promote NAME`. |
| `Já existe uma skill oficial chamada` | Folder or frontmatter name already taken. Pick a new name or have the user remove the old skill. |
| `Rascunho recusado: contém symlink` / `arquivo especial ou com hardlink` / `outras skills aninhadas` | Drafts may hold only regular files and one top-level `SKILL.md`. |
| `O frontmatter declara name=... diferente da pasta` | Make the frontmatter `name` equal the draft folder name. |
| `é um symlink ou está fora do runtime` | `drafts/` or `skills/` is a symlink. Make them real folders under the runtime. |
