"""A demo app for the launcher: the smallest thing that holds a port."""
import http.server
import json
import os
import socketserver

PORT = int(os.environ.get("PORT", "5061"))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"app": "hello-api", "path": self.path,
                           "message": "Hello from a demo app."}, indent=2)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, fmt, *args):     # stdout, so the launcher captures it
        print("hello-api " + (fmt % args), flush=True)


print("hello-api listening on http://127.0.0.1:%d" % PORT, flush=True)
with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as srv:
    srv.serve_forever()
