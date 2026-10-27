import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from test_hosts import drain
from test_qq import config, event, plugin_at, start


@pytest.mark.parametrize('platform', ['weixin', 'qqbot'])
def test_slow_host_switch_succeeds_without_unavailable_feedback(tmp_path, monkeypatch, platform):
    plugin = plugin_at(tmp_path, monkeypatch)

    async def check():
        directory = plugin.channel_dir(platform)
        sent, feedback = [], []

        async def send(chat_id, payload):
            sent.append(payload)
            return 'receipt'

        async def ack(chat_id, text, **kwargs):
            feedback.append(text)

        hub, clients, runner = await start(directory, config(directory, platform), send)
        gateway = SimpleNamespace(_is_user_authorized_for_source=lambda _: True,
                                  _intake_adapter_for=lambda _: SimpleNamespace(send=ack))
        try:
            await hub.chat.accept({'platform': platform, 'chat_id': 'owner', 'user_id': 'owner',
                                   'text': '新建会话 本地任务', 'message_id': 'new'})
            await drain(hub)
            rpc = clients['ubuntu'].rpc

            async def slow_unsubscribe(method, params):
                if method == 'thread/unsubscribe':
                    # Real switch in the incident took 7s; the old 1s deadline
                    # reported failure even though this operation completed.
                    await asyncio.sleep(1.2)
                return await rpc(method, params)

            clients['ubuntu'].rpc = slow_unsubscribe
            result = await plugin.dispatch(event('切换 macmini', 'switch', platform), gateway)
            await drain(hub)
            assert result['reason'].endswith('-chat')
            assert hub.chat.selected() == 'mac-mini'
            assert any('已切换到 Mac mini' in text for text in sent)
            assert not feedback
            assert [m for m, _ in clients['ubuntu'].calls].count('thread/unsubscribe') == 1
        finally:
            await plugin.close_outbound()
            await runner.cleanup()

    asyncio.run(check())


@pytest.mark.parametrize('platform', ['weixin', 'qqbot'])
def test_acknowledgement_timeout_reports_pending_without_replaying(tmp_path, monkeypatch, platform):
    plugin = plugin_at(tmp_path, monkeypatch)
    monkeypatch.setattr(plugin, 'CHAT_TIMEOUT', 0.02)
    directory = plugin.channel_dir(platform)
    directory.mkdir(parents=True)
    (directory / 'config.json').write_text(json.dumps(config(directory, platform)))

    async def check():
        received, feedback = [], []
        finished = asyncio.Event()

        async def ready(request):
            return web.json_response({'ok': True})

        async def message(request):
            received.append(await request.json())
            await asyncio.sleep(0.08)
            finished.set()
            return web.json_response({'handled': True})

        async def ack(chat_id, text, **kwargs):
            feedback.append(text)

        app = web.Application()
        app.router.add_post('/channel-ready', ready)
        app.router.add_post('/messages', message)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.UnixSite(runner, str(directory / 'bridge.sock')).start()
        gateway = SimpleNamespace(_is_user_authorized_for_source=lambda _: True,
                                  _intake_adapter_for=lambda _: SimpleNamespace(send=ack))
        try:
            result = await plugin.dispatch(event('切换 ubuntu', 'slow', platform), gateway)
            assert result['reason'].endswith('-chat-pending')
            assert len(feedback) == 1 and '等待' in feedback[0]
            assert '不可用' not in feedback[0] and '桌面' not in feedback[0]
            await asyncio.wait_for(finished.wait(), 1)
            assert len(received) == 1
        finally:
            await plugin.close_outbound()
            await runner.cleanup()

    asyncio.run(check())


@pytest.mark.parametrize('platform', ['weixin', 'qqbot'])
def test_missing_bridge_still_reports_unavailable(tmp_path, monkeypatch, platform):
    plugin = plugin_at(tmp_path, monkeypatch)
    directory = plugin.channel_dir(platform)
    directory.mkdir(parents=True)
    (directory / 'config.json').write_text(json.dumps(config(directory, platform)))

    async def check():
        feedback = []

        async def ack(chat_id, text, **kwargs):
            feedback.append(text)

        gateway = SimpleNamespace(_is_user_authorized_for_source=lambda _: True,
                                  _intake_adapter_for=lambda _: SimpleNamespace(send=ack))
        try:
            result = await plugin.dispatch(event('切换 ubuntu', 'offline', platform), gateway)
            assert result['reason'].endswith('-chat-unavailable')
            assert len(feedback) == 1 and '不可用' in feedback[0]
        finally:
            await plugin.close_outbound()

    asyncio.run(check())
