"""Low-level HTTP plumbing built on http.client so that every probe controls
exactly which headers are sent and which connection is (re)used."""

from __future__ import annotations

import base64
import errno
import http.client
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

from . import __version__

DEFAULT_USER_AGENT = f"sparql-doctor/{__version__} (SPARQL endpoint diagnostics; python http.client)"

SPARQL_JSON = "application/sparql-results+json"
SPARQL_XML = "application/sparql-results+xml"

_DEFAULT = object()  # sentinel: "use the target's User-Agent"


@dataclass
class Target:
    url: str
    headers: Dict[str, str] = field(default_factory=dict)
    user_agent: str = DEFAULT_USER_AGENT
    timeout: float = 30.0
    insecure: bool = False
    use_proxy: bool = True
    delay: float = 0.15  # politeness gap between requests (seconds)

    def __post_init__(self):
        p = urllib.parse.urlsplit(self.url)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise ValueError(f"not an http(s) URL: {self.url!r}")
        if p.username is not None:
            # Move credentials out of the URL into an Authorization header.
            creds = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
            self.headers.setdefault("Authorization", "Basic " + base64.b64encode(creds.encode()).decode())
            netloc = p.hostname if p.port is None else f"{p.hostname}:{p.port}"
            if ":" in p.hostname:
                netloc = f"[{p.hostname}]" + (f":{p.port}" if p.port else "")
            self.url = urllib.parse.urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
            p = urllib.parse.urlsplit(self.url)
        self.scheme = p.scheme
        self.host = p.hostname
        self.port = p.port or (443 if p.scheme == "https" else 80)
        self.path = p.path or "/"
        self.base_query = p.query

    def with_url(self, url: str) -> "Target":
        return replace(self, url=url, headers=dict(self.headers))

    @property
    def is_https(self) -> bool:
        return self.scheme == "https"

    @property
    def safe_url(self) -> str:
        return redact_url(self.url)

    def path_with(self, params: Optional[Dict[str, str]] = None) -> str:
        qs = self.base_query
        if params:
            extra = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
            qs = f"{qs}&{extra}" if qs else extra
        return self.path + (f"?{qs}" if qs else "")

    def proxy(self) -> Optional[Tuple[str, int]]:
        if not self.use_proxy:
            return None
        proxies = urllib.request.getproxies()
        if urllib.request.proxy_bypass(self.host):
            return None
        url = proxies.get(self.scheme)
        if not url:
            return None
        p = urllib.parse.urlsplit(url if "://" in url else f"http://{url}")
        return p.hostname, p.port or 8080


def redact_url(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    if p.username is None:
        return url
    netloc = "***@" + p.netloc.rsplit("@", 1)[1]
    return urllib.parse.urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))


SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "x-api-key", "api-key"}


def redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    return {k: ("***" if k.lower() in SECRET_HEADERS else v) for k, v in headers.items()}


# --------------------------------------------------------------------------- connections


def ssl_context(target: Target, alpn: Optional[List[str]] = None, verify: Optional[bool] = None) -> ssl.SSLContext:
    verify = (not target.insecure) if verify is None else verify
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if alpn:
        ctx.set_alpn_protocols(alpn)
    return ctx


def open_connection(target: Target, timeout: Optional[float] = None, context: Optional[ssl.SSLContext] = None,
                    connect: bool = False) -> http.client.HTTPConnection:
    """Create (and optionally connect) a connection, honouring HTTP(S)_PROXY via CONNECT
    for https and absolute-form request targets for plain http."""
    timeout = target.timeout if timeout is None else timeout
    proxy = target.proxy()
    if target.is_https:
        ctx = context or ssl_context(target)
        if proxy:
            conn = http.client.HTTPSConnection(proxy[0], proxy[1], timeout=timeout, context=ctx)
            conn.set_tunnel(target.host, target.port)
        else:
            conn = http.client.HTTPSConnection(target.host, target.port, timeout=timeout, context=ctx)
    else:
        if proxy:
            conn = http.client.HTTPConnection(proxy[0], proxy[1], timeout=timeout)
            conn._sd_absolute = f"http://{target.host}:{target.port}"  # type: ignore[attr-defined]
        else:
            conn = http.client.HTTPConnection(target.host, target.port, timeout=timeout)
    if connect:
        conn.connect()
    return conn


def local_port(conn: http.client.HTTPConnection) -> Optional[int]:
    try:
        return conn.sock.getsockname()[1] if conn.sock else None
    except OSError:
        return None


# --------------------------------------------------------------------------- responses


