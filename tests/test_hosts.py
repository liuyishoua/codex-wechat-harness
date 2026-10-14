import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web, ClientSession, UnixConnector

from harness.hosts import Hosts, host_key
from harness.service import make_app
from harness.store import Store, Conflict
from harness.transport import ChannelNeedsMessage
from test_chat import Codex, CONFIG


def setup(tmp_path, store=None, sender=None, extra_hosts=()):
    sent = []
    async def send(chat_id, payload):
        sent.append(payload)
        return 'receipt'
    config = {**CONFIG, 'hosts': [{'id': 'mac-mini', 'name': 'Mac mini',
                'aliases': ['leomac-mini'], 'codex_socket': '/tmp/mac-control.sock',
                'project_cwd': '/Users/leo/leo-project'}, *extra_hosts]}
    clients = {host: Codex() for host in ['ubuntu', *(h['id'] for h in config['hosts'])]}
    hub = Hosts(config, store or Store(tmp_path / 'state.sqlite3'), sender or send, clients)
    for b in hub.bridges.values():
        b.connected = True
        b.desktop_questions = SimpleNamespace(poll=lambda threads: [], turns={})
    return hub, clients, sent


async def drain(hub):
    while hub.tasks:
        await asyncio.gather(*list(hub.tasks))
        # Hosts.tasks is a snapshot; let completion callbacks remove done tasks.
        await asyncio.sleep(0)


def data(text, message_id):
    return {'platform': 'weixin', 'chat_id': 'owner', 'user_id': 'owner',
            'text': text, 'message_id': message_id}


async def message(hub, text, ident):
    result = await hub.chat.accept(data(text, ident))
    await drain(hub)
    return result


def test_four_hosts_keep_inputs_results_questions_and_selection_isolated(tmp_path):
    async def check():
        extra = [{'id': 'windows', 'name': 'Windows', 'aliases': ['win'],
                  'codex_socket': '/tmp/windows.sock', 'remote_platform': 'windows',
                  'project_cwd': 'E:/leo-project'},
                 {'id': 'macbook', 'name': 'MacBook', 'codex_socket': '/tmp/macbook.sock',
                  'project_cwd': '/Users/liuzhiyuan/leo-project'}]
        hub, clients, sent = setup(tmp_path, extra_hosts=extra)
        for host in hub.bridges:
            await message(hub, '切换 ' + host_key(hub.names[host]), 'switch-' + host)
            await message(hub, '新建会话 ' + host, 'new-' + host)
            await message(hub, '开始 ' + host, 'input-' + host)
            assert clients[host].calls[-1][1]['input'][0]['text'] == '开始 ' + host
            assert hub.chat.current()['host_id'] == host
        assert hub.bridges['windows'].chat.cwd == 'E:\\leo-project'
        assert clients['windows'].threads['thread-1']['cwd'] == 'E:\\leo-project'
        before = len(sent)
        for host, bridge in hub.bridges.items():
            bridge.chat.enqueue('status-' + host, '状态 ' + host)
            await bridge.add('thread-1', [{'id': 'q', 'question': '确认 ' + host, 'options': []}], key='q-' + host)
        hub.chat.schedule_flush()
        await drain(hub)
        assert all(p.startswith('【MacBook】') for p in sent[before:])
        await message(hub, '切换 win', 'back-windows')
        assert hub.chat.selected() == 'windows'
        assert '【Windows】\n状态 windows' in sent
        await message(hub, 'help', 'help-four')
        assert all('切换 ' + name in sent[-1] for name in ['ubuntu', 'macmini', 'windows', 'macbook'])
        await message(hub, '主机列表', 'list-four')
        assert all('切换 ' + name in sent[-1] for name in ['ubuntu', 'macmini', 'windows', 'macbook'])
        assert hub.chat.action('切换macmini后处理', []) == (None, None)
        assert hub.chat.action('切换macmini', ['image.png']) == (None, None)
        assert hub.chat.action('/codex 切换MACMINI', []) == ('host-switch', 'mac-mini')
        assert hub.chat.action('切换 2', []) == (None, None)
        restored, _, _ = setup(tmp_path, store=hub.store, extra_hosts=extra)
        assert restored.chat.selected() == 'windows'
        assert restored.chat.current()['name'] == 'windows'
    asyncio.run(check())


