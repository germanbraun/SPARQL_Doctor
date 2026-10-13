"""Keep-alive support and idle-timeout probing.

This is the check behind most "Connection reset by peer" tickets: a client pools a
connection, the server or a load balancer silently drops it after N seconds of idleness,
and the next query on that pooled connection dies.

Three independent measurements, run concurrently:
  * advertised  - the `Keep-Alive: timeout=N` header, if any
  * passive     - hold one idle connection and watch for the peer's FIN/RST
  * active      - a ladder of connections, each idles for d seconds, then sends a query
                  (exactly what a connection pool does)
"""

from __future__ import annotations

import errno
import re
import select
import ssl
import threading
import time
from typing import Dict, List, Optional

from ..model import CheckResult, Context, Status
from ..transport import local_port, open_connection, sparql_request

PROBE = "ASK {}"
LADDER = [2, 5, 10, 20, 30, 45, 58, 65, 75, 95, 125, 185, 245, 305]


def ladder_for(idle_max: float) -> List[float]:
    steps = [d for d in LADDER if d <= idle_max]
    if not steps or steps[-1] < idle_max:
        steps.append(idle_max)
    return steps


def _parse_keep_alive(value: Optional[str]) -> Dict[str, int]:
    out = {}
    for k, v in re.findall(r"(\w+)\s*=\s*(\d+)", value or ""):
        out[k.lower()] = int(v)
    return out


def _watch_idle(ctx: Context, deadline_s: float) -> Dict:
    """Open one connection, run a query, then wait until the peer closes it."""
    t = ctx.target
    conn = open_connection(t)
    try:
        resp = sparql_request(t, PROBE, "GET", conn=conn)
        if not resp.ok or conn.sock is None:
            return {"outcome": "no-keepalive", "response": resp.describe()}
        sock = conn.sock
        start = time.monotonic()
        while True:
            remaining = deadline_s - (time.monotonic() - start)
            if remaining <= 0:
                return {"outcome": "open", "after": deadline_s}
            readable, _, _ = select.select([sock], [], [], min(remaining, 1.0))
            if not readable:
                continue
            try:
                sock.setblocking(False)
                data = sock.recv(1)
            except (ssl.SSLWantReadError, BlockingIOError):
                continue  # TLS housekeeping (e.g. session tickets), not application data
            except ssl.SSLError as e:
                return {"outcome": "tls-error", "after": time.monotonic() - start, "detail": str(e)}
            except ConnectionResetError:
                return {"outcome": "reset", "after": time.monotonic() - start}
            except OSError as e:
                kind = "reset" if e.errno == errno.ECONNRESET else "error"
                return {"outcome": kind, "after": time.monotonic() - start, "detail": str(e)}
            finally:
                try:
                    sock.setblocking(True)
                except OSError:
                    pass
            if data == b"":
                return {"outcome": "fin", "after": time.monotonic() - start}
            return {"outcome": "unexpected-data", "after": time.monotonic() - start}
    finally:
        conn.close()


def _reuse_after(ctx: Context, delay: float) -> Dict:
    """Simulate a connection pool: query, idle `delay` s, query again on the same socket."""
    t = ctx.target
    conn = open_connection(t)
    try:
        first = sparql_request(t, PROBE, "GET", conn=conn, throttle=False)
        if not first.ok:
            return {"delay": delay, "outcome": "first-failed", "detail": first.describe()}
        port = local_port(conn)
        if port is None:
            return {"delay": delay, "outcome": "no-keepalive"}
        time.sleep(delay)
        second = sparql_request(t, PROBE, "GET", conn=conn, timeout=min(t.timeout, 20), throttle=False)
        if second.error:
            return {"delay": delay, "outcome": second.error_kind, "detail": second.error, "ms": second.ms}
        reused = local_port(conn) == port
        return {"delay": delay, "outcome": "ok" if reused else "reconnected",
                "status": second.status, "ms": second.ms}
    finally:
        conn.close()


def _basic_keepalive(ctx: Context, r: CheckResult) -> Optional[Dict]:
    t = ctx.target
    conn = open_connection(t)
    try:
        ports, resps = [], []
        for _ in range(3):
            resp = sparql_request(t, PROBE, "GET", conn=conn)
            resps.append(resp)
            ports.append(local_port(conn))
            if not resp.ok:
                break
        first = resps[0]
        if not first.ok:
            r.add(Status.FAIL, f"Probe query failed: {first.describe()}")
            return None
        ka = _parse_keep_alive(first.header("Keep-Alive"))
        conn_hdr = (first.header("Connection") or "").lower()
        reused = len(ports) == 3 and ports[0] is not None and len(set(ports)) == 1 and all(x.ok for x in resps)
        info = {"connection_header": first.header("Connection"), "keep_alive_header": first.header("Keep-Alive"),
                "reused": reused, "advertised_timeout": ka.get("timeout"), "advertised_max": ka.get("max")}
        r.data["keepalive"] = info
        if "close" in conn_hdr or not reused:
            r.add(Status.WARN, "Server does not keep connections alive (Connection: close or socket not reused).",
                  "Every query pays a new TCP (+TLS) handshake. Usually a proxy setting (e.g. nginx keepalive_timeout 0, "
                  "or HTTP/1.0 upstream). Not a reset risk, but adds latency.")
            return None
        msg = "Keep-alive works (3 queries on one connection)"
        if ka.get("timeout") is not None:
            msg += f"; server advertises Keep-Alive timeout={ka['timeout']}s"
            if ka.get("max") is not None:
                msg += f", max={ka['max']} requests"
        r.add(Status.PASS, msg + ".")
        return info
    finally:
        conn.close()


