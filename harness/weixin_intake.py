"""Keep repeated owner input while retaining upstream message-ID replay protection.

Hermes' Weixin adapter also deduplicates by text for five minutes. That heuristic
cannot distinguish a replay from a new 'continue' sent by the bridge owner.
Install a reversible, task-local exception for the exact bound private chat.
"""
from contextvars import ContextVar
from functools import wraps
import json


def install(config_path, adapter_class=None, dedup_class=None, guess_chat_type=None):
    if adapter_class is None:
        from gateway.platforms.weixin import WeixinAdapter, _guess_chat_type
        from gateway.platforms.helpers import MessageDeduplicator
        adapter_class, dedup_class, guess_chat_type = WeixinAdapter, MessageDeduplicator, _guess_chat_type
    original_process = adapter_class._process_message
    original_duplicate = dedup_class.is_duplicate
    scope = ContextVar('codex_weixin_owner_intake', default=None)

    @wraps(original_process)
    async def process(adapter, message):
        sender = str(message.get('from_user_id') or '').strip()
        ident = str(message.get('message_id') or '').strip()
        allowed = False
        if sender and ident:
            try:
                config = json.loads(config_path.read_text())
                kind, chat_id = guess_chat_type(message, adapter._account_id)
                allowed = (config.get('platform', 'weixin') == 'weixin'
                           and config.get('chat_enabled') is True and kind == 'dm'
                           and chat_id == config.get('chat_id')
                           and sender in config.get('allowed_users', []))
            except (OSError, ValueError, TypeError, AttributeError):
                pass  # Missing/invalid binding retains the adapter's default protection.
        token = scope.set((adapter._dedup, sender) if allowed else None)
        try:
            return await original_process(adapter, message)
        finally:
            scope.reset(token)

    @wraps(original_duplicate)
    def duplicate(cache, key, *args, **kwargs):
        current = scope.get()
        if current and current[0] is cache and key.startswith('content:' + current[1] + ':'):
            return False
        return original_duplicate(cache, key, *args, **kwargs)

    adapter_class._process_message = process
    dedup_class.is_duplicate = duplicate

    def restore():
        # Do not overwrite a newer plugin/SDK replacement installed after us.
        if adapter_class._process_message is process:
            adapter_class._process_message = original_process
        if dedup_class.is_duplicate is duplicate:
            dedup_class.is_duplicate = original_duplicate
    return restore
