"""Tests for linked project memory (dependency-project recall, fork).

A session inside project A also sees the project memory of A's declared
dependencies when those dependencies exist as local sibling repos with their
own project memory file. Injection is READ-ONLY: the memory tool still writes
only to the session's own project store.
"""

import json
import os
from pathlib import Path

import pytest

import tools.memory_tool as mt
from tools.memory_tool import MemoryStore


def _make_repo(base: Path, name: str, deps=None) -> Path:
    repo = base / name
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    pkg = {"name": name, "version": "0.0.0"}
    if deps:
        pkg["dependencies"] = deps
    (repo / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    return repo


@pytest.fixture()
def linked_env(tmp_path, monkeypatch):
    """center-app depends on core-lib; both are sibling repos with memory."""
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    # Keep sibling-scan deterministic: no config roots.
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: {}, raising=False
    )
    lib = _make_repo(tmp_path, "core-lib")
    app = _make_repo(tmp_path, "center-app", deps={"core-lib": "^1.0.0"})

    from tools.memory_tool import _project_slug

    proj_dir = tmp_path / "projects"
    proj_dir.mkdir()
    (proj_dir / f"{_project_slug(lib)}.md").write_text(
        "core-lib构建: npm run build:test\n§\ncore-lib坑: 测试模式须NODE_ENV=test", encoding="utf-8"
    )
    monkeypatch.setattr(
        "tools.memory_tool._project_root_for_session", lambda: app
    )
    s = MemoryStore(memory_char_limit=500, user_char_limit=300, project_char_limit=400)
    s.load_from_disk()
    return s, app, lib


def test_linked_dependency_memory_injected(linked_env):
    s, app, lib = linked_env
    block = s.format_for_system_prompt("linked_projects")
    assert block is not None
    assert "core-lib" in block
    assert "npm run build:test" in block
    assert "read-only" in block


def test_own_project_memory_still_separate(linked_env):
    s, app, lib = linked_env
    # The app's own store starts empty; linked content must NOT leak into it.
    assert s.project_entries == []
    assert s.format_for_system_prompt("project") is None


def test_linked_memory_not_writable_via_store(linked_env):
    s, app, lib = linked_env
    # Write to own project store; the dependency's file must stay untouched.
    res = s.add("project", "center-app fact")
    assert res["success"] is True
    from tools.memory_tool import _project_slug

    lib_text = (
        mt.get_memory_dir() / "projects" / f"{mt._project_slug(lib)}.md"
    ).read_text(encoding="utf-8")
    assert "center-app fact" not in lib_text
    assert "npm run build:test" in lib_text


def test_no_project_memory_files_no_scan(monkeypatch, tmp_path):
    """When no project memory files exist, linking resolves nothing."""
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    lib = _make_repo(tmp_path, "core-lib")
    app = _make_repo(tmp_path, "center-app", deps={"core-lib": "^1.0.0"})
    monkeypatch.setattr(
        "tools.memory_tool._project_root_for_session", lambda: app
    )
    s = MemoryStore()
    s.load_from_disk()
    assert s._linked_projects == []


def test_dependency_without_memory_file_not_linked(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    _make_repo(tmp_path, "core-lib")  # exists, but no memory file
    app = _make_repo(tmp_path, "center-app", deps={"core-lib": "^1.0.0"})
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "dummy.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        "tools.memory_tool._project_root_for_session", lambda: app
    )
    s = MemoryStore()
    s.load_from_disk()
    assert s._linked_projects == []


def test_max_linked_projects_cap(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    from tools.memory_tool import _project_slug, MAX_LINKED_PROJECTS

    deps = {f"lib-{i}": "^1.0.0" for i in range(MAX_LINKED_PROJECTS + 3)}
    for i in range(MAX_LINKED_PROJECTS + 3):
        _make_repo(tmp_path, f"lib-{i}")
    app = _make_repo(tmp_path, "center-app", deps=deps)
    proj_dir = tmp_path / "projects"
    proj_dir.mkdir()
    for i in range(MAX_LINKED_PROJECTS + 3):
        lib = tmp_path / f"lib-{i}"
        (proj_dir / f"{_project_slug(lib)}.md").write_text(
            f"lib-{i} fact", encoding="utf-8"
        )
    monkeypatch.setattr(
        "tools.memory_tool._project_root_for_session", lambda: app
    )
    s = MemoryStore()
    s.load_from_disk()
    assert len(s._linked_projects) == MAX_LINKED_PROJECTS


def test_python_requirements_deps_linked(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    from tools.memory_tool import _project_slug

    lib = tmp_path / "pylib"
    lib.mkdir()
    (lib / ".git").mkdir()
    (lib / "pyproject.toml").write_text(
        '[project]\nname = "pylib"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    app = _make_repo(tmp_path, "center-app")
    (app / "requirements.txt").write_text(
        "pylib>=0.1\nrequests==2.31.0\n# comment\n-r other.txt\n", encoding="utf-8"
    )
    proj_dir = tmp_path / "projects"
    proj_dir.mkdir()
    (proj_dir / f"{_project_slug(lib)}.md").write_text("pylib fact", encoding="utf-8")
    monkeypatch.setattr(
        "tools.memory_tool._project_root_for_session", lambda: app
    )
    s = MemoryStore()
    s.load_from_disk()
    names = [n for n, _ in s._linked_projects]
    assert names == ["pylib"]
