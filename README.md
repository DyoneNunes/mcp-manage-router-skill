# mcp-manage-router-skill

**MCP Server + Meta-Skill de roteamento de skills.**

Quem acumula dezenas ou centenas de skills paga por isso em toda sessão: cada cliente
(Claude Code, Gemini CLI, Cursor...) carrega a descrição de todas elas no contexto. Este
projeto troca esse custo por uma busca:

- as skills ficam numa **fonte única** (`~/.ai-skills/skills`), fora do contexto;
- um **servidor MCP** indexa os `SKILL.md` e responde buscas com metadados resumidos;
- uma **meta-skill** (`skill-orchestrator`) ensina o agente a buscar, reaproveitar e
  combinar skills — e a nunca criar uma nova sem aprovação humana.

O agente passa a enxergar nativamente só o orquestrador e as skills que você fixar.

## Instalação

Requer [uv](https://docs.astral.sh/uv/) e Python 3.10+.

```bash
git clone <url-do-repositorio> mcp-manage-router-skill
cd mcp-manage-router-skill
uv run skills-router init --dry-run   # mostra o que seria feito
uv run skills-router init             # executa
```

Ou como ferramenta global: `uv tool install <url-ou-caminho>` e depois `skills-router init`.

### O que o `init` faz

1. Cria `~/.ai-skills/skills` e `~/.ai-skills/drafts`.
2. Instala a meta-skill em `~/.ai-skills/skills/skill-orchestrator/SKILL.md`.
3. Detecta os clientes instalados e **importa** as skills que eles já têm para a fonte
   única. Os originais não são apagados: vão para `~/.ai-skills/backups/<data>/`, com um
   manifesto que permite desfazer tudo.
4. Deixa na pasta de skills de cada cliente apenas symlinks para o orquestrador e para as
   skills fixadas (`--pin`).
5. Registra o servidor MCP `skills-router` na config global de cada cliente (com cópia de
   segurança do arquivo antes de editar).
6. Gera o `index.json` inicial.

Opções úteis:

| Opção | Efeito |
|---|---|
| `--dry-run` | Só relata; não altera nada. |
| `--pin NOME` | Mantém a skill nativa nos clientes (repetível). |
| `--expose all` | Em vez de links individuais, a pasta do cliente vira symlink para `~/.ai-skills/skills` (unifica, mas não economiza contexto). |
| `--clients claude,cursor` | Restringe os clientes tratados. |
| `--server-command "..."` | Define o comando usado pelos clientes para subir o servidor. |

Clientes suportados:

| Cliente | Pasta de skills | Config MCP |
|---|---|---|
| Claude Code | `~/.claude/skills` | `claude mcp add --scope user` (ou `~/.claude.json`) |
| Gemini CLI | `~/.gemini/skills` | `~/.gemini/settings.json` |
| Cursor | `~/.cursor/skills` | `~/.cursor/mcp.json` |
| Codex CLI | `~/.codex/skills` | `~/.codex/config.toml` |
| opencode | `~/.config/opencode/skills` | `~/.config/opencode/opencode.json` |
| Antigravity | `~/.gemini/config/skills` | `~/.gemini/config/mcp_config.json` |
| `~/.agents` (compartilhado) | `~/.agents/skills` | — |

Itens que não são skills (pastas sem `SKILL.md`, diretórios ocultos como `.system`) nunca
são movidos. Se dois clientes tiverem skills homônimas com conteúdo diferente, a primeira
é mantida e a outra vai para `~/.ai-skills/conflicts/` para você decidir.

## Ambiente local (`~/.ai-skills/`)

```
~/.ai-skills/
├── skills/        fonte única de verdade (uma pasta por skill, com SKILL.md)
├── drafts/        quarentena de rascunhos aguardando aprovação humana
├── index.json     cache leve: name, description, tags e path de cada skill
├── config.json    preferências desta máquina (skills fixadas, clientes)
└── backups/       originais movidos pelo init + manifesto para o restore
```

Defina `AI_SKILLS_HOME` para usar outro diretório. Pacotes que agrupam sub-skills em
`<pacote>/skills/<sub>/SKILL.md` também são indexados.

## Tools do servidor MCP

| Tool | O que faz |
|---|---|
| `search_installed_skills(query, limit=5)` | Busca por relevância no índice; devolve só `name`, `description`, `tags`, `path` e `score`. |
| `get_skill_content(skill_name)` | Devolve o `SKILL.md` completo da skill escolhida e a lista de arquivos de apoio. |
| `rebuild_index()` | Força a reindexação. |
| `propose_skill_draft(name, description, content)` | Salva um rascunho **somente** em `drafts/<name>/SKILL.md`. |
| `promote_draft(skill_name)` | Move o rascunho para `skills/` e reindexa. |

O índice se atualiza sozinho: na subida do servidor e a cada busca, os `SKILL.md` são
comparados (caminho, data e tamanho) e a reindexação só ocorre se algo mudou.

## Meta-skill `skill-orchestrator`

Distribuída em [`templates/skill-orchestrator/SKILL.md`](templates/skill-orchestrator/SKILL.md).
Regras centrais:

1. **Gatilho** — em tarefas de desenvolvimento, arquitetura ou automação, buscar primeiro.
2. **Gatekeeping** — reaproveitar → combinar → só então apontar a lacuna.
3. **Human-in-the-loop** — nunca criar skill automaticamente; rascunho e promoção exigem
   duas confirmações explícitas e separadas.

## Outros comandos

```bash
skills-router status            # resumo do ambiente
skills-router search fastapi postgres
skills-router pin NOME          # expõe uma skill nativamente
skills-router unpin NOME
skills-router drafts            # rascunhos em quarentena
skills-router promote NOME      # promoção feita por você, pelo terminal
skills-router reindex
skills-router restore           # desfaz o init a partir do último backup
```

## Privacidade

O repositório não guarda nada sobre quem o usa. Caminhos saem de `$HOME`/`AI_SKILLS_HOME`,
as preferências ficam em `~/.ai-skills/config.json`, e o autor gravado nos rascunhos é lido
em tempo de execução do `git config --global user.name` (ou do usuário do sistema).
`index.json`, rascunhos e backups nunca são versionados.

## Desenvolvimento

```bash
uv sync
AI_SKILLS_HOME=/tmp/ai-skills-teste uv run skills-router status
uv run skills-router serve      # servidor MCP em stdio
```

## Licença

[MIT + Commons Clause v1.0](LICENSE) © 2026 Dyone Andrade — <contato@dyonedev.com.br>

O código é aberto e gratuito, mas **não pode ser vendido**.

| Pode | Não pode |
|---|---|
| Usar gratuitamente, em casa ou dentro da sua empresa | Vender o software, no todo ou em parte |
| Estudar, copiar e modificar | Cobrar por produto ou serviço cujo valor venha substancialmente dele (hospedagem, consultoria ou suporte pagos) |
| Redistribuir gratuitamente, mantendo a licença e o copyright | Remover ou alterar o aviso de licença |

Por causa da restrição de venda, esta não é uma licença "open source" na definição da
OSI; o termo correto é *source-available*. Para uso comercial fora desses termos, fale com
o autor.
