"""A local stand-in for api.openai.com: 503 on one port, a dropped connection on another."""

import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Unavailable(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        body = b'{"error":{"type":"server_error","message":"forced outage"}}'
        self.send_response(503)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        sys.stderr.write("fake-openai 503 %s\n" % self.path)


class Dropped(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        sys.stderr.write("fake-openai drop %s\n" % self.path)
        self.connection.shutdown(socket.SHUT_RDWR)
        self.connection.close()


for port, handler in ((18503, Unavailable), (18504, Dropped)):
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
print("fake-openai ready", flush=True)
threading.Event().wait()
