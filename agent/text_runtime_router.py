"""主对话文本运行时自动路由。

目标：
- 简单文本处理优先走本地 Ollama。
- 稍复杂或明确需要推理/规划/代码/多步分析的请求继续走主模型。

设计原则：
- 只做轻量规则判断，不额外触发一次 LLM 分类。
- 只在当前回合临时覆盖运行时，不改用户主模型配置。
- 配置缺失或本地路由不可用时静默回退到主模型。
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from hermes_cli.config import load_config_readonly
from hermes_cli.providers import determine_api_mode


_COMPLEX_KEYWORDS = (
    "代码", "编程", "调试", "修复", "报错", "异常", "堆栈", "traceback",
    "测试", "重构", "review", "审查", "架构", "设计", "方案", "计划",
    "分析", "深入", "详细", "解释", "原理", "为什么", "推导", "证明",
    "比较", "对比", "总结", "归纳", "提炼", "研究", "调研", "检索",
    "工具", "命令", "脚本", "部署", "配置", "数据库", "sql", "接口",
    "api", "bug", "issue", "pr", "commit", "diff", "patch",
)

_SIMPLE_PREFIXES = (
    "翻译", "改写", "润色", "纠错", "纠正", "校对", "缩写", "扩写",
    "提取", "抽取", "分类", "打标签", "命名", "取标题", "起标题",
    "一句话", "一段话", "回复一个词", "回复一句", "简单回复",
)

_SIMPLE_EXACT = {
    "你好", "hi", "hello", "早", "早上好", "中午好", "晚上好",
    "谢谢", "ok", "okay", "好的", "嗯", "是", "否",
}


def _read_router_config() -> Dict[str, Any]:
    cfg = load_config_readonly()
    router = cfg.get("text_runtime_router")
    if isinstance(router, dict):
        return router
    return {}


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _normalize_runtime_entry(entry: Any) -> Dict[str, str]:
    if not isinstance(entry, dict):
        return {}
    provider = str(entry.get("provider") or "").strip()
    model = str(entry.get("model") or "").strip()
    base_url = str(entry.get("base_url") or "").strip()
    api_key = str(entry.get("api_key") or "").strip()
    api_mode = str(entry.get("api_mode") or "").strip()
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "api_mode": api_mode,
    }


def _run_ollama_command(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ollama", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _ollama_model_loaded(model: str) -> bool:
    result = _run_ollama_command(["ps"], timeout=15)
    if result.returncode != 0:
        return False
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    return any(line.startswith(model) for line in lines[1:])


def _ollama_model_installed(model: str) -> bool:
    result = _run_ollama_command(["list"], timeout=20)
    if result.returncode != 0:
        return False
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    return any(line.startswith(model) for line in lines[1:])


def _warm_ollama_model(base_url: str, model: str) -> bool:
    endpoint = base_url.rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = endpoint[:-3]
    req = urllib.request.Request(
        endpoint + "/api/generate",
        data=json.dumps(
            {
                "model": model,
                "prompt": "hi",
                "stream": False,
                "keep_alive": "10m",
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return 200 <= getattr(response, "status", 200) < 300
    except (OSError, urllib.error.URLError):
        return False


def ensure_local_text_runtime_ready(runtime: Dict[str, str]) -> bool:
    """按需确保本地 Ollama 文本模型已装载。

    仅对 ``provider=ollama`` 生效；其它 provider 直接返回 True。
    """
    provider = str(runtime.get("provider") or "").strip().lower()
    model = str(runtime.get("model") or "").strip()
    base_url = str(runtime.get("base_url") or "").strip()
    if provider != "ollama" or not model or not base_url:
        return True

    if _ollama_model_loaded(model):
        return True

    if not _ollama_model_installed(model):
        pull = _run_ollama_command(["pull", model], timeout=1800)
        if pull.returncode != 0:
            return False

    if _ollama_model_loaded(model):
        return True

    return _warm_ollama_model(base_url, model)


def _estimate_text_complexity(message: str, *, short_length: int) -> str:
    text = (message or "").strip()
    if not text:
        return "simple"

    lowered = text.lower()
    if lowered in _SIMPLE_EXACT:
        return "simple"

    if any(lowered.startswith(prefix) for prefix in _SIMPLE_PREFIXES):
        if len(text) <= max(short_length * 2, 80):
            return "simple"

    if any(keyword in lowered for keyword in _COMPLEX_KEYWORDS):
        return "complex"

    line_count = text.count("\n") + 1
    if line_count >= 4:
        return "complex"

    if len(text) > short_length:
        return "complex"

    punctuation_hits = sum(lowered.count(mark) for mark in ("？", "?", "；", ";", "：", ":"))
    if punctuation_hits >= 3:
        return "complex"

    return "simple"


def decide_text_runtime(
    user_message: str,
    *,
    current_runtime: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, str]]:
    """根据当前用户文本决定是否切换本回合主模型运行时。

    返回：
    - ``None``：保持当前主运行时不变
    - ``dict``：切换到该运行时
    """
    router = _read_router_config()
    if not _coerce_bool(router.get("enabled"), False):
        return None

    short_length = int(router.get("simple_text_max_chars") or 120)
    task_kind = _estimate_text_complexity(user_message, short_length=short_length)

    target_key = "simple_runtime" if task_kind == "simple" else "complex_runtime"
    target = _normalize_runtime_entry(router.get(target_key))
    if not target.get("provider") or not target.get("model"):
        return None

    current = current_runtime or {}
    current_provider = str(current.get("provider") or "").strip().lower()
    current_model = str(current.get("model") or "").strip()
    current_base_url = str(current.get("base_url") or "").strip()
    current_api_mode = str(current.get("api_mode") or "").strip()

    target_provider = target["provider"].strip().lower()
    target_model = target["model"]
    target_base_url = target["base_url"]
    target_api_mode = target["api_mode"] or determine_api_mode(target_provider, target_base_url)

    if (
        current_provider == target_provider
        and current_model == target_model
        and current_base_url == target_base_url
        and current_api_mode == target_api_mode
    ):
        return None

    target["provider"] = target_provider
    target["api_mode"] = target_api_mode
    target["route_reason"] = task_kind
    return target


__all__ = ["decide_text_runtime", "ensure_local_text_runtime_ready"]
