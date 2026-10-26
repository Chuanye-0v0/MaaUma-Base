"""Local browser UI and the same JSON tools consumed by chat adapters."""
import argparse
import json
import mimetypes
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,unquote
import webbrowser

TASK=Path(__file__).resolve().parent
sys.path.insert(0,str(TASK))
from hub.api import dispatch,capabilities

def make_handler(storage_root=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send_json(self,value,status=200):
            raw=json.dumps(value,ensure_ascii=False).encode('utf-8');self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def do_GET(self):
            path=urlsplit(self.path).path
            if path=='/api/status':return self.send_json(capabilities())
            if path=='/api/demo-inventory':
                source=TASK/'data/inheritance/demo-inventory.json'
                if source.is_file():return self.send_json(json.loads(source.read_text('utf-8-sig')))
                return self.send_json({'ok':False,'error':'演示马库仍在准备。'},503)
            workers={'/engine/jp.worker.js':TASK/'engine/dist/simulation-jp.worker.js',
                     '/engine/global.worker.js':TASK/'engine/dist/simulation-global.worker.js'}
            if path in workers:
                raw=workers[path].read_bytes();self.send_response(200)
                self.send_header('Content-Type','application/javascript; charset=utf-8');self.send_header('Content-Length',str(len(raw)))
                self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw);return
            web=TASK/'web'
            static={'/':web/'index.html','/index.html':web/'index.html','/style.css':web/'style.css','/app.js':web/'app.js'}
            file=static.get(path)
            if file is None or not file.is_file():return self.send_json({'ok':False,'error':'页面不存在。'},404)
            raw=file.read_bytes();self.send_response(200)
            self.send_header('Content-Type',mimetypes.guess_type(str(file))[0]or'application/octet-stream')
            self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw)
        def do_POST(self):
            if urlsplit(self.path).path!='/api/tool':return self.send_json({'ok':False,'error':'接口不存在。'},404)
            if self.headers.get_content_type()!='application/json':return self.send_json({'ok':False,'error':'请使用JSON工具请求。'},415)
            origin=self.headers.get('Origin')
            if origin and urlsplit(origin).hostname not in {'127.0.0.1','localhost'}:
                return self.send_json({'ok':False,'error':'本地工具只接受本机页面请求。'},403)
            try:
                count=int(self.headers.get('Content-Length','0'))
                if not 0<count<=16*1024*1024:return self.send_json({'ok':False,'error':'请求为空或文件过大。'},413)
                request=json.loads(self.rfile.read(count));result=dispatch(request,storage_root=storage_root)
                return self.send_json(result)
            except (json.JSONDecodeError,UnicodeDecodeError,ValueError):return self.send_json({'ok':False,'error':'请求格式未能读取。'},400)
            except Exception as error:
                print(type(error).__name__+': '+str(error),file=sys.stderr)
                return self.send_json({'ok':False,'error':'工具计算遇到问题，输入仍保留。'},500)
    return Handler

def create_server(port=18765,storage_root=None):
    return ThreadingHTTPServer(('127.0.0.1',port),make_handler(storage_root))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=18765);parser.add_argument('--open',action='store_true')
    args=parser.parse_args();server=create_server(args.port);url=f'http://127.0.0.1:{server.server_port}/'
    print(json.dumps({'ok':True,'url':url,'app_id':'maauma-knowledge-hub','integration_protocol':1}),flush=True)
    if args.open:threading.Timer(.4,lambda:webbrowser.open(url)).start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()

if __name__=='__main__':main()
