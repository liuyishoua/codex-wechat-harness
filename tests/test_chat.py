import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from harness.chat import HELP, command
from harness.service import Bridge, make_app
from harness.store import Store, Conflict
from harness.transport import ChannelNeedsMessage

CONFIG = {'chat_id': 'owner', 'allowed_users': ['owner'], 'codex_socket': 'unused', 'chat_enabled': True, 'project_cwd': '/tmp/example-project'}


class Codex:
    def __init__(self):
        self.calls = []
        self.threads = {}
        self.turns = {}
        self.turn_counter = 0

    async def rpc(self, method, params):
        self.calls.append((method, params))
        thread_id = params.get('threadId')
        if method == 'thread/list':
            threads = [t for t in self.threads.values() if t.get('cwd') == params['cwd']]
            return {'data': sorted(threads, key=lambda t: t.get('updatedAt', 0), reverse=True)}
        if method == 'thread/start':
            thread_id = 'thread-' + str(len(self.threads) + 1)
            self.threads[thread_id] = {'id': thread_id, 'cwd': params['cwd']}
            return {'thread': self.threads[thread_id]}
        if method == 'thread/name/set':
            self.threads[thread_id]['name'] = params['name']
        if method == 'thread/turns/list':
            return {'data': self.turns.get(thread_id, [])}
        if method == 'turn/start':
            self.turn_counter += 1
            turn = {'id': 'turn-' + str(self.turn_counter), 'status': 'inProgress'}
            self.turns.setdefault(thread_id, []).insert(0, turn)
            return {'turn': turn}
        return {}

    async def latest(self, thread_id):
        return self.turns[thread_id][0].copy()


def setup(tmp_path, sender=None):
    sent = []
    async def send(chat_id, text):
        sent.append(text)
        return 'receipt'
    c = Codex()
    bridge = Bridge(CONFIG, Store(tmp_path / 'state.sqlite'), sender or send, c)
    bridge.connected = True
    bridge.desktop_questions = SimpleNamespace(poll=lambda threads: [], turns={})
    return bridge, c, sent


async def drain(bridge):
    while bridge.tasks:
        await asyncio.gather(*list(bridge.tasks))


async def message(bridge, text, message_id):
    result = await bridge.chat.accept({'platform': 'weixin', 'chat_id': 'owner', 'user_id': 'owner',
                                       'text': text, 'message_id': message_id})
    await drain(bridge)
    return result


