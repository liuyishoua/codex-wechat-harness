"""Expose an existing remote Codex socket locally using its raw stdio proxy."""
import argparse
import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

from .config import load_config
from .remote import ssh_args

log = logging.getLogger('codex-wechat-proxy')


async def relay(reader, writer, args):
    proc = None
    tasks = []
    try:
        proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

        async def copy(source, destination):
            while data := await source.read(65536):
                destination.write(data)
                await destination.drain()

        tasks = [asyncio.create_task(copy(reader, proc.stdin)),
                 asyncio.create_task(copy(proc.stdout, writer))]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (ConnectionError, BrokenPipeError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if proc and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


async def serve(config):
    path = Path(config['codex_socket'])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and not path.is_socket():
        raise ValueError('控制 socket 路径已被其他文件占用')
    path.unlink(missing_ok=True)
    args = ssh_args(config, [config.get('codex_executable', 'codex'), 'app-server',
                            'proxy', '--sock', config['codex_proxy_socket']])
    clients = set()

    async def client(reader, writer):
        task = asyncio.current_task()
        clients.add(task)
        try:
            await relay(reader, writer, args)
        finally:
            clients.discard(task)

    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    server = await asyncio.start_unix_server(client, str(path))
    path.chmod(0o600)
    try:
        async with server:
            await stopped.wait()
    finally:
        for task in list(clients):
            task.cancel()
        await asyncio.gather(*clients, return_exceptions=True)
        path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    name = parser.parse_args().host
    config = next((h for h in load_config().get('hosts', []) if h['id'] == name), None)
    if not config:
        parser.error('未配置目标主机')
    os.umask(0o077)
    asyncio.run(serve(config))


if __name__ == '__main__':
    main()
