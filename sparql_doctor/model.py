"""Result model shared by all checks and reporters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Optional


class Status(str, Enum):
    PASS = "pass"
    INFO = "info"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


_RANK = {Status.SKIP: -1, Status.INFO: 0, Status.PASS: 1, Status.WARN: 2, Status.FAIL: 3}


def worst(statuses) -> Status:
    statuses = list(statuses)
    if not statuses:
        return Status.INFO
    return max(statuses, key=lambda s: _RANK[s])


@dataclass
class Finding:
    status: Status
    message: str
    hint: Optional[str] = None


@dataclass
class Table:
    columns: List[str]
    rows: List[List[Any]] = field(default_factory=list)


@dataclass
class CheckResult:
    id: str
    title: str
    status: Optional[Status] = None
    summary: str = ""
    findings: List[Finding] = field(default_factory=list)
    tables: List[Table] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    duration: float = 0.0

    def add(self, status: Status, message: str, hint: Optional[str] = None) -> Finding:
        f = Finding(status, message, hint)
        self.findings.append(f)
        return f

    def table(self, *columns: str) -> Table:
        t = Table(list(columns))
        self.tables.append(t)
        return t

    def finalize(self) -> "CheckResult":
        if self.status is None:
            self.status = worst(f.status for f in self.findings)
        if not self.summary:
            # checks add details first and their conclusion last
            lead = [f for f in self.findings if f.status == self.status] or self.findings
            self.summary = lead[-1].message if lead else ""
        return self

    def skip(self, reason: str) -> "CheckResult":
        self.status = Status.SKIP
        self.summary = reason
        return self


@dataclass
class Context:
    """Options and mutable state shared across checks."""

    target: Any  # transport.Target
    origin: str = "https://example.org"
    services: List[str] = field(default_factory=list)
    idle_max: float = 75.0
    max_get: int = 128 * 1024
    max_post: int = 2 * 1024 * 1024
    service_timeout: float = 60.0
    log: Callable[[str], None] = lambda msg: None
    notes: dict = field(default_factory=dict)
