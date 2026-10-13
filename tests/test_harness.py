import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from harness.service import Bridge
from harness.store import Store, Conflict, answers_for, parse_reply
from harness.rollouts import Questions

Q=[{'id':'answer','question':'如何处理这个卡点？','options':['继续','等一下']}]
CONFIG={'chat_id':'owner','allowed_users':['owner'],'codex_socket':'unused'}
class FakeCodex:
    def __init__(self):
        self.turn={'id':'turn-1','status':'inProgress'};self.calls=[]
    async def latest(self,thread): return self.turn.copy()
    async def rpc(self,method,params): self.calls.append((method,params));return {}

def setup(tmp_path,sender=None):
    async def send(chat,text): return 'wx-receipt'
    codex=FakeCodex();store=Store(tmp_path/'state.sqlite')
    return Bridge(CONFIG,store,sender or send,codex),codex

def run(coro): return asyncio.run(coro)
async def drain(bridge):
    if bridge.tasks: await asyncio.gather(*list(bridge.tasks))

def test_send_and_original_thread_steering(tmp_path):
    async def check():
        b,c=setup(tmp_path);r=await b.add('original',Q,key='one')
        assert r['outbound']=='sent'
        reply={'platform':'weixin','chat_id':'owner','user_id':'owner','message_id':'m1','text':r['id']+' 1'}
        await b.reply(reply);await drain(b)
        assert b.store.get(r['id'])['state']=='delivered'
        method,p=c.calls[-1]
        assert method=='turn/steer' and p['threadId']=='original' and p['expectedTurnId']=='turn-1'
        assert '回答：继续' in p['input'][0]['text']
        await b.reply(reply);await drain(b)
        assert len(c.calls)==1
    run(check())

def test_idle_reply_continues_same_chat(tmp_path):
    async def check():
        b,c=setup(tmp_path);r=await b.add('original',Q);c.turn['status']='completed'
        await b.reply({'platform':'weixin','chat_id':'owner','user_id':'owner','message_id':'m1','text':r['id']+' 继续'})
        await drain(b)
        assert [m for m,p in c.calls]==['thread/resume','turn/start']
        assert all(p['threadId']=='original' for m,p in c.calls)
    run(check())

def test_new_turn_does_not_receive_old_answer(tmp_path):
    async def check():
        b,c=setup(tmp_path);r=await b.add('original',Q);c.turn['id']='different-turn'
        await b.reply({'platform':'weixin','chat_id':'owner','user_id':'owner','message_id':'m1','text':r['id']+' 继续'})
        await drain(b)
        assert b.store.get(r['id'])['state']=='stale' and not c.calls
    run(check())

@pytest.mark.parametrize('field,value',[('user_id','stranger'),('chat_id','group'),('platform','telegram')])
def test_foreign_sender_rejected(tmp_path,field,value):
    async def check():
        b,c=setup(tmp_path);r=await b.add('original',Q)
        data={'platform':'weixin','chat_id':'owner','user_id':'owner','message_id':'m1','text':r['id']+' 1'};data[field]=value
        with pytest.raises(PermissionError): await b.reply(data)
        assert b.store.get(r['id'])['state']=='pending' and not c.calls
    run(check())

def test_outbound_timeout_does_not_resend(tmp_path):
    count=[]
    async def uncertain(chat,text):count.append(text);raise TimeoutError()
    async def check():
        b,c=setup(tmp_path,uncertain)
        r=await b.add('original',Q,key='one');await b.add('original',Q,key='one')
        assert r['outbound']=='uncertain' and len(count)==1
    run(check())

def test_changed_idempotency_and_answer_rejected(tmp_path):
    s=Store(tmp_path/'state.sqlite');r,_=s.add('key','t','turn',Q)
    with pytest.raises(Conflict):s.add('key','other-thread','turn',Q)
    s.accept(r['id'],'message','1')
    with pytest.raises(Conflict):s.accept(r['id'],'message','2')
    with pytest.raises(Conflict):s.accept(r['id'],'message-2','2')

