"""Generate private configuration, link the Hermes plugin, and write a user service."""
import argparse
import json
import os
import sys
from pathlib import Path

from .config import ROOT, data_dir, default_state_db


def unit_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def service_unit(repo, local, python, hermes_home, hermes_source=None):
    paths = [str(repo)] + ([str(hermes_source)] if hermes_source else [])
    return '\n'.join([
        '[Unit]', 'Description=Codex Hermes WeChat bridge', 'After=network-online.target', '',
        '[Service]', 'Type=simple', 'WorkingDirectory=' + unit_quote(repo),
        'Environment=' + unit_quote('PYTHONPATH=' + ':'.join(paths)),
        'Environment=' + unit_quote('CODEX_WECHAT_HOME=' + str(local)),
        'Environment=' + unit_quote('HERMES_HOME=' + str(hermes_home)),
        'ExecStart=' + unit_quote(python) + ' -m harness.service',
        'Restart=on-failure', 'RestartSec=5', 'UMask=0077', '',
        '[Install]', 'WantedBy=default.target', '',
    ])


def install(args):
    local = data_dir()
    local.mkdir(parents=True, exist_ok=True, mode=0o700)
    local.chmod(0o700)
    config_path = local / 'config.json'
    if config_path.exists():
        raise ValueError('配置已存在，保留原文件；请手动编辑 ' + str(config_path))
    project = Path(args.project_cwd).expanduser().resolve()
    if not project.is_dir():
        raise ValueError('项目目录不存在')
    home = Path(args.hermes_home).expanduser().resolve()
    destination = home / 'plugins' / 'codex-wechat-harness'
    source = ROOT / 'hermes_plugin'
    if (destination.exists() or destination.is_symlink()) and destination.resolve() != source.resolve():
        raise ValueError('已有其他同名 Hermes 插件，请先检查 ' + str(destination))
    config = {
        'chat_id': args.chat_id, 'allowed_users': args.user_id,
        'codex_socket': str(Path(args.codex_socket).expanduser().resolve()),
        'project_cwd': str(project), 'chat_enabled': True, 'thread_ids': [],
        'codex_state_db': str(Path(args.codex_state_db).expanduser().resolve()) if args.codex_state_db else default_state_db(),
    }
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.is_symlink():
        destination.symlink_to(source, target_is_directory=True)
    unit_path = local / 'codex-wechat-harness.service'
    python = Path(args.python).expanduser().absolute()
    hermes_source = Path(args.hermes_source).expanduser().resolve() if args.hermes_source else None
    unit_path.write_text(service_unit(ROOT, local, python, home, hermes_source))
    print('配置：' + str(config_path))
    print('插件：' + str(destination))
    print('服务：' + str(unit_path))
    print('下一步：hermes plugins enable codex-wechat-harness')
    print('先以前台 python -m harness.service 验证连接，再按 README 安装用户服务。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-cwd', required=True)
    parser.add_argument('--codex-socket', required=True)
    parser.add_argument('--chat-id', required=True)
    parser.add_argument('--user-id', action='append', required=True)
    parser.add_argument('--hermes-home', default=os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    parser.add_argument('--hermes-source', help='Optional Hermes checkout, if not importable from the selected Python')
    parser.add_argument('--python', default=sys.executable, help='Python with Hermes and bridge dependencies installed')
    parser.add_argument('--codex-state-db', help='Optional desktop rollout index; auto-detected when available')
    args = parser.parse_args()
    if not args.chat_id.strip() or not all(user.strip() for user in args.user_id):
        parser.error('微信 chat-id 和 user-id 不能为空')
    try:
        install(args)
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
