"""
Tests for migrate.py.

Run with:
    python3 -m unittest discover -s tests
    # or
    python3 tests/test_migrate.py

Uses only the standard library (matches the migrator's stdlib-only stance).
Works on Python 3.9+ (uses migrate.py's TOML reader fallback when stdlib
`tomllib` isn't available).
"""

from __future__ import annotations

import json
import io
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

# Make migrate.py importable when run from anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import migrate as m  # noqa: E402

# Use whichever TOML reader migrate.py chose (stdlib on 3.11+,
# hand-rolled fallback on 3.9/3.10). Both expose `.loads(text)`.
tomllib = m.tomllib


def make_ctx(src: Path, dst: Path, **overrides) -> m.Ctx:
    """Build a Ctx for tests. plan_mode=False, dry_run=False, backup=True by default."""
    defaults = dict(
        src_root=src,
        dst_root=dst,
        src_doc=None,
        dst_doc=None,
        dry_run=False,
        merge=True,
        backup=True,
        report=m.Report(direction="test"),
    )
    defaults.update(overrides)
    return m.Ctx(**defaults)


# ============================================================================
# Pure-function tests (no filesystem)
# ============================================================================

class TomlWriterTests(unittest.TestCase):
    """The migrator ships its own TOML writer (stdlib is read-only).
    Tests verify it produces output tomllib can parse back to the same dict."""

    def test_roundtrip_scalars(self):
        data = {"model": "gpt-5", "approval_policy": "never",
                "max_turns": 5, "verbose": True}
        self.assertEqual(tomllib.loads(m.render_toml(data)), data)

    def test_roundtrip_nested_tables(self):
        data = {
            "model": "gpt-5",
            "mcp_servers": {
                "foo": {"command": "fooserver", "args": ["--x"],
                        "env": {"K": "v"}},
            },
            "shell_environment_policy": {"set": {"A": "1", "B": "2"}},
        }
        self.assertEqual(tomllib.loads(m.render_toml(data)), data)

    def test_escapes_quotes_and_backslashes(self):
        data = {"path": 'C:\\Users\\me', "msg": 'said "hi"'}
        self.assertEqual(tomllib.loads(m.render_toml(data)), data)

    def test_array_of_strings(self):
        data = {"notify": ["/bin/echo", "ding", "dong"]}
        self.assertEqual(tomllib.loads(m.render_toml(data)), data)


class FrontmatterTests(unittest.TestCase):
    """YAML-like frontmatter parsing + the round-trip-able meta-comment encoding."""

    def test_strip_returns_body_and_dict(self):
        body, fm = m.strip_frontmatter("---\nfoo: bar\nbaz: 1\n---\nbody\n")
        self.assertEqual(body, "body\n")
        self.assertEqual(fm, {"foo": "bar", "baz": "1"})

    def test_strip_missing_returns_none_dict(self):
        body, fm = m.strip_frontmatter("no frontmatter here\n")
        self.assertIsNone(fm)
        self.assertEqual(body, "no frontmatter here\n")

    def test_meta_comment_roundtrip(self):
        fm = {"description": 'do a "thing" \\ safely', "argument-hint": "<arg>"}
        text = m.frontmatter_to_meta_comment(fm) + "rest\n"
        rest, parsed = m.meta_comment_to_frontmatter(text)
        self.assertEqual(rest, "rest\n")
        self.assertEqual(parsed, fm)

    def test_meta_comment_only_carries_supported_keys(self):
        # description + argument-hint round-trip; model + allowed-tools don't.
        fm = {"description": "d", "model": "opus", "allowed-tools": "Bash"}
        comment = m.frontmatter_to_meta_comment(fm)
        _, parsed = m.meta_comment_to_frontmatter(comment)
        self.assertEqual(parsed, {"description": "d"})
        self.assertNotIn("model", comment)
        self.assertNotIn("allowed-tools", comment)


class FencedBlockTests(unittest.TestCase):
    """Migrator uses <!-- migrator:begin/end --> fenced blocks to round-trip
    content like Codex `instructions` or Claude `outputStyle` files."""

    def test_encode_then_extract(self):
        text = "preamble\n" + m.fenced_block("outputStyle", "concise", "Be terse.")
        cleaned, body = m.extract_fenced(text, "outputStyle")
        self.assertEqual(body, "Be terse.")
        self.assertNotIn("migrator:begin", cleaned)

    def test_extract_missing_returns_none(self):
        cleaned, body = m.extract_fenced("nothing here", "outputStyle")
        self.assertIsNone(body)
        self.assertEqual(cleaned, "nothing here")

    def test_only_first_match_of_kind_returned(self):
        text = (m.fenced_block("k", "a", "first") +
                m.fenced_block("k", "b", "second"))
        _, body = m.extract_fenced(text, "k")
        self.assertEqual(body, "first")


class EffortMappingTests(unittest.TestCase):
    def test_xhigh_round_trips_one_to_one(self):
        # Both tools natively support `xhigh` (Claude Code 2.1+; Codex).
        self.assertEqual(m.EFFORT_C2X["xhigh"], "xhigh")
        self.assertEqual(m.EFFORT_X2C["xhigh"], "xhigh")
        self.assertEqual(m.EFFORT_C2X["low"], "low")

    def test_claude_legacy_max_maps_to_xhigh(self):
        # `max` is the pre-2.1 Claude name for today's `xhigh`.
        self.assertEqual(m.EFFORT_C2X["max"], "xhigh")
        self.assertEqual(m._codex_effort_to_claude("xhigh"), "xhigh")

    def test_codex_minimal_maps_to_low(self):
        # Codex `minimal` has no Claude equivalent; it collapses to `low`.
        self.assertEqual(m.EFFORT_X2C["minimal"], "low")
        self.assertEqual(m.EFFORT_X2C["high"], "high")
        self.assertEqual(m._codex_effort_to_claude("minimal"), "low")


class McpNormalizeTests(unittest.TestCase):
    def test_stdio_server_passes(self):
        report = m.Report("test")
        out = m._normalize_mcp_claude_to_codex(
            "foo", {"command": "x", "args": ["a"], "env": {"K": "v"}}, report)
        self.assertEqual(out, {"command": "x", "args": ["a"], "env": {"K": "v"}})

    def test_sse_server_skipped_with_report_note(self):
        report = m.Report("test")
        out = m._normalize_mcp_claude_to_codex(
            "foo", {"type": "sse", "url": "https://x"}, report)
        self.assertIsNone(out)
        self.assertTrue(any("sse" in s for s in report.skipped_unmappable))

    def test_command_missing_skipped(self):
        report = m.Report("test")
        out = m._normalize_mcp_claude_to_codex("foo", {}, report)
        self.assertIsNone(out)

    def test_codex_to_claude_injects_stdio_type(self):
        out = m._normalize_mcp_codex_to_claude({"command": "x", "args": ["a"]})
        self.assertEqual(out["type"], "stdio")
        self.assertEqual(out["command"], "x")


# ============================================================================
# Filesystem tests (real tmpdirs, no subprocesses)
# ============================================================================

class FsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="migrate-test-"))
        self.src = self.tmp / "src"
        self.dst = self.tmp / "dst"
        self.src.mkdir()
        self.dst.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TierASettingsClaudeToCodexTests(FsTestBase):
    def test_all_clean_fields_translate(self):
        (self.src / "settings.json").write_text(json.dumps({
            "model": "claude-opus",
            "mcpServers": {"foo": {"command": "fooserver", "args": ["--x"]}},
            "env": {"API_KEY": "secret"},
            "effortLevel": "max",  # legacy alias for xhigh
        }))
        ctx = make_ctx(self.src, self.dst)
        m.tier_a_settings_claude_to_codex(ctx)

        cfg = tomllib.loads((self.dst / "config.toml").read_text())
        self.assertEqual(cfg["model"], "claude-opus")
        self.assertEqual(cfg["mcp_servers"]["foo"]["command"], "fooserver")
        self.assertEqual(cfg["shell_environment_policy"]["set"]["API_KEY"], "secret")
        self.assertEqual(cfg["model_reasoning_effort"], "xhigh")

    def test_xhigh_effort_passes_through(self):
        # Claude Code 2.1+ and Codex share `xhigh`, so it maps 1:1.
        (self.src / "settings.json").write_text(json.dumps({"effortLevel": "xhigh"}))
        ctx = make_ctx(self.src, self.dst)
        m.tier_a_settings_claude_to_codex(ctx)
        cfg = tomllib.loads((self.dst / "config.toml").read_text())
        self.assertEqual(cfg["model_reasoning_effort"], "xhigh")

    def test_unmappable_keys_are_reported(self):
        (self.src / "settings.json").write_text(json.dumps({
            "statusLine": {"type": "command", "command": "echo hi"},
            "theme": "dark",
        }))
        ctx = make_ctx(self.src, self.dst)
        m.tier_a_settings_claude_to_codex(ctx)
        self.assertTrue(any("statusLine" in s for s in ctx.report.skipped_unmappable))
        self.assertTrue(any("theme" in s for s in ctx.report.skipped_unmappable))