def test_switch_routes_to_remote_and_keeps_same_thread_ids_isolated(tmp_path):
    async def check():
        hub, clients, sent = setup(tmp_path)
        await message(hub, '新建任务 本地任务', 'local-new')
        await message(hub, '本地开始', 'local-input')
        await message(hub, '切换 Mac mini', 'switch')
        await message(hub, '新建任务 远端任务', 'remote-new')
        await message(hub, '远端开始', 'remote-input')
        # Deliberately collide thread and turn IDs in both app servers.
        assert hub.bridges['ubuntu'].chat.current()['thread_id'] == 'thread-1'
        assert hub.bridges['mac-mini'].chat.current()['thread_id'] == 'thread-1'
        assert clients['mac-mini'].calls[-1][1]['input'][0]['text'] == '远端开始'
        assert [p for m, p in clients['ubuntu'].calls if m == 'turn/start'][-1]['input'][0]['text'] == '本地开始'
        assert clients['ubuntu'].calls[-1][0] == 'thread/unsubscribe'
        await message(hub, '切换 Ubuntu', 'back')
        await message(hub, '本地继续', 'continue')
        assert clients['ubuntu'].calls[-1][0] == 'turn/steer'
        assert hub.chat.current()['name'] == '本地任务'
        assert '【Mac mini】' in ''.join(sent) and '【Ubuntu】' in ''.join(sent)
        assert not any(m == 'turn/interrupt' for c in clients.values() for m, p in c.calls)
    asyncio.run(check())


def test_burst_switch_pins_following_input_and_offline_queue(tmp_path):
    async def check():
        hub, clients, sent = setup(tmp_path)
        await hub.chat.accept(data('切换 Mac mini', 'switch'))
        await hub.chat.accept(data('远端任务', 'input'))
        await hub.chat.accept(data('切换 Ubuntu', 'back'))
        await drain(hub)
        assert any(m == 'turn/start' for m, p in clients['mac-mini'].calls)
        assert not clients['ubuntu'].calls
        mac = hub.bridges['mac-mini']
        await message(hub, '切换 Mac mini', 'again')
        mac.connected = False
        await message(hub, '等远端恢复', 'offline')
        await message(hub, '切换 Ubuntu', 'leave')
        mac.connected = True
        await mac.chat.work()
        await drain(hub)
        assert clients['mac-mini'].calls[-1][1]['input'][0]['text'] == '等远端恢复'
        assert not clients['ubuntu'].calls
    asyncio.run(check())


def test_slow_image_copy_keeps_receipt_host_before_later_switch(tmp_path, monkeypatch):
    import threading
    from PIL import Image
    from harness import images
    original = images.snapshot
    started, release = threading.Event(), threading.Event()
    calls = []
    def delayed(path, directory):
        calls.append(path)
        if len(calls) == 1:
            started.set()
            assert release.wait(3)
        return original(path, directory)
    monkeypatch.setattr(images, 'snapshot', delayed)
    source = tmp_path / 'image.png'
    Image.new('RGB', (8, 8)).save(source)
    async def check():
        hub, clients, sent = setup(tmp_path)
        receiving = asyncio.create_task(hub.chat.accept({**data('看图片', 'image'), 'images': [str(source)]}))
        assert await asyncio.to_thread(started.wait, 3)
        switching = asyncio.create_task(hub.chat.accept(data('切换 Mac mini', 'switch')))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(receiving, switching)
        await drain(hub)
        assert hub.chat.selected() == 'mac-mini'
        assert any(m == 'turn/start' for m, p in clients['ubuntu'].calls)
        assert not clients['mac-mini'].calls
    asyncio.run(check())


