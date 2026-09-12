"""Tests for project-aware multi-lib workspace (fork).

Two behaviors:
1. Memory anchoring: a session inside ANY member folder of a ``hermes
   project`` workspace stores project memory against the project's PRIMARY
   folder, so all libs share one store.
2. Prompt awareness: ``build_context_files_prompt`` injects a deterministic
   workspace map listing all member folders when cwd is inside the project.
"""

import json
from pathlib import Path

import pytest

import tools.memory_tool as mt
from tools.memory_tool import MemoryStore


@pytest.fixture()
def project_env(tmp_path, monkeypatch):
    """center-app (primary) + core-lib in one first-class Project."""
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: {}, raising=False
    )
    app = tmp_path / "center-app"
    lib = tmp_path / "core-lib"
    for d in (app, lib):
        d.mkdir()
        (d / ".git").mkdir()
        (d / "package.json").write_text(
            json.dumps({"name": d.name, "version": "0.0.0"}), encoding="utf-8"
        )
    return app, lib


def _create_project(app: Path, lib: Path):
    from hermes_cli.projects_db import connect_closing, create_project

    with connect_closing() as conn:
        pid = create_project(
            conn,
            name="Center Suite",
            folders=[str(app), str(lib)],
            primary_path=str(app),
        )
    return pid


def test_memory_anchors_to_primary_folder(project_env):
    app, lib = project_env
    _create_project(app, lib)
    # Session inside the NON-primary lib must anchor to the primary.
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr("agent.runtime_cwd.resolve_agent_cwd", lambda: lib)
        root = mt._project_root_for_session()
        assert root == app.resolve()
    finally:
        mp.undo()


def test_memory_shared_store_across_libs(project_env):
    app, lib = project_env
    _create_project(app, lib)
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr("agent.runtime_cwd.resolve_agent_cwd", lambda: lib)
        s = MemoryStore(memory_char_limit=500, user_char_limit=300, project_char_limit=400)
        s.load_from_disk()
        res = s.add("project", "shared fact from lib")
        assert res["success"] is True
        # Written under the PRIMARY folder's slug, not the lib's.
        from tools.memory_tool import _project_slug

        f = Path(mt.get_memory_dir()) / "projects" / f"{_project_slug(app.resolve())}.md"
        assert "shared fact from lib" in f.read_text(encoding="utf-8")
        assert not (Path(mt.get_memory_dir()) / "projects" / f"{_project_slug(lib.resolve())}.md").exists()
    finally:
        mp.undo()


def test_no_project_falls_back_to_git_root(project_env):
    app, lib = project_env
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr("agent.runtime_cwd.resolve_agent_cwd", lambda: app)
        root = mt._project_root_for_session()
        assert root == app.resolve()
    finally:
        mp.undo()


def test_workspace_map_injected_into_prompt(project_env):
    app, lib = project_env
    _create_project(app, lib)
    from agent.prompt_builder import build_context_files_prompt

    prompt = build_context_files_prompt(cwd=str(lib), skip_soul=True)
    assert "## Project Workspace: Center Suite" in prompt
    assert str(lib) in prompt
    assert str(app) in prompt
    assert "(primary)" in prompt
    # Both member folders listed.
    assert prompt.index(str(app)) < prompt.index(str(lib))


def test_workspace_map_absent_outside_project(tmp_path):
    from agent.prompt_builder import build_context_files_prompt

    lone = tmp_path / "lone-repo"
    lone.mkdir()
    (lone / ".git").mkdir()
    prompt = build_context_files_prompt(cwd=str(lone), skip_soul=True)
    assert "Project Workspace" not in prompt
