"""Remote paths and SSH arguments must use the destination's conventions."""
import ntpath
import shlex
from pathlib import PureWindowsPath


def windows(config):
    return config.get('remote_platform') == 'windows'


def absolute(path, config):
    if not isinstance(path, str):
        return False
    return PureWindowsPath(path).is_absolute() if windows(config) else path.startswith('/')


def ssh_args(config, command):
    target = config['ssh_target']
    if not isinstance(target, str) or not target or target.startswith('-'):
        raise ValueError('SSH 目标无效')
    args = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=8', '-o', 'IdentitiesOnly=yes',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']
    if config.get('ssh_identity'):
        args += ['-i', config['ssh_identity']]
    # Current Windows SSH hosts use Git Bash, as do the POSIX hosts.
    return [*args, target, shlex.join(command)]


def same_cwd(left, right, config):
    if windows(config):
        return ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right))
    from pathlib import Path
    return str(Path(left).resolve()) == right
