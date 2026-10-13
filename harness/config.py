"""Portable local paths and explicit account configuration; no WeChat credentials."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def data_dir():
    return Path(os.environ.get('CODEX_WECHAT_HOME', str(ROOT / '.local'))).expanduser().resolve()


def codex_home():
    return Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser().resolve()


def default_state_db():
    candidates = list(codex_home().glob('state_*.sqlite'))
    def version(path):
        try:
            return int(path.stem.split('_')[-1])
        except ValueError:
            return -1
    return str(max(candidates, key=version)) if candidates else None


def load_config():
    config = json.loads((data_dir() / 'config.json').read_text())
    for field in ('chat_id', 'codex_socket'):
        if not isinstance(config.get(field), str) or not config[field].strip():
            raise ValueError('配置缺少 ' + field)
    users = config.get('allowed_users')
    if not isinstance(users, list) or not users or not all(isinstance(u, str) and u.strip() for u in users):
        raise ValueError('allowed_users 必须包含授权微信用户 ID')
    config['codex_socket'] = str(Path(config['codex_socket']).expanduser().resolve())
    config['project_cwd'] = str(Path(config.get('project_cwd', os.getcwd())).expanduser().resolve())
    config.setdefault('codex_state_db', default_state_db())
    return config