def run(ctx: Context) -> CheckResult:
    r = CheckResult("keepalive", "Keep-alive & idle timeout")
    basic = _basic_keepalive(ctx, r)
    if basic is None:
        return r.finalize()

    idle_max = ctx.idle_max
    steps = ladder_for(idle_max)
    ctx.log(f"keep-alive: probing idle timeout up to {idle_max:.0f}s ({len(steps)} parallel connections)…")
    watch: Dict = {}
    ladder: List[Dict] = [None] * len(steps)  # type: ignore[list-item]

    def do_watch():
        watch.update(_watch_idle(ctx, idle_max + 5))

    def do_step(i: int, d: float):
        ladder[i] = _reuse_after(ctx, d)

    threads = [threading.Thread(target=do_watch, daemon=True)]
    threads += [threading.Thread(target=do_step, args=(i, d), daemon=True) for i, d in enumerate(steps)]
    for th in threads:
        th.start()
        time.sleep(0.05)
    for th in threads:
        th.join(idle_max + ctx.target.timeout + 30)

    r.data["passive_watch"] = watch
    r.data["reuse_ladder"] = ladder
    tbl = r.table("Idle before 2nd query", "Outcome", "Detail")
    labels = {"ok": "reused OK", "reconnected": "server had closed; client reconnected",
              "reset": "Connection reset by peer", "closed": "closed without response",
              "broken-pipe": "broken pipe", "timeout": "hang / timeout"}
    for row in ladder:
        if row:
            tbl.rows.append([f"{row['delay']:.0f} s", labels.get(row["outcome"], row["outcome"]),
                             row.get("detail") or (f"{row.get('status')} in {row.get('ms')} ms" if row.get("status") else "")])

    good = [x["delay"] for x in ladder if x and x["outcome"] == "ok"]
    bad = [x for x in ladder if x and x["outcome"] not in ("ok", "first-failed", "no-keepalive")]
    max_ok = max(good) if good else None
    first_bad = min(bad, key=lambda x: x["delay"]) if bad else None
    w_out, w_after = watch.get("outcome"), watch.get("after")
    advertised = basic.get("advertised_timeout")

    # Passive observation
    if w_out == "fin":
        r.add(Status.INFO, f"Server closes idle connections gracefully (FIN) after ≈{w_after:.1f} s.")
    elif w_out == "reset":
        r.add(Status.WARN, f"Idle connection was RESET by the peer after ≈{w_after:.1f} s.",
              "An RST instead of a graceful close usually comes from a load balancer or firewall.")
    elif w_out == "open":
        r.add(Status.PASS, f"An idle connection stayed open for the whole probe window ({idle_max:.0f} s).")

    # Active ladder - the client-visible symptom
    if first_bad:
        kind = first_bad["outcome"]
        window = f"between {max_ok:.0f} s and {first_bad['delay']:.0f} s" if max_ok is not None and max_ok < first_bad["delay"] \
            else f"at or below {first_bad['delay']:.0f} s"
        r.data["idle_timeout_estimate"] = {"lower": max_ok, "upper": first_bad["delay"],
                                           "observed_close": w_after if w_out in ("fin", "reset") else None}
        safe = max(1, int(min(first_bad["delay"], w_after or 1e9, advertised or 1e9) * 0.75))
        hint = (f"Clients must not reuse pooled connections idle longer than ~{safe} s. "
                f"Apache HttpClient 4.x: evictIdleConnections({safe}, SECONDS) + setValidateAfterInactivity(1000); "
                f"HttpClient 5: setConnectionTimeToLive/evictIdleConnections; OkHttp: ConnectionPool keepAlive={safe}s; "
                f"or raise the idle timeout on the proxy/LB. Also retry idempotent queries once on reset.")
        if kind == "timeout":
            r.add(Status.FAIL, f"Reusing a connection idle {window} HANGS until timeout (packets silently dropped).",
                  "A NAT gateway / firewall forgets idle flows without telling either side. "
                  "Enable TCP keep-alive below that value or evict idle pooled connections. " + hint)
        elif kind == "reconnected":
            r.add(Status.INFO, f"Connections idle {window} are closed by the server; well-behaved clients reconnect.",
                  hint)
        else:
            r.add(Status.WARN, f"Reusing a connection idle {window} fails with '{labels.get(kind, kind)}'. "
                               f"This is the classic 'Connection reset by peer' pattern.", hint)
    elif good:
        r.add(Status.PASS, f"Pooled connections can be reused after up to {max(good):.0f} s idle.")

    if advertised and w_out in ("fin", "reset") and w_after is not None and w_after + 2 < advertised:
        r.add(Status.WARN, f"Server advertises Keep-Alive timeout={advertised}s but the connection was closed "
                           f"after ≈{w_after:.0f} s.",
              "An intermediary (load balancer, proxy, firewall) has a shorter idle timeout than the server. "
              "Clients trusting the header will hit resets.")
    if ctx.target.proxy():
        r.add(Status.INFO, "Measured through a proxy: timeouts reflect the proxy↔client leg.")
    return r.finalize()
