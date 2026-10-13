"""Observe desktop-hosted question tools, which are not app-server native requests."""
import json
import sqlite3
from pathlib import Path

class Questions:
    def __init__(self, state_db):
        self.state_db=Path(state_db) if state_db else None
        self.positions={}
        self.turns={}
        self.resolved=set()

    def poll(self, thread_ids):
        found=[]
        if not self.state_db or not self.state_db.is_file(): return found
        with sqlite3.connect('file:'+str(self.state_db)+'?mode=ro',uri=True) as db:
            for thread_id in thread_ids:
                row=db.execute('SELECT rollout_path FROM threads WHERE id=?',(thread_id,)).fetchone()
                if not row or not row[0]: continue
                path=Path(row[0])
                try:
                    stat=path.stat()
                    old=self.positions.get(thread_id)
                    if old is None or old[0]!=stat.st_ino or stat.st_size<old[1]:
                        self.positions[thread_id]=(stat.st_ino,stat.st_size)
                        continue  # Never replay history or unanswered questions from previous sessions.
                    with path.open('rb') as stream:
                        stream.seek(old[1])
                        for _ in range(2000):
                            start=stream.tell();line=stream.readline()
                            if not line: break
                            if not line.endswith(b'\n'):
                                stream.seek(start);break
                            try:
                                event=json.loads(line);payload=event.get('payload') or {}
                                if event.get('type')=='event_msg' and payload.get('type')=='user_message':
                                    text=payload.get('message','')
                                    if isinstance(text,str) and text.lstrip().startswith('<send_user_message_question_reply>'):
                                        body=text.split('>',1)[1].split('</send_user_message_question_reply>',1)[0].strip()
                                        for answer in json.loads(body):
                                            key=json.loads(answer['questionItemId'])
                                            if len(key)>=2 and key[0]=='request_user_input_async':self.resolved.add((thread_id,key[1]))
                                if event.get('type')=='event_msg' and payload.get('type')=='task_started':
                                    self.turns[thread_id]=payload.get('turn_id')
                                if event.get('type')=='response_item' and payload.get('type')=='function_call' and payload.get('name') in ('request_user_input_async','functions.request_user_input_async'):
                                    args=json.loads(payload['arguments'])
                                    questions=[{'id':str(i+1),'question':q['title'],'options':q.get('options') or []} for i,q in enumerate(args['questions'])]
                                    found.append((thread_id,payload['call_id'],questions,self.turns.get(thread_id)))
                            except (ValueError,KeyError,TypeError): continue
                        self.positions[thread_id]=(stat.st_ino,stream.tell())
                except FileNotFoundError: continue
        return [item for item in found if (item[0],item[1]) not in self.resolved]
