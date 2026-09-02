"""A demo app for the launcher: renders something to look at in the viewer."""
import http.server
import os
import socketserver
import time

PORT = int(os.environ.get("PORT", "5062"))

PAGE = """<!doctype html><meta charset="utf-8">
<title>Clock</title>
<style>
 body {{ margin:0; height:100vh; display:grid; place-items:center;
        font:600 12vw/1 ui-monospace, Consolas, monospace;
        background:#0f1115; color:#5aa9ff; }}
 small {{ display:block; font:400 14px/2 system-ui; color:#98a0b0; text-align:center }}
</style>
<div>{now}<small>demo app, embedded by App Launcher</small></div>
<script>setTimeout(() => location.reload(), 1000)</script>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.format(now=time.strftime("%H:%M:%S")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("clock " + (fmt % args), flush=True)


print("clock listening on http://127.0.0.1:%d" % PORT, flush=True)
with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as srv:
    srv.serve_forever()
