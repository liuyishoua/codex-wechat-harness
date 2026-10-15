"""WeChat conversation routing over the existing Codex app-server connection."""
import asyncio
import json
import ntpath
import time
from pathlib import Path

from .config import ROOT, channel_name, message_prefix
from .images import snapshot, local_result_images, MAX_IMAGES
from .codex import RpcError
from .store import Conflict
from .transport import ChannelNeedsMessage, NotificationDeferred
from .remote import windows, same_cwd
from .projects import project_id, attach as attach_project

HELP = '''🤖 Codex 微信助手
项目：{project}

💬 直接聊天
文字、图片和语音会接着当前会话处理。
首次聊天自动接上最近的会话。

📋 会话管理
会话列表 → 查看带序号的会话
切换 2 → 切到列表中的第 2 个会话
切换 名称 → 按完整名称切换
当前会话 → 查看正在聊的任务
新建会话 名称 → 开启新的会话
断开会话 → 断开当前桥接连接

🙋 需要你回答时
桌面和微信都会收到提问。
按提示回复：Cxxxxxx 你的答案
答案回到提问的原会话。

发送 help 或 帮助，可再看本说明。'''


def display_name(name):
    """Keep each list entry on one bounded line on a phone."""
    name = ' '.join(name.split())
    return name if len(name) <= 48 else name[:47] + '…'


def list_number(argument):
    if len(argument) >= 2 and (argument[0], argument[-1]) in [('「','」'), ('『','』'), ('"','"')]:
        argument = argument[1:-1].strip()
    return int(argument) if argument.isdecimal() else None


def command(text):
    text = text.strip()
    explicit = False
    if text.lower() == '/codex':
        return 'help', '', True
    if text.lower().startswith(('/codex ', 'codex ')):
        text = text.split(None, 1)[1].strip()
        explicit = True
    if text.lower() in ('help', '/help') or text in ('帮助', '会话列表', '当前会话', '断开会话'):
        return {'帮助': 'help', '断开会话': 'disconnect'}.get(text, text.lower().lstrip('/')), '', True
    for prefix, action in [('新建会话', 'new'), ('新建任务', 'new'), ('切换', 'switch')]:
        if text == prefix or text.startswith(prefix + ' '):
            return action, text[len(prefix):].strip(), True
    return 'message', text, explicit


