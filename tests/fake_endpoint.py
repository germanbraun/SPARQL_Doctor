"""A tiny, misbehaving-on-purpose SPARQL endpoint for tests and demos.

    python tests/fake_endpoint.py --port 8890 --idle 4 --block-ua --max-url 4096 --cors
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import struct
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULTS = dict(idle=30.0, idle_rst=False, block_ua=False, max_url=None, max_body=None, cors=False,
                keepalive_header=None, broken_preflight=False, mislabel=False, limit_status=None, html=False)

SELECT_JSON = {"head": {"vars": ["x"]}, "results": {"bindings": [{"x": {"type": "literal", "value": "1"}}]}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeSPARQL/1.0"

    def setup(self):
        self.timeout = self.server.cfg["idle"]
        self._timed_out = False
        super().setup()

    def log_message(self, fmt, *args):  # quiet
        pass

    def log_error(self, fmt, *args):
        if "timed out" in (fmt % args):
            self._timed_out = True

    def handle(self):
        super().handle()
        if self._timed_out and self.server.cfg["idle_rst"]:
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))

    # ------------------------------------------------------------------ helpers
    def _send(self, status, body=b"", ctype="text/plain", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if self.server.cfg["keepalive_header"]:
            self.send_header("Keep-Alive", self.server.cfg["keepalive_header"])
        if self.server.cfg["cors"] and self.headers.get("Origin"):
            self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _ua_blocked(self):
        if not self.server.cfg["block_ua"]:
            return False
        ua = self.headers.get("User-Agent")
        return ua is None or re.match(r"(Java|python-requests|Apache-HttpClient|curl)/", ua) is not None

    # ------------------------------------------------------------------ verbs
    def do_OPTIONS(self):
        if self.server.cfg["broken_preflight"]:
            return self._send(500, "<html>internal error</html>", "text/html")
        if not self.server.cfg["cors"]:
            return self._send(405, "no")
        self._send(204, b"", extra={"Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                                    "Access-Control-Allow-Headers": "Content-Type, Accept, Authorization"})

    def do_GET(self):
        cfg = self.server.cfg
        if cfg["max_url"] and len(self.path) > cfg["max_url"]:
            return self._send(cfg["limit_status"] or 414, "URI Too Long")
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        self._answer(qs.get("query", [None])[0])

    def do_POST(self):
        cfg = self.server.cfg
        n = int(self.headers.get("Content-Length") or 0)
        if cfg["max_body"] and n > cfg["max_body"]:
            self.close_connection = True
            return self._send(cfg["limit_status"] or 413, "Payload Too Large", extra={"Connection": "close"})
        body = self.rfile.read(n).decode()
        ctype = (self.headers.get("Content-Type") or "").split(";")[0]
        if ctype == "application/x-www-form-urlencoded":
            q = urllib.parse.parse_qs(body).get("query", [None])[0]
        elif ctype == "application/sparql-query":
            q = body
        else:
            return self._send(415, "unsupported content type")
        self._answer(q)

    def _answer(self, q):
        if self.server.cfg["html"]:
            return self._send(405, "<!DOCTYPE html><html><body>Not Allowed</body></html>", "text/html")
        if self._ua_blocked():
            return self._send(403, "Please set a descriptive User-Agent, see our robot policy.")
        if not q:
            return self._send(400, "missing query")
        accept = self.headers.get("Accept") or "*/*"
        m = re.search(r"SERVICE\s*<([^>]+)>", q)
        if m:
            if "blocked" in m.group(1):
                return self._send(500, f"org.eclipse.rdf4j.query.QueryEvaluationException: "
                                       f"java.net.UnknownHostException: {urllib.parse.urlsplit(m.group(1)).hostname}")
            return self._send(200, json.dumps(SELECT_JSON), "application/sparql-results+json")
        kind = re.sub(r"#[^\n]*", "", q).strip().split()[0].upper()
        if kind == "ASK":
            if self.server.cfg["mislabel"] or ("xml" in accept and "json" not in accept):
                return self._send(200, '<?xml version="1.0"?><sparql xmlns="http://www.w3.org/2005/sparql-results#">'
                                       "<head/><boolean>true</boolean></sparql>",
                                  "application/sparql-results+json" if self.server.cfg["mislabel"]
                                  else "application/sparql-results+xml")
            return self._send(200, '{"head":{},"boolean":true}', "application/sparql-results+json")
        if kind == "SELECT":
            if accept in ("*/*", "application/sparql-results+json"):
                return self._send(200, json.dumps(SELECT_JSON), "application/sparql-results+json")
            if accept == "application/sparql-results+xml":
                return self._send(200, '<?xml version="1.0"?><sparql xmlns="http://www.w3.org/2005/sparql-results#">'
                                       '<head><variable name="x"/></head><results><result><binding name="x">'
                                       "<literal>1</literal></binding></result></results></sparql>",
                                  "application/sparql-results+xml")
            if accept == "text/csv":
                return self._send(200, "x\r\n1\r\n", "text/csv")
            return self._send(406, "Not Acceptable")
        if kind == "CONSTRUCT":
            if accept in ("*/*", "text/turtle"):
                return self._send(200, "<urn:s> <urn:p> 1 .\n", "text/turtle")
            if accept == "application/n-triples":
                return self._send(200, '<urn:s> <urn:p> "1"^^<http://www.w3.org/2001/XMLSchema#integer> .\n',
                                  "application/n-triples")
            return self._send(406, "Not Acceptable")
        return self._send(400, "parse error")


def start(**cfg) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", cfg.pop("port", 0)), Handler)
    srv.daemon_threads = True
    srv.cfg = {**DEFAULTS, **cfg}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def url(srv: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}/sparql"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8890)
    ap.add_argument("--idle", type=float, default=30)
    ap.add_argument("--idle-rst", action="store_true")
    ap.add_argument("--block-ua", action="store_true")
    ap.add_argument("--max-url", type=int)
    ap.add_argument("--max-body", type=int)
    ap.add_argument("--cors", action="store_true")
    ap.add_argument("--keepalive-header")
    ap.add_argument("--broken-preflight", action="store_true")
    ap.add_argument("--mislabel", action="store_true", help="XML ASK body labelled as JSON")
    ap.add_argument("--limit-status", type=int, help="status for oversized requests, e.g. 403 like a WAF")
    ap.add_argument("--html", action="store_true", help="answer everything with an HTML 405 page")
    a = vars(ap.parse_args())
    s = start(**a)
    print(f"fake endpoint on {url(s)}")
    threading.Event().wait()
