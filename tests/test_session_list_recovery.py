import asyncio
from unittest.mock import AsyncMock

import pytest
import aiohttp

from harness.codex import Codex, RpcError
from test_chat import setup, message


def thread(index, **extra):
    return {'id': str(index), 'name': '任务' + str(index), 'cwd': '/tmp/project', **extra}


def test_recent_list_stops_before_next_page(tmp_path):
    async def check():
        bridge, codex, sent = setup(tmp_path)
        codex.rpc = AsyncMock(return_value={'data': [thread(i) for i in range(10)], 'nextCursor': 'more'})
        await message(bridge, '会话列表', 'list')
        codex.rpc.assert_awaited_once()
        method, params = codex.rpc.call_args.args
        assert method == 'thread/list' and params['limit'] == 10
        assert '10. 任务9' in sent[-1] and '/tmp/project' not in sent[-1]
    asyncio.run(check())


def test_recent_list_skips_children_and_fills_from_next_page(tmp_path):
    async def check():
        bridge, codex, sent = setup(tmp_path)
        codex.rpc = AsyncMock(side_effect=[
            {'data': [thread('child', canAcceptDirectInput=False)] + [thread(i) for i in range(9)],
             'nextCursor': 'page-two'},
            {'data': [thread(9)], 'nextCursor': 'unused'},
        ])
        assert await bridge.chat.refresh(limit=10) == [str(i) for i in range(10)]
        assert codex.rpc.await_count == 2
        assert codex.rpc.call_args.args[1]['limit'] == 1
        assert codex.rpc.call_args.args[1]['cursor'] == 'page-two'
    asyncio.run(check())


def test_list_failure_does_not_require_desktop_confirmation(tmp_path):
    async def check():
        bridge, codex, sent = setup(tmp_path)
        codex.rpc = AsyncMock(side_effect=TimeoutError())
        await message(bridge, '会话列表', 'failed-list')
        with bridge.store.db() as db:
            assert db.execute("SELECT state FROM chat_jobs WHERE message_id='failed-list'").fetchone()['state'] == 'failed'
        assert '无需到桌面确认' in sent[-1] and '请求未确认' not in sent[-1]
        codex.rpc = AsyncMock(return_value={'data': [thread(1)]})
        await message(bridge, '会话列表', 'new-list')
        assert '1. 任务1' in sent[-1]
    asyncio.run(check())


def test_uncertain_turn_is_never_retried(tmp_path):
    async def check():
        bridge, codex, sent = setup(tmp_path)
        bridge.chat.remember('current', '任务')
        bridge.chat.select('current')
        original = codex.rpc
        async def rpc(method, params):
            if method == 'turn/start':
                codex.calls.append((method, params))
                raise TimeoutError()
            return await original(method, params)
        codex.rpc = rpc
        await message(bridge, '继续任务', 'write')
        assert [m for m, p in codex.calls].count('turn/start') == 1
        assert '请求未确认' in sent[-1]
        with bridge.store.db() as db:
            assert db.execute("SELECT state FROM chat_jobs WHERE message_id='write'").fetchone()['state'] == 'uncertain'
    asyncio.run(check())


@pytest.mark.parametrize('failure', [TimeoutError(), ConnectionError(), OSError(), aiohttp.ServerDisconnectedError()])
def test_read_query_retries_once_on_fresh_connection(monkeypatch, failure):
    async def check():
        original = Codex('socket')
        original.rpc = AsyncMock(side_effect=failure)
        original.close = AsyncMock()
        fallback = Codex('socket')
        fallback.connect = AsyncMock()
        fallback.rpc = AsyncMock(return_value={'data': [thread(1)]})
        fallback.close = AsyncMock()
        monkeypatch.setattr('harness.codex.Codex', lambda path: fallback)
        params = {'limit': 10}
        assert await original.read_only_rpc('thread/list', params) == {'data': [thread(1)]}
        original.rpc.assert_awaited_once_with('thread/list', params)
        fallback.connect.assert_awaited_once()
        fallback.rpc.assert_awaited_once_with('thread/list', params)
        fallback.close.assert_awaited_once()
        original.close.assert_not_awaited()
    asyncio.run(check())


