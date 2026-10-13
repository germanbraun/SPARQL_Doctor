"""Render a run as terminal text, Markdown (ticket attachment) or JSON."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Dict, List

from .model import CheckResult, Status, Table

ICONS = {Status.PASS: "✔", Status.INFO: "ℹ", Status.WARN: "⚠", Status.FAIL: "✘", Status.SKIP: "–"}
LABELS = {Status.PASS: "PASS", Status.INFO: "INFO", Status.WARN: "WARN", Status.FAIL: "FAIL", Status.SKIP: "SKIP"}
COLORS = {Status.PASS: "32", Status.INFO: "36", Status.WARN: "33", Status.FAIL: "31", Status.SKIP: "90"}


def _c(text: str, status: Status, color: bool) -> str:
    return f"\033[{COLORS[status]}m{text}\033[0m" if color else text


def _fmt_table(t: Table, indent: str = "    ", max_col: int = 60) -> List[str]:
    cells = [[str(c) if len(str(c)) <= max_col else str(c)[: max_col - 1] + "…" for c in row] for row in t.rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells if i < len(r)]) for i, h in enumerate(t.columns)]
    line = lambda row: indent + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)).rstrip()  # noqa: E731
    return [line(t.columns), indent + "  ".join("─" * w for w in widths)] + [line(r) for r in cells]


def counts(results: List[CheckResult]) -> Dict[str, int]:
    out = {s.value: 0 for s in Status}
    for r in results:
        out[r.status.value] += 1
    return out


def render_text(meta: dict, results: List[CheckResult], color: bool = False, verbose: bool = False) -> str:
    out = [f"SPARQL Doctor {meta['tool_version']} — {meta['endpoint']}",
           f"Run {meta['started']} from {meta['client']['hostname']} ({meta['client']['platform']})", ""]
    for r in results:
        dur = f"({r.duration:.1f}s)"
        out.append(_c(f"{ICONS[r.status]} {LABELS[r.status]:4}", r.status, color)
                   + f"  {r.title}  " + (f"\033[90m{dur}\033[0m" if color else dur))
        if r.status == Status.SKIP:
            out.append(f"    {r.summary}")
        for f in r.findings:
            if not verbose and f.status in (Status.INFO, Status.PASS) and r.status in (Status.WARN, Status.FAIL):
                continue
            out.append("    " + _c(ICONS[f.status], f.status, color) + " " + f.message)
            if f.hint and (verbose or f.status in (Status.WARN, Status.FAIL)):
                out.append("      → " + f.hint)
        if verbose or r.status in (Status.WARN, Status.FAIL):
            for t in r.tables:
                out.append("")
                out.extend(_fmt_table(t))
        out.append("")
    c = counts(results)
    out.append(f"Summary: {c['pass']} pass, {c['warn']} warn, {c['fail']} fail, {c['info']} info, {c['skip']} skipped")
    return "\n".join(out)


def _md_escape(v) -> str:
    return str(v).replace("|", "\\|").replace("\n", " ")


def render_markdown(meta: dict, results: List[CheckResult]) -> str:
    c = counts(results)
    out = ["# SPARQL Doctor report", "",
           "| | |", "|---|---|",
           f"| Endpoint | `{meta['endpoint']}` |"]
    if meta.get("effective_endpoint") and meta["effective_endpoint"] != meta["endpoint"]:
        out.append(f"| Effective endpoint (after redirects) | `{meta['effective_endpoint']}` |")
    out += [f"| Run at | {meta['started']} (duration {meta['duration_s']:.0f} s) |",
            f"| Client host | {meta['client']['hostname']} — {meta['client']['platform']}, Python {meta['client']['python']} |",
            f"| Proxy | {meta['client'].get('proxy') or 'none'} |",
            f"| User-Agent used | `{meta['user_agent']}` |",
            f"| Extra headers | {', '.join(f'`{k}: {v}`' for k, v in meta['headers'].items()) or 'none'} |",
            f"| Tool | sparql-doctor {meta['tool_version']} — `{meta['command']}` |",
            f"| Result | **{c['fail']} fail, {c['warn']} warn**, {c['pass']} pass, {c['info']} info, {c['skip']} skipped |",
            "", "## Overview", "", "| Check | Status | Summary |", "|---|---|---|"]
    for r in results:
        out.append(f"| {r.title} | {ICONS[r.status]} {LABELS[r.status]} | {_md_escape(r.summary)} |")
    out.append("")
    for r in results:
        out += [f"## {ICONS[r.status]} {r.title}", ""]
        if r.status == Status.SKIP:
            out += [f"_Skipped: {r.summary}_", ""]
            continue
        for f in r.findings:
            out.append(f"- **{LABELS[f.status]}** {f.message}")
            if f.hint:
                out.append(f"  - _Hint:_ {f.hint}")
        out.append("")
        for t in r.tables:
            out.append("| " + " | ".join(t.columns) + " |")
            out.append("|" + "---|" * len(t.columns))
            for row in t.rows:
                out.append("| " + " | ".join(_md_escape(x) for x in row) + " |")
            out.append("")
    return "\n".join(out)


def render_json(meta: dict, results: List[CheckResult]) -> str:
    def default(o):
        if isinstance(o, Status):
            return o.value
        return str(o)
    return json.dumps({"meta": meta, "summary": counts(results), "checks": [asdict(r) for r in results]},
                      indent=2, default=default, ensure_ascii=False)
