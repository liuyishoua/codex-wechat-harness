import asyncio
from types import SimpleNamespace

from harness.service import Bridge
from harness.store import Store


def bridge(tmp_path, platform='weixin'):
    sent = []
    async def send(chat, payload):
        sent.append(payload)
        return 'receipt'
    config = {'chat_id':'owner', 'allowed_users':['owner'], 'codex_socket':'unused',
              'chat_enabled':True, 'project_cwd':'/tmp/test', 'codex_state_db':None,
              'platform':platform, 'outbound_queue_limit':10}
    result = Bridge(config, Store(tmp_path/'state.sqlite3'), send, SimpleNamespace())
    return result, sent


def pending(store):
    with store.db() as db:
        return [r[0] for r in db.execute("SELECT key FROM chat_outbox WHERE state='queued' ORDER BY created,rowid")]


def test_retains_only_latest_ten_and_discarded_keys_cannot_replay(tmp_path):
    b, sent = bridge(tmp_path)
    for n in range(25):
        b.chat.enqueue(str(n), 'message-'+str(n))
    assert pending(b.store) == [str(n) for n in range(15,25)]
    b.chat.enqueue('0', 'message-0')
    asyncio.run(b.chat.flush())
    assert sent == ['message-'+str(n) for n in range(15,25)]
    assert pending(b.store) == []


def test_all_hosts_share_ten_including_questions_without_answering_approvals(tmp_path):
    first,_ = bridge(tmp_path/'first')
    second,_ = bridge(tmp_path/'second')
    stores = [first.store, second.store]
    for store in stores:
        store.outbound_stores = stores
    for b in [first,second]:
        for n in range(7):b.chat.enqueue(str(n), 'text')
    question = [{'id':'q','question':'Proceed?','options':['yes','no']}]
    q,_ = first.store.add('approval','thread','turn',question,'command_approval',123)
    first.store.update(q['id'],outbound='blocked_session')
    other,_ = second.store.add('question','thread','turn',question)
    assert sum(len(pending(s)) for s in stores) == 8
    assert first.store.get(q['id'])['state'] == 'pending'
    assert second.store.get(other['id'])['outbound'] == 'queued'
    for n in range(10):second.chat.enqueue('new-'+str(n),'new')
    assert first.store.get(q['id'])['outbound']=='discarded'
    assert first.store.get(q['id'])['state']=='pending'
    assert first.store.get(q['id'])['answer']==''
    assert sum(len(pending(s)) for s in stores)==10


def test_qq_queue_uses_its_own_limit(tmp_path):
    b,_=bridge(tmp_path,'qqbot')
    for n in range(15):b.chat.enqueue(str(n),'text')
    assert pending(b.store)==[str(n) for n in range(5,15)]


def test_discarded_questions_are_not_resurrected_when_channel_blocked(tmp_path):
    b,_=bridge(tmp_path)
    b.channel_blocked=True
    async def latest(thread):return {'id':'turn'}
    b.codex.latest=latest
    questions=[]
    for n in range(15):
        q,_=b.store.add(str(n),'thread','turn',[{'id':'q','question':'Continue?','options':[]}])
        questions.append(q)
    async def send_all():
        for q in questions:await b.send(q)
    asyncio.run(send_all())
    assert all(b.store.get(q['id'])['outbound']=='discarded' for q in questions[:5])
    assert all(b.store.get(q['id'])['outbound']=='blocked_session' for q in questions[5:])