def test_inactive_host_queues_questions_and_results_until_selected(tmp_path):
    async def check():
        hub, clients, sent = setup(tmp_path)
        await message(hub, '切换 Mac mini', 'switch')
        await message(hub, '新建任务 原任务', 'new')
        await message(hub, '开始', 'start')
        mac = hub.bridges['mac-mini']
        await message(hub, '切换 Ubuntu', 'leave')
        before = len(sent)
        q = await mac.add('thread-1', [{'id': 'choice', 'question': '请选择', 'options': ['继续']}], key='q')
        assert q['outbound'] == 'queued'
        with pytest.raises(Conflict, match='先切换'):
            await hub.reply(data(q['id'] + ' 1', 'answer'))
        await drain(hub)
        assert hub.question_row(q['id'])['state'] == 'pending'
        assert not clients['ubuntu'].calls
        event = {'method': 'turn/completed', 'params': {'threadId': 'thread-1',
                 'turn': {'id': 'turn-1', 'status': 'completed', 'items': [
                    {'type': 'agentMessage', 'phase': 'final_answer', 'text': '远端完成'}]}}}
        await mac.event(event)
        await mac.event(event)
        await drain(hub)
        assert len(sent) == before
        assert hub.chat.selected() == 'ubuntu'
        # Channel recovery must not bypass the selected-host filter.
        await hub.channel_ready(data('', 'ready'))
        await drain(hub)
        assert len(sent) == before
        await message(hub, '切换 Mac mini', 'return')
        assert sent.count('【Mac mini】\n「原任务」\n\n远端完成') == 1
        assert hub.question_row(q['id'])['outbound'] == 'sent'
        await hub.reply(data(q['id'] + ' 1', 'answer'))
        await drain(hub)
        assert hub.question_row(q['id'])['state'] == 'delivered'
        assert clients['mac-mini'].calls[-1][0] == 'turn/steer'
    asyncio.run(check())


def test_inactive_host_images_and_status_wait_for_switch_back(tmp_path):
    from PIL import Image
    image = tmp_path / 'result.png'
    Image.new('RGB', (8, 8)).save(image)
    async def check():
        hub, _, sent = setup(tmp_path)
        await message(hub, '切换 Mac mini', 'switch')
        before = len(sent)
        await hub.chat.queue_image('local-image', str(image))
        hub.local.chat.enqueue('local-status', '本地状态')
        hub.chat.schedule_flush()
        await drain(hub)
        assert len(sent) == before
        with hub.local.store.db() as db:
            assert {r['state'] for r in db.execute('SELECT state FROM chat_outbox')} == {'queued'}
        await message(hub, '切换 Ubuntu', 'back')
        assert len([p for p in sent if isinstance(p, dict)]) == 1
        assert '【Ubuntu】\n本地状态' in sent
    asyncio.run(check())


def test_switch_waits_for_inflight_delivery_and_defers_later_messages(tmp_path):
    async def check():
        started, release = asyncio.Event(), asyncio.Event()
        sent = []
        async def sender(chat_id, payload):
            if payload.endswith('发送中'):
                started.set()
                await release.wait()
            sent.append(payload)
            return 'receipt'
        hub, _, _ = setup(tmp_path, sender=sender)
        hub.local.chat.enqueue('first', '发送中')
        hub.local.chat.enqueue('second', '稍后发送')
        hub.chat.schedule_flush()
        await started.wait()
        switching = asyncio.create_task(hub.chat.accept(data('切换 Mac mini', 'switch')))
        await asyncio.sleep(0)
        assert hub.chat.selected() == 'ubuntu'
        release.set()
        await switching
        await drain(hub)
        assert hub.chat.selected() == 'mac-mini'
        assert '【Ubuntu】\n稍后发送' not in sent
        assert sent[-1].startswith('【Mac mini】')
    asyncio.run(check())


def test_host_commands_work_offline_without_starting_model_or_switching(tmp_path):
    async def check():
        hub, clients, sent = setup(tmp_path)
        for b in hub.bridges.values():
            b.connected = False
        for text, ident in [('主机列表', 'list'), ('当前主机', 'current'),
                            ('help', 'help'), ('切换 Mac mini', 'switch'),
                            ('切换主机 不存在', 'unknown')]:
            await message(hub, text, ident)
        assert '这次没有切换' in ''.join(sent)
        assert '未找到主机' in sent[-1]
        assert hub.chat.selected() == 'ubuntu'
        assert not any(c.calls for c in clients.values())
    asyncio.run(check())


def test_restart_deduplicates_host_switch_and_input_without_changing_target(tmp_path):
    async def check():
        hub, clients, sent = setup(tmp_path)
        await message(hub, '切换 Mac mini', 'switch')
        await message(hub, '开始', 'input')
        await message(hub, '切换 Ubuntu', 'leave')
        restored, fresh_clients, _ = setup(tmp_path, hub.store)
        assert restored.chat.selected() == 'ubuntu'
        assert (await message(restored, '切换 Mac mini', 'switch'))['duplicate']
        assert (await message(restored, '开始', 'input'))['duplicate']
        assert restored.chat.selected() == 'ubuntu'
        assert not any(c.calls for c in fresh_clients.values())
        with pytest.raises(Conflict):
            await message(restored, '不同内容', 'input')
    asyncio.run(check())