class TierASettingsCodexToClaudeTests(FsTestBase):
    def test_all_clean_fields_translate(self):
        (self.src / "config.toml").write_text(
            'model = "gpt-5"\n'
            'model_reasoning_effort = "minimal"\n'
            '[mcp_servers.foo]\n'
            'command = "fooserver"\n'
            'args = ["--x"]\n'
            '[shell_environment_policy]\n'
            'set = { API_KEY = "secret" }\n'
        )
        ctx = make_ctx(self.src, self.dst)
        m.tier_a_settings_codex_to_claude(ctx)

        s = json.loads((self.dst / "settings.json").read_text())
        mcp = json.loads((self.dst / "mcp.json").read_text())
        self.assertEqual(s["model"], "gpt-5")
        self.assertEqual(mcp["mcpServers"]["foo"]["command"], "fooserver")
        self.assertEqual(mcp["mcpServers"]["foo"]["type"], "stdio")
        self.assertEqual(s["env"]["API_KEY"], "secret")
        self.assertEqual(s["effortLevel"], "low")

    def test_xhigh_effort_passes_through(self):
        # `xhigh` is a native Claude effortLevel (2.1+), so it survives the trip.
        (self.src / "config.toml").write_text(
            'model = "gpt-5"\nmodel_reasoning_effort = "xhigh"\n')
        ctx = make_ctx(self.src, self.dst)
        m.tier_a_settings_codex_to_claude(ctx)
        s = json.loads((self.dst / "settings.json").read_text())
        self.assertEqual(s["effortLevel"], "xhigh")

    def test_project_scope_mcp_writes_project_root_mcp_json(self):
        project = self.tmp / "project"
        codex = project / ".codex"
        claude = project / ".claude"
        codex.mkdir(parents=True)
        claude.mkdir()
        (codex / "config.toml").write_text(
            '[mcp_servers.foo]\ncommand = "fooserver"\n')

        ctx = make_ctx(
            codex, claude,
            src_doc=project / "AGENTS.md",
            dst_doc=project / "CLAUDE.md",
        )
        m.tier_a_settings_codex_to_claude(ctx)

        self.assertFalse((claude / "settings.json").exists())
        data = json.loads((project / ".mcp.json").read_text())
        self.assertEqual(data["mcpServers"]["foo"]["command"], "fooserver")

    def test_project_agents_md_import_is_used(self):
        project = self.tmp / "project"
        codex = project / ".codex"
        claude = project / ".claude"
        codex.mkdir(parents=True)
        claude.mkdir()
        (project / "AGENTS.md").write_text("# Existing Codex guide\n")

        ctx = make_ctx(
            codex, claude,
            src_doc=project / "AGENTS.md",
            dst_doc=project / "CLAUDE.md",
        )
        m.tier_a_docs_codex_to_claude(ctx)

        self.assertEqual((project / "CLAUDE.md").read_text(), "@AGENTS.md\n")


class CommandsRoundTripTests(FsTestBase):
    """Verify round-trip preservation of supported frontmatter keys."""

    def test_description_and_argument_hint_round_trip(self):
        cmds = self.src / "commands"
        cmds.mkdir()
        (cmds / "x.md").write_text(
            "---\ndescription: do things\nargument-hint: <x>\n---\nbody text\n")

        ctx_c2x = make_ctx(self.src, self.dst)
        m.tier_a_commands_to_prompts(ctx_c2x)

        # Now go back: dst (with prompts) → dst2 (which gets commands).
        dst2 = self.tmp / "dst2"
        dst2.mkdir()
        ctx_x2c = make_ctx(self.dst, dst2)
        m.tier_a_prompts_to_commands(ctx_x2c)

        result = (dst2 / "commands" / "x.md").read_text()
        body, fm = m.strip_frontmatter(result)
        self.assertEqual(fm, {"description": "do things", "argument-hint": "<x>"})
        self.assertEqual(body.strip(), "body text")

    def test_unsupported_frontmatter_keys_noted(self):
        cmds = self.src / "commands"
        cmds.mkdir()
        (cmds / "foo.md").write_text(
            "---\ndescription: D\nmodel: opus\nallowed-tools: Bash\n---\nbody\n")

        ctx = make_ctx(self.src, self.dst)
        m.tier_a_commands_to_prompts(ctx)

        # description becomes native Codex prompt frontmatter;
        # model + allowed-tools have no prompt equivalent.
        out = (self.dst / "prompts" / "foo.md").read_text()
        _, parsed = m.strip_frontmatter(out)
        self.assertEqual(parsed, {"description": "D"})
        self.assertNotIn("allowed-tools", out)
        self.assertTrue(any("model" in n and "allowed-tools" in n
                            for n in ctx.report.notes))

    def test_legacy_meta_comment_prompts_still_convert(self):
        # Prompts written by older migrator versions carry frontmatter in a
        # migrator:meta comment instead of native frontmatter.
        prompts = self.src / "prompts"
        prompts.mkdir()
        (prompts / "old.md").write_text(
            '<!-- migrator:meta json={"description": "legacy"} -->\nbody\n')
        ctx = make_ctx(self.src, self.dst)
        m.tier_a_prompts_to_commands(ctx)

        body, fm = m.strip_frontmatter(
            (self.dst / "commands" / "old.md").read_text())
        self.assertEqual(fm, {"description": "legacy"})
        self.assertEqual(body.strip(), "body")


class TierBPermissionsToSandboxTests(FsTestBase):
    """Verify the heuristic that maps Claude's per-tool patterns to Codex's
    coarse sandbox modes. Round-trip is not exact (lossy by design)."""

    def _run(self, permissions):
        (self.src / "settings.json").write_text(
            json.dumps({"permissions": permissions}))
        ctx = make_ctx(self.src, self.dst)
        m._apply_claude_permissions(ctx)
        return tomllib.loads((self.dst / "config.toml").read_text())

    def test_bash_star_with_no_deny_is_full_access(self):
        cfg = self._run({"allow": ["Bash(*)"]})
        self.assertEqual(cfg["approval_policy"], "never")
        self.assertEqual(cfg["sandbox_mode"], "danger-full-access")

    def test_reads_only_is_read_only_sandbox(self):
        cfg = self._run({"allow": ["Read(*)"]})
        self.assertEqual(cfg["sandbox_mode"], "read-only")

    def test_write_patterns_become_writable_roots(self):
        cfg = self._run({"allow": ["Read(*)", "Write(./src/**)",
                                   "Write(./tests/**)"]})
        roots = cfg["sandbox_workspace_write"]["writable_roots"]
        self.assertIn("./src", roots)
        self.assertIn("./tests", roots)

    def test_webfetch_deny_disables_network(self):
        cfg = self._run({"allow": ["Read(*)", "Write(./src/**)"],
                         "deny": ["WebFetch(*)"]})
        self.assertFalse(cfg["sandbox_workspace_write"]["network_access"])


class TierBSandboxToPermissionsTests(FsTestBase):
    def _run(self, toml_body):
        (self.src / "config.toml").write_text(toml_body)
        ctx = make_ctx(self.src, self.dst)
        m._apply_codex_sandbox(ctx)
        return json.loads((self.dst / "settings.json").read_text())

    def test_danger_full_expands_to_wildcards(self):
        s = self._run('sandbox_mode = "danger-full-access"\n')
        self.assertIn("Bash(*)", s["permissions"]["allow"])

    def test_read_only_denies_writes_and_bash(self):
        s = self._run('sandbox_mode = "read-only"\n')
        self.assertIn("Read(*)", s["permissions"]["allow"])
        self.assertIn("Write(*)", s["permissions"]["deny"])
        self.assertIn("Bash(*)", s["permissions"]["deny"])

    def test_workspace_write_with_roots_and_no_network(self):
        s = self._run(
            'sandbox_mode = "workspace-write"\n'
            '[sandbox_workspace_write]\n'
            'writable_roots = ["./src", "./tests"]\n'
            'network_access = false\n'
        )
        self.assertIn("Write(./src/**)", s["permissions"]["allow"])
        self.assertIn("Write(./tests/**)", s["permissions"]["allow"])
        self.assertIn("WebFetch(*)", s["permissions"]["deny"])


