"""Resumable job state.

The pipeline runs on compute that dies without warning: a Kaggle kernel hits its
12-hour wall, Colab reclaims an idle VM, an Actions runner is cancelled. So every
long operation is expressed as a set of **idempotent units of work** whose
completion is recorded durably the moment it lands -- not at the end of a run.

State lives in JSON so it can be committed, diffed, uploaded to an artifact
store and read by a human. Writes are atomic (temp file + ``os.replace``) because
the process may be killed mid-write.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Iterator

__all__ = ["JobState", "UnitStatus", "load", "save"]

PENDING, RUNNING, DONE, FAILED, SKIPPED = "pending", "running", "done", "failed", "skipped"
TERMINAL = (DONE, SKIPPED)


@dataclass
class UnitStatus:
    """One idempotent unit of work (normally: one release, one cell, one chunk)."""

    unit_id: str
    status: str = PENDING
    attempts: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    output: str | None = None
    checksum: str | None = None
    error: str | None = None
    bytes_in: int | None = None
    rows_out: int | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL


@dataclass
class JobState:
    """Whole-job state: a dict of units plus provenance for the run."""

    job: str = "unnamed"
    schema_version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    units: dict[str, UnitStatus] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    # -- unit lifecycle ---------------------------------------------------- #

    def ensure(self, unit_id: str) -> UnitStatus:
        unit = self.units.get(unit_id)
        if unit is None:
            unit = UnitStatus(unit_id=unit_id)
            self.units[unit_id] = unit
        return unit

    def start(self, unit_id: str) -> UnitStatus:
        unit = self.ensure(unit_id)
        unit.status = RUNNING
        unit.attempts += 1
        unit.started_at = time.time()
        unit.error = None
        self.touch()
        return unit

    def finish(self, unit_id: str, **fields: Any) -> UnitStatus:
        unit = self.ensure(unit_id)
        unit.status = DONE
        unit.finished_at = time.time()
        unit.error = None
        for key, value in fields.items():
            if hasattr(unit, key):
                setattr(unit, key, value)
        self.touch()
        return unit

    def fail(self, unit_id: str, error: str) -> UnitStatus:
        unit = self.ensure(unit_id)
        # A failure ends an attempt. If the caller drove this unit straight to
        # failure without start(), count it here -- otherwise the retry cap never
        # advances and a permanently broken unit retries forever.
        if unit.status != RUNNING:
            unit.attempts += 1
        unit.status = FAILED
        unit.finished_at = time.time()
        unit.error = error[:2000]
        self.touch()
        return unit

    def skip(self, unit_id: str, reason: str = "") -> UnitStatus:
        unit = self.ensure(unit_id)
        unit.status = SKIPPED
        unit.finished_at = time.time()
        if reason:
            unit.error = reason[:2000]
        self.touch()
        return unit

    def touch(self) -> None:
        self.updated_at = time.time()

    # -- queries ----------------------------------------------------------- #

    def pending(self, unit_ids: Iterator[str] | list[str]) -> list[str]:
        """Units that still need doing: never finished, or failed under the retry cap."""
        return [
            uid for uid in unit_ids
            if uid not in self.units
            or self.units[uid].status in (PENDING, RUNNING)
            or (self.units[uid].status == FAILED and self.units[uid].attempts < self.max_attempts)
        ]

    @property
    def max_attempts(self) -> int:
        return int(self.meta.get("max_attempts", 3))

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for unit in self.units.values():
            out[unit.status] = out.get(unit.status, 0) + 1
        return out

    def is_complete(self) -> bool:
        return bool(self.units) and all(u.is_terminal for u in self.units.values())

    def progress(self) -> str:
        counts = self.counts()
        total = len(self.units)
        done = counts.get(DONE, 0) + counts.get(SKIPPED, 0)
        pct = (100.0 * done / total) if total else 0.0
        bits = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        return f"{done}/{total} complete ({pct:.1f}%) :: {bits}"

    # -- serialisation ----------------------------------------------------- #

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobState":
        units = {
            uid: UnitStatus(**u) for uid, u in (data.get("units") or {}).items()
        }
        return cls(
            job=data.get("job", "unnamed"),
            schema_version=data.get("schema_version", 1),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            units=units,
            meta=data.get("meta") or {},
        )


def save(state: JobState, path: str) -> str:
    """Atomically persist state. Safe to call after every unit completes."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh, indent=2, sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def load(path: str) -> JobState:
    """Load state, or return an empty state when the file does not exist yet."""
    if not os.path.exists(path):
        return JobState()
    with open(path, "r", encoding="utf-8") as fh:
        return JobState.from_dict(json.load(fh))