def test_upgrade_preserves_single_host_message_deduplication(tmp_path):
    from test_chat import setup as local_setup, message as local_message
    async def check():
        old, _, _ = local_setup(tmp_path)
        await local_message(old, '开始旧任务', 'pre-upgrade')
        hub, clients, sent = setup(tmp_path, old.store)
        await message(hub, '切换 Mac mini', 'switch')
        assert (await message(hub, '开始旧任务', 'pre-upgrade'))['duplicate']
        assert not clients['mac-mini'].calls
        assert hub.chat.selected() == 'mac-mini'
    asyncio.run(check())


def test_numbered_session_lists_are_separate_per_host(tmp_path):
    async def check():
        hub, clients, sent = setup(tmp_path)
        for host, c in clients.items():
            c.threads['thread-1'] = {'id': 'thread-1', 'name': host, 'cwd': hub.bridges[host].chat.cwd}
        await message(hub, '会话列表', 'ubuntu-list')
        await message(hub, '切换 Mac mini', 'switch')
        await message(hub, '切换 1', 'no-remote-list')
        assert hub.chat.current() is None and '请先发送' in sent[-1]
        await message(hub, '会话列表', 'mac-list')
        await message(hub, '切换 1', 'mac-session')
        assert hub.chat.current()['name'] == 'mac-mini'
        await message(hub, '切换 Ubuntu', 'back')
        await message(hub, '切换 1', 'ubuntu-session')
        assert hub.chat.current()['name'] == 'ubuntu'
    asyncio.run(check())


def test_duplicate_question_ids_fail_closed(tmp_path):
    async def check():
        hub, clients, sent = setup(tmp_path)
        await message(hub, '开始', 'local')
        await message(hub, '切换 Mac mini', 'switch')
        await message(hub, '开始', 'remote')
        q = [{'id': 'q', 'question': '回答', 'options': []}]
        a = await hub.local.add('thread-1', q, key='local')
        b = await hub.bridges['mac-mini'].add('thread-1', q, key='remote')
        with hub.bridges['mac-mini'].store.db() as db:
            db.execute('UPDATE questions SET id=? WHERE id=?', (a['id'], b['id']))
        before = [len(c.calls) for c in clients.values()]
        with pytest.raises(Conflict):
            await hub.reply(data(a['id'] + ' 同意', 'answer'))
        assert [len(c.calls) for c in clients.values()] == before
    asyncio.run(check())


def test_shared_wechat_session_recovery_and_unauthorized_rejection(tmp_path):
    calls = []
    async def sender(chat_id, text):
        calls.append(text)
        if len(calls) == 1:
            raise ChannelNeedsMessage()
        return 'receipt'
    async def check():
        hub, clients, _ = setup(tmp_path, sender=sender)
        await message(hub, '主机列表', 'list')
        assert all(b.channel_blocked for b in hub.bridges.values())
        with pytest.raises(PermissionError):
            await hub.chat.accept({**data('切换 Mac mini', 'bad'), 'user_id': 'stranger'})
        assert hub.chat.selected() == 'ubuntu'
        await hub.channel_ready(data('', 'ready'))
        await drain(hub)
        assert not hub.channel_blocked
        assert len(calls) == 2
    asyncio.run(check())


def test_http_health_and_remote_question_status(tmp_path):
    async def check():
        hub, clients, sent = setup(tmp_path)
        await message(hub, '切换 Mac mini', 'switch')
        await message(hub, '开始', 'start')
        q = await hub.bridges['mac-mini'].add('thread-1', [{'id': 'q', 'question': '问题', 'options': []}], key='q')
        runner = web.AppRunner(make_app(hub))
        await runner.setup()
        socket = str(tmp_path / 'bridge.sock')
        await web.UnixSite(runner, socket).start()
        try:
            async with ClientSession(connector=UnixConnector(path=socket)) as client:
                async with client.get('http://localhost/health') as response:
                    result = await response.json()
                    assert [h['id'] for h in result['hosts'] if h['selected']] == ['mac-mini']
                    assert result['current_chat']['host_id'] == 'mac-mini'
                async with client.get('http://localhost/questions/' + q['id']) as response:
                    assert response.status == 200 and (await response.json())['id'] == q['id']
        finally:
            await runner.cleanup()
    asyncio.run(check())
