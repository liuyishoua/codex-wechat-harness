import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from aiohttp import web

from harness.images import snapshot, local_result_images
from harness.service import make_app
from harness.store import Conflict
from harness.transport import ChannelNeedsMessage
from test_chat import setup, drain, message, CONFIG


def photo(tmp_path, color='blue'):
    path = tmp_path / (color + '.png')
    Image.new('RGB', (24, 16), color).save(path)
    return str(path)


def test_image_only_and_caption_reach_start_and_steer_and_deduplicate(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        path = photo(tmp_path)
        data = {**CONFIG, 'platform': 'weixin', 'user_id': 'owner', 'text': '',
                'message_id': 'photo', 'images': [path]}
        await b.chat.accept(data)
        await drain(b)
        start = [p for m, p in c.calls if m == 'turn/start'][-1]
        image = start['input'][0]
        assert image['type'] == 'localImage'
        assert Path(image['path']).read_bytes() == Path(path).read_bytes()
        assert Path(image['path']).stat().st_mode & 0o777 == 0o600
        assert (await b.chat.accept(data))['duplicate']
        with pytest.raises(Conflict):
            await b.chat.accept({**data, 'images': [photo(tmp_path, 'red')]})
        # Cache cleanup must not remove the durable copy handed to Codex.
        Path(path).unlink()
        assert Path(image['path']).exists()
        await b.chat.accept({**data, 'message_id': 'caption', 'text': 'help', 'images': [image['path']]})
        await drain(b)
        steer = [p for m, p in c.calls if m == 'turn/steer'][-1]
        assert steer['input'] == [{'type': 'text', 'text': 'help'}, image]
        with pytest.raises(PermissionError):
            await b.chat.accept({**data, 'user_id': 'stranger'})
    asyncio.run(check())


def test_invalid_images_never_enter_codex(tmp_path):
    bad = tmp_path / 'bad.png'
    bad.write_text('not an image')
    with pytest.raises(ValueError):
        snapshot(str(bad), tmp_path / 'images')
    async def check():
        b, c, sent = setup(tmp_path)
        for images in [[str(bad)], ['https://example.org/p.png'], [photo(tmp_path)] * 10]:
            with pytest.raises(ValueError):
                await b.chat.accept({'platform': 'weixin', 'chat_id': 'owner', 'user_id': 'owner',
                                     'message_id': 'invalid', 'text': '', 'images': images})
        assert not c.calls
    asyncio.run(check())


def test_plugin_real_unix_socket_routes_image_event(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('image_plugin', Path(__file__).parents[1] / 'hermes_plugin/__init__.py')
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
        source = SimpleNamespace(platform='weixin', chat_id='owner', user_id='owner')
        gateway = SimpleNamespace(_is_user_authorized_for_source=lambda s: True, _intake_adapter_for=lambda s: None)
        try:
            event = SimpleNamespace(source=source, text='', message_id='wx-image',
                                    media_urls=[photo(tmp_path)], media_types=['image/png'])
            assert (await plugin.dispatch(event, gateway))['action'] == 'skip'
            await drain(b)
            assert [p for m, p in c.calls if m == 'turn/start'][0]['input'][0]['type'] == 'localImage'
            event.media_types = ['application/pdf']
            assert (await plugin.dispatch(event, gateway))['reason'] == 'codex-wechat-chat-unsupported-media'
        finally:
            await runner.cleanup()
    asyncio.run(check())


def test_result_image_and_native_generation_send_once(tmp_path):
    async def check():
        b, c, sent = setup(tmp_path)
        await message(b, '开始', 'begin')
        session = b.chat.current()
        path = photo(tmp_path)
        turn = {'id': 'turn-1', 'status': 'completed', 'items': [
            {'type': 'agentMessage', 'phase': 'final_answer', 'text': f'图片：![结果](sandbox:{path})'},
            {'type': 'imageGeneration', 'savedPath': path}]}
        await b.chat.completed(session, turn)
        await b.chat.completed(session, turn)
        await drain(b)
        images = [p for p in sent if isinstance(p, dict)]
        assert len(images) == 1
        assert Path(images[0]['image']).read_bytes() == Path(path).read_bytes()
    asyncio.run(check())


def test_image_outbox_recovery_and_unknown_send_not_replayed(tmp_path):
    calls = []
    async def send(chat, payload):
        calls.append(payload)
        if len(calls) == 1:
            raise ChannelNeedsMessage()
    async def check():
        b, c, sent = setup(tmp_path, send)
        await b.chat.queue_image('image', photo(tmp_path))
        await b.chat.flush()
        assert b.channel_blocked
        await b.channel_ready({'platform': 'weixin', 'chat_id': 'owner', 'user_id': 'owner'})
        await drain(b)
        assert len(calls) == 2
        async def timeout(chat, payload):
            raise TimeoutError()
        b.sender = timeout
        await b.chat.queue_image('unknown', photo(tmp_path))
        await b.chat.flush()
        b.sender = send
        await b.chat.flush()
        assert len(calls) == 2
        with b.store.db() as db:
            assert db.execute("SELECT state FROM chat_outbox WHERE key='unknown'").fetchone()['state'] == 'uncertain'
    asyncio.run(check())


def test_only_explicit_local_images_are_extracted():
    assert local_result_images('![x](sandbox:/tmp/a%20b.png)\nMEDIA: /tmp/c.png\n/tmp/d.png\n![remote](https://example.org/a.png)') == ['/tmp/a b.png', '/tmp/c.png']


def test_manual_image_endpoint_deduplicates(tmp_path):
    from aiohttp import ClientSession, UnixConnector
    async def check():
        b, c, sent = setup(tmp_path)
        runner = web.AppRunner(make_app(b))
        await runner.setup()
        socket = tmp_path / 'bridge.sock'
        await web.UnixSite(runner, str(socket)).start()
        try:
            async with ClientSession(connector=UnixConnector(path=str(socket))) as client:
                payload = {'key': 'one', 'thread_id': 'original', 'path': photo(tmp_path)}
                for _ in range(2):
                    async with client.post('http://localhost/images', json=payload) as response:
                        assert response.status == 200
                        assert (await response.json())['state'] in ('queued', 'sending', 'sent')
                    await drain(b)
                payload['path'] = photo(tmp_path, 'red')
                async with client.post('http://localhost/images', json=payload) as response:
                    assert response.status == 409
            assert len(sent) == 1 and isinstance(sent[0], dict)
        finally:
            await runner.cleanup()
    asyncio.run(check())


def test_transport_passes_image_to_hermes(monkeypatch, tmp_path):
    import sys
    from harness.transport import send_wechat
    calls = []
    platform = SimpleNamespace(enabled=True, extra={}, token='fake')
    monkeypatch.setitem(sys.modules, 'hermes_cli.env_loader', SimpleNamespace(load_hermes_dotenv=lambda: None))
    monkeypatch.setitem(sys.modules, 'gateway.config', SimpleNamespace(
        load_gateway_config=lambda: SimpleNamespace(platforms={'weixin': platform}),
        Platform=SimpleNamespace(WEIXIN='weixin')))
    async def direct(**kwargs):
        calls.append(kwargs)
        return {'success': True, 'message_id': 'receipt'}
    monkeypatch.setitem(sys.modules, 'gateway.platforms.weixin', SimpleNamespace(send_weixin_direct=direct))
    async def check():
        path = photo(tmp_path)
        assert await send_wechat('owner', {'image': path}) == 'receipt'
        assert calls[-1]['message'] == '' and calls[-1]['media_files'] == [(path, False)]
        await send_wechat('owner', '文字')
        assert calls[-1]['message'] == '文字' and calls[-1]['media_files'] is None
    asyncio.run(check())
