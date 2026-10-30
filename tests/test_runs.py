from pathlib import Path

import pytest

from ave.runs import TRANSITIONS, RunError, RunStore, Status

HAPPY_PATH = [
    Status.ANALYZING,
    Status.ANALYZED,
    Status.PLANNING,
    Status.AWAITING_PLAN_APPROVAL,
    Status.PLAN_APPROVED,
    Status.EDITING,
    Status.AWAITING_CHANGE_DECISION,
    Status.EDITING,
    Status.AWAITING_FINAL_APPROVAL,
    Status.EXPORTING,
    Status.DONE,
]


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "runs")


def test_every_status_has_transition_entry():
    assert set(TRANSITIONS) == set(Status)


def test_create_increments_ids(store: RunStore):
    assert [store.create().id for _ in range(3)] == ["r-0001", "r-0002", "r-0003"]
    assert store.latest().id == "r-0003"


def test_happy_path_and_persistence(store: RunStore):
    state = store.create()
    for dst in HAPPY_PATH:
        state.move(dst)
        store.save(state)
    loaded = store.get(state.id)
    assert loaded.status is Status.DONE
    assert loaded.plan_version == 1
    assert [t.dst for t in loaded.history] == HAPPY_PATH


def test_pending_gates(store: RunStore):
    state = store.create()
    seen = {}
    for dst in HAPPY_PATH:
        state.move(dst)
        seen[dst] = state.pending_gate
    assert seen[Status.AWAITING_PLAN_APPROVAL] == "plan_approval"
    assert seen[Status.AWAITING_CHANGE_DECISION] == "change_decision"
    assert seen[Status.AWAITING_FINAL_APPROVAL] == "final_approval"
    assert seen[Status.EDITING] is None


def test_plan_rejection_bumps_version(store: RunStore):
    state = store.create()
    for dst in HAPPY_PATH[:4]:
        state.move(dst)
    state.move(Status.PLANNING, note="shorten auth part")
    state.move(Status.AWAITING_PLAN_APPROVAL)
    assert state.plan_version == 2
    assert state.history[-2].note == "shorten auth part"


def test_illegal_transition(store: RunStore):
    state = store.create()
    with pytest.raises(RunError, match="illegal transition created -> editing"):
        state.move(Status.EDITING)


def test_fail_and_resume(store: RunStore):
    state = store.create()
    state.move(Status.ANALYZING)
    state.move(Status.FAILED, note="ocr crashed")
    assert state.failed_in is Status.ANALYZING
    assert state.error == "ocr crashed"
    assert not state.can_move(Status.PLANNING)
    state.move(Status.ANALYZING)
    assert state.error is None and state.failed_in is None


def test_failure_at_gate_is_terminal(store: RunStore):
    state = store.create()
    for dst in HAPPY_PATH[:4]:
        state.move(dst)
    state.move(Status.FAILED)
    assert not any(state.can_move(s) for s in Status)


def test_done_cannot_fail(store: RunStore):
    state = store.create()
    for dst in HAPPY_PATH:
        state.move(dst)
    assert not state.can_move(Status.FAILED)


def test_unknown_run_and_corrupt_state(store: RunStore):
    with pytest.raises(RunError, match="no such run"):
        store.get("r-0042")
    good = store.create()
    bad = store.create()
    (store.run_dir(bad.id) / "state.json").write_text("{not json")
    assert [r.id for r in store.list()] == [good.id]


def test_ignores_foreign_dirs(store: RunStore):
    store.create()
    (store.dir / "scratch").mkdir()
    assert [r.id for r in store.list()] == ["r-0001"]
    assert store.create().id == "r-0002"
