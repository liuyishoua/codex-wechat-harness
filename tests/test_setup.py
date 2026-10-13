import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness import config, setup
from harness.rollouts import Questions


def test_installer_generates_private_config_and_relocatable_service(tmp_path, monkeypatch):
    repo = tmp_path / 'a different checkout'
    (repo / 'hermes_plugin').mkdir(parents=True)
    project = tmp_path / 'my project'
    project.mkdir()
    local = repo / '.local'
    monkeypatch.setattr(setup, 'ROOT', repo)
    monkeypatch.setattr(setup, 'data_dir', lambda: local)
    monkeypatch.setattr(setup, 'default_state_db', lambda: None)
    args = SimpleNamespace(project_cwd=str(project), hermes_home=str(tmp_path / 'hermes'),
                           codex_socket=str(tmp_path / 'control.sock'), chat_id='owner', user_id=['owner'],
                           codex_state_db=None, python='/some path/python', hermes_source='/another/hermes')
    setup.install(args)
    assert json.loads((local / 'config.json').read_text())['project_cwd'] == str(project)
    assert local.stat().st_mode & 0o777 == 0o700
    assert (local / 'config.json').stat().st_mode & 0o777 == 0o600
    assert (tmp_path / 'hermes/plugins/codex-wechat-harness').resolve() == repo / 'hermes_plugin'
    unit = (local / 'codex-wechat-harness.service').read_text()
    assert 'WorkingDirectory="' + str(repo) + '"' in unit
    assert 'ExecStart="/some path/python" -m harness.service' in unit
    with pytest.raises(ValueError, match='配置已存在'):
        setup.install(args)


def test_state_db_version_discovery_and_optional_watcher(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    assert config.default_state_db() is None
    for name in ('state_2.sqlite', 'state_11.sqlite', 'state_4.sqlite'):
        (tmp_path / name).touch()
    assert config.default_state_db() == str(tmp_path / 'state_11.sqlite')
    assert Questions(None).poll(['missing']) == []
    assert Questions(tmp_path / 'absent.sqlite').poll(['missing']) == []


def test_explicit_runtime_paths_and_user_validation(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_WECHAT_HOME', str(tmp_path))
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'chat_id': 'owner', 'allowed_users': ['owner'], 'codex_socket': '/tmp/example.sock',
                                'project_cwd': '/tmp/example-project', 'codex_state_db': None}))
    assert config.load_config()['project_cwd'] == '/tmp/example-project'
    assert config.load_config()['codex_state_db'] is None
    path.write_text(json.dumps({'chat_id': 'owner', 'allowed_users': [], 'codex_socket': '/tmp/example.sock'}))
    with pytest.raises(ValueError, match='allowed_users'):
        config.load_config()


def test_systemd_escaping():
    assert setup.unit_quote('/a%/b"c') == '"/a%%/b\\"c"'
