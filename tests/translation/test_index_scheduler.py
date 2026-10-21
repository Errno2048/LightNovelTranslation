"""Persistent task DAG, fair selection, and monotonic pointers."""
from translation.index import Task, TaskStore, fair_select, run_once


def test_dependencies_gate_ready_tasks(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    try:
        store.add(Task("a", "translate", "translate"))
        store.add(Task("b", "review", "review"), depends_on=("a",))
        assert [task["task_id"] for task in store.ready()] == ["a"]
        assert store.claim("a")
        store.complete("a")
        assert [task["task_id"] for task in store.ready()] == ["b"]
    finally:
        store.close()


def test_idempotency_key_reuses_task(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    try:
        first = store.add(Task("a", "translate", "translate",
                               idempotency_key="same"))
        second = store.add(Task("b", "translate", "translate",
                                idempotency_key="same"))
        assert first == second == "a"
        assert len(store.ready()) == 1
    finally:
        store.close()


def test_fair_select_allocates_by_weight():
    ready = ([{"task_id": f"r{i}", "stage": "review", "priority": 0}
              for i in range(10)] +
             [{"task_id": f"t{i}", "stage": "translate", "priority": 0}
              for i in range(10)])
    selected = fair_select(ready, {"review": 40, "translate": 60}, 10)
    assert len(selected) == 10
    assert sum(1 for task in selected if task["stage"] == "review") == 4
    assert sum(1 for task in selected if task["stage"] == "translate") == 6


def test_fair_select_redistributes_shortage():
    ready = ([{"task_id": "r0", "stage": "review", "priority": 0}] +
             [{"task_id": f"t{i}", "stage": "translate", "priority": 0}
              for i in range(10)])
    selected = fair_select(ready, {"review": 40, "translate": 60}, 10)
    assert len(selected) == 10
    assert sum(1 for task in selected if task["stage"] == "review") == 1
    assert sum(1 for task in selected if task["stage"] == "translate") == 9


def test_pointer_advance_is_monotonic_and_idempotent(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    try:
        event = {"kind": "boundary", "start": 0, "end": 10}
        value, inserted = store.advance_pointer("boundary", 10, events=[event])
        assert value == 10 and inserted == 1
        value, inserted = store.advance_pointer("boundary", 9, events=[event])
        assert value == 10 and inserted == 0
        value, inserted = store.advance_pointer("boundary", 20, events=[event])
        assert value == 20 and inserted == 0  # same event was already stored
        assert store.pointer("boundary") == 20
        assert len(store.pending_events()) == 1
        store.ack_event(store.pending_events()[0]["event_id"])
        assert store.pending_events() == []
        store.advance_pointer("materialized", 5, level=1)
        store.advance_pointer("materialized", 3, level=2)
        assert store.pointer("materialized", 1) == 5
        assert store.pointer("materialized", 2) == 3
    finally:
        store.close()


def test_recover_running_and_run_once(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    try:
        store.add(Task("a", "translate", "translate"))
        assert store.claim("a")
        assert store.recover_running() == 1
        assert [task["task_id"] for task in store.ready()] == ["a"]
        calls = []
        result = run_once(store, {"translate": lambda task: calls.append(task["task_id"])},
                          {"translate": 1}, limit=1)
        assert result["completed"] == ["a"]
        assert calls == ["a"]
        assert store.ready() == []
    finally:
        store.close()
