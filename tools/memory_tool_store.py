"""MemoryStore — bounded, file-backed curated memory (MEMORY.md / USER.md).
Entries are joined by ``ENTRY_DELIMITER``; budgets are in chars (model-independent).
Module state that tests monkeypatch (``get_memory_dir``, ``fcntl``/``msvcrt``) stays
in ``tools.memory_tool`` and is read lazily."""

import logging
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils import atomic_write_text
from tools.threat_patterns import first_threat_message as _first_threat_message

logger = logging.getLogger("tools.memory_tool")

# Block header prefixes rendered by _render_block; agent/conversation_compression.py
# matches them to detect a leftover block for an emptied target — keep in lockstep.
MEMORY_BLOCK_HEADERS = {
    "memory": "MEMORY (your personal notes)", "user": "USER PROFILE (who the user is)",
    "project": "PROJECT MEMORY (facts scoped to this repo)"}

# Linked project memory (fork): cap on dependency repos recalled read-only.
MAX_LINKED_PROJECTS = 5

ENTRY_DELIMITER = "\n§\n"


def _scan_memory_content(content: str) -> Optional[str]:
    """Error string if *content* matches injection/exfil patterns. Strict scope:
    memory enters the system prompt, so a poisoned entry persists across sessions."""
    return _first_threat_message(content, scope="strict")


def _error(message: str, **extra) -> Dict[str, Any]:
    return {"success": False, "error": message, **extra}


def _drift_error(path: Path, bak_path: str) -> Dict[str, Any]:
    """External drift: the file wouldn't round-trip, so flushing would discard content."""
    return _error((
        f"Refusing to write {path.name}: file on disk has content that wouldn't round-trip "
        f"through the memory tool (likely added by the patch tool, a shell append, a manual edit, "
        f"or a concurrent session). A snapshot was saved to {bak_path}. Resolve the drift first — "
        f"either rewrite the file as a clean §-delimited list of entries, or move the extra "
        f"content out — then retry. This guard exists to prevent silent data loss (issue #26045)."
    ), drift_backup=bak_path, remediation=(
        "Open the .bak file, integrate the missing entries into the memory tool one at a time via "
        "memory(action=add, content=...), then remove or rewrite the original file to a clean state."))


def _read_failed_error(path: Path) -> Dict[str, Any]:
    """Existing-but-unreadable file: saving from an assumed-empty view would wipe it."""
    return _error(
        f"Refusing to write {path.name}: the file exists on disk but could not be read right now "
        f"(temporarily locked by another program, a permission change, invalid/corrupt text encoding, "
        f"or a filesystem error). Treating an unreadable file as empty and saving would wipe existing "
        f"memory, so the write is refused. Nothing was changed — retry in a moment.")


def _find_unique_match(entries: List[str], old_text: str) -> Tuple[Optional[int], bool]:
    """``(index, ambiguous)`` for entries containing *old_text*. Exact-duplicate
    matches are safe (first wins); distinct matches → ``(None, True)``."""
    matches = [i for i, e in enumerate(entries) if old_text in e]
    if len({entries[i] for i in matches}) > 1:
        return None, True
    return (matches[0] if matches else None), False


