"""Codex-side blocker notification and bounded status checks."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import aiohttp
from .config import data_dir

ROOT=Path(__file__).resolve().parents[1]

async def run(args):
    async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(data_dir()/'bridge.sock'))) as session:
        async def call(method,path,data=None):
            async with session.request(method,'http://localhost'+path,json=data) as response:
                result=await response.json()
                if response.status>=400: raise RuntimeError(result.get('error','请求失败'))
                return result
        if args.command=='send-image':
            thread=os.environ.get('CODEX_THREAD_ID')
            if not thread: raise RuntimeError('必须在目标 Codex 会话内执行，缺少 CODEX_THREAD_ID')
            result=await call('POST','/images',{'thread_id':thread,'key':args.key,'path':str(Path(args.path).expanduser().resolve())})
        elif args.command=='ask':
            thread=os.environ.get('CODEX_THREAD_ID')
            if not thread: raise RuntimeError('必须在目标 Codex 会话内执行，缺少 CODEX_THREAD_ID')
            result=await call('POST','/questions',{'thread_id':thread,'key':args.key,
                'kind':'blocker','questions':[{'id':'answer','question':args.question,'options':args.option}]})
        elif args.command=='health': result=await call('GET','/health')
        else:
            deadline=asyncio.get_running_loop().time()+getattr(args,'seconds',0)
            while True:
                result=await call('GET','/questions/'+args.id.upper())
                if result['state'] not in ('pending','received','delivering') or asyncio.get_running_loop().time()>=deadline: break
                await asyncio.sleep(2)
        print(json.dumps(result,ensure_ascii=False))

def main():
    parser=argparse.ArgumentParser()
    commands=parser.add_subparsers(dest='command',required=True)
    ask=commands.add_parser('ask');ask.add_argument('--question',required=True);ask.add_argument('--key',required=True);ask.add_argument('--option',action='append',default=[])
    image=commands.add_parser('send-image');image.add_argument('--path',required=True);image.add_argument('--key',required=True)
    commands.add_parser('health')
    status=commands.add_parser('status');status.add_argument('id')
    wait=commands.add_parser('wait');wait.add_argument('id');wait.add_argument('--seconds',type=int,choices=range(0,56),default=55)
    try: asyncio.run(run(parser.parse_args()))
    except Exception as exc: parser.exit(1,str(exc)+'\n')

if __name__=='__main__': main()
