"""Smart approval: jev layered risk assessment for :mod:`tools.approval`.

Fork change: the guardian verdict comes from jev (rule engine + TypeSafe
SystemOne noul probability) instead of an auxiliary-LLM chat call.

Decision contract (``tools.approval`` consumes 'approve' | 'deny' | 'escalate'):

- L1 rules — irreversible destruction (``rm -rf /``, force push, ``drop
  database``, …) → DENY outright; caution patterns → ESCALATE (a human looks);
  everything else with no risk signal → APPROVE.
- L2 TypeSafe — commands with a risk signal that L1 didn't classify ask
  SystemOne for P(destructive): >= 0.90 DENY, >= 0.35 ESCALATE, else APPROVE.
- Any jev failure (no key, network down, timeout, bad payload) → ESCALATE —
  the guardian must fail toward the human, never toward silent approval.

The command text is untrusted — it originates from the primary LLM, which may
itself be prompt-injected. Defenses carried over from the LLM guardian:
shell comments are stripped before assessment, and rules/API see only the
stripped command text (no surrounding conversation to inject into).
Inspired by OpenAI Codex's Smart Approvals guardian subagent.
"""

import logging
import os
import re
import time
from tools import approval_context as _ctx

logger = logging.getLogger("tools.approval")


def _get_smart_policy() -> str:
    """Operator rules (``approvals.smart_policy``) — jev evaluates each line as
    a case-insensitive DENY regex (trusted channel: config.yaml, never the
    command text)."""
    policy = _ctx._get_approval_config().get("smart_policy", "")
    return policy.strip() if isinstance(policy, str) else ""


def _operator_policy_deny(command: str) -> str | None:
    """Each non-empty smart_policy line is a regex; a match DENIES outright
    (operator rules are the strictest channel)."""
    policy = _get_smart_policy()

    if not policy:
        return None
    for line in policy.splitlines():
        pattern = line.strip()

        if not pattern:
            continue
        try:
            if re.search(pattern, command, re.IGNORECASE):
                return f"operator smart_policy rule: {pattern[:80]}"
        except re.error:
            continue  # a bad operator regex degrades to no-op, never a crash
    return None

# TypeSafe probability bands. >= DENY_BAND is a confident destructive call;
# [ESCALATE_BAND, DENY_BAND) goes to a human; below ESCALATE_BAND approves.
DENY_BAND = 0.90
ESCALATE_BAND = 0.35

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in _TRUTHY


def _strip_line_comment(line: str) -> str:
    """Remove a trailing ``# comment`` from one shell line, quote-aware
    (``echo "hello # world"`` survives)."""
    in_single = in_double = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and in_double and i + 1 < len(line):
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i].rstrip()
        i += 1
    return line


def _strip_shell_comments(command: str) -> str:
    lines = []
    for line in (command or "").splitlines():
        if line.lstrip().startswith("#"):
            continue
        lines.append(_strip_line_comment(line))
    return "\n".join(lines)


# --- jev L1: rule engine (mirrors ~/.hermes/plugins/jev) ---------------------

_IRREVERSIBLE: list[tuple[str, str]] = [
    (r"rm\s+[^&|;]*\s+(/|~|\$HOME)(\s|$|/)", "recursive delete of a root/home path"),
    (r"--no-preserve-root", "--no-preserve-root bypass"),
    (r"git\s+push\s+(?!.*--force-with-lease).*--force\b", "force push overwriting remote history"),
    (r"git\s+push\s+-f\b(?!.*force-with-lease)", "force push overwriting remote history"),
    (r"drop\s+(database|schema)\b", "dropping a database"),
    (r"truncate\s+table\b", "truncating a table"),
    (r"delete\s+from\s+\w+\s*;\s*$", "unscoped table delete"),
    (r"(mkfs|dd\s+if=.*of=/dev/)", "raw disk write/format"),
    (r"chmod\s+-R\s+777\s+/", "system-wide open permissions"),
    (r">\s*/dev/sd[a-z]", "direct block-device write"),
    (r":\(\)\{.*\};:", "fork bomb"),
]

_CAUTION: list[tuple[str, str]] = [
    (r"git\s+reset\s+--hard", "hard reset discards uncommitted work"),
    (r"git\s+clean\s+-[a-zA-Z]*f", "git clean deletes untracked files"),
    (r"(npm|pnpm|yarn|bun)\s+publish", "package publish"),
    (r"docker\s+(system|volume)\s+prune", "docker prune"),
    (r"curl\s+[^\|]*\|\s*(ba)?sh", "curl piped to shell"),
    (r"helm\s+(uninstall|delete)", "helm uninstall"),
    (r"terraform\s+(destroy|apply)", "terraform state change"),
    (r"(shutdown|reboot|halt)\b", "shutdown/reboot"),
]

import re  # noqa: E402 — rule tables

