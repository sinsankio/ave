"""Run state machine, persisted as runs/<id>/state.json.

Each pipeline run moves through fixed phases. States named `awaiting_*` are approval
gates: the pipeline stops there until the user decides, from the shell or a later
`ave` command.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class RunError(Exception):
    """Raised for unknown runs or illegal state transitions."""


class Status(StrEnum):
    CREATED = "created"
    ANALYZING = "analyzing"
    ANALYZED = "analyzed"
    PLANNING = "planning"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    PLAN_APPROVED = "plan_approved"
    EDITING = "editing"
    AWAITING_CHANGE_DECISION = "awaiting_change_decision"
    AWAITING_FINAL_APPROVAL = "awaiting_final_approval"
    EXPORTING = "exporting"
    DONE = "done"
    FAILED = "failed"


S = Status
TRANSITIONS: dict[Status, frozenset[Status]] = {
    S.CREATED: frozenset({S.ANALYZING}),
    S.ANALYZING: frozenset({S.ANALYZED}),
    S.ANALYZED: frozenset({S.PLANNING, S.ANALYZING}),
    S.PLANNING: frozenset({S.AWAITING_PLAN_APPROVAL}),
    # rejecting a plan sends it back to planning with feedback
    S.AWAITING_PLAN_APPROVAL: frozenset({S.PLAN_APPROVED, S.PLANNING}),
    S.PLAN_APPROVED: frozenset({S.EDITING}),
    S.EDITING: frozenset({S.AWAITING_CHANGE_DECISION, S.AWAITING_FINAL_APPROVAL}),
    S.AWAITING_CHANGE_DECISION: frozenset({S.EDITING, S.PLANNING}),
    S.AWAITING_FINAL_APPROVAL: frozenset({S.EXPORTING, S.EDITING, S.PLANNING}),
    S.EXPORTING: frozenset({S.DONE}),
    S.DONE: frozenset({S.EDITING, S.PLANNING}),
    S.FAILED: frozenset(),
}

GATES: dict[Status, str] = {
    S.AWAITING_PLAN_APPROVAL: "plan_approval",
    S.AWAITING_CHANGE_DECISION: "change_decision",
    S.AWAITING_FINAL_APPROVAL: "final_approval",
}

# a failed run can be resumed from the phase it failed in
_RESUMABLE = {S.ANALYZING, S.PLANNING, S.EDITING, S.EXPORTING}

_RUN_ID = re.compile(r"^r-(\d{4,})$")


def _now() -> datetime:
    return datetime.now(UTC)


class Transition(BaseModel):
    src: Status
    dst: Status
    at: datetime
    note: str | None = None


class RunState(BaseModel):
    id: str
    created_at: datetime
    updated_at: datetime
    status: Status = Status.CREATED
    plan_version: int = 0
    failed_in: Status | None = None
    error: str | None = None
    history: list[Transition] = Field(default_factory=list)

    @property
    def pending_gate(self) -> str | None:
        return GATES.get(self.status)

    def can_move(self, dst: Status) -> bool:
        if dst is Status.FAILED:
            return self.status not in (Status.DONE, Status.FAILED)
        if self.status is Status.FAILED:
            return self.failed_in is not None and dst is self.failed_in
        return dst in TRANSITIONS[self.status]

    def move(self, dst: Status, note: str | None = None) -> None:
        if not self.can_move(dst):
            raise RunError(f"run {self.id}: illegal transition {self.status} -> {dst}")
        if dst is Status.FAILED:
            self.failed_in = self.status if self.status in _RESUMABLE else None
            self.error = note
        elif self.status is Status.FAILED:
            self.failed_in = None
            self.error = None
        if dst is Status.AWAITING_PLAN_APPROVAL:
            self.plan_version += 1
        now = _now()
        self.history.append(Transition(src=self.status, dst=dst, at=now, note=note))
        self.status = dst
        self.updated_at = now


class RunStore:
    """Reads and writes run state under a project's runs/ directory."""

    def __init__(self, runs_dir: Path):
        self.dir = runs_dir

    def run_dir(self, run_id: str) -> Path:
        return self.dir / run_id

    def _ids(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        ids = [p.name for p in self.dir.iterdir() if _RUN_ID.match(p.name) and p.is_dir()]
        return sorted(ids, key=lambda i: int(_RUN_ID.match(i).group(1)))

    def create(self) -> RunState:
        ids = self._ids()
        nxt = int(_RUN_ID.match(ids[-1]).group(1)) + 1 if ids else 1
        now = _now()
        state = RunState(id=f"r-{nxt:04d}", created_at=now, updated_at=now)
        self.run_dir(state.id).mkdir(parents=True, exist_ok=False)
        self.save(state)
        return state

    def get(self, run_id: str) -> RunState:
        f = self.run_dir(run_id) / "state.json"
        if not f.is_file():
            raise RunError(f"no such run: {run_id}")
        return RunState.model_validate_json(f.read_text())

    def list(self) -> list[RunState]:
        out = []
        for run_id in self._ids():
            try:
                out.append(self.get(run_id))
            except (RunError, ValueError):
                continue
        return out

    def latest(self) -> RunState | None:
        runs = self.list()
        return runs[-1] if runs else None

    def save(self, state: RunState) -> None:
        f = self.run_dir(state.id) / "state.json"
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(state.model_dump_json(indent=2))
        os.replace(tmp, f)
