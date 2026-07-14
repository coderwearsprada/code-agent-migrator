# code-agent-migrator

A single-file Python script that migrates settings and custom configuration
between [Claude Code](https://claude.com/claude-code) (`~/.claude`),
[Codex CLI](https://github.com/openai/codex) (`~/.codex`),
[Cursor](https://cursor.com) (`~/.cursor`),
[opencode](https://opencode.ai) (`~/.config/opencode`), and
[pi](https://pi.dev) (`~/.pi/agent`), in any pairwise direction — with an
upfront backup of every file it will touch and a `--restore` command to
undo a run.

Requires Python 3.9+. No third-party dependencies. On 3.11+ it uses the
stdlib `tomllib`; on 3.9/3.10 it falls back to a small bundled TOML reader
covering the subset Codex's `config.toml` uses.

**Tested against:** Claude Code `2.1.207`, Codex CLI `0.137.0` (schemas
cross-checked against the 0.144 docs), Cursor 2.4+/3.x schemas, opencode
1.x schemas (config.json schema + docs), and pi 0.80.x docs, all as of
2026-07. The script reads documented config schemas, so minor version bumps
should keep working; if a future release renames or removes a key, the
migrator will flag it as "not translated" in the report rather than corrupt
your config.

> Codex CLI 0.140+ ships its own interactive `/import` command for pulling
> Claude Code config in. This migrator remains useful for the other five
> directions, for scriptable/non-interactive runs, and for the backup +
> restore safety net.

## Usage

```bash
# Any pairwise direction between {claude, codex, cursor, opencode, pi}.
python3 migrate.py --from claude --to codex
python3 migrate.py --from cursor --to claude
python3 migrate.py --from codex  --to cursor
python3 migrate.py --from claude --to opencode
python3 migrate.py --from opencode --to codex
python3 migrate.py --from pi --to claude

# Project-level instead of user-level (also accepts --scope both)
python3 migrate.py --from claude --to cursor --scope project

# Explicit paths (overrides --scope)
python3 migrate.py --from claude --to cursor \
    --claude-dir /path/to/.claude --cursor-dir /path/to/.cursor

# Preview without writing anything
python3 migrate.py --from claude --to cursor --dry-run

# Revert the most recent migration (or pass a specific backup directory)
python3 migrate.py --restore
python3 migrate.py --restore /path/to/backups/pre-migrate-YYYYMMDD-HHMMSS
```

### Flags

| Flag | Meaning |
|---|---|
| `--from {claude,codex,cursor,opencode,pi}` / `--to {claude,codex,cursor,opencode,pi}` | Source and destination tools. Required unless `--restore` is given. |
| `--restore [BACKUP_DIR]` | Reverse a previous migration. Omit to use the latest backup found under any tool's backups dir. |
| `--scope {user,project,both}` | Which config scope(s) to migrate (default: `user`). |
| `--claude-dir PATH` / `--codex-dir PATH` / `--cursor-dir PATH` / `--opencode-dir PATH` / `--pi-dir PATH` | Explicit config dirs; overrides `--scope`. |
| `--dry-run` | Print the plan and report, write nothing. |
| `--merge` / `--overwrite` | Merge into existing destination files where sensible (default), or replace outright. Backups happen either way. |
| `--no-backup` | Skip the upfront backup (and disable `--restore` for this run). Not recommended. |
| `--no-interactive` | Don't prompt for Tier B confirmations; combine with `--apply-lossy`/`--skip-lossy`. |
| `--apply-lossy=IDS` / `--skip-lossy=IDS` | Comma-separated Tier B option IDs (or `all`). IDs: `permissions`, `sandbox`, `rules`, `hooks`, `codex_hooks`, `notify`, `agents`, `profiles`, `agents_cursor`, `hooks_cursor`, `cursor_hooks`, `permissions_opencode`, `opencode_permissions`, `rules_opencode`, `opencode_rules`, `agents_opencode`. Unknown IDs (including retired ones) warn and are ignored. |

After each run the script writes `MIGRATION_REPORT.md` at the destination,
split into: migrated cleanly (Tier A), migrated with loss (Tier B, user-
confirmed), skipped by user choice, and not translated (no equivalent).

## What gets translated

### Tier A — clean, always applied

**Skills (every direction)**

All supported tools speak the same open [Agent Skills](https://agentskills.io)
format, so `skills/<name>/` directories copy verbatim — SKILL.md,
frontmatter, and bundled assets included. Codex's tool-managed
`skills/.system/` is excluded, opencode's legacy singular `skill/` dir is
read (the plural is written), and pi's bare `skills/*.md` files become
`<name>/SKILL.md` dirs with synthesized frontmatter when needed. (Newer
Codex versions also discover the vendor-neutral `~/.agents/skills/`, and
opencode and pi read `.claude/skills` / foreign skill dirs natively; this
migrator writes to each tool's own `skills/` dir, which all tested
versions read.)

**Claude Code ↔ Codex CLI**

| Claude Code                            | Codex CLI                                  |
|----------------------------------------|--------------------------------------------|
| `CLAUDE.md`                            | `AGENTS.md`                                |
| `commands/*.md` (`description`, `argument-hint` as native frontmatter) | `prompts/*.md` |
| `settings.json:model`                  | `config.toml:model`                        |
| `.mcp.json` / `~/.claude.json`: `mcpServers` (stdio + HTTP) | `config.toml:[mcp_servers.*]` |
| `settings.json:env`                    | `config.toml:[shell_environment_policy] set` |
| `settings.json:effortLevel`            | `config.toml:model_reasoning_effort`       |
| `outputStyle` file contents *(c→x only)*  | fenced block inside `AGENTS.md`         |
| `CLAUDE.md` imports `AGENTS.md` *(project x→c)* | `AGENTS.md`                    |
| fenced block inside `CLAUDE.md` *(x→c only)* | `config.toml:instructions`           |
| `agents/*.md` *(x→c: from `.codex/agents/*.toml`)* | `agents/*.toml`               |

**Cursor ↔ Claude / Codex**

| Cursor                                 | Claude Code              | Codex CLI                   |
|----------------------------------------|--------------------------|-----------------------------|
| `<root>/mcp.json:mcpServers`           | `.mcp.json` / `~/.claude.json`: `mcpServers` | `config.toml:[mcp_servers.*]` |
| `.cursor/rules/*.mdc` + `.cursorrules` | `.claude/rules/*.md`     | `AGENTS.md`                 |
| `cli-config.json:model`                | `settings.json:model`    | `config.toml:model`         |
| `agents/*.md` *(cursor→claude only; Tier B the other way)* | `agents/*.md` | —      |
| `skills/<name>/SKILL.md` with `disable-model-invocation` *(into cursor)* | ← `commands/*.md` | ← `prompts/*.md` |

Cursor user scope (`~/.cursor`) has global MCP, skills, agents, hooks, and
the CLI config — Cursor has no user-level rules file. Project-scope rules
go to/from `<project>/.cursor/`. The legacy `.cursorrules` (plain markdown
at project root) is read on the way out and re-emitted as a single
`.cursor/rules/_cursorrules_legacy.mdc` on the way in. Cursor rule
frontmatter maps to native Claude rule frontmatter (`globs` → `paths`),
with a small migrator metadata comment for Cursor-only fields such as
`alwaysApply`.

**opencode ↔ Claude / Codex / Cursor**

| opencode                               | Maps to |
|----------------------------------------|---------|
| `AGENTS.md` (global or project)        | `CLAUDE.md` / `AGENTS.md` / cursor rules. opencode reads project `AGENTS.md` and even `CLAUDE.md` natively, so same-file cases become a report note instead of a copy. |
| `opencode.json:model` (`provider/model`) | `settings.json:model` / `config.toml:model` / `cli-config.json:model` — the provider prefix is added from the source tool (`anthropic/`, `openai/`) or stripped on the way out, with a note when the provider doesn't match the destination. |
| `opencode.json:mcp` (`local` argv / `remote` url) | `mcpServers` (stdio/http) / `[mcp_servers.*]` |
| `commands/*.md` (`description`)        | Claude `commands/`, Codex `prompts/`, Cursor slash-invocable skills. `argument-hint` rides in a migrator:meta comment; opencode's `agent`/`model`/`subtask` keys are dropped with notes. |
| `agents/*.md` (`description`, `mode`, `model`) | Claude/Cursor subagent markdown or Codex agent TOML — Tier A out of opencode; claude→opencode is Tier B (`agents_opencode`) since Claude's `tools`/`hooks`/`memory` fields don't carry. |

opencode's config is read from `opencode.json`/`opencode.jsonc` (comments
and trailing commas handled); the migrator always writes plain
`opencode.json`. Singular legacy dirs (`agent/`, `command/`, `skill/`) are
read; plural canonical dirs are written.

**pi ↔ everything**

| pi (`~/.pi/agent`, project `.pi/`)     | Maps to |
|----------------------------------------|---------|
| `AGENTS.md`                            | `CLAUDE.md` / `AGENTS.md` / cursor rules. pi reads AGENTS.md *and* CLAUDE.md natively, so same-file project cases become a report note. |
| `prompts/*.md` (`description`, `argument-hint`) | Claude `commands/`, Codex `prompts/` (verbatim — the formats are identical), Cursor slash-invocable skills, opencode `commands/`. |
| `settings.json:defaultProvider` + `defaultModel` | `settings.json:model` / `config.toml:model` / `cli-config.json:model` / opencode's `provider/model` — the provider field makes this the cleanest model mapping of any pair; cross-provider moves get a review note. |
| `settings.json:defaultThinkingLevel`   | `effortLevel` / `model_reasoning_effort`. pi's scale is a superset (`off`…`xhigh`, `max`): `max` maps down to `xhigh` with a note, `off` is reported, and Codex's `minimal` survives a round trip. |

pi deliberately has **no MCP, no subagents, and no hooks** (extensions are
TypeScript code). Migrating *to* pi reports those source features as not
translated — MCP servers are suggested as CLI-tool skills, subagents point
at the `pi-subagents` community package — instead of silently dropping
them. That also means every pi pair is Tier A only: there are no pi Tier B
options to confirm.

Notes:

- Cursor deprecated `.cursor/commands/` in favor of skills, so Claude
  slash commands and Codex prompts land in Cursor as skills with
  `disable-model-invocation: true` — still invocable from the `/` menu.
  `argument-hint` rides along in a `<!-- migrator:meta ... -->` comment.
- Codex prompts natively support `description` and `argument-hint`
  frontmatter now, so commands↔prompts round-trip through real
  frontmatter. Prompts migrated by older versions of this tool (meta
  comment instead of frontmatter) are still read. Other command
  frontmatter keys (`model`, `allowed-tools`, …) are dropped and logged.
- Codex `instructions` (TOML string) and Claude `outputStyle` files
  (markdown) are different shapes for similar things, so they're embedded
  inside the target's instruction document as a `<!-- migrator:begin ... -->`
  fenced block that the reverse direction can unwrap.
- MCP server transports: **stdio** transfers everywhere. **Streamable
  HTTP** (`url` + `headers`) also transfers everywhere — Codex stores it
  as `url`/`http_headers`. Legacy **SSE** and **WebSocket** servers
  transfer between Claude Code and Cursor but are skipped going to Codex,
  with a report note. Codex-only auth fields (`bearer_token_env_var`,
  `env_http_headers`) don't leave Codex.
- Effort levels (Claude `effortLevel` ↔ Codex `model_reasoning_effort`)
  share the `low`/`medium`/`high`/`xhigh` vocabulary and map 1:1. Codex's
  extra `minimal` maps to Claude `low`; the legacy Claude `max` alias (the
  pre-2.1 name for `xhigh`) maps to Codex `xhigh`. Cursor has no global
  reasoning-effort knob, so this field is reported but not carried (agent
  files use Cursor's `model[effort=…]` bracket syntax, which does map).
- At project scope, Cursor reads `AGENTS.md` natively, so codex→cursor
  leaves it in place and notes that instead of splitting it into rules.

### Tier B — lossy, user-confirmed

These don't have an exact equivalent on the destination side. The preflight
scan shows a one-line preview and rationale for each detected item and
lets you accept or skip per-item (interactively, or via
`--apply-lossy`/`--skip-lossy`). Options are grouped below by **source tool**.

#### From Claude Code (`--from claude`)

| Target | ID | Translation | Why lossy |
|---|---|---|---|
| Codex  | `permissions`     | `permissions.allow/deny/ask` → `sandbox_mode` + `approval_policy` + `rules/default.rules` | Overall posture collapses into coarse sandbox modes; `Bash(...)` rules become Starlark `prefix_rule()` entries (exact-match rules widen to prefix matches); `Write()` patterns become `writable_roots`; `WebFetch`/`WebSearch` deny becomes `network_access=false`. |
| Codex  | `hooks`           | `hooks` → `.codex/hooks.json` + `Notification` → `notify` | Codex hooks share Claude's event names/shape, so command hooks on shared events (PreToolUse, PostToolUse, Stop, …) translate near-verbatim. Non-command hook types (http, mcp_tool, prompt, agent) and Claude-only events (SessionEnd, FileChanged, …) are dropped. |
| Codex  | `agents`          | `agents/*.md` → `.codex/agents/*.toml` | Claude subagents become Codex custom agents. `name`, `description`, `model`, `effort`, and selected `permissionMode` values map to TOML; skills/tool lists become prompt guidance for review. |
| Cursor | `agents_cursor`   | `agents/*.md` → `.cursor/agents/*.md` | Cursor 2.4+ runs subagents natively. `name`/`description`/`model` map; `effort` folds into Cursor's `model[effort=…]`; `background` → `is_background`; `permissionMode: readOnly/plan` → `readonly`. Claude-only fields (`tools`, `hooks`, `memory`, `skills`, …) are dropped with in-file notes. |
| Cursor | `hooks_cursor`    | `hooks` → `.cursor/hooks.json` | Cursor hooks use camelCase events and a flat shape. Command hooks on shared events translate; matchers, non-command hook types, and Claude-only events are dropped. |
| opencode | `permissions_opencode` | `permissions` → `opencode.json:permission` | Claude `Bash(cmd:*)` prefix rules become opencode glob patterns (`"cmd*"`); `Write`/`Read`/`WebFetch` wildcards become tool-level actions. The intent carries, but opencode is last-match-wins where Claude is most-specific-wins. |
| opencode | `agents_opencode` | `agents/*.md` → opencode `agents/*.md` | opencode runs subagents natively (`mode: subagent`). `name`/`description`/`model` map (provider-qualified); `readOnly`/`plan` permission modes become `permission: edit/bash deny`; Claude-only fields are dropped with in-file notes. |

#### From Codex CLI (`--from codex`)

| Target | ID | Translation | Why lossy |
|---|---|---|---|
| Claude | `sandbox`         | `sandbox_mode`/`approval_policy` → `permissions.allow`/`deny` | Coarse modes expanded into Claude wildcard patterns. Round-trip is semantic, not byte-identical. |
| Claude | `rules`           | `rules/*.rules` → `permissions` `Bash(...)` patterns | Starlark `prefix_rule()` entries map to `Bash(cmd:*)` allow/ask/deny rules (`allow`→allow, `prompt`→ask, `forbidden`→deny). Prefix semantics are approximated with `:*`; union pattern elements can't be translated. |
| Claude | `codex_hooks`     | `hooks.json` → `settings.json:hooks` | Command hooks on shared events translate near-verbatim; `commandWindows` variants and non-command hook types are dropped. |
| Claude | `notify`          | `notify` argv → `hooks.Notification` | Becomes a single-command Claude hook with no matcher (a `/bin/sh -c` wrapper added by a previous claude→codex run is unwrapped). |
| Claude | `profiles`        | `<name>.config.toml` (and legacy `[profiles.*]`) → `~/.claude/profiles/NAME.settings.json` | Claude has no profile runtime; each profile is materialized as a standalone settings file you can copy over `settings.json` to activate. |
| opencode | `rules_opencode` | `rules/*.rules` → `opencode.json:permission.bash` | Codex prefix rules become opencode glob patterns (`allow`→allow, `prompt`→ask, `forbidden`→deny). Union pattern elements can't be translated. |

#### From Cursor (`--from cursor`)

| Target | ID | Translation | Why lossy |
|---|---|---|---|
| Claude | `cursor_hooks`    | `hooks.json` → `settings.json:hooks` | Command hooks on shared events translate; Cursor prompt-type hooks and Cursor-only events (`beforeShellExecution`, MCP interception, tab hooks, …) are dropped. |

#### From opencode (`--from opencode`)

| Target | ID | Translation | Why lossy |
|---|---|---|---|
| Claude | `opencode_permissions` | `permission` → `settings.json:permissions` | opencode glob patterns become Claude prefix rules; mid-pattern wildcards and per-path `edit` maps can't be translated. |
| Codex  | `opencode_rules`  | `permission.bash` → `rules/default.rules` | opencode bash glob patterns become Codex `prefix_rule()` entries; mid-pattern wildcards can't be expressed as prefixes. |

Everything else cursor→claude and cursor→codex is clean Tier A: rules, MCP
servers, skills, subagents (to Claude), and the CLI default model translate
directly, and rule frontmatter (`description`/`globs`/`alwaysApply`)
round-trips through native Claude rules plus a migrator metadata comment,
or through fenced metadata in `AGENTS.md` for Codex.

### Tier C — not translated

Listed in `MIGRATION_REPORT.md` so you know to recreate them by hand:

- **Claude-only:** `statusLine`, `plugins/`, theme, `fallbackModel`,
  `availableModels`, `autoMode`, auto-memory settings, sandbox settings,
  slash-command `model`/`allowed-tools` frontmatter, and hook events with
  no counterpart on the destination. When migrating to Cursor, also:
  `permissions` (Cursor's CLI has its own allow/deny rule files —
  `~/.cursor/cli-config.json` / `.cursor/cli.json` — rebuild by hand),
  `effortLevel`, `env`, `outputStyle`.
- **Codex-only:** `model_provider(s)`, `web_search`, `features`,
  `default_permissions` / named `[permissions.*]` profiles,
  `disable_response_storage` / history persistence, `tui` settings,
  `hide_agent_reasoning`, `project_doc_max_bytes`,
  `project_doc_fallback_filenames`, `[agents]`/`[memories]`/`[apps]`/
  `[plugins]` tables. When migrating to Cursor, also: `approval_policy`,
  `sandbox_mode`, `sandbox_workspace_write`, `shell_environment_policy`,
  `profiles`, `model_reasoning_effort`, `notify`.
- **Cursor-only:** Cursor IDE settings (`User/settings.json`),
  keybindings, extensions list, in-app global rules, plugins
  (`.cursor-plugin/` manifests differ from Claude plugins — reinstall from
  the marketplace), `permissions.json` MCP/terminal allowlists, and
  `cli-config.json` keys other than `model` — out of scope (IDE config,
  not agent config).
- **opencode-only:** `provider` blocks, `small_model`, `plugin` (plugins
  are TypeScript code, not translatable config — the reason Claude hooks
  also can't land in opencode), `formatter`, `lsp`, `theme`/`keybinds`
  (tui.json), `share`, `autoupdate`, `snapshot`, `compaction`,
  `instructions` file references (noted so you can copy the files).
- **pi-only:** `SYSTEM.md`/`APPEND_SYSTEM.md` (system-prompt replacement),
  `keybindings.json`, `models.json` (custom providers — may embed
  secrets), themes, `extensions/` (TypeScript code), and settings keys
  like `steeringMode`, `compaction`, `thinkingBudgets`, `packages`.

## What is never touched

The script ignores state, secrets, and caches on the source side, including:

- **Claude:** `.credentials.json`, `history.jsonl`, `sessions/`, `projects/`,
  `file-history/`, `cache/`, `paste-cache/`, `shell-snapshots/`,
  `telemetry/`, `mcp-needs-auth-cache.json`
- **Codex:** `auth.json`, `history.jsonl`, `sessions/`, `log/`,
  `version.json`, `*.sqlite*` state (memories, goals, logs),
  `skills/.system/` (tool-managed system skills)
- **Cursor:** the OS-specific user settings dir (`User/settings.json`,
  keybindings, extensions, workspace storage) — anywhere outside
  `<root>/mcp.json`, `<root>/rules/*.mdc`, `<root>/skills/`,
  `<root>/agents/`, `<root>/hooks.json`, and `cli-config.json:model`
- **opencode:** `~/.local/share/opencode/` (auth.json, mcp-auth.json,
  session storage, logs) and `~/.cache/opencode/` — everything outside
  `opencode.json[c]`, `AGENTS.md`, and the agents/commands/skills dirs
- **pi:** `~/.pi/agent/auth.json`, `sessions/`, `trust.json`, and the
  `npm/`/`git/` package caches

## How a migration runs

1. **Preflight scan** — detect Tier B translations applicable to your
   source and confirm each one. Tier A items are always applied.
2. **Plan pass** — walk the migration once without writing anything,
   collecting the complete list of destination files that will be touched.
3. **Backup** — copy every planned destination that already exists into
   `<dst>/backups/pre-migrate-<timestamp>/`, preserving relative layout,
   and write a `manifest.json` that `--restore` later reads. Files the
   migration will *create* (vs. modify) are recorded as
   `existed_before: false` so restore can delete them on revert.
4. **Confirm** — show the list of planned changes plus the backup
   location and ask one final time.
5. **Apply** — write for real; produce `MIGRATION_REPORT.md` at the
   destination.

## Restoring a migration

`--restore` reverses a previous migration using the backup manifest: files
that existed before are copied back from the backup byte-for-byte, and
files the migration created are deleted.

```bash
python3 migrate.py --restore                    # latest backup found
python3 migrate.py --restore /path/to/backup    # specific run
python3 migrate.py --restore --dry-run          # preview only
```

## Tests

```bash
python3 -m unittest discover -s tests
```

115 tests, stdlib-only. They cover the TOML writer, JSONC stripping,
frontmatter and fenced-block round-trips, MCP normalization (stdio +
streamable HTTP + opencode local/remote), every Tier A direction across
all 20 tool pairs, skills tree copies (byte-identical round-trip, the
Codex system-skills exclusion, pi bare-file conversion), the slash-command
`description`/`argument-hint` round-trips (including through opencode and
pi), prefix-rule emission/parsing and its round-trips (Claude and
opencode), permission-map translation both ways, agent round-trips through
opencode, hooks translation to Codex and Cursor, pi thinking-level
mapping and feature-gap reporting, MDC frontmatter + legacy `.cursorrules`
parsing, every Tier B heuristic, the plan-mode contract, the
backup-then-restore round-trip, and a full cursor→claude→cursor metadata
round-trip. Verified on Python 3.9 and 3.13. The generated Codex prefix
rules were additionally validated against the real `codex execpolicy
check` tool.

## License

Apache License 2.0
