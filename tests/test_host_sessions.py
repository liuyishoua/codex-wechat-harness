"""Discover and resume host-wide sessions without changing their workspaces."""
import asyncio

import pytest

from harness.service import Bridge
from test_chat import setup, message, CONFIG
from test_hosts import setup as host_setup, message as host_message

TARGET = '01a15c4c-f65a-7be1-aeb9-6e9cae4e805f'
OTHER_CWD = '/home/owner/Documents/Codex/2026-10-21/leo-project'


def test_all_projects_are_listed_and_number_switch_retains_real_directory(tmp_path):
    async def check():
        b,c,sent=setup(tmp_path)
        c.threads={'original':{'id':'original','cwd':b.chat.cwd,'name':'同名','updatedAt':1},
                   TARGET:{'id':TARGET,'cwd':OTHER_CWD,'name':'同名','updatedAt':2},
                   'child':{'id':'child','cwd':'/tmp/child','name':'不可输入','canAcceptDirectInput':False}}
        await message(b,'会话列表','list')
        assert OTHER_CWD not in sent[-1] and b.chat.cwd not in sent[-1]
        assert '不可输入' not in sent[-1]
        queries=[p for m,p in c.calls if m=='thread/list']
        assert all('cwd' not in p and p['sortDirection']=='desc' for p in queries)
        await message(b,'切换 同名','ambiguous')
        assert b.chat.current() is None
        await message(b,'切换 1','number')
        assert b.chat.current()['thread_id']==TARGET and b.chat.current()['cwd']==OTHER_CWD
        assert OTHER_CWD in sent[-1]
        await message(b,'当前会话','status')
        assert OTHER_CWD in sent[-1] and TARGET in sent[-1]
        await message(b,'继续','input')
        assert [p for m,p in c.calls if m=='turn/start'][-1]['threadId']==TARGET
        assert all('cwd' not in p for m,p in c.calls if m in ('thread/resume','turn/start'))
        restored=Bridge(CONFIG,b.store,b.sender,c)
        assert restored.chat.current()['cwd']==OTHER_CWD
    asyncio.run(check())


def test_uuid_switch_reads_target_without_listing_or_starting_a_turn(tmp_path):
    async def check():
        b,c,sent=setup(tmp_path)
        await message(b,'新建会话 原任务','old')
        old=b.chat.current()['thread_id']
        c.threads[TARGET]={'id':TARGET,'cwd':OTHER_CWD,'name':'目标会话'}
        c.calls.clear()
        await message(b,'切换 '+TARGET,'uuid')
        assert b.chat.current()['thread_id']==TARGET
        assert b.joined=={TARGET} and b.chat.released(old)
        methods=[m for m,p in c.calls]
        assert methods.index('thread/read')<methods.index('thread/resume')<methods.index('thread/unsubscribe')
        assert not any(m in ('thread/list','turn/start','turn/interrupt','thread/metadata/update') for m in methods)
        assert OTHER_CWD in sent[-1] and TARGET in sent[-1]
    asyncio.run(check())


@pytest.mark.parametrize('change', [None, {'canAcceptDirectInput':False}, {'ephemeral':True}, {'archived':True}, {'cwd':None}])
def test_invalid_uuid_target_preserves_selection_and_subscription(tmp_path,change):
    async def check():
        b,c,sent=setup(tmp_path)
        await message(b,'新建会话 原任务','old')
        old=b.chat.current()['thread_id']
        if change is not None:c.threads[TARGET]={'id':TARGET,'cwd':OTHER_CWD,'name':'目标'}|change
        c.calls.clear()
        await message(b,'切换 '+TARGET,'bad')
        assert b.chat.current()['thread_id']==old and b.joined=={old}
        assert '未切换' in sent[-1]
        assert [m for m,p in c.calls]==['thread/read']
    asyncio.run(check())


def test_cross_project_name_switch_handles_pagination(tmp_path):
    async def check():
        b,c,sent=setup(tmp_path)
        c.threads={'first':{'id':'first','cwd':b.chat.cwd,'name':'原项目'},
                   TARGET:{'id':TARGET,'cwd':OTHER_CWD,'name':'目标项目'}}
        rpc=c.rpc
        async def pages(method,params):
            if method=='thread/list':
                c.calls.append((method,params))
                return {'data':[c.threads[TARGET]]} if params.get('cursor')=='page2' else {'data':[c.threads['first']], 'nextCursor':'page2'}
            return await rpc(method,params)
        c.rpc=pages
        await message(b,'切换 目标项目','switch')
        assert b.chat.current()['thread_id']==TARGET
        assert [p.get('cursor') for m,p in c.calls if m=='thread/list']==[None,'page2']
    asyncio.run(check())


def test_same_uuid_and_cross_project_threads_stay_on_selected_host(tmp_path):
    async def check():
        hub,clients,sent=host_setup(tmp_path)
        clients['ubuntu'].threads[TARGET]={'id':TARGET,'cwd':OTHER_CWD,'name':'本机会话'}
        clients['mac-mini'].threads[TARGET]={'id':TARGET,'cwd':'/Users/leo/other-project','name':'远端会话'}
        await host_message(hub,'会话列表','local-list')
        assert '本机会话' in sent[-1] and '远端会话' not in sent[-1]
        await host_message(hub,'切换 '+TARGET,'local-id')
        assert hub.chat.current()['name']=='本机会话'
        await host_message(hub,'切换 macmini','host')
        await host_message(hub,'会话列表','remote-list')
        assert '远端会话' in sent[-1] and '本机会话' not in sent[-1]
        await host_message(hub,'切换 '+TARGET,'remote-id')
        assert hub.chat.current()['name']=='远端会话'
        assert hub.chat.current()['cwd']=='/Users/leo/other-project'
        assert hub.local.chat.current()['name']=='本机会话'
        await host_message(hub,'当前主机','remote-status')
        assert '会话目录：/Users/leo/other-project' in sent[-1]
    asyncio.run(check())


def test_new_session_keeps_configured_default_after_cross_project_switch(tmp_path):
    async def check():
        b,c,sent=setup(tmp_path)
        c.threads[TARGET]={'id':TARGET,'cwd':OTHER_CWD,'name':'其他项目'}
        await message(b,'切换 '+TARGET,'switch')
        await message(b,'新建会话 新任务','new')
        assert b.chat.current()['cwd']==CONFIG['project_cwd']
        starts=[p for m,p in c.calls if m=='thread/start']
        assert len(starts)==1 and starts[0]['cwd']==CONFIG['project_cwd']
    asyncio.run(check())


def test_windows_host_keeps_remote_directory_in_session_status(tmp_path):
    async def check():
        b,c,sent=setup(tmp_path)
        b.config={**b.config,'remote_platform':'windows'}
        cwd='D:\\Documents\\Codex\\other-project'
        c.threads[TARGET]={'id':TARGET,'cwd':cwd,'name':'Windows 任务'}
        await message(b,'切换 '+TARGET,'windows')
        await message(b,'当前会话','status')
        assert b.chat.current()['cwd']==cwd
        assert '项目：other-project' in sent[-1] and cwd in sent[-1]
    asyncio.run(check())
