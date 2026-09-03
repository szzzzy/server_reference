# -*- coding: utf-8 -*-
"""HTTPS 文件服务:固件 bin / 音频素材。支持 Range 断点续传、ETag 恒定、Content-Length 校验。"""
import hashlib
import logging
import os
import socketserver
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("vs.http")

ALLOWED_METHODS = {"GET", "HEAD"}


class RangeFileHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("http %s", fmt % args)

    def do_HEAD(self):
        self._serve(head_only=True)

    def do_GET(self):
        self._serve(head_only=False)

    # ---------------- 实现细节 ----------------

    def _serve(self, head_only):
        path = self.path.split("?", 1)[0]
        if path == "/__status":
            self._status(head_only)
            return
        if path == "/__stall":
            self._stall(head_only)
            return
        if path == "/__debug":
            self._debug_page(head_only)
            return
        root = self.server.root_dir
        rel = self.path.split("?", 1)[0]
        rel = os.path.normpath(rel.lstrip("/"))
        if rel.startswith("..") or rel.startswith(os.sep):
            self._send(400, b"bad path", "text/plain", no_range=True)
            return
        file_path = (root / rel).resolve()
        if not str(file_path).startswith(str(root.resolve())) or not file_path.is_file():
            self._send(404, b"not found", "text/plain", no_range=True)
            return

        size = file_path.stat().st_size
        etag = self._etag(file_path)
        range_header = self.headers.get("Range", "")
        if_range = self.headers.get("If-Range", "")

        if range_header and if_range and self._quote(if_range) != etag:
            range_header = ""  # ETag 变化 → 全量

        if not range_header:
            data = file_path.read_bytes() if not head_only else b""
            self._send(200, data, "application/octet-stream", size=size, etag=etag)
            return

        # 解析单段 Range: bytes=start- 或 bytes=start-end
        m = range_header.strip()
        try:
            unit, rng = m.split("=", 1)
            if unit != "bytes" or "," in rng:
                raise ValueError
            start_s, _, end_s = rng.partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else size - 1
            if start < 0 or end < start or start >= size:
                raise ValueError
            end = min(end, size - 1)
        except ValueError:
            self._send(400, b"bad range", "text/plain", no_range=True)
            return

        length = end - start + 1
        with open(file_path, "rb") as f:
            f.seek(start)
            body = f.read(length) if not head_only else b""
        self.send_response(206)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _stall(self, head_only):
        """注压测试端点: GET /__stall?sec=<秒> → 暂停读取 WSS 上行,制造设备 TX 背压。
        供浏览器/curl 手动触发(等价于服务器控制台输入 'wait <秒>')。"""
        import json as _json
        fn = getattr(self.server, "stall_fn", None)
        sec = 5.0
        q = self.path.partition("?")[2]
        for kv in q.split("&"):
            k, _, v = kv.partition("=")
            if k == "sec":
                try:
                    sec = float(v)
                except ValueError:
                    pass
        ok = False
        if fn is not None:
            try:
                fn(sec)
                ok = True
            except Exception as e:
                log.warning("__stall 调用失败: %s", e)
        body = _json.dumps({"ok": ok, "stall_seconds": sec}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body) if not head_only else 0))
        self.end_headers()
        if body and not head_only:
            self.wfile.write(body)

    def _status(self, head_only):
        import json as _json
        fn = getattr(self.server, "status_fn", None)
        body = _json.dumps(fn() if fn else {}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body) if not head_only else 0))
        self.end_headers()
        if body and not head_only:
            self.wfile.write(body)

    def _debug_page(self, head_only):
        """无线联调状态台:浏览器打开 https://<IP>:8443/__debug,每 2 秒自动刷新。"""
        html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>虚拟服务器 · 无线联调状态台</title>
<style>body{font-family:Consolas,monospace;background:#111;color:#0f0;padding:12px}
h1{font-size:16px} pre{white-space:pre-wrap;word-break:break-all;font-size:12px}
.tag{color:#0ff}.ok{color:#0f0}.warn{color:#ff0}</style></head>
<body>
<h1>虚拟服务器 · 无线联调状态台 <span class="tag">每 2s 自动刷新</span></h1>
<div id="conn" class="warn">连接中…</div>
<h1>最近事件</h1><pre id="recent">(空)</pre>
<h1>设备状态</h1><pre id="hub">(空)</pre>
<h1>语音引擎</h1><pre id="engine">(空)</pre>
<script>
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')}
async function pull(){
  try{
    const r=await fetch('/__status',{cache:'no-store'});
    const j=await r.json();
    document.getElementById('conn').innerHTML='<span class="ok">● 在线</span> '+new Date().toLocaleTimeString();
    const rec=(j.recent||[]).slice(-20).map(e=>'['+e.t+'] '+e.kind+' '+e.device+' : '+esc(e.text)).join('\\n');
    document.getElementById('recent').textContent=rec||'(空)';
    document.getElementById('hub').textContent=JSON.stringify(j.hub||{},null,1);
    document.getElementById('engine').textContent=JSON.stringify(j.engine||{},null,1);
  }catch(e){document.getElementById('conn').textContent='连接失败: '+e}
  setTimeout(pull,2000);
}
pull();
</script></body></html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body) if not head_only else 0))
        self.end_headers()
        if body and not head_only:
            self.wfile.write(body)

    def _send(self, code, body, ctype, size=None, etag=None, no_range=False):
        self.send_response(code)
        if not no_range:
            self.send_header("Accept-Ranges", "bytes")
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size if size is not None else len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _etag(self, path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return '"' + h.hexdigest() + '"'

    @staticmethod
    def _quote(s):
        return s.strip().strip('"')


class StoppableServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_ssl_context(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    return ctx


def serve_file_server(root_dir, port, ssl_ctx=None, host="0.0.0.0", status_fn=None):
    """在后台线程启动文件服务;返回 server 对象(可 shutdown)。"""
    root_dir = Path(root_dir).resolve()
    server = StoppableServer((host, port), RangeFileHandler)
    server.root_dir = root_dir
    server.status_fn = status_fn
    if ssl_ctx is not None:
        server.socket = ssl_ctx.wrap_socket(server.socket, server_side=True)
    t = threading.Thread(target=server.serve_forever, name="https-file-server", daemon=True)
    t.start()
    log.info("HTTPS 文件服务已启动: %s:%s(根目录 %s) TLS=%s",
             host, port, root_dir, ssl_ctx is not None)
    return server
