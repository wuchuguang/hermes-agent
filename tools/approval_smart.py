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
from tools.approval_detection import _scan_shell

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


def _next_comment(text: str, start: int = 0) -> int | None:
    """Offset of the next comment and its unquoted padding, or None.

    An in-word hash (``echo a#; next``) is data, so stripping there would hide
    executable operations from the reviewer. Quoted/escaped blanks and operators
    are part of the word too; looking only at the previous character is unsafe.
    Quote state carries across lines, so a ``#`` inside a multi-line string stays
    data. *start* must sit outside any quote (the end of a previous comment does).
    """
    word_start = True
    padding = None
    for kind, i, _, quote in _scan_shell(text, start, subst="uq", brace=True):
        unquoted = kind == "char" and quote is None
        if unquoted and text[i] == "#" and word_start:
            return i if padding is None else padding
        # Only ordinary blanks can be trimmed. An escaped blank is shell data;
        # deleting it can expose a backslash and join two executable lines.
        if unquoted and text[i] in " \t":
            if padding is None:
                padding = i
        else:
            padding = None
        word_start = unquoted and text[i] in " \t\n;&|()<>"
    return None


def _comment_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every comment, each running to (not including) its newline."""
    spans: list[tuple[int, int]] = []
    start = _next_comment(text)
    while start is not None:
        end = text.find("\n", start)
        end = len(text) if end < 0 else end
        spans.append((start, end))
        start = _next_comment(text, end) if end < len(text) else None
    return spans


def _has_heredoc(text: str, comments: list[tuple[int, int]]) -> bool:
    """Whether an unquoted ``<<``/``<<-`` operator (not a ``<<<`` here-string) appears outside comments."""
    pos = 0
    # Comments cannot open quotes or substitutions in the following shell code.
    # Each comment boundary is unquoted, so restarting after it is safe.
    for start, end in [*comments, (len(text), len(text))]:
        after_lt = False  # an escaped '<' is word data, not the first '<' of a '<<<' here-string
        for kind, i, _, quote in _scan_shell(text, pos, start, subst="uq", brace=True):
            operator = kind == "char" and quote is None
            if operator and text.startswith("<<", i) and not text.startswith("<<<", i) and not after_lt:
                return True
            after_lt = operator and text[i] == "<"
        pos = end
    return False


def _strip_line_comment(line: str) -> str:
    """Strip a comment only at an unquoted shell word boundary (single-line form)."""
    start = _next_comment(line)
    return line if start is None else line[:start]


def _strip_shell_comments(command: str) -> str:
    """Strip shell comments before LLM assessment.

    This is a word/quote-aware heuristic, not a full shell or heredoc parser.
    Quote state carries across lines. Commands containing expansion, extglob,
    arithmetic-command, assignment, conditional or special-quote markers, or a heredoc, are preserved
    verbatim: the shared scanner cannot establish word boundaries in those contexts,
    and a heredoc body is data (``execute_code`` wraps its Python script as one).
    """
    # Even quoted/escaped markers take this conservative path. Trying to classify
    # them here could miss nested or multiline substitutions and hide executable
    # suffixes. The guardian's untrusted-input instructions still apply to comments.
    # Detect markers split by line continuations without changing the input.
    # Extglob may be enabled by an earlier line or inherited shell state; its
    # closing ')' belongs to the word, so a following '#' can still be data.
    # Assignment words and [[...]] regexes also have non-shell word boundaries.
    # Bare ((...)) is distinct from $((...)); arithmetic errors do not prevent
    # a later command from running, so invalid expressions must be kept too.
    marker_text = command.replace("\\\n", "")
    if any(marker in marker_text for marker in (
        "<(", ">(", "$(", "$[", "${", "$'", '$"', "`", "@(", "?(", "*(", "+(", "!(", "=(", "[[", "((",
    )):
        return command
    # A continuation can split a heredoc operator. Do not strip the body while
    # waiting for the raw scanner to recognize adjacent '<' characters. Like
    # the marker fallback above, quoted markers and here-strings may overmatch.
    if marker_text != command and "<<" in marker_text:
        return command

    spans = _comment_spans(command)
    if not spans:
        return command
    if _has_heredoc(command, spans):
        return command

    kept: list[str] = []
    pos = 0
    has_content = False
    for start, end in spans:
        piece = command[pos:start]
        kept.append(piece)
        has_content = has_content or bool(piece)
        pos = end
        # Drop leading comments only. Later newlines may terminate a command
        # whose preceding physical line ended in a backslash continuation.
        if pos < len(command) and not has_content:
            pos += 1
    kept.append(command[pos:])
    return "".join(kept)


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
    except Exception as exc:
        logger.warning("jev smart: TypeSafe call failed (%s: %s), escalating",
                       type(exc).__name__, exc, exc_info=True)
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
    except Exception as e:
        logger.warning("jev smart: assessment failed after %.1fs (%s: %s), escalating",
                       time.monotonic() - _smart_t0, type(e).__name__, e, exc_info=True)
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
