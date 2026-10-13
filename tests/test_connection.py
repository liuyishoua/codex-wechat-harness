import asyncio
from types import SimpleNamespace

import pytest

from harness.codex import RpcError
from test_chat import setup, message


def test_rejected_loaded_thread_does_not_drop_shared_connection(tmp_path, monkeypatch):
    from harness import service
    async def check():
        b, c, sent = setup(tmp_path)
        b.chat = None
        async def connect():
            c.reader = SimpleNamespace(done=lambda: False)
        async def close():
            pass
        async def rpc(method, params):
            if method == 'thread/loaded/list':
                return {'data': ['bad', 'good']}
            if method == 'thread/resume' and params['threadId'] == 'bad':
                raise RpcError({'code': -32000})
            return {'data': [{'id': 'turn-good'}]} if method == 'thread/turns/list' else {}
        async def stop(delay):
            assert b.connected
            assert b.joined == {'good'}
            raise asyncio.CancelledError()
        c.connect, c.close, c.rpc = connect, close, rpc
        b.desktop_questions.resolved = []
        monkeypatch.setattr(service.asyncio, 'sleep', stop)
        with pytest.raises(asyncio.CancelledError):
            await b.monitor()
    asyncio.run(check())


def test_offline_input_preserves_original_selected_thread(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        await message(b, '新建任务 原会话', 'first')
        original = b.chat.current()['thread_id']
        await message(b, '新建任务 另一个', 'second')
        other = b.chat.current()['thread_id']
        b.chat.select(original)
        b.connected = False
        await message(b, '等连接后继续', 'offline')
        b.chat.select(other)
        b.connected = True
        await b.chat.work()
        assert [p for m, p in c.calls if m == 'turn/start'][-1]['threadId'] == original
    asyncio.run(check())
