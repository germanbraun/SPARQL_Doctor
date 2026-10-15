import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import fake_endpoint  # noqa: E402
from sparql_doctor import cli  # noqa: E402
from sparql_doctor.checks import connectivity, cors, federation, formats, keepalive, query_length, user_agent  # noqa: E402
from sparql_doctor.model import Context, Status  # noqa: E402
from sparql_doctor.transport import Target  # noqa: E402


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(v, raising=False)


def make(**cfg):
    srv = fake_endpoint.start(**cfg)
    ctx = Context(target=Target(fake_endpoint.url(srv), delay=0, timeout=5), idle_max=5,
                  max_get=32 * 1024, max_post=256 * 1024, service_timeout=5)
    return srv, ctx


def test_connectivity_all_styles():
    srv, ctx = make()
    r = connectivity.run(ctx)
    assert r.status == Status.PASS
    assert any("All SPARQL 1.1 request styles work" in f.message for f in r.findings)
    assert r.data["server"]["Server"].startswith("FakeSPARQL")
    srv.shutdown()


def test_unreachable_port():
    ctx = Context(target=Target("http://127.0.0.1:1/sparql", delay=0, timeout=2))
    r = connectivity.run(ctx)
    assert r.status == Status.FAIL and ctx.notes["unreachable"]


def test_user_agent_policy():
    srv, ctx = make(block_ua=True)
    r = user_agent.run(ctx)
    assert r.status == Status.WARN
    msg = r.findings[0].message
    assert "Java default" in msg and "no User-Agent" in msg and "Browser" not in msg
    srv.shutdown()


def test_idle_timeout_graceful_close():
    srv, ctx = make(idle=1.5, keepalive_header="timeout=30")
    r = keepalive.run(ctx)
    assert r.data["passive_watch"]["outcome"] == "fin"
    assert 1.0 < r.data["passive_watch"]["after"] < 3
    est = r.data["idle_timeout_estimate"]
    assert est["upper"] == 2
    assert r.status == Status.WARN
    msgs = " ".join(f.message for f in r.findings)
    assert "advertises Keep-Alive timeout=30s" in msgs
    srv.shutdown()


def test_idle_timeout_rst():
    srv, ctx = make(idle=1.5, idle_rst=True)
    r = keepalive.run(ctx)
    assert r.data["passive_watch"]["outcome"] == "reset"
    # after an RST the OS reports the next write as ECONNRESET or EPIPE depending on platform
    assert all(x["outcome"] in ("reset", "broken-pipe") for x in r.data["reuse_ladder"] if x["delay"] >= 2)
    srv.shutdown()


def test_idle_no_timeout():
    srv, ctx = make(idle=30)
    ctx.idle_max = 3
    r = keepalive.run(ctx)
    assert r.status == Status.PASS
    srv.shutdown()


def test_query_length_limits():
    srv, ctx = make(max_url=4096, max_body=100_000)
    r = query_length.run(ctx)
    get = r.data["get"]
    assert get["max_ok_url_chars"] <= 4096
    assert 3500 < get["max_ok_query_chars"] < get["first_fail_query_chars"]
    assert get["first_fail_query_chars"] - get["max_ok_query_chars"] <= 128
    assert r.data["post"]["max_ok_bytes"] < 100_000 <= r.data["post"]["first_fail_bytes"]
    srv.shutdown()


def test_cors():
    srv, ctx = make(cors=True)
    assert cors.run(ctx).status == Status.PASS
    srv.shutdown()
    srv, ctx = make(cors=False)
    assert cors.run(ctx).status == Status.INFO
    srv.shutdown()


def test_formats():
    srv, ctx = make()
    r = formats.run(ctx)
    assert "application/sparql-results+json" in r.data["select"]
    assert "text/csv" in r.data["select"]
    assert "application/rdf+xml" not in r.data["graph"]
    assert r.status == Status.WARN  # rdf+xml is an "important" format
    srv.shutdown()


