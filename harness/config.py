"""Portable local paths and explicit account configuration; no WeChat credentials."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def channel_name(config):
    return 'QQ' if config.get('platform', 'weixin') == 'qqbot' else '微信'


def message_prefix(config):
    return 'qqbot' if config.get('platform', 'weixin') == 'qqbot' else 'wechat'


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
    if config.get('platform', 'weixin') not in ('weixin', 'qqbot'):
        raise ValueError('不支持的消息平台')
    for field in ('chat_id', 'codex_socket'):
        if not isinstance(config.get(field), str) or not config[field].strip():
            raise ValueError('配置缺少 ' + field)
    users = config.get('allowed_users')
    if not isinstance(users, list) or not users or not all(isinstance(u, str) and u.strip() for u in users):
        raise ValueError('allowed_users 必须包含授权用户 ID')
    if config.get('platform') == 'qqbot':
        config.setdefault('gateway_send_socket', str(data_dir() / 'outbound.sock'))
    config['codex_socket'] = str(Path(config['codex_socket']).expanduser().resolve())
    config['project_cwd'] = str(Path(config.get('project_cwd', os.getcwd())).expanduser().resolve())
    config.setdefault('codex_state_db', default_state_db())
    return config
