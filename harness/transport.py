"""Use the configured Hermes channel without creating another gateway login."""
from functools import partial
from pathlib import Path


class ChannelNeedsMessage(RuntimeError):
    pass


class NotificationDeferred(RuntimeError):
    """The source host is inactive; keep the notification queued."""


def channel_sender(config):
    if config.get('platform', 'weixin') == 'weixin':
        return send_wechat
    if config.get('platform') == 'qqbot':
        return partial(send_qqbot, socket_path=config['gateway_send_socket'])
    raise ValueError('不支持的消息平台')


async def send_qqbot(chat_id, message, *, socket_path):
    import aiohttp
    if not Path(socket_path).is_socket():
        raise ChannelNeedsMessage('QQ 尚未建立回复上下文')
    try:
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=socket_path)) as session:
            async with session.post('http://localhost/send', json={'chat_id': chat_id, 'payload': message}) as response:
                result = await response.json()
                if response.status == 409:
                    raise ChannelNeedsMessage('QQ 回复上下文需要恢复')
                if response.status >= 400 or not result.get('ok'):
                    raise RuntimeError('QQ 发送没有确认回执')
                return result.get('message_id') or ''
    except aiohttp.ClientConnectorError as exc:
        # Connect failures happen before sending; interrupted requests stay uncertain.
        raise ChannelNeedsMessage('QQ 发送入口尚未连接') from exc


async def send_wechat(chat_id, message):
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv()
    from gateway.config import load_gateway_config, Platform
    from gateway.platforms.weixin import send_weixin_direct
    config = load_gateway_config()
    platform = config.platforms.get(Platform.WEIXIN)
    if platform is None or not platform.enabled:
        raise RuntimeError('Hermes 微信通道没有启用')
    result = await send_weixin_direct(extra=platform.extra, token=platform.token,
        chat_id=chat_id, message='' if isinstance(message, dict) else message,
        media_files=[(message['image'], False)] if isinstance(message, dict) else None)
    if not result.get('success'):
        if 'session not ready' in str(result.get('error','')):
            raise ChannelNeedsMessage('微信会话需要用户发一条消息来恢复')
        raise RuntimeError('微信发送没有确认回执')
    return result.get('message_id') or ''
