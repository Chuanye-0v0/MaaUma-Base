"""Optional NoneBot2 command bridge. Loading this plugin never logs in to QQ."""
import asyncio
import json
import os
from urllib.request import Request,urlopen
from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg

matcher=on_command('马娘',aliases={'uma'},block=True)

def _query(text):
    base=os.environ.get('MAAUMA_KNOWLEDGE_URL','http://127.0.0.1:18765').rstrip('/')
    request=Request(base+'/api/tool',data=json.dumps({'action':'chat.query','text':text,'server':'cn'},ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type':'application/json'},method='POST')
    with urlopen(request,timeout=60)as response:value=json.load(response)
    return (value.get('result')or{}).get('text')or value.get('error')or'本次工具查询没有完成。'

@matcher.handle()
async def receive(args:Message=CommandArg()):
    text=args.extract_plain_text()
    try:answer=await asyncio.to_thread(_query,text)
    except Exception:answer='本地资料工具箱暂时没有连接。资料服务启动后再试即可。'
    await matcher.finish(answer)