class PrefixRulesTests(FsTestBase):
    """Claude Bash() permission patterns ↔ Codex Starlark prefix rules."""

    def test_bash_rules_become_prefix_rules(self):
        (self.src / "settings.json").write_text(json.dumps({
            "permissions": {
                "allow": ["Bash(git commit:*)", "Bash(npm run build:*)",
                          "Read(*)"],
                "ask": ["Bash(git push:*)"],
                "deny": ["Bash(rm -rf:*)"],
            }
        }))
        ctx = make_ctx(self.src, self.dst)
        m._apply_claude_permissions(ctx)

        rules = (self.dst / "rules" / "default.rules").read_text()
        self.assertIn(
            'prefix_rule(pattern=["git", "commit"], decision="allow")', rules)
        self.assertIn(
            'prefix_rule(pattern=["npm", "run", "build"], decision="allow")',
            rules)
        self.assertIn(
            'prefix_rule(pattern=["git", "push"], decision="prompt")', rules)
        self.assertIn(
            'prefix_rule(pattern=["rm", "-rf"], decision="forbidden")', rules)
        # Read() isn't a shell command — no prefix rule for it.
        self.assertNotIn("Read", rules)

    def test_existing_smart_approval_rules_are_appended_not_replaced(self):
        (self.dst / "rules").mkdir()
        (self.dst / "rules" / "default.rules").write_text(
            'prefix_rule(pattern=["vercel", "deploy"], decision="allow")\n')
        (self.src / "settings.json").write_text(json.dumps({
            "permissions": {"allow": ["Bash(git status:*)"]}
        }))
        ctx = make_ctx(self.src, self.dst)
        m._apply_claude_permissions(ctx)

        rules = (self.dst / "rules" / "default.rules").read_text()
        self.assertIn('pattern=["vercel", "deploy"]', rules)
        self.assertIn('pattern=["git", "status"]', rules)

    def test_prefix_rules_become_claude_permissions(self):
        (self.src / "rules").mkdir()
        (self.src / "rules" / "default.rules").write_text(
            'prefix_rule(pattern=["npm", "run", "build"], decision="allow")\n'
            'prefix_rule(\n'
            '    pattern = ["gh", "pr", "view"],\n'
            '    decision = "prompt",\n'
            '    justification = "PR views need approval",\n'
            ')\n'
            'prefix_rule(pattern=["rm"], decision="forbidden")\n'
            'prefix_rule(pattern=["gh", ["view", "list"]])\n')
        ctx = make_ctx(self.src, self.dst)
        m._apply_codex_rules(ctx)

        perms = json.loads(
            (self.dst / "settings.json").read_text())["permissions"]
        self.assertIn("Bash(npm run build:*)", perms["allow"])
        self.assertIn("Bash(gh pr view:*)", perms["ask"])
        self.assertIn("Bash(rm:*)", perms["deny"])
        # Union pattern elements can't be translated — noted instead.
        self.assertTrue(any("union" in n for n in ctx.report.notes))

    def test_round_trip_preserves_rules(self):
        (self.src / "settings.json").write_text(json.dumps({
            "permissions": {"allow": ["Bash(git commit:*)"]}
        }))
        mid = self.tmp / "mid"
        mid.mkdir()
        m._apply_claude_permissions(make_ctx(self.src, mid))
        ctx_back = make_ctx(mid, self.dst)
        m._apply_codex_rules(ctx_back)
        perms = json.loads(
            (self.dst / "settings.json").read_text())["permissions"]
        self.assertIn("Bash(git commit:*)", perms["allow"])


class McpHttpTransportTests(FsTestBase):
    """Codex speaks streamable HTTP MCP now — URL servers carry over."""

    def test_claude_http_server_maps_to_codex_url(self):
        rep = m.Report(direction="t")
        out = m._normalize_mcp_claude_to_codex(
            "gh", {"type": "http", "url": "https://x.test/mcp",
                   "headers": {"Authorization": "Bearer t"}}, rep)
        self.assertEqual(out["url"], "https://x.test/mcp")
        self.assertEqual(out["http_headers"]["Authorization"], "Bearer t")

    def test_cursor_untyped_url_server_maps_to_codex(self):
        # Cursor omits `type` for remote servers; a bare url means HTTP.
        rep = m.Report(direction="t")
        out = m._normalize_mcp_claude_to_codex(
            "r", {"url": "https://r.test/mcp"}, rep)
        self.assertEqual(out, {"url": "https://r.test/mcp"})

    def test_sse_and_ws_still_skipped_for_codex(self):
        rep = m.Report(direction="t")
        self.assertIsNone(m._normalize_mcp_claude_to_codex(
            "s", {"type": "sse", "url": "https://s.test"}, rep))
        self.assertIsNone(m._normalize_mcp_claude_to_codex(
            "w", {"type": "ws", "url": "wss://w.test"}, rep))
        self.assertEqual(len(rep.skipped_unmappable), 2)

    def test_codex_url_server_maps_to_claude_http(self):
        out = m._normalize_mcp_codex_to_claude(
            {"url": "https://x.test/mcp",
             "http_headers": {"X-K": "v"}})
        self.assertEqual(out["type"], "http")
        self.assertEqual(out["url"], "https://x.test/mcp")
        self.assertEqual(out["headers"], {"X-K": "v"})

    def test_codex_url_server_maps_to_cursor(self):
        out = m._native_mcp_from_codex(
            "x", {"url": "https://x.test/mcp"})
        self.assertEqual(out, {"url": "https://x.test/mcp"})


class TierBHooksNotifyTests(FsTestBase):
    def test_claude_hooks_split_between_hooks_json_and_notify(self):
        (self.src / "settings.json").write_text(json.dumps({
            "hooks": {
                "Notification": [{"hooks": [
                    {"type": "command", "command": "say hello"}
                ]}],
                "PreToolUse": [{"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "echo pre", "timeout": 30}
                ]}],
                "FileChanged": [{"hooks": [
                    {"type": "command", "command": "echo changed"}
                ]}],
            }
        }))
        ctx = make_ctx(self.src, self.dst)
        m._apply_claude_notify_hook(ctx)

        # Notification → legacy notify argv (wrapped via /bin/sh -c).
        cfg = tomllib.loads((self.dst / "config.toml").read_text())
        self.assertEqual(cfg["notify"], ["/bin/sh", "-c", "say hello"])

        # PreToolUse translates near-verbatim into Codex hooks.json.
        hooks = json.loads((self.dst / "hooks.json").read_text())["hooks"]
        entry = hooks["PreToolUse"][0]
        self.assertEqual(entry["matcher"], "Bash")
        self.assertEqual(entry["hooks"][0]["command"], "echo pre")
        self.assertEqual(entry["hooks"][0]["timeout"], 30)

        # Claude-only events are reported, not silently dropped.
        self.assertTrue(any("FileChanged" in s
                            for s in ctx.report.skipped_unmappable))

    def test_codex_hooks_json_becomes_claude_hooks(self):
        (self.src / "hooks.json").write_text(json.dumps({
            "hooks": {
                "Stop": [{"hooks": [
                    {"type": "command", "command": "./done.sh"}
                ]}],
                "PostToolUse": [{"hooks": [
                    {"type": "command", "command": "fmt",
                     "commandWindows": "fmt.exe"}
                ]}],
            }
        }))
        ctx = make_ctx(self.src, self.dst)
        m._apply_codex_hooks(ctx)

        s = json.loads((self.dst / "settings.json").read_text())
        self.assertEqual(
            s["hooks"]["Stop"][0]["hooks"][0]["command"], "./done.sh")
        self.assertEqual(
            s["hooks"]["PostToolUse"][0]["hooks"][0]["command"], "fmt")
        # Windows command variants have no Claude equivalent — noted.
        self.assertTrue(any("commandWindows" in n for n in ctx.report.notes))

    def test_codex_notify_becomes_notification_hook(self):
        (self.src / "config.toml").write_text(
            'notify = ["/bin/echo", "ding"]\n')
        ctx = make_ctx(self.src, self.dst)
        m._apply_codex_notify(ctx)

        s = json.loads((self.dst / "settings.json").read_text())
        self.assertIn("Notification", s["hooks"])

    def test_notify_round_trip_unwraps_shell_wrapper(self):
        # claude→codex wraps hook commands as ["/bin/sh", "-c", CMD];
        # codex→claude must unwrap back to CMD, not " ".join() the argv.
        (self.src / "config.toml").write_text(
            'notify = ["/bin/sh", "-c", "say \\"all done\\""]\n')
        ctx = make_ctx(self.src, self.dst)
        m._apply_codex_notify(ctx)
        s = json.loads((self.dst / "settings.json").read_text())
        cmd = s["hooks"]["Notification"][0]["hooks"][0]["command"]
        self.assertEqual(cmd, 'say "all done"')

    def test_notify_argv_is_shell_quoted(self):
        (self.src / "config.toml").write_text(
            'notify = ["notify-send", "job done"]\n')
        ctx = make_ctx(self.src, self.dst)
        m._apply_codex_notify(ctx)
        s = json.loads((self.dst / "settings.json").read_text())
        cmd = s["hooks"]["Notification"][0]["hooks"][0]["command"]
        self.assertEqual(cmd, "notify-send 'job done'")


