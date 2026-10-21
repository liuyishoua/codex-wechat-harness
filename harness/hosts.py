"""One WeChat intake, independent durable bridges for each registered machine."""
import asyncio
import hashlib
import json
import re
from pathlib import Path

from .chat import command
from .service import Bridge
from .store import Store, Conflict, parse_reply
from .transport import channel_sender, ChannelNeedsMessage, NotificationDeferred
from .remote import absolute


def host_key(value):
    return re.sub(r'[\s_-]+', '', value.casefold())


class Hosts:
    def __init__(self, config, store, sender=None, clients=None):
        self.config, self.store = config, store
        sender = sender or channel_sender(config)
        self.bridges, self.names, self.aliases = {}, {}, {}
        self.delivery_lock = asyncio.Lock()
        self.local_id = config.get('local_host_id', 'ubuntu')
        entries = [{'id': self.local_id, 'name': config.get('local_host_name', 'Ubuntu'),
                    'aliases': ['本机', '服务器', 'leo-ubuntu']}, *config.get('hosts', [])]
        for entry in entries:
            ident = entry['id']
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', ident) or ident in self.bridges:
                raise ValueError('主机编号无效或重复')
            name = entry.get('name') or ident
            self.names[ident] = name
            for alias in [ident, name, *entry.get('aliases', [])]:
                key = host_key(alias)
                if key in self.aliases and self.aliases[key] != ident:
                    raise ValueError('主机别名重复')
                self.aliases[key] = ident
            local = ident == self.local_id
            cfg = dict(config)
            if not local:
                if not isinstance(entry.get('codex_socket'), str) or not entry['codex_socket'].startswith('/'):
                    raise ValueError('远端主机缺少绝对路径：codex_socket')
                if not absolute(entry.get('project_cwd'), entry):
                    raise ValueError('远端主机缺少绝对路径：project_cwd')
                cfg.update(entry)
                cfg['codex_state_db'] = None  # Remote rollouts are not local files.
                cfg['thread_ids'] = []
                cfg['managed_threads_only'] = True
            path = Path(store.path).parent / 'hosts' / ident / 'state.sqlite3'
            host_store = store if local else Store(path)
            if not local:
                host_store.recover()

            async def labeled_send(chat_id, payload, host=ident):
                # Serialize selection with actual delivery, including images.
                async with self.delivery_lock:
                    if self.chat and self.chat.selected() != host:
                        raise NotificationDeferred()
                    if isinstance(payload, str):
                        payload = '【' + self.names[host] + '】\n' + payload
                    try:
                        return await sender(chat_id, payload)
                    except ChannelNeedsMessage:
                        for bridge in self.bridges.values():
                            bridge.channel_blocked = True
                        raise

            bridge = Bridge(cfg, host_store, labeled_send, (clients or {}).get(ident))
            if entry.get('ssh_target'):
                from .remote_images import RemoteImages
                bridge.image_transport = RemoteImages(entry)
            self.bridges[ident] = bridge
        with self.store.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS host_selection(chat_id TEXT PRIMARY KEY, host_id TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS host_inbound(message_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL, host_id TEXT NOT NULL, state TEXT NOT NULL);
            ''')
            # Preserve deduplication for messages received before multihost upgrade.
            if self.local.chat:
                for row in db.execute('SELECT j.message_id,j.content,i.paths FROM chat_jobs j '
                                      'LEFT JOIN chat_job_images i ON i.message_id=j.message_id').fetchall():
                    paths = json.loads(row['paths']) if row['paths'] else []
                    fingerprint = hashlib.sha256(json.dumps([row['content'], paths], ensure_ascii=False).encode()).hexdigest()
                    db.execute('INSERT OR IGNORE INTO host_inbound VALUES(?,?,?,?)',
                               (row['message_id'], fingerprint, self.local_id, 'done'))
        stores = [bridge.store for bridge in self.bridges.values()]
        for host_store in stores:
            host_store.outbound_stores = stores
        self.store.prune_outbound()
        self.chat = HostChat(self) if config.get('chat_enabled') else None
        for ident, bridge in self.bridges.items():
            bridge.is_selected = lambda host=ident: not self.chat or self.chat.selected() == host

    @property
    def local(self):
        return self.bridges[self.local_id]

    @property
    def connected(self):
        return self.local.connected

    @property
    def joined(self):
        return {(host, thread) for host, b in self.bridges.items() for thread in b.joined}

    @property
    def channel_blocked(self):
        return any(b.channel_blocked for b in self.bridges.values())

    @property
    def tasks(self):
        return set().union(*(b.tasks for b in self.bridges.values()))

    def authorized(self, data):
        return self.local.authorized(data)

    async def add(self, *args, **kwargs):
        # The existing local CLI always refers to the local Codex, not selection.
        return await self.local.add(*args, **kwargs)

    def question_owner(self, ident):
        matches = [b for b in self.bridges.values() if b.store.get(ident)]
        if len(matches) != 1:
            raise Conflict('问题不存在或编号不唯一，请在原 Codex 核对')
        return matches[0]

    def question_row(self, ident):
        return self.question_owner(ident).store.get(ident)

    async def reply(self, data):
        if not self.authorized(data):
            raise PermissionError('消息来源不匹配')
        parsed = parse_reply(data.get('text', ''))
        if not parsed:
            raise ValueError('回复需包含任务编号')
        owner = self.question_owner(parsed[0])
        if self.chat and owner is not self.bridges[self.chat.selected()]:
            raise Conflict('此问题属于其他主机，请先切换到原主机再回复')
        result = await owner.reply(data)
        host = next(h for h, b in self.bridges.items() if b is owner)
        result['message'] = '【' + self.names[host] + '】' + result['message']
        return result

    async def channel_ready(self, data):
        if not self.authorized(data):
            raise PermissionError('消息来源不匹配')
        for b in self.bridges.values():
            await b.channel_ready(data)
        return {'ok': True}

    def host_status(self):
        selected = self.chat.selected() if self.chat else self.local_id
        return [{'id': h, 'name': self.names[h], 'connected': b.connected,
                 'selected': h == selected, 'project_cwd': b.chat.cwd if b.chat else None,
                 'current_chat': b.chat.current() if b.chat else None}
                for h, b in self.bridges.items()]

    async def monitor(self):
        await asyncio.gather(*(b.monitor() for b in self.bridges.values()))


class HostChat:
    def __init__(self, hub):
        self.hub = hub
        self.lock = asyncio.Lock()

    def selected(self):
        with self.hub.store.db() as db:
            row = db.execute('SELECT host_id FROM host_selection WHERE chat_id=?',
                             (self.hub.config['chat_id'],)).fetchone()
        ident = row['host_id'] if row else self.hub.local_id
        # Never silently reroute a removed host's pending input.
        if ident not in self.hub.bridges:
            raise Conflict('当前主机已移除，请先切换主机')
        return ident

    @property
    def cwd(self):
        return self.hub.bridges[self.selected()].chat.cwd

    def current(self):
        current = self.hub.bridges[self.selected()].chat.current()
        return {**current, 'host_id': self.selected()} if current else None

    def schedule_flush(self):
        for b in self.hub.bridges.values():
            b.chat.schedule_flush()

    async def queue_image(self, key, path):
        # Manual /images requests originate in local Codex, regardless of selection.
        return await self.hub.local.chat.queue_image(key, path)

    def action(self, text, images):
        if images:
            return None, None
        action, arg, _ = command(text)
        cleaned = text.strip()
        if cleaned.lower().startswith(('/codex ', 'codex ')):
            cleaned = cleaned.split(None, 1)[1].strip()
        if cleaned in ('主机列表', '当前主机'):
            return cleaned, None
        if cleaned == '切换主机' or cleaned.startswith('切换主机 '):
            return 'host-switch', self.hub.aliases.get(host_key(cleaned[4:].strip()))
        if cleaned.startswith('切换'):
            target = self.hub.aliases.get(host_key(cleaned[2:]))
            if target:
                return 'host-switch', target
        if action == 'help':
            return 'help', None
        return None, None

    async def accept(self, data):
        if not self.hub.authorized(data):
            raise PermissionError('消息来源不匹配')
        text, ident, images = data.get('text', ''), data.get('message_id'), data.get('images', [])
        if not isinstance(text, str) or len(text) > 10000 or not isinstance(ident, str) or not ident:
            raise ValueError('消息格式不正确')
        if not isinstance(images, list) or len(images) > 9 or not all(isinstance(p, str) for p in images):
            raise ValueError('图片格式不正确')
        if not text.strip() and not images:
            raise ValueError('需要文字或图片')
        # Fingerprint content, not source filenames that Hermes may recache.
        from .images import snapshot
        async with self.lock:
            # Serialize before copying attachments: a later switch must not move
            # an image message that arrived on the previous machine.
            paths = [await asyncio.to_thread(snapshot, p, Path(self.hub.store.path).parent / 'images') for p in images]
            fingerprint = hashlib.sha256(json.dumps([text, paths], ensure_ascii=False).encode()).hexdigest()
            with self.hub.store.db() as db:
                old = db.execute('SELECT * FROM host_inbound WHERE message_id=?', (ident,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise Conflict('同一消息编号对应不同内容')
                if old['state'] == 'done':
                    return {'handled': True, 'duplicate': True}
                # Do not replay a host switch after a process interruption.
                if old['state'] == 'command':
                    return {'handled': True, 'duplicate': True, 'state': 'uncertain'}
                host = old['host_id']
            action, target = self.action(text, images)
            if not old:
                try:
                    host = self.selected()
                except Conflict:
                    if action not in ('host-switch', '主机列表', '当前主机', 'help'):
                        raise
                    host = self.hub.local_id
            if not old:
                with self.hub.store.db() as db:
                    db.execute('INSERT INTO host_inbound VALUES(?,?,?,?)',
                               (ident, fingerprint, host, 'command' if action else 'routing'))
            if action:
                b = self.hub.bridges[host]
                if action == 'host-switch':
                    if target is None:
                        response = '未找到主机。发送「主机列表」查看，再发送「切换主机 名称」。'
                    elif not self.hub.bridges[target].connected:
                        response = self.hub.names[target] + ' 的 Codex 尚未连接，这次没有切换。当前主机：' + self.hub.names[host]
                    else:
                        # Drain already accepted input before releasing its connection.
                        async with b.chat.lock:
                            await b.chat.release_all()
                        destination = self.hub.bridges[target]
                        async with destination.chat.lock:
                            current = destination.chat.current()
                            if current:
                                await destination.chat.join(current['thread_id'])
                        async with self.hub.delivery_lock:
                            with self.hub.store.db() as db:
                                db.execute('INSERT OR REPLACE INTO host_selection VALUES(?,?)',
                                           (self.hub.config['chat_id'], target))
                        b = self.hub.bridges[target]
                        for row in b.store.rows():
                            if row['state'] == 'pending' and row['outbound'] == 'queued':
                                b.spawn(b.send(row))
                        current = b.chat.current()
                        response = '已切换到 ' + self.hub.names[target] + '\n新建会话默认目录：' + b.chat.cwd
                        response += '\n当前会话：' + (current['name'] if current else '尚未选择；发送「会话列表」或「新建会话 名称」。')
                        if current:
                            response += '\n会话目录：' + current['cwd']
                        response += '\n只接收此主机的会话消息；原主机任务继续运行，消息切回后发送。'
                elif action == '主机列表':
                    response = '\n'.join(s['name'] + ' · ' + ('Codex 已连接' if s['connected'] else 'Codex 未连接') +
                                         (' ← 当前' if s['selected'] else '') + ' · 切换 ' + host_key(s['name'])
                                         for s in self.hub.host_status())
                    response += '\n\n发送对应的切换命令即可。'
                elif action == '当前主机':
                    response = '当前主机：' + self.hub.names[host] + '\n新建会话默认目录：' + b.chat.cwd
                    current = b.chat.current()
                    response += '\n当前会话：' + (current['name'] if current else '尚未选择')
                    if current:
                        response += '\n会话目录：' + current['cwd']
                else:
                    examples = '\n'.join('切换 ' + host_key(name) for name in self.hub.names.values())
                    response = b.chat.help() + '\n\n🖥 主机管理\n主机列表\n当前主机\n' + examples + '\n切换主机 名称\n会话列表和新建会话作用于当前主机。'
                b.chat.enqueue('host-ack:' + ident, response)
                b.chat.schedule_flush()
            else:
                if host not in self.hub.bridges:
                    raise Conflict('原目标主机已移除，消息未转发')
                await self.hub.bridges[host].chat.accept({**data, 'images': paths})
            with self.hub.store.db() as db:
                db.execute("UPDATE host_inbound SET state='done' WHERE message_id=?", (ident,))
        return {'handled': True}
