"""Connect to the EXISTING app-server, never spawn another model or restart it."""
import asyncio
import contextlib
import itertools
import aiohttp


class RpcError(RuntimeError):
    def __init__(self, error):
        self.code = error.get('code')
        self.method = None
        super().__init__('Codex RPC 被拒绝，code='+str(self.code))


class Codex:
    def __init__(self, path, on_event=None):
        self.path, self.on_event = path, on_event
        self.pending, self.counter, self.events = {}, itertools.count(1), set()
        self.ws = self.session = self.reader = None

    async def connect(self):
        self.session = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(self.path)),
                                             timeout=aiohttp.ClientTimeout(total=20))
        self.ws = await self.session.ws_connect('http://localhost/', max_msg_size=64*1024*1024, heartbeat=20)
        self.reader = asyncio.create_task(self.read())
        await self.rpc('initialize', {'clientInfo': {'name': 'codex_wechat_harness', 'version': '0.1.0'},
            'capabilities': {'experimentalApi': True}})
        await self.ws.send_json({'method': 'initialized'})

    async def read(self):
        try:
            async for frame in self.ws:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = frame.json()
                if 'method' not in data and data.get('id') in self.pending:
                    future = self.pending[data['id']]
                    if not future.done():
                        if 'error' in data:
                            future.set_exception(RpcError(data['error']))
                        else:
                            future.set_result(data.get('result', {}))
                elif self.on_event:
                    task = asyncio.create_task(self.on_event(data))
                    self.events.add(task)
                    task.add_done_callback(self.events.discard)
        finally:
            for f in self.pending.values():
                if not f.done(): f.set_exception(ConnectionError('Codex 控制连接已断开'))

    async def rpc(self, method, params):
        ident = 'wechat-'+str(next(self.counter))
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.ws.send_json({'id': ident, 'method': method, 'params': params})
            try:
                return await asyncio.wait_for(future, 20)
            except RpcError as exc:
                exc.method = method
                raise
        finally:
            self.pending.pop(ident, None)

    async def latest(self, thread_id):
        result = await self.rpc('thread/turns/list', {'threadId': thread_id, 'limit': 1,
            'sortDirection': 'desc', 'itemsView': 'summary'})
        turns = result.get('data') or []
        if not turns: raise ValueError('该 Codex 任务没有可续接的执行记录')
        return turns[0]

    async def close(self):
        if self.ws: await self.ws.close()
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception): await self.reader
        if self.session: await self.session.close()
