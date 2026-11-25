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


def test_default_call_site_keeps_upstream_semantics():
    """No store supplied (or unreadable config) → upstream behaviour: api_server hidden."""
    assert 'api_server' in _excluded_sources()
    assert 'api_server' in _excluded_sources(types.SimpleNamespace())