_IRREVERSIBLE_RE = [(re.compile(pattern, re.IGNORECASE), why) for pattern, why in _IRREVERSIBLE]
_CAUTION_RE = [(re.compile(pattern, re.IGNORECASE), note) for pattern, note in _CAUTION]
_RISKY_SIGNALS = ("rm", "delete", "drop", "push", "deploy", "prod", "secret", "key",
                  "token", "password", "credential", ".env", "force", "reset", "prune", "kill")


def _rule_classify(command: str) -> str | None:
    """L1: 'deny' | 'escalate' | None (no rule opinion)."""
    for regex, why in _IRREVERSIBLE_RE:
        if regex.search(command):
            logger.info("jev smart: L1 DENY (%s)", why)
            return "deny"
    for regex, _note in _CAUTION_RE:
        if regex.search(command):
            return "escalate"
    return None


# --- jev L2: TypeSafe SystemOne ------------------------------------------------

def _typesafe_destructive_probability(command: str) -> float | None:
    """P(destructive) via SystemOne noul; None on ANY failure (fail-escalate)."""
    key = os.environ.get("TYPESAFE_API_KEY") or ""

    if not key:
        return None
    base = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
    model = os.environ.get("TYPESAFE_MODEL", "jev-latest")
    try:
        import json
        import urllib.request

        body = json.dumps({
            "state": command[:800],
            "model": model,
            "questions": {
                "destructive": {
                    "type": "noul",
                    "instructions": "Is this shell command destructive, irreversible, or dangerous to production/remote state?",
                    "criteria": {
                        "true": "Irreversible deletion, overwriting remote history, leaking secrets, or impacting production",
                        "false": "Read-only, reversible, or scoped to disposable resources",
                    },
                }
            },
        }).encode()
        req = urllib.request.Request(
            f"{base}/v1/systemone",
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        prob = (data.get("answers") or {}).get("destructive") or {}
        value = prob.get("noul")
        return float(value) if isinstance(value, (int, float)) else None
    except Exception as exc:  # noqa: BLE001 — fail toward the human, never silent approval
        logger.warning("jev smart: TypeSafe call failed (%s: %s), escalating",
                       type(exc).__name__, exc)
        return None


def _smart_approve(command: str, description: str) -> str:
    _smart_t0 = time.monotonic()
    try:
        if _env_flag("JEV_SMART_DISABLE"):
            return "escalate"

        stripped = _strip_shell_comments(command)

        # Operator policy (config, trusted channel) is the FIRST gate.
        operator_deny = _operator_policy_deny(stripped)
        if operator_deny is not None:
            logger.info("jev smart: operator policy DENY (%s)", operator_deny)
            return "deny"

        # L1: deterministic rules.
        rule = _rule_classify(stripped)
        if rule is not None:
            logger.debug("jev smart: L1 verdict %s in %.1fs", rule, time.monotonic() - _smart_t0)
            return rule

        # No risk signal at all → approve without spending an API call.
        if not any(signal in stripped.lower() for signal in _RISKY_SIGNALS):
            logger.debug("jev smart: no risk signal, approve in %.1fs", time.monotonic() - _smart_t0)
            return "approve"

        # L2: TypeSafe probability bands.
        prob = _typesafe_destructive_probability(stripped)
        elapsed = time.monotonic() - _smart_t0

        if prob is None:
            logger.warning("jev smart: TypeSafe unavailable after %.1fs, escalating to human", elapsed)
            return "escalate"

        if prob >= DENY_BAND:
            logger.info("jev smart: L2 DENY p=%.2f in %.1fs", prob, elapsed)
            return "deny"
        if prob >= ESCALATE_BAND:
            logger.info("jev smart: L2 ESCALATE p=%.2f in %.1fs", prob, elapsed)
            return "escalate"

        logger.debug("jev smart: L2 approve p=%.2f in %.1fs", prob, elapsed)
        return "approve"
    except Exception as e:  # noqa: BLE001 — the guardian fails toward the human
        logger.warning("jev smart: assessment failed after %.1fs (%s: %s), escalating",
                       time.monotonic() - _smart_t0, type(e).__name__, e)
        return "escalate"


def _smart_verdict(command: str, description: str, pattern_key: str,
                   pattern_keys: list[str], session_key: str) -> str:
    """Run the jev guardian with observer hooks; 'approve' | 'deny' | 'escalate'.
    Same observer contract as the previous LLM guardian — redaction is
    observer-payload preparation, not approval policy; if it fails, skip
    observability rather than leak raw data or block the decision."""
    try:
        from agent.redact import redact_sensitive_text
        payload = {
            "command": redact_sensitive_text(command, force=True),
            "description": redact_sensitive_text(description, force=True),
            "pattern_key": pattern_key, "pattern_keys": list(pattern_keys),
            "session_key": session_key, "surface": "smart",
        }
    except Exception as exc:
        logger.debug("Smart approval hook redaction failed: %s", exc)
        payload = None
    else:
        _ctx._fire_approval_hook("pre_approval_request", **payload)
    verdict = _smart_approve(command, description)
    if payload is not None and verdict in {"approve", "deny"}:
        _ctx._fire_approval_hook("post_approval_response", **payload, choice=f"smart_{verdict}", decided_by="jev")
    return verdict
