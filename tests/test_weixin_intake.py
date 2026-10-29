import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from harness.weixin_intake import install


def test_owner_repeats_reach_intake_but_replays_and_other_scopes_do_not(tmp_path):
    class Dedup:
        def __init__(self): self.seen = set()
        def is_duplicate(self, key):
            old = key in self.seen
            self.seen.add(key)
            return old
    class Adapter:
        def __init__(self): self._account_id = 'bot'; self._dedup = Dedup(); self.received = []
        async def _process_message(self, message):
            if self._dedup.is_duplicate(message['message_id']): return
            key = 'content:' + message['from_user_id'] + ':' + hashlib.md5(message['text'].encode()).hexdigest()
            if self._dedup.is_duplicate(key): return
            await asyncio.sleep(0)
            self.received.append(message['message_id'])
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'chat_enabled': True, 'chat_id': 'owner', 'allowed_users': ['owner']}))
    def kind(message, _): return ('group', 'room') if message.get('group') else ('dm', message['from_user_id'])
    original_process, original_duplicate = Adapter._process_message, Dedup.is_duplicate
    restore = install(config, Adapter, Dedup, kind)
    async def check():
        a = Adapter()
        def msg(ident, sender='owner', **extra):return {'message_id':ident,'from_user_id':sender,'text':'继续',**extra}
        await a._process_message(msg('before', 'other'))
        await a._process_message(msg('other-repeat', 'other'))
        await a._process_message(msg('one'))
        await a._process_message(msg('one'))  # Same upstream event must execute only once.
        await a._process_message(msg('two'))
        await a._process_message(msg('three'))
        assert a.received == ['before', 'one', 'two', 'three']
        # An authorized sender in an unrelated group retains content dedup.
        await a._process_message(msg('group-one', group=True))
        await a._process_message(msg('group-two', group=True))
        assert a.received[-1] == 'group-one'
        # Without a stable ID, the existing fallback still protects against replays.
        await a._process_message(msg(''))
        await a._process_message(msg(''))
        assert a.received.count('') == 0  # Group message primed the content fingerprint.
        # A second adapter/cache and concurrent tasks cannot borrow this exemption.
        b=Adapter()
        await asyncio.gather(a._process_message(msg('four')), b._process_message(msg('other-1','other')))
        await b._process_message(msg('other-2','other'))
        assert b.received == ['other-1']
        config.write_text(json.dumps({'chat_enabled':False,'chat_id':'owner','allowed_users':['owner']}))
        await a._process_message(msg('disabled'))
        assert a.received[-1] == 'four'
    try:asyncio.run(check())
    finally:restore()
    assert Adapter._process_message is original_process
    assert Dedup.is_duplicate is original_duplicate


def test_real_weixin_adapter_keeps_distinct_identical_owner_messages(tmp_path):
    wx = pytest.importorskip('gateway.platforms.weixin')
    from gateway.platforms.helpers import MessageDeduplicator
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'chat_enabled':True,'chat_id':'owner','allowed_users':['owner']}))
    adapter = object.__new__(wx.WeixinAdapter)
    adapter._poll_session = object();adapter._account_id='bot';adapter._dedup=MessageDeduplicator()
    adapter._token='';adapter._typing_cache={};adapter.platform=SimpleNamespace(value='weixin')
    adapter._is_dm_intake_allowed=lambda _:True
    received=[];adapter._enqueue_text_event=received.append
    adapter.build_source=lambda **kw:SimpleNamespace(chat_type=kw['chat_type'])
    async def baseline():
        for ident in ['old-one','old-two']:
            await adapter._process_message({'from_user_id':'owner','message_id':ident,'item_list':[{'type':1,'text_item':{'text':'继续'}}]})
        assert [event.message_id for event in received] == ['old-one']
    asyncio.run(baseline())
    restore=install(config)
    async def check():
        def message(ident):return {'from_user_id':'owner','message_id':ident,'item_list':[{'type':1,'text_item':{'text':'继续'}}]}
        await adapter._process_message(message('one'))
        await adapter._process_message(message('one'))
        await adapter._process_message(message('two'))
        assert [event.message_id for event in received] == ['old-one','one','two']
        # Missing IDs keep content-fingerprint replay protection.
        a=message('');a['item_list'][0]['text_item']['text']='无编号消息'
        await adapter._process_message(a)
        await adapter._process_message(a)
        assert sum(event.message_id is None for event in received) == 1
    try:asyncio.run(check())
    finally:restore()
