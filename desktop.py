"""Launch the browser UI and its local terminal worker. Standard library only."""
import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import sys
from threading import Timer
from urllib.parse import unquote, urlsplit
import webbrowser

UI = Path(__file__).resolve().parent / "ui"


class Handler(BaseHTTPRequestHandler):
    def __init__(self,*args,session,**kwargs):
        self.session=session
        super().__init__(*args,**kwargs)

    def log_message(self,*args):
        pass

    def send_body(self, status, payload, content_type):
        self.send_response(status)
        self.send_header("Content-Type",content_type)
        self.send_header("Content-Length",str(len(payload)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("X-Frame-Options","DENY")
        self.send_header("Referrer-Policy","no-referrer")
        self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        if self.command!="HEAD":
            try:self.wfile.write(payload)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass

    def respond(self,status,obj):
        self.send_body(status,json.dumps(obj,ensure_ascii=False).encode(),"application/json; charset=utf-8")

    def trusted(self):
        host=f"127.0.0.1:{self.server.server_port}"
        return self.headers.get("Host")==host and self.headers.get("Origin",f"http://{host}")==f"http://{host}"

    def do_GET(self):
        if not self.trusted():
            return self.respond(403,{"error":"请通过本机工作台地址打开。"})
        path=unquote(urlsplit(self.path).path)
        if path=="/api/state":return self.respond(200,self.session.state())
        if path=="/favicon.ico":return self.send_body(204,b"","image/x-icon")
        resource=(UI/(path.lstrip("/") or "index.html")).resolve()
        if not resource.is_relative_to(UI) or not resource.is_file():
            return self.respond(404,{"error":"找不到页面。"})
        kind={".js":"text/javascript",".css":"text/css",".html":"text/html"}.get(resource.suffix)
        self.send_body(200,resource.read_bytes(),kind+"; charset=utf-8" if kind else mimetypes.guess_type(resource.name)[0] or "application/octet-stream")

    do_HEAD=do_GET

    def do_POST(self):
        if not self.trusted() or not secrets.compare_digest(self.headers.get("X-Workbench-Token","").encode(),self.session.token.encode()):
            return self.respond(403,{"error":"页面连接已过期，请刷新本机工作台。"})
        if self.headers.get_content_type()!="application/json":
            return self.respond(415,{"error":"请发送 JSON。"})
        if self.path not in {"/api/"+p for p in ("login","logout","refresh","model","preview","start","resume","chat","chat-action")}:
            return self.respond(404,{"error":"找不到操作。"})
        try:
            length=int(self.headers.get("Content-Length","0"))
            if not 0<length<=32768:raise ValueError("请求长度超出范围。")
            payload=json.loads(self.rfile.read(length))
            if not isinstance(payload,dict):raise ValueError("请求格式有误。")
            self.session.dispatch(self.path.rsplit("/",1)[1],payload)
        except (ValueError,TypeError) as exc:
            return self.respond(400,{"error":str(exc)})
        self.respond(200,{"accepted":True})


def serve(data, port=0, *, open_browser=True):
    from web_session import DesktopSession
    session=DesktopSession(data)
    handler=partial(Handler,session=session)
    with ThreadingHTTPServer(("127.0.0.1",port),handler) as server:
        url=f"http://127.0.0.1:{server.server_port}/"
        print("SIMtrade 工作台已启动",url,flush=True)
        print("保持此终端开启。浏览器可随时重新打开此地址；按 Ctrl+C 结束工作台。",flush=True)
        if open_browser:Timer(.2,lambda:webbrowser.open(url)).start()
        try:server.serve_forever()
        except KeyboardInterrupt:pass


def main():
    if sys.argv[1:2]==["--agent-tool"]:
        from agent_terminal import terminal_main
        raise SystemExit(terminal_main(sys.argv[2:]))
    if "--terminal" in sys.argv or "--self-check" in sys.argv:
        import workbench
        if "--terminal" in sys.argv:sys.argv.remove("--terminal")
        workbench.main()
        return
    parser=argparse.ArgumentParser(description="SIMtrade 本机工作台（--terminal 进入纯终端菜单）")
    parser.add_argument("--no-browser",action="store_true",help="只启动本机服务，手动打开终端显示的地址")
    parser.add_argument("--port",type=int,default=0,help="本机端口，默认自动选择空闲端口")
    parser.add_argument("--data",type=Path,help="存档目录，终端与可视界面共用")
    args=parser.parse_args()
    from workbench import default_data
    serve((args.data or default_data()).resolve(),args.port,open_browser=not args.no_browser)


if __name__=="__main__":
    main()