class TierBProfilesTests(FsTestBase):
    def test_profile_materializes_settings_with_xhigh_effort(self):
        (self.src / "config.toml").write_text(
            'model = "gpt-5"\n'
            '[profiles.deep]\n'
            'model_reasoning_effort = "xhigh"\n')
        ctx = make_ctx(self.src, self.dst)
        m._apply_codex_profiles(ctx)

        s = json.loads((self.dst / "profiles" / "deep.settings.json").read_text())
        self.assertEqual(s["model"], "gpt-5")  # inherited from base config
        self.assertEqual(s["effortLevel"], "xhigh")

    def test_file_based_profiles_are_read(self):
        # Codex 0.134+ keeps each profile in its own <name>.config.toml.
        (self.src / "config.toml").write_text('model = "gpt-5"\n')
        (self.src / "fast.config.toml").write_text(
            'model = "gpt-5-mini"\nmodel_reasoning_effort = "low"\n')
        ctx = make_ctx(self.src, self.dst)
        self.assertTrue(m._detect_codex_profiles(ctx))
        m._apply_codex_profiles(ctx)

        s = json.loads(
            (self.dst / "profiles" / "fast.settings.json").read_text())
        self.assertEqual(s["model"], "gpt-5-mini")
        self.assertEqual(s["effortLevel"], "low")


class TierBAgentsAndSkillsTests(FsTestBase):
    def test_subagent_becomes_codex_custom_agent(self):
        agents = self.src / "agents"
        agents.mkdir()
        (agents / "reviewer.md").write_text(
            "---\nname: reviewer\ndescription: Review code\n"
            "permissionMode: readOnly\nskills: release-notes\n"
            "tools: Read\ndisallowedTools: Bash\neffort: max\n---\nDo a review.\n")
        ctx = make_ctx(self.src, self.dst)
        m._apply_claude_agents(ctx)

        agent = tomllib.loads((self.dst / "agents" / "reviewer.toml").read_text())
        self.assertEqual(agent["name"], "reviewer")
        self.assertEqual(agent["description"], "Review code")
        self.assertEqual(agent["sandbox_mode"], "read-only")
        self.assertEqual(agent["model_reasoning_effort"], "xhigh")
        self.assertIn("Do a review.", agent["developer_instructions"])
        self.assertIn("$release-notes", agent["developer_instructions"])
        self.assertIn("Don't use these tools", agent["developer_instructions"])

    def test_subagent_xhigh_effort_maps_one_to_one(self):
        agents = self.src / "agents"
        agents.mkdir()
        (agents / "deep.md").write_text(
            "---\nname: deep\ndescription: Deep work\neffort: xhigh\n---\nThink hard.\n")
        ctx = make_ctx(self.src, self.dst)
        m._apply_claude_agents(ctx)
        agent = tomllib.loads((self.dst / "agents" / "deep.toml").read_text())
        self.assertEqual(agent["model_reasoning_effort"], "xhigh")

    def test_codex_custom_agent_becomes_claude_subagent(self):
        agents = self.src / "agents"
        agents.mkdir()
        (agents / "reviewer.toml").write_text(
            'name = "reviewer"\n'
            'description = "Review code"\n'
            'model = "gpt-5"\n'
            'model_reasoning_effort = "xhigh"\n'
            'sandbox_mode = "read-only"\n'
            'developer_instructions = "Do a review."\n'
        )
        ctx = make_ctx(self.src, self.dst)
        m.tier_a_codex_agents_to_claude(ctx)

        out = (self.dst / "agents" / "reviewer.md").read_text()
        body, fm = m.strip_frontmatter(out)
        self.assertEqual(fm["name"], "reviewer")
        self.assertEqual(fm["description"], "Review code")
        self.assertEqual(fm["model"], "gpt-5")
        self.assertEqual(fm["effort"], "xhigh")
        self.assertIn("Do a review.", body)
        self.assertIn("sandbox_mode", body)

    def test_skill_copies_verbatim_with_assets(self):
        # All three tools share the Agent Skills format, so skills are a
        # Tier A verbatim tree copy — frontmatter, body, and assets survive.
        sk = self.src / "skills" / "myskill"
        (sk / "scripts").mkdir(parents=True)
        skill_text = "---\nname: myskill\ndescription: Does things.\n---\nSkill content.\n"
        (sk / "SKILL.md").write_text(skill_text)
        (sk / "scripts" / "asset.txt").write_text("asset bytes")

        ctx = make_ctx(self.src, self.dst)
        m.tier_a_skills_copy(ctx)

        out = self.dst / "skills" / "myskill"
        self.assertEqual((out / "SKILL.md").read_text(), skill_text)
        self.assertEqual((out / "scripts" / "asset.txt").read_text(), "asset bytes")
        self.assertTrue(any("skills/myskill/" in s for s in ctx.report.migrated_clean))

    def test_codex_system_skills_are_not_migrated(self):
        # Codex keeps tool-managed skills under skills/.system/ — those
        # belong to the Codex install, not the user's config.
        sysdir = self.src / "skills" / ".system" / "builtin"
        sysdir.mkdir(parents=True)
        (sysdir / "SKILL.md").write_text("---\nname: builtin\n---\nBuiltin.\n")
        user = self.src / "skills" / "mine"
        user.mkdir(parents=True)
        (user / "SKILL.md").write_text("---\nname: mine\n---\nMine.\n")

        ctx = make_ctx(self.src, self.dst)
        m.tier_a_skills_copy(ctx)

        self.assertTrue((self.dst / "skills" / "mine" / "SKILL.md").exists())
        self.assertFalse((self.dst / "skills" / ".system").exists())

    def test_skill_round_trip_is_byte_identical(self):
        sk = self.src / "skills" / "rt"
        sk.mkdir(parents=True)
        text = "---\nname: rt\nallowed-tools: Bash(git:*)\n---\nBody.\n"
        (sk / "SKILL.md").write_text(text)

        mid = self.tmp / "mid"
        mid.mkdir()
        m.tier_a_skills_copy(make_ctx(self.src, mid))
        m.tier_a_skills_copy(make_ctx(mid, self.dst))

        self.assertEqual(
            (self.dst / "skills" / "rt" / "SKILL.md").read_text(), text)


# ============================================================================
# Plan-then-apply + backup + restore
# ============================================================================

class PlanModeTests(FsTestBase):
    def test_plan_records_writes_without_creating_files(self):
        (self.src / "settings.json").write_text(json.dumps({"model": "x"}))
        ctx = make_ctx(self.src, self.dst, plan_mode=True)
        m.tier_a_settings_claude_to_codex(ctx)

        self.assertIn(self.dst / "config.toml", ctx.planned_writes)
        self.assertFalse((self.dst / "config.toml").exists())

    def test_apply_writes(self):
        (self.src / "settings.json").write_text(json.dumps({"model": "x"}))
        ctx = make_ctx(self.src, self.dst)  # plan_mode=False
        m.tier_a_settings_claude_to_codex(ctx)
        self.assertTrue((self.dst / "config.toml").exists())


