#!/usr/bin/env python3
"""
migrate.py — Migrate settings + custom configuration between Claude Code
(~/.claude), Codex CLI (~/.codex), Cursor (~/.cursor), opencode
(~/.config/opencode), and pi (~/.pi/agent), in any pairwise direction.

Requires Python 3.9+. No third-party dependencies.

Usage:
    python3 migrate.py --from claude --to codex
    python3 migrate.py --from codex --to claude
    python3 migrate.py --restore [BACKUP_DIR]      # revert a previous run
    # See --help for full options.

Design overview
---------------
Migration runs in five phases:

  1. **Preflight scan**  — detect Tier B (lossy) translations and let the
     user accept/skip each one. Tier A (clean) items are always applied.
  2. **Plan pass**       — re-run the migration with `Ctx.plan_mode=True`;
     write helpers record destination paths into `Ctx.planned_writes`
     instead of touching the filesystem. This produces a complete list of
     destination files *before* any writes happen.
  3. **Backup**          — copy every planned path that already exists into
     `<dst>/backups/pre-migrate-<timestamp>/`, preserving relative layout,
     and write a `manifest.json` recording each entry's `existed_before`
     flag. `--restore` later uses this to reverse the migration.
  4. **Confirm**         — show the user the planned changes and ask one
     final time before any writes.
  5. **Apply pass**      — run the migration again with `plan_mode=False`;
     write helpers actually write. Each function re-reads the destination
     from disk, so layered writes (e.g., Tier A then Tier B both touching
     settings.json) compose correctly.

The round-trippable pieces of content (Codex `instructions`, Claude
`outputStyle` files, and slash-command frontmatter carried into targets
without native support for it) ride along inside HTML comments that the
reverse direction recognizes and unwraps. See the `fenced_block` /
`extract_fenced` and `frontmatter_to_meta_comment` /
`meta_comment_to_frontmatter` helpers.

Skills need none of that: Claude Code, Codex CLI, and Cursor all speak the
open Agent Skills format (skills/<name>/SKILL.md + assets), so skills copy
verbatim in every direction.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# ----------------------------------------------------------------------------
# TOML reader compatibility shim
# ----------------------------------------------------------------------------
# Python 3.11+ ships `tomllib` in the stdlib. On 3.9/3.10 we fall back to a
# small hand-rolled parser covering the subset Codex's `config.toml` uses:
# top-level scalars (strings, bools, ints, floats), `[table]` and
# `[table.sub]` headers, arrays of scalars, inline `{ k = v, ... }` tables,
# and `# ...` comments. Anything more exotic (multi-line strings, dates,
# `[[arrays.of.tables]]`, dotted keys, hex/octal numbers) is unsupported —
# the migrator doesn't need any of it.

try:
    import tomllib  # type: ignore[import-not-found]  # Python 3.11+
except ModuleNotFoundError:
    class _MinimalToml:
        class TOMLDecodeError(ValueError):
            pass

        @classmethod
        def loads(cls, text: str) -> dict:
            return _toml_minimal_parse(text, cls.TOMLDecodeError)

    def _toml_strip_comment(line: str) -> str:
        """Strip `# ...` comments, respecting `"..."` strings."""
        in_str = False
        esc = False
        for i, c in enumerate(line):
            if esc:
                esc = False
                continue
            if c == "\\" and in_str:
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if c == "#" and not in_str:
                return line[:i].rstrip()
        return line.rstrip()

    def _toml_skip_ws(s: str, i: int) -> int:
        while i < len(s) and s[i] in " \t":
            i += 1
        return i

    def _toml_parse_string(s: str, i: int, err) -> tuple[str, int]:
        assert s[i] == '"'
        i += 1
        out: list[str] = []
        while i < len(s):
            c = s[i]
            if c == "\\":
                if i + 1 >= len(s):
                    raise err("unterminated escape in string")
                nxt = s[i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r",
                            "\\": "\\", '"': '"', "/": "/"}.get(nxt, nxt))
                i += 2
            elif c == '"':
                return "".join(out), i + 1
            else:
                out.append(c)
                i += 1
        raise err("unterminated string")

    def _toml_parse_value(s: str, i: int, err) -> tuple[object, int]:
        i = _toml_skip_ws(s, i)
        if i >= len(s):
            raise err("expected value")
        c = s[i]
        if c == '"':
            return _toml_parse_string(s, i, err)
        if c == "[":
            return _toml_parse_array(s, i, err)
        if c == "{":
            return _toml_parse_inline_table(s, i, err)
        # Bare token: bool / int / float.
        start = i
        while i < len(s) and s[i] not in " \t,]}#":
            i += 1
        token = s[start:i]
        if token == "true":
            return True, i
        if token == "false":
            return False, i
        try:
            if any(ch in token for ch in ".eE"):
                return float(token), i
            return int(token), i
        except ValueError:
            raise err(f"could not parse value: {token!r}")

    def _toml_parse_array(s: str, i: int, err) -> tuple[list, int]:
        assert s[i] == "["
        i += 1
        items: list = []
        while i < len(s):
            i = _toml_skip_ws(s, i)
            if i < len(s) and s[i] == "]":
                return items, i + 1
            val, i = _toml_parse_value(s, i, err)
            items.append(val)
            i = _toml_skip_ws(s, i)
            if i < len(s) and s[i] == ",":
                i += 1
        raise err("unterminated array")

    def _toml_parse_inline_table(s: str, i: int, err) -> tuple[dict, int]:
        assert s[i] == "{"
        i += 1
        table: dict = {}
        while i < len(s):
            i = _toml_skip_ws(s, i)
            if i < len(s) and s[i] == "}":
                return table, i + 1
            if s[i] == ",":
                i += 1
                continue
            key_start = i
            while i < len(s) and s[i] not in " \t=,}":
                i += 1
            key = s[key_start:i].strip()
            if key.startswith('"') and key.endswith('"') and len(key) >= 2:
                key = key[1:-1]
            i = _toml_skip_ws(s, i)
            if i >= len(s) or s[i] != "=":
                raise err("expected '=' in inline table")
            i += 1
            val, i = _toml_parse_value(s, i, err)
            table[key] = val
        raise err("unterminated inline table")

    def _toml_minimal_parse(text: str, err) -> dict:
        root: dict = {}
        current: dict = root
        for raw in text.splitlines():
            line = _toml_strip_comment(raw).strip()
            if not line:
                continue
            if line.startswith("["):
                end = line.find("]")
                if end < 0:
                    raise err(f"unterminated section header: {line!r}")
                path = [p.strip() for p in line[1:end].split(".")]
                current = root
                for p in path:
                    if p.startswith('"') and p.endswith('"') and len(p) >= 2:
                        p = p[1:-1]
                    current = current.setdefault(p, {})
                continue
            eq = line.find("=")
            if eq < 0:
                raise err(f"expected key = value, got: {line!r}")
            key = line[:eq].strip()
            if key.startswith('"') and key.endswith('"') and len(key) >= 2:
                key = key[1:-1]
            value, _ = _toml_parse_value(line, eq + 1, err)
            current[key] = value
        return root

    tomllib = _MinimalToml()  # type: ignore[assignment]


# ============================================================================
# Constants
# ============================================================================

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
MIGRATOR_META_RE = re.compile(
    r"^<!--\s*migrator:meta\s+(?P<attrs>.+?)\s*-->\s*\n?", re.MULTILINE)
MIGRATOR_BEGIN = "<!-- migrator:begin kind={kind} source={source} -->"
MIGRATOR_END = "<!-- migrator:end -->"
MIGRATOR_BLOCK_RE = re.compile(
    r"<!--\s*migrator:begin\s+kind=(?P<kind>\S+)\s+source=(?P<source>\S+)\s*-->\n"
    r"(?P<body>.*?)\n?<!--\s*migrator:end\s*-->",
    re.DOTALL,
)

CLAUDE_UNMAPPABLE_KEYS = (
    "statusLine", "outputStyle",  # outputStyle handled as Tier A (content)
    "agentPushNotifEnabled", "remoteControlAtStartup",
    "autoUpdates", "verbose", "theme", "preferredNotifChannel",
    "enableAllProjectMcpServers", "enabledMcpjsonServers",
    "disabledMcpjsonServers", "alwaysThinkingEnabled",
    # Newer Claude Code (2.1.15x+) surface with no Codex equivalent.
    "fallbackModel", "availableModels", "defaultMode", "autoMemoryEnabled",
    "attribution", "sandbox", "autoMode",
    # 2.1.20x additions.
    "disableAutoMode", "emojiCompletionEnabled", "workflowSizeGuideline",
    "vimInsertModeRemaps",
)
CODEX_UNMAPPABLE_KEYS = (
    "model_provider", "model_providers", "tools", "tui",
    "hide_agent_reasoning", "show_raw_agent_reasoning",
    "model_reasoning_summary", "reasoning_summary", "model_verbosity",
    "disable_response_storage", "history", "project_doc_max_bytes",
    # Newer Codex (0.13x+) surface with no Claude Code equivalent.
    "web_search", "features", "default_permissions", "permissions",
    "agents", "memories", "apps", "plugins", "service_tier",
    "project_doc_fallback_filenames",
)

# Claude `effortLevel` and Codex `model_reasoning_effort` share the
# low/medium/high/xhigh vocabulary (Claude Code 2.1+; Codex), so those map 1:1.
# Codex also has `minimal` (→ Claude `low`) and, since ~0.145, first-class
# `none`, `max`, and `ultra` tiers. Claude tops out at `xhigh` (its `max`
# is a legacy *alias* for xhigh, not a higher tier), so Codex max/ultra
# collapse down to xhigh with a report note, and `none` has no Claude
# equivalent at all (mapped to nothing; callers report it).
EFFORT_C2X = {
    "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh",
    "max": "xhigh",  # legacy Claude alias = today's xhigh
}
EFFORT_X2C = {
    "minimal": "low", "low": "low", "medium": "medium", "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh", "ultra": "xhigh",  # above-xhigh Codex tiers collapse
}
# Codex efforts that lose meaning when collapsed into Claude's scale.
EFFORT_X_DOWNGRADED = ("max", "ultra")


# ============================================================================
# Tiny TOML writer
# ============================================================================
# Python's stdlib has `tomllib` for reading TOML but no writer. We only need
# to emit a small, well-defined subset (top-level scalars, nested tables for
# `[mcp_servers.NAME]` / `[sandbox_workspace_write]` / etc., arrays of
# strings, and small inline tables for env-style maps), so a hand-rolled
# writer beats taking on a dependency.

def _toml_escape(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _toml_key(k: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", k):
        return k
    return _toml_escape(k)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return _toml_escape(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(
            f"{_toml_key(k)} = {_toml_value(val)}" for k, val in v.items()
        ) + " }"
    if v is None:
        return _toml_escape("")
    raise TypeError(f"Cannot serialize {type(v).__name__} to TOML")


def _is_inline_dict(d: dict) -> bool:
    """A dict with no nested dicts is rendered as an inline `{ k = v, ... }`
    table instead of as a `[parent.child]` section header. This keeps small
    env-style maps (`env = { K = "v" }`) compact and is also what Codex'
    documented schema uses for things like `shell_environment_policy.set`."""
    return all(not isinstance(v, dict) for v in d.values())


def render_toml(data: dict) -> str:
    """Render a (nested) dict to a TOML string parseable by `tomllib`."""
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}

    for k, v in scalars.items():
        lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
    if scalars and tables:
        lines.append("")

    def emit(prefix: str, table: dict) -> None:
        subtables = {k: v for k, v in table.items()
                     if isinstance(v, dict) and not _is_inline_dict(v)}
        own = {k: v for k, v in table.items() if k not in subtables}
        lines.append(f"[{prefix}]")
        for k, v in own.items():
            lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
        lines.append("")
        for sk, sv in subtables.items():
            emit(f"{prefix}.{_toml_key(sk)}", sv)

    for k, v in tables.items():
        emit(_toml_key(k), v)

    return "\n".join(lines).rstrip() + "\n"


def write_toml(data: dict, path: Path) -> None:
    path.write_text(render_toml(data), encoding="utf-8")


# ============================================================================
# Helpers
# ============================================================================

def ts() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _unique_backup_root(dst_root: Path) -> Path:
    return dst_root / "backups" / f"pre-migrate-{ts()}-{uuid.uuid4().hex[:8]}"


def strip_frontmatter(text: str) -> tuple[str, dict | None]:
    """Return (body, parsed_frontmatter_or_None)."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return text, None
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return text[m.end():], fm


def meta_comment_to_frontmatter(text: str) -> tuple[str, dict | None]:
    """Inverse of frontmatter_to_meta_comment().

    The `<!-- migrator:meta ... -->` HTML comment carries slash-command
    frontmatter (`description`, `argument-hint`) through targets that
    don't support those keys natively — Cursor skills today, and Codex
    prompts migrated by older versions of this tool (Codex prompts gained
    native frontmatter for both keys since).
    """
    m = MIGRATOR_META_RE.match(text)
    if not m:
        return text, None
    attrs = _parse_comment_meta(m.group("attrs"))
    return text[m.end():], (attrs or None)


def frontmatter_to_meta_comment(fm: dict) -> str:
    keep = {k: v for k, v in fm.items() if k in ("description", "argument-hint")}
    if not keep:
        return ""
    return f"<!-- migrator:meta json={json.dumps(keep, ensure_ascii=False)} -->\n"


def _parse_comment_meta(raw: str) -> dict:
    if raw.startswith("json="):
        try:
            data = json.loads(raw.removeprefix("json="))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return dict(re.findall(r'(\w[\w-]*)="((?:[^"\\]|\\.)*)"', raw))


def safe_cursor_rule_name(name: str, fallback: str = "migrated") -> str:
    """Return a safe Cursor rule basename, never a path."""
    stem = Path(str(name)).name
    if stem.endswith(".mdc"):
        stem = stem[:-4]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")
    return stem or fallback


def safe_agent_name(name: str, fallback: str = "agent") -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", str(name)).strip("-_")
    return stem or fallback


def safe_skill_name(name: str, fallback: str = "migrated") -> str:
    """Skill names must be lowercase letters/digits/hyphens and match the
    skill's directory name (the Agent Skills standard all three tools use)."""
    stem = re.sub(r"[^a-z0-9-]+", "-", str(name).lower()).strip("-")
    return stem or fallback


def _fm_bool(value: object) -> bool:
    """Frontmatter booleans: Claude Code 2.1.218+ accepts yes/no/on/off/1/0
    alongside true/false (case-insensitive); treat them all as booleans
    everywhere so migrated files keep their meaning."""
    return str(value).strip().lower() in ("true", "yes", "on", "1")


def _csv_values(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(str(x).strip() for x in value if str(x).strip())
    return tuple(x.strip() for x in str(value).split(",") if x.strip())


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        m = re.match(r"\s*#\s+(.+?)\s*$", line)
        if m:
            return m.group(1)
    return None


def _claude_permission_to_codex_sandbox(mode: str | None) -> str | None:
    if not mode:
        return None
    return {
        "readOnly": "read-only",
        "acceptEdits": "workspace-write",
    }.get(mode)


def _codex_effort_to_claude(effort: str | None) -> str | None:
    if not effort:
        return None
    if effort == "none":
        return None  # Claude has no effort-off setting
    # minimal/max/ultra collapse into Claude's low…xhigh scale; any other
    # value (incl. shared ones and future custom strings) passes through.
    return EFFORT_X2C.get(effort, effort)


_UNSET = object()


def claude_project_root(ctx: Ctx, root: Path | None = None,
                        doc: object = _UNSET) -> Path | None:
    root = root or ctx.dst_root
    doc = ctx.dst_doc if doc is _UNSET else doc
    if root.name == ".claude" and doc is not None:
        return root.parent
    return None


def claude_mcp_path(ctx: Ctx, root: Path | None = None,
                    doc: object = _UNSET) -> Path:
    """Return the native Claude MCP file for this scope.

    Project-scoped MCP belongs in `<project>/.mcp.json`. User/local MCP is
    stored in `~/.claude.json`; for override dirs, fall back to `mcp.json`
    under the provided root so tests and explicit paths stay self-contained.
    """
    root = root or ctx.dst_root
    doc = ctx.dst_doc if doc is _UNSET else doc
    project_root = claude_project_root(ctx, root, doc)
    if project_root:
        return project_root / ".mcp.json"
    if root.name == ".claude":
        return root.parent / ".claude.json"
    return root / "mcp.json"


def make_frontmatter(d: dict) -> str:
    lines = ["---"]
    for k, v in d.items():
        lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


def fenced_block(kind: str, source: str, body: str) -> str:
    """Wrap content that lives in different shapes on each side (Codex
    `instructions` as a TOML string vs. Claude `outputStyle` files) in an
    HTML comment fence so it can be embedded in the target's instruction
    document and unwrapped on the way back."""
    body = body.rstrip()
    return (f"\n\n{MIGRATOR_BEGIN.format(kind=kind, source=source)}\n"
            f"{body}\n{MIGRATOR_END}\n")


def extract_fenced(text: str, kind: str) -> tuple[str, str | None]:
    """Inverse of fenced_block(). Returns (text_with_block_removed,
    block_body_or_None) for the first matching kind."""
    for m in MIGRATOR_BLOCK_RE.finditer(text):
        if m.group("kind") == kind:
            return text[:m.start()].rstrip() + "\n" + text[m.end():].lstrip(), m.group("body")
    return text, None


# ============================================================================
# Report + I/O context
# ============================================================================

@dataclass
class Report:
    direction: str
    migrated_clean: list[str] = field(default_factory=list)
    migrated_lossy: list[str] = field(default_factory=list)
    skipped_by_user: list[str] = field(default_factory=list)
    skipped_unmappable: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self, backup_root: Path | None) -> str:
        def section(title: str, items: list[str], empty: str = "_(none)_") -> list[str]:
            return [f"## {title}", *([f"- {x}" for x in items] if items else [empty]), ""]

        out = [
            f"# Migration report — {self.direction}",
            f"_{dt.datetime.now().isoformat(timespec='seconds')}_",
            "",
            *section("Migrated cleanly (Tier A)", self.migrated_clean),
            *section("Migrated with loss (Tier B, user-confirmed)", self.migrated_lossy),
            *section("Skipped by user choice", self.skipped_by_user),
            *section("Not translated (no equivalent exists)", self.skipped_unmappable),
        ]
        if self.notes:
            out += section("Notes", self.notes)
        out.append("## Backups")
        if backup_root and self.backups:
            out.append(f"Existing destination files moved to: `{backup_root}`")
            out += [f"- {x}" for x in self.backups]
        else:
            out.append("_(nothing replaced)_")
        return "\n".join(out) + "\n"


@dataclass
class Ctx:
    """Shared state for a single source→destination migration pair.

    `plan_mode` is the key knob: when True, write_text/copy_file just
    record the destination path in `planned_writes` instead of touching
    disk. The migration is run twice — once in plan mode to discover every
    file we'll touch (so we can back them all up upfront), and once in
    apply mode to actually write.
    """
    src_root: Path
    dst_root: Path
    src_doc: Path | None
    dst_doc: Path | None
    dry_run: bool
    merge: bool
    backup: bool
    report: Report
    plan_mode: bool = False
    planned_writes: set[Path] = field(default_factory=set)
    backup_root: Path = field(init=False)

    def __post_init__(self) -> None:
        self.backup_root = _unique_backup_root(self.dst_root)


