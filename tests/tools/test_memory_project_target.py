"""Tests for the per-project memory target ('project').

The project store is keyed by the session's working directory: at load time
the MemoryStore resolves the current git root (via resolve_agent_cwd() and an
ancestor walk) and maps it to ~/.hermes/memories/projects/<slug>.md. Writes
with target='project' land in that file; the system-prompt snapshot renders a
PROJECT MEMORY block carrying the project path in its header.

Design (fork feature, wcg):
- slug = 8-hex of sha1(git root) + '-' + sanitized trailing path segments
- project store lives under memories/projects/ alongside MEMORY.md / USER.md
- independent char limit (default 2000), doesn't eat the global budget
"""

import os
from pathlib import Path

import pytest

from tools.memory_tool import MemoryStore, MEMORY_BLOCK_HEADERS, get_memory_dir


@pytest.fixture()
def proj_store(tmp_path, monkeypatch):
    """Create a MemoryStore whose project root is a fake git repo."""
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    repo = tmp_path / "code" / "myproj"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    monkeypatch.setattr(
        "tools.memory_tool._project_root_for_session",
        lambda: repo,
    )
    s = MemoryStore(memory_char_limit=500, user_char_limit=300, project_char_limit=400)
    s.load_from_disk()
    return s


def _project_file(tmp_path, repo: Path) -> Path:
    """Locate the project memory file created for *repo* under *tmp_path*."""
    import hashlib

    slug_src = str(repo)
    h = hashlib.sha1(slug_src.encode("utf-8")).hexdigest()[:8]
    tail = repo.name.lower().replace(" ", "-")
    slug = f"{h}-{tail}"
    return tmp_path / "projects" / f"{slug}.md"


class TestProjectTargetBasics:
    def test_add_writes_project_file(self, proj_store, tmp_path):
        result = proj_store.add("project", "run tests with npm test")
        assert result["success"] is True
        assert "run tests with npm test" in proj_store.project_entries

        pf = _project_file(tmp_path, proj_store._project_root)
        assert pf.exists()
        assert "npm test" in pf.read_text(encoding="utf-8")

    def test_global_memory_untouched_by_project_writes(self, proj_store):
        proj_store.add("project", "project-only fact")
        assert proj_store.memory_entries == []
        assert (get_memory_dir() / "MEMORY.md").exists() is False or (
            get_memory_dir() / "MEMORY.md"
        ).read_text(encoding="utf-8").strip() == ""

    def test_project_block_header_carries_path(self, proj_store):
        proj_store.add("project", "uses bun not node")
        proj_store.load_from_disk()  # refresh snapshot the way a new session would
        block = proj_store.format_for_system_prompt("project")
        assert block is not None
        assert "PROJECT MEMORY" in block
        assert "myproj" in block
        # header includes usage indicator like the other targets
        assert "chars]" in block

    def test_project_snapshot_frozen_like_others(self, proj_store):
        # Snapshot captured at load time must not change mid-session: the
        # block stays absent until the NEXT load_from_disk refreshes it.
        assert proj_store.format_for_system_prompt("project") is None
        proj_store.add("project", "fact one")
        assert proj_store.format_for_system_prompt("project") is None
        proj_store.load_from_disk()
        block = proj_store.format_for_system_prompt("project")
        assert block is not None and "fact one" in block

    def test_replace_and_remove_on_project(self, proj_store):
        proj_store.add("project", "old convention")
        proj_store.load_from_disk()  # re-load to refresh snapshot/state
        result = proj_store.replace("project", "old convention", "new convention")
        assert result["success"] is True
        result = proj_store.remove("project", "new convention")
        assert result["success"] is True
        assert proj_store.project_entries == []

    def test_apply_batch_on_project(self, proj_store):
        result = proj_store.apply_batch(
            "project",
            [
                {"action": "add", "content": "first"},
                {"action": "add", "content": "second"},
            ],
        )
        assert result["success"] is True
        assert len(proj_store.project_entries) == 2

    def test_project_overflow_reports_target(self, proj_store):
        proj_store.add("project", "x" * 380)
        result = proj_store.add("project", "y" * 100)
        assert result["success"] is False
        assert "exceed" in result["error"].lower()

    def test_no_project_root_means_target_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "tools.memory_tool._project_root_for_session", lambda: None
        )
        s = MemoryStore()
        s.load_from_disk()
        result = s.add("project", "fact")
        assert result["success"] is False
        assert "no project" in result["error"].lower()


class TestProjectSlug:
    def test_slug_is_deterministic_and_safe(self):
        from tools.memory_tool import _project_slug

        slug = _project_slug(Path("/Users/wu/code/my proj"))
        assert slug == _project_slug(Path("/Users/wu/code/my proj"))
        h, _, tail = slug.partition("-")
        assert len(h) == 8
        int(h, 16)  # hex
        assert tail == "my-proj"

    def test_different_roots_different_slugs(self):
        from tools.memory_tool import _project_slug

        assert _project_slug(Path("/a/one")) != _project_slug(Path("/a/two"))


class TestMemoryToolDispatch:
    def test_invalid_target_still_rejected(self, proj_store):
        import json

        from tools.memory_tool import memory_tool

        out = json.loads(
            memory_tool(action="add", target="bogus", content="x", store=proj_store)
        )
        assert out["success"] is False

    def test_project_target_accepted(self, proj_store):
        import json

        from tools.memory_tool import memory_tool

        out = json.loads(
            memory_tool(
                action="add", target="project", content="hello", store=proj_store
            )
        )
        assert out["success"] is True

    def test_schema_mentions_project(self):
        from tools.memory_tool import MEMORY_SCHEMA

        desc = MEMORY_SCHEMA["parameters"]["properties"]["target"]["description"]
        assert "project" in desc


class TestBlockHeaderRegistry:
    def test_project_header_registered(self):
        assert "project" in MEMORY_BLOCK_HEADERS
        assert "PROJECT MEMORY" in MEMORY_BLOCK_HEADERS["project"]
