import asyncio
import json
from types import SimpleNamespace

import pytest

from harness.approvals import command_question
from harness.store import Conflict
from test_harness import setup, drain
from test_hosts import setup as hosts_setup, message as hosts_message, drain as hosts_drain, data


def request(ident=20, callback=None):
    params = {'threadId':'original','turnId':'turn-1','itemId':'exec-1','kind':'command',
              'command':'mindwiki tool context --max-chars 9000','cwd':'/Users/didi/project',
              'reason':'是否允许在临时目录解包并读取代码索引？',
              'availableDecisions':['accept',{'acceptWithExecpolicyAmendment':{}},'cancel']}
    if callback: params['approvalId'] = callback
    return {'id':ident,'method':'item/commandExecution/requestApproval','params':params}


@pytest.mark.parametrize('answer,decision',[('1','accept'),('允许本次','accept'),('2','cancel')])
@pytest.mark.parametrize('platform',['weixin','qqbot'])
def test_explicit_command_approval_returns_decision_and_waits_for_ack(tmp_path,answer,decision,platform):
    async def check():
        b,c=setup(tmp_path)
        b.config={**b.config,'platform':platform}
        frames=[]
        async def send_json(frame):
            frames.append(frame)
            await b.event({'method':'serverRequest/resolved','params':{'requestId':20}})
        c.ws=SimpleNamespace(send_json=send_json)
        await b.event(request())
        await drain(b)
        q=b.store.rows()[0]
        assert q['kind']=='command_approval' and q['outbound']=='sent'
        choices=json.loads(q['questions'])[0]
        assert choices['options']==['允许本次','拒绝并停止本轮']
        assert 'mindwiki' in choices['question'] and '临时目录' in choices['question']
        response={'platform':platform,'chat_id':'owner','user_id':'owner','message_id':'reply','text':q['id']+' '+answer}
        await b.reply(response);await drain(b)
        assert frames==[{'id':20,'result':{'decision':decision}}]
        assert b.store.get(q['id'])['state']=='delivered'
        await b.reply(response);await drain(b)
        assert len(frames)==1 and not c.calls
    asyncio.run(check())


def test_free_text_is_not_guessed_as_approval_and_replay_updates_runtime_id(tmp_path):
    async def check():
        b,c=setup(tmp_path)
        await b.event(request());await drain(b)
        q=b.store.rows()[0]
        with pytest.raises(ValueError,match='选项'):
            await b.reply({'platform':'weixin','chat_id':'owner','user_id':'owner','message_id':'ambiguous','text':q['id']+' 好的'})
        assert b.store.get(q['id'])['state']=='pending'
        await b.event(request(21));await drain(b)
        assert len(b.store.rows())==1 and b.native[q['id']]==21
        await b.event(request(22,'subcommand-2'));await drain(b)
        assert len(b.store.rows())==2
    asyncio.run(check())


def test_incomplete_or_long_command_has_no_accept_option():
    p=request()['params']
    for command in ['', 'x'*3000]:
        question=command_question({**p,'command':command})
        assert question['decisions']==['cancel']
        assert len(question['question'])<=2000
    assert command_question({**p,'kind':'stdin'})['decisions']==['cancel']


def test_remote_approval_and_progress_follow_selected_host(tmp_path):
    async def check():
        hub,clients,sent=hosts_setup(tmp_path)
        await hosts_message(hub,'切换 macmini','switch')
        await hosts_message(hub,'开始','start')
        mac=hub.bridges['mac-mini']
        req=request();req['params']['threadId']='thread-1'
        await hosts_message(hub,'切换 ubuntu','leave')
        before=len(sent)
        await mac.event(req)
        progress={'method':'item/completed','params':{'threadId':'thread-1','turnId':'turn-1',
                  'item':{'id':'progress-1','type':'agentMessage','phase':'commentary','text':'正在检查代码'}}}
        await mac.event(progress);await mac.event(progress);await hosts_drain(hub)
        assert len(sent)==before
        q=mac.store.rows()[0]
        with pytest.raises(Conflict,match='切换'):
            await hub.reply(data(q['id']+' 1','wrong-host'))
        await hosts_message(hub,'切换 macmini','return')
        assert sum('正在检查代码' in p for p in sent)==1
        assert mac.store.get(q['id'])['outbound']=='sent'
        assert not any('acceptForSession' in p for p in sent)
        await hosts_message(hub,'是否已经收到？','followup')
        assert '正在等待命令授权' in sent[-1]
    asyncio.run(check())
