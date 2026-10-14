"""Local Codex ↔ Hermes Weixin harness; filesystem ACLs protect the socket."""
import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from pathlib import Path

from aiohttp import web
from .config import data_dir, load_config, default_state_db, channel_name, message_prefix
from .codex import Codex, RpcError
from .store import Store, Conflict, answers_for, card, parse_reply
from .transport import channel_sender, ChannelNeedsMessage, NotificationDeferred
from .rollouts import Questions
from .chat import Chat
from .approvals import command_question, native_result

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger('codex-wechat')


class Bridge:
    def __init__(self, config, store, sender=None, codex=None):
        self.config, self.store = config, store
        self.sender = sender or channel_sender(config)
        self.codex = codex or Codex(config['codex_socket'], self.event)
        self.native, self.resolved, self.joined = {}, {}, set()
        self.connected = False
        self.channel_blocked = any(r['outbound']=='blocked_session' for r in store.rows())
        self.tasks = set()
        self.desktop_questions = Questions(config.get('codex_state_db', default_state_db()))
        self.chat = Chat(self) if config.get('chat_enabled', False) else None

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        def finished(done):
            self.tasks.discard(done)
            if not done.cancelled() and done.exception():log.warning('background_error=%s',type(done.exception()).__name__)
        task.add_done_callback(finished)
        return task

    async def notify(self, payload):
        # Delivery is independent of task execution and has a bounded wait.
        timeout = max(0.01, min(60, float(self.config.get('notification_timeout', 20))))
        return await asyncio.wait_for(self.sender(self.config['chat_id'], payload), timeout)

    async def add(self, thread_id, questions, key=None, kind='question', runtime_id=None, turn_id=None, defer_send=False):
        latest = await self.codex.latest(thread_id)
        if turn_id and latest['id'] != turn_id:
            raise Conflict('原问题所在执行已结束，不再发送旧问题')
        row, created = self.store.add(key or 'manual:'+str(uuid.uuid4()), thread_id,
            latest['id'], questions, kind, runtime_id)
        if runtime_id is not None:
            self.native[row['id']] = runtime_id
        if created or row['outbound'] == 'queued':
            if defer_send:
                self.spawn(self.send(row))
            else:
                await self.send(row)
        return self.store.get(row['id'])

    async def send(self, row):
        if row['state']!='pending': return
        if row['expires']<time.time():
            self.store.update(row['id'],state='expired')
            return
        latest=await self.codex.latest(row['thread_id'])
        if latest['id']!=row['turn_id']:
            self.store.update(row['id'],state='stale',delivery_error='原问题所在轮次已经结束')
            return
        if self.channel_blocked:
            self.store.update(row['id'],outbound='blocked_session')
            return
        if not self.store.claim_send(row['id']): return
        try:
            receipt = await self.notify(card(row))
            self.store.update(row['id'], outbound='sent', outbound_id=receipt)
            log.info('question=%s notification=sent', row['id'])
        except ChannelNeedsMessage:
            self.channel_blocked=True
            self.store.update(row['id'],outbound='blocked_session',delivery_error=channel_name(self.config)+'需要用户先给机器人发一条消息来恢复会话')
            log.warning('question=%s notification=blocked_session',row['id'])
        except NotificationDeferred:
            self.store.update(row['id'], outbound='queued')
        except Exception as exc:
            self.store.update(row['id'], outbound='uncertain', delivery_error=channel_name(self.config)+'通知未确认：'+type(exc).__name__)
            log.warning('question=%s notification=uncertain error=%s', row['id'], type(exc).__name__)

    async def event(self, data):
        try:
            method, params = data.get('method'), data.get('params') or {}
            if self.chat:
                try: await self.chat.event(method, params)
                except Exception as exc: log.warning('chat_event_error=%s', type(exc).__name__)
            if method == 'item/commandExecution/requestApproval':
                question = command_question(params)
                callback = params.get('approvalId') or params['itemId']
                await self.add(params['threadId'], [question],
                    key='approval:'+params['threadId']+':'+params['turnId']+':'+callback,
                    kind='command_approval', runtime_id=data['id'], turn_id=params['turnId'], defer_send=True)
            elif method == 'item/tool/requestUserInput':
                questions = []
                for i,q in enumerate(params.get('questions') or []):
                    options = [v if isinstance(v,str) else v['label'] for v in q.get('options') or []]
                    questions.append({'id': q.get('id') or str(i+1), 'question': q.get('question') or q.get('title'), 'options': options})
                await self.add(params['threadId'], questions,
                    key='native:'+params['threadId']+':'+params['itemId'], runtime_id=data['id'], turn_id=params['turnId'], defer_send=True)
            elif method == 'serverRequest/resolved':
                request = params.get('requestId')
                future = self.resolved.get(str(request))
                if future and not future.done(): future.set_result(True)
                for ident,native_id in list(self.native.items()):
                    if native_id == request:
                        row = self.store.get(ident)
                        if row and row['state'] == 'pending': self.store.update(ident, state='resolved_elsewhere')
            elif method == 'turn/completed':
                turn = params.get('turn') or {}
                if turn.get('status') == 'failed':
                    await self.add(params['threadId'], [{'id': 'next', 'question': 'Codex 执行失败，任务已保留。需要你补充外部状态或下一步指示；也可以回到 Codex 查看错误详情。', 'options': []}],
                        key='failure:'+params['threadId']+':'+turn['id'], kind='blocker', turn_id=turn['id'], defer_send=True)
        except Exception as exc:
            log.warning('event=%s error=%s', data.get('method'), type(exc).__name__)

    async def subscribe(self, ident):
        if self.chat:
            if not await self.chat.join(ident, automatic=True):
                return
        else:
            await self.codex.rpc('thread/resume', {'threadId': ident, 'excludeTurns': True})
        turns = await self.codex.rpc('thread/turns/list', {'threadId': ident, 'limit': 1, 'itemsView': 'summary'})
        self.joined.add(ident)
        if turns.get('data'):
            self.desktop_questions.turns[ident] = turns['data'][0]['id']

    async def monitor(self):
        delay = 1
        while True:
            try:
                await self.codex.connect()
                self.connected = True
                self.joined.clear()
                delay = 1
                while not self.codex.reader.done():
                    result = ({'data': []} if self.config.get('managed_threads_only') or
                              (self.chat and not self.chat.active_host()) else
                              await self.codex.rpc('thread/loaded/list', {'limit': 100}))
                    for thread in result.get('data') or []:
                        ident = thread if isinstance(thread,str) else thread['id']
                        if self.config.get('thread_ids') and ident not in self.config['thread_ids']: continue
                        if self.chat and self.chat.released(ident): continue
                        if ident not in self.joined:
                            try:
                                await self.subscribe(ident)
                            except RpcError as exc:
                                # A rejected thread does not invalidate the shared connection.
                                log.warning('thread_subscription=skip method=%s code=%s', exc.method, exc.code)
                    for thread_id,call_id,questions,turn_id in self.desktop_questions.poll(self.joined):
                        try: await self.add(thread_id,questions,key='desktop:'+thread_id+':'+call_id,kind='desktop_question',turn_id=turn_id,defer_send=True)
                        except Conflict: pass
                    for thread_id,call_id in self.desktop_questions.resolved:
                        self.store.resolve_desktop(thread_id,call_id)
                    self.desktop_questions.resolved.clear()
                    for row in self.store.rows():
                        if row['state']=='pending' and row['outbound']=='queued': self.spawn(self.send(row))
                    for row,message_id in self.store.waiting():
                        self.spawn(self.deliver(row,message_id))
                    if self.chat:
                        await self.chat.poll()
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('codex_connection=retry error=%s method=%s code=%s',
                            type(exc).__name__, getattr(exc, 'method', None), getattr(exc, 'code', None))
            finally:
                self.connected = False
                self.native.clear()
                await self.codex.close()
            await asyncio.sleep(delay)
            delay = min(30,delay*2)

    async def deliver(self, row, message_id):
        ident = row['id']
        if not self.store.claim_delivery(ident): return
        try:
            answer = answers_for(row, row['answer'])
            latest = await self.codex.latest(row['thread_id'])
            if latest['id'] != row['turn_id']:
                self.store.update(ident, state='stale', delivery_error='任务已进入另一轮，未把旧答案送入新一轮')
                return
            if row['runtime_id'] is not None:
                if ident not in self.native:
                    self.store.update(ident, state='needs_review', delivery_error='原输入请求的控制连接已恢复，请在 Codex 核对；未猜测批准')
                    return
                native_id = self.native[ident]
                future = asyncio.get_running_loop().create_future()
                self.resolved[str(native_id)] = future
                try:
                    await self.codex.ws.send_json({'id': native_id, 'result': native_result(row, answer)})
                    await asyncio.wait_for(future, 15)
                finally:
                    self.resolved.pop(str(native_id),None)
            else:
                text = '['+channel_name(self.config)+'用户对 Codex 问题 '+ident+' 的回复]\n' + '\n'.join(
                    q['question']+'\n回答：'+answer[q['id']]['answers'][0] for q in json.loads(row['questions']))
                params = {'threadId': row['thread_id'], 'input': [{'type':'text','text':text}],
                          'clientUserMessageId': message_prefix(self.config)+':'+message_id}
                if latest['status'] == 'inProgress':
                    await self.codex.rpc('turn/steer', {**params, 'expectedTurnId': row['turn_id']})
                else:
                    await self.codex.rpc('thread/resume', {'threadId':row['thread_id'], 'excludeTurns':True})
                    await self.codex.rpc('turn/start', params)
            self.store.update(ident, state='delivered', delivery_error='')
            log.info('question=%s reply=delivered',ident)
        except (Conflict, ValueError, RpcError) as exc:
            self.store.update(ident, state='needs_review', delivery_error='回复递交被拒绝：'+type(exc).__name__)
        except Exception as exc:
            self.store.update(ident, state='uncertain', delivery_error='回复递交未确认：'+type(exc).__name__)

    def authorized(self,data):
        return (data.get('platform')==self.config.get('platform', 'weixin') and data.get('chat_id')==self.config['chat_id']
                and data.get('user_id') in self.config['allowed_users'])

    async def channel_ready(self,data):
        if not self.authorized(data): raise PermissionError('消息来源不匹配')
        self.channel_blocked=False
        for row in self.store.rows():
            if row['state']=='pending' and row['outbound']=='blocked_session':
                self.store.update(row['id'],outbound='queued',delivery_error='')
                self.spawn(self.send(self.store.get(row['id'])))
        if self.chat:
            self.chat.schedule_flush()
        return {'ok':True}

    async def reply(self, data):
        if not self.authorized(data):
            raise PermissionError('消息来源不匹配')
        parsed = parse_reply(data.get('text',''))
        if not parsed: raise ValueError('回复需包含任务编号')
        ident,text = parsed
        row = self.store.get(ident)
        if not row: raise Conflict('问题不存在或已过期')
        answer = answers_for(row,text)  # Validate BEFORE reserving the user's one answer.
        native_result(row, answer)
        row,claimed = self.store.accept(ident,data['message_id'],text)
        if claimed: self.spawn(self.deliver(row,data['message_id']))
        row=self.store.get(ident)
        messages = {'delivered':'已交回原 Codex 任务，继续处理。', 'received':'回复已收下，正在交回原 Codex 任务。', 'delivering':'回复正在交回 Codex。',
            'stale':'原任务已推进到另一轮，未套用旧答案；请回 Codex 核对。',
            'uncertain':'回复递交回执未确认，已保留，未重复执行。',
            'needs_review':'回复已保留，但未能确认交给 Codex；请查看 Codex 原任务。'}
        return {'id':ident,'state':row['state'],'duplicate':not claimed,'message':messages.get(row['state'],'这条问题已经处理。')}


