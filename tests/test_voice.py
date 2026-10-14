import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
import pytest

from test_chat import setup, drain, CONFIG


def plugin_at(tmp_path, monkeypatch, enabled=True):
    spec = importlib.util.spec_from_file_location('voice_plugin', Path(__file__).parents[1] / 'hermes_plugin/__init__.py')
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    (tmp_path / '.local').mkdir()
    (tmp_path / '.local/config.json').write_text(json.dumps({**CONFIG, 'chat_enabled': enabled}))
    monkeypatch.setattr(plugin, 'ROOT', tmp_path)
    return plugin


def event(text='', user='owner'):
    source = SimpleNamespace(platform='weixin', chat_id='owner', user_id=user)
    return SimpleNamespace(source=source, text=text, message_id='voice', message_type='voice',
                           media_urls=['/hermes/cache/audio/clip.silk'], media_types=['audio/silk'])


def test_voice_reuses_hermes_stt_and_reaches_codex_start_and_steer(tmp_path, monkeypatch):
    from harness.service import make_app
    plugin = plugin_at(tmp_path, monkeypatch)
    async def check():
        b, c, sent = setup(tmp_path)
        runner = web.AppRunner(make_app(b))
        await runner.setup()
        await web.UnixSite(runner, str(tmp_path / '.local/bridge.sock')).start()
        calls = []
        async def transcribe(e):
            calls.append(e)
            return '"帮我检查项目"', ['幫我檢查項目']
        gateway = SimpleNamespace(_is_user_authorized_for_source=lambda s: True,
                                  _intake_adapter_for=lambda s: None,
                                  _transcribe_pending_audio_event_once=transcribe)
        try:
            e = event()
            assert (await plugin.dispatch(e, gateway))['reason'] == 'codex-wechat-chat'
            await drain(b)
            assert [p for m, p in c.calls if m == 'turn/start'][-1]['input'] == [{'type': 'text', 'text': '帮我检查项目'}]
            assert (await plugin.dispatch(e, gateway))['action'] == 'skip'
            await drain(b)
            assert [m for m, p in c.calls].count('turn/start') == 1
            e.message_id = 'voice-next'
            await plugin.dispatch(e, gateway)
            await drain(b)
            assert [p for m, p in c.calls if m == 'turn/steer'][-1]['input'][0]['text'] == '帮我检查项目'
        finally:
            await runner.cleanup()
    asyncio.run(check())


@pytest.mark.parametrize('result', [('', []), ('""', [' ']), ('partial', ['only one']), RuntimeError('STT unavailable')])
def test_failed_and_partial_transcription_never_submitted(tmp_path, monkeypatch, result):
    plugin = plugin_at(tmp_path, monkeypatch)
    async def check():
        sent = []
        async def ack(chat, text):
            sent.append(text)
        async def transcribe(e):
            if isinstance(result, Exception):
                raise result
            return result
        gateway = SimpleNamespace(_is_user_authorized_for_source=lambda s: True,
                                  _intake_adapter_for=lambda s: SimpleNamespace(send=ack),
                                  _transcribe_pending_audio_event_once=transcribe)
        e = event()
        if result == ('partial', ['only one']):
            e.media_urls.append('/hermes/cache/audio/second.silk')
            e.media_types.append('audio/silk')
        assert (await plugin.dispatch(e, gateway))['reason'] == 'codex-wechat-chat-transcription-failed'
        assert len(sent) == 1 and '没能完整识别' in sent[0]
    asyncio.run(check())


def test_unauthorized_and_disabled_voice_bypass_stt(tmp_path, monkeypatch):
    plugin = plugin_at(tmp_path, monkeypatch, enabled=False)
    async def check():
        calls = []
        async def transcribe(e):
            calls.append(e)
            return '', []
        gateway = SimpleNamespace(_is_user_authorized_for_source=lambda s: True,
                                  _intake_adapter_for=lambda s: None,
                                  _transcribe_pending_audio_event_once=transcribe)
        assert await plugin.dispatch(event(user='stranger'), gateway) is None
        assert await plugin.dispatch(event(), gateway) is None
        assert not calls
    asyncio.run(check())


def test_transcribed_numbered_answer_returns_to_question_thread(tmp_path, monkeypatch):
    from harness.service import make_app
    from test_chat import message
    plugin = plugin_at(tmp_path, monkeypatch)
    async def check():
        b, c, sent = setup(tmp_path)
        await message(b, '开始任务', 'begin')
        original = b.chat.current()['thread_id']
        q = await b.add(original, [{'id': 'answer', 'question': '是否继续？', 'options': ['继续']}], key='voice-question')
        await message(b, '新建任务 另一个', 'switch')
        runner = web.AppRunner(make_app(b))
        await runner.setup()
        await web.UnixSite(runner, str(tmp_path / '.local/bridge.sock')).start()
        async def transcribe(e):
            return '', [q['id'] + ' 1']
        async def ack(chat, text):
            pass
        gateway = SimpleNamespace(_is_user_authorized_for_source=lambda s: True,
                                  _intake_adapter_for=lambda s: SimpleNamespace(send=ack),
                                  _transcribe_pending_audio_event_once=transcribe)
        try:
            assert (await plugin.dispatch(event(), gateway))['reason'] == 'codex-wechat-harness'
            await drain(b)
            assert b.store.get(q['id'])['state'] == 'delivered'
            assert [p for m, p in c.calls if m == 'turn/steer'][-1]['threadId'] == original
            assert b.chat.current()['thread_id'] != original
        finally:
            await runner.cleanup()
    asyncio.run(check())