class BackupAndRestoreTests(FsTestBase):
    def test_perform_backup_writes_manifest_and_files(self):
        (self.dst / "settings.json").write_text('{"model": "original"}')

        ctx = make_ctx(self.src, self.dst)
        ctx.planned_writes = {
            self.dst / "settings.json",
            self.dst / "new_file.json",
        }
        backup_root = m.perform_backup(ctx)
        self.assertIsNotNone(backup_root)

        manifest = json.loads((backup_root / "manifest.json").read_text())
        entries = {e["relative"]: e for e in manifest["entries"]}
        self.assertTrue(entries["settings.json"]["existed_before"])
        self.assertFalse(entries["new_file.json"]["existed_before"])
        # The pre-migration content is in the backup byte-for-byte.
        self.assertEqual((backup_root / "settings.json").read_text(),
                         '{"model": "original"}')

    def test_full_migrate_then_restore_round_trip(self):
        # Pre-existing settings.json that the migration will modify.
        (self.dst / "settings.json").write_text('{"model": "original"}')

        ctx = make_ctx(self.src, self.dst)
        ctx.planned_writes = {
            self.dst / "settings.json",
            self.dst / "fresh.json",
        }
        backup_root = m.perform_backup(ctx)

        # Simulate the migration's effects.
        (self.dst / "settings.json").write_text('{"model": "modified"}')
        (self.dst / "fresh.json").write_text("{}")

        rc = m.restore_from_backup(
            backup_root, interactive=False, dry_run=False)
        self.assertEqual(rc, 0)

        # Pre-existing file restored byte-for-byte; freshly-created one gone.
        self.assertEqual((self.dst / "settings.json").read_text(),
                         '{"model": "original"}')
        self.assertFalse((self.dst / "fresh.json").exists())

    def test_report_file_can_be_backed_up_and_restored(self):
        (self.dst / "MIGRATION_REPORT.md").write_text("old report")
        ctx = make_ctx(self.src, self.dst)
        ctx.planned_writes = {self.dst / "MIGRATION_REPORT.md"}
        backup_root = m.perform_backup(ctx)

        (self.dst / "MIGRATION_REPORT.md").write_text("new report")
        rc = m.restore_from_backup(backup_root, interactive=False, dry_run=False)

        self.assertEqual(rc, 0)
        self.assertEqual((self.dst / "MIGRATION_REPORT.md").read_text(),
                         "old report")

    def test_backup_roots_are_unique(self):
        a = make_ctx(self.src, self.dst).backup_root
        b = make_ctx(self.src, self.dst).backup_root
        self.assertNotEqual(a, b)


# ============================================================================
# Cursor support
# ============================================================================

class CursorMcpTests(FsTestBase):
    def test_read_mcp_json(self):
        (self.src / "mcp.json").write_text(json.dumps({
            "mcpServers": {"foo": {"command": "fooserver", "args": ["--x"]}}
        }))
        servers = m.cursor_read_mcp(self.src)
        self.assertEqual(servers["foo"]["command"], "fooserver")

    def test_write_mcp_merges_existing(self):
        (self.dst / "mcp.json").write_text(json.dumps({
            "mcpServers": {"keep": {"command": "k"}}
        }))
        ctx = make_ctx(self.src, self.dst)
        m.cursor_write_mcp({"new": {"command": "n"}}, self.dst, ctx)
        data = json.loads((self.dst / "mcp.json").read_text())
        self.assertEqual(set(data["mcpServers"].keys()), {"keep", "new"})


class CursorRulesTests(FsTestBase):
    def test_mdc_round_trip_preserves_globs_and_alwaysapply(self):
        rules_dir = self.src / "rules"
        rules_dir.mkdir()
        (rules_dir / "react.mdc").write_text(
            "---\ndescription: React rules\nglobs: src/**/*.tsx\n"
            "alwaysApply: false\n---\nUse hooks.\n")
        (rules_dir / "general.mdc").write_text(
            "---\ndescription: General\nalwaysApply: true\n---\nBe concise.\n")

        rules = m.cursor_read_rules(self.src)
        # Order: directory listing is sorted, so general before react.
        names = sorted(r.name for r in rules)
        self.assertEqual(names, ["general", "react"])

        # Render to a fenced doc, parse back, write fresh rules — should
        # reproduce the originals.
        doc = m.cursor_rules_to_doc(rules)
        parsed = m.doc_to_cursor_rules(doc)
        # 2 fenced rules + possibly a default "migrated" if leftover text;
        # there's no leftover in this case.
        by_name = {r.name: r for r in parsed}
        self.assertIn("react", by_name)
        self.assertEqual(by_name["react"].globs, "src/**/*.tsx")
        self.assertFalse(by_name["react"].always_apply)
        self.assertEqual(by_name["general"].body, "Be concise.")
        self.assertTrue(by_name["general"].always_apply)

    def test_legacy_cursorrules_picked_up(self):
        # Simulate <project_root>/.cursorrules next to a .cursor/ dir.
        project_root = self.tmp / "proj"
        project_root.mkdir()
        cursor_root = project_root / ".cursor"
        cursor_root.mkdir()
        (project_root / ".cursorrules").write_text("Use 2-space indent.\n")

        rules = m.cursor_read_rules(cursor_root)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].name, "_cursorrules_legacy")
        self.assertIn("2-space", rules[0].body)

    def test_doc_with_leftover_becomes_default_rule(self):
        text = ("# Top-level notes\nBe nice.\n\n" +
                m.cursor_rules_to_doc([
                    m.CursorRule(name="r1", description="", globs=None,
                                 always_apply=True, body="rule one body")]))
        rules = m.doc_to_cursor_rules(text, default_name="leftover")
        names = {r.name for r in rules}
        self.assertEqual(names, {"r1", "leftover"})
        leftover = next(r for r in rules if r.name == "leftover")
        self.assertIn("Be nice", leftover.body)


class CursorToolPathsTests(unittest.TestCase):
    def test_cursor_paths_user_scope(self):
        p = m._tool_paths("cursor", "user", None)
        self.assertEqual(p["root"], Path.home() / ".cursor")
        self.assertIsNone(p["doc"])

    def test_cursor_paths_project_scope(self):
        p = m._tool_paths("cursor", "project", None)
        self.assertEqual(p["root"], Path.cwd() / ".cursor")
        self.assertEqual(p["doc"], Path.cwd() / ".cursorrules")

    def test_override_dir_short_circuits_scope(self):
        p = m._tool_paths("cursor", "user", "/tmp/somewhere")
        self.assertEqual(p["root"], Path("/tmp/somewhere"))


class CliTests(unittest.TestCase):
    def test_legacy_direction_flag_is_removed(self):
        with mock.patch.object(sys, "argv", ["migrate.py", "--direction", "claude-to-codex"]), \
             mock.patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                m.main()
        self.assertEqual(cm.exception.code, 2)


class CursorDirectionDriversTests(FsTestBase):
    def test_claude_to_cursor_creates_rules_and_mcp(self):
        (self.src / "settings.json").write_text(json.dumps({
            "mcpServers": {"foo": {"command": "fooserver"}}
        }))
        (self.src / "CLAUDE.md").write_text("# Be concise\n")

        ctx = make_ctx(self.src, self.dst)
        m.run_claude_to_cursor(ctx, {})

        self.assertTrue((self.dst / "mcp.json").exists())
        mdc = list((self.dst / "rules").glob("*.mdc"))
        self.assertEqual(len(mdc), 1)
        self.assertIn("Be concise", mdc[0].read_text())

    def test_cursor_to_claude_round_trips_rules(self):
        rules_dir = self.src / "rules"
        rules_dir.mkdir()
        (rules_dir / "r1.mdc").write_text(
            "---\ndescription: R1\nglobs: src/**\nalwaysApply: false\n---\n"
            "Rule one.\n")
        (self.src / "mcp.json").write_text(json.dumps({
            "mcpServers": {"foo": {"command": "fooserver"}}}))

        ctx = make_ctx(self.src, self.dst)
        m.run_cursor_to_claude(ctx, {})

        rule_text = (self.dst / "rules" / "r1.md").read_text()
        self.assertIn("paths: src/**", rule_text)
        self.assertIn("Rule one.", rule_text)
        mcp = json.loads((self.dst / "mcp.json").read_text())
        self.assertEqual(mcp["mcpServers"]["foo"]["type"], "stdio")

    def test_cursor_to_codex_filters_non_stdio_mcp(self):
        (self.src / "mcp.json").write_text(json.dumps({
            "mcpServers": {
                "ok":  {"command": "x"},
                "bad": {"type": "sse", "url": "https://x"},
            }
        }))
        ctx = make_ctx(self.src, self.dst)
        m.run_cursor_to_codex(ctx, {})

        cfg = tomllib.loads((self.dst / "config.toml").read_text())
        self.assertIn("ok", cfg["mcp_servers"])
        self.assertNotIn("bad", cfg["mcp_servers"])
        self.assertTrue(any("sse" in s for s in ctx.report.skipped_unmappable))

    def test_codex_to_cursor_translates_mcp_and_docs(self):
        (self.src / "config.toml").write_text(
            'model = "gpt-5"\n'
            '[mcp_servers.foo]\ncommand = "fooserver"\n'
        )
        (self.src / "AGENTS.md").write_text("# Test rules\n")
        ctx = make_ctx(self.src, self.dst)
        m.run_codex_to_cursor(ctx, {})

        data = json.loads((self.dst / "mcp.json").read_text())
        self.assertEqual(data["mcpServers"]["foo"]["command"], "fooserver")
        mdcs = list((self.dst / "rules").glob("*.mdc"))
        self.assertEqual(len(mdcs), 1)


