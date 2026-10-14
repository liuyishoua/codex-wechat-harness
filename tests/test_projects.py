import asyncio
from types import SimpleNamespace

from harness.codex import RpcError
from harness.projects import attach, project_id
from test_chat import setup


class Projects:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    async def rpc(self, method, params):
        self.calls.append((method, params))
        if method == 'project/list':
            return self.pages[params['cursor']]
        if method == 'project/create':
            return {'project': {'id': 'created'}}
        return {}


def test_windows_project_lookup_paginates_and_preserves_existing_project():
    async def check():
        c = Projects({None: {'data': [], 'nextCursor': 'next'}, 'next': {'data': [
            {'id': 'windows-project', 'roots': [{'path': 'E:\\leo-project'}]}]}})
        cfg = {'desktop_project_sync': True, 'remote_platform': 'windows'}
        assert await project_id(c, 'e:/leo-project', cfg) == 'windows-project'
        assert [m for m, p in c.calls] == ['project/list', 'project/list']
        c.calls.clear()
        for thread in [
            {'id': 'owned', 'cwd': 'E:/leo-project', 'projectId': 'another-project'},
            {'id': 'elsewhere', 'cwd': 'E:/other'},
            {'id': 'ephemeral', 'cwd': 'E:/leo-project', 'ephemeral': True},
        ]:
            assert not await attach(c, thread, 'e:/leo-project', cfg)
        assert not c.calls
    asyncio.run(check())


def test_missing_project_is_idempotent_and_ambiguous_roots_are_not_reassigned():
    async def check():
        cfg = {'desktop_project_sync': True}
        c = Projects({None: {'data': []}})
        assert await attach(c, {'id': 'old', 'cwd': '/Users/didi/leo-project'}, '/Users/didi/leo-project', cfg)
        assert c.calls[-1] == ('thread/metadata/update', {'threadId': 'old', 'projectId': 'created'})
        await project_id(c, '/Users/didi/leo-project', cfg)
        creations = [p for m, p in c.calls if m == 'project/create']
        assert creations[0] == creations[1]
        c = Projects({None: {'data': [{'id': n, 'roots': [{'path': '/tmp/p'}]} for n in ['one', 'two']]}})
        assert not await attach(c, {'id': 'old', 'cwd': '/tmp/p'}, '/tmp/p', cfg)
        assert all(m == 'project/list' for m, p in c.calls)
    asyncio.run(check())


def test_new_thread_inherits_policy_and_has_native_project(tmp_path):
    async def check():
        b, c, _ = setup(tmp_path)
        b.config = {**b.config, 'desktop_project_sync': True}
        original = c.rpc
        async def rpc(method, params):
            if method == 'project/list':
                return {'data': [{'id': 'project', 'roots': [{'path': b.chat.cwd}]}]}
            return await original(method, params)
        c.rpc = rpc
        await b.chat.start_thread()
        assert c.calls[-1] == ('thread/start', {'cwd': b.chat.cwd, 'projectId': 'project'})
    asyncio.run(check())


def test_older_server_keeps_chat_compatible():
    async def check():
        async def rpc(method, params):
            raise RpcError({'code': -32601})
        assert await project_id(SimpleNamespace(rpc=rpc), '/tmp/p', {'desktop_project_sync': True}) is None
    asyncio.run(check())
