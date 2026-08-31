from types import SimpleNamespace

from agent.text_runtime_router import decide_text_runtime, ensure_local_text_runtime_ready


def test_simple_text_routes_to_local_ollama(monkeypatch):
    monkeypatch.setattr(
        "agent.text_runtime_router.load_config_readonly",
        lambda: {
            "text_runtime_router": {
                "enabled": True,
                "simple_text_max_chars": 120,
                "simple_runtime": {
                    "provider": "ollama",
                    "model": "qwen2.5:7b",
                    "base_url": "http://localhost:11434/v1",
                    "api_key": "ollama",
                    "api_mode": "chat_completions",
                },
                "complex_runtime": {
                    "provider": "custom",
                    "model": "gpt-5.4",
                    "base_url": "https://api.ovocould.vip",
                    "api_key": "",
                    "api_mode": "codex_responses",
                },
            }
        },
    )

    route = decide_text_runtime(
        "把这句话翻译成英文：你好",
        current_runtime={
            "provider": "custom",
            "model": "gpt-5.4",
            "base_url": "https://api.ovocould.vip",
            "api_mode": "codex_responses",
        },
    )

    assert route is not None
    assert route["provider"] == "ollama"
    assert route["model"] == "qwen2.5:7b"
    assert route["route_reason"] == "simple"


def test_complex_text_routes_to_current_gpt_runtime(monkeypatch):
    monkeypatch.setattr(
        "agent.text_runtime_router.load_config_readonly",
        lambda: {
            "text_runtime_router": {
                "enabled": True,
                "simple_text_max_chars": 120,
                "simple_runtime": {
                    "provider": "ollama",
                    "model": "qwen2.5:7b",
                    "base_url": "http://localhost:11434/v1",
                    "api_key": "ollama",
                    "api_mode": "chat_completions",
                },
                "complex_runtime": {
                    "provider": "custom",
                    "model": "gpt-5.4",
                    "base_url": "https://api.ovocould.vip",
                    "api_key": "",
                    "api_mode": "codex_responses",
                },
            }
        },
    )

    route = decide_text_runtime(
        "帮我分析这个报错原因，并给出修复步骤和可能的回归风险",
        current_runtime={
            "provider": "ollama",
            "model": "qwen2.5:7b",
            "base_url": "http://localhost:11434/v1",
            "api_mode": "chat_completions",
        },
    )

    assert route is not None
    assert route["provider"] == "custom"
    assert route["model"] == "gpt-5.4"
    assert route["route_reason"] == "complex"


def test_no_switch_when_runtime_already_matches_target(monkeypatch):
    monkeypatch.setattr(
        "agent.text_runtime_router.load_config_readonly",
        lambda: {
            "text_runtime_router": {
                "enabled": True,
                "simple_text_max_chars": 120,
                "simple_runtime": {
                    "provider": "ollama",
                    "model": "qwen2.5:7b",
                    "base_url": "http://localhost:11434/v1",
                    "api_key": "ollama",
                    "api_mode": "chat_completions",
                },
                "complex_runtime": {
                    "provider": "custom",
                    "model": "gpt-5.4",
                    "base_url": "https://api.ovocould.vip",
                    "api_key": "",
                    "api_mode": "codex_responses",
                },
            }
        },
    )

    route = decide_text_runtime(
        "你好",
        current_runtime={
            "provider": "ollama",
            "model": "qwen2.5:7b",
            "base_url": "http://localhost:11434/v1",
            "api_mode": "chat_completions",
        },
    )

    assert route is None


def test_ensure_local_text_runtime_ready_returns_true_when_loaded(monkeypatch):
    monkeypatch.setattr(
        "agent.text_runtime_router._ollama_model_loaded",
        lambda model: True,
    )

    assert ensure_local_text_runtime_ready(
        {
            "provider": "ollama",
            "model": "qwen2.5:7b",
            "base_url": "http://localhost:11434/v1",
        }
    ) is True


def test_ensure_local_text_runtime_ready_warms_installed_model(monkeypatch):
    monkeypatch.setattr(
        "agent.text_runtime_router._ollama_model_loaded",
        lambda model: False,
    )
    monkeypatch.setattr(
        "agent.text_runtime_router._ollama_model_installed",
        lambda model: True,
    )
    monkeypatch.setattr(
        "agent.text_runtime_router._warm_ollama_model",
        lambda base_url, model: True,
    )

    assert ensure_local_text_runtime_ready(
        {
            "provider": "ollama",
            "model": "qwen2.5:7b",
            "base_url": "http://localhost:11434/v1",
        }
    ) is True


def test_ensure_local_text_runtime_ready_pulls_missing_model(monkeypatch):
    loaded_states = iter([False, False])
    monkeypatch.setattr(
        "agent.text_runtime_router._ollama_model_loaded",
        lambda model: next(loaded_states),
    )
    monkeypatch.setattr(
        "agent.text_runtime_router._ollama_model_installed",
        lambda model: False,
    )
    monkeypatch.setattr(
        "agent.text_runtime_router._run_ollama_command",
        lambda args, timeout=60: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        "agent.text_runtime_router._warm_ollama_model",
        lambda base_url, model: True,
    )

    assert ensure_local_text_runtime_ready(
        {
            "provider": "ollama",
            "model": "qwen2.5:7b",
            "base_url": "http://localhost:11434/v1",
        }
    ) is True
