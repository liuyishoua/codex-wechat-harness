"""Durable routing and delivery intents. No credentials live in this database."""
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS questions(
                id TEXT PRIMARY KEY, source_key TEXT UNIQUE NOT NULL,
                thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                questions TEXT NOT NULL, kind TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', outbound TEXT NOT NULL DEFAULT 'queued',
                outbound_id TEXT NOT NULL DEFAULT '', runtime_id TEXT,
                created REAL NOT NULL, expires REAL NOT NULL, answer TEXT NOT NULL DEFAULT '',
                delivery_error TEXT NOT NULL DEFAULT '');
              CREATE TABLE IF NOT EXISTS inbound(
                message_id TEXT PRIMARY KEY, question_id TEXT NOT NULL,
                content TEXT NOT NULL, created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY, question_id TEXT, kind TEXT NOT NULL, created REAL NOT NULL);
            ''')
        os.chmod(path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, key, thread_id, turn_id, questions, kind='question', runtime_id=None):
        if not questions or len(questions) > 3:
            raise ValueError('需要 1–3 个问题')
        if len({q.get('id') for q in questions}) != len(questions):
            raise ValueError('问题编号不能重复')
        for q in questions:
            if not isinstance(q.get('id'),str) or not q['id']:
                raise ValueError('问题缺少编号')
            if not isinstance(q.get('options',[]),list) or any(not isinstance(v,str) for v in q.get('options',[])):
                raise ValueError('选项必须是文本列表')
            if not isinstance(q.get('question'), str) or not q['question'].strip() or len(q['question']) > 2000:
                raise ValueError('问题为空或过长')
        with self.db() as db:
            old = db.execute('SELECT * FROM questions WHERE source_key=?', (key,)).fetchone()
            if old:
                if old['thread_id'] != thread_id or old['turn_id'] != turn_id or json.loads(old['questions']) != questions:
                    raise Conflict('相同问题编号不能改写内容或目标任务')
                return dict(old), False
            ident = 'C' + secrets.token_hex(3).upper()
            db.execute('INSERT INTO questions(id,source_key,thread_id,turn_id,questions,kind,runtime_id,created,expires) VALUES(?,?,?,?,?,?,?,?,?)',
                (ident, key, thread_id, turn_id, json.dumps(questions, ensure_ascii=False), kind,
                 json.dumps(runtime_id) if runtime_id is not None else None, time.time(), time.time()+86400))
            db.execute('INSERT INTO events(question_id,kind,created) VALUES(?,?,?)', (ident, 'created', time.time()))
            return dict(db.execute('SELECT * FROM questions WHERE id=?', (ident,)).fetchone()), True

    def get(self, ident):
        with self.db() as db:
            row = db.execute('SELECT * FROM questions WHERE id=?', (ident,)).fetchone()
            return dict(row) if row else None

    def rows(self):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM questions ORDER BY created DESC LIMIT 100')]

    def update(self, ident, **fields):
        assert set(fields) <= {'state', 'outbound', 'outbound_id', 'delivery_error', 'runtime_id'}
        with self.db() as db:
            db.execute('UPDATE questions SET '+','.join(k+'=?' for k in fields)+' WHERE id=?', (*fields.values(), ident))
            db.execute('INSERT INTO events(question_id,kind,created) VALUES(?,?,?)',
                       (ident, 'state:'+fields.get('state', fields.get('outbound','update')), time.time()))

    def claim_send(self, ident):
        with self.db() as db:
            return db.execute("UPDATE questions SET outbound='sending' WHERE id=? AND outbound='queued' AND state='pending'", (ident,)).rowcount == 1

    def accept(self, ident, message_id, content):
        if not message_id or not content.strip() or len(content) > 10000:
            raise ValueError('回复缺少消息编号、为空或过长')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM inbound WHERE message_id=?', (message_id,)).fetchone()
            if old:
                if old['question_id'] != ident or old['content'] != content:
                    raise Conflict('同一个消息编号对应的内容不一致')
                return dict(db.execute('SELECT * FROM questions WHERE id=?', (ident,)).fetchone()), False
            q = db.execute('SELECT * FROM questions WHERE id=?', (ident,)).fetchone()
            if not q or q['expires'] < time.time() or q['state'] != 'pending':
                raise Conflict('该问题已处理、已过期或不存在，请查看 Codex 当前状态')
            db.execute('INSERT INTO inbound VALUES(?,?,?,?)', (message_id, ident, content, time.time()))
            db.execute("UPDATE questions SET state='received', answer=? WHERE id=?", (content, ident))
            return dict(db.execute('SELECT * FROM questions WHERE id=?', (ident,)).fetchone()), True

    def resolve_desktop(self, thread_id, call_id):
        with self.db() as db:
            db.execute("UPDATE questions SET state='resolved_elsewhere' WHERE source_key=? AND state='pending'",
                       ('desktop:'+thread_id+':'+call_id,))

    def claim_delivery(self, ident):
        with self.db() as db:
            return db.execute("UPDATE questions SET state='delivering' WHERE id=? AND state='received'", (ident,)).rowcount == 1

    def waiting(self):
        with self.db() as db:
            return [(dict(r), r['message_id']) for r in db.execute(
                "SELECT q.*,i.message_id FROM questions q JOIN inbound i ON i.question_id=q.id WHERE q.state='received'")]

    def recover(self):
        # A crash after the network write must never duplicate a notification or user turn.
        with self.db() as db:
            db.execute("UPDATE questions SET outbound='uncertain' WHERE outbound='sending'")
            db.execute("UPDATE questions SET state='uncertain',delivery_error='进程在递交回复时中断，需要核对' WHERE state='delivering'")


def parse_reply(text):
    match = re.fullmatch(r'\s*(?:/?codex\s+)?(C[0-9a-f]{6})\s+([\s\S]+?)\s*', text, re.I)
    return (match[1].upper(), match[2]) if match else None


def answers_for(row, text):
    questions = json.loads(row['questions'])
    if len(questions) == 1:
        values = {questions[0]['id']: text}
    else:
        try:
            values = json.loads(text)
        except ValueError:
            raise ValueError('多个问题请用 JSON 分别回答，键为各问题编号') from None
        if not isinstance(values, dict) or set(values) != {q['id'] for q in questions}:
            raise ValueError('请分别回答这条消息中的全部问题')
    result = {}
    for q in questions:
        value = values[q['id']]
        if not isinstance(value, str) or not value.strip():
            raise ValueError('回答不能为空')
        choices = q.get('options') or []
        if value.strip().isdigit() and 1 <= int(value.strip()) <= len(choices):
            value = choices[int(value.strip())-1]
        result[q['id']] = {'answers': [value]}
    return result


def card(row):
    parts = [f"Codex 需要你 · {row['id']}"]
    questions = json.loads(row['questions'])
    for q in questions:
        parts.append(q['question'])
        parts.extend(f'{n}. {v}' for n,v in enumerate(q.get('options') or [],1))
    if len(questions) == 1:
        parts.append(f"回复：{row['id']} 你的答案（也可回复选项序号）")
    else:
        template = json.dumps({q['id']: '答案' for q in questions},ensure_ascii=False)
        parts.append(f"回复：{row['id']} {template}")
    parts.append('答案会交回提问的原 Codex 会话，不受当前会话切换影响。')
    return '\n\n'.join(parts)
