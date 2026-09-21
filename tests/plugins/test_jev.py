"""jev 插件单测 — 规则引擎、分层判断、fail-open 合同。

跑法（repo root）：
  scripts/run_tests.sh tests/plugins/test_jev.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_PLUGIN_DIR = Path.home() / ".hermes" / "plugins" / "jev"


def _load_jev():
    spec = importlib.util.spec_from_file_location("jev_plugin", _PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["jev_plugin"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def jev(monkeypatch):
    module = _load_jev()
    monkeypatch.setattr(module, "_cfg", lambda: {"enabled": True, "block": True, "llm_advisory": False})
    return module


class TestRuleVerdict:
    def test_read_only_commands_pass_through(self, jev):
        assert jev.rule_verdict("bash", {"command": "git status"}) is None
        assert jev.rule_verdict("bash", {"command": "ls -la ~/.hermes"}) is None

    def test_irreversible_blocked(self, jev):
        out = jev.rule_verdict("bash", {"command": "git push --force origin main"})
        assert out is not None and out["action"] == "block"
        assert "jev" in out["message"]

    def test_rm_rf_root_blocked(self, jev):
        out = jev.rule_verdict("bash", {"command": "rm -rf / --no-preserve-root"})
        assert out is not None and out["action"] == "block"

    def test_drop_database_blocked(self, jev):
        out = jev.rule_verdict("bash", {"command": "psql -c 'drop database prod'"})
        assert out is not None and out["action"] == "block"

    def test_caution_advises_not_blocks(self, jev):
        out = jev.rule_verdict("bash", {"command": "git reset --hard HEAD~1"})
        assert out is not None and out["action"] == "advise"

    def test_force_with_lease_not_blocked(self, jev):
        assert jev.rule_verdict("bash", {"command": "git push --force-with-lease origin main"}) is None

    def test_empty_args_no_opinion(self, jev):
        assert jev.rule_verdict("write_file", {"path": "a.txt", "content": "x"}) is None


class TestFailOpen:
    def test_disabled_plugin_never_blocks(self, jev, monkeypatch):
        monkeypatch.setattr(jev, "_cfg", lambda: {"enabled": False})
        assert jev._on_pre_tool_call("bash", {"command": "rm -rf /"}) is None

    def test_env_kill_switch(self, jev, monkeypatch):
        monkeypatch.setenv("JEV_DISABLE", "1")
        assert jev._on_pre_tool_call("bash", {"command": "rm -rf /"}) is None

    def test_block_off_degrades_to_advise(self, jev, monkeypatch):
        monkeypatch.setattr(jev, "_cfg", lambda: {"enabled": True, "block": False})
        assert jev._on_pre_tool_call("bash", {"command": "git push --force origin main"}) is None
        result = jev._on_transform_tool_result("bash", {"command": "git push --force origin main"}, "pushed")
        assert result is not None and "jev" in result


class TestAdvisoryTransform:
    def test_advise_rides_result(self, jev):
        out = jev._on_transform_tool_result("bash", {"command": "git clean -fd"}, "removed 3 files")
        assert out is not None and "git clean" in out

    def test_clean_result_untouched(self, jev):
        assert jev._on_transform_tool_result("bash", {"command": "git status"}, "clean") is None


class TestLLMAdvisory:
    def test_disabled_by_config(self, jev):
        jev._cfg = lambda: {"enabled": True, "llm_advisory": False}
        assert jev.llm_advisory("bash", {"command": "rm backup.tar.gz"}) is None

    def test_no_api_key_fails_open(self, jev, monkeypatch):
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        assert jev.llm_advisory("bash", {"command": "rm backup.tar.gz"}) is None

    def test_no_risk_signal_skips_api(self, jev, monkeypatch):
        calls = []

        def fake_ask(command, tool_name, timeout=6.0):
            calls.append(command)

            return 0.9

        monkeypatch.setattr(jev, "_ask_typesafe", fake_ask)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test")
        assert jev.llm_advisory("bash", {"command": "echo hello"}) is None
        assert calls == []

    def test_typesafe_risky_flags_advisory(self, jev, monkeypatch):
        monkeypatch.setattr(jev, "_cfg", lambda: {"enabled": True, "llm_advisory": True})

        def fake_ask(command, tool_name, timeout=6.0):
            return 0.91

        monkeypatch.setattr(jev, "_ask_typesafe", fake_ask)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test")
        out = jev.llm_advisory("bash", {"command": "rm /srv/app/tmp.dump"})
        assert out is not None and "TypeSafe" in out and "91%" in out

    def test_typesafe_low_prob_is_silent(self, jev, monkeypatch):
        def fake_ask(command, tool_name, timeout=6.0):
            return 0.05

        monkeypatch.setattr(jev, "_ask_typesafe", fake_ask)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test")
        assert jev.llm_advisory("bash", {"command": "rm /tmp/build.lock"}) is None

    def test_api_down_is_silent(self, jev, monkeypatch):
        def boom(command, tool_name, timeout=6.0):
            raise OSError("connection refused")

        monkeypatch.setattr(jev, "_ask_typesafe", boom)
        monkeypatch.setenv("TYPESAFE_API_KEY", "test")
        assert jev.llm_advisory("bash", {"command": "rm /srv/app/tmp.dump"}) is None


class TestJevDecide:
    def test_six_tuple_structure(self, jev):
        out = jev._jev_decide_handler(task="drop the staging database", context="staging is broken")
        assert out["framework"] == "jev-6t"
        assert len(out["fields"]) == 6
        assert any("做不做" in key for key in out["fields"])

    def test_long_inputs_truncated(self, jev):
        out = jev._jev_decide_handler(task="x" * 2000)
        assert len(out["task"]) <= 500


class TestRegister:
    def test_register_wires_hooks_and_tool(self, jev):
        registered = {"hooks": [], "tools": []}

        class Ctx:
            def register_hook(self, name, cb):
                registered["hooks"].append(name)

            def register_tool(self, **kwargs):
                registered["tools"].append(kwargs["name"])

        jev.register(Ctx())

        assert registered["hooks"] == ["pre_tool_call", "transform_tool_result"]
        assert registered["tools"] == ["jev_decide"]