def test_failed_fallback_is_closed_and_not_retried_again(monkeypatch):
    async def check():
        original = Codex('socket')
        original.rpc = AsyncMock(side_effect=TimeoutError())
        fallback = Codex('socket')
        fallback.connect = AsyncMock(side_effect=ConnectionError())
        fallback.close = AsyncMock()
        monkeypatch.setattr('harness.codex.Codex', lambda path: fallback)
        with pytest.raises(ConnectionError):
            await original.read_only_rpc('thread/list', {})
        fallback.connect.assert_awaited_once()
        fallback.close.assert_awaited_once()
    asyncio.run(check())


def test_rejected_query_and_mutating_methods_are_not_retried(monkeypatch):
    async def check():
        original = Codex('socket')
        original.rpc = AsyncMock(side_effect=RpcError({'code': -32000}))
        def unexpected(path):
            pytest.fail('must not open fallback connection')
        monkeypatch.setattr('harness.codex.Codex', unexpected)
        with pytest.raises(RpcError):
            await original.read_only_rpc('thread/list', {})
        with pytest.raises(ValueError):
            await original.read_only_rpc('turn/start', {})
        original.rpc.assert_awaited_once_with('thread/list', {})
    asyncio.run(check())


def test_number_switch_never_scans_beyond_displayed_ten(tmp_path):
    async def check():
        bridge, codex, sent = setup(tmp_path)
        original = codex.rpc
        async def rpc(method, params):
            if method == 'thread/list':
                codex.calls.append((method, params))
                assert params['limit'] <= 10
                assert 'cursor' not in params, 'numbered switching must not scan older pages'
                return {'data': [thread(i) for i in range(10)], 'nextCursor': 'slow-old-history'}
            return await original(method, params)
        codex.rpc = rpc
        await message(bridge, '会话列表', 'list')
        await message(bridge, '切换 2', 'switch')
        assert bridge.chat.current()['thread_id'] == '1'
        assert '已切到' in sent[-1]
    asyncio.run(check())


def test_name_switch_and_new_session_use_server_title_filter(tmp_path):
    async def check():
        bridge, codex, sent = setup(tmp_path)
        codex.threads = {'existing': thread('existing', name='已有任务')}
        await message(bridge, '切换 已有任务', 'switch')
        await message(bridge, '新建会话 新任务', 'new')
        queries = [p for m, p in codex.calls if m == 'thread/list']
        assert [p['searchTerm'] for p in queries] == ['已有任务', '新任务']
        assert all(p['limit'] <= 10 for p in queries)
    asyncio.run(check())


def test_rejected_destination_keeps_old_subscription_and_selection(tmp_path):
    async def check():
        bridge, codex, sent = setup(tmp_path)
        codex.threads = {'old': thread('old', updatedAt=1), 'new': thread('new', updatedAt=2)}
        bridge.chat.remember('old', '旧任务')
        bridge.chat.select('old')
        bridge.joined.add('old')
        await message(bridge, '会话列表', 'list')
        original = codex.rpc
        async def rpc(method, params):
            if method == 'thread/resume' and params['threadId'] == 'new':
                raise RpcError({'code': -32600, 'message': 'thread new already has an active writer'})
            return await original(method, params)
        codex.rpc = rpc
        await message(bridge, '切换 1', 'blocked-switch')
        assert bridge.chat.current()['thread_id'] == 'old'
        assert bridge.joined == {'old'} and not bridge.chat.released('old')
        assert not any(m == 'thread/unsubscribe' for m, p in codex.calls)
        assert '占用' in sent[-1] and '请求未确认' not in sent[-1]
        with bridge.store.db() as db:
            assert db.execute("SELECT state FROM chat_jobs WHERE message_id='blocked-switch'").fetchone()['state'] == 'failed'
    asyncio.run(check())


