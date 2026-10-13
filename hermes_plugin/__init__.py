"""Hermes transports WeChat; the bridge selects the Codex conversation."""
import json
import os
import re
from pathlib import Path
import aiohttp

ROOT=Path(__file__).resolve().parents[1]

def data_dir():
    return Path(os.environ.get('CODEX_WECHAT_HOME', str(ROOT/'.local'))).expanduser().resolve()
COMMAND=re.compile(r'^\s*(?:/?codex\s+)?C[0-9a-f]{6}\s+\S',re.I)

async def dispatch(event, gateway, session_store=None, **kwargs):
    source=event.source
    if getattr(source.platform,'value',source.platform)!='weixin': return
    config_path=data_dir()/'config.json'
    if not config_path.is_file(): return
    config=json.loads(config_path.read_text())
    if source.chat_id!=config['chat_id'] or source.user_id not in config['allowed_users']: return
    if not gateway._is_user_authorized_for_source(source): return
    media = list(getattr(event, 'media_urls', None) or [])
    types = list(getattr(event, 'media_types', None) or [])
    images = [path for n, path in enumerate(media) if n < len(types) and types[n].startswith('image/')]
    if config.get('chat_enabled', False) and media and len(images) != len(media):
        adapter=gateway._intake_adapter_for(source)
        if adapter:
            try: await adapter.send(source.chat_id,'Codex 微信入口支持文字和图片；其他附件请在 Codex 桌面发送。')
            except Exception: pass
        return {'action':'skip','reason':'codex-wechat-chat-unsupported-media'}
    if images or not COMMAND.match(event.text or ''):
        try:
            async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(data_dir()/'bridge.sock')),timeout=aiohttp.ClientTimeout(total=8 if images else 1)) as session:
                async with session.post('http://localhost/channel-ready',json={'platform':'weixin','chat_id':source.chat_id,'user_id':source.user_id}) as response:
                    await response.read()
                if config.get('chat_enabled', False):
                    async with session.post('http://localhost/messages', json={'platform':'weixin',
                            'chat_id':source.chat_id,'user_id':source.user_id,
                            'message_id':str(event.message_id or ''),'text':event.text or '', 'images':images}) as response:
                        if response.status >= 400:
                            raise RuntimeError('Codex 消息入口拒绝请求')
                        result = await response.json()
                        if result.get('handled'):
                            return {'action':'skip','reason':'codex-wechat-chat'}
        except Exception:
            # Once chat routing is enabled, failures must not silently execute via Hermes.
            if config.get('chat_enabled', False):
                adapter=gateway._intake_adapter_for(source)
                if adapter:
                    try: await adapter.send(source.chat_id,'Codex 入口暂不可用，消息接收未确认；请到桌面核对后继续。')
                    except Exception: pass
                return {'action':'skip','reason':'codex-wechat-chat-unavailable'}
        return
    try:
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(data_dir()/'bridge.sock')),
                timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.post('http://localhost/replies',json={'platform':'weixin',
                    'chat_id':source.chat_id,'user_id':source.user_id,'message_id':str(event.message_id or ''),'text':event.text}) as response:
                result=await response.json()
                message=result.get('message') or result.get('error') or '回复未被接收，请查看 Codex。'
    except Exception:
        message='Codex 桥接暂时不可用，未执行这条回复；请到 Codex 查看。'
    adapter=gateway._intake_adapter_for(source)
    if adapter:
        try: await adapter.send(source.chat_id,message)
        except Exception: pass
    return {'action':'skip','reason':'codex-wechat-harness'}

def register(ctx):
    ctx.register_hook('pre_gateway_dispatch',dispatch)