class CursorNativeTargetsTests(FsTestBase):
    """Cursor-bound translations against Cursor 2.4+ native runtimes:
    claude agents → .cursor/agents/*.md subagents (Tier B), and claude
    commands / codex prompts → slash-invocable Cursor skills (Tier A)."""

    def test_agents_become_native_cursor_subagents(self):
        agents = self.src / "agents"
        agents.mkdir()
        (agents / "reviewer.md").write_text(
            "---\ndescription: Code reviewer\nmodel: claude-opus-4-8\n"
            "effort: high\ntools: Read, Grep\n---\nReview carefully.\n")
        ctx = make_ctx(self.src, self.dst)
        m._apply_claude_agents_cursor(ctx)

        out = (self.dst / "agents" / "reviewer.md").read_text()
        body, fm = m.strip_frontmatter(out)
        self.assertEqual(fm.get("name"), "reviewer")
        self.assertEqual(fm.get("description"), "Code reviewer")
        # effort folds into Cursor's model bracket syntax.
        self.assertEqual(fm.get("model"), "claude-opus-4-8[effort=high]")
        self.assertIn("Review carefully", body)
        # tools has no Cursor field — dropped with an in-body note.
        self.assertIn("`tools`", body)

    def test_readonly_permission_mode_maps_to_cursor_readonly(self):
        agents = self.src / "agents"
        agents.mkdir()
        (agents / "scout.md").write_text(
            "---\ndescription: Scout\npermissionMode: plan\n---\nLook around.\n")
        ctx = make_ctx(self.src, self.dst)
        m._apply_claude_agents_cursor(ctx)
        _, fm = m.strip_frontmatter(
            (self.dst / "agents" / "scout.md").read_text())
        self.assertEqual(fm.get("readonly"), "true")

    def test_commands_become_slash_invocable_cursor_skills(self):
        cmds = self.src / "commands"
        cmds.mkdir()
        (cmds / "foo.md").write_text(
            "---\ndescription: Do foo\nargument-hint: [target]\n---\nFoo body.\n")
        ctx = make_ctx(self.src, self.dst)
        m.tier_a_commands_to_cursor_skills(ctx, "commands", "commands")

        out = (self.dst / "skills" / "foo" / "SKILL.md").read_text()
        body, fm = m.strip_frontmatter(out)
        self.assertEqual(fm.get("name"), "foo")
        self.assertEqual(fm.get("description"), "Do foo")
        self.assertEqual(fm.get("disable-model-invocation"), "true")
        self.assertIn("Foo body", body)
        # argument-hint survives via the migrator meta comment.
        self.assertIn("argument-hint", out)

    def test_codex_prompts_become_cursor_skills(self):
        prompts = self.src / "prompts"
        prompts.mkdir()
        (prompts / "summarize.md").write_text("Summarize this.\n")
        ctx = make_ctx(self.src, self.dst)
        m.tier_a_commands_to_cursor_skills(ctx, "prompts", "prompts")

        out = (self.dst / "skills" / "summarize" / "SKILL.md").read_text()
        body, fm = m.strip_frontmatter(out)
        # No source description → falls back to the slash-command name.
        self.assertIn("summarize", fm["description"].lower())
        self.assertEqual(fm["disable-model-invocation"], "true")
        self.assertIn("Summarize this.", body)

    def test_cursor_agents_round_trip_to_claude(self):
        agents = self.src / "agents"
        agents.mkdir()
        (agents / "helper.md").write_text(
            "---\nname: helper\ndescription: Helps out\n"
            "model: claude-opus-4-8[effort=high]\nis_background: true\n"
            "readonly: true\n---\nHelp with things.\n")
        ctx = make_ctx(self.src, self.dst)
        m.tier_a_cursor_agents_to_claude(ctx)

        out = (self.dst / "agents" / "helper.md").read_text()
        body, fm = m.strip_frontmatter(out)
        self.assertEqual(fm.get("name"), "helper")
        self.assertEqual(fm.get("model"), "claude-opus-4-8")
        self.assertEqual(fm.get("effort"), "high")
        self.assertEqual(fm.get("background"), "true")
        self.assertIn("Help with things.", body)
        # readonly has no Claude field — kept as a review note.
        self.assertIn("readonly", body)