def test_crash_recovery_preserves_uncertainty_and_queued_reply(tmp_path):
    s=Store(tmp_path/'state.sqlite');r,_=s.add('key','t','turn',Q)
    s.claim_send(r['id']);s.accept(r['id'],'message','1');s.recover()
    assert s.get(r['id'])['outbound']=='uncertain' and s.waiting()[0][1]=='message'
    assert s.claim_delivery(r['id']) and not s.claim_delivery(r['id'])
    s.recover();assert s.get(r['id'])['state']=='uncertain' and not s.waiting()

def test_numeric_multi_and_normal_chat():
    row={'questions':json.dumps(Q)}
    assert answers_for(row,'2')['answer']['answers']==['等一下']
    assert parse_reply('普通聊天') is None
    assert parse_reply('/codex Cabc123 好的')==('CABC123','好的')
    row={'questions':json.dumps(Q+[{'id':'next','question':'另一个问题'}])}
    with pytest.raises(ValueError):answers_for(row,'一个答案')
    assert answers_for(row,'{"answer":"1","next":"可以"}')['answer']['answers']==['继续']

def test_desktop_question_watch_starts_at_eof(tmp_path):
    import sqlite3
    db=tmp_path/'state.sqlite';rollout=tmp_path/'rollout.jsonl'
    event={'type':'response_item','payload':{'type':'function_call','name':'request_user_input_async','call_id':'call-1','arguments':json.dumps({'questions':[{'title':'卡点信息','options':['A','B']}]})}}
    line=json.dumps(event)+'\n';rollout.write_text(line)
    with sqlite3.connect(db) as c:
        c.execute('create table threads(id text,rollout_path text)');c.execute('insert into threads values(?,?)',('thread',str(rollout)))
    watcher=Questions(db);assert watcher.poll(['thread'])==[]
    with rollout.open('a') as f:f.write(line)
    assert watcher.poll(['thread'])==[('thread','call-1',[{'id':'1','question':'卡点信息','options':['A','B']}],None)]
    assert watcher.poll(['thread'])==[]

def test_hermes_ordinary_and_unauthorized_messages_not_consumed(tmp_path,monkeypatch):
    path=Path(__file__).parents[1]/'hermes_plugin/__init__.py'
    spec=importlib.util.spec_from_file_location('wx_plugin',path);plugin=importlib.util.module_from_spec(spec);spec.loader.exec_module(plugin)
    (tmp_path/'.local').mkdir();(tmp_path/'.local/config.json').write_text(json.dumps(CONFIG));monkeypatch.setattr(plugin,'ROOT',tmp_path)
    source=SimpleNamespace(platform=SimpleNamespace(value='weixin'),chat_id='owner',user_id='owner')
    gateway=SimpleNamespace(_is_user_authorized_for_source=lambda source:False)
    assert run(plugin.dispatch(SimpleNamespace(source=source,text='你在吗'),gateway)) is None
    assert run(plugin.dispatch(SimpleNamespace(source=source,text='CABC123 可以'),gateway)) is None

def test_blocked_wechat_resumes_only_on_authorized_new_message(tmp_path):
    from harness.transport import ChannelNeedsMessage
    calls=[]
    async def sender(chat,text):
        calls.append(text)
        if len(calls)==1:raise ChannelNeedsMessage()
        return 'receipt'
    async def check():
        b,c=setup(tmp_path,sender);r=await b.add('original',Q,key='one')
        assert r['outbound']=='blocked_session' and b.channel_blocked
        second=await b.add('original',[{'id':'another','question':'第二个卡点'}],key='two')
        assert len(calls)==1 and second['outbound']=='blocked_session'
        with pytest.raises(PermissionError):await b.channel_ready({'platform':'weixin','chat_id':'owner','user_id':'stranger'})
        assert len(calls)==1
        await b.channel_ready({'platform':'weixin','chat_id':'owner','user_id':'owner'});await drain(b)
        assert len(calls)==3 and b.store.get(r['id'])['outbound']=='sent'
        await b.channel_ready({'platform':'weixin','chat_id':'owner','user_id':'owner'});await drain(b)
        assert len(calls)==3
    run(check())

