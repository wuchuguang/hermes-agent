"""The fork's jev guardian does not spend an auxiliary reasoning-model call.

Approval reasoning settings belong to the upstream LLM guardian. They cannot
change jev's deterministic rules or its fail-to-human TypeSafe fallback.
"""

from unittest.mock import patch

import pytest

from tools import approval_smart


@pytest.mark.parametrize("task_config", [
    {},
    {"reasoning_effort": "high"},
    {"extra_body": {"reasoning": {"enabled": True, "effort": "high"}}},
])
def test_jev_verdict_does_not_call_auxiliary_reasoning_model(monkeypatch, task_config):
    monkeypatch.delenv("JEV_SMART_DISABLE", raising=False)
    monkeypatch.setattr(approval_smart, "_get_smart_policy", lambda: "")
    with (
        patch("agent.auxiliary_task_config._get_auxiliary_task_config", return_value=task_config),
        patch("agent.auxiliary_client.call_llm") as call_llm,
        patch.object(approval_smart, "_typesafe_destructive_probability", return_value=None) as probability,
    ):
        assert approval_smart._smart_approve("python -c \"print('hi')\"", "script execution") == "approve"
        assert approval_smart._smart_approve("rm -rf /", "recursive delete") == "deny"
        assert approval_smart._smart_approve("git push origin main", "remote write") == "escalate"
        probability.assert_called_once_with("git push origin main")
        call_llm.assert_not_called()
