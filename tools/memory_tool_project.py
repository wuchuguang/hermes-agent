"""Project-memory support (fork) for the upstream MemoryStore.

Ported from the pre-0.21.2 fork inline store: per-project file backed by the
session's git root (or first-class Project primary folder), plus read-only
linked-project blocks resolved from dependency package names. The upstream
``tools/memory_tool_store.MemoryStore`` delegates its ``project`` target here.
"""


def _project_slug(project_root: Path) -> str:
    """Deterministic filesystem-safe slug for a project root.

    ``<8-hex of sha1(root)>-<sanitized trailing segment>`` — the hash makes
    collisions between same-named projects impossible, the readable tail keeps
    the file recognizable when browsing ``memories/projects/`` by hand.
    """
    digest = hashlib.sha1(str(project_root).encode("utf-8")).hexdigest()[:8]
    tail = project_root.name.strip().lower()
    tail = re.sub(r"[^a-z0-9._-]+", "-", tail).strip("-.") or "project"
    return f"{digest}-{tail[:32]}"



def _project_dependency_names(project_root: Optional[Path]) -> List[str]:
    """Collect declared dependency package names for a project root.

    Reads ``package.json`` (dependencies + devDependencies) and Python
    requirement files (requirements*.txt / pyproject dependencies declared as
    ``name`` / ``name>=x`` / ``name==x``), best-effort: any parse failure just
    skips that file. Used to link a project's memory store to its local
    dependency libraries (same-machine sibling repos), so a session inside
    ``center-browser-cmd`` also sees facts learned in ``yhyun-core-utils``.
    """
    if project_root is None:
        return []
    names: List[str] = []
    pkg = project_root / "package.json"
    try:
        if pkg.is_file():
            data = json.loads(pkg.read_text("utf-8"))
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                names.extend((data.get(key) or {}).keys())
    except (OSError, ValueError):
        pass
    for req_name in ("requirements.txt", "requirements-dev.txt", "dev-requirements.txt"):
        req = project_root / req_name
        try:
            if req.is_file():
                for line in req.read_text("utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith(("#", "-")):
                        continue
                    # strip extras/env markers, keep the distribution name
                    name = re.split(r"[\s<>=!~\[;]", line, maxsplit=1)[0]
                    if name:
                        names.append(name)
        except OSError:
            pass
    py = project_root / "pyproject.toml"
    try:
        if py.is_file():
            text = py.read_text("utf-8")
            in_deps = False
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("dependencies") and "=" in s:
                    in_deps = True
                    continue
                if in_deps:
                    if not s.startswith("\"") and not s.startswith("'"):
                        break
                    m = re.match(r"[\"']([A-Za-z0-9_.-]+)", s)
                    if m:
                        names.append(m.group(1))
    except OSError:
        pass
    # de-dup, preserve order
    return list(dict.fromkeys(names))



def _package_name_for_root(root: Path) -> Optional[str]:
    """Package name of a local repo: package.json ``name`` then pyproject name."""
    pkg = root / "package.json"
    try:
        if pkg.is_file():
            name = json.loads(pkg.read_text("utf-8")).get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    except (OSError, ValueError):
        pass
    py = root / "pyproject.toml"
    try:
        if py.is_file():
            m = re.search(r'(?m)^\s*name\s*=\s*["\']([^"\']+)["\']', py.read_text("utf-8"))
            if m:
                return m.group(1).strip()
    except OSError:
        pass
    return None



def _project_root_for_session() -> Optional[Path]:
    """Resolve the project root for the current session, or None.

    Walks up from the agent's logical working directory (session override →
    TERMINAL_CWD → os.getcwd(), same resolution as context-file loading) to
    the nearest containing ``.git`` directory. Returns None outside any repo
    — sessions not tied to a project simply have no project store.

    Fork extension: when no ``.git`` ancestor exists, a directory carrying a
    package manifest (``package.json`` / ``pyproject.toml``) still counts as a
    project root — npm libraries cloned/extracted without git history
    (e.g. yhyun-pkg-server) keep their own per-project memory. The user's
    home directory itself is never a project root.

    Fork extension 2 (project-aware memory): when the session's directory
    belongs to a first-class Project (``hermes project`` / projects.db
    multi-folder workspace), the store anchors to the project's PRIMARY
    folder instead of the session's own git root. Sessions started inside
    any member library then share one store; a single-folder project
    resolves to that folder, matching the git-root behavior below.
    """
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        start = resolve_agent_cwd()
    except Exception:
        start = Path.cwd()
    try:
        start = start.resolve()
    except Exception:
        pass
    if not start.is_dir():
        return None
    # Never resolve a project store for the Hermes install tree itself.
    try:
        from agent.runtime_cwd import _is_install_tree

        if _is_install_tree(start):
            return None
    except Exception:
        pass
    home = Path.home()
    manifest_root: Optional[Path] = None
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            # First-class Project wins over the bare git root: sessions in any
            # member library share the project's primary-folder store.
            project_root = _primary_root_for_session(start)
            if project_root is not None:
                return project_root
            return candidate
        if manifest_root is None and candidate != home and not _is_install_tree_safe(candidate):
            if (candidate / "package.json").is_file() or (candidate / "pyproject.toml").is_file():
                manifest_root = candidate
    return manifest_root



def _is_install_tree_safe(path: Path) -> bool:
    try:
        from agent.runtime_cwd import _is_install_tree

        return _is_install_tree(path)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Memory content scanning — lightweight check for injection/exfiltration
# in content that gets injected into the system prompt.
#
# Patterns live in ``tools/threat_patterns.py`` — the single source of truth
# shared with the context-file scanner and the tool-result delimiter system.
# Memory uses the "strict" scope (broadest pattern set) because:
#  - memory entries are user-curated; the user can rewrite a flagged entry
#  - memory enters the system prompt as a FROZEN snapshot, so a poisoned
#    entry persists for the entire session and across sessions until
#    explicitly removed.
# ---------------------------------------------------------------------------

from tools.threat_patterns import first_threat_message as _first_threat_message



NO_PROJECT_ERROR = {
    "success": False,
    "error": (
        "No project detected for this session (no git root above the "
        "working directory). Project memory is unavailable — use "
        "target='memory' for global notes instead."
    ),
    "target": "project",
}


def project_slug(root: str) -> str:
    import hashlib
    h = hashlib.sha1(root.encode()).hexdigest()[:8]
    tail = str(root).rstrip("/").split("/")[-1]
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in tail)
    return f"{h}-{safe}"[:64]


