"""Transport-neutral QQ message adapter, runnable offline without a QQ account."""
from hub.api import dispatch

def handle_message(event,*,context=None):
    text=str(event.get('text')or'').strip()
    for prefix in ('/马娘','/uma','马娘 '):
        if text.startswith(prefix):text=text[len(prefix):].strip();break
    # Public chat uses only the provided context; no scan of MaaUma account books.
    scope=dict(context or{});scope.pop('library_id',None);scope.pop('inventory_path',None)
    result=dispatch({'action':'chat.query','text':text,'server':scope.get('server','cn'),'context':scope})
    answer=(result.get('result')or{}).get('text')or result.get('error')or'这次工具查询尚未完成。'
    reply={'ok':result.get('ok',False),'text':answer,'tool_result':result,
        'destination':{'user_id':event.get('user_id'),'group_id':event.get('group_id')},'sent':False}
    if event.get('render_image')is True:
        from hub.render import render_result
        reply['image']=render_result(result)
    return reply