class CursorHooksTests(FsTestBase):
    def test_claude_hooks_translate_to_cursor_hooks_json(self):
        (self.src / "settings.json").write_text(json.dumps({
            "hooks": {
                "PreToolUse": [{"matcher": "Bash",
                                "hooks": [{"type": "command",
                                           "command": "./check.sh",
                                           "timeout": 30}]}],
                "Notification": [{"hooks": [{"type": "command",
                                             "command": "notify-send hi"}]}],
            },
        }))
        ctx = make_ctx(self.src, self.dst)
        m._apply_claude_hooks_cursor(ctx)

        data = json.loads((self.dst / "hooks.json").read_text())
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["hooks"]["preToolUse"][0]["command"], "./check.sh")
        self.assertEqual(data["hooks"]["preToolUse"][0]["timeout"], 30)
        # Notification has no Cursor event — reported, not written.
        self.assertNotIn("notification", {k.lower() for k in data["hooks"]})
        self.assertTrue(any("Notification" in s
                            for s in ctx.report.skipped_unmappable))
        # The matcher can't be represented — noted.
        self.assertTrue(any("matcher" in n for n in ctx.report.notes))

    def test_cursor_hooks_translate_to_claude_settings(self):
        (self.src / "hooks.json").write_text(json.dumps({
            "version": 1,
            "hooks": {
                "stop": [{"command": "./done.sh"}],
                "beforeShellExecution": [{"command": "./guard.sh"}],
                "preToolUse": [{"command": "./ask.py", "type": "prompt"}],
            },
        }))
        ctx = make_ctx(self.src, self.dst)
        m._apply_cursor_hooks(ctx)

        settings = json.loads((self.dst / "settings.json").read_text())
        stop = settings["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(stop, {"type": "command", "command": "./done.sh"})
        # Cursor-only event dropped with a report entry.
        self.assertTrue(any("beforeShellExecution" in s
                            for s in ctx.report.skipped_unmappable))
        # prompt-type hooks aren't translatable.
        self.assertNotIn("PreToolUse", settings["hooks"])
        self.assertTrue(any("prompt-type" in n for n in ctx.report.notes))


class CursorTierBIntegrationTests(FsTestBase):
    """End-to-end: a claude→cursor run with agents/skills/commands present.
    Agents/commands should appear under migrated_lossy (when accepted),
    skills under migrated_clean (Tier A), and none of them also under
    skipped_unmappable as Tier C."""

    def test_accepted_tier_b_doesnt_double_count_as_tier_c(self):
        (self.src / "settings.json").write_text("{}")
        for sub in ("agents", "skills/sk1", "commands"):
            (self.src / sub).mkdir(parents=True)
        (self.src / "agents" / "a.md").write_text("agent body")
        (self.src / "skills" / "sk1" / "SKILL.md").write_text("skill body")
        (self.src / "commands" / "c.md").write_text("cmd body")

        ctx = make_ctx(self.src, self.dst)
        m.run_claude_to_cursor(ctx, lossy_decisions={
            "agents_cursor": True,
        })

        # Agents lossy; skills and commands copied cleanly as Tier A.
        joined = " ".join(ctx.report.migrated_lossy)
        self.assertIn("agents/a.md", joined)
        clean = " ".join(ctx.report.migrated_clean)
        self.assertIn("skills/sk1/", clean)
        self.assertIn("commands/c.md", clean)
        self.assertTrue(
            (self.dst / "skills" / "sk1" / "SKILL.md").exists())
        self.assertTrue(
            (self.dst / "skills" / "c" / "SKILL.md").exists())

        # And NOT reported as "no equivalent" under unmappable.
        unmappable = " ".join(ctx.report.skipped_unmappable)
        self.assertNotIn("agents/", unmappable)
        self.assertNotIn("skills/", unmappable)
        self.assertNotIn("commands/", unmappable)

    def test_declined_tier_b_appears_under_skipped_by_user(self):
        (self.src / "agents").mkdir()
        (self.src / "agents" / "r.md").write_text("x")
        ctx = make_ctx(self.src, self.dst)
        m.run_claude_to_cursor(ctx, lossy_decisions={"agents_cursor": False})

        joined = " ".join(ctx.report.skipped_by_user)
        self.assertIn("agents/ → .cursor/agents/*.md", joined)
        # Still not double-counted as unmappable.
        self.assertNotIn("agents/",
                         " ".join(ctx.report.skipped_unmappable))


class JsoncTests(unittest.TestCase):
    def test_comments_and_trailing_commas_stripped(self):
        text = ('{\n  // line comment\n  "a": 1, /* block */\n'
                '  "url": "https://x/y", // not a comment inside string\n'
                '  "b": [1, 2,],\n}\n')
        data = json.loads(m._strip_jsonc(text))
        self.assertEqual(data, {"a": 1, "url": "https://x/y", "b": [1, 2]})


class OpencodeMcpTests(FsTestBase):
    def test_local_and_remote_round_trip(self):
        rep = m.Report(direction="t")
        native_local = {"type": "stdio", "command": "srv", "args": ["-v"],
                        "env": {"K": "v"}}
        oc = m._mcp_native_to_opencode("x", native_local, rep)
        self.assertEqual(oc, {"type": "local", "command": ["srv", "-v"],
                              "environment": {"K": "v"}})
        self.assertEqual(m._mcp_opencode_to_native(oc), native_local)

        native_remote = {"type": "http", "url": "https://r.test",
                         "headers": {"X": "y"}}
        oc = m._mcp_native_to_opencode("r", native_remote, rep)
        self.assertEqual(oc, {"type": "remote", "url": "https://r.test",
                              "headers": {"X": "y"}})
        self.assertEqual(m._mcp_opencode_to_native(oc), native_remote)

    def test_ws_skipped_sse_noted(self):
        rep = m.Report(direction="t")
        self.assertIsNone(m._mcp_native_to_opencode(
            "w", {"type": "ws", "url": "wss://w"}, rep))
        self.assertEqual(len(rep.skipped_unmappable), 1)
        out = m._mcp_native_to_opencode(
            "s", {"type": "sse", "url": "https://s"}, rep)
        self.assertEqual(out["type"], "remote")
        self.assertTrue(any("SSE" in n for n in rep.notes))


class OpencodeModelTests(unittest.TestCase):
    def test_prefixing_by_source_tool(self):
        self.assertEqual(m.model_to_opencode("claude-opus-4-8", "claude"),
                         "anthropic/claude-opus-4-8")
        self.assertEqual(m.model_to_opencode("gpt-5.6", "codex"),
                         "openai/gpt-5.6")
        # Already qualified or unknown provider → pass through.
        self.assertEqual(m.model_to_opencode("groq/llama", "claude"),
                         "groq/llama")
        self.assertEqual(m.model_to_opencode("mystery", "cursor"), "mystery")

    def test_stripping(self):
        self.assertEqual(m.model_from_opencode("anthropic/claude-x"),
                         ("claude-x", "anthropic"))
        self.assertEqual(m.model_from_opencode("bare"), ("bare", None))


class OpencodePermissionTests(FsTestBase):
    def test_claude_rules_become_glob_map(self):
        rep = m.Report(direction="t")
        mapped = m._claude_perms_to_opencode({
            "allow": ["Bash(git commit:*)", "Read(*)"],
            "ask": ["Bash(git push:*)"],
            "deny": ["Bash(rm -rf:*)", "WebFetch(*)"],
        }, rep)
        self.assertEqual(mapped["bash"]["git commit*"], "allow")
        self.assertEqual(mapped["bash"]["git push*"], "ask")
        self.assertEqual(mapped["bash"]["rm -rf*"], "deny")
        self.assertEqual(mapped["read"], "allow")
        self.assertEqual(mapped["webfetch"], "deny")

    def test_opencode_map_becomes_claude_rules(self):
        rep = m.Report(direction="t")
        mapped = m._opencode_perms_to_claude({
            "bash": {"git *": "allow", "*": "ask", "rm *": "deny"},
            "edit": "allow",
        }, rep)
        self.assertIn("Bash(git:*)", mapped["allow"])
        self.assertIn("Write(*)", mapped["allow"])
        self.assertIn("Bash(*)", mapped["ask"])
        self.assertIn("Bash(rm:*)", mapped["deny"])

    def test_round_trip_preserves_bash_intent(self):
        rep = m.Report(direction="t")
        oc = m._claude_perms_to_opencode(
            {"allow": ["Bash(npm run build:*)"]}, rep)
        back = m._opencode_perms_to_claude(oc, rep)
        self.assertIn("Bash(npm run build:*)", back["allow"])

    def test_opencode_bash_map_becomes_codex_prefix_rules(self):
        (self.src / "opencode.json").write_text(json.dumps({
            "permission": {"bash": {"git push*": "ask", "docker*": "deny",
                                    "mid*fix": "allow"}}
        }))
        ctx = make_ctx(self.src, self.dst)
        m._apply_opencode_bash_rules(ctx)
        rules = (self.dst / "rules" / "default.rules").read_text()
        self.assertIn(
            'prefix_rule(pattern=["git", "push"], decision="prompt")', rules)
        self.assertIn('prefix_rule(pattern=["docker"], decision="forbidden")',
                      rules)
        # Mid-pattern wildcards can't be prefixes — noted, not emitted.
        self.assertNotIn("mid", rules)
        self.assertTrue(any("mid*fix" in n for n in ctx.report.notes))


class OpencodeConfigIoTests(FsTestBase):
    def test_jsonc_config_read_and_json_written(self):
        (self.src / "opencode.jsonc").write_text(
            '{\n  // c\n  "model": "anthropic/claude-x",\n'
            '  "theme": "dark",\n}\n')
        cfg = m.load_opencode_config(self.src)
        self.assertEqual(cfg["model"], "anthropic/claude-x")

        ctx = make_ctx(self.src, self.dst)
        m.opencode_update_config(ctx, {"model": "anthropic/claude-y"})
        out = json.loads((self.dst / "opencode.json").read_text())
        self.assertEqual(out["model"], "anthropic/claude-y")

    def test_project_scope_reads_and_writes_project_root_config(self):
        oc_root = self.tmp / "proj" / ".opencode"
        oc_root.mkdir(parents=True)
        (oc_root.parent / "opencode.json").write_text(
            json.dumps({"model": "anthropic/claude-x"}))
        self.assertEqual(
            m.load_opencode_config(oc_root)["model"], "anthropic/claude-x")
        self.assertEqual(m.opencode_config_write_path(oc_root),
                         oc_root.parent / "opencode.json")

    def test_nested_dict_updates_merge(self):
        (self.dst / "opencode.json").write_text(json.dumps(
            {"mcp": {"keep": {"type": "remote", "url": "https://k"}}}))
        ctx = make_ctx(self.src, self.dst)
        m.opencode_update_config(
            ctx, {"mcp": {"new": {"type": "remote", "url": "https://n"}}})
        out = json.loads((self.dst / "opencode.json").read_text())
        self.assertIn("keep", out["mcp"])
        self.assertIn("new", out["mcp"])


class OpencodeAgentsTests(FsTestBase):
    def test_claude_agent_round_trips_through_opencode(self):
        agents = self.src / "agents"
        agents.mkdir()
        (agents / "rev.md").write_text(
            "---\ndescription: Reviewer\nmodel: claude-opus-4-8\n"
            "permissionMode: plan\n---\nReview things.\n")
        mid = self.tmp / "mid"
        mid.mkdir()
        ctx = make_ctx(self.src, mid)
        m._agents_write_opencode(ctx, m._agents_read_claude(self.src), "claude")

        out = (mid / "agents" / "rev.md").read_text()
        body, fm = m.strip_frontmatter(out)
        self.assertEqual(fm.get("description"), "Reviewer")
        self.assertEqual(fm.get("mode"), "subagent")
        self.assertEqual(fm.get("model"), "anthropic/claude-opus-4-8")
        # plan permission mode became a permission deny block.
        self.assertIn("edit: deny", out)
        self.assertIn("Review things.", body)

        ctx_back = make_ctx(mid, self.dst)
        m._agents_write_claude(ctx_back, m._agents_read_opencode(mid))
        back = (self.dst / "agents" / "rev.md").read_text()
        _, fm_back = m.strip_frontmatter(back)
        self.assertEqual(fm_back.get("description"), "Reviewer")
        self.assertEqual(fm_back.get("model"), "claude-opus-4-8")

    def test_opencode_agent_to_codex_toml(self):
        agents = self.src / "agents"
        agents.mkdir()
        (agents / "h.md").write_text(
            "---\ndescription: Helper\nmode: subagent\n"
            "model: openai/gpt-5.6\n---\nHelp.\n")
        ctx = make_ctx(self.src, self.dst)
        m._agents_write_codex(ctx, m._agents_read_opencode(self.src))
        cfg = tomllib.loads((self.dst / "agents" / "h.toml").read_text())
        self.assertEqual(cfg["name"], "h")
        self.assertEqual(cfg["model"], "gpt-5.6")
        self.assertIn("Help.", cfg["developer_instructions"])

    def test_legacy_singular_agent_dir_read(self):
        legacy = self.src / "agent"
        legacy.mkdir()
        (legacy / "old.md").write_text("---\ndescription: Old\n---\nOld body.\n")
        specs = m._agents_read_opencode(self.src)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].description, "Old")