def project_file_for_root(mem_dir, root):
    """memories/projects/<sha1-8>-<tail>.md for the frozen project root."""
    if root is None:
        raise ValueError("no project root")
    from pathlib import Path as _P
    return _P(mem_dir) / "projects" / (project_slug(str(root)) + ".md")


def resolve_project_root():
    """Walk up from the agent cwd to the nearest .git / manifest root.
    Ported from the pre-0.21.2 fork memory_tool (frozen at load_from_disk)."""
    from pathlib import Path as _P
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        start = _P(resolve_agent_cwd())
    except Exception:
        start = _P.cwd()
    try:
        start = start.resolve()
    except OSError:
        return None
    home = _P.home()

    def _is_install_tree_safe(path: _P) -> bool:
        try:
            from agent.runtime_cwd import _is_install_tree
            return _is_install_tree(path)
        except Exception:
            return False

    # First-class Project (projects.db) primary folder wins when configured.
    try:
        from hermes_cli.projects_db import connect_closing, project_for_path
        with connect_closing() as conn:
            project = project_for_path(conn, str(start))
        if project is not None and project.folders:
            primary = project.primary_path or next(
                (f.path for f in project.folders if f.is_primary), None)
            if primary:
                return _P(primary)
    except Exception:
        pass

    current = start
    while True:
        if (current / ".git").exists():
            return None if _is_install_tree_safe(current) else current
        if current == home or current.parent == current:
            break
        current = current.parent

    # Fork extension: manifest dirs without .git count as project roots
    # (yhyun-pkg-server style) — but never the home dir itself.
    for candidate in [start, *start.parents]:
        if candidate == home:
            break
        if (candidate / "package.json").is_file() or (candidate / "pyproject.toml").is_file():
            if not _is_install_tree_safe(candidate):
                return candidate
    return None
