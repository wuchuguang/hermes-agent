from hermes_cli.model_switch import switch_model


_MOCK_VALIDATION = {
    "accepted": True,
    "persist": True,
    "recognized": True,
    "message": None,
}


def test_switch_model_from_custom_cloud_to_deepseek_provider(monkeypatch):
    """Known cloud models should not stay pinned to a non-local custom endpoint.

    Regression: switching from a saved custom endpoint (e.g. ovo_cloud) to
    ``deepseek-v4-pro`` kept the provider as ``custom`` and later fell through
    to an OpenRouter runtime with no OPENROUTER_API_KEY, yielding HTTP 401
    "Missing Authentication header" even though DEEPSEEK_API_KEY existed.
    """

    captured_runtime_requests = []

    def fake_runtime_provider(**kwargs):
        captured_runtime_requests.append(kwargs)
        return {
            "api_key": "deepseek-test-key",
            "base_url": "https://api.deepseek.com",
            "api_mode": "chat_completions",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        fake_runtime_provider,
    )
    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model",
        lambda *a, **k: _MOCK_VALIDATION,
    )
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.model_switch.get_model_capabilities",
        lambda *a, **k: None,
    )

    result = switch_model(
        raw_input="deepseek-v4-pro",
        current_provider="custom",
        current_model="gpt-5.4",
        current_base_url="https://ai.ovocloud.vip",
        current_api_key="ovo-test-key",
        user_providers={},
        custom_providers=[
            {
                "name": "ovo_cloud",
                "base_url": "https://ai.ovocloud.vip",
                "api_mode": "codex_responses",
                "models": {
                    "gpt-5.4": {"context_length": 131072},
                },
            }
        ],
    )

    assert result.success is True
    assert result.target_provider == "deepseek"
    assert result.new_model == "deepseek-v4-pro"
    assert result.base_url == "https://api.deepseek.com"
    assert result.api_key == "deepseek-test-key"
    assert captured_runtime_requests == [
        {"requested": "deepseek", "target_model": "deepseek-v4-pro"}
    ]