class MemoryStore:
    """Bounded curated memory with file persistence; one instance per AIAgent.
    ``_system_prompt_snapshot`` is frozen at load time (prefix-cache stable);
    ``memory_entries`` / ``user_entries`` are live state persisted to disk."""

    # Failed consolidation attempts (overflow / zero-match) allowed per turn before
    # a TERMINAL "save skipped" result, so a fragile replace/add can't loop the turn
    # to budget exhaustion and suppress the user's reply.
    # See #42405.
    _MAX_CONSOLIDATION_FAILURES_PER_TURN = 3

    def __init__(self, memory_char_limit: int = 2200, user_char_limit: int = 1375, *,
                 memory_enabled: bool = True, user_profile_enabled: bool = True,
                 project_char_limit: int = 2000):
        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
        # Project store (fork): per-repo entries in memories/projects/<slug>.md.
        self.project_entries: List[str] = []
        self.project_char_limit = project_char_limit
        self._project_root = None  # frozen at load_from_disk()
        # Linked project memory (fork v2): read-only recall of dependency
        # repos' project memory. [(dep_name, dep_root)] + their entries.
        self._linked_projects: List[Tuple[str, Path]] = []
        self._linked_entries: List[Tuple[str, List[str]]] = []
        self.memory_char_limit, self.user_char_limit = memory_char_limit, user_char_limit
        self.memory_enabled, self.user_profile_enabled = memory_enabled, user_profile_enabled
        self._system_prompt_snapshot: Dict[str, str] = {"memory": "", "user": "", "project": "", "linked_projects": ""}
        self._consolidation_failures = 0  # per turn; reset by reset_consolidation_failures()

    # Per-turn counter of failed at-capacity consolidation attempts; reset at each turn boundary by
    # reset_consolidation_failures() (#42405).
    def target_enabled(self, target: str) -> bool:
        if target == "user":
            return self.user_profile_enabled
        if target == "project":
            return True  # project store is always on when a root resolves
        return self.memory_enabled

    def reset_consolidation_failures(self) -> None:
        """Call at turn start."""
        self._consolidation_failures = 0

    # ── Project store (fork) ─────────────────────────────────────────────
    # The 'project' target reuses the generic mutate/load machinery above via
    # _path_for/_entries_for/_char_limit overrides; only root resolution, the
    # no-project guard, and linked-project resolution live here.

    def _no_project_error(self) -> Dict[str, Any]:
        from tools.memory_tool_project import NO_PROJECT_ERROR
        return dict(NO_PROJECT_ERROR)

    def _path_for_target(self, target: str) -> Path:
        """Instance-aware path lookup covering all three targets."""
        return self._path_for(target)

    def _path_for_project(self) -> Path:
        """File path for the project store; requires a resolved project root."""
        if self._project_root is None:
            raise RuntimeError(
                "No project root for this session — the 'project' target is "
                "unavailable. Use target='memory' for global notes instead."
            )
        from tools.memory_tool_project import project_slug
        from tools import memory_tool
        return memory_tool.get_memory_dir() / "projects" / f"{project_slug(str(self._project_root))}.md"


    def _resolve_linked_projects(self, mem_dir: Path) -> List[Tuple[str, Path]]:
        """Map the project's declared deps to local repos with memory files.

        Resolution is name-based: scan the project root's parent directory
        (sibling repos — the common monorepo layout) plus ``memory.project_code_roots``
        from config (for sibling-dir layouts like ~/git-repo), reading each
        child's ``package.json`` name / ``pyproject.toml`` name. A dependency
        links only if (a) a local repo with that package name exists and
        (b) it already has a project memory file to recall. Skipped entirely
        when no project memory files exist yet (zero scan cost cold start).
        Read-only: linked files are never written through this store.
        """
        if self._project_root is None:
            return []
        from tools.memory_tool_project import (
            _package_name_for_root, _project_dependency_names, _project_slug)
        from tools.memory_tool import get_builtin_memory_config
        proj_dir = mem_dir / "projects"
        try:
            if not any(proj_dir.glob("*.md")):
                return []
        except OSError:
            return []
        dep_names = set(_project_dependency_names(self._project_root))
        if not dep_names:
            return []
        roots: List[Path] = []
        parent = self._project_root.parent
        if parent.is_dir() and parent != self._project_root:
            roots.append(parent)
        try:
            from hermes_cli.config import load_config

            cfg = load_config() or {}
            for raw in (get_builtin_memory_config(cfg).get("project_code_roots") or []):
                p = Path(str(raw)).expanduser()
                if p.is_dir() and p not in roots:
                    roots.append(p)
        except Exception:
            pass
        name_to_root: Dict[str, Path] = {}
        for root in roots:
            try:
                children = sorted(root.iterdir())
            except OSError:
                continue
            for child in children:
                if not child.is_dir() or child == self._project_root:
                    continue
                if child.name in {".git", "node_modules"} or child.name.startswith("."):
                    continue
                pkg_name = _package_name_for_root(child)
                if pkg_name and pkg_name not in name_to_root:
                    name_to_root[pkg_name] = child
        linked: List[Tuple[str, Path]] = []
        for dep in _project_dependency_names(self._project_root):
            root = name_to_root.get(dep)
            if root is None or root == self._project_root:
                continue
            if (proj_dir / f"{_project_slug(root)}.md").is_file():
                linked.append((dep, root))
            if len(linked) >= MAX_LINKED_PROJECTS:
                break
        return linked


    def _render_linked_block(self, parts: List[str]) -> str:
        """Render the LINKED PROJECT MEMORY block (fork).

        Read-only recall of dependency projects' project memory. Explicitly
        labeled read-only so the model treats these facts as belonging to the
        dependency's own store, not writable from this session.
        """
        if not parts or self._project_root is None:
            return ""
        separator = "═" * 46
        header = (
            f"LINKED PROJECT MEMORY (read-only recall, {self._project_root}) — "
            f"facts learned inside dependency projects of this repo. They are "
            f"stored in each dependency's own project memory; do NOT write to "
            f"them from this session."
        )
        return f"{separator}\n{header}\n{separator}\n" + "\n".join(parts)


    def _linked_slug_for(self, root_path):
        from tools.memory_tool_project import project_slug
        return project_slug(str(root_path))

    def _consolidation_failure(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Count a consolidation failure: under the per-turn cap return ``response``
        (it says how to retry); past it a TERMINAL result so the model stops looping.

        Once the cap is exceeded, drop the retry instruction and return a TERMINAL result so the model stops
        looping memory calls and proceeds to answer the user — a failed memory side effect must never block
        the turn's reply (#42405).
        """
        self._consolidation_failures += 1
        if self._consolidation_failures <= self._MAX_CONSOLIDATION_FAILURES_PER_TURN:
            return response
        return {"success": False, "done": True, "error": (
            f"Memory consolidation failed {self._consolidation_failures} times this turn. Stop retrying "
            "memory calls — leave memory unchanged for now and continue with your reply to the user. "
            "The fact can be saved in a later turn.")}

    def load_from_disk(self):
        """Load MEMORY.md / USER.md and capture the frozen system-prompt snapshot.
        Threat hits are replaced by a ``[BLOCKED: …]`` placeholder in the SNAPSHOT only;
        live lists keep the raw text so the user can see and remove poisoned entries
        (dropping them silently would hide the attack)."""
        from tools.threat_patterns import scan_for_threats

        def _sanitize(entry, filename):
            # Strict scope, same as writes; empty / already-blocked entries pass through.
            findings = scan_for_threats(entry, scope="strict") if entry and not entry.startswith("[BLOCKED:") else None
            if not findings:
                return entry
            logger.warning("Memory entry from %s blocked at load time: %s", filename, ", ".join(findings))
            return (f"[BLOCKED: {filename} entry contained threat pattern(s): {', '.join(findings)}. "
                    f"Removed from system prompt; use memory(action=remove) to delete the original.]")

        # Project store (fork): freeze the project root here (same
        # frozen-at-load lifecycle as the prompt snapshot). The seam lives on
        # tools.memory_tool (re-exported from memory_tool_project) so tests
        # can pin the session root; the real resolution uses the agent's
        # logical cwd (session override → TERMINAL_CWD → os.getcwd()).
        from tools import memory_tool as _memory_tool
        self._project_root = _memory_tool._project_root_for_session()

        targets = ("memory", "user", "project")
        for target in targets:
            if target == "project" and self._project_root is None:
                self.project_entries = []
                self._system_prompt_snapshot["project"] = ""
                continue
            path = self._path_for(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Deduplicate (order-preserving, first occurrence wins).
            entries = list(dict.fromkeys(self._read_file(path)))
            self._set_entries(target, entries)
            self._system_prompt_snapshot[target] = self._render_block(target, [_sanitize(e, path.name) for e in entries])

        # Linked project memory (fork v2): read-only snapshot from dependency repos.
        self._linked_projects = []
        self._linked_entries = []
        linked_snapshot = ""
        if self._project_root is not None:
            try:
                mem_dir = self._path_for("memory").parent
                self._linked_projects = self._resolve_linked_projects(mem_dir)
                for dep_name, dep_root in self._linked_projects:
                    entries = self._read_file(
                        mem_dir / "projects" / f"{self._linked_slug_for(dep_root)}.md")
                    if entries:
                        self._linked_entries.append((dep_name, entries))
                parts = [self._render_block("project", entries) for _, entries in self._linked_entries]
                linked_snapshot = self._render_linked_block(parts)
            except Exception:
                self._linked_projects, self._linked_entries, linked_snapshot = [], [], ""
        self._system_prompt_snapshot["linked_projects"] = linked_snapshot

    @staticmethod
    @contextmanager
    def _file_lock(path: Path):
        """Exclusive lock on a separate .lock file so the memory file itself can
        still be atomically replaced."""
        from tools import memory_tool as _mt  # fcntl/msvcrt live (and are patched) there
        fcntl, msvcrt = _mt.fcntl, _mt.msvcrt
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None and msvcrt is None:
            yield
            return
        with open(lock_path, "a+", encoding="utf-8") as fd:
            def _flock(unlock: bool):
                if fcntl:
                    fcntl.flock(fd, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX)
                else:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK if unlock else msvcrt.LK_LOCK, 1)
            _flock(False)
            try:
                yield
            finally:
                with suppress(OSError):
                    _flock(True)

    def _path_for(self, target: str) -> Path:
        from tools import memory_tool  # get_memory_dir is monkeypatched there
        if target == "project":
            # Project files are keyed by the session's frozen project root;
            # resolution + no-root guard live in _path_for_project.
            return self._path_for_project()
        return memory_tool.get_memory_dir() / ("USER.md" if target == "user" else "MEMORY.md")

    def _entries_for(self, target: str) -> List[str]:
        if target == "project":
            return self.project_entries
        return self.user_entries if target == "user" else self.memory_entries

    def _set_entries(self, target: str, entries: List[str]):
        if target == "project":
            self.project_entries = entries
            return
        setattr(self, "user_entries" if target == "user" else "memory_entries", entries)

    def _char_count(self, target: str) -> int:
        return len(ENTRY_DELIMITER.join(self._entries_for(target)))

    def _char_limit(self, target: str) -> int:
        if target == "project":
            return self.project_char_limit
        return self.user_char_limit if target == "user" else self.memory_char_limit

    def _usage(self, target: str) -> str:
        return f"{self._char_count(target):,}/{self._char_limit(target):,}"

    def _usage_pct(self, target: str, current: int) -> str:
        limit = self._char_limit(target)
        return f"{min(100, int((current / limit) * 100)) if limit > 0 else 0}% — {current:,}/{limit:,} chars"

    def _failure_with_entries(self, target: str, message: str) -> Dict[str, Any]:
        """Consolidation failure carrying the live entries so the model can consolidate."""
        return self._consolidation_failure(
            _error(message, current_entries=self._entries_for(target), usage=self._usage(target)))

    def _mutate(self, target: str, mutate, *, skip_drift: bool = False) -> Dict[str, Any]:
        """Lock, re-read from disk, run ``mutate(entries, limit)`` -> ``(new_entries, message)``
        or an error dict, then persist and return the success response. The reload aborts
        on an existing-but-unreadable file (even append-only ``add`` rewrites the whole
        file) and, unless *skip_drift*, on external drift (flushing would discard
        un-roundtrippable content). Drift check and parse use the SAME raw snapshot —
        a failed second read used to count as "no drift"."""
        if target == "project" and self._project_root is None:
            return self._no_project_error()
        path = self._path_for(target)
        with self._file_lock(path):
            raw, read_ok = self._read_raw_checked(path)
            if not read_ok:
                return _read_failed_error(path)
            bak = None if skip_drift else self._detect_external_drift(target, raw)
            self._set_entries(target, list(dict.fromkeys(self._parse_entries(raw))))
            if bak:
                return _drift_error(path, bak)
            result = mutate(self._entries_for(target), self._char_limit(target))
            if isinstance(result, dict):
                return result
            self._set_entries(target, result[0])
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_file(path, result[0])
            return self._success_response(target, result[1])

    def add(self, target: str, content: str) -> Dict[str, Any]:
        """Append a new entry. Returns error if it would exceed the char limit."""
        content = content.strip()
        if not content:
            return _error("Content cannot be empty.")
        if scan_error := _scan_memory_content(content):
            return _error(scan_error)

        def _add(entries, limit):
            if content in entries:
                return self._success_response(target, "Entry already exists (no duplicate added).")
            if len(ENTRY_DELIMITER.join(entries + [content])) > limit:
                return self._failure_with_entries(target, (
                    f"Memory at {self._char_count(target):,}/{limit:,} chars. Adding this entry "
                    f"({len(content)} chars) would exceed the limit. Consolidate now: use 'replace' to merge "
                    f"overlapping entries into shorter ones or 'remove' stale or less important entries (see "
                    f"current_entries below), then retry this add — all in this turn."))
            return entries + [content], "Entry added."
        # Append-only: skip the drift guard (appending never clobbers foreign
        # content) but still refuse a failed read — add rewrites the WHOLE file.
        return self._mutate(target, _add, skip_drift=True)

    def replace(self, target: str, old_text: str, new_content: str) -> Dict[str, Any]:
        """Find entry containing old_text substring, replace it with new_content."""
        new_content = new_content.strip()
        if not old_text.strip():
            return _error("old_text cannot be empty.")
        if not new_content:
            return _error("new_content cannot be empty. Use 'remove' to delete entries.")
        if scan_error := _scan_memory_content(new_content):
            return _error(scan_error)
        return self._edit(target, old_text.strip(), new_content)

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        """Remove the entry containing old_text substring."""
        if not old_text.strip():
            return _error("old_text cannot be empty.")
        return self._edit(target, old_text.strip(), None)

    def _edit(self, target: str, old_text: str, new_content: Optional[str]) -> Dict[str, Any]:
        """Locked replace (``new_content`` set) or remove (None) of the entry matching *old_text*."""
        def _apply(entries, limit):
            idx, ambiguous = _find_unique_match(entries, old_text)
            if ambiguous:
                return _error(f"Multiple entries matched '{old_text}'. Be more specific.",
                              matches=[e[:80] + ("..." if len(e) > 80 else "") for e in entries if old_text in e])
            if idx is None:
                return self._consolidation_failure(_error(
                    f"No entry matched '{old_text}'. Check current_entries below and retry with the exact text "
                    f"of the entry you want to {'replace' if new_content else 'remove'}.", current_entries=entries))
            replaced = entries[:idx] + ([] if new_content is None else [new_content]) + entries[idx + 1:]
            if new_content is None:
                return replaced, "Entry removed."
            new_total = len(ENTRY_DELIMITER.join(replaced))
            if new_total > limit:
                return self._failure_with_entries(target, (
                    f"Replacement would put memory at {new_total:,}/{limit:,} chars. Shorten the new content, "
                    f"or 'remove' other stale or less important entries to make room (see current_entries "
                    f"below), then retry — all in this turn."))
            return replaced, "Entry replaced."
        return self._mutate(target, _apply)

    @staticmethod
    def _apply_batch_op(working: List[str], act: str, content: str, old_text: str, pos: str) -> Optional[str]:
        """Apply one batch op to *working* in place; return an error message or None."""
        if act == "add":
            if not content:
                return f"{pos}: content is required."
            if content not in working:  # idempotent -- skip duplicate, don't fail the batch
                working.append(content)
            return None
        if act not in ("replace", "remove"):
            return f"{pos}: unknown action. Use add, replace, or remove."
        if not old_text:
            return f"{pos}: old_text is required."
        if act == "replace" and not content:
            return f"{pos}: content is required (use action='remove' to delete)."
        idx, ambiguous = _find_unique_match(working, old_text)
        if ambiguous:
            return f"{pos}: '{old_text}' matched multiple distinct entries -- be more specific."
        if idx is None:
            return f"{pos}: no entry matched '{old_text}'."
        working[idx:idx + 1] = [content] if act == "replace" else []
        return None

    def apply_batch(self, target: str, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Apply add/replace/remove ops atomically against the FINAL budget, so one call
        can free space and add entries. All-or-nothing: any malformed / unmatched op or
        an over-limit result writes NOTHING and returns the first failure plus live state."""
        if not operations:
            return _error("operations list is empty.")
        ops = [op or {} for op in operations]
        # Scan every add/replace content BEFORE touching disk -- one poisoned op rejects the batch.
        for i, op in enumerate(ops):
            scan_error = op.get("action") in {"add", "replace"} and op.get("content") and _scan_memory_content(op["content"])
            if scan_error:
                return _error(f"Operation {i + 1}: {scan_error}")

        def _apply(entries, limit):
            working = list(entries)  # only committed if the whole batch validates
            for i, op in enumerate(ops):
                act = op.get("action")
                msg = self._apply_batch_op(working, act, (op.get("content") or op.get("new_text") or "").strip(),
                                           (op.get("old_text") or "").strip(), f"Operation {i + 1} ({act or 'unknown'})")
                if msg:
                    return self._failure_with_entries(target, msg + " No operations were applied (batch is all-or-nothing).")
            if entries and not working:
                # #103419: a consolidation batch that removes the last entry would
                # commit an empty file as a normal successful write. Refuse; single
                # remove() is the deliberate-wipe path.
                label = self._path_for(target).name
                return self._failure_with_entries(target, (
                    f"Refusing to empty {label}: this batch would remove every entry from a "
                    f"previously non-empty store. Nothing was applied (batch is all-or-nothing). "
                    f"Keep at least one entry — merge overlapping entries into a shorter one instead "
                    f"of removing the last one (see current_entries below). To delete the final entry "
                    f"deliberately, use single remove() calls."))
            new_total = len(ENTRY_DELIMITER.join(working))  # budget check against the FINAL state only
            if new_total > limit:
                return self._failure_with_entries(target, (
                    f"After applying all {len(operations)} operations, memory would be at "
                    f"{new_total:,}/{limit:,} chars -- over the limit. Remove or shorten more "
                    f"entries in the same batch (see current_entries below), then retry."))
            return working, f"Applied {len(operations)} operation(s)."
        return self._mutate(target, _apply)

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """Frozen load-time snapshot (NOT live state — mid-session writes don't touch
        it, preserving the prefix cache); None if empty. ``linked_projects`` is a
        fork-only read-only snapshot (no file target)."""
        return self._system_prompt_snapshot.get(target, "") or None

    def _success_response(self, target: str, message: str = None) -> Dict[str, Any]:
        """TERMINAL and WITHOUT the entries list: echoing entries invites the model to
        "find more to fix" and re-issue the same ops. A successful write resets the
        per-turn failure budget."""
        # A successful write means the consolidation loop made progress, so the per-turn failure budget
        # resets (the cap counts consecutive failures, not lifetime ones within a turn) (#42405).
        self._consolidation_failures = 0
        return {"success": True, "done": True, "target": target,
                "usage": self._usage_pct(target, self._char_count(target)),
                "entry_count": len(self._entries_for(target)), **({"message": message} if message else {}),
                "note": "Write saved. This update is complete — do not repeat it."}

    def _render_block(self, target: str, entries: List[str]) -> str:
        """System prompt block: header + usage indicator + entries ("" when empty)."""
        if not entries:
            return ""
        content, sep = ENTRY_DELIMITER.join(entries), "═" * 46
        default_header = "PROJECT MEMORY" if target == "project" else (
            "USER PROFILE (who the user is)" if target == "user" else "MEMORY (your personal notes)")
        title = MEMORY_BLOCK_HEADERS.get(target, default_header)
        if target == "project" and self._project_root is not None:
            title = f"{title} — {self._project_root}"
        return f"{sep}\n{title} [{self._usage_pct(target, len(content))}]\n{sep}\n{content}"

    @staticmethod
    def _read_raw_checked(path: Path) -> Tuple[str, bool]:
        """``(raw, read_ok)``; ``read_ok`` is False ONLY when the file EXISTS but can't be
        read. Decoding stays STRICT (``errors="replace"`` would hand callers a lossy view
        a save then persists); ``utf-8-sig`` strips a Notepad BOM off the first entry."""
        if not path.exists():
            return "", True
        try:
            # utf-8-sig strips a leading UTF-8 BOM (Notepad-edited memory files on Windows) and is
            # byte-identical to utf-8 otherwise. Plain utf-8 kept U+FEFF glued to the first entry,
            # corrupting matching/dedup for that entry forever (#10878 / PR #10888). Decode errors stay
            # STRICT on purpose: errors="replace" would hand read-modify-write callers a lossy view that a
            # subsequent save persists over the real bytes — the wipe class documented above. Undecodable
            # bytes must surface as read_ok=False.
            return path.read_text(encoding="utf-8-sig"), True
        except (OSError, UnicodeDecodeError):
            return "", False

    @staticmethod
    def _parse_entries(raw: str) -> List[str]:
        """Stripped, non-empty entries; splits on the FULL delimiter so a bare "§" survives."""
        return [e for e in (x.strip() for x in raw.split(ENTRY_DELIMITER)) if e]

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """Entries of a memory file ([] on any error). Read-only callers only; mutation
        paths use ``_read_raw_checked`` so they can refuse to overwrite an unreadable file."""
        return MemoryStore._parse_entries(MemoryStore._read_raw_checked(path)[0])

    @staticmethod
    def _write_file(path: Path, entries: List[str]):
        """Atomic temp-file + rename: readers never see a truncated file. Also used by
        agent/learning_mutations.py."""
        try:
            atomic_write_text(path, ENTRY_DELIMITER.join(entries), tmp_prefix=".mem_")
        except OSError as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}")

    def _detect_external_drift(self, target: str, raw: str) -> Optional[str]:
        """``.bak.<ts>`` snapshot path if *raw* shows external drift, else None. Signals:
        round-trip mismatch, or one entry over the whole-file limit (no tool-written
        entry can be — an external writer appended free-form text)."""
        parsed = self._parse_entries(raw)
        if not raw.strip() or (raw.strip() == ENTRY_DELIMITER.join(parsed)
                               and max(map(len, parsed), default=0) <= self._char_limit(target)):
            return None
        path = self._path_for(target)
        bak_path = path.with_suffix(path.suffix + f".bak.{int(time.time())}")
        try:
            bak_path.write_text(raw, encoding="utf-8")
        except OSError:
            return str(bak_path) + " (BACKUP FAILED — file unchanged on disk)"
        return str(bak_path)