@dataclass
class Response:
    status: Optional[int] = None
    reason: str = ""
    headers: List[Tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    elapsed: float = 0.0
    version: Optional[str] = None
    error: Optional[str] = None
    error_kind: Optional[str] = None
    truncated: bool = False

    def header(self, name: str) -> Optional[str]:
        vals = [v for k, v in self.headers if k.lower() == name.lower()]
        return ", ".join(vals) if vals else None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return (self.header("Content-Type") or "").split(";")[0].strip().lower()

    def text(self, limit: int = 300) -> str:
        t = self.body[:limit * 4].decode("utf-8", "replace")
        t = " ".join(t.split())
        return t[:limit] + ("…" if len(t) > limit else "")

    def describe(self) -> str:
        if self.error:
            return f"{self.error_kind}: {self.error}"
        return f"{self.status} {self.reason}".strip()

    @property
    def ms(self) -> int:
        return int(round(self.elapsed * 1000))


def classify_exception(e: BaseException) -> Tuple[str, str]:
    """Map low-level failures to stable kinds used in reports."""
    if isinstance(e, socket.gaierror):
        return "dns", f"name resolution failed ({e})"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "timeout", "timed out"
    if isinstance(e, ConnectionResetError):
        return "reset", "Connection reset by peer"
    if isinstance(e, ConnectionRefusedError):
        return "refused", "Connection refused"
    if isinstance(e, BrokenPipeError):
        return "broken-pipe", "Broken pipe (peer closed the connection while sending)"
    if isinstance(e, http.client.RemoteDisconnected):
        return "closed", "Remote end closed connection without response"
    if isinstance(e, ssl.SSLCertVerificationError):
        return "tls-cert", e.verify_message or str(e)
    if isinstance(e, ssl.SSLError):
        return "tls", str(e)
    if isinstance(e, http.client.IncompleteRead):
        return "incomplete", f"incomplete read ({len(e.partial)} bytes)"
    if isinstance(e, OSError) and e.errno in (errno.ECONNRESET, errno.EPIPE):
        return "reset", e.strerror or str(e)
    if isinstance(e, OSError) and e.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH):
        return "unreachable", e.strerror or str(e)
    if isinstance(e, http.client.HTTPException):
        return "protocol", f"{type(e).__name__}: {e}"
    return "other", f"{type(e).__name__}: {e}"


_throttle_lock = threading.Lock()
_last_request = [0.0]


def _throttle(delay: float) -> None:
    if delay <= 0:
        return
    with _throttle_lock:
        wait = _last_request[0] + delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.monotonic()


def raw_request(target: Target, method: str, path: str, headers: Optional[Dict[str, str]] = None,
                body: Optional[bytes] = None, conn: Optional[http.client.HTTPConnection] = None,
                user_agent=_DEFAULT, timeout: Optional[float] = None, read_limit: int = 4 * 1024 * 1024,
                throttle: bool = True) -> Response:
    """Send one request. If `conn` is given it is reused and left open; otherwise a fresh
    connection is created and closed. Never raises for network errors."""
    if throttle:
        _throttle(target.delay)
    own = conn is None
    resp_obj = Response()
    hdrs = dict(target.headers)
    ua = target.user_agent if user_agent is _DEFAULT else user_agent
    if ua is not None:
        hdrs["User-Agent"] = ua
    hdrs.update(headers or {})
    start = time.monotonic()
    try:
        if own:
            conn = open_connection(target, timeout=timeout)
        elif timeout is not None and conn.sock is not None:
            conn.sock.settimeout(timeout)
        prefix = getattr(conn, "_sd_absolute", "")
        conn.putrequest(method, prefix + path, skip_accept_encoding=True)
        for k, v in hdrs.items():
            conn.putheader(k, v)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        r = conn.getresponse()
        resp_obj.status, resp_obj.reason = r.status, r.reason
        resp_obj.version = {9: "HTTP/0.9", 10: "HTTP/1.0", 11: "HTTP/1.1"}.get(r.version, str(r.version))
        resp_obj.headers = list(r.getheaders())
        if method == "HEAD":
            resp_obj.body = b""
        else:
            resp_obj.body = r.read(read_limit)
            if not r.isclosed():
                resp_obj.truncated = True
                conn.close()
    except BaseException as e:  # noqa: BLE001 - network probing must not crash
        if isinstance(e, KeyboardInterrupt):
            raise
        resp_obj.error_kind, resp_obj.error = classify_exception(e)
        if conn is not None:
            conn.close()
    finally:
        resp_obj.elapsed = time.monotonic() - start
        if own and conn is not None:
            conn.close()
    return resp_obj


def sparql_request(target: Target, query: str, method: str = "GET", accept: Optional[str] = SPARQL_JSON,
                   headers: Optional[Dict[str, str]] = None, **kw) -> Response:
    """method: GET | POST (form-encoded) | POST-DIRECT (application/sparql-query)."""
    hdrs: Dict[str, str] = {}
    if accept:
        hdrs["Accept"] = accept
    body = None
    if method == "GET":
        path, verb = target.path_with({"query": query}), "GET"
    elif method == "POST":
        path, verb = target.path_with(), "POST"
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        body = urllib.parse.urlencode({"query": query}).encode()
    elif method == "POST-DIRECT":
        path, verb = target.path_with(), "POST"
        hdrs["Content-Type"] = "application/sparql-query"
        body = query.encode()
    else:
        raise ValueError(method)
    hdrs.update(headers or {})
    return raw_request(target, verb, path, hdrs, body, **kw)


def ask_boolean(resp: Response) -> Optional[bool]:
    """Extract the boolean from an ASK result (JSON or XML); None if unparsable."""
    import json
    import re

    if not resp.ok:
        return None
    try:
        if "json" in resp.content_type:
            return bool(json.loads(resp.body.decode("utf-8"))["boolean"])
    except Exception:  # noqa: BLE001
        return None
    m = re.search(rb"<boolean>\s*(true|false)\s*</boolean>", resp.body)
    if m:
        return m.group(1) == b"true"
    return None