@pytest.mark.parametrize('switch_succeeds', [True, False])
def test_message_received_during_switch_never_goes_to_old_session(tmp_path, switch_succeeds):
    async def check():
        bridge, codex, sent = setup(tmp_path)
        codex.threads = {'old': thread('old', updatedAt=1), 'new': thread('new', updatedAt=2)}
        bridge.chat.remember('old', '旧任务')
        bridge.chat.select('old')
        bridge.joined.add('old')
        await message(bridge, '会话列表', 'list')
        started, release = asyncio.Event(), asyncio.Event()
        original = codex.rpc
        async def rpc(method, params):
            if method == 'thread/resume' and params['threadId'] == 'new':
                started.set()
                await release.wait()
                if not switch_succeeds:
                    raise RpcError({'code': -32600, 'message': 'thread new already has an active writer'})
            return await original(method, params)
        codex.rpc = rpc
        data = {'platform': 'weixin', 'chat_id': 'owner', 'user_id': 'owner'}
        await bridge.chat.accept({**data, 'text': '切换 1', 'message_id': 'switch'})
        await asyncio.wait_for(started.wait(), 1)
        await bridge.chat.accept({**data, 'text': '紧跟着的消息', 'message_id': 'input'})
        release.set()
        while bridge.tasks:
            await asyncio.gather(*list(bridge.tasks))
        writes = [p for m, p in codex.calls if m in ('turn/start', 'turn/steer')]
        assert all(p['threadId'] != 'old' for p in writes)
        if switch_succeeds:
            assert len(writes) == 1 and writes[0]['threadId'] == 'new'
        else:
            assert not writes and '这条消息未发送' in sent[-1]
    asyncio.run(check())


def test_failed_switch_lookup_is_safe_to_retry_and_keeps_current(tmp_path):
    async def check():
        bridge, codex, sent = setup(tmp_path)
        bridge.chat.remember('old', '旧任务')
        bridge.chat.select('old')
        bridge.joined.add('old')
        codex.rpc = AsyncMock(side_effect=TimeoutError())
        await message(bridge, '切换 2', 'timeout-switch')
        assert '切换未执行' in sent[-1] and '旧任务' in sent[-1]
        assert '请求未确认' not in sent[-1]
        assert bridge.chat.current()['thread_id'] == 'old' and bridge.joined == {'old'}
        assert all(call.args[0] == 'thread/list' for call in codex.rpc.await_args_list)
    asyncio.run(check())


def test_restart_with_uncertain_switch_does_not_release_waiting_message_into_old_thread(tmp_path):
    async def check():
        bridge, codex, sent = setup(tmp_path)
        bridge.chat.remember('old', '旧任务')
        bridge.chat.select('old')
        bridge.connected = False
        await message(bridge, '切换 2', 'pending-switch')
        await message(bridge, '跟着切换的消息', 'pending-input')
        with bridge.store.db() as db:
            db.execute("UPDATE chat_jobs SET state='processing' WHERE message_id='pending-switch'")
        restored, new_codex, restored_sent = setup(tmp_path)
        await restored.chat.work()
        while restored.tasks:
            await asyncio.gather(*list(restored.tasks))
        assert not any(m in ('turn/start', 'turn/steer') for m, p in new_codex.calls)
        assert '这条消息未发送' in restored_sent[-1]
        assert restored.chat.current()['thread_id'] == 'old'
    asyncio.run(check())


def test_explicit_input_restores_reconnect_intent_after_rejected_release(tmp_path):
    async def check():
        bridge, codex, sent = setup(tmp_path)
        await message(bridge, '新建会话 原会话', 'create')
        current = bridge.chat.current()['thread_id']
        original = codex.rpc
        async def rpc(method, params):
            if method == 'thread/unsubscribe':
                raise RpcError({'code': -32000})
            return await original(method, params)
        codex.rpc = rpc
        with pytest.raises(RpcError):
            await bridge.chat.start_thread()
        assert bridge.chat.released(current) and current in bridge.joined
        await message(bridge, '继续原任务', 'continue')
        assert not bridge.chat.released(current)
        assert [p for m, p in codex.calls if m == 'turn/start'][-1]['threadId'] == current
    asyncio.run(check())