class OpencodeCommandsTests(FsTestBase):
    def test_claude_command_round_trips_through_opencode(self):
        cmds = self.src / "commands"
        cmds.mkdir()
        (cmds / "ship.md").write_text(
            "---\ndescription: Ship it\nargument-hint: [env]\n---\nShip $1.\n")
        mid = self.tmp / "mid"
        mid.mkdir()
        m.tier_a_commands_to_opencode(make_ctx(self.src, mid), "commands")

        out = (mid / "commands" / "ship.md").read_text()
        _, fm = m.strip_frontmatter(out)
        self.assertEqual(fm, {"description": "Ship it"})
        self.assertIn("argument-hint", out)  # rides in the meta comment

        m.tier_a_opencode_commands_to(make_ctx(mid, self.dst), "commands")
        back = (self.dst / "commands" / "ship.md").read_text()
        body, fm_back = m.strip_frontmatter(back)
        self.assertEqual(fm_back.get("description"), "Ship it")
        self.assertEqual(fm_back.get("argument-hint"), "[env]")
        self.assertIn("Ship $1.", body)

    def test_opencode_only_keys_dropped_with_note(self):
        cmds = self.src / "commands"
        cmds.mkdir()
        (cmds / "sum.md").write_text(
            "---\ndescription: Sum\nagent: build\nsubtask: true\n---\nGo.\n")
        ctx = make_ctx(self.src, self.dst)
        m.tier_a_opencode_commands_to(ctx, "prompts")
        _, fm = m.strip_frontmatter((self.dst / "prompts" / "sum.md").read_text())
        self.assertEqual(fm, {"description": "Sum"})
        self.assertTrue(any("agent" in n and "subtask" in n
                            for n in ctx.report.notes))


class OpencodeRunnerTests(FsTestBase):
    def test_claude_to_opencode_end_to_end(self):
        (self.src / "CLAUDE.md").write_text("Project instructions.\n")
        (self.src / "settings.json").write_text(json.dumps({
            "model": "claude-opus-4-8",
            "hooks": {"Stop": [{"hooks": [{"type": "command",
                                           "command": "x"}]}]},
            "mcpServers": {"loc": {"command": "srv"}},
        }))
        sk = self.src / "skills" / "s1"
        sk.mkdir(parents=True)
        (sk / "SKILL.md").write_text("---\nname: s1\ndescription: d\n---\nb\n")

        ctx = make_ctx(self.src, self.dst)
        m.run_claude_to_opencode(ctx, lossy_decisions={})

        cfg = json.loads((self.dst / "opencode.json").read_text())
        self.assertEqual(cfg["model"], "anthropic/claude-opus-4-8")
        self.assertEqual(cfg["mcp"]["loc"]["command"], ["srv"])
        self.assertTrue((self.dst / "AGENTS.md").exists())
        self.assertTrue((self.dst / "skills" / "s1" / "SKILL.md").exists())
        # Hooks can't exist in opencode — reported, not silently dropped.
        self.assertTrue(any("hooks" in s
                            for s in ctx.report.skipped_unmappable))

    def test_opencode_to_claude_end_to_end(self):
        (self.src / "AGENTS.md").write_text("OC instructions.\n")
        (self.src / "opencode.json").write_text(json.dumps({
            "model": "anthropic/claude-sonnet-4-5",
            "mcp": {"rem": {"type": "remote", "url": "https://r.test"}},
            "theme": "dark",
        }))
        ctx = make_ctx(self.src, self.dst)
        m.run_opencode_to_claude(ctx, lossy_decisions={})

        settings = json.loads((self.dst / "settings.json").read_text())
        self.assertEqual(settings["model"], "claude-sonnet-4-5")
        self.assertTrue((self.dst / "CLAUDE.md").exists())
        mcp = json.loads((self.dst / "mcp.json").read_text())["mcpServers"]
        self.assertEqual(mcp["rem"]["type"], "http")
        # theme is opencode-only.
        self.assertTrue(any("theme" in s
                            for s in ctx.report.skipped_unmappable))


class FindLatestBackupTests(FsTestBase):
    """find_latest_backup must consider every tool's backups dir, ordered
    by the manifest's created_at, so consecutive migrations across
    different tools restore the *most recent* one, not the first-seen."""

    def _make_backup(self, root: Path, ts: str) -> Path:
        b = root / "backups" / f"pre-migrate-{ts}"
        b.mkdir(parents=True)
        (b / "manifest.json").write_text(json.dumps({
            "created_at": ts,
            "direction": f"test-{ts}",
            "src_root": str(root),
            "dst_root": str(root),
            "entries": [],
        }))
        return b

    def test_latest_across_tools_wins(self):
        # Pretend we have backups in all three tools' dirs. The cursor one
        # is newest by created_at; it must be returned.
        claude_dir = self.tmp / ".claude"
        codex_dir = self.tmp / ".codex"
        cursor_dir = self.tmp / ".cursor"
        for d in (claude_dir, codex_dir, cursor_dir):
            d.mkdir()

        self._make_backup(claude_dir, "20260101-000000")
        self._make_backup(codex_dir,  "20260102-000000")
        latest_expected = self._make_backup(cursor_dir, "20260103-000000")

        # Patch the search bases to point at our tmp tree.
        import unittest.mock as mock
        with mock.patch.object(m.Path, "home", return_value=self.tmp), \
             mock.patch.object(m.Path, "cwd",  return_value=self.tmp):
            got = m.find_latest_backup()
        self.assertEqual(got, latest_expected)


class CursorFullRoundTripTests(FsTestBase):
    def test_cursor_to_claude_to_cursor_preserves_rule_metadata(self):
        # Original cursor rules with globs + alwaysApply.
        c1 = self.tmp / "c1"
        rules_c1 = c1 / "rules"
        rules_c1.mkdir(parents=True)
        (rules_c1 / "react.mdc").write_text(
            "---\ndescription: React rules\nglobs: src/**/*.tsx\n"
            "alwaysApply: false\n---\nUse hooks.\n")

        # cursor → claude
        cl = self.tmp / "claude"
        cl.mkdir()
        ctx1 = make_ctx(c1, cl)
        m.run_cursor_to_claude(ctx1, {})

        # claude → cursor (different dst dir to avoid the original)
        c2 = self.tmp / "c2"
        c2.mkdir()
        ctx2 = make_ctx(cl, c2)
        m.run_claude_to_cursor(ctx2, {})

        # The react rule's frontmatter should round-trip byte-equivalent.
        result = (c2 / "rules" / "react.mdc").read_text()
        body, fm = m.strip_frontmatter(result)
        self.assertEqual(fm.get("globs"), "src/**/*.tsx")
        self.assertEqual(fm.get("alwaysApply"), "false")
        self.assertEqual(body.strip(), "Use hooks.")

    def test_cursor_metadata_roundtrip_preserves_quotes(self):
        c1 = self.tmp / "c1"
        rules_c1 = c1 / "rules"
        rules_c1.mkdir(parents=True)
        (rules_c1 / "quoted.mdc").write_text(
            '---\ndescription: Use "strict" mode\\paths\n'
            "alwaysApply: true\n---\nBody.\n")

        cl = self.tmp / "claude"
        cl.mkdir()
        m.run_cursor_to_claude(make_ctx(c1, cl), {})

        c2 = self.tmp / "c2"
        c2.mkdir()
        m.run_claude_to_cursor(make_ctx(cl, c2), {})

        _, fm = m.strip_frontmatter((c2 / "rules" / "quoted.mdc").read_text())
        self.assertEqual(fm.get("description"), 'Use "strict" mode\\paths')

    def test_cursor_rule_source_cannot_escape_rules_dir(self):
        cl = self.tmp / "claude"
        cl.mkdir()
        (cl / "CLAUDE.md").write_text(
            "<!-- migrator:begin kind=cursor-rule source=../../outside -->\n"
            "<!-- migrator:cursor-meta json={\"alwaysApply\":\"true\"} -->\n"
            "Body.\n"
            "<!-- migrator:end -->\n")

        c2 = self.tmp / "cursor"
        c2.mkdir()
        m.run_claude_to_cursor(make_ctx(cl, c2), {})

        self.assertTrue((c2 / "rules" / "outside.mdc").exists())
        self.assertFalse((self.tmp / "outside.mdc").exists())


if __name__ == "__main__":
    unittest.main()