def test_help_without_starting_task(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        assert (await message(b, 'help', 'help'))['handled']
        assert sent == [HELP.format(project=b.chat.project_name)] and not c.calls
        assert '停止当前任务' not in HELP and '切换 2' in HELP
    asyncio.run(check())


def test_plain_chat_uses_latest_project_session_and_preserves_selection(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        c.threads = {
            'old': {'id': 'old', 'name': '旧任务', 'cwd': b.chat.cwd, 'updatedAt': 1},
            'recent': {'id': 'recent', 'name': '最近任务', 'cwd': b.chat.cwd, 'updatedAt': 2},
            'child': {'id': 'child', 'name': '子任务', 'cwd': b.chat.cwd, 'updatedAt': 3, 'canAcceptDirectInput': False},
            'other': {'id': 'other', 'name': '其他项目', 'cwd': '/tmp/other', 'updatedAt': 4},
        }
        assert (await message(b, '继续聊', 'ordinary'))['handled']
        assert b.chat.current()['thread_id'] == 'recent'
        assert [p for m, p in c.calls if m == 'turn/start'][-1]['threadId'] == 'recent'
        assert not any(m == 'thread/start' for m, p in c.calls)
        await message(b, '切换 旧任务', 'switch')
        c.threads['recent']['updatedAt'] = 99
        await message(b, '接着说', 'followup')
        assert b.chat.current()['thread_id'] == 'old'
        assert [p for m, p in c.calls if m == 'turn/start'][-1]['threadId'] == 'old'
    asyncio.run(check())


def test_plain_chat_creates_default_only_when_project_has_no_sessions(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        assert (await message(b, '你好', 'first'))['handled']
        assert b.chat.current()['name'] == '默认聊天'
        await message(b, '继续', 'second')
        assert [m for m, p in c.calls].count('thread/start') == 1
        assert [m for m, p in c.calls].count('turn/steer') == 1
    asyncio.run(check())


def test_disconnected_plain_chat_is_consumed_without_hermes_fallback(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        b.connected = False
        assert (await message(b, '你好', 'offline'))['handled']
        assert not c.calls and '等待' in sent[-1]
        with b.store.db() as db:
            assert db.execute("SELECT state FROM chat_jobs WHERE message_id='offline'").fetchone()['state'] == 'queued'
        b.connected = True
        await b.chat.work()
        assert [m for m, p in c.calls].count('turn/start') == 1
    asyncio.run(check())


def test_named_sessions_keep_context_and_deduplicate(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        await message(b, '新建会话 微信接入', 'new1')
        first = b.chat.current()['thread_id']
        await message(b, '实现消息分流', 'task1')
        await message(b, '补充一个要求', 'task2')
        assert [m for m, p in c.calls].count('turn/start') == 1
        steer = [p for m, p in c.calls if m == 'turn/steer'][-1]
        assert steer['threadId'] == first and steer['expectedTurnId'] == 'turn-1'
        await message(b, '新建任务 门户页面', 'new2')
        second = b.chat.current()['thread_id']
        assert second != first
        await message(b, '切换 微信接入', 'switch')
        assert b.chat.current()['thread_id'] == first
        count = len(c.calls)
        assert (await message(b, '切换 微信接入', 'switch'))['duplicate']
        assert len(c.calls) == count
        for method, params in c.calls:
            if method == 'thread/start':
                assert params == {'cwd': '/tmp/example-project'}
        assert not any(method == 'turn/interrupt' for method, params in c.calls)
        with pytest.raises(Conflict):
            await message(b, '切换 门户页面', 'switch')
    asyncio.run(check())


def test_question_reply_targets_original_after_switch(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        await message(b, '新建任务 原任务', 'new1')
        first = b.chat.current()['thread_id']
        await message(b, '开始', 'task')
        q = await b.add(first, [{'id': 'answer', 'question': '请选择', 'options': ['继续', '等等']}], key='desktop:question')
        await message(b, '新建任务 另一任务', 'new2')
        await b.reply({'platform': 'weixin', 'chat_id': 'owner', 'user_id': 'owner',
                       'message_id': 'answer', 'text': q['id'] + ' 1'})
        await drain(b)
        assert b.store.get(q['id'])['state'] == 'delivered'
        assert c.calls[-1][0] == 'turn/steer' and c.calls[-1][1]['threadId'] == first
        assert b.chat.current()['thread_id'] != first
    asyncio.run(check())


def test_results_label_old_session_and_send_once(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        await message(b, '新建任务 原任务', 'new1')
        first = b.chat.current()['thread_id']
        await message(b, '开始', 'task')
        await message(b, '新建任务 另一任务', 'new2')
        await b.event({'method': 'item/completed', 'params': {'threadId': first, 'turnId': 'turn-1',
                       'item': {'id': 'result', 'type': 'agentMessage', 'phase': 'final_answer', 'text': '已完成'}}})
        event = {'method': 'turn/completed', 'params': {'threadId': first,
                 'turn': {'id': 'turn-1', 'status': 'completed'}}}
        await b.event(event)
        await b.event(event)
        await drain(b)
        assert sent.count('「原任务」\n\n已完成') == 1
        assert b.chat.current()['name'] == '另一任务'
    asyncio.run(check())


def test_restart_restores_selection_and_avoids_replaying_unknown_writes(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        await message(b, '新建任务 原任务', 'new1')
        await message(b, '开始', 'task')
        with b.store.db() as db:
            db.execute("INSERT INTO chat_jobs VALUES('crashed','危险重放','processing',NULL,0)")
            db.execute("INSERT INTO chat_outbox VALUES('uncertain-send','危险重发','sending',0)")
        restored = Bridge(CONFIG, b.store, b.sender, c)
        restored.connected = True
        restored.desktop_questions = b.desktop_questions
        assert restored.chat.current()['name'] == '原任务'
        c.turns[b.chat.current()['thread_id']][0].update(status='completed', items=[
            {'type': 'agentMessage', 'text': '重连后的结果', 'phase': 'final_answer'}])
        await restored.chat.poll()
        await drain(restored)
        assert sent.count('「原任务」\n\n重连后的结果') == 1
        assert '危险重发' not in sent
        assert not any(p.get('input', [{}])[0].get('text') == '危险重放' for m, p in c.calls)
    asyncio.run(check())


def test_blocked_chat_outbox_recovers_on_message(tmp_path):
    calls = []
    async def send(chat_id, text):
        calls.append(text)
        if len(calls) == 1:
            raise ChannelNeedsMessage()
        return 'receipt'
    async def check():
        b, c, sent = setup(tmp_path, send)
        await message(b, 'help', 'help')
        assert b.channel_blocked
        await b.channel_ready({'platform': 'weixin', 'chat_id': 'owner', 'user_id': 'owner'})
        await drain(b)
        assert calls == [HELP.format(project=b.chat.project_name)] * 2
        await b.chat.flush()
        assert len(calls) == 2
    asyncio.run(check())


def test_unauthorized_chat_rejected(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        with pytest.raises(PermissionError):
            await b.chat.accept({'platform': 'weixin', 'chat_id': 'owner', 'user_id': 'stranger',
                                 'message_id': 'x', 'text': '新建任务 入侵'})
        assert not c.calls and not sent
    asyncio.run(check())


def test_desktop_sessions_exact_names_and_duplicates(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        c.threads = {'desktop': {'id': 'desktop', 'name': '桌面任务', 'cwd': '/tmp/example-project'}}
        await message(b, '会话列表', 'list')
        assert '桌面任务' in sent[-1]
        await b.event({'method': 'turn/completed', 'params': {'threadId': 'desktop',
                       'turn': {'id': 'unrelated', 'status': 'completed', 'items': [{'type': 'agentMessage', 'text': '不应发送'}]}}})
        assert '不应发送' not in '\n'.join(sent)
        await message(b, '切换 2', 'numeric')
        assert b.chat.current() is None
        await message(b, '切换 桌面任务', 'switch')
        assert b.chat.current()['thread_id'] == 'desktop'
        c.threads['duplicate'] = {'id': 'duplicate', 'name': '桌面任务', 'cwd': '/tmp/example-project'}
        await message(b, '切换 桌面任务', 'ambiguous')
        assert '唯一' in sent[-1]
        assert b.chat.current()['thread_id'] == 'desktop'
    asyncio.run(check())


def test_number_switch_uses_delivered_list_despite_new_order_and_restart(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        c.threads = {
            'first': {'id': 'first', 'name': '同名任务', 'cwd': b.chat.cwd, 'updatedAt': 2},
            'second': {'id': 'second', 'name': '同名任务', 'cwd': b.chat.cwd, 'updatedAt': 1},
        }
        await message(b, '会话列表', 'list')
        assert '1. 同名任务\n\n2. 同名任务' in sent[-1]
        c.threads['second']['updatedAt'] = 99
        restored = Bridge(CONFIG, b.store, b.sender, c)
        restored.connected = True
        restored.desktop_questions = b.desktop_questions
        await message(restored, '切换 「2」', 'switch')
        assert restored.chat.current()['thread_id'] == 'second'
        assert '已切到「同名任务」会话' in sent[-1]
        await message(restored, '会话列表', 'new-list')
        assert '1. 同名任务  ← 当前' in sent[-1]
        await message(restored, '切换 2', 'new-switch')
        assert restored.chat.current()['thread_id'] == 'first'
    asyncio.run(check())


def test_number_switch_requires_list_and_rejects_unavailable_target(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        c.threads = {'one': {'id': 'one', 'name': '任务', 'cwd': b.chat.cwd}}
        await message(b, '切换 1', 'before-list')
        assert b.chat.current() is None and '请先发送' in sent[-1]
        await message(b, '会话列表', 'list')
        for n in ['0', '2']:
            await message(b, '切换 ' + n, 'invalid-' + n)
            assert b.chat.current() is None and '序号不在列表' in sent[-1]
        c.threads.clear()
        await message(b, '切换 1', 'deleted')
        assert b.chat.current() is None and '已不可用' in sent[-1]
    asyncio.run(check())


def test_number_switch_rejects_old_list_or_newly_created_thread(tmp_path):
    async def check():
        b,c,sent=setup(tmp_path)
        c.threads={'one':{'id':'one','name':'旧任务','cwd':b.chat.cwd}}
        await message(b,'会话列表','old-list')
        with b.store.db() as db:
            db.execute('UPDATE chat_outbox SET created=created-601 WHERE key=?',('ack:old-list',))
        await message(b,'切换 1','stale-switch')
        assert b.chat.current() is None and '旧列表已失效' in sent[-1]
        await message(b,'会话列表','fresh-list')
        c.threads['new']={'id':'new','name':'自动登录','cwd':b.chat.cwd,'updatedAt':999}
        await message(b,'切换 1','changed-switch')
        assert b.chat.current() is None and '旧列表已失效' in sent[-1]
        await message(b,'会话列表','updated-list')
        await message(b,'切换 1','correct-switch')
        assert b.chat.current()['thread_id']=='new'
    asyncio.run(check())


def test_number_switch_pins_received_list_before_new_list_delivery(tmp_path):
    async def check():
        b,c,sent=setup(tmp_path)
        c.threads={'one':{'id':'one','name':'第一任务','cwd':b.chat.cwd,'updatedAt':2},
                   'two':{'id':'two','name':'第二任务','cwd':b.chat.cwd,'updatedAt':1}}
        await message(b,'会话列表','first-list')
        async with b.chat.lock:
            await b.chat.accept({'platform':'weixin','chat_id':'owner','user_id':'owner',
                                'message_id':'numeric','text':'切换 1'})
            c.threads['two']['updatedAt']=99
            await b.chat.execute({'message_id':'second-list','content':'会话列表','created':0})
            await b.chat.flush()
        await drain(b)
        assert b.chat.current()['thread_id']=='one'
    asyncio.run(check())


def test_unsent_list_does_not_replace_numbers_user_already_received(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        c.threads = {
            'one': {'id': 'one', 'name': '第一任务', 'cwd': b.chat.cwd, 'updatedAt': 2},
            'two': {'id': 'two', 'name': '第二任务', 'cwd': b.chat.cwd, 'updatedAt': 1},
        }
        await message(b, '会话列表', 'delivered')
        c.threads['two']['updatedAt'] = 99
        b.channel_blocked = True
        await message(b, '会话列表', 'blocked')
        await message(b, '切换 2', 'switch')
        assert b.chat.current()['thread_id'] == 'two'
    asyncio.run(check())


def test_plugin_routes_help_and_plain_text_over_real_unix_socket(tmp_path, monkeypatch):
    from aiohttp import web
    path = Path(__file__).parents[1] / 'hermes_plugin/__init__.py'
    spec = importlib.util.spec_from_file_location('chat_plugin', path)
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    (tmp_path / '.local').mkdir()
    (tmp_path / '.local/config.json').write_text(json.dumps(CONFIG))
    monkeypatch.setattr(plugin, 'ROOT', tmp_path)
    async def check():
        b, c, sent = setup(tmp_path)
        runner = web.AppRunner(make_app(b))
        await runner.setup()
        await web.UnixSite(runner, str(tmp_path / '.local/bridge.sock')).start()
        gateway = SimpleNamespace(_is_user_authorized_for_source=lambda s: True,
                                  _intake_adapter_for=lambda s: None)
        source = SimpleNamespace(platform=SimpleNamespace(value='weixin'), chat_id='owner', user_id='owner')
        async def dispatch(text, ident):
            return await plugin.dispatch(SimpleNamespace(source=source, text=text, message_id=ident), gateway)
        try:
            assert (await dispatch('普通聊天', 'ordinary'))['action'] == 'skip'
            await drain(b)
            assert b.chat.current()['name'] == '默认聊天'
            assert (await dispatch('help', 'help'))['action'] == 'skip'
            await drain(b)
            assert sent[-1] == HELP.format(project=b.chat.project_name)
            await dispatch('新建任务 项目任务', 'new')
            await drain(b)
            assert (await dispatch('帮我执行', 'task'))['action'] == 'skip'
            await drain(b)
            assert any(m == 'turn/start' for m, p in c.calls)
        finally:
            await runner.cleanup()
    asyncio.run(check())


def test_slow_notification_does_not_block_completed_or_new_input(tmp_path):
    async def check():
        started, release = asyncio.Event(), asyncio.Event()
        async def sender(chat_id, text):
            started.set()
            await release.wait()
            return 'receipt'
        b, c, _ = setup(tmp_path, sender)
        await b.chat.accept({'platform': 'weixin', 'chat_id': 'owner', 'user_id': 'owner',
                             'text': '新建任务 原会话', 'message_id': 'first'})
        await asyncio.wait_for(started.wait(), 1)
        thread_id = b.chat.current()['thread_id']
        completion = {'id': 'finished', 'status': 'completed', 'items': [
            {'type': 'agentMessage', 'text': '已完成', 'phase': 'final_answer'}]}
        await asyncio.wait_for(b.chat.completed(b.chat.current(), completion), .5)
        task = b.chat.flush_task
        for _ in range(5):
            assert b.chat.schedule_flush() is task
        await b.chat.accept({'platform': 'weixin', 'chat_id': 'owner', 'user_id': 'owner',
                             'text': '继续下一步', 'message_id': 'second'})
        await asyncio.wait_for(b.chat.work(), .5)
        assert any(m == 'turn/start' and p['threadId'] == thread_id for m, p in c.calls)
        with b.store.db() as db:
            assert db.execute("SELECT state FROM chat_outbox WHERE key=?",
                              ('result:' + thread_id + ':finished',)).fetchone()['state'] == 'queued'
        release.set()
        await drain(b)
        with b.store.db() as db:
            assert db.execute("SELECT state FROM chat_outbox WHERE key=?",
                              ('result:' + thread_id + ':finished',)).fetchone()['state'] == 'sent'
    asyncio.run(check())


def test_notification_timeout_is_uncertain_and_not_replayed(tmp_path):
    async def check():
        calls = []
        async def sender(chat_id, text):
            calls.append(text)
            await asyncio.Event().wait()
        b, c, _ = setup(tmp_path, sender)
        b.config = {**b.config, 'notification_timeout': .03}
        b.chat.enqueue('bounded-send', '通知')
        b.chat.schedule_flush()
        await asyncio.wait_for(drain(b), 1)
        with b.store.db() as db:
            assert db.execute("SELECT state FROM chat_outbox WHERE key='bounded-send'").fetchone()['state'] == 'uncertain'
        b.chat.schedule_flush()
        await drain(b)
        assert calls == ['通知']
    asyncio.run(check())
