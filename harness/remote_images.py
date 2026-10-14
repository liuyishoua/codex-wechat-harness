"""Bounded image copies over the same authenticated SSH connection path."""
import asyncio
import os
import tempfile
from pathlib import Path

from .images import MAX_BYTES, snapshot
from .remote import absolute, windows, ssh_args
import ntpath


class RemoteImages:
    def __init__(self, config):
        self.target = config['ssh_target']
        self.identity = config.get('ssh_identity')
        self.directory = config['remote_image_dir']
        self.config = config
        if not absolute(self.directory, config) or self.target.startswith('-'):
            raise ValueError('远端图片传输配置无效')

    async def run(self, script, path, data=None):
        executable = self.config.get('remote_python', 'python' if windows(self.config) else 'python3')
        args = ssh_args(self.config, [executable, '-c', script, path])
        proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(proc.communicate(data), 40)
            if proc.returncode:
                raise ValueError('远端图片传输失败')
            return output
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def upload(self, path):
        source = Path(path)
        data = source.read_bytes()
        if len(data) > MAX_BYTES:
            raise ValueError('图片大小需在 20 MiB 以内')
        remote = ntpath.join(self.directory, source.name) if windows(self.config) else self.directory.rstrip('/') + '/' + source.name
        script = '''import os,sys,tempfile
from pathlib import Path
p=Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
data=sys.stdin.buffer.read(20971521)
if not data or len(data)>20971520: sys.exit(2)
fd,tmp=tempfile.mkstemp(dir=p.parent)
try:
 with os.fdopen(fd,'wb') as f: f.write(data)
 os.replace(tmp,p)
finally:
 Path(tmp).unlink(missing_ok=True)
'''
        await self.run(script, remote, data)
        return remote

    async def download(self, path, directory):
        if not absolute(path, self.config):
            raise ValueError('远端图片需要绝对路径')
        script = '''import sys
from pathlib import Path
p=Path(sys.argv[1])
with p.open('rb') as f: data=f.read(20971521)
if not data or len(data)>20971520: sys.exit(2)
sys.stdout.buffer.write(data)
'''
        data = await self.run(script, path)
        if not data or len(data) > MAX_BYTES:
            raise ValueError('远端图片大小无效')
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            return await asyncio.to_thread(snapshot, temporary, directory)
        finally:
            Path(temporary).unlink(missing_ok=True)