def make_app(bridge):
    app=web.Application(client_max_size=32768)
    @web.middleware
    async def errors(request, handler):
        try: return await handler(request)
        except web.HTTPException: raise
        except PermissionError: return web.json_response({'error':'来源未授权'},status=403)
        except Conflict as exc: return web.json_response({'error':str(exc)},status=409)
        except (ValueError, KeyError): return web.json_response({'error':'请求格式不完整或回答格式不正确'},status=400)
        except Exception as exc:
            log.warning('http_error=%s',type(exc).__name__)
            return web.json_response({'error':'桥接暂不可用，请查看 Codex'},status=503)
    app.middlewares.append(errors)
    async def health(request):
        return web.json_response({'codex_connected':bridge.connected,'wechat_needs_message':bridge.channel_blocked,
            'platform':bridge.config.get('platform', 'weixin'),'channel_needs_message':bridge.channel_blocked,'subscribed_threads':len(bridge.joined),
            'hosts':bridge.host_status() if hasattr(bridge, 'host_status') else [],
            'chat_enabled':bool(bridge.chat),'project_cwd':bridge.chat.cwd if bridge.chat else None,
            'current_chat':bridge.chat.current() if bridge.chat else None,
            'questions':[{k:r[k] for k in ('id','kind','state','outbound','created')} for r in bridge.store.rows()]})
    async def question(request):
        d=await request.json()
        row=await bridge.add(d['thread_id'],d['questions'],key=d.get('key'),kind=d.get('kind','question'),defer_send=True)
        return web.json_response({k:row[k] for k in ('id','state','outbound')})
    async def reply(request): return web.json_response(await bridge.reply(await request.json()))
    async def ready(request): return web.json_response(await bridge.channel_ready(await request.json()))
    async def message(request):
        data = await request.json()
        if not bridge.authorized(data): raise PermissionError('消息来源不匹配')
        if not bridge.chat: return web.json_response({'handled': False})
        return web.json_response(await bridge.chat.accept(data))
    async def image(request):
        data = await request.json()
        if not bridge.chat: raise ValueError('消息入口未启用')
        if not data.get('key') or not data.get('thread_id'): raise ValueError('缺少发送编号或会话')
        key = 'manual-image:' + data['thread_id'] + ':' + data['key']
        await bridge.chat.queue_image(key, data['path'])
        bridge.chat.schedule_flush()
        with bridge.store.db() as db:
            state = db.execute('SELECT state FROM chat_outbox WHERE key=?', (key,)).fetchone()['state']
        return web.json_response({'key': key, 'state': state})
    async def status(request):
        row=(bridge.question_row(request.match_info['id']) if hasattr(bridge, 'question_row') else
             bridge.store.get(request.match_info['id']))
        if not row: raise web.HTTPNotFound()
        return web.json_response({k:row[k] for k in ('id','state','outbound','answer','delivery_error')})
    app.router.add_get('/health',health)
    app.router.add_post('/questions',question)
    app.router.add_post('/replies',reply)
    app.router.add_post('/channel-ready',ready)
    app.router.add_post('/messages',message)
    app.router.add_post('/images',image)
    app.router.add_get('/questions/{id}',status)
    return app


async def main():
    config=load_config()
    local=data_dir();local.mkdir(parents=True,exist_ok=True,mode=0o700);local.chmod(0o700)
    (local/'config.json').chmod(0o600)
    store=Store(local/'state.sqlite3');store.recover()
    if config.get('hosts'):
        from .hosts import Hosts
        bridge=Hosts(config,store)
    else:
        bridge=Bridge(config,store)
    runner=web.AppRunner(make_app(bridge));await runner.setup()
    path=local/'bridge.sock'
    path.unlink(missing_ok=True)
    await web.UnixSite(runner,str(path)).start();path.chmod(0o600)
    monitor=asyncio.create_task(bridge.monitor())
    try: await asyncio.Event().wait()
    finally:
        monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError): await monitor
        for task in list(bridge.tasks): task.cancel()
        if bridge.tasks: await asyncio.gather(*bridge.tasks,return_exceptions=True)
        await runner.cleanup()


if __name__=='__main__':
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(main())
