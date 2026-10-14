import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, UnixConnector, web
from PIL import Image

from harness.hosts import Hosts
from harness.service import make_app
from harness.store import Store, Conflict
from harness.transport import send_qqbot, ChannelNeedsMessage
from test_chat import CONFIG, Codex
from test_hosts import drain


def config(directory, platform):
    return {**CONFIG, 'platform': platform, 'managed_threads_only': True,
            'gateway_send_socket': str(directory / 'outbound.sock'),
            'hosts': [{'id': h, 'name': name, 'codex_socket': '/tmp/'+h+'.sock',
                       'project_cwd': '/tmp/'+h} for h, name in
                      [('mac-mini', 'Mac mini'), ('macbook', 'MacBook'), ('windows', 'Windows')]]}


def plugin_at(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('qq_bridge_plugin',
        Path(__file__).parents[1] / 'hermes_plugin/__init__.py')
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    monkeypatch.setattr(plugin, 'ROOT', tmp_path)
    return plugin


def event(text, ident, platform='qqbot', user='owner', images=()):
    return SimpleNamespace(source=SimpleNamespace(platform=platform, chat_id='owner', user_id=user),
        text=text, message_id=ident, media_urls=list(images), media_types=['image/png']*len(images))


async def start(directory, cfg, sender=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'config.json').write_text(json.dumps(cfg))
    clients = {h: Codex() for h in ['ubuntu', 'mac-mini', 'macbook', 'windows']}
    hub = Hosts(cfg, Store(directory / 'state.sqlite3'), sender=sender, clients=clients)
    for b in hub.bridges.values():
        b.connected = True
        b.desktop_questions = SimpleNamespace(poll=lambda _: [], turns={})
    runner = web.AppRunner(make_app(hub))
    await runner.setup()
    await web.UnixSite(runner, str(directory / 'bridge.sock')).start()
    return hub, clients, runner


def test_qq_and_wechat_full_routing_and_outbound_are_isolated(tmp_path, monkeypatch):
    plugin = plugin_at(tmp_path, monkeypatch)
    async def check():
        local = tmp_path / '.local'
        qqdir = local / 'channels/qqbot'
        wxsent, qqsent = [], []
        async def wxsend(chat, payload):
            wxsent.append(payload)
            return 'wx-receipt'
        async def qqsend(chat, content, reply_to=None):
            qqsent.append((content, reply_to))
            return SimpleNamespace(success=True, message_id='qq-receipt')
        async def qqimage(chat, path, reply_to=None):
            qqsent.append(({'image': path}, reply_to))
            return SimpleNamespace(success=True, message_id='qq-image')
        adapter = SimpleNamespace(send=qqsend, send_image_file=qqimage)
        gateway = SimpleNamespace(_is_user_authorized_for_source=lambda s: True,
                                  _intake_adapter_for=lambda s: adapter)
        wx, wxclients, wxrunner = await start(local, config(local, 'weixin'), wxsend)
        qq, qqclients, qqrunner = await start(qqdir, config(qqdir, 'qqbot'))
        async def dispatch(text, ident, platform='qqbot', images=()):
            result = await plugin.dispatch(event(text, ident, platform, images=images), gateway)
            await drain(qq if platform == 'qqbot' else wx)
            return result
        try:
            assert (await dispatch('help', 'same-id'))['reason'] == 'codex-qq-chat'
            assert 'Codex QQ助手' in qqsent[-1][0]
            assert '桌面和QQ都会收到提问' in qqsent[-1][0]
            assert all('切换 '+h in qqsent[-1][0] for h in ['ubuntu', 'macmini', 'macbook', 'windows'])
            assert not wxsent and not any(c.calls for c in qqclients.values())
            await dispatch('help', 'same-id', 'weixin')
            assert 'Codex 微信助手' in wxsent[-1]
            await dispatch('主机列表', 'list')
            assert '切换 macmini' in qqsent[-1][0]
            for host in ['macmini', 'macbook', 'windows', 'ubuntu']:
                await dispatch('切换 '+host, 'switch-'+host)
                await dispatch('新建会话 '+host, 'new-'+host)
                await dispatch('开始 '+host, 'start-'+host)
            assert not any(c.calls for c in wxclients.values())
            assert qq.chat.selected() == 'ubuntu' and wx.chat.current() is None
            q = await qq.local.add('thread-1', [{'id': 'q', 'question': '确认 QQ', 'options': ['继续']}], key='qq-question')
            assert q['outbound'] == 'sent' and qqsent[-1][1] == 'start-ubuntu'
            assert (await dispatch(q['id']+' 1', 'reply'))['reason'] == 'codex-qq-harness'
            assert qq.question_row(q['id'])['state'] == 'delivered'
            assert 'QQ用户' in qqclients['ubuntu'].calls[-1][1]['input'][0]['text']
            await dispatch('切换 macbook', 'leave')
            assert wx.chat.selected() == 'ubuntu'
            before = len(qqsent)
            qq.local.chat.enqueue('old-status', '原主机状态')
            qq.chat.schedule_flush()
            await drain(qq)
            assert len(qqsent) == before
            path = tmp_path / 'photo.png'
            Image.new('RGB', (8, 8)).save(path)
            await dispatch('看图', 'image', images=[str(path)])
            mac = qq.bridges['macbook']
            turn = {'id': 'turn-1', 'status': 'completed', 'items': [
                {'type': 'agentMessage', 'phase': 'final_answer', 'text': 'QQ完成'},
                {'type': 'imageGeneration', 'savedPath': str(path)}]}
            await mac.chat.completed(mac.chat.current(), turn)
            await drain(qq)
            assert any(isinstance(payload, dict) and reply == 'image' for payload, reply in qqsent)
            assert 'QQ完成' in qqsent[-1][0]
            await dispatch('切换 ubuntu', 'return')
            assert ('【Ubuntu】\n原主机状态', 'return') in qqsent
            assert len(wxsent) == 1
            async def transcribe(e):
                return '', ['当前主机']
            gateway._transcribe_pending_audio_event_once = transcribe
            voice = event('', 'voice')
            voice.media_urls, voice.media_types = ['/tmp/voice.silk'], ['audio/silk']
            assert (await plugin.dispatch(voice, gateway))['reason'] == 'codex-qq-chat'
            await drain(qq)
            assert '当前主机：Ubuntu' in qqsent[-1][0]
            assert qqsent[-1][1] == 'voice'
            native_voice = event('[Voice] 切换 windows', 'native-voice')
            native_voice.raw_message = {'attachments':[{'content_type':'audio/silk','filename':'voice.silk'}]}
            await plugin.dispatch(native_voice, gateway)
            await drain(qq)
            assert qq.chat.selected() == 'windows'
            assert wx.chat.selected() == 'ubuntu'
            native_voice.text, native_voice.message_id = '[Voice] [语音识别失败]', 'failed-native-voice'
            assert (await plugin.dispatch(native_voice, gateway))['reason'] == 'codex-qq-chat-transcription-failed'
            assert qq.chat.selected() == 'windows'
            async with ClientSession(connector=UnixConnector(path=str(qqdir / 'bridge.sock'))) as client:
                async with client.post('http://localhost/messages', json={'platform':'weixin',
                        'chat_id':'owner','user_id':'owner','text':'help','message_id':'wrong-platform'}) as r:
                    assert r.status == 403
            with pytest.raises(Conflict):
                await wx.reply({'platform':'weixin','chat_id':'owner','user_id':'owner',
                                'text':q['id']+' 1','message_id':'wrong-channel'})
        finally:
            await plugin.close_outbound()
            await qqrunner.cleanup()
            await wxrunner.cleanup()
    asyncio.run(check())


def test_qq_unbound_users_passthrough_and_bound_outage_does_not_start_hermes(tmp_path, monkeypatch):
    plugin = plugin_at(tmp_path, monkeypatch)
    directory = tmp_path / '.local/channels/qqbot'
    directory.mkdir(parents=True)
    (directory / 'config.json').write_text(json.dumps(config(directory, 'qqbot')))
    async def check():
        sent = []
        async def send(chat, text, **kwargs):
            sent.append(text)
            return SimpleNamespace(success=True, message_id='receipt')
        gateway = SimpleNamespace(_is_user_authorized_for_source=lambda s: True,
                                  _intake_adapter_for=lambda s: SimpleNamespace(send=send))
        try:
            assert await plugin.dispatch(event('help', 'foreign', user='stranger'), gateway) is None
            assert not plugin.OUTBOUND and not sent
            result = await plugin.dispatch(event('help', 'offline'), gateway)
            assert result['reason'] == 'codex-qq-chat-unavailable'
            assert len(sent) == 1
            assert (directory/'outbound.sock').stat().st_mode & 0o777 == 0o600
        finally: await plugin.close_outbound()
    asyncio.run(check())


def test_qq_notification_before_inbound_is_queued(tmp_path):
    async def check():
        with pytest.raises(ChannelNeedsMessage):
            await send_qqbot('owner', 'help', socket_path=str(tmp_path/'absent.sock'))
    asyncio.run(check())


def test_qq_pending_question_resumes_with_live_context_and_send_scope_is_checked(tmp_path, monkeypatch):
    plugin = plugin_at(tmp_path, monkeypatch)
    async def check():
        directory = tmp_path / '.local/channels/qqbot'
        hub, clients, runner = await start(directory, config(directory, 'qqbot'))
        clients['ubuntu'].turns['thread-1'] = [{'id':'turn-1','status':'inProgress'}]
        sent = []
        async def send(chat, content, reply_to=None):
            sent.append((content, reply_to))
            return SimpleNamespace(success=True, message_id='receipt')
        gateway = SimpleNamespace(_is_user_authorized_for_source=lambda s: True,
                                  _intake_adapter_for=lambda s: SimpleNamespace(send=send))
        try:
            q = await hub.local.add('thread-1', [{'id':'q','question':'待恢复的问题','options':[]}], key='queued')
            assert q['outbound'] == 'blocked_session'
            await plugin.dispatch(event('help', 'fresh'), gateway)
            await drain(hub)
            assert hub.question_row(q['id'])['outbound'] == 'sent'
            assert not hub.channel_blocked
            assert any('待恢复的问题' in text and reply == 'fresh' for text, reply in sent)
            before = len(sent)
            async with ClientSession(connector=UnixConnector(path=str(directory/'outbound.sock'))) as client:
                async with client.post('http://localhost/send',json={'chat_id':'other','payload':'wrong recipient'}) as r:
                    assert r.status == 403
                async with client.post('http://localhost/send',json={'chat_id':'owner','payload':{'image':str(tmp_path/'outside.png')}}) as r:
                    assert r.status == 400
                gateway._is_user_authorized_for_source = lambda s: False
                async with client.post('http://localhost/send',json={'chat_id':'owner','payload':'revoked'}) as r:
                    assert r.status == 403
            assert len(sent) == before
        finally:
            await plugin.close_outbound()
            await runner.cleanup()
    asyncio.run(check())
