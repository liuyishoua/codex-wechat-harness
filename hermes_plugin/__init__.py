"""Hermes transports WeChat and QQ; each bridge selects its Codex conversation."""
import asyncio
import json
import os
import re
from pathlib import Path
import aiohttp
from aiohttp import web
from opencc import OpenCC

ROOT=Path(__file__).resolve().parents[1]

def data_dir():
    return Path(os.environ.get('CODEX_WECHAT_HOME', str(ROOT/'.local'))).expanduser().resolve()
COMMAND=re.compile(r'^\s*(?:/?codex\s+)?C[0-9a-f]{6}\s+\S',re.I)
OUTBOUND = {}
OUTBOUND_LOCK = asyncio.Lock()
SIMPLIFIED_CHINESE = OpenCC('t2s')
# Host switches wait for Codex subscription changes before acknowledging intake.
# Leave time for those RPCs while staying below Hermes' default 30s hook limit.
CHAT_TIMEOUT = 15


def channel_dir(platform):
    return data_dir() if platform == 'weixin' else data_dir() / 'channels' / platform


async def feedback(adapter, source, text, platform, message_id):
    if adapter:
        try:
            kwargs = {'reply_to': message_id} if platform == 'qqbot' else {}
            await adapter.send(source.chat_id, text, **kwargs)
        except Exception:
            pass


async def ensure_qq_outbound(directory, event, gateway):
    async with OUTBOUND_LOCK:
        await open_qq_outbound(directory, event, gateway)


async def open_qq_outbound(directory, event, gateway):
    """Reuse the live QQ adapter, including its reply id and image uploader."""
    key = str(directory)
    existing = OUTBOUND.get(key)
    if existing:
        existing.update(source=event.source, reply_to=str(event.message_id or ''), gateway=gateway)
        return
    entry = {'source': event.source, 'reply_to': str(event.message_id or ''), 'gateway': gateway,
             'loop': asyncio.get_running_loop(), 'lock': asyncio.Lock()}
    path = directory / 'outbound.sock'

    async def send(request):
        data = await request.json()
        current = json.loads((directory / 'config.json').read_text())
        source = entry['source']
        if (current.get('platform') != 'qqbot' or data.get('chat_id') != current['chat_id']
                or source.chat_id != current['chat_id'] or source.user_id not in current['allowed_users']
                or not entry['gateway']._is_user_authorized_for_source(source)):
            return web.json_response({'ok': False}, status=403)
        adapter = entry['gateway']._intake_adapter_for(source)
        if not adapter or not entry['reply_to']:
            return web.json_response({'ok': False}, status=409)
        payload = data.get('payload')
        async with entry['lock']:
            try:
                if isinstance(payload, dict) and set(payload) == {'image'}:
                    image = Path(payload['image']).resolve()
                    if not image.is_relative_to(directory.resolve()) or not image.is_file():
                        return web.json_response({'ok': False}, status=400)
                    result = await adapter.send_image_file(source.chat_id, str(image), reply_to=entry['reply_to'])
                elif isinstance(payload, str) and payload.strip():
                    result = await adapter.send(source.chat_id, payload, reply_to=entry['reply_to'])
                else:
                    return web.json_response({'ok': False}, status=400)
            except Exception:
                return web.json_response({'ok': False}, status=503)
        if not getattr(result, 'success', False):
            status = 409 if getattr(result, 'error', '') == 'Not connected' else 503
            return web.json_response({'ok': False}, status=status)
        return web.json_response({'ok': True, 'message_id': getattr(result, 'message_id', '')})

    app = web.Application(client_max_size=128 * 1024)
    app.router.add_post('/send', send)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.UnixSite(runner, str(path)).start()
        path.chmod(0o600)
    except BaseException:
        await runner.cleanup()
        raise
    entry.update(runner=runner, path=path, inode=path.stat().st_ino)
    OUTBOUND[key] = entry


async def close_outbound():
    async with OUTBOUND_LOCK:
        entries = list(OUTBOUND.values())
        OUTBOUND.clear()
        for entry in entries:
            await entry['runner'].cleanup()
            path = entry['path']
            if path.exists() and path.stat().st_ino == entry['inode']:
                path.unlink()


def unload():
    for entry in list(OUTBOUND.values()):
        entry['loop'].call_soon_threadsafe(lambda: asyncio.create_task(close_outbound()))
        break