def test_federation_classification():
    srv, ctx = make()
    ok = fake_endpoint.url(srv)
    ctx.services = [ok, "http://blocked.invalid/sparql"]
    r = federation.run(ctx)
    rows = r.tables[0].rows
    assert rows[0][3] == "reachable"
    assert rows[1][3].startswith("DNS: unreachable from both")
    assert r.status == Status.FAIL
    srv.shutdown()


def test_cli_end_to_end(tmp_path):
    srv = fake_endpoint.start(block_ua=True, max_url=8000, cors=True, idle=30)
    md, js = tmp_path / "r.md", tmp_path / "r.json"
    code = cli.main([fake_endpoint.url(srv), "--quick", "--idle-max", "3", "--delay", "0",
                     "-H", "Authorization: Bearer s3cret", "--markdown", str(md), "--json", str(js), "--no-color"])
    assert code == 1  # warnings (UA policy, rdf+xml), no failures
    text = md.read_text()
    assert "s3cret" not in text and "s3cret" not in js.read_text()
    assert "# SPARQL Doctor report" in text and "User-Agent policy" in text
    data = json.loads(js.read_text())
    assert {c["id"] for c in data["checks"]} >= {"connectivity", "keepalive", "federation"}
    srv.shutdown()


def test_content_type_mismatch():
    srv, ctx = make(mislabel=True)
    r = connectivity.run(ctx)
    assert any("Content-Type mismatch" in f.message for f in r.findings)
    srv.shutdown()


def test_cors_broken_preflight_is_info_without_auth():
    srv, ctx = make(cors=True, broken_preflight=True)
    r = cors.run(ctx)
    assert r.status == Status.PASS
    assert any(f.status == Status.INFO and "preflight fails" in f.message for f in r.findings)
    ctx.target.headers["Authorization"] = "Bearer x"
    assert cors.run(ctx).status == Status.WARN
    srv.shutdown()


def test_waf_style_size_limit():
    srv, ctx = make(max_url=2048, max_body=3000, limit_status=403)
    r = query_length.run(ctx)
    assert r.data["post"]["max_ok_bytes"] == 1024
    hints = " ".join(f.hint or "" for f in r.findings)
    assert "web application firewall" in hints
    srv.shutdown()


def test_not_a_sparql_endpoint_skips_rest(tmp_path):
    srv = fake_endpoint.start(html=True)
    js = tmp_path / "r.json"
    assert cli.main([fake_endpoint.url(srv), "--quick", "--delay", "0", "--json", str(js)]) == 2
    checks = {c["id"]: c for c in json.loads(js.read_text())["checks"]}
    fail = next(f for f in checks["connectivity"]["findings"] if f["status"] == "fail")
    assert "HTML page" in fail["hint"]
    assert checks["http"]["status"] != "warn"
    assert checks["keepalive"]["status"] == "skip" and checks["formats"]["status"] == "skip"
    srv.shutdown()


def test_human_bytes_keeps_nearby_sizes_comparable():
    from sparql_doctor.checks.common import human_bytes
    assert human_bytes(1_024_000) == "1,000 KB"
    assert human_bytes(1024 * 1024) == "1,024 KB"
    assert human_bytes(16 * 1024 * 1024) == "16 MB"


def test_generous_post_limit_is_info_not_warn():
    srv, ctx = make(max_body=600_000)
    ctx.max_post = 2 * 1024 * 1024
    r = query_length.run(ctx)
    post = [f for f in r.findings if f.message.startswith("POST (form) limit")][0]
    assert post.status == Status.INFO
    srv.shutdown()


def test_size_search_tests_the_configured_maximum():
    srv, ctx = make()
    ctx.max_post = 100_000  # not a ladder step
    r = query_length.run(ctx)
    assert r.data["post"]["max_ok_bytes"] == 100_000
    srv.shutdown()