def write_text(ctx: Ctx, path: Path, content: str) -> None:
    """Write content to `path` — or, in plan_mode, just record that we
    would. Used by every Tier A/B function that produces output."""
    if ctx.plan_mode:
        ctx.planned_writes.add(path)
        return
    if ctx.dry_run:
        ctx.planned_writes.add(path)
        print(f"[dry-run] write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def copy_file(ctx: Ctx, src: Path, dst: Path) -> None:
    if ctx.plan_mode:
        ctx.planned_writes.add(dst)
        return
    if ctx.dry_run:
        ctx.planned_writes.add(dst)
        print(f"[dry-run] copy {src} → {dst}")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def perform_backup(ctx: Ctx) -> Path | None:
    """Back up every planned destination + write a manifest. Returns the
    backup dir (or None on dry-run / nothing to back up).

    The manifest records, for each planned destination:
      - `original`        — absolute path on disk
      - `relative`        — path inside the backup directory
      - `existed_before`  — whether the destination existed pre-migration

    `--restore` uses `existed_before` to decide whether to copy a file
    back (existing → restore) or delete the destination (newly-created by
    the migration → remove).
    """
    if not ctx.backup or not ctx.planned_writes:
        return None

    entries: list[dict] = []
    for p in sorted(ctx.planned_writes):
        try:
            rel = p.relative_to(ctx.dst_root)
        except ValueError:
            rel = Path(p.name)
        existed = p.exists()
        entries.append({
            "original": str(p),
            "relative": str(rel),
            "existed_before": existed,
        })

    if ctx.dry_run:
        for e in entries:
            if e["existed_before"]:
                ctx.report.backups.append(
                    f"would back up: {e['original']} → "
                    f"{ctx.backup_root}/{e['relative']}"
                )
        return None

    ctx.backup_root.mkdir(parents=True, exist_ok=True)
    for e in entries:
        if not e["existed_before"]:
            continue
        src = Path(e["original"])
        dest = ctx.backup_root / e["relative"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        ctx.report.backups.append(str(dest))

    manifest = {
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "direction": ctx.report.direction,
        "src_root": str(ctx.src_root),
        "dst_root": str(ctx.dst_root),
        "entries": entries,
    }
    (ctx.backup_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return ctx.backup_root


def load_json(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"warn: {p} not valid JSON ({e}); treating as empty", file=sys.stderr)
        return {}


def load_toml(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        print(f"warn: {p} not valid TOML ({e}); treating as empty", file=sys.stderr)
        return {}


def load_claude_settings(claude_dir: Path) -> dict:
    base = load_json(claude_dir / "settings.json")
    local = load_json(claude_dir / "settings.local.json")
    return {**base, **local}


# ============================================================================
# Tier A — clean translations (always applied)
# ============================================================================
# Tier A translations have a near-1:1 mapping on the other side. They're
# applied unconditionally and don't trigger any user prompts. Each function
# is structured so that re-running it after an earlier write reads the
# updated destination via load_json/load_toml and layers on top — that's
# what makes plan→backup→apply correct even when multiple translations
# target the same file (e.g. settings.json).

def tier_a_docs_claude_to_codex(ctx: Ctx) -> None:
    """CLAUDE.md → AGENTS.md, with optional outputStyle content fenced in."""
    src = ctx.src_doc or (ctx.src_root / "CLAUDE.md")
    if not src.exists():
        src = ctx.src_root / "CLAUDE.md"
    dst = ctx.dst_doc or (ctx.dst_root / "AGENTS.md")

    content_parts: list[str] = []
    if src.exists():
        content_parts.append(src.read_text(encoding="utf-8").rstrip() + "\n")
    rules_doc = claude_rules_to_doc(ctx)
    if rules_doc:
        content_parts.append(rules_doc + "\n")
        ctx.report.migrated_clean.append(".claude/rules/*.md → fenced blocks in AGENTS.md")

    # outputStyle: if set, append the style file's content fenced.
    settings = load_claude_settings(ctx.src_root)
    style_name = settings.get("outputStyle")
    if style_name:
        for cand in [ctx.src_root / "output-styles" / f"{style_name}.md",
                     Path.home() / ".claude" / "output-styles" / f"{style_name}.md"]:
            if cand.exists():
                content_parts.append(fenced_block(
                    "outputStyle", style_name,
                    cand.read_text(encoding="utf-8")))
                ctx.report.migrated_clean.append(
                    f"outputStyle '{style_name}' → fenced block in AGENTS.md")
                break

    if not content_parts:
        return

    body = "".join(content_parts)
    if dst.exists() and ctx.merge:
        existing = dst.read_text(encoding="utf-8").rstrip()
        body = existing + "\n\n" + body if existing else body
    write_text(ctx, dst, body)
    if src.exists():
        ctx.report.migrated_clean.append(f"{src.name} → {dst.name}")


def tier_a_docs_codex_to_claude(ctx: Ctx) -> None:
    """AGENTS.md → CLAUDE.md, plus config.toml:instructions appended fenced."""
    src = ctx.src_doc or (ctx.src_root / "AGENTS.md")
    if not src.exists():
        src = ctx.src_root / "AGENTS.md"
    dst = ctx.dst_doc or (ctx.dst_root / "CLAUDE.md")

    content_parts: list[str] = []
    if src.exists():
        project_root = claude_project_root(ctx)
        if project_root and src == project_root / "AGENTS.md":
            content_parts.append("@AGENTS.md\n")
            ctx.report.migrated_clean.append("AGENTS.md → CLAUDE.md import")
        else:
            content_parts.append(src.read_text(encoding="utf-8").rstrip() + "\n")

    cfg = load_toml(ctx.src_root / "config.toml")
    if "instructions" in cfg and isinstance(cfg["instructions"], str):
        content_parts.append(fenced_block(
            "instructions", "config.toml", cfg["instructions"]))
        ctx.report.migrated_clean.append(
            "config.toml:instructions → fenced block in CLAUDE.md")

    if not content_parts:
        return

    body = "".join(content_parts)
    if dst.exists() and ctx.merge:
        existing = dst.read_text(encoding="utf-8").rstrip()
        body = existing + "\n\n" + body if existing else body
    write_text(ctx, dst, body)
    if src.exists() and not any("AGENTS.md → CLAUDE.md import" in x for x in ctx.report.migrated_clean):
        ctx.report.migrated_clean.append(f"{src.name} → {dst.name}")


def tier_a_commands_to_prompts(ctx: Ctx) -> None:
    """Claude slash commands → Codex custom prompts.

    Codex prompts natively support `description` and `argument-hint`
    frontmatter now, so those keys carry over as real frontmatter instead
    of the meta-comment smuggling older versions needed. Other frontmatter
    keys (`model`, `allowed-tools`, …) have no prompt equivalent and are
    dropped with a note."""
    src_dir = ctx.src_root / "commands"
    if not src_dir.is_dir():
        return
    dst_dir = ctx.dst_root / "prompts"
    for f in sorted(src_dir.rglob("*.md")):
        rel = f.relative_to(src_dir)
        body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
        fm = fm or {}
        keep = {k: v for k, v in fm.items()
                if k in ("description", "argument-hint")}
        dropped = sorted(fm.keys() - keep.keys())
        if dropped:
            ctx.report.notes.append(
                f"commands/{rel}: dropped frontmatter keys: {', '.join(dropped)}")
        out = (make_frontmatter(keep) if keep else "") + body
        write_text(ctx, dst_dir / rel, out)
        ctx.report.migrated_clean.append(f"commands/{rel} → prompts/{rel}")


def tier_a_prompts_to_commands(ctx: Ctx) -> None:
    src_dir = ctx.src_root / "prompts"
    if not src_dir.is_dir():
        return
    dst_dir = ctx.dst_root / "commands"
    for f in sorted(src_dir.rglob("*.md")):
        rel = f.relative_to(src_dir)
        text = f.read_text(encoding="utf-8")
        body, fm = strip_frontmatter(text)
        if fm is None:
            # Prompts migrated by older versions carried description/
            # argument-hint in a migrator:meta comment; still honor it.
            body, fm = meta_comment_to_frontmatter(text)
        if _SHELL_DEFAULT_RE.search(body):
            ctx.report.notes.append(
                f"prompts/{rel}: uses shell-style default substitutions "
                "(${1:-…}) that only pi expands — review after migrating")
        if fm:
            body = make_frontmatter(fm) + body
        write_text(ctx, dst_dir / rel, body)
        ctx.report.migrated_clean.append(f"prompts/{rel} → commands/{rel}")


def _skill_dirs(root: Path, subdir: str = "skills") -> list[Path]:
    """Skill dirs under <root>/<subdir> — one dir per skill, identified by
    a SKILL.md inside. Hidden dirs are skipped: Codex keeps system-managed
    skills under `skills/.system/`, which belong to the tool, not the user."""
    skills_root = root / subdir
    if not skills_root.is_dir():
        return []
    out: list[Path] = []
    for d in sorted(skills_root.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and (d / "SKILL.md").is_file():
            out.append(d)
    return out


def tier_a_skills_copy(ctx: Ctx, src_subdir: str = "skills",
                       dst_subdir: str = "skills") -> None:
    """skills/<name>/ → skills/<name>/ — every supported tool speaks the
    same Agent Skills format (SKILL.md + bundled assets), so skills
    transfer as a verbatim tree copy in every direction. Only the
    directory name varies per tool (e.g. opencode uses `skill/`)."""
    for skill_dir in _skill_dirs(ctx.src_root, src_subdir):
        dst_dir = ctx.dst_root / dst_subdir / skill_dir.name
        n_files = 0
        for f in sorted(skill_dir.rglob("*")):
            if f.is_file():
                copy_file(ctx, f, dst_dir / f.relative_to(skill_dir))
                n_files += 1
        ctx.report.migrated_clean.append(
            f"{src_subdir}/{skill_dir.name}/ → {dst_subdir}/{skill_dir.name}/ "
            f"({n_files} file(s), incl. assets)")


def tier_a_codex_agents_to_claude(ctx: Ctx) -> None:
    src_dir = ctx.src_root / "agents"
    if not src_dir.is_dir():
        return
    dst_dir = ctx.dst_root / "agents"
    for f in sorted(src_dir.glob("*.toml")):
        cfg = load_toml(f)
        name = safe_agent_name(cfg.get("name") or f.stem)
        description = str(cfg.get("description") or f"Imported from Codex agent {name}.")
        body = str(cfg.get("developer_instructions") or cfg.get("instructions") or "").strip()
        if not body:
            ctx.report.skipped_unmappable.append(
                f"agents/{f.name} (missing developer_instructions)")
            continue

        fm: dict[str, str] = {"name": name, "description": description}
        if cfg.get("model"):
            fm["model"] = str(cfg["model"])
        effort = _codex_effort_to_claude(cfg.get("model_reasoning_effort"))
        if effort:
            fm["effort"] = effort
        sandbox = cfg.get("sandbox_mode")
        notes: list[str] = []
        if sandbox:
            notes.append(
                f"Codex sandbox_mode={sandbox!r} has no exact Claude subagent field; "
                "review permissions before relying on this agent."
            )
        if cfg.get("mcp_servers"):
            notes.append(
                "Codex agent-local mcp_servers were not copied; configure Claude MCP separately if needed."
            )
        if notes:
            body += "\n\n## Manual migration notes\n\n" + "\n".join(f"- {n}" for n in notes)
        write_text(ctx, dst_dir / f"{name}.md", make_frontmatter(fm) + body.rstrip() + "\n")
        ctx.report.migrated_clean.append(f"agents/{f.name} → agents/{name}.md")


def _parse_cursor_model(model: str) -> tuple[str, str | None, list[str]]:
    """Split Cursor's model bracket syntax into (model_id, effort_or_None,
    other_params). The brackets take comma-separated id=value pairs —
    `model[effort=high,context=300k,fast=false]` — and empty `[]` pins the
    standard variant; only `effort` has a cross-tool meaning, so the rest
    is returned for the caller to flag. Plain model ids pass through."""
    m = re.fullmatch(r"(?P<id>[^\[\]]+)\[(?P<params>[^\]]*)\]", model.strip())
    if not m:
        return model.strip(), None, []
    effort = None
    extras: list[str] = []
    for part in m.group("params").split(","):
        part = part.strip()
        if not part:
            continue
        k, _, v = part.partition("=")
        if k.strip() == "effort" and v.strip():
            effort = v.strip()
        else:
            extras.append(part)
    return m.group("id").strip(), effort, extras


def tier_a_cursor_agents_to_claude(ctx: Ctx) -> None:
    """Cursor subagents (.cursor/agents/*.md) → Claude subagents.

    Cursor 2.4+ subagents are markdown + frontmatter like Claude's;
    name/description/model translate directly, Cursor's model bracket
    effort (`model[effort=high]`) splits into Claude's `effort` field, and
    `is_background` maps to Claude's `background`. Cursor's `readonly`
    flag has no exact Claude field and is preserved as a review note."""
    src_dir = ctx.src_root / "agents"
    if not src_dir.is_dir():
        return
    dst_dir = ctx.dst_root / "agents"
    for f in sorted(src_dir.rglob("*.md")):
        if f.stem == "README":
            continue
        body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
        fm = fm or {}
        name = safe_agent_name(fm.get("name") or f.stem)
        out_fm: dict[str, str] = {
            "name": name,
            "description": fm.get("description")
            or f"Imported from Cursor subagent {name}.",
        }
        model = fm.get("model")
        extras: list[str] = []
        if model and model != "inherit":
            model_id, effort, extras = _parse_cursor_model(model)
            out_fm["model"] = model_id
            if effort:
                out_fm["effort"] = _codex_effort_to_claude(effort) or effort
        if _fm_bool(fm.get("is_background", "")):
            out_fm["background"] = "true"
        notes: list[str] = []
        if extras:
            notes.append(
                "Cursor model bracket params with no Claude equivalent were "
                "dropped: " + ", ".join(f"`{x}`" for x in extras) + ".")
        if _fm_bool(fm.get("readonly", "")):
            notes.append(
                "Cursor readonly=true has no exact Claude subagent field; "
                "restrict `tools` or use permissions if enforcement is needed.")
        if notes:
            body = body.rstrip() + "\n\n## Manual migration notes\n\n" + \
                "\n".join(f"- {n}" for n in notes)
        write_text(ctx, dst_dir / f"{name}.md",
                   make_frontmatter(out_fm) + body.rstrip() + "\n")
        ctx.report.migrated_clean.append(f"agents/{f.name} → agents/{name}.md")


def tier_a_commands_to_cursor_skills(ctx: Ctx, src_subdir: str,
                                     src_label: str) -> None:
    """Slash commands (Claude commands/, Codex prompts/) → Cursor skills
    with `disable-model-invocation: true`.

    Cursor deprecated `.cursor/commands/*.md` in favor of skills; a skill
    with `disable-model-invocation: true` is the modern equivalent of a
    slash command (invocable from the / menu, never auto-triggered).
    `argument-hint` and other extra frontmatter ride along in a
    migrator:meta comment so nothing is silently dropped."""
    src_dir = ctx.src_root / src_subdir
    if not src_dir.is_dir():
        return
    for f in sorted(src_dir.rglob("*.md")):
        rel = f.relative_to(src_dir)
        text = f.read_text(encoding="utf-8")
        body, fm = strip_frontmatter(text)
        if fm is None:
            # Codex prompts carry claude-origin frontmatter in a meta comment.
            body, fm = meta_comment_to_frontmatter(text)
        fm = fm or {}
        name = safe_skill_name(rel.as_posix().replace("/", "-").rsplit(".", 1)[0])
        out_fm: dict[str, str] = {
            "name": name,
            "description": fm.get("description") or f"Slash command /{name}",
            "disable-model-invocation": "true",
        }
        meta = frontmatter_to_meta_comment(
            {k: v for k, v in fm.items() if k == "argument-hint"})
        dropped = sorted(fm.keys() - {"description", "argument-hint"})
        if dropped:
            ctx.report.notes.append(
                f"{src_subdir}/{rel}: dropped frontmatter keys: {', '.join(dropped)}")
        write_text(ctx, ctx.dst_root / "skills" / name / "SKILL.md",
                   make_frontmatter(out_fm) + meta + body.lstrip("\n"))
        ctx.report.migrated_clean.append(
            f"{src_label}/{rel} → skills/{name}/SKILL.md "
            "(slash-invocable Cursor skill)")


def _mcp_transport(spec: dict) -> str:
    """Infer the transport of a Claude/Cursor-shaped MCP spec. Cursor omits
    `type` for remote servers, so a bare `url` means streamable HTTP."""
    t = str(spec.get("type") or "").lower()
    if t:
        return {"streamable-http": "http"}.get(t, t)
    return "http" if spec.get("url") else "stdio"


def _normalize_mcp_claude_to_codex(name: str, spec: dict, report: Report) -> dict | None:
    """Codex speaks stdio and streamable HTTP MCP. SSE (deprecated) and
    WebSocket servers can't be represented and get reported as skipped."""
    t = _mcp_transport(spec)
    if t == "http":
        if not spec.get("url"):
            report.skipped_unmappable.append(f"MCP server '{name}' has no url")
            return None
        out: dict[str, Any] = {"url": spec["url"]}
        if spec.get("headers"):
            out["http_headers"] = dict(spec["headers"])
        return out
    if t != "stdio":
        report.skipped_unmappable.append(
            f"MCP server '{name}' uses type='{t}' "
            "(Codex supports stdio and streamable HTTP)")
        return None
    out = {}
    if "command" in spec:
        out["command"] = spec["command"]
    if spec.get("args"):
        out["args"] = list(spec["args"])
    if spec.get("env"):
        out["env"] = dict(spec["env"])
    if not out.get("command"):
        report.skipped_unmappable.append(f"MCP server '{name}' has no command")
        return None
    return out


def _normalize_mcp_codex_to_claude(spec: dict) -> dict:
    if spec.get("url"):
        out: dict[str, Any] = {"type": "http", "url": spec["url"]}
        if spec.get("http_headers"):
            out["headers"] = dict(spec["http_headers"])
        return out
    out = {"type": "stdio"}
    for k in ("command", "args", "env"):
        if spec.get(k):
            out[k] = spec[k] if k == "command" else (
                list(spec[k]) if k == "args" else dict(spec[k]))
    return out


def claude_read_mcp(ctx: Ctx, root: Path | None = None,
                    doc: Path | None = None) -> dict:
    root = root or ctx.src_root
    doc = ctx.src_doc if doc is None else doc
    native = claude_mcp_path(ctx, root, doc)
    candidates = [native, root / "mcp.json"]
    settings = load_claude_settings(root)
    out: dict = {}
    if isinstance(settings.get("mcpServers"), dict):
        out.update(settings["mcpServers"])
    for p in candidates:
        if p.exists():
            data = load_json(p)
            if isinstance(data.get("mcpServers"), dict):
                out.update(data["mcpServers"])
    return out


def claude_write_mcp(ctx: Ctx, servers: dict) -> None:
    dst = claude_mcp_path(ctx)
    existing = load_json(dst) if (ctx.merge and dst.exists()) else {}
    existing.setdefault("mcpServers", {})
    existing["mcpServers"].update(servers)
    write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")


def claude_rules_to_doc(ctx: Ctx) -> str:
    rules_dir = ctx.src_root / "rules"
    if not rules_dir.is_dir():
        return ""
    parts: list[str] = []
    for f in sorted(rules_dir.rglob("*.md")):
        rel = f.relative_to(rules_dir)
        body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
        fm = fm or {}
        source = safe_cursor_rule_name(rel.as_posix().replace("/", "-").rsplit(".", 1)[0])
        parts.append(fenced_block("claude-rule", source, body.strip()))
        if fm:
            ctx.report.notes.append(
                f"rules/{rel}: native Claude rule frontmatter preserved only in rule file")
    return "\n\n".join(parts)


def cursor_rules_to_claude_rules(rules: list[CursorRule], claude_root: Path,
                                 ctx: Ctx) -> None:
    rules_dir = claude_root / "rules"
    for r in rules:
        fm: dict = {}
        if r.description:
            fm["description"] = r.description
        if r.globs is not None:
            fm["paths"] = r.globs if isinstance(r.globs, str) else ",".join(r.globs)
        meta = {"alwaysApply": str(r.always_apply).lower()}
        body = (f"<!-- migrator:cursor-meta json={json.dumps(meta, ensure_ascii=False)} -->\n"
                f"{r.body.rstrip()}\n")
        write_text(ctx, rules_dir / f"{safe_cursor_rule_name(r.name)}.md", make_frontmatter(fm) + body)


def claude_read_rules_as_cursor(ctx: Ctx) -> list[CursorRule]:
    rules_dir = ctx.src_root / "rules"
    rules: list[CursorRule] = []
    if not rules_dir.is_dir():
        return rules
    for f in sorted(rules_dir.rglob("*.md")):
        rel = f.relative_to(rules_dir)
        body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
        fm = fm or {}
        always = False
        m = re.match(r"<!--\s*migrator:cursor-meta\s+(?P<meta>.*?)\s*-->\s*\n?",
                     body, re.DOTALL)
        if m:
            attrs = _parse_comment_meta(m.group("meta"))
            always = _fm_bool(attrs.get("alwaysApply", "false"))
            body = body[m.end():]
        paths = fm.get("paths") or fm.get("globs")
        globs: object = paths
        if isinstance(paths, str) and "," in paths:
            globs = [g.strip() for g in paths.split(",")]
        rules.append(CursorRule(
            name=safe_cursor_rule_name(rel.as_posix().replace("/", "-").rsplit(".", 1)[0]),
            description=fm.get("description", ""),
            globs=globs,
            always_apply=always,
            body=body.strip(),
        ))
    return rules


def tier_a_settings_claude_to_codex(ctx: Ctx) -> None:
    settings = load_claude_settings(ctx.src_root)
    if not settings:
        return
    dst = ctx.dst_root / "config.toml"
    existing = load_toml(dst) if (ctx.merge and dst.exists()) else {}

    if "model" in settings:
        existing["model"] = settings["model"]
        ctx.report.migrated_clean.append("settings.json:model → config.toml:model")

    mcp = claude_read_mcp(ctx)
    if mcp:
        existing.setdefault("mcp_servers", {})
        for name, spec in mcp.items():
            t = _normalize_mcp_claude_to_codex(name, spec, ctx.report)
            if t is not None:
                existing["mcp_servers"][name] = t
                ctx.report.migrated_clean.append(
                    f"mcpServers.{name} → [mcp_servers.{name}]")

    if isinstance(settings.get("env"), dict) and settings["env"]:
        existing.setdefault("shell_environment_policy", {})
        existing["shell_environment_policy"].setdefault("set", {})
        existing["shell_environment_policy"]["set"].update(settings["env"])
        ctx.report.migrated_clean.append(
            f"settings.json:env ({len(settings['env'])} vars) → "
            "[shell_environment_policy] set")

    if "effortLevel" in settings:
        mapped = EFFORT_C2X.get(settings["effortLevel"])
        if mapped:
            existing["model_reasoning_effort"] = mapped
            ctx.report.migrated_clean.append(
                f"effortLevel={settings['effortLevel']} → "
                f"model_reasoning_effort={mapped}")

    for k in CLAUDE_UNMAPPABLE_KEYS:
        if k in settings:
            ctx.report.skipped_unmappable.append(
                f"settings.json:{k} (no Codex equivalent)")

    if existing:
        write_text(ctx, dst, render_toml(existing))


def tier_a_settings_codex_to_claude(ctx: Ctx) -> None:
    cfg = load_toml(ctx.src_root / "config.toml")
    if not cfg:
        return
    dst = ctx.dst_root / "settings.json"
    existing = load_json(dst) if (ctx.merge and dst.exists()) else {}

    if "model" in cfg:
        existing["model"] = cfg["model"]
        ctx.report.migrated_clean.append("config.toml:model → settings.json:model")

    mcp = cfg.get("mcp_servers") or {}
    if mcp:
        claude_write_mcp(ctx, {
            name: _normalize_mcp_codex_to_claude(spec)
            for name, spec in mcp.items()
        })
        for name in mcp:
            ctx.report.migrated_clean.append(
                f"[mcp_servers.{name}] → {claude_mcp_path(ctx).name}:mcpServers.{name}")

    sep = cfg.get("shell_environment_policy") or {}
    set_vars = sep.get("set") or {}
    if set_vars:
        existing.setdefault("env", {})
        existing["env"].update({k: str(v) for k, v in set_vars.items()})
        ctx.report.migrated_clean.append(
            f"[shell_environment_policy] set ({len(set_vars)} vars) → settings.json:env")
    for k in ("include_only", "exclude", "inherit"):
        if k in sep:
            ctx.report.skipped_unmappable.append(
                f"shell_environment_policy.{k} (no Claude Code equivalent)")

    eff = cfg.get("model_reasoning_effort")
    if isinstance(eff, str):
        mapped = EFFORT_X2C.get(eff)
        if mapped:
            existing["effortLevel"] = mapped
            ctx.report.migrated_clean.append(
                f"model_reasoning_effort={eff} → effortLevel={mapped}")
            if eff in EFFORT_X_DOWNGRADED:
                ctx.report.notes.append(
                    f"Codex effort {eff!r} sits above Claude's scale — "
                    "collapsed to xhigh")
        else:
            ctx.report.skipped_unmappable.append(
                f"model_reasoning_effort={eff!r} (no Claude effortLevel "
                "equivalent)")

    for k in CODEX_UNMAPPABLE_KEYS:
        if k in cfg:
            ctx.report.skipped_unmappable.append(
                f"config.toml:{k} (no Claude Code equivalent)")

    if existing:
        write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")


# ============================================================================
# Cursor — I/O helpers + direction drivers
# ============================================================================
# Cursor (a VS Code fork) stores its agent config across:
#   - <cursor_root>/mcp.json                  — MCP servers (same shape as Claude)
#   - <cursor_root>/rules/*.mdc               — project rules (Markdown +
#                                                YAML frontmatter)
#   - <project_root>/.cursorrules             — legacy plain-text rules
# User scope (~/.cursor) only has global mcp.json. The Cursor IDE settings
# (themes, keybindings, extensions) live elsewhere and are deliberately out
# of scope for this migrator — those are editor config, not agent config.
#
# We translate by going through a small intermediate: a list[CursorRule]
# for instruction docs, and a dict[name, McpSpec] for MCP servers. That
# keeps cursor↔claude and cursor↔codex symmetrical.

CURSOR_RULE_BLOCK_RE = re.compile(
    r"<!--\s*migrator:begin\s+kind=cursor-rule\s+source=(?P<name>\S+)\s*-->\n"
    r"(?:<!--\s*migrator:cursor-meta\s+(?P<meta>.*?)\s*-->\n)?"
    r"(?P<body>.*?)\n?<!--\s*migrator:end\s*-->",
    re.DOTALL,
)


@dataclass
class CursorRule:
    name: str
    description: str
    globs: object  # str | list[str] | None
    always_apply: bool
    body: str


def cursor_project_root_from(cursor_root: Path) -> Path | None:
    """Cursor's `.cursorrules` (legacy) sits at the project root, alongside
    the `.cursor/` dir. For a project-scope cursor_root like `./.cursor`,
    the project root is its parent. For a user-scope root like `~/.cursor`,
    there is no legacy file location.
    """
    if cursor_root.name == ".cursor":
        return cursor_root.parent
    return None


def cursor_read_mcp(cursor_root: Path) -> dict:
    p = cursor_root / "mcp.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"warn: {p} not valid JSON ({e}); treating as empty",
              file=sys.stderr)
        return {}
    return data.get("mcpServers") or {}


def cursor_write_mcp(servers: dict, cursor_root: Path, ctx: Ctx) -> None:
    """Write MCP servers into <cursor_root>/mcp.json, merging with any
    existing `mcpServers` block."""
    p = cursor_root / "mcp.json"
    existing: dict = {}
    if p.exists() and ctx.merge:
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    existing.setdefault("mcpServers", {})
    existing["mcpServers"].update(servers)
    write_text(ctx, p, json.dumps(existing, indent=2) + "\n")


def cursor_read_rules(cursor_root: Path) -> list[CursorRule]:
    """Read both .cursor/rules/*.mdc and legacy <project>/.cursorrules."""
    rules: list[CursorRule] = []
    rules_dir = cursor_root / "rules"
    if rules_dir.is_dir():
        for f in sorted(rules_dir.rglob("*.mdc")):
            body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
            fm = fm or {}
            always = _fm_bool(fm.get("alwaysApply", "false"))
            globs = fm.get("globs")
            # MDC `globs` may be a comma-separated string or a YAML list;
            # we only parsed simple `k: v` lines, so commas stay literal.
            if globs and "," in globs:
                globs = [g.strip() for g in globs.split(",")]
            rules.append(CursorRule(
                name=f.stem,
                description=fm.get("description", ""),
                globs=globs,
                always_apply=always,
                body=body.strip(),
            ))
    project_root = cursor_project_root_from(cursor_root)
    if project_root:
        legacy = project_root / ".cursorrules"
        if legacy.is_file():
            rules.append(CursorRule(
                name="_cursorrules_legacy",
                description="Imported from legacy .cursorrules",
                globs=None,
                always_apply=True,
                body=legacy.read_text(encoding="utf-8").strip(),
            ))
    return rules


def cursor_rules_to_doc(rules: list[CursorRule]) -> str:
    """Concatenate Cursor rules into a single instruction doc with fenced
    metadata so a reverse migration can split them back out."""
    parts: list[str] = []
    for r in rules:
        meta_attrs: list[tuple[str, str]] = []
        if r.description:
            meta_attrs.append(("description", r.description))
        if r.globs is not None:
            g = r.globs if isinstance(r.globs, str) else ",".join(r.globs)
            meta_attrs.append(("globs", g))
        meta_attrs.append(("alwaysApply", str(r.always_apply).lower()))
        meta = {k: v for k, v in meta_attrs}
        parts.append(
            f"<!-- migrator:begin kind=cursor-rule source={safe_cursor_rule_name(r.name)} -->\n"
            f"<!-- migrator:cursor-meta json={json.dumps(meta, ensure_ascii=False)} -->\n"
            f"{r.body}\n"
            f"<!-- migrator:end -->"
        )
    return "\n\n".join(parts)


def doc_to_cursor_rules(text: str, default_name: str = "migrated") -> list[CursorRule]:
    """Inverse of cursor_rules_to_doc(). Any text outside cursor-rule
    fenced blocks becomes a single `<default_name>.mdc` with alwaysApply
    true so the content still loads in Cursor."""
    rules: list[CursorRule] = []
    spans: list[tuple[int, int]] = []
    for m in CURSOR_RULE_BLOCK_RE.finditer(text):
        attrs_str = m.group("meta") or ""
        attrs = _parse_comment_meta(attrs_str)
        globs = attrs.get("globs")
        if globs and "," in globs:
            globs = [g.strip() for g in globs.split(",")]
        rules.append(CursorRule(
            name=safe_cursor_rule_name(m.group("name")),
            description=attrs.get("description", ""),
            globs=globs,
            always_apply=_fm_bool(attrs.get("alwaysApply", "false")),
            body=m.group("body").strip(),
        ))
        spans.append((m.start(), m.end()))

    # Whatever sits outside the fenced blocks is loose content; package it
    # as one alwaysApply rule so nothing is silently dropped.
    leftover_parts: list[str] = []
    last = 0
    for s, e in spans:
        chunk = text[last:s].strip()
        if chunk:
            leftover_parts.append(chunk)
        last = e
    tail = text[last:].strip()
    if tail:
        leftover_parts.append(tail)
    leftover = "\n\n".join(leftover_parts).strip()
    if leftover:
        rules.append(CursorRule(
            name=safe_cursor_rule_name(default_name), description="Migrated content",
            globs=None, always_apply=True, body=leftover,
        ))
    return rules


def cursor_write_rules(rules: list[CursorRule], cursor_root: Path,
                       ctx: Ctx) -> None:
    """Write Cursor rules as .mdc files under <cursor_root>/rules/."""
    rules_dir = cursor_root / "rules"
    for r in rules:
        fm: dict = {}
        if r.description:
            fm["description"] = r.description
        if r.globs is not None:
            fm["globs"] = (r.globs if isinstance(r.globs, str)
                           else ",".join(r.globs))
        fm["alwaysApply"] = str(r.always_apply).lower()
        body = make_frontmatter(fm) + r.body.rstrip() + "\n"
        write_text(ctx, rules_dir / f"{safe_cursor_rule_name(r.name)}.mdc", body)


def cursor_cli_config_write_model(ctx: Ctx, model: str) -> None:
    """Carry the default model into Cursor's CLI config
    (<cursor_root>/cli-config.json, `version: 1`). Model ids aren't
    renamed — fix the id by hand if the destination doesn't know it."""
    p = ctx.dst_root / "cli-config.json"
    existing = load_json(p) if (ctx.merge and p.exists()) else {}
    existing.setdefault("version", 1)
    existing["model"] = model
    write_text(ctx, p, json.dumps(existing, indent=2) + "\n")


def cursor_cli_config_read_model(cursor_root: Path) -> str | None:
    model = load_json(cursor_root / "cli-config.json").get("model")
    return model if isinstance(model, str) and model else None


def _cursor_label_mcp(direction: str, mcp: dict) -> str:
    return (f"mcpServers ({len(mcp)} entr{'y' if len(mcp) == 1 else 'ies'}) "
            f"{direction}")


def _normalize_mcp_for_cursor(spec: dict) -> dict:
    """Cursor accepts the Claude shape verbatim (stdio + SSE/HTTP). Keep
    documented keys only — drop anything migrator-internal."""
    out: dict = {}
    for k in ("command", "args", "env", "type", "url", "headers"):
        if k in spec and spec[k] is not None:
            out[k] = spec[k]
    return out


def _native_mcp_from_codex(name: str, spec: dict) -> dict:
    """Codex stores MCP under a TOML table (stdio or streamable HTTP);
    produce a Claude/Cursor JSON-shaped dict."""
    if spec.get("url"):
        out: dict = {"url": spec["url"]}
        if spec.get("http_headers"):
            out["headers"] = dict(spec["http_headers"])
        return out
    out = {"type": "stdio"}
    for k in ("command", "args", "env"):
        if spec.get(k):
            out[k] = list(spec[k]) if k == "args" else (
                dict(spec[k]) if k == "env" else spec[k])
    return out


# ============================================================================
# opencode — I/O helpers
# ============================================================================
# opencode (opencode.ai) keeps its global config under ~/.config/opencode
# (XDG layout, not a home dot-dir): opencode.json[c] + AGENTS.md +
# agents/ + commands/ + skills/ (plural canonical; singular legacy names
# are still read). Project scope is <project>/opencode.json plus a
# .opencode/ dir with the same subdirs. Models are provider-qualified
# ("anthropic/claude-…"), MCP servers live under the "mcp" config key as
# local (argv array) / remote (url) entries, and permissions are
# tool→pattern-map objects that line up well with Claude's Bash() rules.
# opencode reads CLAUDE.md and .claude/skills natively, so some
# claude→opencode moves are no-ops that we surface as notes.

OPENCODE_PROVIDER_FOR_TOOL = {"claude": "anthropic", "codex": "openai"}

OPENCODE_UNMAPPABLE_KEYS = (
    "provider", "plugin", "formatter", "lsp", "share", "autoupdate",
    "snapshot", "compaction", "keybinds", "theme", "tools", "watcher",
    "disabled_providers", "enabled_providers", "experimental", "server",
    "subagent_depth",  # 1.18.2+: no cross-tool nesting-depth equivalent
)


def _strip_jsonc(text: str) -> str:
    """Strip // and /* */ comments plus trailing commas from JSONC,
    respecting string literals. opencode accepts .jsonc config files."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    # Trailing commas: `,` immediately before `}` or `]`.
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _opencode_project_root(root: Path) -> Path | None:
    return root.parent if root.name == ".opencode" else None


def opencode_config_paths(root: Path) -> list[Path]:
    """Candidate config files for this scope, lowest→highest precedence."""
    candidates: list[Path] = []
    project = _opencode_project_root(root)
    for base in filter(None, [project, root]):
        candidates += [base / "opencode.jsonc", base / "opencode.json"]
    return candidates


def opencode_config_write_path(root: Path) -> Path:
    """Canonical config file to write: <project>/opencode.json at project
    scope, <root>/opencode.json otherwise."""
    project = _opencode_project_root(root)
    return (project or root) / "opencode.json"


def load_opencode_config(root: Path) -> dict:
    merged: dict = {}
    for p in opencode_config_paths(root):
        if not p.exists():
            continue
        try:
            data = json.loads(_strip_jsonc(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError as e:
            print(f"warn: {p} not valid JSON(C) ({e}); skipping",
                  file=sys.stderr)
            continue
        if isinstance(data, dict):
            merged.update(data)
    return merged


def opencode_update_config(ctx: Ctx, updates: dict) -> None:
    """Merge `updates` into the destination opencode.json (one level of
    nested-dict merging, enough for `mcp` / `permission`)."""
    dst = opencode_config_write_path(ctx.dst_root)
    existing: dict = {}
    if ctx.merge:
        if dst.exists():
            try:
                existing = json.loads(
                    _strip_jsonc(dst.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                existing = {}
        else:
            existing = load_opencode_config(ctx.dst_root)
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(existing.get(k), dict):
            existing[k].update(v)
        else:
            existing[k] = v
    write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")


def _opencode_subdirs(root: Path, plural: str) -> list[Path]:
    """Existing content dirs for a category, plural first, singular legacy
    second (both are read by opencode; we only ever write the plural)."""
    return [d for d in (root / plural, root / plural.rstrip("s"))
            if d.is_dir()]


def model_to_opencode(model: str, src_tool: str) -> str:
    """opencode model ids are provider-qualified. Bare ids from tools with
    a known provider get prefixed; anything already qualified passes."""
    if "/" in model:
        return model
    provider = OPENCODE_PROVIDER_FOR_TOOL.get(src_tool)
    return f"{provider}/{model}" if provider else model


def model_from_opencode(model: str) -> tuple[str, str | None]:
    if "/" in model:
        provider, _, model_id = model.partition("/")
        return model_id, provider
    return model, None


def _mcp_opencode_to_native(spec: dict) -> dict | None:
    """opencode MCP entry → Claude/Cursor-shaped spec."""
    t = str(spec.get("type") or ("local" if spec.get("command") else "remote"))
    if t == "local":
        cmd = spec.get("command")
        if isinstance(cmd, str):
            cmd = [cmd]
        if not cmd:
            return None
        out: dict = {"type": "stdio", "command": cmd[0]}
        if list(cmd[1:]):
            out["args"] = list(cmd[1:])
        if spec.get("environment"):
            out["env"] = dict(spec["environment"])
        return out
    if t == "remote" and spec.get("url"):
        out = {"type": "http", "url": spec["url"]}
        if spec.get("headers"):
            out["headers"] = dict(spec["headers"])
        return out
    return None


def _mcp_native_to_opencode(name: str, spec: dict, report: Report) -> dict | None:
    """Claude/Cursor-shaped spec → opencode MCP entry (argv array,
    `environment`, local/remote discriminator)."""
    t = _mcp_transport(spec)
    if t == "stdio":
        if not spec.get("command"):
            report.skipped_unmappable.append(f"MCP server '{name}' has no command")
            return None
        out: dict = {"type": "local",
                     "command": [spec["command"], *list(spec.get("args") or [])]}
        if spec.get("env"):
            out["environment"] = dict(spec["env"])
        return out
    if t in ("http", "sse"):
        if not spec.get("url"):
            report.skipped_unmappable.append(f"MCP server '{name}' has no url")
            return None
        out = {"type": "remote", "url": spec["url"]}
        if spec.get("headers"):
            out["headers"] = dict(spec["headers"])
        if t == "sse":
            report.notes.append(
                f"MCP server '{name}': legacy SSE transport mapped to an "
                "opencode remote server — verify it still connects")
        return out
    report.skipped_unmappable.append(
        f"MCP server '{name}' uses type='{t}' (opencode supports local "
        "stdio and remote url servers)")
    return None


def opencode_read_mcp(root: Path) -> dict:
    """Read opencode MCP servers as Claude-shaped specs."""
    out: dict = {}
    for name, spec in (load_opencode_config(root).get("mcp") or {}).items():
        if isinstance(spec, dict):
            native = _mcp_opencode_to_native(spec)
            if native:
                out[name] = native
    return out


def opencode_write_mcp(ctx: Ctx, servers: dict) -> None:
    """Write Claude-shaped MCP specs into opencode.json:mcp."""
    entries: dict = {}
    for name, spec in servers.items():
        t = _mcp_native_to_opencode(name, spec, ctx.report)
        if t is not None:
            entries[name] = t
            ctx.report.migrated_clean.append(
                f"MCP server '{name}' → opencode.json:mcp.{name}")
    if entries:
        opencode_update_config(ctx, {"mcp": entries})


def opencode_read_commands(root: Path) -> list[tuple[Path, dict, str]]:
    items: list[tuple[Path, dict, str]] = []
    for d in _opencode_subdirs(root, "commands"):
        for f in sorted(d.rglob("*.md")):
            body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
            items.append((f.relative_to(d), fm or {}, body))
    return items


def opencode_docs_note_or_copy(ctx: Ctx, src_doc: Path) -> None:
    """Instruction doc → opencode. opencode reads AGENTS.md natively (and
    falls back to CLAUDE.md), so at project scope an existing AGENTS.md
    needs no work; otherwise write <dst>/AGENTS.md."""
    project = _opencode_project_root(ctx.dst_root)
    dst = (project / "AGENTS.md") if project else (ctx.dst_root / "AGENTS.md")
    if not src_doc.exists():
        return
    if src_doc == dst:
        ctx.report.notes.append(
            "AGENTS.md at the project root is read natively by opencode — "
            "no translation needed")
        return
    body = src_doc.read_text(encoding="utf-8").rstrip() + "\n"
    if dst.exists() and ctx.merge:
        existing = dst.read_text(encoding="utf-8").rstrip()
        body = existing + "\n\n" + body if existing else body
    write_text(ctx, dst, body)
    ctx.report.migrated_clean.append(f"{src_doc.name} → {dst.name}")


# ---- opencode ↔ claude permission maps -------------------------------------
# opencode `permission` is a tool→action or tool→{pattern: action} map with
# glob patterns ("git push*") and last-match-wins. Claude Bash() rules use
# prefix patterns ("Bash(git push:*)"). The translation is close but the
# matching semantics differ, hence Tier B.

def _claude_perms_to_opencode(perms: dict, report: Report) -> dict:
    out: dict = {}
    bash: dict = {}
    for key in ("allow", "ask", "deny"):
        for rule in perms.get(key) or []:
            rule = str(rule)
            tokens = _claude_bash_rule_to_tokens(rule)
            if tokens:
                bash[f"{shlex.join(tokens)}*"] = key
                continue
            if re.fullmatch(r"Bash(\(\*?\))?", rule):
                bash["*"] = key
            elif rule.startswith(("Write(", "Edit(")):
                out.setdefault("edit", key)
            elif rule.startswith(("WebFetch", "WebSearch")):
                out.setdefault("webfetch", key)
            elif rule.startswith("Read("):
                out.setdefault("read", key)
            elif rule.startswith("Skill"):
                out.setdefault("skill", key)
            else:
                report.notes.append(
                    f"permissions: {rule} has no opencode permission "
                    "equivalent")
    if bash:
        out["bash"] = bash
    return out


def _opencode_perms_to_claude(permission: dict, report: Report) -> dict:
    """opencode permission map → Claude permissions.allow/ask/deny lists."""
    out: dict[str, list[str]] = {"allow": [], "ask": [], "deny": []}

    def add(action: object, rule: str) -> None:
        if action in out:
            out[str(action)].append(rule)

    tool_rules = {"edit": "Write", "read": "Read", "webfetch": "WebFetch"}
    for tool, val in permission.items():
        if tool == "bash":
            if isinstance(val, str):
                add(val, "Bash(*)")
                continue
            for pattern, action in (val or {}).items():
                if pattern in ("*", ""):
                    add(action, "Bash(*)")
                    continue
                base = pattern.rstrip("*").strip()
                if "*" in base:
                    report.notes.append(
                        f"permission.bash: pattern {pattern!r} has "
                        "mid-pattern wildcards — not translated")
                    continue
                add(action, f"Bash({base}:*)")
        elif tool in tool_rules:
            action = val if isinstance(val, str) else None
            if action:
                add(action, f"{tool_rules[tool]}(*)")
            else:
                report.notes.append(
                    f"permission.{tool}: per-path pattern map not "
                    "translated — recreate path rules by hand")
        else:
            report.notes.append(
                f"permission.{tool} has no Claude permissions equivalent")
    return {k: sorted(set(v)) for k, v in out.items() if v}


# ============================================================================
# pi — I/O helpers
# ============================================================================
# pi (pi.dev, earendil-works/pi) keeps user config in ~/.pi/agent/ and
# project config in .pi/. It reads AGENTS.md *or* CLAUDE.md natively
# (global + walking up from cwd), speaks the Agent Skills standard
# (skills/<name>/SKILL.md, plus bare skills/*.md files), and its prompt
# templates (prompts/*.md, description/argument-hint frontmatter,
# $ARGUMENTS substitution) are format-identical to Codex custom prompts.
# settings.json carries defaultProvider/defaultModel/defaultThinkingLevel.
# pi deliberately has NO MCP, NO subagents, and NO hooks (extensions are
# TypeScript code) — those source features become report notes, so every
# pi pair is Tier A only.

PI_THINKING_TO_CLAUDE = {
    "minimal": "low", "low": "low", "medium": "medium", "high": "high",
    "xhigh": "xhigh", "max": "xhigh",  # pi keeps a max above xhigh; Claude doesn't
}
# Codex ~0.145 grew first-class none/max/ultra tiers, so pi's scale now
# maps 1:1 both ways (off↔none, max↔max); only Codex's `ultra` has no pi
# name and collapses to max.
PI_THINKING_TO_CODEX = {
    "off": "none", "minimal": "minimal", "low": "low", "medium": "medium",
    "high": "high", "xhigh": "xhigh", "max": "max",
}
CODEX_EFFORT_TO_PI = {
    "none": "off", "minimal": "minimal", "low": "low", "medium": "medium",
    "high": "high", "xhigh": "xhigh", "max": "max", "ultra": "max",
}

PI_UNMAPPABLE_SETTINGS = (
    "theme", "enabledModels", "thinkingBudgets", "defaultProjectTrust",
    "externalEditor", "quietStartup", "hideThinkingBlock", "steeringMode",
    "followUpMode", "transport", "compaction", "retry", "terminal",
    "images", "shellPath", "shellCommandPrefix", "npmCommand", "sessionDir",
    "httpProxy", "packages", "extensions", "themes",
    # Resource arrays point at external dirs — copy those separately.
    "skills", "prompts",
    "enableSkillCommands", "enableInstallTelemetry", "enableAnalytics",
)

# pi prompt templates support shell-style default substitutions
# (`${1:-default}`, `${@:-default}`, `${@:N:L}`) that no other tool
# expands — flag them when a template leaves pi.
_SHELL_DEFAULT_RE = re.compile(r"\$\{[^}]*:-|\$\{@")


def load_pi_settings(root: Path) -> dict:
    return load_json(root / "settings.json")


def pi_update_settings(ctx: Ctx, updates: dict) -> None:
    dst = ctx.dst_root / "settings.json"
    existing = load_json(dst) if (ctx.merge and dst.exists()) else {}
    existing.update(updates)
    write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")


def pi_report_unmappables(ctx: Ctx, settings: dict) -> None:
    for k in PI_UNMAPPABLE_SETTINGS:
        if k in settings:
            ctx.report.skipped_unmappable.append(
                f"settings.json:{k} (pi-only — no equivalent)")
    for f in ("SYSTEM.md", "APPEND_SYSTEM.md", "keybindings.json",
              "models.json"):
        if (ctx.src_root / f).exists():
            ctx.report.skipped_unmappable.append(
                f"{f} (pi-only — system prompt/keybinding/provider config "
                "has no equivalent; models.json may contain secrets)")


def _pi_feature_gap_notes(ctx: Ctx, mcp: bool = False, agents: bool = False,
                          hooks: bool = False, permissions: bool = False) -> None:
    """Source features pi deliberately doesn't have. Reported once each so
    nothing silently disappears."""
    if mcp:
        ctx.report.skipped_unmappable.append(
            "MCP servers (pi has no MCP — wrap the server as a CLI tool "
            "with a skill instead)")
    if agents:
        ctx.report.skipped_unmappable.append(
            "subagents (core pi has none — see the pi-subagents community "
            "package if you need them)")
    if hooks:
        ctx.report.skipped_unmappable.append(
            "hooks (pi extensions are TypeScript code, not configurable "
            "hooks — port by hand)")
    if permissions:
        ctx.report.skipped_unmappable.append(
            "permission rules (pi uses per-project trust, not per-command "
            "rules)")


def skills_from_pi(ctx: Ctx) -> None:
    """Skills out of pi: standard skill dirs copy verbatim; pi also treats
    bare skills/*.md files as skills — those become <name>/SKILL.md dirs
    (the only layout the other tools read), synthesizing name/description
    frontmatter when the bare file lacks it."""
    tier_a_skills_copy(ctx)
    skills_root = ctx.src_root / "skills"
    if not skills_root.is_dir():
        return
    for f in sorted(skills_root.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        body, fm = strip_frontmatter(text)
        fm = fm or {}
        name = safe_skill_name(fm.get("name") or f.stem)
        if fm.get("name") and fm.get("description"):
            content = text
        else:
            merged = dict(fm)
            merged["name"] = name
            merged.setdefault(
                "description",
                _first_heading(body) or f"Migrated pi skill {name}")
            # name/description lead; any other keys keep their values.
            ordered = {"name": merged.pop("name"),
                       "description": merged.pop("description"), **merged}
            content = make_frontmatter(ordered) + body.lstrip("\n")
            ctx.report.notes.append(
                f"skills/{f.name}: synthesized missing name/description "
                "frontmatter for the SKILL.md standard")
        write_text(ctx, ctx.dst_root / "skills" / name / "SKILL.md", content)
        ctx.report.migrated_clean.append(
            f"skills/{f.name} → skills/{name}/SKILL.md (bare pi skill file)")


def copy_instruction_doc(ctx: Ctx, src_doc: Path, dst_doc: Path,
                         native_note: str) -> None:
    """AGENTS.md-style doc copy between tools that both read it natively;
    same-file cases (shared project root) become a note instead."""
    if not src_doc.exists():
        return
    if src_doc == dst_doc:
        ctx.report.notes.append(native_note)
        return
    body = src_doc.read_text(encoding="utf-8").rstrip() + "\n"
    if dst_doc.exists() and ctx.merge:
        existing = dst_doc.read_text(encoding="utf-8").rstrip()
        body = existing + "\n\n" + body if existing else body
    write_text(ctx, dst_doc, body)
    ctx.report.migrated_clean.append(f"{src_doc.name} → {dst_doc.name}")


def tier_a_prompts_copy(ctx: Ctx, warn_shell_defaults: bool = True) -> None:
    """Codex prompts ↔ pi prompt templates — the formats are identical
    (description/argument-hint frontmatter, $ARGUMENTS/$1 substitution),
    so files transfer as-is; legacy migrator:meta comments from old
    migrations are normalized back to frontmatter."""
    src_dir = ctx.src_root / "prompts"
    if not src_dir.is_dir():
        return
    for f in sorted(src_dir.rglob("*.md")):
        rel = f.relative_to(src_dir)
        text = f.read_text(encoding="utf-8")
        body, fm = strip_frontmatter(text)
        if fm is None:
            body, fm = meta_comment_to_frontmatter(text)
        if warn_shell_defaults and _SHELL_DEFAULT_RE.search(body):
            ctx.report.notes.append(
                f"prompts/{rel}: uses shell-style default substitutions "
                "(${1:-…}) that only pi expands — review after migrating")
        out = (make_frontmatter(fm) if fm else "") + body
        write_text(ctx, ctx.dst_root / "prompts" / rel, out)
        ctx.report.migrated_clean.append(f"prompts/{rel} → prompts/{rel}")


def _pi_thinking_out(ctx: Ctx, settings: dict, table: dict) -> str | None:
    level = settings.get("defaultThinkingLevel")
    if not isinstance(level, str):
        return None
    mapped = table.get(level)
    if mapped is None:
        ctx.report.skipped_unmappable.append(
            f"settings.json:defaultThinkingLevel={level!r} (no equivalent)")
        return None
    if level == "max" and mapped != "max":
        ctx.report.notes.append(
            "pi thinking level 'max' sits above 'xhigh'; mapped to the "
            "destination's highest level")
    return mapped


# ---- pi direction runners ---------------------------------------------------

def run_claude_to_pi(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    tier_a_docs_claude_to_codex(ctx)  # CLAUDE.md (+rules/outputStyle) → AGENTS.md
    ctx.report.notes.append(
        "pi also reads CLAUDE.md natively — the AGENTS.md copy makes the "
        "config self-contained")
    tier_a_commands_to_prompts(ctx)   # description/argument-hint match pi's format
    tier_a_skills_copy(ctx)

    settings = load_claude_settings(ctx.src_root)
    updates: dict = {}
    if isinstance(settings.get("model"), str):
        updates["defaultProvider"] = "anthropic"
        updates["defaultModel"] = settings["model"]
        ctx.report.migrated_clean.append(
            "settings.json:model → defaultProvider/defaultModel")
    effort = settings.get("effortLevel")
    if isinstance(effort, str):
        mapped = EFFORT_C2X.get(effort)  # normalizes legacy max → xhigh
        if mapped:
            updates["defaultThinkingLevel"] = mapped
            ctx.report.migrated_clean.append(
                f"effortLevel={effort} → defaultThinkingLevel={mapped}")
    if updates:
        pi_update_settings(ctx, updates)

    _pi_feature_gap_notes(
        ctx,
        mcp=bool(claude_read_mcp(ctx)),
        agents=(ctx.src_root / "agents").is_dir()
        and any((ctx.src_root / "agents").rglob("*.md")),
        hooks=bool(settings.get("hooks")),
        permissions=bool(settings.get("permissions")),
    )
    for key in ("env", "statusLine", "outputStyle"):
        if settings.get(key):
            ctx.report.skipped_unmappable.append(
                f"settings.json:{key} (no pi equivalent)")

    _run_lossy(ctx, lossy_decisions, "claude->pi")


def run_pi_to_claude(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    tier_a_docs_codex_to_claude(ctx)  # AGENTS.md → CLAUDE.md (@import at project)
    tier_a_prompts_to_commands(ctx)
    skills_from_pi(ctx)

    settings = load_pi_settings(ctx.src_root)
    dst = ctx.dst_root / "settings.json"
    existing = load_json(dst) if (ctx.merge and dst.exists()) else {}
    changed = False
    if isinstance(settings.get("defaultModel"), str):
        existing["model"] = settings["defaultModel"]
        changed = True
        ctx.report.migrated_clean.append(
            "defaultModel → settings.json:model")
        provider = settings.get("defaultProvider")
        if provider and provider != "anthropic":
            ctx.report.notes.append(
                f"model came from provider {provider!r} — verify Claude "
                "Code can run it")
    mapped = _pi_thinking_out(ctx, settings, PI_THINKING_TO_CLAUDE)
    if mapped:
        existing["effortLevel"] = mapped
        changed = True
        ctx.report.migrated_clean.append(
            f"defaultThinkingLevel → effortLevel={mapped}")
    if changed:
        write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")
    pi_report_unmappables(ctx, settings)

    _run_lossy(ctx, lossy_decisions, "pi->claude")


def run_codex_to_pi(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    src_doc = (ctx.src_doc if ctx.src_doc and ctx.src_doc.exists()
               else ctx.src_root / "AGENTS.md")
    copy_instruction_doc(
        ctx, src_doc, ctx.dst_doc or (ctx.dst_root / "AGENTS.md"),
        "AGENTS.md at the project root is read natively by both tools — "
        "no translation needed")
    # Destination is pi, which expands every substitution form — no
    # shell-default warning needed in this direction.
    tier_a_prompts_copy(ctx, warn_shell_defaults=False)
    tier_a_skills_copy(ctx)

    cfg = load_toml(ctx.src_root / "config.toml")
    updates: dict = {}
    if isinstance(cfg.get("model"), str):
        updates["defaultProvider"] = "openai"
        updates["defaultModel"] = cfg["model"]
        ctx.report.migrated_clean.append(
            "config.toml:model → defaultProvider/defaultModel")
    effort = cfg.get("model_reasoning_effort")
    if isinstance(effort, str):
        mapped = CODEX_EFFORT_TO_PI.get(effort)
        if mapped:
            updates["defaultThinkingLevel"] = mapped
            ctx.report.migrated_clean.append(
                f"model_reasoning_effort={effort} → defaultThinkingLevel="
                f"{mapped}")
            if effort == "ultra":
                ctx.report.notes.append(
                    "Codex effort 'ultra' has no pi tier — mapped to pi's "
                    "highest level 'max'")
        else:
            ctx.report.skipped_unmappable.append(
                f"model_reasoning_effort={effort!r} (no pi thinking-level "
                "equivalent)")
    if updates:
        pi_update_settings(ctx, updates)

    _pi_feature_gap_notes(
        ctx,
        mcp=bool(cfg.get("mcp_servers")),
        agents=(ctx.src_root / "agents").is_dir()
        and any((ctx.src_root / "agents").glob("*.toml")),
        hooks=bool(cfg.get("notify")
                   or load_json(ctx.src_root / "hooks.json").get("hooks")),
        permissions=(ctx.src_root / "rules").is_dir()
        or any(k in cfg for k in ("sandbox_mode", "approval_policy")),
    )

    _run_lossy(ctx, lossy_decisions, "codex->pi")


def run_pi_to_codex(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    src_doc = ctx.src_root / "AGENTS.md"
    if ctx.src_doc and ctx.src_doc.exists():
        src_doc = ctx.src_doc
    copy_instruction_doc(
        ctx, src_doc, ctx.dst_doc or (ctx.dst_root / "AGENTS.md"),
        "AGENTS.md at the project root is read natively by both tools — "
        "no translation needed")
    tier_a_prompts_copy(ctx)
    skills_from_pi(ctx)

    settings = load_pi_settings(ctx.src_root)
    dst = ctx.dst_root / "config.toml"
    existing = load_toml(dst) if (ctx.merge and dst.exists()) else {}
    changed = False
    if isinstance(settings.get("defaultModel"), str):
        existing["model"] = settings["defaultModel"]
        changed = True
        ctx.report.migrated_clean.append("defaultModel → config.toml:model")
        provider = settings.get("defaultProvider")
        if provider and provider != "openai":
            ctx.report.notes.append(
                f"model came from provider {provider!r} — verify Codex can "
                "run it")
    mapped = _pi_thinking_out(ctx, settings, PI_THINKING_TO_CODEX)
    if mapped:
        existing["model_reasoning_effort"] = mapped
        changed = True
        ctx.report.migrated_clean.append(
            f"defaultThinkingLevel → model_reasoning_effort={mapped}")
    if changed:
        write_text(ctx, dst, render_toml(existing))
    pi_report_unmappables(ctx, settings)

    _run_lossy(ctx, lossy_decisions, "pi->codex")


def run_cursor_to_pi(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    rules = cursor_read_rules(ctx.src_root)
    if rules:
        doc = cursor_rules_to_doc(rules)
        dst_doc = ctx.dst_doc or (ctx.dst_root / "AGENTS.md")
        if dst_doc.exists() and ctx.merge:
            doc = dst_doc.read_text(encoding="utf-8").rstrip() + "\n\n" + doc
        write_text(ctx, dst_doc, doc + "\n")
        ctx.report.migrated_clean.append(
            f"{len(rules)} cursor rule(s) → {dst_doc.name}")
    tier_a_skills_copy(ctx)

    model = cursor_cli_config_read_model(ctx.src_root)
    if model:
        pi_update_settings(ctx, {"defaultModel": model})
        ctx.report.migrated_clean.append(
            "cli-config.json:model → defaultModel")
        ctx.report.notes.append(
            "defaultProvider was not set (Cursor doesn't record one) — "
            "set it in ~/.pi/agent/settings.json if needed")
    _pi_feature_gap_notes(
        ctx,
        mcp=bool(cursor_read_mcp(ctx.src_root)),
        agents=(ctx.src_root / "agents").is_dir()
        and any((ctx.src_root / "agents").rglob("*.md")),
        hooks=bool(load_json(ctx.src_root / "hooks.json").get("hooks")),
    )

    _run_lossy(ctx, lossy_decisions, "cursor->pi")


def run_pi_to_cursor(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    src_doc = ctx.src_root / "AGENTS.md"
    if ctx.src_doc and ctx.src_doc.exists():
        src_doc = ctx.src_doc
    project_dst = cursor_project_root_from(ctx.dst_root)
    if (project_dst and src_doc.exists()
            and src_doc == project_dst / "AGENTS.md"):
        ctx.report.notes.append(
            "AGENTS.md at the project root is read natively by Cursor — "
            "no translation needed")
    elif src_doc.exists():
        rules = doc_to_cursor_rules(src_doc.read_text(encoding="utf-8"))
        if rules:
            cursor_write_rules(rules, ctx.dst_root, ctx)
            ctx.report.migrated_clean.append(
                f"AGENTS.md → {len(rules)} cursor rule file(s)")
    tier_a_commands_to_cursor_skills(ctx, "prompts", "prompts")
    skills_from_pi(ctx)

    settings = load_pi_settings(ctx.src_root)
    if isinstance(settings.get("defaultModel"), str):
        cursor_cli_config_write_model(ctx, settings["defaultModel"])
        ctx.report.migrated_clean.append(
            "defaultModel → cli-config.json:model")
    pi_report_unmappables(ctx, settings)

    _run_lossy(ctx, lossy_decisions, "pi->cursor")


def run_opencode_to_pi(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    src_doc = ctx.src_root / "AGENTS.md"
    project_src = _opencode_project_root(ctx.src_root)
    if project_src and (project_src / "AGENTS.md").exists():
        src_doc = project_src / "AGENTS.md"
    copy_instruction_doc(
        ctx, src_doc, ctx.dst_doc or (ctx.dst_root / "AGENTS.md"),
        "AGENTS.md at the project root is read natively by both tools — "
        "no translation needed")
    tier_a_opencode_commands_to(ctx, "prompts")
    _skills_copy_from_opencode(ctx)

    cfg = load_opencode_config(ctx.src_root)
    if isinstance(cfg.get("model"), str):
        model_id, provider = model_from_opencode(cfg["model"])
        updates: dict = {"defaultModel": model_id}
        if provider:
            updates["defaultProvider"] = provider
        pi_update_settings(ctx, updates)
        ctx.report.migrated_clean.append(
            "opencode.json:model → defaultProvider/defaultModel")
    _pi_feature_gap_notes(
        ctx,
        mcp=bool(cfg.get("mcp")),
        agents=any(_opencode_subdirs(ctx.src_root, "agents")),
        permissions=bool(cfg.get("permission")),
    )
    _opencode_report_unmappables(ctx, cfg)

    _run_lossy(ctx, lossy_decisions, "opencode->pi")


def run_pi_to_opencode(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    src_doc = ctx.src_root / "AGENTS.md"
    if ctx.src_doc and ctx.src_doc.exists():
        src_doc = ctx.src_doc
    opencode_docs_note_or_copy(ctx, src_doc)
    tier_a_commands_to_opencode(ctx, "prompts")
    skills_from_pi(ctx)

    settings = load_pi_settings(ctx.src_root)
    if isinstance(settings.get("defaultModel"), str):
        provider = settings.get("defaultProvider")
        model = (f"{provider}/{settings['defaultModel']}" if provider
                 else settings["defaultModel"])
        opencode_update_config(ctx, {"model": model})
        ctx.report.migrated_clean.append(
            f"defaultProvider/defaultModel → opencode.json:model={model}")
        if not provider:
            ctx.report.notes.append(
                "opencode model ids are provider-qualified — check "
                f"`{settings['defaultModel']}` resolves")
    if settings.get("defaultThinkingLevel"):
        ctx.report.skipped_unmappable.append(
            "settings.json:defaultThinkingLevel (opencode has no "
            "reasoning-effort knob)")
    pi_report_unmappables(ctx, settings)

    _run_lossy(ctx, lossy_decisions, "pi->opencode")


# ============================================================================
# AgentSpec — shared subagent intermediate for the newer tool pairs
# ============================================================================
# claude↔codex and claude↔cursor agent translation predates this and stays
# as-is; every pair involving opencode (and future tools) goes through this
# normalized record so each tool needs one reader + one writer instead of
# a converter per pair.

@dataclass
class AgentSpec:
    name: str
    description: str
    body: str
    model: str | None = None      # bare model id (no provider prefix)
    provider: str | None = None   # provider hint when the source knows it
    effort: str | None = None
    background: bool = False
    readonly: bool = False
    dropped: list[str] = field(default_factory=list)  # untranslatable keys


def _spec_notes_section(spec: AgentSpec) -> str:
    if not spec.dropped:
        return ""
    return ("\n\n## Manual migration notes\n\n- Source agent fields with no "
            "equivalent here were dropped: "
            + ", ".join(f"`{d}`" for d in sorted(set(spec.dropped))) + ".\n")


def _agents_read_opencode(root: Path) -> list[AgentSpec]:
    specs: list[AgentSpec] = []
    carried = {"description", "mode", "model", "name"}
    for d in _opencode_subdirs(root, "agents"):
        for f in sorted(d.rglob("*.md")):
            body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
            fm = fm or {}
            model_id, provider = (None, None)
            if fm.get("model"):
                model_id, provider = model_from_opencode(fm["model"])
            # Our frontmatter parser is flat; a nested `permission:` block
            # surfaces as stray keys — report them as dropped rather than
            # mis-translating.
            dropped = sorted(k for k in fm if k not in carried)
            specs.append(AgentSpec(
                name=safe_agent_name(fm.get("name") or f.stem),
                description=fm.get("description")
                or f"Imported from opencode agent {f.stem}.",
                body=body.strip(),
                model=model_id, provider=provider,
                dropped=dropped,
            ))
    return specs


def _agents_write_opencode(ctx: Ctx, specs: list[AgentSpec],
                           src_tool: str) -> None:
    dst_dir = ctx.dst_root / "agents"
    provider_default = OPENCODE_PROVIDER_FOR_TOOL.get(src_tool)
    for spec in specs:
        lines = ["---", f"description: {spec.description}", "mode: subagent"]
        if spec.model:
            provider = spec.provider or provider_default
            if provider:
                lines.append(f"model: {provider}/{spec.model}")
            else:
                lines.append(f"model: {spec.model}")
                ctx.report.notes.append(
                    f"agent {spec.name}: opencode model ids are "
                    f"provider-qualified — check `{spec.model}` resolves")
        if spec.readonly:
            lines += ["permission:", "  edit: deny", "  bash: deny"]
        lines.append("---\n")
        dropped = list(spec.dropped)
        if spec.effort:
            dropped.append("effort (opencode has no reasoning-effort knob)")
        if spec.background:
            dropped.append("background")
        body = spec.body + _spec_notes_section(
            AgentSpec(spec.name, spec.description, "", dropped=dropped))
        write_text(ctx, dst_dir / f"{spec.name}.md",
                   "\n".join(lines) + body.rstrip() + "\n")
        label = f"agent {spec.name} → agents/{spec.name}.md (opencode subagent)"
        if dropped:
            ctx.report.migrated_lossy.append(
                f"{label} (dropped: {', '.join(sorted(set(dropped)))})")
        else:
            ctx.report.migrated_clean.append(label)


def _agents_read_claude(root: Path) -> list[AgentSpec]:
    src_dir = root / "agents"
    specs: list[AgentSpec] = []
    if not src_dir.is_dir():
        return specs
    carried = {"name", "description", "model", "effort", "background",
               "permissionMode"}
    for f in sorted(src_dir.rglob("*.md")):
        if f.stem == "README":
            continue
        body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
        fm = fm or {}
        specs.append(AgentSpec(
            name=safe_agent_name(fm.get("name") or f.stem),
            description=fm.get("description")
            or f"Migrated Claude subagent {f.stem}.",
            body=body.strip(),
            model=fm.get("model"), provider=None,
            effort=fm.get("effort"),
            background=_fm_bool(fm.get("background", "")),
            readonly=fm.get("permissionMode") in ("readOnly", "plan"),
            dropped=sorted(k for k in fm if k not in carried),
        ))
    return specs


def _agents_read_codex(root: Path) -> list[AgentSpec]:
    src_dir = root / "agents"
    specs: list[AgentSpec] = []
    if not src_dir.is_dir():
        return specs
    carried = {"name", "description", "developer_instructions",
               "instructions", "model", "model_reasoning_effort",
               "sandbox_mode"}
    for f in sorted(src_dir.glob("*.toml")):
        cfg = load_toml(f)
        body = str(cfg.get("developer_instructions")
                   or cfg.get("instructions") or "").strip()
        if not body:
            continue
        specs.append(AgentSpec(
            name=safe_agent_name(cfg.get("name") or f.stem),
            description=str(cfg.get("description")
                            or f"Imported from Codex agent {f.stem}."),
            body=body,
            model=cfg.get("model"), provider="openai",
            effort=_codex_effort_to_claude(cfg.get("model_reasoning_effort")),
            readonly=cfg.get("sandbox_mode") == "read-only",
            dropped=sorted(k for k in cfg if k not in carried),
        ))
    return specs


def _agents_read_cursor(root: Path) -> list[AgentSpec]:
    src_dir = root / "agents"
    specs: list[AgentSpec] = []
    if not src_dir.is_dir():
        return specs
    carried = {"name", "description", "model", "readonly", "is_background"}
    for f in sorted(src_dir.rglob("*.md")):
        if f.stem == "README":
            continue
        body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
        fm = fm or {}
        model_id, effort, extras = (None, None, [])
        if fm.get("model") and fm["model"] != "inherit":
            model_id, effort, extras = _parse_cursor_model(fm["model"])
        dropped = sorted(k for k in fm if k not in carried)
        dropped += [f"model param {x!r}" for x in extras]
        specs.append(AgentSpec(
            name=safe_agent_name(fm.get("name") or f.stem),
            description=fm.get("description")
            or f"Imported from Cursor subagent {f.stem}.",
            body=body.strip(),
            model=model_id, effort=effort,
            background=_fm_bool(fm.get("is_background", "")),
            readonly=_fm_bool(fm.get("readonly", "")),
            dropped=dropped,
        ))
    return specs


def _agents_write_claude(ctx: Ctx, specs: list[AgentSpec]) -> None:
    dst_dir = ctx.dst_root / "agents"
    for spec in specs:
        fm: dict[str, str] = {"name": spec.name,
                              "description": spec.description}
        if spec.model:
            fm["model"] = spec.model
            if spec.provider and spec.provider != "anthropic":
                ctx.report.notes.append(
                    f"agent {spec.name}: model `{spec.model}` came from "
                    f"provider {spec.provider!r} — verify Claude Code can "
                    "run it")
        if spec.effort:
            fm["effort"] = _codex_effort_to_claude(spec.effort) or spec.effort
        if spec.background:
            fm["background"] = "true"
        dropped = list(spec.dropped)
        if spec.readonly:
            dropped.append("readonly (restrict `tools` by hand if needed)")
        body = spec.body + _spec_notes_section(
            AgentSpec(spec.name, spec.description, "", dropped=dropped))
        write_text(ctx, dst_dir / f"{spec.name}.md",
                   make_frontmatter(fm) + body.rstrip() + "\n")
        ctx.report.migrated_clean.append(
            f"agent {spec.name} → agents/{spec.name}.md")


def _agents_write_codex(ctx: Ctx, specs: list[AgentSpec]) -> None:
    dst_dir = ctx.dst_root / "agents"
    for spec in specs:
        agent: dict[str, object] = {
            "name": spec.name,
            "description": spec.description,
            "developer_instructions":
                spec.body + _spec_notes_section(spec),
        }
        if spec.model:
            agent["model"] = spec.model
            if spec.provider and spec.provider != "openai":
                ctx.report.notes.append(
                    f"agent {spec.name}: model `{spec.model}` came from "
                    f"provider {spec.provider!r} — verify Codex can run it")
        if spec.effort:
            agent["model_reasoning_effort"] = EFFORT_C2X.get(
                spec.effort, spec.effort)
        if spec.readonly:
            agent["sandbox_mode"] = "read-only"
        write_text(ctx, dst_dir / f"{spec.name}.toml", render_toml(agent))
        ctx.report.migrated_clean.append(
            f"agent {spec.name} → agents/{spec.name}.toml (Codex custom agent)")


def _agents_write_cursor(ctx: Ctx, specs: list[AgentSpec]) -> None:
    dst_dir = ctx.dst_root / "agents"
    for spec in specs:
        fm: dict[str, str] = {"name": spec.name,
                              "description": spec.description}
        if spec.model and spec.effort:
            fm["model"] = f"{spec.model}[effort={spec.effort}]"
        elif spec.model:
            fm["model"] = spec.model
        if spec.background:
            fm["is_background"] = "true"
        if spec.readonly:
            fm["readonly"] = "true"
        body = spec.body + _spec_notes_section(spec)
        write_text(ctx, dst_dir / f"{spec.name}.md",
                   make_frontmatter(fm) + body.rstrip() + "\n")
        ctx.report.migrated_clean.append(
            f"agent {spec.name} → agents/{spec.name}.md (Cursor subagent)")


AGENT_READERS: dict[str, Callable[[Path], list[AgentSpec]]] = {
    "claude": _agents_read_claude,
    "codex": _agents_read_codex,
    "cursor": _agents_read_cursor,
    "opencode": _agents_read_opencode,
}


# ---- opencode commands ↔ claude/codex slash commands ------------------------

def tier_a_commands_to_opencode(ctx: Ctx, src_subdir: str) -> None:
    """Claude commands / Codex prompts → opencode commands/*.md.

    `description` is shared; `argument-hint` has no opencode key and rides
    in a migrator:meta comment ($ARGUMENTS/$1 substitution works the same
    on both sides). Other frontmatter keys are dropped with a note."""
    src_dir = ctx.src_root / src_subdir
    if not src_dir.is_dir():
        return
    dst_dir = ctx.dst_root / "commands"
    for f in sorted(src_dir.rglob("*.md")):
        rel = f.relative_to(src_dir)
        text = f.read_text(encoding="utf-8")
        body, fm = strip_frontmatter(text)
        if fm is None:
            body, fm = meta_comment_to_frontmatter(text)
        fm = fm or {}
        out_fm = {k: v for k, v in fm.items() if k == "description"}
        meta = frontmatter_to_meta_comment(
            {k: v for k, v in fm.items() if k == "argument-hint"})
        dropped = sorted(fm.keys() - {"description", "argument-hint"})
        if dropped:
            ctx.report.notes.append(
                f"{src_subdir}/{rel}: dropped frontmatter keys: "
                f"{', '.join(dropped)}")
        out = (make_frontmatter(out_fm) if out_fm else "") + meta + body
        write_text(ctx, dst_dir / rel, out)
        ctx.report.migrated_clean.append(
            f"{src_subdir}/{rel} → commands/{rel}")


def tier_a_opencode_commands_to(ctx: Ctx, dst_subdir: str) -> None:
    """opencode commands → Claude commands/ or Codex prompts/.

    `description` is shared. opencode's `agent`/`model`/`subtask` keys
    have no equivalent slash-command field and are dropped with notes
    (model ids are provider-qualified and wouldn't resolve anyway)."""
    for d in _opencode_subdirs(ctx.src_root, "commands"):
        for f in sorted(d.rglob("*.md")):
            rel = f.relative_to(d)
            body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
            # A meta comment may follow the frontmatter (that's how
            # argument-hint survives the trip into opencode) — merge both.
            body, meta = meta_comment_to_frontmatter(body)
            fm = {**(meta or {}), **(fm or {})}
            out_fm = {k: v for k, v in fm.items()
                      if k in ("description", "argument-hint")}
            dropped = sorted(fm.keys() - {"description", "argument-hint"})
            if dropped:
                ctx.report.notes.append(
                    f"commands/{rel}: dropped frontmatter keys: "
                    f"{', '.join(dropped)}")
            out = (make_frontmatter(out_fm) if out_fm else "") + body
            write_text(ctx, ctx.dst_root / dst_subdir / rel, out)
            ctx.report.migrated_clean.append(
                f"commands/{rel} → {dst_subdir}/{rel}")


def _skills_copy_from_opencode(ctx: Ctx) -> None:
    """Skills out of opencode: the plural canonical dir plus the singular
    legacy dir (opencode reads both; we write only the plural)."""
    tier_a_skills_copy(ctx)
    tier_a_skills_copy(ctx, src_subdir="skill")


def _opencode_report_unmappables(ctx: Ctx, cfg: dict) -> None:
    for k in OPENCODE_UNMAPPABLE_KEYS:
        if k in cfg:
            ctx.report.skipped_unmappable.append(
                f"opencode.json:{k} (no equivalent — recreate by hand)")
    if cfg.get("instructions"):
        ctx.report.notes.append(
            "opencode.json:instructions references extra instruction files "
            "— copy those files over manually if still relevant")
    if cfg.get("small_model"):
        ctx.report.skipped_unmappable.append(
            "opencode.json:small_model (no equivalent)")


# ---- opencode direction runners ---------------------------------------------

def run_claude_to_opencode(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    tier_a_docs_claude_to_codex(ctx)  # CLAUDE.md (+rules/outputStyle) → AGENTS.md
    ctx.report.notes.append(
        "opencode also reads CLAUDE.md and .claude/skills natively — the "
        "copies above make the config self-contained")
    tier_a_commands_to_opencode(ctx, "commands")
    tier_a_skills_copy(ctx)

    settings = load_claude_settings(ctx.src_root)
    updates: dict = {}
    if isinstance(settings.get("model"), str):
        updates["model"] = model_to_opencode(settings["model"], "claude")
        ctx.report.migrated_clean.append(
            f"settings.json:model → opencode.json:model={updates['model']}")
    if updates:
        opencode_update_config(ctx, updates)
    mcp = claude_read_mcp(ctx)
    if mcp:
        opencode_write_mcp(ctx, mcp)
    for key in ("effortLevel", "env", "statusLine", "outputStyle"):
        if settings.get(key):
            ctx.report.skipped_unmappable.append(
                f"settings.json:{key} (no opencode equivalent)")
    if settings.get("hooks"):
        ctx.report.skipped_unmappable.append(
            "settings.json:hooks (opencode plugins are TypeScript code, "
            "not configurable hooks — port by hand)")

    _run_lossy(ctx, lossy_decisions, "claude->opencode")


def run_opencode_to_claude(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    tier_a_docs_codex_to_claude(ctx)  # AGENTS.md → CLAUDE.md (@import at project)
    tier_a_opencode_commands_to(ctx, "commands")
    _skills_copy_from_opencode(ctx)
    _agents_write_claude(ctx, _agents_read_opencode(ctx.src_root))

    cfg = load_opencode_config(ctx.src_root)
    dst = ctx.dst_root / "settings.json"
    existing = load_json(dst) if (ctx.merge and dst.exists()) else {}
    if isinstance(cfg.get("model"), str):
        model_id, provider = model_from_opencode(cfg["model"])
        existing["model"] = model_id
        ctx.report.migrated_clean.append(
            f"opencode.json:model → settings.json:model={model_id}")
        if provider and provider != "anthropic":
            ctx.report.notes.append(
                f"model came from provider {provider!r} — verify Claude "
                "Code can run it")
        write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")
    mcp = opencode_read_mcp(ctx.src_root)
    if mcp:
        claude_write_mcp(ctx, mcp)
        for name in mcp:
            ctx.report.migrated_clean.append(
                f"opencode.json:mcp.{name} → "
                f"{claude_mcp_path(ctx).name}:mcpServers.{name}")
    _opencode_report_unmappables(ctx, cfg)

    _run_lossy(ctx, lossy_decisions, "opencode->claude")


def run_codex_to_opencode(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    src_doc = (ctx.src_doc if ctx.src_doc and ctx.src_doc.exists()
               else ctx.src_root / "AGENTS.md")
    opencode_docs_note_or_copy(ctx, src_doc)
    tier_a_commands_to_opencode(ctx, "prompts")
    tier_a_skills_copy(ctx)
    _agents_write_opencode(ctx, _agents_read_codex(ctx.src_root), "codex")

    cfg = load_toml(ctx.src_root / "config.toml")
    if isinstance(cfg.get("model"), str):
        opencode_update_config(
            ctx, {"model": model_to_opencode(cfg["model"], "codex")})
        ctx.report.migrated_clean.append(
            "config.toml:model → opencode.json:model")
    mcp = cfg.get("mcp_servers") or {}
    if mcp:
        opencode_write_mcp(
            ctx, {n: _native_mcp_from_codex(n, s) for n, s in mcp.items()})
    for k in ("model_reasoning_effort", "notify", "shell_environment_policy",
              "sandbox_mode", "approval_policy", "profiles"):
        if k in cfg:
            ctx.report.skipped_unmappable.append(
                f"config.toml:{k} (no opencode equivalent)")

    _run_lossy(ctx, lossy_decisions, "codex->opencode")


def run_opencode_to_codex(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    src_doc = ctx.src_root / "AGENTS.md"
    project = _opencode_project_root(ctx.src_root)
    if project and (project / "AGENTS.md").exists():
        src_doc = project / "AGENTS.md"
    dst_doc = ctx.dst_doc or (ctx.dst_root / "AGENTS.md")
    if src_doc.exists():
        if src_doc == dst_doc:
            ctx.report.notes.append(
                "AGENTS.md at the project root is read natively by both "
                "tools — no translation needed")
        else:
            body = src_doc.read_text(encoding="utf-8").rstrip() + "\n"
            if dst_doc.exists() and ctx.merge:
                existing = dst_doc.read_text(encoding="utf-8").rstrip()
                body = existing + "\n\n" + body if existing else body
            write_text(ctx, dst_doc, body)
            ctx.report.migrated_clean.append(f"AGENTS.md → {dst_doc.name}")
    tier_a_opencode_commands_to(ctx, "prompts")
    _skills_copy_from_opencode(ctx)
    _agents_write_codex(ctx, _agents_read_opencode(ctx.src_root))

    cfg = load_opencode_config(ctx.src_root)
    if isinstance(cfg.get("model"), str):
        model_id, provider = model_from_opencode(cfg["model"])
        dst = ctx.dst_root / "config.toml"
        existing = load_toml(dst) if (ctx.merge and dst.exists()) else {}
        existing["model"] = model_id
        write_text(ctx, dst, render_toml(existing))
        ctx.report.migrated_clean.append(
            f"opencode.json:model → config.toml:model={model_id}")
        if provider and provider != "openai":
            ctx.report.notes.append(
                f"model came from provider {provider!r} — verify Codex can "
                "run it")
    mcp = opencode_read_mcp(ctx.src_root)
    if mcp:
        dst = ctx.dst_root / "config.toml"
        existing = load_toml(dst) if (ctx.merge and dst.exists()) else {}
        existing.setdefault("mcp_servers", {})
        for name, spec in mcp.items():
            t = _normalize_mcp_claude_to_codex(name, spec, ctx.report)
            if t is not None:
                existing["mcp_servers"][name] = t
                ctx.report.migrated_clean.append(
                    f"opencode.json:mcp.{name} → [mcp_servers.{name}]")
        write_text(ctx, dst, render_toml(existing))
    _opencode_report_unmappables(ctx, cfg)

    _run_lossy(ctx, lossy_decisions, "opencode->codex")


def run_cursor_to_opencode(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    rules = cursor_read_rules(ctx.src_root)
    if rules:
        doc = cursor_rules_to_doc(rules)
        project = _opencode_project_root(ctx.dst_root)
        dst_doc = (project / "AGENTS.md") if project else (
            ctx.dst_root / "AGENTS.md")
        if dst_doc.exists() and ctx.merge:
            doc = dst_doc.read_text(encoding="utf-8").rstrip() + "\n\n" + doc
        write_text(ctx, dst_doc, doc + "\n")
        ctx.report.migrated_clean.append(
            f"{len(rules)} cursor rule(s) → {dst_doc.name}")
    tier_a_skills_copy(ctx)
    _agents_write_opencode(ctx, _agents_read_cursor(ctx.src_root), "cursor")

    mcp = cursor_read_mcp(ctx.src_root)
    if mcp:
        opencode_write_mcp(
            ctx, {n: _normalize_mcp_for_cursor(s) for n, s in mcp.items()})
    model = cursor_cli_config_read_model(ctx.src_root)
    if model:
        opencode_update_config(
            ctx, {"model": model_to_opencode(model, "cursor")})
        ctx.report.migrated_clean.append(
            "cli-config.json:model → opencode.json:model")
        if "/" not in model:
            ctx.report.notes.append(
                f"opencode model ids are provider-qualified — check "
                f"`{model}` resolves")

    _run_lossy(ctx, lossy_decisions, "cursor->opencode")


def run_opencode_to_cursor(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    src_doc = ctx.src_root / "AGENTS.md"
    project_src = _opencode_project_root(ctx.src_root)
    if project_src and (project_src / "AGENTS.md").exists():
        src_doc = project_src / "AGENTS.md"
    project_dst = cursor_project_root_from(ctx.dst_root)
    if (project_dst and src_doc.exists()
            and src_doc == project_dst / "AGENTS.md"):
        ctx.report.notes.append(
            "AGENTS.md at the project root is read natively by Cursor — "
            "no translation needed")
    elif src_doc.exists():
        rules = doc_to_cursor_rules(src_doc.read_text(encoding="utf-8"))
        if rules:
            cursor_write_rules(rules, ctx.dst_root, ctx)
            ctx.report.migrated_clean.append(
                f"AGENTS.md → {len(rules)} cursor rule file(s)")
    _skills_copy_from_opencode(ctx)
    for subdir in ("commands", "command"):
        tier_a_commands_to_cursor_skills(ctx, subdir, "commands")
    _agents_write_cursor(ctx, _agents_read_opencode(ctx.src_root))

    cfg = load_opencode_config(ctx.src_root)
    mcp = opencode_read_mcp(ctx.src_root)
    if mcp:
        out = {n: _normalize_mcp_for_cursor(s) for n, s in mcp.items()}
        cursor_write_mcp(out, ctx.dst_root, ctx)
        ctx.report.migrated_clean.append(_cursor_label_mcp("→ cursor mcp.json", out))
    if isinstance(cfg.get("model"), str):
        model_id, _ = model_from_opencode(cfg["model"])
        cursor_cli_config_write_model(ctx, model_id)
        ctx.report.migrated_clean.append(
            f"opencode.json:model → cli-config.json:model={model_id}")
    _opencode_report_unmappables(ctx, cfg)

    _run_lossy(ctx, lossy_decisions, "opencode->cursor")


# ---- claude → cursor -------------------------------------------------------

def run_claude_to_cursor(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    # Tier A: instruction doc.
    src_doc = (ctx.src_doc if ctx.src_doc and ctx.src_doc.exists()
               else ctx.src_root / "CLAUDE.md")
    if src_doc.exists():
        text = src_doc.read_text(encoding="utf-8")
        rules = doc_to_cursor_rules(text)
        if rules:
            cursor_write_rules(rules, ctx.dst_root, ctx)
            ctx.report.migrated_clean.append(
                f"{src_doc.name} → {len(rules)} cursor rule file(s)")
    native_rules = claude_read_rules_as_cursor(ctx)
    if native_rules:
        cursor_write_rules(native_rules, ctx.dst_root, ctx)
        ctx.report.migrated_clean.append(
            f".claude/rules ({len(native_rules)} file(s)) → cursor rule file(s)")

    # Tier A: MCP servers (Claude native MCP → Cursor's mcp.json).
    settings = load_claude_settings(ctx.src_root)
    mcp = claude_read_mcp(ctx)
    if mcp:
        out = {n: _normalize_mcp_for_cursor(s) for n, s in mcp.items()}
        cursor_write_mcp(out, ctx.dst_root, ctx)
        ctx.report.migrated_clean.append(_cursor_label_mcp("→ cursor mcp.json", out))

    # Tier A: skills (shared Agent Skills format) + slash commands (Cursor
    # deprecated .cursor/commands in favor of slash-invocable skills).
    tier_a_skills_copy(ctx)
    tier_a_commands_to_cursor_skills(ctx, "commands", "commands")

    # Tier A: default model → Cursor CLI config.
    if isinstance(settings.get("model"), str):
        cursor_cli_config_write_model(ctx, settings["model"])
        ctx.report.migrated_clean.append(
            "settings.json:model → cli-config.json:model")

    # Settings keys with no Cursor equivalent (truly Tier C). Hooks have a
    # Tier B option below; permissions have a partial Cursor CLI analog
    # the user has to build by hand.
    for key in ("effortLevel", "env"):
        if settings.get(key):
            ctx.report.skipped_unmappable.append(
                f"settings.json:{key} (Cursor has no equivalent)")
    if settings.get("permissions"):
        ctx.report.skipped_unmappable.append(
            "settings.json:permissions (no automatic mapping — Cursor CLI "
            "has its own allow/deny rules in ~/.cursor/cli-config.json)")
    for key in ("statusLine", "outputStyle"):
        if settings.get(key):
            ctx.report.skipped_unmappable.append(
                f"settings.json:{key} (Cursor has no equivalent)")
    # `plugins/`: Cursor has a plugin system now, but the layouts differ
    # (Claude marketplaces vs .cursor-plugin manifests) — manual only.
    plugins_dir = ctx.src_root / "plugins"
    if plugins_dir.is_dir() and any(plugins_dir.iterdir()):
        ctx.report.skipped_unmappable.append(
            "plugins/ (Cursor plugins use a different manifest format — "
            "reinstall from the Cursor marketplace)")

    # Tier B options applicable in this direction.
    _run_lossy(ctx, lossy_decisions, "claude->cursor")


# ---- cursor → claude -------------------------------------------------------

def run_cursor_to_claude(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    # Tier A: rules → native .claude/rules/*.md.
    rules = cursor_read_rules(ctx.src_root)
    if rules:
        cursor_rules_to_claude_rules(rules, ctx.dst_root, ctx)
        ctx.report.migrated_clean.append(
            f"{len(rules)} cursor rule(s) → .claude/rules/*.md")

    # Tier A: MCP.
    mcp = cursor_read_mcp(ctx.src_root)
    if mcp:
        out = {n: _normalize_mcp_for_cursor(s) for n, s in mcp.items()}
        for s in out.values():
            s.setdefault("type", "stdio")
        claude_write_mcp(ctx, out)
        ctx.report.migrated_clean.append(
            _cursor_label_mcp(f"→ {claude_mcp_path(ctx).name}:mcpServers", mcp))

    tier_a_skills_copy(ctx)
    tier_a_cursor_agents_to_claude(ctx)

    model = cursor_cli_config_read_model(ctx.src_root)
    if model:
        dst = ctx.dst_root / "settings.json"
        existing = load_json(dst) if (ctx.merge and dst.exists()) else {}
        existing["model"] = model
        write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")
        ctx.report.migrated_clean.append(
            "cli-config.json:model → settings.json:model")

    _run_lossy(ctx, lossy_decisions, "cursor->claude")


# ---- codex → cursor --------------------------------------------------------

def run_codex_to_cursor(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    # Tier A: AGENTS.md → cursor rules. At project scope, Cursor reads
    # <project>/AGENTS.md natively, so the file already works as-is; the
    # rules copy is only needed for user scope (~/.codex/AGENTS.md has no
    # Cursor-side equivalent location).
    src_doc = (ctx.src_doc if ctx.src_doc and ctx.src_doc.exists()
               else ctx.src_root / "AGENTS.md")
    project_root = cursor_project_root_from(ctx.dst_root)
    if (project_root and src_doc.exists()
            and src_doc == project_root / "AGENTS.md"):
        ctx.report.notes.append(
            "AGENTS.md at the project root is read natively by Cursor — "
            "no translation needed")
    elif src_doc.exists():
        text = src_doc.read_text(encoding="utf-8")
        rules = doc_to_cursor_rules(text)
        if rules:
            cursor_write_rules(rules, ctx.dst_root, ctx)
            ctx.report.migrated_clean.append(
                f"{src_doc.name} → {len(rules)} cursor rule file(s)")

    # Tier A: MCP (TOML → Cursor JSON).
    cfg = load_toml(ctx.src_root / "config.toml")
    mcp = cfg.get("mcp_servers") or {}
    if mcp:
        out = {n: _native_mcp_from_codex(n, s) for n, s in mcp.items()}
        cursor_write_mcp(out, ctx.dst_root, ctx)
        ctx.report.migrated_clean.append(_cursor_label_mcp("→ cursor mcp.json", out))

    tier_a_skills_copy(ctx)
    tier_a_commands_to_cursor_skills(ctx, "prompts", "prompts")

    if isinstance(cfg.get("model"), str):
        cursor_cli_config_write_model(ctx, cfg["model"])
        ctx.report.migrated_clean.append(
            "config.toml:model → cli-config.json:model")

    # Codex-only items with no Cursor equivalent.
    for k in ("approval_policy", "sandbox_mode", "sandbox_workspace_write",
              "shell_environment_policy", "profiles",
              "model_reasoning_effort", "notify"):
        if k in cfg:
            ctx.report.skipped_unmappable.append(
                f"config.toml:{k} (Cursor has no equivalent)")

    _run_lossy(ctx, lossy_decisions, "codex->cursor")


# ---- cursor → codex --------------------------------------------------------

def run_cursor_to_codex(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    rules = cursor_read_rules(ctx.src_root)
    if rules:
        doc = cursor_rules_to_doc(rules)
        dst_doc = ctx.dst_doc or (ctx.dst_root / "AGENTS.md")
        if dst_doc.exists() and ctx.merge:
            doc = dst_doc.read_text(encoding="utf-8").rstrip() + "\n\n" + doc
        write_text(ctx, dst_doc, doc + "\n")
        ctx.report.migrated_clean.append(
            f"{len(rules)} cursor rule(s) → {dst_doc.name}")

    mcp = cursor_read_mcp(ctx.src_root)
    if mcp:
        dst = ctx.dst_root / "config.toml"
        existing = load_toml(dst) if (ctx.merge and dst.exists()) else {}
        existing.setdefault("mcp_servers", {})
        for name, spec in mcp.items():
            t = _normalize_mcp_claude_to_codex(name, spec, ctx.report)
            if t is not None:
                existing["mcp_servers"][name] = t
        write_text(ctx, dst, render_toml(existing))
        ctx.report.migrated_clean.append(
            _cursor_label_mcp("→ config.toml:[mcp_servers.*]", mcp))

    tier_a_skills_copy(ctx)

    model = cursor_cli_config_read_model(ctx.src_root)
    if model:
        dst = ctx.dst_root / "config.toml"
        existing = load_toml(dst) if (ctx.merge and dst.exists()) else {}
        existing["model"] = model
        write_text(ctx, dst, render_toml(existing))
        ctx.report.migrated_clean.append(
            "cli-config.json:model → config.toml:model")

    _run_lossy(ctx, lossy_decisions, "cursor->codex")


def _run_lossy(ctx: Ctx, decisions: dict[str, bool], direction_key: str) -> None:
    """Shared Tier B runner: iterate the catalog, applying detected items
    the user accepted and recording the rest as declined."""
    for opt in TIER_B:
        if opt.direction != direction_key or not opt.detect(ctx):
            continue
        if decisions.get(opt.id):
            opt.apply(ctx)
        else:
            ctx.report.skipped_by_user.append(f"{opt.label} (declined)")


# ============================================================================
# Tier B — lossy translations (user-confirmed)
# ============================================================================
# Tier B items don't have an exact equivalent on the other side, so the
# migration is heuristic. Each option's `detect` says whether the relevant
# source state exists, `preview` produces a one-line "here's what will
# happen" string for the preflight UI, and `apply` performs the
# translation. The user accepts or skips each one (interactively, or via
# --apply-lossy / --skip-lossy in non-interactive mode).

@dataclass
class LossyOption:
    id: str
    direction: str  # e.g. 'claude->codex', 'cursor->claude', 'codex->cursor'
    label: str
    rationale: str
    detect: Callable[[Ctx], bool]
    preview: Callable[[Ctx], str]
    apply: Callable[[Ctx], None]


# ---- B1: permissions ↔ sandbox + approval + prefix rules -------------------
# Codex has two permission surfaces: the coarse sandbox_mode/approval_policy
# knobs in config.toml, and per-command Starlark prefix rules in
# rules/*.rules — `prefix_rule(pattern=["git", "commit"], decision="allow")`.
# The prefix rules map naturally onto Claude's `Bash(git commit:*)`
# permission patterns, so both directions translate command rules
# rule-for-rule and use the sandbox knobs only for the overall posture.

_RULE_DECISION_C2X = {"allow": "allow", "ask": "prompt", "deny": "forbidden"}
_RULE_DECISION_X2C = {"allow": "allow", "prompt": "ask", "forbidden": "deny"}


def _claude_bash_rule_to_tokens(rule: str) -> list[str] | None:
    """`Bash(git commit:*)` → ["git", "commit"], or None if the rule can't
    be expressed as a Codex prefix rule (bare Bash, wildcards mid-command,
    unparseable quoting)."""
    m = re.fullmatch(r"Bash\((?P<body>.+)\)", rule.strip())
    if not m:
        return None
    body = m.group("body").strip()
    if body.endswith(":*"):
        body = body[:-2]
    if not body or "*" in body:
        return None
    try:
        tokens = shlex.split(body)
    except ValueError:
        return None
    return tokens or None


def _render_prefix_rule(tokens: list[str], decision: str) -> str:
    pattern = ", ".join(json.dumps(t, ensure_ascii=False) for t in tokens)
    return f'prefix_rule(pattern=[{pattern}], decision="{decision}")'


def _parse_prefix_rules(text: str) -> list[tuple[list[str] | None, str]]:
    """Extract (tokens, decision) pairs from a Codex .rules file.

    Returns tokens=None for patterns this migrator can't translate
    (union elements — nested lists inside `pattern`). This is a pragmatic
    scan of the documented `prefix_rule(...)` shape, not a full Starlark
    parser."""
    out: list[tuple[list[str] | None, str]] = []
    for m in re.finditer(r"prefix_rule\s*\(", text):
        # Walk to the matching close paren, respecting strings.
        i, depth, in_str, esc = m.end(), 1, False, False
        while i < len(text) and depth:
            c = text[i]
            if esc:
                esc = False
            elif in_str:
                if c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        body = text[m.end():i - 1]
        pm = re.search(r"pattern\s*=\s*\[", body)
        if not pm:
            continue
        # Balanced-bracket scan for the pattern list (may contain unions).
        j, bdepth, in_str, esc = pm.end(), 1, False, False
        while j < len(body) and bdepth:
            c = body[j]
            if esc:
                esc = False
            elif in_str:
                if c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "[":
                bdepth += 1
            elif c == "]":
                bdepth -= 1
            j += 1
        inner = body[pm.end():j - 1]
        dm = re.search(r'decision\s*=\s*"(\w+)"', body)
        decision = dm.group(1) if dm else "allow"
        if "[" in inner:
            out.append((None, decision))
            continue
        tokens = [re.sub(r'\\(.)', r"\1", t)
                  for t in re.findall(r'"((?:[^"\\]|\\.)*)"', inner)]
        out.append((tokens or None, decision))
    return out


# First tokens whose `allow` prefix rules Codex 0.145+ deletes from
# rules/default.rules on first launch (one-time protected-prefix
# migration). Approximation of Codex's list — used only to warn.
_CODEX_PROTECTED_PREFIXES = frozenset((
    "sh", "bash", "zsh", "fish", "dash", "env", "eval", "source", "xargs",
    "python", "python3", "node", "deno", "bun", "ruby", "perl",
    "npx", "npm", "pnpm", "yarn", "uv", "uvx", "pip", "pip3",
    "rm", "sudo", "chmod", "chown", "dd", "mkfs", "kill", "killall",
))


def _emit_codex_prefix_rules(ctx: Ctx, perms: dict) -> None:
    """Claude Bash() permission rules → Codex rules/default.rules.

    Appends to (rather than replaces) default.rules because Codex's smart
    approvals write user-accepted rules to the same file. Duplicate
    patterns already present are skipped."""
    dst = ctx.dst_root / "rules" / "default.rules"
    existing = dst.read_text(encoding="utf-8") if dst.exists() else ""
    lines: list[str] = []
    skipped: list[str] = []
    protected: list[str] = []
    for key, decision in _RULE_DECISION_C2X.items():
        for rule in perms.get(key) or []:
            if not str(rule).startswith("Bash"):
                continue
            tokens = _claude_bash_rule_to_tokens(str(rule))
            if tokens is None:
                skipped.append(str(rule))
                continue
            if decision == "allow" and tokens[0] in _CODEX_PROTECTED_PREFIXES:
                protected.append(shlex.join(tokens))
            rendered = _render_prefix_rule(tokens, decision)
            if rendered not in existing and rendered not in lines:
                lines.append(rendered)
    if protected:
        ctx.report.notes.append(
            "Codex 0.145+ strips allow rules for protected commands "
            "(shells, interpreters, package runners, destructive tools) on "
            "first launch — these may not survive: "
            + ", ".join(sorted(set(protected))))
    if lines:
        body = existing.rstrip() + "\n" if existing.strip() else ""
        write_text(ctx, dst, body + "\n".join(lines) + "\n")
        ctx.report.migrated_lossy.append(
            f"permissions Bash rules → rules/default.rules "
            f"({len(lines)} prefix rule(s); exact-match rules widen to "
            "prefix matches)")
    for rule in skipped:
        ctx.report.notes.append(
            f"permissions: {rule} not expressible as a Codex prefix rule "
            "(covered only by the coarse sandbox mode)")

def _detect_claude_permissions(ctx: Ctx) -> bool:
    s = load_claude_settings(ctx.src_root)
    return bool(s.get("permissions"))


def _preview_claude_permissions(ctx: Ctx) -> str:
    s = load_claude_settings(ctx.src_root).get("permissions", {})
    parts = []
    for key in ("allow", "deny", "ask"):
        if s.get(key):
            parts.append(f"{key}={len(s[key])} rules")
    return ("permissions → sandbox_mode + approval_policy (heuristic) + "
            "Bash rules → rules/default.rules. " + ", ".join(parts))


def _apply_claude_permissions(ctx: Ctx) -> None:
    """Heuristic: Claude's per-tool allow/deny patterns are richer than
    Codex's coarse sandbox modes. We classify the rules into a closest-fit
    sandbox mode + extract Write() patterns into writable_roots + deny of
    WebFetch/WebSearch into network_access=false. Round-trip is *not*
    byte-identical — that's the lossy part."""
    s = load_claude_settings(ctx.src_root).get("permissions", {}) or {}
    allow = s.get("allow") or []
    deny = s.get("deny") or []

    dst = ctx.dst_root / "config.toml"
    existing = load_toml(dst) if dst.exists() else {}

    has_bash_all = any(re.fullmatch(r"Bash\(\*\)?|.*\*.*", a) and "Bash" in a for a in allow)
    only_reads = allow and all(a.startswith("Read(") for a in allow)
    write_patterns = [a for a in allow if a.startswith("Write(")]
    deny_net = any(re.search(r"Web(Fetch|Search)", d) for d in deny)

    if has_bash_all and not deny:
        existing["approval_policy"] = "never"
        existing["sandbox_mode"] = "danger-full-access"
        explain = "Bash(*) in allow + no deny → danger-full-access / approval=never"
    elif only_reads:
        existing["sandbox_mode"] = "read-only"
        existing["approval_policy"] = "on-request"
        explain = "Only Read() patterns allowed → sandbox=read-only / approval=on-request"
    else:
        existing["sandbox_mode"] = "workspace-write"
        existing["approval_policy"] = "on-request"
        explain = "Mixed rules → sandbox=workspace-write / approval=on-request"

    if write_patterns:
        roots: list[str] = []
        for w in write_patterns:
            m = re.match(r"Write\((.+?)\)", w)
            if m:
                p = re.sub(r"/?\*\*?$", "", m.group(1)).rstrip("/")
                if p and p not in roots:
                    roots.append(p)
        if roots:
            existing.setdefault("sandbox_workspace_write", {})
            existing["sandbox_workspace_write"]["writable_roots"] = roots
    if deny_net:
        existing.setdefault("sandbox_workspace_write", {})
        existing["sandbox_workspace_write"]["network_access"] = False

    write_text(ctx, dst, render_toml(existing))
    ctx.report.migrated_lossy.append(
        f"permissions → sandbox_mode/approval_policy ({explain})")

    # Per-command Bash rules translate rule-for-rule into Codex prefix rules.
    _emit_codex_prefix_rules(ctx, s)


def _detect_codex_rules(ctx: Ctx) -> bool:
    p = ctx.src_root / "rules"
    return p.is_dir() and any(p.glob("*.rules"))


def _preview_codex_rules(ctx: Ctx) -> str:
    n = 0
    for f in sorted((ctx.src_root / "rules").glob("*.rules")):
        n += len(_parse_prefix_rules(f.read_text(encoding="utf-8")))
    return (f"{n} prefix rule(s) in rules/*.rules → permissions "
            "Bash(...) patterns (allow/prompt→ask/forbidden→deny)")


def _apply_codex_rules(ctx: Ctx) -> None:
    dst = ctx.dst_root / "settings.json"
    existing = load_json(dst) if dst.exists() else {}
    perms = existing.setdefault("permissions", {})
    added = 0
    for f in sorted((ctx.src_root / "rules").glob("*.rules")):
        for tokens, decision in _parse_prefix_rules(
                f.read_text(encoding="utf-8")):
            key = _RULE_DECISION_X2C.get(decision)
            if key is None:
                ctx.report.notes.append(
                    f"rules/{f.name}: unknown decision {decision!r} skipped")
                continue
            if tokens is None:
                ctx.report.notes.append(
                    f"rules/{f.name}: a prefix rule with union pattern "
                    "elements couldn't be translated — recreate by hand")
                continue
            rule = f"Bash({shlex.join(tokens)}:*)"
            bucket = perms.setdefault(key, [])
            if rule not in bucket:
                bucket.append(rule)
                added += 1
    if added:
        write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")
        ctx.report.migrated_lossy.append(
            f"rules/*.rules → permissions ({added} Bash prefix rule(s); "
            "prefix semantics approximated with :* patterns)")


def _detect_codex_sandbox(ctx: Ctx) -> bool:
    c = load_toml(ctx.src_root / "config.toml")
    return any(k in c for k in
               ("sandbox_mode", "approval_policy", "sandbox_workspace_write"))


def _preview_codex_sandbox(ctx: Ctx) -> str:
    c = load_toml(ctx.src_root / "config.toml")
    parts = []
    if "sandbox_mode" in c:
        parts.append(f"sandbox_mode={c['sandbox_mode']}")
    if "approval_policy" in c:
        parts.append(f"approval_policy={c['approval_policy']}")
    return "sandbox/approval → permissions (heuristic). " + ", ".join(parts)


def _apply_codex_sandbox(ctx: Ctx) -> None:
    c = load_toml(ctx.src_root / "config.toml")
    dst = ctx.dst_root / "settings.json"
    existing = load_json(dst) if dst.exists() else {}
    perms = existing.setdefault("permissions", {})
    allow = list(perms.get("allow") or [])
    deny = list(perms.get("deny") or [])

    mode = c.get("sandbox_mode", "workspace-write")
    if mode == "danger-full-access":
        allow = ["Bash(*)", "Read(*)", "Write(*)", "WebFetch(*)"]
    elif mode == "read-only":
        allow = ["Read(*)"]
        deny += ["Write(*)", "Bash(*)"]
    else:  # workspace-write
        allow = ["Read(*)", "Bash(*)"]
        sww = c.get("sandbox_workspace_write") or {}
        roots = sww.get("writable_roots") or ["."]
        for r in roots:
            allow.append(f"Write({r.rstrip('/')}/**)")
        if sww.get("network_access") is False:
            deny += ["WebFetch(*)"]

    perms["allow"] = sorted(set(allow))
    if deny:
        perms["deny"] = sorted(set(deny))

    write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")
    ctx.report.migrated_lossy.append(
        f"sandbox_mode={mode!r} + approval_policy={c.get('approval_policy','?')!r} "
        "→ permissions.allow/deny (coarse)")


# ---- B2: hooks ↔ hooks.json / notify ----------------------------------------
# Codex has a Claude-style hooks system now (~/.codex/hooks.json with the
# same event names and the same {matcher, hooks:[{type:"command",...}]}
# shape), so shared events translate almost verbatim. Claude's
# `Notification` event has no Codex hook equivalent but maps onto the
# legacy `notify` program. Non-command hook types (http, mcp_tool, prompt,
# agent) and either side's exclusive events are dropped with notes —
# that residual loss is why this stays Tier B.

HOOK_EVENTS_SHARED_CODEX = (
    "SessionStart", "SubagentStart", "UserPromptSubmit", "PreToolUse",
    "PermissionRequest", "PostToolUse", "PreCompact", "PostCompact",
    "SubagentStop", "Stop",
    "SessionEnd",  # Codex 0.145+ (1s default timeout there — keep hooks fast)
)


def _detect_claude_notify_hook(ctx: Ctx) -> bool:
    return bool(load_claude_settings(ctx.src_root).get("hooks"))


def _preview_claude_notify_hook(ctx: Ctx) -> str:
    hooks = load_claude_settings(ctx.src_root).get("hooks") or {}
    shared = [k for k in hooks if k in HOOK_EVENTS_SHARED_CODEX]
    other = [k for k in hooks
             if k not in HOOK_EVENTS_SHARED_CODEX and k != "Notification"]
    parts = []
    if shared:
        parts.append(f"hooks: {', '.join(shared)} → .codex/hooks.json")
    if "Notification" in hooks:
        parts.append("Notification → notify")
    msg = "; ".join(parts) or "hooks: (no translatable events)"
    if other:
        msg += f". DROPPED events: {', '.join(other)}"
    return msg


def _extract_first_command(entries: list) -> list[str] | None:
    """Extract the first runnable shell command from a Claude hook config.

    Claude hooks are: [{matcher, hooks: [{type:'command', command:'...'}]}].
    Codex `notify` takes an argv-style list. We wrap the user's shell
    command in `/bin/sh -c` rather than trying to tokenize it, since
    Claude commands are meant to be shell-parsed (with redirects, pipes,
    `$VAR` expansion, etc.).
    """
    for grp in entries or []:
        for h in (grp.get("hooks") or []) if isinstance(grp, dict) else []:
            if h.get("type") == "command" and h.get("command"):
                return ["/bin/sh", "-c", h["command"]]
    return None


def _command_hooks_only(ctx: Ctx, event: str, grp: dict,
                        source: str) -> list[dict]:
    """Filter one hook group down to command-type hooks, keeping the
    fields both tools understand. Non-command hooks are noted."""
    out: list[dict] = []
    for h in grp.get("hooks") or []:
        if h.get("type") == "command" and h.get("command"):
            kept = {"type": "command", "command": h["command"]}
            for k in ("timeout", "statusMessage"):
                if h.get(k):
                    kept[k] = h[k]
            out.append(kept)
        else:
            ctx.report.notes.append(
                f"{source}:{event}: non-command hook "
                f"(type={h.get('type')!r}) not translated")
    return out


def _apply_claude_notify_hook(ctx: Ctx) -> None:
    s = load_claude_settings(ctx.src_root)
    hooks = s.get("hooks") or {}

    # Shared events → hooks.json (near-verbatim, matchers included).
    dst_hooks = ctx.dst_root / "hooks.json"
    existing_hooks = load_json(dst_hooks) if (ctx.merge and dst_hooks.exists()) else {}
    out = existing_hooks.setdefault("hooks", {})
    migrated_events: list[str] = []
    for event in HOOK_EVENTS_SHARED_CODEX:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        for grp in groups:
            if not isinstance(grp, dict):
                continue
            kept = _command_hooks_only(ctx, event, grp, "hooks")
            if not kept:
                continue
            entry: dict[str, Any] = {"hooks": kept}
            if grp.get("matcher"):
                entry["matcher"] = grp["matcher"]
            out.setdefault(event, []).append(entry)
            if event not in migrated_events:
                migrated_events.append(event)
    if migrated_events:
        write_text(ctx, dst_hooks, json.dumps(existing_hooks, indent=2) + "\n")
        ctx.report.migrated_lossy.append(
            f"hooks ({', '.join(migrated_events)}) → .codex/hooks.json "
            "(command hooks only)")

    # Notification → legacy notify program.
    cmd = _extract_first_command(hooks.get("Notification"))
    if cmd:
        dst = ctx.dst_root / "config.toml"
        existing = load_toml(dst) if dst.exists() else {}
        existing["notify"] = cmd
        write_text(ctx, dst, render_toml(existing))
        ctx.report.migrated_lossy.append(
            "hooks.Notification → config.toml:notify "
            "(wrapped via /bin/sh -c; matcher patterns dropped)")

    for k in hooks:
        if k not in HOOK_EVENTS_SHARED_CODEX and k != "Notification":
            ctx.report.skipped_unmappable.append(
                f"hooks.{k} (no Codex hook event equivalent)")


def _detect_codex_hooks(ctx: Ctx) -> bool:
    return bool(load_json(ctx.src_root / "hooks.json").get("hooks"))


def _preview_codex_hooks(ctx: Ctx) -> str:
    hooks = load_json(ctx.src_root / "hooks.json").get("hooks") or {}
    shared = [k for k in hooks if k in HOOK_EVENTS_SHARED_CODEX]
    other = [k for k in hooks if k not in HOOK_EVENTS_SHARED_CODEX]
    msg = f"hooks.json: {', '.join(shared) or '(none)'} → settings.json:hooks"
    if other:
        msg += f". DROPPED events: {', '.join(other)}"
    return msg


def _apply_codex_hooks(ctx: Ctx) -> None:
    hooks = load_json(ctx.src_root / "hooks.json").get("hooks") or {}
    dst = ctx.dst_root / "settings.json"
    existing = load_json(dst) if (ctx.merge and dst.exists()) else {}
    out = existing.setdefault("hooks", {})
    migrated_events: list[str] = []
    for event, groups in hooks.items():
        if event not in HOOK_EVENTS_SHARED_CODEX:
            ctx.report.skipped_unmappable.append(
                f"hooks.json:{event} (no Claude hook event equivalent)")
            continue
        for grp in groups if isinstance(groups, list) else []:
            if not isinstance(grp, dict):
                continue
            kept = _command_hooks_only(ctx, event, grp, "hooks.json")
            if any(h.get("commandWindows") for h in grp.get("hooks") or []):
                ctx.report.notes.append(
                    f"hooks.json:{event}: commandWindows variants dropped "
                    "(Claude hooks have no Windows-specific command field)")
            if not kept:
                continue
            entry: dict[str, Any] = {"hooks": kept}
            if grp.get("matcher"):
                entry["matcher"] = grp["matcher"]
            out.setdefault(event, []).append(entry)
            if event not in migrated_events:
                migrated_events.append(event)
    if migrated_events:
        write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")
        ctx.report.migrated_lossy.append(
            f"hooks.json ({', '.join(migrated_events)}) → settings.json:hooks "
            "(command hooks only)")


def _detect_codex_notify(ctx: Ctx) -> bool:
    c = load_toml(ctx.src_root / "config.toml")
    return bool(c.get("notify"))


def _preview_codex_notify(ctx: Ctx) -> str:
    c = load_toml(ctx.src_root / "config.toml")
    return f"notify={c.get('notify')} → hooks.Notification"


def _apply_codex_notify(ctx: Ctx) -> None:
    c = load_toml(ctx.src_root / "config.toml")
    cmd = c.get("notify")
    if not cmd:
        return
    if isinstance(cmd, list):
        argv = [str(c) for c in cmd]
        if len(argv) == 3 and argv[:2] == ["/bin/sh", "-c"]:
            # Unwrap the shell wrapping a claude→codex migration added, so
            # the hook command round-trips to its original form.
            cmd_str = argv[2]
        else:
            cmd_str = shlex.join(argv)
    else:
        cmd_str = str(cmd)
    dst = ctx.dst_root / "settings.json"
    existing = load_json(dst) if dst.exists() else {}
    existing.setdefault("hooks", {}).setdefault("Notification", []).append({
        "hooks": [{"type": "command", "command": cmd_str}],
    })
    write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")
    ctx.report.migrated_lossy.append(
        "config.toml:notify → hooks.Notification (single command, no matcher)")


# ---- B3: Claude subagents → Codex custom-agent TOML ------------------------

def _detect_claude_agents(ctx: Ctx) -> bool:
    p = ctx.src_root / "agents"
    return p.is_dir() and any(p.glob("*.md"))


def _preview_claude_agents(ctx: Ctx) -> str:
    n = sum(1 for _ in (ctx.src_root / "agents").glob("*.md"))
    return f"{n} subagent file(s) → .codex/agents/*.toml (Codex custom agents)"


def _render_codex_agent_body(body: str, fm: dict, unsupported: list[str]) -> str:
    sections: list[str] = []
    skills = _csv_values(fm.get("skills"))
    tools = _csv_values(fm.get("tools"))
    disallowed = _csv_values(fm.get("disallowedTools"))
    permission_mode = fm.get("permissionMode")

    if skills:
        sections.append(
            "## Skills\n\nYou're allowed to use these skills when working on this task:\n\n" +
            "\n".join(f"- ${s}" for s in skills)
        )
    if tools or disallowed:
        lines = [
            "## Tools",
            "",
            "Claude tool allow/deny lists were preserved as prompt guidance, not Codex permissions.",
        ]
        if tools:
            lines += ["", "You're allowed to use these tools:", "", *[f"- {t}" for t in tools]]
        if disallowed:
            lines += ["", "Don't use these tools:", "", *[f"- {t}" for t in disallowed]]
        sections.append("\n".join(lines))

    notes: list[str] = []
    if permission_mode and not _claude_permission_to_codex_sandbox(permission_mode):
        notes.append(
            f"Claude permissionMode={permission_mode!r} has no direct Codex mapping; "
            "choose sandbox_mode or permissions manually if needed."
        )
    if skills:
        notes.append("Claude skills preload semantics were preserved as prompt guidance.")
    if tools or disallowed:
        notes.append("Rebuild Claude tool allow/deny intent with Codex sandbox, MCP filters, or app tool filters if hard enforcement is required.")
    if unsupported:
        notes.append("Review unsupported Claude subagent fields manually: " + ", ".join(f"`{x}`" for x in unsupported) + ".")
    if notes:
        sections.append("## MANUAL MIGRATION REQUIRED\n\n" + "\n".join(f"- {n}" for n in notes))
    if not sections:
        return body.strip()
    return body.rstrip() + "\n\n" + "\n\n".join(sections)


def _apply_claude_agents(ctx: Ctx) -> None:
    src_dir = ctx.src_root / "agents"
    dst_dir = ctx.dst_root / "agents"
    supported = {
        "name", "description", "model", "permissionMode", "skills",
        "tools", "disallowedTools", "effort",
    }
    for f in sorted(src_dir.glob("*.md")):
        if f.stem == "README":
            continue
        rel = f.relative_to(src_dir)
        body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
        fm = fm or {}
        name = safe_agent_name(fm.get("name") or f.stem)
        description = fm.get("description") or (
            f"Migrated Claude subagent inferred from `{_first_heading(body)}`."
            if _first_heading(body) else f"Migrated Claude subagent inferred from `{f.name}`."
        )
        unsupported = sorted(k for k in fm if k not in supported)
        agent: dict[str, object] = {
            "name": name,
            "description": description,
            "developer_instructions": _render_codex_agent_body(body, fm, unsupported),
        }
        if fm.get("model"):
            agent["model"] = fm["model"]
        if fm.get("effort"):
            mapped = EFFORT_C2X.get(fm["effort"], fm["effort"])
            agent["model_reasoning_effort"] = mapped
        sandbox = _claude_permission_to_codex_sandbox(fm.get("permissionMode"))
        if sandbox:
            agent["sandbox_mode"] = sandbox

        out_name = f"{name}.toml"
        write_text(ctx, dst_dir / out_name, render_toml(agent))
        caveats = []
        if unsupported:
            caveats.append("unsupported fields")
        if fm.get("skills") or fm.get("tools") or fm.get("disallowedTools"):
            caveats.append("prompt-guidance fields")
        suffix = f" (review: {', '.join(caveats)})" if caveats else ""
        ctx.report.migrated_lossy.append(
            f"agents/{rel} → agents/{out_name} (Codex custom agent){suffix}")


# ---- B5: codex profiles → ~/.claude/profiles/*.json -----------------------
# Codex 0.134 moved profiles out of config.toml: each profile is now its
# own `<name>.config.toml` next to config.toml, and the legacy inline
# `[profiles.*]` tables are rejected. We read both shapes (old configs
# still exist in the wild) and materialize each as a standalone Claude
# settings file.

def _codex_read_profiles(root: Path) -> dict[str, dict]:
    profiles: dict[str, dict] = {}
    cfg = load_toml(root / "config.toml")
    for name, override in (cfg.get("profiles") or {}).items():
        if isinstance(override, dict):
            profiles[name] = override
    for f in sorted(root.glob("*.config.toml")):
        data = load_toml(f)
        if data:
            profiles[f.name[:-len(".config.toml")]] = data
    return profiles


def _detect_codex_profiles(ctx: Ctx) -> bool:
    return bool(_codex_read_profiles(ctx.src_root))


def _preview_codex_profiles(ctx: Ctx) -> str:
    names = list(_codex_read_profiles(ctx.src_root))
    return (f"{len(names)} Codex profile(s): {', '.join(names)} → "
            "~/.claude/profiles/*.settings.json (no Claude profile runtime; "
            "swap manually)")


def _apply_codex_profiles(ctx: Ctx) -> None:
    c = load_toml(ctx.src_root / "config.toml")
    base = {k: v for k, v in c.items() if k != "profiles"}
    profiles = _codex_read_profiles(ctx.src_root)
    out_dir = ctx.dst_root / "profiles"
    for name, override in profiles.items():
        merged = {**base, **(override if isinstance(override, dict) else {})}
        # Translate merged config into Claude settings shape using a temp ctx.
        sub_report = Report(direction=f"profile:{name}")
        # Build a transient cfg file scenario inline:
        out: dict = {}
        if "model" in merged:
            out["model"] = merged["model"]
        if merged.get("mcp_servers"):
            out["mcpServers"] = {
                n: _normalize_mcp_codex_to_claude(spec)
                for n, spec in merged["mcp_servers"].items()
            }
        sep = merged.get("shell_environment_policy") or {}
        if sep.get("set"):
            out["env"] = {k: str(v) for k, v in sep["set"].items()}
        if isinstance(merged.get("model_reasoning_effort"), str):
            m = EFFORT_X2C.get(merged["model_reasoning_effort"])
            if m:
                out["effortLevel"] = m
        path = out_dir / f"{name}.settings.json"
        write_text(ctx, path, json.dumps(out, indent=2) + "\n")
        ctx.report.migrated_lossy.append(
            f"[profiles.{name}] → {path.relative_to(ctx.dst_root)} "
            "(no Claude profile runtime — copy over settings.json to activate)")
        _ = sub_report  # unused but kept to mirror structure


# ---- B6: claude subagents → native cursor subagents ------------------------
# Cursor 2.4+ has a native subagent runtime: markdown files under
# <cursor_root>/agents/ with name/description/model(+bracket effort)/
# readonly/is_background frontmatter. name/description/model/effort/
# background translate; Claude-only fields (tools, hooks, memory,
# skills, maxTurns, …) have no Cursor equivalent — that residual loss is
# why this stays Tier B. (Skills and slash commands are NOT in this
# bucket anymore: skills copy verbatim and commands become
# slash-invocable Cursor skills, both Tier A.)

def _detect_claude_agents_cursor(ctx: Ctx) -> bool:
    p = ctx.src_root / "agents"
    return p.is_dir() and any(p.rglob("*.md"))


def _preview_claude_agents_cursor(ctx: Ctx) -> str:
    n = sum(1 for _ in (ctx.src_root / "agents").rglob("*.md"))
    return (f"{n} subagent file(s) → .cursor/agents/*.md (native Cursor "
            "subagents; tools/hooks/memory fields don't carry)")


_CLAUDE_AGENT_FIELDS_CURSOR = {
    "name", "description", "model", "effort", "background", "permissionMode",
}


def _apply_claude_agents_cursor(ctx: Ctx) -> None:
    src_dir = ctx.src_root / "agents"
    dst_dir = ctx.dst_root / "agents"
    for f in sorted(src_dir.rglob("*.md")):
        if f.stem == "README":
            continue
        rel = f.relative_to(src_dir)
        body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
        fm = fm or {}
        name = safe_agent_name(fm.get("name") or f.stem)
        out_fm: dict[str, str] = {
            "name": name,
            "description": fm.get("description")
            or f"Migrated from Claude subagent {name}.",
        }
        model, effort = fm.get("model"), fm.get("effort")
        if model and effort:
            # Cursor folds effort into the model id: `model[effort=high]`.
            out_fm["model"] = f"{model}[effort={effort}]"
        elif model:
            out_fm["model"] = model
        if _fm_bool(fm.get("background", "")):
            out_fm["is_background"] = "true"
        if fm.get("permissionMode") in ("readOnly", "plan"):
            out_fm["readonly"] = "true"
        dropped = sorted(k for k in fm if k not in _CLAUDE_AGENT_FIELDS_CURSOR)
        if effort and not model:
            dropped.append("effort (needs an explicit model in Cursor)")
        if dropped:
            body = body.rstrip() + (
                "\n\n## Manual migration notes\n\n"
                "- Claude subagent fields with no Cursor equivalent were "
                "dropped: " + ", ".join(f"`{d}`" for d in dropped) + ".\n")
        write_text(ctx, dst_dir / f"{name}.md",
                   make_frontmatter(out_fm) + body.rstrip() + "\n")
        suffix = f" (dropped: {', '.join(dropped)})" if dropped else ""
        ctx.report.migrated_lossy.append(
            f"agents/{rel} → agents/{name}.md (native Cursor subagent){suffix}")


# ---- B7: claude hooks ↔ cursor hooks.json ----------------------------------
# Cursor has a real hooks system (<cursor_root>/hooks.json, `version: 1`,
# camelCase events, flat entry lists). A subset of events exists on both
# sides; command-type hooks translate, everything else is dropped with a
# note. Claude matchers have no Cursor equivalent in the translated shape,
# and Cursor's prompt-type / shell-interception hooks have no Claude
# equivalent, so both directions are Tier B.

HOOK_EVENTS_CLAUDE_TO_CURSOR = {
    "PreToolUse": "preToolUse",
    "PostToolUse": "postToolUse",
    "SessionStart": "sessionStart",
    "SessionEnd": "sessionEnd",
    "Stop": "stop",
    "PreCompact": "preCompact",
    "SubagentStart": "subagentStart",
    "SubagentStop": "subagentStop",
    "UserPromptSubmit": "beforeSubmitPrompt",
}
HOOK_EVENTS_CURSOR_TO_CLAUDE = {v: k for k, v in
                                HOOK_EVENTS_CLAUDE_TO_CURSOR.items()}


def _detect_claude_hooks_cursor(ctx: Ctx) -> bool:
    s = load_claude_settings(ctx.src_root)
    return bool(s.get("hooks"))


def _preview_claude_hooks_cursor(ctx: Ctx) -> str:
    hooks = load_claude_settings(ctx.src_root).get("hooks") or {}
    mappable = [k for k in hooks if k in HOOK_EVENTS_CLAUDE_TO_CURSOR]
    dropped = [k for k in hooks if k not in HOOK_EVENTS_CLAUDE_TO_CURSOR]
    msg = f"hooks: {', '.join(mappable) or '(none)'} → .cursor/hooks.json"
    if dropped:
        msg += f". DROPPED events: {', '.join(dropped)}"
    return msg


def _apply_claude_hooks_cursor(ctx: Ctx) -> None:
    hooks = load_claude_settings(ctx.src_root).get("hooks") or {}
    dst = ctx.dst_root / "hooks.json"
    existing = load_json(dst) if (ctx.merge and dst.exists()) else {}
    existing.setdefault("version", 1)
    out = existing.setdefault("hooks", {})
    migrated = 0
    for event, groups in hooks.items():
        cursor_event = HOOK_EVENTS_CLAUDE_TO_CURSOR.get(event)
        if not cursor_event:
            ctx.report.skipped_unmappable.append(
                f"hooks.{event} (no Cursor hook event equivalent)")
            continue
        for grp in groups if isinstance(groups, list) else []:
            if not isinstance(grp, dict):
                continue
            if grp.get("matcher"):
                ctx.report.notes.append(
                    f"hooks.{event}: matcher {grp['matcher']!r} dropped "
                    "(Cursor hook matchers work differently — review)")
            for h in grp.get("hooks") or []:
                if h.get("type") == "command" and h.get("command"):
                    entry: dict[str, Any] = {"command": h["command"]}
                    if h.get("timeout"):
                        entry["timeout"] = h["timeout"]
                    out.setdefault(cursor_event, []).append(entry)
                    migrated += 1
                else:
                    ctx.report.notes.append(
                        f"hooks.{event}: non-command hook "
                        f"(type={h.get('type')!r}) not translated")
    if migrated:
        write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")
        ctx.report.migrated_lossy.append(
            f"hooks → .cursor/hooks.json ({migrated} command hook(s); "
            "matchers and non-command hooks dropped)")


def _detect_cursor_hooks(ctx: Ctx) -> bool:
    return bool(load_json(ctx.src_root / "hooks.json").get("hooks"))


def _preview_cursor_hooks(ctx: Ctx) -> str:
    hooks = load_json(ctx.src_root / "hooks.json").get("hooks") or {}
    mappable = [k for k in hooks if k in HOOK_EVENTS_CURSOR_TO_CLAUDE]
    dropped = [k for k in hooks if k not in HOOK_EVENTS_CURSOR_TO_CLAUDE]
    msg = f"hooks.json: {', '.join(mappable) or '(none)'} → settings.json:hooks"
    if dropped:
        msg += f". DROPPED events: {', '.join(dropped)}"
    return msg


def _apply_cursor_hooks(ctx: Ctx) -> None:
    hooks = load_json(ctx.src_root / "hooks.json").get("hooks") or {}
    dst = ctx.dst_root / "settings.json"
    existing = load_json(dst) if (ctx.merge and dst.exists()) else {}
    out = existing.setdefault("hooks", {})
    migrated = 0
    for event, entries in hooks.items():
        claude_event = HOOK_EVENTS_CURSOR_TO_CLAUDE.get(event)
        if not claude_event:
            ctx.report.skipped_unmappable.append(
                f"hooks.json:{event} (no Claude hook event equivalent)")
            continue
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict) or not e.get("command"):
                continue
            if e.get("type") == "prompt":
                ctx.report.notes.append(
                    f"hooks.json:{event}: prompt-type hook not translated")
                continue
            h: dict[str, Any] = {"type": "command", "command": e["command"]}
            if e.get("timeout"):
                h["timeout"] = e["timeout"]
            out.setdefault(claude_event, []).append({"hooks": [h]})
            migrated += 1
    if migrated:
        write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")
        ctx.report.migrated_lossy.append(
            f"hooks.json → settings.json:hooks ({migrated} command hook(s); "
            "prompt-type hooks and Cursor-only events dropped)")


# ---- B8: opencode permissions ↔ claude permissions / codex rules -----------
# opencode's `permission` map and Claude's `permissions` lists carry the
# same intent with different pattern semantics (glob last-match-wins vs
# prefix specificity), and Codex prefix rules bridge through the same
# Claude-shaped intermediate. All four directions are Tier B.

def _detect_claude_permissions_opencode(ctx: Ctx) -> bool:
    return bool(load_claude_settings(ctx.src_root).get("permissions"))


def _preview_claude_permissions_opencode(ctx: Ctx) -> str:
    perms = load_claude_settings(ctx.src_root).get("permissions") or {}
    n = sum(len(perms.get(k) or []) for k in ("allow", "ask", "deny"))
    return (f"{n} permission rule(s) → opencode.json:permission "
            "(glob patterns, last-match-wins)")


def _apply_claude_permissions_opencode(ctx: Ctx) -> None:
    perms = load_claude_settings(ctx.src_root).get("permissions") or {}
    mapped = _claude_perms_to_opencode(perms, ctx.report)
    if not mapped:
        return
    opencode_update_config(ctx, {"permission": mapped})
    ctx.report.migrated_lossy.append(
        "permissions → opencode.json:permission "
        "(prefix rules approximated as globs; matching semantics differ)")


def _detect_opencode_permissions(ctx: Ctx) -> bool:
    return bool(load_opencode_config(ctx.src_root).get("permission"))


def _preview_opencode_permissions(ctx: Ctx) -> str:
    permission = load_opencode_config(ctx.src_root).get("permission") or {}
    keys = (", ".join(sorted(permission))
            if isinstance(permission, dict) else str(permission))
    return f"permission ({keys}) → settings.json:permissions"


def _apply_opencode_permissions(ctx: Ctx) -> None:
    permission = load_opencode_config(ctx.src_root).get("permission")
    if isinstance(permission, str):
        permission = {"bash": permission, "edit": permission}
    if not isinstance(permission, dict):
        return
    mapped = _opencode_perms_to_claude(permission, ctx.report)
    if not mapped:
        return
    dst = ctx.dst_root / "settings.json"
    existing = load_json(dst) if dst.exists() else {}
    perms = existing.setdefault("permissions", {})
    for key, rules in mapped.items():
        merged = list(perms.get(key) or [])
        merged += [r for r in rules if r not in merged]
        perms[key] = merged
    write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")
    ctx.report.migrated_lossy.append(
        "permission → settings.json:permissions "
        "(glob patterns approximated as prefix rules)")


def _preview_codex_rules_opencode(ctx: Ctx) -> str:
    n = 0
    for f in sorted((ctx.src_root / "rules").glob("*.rules")):
        n += len(_parse_prefix_rules(f.read_text(encoding="utf-8")))
    return (f"{n} prefix rule(s) in rules/*.rules → "
            "opencode.json:permission.bash")


def _apply_codex_rules_opencode(ctx: Ctx) -> None:
    bash: dict = {}
    for f in sorted((ctx.src_root / "rules").glob("*.rules")):
        for tokens, decision in _parse_prefix_rules(
                f.read_text(encoding="utf-8")):
            action = {"allow": "allow", "prompt": "ask",
                      "forbidden": "deny"}.get(decision)
            if action is None or tokens is None:
                ctx.report.notes.append(
                    f"rules/{f.name}: a prefix rule couldn't be translated "
                    "(union pattern or unknown decision)")
                continue
            bash[f"{shlex.join(tokens)}*"] = action
    if not bash:
        return
    opencode_update_config(ctx, {"permission": {"bash": bash}})
    ctx.report.migrated_lossy.append(
        f"rules/*.rules → opencode.json:permission.bash "
        f"({len(bash)} pattern(s); prefix semantics approximated as globs)")


def _detect_opencode_bash_rules(ctx: Ctx) -> bool:
    permission = load_opencode_config(ctx.src_root).get("permission") or {}
    return isinstance(permission, dict) and bool(permission.get("bash"))


def _preview_opencode_bash_rules(ctx: Ctx) -> str:
    bash = (load_opencode_config(ctx.src_root).get("permission") or {}).get("bash")
    n = len(bash) if isinstance(bash, dict) else 1
    return f"{n} permission.bash pattern(s) → rules/default.rules"


def _apply_opencode_bash_rules(ctx: Ctx) -> None:
    bash = (load_opencode_config(ctx.src_root).get("permission") or {}).get("bash")
    if isinstance(bash, str):
        bash = {"*": bash}
    if not isinstance(bash, dict):
        return
    decision_map = {"allow": "allow", "ask": "prompt", "deny": "forbidden"}
    dst = ctx.dst_root / "rules" / "default.rules"
    existing = dst.read_text(encoding="utf-8") if dst.exists() else ""
    lines: list[str] = []
    for pattern, action in bash.items():
        decision = decision_map.get(str(action))
        base = str(pattern).rstrip("*").strip()
        if decision is None or not base or "*" in base:
            ctx.report.notes.append(
                f"permission.bash: pattern {pattern!r} not expressible as a "
                "Codex prefix rule")
            continue
        try:
            tokens = shlex.split(base)
        except ValueError:
            ctx.report.notes.append(
                f"permission.bash: pattern {pattern!r} has unbalanced "
                "quoting — skipped")
            continue
        rendered = _render_prefix_rule(tokens, decision)
        if rendered not in existing and rendered not in lines:
            lines.append(rendered)
    if not lines:
        return
    body = existing.rstrip() + "\n" if existing.strip() else ""
    write_text(ctx, dst, body + "\n".join(lines) + "\n")
    ctx.report.migrated_lossy.append(
        f"permission.bash → rules/default.rules ({len(lines)} prefix "
        "rule(s); glob semantics approximated as prefixes)")


def _detect_claude_agents_opencode(ctx: Ctx) -> bool:
    p = ctx.src_root / "agents"
    return p.is_dir() and any(p.rglob("*.md"))


def _preview_claude_agents_opencode(ctx: Ctx) -> str:
    n = sum(1 for _ in (ctx.src_root / "agents").rglob("*.md"))
    return (f"{n} subagent file(s) → opencode agents/*.md (mode: subagent; "
            "tools/hooks/memory fields don't carry)")


def _apply_claude_agents_opencode(ctx: Ctx) -> None:
    _agents_write_opencode(ctx, _agents_read_claude(ctx.src_root), "claude")


# ---- Catalog ---------------------------------------------------------------

TIER_B: list[LossyOption] = [
    LossyOption(
        id="permissions",
        direction="claude->codex",
        label="permissions → sandbox_mode + approval_policy",
        rationale=("Claude's per-tool regex permissions don't map exactly to "
                   "Codex's coarse sandbox modes. We infer the closest match."),
        detect=_detect_claude_permissions,
        preview=_preview_claude_permissions,
        apply=_apply_claude_permissions,
    ),
    LossyOption(
        id="sandbox",
        direction="codex->claude",
        label="sandbox_mode/approval_policy → permissions",
        rationale=("Codex's coarse sandbox mode is expanded into a set of "
                   "Claude allow/deny patterns. Round-trip is not exact."),
        detect=_detect_codex_sandbox,
        preview=_preview_codex_sandbox,
        apply=_apply_codex_sandbox,
    ),
    LossyOption(
        id="rules",
        direction="codex->claude",
        label="rules/*.rules → permissions Bash(...) patterns",
        rationale=("Codex prefix rules map onto Claude Bash() permission "
                   "patterns. Prefix semantics are approximated with :* "
                   "suffixes; union pattern elements can't be translated."),
        detect=_detect_codex_rules,
        preview=_preview_codex_rules,
        apply=_apply_codex_rules,
    ),
    LossyOption(
        id="hooks",
        direction="claude->codex",
        label="hooks → .codex/hooks.json (+ Notification → notify)",
        rationale=("Codex hooks share Claude's event names and shape, so "
                   "command hooks on shared events translate near-verbatim. "
                   "Notification maps to the legacy notify program; "
                   "non-command hook types and Claude-only events drop."),
        detect=_detect_claude_notify_hook,
        preview=_preview_claude_notify_hook,
        apply=_apply_claude_notify_hook,
    ),
    LossyOption(
        id="codex_hooks",
        direction="codex->claude",
        label="hooks.json → settings.json:hooks",
        rationale=("Command hooks on shared events translate near-verbatim; "
                   "commandWindows variants and non-command hook types are "
                   "dropped."),
        detect=_detect_codex_hooks,
        preview=_preview_codex_hooks,
        apply=_apply_codex_hooks,
    ),
    LossyOption(
        id="notify",
        direction="codex->claude",
        label="notify → hooks.Notification",
        rationale=("Codex's single notify program is registered as a Claude "
                   "Notification hook without a matcher."),
        detect=_detect_codex_notify,
        preview=_preview_codex_notify,
        apply=_apply_codex_notify,
    ),
    LossyOption(
        id="agents",
        direction="claude->codex",
        label="agents/ → .codex/agents/*.toml",
        rationale=("Claude subagents map to Codex custom-agent TOML. Some "
                   "Claude-only fields become prompt guidance or review notes."),
        detect=_detect_claude_agents,
        preview=_preview_claude_agents,
        apply=_apply_claude_agents,
    ),
    LossyOption(
        id="profiles",
        direction="codex->claude",
        label="[profiles.*] → ~/.claude/profiles/*.settings.json",
        rationale=("Claude has no profile runtime. Each Codex profile is "
                   "materialized as a standalone settings file you can copy "
                   "over settings.json to activate."),
        detect=_detect_codex_profiles,
        preview=_preview_codex_profiles,
        apply=_apply_codex_profiles,
    ),
    LossyOption(
        id="agents_cursor",
        direction="claude->cursor",
        label="agents/ → .cursor/agents/*.md (native Cursor subagents)",
        rationale=("Cursor 2.4+ runs subagents natively. name/description/"
                   "model/effort/background translate; Claude-only fields "
                   "(tools, hooks, memory, skills, …) are dropped with notes."),
        detect=_detect_claude_agents_cursor,
        preview=_preview_claude_agents_cursor,
        apply=_apply_claude_agents_cursor,
    ),
    LossyOption(
        id="hooks_cursor",
        direction="claude->cursor",
        label="hooks → .cursor/hooks.json",
        rationale=("A subset of hook events exists on both sides; command "
                   "hooks translate, matchers and Claude-only events are "
                   "dropped."),
        detect=_detect_claude_hooks_cursor,
        preview=_preview_claude_hooks_cursor,
        apply=_apply_claude_hooks_cursor,
    ),
    LossyOption(
        id="cursor_hooks",
        direction="cursor->claude",
        label="hooks.json → settings.json:hooks",
        rationale=("Command hooks on shared events translate; Cursor "
                   "prompt-type hooks and Cursor-only events (shell/MCP "
                   "interception, tab hooks) are dropped."),
        detect=_detect_cursor_hooks,
        preview=_preview_cursor_hooks,
        apply=_apply_cursor_hooks,
    ),
    LossyOption(
        id="permissions_opencode",
        direction="claude->opencode",
        label="permissions → opencode.json:permission",
        rationale=("Claude prefix rules become opencode glob patterns. "
                   "The intent carries, but opencode is last-match-wins "
                   "where Claude is most-specific-wins — review the result."),
        detect=_detect_claude_permissions_opencode,
        preview=_preview_claude_permissions_opencode,
        apply=_apply_claude_permissions_opencode,
    ),
    LossyOption(
        id="opencode_permissions",
        direction="opencode->claude",
        label="permission → settings.json:permissions",
        rationale=("opencode glob patterns become Claude prefix rules; "
                   "mid-pattern wildcards and per-path edit maps can't be "
                   "translated."),
        detect=_detect_opencode_permissions,
        preview=_preview_opencode_permissions,
        apply=_apply_opencode_permissions,
    ),
    LossyOption(
        id="rules_opencode",
        direction="codex->opencode",
        label="rules/*.rules → opencode.json:permission.bash",
        rationale=("Codex prefix rules become opencode glob patterns "
                   "(allow/prompt→ask/forbidden→deny). Union pattern "
                   "elements can't be translated."),
        detect=_detect_codex_rules,
        preview=_preview_codex_rules_opencode,
        apply=_apply_codex_rules_opencode,
    ),
    LossyOption(
        id="opencode_rules",
        direction="opencode->codex",
        label="permission.bash → rules/default.rules",
        rationale=("opencode bash glob patterns become Codex prefix rules; "
                   "mid-pattern wildcards can't be expressed as prefixes."),
        detect=_detect_opencode_bash_rules,
        preview=_preview_opencode_bash_rules,
        apply=_apply_opencode_bash_rules,
    ),
    LossyOption(
        id="agents_opencode",
        direction="claude->opencode",
        label="agents/ → opencode agents/*.md (mode: subagent)",
        rationale=("opencode runs subagents natively. name/description/"
                   "model translate (provider-qualified); readOnly/plan "
                   "permission modes become permission denies; Claude-only "
                   "fields (tools, hooks, memory, skills, …) are dropped "
                   "with notes."),
        detect=_detect_claude_agents_opencode,
        preview=_preview_claude_agents_opencode,
        apply=_apply_claude_agents_opencode,
    ),
]


# ============================================================================
# Pre-flight interactive scan
# ============================================================================

def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask_yn(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        ans = input(f"{prompt} {suffix}: ").strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans.startswith("y")


def preflight(ctx: Ctx, direction_key: str,
              apply_set: set[str] | None,
              skip_set: set[str] | None,
              interactive: bool) -> dict[str, bool]:
    """Phase 1 of migration: scan source, preview Tier A, and decide each
    Tier B item. Returns {option_id: should_apply} for the apply pass.

    Decision precedence: --apply-lossy/--skip-lossy override all; else if
    running interactively, ask per item; else accept everything (the
    non-interactive default is to migrate as much as we can).
    """
    candidates = [o for o in TIER_B if o.direction == direction_key and o.detect(ctx)]
    decisions: dict[str, bool] = {}

    print()
    print("=" * 72)
    print(f"Pre-migration scan — {ctx.report.direction}")
    print("=" * 72)
    print(f"Source: {ctx.src_root}")
    print(f"Destination: {ctx.dst_root}")
    print()

    # Tier A summary (just announce what will run).
    print("Tier A — clean translations (always applied):")
    a_items = []
    if (ctx.src_root / "CLAUDE.md").exists() or (ctx.src_root / "AGENTS.md").exists() \
            or (ctx.src_doc and ctx.src_doc.exists()):
        a_items.append("instruction doc (CLAUDE.md ↔ AGENTS.md)")
    if (ctx.src_root / "commands").is_dir() or (ctx.src_root / "prompts").is_dir() \
            or (ctx.src_root / "command").is_dir():
        a_items.append("slash commands ↔ prompts")
    if _skill_dirs(ctx.src_root) or _skill_dirs(ctx.src_root, "skill"):
        a_items.append("skills (shared Agent Skills format)")
    if (ctx.src_root / "settings.json").exists() \
            or (ctx.src_root / "config.toml").exists() \
            or any(p.exists() for p in opencode_config_paths(ctx.src_root)):
        a_items.append("model + mcpServers + env + reasoning effort")
    for x in a_items:
        print(f"  ✓ {x}")
    if not a_items:
        print("  (nothing detected)")
    print()

    if not candidates:
        print("Tier B — lossy translations: none detected.")
        print()
    else:
        print("Tier B — lossy translations (please confirm):")
        for i, opt in enumerate(candidates, 1):
            forced_apply = apply_set and (opt.id in apply_set or "all" in apply_set)
            forced_skip = skip_set and (opt.id in skip_set or "all" in skip_set)
            print(f"  [{i}] {opt.label}")
            print(f"      why lossy: {opt.rationale}")
            print(f"      preview:   {opt.preview(ctx)}")
            if forced_apply:
                decisions[opt.id] = True
                print("      → APPLY (forced via --apply-lossy)")
            elif forced_skip:
                decisions[opt.id] = False
                print("      → SKIP (forced via --skip-lossy)")
            elif interactive:
                decisions[opt.id] = ask_yn("      Apply this translation?", True)
            else:
                decisions[opt.id] = True
                print("      → APPLY (non-interactive default)")
            print()

    # Tier C preview (best-effort; full details land in the report after run).
    print("Tier C — items with no equivalent will be listed in MIGRATION_REPORT.md.")
    print()

    if interactive and not ask_yn("Proceed with migration?", True):
        print("Aborted by user.")
        sys.exit(0)

    return decisions


# ============================================================================
# Drivers
# ============================================================================

def run_claude_to_codex(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    tier_a_docs_claude_to_codex(ctx)
    tier_a_commands_to_prompts(ctx)
    tier_a_skills_copy(ctx)
    tier_a_settings_claude_to_codex(ctx)
    _run_lossy(ctx, lossy_decisions, "claude->codex")

    for sub in ("plugins",):
        p = ctx.src_root / sub
        if p.is_dir() and any(p.iterdir()):
            ctx.report.skipped_unmappable.append(
                f"{sub}/ (Codex plugins use a different format — "
                "reinstall via `codex plugin`)")


def run_codex_to_claude(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    tier_a_docs_codex_to_claude(ctx)
    tier_a_prompts_to_commands(ctx)
    tier_a_skills_copy(ctx)
    tier_a_codex_agents_to_claude(ctx)
    tier_a_settings_codex_to_claude(ctx)
    _run_lossy(ctx, lossy_decisions, "codex->claude")


# Dispatch table for (from_tool, to_tool) pairs. Every directed pair of
# TOOLS is populated; same-tool pairs are rejected at the CLI before
# reaching this dict.
RUNNERS: dict[tuple[str, str], Callable[[Ctx, dict[str, bool]], None]] = {
    ("claude", "codex"):  run_claude_to_codex,
    ("codex",  "claude"): run_codex_to_claude,
    ("claude", "cursor"): run_claude_to_cursor,
    ("cursor", "claude"): run_cursor_to_claude,
    ("codex",  "cursor"): run_codex_to_cursor,
    ("cursor", "codex"):  run_cursor_to_codex,
    ("claude",   "opencode"): run_claude_to_opencode,
    ("opencode", "claude"):   run_opencode_to_claude,
    ("codex",    "opencode"): run_codex_to_opencode,
    ("opencode", "codex"):    run_opencode_to_codex,
    ("cursor",   "opencode"): run_cursor_to_opencode,
    ("opencode", "cursor"):   run_opencode_to_cursor,
    ("claude",   "pi"): run_claude_to_pi,
    ("pi", "claude"):   run_pi_to_claude,
    ("codex",    "pi"): run_codex_to_pi,
    ("pi", "codex"):    run_pi_to_codex,
    ("cursor",   "pi"): run_cursor_to_pi,
    ("pi", "cursor"):   run_pi_to_cursor,
    ("opencode", "pi"): run_opencode_to_pi,
    ("pi", "opencode"): run_pi_to_opencode,
}


# ============================================================================
# Restore
# ============================================================================

def find_latest_backup() -> Path | None:
    """Return the most recently created migration backup across every
    known tool's backups dir (user + project scope). Ranks by the
    `created_at` field in the manifest, with mtime as a tiebreak, so the
    correct "last migration" wins even when filesystems have coarse mtime
    resolution or when backups have been copied around.
    """
    bases: list[Path] = []
    for tool in TOOLS:
        bases.append(_tool_user_root(tool) / "backups")
        bases.append(Path.cwd() / f".{tool}" / "backups")

    candidates: list[tuple[str, float, Path]] = []
    for b in bases:
        if not b.is_dir():
            continue
        for p in b.glob("pre-migrate-*"):
            manifest = p / "manifest.json"
            if not manifest.exists():
                continue
            created_at = ""
            try:
                created_at = json.loads(
                    manifest.read_text(encoding="utf-8")
                ).get("created_at", "")
            except json.JSONDecodeError:
                pass
            candidates.append((created_at, p.stat().st_mtime, p))
    if not candidates:
        return None
    return max(candidates)[2]


def restore_from_backup(backup_path: Path, interactive: bool,
                        dry_run: bool) -> int:
    manifest_path = backup_path / "manifest.json"
    if not manifest_path.exists():
        print(f"No manifest.json at {backup_path}", file=sys.stderr)
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Could not parse manifest: {e}", file=sys.stderr)
        return 1

    entries = manifest.get("entries", [])
    to_restore = [e for e in entries if e.get("existed_before")]
    to_delete = [e for e in entries if not e.get("existed_before")]

    print("=" * 72)
    print(f"Restore plan — backup at {backup_path}")
    print("=" * 72)
    print(f"Original migration: {manifest.get('direction', '?')}")
    print(f"Backup created:     {manifest.get('created_at', '?')}")
    print()
    print(f"Will restore {len(to_restore)} file(s) (overwriting current state):")
    for e in to_restore:
        print(f"  ← {e['original']}")
    print()
    print(f"Will delete {len(to_delete)} file(s) (created by the migration):")
    for e in to_delete:
        print(f"  ✗ {e['original']}")
    print()

    if interactive and not ask_yn("Proceed with restore?", True):
        print("Aborted.")
        return 0

    restored, deleted, missing = [], [], []
    for e in to_restore:
        backup_file = backup_path / e["relative"]
        original = Path(e["original"])
        if not backup_file.exists():
            missing.append(e["original"])
            continue
        if dry_run:
            print(f"[dry-run] restore {backup_file} → {original}")
        else:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_file, original)
        restored.append(e["original"])

    for e in to_delete:
        original = Path(e["original"])
        if not original.exists():
            continue
        if dry_run:
            print(f"[dry-run] delete {original}")
        else:
            original.unlink()
        deleted.append(e["original"])

    print()
    print(f"Restored: {len(restored)}")
    print(f"Deleted:  {len(deleted)}")
    if missing:
        print(f"WARNING — backup files missing for: {missing}", file=sys.stderr)
        return 2
    return 0


TOOLS = ("claude", "codex", "cursor", "opencode", "pi")


def _tool_user_root(tool: str) -> Path:
    """User-scope config root. opencode is the XDG outlier
    (~/.config/opencode) and pi nests under ~/.pi/agent; everything else
    is a home dot-dir."""
    home = Path.home()
    if tool == "opencode":
        return home / ".config" / "opencode"
    if tool == "pi":
        return home / ".pi" / "agent"
    return home / f".{tool}"


def _tool_paths(tool: str, scope: str, override_dir: str | None) -> dict:
    """Return {'root': Path, 'doc': Path | None} for a tool at a given scope.

    `root`  — the tool's configuration dir for this scope.
    `doc`   — the conventional project instruction file alongside (e.g.
              ./CLAUDE.md or ./AGENTS.md). None when not applicable
              (user scope, or tools without a sibling doc).
    """
    if override_dir:
        return {"root": Path(override_dir).expanduser(), "doc": None}
    cwd = Path.cwd()
    if scope == "user":
        return {"root": _tool_user_root(tool), "doc": None}
    project_docs = {
        "claude": cwd / "CLAUDE.md",
        "codex": cwd / "AGENTS.md",
        # Cursor reads project AGENTS.md natively now, but the legacy
        # .cursorrules still marks the project root for rule reading.
        "cursor": cwd / ".cursorrules",
        "opencode": cwd / "AGENTS.md",
        "pi": cwd / "AGENTS.md",
    }
    if tool not in project_docs:
        raise ValueError(f"unknown tool: {tool}")
    return {"root": cwd / f".{tool}", "doc": project_docs[tool]}


def _csv_set(s: str | None) -> set[str] | None:
    if not s:
        return None
    return {x.strip() for x in s.split(",") if x.strip()}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--from", dest="from_tool", choices=TOOLS,
                    help="Source tool to migrate from.")
    ap.add_argument("--to", dest="to_tool", choices=TOOLS,
                    help="Destination tool to migrate to.")
    ap.add_argument("--restore", nargs="?", const="__latest__",
                    metavar="BACKUP_DIR",
                    help="Restore from a previous migration backup. Pass a "
                    "specific backup directory, or omit to use the latest "
                    "one under any tool's backups dir.")
    ap.add_argument("--scope", choices=["user", "project", "both"],
                    default="user")
    ap.add_argument("--claude-dir")
    ap.add_argument("--codex-dir")
    ap.add_argument("--cursor-dir")
    ap.add_argument("--opencode-dir")
    ap.add_argument("--pi-dir")
    ap.add_argument("--dry-run", action="store_true")
    mg = ap.add_mutually_exclusive_group()
    mg.add_argument("--merge", dest="merge", action="store_true", default=True)
    mg.add_argument("--overwrite", dest="merge", action="store_false")
    ap.add_argument("--no-backup", dest="backup", action="store_false", default=True)
    ap.add_argument("--no-interactive", action="store_true")
    ap.add_argument("--apply-lossy", metavar="IDS",
                    help="Comma-separated Tier B IDs to apply (or 'all'). "
                    "IDs: " + ", ".join(o.id for o in TIER_B))
    ap.add_argument("--skip-lossy", metavar="IDS",
                    help="Comma-separated Tier B IDs to skip (or 'all').")
    args = ap.parse_args()

    have_migrate = args.from_tool and args.to_tool
    have_restore = args.restore is not None
    if not have_migrate and not have_restore:
        ap.error("either --from + --to or --restore is required")
    if have_migrate and have_restore:
        ap.error("--from/--to and --restore are mutually exclusive")
    if have_migrate and args.from_tool == args.to_tool:
        ap.error("--from and --to must differ")

    interactive_default = not args.no_interactive and is_interactive()

    # ---- Restore mode ------------------------------------------------------
    if args.restore is not None:
        if args.restore == "__latest__":
            backup_path = find_latest_backup()
            if not backup_path:
                print("No backups found under any tool's backups dir "
                      "(~/.claude, ~/.codex, ~/.cursor, ~/.config/opencode, "
                      "or ./.<tool>).", file=sys.stderr)
                return 1
            print(f"Using latest backup: {backup_path}")
        else:
            backup_path = Path(args.restore).expanduser()
            if not backup_path.is_dir():
                print(f"Not a directory: {backup_path}", file=sys.stderr)
                return 1
        return restore_from_backup(backup_path, interactive_default, args.dry_run)

    # ---- Migrate mode ------------------------------------------------------
    apply_set = _csv_set(args.apply_lossy)
    skip_set = _csv_set(args.skip_lossy)
    valid_ids = {o.id for o in TIER_B} | {"all"}
    for flag, ids in (("--apply-lossy", apply_set), ("--skip-lossy", skip_set)):
        for unknown in sorted((ids or set()) - valid_ids):
            print(f"warn: {flag}: unknown Tier B id {unknown!r} — ignored. "
                  f"Valid ids: {', '.join(sorted(valid_ids))}", file=sys.stderr)
    interactive = interactive_default and not (apply_set or skip_set)

    from_tool, to_tool = args.from_tool, args.to_tool
    direction_key = f"{from_tool}->{to_tool}"

    runner = RUNNERS.get((from_tool, to_tool))
    if runner is None:
        ap.error(f"unsupported direction: {direction_key}")

    overrides = {
        "claude": args.claude_dir,
        "codex": args.codex_dir,
        "cursor": args.cursor_dir,
        "opencode": args.opencode_dir,
        "pi": args.pi_dir,
    }
    scopes = ["user", "project"] if args.scope == "both" else [args.scope]

    for scope in scopes:
        src = _tool_paths(from_tool, scope, overrides[from_tool])
        dst = _tool_paths(to_tool, scope, overrides[to_tool])
        src_root, dst_root = src["root"], dst["root"]
        src_doc, dst_doc = src["doc"], dst["doc"]

        pretty = {"claude": "Claude Code", "codex": "Codex CLI",
                  "cursor": "Cursor", "opencode": "opencode", "pi": "pi"}
        label = (f"{pretty[from_tool]} → {pretty[to_tool]} "
                 f"({src_root} → {dst_root})")

        if not src_root.exists() and not (src_doc and src_doc.exists()):
            print(f"\n--- {label}\n(no source files — skipped)")
            continue

        report = Report(direction=label)
        ctx = Ctx(src_root=src_root, dst_root=dst_root,
                  src_doc=src_doc, dst_doc=dst_doc,
                  dry_run=args.dry_run, merge=args.merge,
                  backup=args.backup, report=report)

        # Step 1: interactive preflight (decides which Tier B items run).
        decisions = preflight(ctx, direction_key, apply_set, skip_set, interactive)

        # Step 2: PLAN pass — re-run the migration with plan_mode=True so
        # write_text/copy_file just record destination paths. This lets us
        # back up the full set of files atomically (and show the user the
        # complete list of changes) before any writes happen.
        ctx.plan_mode = True
        runner(ctx, decisions)
        ctx.planned_writes.add(dst_root / "MIGRATION_REPORT.md")
        planned = sorted(ctx.planned_writes)

        print()
        print("Planned destination files ({}):".format(len(planned)))
        for p in planned:
            tag = "modify" if p.exists() else "create"
            print(f"  [{tag}] {p}")
        print()

        # Step 3: BACKUP everything that already exists + write manifest.
        if args.backup and planned and not args.dry_run:
            backup_root = perform_backup(ctx)
            if backup_root:
                print(f"Backup written to: {backup_root}")
                print(f"  manifest: {backup_root / 'manifest.json'}")
                print(f"  to restore later: "
                      f"python3 migrate.py --restore {backup_root}")
                print()
        elif args.backup and args.dry_run:
            perform_backup(ctx)  # populates report.backups with "would back up" notes

        if interactive and not ask_yn("Apply migration?", True):
            print("Aborted by user (no changes written; backup retained).")
            continue

        # The plan pass populated the report (Tier A/B functions don't
        # know they're being dry-run). Clear those entries so the apply
        # pass produces a clean report. `backups` is preserved.
        ctx.report.migrated_clean.clear()
        ctx.report.migrated_lossy.clear()
        ctx.report.skipped_by_user.clear()
        ctx.report.skipped_unmappable.clear()
        ctx.report.notes.clear()

        # Step 4: APPLY pass — really write.
        ctx.plan_mode = False
        ctx.planned_writes.clear()
        print(f"--- Running: {label}")
        runner(ctx, decisions)

        # Step 5: write report.
        if not args.dry_run:
            dst_root.mkdir(parents=True, exist_ok=True)
            (dst_root / "MIGRATION_REPORT.md").write_text(
                report.render(ctx.backup_root if args.backup else None),
                encoding="utf-8")
        print()
        print(report.render(ctx.backup_root if args.backup else None))

    return 0


if __name__ == "__main__":
    sys.exit(main())