async def dispatch(event, gateway, session_store=None, **kwargs):
    source=event.source
    platform=getattr(source.platform,'value',source.platform)
    if platform not in ('weixin', 'qqbot'): return
    directory=channel_dir(platform)
    config_path=directory/'config.json'
    if not config_path.is_file(): return
    config=json.loads(config_path.read_text())
    if config.get('platform', 'weixin') != platform: return
    if source.chat_id!=config['chat_id'] or source.user_id not in config['allowed_users']: return
    if not gateway._is_user_authorized_for_source(source): return
    label = 'QQ' if platform == 'qqbot' else '微信'
    reason = 'codex-qq' if platform == 'qqbot' else 'codex-wechat'
    if platform == 'qqbot':
        try:
            await ensure_qq_outbound(directory, event, gateway)
        except Exception:
            await feedback(gateway._intake_adapter_for(source), source,
                           'Codex QQ 入口暂不可用，消息未转交；请稍后重试。', platform, event.message_id)
            return {'action': 'skip', 'reason': reason+'-chat-unavailable'}
    media = list(getattr(event, 'media_urls', None) or [])
    types = list(getattr(event, 'media_types', None) or [])
    images = [path for n, path in enumerate(media) if n < len(types) and types[n].startswith('image/')]
    kind = getattr(getattr(event, 'message_type', None), 'value', getattr(event, 'message_type', None))
    audio = [path for n, path in enumerate(media)
             if (n < len(types) and types[n].startswith('audio/'))
             or (n >= len(types) and kind == 'voice')]
    text = event.text or ''
    # QQ has already transcribed voice before this hook and marks it [Voice].
    # Normalize only SDK voice events, so spoken management commands work too.
    raw = getattr(event, 'raw_message', None)
    if platform == 'qqbot' and config.get('chat_enabled', False) and isinstance(raw, dict):
        from gateway.platforms.qqbot import QQAdapter
        attachments = raw.get('attachments') or []
        voice = any(isinstance(a, dict) and QQAdapter._is_voice_content_type(
            str(a.get('content_type', '')), str(a.get('filename', ''))) for a in attachments)
        if voice:
            if '[Voice] [语音识别失败]' in text:
                await feedback(gateway._intake_adapter_for(source), source,
                               '语音暂时没能完整识别；请重发语音或改用文字。', platform, event.message_id)
                return {'action': 'skip', 'reason': reason+'-chat-transcription-failed'}
            text = SIMPLIFIED_CHINESE.convert(re.sub(r'(?m)^\[Voice\]\s*', '', text))
    if config.get('chat_enabled', False) and media and len(images) + len(audio) != len(media):
        adapter=gateway._intake_adapter_for(source)
        if adapter:
            await feedback(adapter, source, 'Codex '+label+'入口支持文字、图片和语音；其他附件请在 Codex 桌面发送。', platform, event.message_id)
        return {'action':'skip','reason':reason+'-chat-unsupported-media'}
    if config.get('chat_enabled', False) and audio:
        adapter = gateway._intake_adapter_for(source)
        try:
            # The hook runs before Hermes' normal STT stage. Reuse its configured
            # provider, local fallback and per-event transcription cache here.
            transcribe = getattr(gateway, '_transcribe_pending_audio_event_once', None)
            if not transcribe:
                raise RuntimeError('Hermes transcription hook unavailable')
            _, transcripts = await asyncio.wait_for(transcribe(event), timeout=180)
            if len(transcripts) != len(set(audio)) or not all(isinstance(t, str) and t.strip() for t in transcripts):
                raise ValueError('Voice transcription incomplete')
            parts = ([text.strip()] if text.strip() else []) + [SIMPLIFIED_CHINESE.convert(t.strip()) for t in transcripts]
            text = '\n\n'.join(parts)
        except Exception:
            # Never submit an inaudible/partial command or execute via Hermes.
            if adapter:
                await feedback(adapter, source, '语音已收到，但暂时没能完整识别；请重发语音或改用文字。', platform, event.message_id)
            return {'action':'skip', 'reason':reason+'-chat-transcription-failed'}
    if images or not COMMAND.match(text):
        try:
            async with asyncio.timeout(CHAT_TIMEOUT), aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(directory/'bridge.sock')),timeout=aiohttp.ClientTimeout(total=CHAT_TIMEOUT)) as session:
                async with session.post('http://localhost/channel-ready',json={'platform':platform,'chat_id':source.chat_id,'user_id':source.user_id}) as response:
                    await response.read()
                if config.get('chat_enabled', False):
                    async with session.post('http://localhost/messages', json={'platform':platform,
                            'chat_id':source.chat_id,'user_id':source.user_id,
                            'message_id':str(event.message_id or ''),'text':text, 'images':images}) as response:
                        if response.status >= 400:
                            raise RuntimeError('Codex 消息入口拒绝请求')
                        result = await response.json()
                        if result.get('handled'):
                            return {'action':'skip','reason':reason+'-chat'}
        except asyncio.TimeoutError:
            # A missing HTTP acknowledgement does not mean the operation failed.
            # The bridge can still finish and deliver its normal result; never replay it.
            if config.get('chat_enabled', False):
                await feedback(gateway._intake_adapter_for(source), source,
                               '响应较慢，正在等待处理确认，请留意后续结果，暂勿重复发送。', platform, event.message_id)
                return {'action':'skip','reason':reason+'-chat-pending'}
        except Exception:
            # Once chat routing is enabled, failures must not silently execute via Hermes.
            if config.get('chat_enabled', False):
                adapter=gateway._intake_adapter_for(source)
                if adapter:
                    await feedback(adapter, source, 'Codex 入口暂不可用，消息接收未确认；请到桌面核对后继续。', platform, event.message_id)
                return {'action':'skip','reason':reason+'-chat-unavailable'}
        return
    try:
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(directory/'bridge.sock')),
                timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.post('http://localhost/channel-ready',json={'platform':platform,
                    'chat_id':source.chat_id,'user_id':source.user_id}) as response:
                await response.read()
            async with session.post('http://localhost/replies',json={'platform':platform,
                    'chat_id':source.chat_id,'user_id':source.user_id,'message_id':str(event.message_id or ''),'text':text}) as response:
                result=await response.json()
                message=result.get('message') or result.get('error') or '回复未被接收，请查看 Codex。'
    except Exception:
        message='Codex 桥接暂时不可用，未执行这条回复；请到 Codex 查看。'
    adapter=gateway._intake_adapter_for(source)
    if adapter:
        await feedback(adapter, source, message, platform, event.message_id)
    return {'action':'skip','reason':reason+'-harness'}

def register(ctx):
    # Hermes discovers this symlinked plugin without installing the bridge package.
    from runpy import run_path
    install = run_path(str(Path(__file__).resolve().parents[1] / 'harness/weixin_intake.py'))['install']
    ctx.on_unload(install(channel_dir('weixin') / 'config.json'))
    ctx.register_hook('pre_gateway_dispatch',dispatch)
    ctx.on_unload(unload)
