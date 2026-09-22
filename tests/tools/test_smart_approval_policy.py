"""Tests for the operator-customizable smart-approval policy (jev guardian).

``approvals.smart_policy`` (config.yaml) holds operator DENY rules. jev
evaluates each line as a case-insensitive regex against the stripped command;
a match denies outright. Security invariants under test:

  1. Empty/missing policy denies nothing on its own.
  2. A policy line matching the command DENIES — even when jev's own layers
     would approve (operator rules are the strictest channel).
  3. A bad operator regex degrades to a no-op instead of crashing the guard.

The policy lives in config.yaml (a TRUSTED channel), never travels with the
untrusted command text.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tools import approval_smart
from tools.approval_smart import _get_smart_policy, _smart_approve

POLICY_TEXT = "Always ESCALATE commands that modify anything under /etc."


@pytest.fixture()
def typesafe_low(monkeypatch):
    """TypeSafe says 'safe' so tests observe the POLICY layer in isolation."""
    monkeypatch.setattr(approval_smart, "_typesafe_destructive_probability", lambda cmd: 0.01)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")


class TestGetSmartPolicy:
    @patch("tools.approval_context._get_approval_config")
    def test_missing_key_returns_empty(self, mock_cfg):
        mock_cfg.return_value = {"mode": "smart"}
        assert _get_smart_policy() == ""

    @patch("tools.approval_context._get_approval_config")
    def test_policy_text_is_stripped(self, mock_cfg):
        mock_cfg.return_value = {"smart_policy": f"  {POLICY_TEXT}\n"}
        assert _get_smart_policy() == POLICY_TEXT


class TestSmartApprovePolicy:
    """The operator policy is the FIRST gate — strictest channel wins."""

    def test_empty_policy_denies_nothing(self, typesafe_low, monkeypatch):
        monkeypatch.setattr(approval_smart, "_get_smart_policy", lambda: "")
        # /etc modification + no policy → falls to signals/typesafe → approve
        assert _smart_approve("touch /etc/nginx/x.conf", "config edit") == "approve"

    def test_policy_line_matching_command_denies(self, typesafe_low, monkeypatch):
        # Policy lines are REGEXES — the shipped POLICY_TEXT is prose, so use
        # a regex line the way a real operator would write it.
        monkeypatch.setattr(approval_smart, "_get_smart_policy", lambda: r"touch\s+/etc/")
        assert _smart_approve("touch /etc/nginx/x.conf", "config edit") == "deny"

    def test_policy_case_insensitive(self, typesafe_low, monkeypatch):
        monkeypatch.setattr(approval_smart, "_get_smart_policy", lambda: "rm\\s+-rf\\s+/srv")
        assert _smart_approve("RM -RF /srv/data", "recursive delete") == "deny"

    def test_policy_non_matching_line_ignored(self, typesafe_low, monkeypatch):
        monkeypatch.setattr(approval_smart, "_get_smart_policy", lambda: "apt-get\\s+install")
        assert _smart_approve("echo hi", "flagged") == "approve"

    def test_bad_operator_regex_degrades_to_noop(self, typesafe_low, monkeypatch):
        monkeypatch.setattr(approval_smart, "_get_smart_policy", lambda: "[unclosed(")
        assert _smart_approve("echo hi", "flagged") == "approve"

    def test_policy_beats_typesafe_approve(self, typesafe_low, monkeypatch):
        # Even with TypeSafe saying 1% destructive, operator policy wins.
        monkeypatch.setattr(approval_smart, "_get_smart_policy", lambda: "/etc/")
        assert _smart_approve("rm -rf /etc/old", "recursive delete") == "deny"

    def test_config_reader_crash_fails_safe(self, monkeypatch):
        def boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(approval_smart, "_get_smart_policy", boom)
        assert _smart_approve("echo hi", "flagged") == "escalate"
