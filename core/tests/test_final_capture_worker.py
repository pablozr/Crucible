from __future__ import annotations

import importlib
import json
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

from crucible_core.core.errors import FinalizationError
from crucible_core.infrastructure.git import final_capture_worker as worker
from crucible_core.schemas.finalizations import (
    FinalCaptureEnvelope,
    FinalCaptureRequest,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    worker.clear_registry_for_tests()
    yield
    for key in list(worker._REGISTRY.keys()):
        try:
            worker.cancel_capture(key)
        except Exception:
            pass
    worker.clear_registry_for_tests()


def _init_repo(root: Path) -> tuple[str, str]:
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "T"], check=True
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "core.autocrlf", "false"],
        check=True,
    )
    (root / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--quiet", "-m", "init"],
        check=True,
    )
    head = (
        subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    branch = (
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "symbolic-ref",
                "--quiet",
                "--short",
                "HEAD",
            ],
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    return head, branch


def _request(
    root: Path, head: str, branch: str, deadline: float
) -> FinalCaptureRequest:
    return FinalCaptureRequest(
        git_root=str(root),
        baseline_head=head,
        baseline_branch=branch,
        baseline_index=b"",
        baseline_files=[],
        max_file_size_bytes=1_000_000,
        deadline_monotonic=deadline,
    )


def test_target_top_level_importable():
    module = importlib.import_module(
        "crucible_core.infrastructure.git.final_capture_worker"
    )
    target = getattr(module, "final_capture_worker_main")
    assert callable(target)
    assert target.__module__ == (
        "crucible_core.infrastructure.git.final_capture_worker"
    )
    # spawn context must be startable without fork/thread/pool.
    assert worker._CTX.get_start_method() == "spawn"


def test_spawn_real_returns_snapshot_with_bytes(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    head, branch = _init_repo(root)
    deadline = time.monotonic() + 10
    key = worker.spawn_capture(
        _request(root, head, branch, deadline + 10),
        "tree-1",
        1,
        "task-1",
    )
    snapshot = worker.wait_capture(key, deadline)
    assert snapshot["head"] == head
    assert snapshot["branch"] == branch
    assert isinstance(snapshot["status"], bytes)
    assert isinstance(snapshot["index"], bytes)
    assert snapshot["baseline_files"] == []
    assert snapshot["changes"] == []
    assert worker._REGISTRY == {}


def test_spawn_real_returns_error_envelope(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    head, branch = _init_repo(root)
    bad = "0" * 40
    deadline = time.monotonic() + 10
    key = worker.spawn_capture(
        _request(root, bad, branch, deadline + 10),
        "tree-err",
        7,
        "task-err",
    )
    with pytest.raises(FinalizationError) as exc:
        worker.wait_capture(key, deadline)
    assert exc.value.code != "FINAL_SNAPSHOT_TIMEOUT"
    assert worker._REGISTRY == {}


def test_wait_timeout_terminates_and_reaps(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    head, branch = _init_repo(root)
    key = worker.spawn_capture(
        _request(root, head, branch, time.monotonic() + 10),
        "tree-timeout",
        3,
        "task-timeout",
    )
    expired = time.monotonic() - 1
    with pytest.raises(FinalizationError) as exc:
        worker.wait_capture(key, expired)
    assert exc.value.code == worker.WAIT_TIMEOUT_CODE
    assert exc.value.code != "FINAL_SNAPSHOT_TIMEOUT"
    assert worker._REGISTRY == {}


def test_cancel_and_reap_tree(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    head, branch = _init_repo(root)
    deadline = time.monotonic() + 10
    key_a = worker.spawn_capture(
        _request(root, head, branch, deadline + 10),
        "tree-cancel",
        1,
        "task-a",
    )
    key_b = worker.spawn_capture(
        _request(root, head, branch, deadline + 10),
        "tree-cancel",
        2,
        "task-b",
    )
    assert worker.snapshot_tree_keys("tree-cancel") == [key_a, key_b]
    assert worker.cancel_capture(key_a) is True
    assert key_a not in worker._REGISTRY
    assert worker.cancel_capture(key_a) is False
    count = worker.cancel_tree_captures("tree-cancel")
    assert count == 1
    assert worker.snapshot_tree_keys("tree-cancel") == []
    # Reap leftovers without leaking processes.
    for key in (key_a, key_b):
        worker.cancel_capture(key)
    assert json.dumps({"ok": True})
    assert str(uuid.uuid4())


def _fake_request() -> FinalCaptureRequest:
    return FinalCaptureRequest(
        git_root="x",
        baseline_head="h",
        baseline_branch="b",
        baseline_index=b"",
        baseline_files=[],
        max_file_size_bytes=1,
        deadline_monotonic=time.monotonic() + 10,
    )


def test_spawn_start_and_cancel_are_atomic(monkeypatch):
    """Cancel racing spawn must block until start completes.

    Registry insertion and Process.start share one BOUNDARY_LOCK
    section, so cancel can never slip between them and remove an
    unstarted handle: the worker always starts before it is
    cancelled, never after the cancel returned.
    """
    start_entered = threading.Event()
    allow_start = threading.Event()
    cancel_done = threading.Event()
    created: list = []

    class _FakeConn:
        def close(self) -> None:
            pass

        def poll(self, timeout=None) -> bool:
            return False

    class _FakeProcess:
        def __init__(self, target, args) -> None:
            self.target = target
            self.args = args
            self.started = False
            self.terminated = False
            created.append(self)

        def start(self) -> None:
            start_entered.set()
            assert allow_start.wait(timeout=10)
            self.started = True

        def is_alive(self) -> bool:
            return self.started and not self.terminated

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def join(self, timeout=None) -> None:
            pass

    class _FakeCtx:
        def Pipe(self, duplex=False):
            return _FakeConn(), _FakeConn()

        def Process(self, target, args):
            return _FakeProcess(target, args)

    monkeypatch.setattr(worker, "_CTX", _FakeCtx())

    errors: list = []
    key_holder: dict = {}

    def _spawn() -> None:
        try:
            key_holder["key"] = worker.spawn_capture(
                _fake_request(), "tree-atomic", 1, "task-atomic"
            )
        except Exception as error:  # pragma: no cover
            errors.append(error)

    spawner = threading.Thread(target=_spawn)
    spawner.start()
    assert start_entered.wait(timeout=10)

    cancel_result: dict = {}

    def _cancel() -> None:
        cancel_result["count"] = worker.cancel_tree_captures("tree-atomic")
        cancel_done.set()

    canceller = threading.Thread(target=_cancel)
    canceller.start()
    # Cancel must stay blocked while start holds BOUNDARY_LOCK.
    assert cancel_done.wait(timeout=0.2) is False
    allow_start.set()
    spawner.join(timeout=10)
    canceller.join(timeout=10)

    assert not errors
    assert len(created) == 1
    # Started before cancel removed it: never started-after-cancel.
    assert created[0].started is True
    assert created[0].terminated is True
    assert cancel_result.get("count") == 1
    assert worker._REGISTRY == {}


def test_spawn_start_error_cleans_registry(monkeypatch):
    """A start failure removes only our entry; nothing starts late."""

    closed: list = []

    class _FakeConn:
        def close(self) -> None:
            closed.append(self)

    class _BoomProcess:
        def __init__(self, target, args) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("boom")

    class _FakeCtx:
        def Pipe(self, duplex=False):
            return _FakeConn(), _FakeConn()

        def Process(self, target, args):
            return _BoomProcess(target, args)

    monkeypatch.setattr(worker, "_CTX", _FakeCtx())
    with pytest.raises(RuntimeError):
        worker.spawn_capture(_fake_request(), "tree-boom", 9, "task")
    assert worker._REGISTRY == {}
    assert len(closed) == 2


def _inject_wait_handle(key, conn, process=None):
    class _DeadProcess:
        def is_alive(self) -> bool:
            return False

        def join(self, timeout=None) -> None:
            pass

    worker._REGISTRY[key] = worker._RunningCapture(
        tree_id=key[0],
        generation=key[1],
        task_id="task",
        process=process or _DeadProcess(),
        conn=conn,
    )


def test_wait_transport_eof_uses_internal_ipc_code():
    """Broken IPC (EOF, invalid envelope) must not look genuine."""

    class _EofConn:
        def poll(self, timeout=None) -> bool:
            return True

        def recv(self):
            raise EOFError("pipe died with the child")

        def close(self) -> None:
            pass

    class _GarbageConn(_EofConn):
        def recv(self):
            return "not-an-envelope"

    key = ("tree-ipc", 1)
    _inject_wait_handle(key, _EofConn())
    with pytest.raises(FinalizationError) as exc:
        worker.wait_capture(key, time.monotonic() + 10)
    assert exc.value.code == worker.IPC_FAILED_CODE
    assert worker._REGISTRY == {}

    _inject_wait_handle(key, _GarbageConn())
    with pytest.raises(FinalizationError) as exc:
        worker.wait_capture(key, time.monotonic() + 10)
    assert exc.value.code == worker.IPC_FAILED_CODE
    assert worker._REGISTRY == {}


def test_wait_genuine_envelope_failure_keeps_its_code():
    """Envelope ok=False travels untouched, even as 500."""

    class _EnvelopeConn:
        def poll(self, timeout=None) -> bool:
            return True

        def recv(self):
            return FinalCaptureEnvelope(
                ok=False,
                error_code="FINALIZATION_FAILED",
                error_status=500,
            )

        def close(self) -> None:
            pass

    key = ("tree-ipc", 2)
    _inject_wait_handle(key, _EnvelopeConn())
    with pytest.raises(FinalizationError) as exc:
        worker.wait_capture(key, time.monotonic() + 10)
    assert exc.value.code == "FINALIZATION_FAILED"
    assert exc.value.code != worker.IPC_FAILED_CODE
    assert worker._REGISTRY == {}


def test_wait_genuine_envelope_timeout_keeps_its_code():
    """A child-side timeout inside the envelope never mixes local."""

    class _TimeoutEnvelopeConn:
        def poll(self, timeout=None) -> bool:
            return True

        def recv(self):
            return FinalCaptureEnvelope(
                ok=False,
                error_code="FINAL_SNAPSHOT_TIMEOUT",
                error_status=400,
            )

        def close(self) -> None:
            pass

    key = ("tree-ipc", 3)
    _inject_wait_handle(key, _TimeoutEnvelopeConn())
    with pytest.raises(FinalizationError) as exc:
        worker.wait_capture(key, time.monotonic() + 10)
    assert exc.value.code == "FINAL_SNAPSHOT_TIMEOUT"
    assert exc.value.code != worker.IPC_FAILED_CODE
    assert exc.value.code != worker.WAIT_TIMEOUT_CODE
    assert worker._REGISTRY == {}
