import asyncio
import shlex
import sys

from harness.remote import absolute, same_cwd, ssh_args
from harness.remote_images import RemoteImages
from harness.ssh_proxy import relay
from harness.images import local_result_images


def test_windows_cwd_and_result_paths():
    config = {'remote_platform': 'windows'}
    assert absolute('E:/leo-project', config)
    assert not absolute('E:leo-project', config)
    assert not absolute('/tmp/local', config)
    assert same_cwd('e:/LEO-project/', 'E:\\leo-project', config)
    text = '![图](sandbox:E:/结果/a%20b.png)\nMEDIA: C:\\结果\\c.png\n![图](file:///E:/结果/d.png)'
    assert local_result_images(text) == ['E:/结果/a b.png', 'C:\\结果\\c.png', 'E:/结果/d.png']


def test_remote_arguments_are_literal_and_binary_images_use_windows_paths(tmp_path):
    path = tmp_path / 'a.png'
    path.write_bytes(b'\x00\xff\r\nimage')
    config = {'ssh_target': 'owner@host', 'remote_platform': 'windows',
              'remote_image_dir': 'C:/Users/owner/.codex/images', 'remote_python': 'D:/Python/python.exe'}
    transport = RemoteImages(config)
    calls = []
    async def run(script, remote, data=None):
        calls.append((remote, data))
        return b''
    transport.run = run
    async def check():
        remote = await transport.upload(str(path))
        assert same_cwd(remote, 'C:/Users/owner/.codex/images/a.png', config)
        assert calls[0][1] == path.read_bytes()
    asyncio.run(check())
    dangerous = "E:/图/it's $(not-a-command) `literal`.png"
    command = [config['remote_python'], '-c', 'print("test")', dangerous]
    args = ssh_args(config, command)
    assert args[-2] == 'owner@host'
    assert shlex.split(args[-1]) == command
    assert 'StrictHostKeyChecking=yes' in args


def test_raw_ssh_proxy_preserves_websocket_handshake_and_binary_frames(tmp_path):
    async def check():
        socket = str(tmp_path / 'proxy.sock')
        clients = set()
        script = '''import os,sys
left=int(sys.argv[1])
while left:
 data=os.read(0,min(65536,left))
 if not data: break
 os.write(1,data)
 left-=len(data)
'''
        payload = b'GET / HTTP/1.1\r\nUpgrade: websocket\r\n\r\n' + bytes(range(256)) * 1024
        async def client(reader, writer):
            task = asyncio.current_task()
            clients.add(task)
            try:
                await relay(reader, writer, [sys.executable, '-c', script, str(len(payload))])
            finally:
                clients.remove(task)
        server = await asyncio.start_unix_server(client, socket)
        try:
            reader, writer = await asyncio.open_unix_connection(socket)
            writer.write(payload)
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(len(payload)), 5) == payload
            assert await asyncio.wait_for(reader.read(), 5) == b''
            writer.close()
            await writer.wait_closed()
            await asyncio.gather(*clients)
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(check())
