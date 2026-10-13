"""Use Hermes' configured Weixin adapter and server-issued context token."""
class ChannelNeedsMessage(RuntimeError):
    pass


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