def test_native_response_requires_resolved_ack(tmp_path):
    async def check():
        b,c=setup(tmp_path)
        async def send_json(data):
            assert data['id']=='request-1'
            assert data['result']['answers']['answer']['answers']==['继续']
            await b.event({'method':'serverRequest/resolved','params':{'requestId':'request-1','threadId':'original'}})
        c.ws=SimpleNamespace(send_json=send_json)
        r=await b.add('original',Q,key='native',runtime_id='request-1')
        await b.reply({'platform':'weixin','chat_id':'owner','user_id':'owner','message_id':'m1','text':r['id']+' 1'});await drain(b)
        assert b.store.get(r['id'])['state']=='delivered' and not c.calls
    run(check())

def test_real_unix_plugin_handoff_and_hermes_passthrough(tmp_path,monkeypatch):
    from aiohttp import web
    path=Path(__file__).parents[1]/'hermes_plugin/__init__.py'
    spec=importlib.util.spec_from_file_location('wx_plugin_live',path);plugin=importlib.util.module_from_spec(spec);spec.loader.exec_module(plugin)
    (tmp_path/'.local').mkdir();(tmp_path/'.local/config.json').write_text(json.dumps(CONFIG));monkeypatch.setattr(plugin,'ROOT',tmp_path)
    async def check():
        from harness.service import make_app
        b,c=setup(tmp_path);runner=web.AppRunner(make_app(b));await runner.setup()
        await web.UnixSite(runner,str(tmp_path/'.local/bridge.sock')).start()
        sent=[]
        async def ack(chat,text):sent.append((chat,text))
        gateway=SimpleNamespace(_is_user_authorized_for_source=lambda s:True,_intake_adapter_for=lambda s:SimpleNamespace(send=ack))
        source=SimpleNamespace(platform=SimpleNamespace(value='weixin'),chat_id='owner',user_id='owner')
        try:
            r=await b.add('original',Q,key='one')
            result=await plugin.dispatch(SimpleNamespace(source=source,text=r['id']+' 2',message_id='wx-real-shape'),gateway)
            await drain(b)
            assert result['action']=='skip' and b.store.get(r['id'])['state']=='delivered'
            assert len(sent)==1 and c.calls[0][0]=='turn/steer'
            normal=await plugin.dispatch(SimpleNamespace(source=source,text='今天吃什么',message_id='normal'),gateway)
            assert normal is None and len(sent)==1
        finally:await runner.cleanup()
    run(check())

def test_desktop_answer_suppresses_card_and_tracks_original_turn(tmp_path):
    import sqlite3
    db=tmp_path/'state.sqlite';rollout=tmp_path/'rollout.jsonl';rollout.write_text('')
    with sqlite3.connect(db) as c:
        c.execute('create table threads(id text,rollout_path text)');c.execute('insert into threads values(?,?)',('thread',str(rollout)))
    watcher=Questions(db);watcher.poll(['thread'])
    started={'type':'event_msg','payload':{'type':'task_started','turn_id':'turn-original'}}
    question={'type':'response_item','payload':{'type':'function_call','name':'request_user_input_async','call_id':'call-1','arguments':json.dumps({'questions':[{'title':'还需要帮助吗'}]})}}
    answered={'type':'event_msg','payload':{'type':'user_message','message':'<send_user_message_question_reply>\n'+json.dumps([{'questionItemId':json.dumps(['request_user_input_async','call-1',0]),'answer':'不用'}])+'\n</send_user_message_question_reply>'}}
    with rollout.open('a') as f:f.write('\n'.join(json.dumps(e) for e in (started,question,answered))+'\n')
    assert watcher.poll(['thread'])==[] and watcher.turns['thread']=='turn-original'
    assert ('thread','call-1') in watcher.resolved

def test_malformed_question_cannot_reserve_delivery(tmp_path):
    s=Store(tmp_path/'state.sqlite')
    with pytest.raises(ValueError):s.add('a','t','turn',[{'question':'missing id'}])
    with pytest.raises(ValueError):s.add('b','t','turn',Q+Q)
    with pytest.raises(ValueError):s.add('c','t','turn',[{'id':'q','question':'bad options','options':'text'}])
    assert s.rows()==[]
