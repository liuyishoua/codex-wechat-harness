import asyncio

from harness.codex import RpcError
from test_chat import setup, message
from test_hosts import setup as host_setup, message as host_message


def test_switch_releases_before_resume_and_polls_old_results_without_reclaiming(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        await message(b, '新建会话 旧会话', 'old')
        await message(b, '执行', 'run')
        old = b.chat.current()['thread_id']
        c.calls.clear()
        await message(b, '新建会话 新会话', 'new')
        methods = [m for m, p in c.calls]
        assert methods.index('thread/unsubscribe') < methods.index('thread/start')
        new = b.chat.current()['thread_id']
        assert b.joined == {new} and b.chat.released(old)
        c.turns[old][0].update(status='completed', items=[{'type': 'agentMessage', 'text': '后台完成'}])
        c.calls.clear()
        await b.chat.poll()
        assert not any(m == 'thread/resume' and p['threadId'] == old for m, p in c.calls)
        assert not any(m == 'turn/interrupt' for m, p in c.calls)
        with b.store.db() as db:
            assert db.execute('SELECT 1 FROM chat_outbox WHERE key LIKE ?', ('result:'+old+':%',)).fetchone()
        await message(b, '切换 旧会话', 'back')
        assert b.joined == {old} and b.chat.released(new) and not b.chat.released(old)
    asyncio.run(check())


def test_disconnect_survives_restart_and_only_explicit_input_reconnects(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        await message(b, '新建会话 桌面继续', 'new')
        thread = b.chat.current()['thread_id']
        await message(b, '断开会话', 'release')
        assert not b.joined and b.chat.released(thread)
        restored, cc, _ = setup(tmp_path)
        await restored.chat.poll()
        assert not any(m == 'thread/resume' for m, p in cc.calls)
        cc.threads = c.threads.copy()
        await message(restored, '继续', 'continue')
        assert restored.joined == {thread} and not restored.chat.released(thread)
    asyncio.run(check())


def test_host_switch_does_not_let_inactive_poll_reacquire_old_thread(tmp_path):
    async def check():
        hub, clients, _ = host_setup(tmp_path)
        await host_message(hub, '新建会话 本机', 'new')
        old = hub.local.chat.current()['thread_id']
        await host_message(hub, '切换 macmini', 'switch')
        assert not hub.local.joined and hub.local.chat.released(old)
        clients['ubuntu'].calls.clear()
        await hub.local.chat.poll()
        await hub.local.subscribe(old)
        assert not any(m == 'thread/resume' for m, p in clients['ubuntu'].calls)
        await host_message(hub, '切换 ubuntu', 'back')
        assert hub.local.joined == {old} and not hub.local.chat.released(old)
    asyncio.run(check())


def test_failed_unsubscribe_does_not_connect_a_new_thread(tmp_path):
    async def check():
        b, c, _ = setup(tmp_path)
        await message(b, '新建会话 旧', 'old')
        old = b.chat.current()['thread_id']
        rpc = c.rpc
        async def reject(method, params):
            if method == 'thread/unsubscribe':
                raise RpcError({'code': -32000})
            return await rpc(method, params)
        c.rpc = reject
        c.calls.clear()
        try:
            await b.chat.start_thread()
        except RpcError:
            pass
        else:
            raise AssertionError('must not connect before releasing old thread')
        assert not any(m == 'thread/start' for m, p in c.calls)
        assert b.chat.current()['thread_id'] == old
    asyncio.run(check())
