"""Fork option: ``session_listing.show_api_server`` controls whether api_server rows are listed."""
import types

import pytest

from gateway.session_listing import _excluded_sources


def _store(tmp_path, config: str | None):
    if config is not None:
        (tmp_path / 'config.yaml').write_text(config, encoding='utf-8')
    return types.SimpleNamespace(scope=types.SimpleNamespace(profile_dir=tmp_path))


@pytest.mark.parametrize('config, listed', [
    (None, False),
    ('model:\n  provider: glm\n', False),
    ('session_listing:\n  show_api_server: false\n', False),
    ('session_listing:\n  show_api_server: true\n', True),
])
def test_show_api_server_option(tmp_path, config, listed):
    excluded = _excluded_sources(_store(tmp_path, config))
    assert ('api_server' in excluded) is (not listed)
    # internal sources stay excluded either way
    assert 'kanban' in excluded and 'oneshot' in excluded


def test_picker_row_prefixes_api_dispatch():
    """平台派发的会话在列表里有 [API] 前缀，人自己的会话不动。"""
    from gateway.session_listing import _picker_row

    row = types.SimpleNamespace(title="审阅 intake 分支", started_at=1.0, message_count=3)
    api = _picker_row("r1", row, types.SimpleNamespace(source="api_server", model="m"), "s1")
    assert api["title"] == "[API] 审阅 intake 分支"
    assert api["source"] == "api_server"

    human = _picker_row("r2", row, types.SimpleNamespace(source="desktop", model="m"), "s2")
    assert human["title"] == "审阅 intake 分支"

    bare = types.SimpleNamespace(title="", started_at=1.0, message_count=0)
    assert _picker_row("r3", bare, types.SimpleNamespace(source="api_server", model="m"), "s3")["title"] == "[API]"


def test_default_call_site_keeps_upstream_semantics():
    """No store supplied (or unreadable config) → upstream behaviour: api_server hidden."""
    assert 'api_server' in _excluded_sources()
    assert 'api_server' in _excluded_sources(types.SimpleNamespace())