class Chat:
    def __init__(self, bridge):
        self.bridge = bridge
        self.store = bridge.store
        cwd = bridge.config.get('project_cwd', str(ROOT.parent))
        self.cwd = ntpath.normpath(cwd) if windows(bridge.config) else str(Path(cwd).resolve())
        self.project_name = bridge.config.get('project_name') or (ntpath.basename(self.cwd) if windows(bridge.config) else Path(self.cwd).name)
        self.lock = asyncio.Lock()
        self.send_lock = asyncio.Lock()
        self.attachment_lock = asyncio.Lock()
        self.flush_task = None
        self.agent_items = {}
        self.image_dir = Path(self.store.path).parent / 'images'
        with self.store.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS chat_job_images(message_id TEXT PRIMARY KEY, paths TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS chat_job_lists(message_id TEXT PRIMARY KEY, list_key TEXT);
              CREATE TABLE IF NOT EXISTS chat_outbox_images(key TEXT PRIMARY KEY, path TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS chat_sessions(
                thread_id TEXT PRIMARY KEY, name TEXT NOT NULL, cwd TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS chat_selection(
                chat_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS chat_managed(
                thread_id TEXT PRIMARY KEY);
              CREATE TABLE IF NOT EXISTS chat_released(thread_id TEXT PRIMARY KEY);
              CREATE TABLE IF NOT EXISTS chat_jobs(
                message_id TEXT PRIMARY KEY, content TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued', thread_id TEXT,
                created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS chat_outbox(
                key TEXT PRIMARY KEY, content TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued', created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS chat_turns(
                thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                PRIMARY KEY(thread_id,turn_id));
              CREATE TABLE IF NOT EXISTS chat_lists(
                key TEXT PRIMARY KEY, chat_id TEXT NOT NULL, thread_ids TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS chat_list_active(
                chat_id TEXT PRIMARY KEY, list_key TEXT NOT NULL);
            ''')
            # An interrupted network write may already have taken effect. Never replay it.
            db.execute("UPDATE chat_jobs SET state='uncertain' WHERE state='processing'")
            db.execute("UPDATE chat_outbox SET state='uncertain' WHERE state='sending'")

    def current(self):
        with self.store.db() as db:
            row = db.execute('SELECT s.* FROM chat_selection c JOIN chat_sessions s '
                             'ON s.thread_id=c.thread_id WHERE c.chat_id=?',
                             (self.bridge.config['chat_id'],)).fetchone()
            return dict(row) if row else None

    def help(self):
        return HELP.format(project=self.project_name).replace('微信', channel_name(self.bridge.config))

    def session(self, thread_id):
        with self.store.db() as db:
            row = db.execute('SELECT * FROM chat_sessions WHERE thread_id=?', (thread_id,)).fetchone()
            return dict(row) if row else None

    def remember(self, thread_id, name):
        with self.store.db() as db:
            db.execute('INSERT OR REPLACE INTO chat_sessions VALUES(?,?,?)', (thread_id, name, self.cwd))

    def select(self, thread_id):
        with self.store.db() as db:
            db.execute('INSERT OR REPLACE INTO chat_selection VALUES(?,?)',
                       (self.bridge.config['chat_id'], thread_id))
            db.execute('INSERT OR IGNORE INTO chat_managed VALUES(?)', (thread_id,))

    async def accept(self, data):
        if not self.bridge.authorized(data):
            raise PermissionError('消息来源不匹配')
        text, message_id = data.get('text', ''), data.get('message_id', '')
        images = data.get('images', [])
        if not isinstance(images, list) or len(images) > MAX_IMAGES:
            raise ValueError('图片数量超限')
        if not isinstance(text, str) or (not text.strip() and not images) or len(text) > 10000 or not message_id:
            raise ValueError('需要文字或图片和消息编号')
        paths = [await asyncio.to_thread(snapshot, path, self.image_dir) for path in images]
        with self.store.db() as db:
            old = db.execute('SELECT * FROM chat_jobs WHERE message_id=?', (message_id,)).fetchone()
            if old:
                saved = db.execute('SELECT paths FROM chat_job_images WHERE message_id=?', (message_id,)).fetchone()
                if old['content'] != text or (json.loads(saved['paths']) if saved else []) != paths:
                    raise Conflict('同一消息编号对应不同内容')
                return {'handled': True, 'duplicate': True}
        with self.store.db() as db:
            selected = self.current()
            target = selected['thread_id'] if selected and (images or command(text)[0] == 'message') else None
            db.execute('INSERT INTO chat_jobs(message_id,content,thread_id,created) VALUES(?,?,?,?)',
                       (message_id, text, target, time.time()))
            db.execute('INSERT INTO chat_job_images VALUES(?,?)', (message_id, json.dumps(paths)))
            action, argument, _ = command(text)
            if not images and action == 'switch' and list_number(argument) is not None:
                active = db.execute('SELECT list_key FROM chat_list_active WHERE chat_id=?',
                                    (self.bridge.config['chat_id'],)).fetchone()
                # Freeze the delivered list at receipt, before work/flush can
                # deliver another list or reconnect and change its active key.
                db.execute('INSERT INTO chat_job_lists VALUES(?,?)',
                           (message_id, active['list_key'] if active else None))
        self.bridge.spawn(self.work())
        return {'handled': True}

    async def default_session(self):
        current = self.current()
        if current:
            return current
        if not self.bridge.connected:
            raise ConnectionError('Codex 未连接')
        cursor = None
        while True:
            params = {'cwd': self.cwd, 'limit': 100, 'sortKey': 'updated_at', 'sortDirection': 'desc',
                      'sourceKinds': ['cli', 'vscode', 'appServer'], 'useStateDbOnly': True}
            if cursor:
                params['cursor'] = cursor
            result = await self.bridge.codex.rpc('thread/list', params)
            for thread in result.get('data') or []:
                if thread.get('canAcceptDirectInput') is False:
                    continue
                if thread.get('cwd') and not same_cwd(thread['cwd'], self.cwd, self.bridge.config):
                    continue
                self.remember(thread['id'], thread.get('name') or thread.get('preview') or thread['id'])
                self.select(thread['id'])
                return self.current()
            cursor = result.get('nextCursor')
            if not cursor:
                break
        result = await self.start_thread()
        thread_id = result['thread']['id']
        self.remember(thread_id, '默认聊天')
        await self.bridge.codex.rpc('thread/name/set', {'threadId': thread_id, 'name': '默认聊天'})
        self.select(thread_id)
        return self.current()

    async def refresh(self):
        cursor = None
        available = []
        while True:
            params = {'cwd': self.cwd, 'limit': 100, 'sortKey': 'updated_at',
                      'sourceKinds': ['cli', 'vscode', 'appServer'], 'useStateDbOnly': True}
            if cursor:
                params['cursor'] = cursor
            result = await self.bridge.codex.rpc('thread/list', params)
            for thread in result.get('data') or []:
                if thread.get('cwd') and not same_cwd(thread['cwd'], self.cwd, self.bridge.config):
                    continue
                if thread.get('canAcceptDirectInput') is False:
                    continue
                self.remember(thread['id'], thread.get('name') or thread.get('preview') or thread['id'])
                if thread['id'] not in available:
                    available.append(thread['id'])
            cursor = result.get('nextCursor')
            if not cursor:
                break
        return available

    async def start_thread(self):
        await self.release_all()
        params = {'cwd': self.cwd}
        ident = await project_id(self.bridge.codex, self.cwd, self.bridge.config)
        if ident:
            params['projectId'] = ident
        return await self.bridge.codex.rpc('thread/start', params)

    def released(self, thread_id):
        with self.store.db() as db:
            return db.execute('SELECT 1 FROM chat_released WHERE thread_id=?', (thread_id,)).fetchone() is not None

    def active_host(self):
        return getattr(self.bridge, 'is_selected', lambda: True)()

    async def _release(self, thread_id):
        # Persist intent before RPC so reconnect cannot reclaim a handed-off thread.
        with self.store.db() as db:
            db.execute('INSERT OR IGNORE INTO chat_released VALUES(?)', (thread_id,))
        if thread_id in self.bridge.joined:
            await self.bridge.codex.rpc('thread/unsubscribe', {'threadId': thread_id})
            self.bridge.joined.discard(thread_id)

    async def release_all(self):
        async with self.attachment_lock:
            current = self.current()
            threads = self.bridge.joined | ({current['thread_id']} if current else set())
            for thread_id in sorted(threads):
                await self._release(thread_id)

    async def join(self, thread_id, automatic=False):
        async with self.attachment_lock:
            if automatic and (not self.active_host() or self.released(thread_id)):
                return False
            if not automatic:
                for old in sorted(self.bridge.joined - {thread_id}):
                    await self._release(old)
            result = await self.bridge.codex.rpc('thread/resume', {'threadId': thread_id, 'excludeTurns': True})
            if result.get('thread'):
                await attach_project(self.bridge.codex, result['thread'], self.cwd, self.bridge.config)
            self.bridge.joined.add(thread_id)
            with self.store.db() as db:
                db.execute('DELETE FROM chat_released WHERE thread_id=?', (thread_id,))
            # Prime the watcher before starting a turn, so a fast question isn't skipped.
            if thread_id not in getattr(self.bridge.desktop_questions, 'positions', {}):
                self.bridge.desktop_questions.poll([thread_id])
            return True

    async def execute(self, job):
        with self.store.db() as db:
            saved = db.execute('SELECT paths FROM chat_job_images WHERE message_id=?', (job['message_id'],)).fetchone()
        images = json.loads(saved['paths']) if saved else []
        action, argument, _ = ('message', job['content'], False) if images else command(job['content'])
        key = 'ack:' + job['message_id']
        if action == 'help':
            self.enqueue(key, self.help())
            return
        if action == 'disconnect':
            await self.release_all()
            self.enqueue(key, '已断开桥接连接，任务继续运行。可在桌面点击「重试」；若仍锁定，可能是其他客户端占用或后台仍保留会话。再次发消息或切换会话会重新连接。')
            return
        if action == '当前会话':
            current = await self.default_session()
            self.enqueue(key, '💬 当前会话\n\n「' + display_name(current['name']) + '」\n项目：{project}\n\n直接发消息，继续这个任务。'.replace('{project}', self.project_name))
            return
        if not self.bridge.connected:
            raise ConnectionError('Codex 未连接')
        if action in ('new', 'switch') and (not argument or len(argument) > 120 or '\n' in argument):
            self.enqueue(key, '💡 请这样发送\n\n新建会话 微信接入\n切换 2\n切换 微信接入\n\n发送「会话列表」查看序号。名称最多 120 字。')
            return
        if action in ('new', 'switch', '会话列表'):
            available = await self.refresh()
            with self.store.db() as db:
                known = {r['thread_id']: dict(r) for r in db.execute('SELECT * FROM chat_sessions WHERE cwd=?', (self.cwd,))}
                sessions = [known[ident] for ident in available]
            matches = [s for s in sessions if s['name'] == argument]
            if action == '会话列表':
                current = self.current()
                shown = sessions[:50]
                lines = [str(n) + '. ' + display_name(s['name']) +
                         ('  ← 当前' if current and s['thread_id'] == current['thread_id'] else '')
                         for n, s in enumerate(shown, 1)]
                with self.store.db() as db:
                    db.execute('INSERT OR REPLACE INTO chat_lists VALUES(?,?,?)',
                               (key, self.bridge.config['chat_id'], json.dumps([s['thread_id'] for s in shown])))
                text = '📋 会话列表 · {project}\n\n' + '\n\n'.join(lines) if lines else '📋 会话列表 · {project}\n\n暂无会话。\n发送「新建会话 名称」即可开始。'
                if len(sessions) > 50:
                    text += '\n\n已显示最近 50 个会话。'
                if lines:
                    text += '\n\n发送「切换 2」切换会话。\n序号对应这份列表，10分钟内有效；新增会话后请刷新列表。\n也支持完整名称。'
                self.enqueue(key, text.replace("{project}", self.project_name))
                return
            if action == 'new':
                if matches:
                    self.enqueue(key, '💡 已有同名会话\n\n「' + display_name(argument) + '」\n\n发送「会话列表」后按序号切换，\n或换个名称新建会话。')
                    return
                # Inherit the app-server's configured model and permission policy.
                result = await self.start_thread()
                thread_id = result['thread']['id']
                self.remember(thread_id, argument)
                await self.bridge.codex.rpc('thread/name/set', {'threadId': thread_id, 'name': argument})
                await self.join(thread_id)
                self.select(thread_id)
                self.enqueue(key, '✅ 已新建并切到\n\n「' + display_name(argument) + '」\n项目：{project}\n\n直接发消息，开始这个任务。'.replace('{project}', self.project_name))
                return
            number = list_number(argument)
            if number is not None:
                with self.store.db() as db:
                    snapshot = db.execute('SELECT l.thread_ids,o.created FROM chat_job_lists j JOIN chat_lists l '
                                          'ON l.key=j.list_key JOIN chat_outbox o ON o.key=l.key '
                                          'WHERE j.message_id=? AND l.chat_id=? AND o.state=\'sent\'',
                                          (job['message_id'],self.bridge.config['chat_id'])).fetchone()
                if not snapshot:
                    self.enqueue(key, '💡 请先发送「会话列表」\n\n收到列表后，再发送「切换 2」。')
                    return
                ids = json.loads(snapshot['thread_ids'])
                if job['created'] - snapshot['created'] > 600 or set(available[:50]) - set(ids):
                    self.enqueue(key, '💡 旧列表已失效，这次没有切换\n\n发送「会话列表」获取新序号，或直接发送「切换 完整会话名称」。')
                    return
                index = number - 1
                if not 0 <= index < len(ids):
                    self.enqueue(key, '💡 序号不在列表中\n\n发送「会话列表」查看可用序号。')
                    return
                target = ids[index]
                if target not in available:
                    self.enqueue(key, '💡 这个会话已不可用\n\n发送「会话列表」获取最新列表。')
                    return
                matches = [known[target]]
            if len(matches) != 1:
                self.enqueue(key, '💡 没有找到唯一的同名会话\n\n发送「会话列表」，再按序号切换。')
                return
            await self.join(matches[0]['thread_id'])
            self.select(matches[0]['thread_id'])
            self.enqueue(key, '✅ 已切到「' + display_name(matches[0]['name']) + '」会话\n\n项目：{project}\n之后的消息接着这里聊。'.replace('{project}', self.project_name))
            return
        current = self.session(job['thread_id']) if job.get('thread_id') else await self.default_session()
        thread_id = current['thread_id']
        with self.store.db() as db:
            db.execute('UPDATE chat_jobs SET thread_id=? WHERE message_id=?', (thread_id, job['message_id']))
        await self.join(thread_id)
        turns = await self.bridge.codex.rpc('thread/turns/list', {'threadId': thread_id, 'limit': 1, 'itemsView': 'summary'})
        latest = (turns.get('data') or [None])[0]
        inputs = ([{'type': 'text', 'text': argument}] if argument.strip() else [])
        if getattr(self.bridge, 'image_transport', None):
            images = [await self.bridge.image_transport.upload(path) for path in images]
        inputs += [{'type': 'localImage', 'path': path} for path in images]
        params = {'threadId': thread_id, 'input': inputs,
                  'clientUserMessageId': message_prefix(self.bridge.config)+'-chat:' + job['message_id']}
        if latest and latest['status'] == 'inProgress':
            await self.bridge.codex.rpc('turn/steer', {**params, 'expectedTurnId': latest['id']})
            turn_id = latest['id']
        else:
            result = await self.bridge.codex.rpc('turn/start', params)
            turn_id = result['turn']['id']
        with self.store.db() as db:
            db.execute('INSERT OR IGNORE INTO chat_turns VALUES(?,?)', (thread_id, turn_id))
        waiting = any(row['kind'] == 'command_approval' and row['state'] == 'pending'
                      and row['thread_id'] == thread_id and row['turn_id'] == turn_id
                      for row in self.store.rows())
        status = ('已收到，当前会话正在等待命令授权；请先回复授权卡片中的编号和选项。'
                  if waiting else '已收到，正在处理。')
        self.enqueue(key, '💬 「' + display_name(current['name']) + '」\n\n' + status)

    async def work(self):
        async with self.lock:
            while True:
                with self.store.db() as db:
                    row = db.execute("SELECT * FROM chat_jobs WHERE state='queued' ORDER BY created,rowid LIMIT 1").fetchone()
                    if not row:
                        break
                    job = dict(row)
                    attachment = db.execute('SELECT paths FROM chat_job_images WHERE message_id=?', (job['message_id'],)).fetchone()
                    image_message = attachment and json.loads(attachment['paths'])
                    if not self.bridge.connected and (image_message or command(job['content'])[0] != 'help'):
                        self.enqueue('waiting:' + job['message_id'], '消息已收到，正在等待 Codex 连接恢复。')
                        break
                    db.execute("UPDATE chat_jobs SET state='processing' WHERE message_id=?", (job['message_id'],))
                try:
                    await self.execute(job)
                    state = 'done'
                except Exception as exc:
                    state = 'uncertain'
                    self.enqueue('ack:' + job['message_id'],
                                 '⚠️ 请求未确认\n\n消息已保留，未自动重试。\n请到 Codex 桌面核对后再继续。')
                    # Do not log message content or exception details containing user data.
                    from .service import log
                    log.warning('chat_request_error=%s', type(exc).__name__)
                with self.store.db() as db:
                    db.execute('UPDATE chat_jobs SET state=? WHERE message_id=?', (state, job['message_id']))
        self.schedule_flush()

    def schedule_flush(self):
        # Coalesce requests so a slow/blocked notification cannot accumulate
        # workers or hold the task-input and completion paths open.
        if self.flush_task is None or self.flush_task.done():
            self.flush_task = self.bridge.spawn(self.flush())
        return self.flush_task

    def enqueue(self, key, text):
        # Keep individual notifications bounded; the full result remains on desktop.
        if len(text) > 3500:
            text = text[:3400] + '\n\n内容较长，完整结果请到 Codex 桌面查看。'
        with self.store.db() as db:
            db.execute('INSERT OR IGNORE INTO chat_outbox(key,content,created) VALUES(?,?,?)', (key, text, time.time()))
        self.store.prune_outbound()

    async def queue_image(self, key, path):
        if getattr(self.bridge, 'image_transport', None):
            path = await self.bridge.image_transport.download(path, self.image_dir)
        saved = await asyncio.to_thread(snapshot, path, self.image_dir)
        with self.store.db() as db:
            old = db.execute('SELECT path FROM chat_outbox_images WHERE key=?', (key,)).fetchone()
            if old and old['path'] != saved:
                raise Conflict('同一发送编号对应不同图片')
            db.execute('INSERT OR IGNORE INTO chat_outbox_images VALUES(?,?)', (key, saved))
            db.execute('INSERT OR IGNORE INTO chat_outbox(key,content,created) VALUES(?,?,?)', (key, '', time.time()))
        self.store.prune_outbound()
        return key

    async def flush(self):
        self.store.prune_outbound()
        async with self.send_lock:
            if self.bridge.channel_blocked:
                return
            while True:
                with self.store.db() as db:
                    row = db.execute("SELECT * FROM chat_outbox WHERE state='queued' ORDER BY created,rowid LIMIT 1").fetchone()
                    if not row:
                        return
                    db.execute("UPDATE chat_outbox SET state='sending' WHERE key=?", (row['key'],))
                try:
                    with self.store.db() as db:
                        image = db.execute('SELECT path FROM chat_outbox_images WHERE key=?', (row['key'],)).fetchone()
                    payload = {'image': image['path']} if image else row['content']
                    await self.bridge.notify(payload)
                    state = 'sent'
                except ChannelNeedsMessage:
                    self.bridge.channel_blocked = True
                    state = 'queued'
                except NotificationDeferred:
                    with self.store.db() as db:
                        db.execute("UPDATE chat_outbox SET state='queued' WHERE key=?", (row['key'],))
                    self.store.prune_outbound()
                    return
                except Exception:
                    state = 'uncertain'
                with self.store.db() as db:
                    db.execute('UPDATE chat_outbox SET state=? WHERE key=?', (state, row['key']))
                    if state == 'sent':
                        snapshot = db.execute('SELECT chat_id FROM chat_lists WHERE key=?', (row['key'],)).fetchone()
                        if snapshot:
                            db.execute('INSERT OR REPLACE INTO chat_list_active VALUES(?,?)', (snapshot['chat_id'], row['key']))
                self.store.prune_outbound()
                if self.bridge.channel_blocked:
                    return

    async def event(self, method, params):
        thread_id = params.get('threadId')
        with self.store.db() as db:
            if not db.execute('SELECT 1 FROM chat_managed WHERE thread_id=?', (thread_id,)).fetchone():
                return
        session = self.session(thread_id)
        if not session:
            return
        if method == 'item/completed':
            item = params.get('item') or {}
            if item.get('type') in ('agentMessage', 'imageGeneration'):
                self.agent_items.setdefault((thread_id, params.get('turnId')), {})[item['id']] = item
                if item.get('type') == 'agentMessage' and item.get('phase') == 'commentary' and item.get('text'):
                    self.enqueue('progress:'+thread_id+':'+str(params.get('turnId'))+':'+item['id'],
                                 '「'+session['name']+'」\n\n'+item['text'])
                    self.schedule_flush()
        elif method == 'turn/completed':
            await self.completed(session, params.get('turn') or {})

    async def completed(self, session, turn):
        thread_id, turn_id = session['thread_id'], turn.get('id')
        if not turn_id:
            return
        with self.store.db() as db:
            if db.execute('SELECT 1 FROM chat_outbox WHERE key=?', ('result:' + thread_id + ':' + turn_id,)).fetchone():
                db.execute('DELETE FROM chat_turns WHERE thread_id=? AND turn_id=?', (thread_id, turn_id))
                return
        items = turn.get('items') or list(self.agent_items.pop((thread_id, turn_id), {}).values())
        if not items:
            result = await self.bridge.codex.rpc('thread/turns/list', {'threadId': thread_id, 'limit': 10, 'itemsView': 'full'})
            items = next((t.get('items') or [] for t in result.get('data') or [] if t['id'] == turn_id), [])
        messages = [i for i in items if i.get('type') == 'agentMessage' and i.get('text')]
        final = [i['text'] for i in messages if i.get('phase') == 'final_answer']
        unphased = [i['text'] for i in messages if not i.get('phase')]
        text = '\n\n'.join(final) if final else (unphased[-1] if unphased else '')
        if turn.get('status') == 'failed':
            text = '执行失败，请到 Codex 查看详情。\n' + text
        elif turn.get('status') == 'interrupted':
            text = '本轮执行已中断。\n' + text
        elif not text:
            text = '本轮处理完成，详情请到 Codex 查看。'
        result_paths = local_result_images(text)
        for item in items:
            if item.get('type') == 'imageGeneration' and item.get('savedPath') and item['savedPath'] not in result_paths:
                result_paths.append(item['savedPath'])
        for n, path in enumerate(result_paths[:MAX_IMAGES]):
            try:
                await self.queue_image('result-image:' + thread_id + ':' + turn_id + ':' + str(n), path)
            except ValueError:
                text += '\n图片未能发送，请到 Codex 桌面查看。'
        self.enqueue('result:' + thread_id + ':' + turn_id, '「' + session['name'] + '」\n\n' + text)
        with self.store.db() as db:
            db.execute('DELETE FROM chat_turns WHERE thread_id=? AND turn_id=?', (thread_id, turn_id))
        self.schedule_flush()

    async def poll(self):
        # Restore subscriptions/results after a bridge reconnect, without replaying input.
        with self.store.db() as db:
            sessions = [dict(r) for r in db.execute('SELECT * FROM chat_sessions')]
            pending = [dict(r) for r in db.execute('SELECT * FROM chat_turns')]
        current = self.current()
        needed = set()
        if current and self.active_host() and not self.released(current['thread_id']):
            needed.add(current['thread_id'])
        for thread_id in needed - self.bridge.joined:
            try:
                await self.join(thread_id, automatic=True)
            except RpcError as exc:
                from .service import log
                log.warning('chat_subscription=skip method=%s code=%s', exc.method, exc.code)
        for entry in pending:
            try:
                result = await self.bridge.codex.rpc('thread/turns/list', {'threadId': entry['thread_id'], 'limit': 10, 'itemsView': 'summary'})
            except RpcError as exc:
                from .service import log
                log.warning('chat_result_poll=skip method=%s code=%s', exc.method, exc.code)
                continue
            for turn in result.get('data') or []:
                if turn['id'] == entry['turn_id'] and turn['status'] != 'inProgress':
                    session = next(s for s in sessions if s['thread_id'] == entry['thread_id'])
                    await self.completed(session, turn)
        await self.work()
