from __future__ import annotations

import gzip
import hashlib
import subprocess
import time
from pathlib import Path

import pytest

import crucible_core.infrastructure.git.baseline_capture as baseline_capture
import crucible_core.infrastructure.git.final_capture as final_capture
from crucible_core.core.errors import AdmissionError, FinalizationError
from crucible_core.schemas.finalizations import FinalCaptureSnapshot

MAX_SIZE = 1_048_576
FAR_DEADLINE = 1_000_000_000.0


def make_repo(root: Path, files: dict[str, str]) -> None:
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
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--quiet", "-m", "init"],
        check=True,
    )


def head_and_branch(root: Path) -> tuple[str, str]:
    head = (
        subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
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
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    return head, branch


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class ChunkProbe:
    """Record opened paths and chunked reads while advancing the clock."""

    def __init__(
        self, clock: FakeClock, step: float = 0.0, real_open=open
    ) -> None:
        self.clock = clock
        self.step = step
        self.opened: list[str] = []
        self.read_calls = 0
        self.data_reads = 0
        self.max_request = 0
        self._real_open = real_open

    def __call__(self, target, mode="rb", *args, **kwargs):
        self.opened.append(str(target))
        handle = self._real_open(target, mode, *args, **kwargs)
        return self._Handle(handle, self)

    class _Handle:
        def __init__(self, handle, probe: ChunkProbe) -> None:
            self._handle = handle
            self._probe = probe

        def read(self, size: int = -1):
            self._probe.read_calls += 1
            if size is not None and size >= 0:
                self._probe.max_request = max(self._probe.max_request, size)
            data = self._handle.read(size)
            if data:
                self._probe.data_reads += 1
                self._probe.clock.now += self._probe.step
            return data

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            self._handle.close()
            return False


def test_baseline_per_file_hash_budget(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    make_repo(root, {"dirty.txt": "changed\n"})
    (root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    clock = FakeClock()
    probe = ChunkProbe(clock, step=0.6)
    with pytest.raises(AdmissionError) as error:
        baseline_capture.capture_baseline(
            root, MAX_SIZE, FAR_DEADLINE, clock=clock, opener=probe
        )
    assert error.value.code == "BASELINE_HASH_TIMEOUT"


def test_baseline_aggregate_hash_budget(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    make_repo(root, {"a.txt": "a\n", "b.txt": "b\n"})
    (root / "a.txt").write_text("dirty-a\n", encoding="utf-8")
    (root / "b.txt").write_text("dirty-b\n", encoding="utf-8")
    clock = FakeClock()
    # Each file stays within the 500ms per-file budget, but the pair
    # exceeds the 750ms aggregate hashing budget.
    probe = ChunkProbe(clock, step=0.4)
    with pytest.raises(AdmissionError) as error:
        baseline_capture.capture_baseline(
            root, MAX_SIZE, FAR_DEADLINE, clock=clock, opener=probe
        )
    assert error.value.code == "BASELINE_HASH_TIMEOUT"


def test_final_per_file_hash_budget(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    make_repo(root, {"dirty.txt": "before\n"})
    (root / "dirty.txt").write_text("after\n", encoding="utf-8")
    head, branch = head_and_branch(root)
    clock = FakeClock()
    probe = ChunkProbe(clock, step=2.5)
    with pytest.raises(FinalizationError) as error:
        final_capture.capture_final(
            root,
            head,
            branch,
            b"manifest",
            [],
            MAX_SIZE,
            FAR_DEADLINE,
            clock=clock,
            opener=probe,
        )
    assert error.value.code == "FINAL_HASH_TIMEOUT"


def test_final_aggregate_hash_budget(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    make_repo(root, {"a.txt": "a\n", "b.txt": "b\n"})
    (root / "a.txt").write_text("dirty-a\n", encoding="utf-8")
    (root / "b.txt").write_text("dirty-b\n", encoding="utf-8")
    head, branch = head_and_branch(root)
    clock = FakeClock()
    # Each file stays within the 2s per-file budget, but the pair
    # exceeds the 3s aggregate hashing budget.
    probe = ChunkProbe(clock, step=1.6)
    with pytest.raises(FinalizationError) as error:
        final_capture.capture_final(
            root,
            head,
            branch,
            b"manifest",
            [],
            MAX_SIZE,
            FAR_DEADLINE,
            clock=clock,
            opener=probe,
        )
    assert error.value.code == "FINAL_HASH_TIMEOUT"


def test_absolute_deadlines_stay_authoritative(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    make_repo(root, {"dirty.txt": "before\n"})
    (root / "dirty.txt").write_text("after\n", encoding="utf-8")
    head, branch = head_and_branch(root)
    expired = FakeClock(now=2000.0)
    with pytest.raises(AdmissionError) as baseline_error:
        baseline_capture.capture_baseline(
            root, MAX_SIZE, 1000.0, clock=expired
        )
    assert baseline_error.value.code == "BASELINE_CAPTURE_TIMEOUT"
    with pytest.raises(FinalizationError) as final_error:
        final_capture.capture_final(
            root,
            head,
            branch,
            b"manifest",
            [],
            MAX_SIZE,
            1000.0,
            clock=expired,
        )
    assert final_error.value.code == "FINAL_SNAPSHOT_TIMEOUT"


def test_final_retries_unstable_once_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    make_repo(root, {"tracked.txt": "before\n"})
    (root / "tracked.txt").write_text("after\n", encoding="utf-8")
    head, branch = head_and_branch(root)
    original = final_capture._capture_paths
    calls = {"count": 0}

    def _flaky(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise FinalizationError("FINAL_SNAPSHOT_UNSTABLE")
        return original(*args, **kwargs)

    monkeypatch.setattr(final_capture, "_capture_paths", _flaky)
    snapshot = final_capture.capture_final(
        root,
        head,
        branch,
        b"manifest",
        [],
        MAX_SIZE,
        time.monotonic() + 60,
    )
    assert calls["count"] == 3
    assert [change.path for change in snapshot.changes] == ["tracked.txt"]


def test_final_second_unstable_raises_without_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    make_repo(root, {"tracked.txt": "before\n"})
    (root / "tracked.txt").write_text("after\n", encoding="utf-8")
    head, branch = head_and_branch(root)
    calls = {"count": 0}

    def _always_unstable(*args, **kwargs):
        calls["count"] += 1
        raise FinalizationError("FINAL_SNAPSHOT_UNSTABLE")

    monkeypatch.setattr(final_capture, "_capture_paths", _always_unstable)
    with pytest.raises(FinalizationError) as error:
        final_capture.capture_final(
            root,
            head,
            branch,
            b"manifest",
            [],
            MAX_SIZE,
            time.monotonic() + 60,
        )
    # Exactly two complete capture attempts: failure is stable and
    # never returns a partial snapshot to freeze or complete.
    assert error.value.code == "FINAL_SNAPSHOT_UNSTABLE"
    assert calls["count"] == 2


def test_clean_tracked_content_is_never_read(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    make_repo(root, {"clean.txt": "clean\n", "dirty.txt": "before\n"})
    (root / "dirty.txt").write_text("after\n", encoding="utf-8")
    head, branch = head_and_branch(root)
    probe = ChunkProbe(FakeClock())

    baseline = baseline_capture.capture_baseline(
        root, MAX_SIZE, time.monotonic() + 60, opener=probe
    )
    assert str(root / "clean.txt") not in probe.opened
    assert str(root / "dirty.txt") in probe.opened
    assert {row.path for row in baseline.files} == {"dirty.txt"}

    probe.opened.clear()
    snapshot = final_capture.capture_final(
        root,
        head,
        branch,
        b"manifest",
        [],
        MAX_SIZE,
        time.monotonic() + 60,
        opener=probe,
    )
    assert str(root / "clean.txt") not in probe.opened
    assert [change.path for change in snapshot.changes] == ["dirty.txt"]


def test_committed_clean_file_needs_no_worktree_read(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    make_repo(root, {"moved.txt": "before\n"})
    head, _ = head_and_branch(root)
    (root / "moved.txt").write_text("after\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "commit", "--quiet", "-am", "advance"],
        check=True,
    )
    _, branch = head_and_branch(root)
    probe = ChunkProbe(FakeClock())
    snapshot = final_capture.capture_final(
        root,
        head,
        branch,
        b"manifest",
        [],
        MAX_SIZE,
        time.monotonic() + 60,
        opener=probe,
    )
    assert str(root / "moved.txt") not in probe.opened
    assert [change.path for change in snapshot.changes] == ["moved.txt"]


def test_worktree_reads_stream_in_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    payload = "line\n" * 40_000
    make_repo(root, {"big.txt": "seed\n"})
    (root / "big.txt").write_bytes(payload.encode())
    expected = hashlib.sha256(payload.encode()).hexdigest()

    def _forbidden(self, *args, **kwargs):
        raise AssertionError("read_bytes must not be used for hashing")

    monkeypatch.setattr(Path, "read_bytes", _forbidden)
    probe = ChunkProbe(FakeClock())
    baseline = baseline_capture.capture_baseline(
        root, MAX_SIZE, time.monotonic() + 60, opener=probe
    )
    assert probe.data_reads > 1
    assert probe.max_request <= 65536
    (row,) = [item for item in baseline.files if item.path == "big.txt"]
    assert row.sha256 == expected
    assert row.size == len(payload.encode())
    assert row.content is not None
    assert gzip.decompress(row.content).decode() == payload


def test_oversize_file_degrades_to_hash_only(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    payload = "x" * 10_000
    make_repo(root, {"big.txt": "seed\n"})
    (root / "big.txt").write_bytes(payload.encode())
    expected = hashlib.sha256(payload.encode()).hexdigest()
    probe = ChunkProbe(FakeClock())
    baseline = baseline_capture.capture_baseline(
        root, 1024, time.monotonic() + 60, opener=probe
    )
    assert probe.data_reads > 1
    (row,) = [item for item in baseline.files if item.path == "big.txt"]
    assert row.sha256 == expected
    assert row.size == len(payload.encode())
    assert row.content is None


class StepTick:
    """Injectable clock advancing a fixed step on every call."""

    def __init__(self, step: float, now: float = 100.0) -> None:
        self.step = step
        self.now = now

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_baseline_aggregate_spans_reads_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    make_repo(root, {"a.txt": "a\n"})
    (root / "a.txt").write_text("dirty-a\n", encoding="utf-8")
    clock = FakeClock()
    probe = ChunkProbe(clock, step=0.3)
    original_identity = baseline_capture._file_identity
    calls = {"count": 0}

    def _mismatch_once(files):
        calls["count"] += 1
        if calls["count"] == 1:
            return [("forced-mismatch",)]
        return original_identity(files)

    monkeypatch.setattr(baseline_capture, "_file_identity", _mismatch_once)
    # First attempt hashes twice (0.3 + 0.3) then retries on the forced
    # identity mismatch; the retry must reuse the same aggregate budget
    # and fail instead of restarting from zero (0.6 + 0.3 > 0.75).
    with pytest.raises(AdmissionError) as error:
        baseline_capture.capture_baseline(
            root, MAX_SIZE, FAR_DEADLINE, clock=clock, opener=probe
        )
    assert error.value.code == "BASELINE_HASH_TIMEOUT"


def test_final_aggregate_spans_stability_reads(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    make_repo(root, {"a.txt": "a\n"})
    (root / "a.txt").write_text("dirty-a\n", encoding="utf-8")
    head, branch = head_and_branch(root)
    clock = FakeClock()
    # One dirty file stays within the 2s per-file budget, but hashing it
    # on both stability reads exceeds the 3s aggregate (1.6 + 1.6).
    probe = ChunkProbe(clock, step=1.6)
    with pytest.raises(FinalizationError) as error:
        final_capture.capture_final(
            root,
            head,
            branch,
            b"manifest",
            [],
            MAX_SIZE,
            FAR_DEADLINE,
            clock=clock,
            opener=probe,
        )
    assert error.value.code == "FINAL_HASH_TIMEOUT"


def _make_committed_clean(root: Path, count: int) -> tuple[str, str]:
    files = {f"f{i}.txt": f"content {i}\n" for i in range(count)}
    make_repo(root, files)
    head, _ = head_and_branch(root)
    for index in range(count):
        (root / f"f{index}.txt").write_text(
            f"changed {index}\n", encoding="utf-8"
        )
    subprocess.run(
        ["git", "-C", str(root), "commit", "--quiet", "-am", "advance"],
        check=True,
    )
    _, branch = head_and_branch(root)
    return head, branch


def test_final_blobs_share_aggregate_budget(tmp_path: Path) -> None:
    single = tmp_path / "single"
    head, branch = _make_committed_clean(single, 1)
    snapshot = final_capture.capture_final(
        single,
        head,
        branch,
        b"manifest",
        [],
        MAX_SIZE,
        FAR_DEADLINE,
        clock=StepTick(0.2),
    )
    assert [change.path for change in snapshot.changes] == ["f0.txt"]

    pair = tmp_path / "pair"
    head, branch = _make_committed_clean(pair, 2)
    # Each committed-clean blob stays within the per-file budget, but the
    # baseline/final blob pair on both stability reads must share the 3s
    # aggregate with worktree hashing under the same injected clock.
    with pytest.raises(FinalizationError) as error:
        final_capture.capture_final(
            pair,
            head,
            branch,
            b"manifest",
            [],
            MAX_SIZE,
            FAR_DEADLINE,
            clock=StepTick(0.2),
        )
    assert error.value.code == "FINAL_HASH_TIMEOUT"


def test_compression_deadline_blocks_baseline_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    make_repo(root, {"a.txt": "a\n"})
    (root / "a.txt").write_text("dirty\n", encoding="utf-8")
    clock = FakeClock(now=100.0)
    real_compress = gzip.compress

    def _advance_on_compress(data: bytes, *args, **kwargs):
        clock.now = 200.0
        return real_compress(data, *args, **kwargs)

    monkeypatch.setattr(gzip, "compress", _advance_on_compress)
    with pytest.raises(AdmissionError) as error:
        baseline_capture.capture_baseline(root, MAX_SIZE, 150.0, clock=clock)
    assert error.value.code == "BASELINE_CAPTURE_TIMEOUT"


def test_compression_deadline_blocks_blob_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    head, branch = _make_committed_clean(root, 1)
    clock = FakeClock(now=100.0)
    real_compress = gzip.compress

    def _advance_on_compress(data: bytes, *args, **kwargs):
        clock.now = 200.0
        return real_compress(data, *args, **kwargs)

    monkeypatch.setattr(gzip, "compress", _advance_on_compress)
    with pytest.raises(FinalizationError) as error:
        final_capture.capture_final(
            root,
            head,
            branch,
            b"manifest",
            [],
            MAX_SIZE,
            150.0,
            clock=clock,
        )
    assert error.value.code == "FINAL_SNAPSHOT_TIMEOUT"


class FakeMonotonic:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.now


class BlockingStream:
    """Stream whose read blocks until closed; used for timeout tests."""

    def __init__(self) -> None:
        import threading

        self._event = threading.Event()
        self.read_calls = 0

    def read(self, size: int = -1):
        self.read_calls += 1
        self._event.wait(timeout=30)
        return b""

    def close(self) -> None:
        self._event.set()


def test_blocked_git_stream_respects_deadline_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time as _time

    from crucible_core.infrastructure.git.content_hash import (
        HashBudget,
        hash_stream,
    )

    root = tmp_path / "repo"
    head, _ = _make_committed_clean(root, 1)
    clock = FakeClock(now=100.0)
    budget = HashBudget(aggregate_budget=10.0, per_file_budget=10.0)
    stream = BlockingStream()

    def _timeout_error() -> FinalizationError:
        return FinalizationError("FINAL_SNAPSHOT_TIMEOUT")

    def _budget_error() -> FinalizationError:
        return FinalizationError("FINAL_HASH_TIMEOUT")

    started = _time.monotonic()
    with pytest.raises(FinalizationError) as error:
        hash_stream(
            stream,
            MAX_SIZE,
            tick=clock,
            budget=budget,
            file_started_at=100.0,
            deadline=FAR_DEADLINE,
            deadline_error=_timeout_error,
            budget_error=_budget_error,
            subprocess_timeout=0.05,
        )
    elapsed = _time.monotonic() - started
    assert error.value.code == "FINAL_SNAPSHOT_TIMEOUT"
    assert elapsed < 5.0
    assert budget.spent == 0.0

    killed: dict[str, bool] = {}
    reaped: dict[str, bool] = {}

    class _FakeStdout(BlockingStream):
        pass

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout: BlockingStream | None = _FakeStdout()
            self.returncode: int | None = None

        def poll(self):
            return self.returncode

        def kill(self) -> None:
            killed["called"] = True
            assert self.stdout is not None
            self.stdout._event.set()

        def wait(self, timeout=None):
            reaped["called"] = True
            assert self.stdout is not None
            self.stdout._event.set()
            self.returncode = -9
            return self.returncode

    fake_proc = _FakeProc()

    def _fake_popen(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr(final_capture.subprocess, "Popen", _fake_popen)
    blob_budget = HashBudget(aggregate_budget=10.0, per_file_budget=10.0)
    started = _time.monotonic()
    with pytest.raises(FinalizationError) as blob_error:
        final_capture._stream_git_blob_to_row(
            root,
            head,
            "f0.txt",
            "  ",
            MAX_SIZE,
            FAR_DEADLINE,
            clock,
            blob_budget,
        )
    elapsed = _time.monotonic() - started
    assert blob_error.value.code == "FINAL_SNAPSHOT_TIMEOUT"
    assert elapsed < 5.0
    assert killed.get("called") is True
    assert reaped.get("called") is True


def test_subprocess_error_after_deadline_prefers_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from crucible_core.infrastructure.git.content_hash import HashBudget

    root = tmp_path / "repo"
    make_repo(root, {"a.txt": "a\n"})
    expired = FakeClock(now=200.0)

    def _boom(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["git"])

    monkeypatch.setattr(baseline_capture.subprocess, "run", _boom)
    with pytest.raises(AdmissionError) as baseline_error:
        baseline_capture._git(root, ["rev-parse"], 100.0, expired)
    assert baseline_error.value.code == "BASELINE_CAPTURE_TIMEOUT"

    monkeypatch.setattr(final_capture.subprocess, "run", _boom)
    with pytest.raises(FinalizationError) as final_error:
        final_capture._git(root, ["rev-parse"], 100.0, expired)
    assert final_error.value.code == "FINAL_SNAPSHOT_TIMEOUT"

    class _FailProc:
        stdout = None
        returncode = 1

        def poll(self):
            return 1

        def kill(self) -> None:
            return None

        def wait(self, timeout=None):
            return 1

    class _EofStream:
        def read(self, size: int = -1):
            return b""

        def close(self) -> None:
            return None

    class _ExpiringStream:
        """EOF stream that pushes the fake clock past the deadline."""

        def __init__(self, clock: FakeClock) -> None:
            self._clock = clock

        def read(self, size: int = -1):
            self._clock.now = 200.0
            return b""

        def close(self) -> None:
            return None

    def _fake_popen_eof(*args, **kwargs):
        proc = _FailProc()
        proc.stdout = _ExpiringStream(post_clock)
        return proc

    monkeypatch.setattr(final_capture.subprocess, "Popen", _fake_popen_eof)
    post_clock = FakeClock(now=90.0)
    blob_budget = HashBudget(aggregate_budget=10.0, per_file_budget=10.0)
    with pytest.raises(FinalizationError) as blob_error:
        final_capture._stream_git_blob_to_row(
            root,
            "deadbeef",
            "missing.txt",
            "  ",
            MAX_SIZE,
            100.0,
            post_clock,
            blob_budget,
        )
    assert blob_error.value.code == "FINAL_SNAPSHOT_TIMEOUT"


def test_git_stream_oserror_charges_once_and_promotes_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from crucible_core.infrastructure.git.content_hash import HashBudget

    root = tmp_path / "repo"
    make_repo(root, {"a.txt": "a\n"})

    class _FlakyStream:
        def __init__(self, clock: FakeClock) -> None:
            self._clock = clock
            self.calls = 0

        def read(self, size: int = -1):
            self.calls += 1
            if self.calls == 1:
                self._clock.now += 0.6
                raise OSError("broken pipe")
            return b""

        def close(self) -> None:
            return None

    clock = FakeClock(now=100.0)
    budget = HashBudget(aggregate_budget=10.0, per_file_budget=0.5)

    class _FakeProc:
        def __init__(self, stream) -> None:
            self.stdout = stream
            self.returncode: int | None = None

        def poll(self):
            return self.returncode

        def kill(self) -> None:
            return None

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

    stream = _FlakyStream(clock)

    def _fake_popen(*args, **kwargs):
        return _FakeProc(stream)

    monkeypatch.setattr(final_capture.subprocess, "Popen", _fake_popen)
    with pytest.raises(FinalizationError) as error:
        final_capture._stream_git_blob_to_row(
            root,
            "deadbeef",
            "a.txt",
            "  ",
            MAX_SIZE,
            FAR_DEADLINE,
            clock,
            budget,
        )
    assert error.value.code == "FINAL_HASH_TIMEOUT"
    assert stream.calls == 1
    assert budget.spent == pytest.approx(0.6)

    clock2 = FakeClock(now=50.0)
    budget2 = HashBudget(aggregate_budget=10.0, per_file_budget=10.0)

    class _QuickFlaky:
        def read(self, size: int = -1):
            raise OSError("gone")

        def close(self) -> None:
            return None

    def _fake_popen_quick(*args, **kwargs):
        return _FakeProc(_QuickFlaky())

    monkeypatch.setattr(final_capture.subprocess, "Popen", _fake_popen_quick)
    with pytest.raises(FinalizationError) as error2:
        final_capture._stream_git_blob_to_row(
            root,
            "deadbeef",
            "a.txt",
            "  ",
            MAX_SIZE,
            FAR_DEADLINE,
            clock2,
            budget2,
        )
    assert error2.value.code == "BASELINE_OBJECT_UNAVAILABLE"
    assert budget2.spent == pytest.approx(0.0)


def test_coordinator_freeze_blocked_after_hook_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sqlite3

    from fastapi.testclient import TestClient

    import crucible_core.services.finalizations as finalization_service
    from crucible_core.main import app

    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(
        "CRUCIBLE_TERMINAL_MAX_AUTH_WINDOW_SECONDS", "3000000000"
    )
    root = tmp_path / "repo"
    project_id = _coordinator_project(root)

    with TestClient(app) as client:
        admitted = client.post(
            "/v1/events", json=_coordinator_candidate(project_id, root)
        )
        assert admitted.status_code == 200, admitted.text
        task_id = admitted.json()["data"]["event"]["task_id"]
        event = _coordinator_completion(project_id, root, task_id)

        monotonic = FakeMonotonic(now=100.0)
        captured: dict[str, float] = {}

        def _fake_capture(*args, **kwargs):
            captured["deadline"] = args[6] if len(args) > 6 else 0.0
            return FinalCaptureSnapshot(
                head="h",
                branch="b",
                status=b"",
                index=b"manifest",
                baseline_files=[],
                changes=[],
            )

        def _fake_hook() -> None:
            monotonic.now = 200.0

        monkeypatch.setattr(
            finalization_service, "_capture_final", _fake_capture
        )
        monkeypatch.setattr(
            finalization_service, "_PUBLICATION_HOOK", _fake_hook
        )
        monkeypatch.setattr(finalization_service, "_MONOTONIC", monotonic)
        response = client.post("/v1/events", json=event)

    assert response.status_code == 400
    assert response.json()["data"]["code"] == "FINAL_SNAPSHOT_TIMEOUT"
    assert captured["deadline"] == pytest.approx(105.0)
    connection = sqlite3.connect(tmp_path / "data" / "crucible.db")
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_id,),
        ).fetchone()
        changes = connection.execute(
            "SELECT COUNT(*) FROM task_file_changes WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert task == ("failed", "FINAL_SNAPSHOT_TIMEOUT", None)
    assert changes == 0


def test_worktree_blocked_read_is_bounded() -> None:
    import time as _time

    from crucible_core.infrastructure.git.content_hash import (
        HashBudget,
        hash_worktree_file,
    )

    class _BlockingHandle:
        def __init__(self) -> None:
            import threading

            self._event = threading.Event()
            self.read_calls = 0

        def read(self, size: int = -1):
            self.read_calls += 1
            self._event.wait(timeout=30)
            return b""

        def close(self) -> None:
            self._event.set()

    class _Opener:
        def __init__(self, handle) -> None:
            self._handle = handle

        def __call__(self, target, mode="rb", *args, **kwargs):
            return self._Handle(self._handle)

        class _Handle:
            def __init__(self, handle) -> None:
                self._handle = handle

            def __enter__(self):
                return self._handle

            def __exit__(self, *exc) -> bool:
                return False

    # Budget-bound: far deadline but tight per-file budget still times out.
    clock = FakeClock(now=100.0)
    budget = HashBudget(aggregate_budget=10.0, per_file_budget=0.5)
    handle = _BlockingHandle()
    started = _time.monotonic()
    with pytest.raises(AdmissionError) as error:
        hash_worktree_file(
            Path("blocked.txt"),
            MAX_SIZE,
            tick=clock,
            open_fn=_Opener(handle),
            budget=budget,
            file_started_at=100.0,
            deadline=FAR_DEADLINE,
            deadline_error=lambda: AdmissionError("BASELINE_CAPTURE_TIMEOUT"),
            budget_error=lambda: AdmissionError("BASELINE_HASH_TIMEOUT"),
        )
    elapsed = _time.monotonic() - started
    assert error.value.code == "BASELINE_HASH_TIMEOUT"
    assert elapsed < 5.0
    assert handle.read_calls >= 1
    assert budget.spent == 0.0
    handle.close()

    # Deadline-bound: generous budget but tight deadline prefers timeout.
    clock2 = FakeClock(now=100.0)
    budget2 = HashBudget(aggregate_budget=10.0, per_file_budget=10.0)
    handle2 = _BlockingHandle()
    started = _time.monotonic()
    with pytest.raises(FinalizationError) as final_error:
        hash_worktree_file(
            Path("blocked.txt"),
            MAX_SIZE,
            tick=clock2,
            open_fn=_Opener(handle2),
            budget=budget2,
            file_started_at=100.0,
            deadline=100.05,
            deadline_error=lambda: FinalizationError("FINAL_SNAPSHOT_TIMEOUT"),
            budget_error=lambda: FinalizationError("FINAL_HASH_TIMEOUT"),
        )
    elapsed = _time.monotonic() - started
    assert final_error.value.code == "FINAL_SNAPSHOT_TIMEOUT"
    assert elapsed < 5.0
    assert handle2.read_calls >= 1
    assert budget2.spent == 0.0
    handle2.close()


def test_worktree_read_oserror_propagates_for_charge() -> None:
    from crucible_core.infrastructure.git.content_hash import (
        HashBudget,
        hash_worktree_file,
    )

    class _FlakyHandle:
        def read(self, size: int = -1):
            raise OSError("broken read")

    class _Opener:
        def __call__(self, target, mode="rb", *args, **kwargs):
            return self._Handle()

        class _Handle:
            def __enter__(self):
                return _FlakyHandle()

            def __exit__(self, *exc) -> bool:
                return False

    clock = FakeClock(now=100.0)
    budget = HashBudget(aggregate_budget=10.0, per_file_budget=10.0)
    with pytest.raises(OSError):
        hash_worktree_file(
            Path("flaky.txt"),
            MAX_SIZE,
            tick=clock,
            open_fn=_Opener(),
            budget=budget,
            file_started_at=100.0,
            deadline=FAR_DEADLINE,
            deadline_error=lambda: AdmissionError("BASELINE_CAPTURE_TIMEOUT"),
            budget_error=lambda: AdmissionError("BASELINE_HASH_TIMEOUT"),
        )
    # hash_worktree_file never commits on failure; callers charge once.
    assert budget.spent == 0.0


def test_freeze_revalidates_deadline_after_lock_before_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sqlite3

    from fastapi.testclient import TestClient

    import crucible_core.services.finalizations as finalization_service
    from crucible_core.main import app

    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(
        "CRUCIBLE_TERMINAL_MAX_AUTH_WINDOW_SECONDS", "3000000000"
    )
    root = tmp_path / "repo"
    project_id = _coordinator_project(root)

    with TestClient(app) as client:
        admitted = client.post(
            "/v1/events", json=_coordinator_candidate(project_id, root)
        )
        assert admitted.status_code == 200, admitted.text
        task_id = admitted.json()["data"]["event"]["task_id"]
        event = _coordinator_completion(project_id, root, task_id)

        calls = {"count": 0}

        def _advancing_monotonic() -> float:
            calls["count"] += 1
            # deadline() + 2 pre-freeze checks stay valid; the in-freeze
            # revalidation after acquiring the lock sees the expiry.
            return 100.0 if calls["count"] <= 3 else 200.0

        def _fake_capture(*args, **kwargs):
            return FinalCaptureSnapshot(
                head="h",
                branch="b",
                status=b"",
                index=b"manifest",
                baseline_files=[],
                changes=[],
            )

        monkeypatch.setattr(
            finalization_service, "_capture_final", _fake_capture
        )
        monkeypatch.setattr(
            finalization_service, "_MONOTONIC", _advancing_monotonic
        )
        response = client.post("/v1/events", json=event)

    assert response.status_code == 400
    assert response.json()["data"]["code"] == "FINAL_SNAPSHOT_TIMEOUT"
    connection = sqlite3.connect(tmp_path / "data" / "crucible.db")
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_id,),
        ).fetchone()
        changes = connection.execute(
            "SELECT COUNT(*) FROM task_file_changes WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        baselines = connection.execute(
            "SELECT COUNT(*) FROM task_baseline_files WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert task == ("failed", "FINAL_SNAPSHOT_TIMEOUT", None)
    assert changes == 0
    assert baselines == 0


def test_freeze_expiry_inside_transaction_before_persist_rolls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sqlite3

    from fastapi.testclient import TestClient

    import crucible_core.application.finalizations as finalizations_app
    import crucible_core.services.finalizations as finalization_service
    from crucible_core.main import app

    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(
        "CRUCIBLE_TERMINAL_MAX_AUTH_WINDOW_SECONDS", "3000000000"
    )
    root = tmp_path / "repo"
    project_id = _coordinator_project(root)

    with TestClient(app) as client:
        admitted = client.post(
            "/v1/events", json=_coordinator_candidate(project_id, root)
        )
        assert admitted.status_code == 200, admitted.text
        task_id = admitted.json()["data"]["event"]["task_id"]
        event = _coordinator_completion(project_id, root, task_id)

        monotonic = FakeMonotonic(now=100.0)
        original = finalizations_app.final_repo.publication_is_current

        def _advance_during_lock(connection, task_id, tree_id, generation):
            monotonic.now = 200.0
            return original(connection, task_id, tree_id, generation)

        def _fake_capture(*args, **kwargs):
            return FinalCaptureSnapshot(
                head="h",
                branch="b",
                status=b"",
                index=b"manifest",
                baseline_files=[],
                changes=[],
            )

        monkeypatch.setattr(
            finalization_service, "_capture_final", _fake_capture
        )
        monkeypatch.setattr(finalization_service, "_MONOTONIC", monotonic)
        monkeypatch.setattr(
            finalizations_app.final_repo,
            "publication_is_current",
            _advance_during_lock,
        )
        response = client.post("/v1/events", json=event)

    assert response.status_code == 400
    assert response.json()["data"]["code"] == "FINAL_SNAPSHOT_TIMEOUT"
    connection = sqlite3.connect(tmp_path / "data" / "crucible.db")
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_id,),
        ).fetchone()
        changes = connection.execute(
            "SELECT COUNT(*) FROM task_file_changes WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert task == ("failed", "FINAL_SNAPSHOT_TIMEOUT", None)
    assert changes == 0


def test_blob_read_budget_bound_prefers_hash_timeout() -> None:
    import time as _time

    from crucible_core.infrastructure.git.content_hash import (
        HashBudget,
        hash_stream,
    )

    def _timeout_error() -> FinalizationError:
        return FinalizationError("FINAL_SNAPSHOT_TIMEOUT")

    def _budget_error() -> FinalizationError:
        return FinalizationError("FINAL_HASH_TIMEOUT")

    class _EofStream:
        def read(self, size: int = -1):
            return b""

    # Deterministic immediate expiry: aggregate already spent, so
    # the tightest bound is the budget even though the stream is
    # ready and the deadline is far.
    clock = FakeClock(now=100.0)
    budget = HashBudget(aggregate_budget=3.0, per_file_budget=2.0)
    budget.spent = 3.0
    with pytest.raises(FinalizationError) as immediate:
        hash_stream(
            _EofStream(),
            MAX_SIZE,
            tick=clock,
            budget=budget,
            file_started_at=100.0,
            deadline=FAR_DEADLINE,
            deadline_error=_timeout_error,
            budget_error=_budget_error,
            subprocess_timeout=2.0,
        )
    assert immediate.value.code == "FINAL_HASH_TIMEOUT"
    assert budget.spent == 3.0

    # Bounded blocking read: far deadline but tight per-file budget
    # must time out with the budget error, not the deadline error.
    clock2 = FakeClock(now=100.0)
    budget2 = HashBudget(aggregate_budget=10.0, per_file_budget=0.05)
    stream = BlockingStream()
    started = _time.monotonic()
    try:
        with pytest.raises(FinalizationError) as blocked:
            hash_stream(
                stream,
                MAX_SIZE,
                tick=clock2,
                budget=budget2,
                file_started_at=100.0,
                deadline=FAR_DEADLINE,
                deadline_error=_timeout_error,
                budget_error=_budget_error,
                subprocess_timeout=2.0,
            )
    finally:
        stream.close()
    elapsed = _time.monotonic() - started
    assert blocked.value.code == "FINAL_HASH_TIMEOUT"
    assert elapsed < 5.0
    assert budget2.spent == 0.0


def test_freeze_exact_deadline_is_expired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sqlite3

    from fastapi.testclient import TestClient

    import crucible_core.services.finalizations as finalization_service
    from crucible_core.main import app

    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(
        "CRUCIBLE_TERMINAL_MAX_AUTH_WINDOW_SECONDS", "3000000000"
    )
    root = tmp_path / "repo"
    project_id = _coordinator_project(root)

    with TestClient(app) as client:
        admitted = client.post(
            "/v1/events", json=_coordinator_candidate(project_id, root)
        )
        assert admitted.status_code == 200, admitted.text
        task_id = admitted.json()["data"]["event"]["task_id"]
        event = _coordinator_completion(project_id, root, task_id)

        calls = {"count": 0}

        def _exact_monotonic() -> float:
            calls["count"] += 1
            # deadline() + 2 pre-freeze checks stay at 100, so the
            # deadline is 105; every in-freeze read sees exactly the
            # limit and must be treated as expired (>=).
            return 100.0 if calls["count"] <= 3 else 105.0

        def _fake_capture(*args, **kwargs):
            return FinalCaptureSnapshot(
                head="h",
                branch="b",
                status=b"",
                index=b"manifest",
                baseline_files=[],
                changes=[],
            )

        monkeypatch.setattr(
            finalization_service, "_capture_final", _fake_capture
        )
        monkeypatch.setattr(
            finalization_service, "_MONOTONIC", _exact_monotonic
        )
        response = client.post("/v1/events", json=event)

    assert response.status_code == 400
    assert response.json()["data"]["code"] == "FINAL_SNAPSHOT_TIMEOUT"
    connection = sqlite3.connect(tmp_path / "data" / "crucible.db")
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_id,),
        ).fetchone()
        changes = connection.execute(
            "SELECT COUNT(*) FROM task_file_changes WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert task == ("failed", "FINAL_SNAPSHOT_TIMEOUT", None)
    assert changes == 0


def test_freeze_advancing_to_limit_after_inserts_rolls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sqlite3

    from fastapi.testclient import TestClient

    import crucible_core.application.finalizations as finalizations_app
    import crucible_core.services.finalizations as finalization_service
    from crucible_core.main import app
    from crucible_core.schemas.persistence import (
        BaselineFileRow,
        TaskFileChangeRow,
    )

    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(
        "CRUCIBLE_TERMINAL_MAX_AUTH_WINDOW_SECONDS", "3000000000"
    )
    root = tmp_path / "repo"
    project_id = _coordinator_project(root)

    with TestClient(app) as client:
        admitted = client.post(
            "/v1/events", json=_coordinator_candidate(project_id, root)
        )
        assert admitted.status_code == 200, admitted.text
        task_id = admitted.json()["data"]["event"]["task_id"]
        event = _coordinator_completion(project_id, root, task_id)

        monotonic = FakeMonotonic(now=100.0)
        original_insert = finalizations_app.final_repo.insert_baseline_file

        def _advance_on_insert(connection, task_id_arg, row):
            # Deadline is 100 + 5; advancing to exactly the limit
            # after the first persist must still expire (>=) and
            # roll back without publishing.
            monotonic.now = 105.0
            return original_insert(connection, task_id_arg, row)

        def _fake_capture(*args, **kwargs):
            return FinalCaptureSnapshot(
                head="h",
                branch="b",
                status=b"",
                index=b"manifest",
                baseline_files=[
                    BaselineFileRow(
                        path="extra.txt",
                        status="  ",
                        sha256="a" * 64,
                        size=1,
                        is_binary=0,
                        content=None,
                    )
                ],
                changes=[
                    TaskFileChangeRow(
                        path="tracked.txt",
                        operation="modified",
                        final_status=" M",
                        final_sha256="b" * 64,
                        final_size=1,
                        final_is_binary=0,
                        final_content=None,
                        evidence_status="hash_only",
                        evidence_reason="SNAPSHOT_SIZE_LIMIT",
                    )
                ],
            )

        monkeypatch.setattr(
            finalization_service, "_capture_final", _fake_capture
        )
        monkeypatch.setattr(finalization_service, "_MONOTONIC", monotonic)
        monkeypatch.setattr(
            finalizations_app.final_repo,
            "insert_baseline_file",
            _advance_on_insert,
        )
        response = client.post("/v1/events", json=event)

    assert response.status_code == 400
    assert response.json()["data"]["code"] == "FINAL_SNAPSHOT_TIMEOUT"
    connection = sqlite3.connect(tmp_path / "data" / "crucible.db")
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_id,),
        ).fetchone()
        changes = connection.execute(
            "SELECT COUNT(*) FROM task_file_changes WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        baselines = connection.execute(
            "SELECT COUNT(*) FROM task_baseline_files WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert task == ("failed", "FINAL_SNAPSHOT_TIMEOUT", None)
    assert changes == 0
    assert baselines == 0


def test_freeze_update_advancing_to_deadline_rolls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sqlite3

    from fastapi.testclient import TestClient

    import crucible_core.services.finalizations as finalization_service
    from crucible_core.main import app

    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(
        "CRUCIBLE_TERMINAL_MAX_AUTH_WINDOW_SECONDS", "3000000000"
    )
    root = tmp_path / "repo"
    project_id = _coordinator_project(root)

    with TestClient(app) as client:
        admitted = client.post(
            "/v1/events", json=_coordinator_candidate(project_id, root)
        )
        assert admitted.status_code == 200, admitted.text
        task_id = admitted.json()["data"]["event"]["task_id"]
        event = _coordinator_completion(project_id, root, task_id)

        calls = {"count": 0}

        def _advancing_after_update() -> float:
            calls["count"] += 1
            # Deadline is 100 + 5; the 9 reads up to the pre-UPDATE
            # check stay valid, the post-UPDATE read sees exactly
            # the limit and must expire (>=) without committing.
            return 100.0 if calls["count"] <= 9 else 105.0

        def _fake_capture(*args, **kwargs):
            return FinalCaptureSnapshot(
                head="h",
                branch="b",
                status=b"",
                index=b"manifest",
                baseline_files=[],
                changes=[],
            )

        monkeypatch.setattr(
            finalization_service, "_capture_final", _fake_capture
        )
        monkeypatch.setattr(
            finalization_service, "_MONOTONIC", _advancing_after_update
        )
        response = client.post("/v1/events", json=event)

    assert response.status_code == 400
    assert response.json()["data"]["code"] == "FINAL_SNAPSHOT_TIMEOUT"
    connection = sqlite3.connect(tmp_path / "data" / "crucible.db")
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_id,),
        ).fetchone()
        changes = connection.execute(
            "SELECT COUNT(*) FROM task_file_changes WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        baselines = connection.execute(
            "SELECT COUNT(*) FROM task_baseline_files WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert task == ("failed", "FINAL_SNAPSHOT_TIMEOUT", None)
    assert changes == 0
    assert baselines == 0


def test_hash_budget_exact_deadline_is_expired() -> None:
    from crucible_core.infrastructure.git.content_hash import HashBudget

    budget = HashBudget(aggregate_budget=10.0, per_file_budget=10.0)
    with pytest.raises(AdmissionError) as error:
        budget.check(
            now=100.0,
            file_started_at=100.0,
            deadline=100.0,
            deadline_error=lambda: AdmissionError("BASELINE_CAPTURE_TIMEOUT"),
            budget_error=lambda: AdmissionError("BASELINE_HASH_TIMEOUT"),
        )
    assert error.value.code == "BASELINE_CAPTURE_TIMEOUT"


def test_blob_wait_consumes_hash_budget() -> None:
    from crucible_core.infrastructure.git.content_hash import HashBudget

    clock = FakeClock(now=100.0)
    budget = HashBudget(aggregate_budget=10.0, per_file_budget=0.5)
    seen: dict[str, float] = {}

    class _EofStream:
        def read(self, size: int = -1):
            return b""

        def close(self) -> None:
            return None

    class _SlowWaitProc:
        def __init__(self) -> None:
            self.stdout: object = _EofStream()
            self.returncode: int | None = None

        def poll(self):
            return self.returncode

        def kill(self) -> None:
            return None

        def wait(self, timeout=None):
            # The fail-safe cleanup in ``finally`` also calls
            # wait(timeout=1); only record the evidence wait, which
            # runs before returncode is set.
            if self.returncode is None:
                seen["timeout"] = float(timeout)
                clock.now = 100.6
                self.returncode = 0
            return self.returncode

    import crucible_core.infrastructure.git.final_capture as fc

    real_popen = fc.subprocess.Popen
    fc.subprocess.Popen = lambda *a, **k: _SlowWaitProc()  # type: ignore
    try:
        with pytest.raises(FinalizationError) as error:
            fc._stream_git_blob_to_row(
                Path("repo"),
                "deadbeef",
                "a.txt",
                "  ",
                MAX_SIZE,
                FAR_DEADLINE,
                clock,
                budget,
            )
    finally:
        fc.subprocess.Popen = real_popen
    assert error.value.code == "FINAL_HASH_TIMEOUT"
    assert seen["timeout"] == pytest.approx(0.5)
    assert budget.spent == pytest.approx(0.0)


def test_blob_wait_success_with_failing_git_is_unavailable() -> None:
    from crucible_core.infrastructure.git.content_hash import HashBudget

    clock = FakeClock(now=100.0)
    budget = HashBudget(aggregate_budget=10.0, per_file_budget=10.0)

    class _EofStream:
        def read(self, size: int = -1):
            return b""

        def close(self) -> None:
            return None

    class _FailWaitProc:
        def __init__(self) -> None:
            self.stdout: object = _EofStream()
            self.returncode: int | None = None

        def poll(self):
            return self.returncode

        def kill(self) -> None:
            return None

        def wait(self, timeout=None):
            self.returncode = 1
            return 1

    import crucible_core.infrastructure.git.final_capture as fc

    real_popen = fc.subprocess.Popen
    fc.subprocess.Popen = lambda *a, **k: _FailWaitProc()  # type: ignore
    try:
        with pytest.raises(FinalizationError) as error:
            fc._stream_git_blob_to_row(
                Path("repo"),
                "deadbeef",
                "a.txt",
                "  ",
                MAX_SIZE,
                FAR_DEADLINE,
                clock,
                budget,
            )
    finally:
        fc.subprocess.Popen = real_popen
    assert error.value.code == "BASELINE_OBJECT_UNAVAILABLE"


def test_freeze_lock_waits_within_deadline_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sqlite3
    import time as _time

    from fastapi.testclient import TestClient

    import crucible_core.services.finalizations as finalization_service
    from crucible_core.main import app

    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(
        "CRUCIBLE_TERMINAL_MAX_AUTH_WINDOW_SECONDS", "3000000000"
    )
    root = tmp_path / "repo"
    project_id = _coordinator_project(root)

    with TestClient(app) as client:
        admitted = client.post(
            "/v1/events", json=_coordinator_candidate(project_id, root)
        )
        assert admitted.status_code == 200, admitted.text
        task_id = admitted.json()["data"]["event"]["task_id"]
        event = _coordinator_completion(project_id, root, task_id)

        calls = {"count": 0}

        def _lock_aware_monotonic() -> float:
            calls["count"] += 1
            if calls["count"] <= 3:
                return 100.0
            if calls["count"] == 4:
                return 104.9
            return 105.0

        monkeypatch.setattr(
            finalization_service, "_MONOTONIC", _lock_aware_monotonic
        )
        db_path = tmp_path / "data" / "crucible.db"

        def _fake_capture(*args, **kwargs):
            return FinalCaptureSnapshot(
                head="h",
                branch="b",
                status=b"",
                index=b"manifest",
                baseline_files=[],
                changes=[],
            )

        monkeypatch.setattr(
            finalization_service, "_capture_final", _fake_capture
        )

        # Hold the write lock on a dedicated thread so only
        # _freeze's BEGIN IMMEDIATE contends (_begin already
        # committed). The hold outlives the ~100ms deadline-aware
        # busy_timeout but is far shorter than sqlite's 5s default,
        # so only deadline-aware acquisition fails fast with
        # FINAL_SNAPSHOT_TIMEOUT; _fail then records once released.
        import threading

        acquired = threading.Event()

        def _hold_write_lock() -> None:
            holder = sqlite3.connect(db_path, timeout=5.0)
            try:
                holder.execute("BEGIN IMMEDIATE")
                holder.execute(
                    "UPDATE tasks SET status = status WHERE id = ?",
                    (task_id,),
                )
                acquired.set()
                _time.sleep(1.5)
                holder.rollback()
            finally:
                holder.close()

        holder_thread = threading.Thread(target=_hold_write_lock, daemon=True)
        holder_thread.start()
        assert acquired.wait(timeout=5.0)
        started = _time.monotonic()
        response = client.post("/v1/events", json=event)
        elapsed = _time.monotonic() - started
        holder_thread.join(timeout=5.0)

    assert response.status_code == 400
    assert response.json()["data"]["code"] == "FINAL_SNAPSHOT_TIMEOUT"
    assert elapsed < 3.0
    connection = sqlite3.connect(db_path)
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_id,),
        ).fetchone()
        changes = connection.execute(
            "SELECT COUNT(*) FROM task_file_changes WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        baselines = connection.execute(
            "SELECT COUNT(*) FROM task_baseline_files WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert task == ("failed", "FINAL_SNAPSHOT_TIMEOUT", None)
    assert changes == 0
    assert baselines == 0


def test_freeze_sub_ms_remainder_rounds_busy_timeout_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sqlite3

    import crucible_core.application.finalizations as finalizations_app
    from crucible_core.core.errors import FinalizationError

    seen: dict[str, int] = {}
    reads = {"count": 0}

    def _scripted_monotonic() -> float:
        reads["count"] += 1
        # 0.4ms of positive remainder at lock acquisition, then
        # exactly the deadline once the attempt contends.
        return 99.9996 if reads["count"] == 1 else 100.0

    class _ContendedConnection:
        def execute(self, sql, *args):
            if sql.startswith("PRAGMA busy_timeout"):
                seen["busy_ms"] = int(sql.rsplit("=", 1)[1])
                return None
            raise sqlite3.OperationalError("database is locked")

        def rollback(self) -> None:
            return None

    class _FakeConnect:
        def __init__(self, *args, **kwargs) -> None:
            self.connection = _ContendedConnection()

        def __enter__(self):
            return self.connection

        def __exit__(self, *exc) -> bool:
            return False

    monkeypatch.setattr(finalizations_app, "connect", _FakeConnect)
    coordinator = finalizations_app.FinalizationCoordinator(
        tmp_path / "crucible.db",
        capture_final=lambda *a, **k: FinalCaptureSnapshot(
            head="h",
            branch="b",
            status=b"",
            index=b"",
            baseline_files=[],
            changes=[],
        ),
        monotonic=_scripted_monotonic,
    )
    with pytest.raises(FinalizationError) as error:
        coordinator._freeze(
            "task-1",
            "tree-1",
            1,
            FinalCaptureSnapshot(
                head="h",
                branch="b",
                status=b"",
                index=b"",
                baseline_files=[],
                changes=[],
            ),
            100.0,
        )
    assert error.value.code == "FINAL_SNAPSHOT_TIMEOUT"
    assert seen["busy_ms"] == 1


def _coordinator_project(root: Path) -> str:
    import json
    import uuid

    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "T"], check=True
    )
    project_id = str(uuid.uuid4())
    directory = root / ".crucible"
    directory.mkdir()
    (directory / "project.json").write_text(
        json.dumps({"project_id": project_id}), encoding="utf-8"
    )
    (root / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--quiet", "-m", "init"],
        check=True,
    )
    return project_id


def _coordinator_candidate(project_id: str, root: Path) -> dict[str, object]:
    import uuid

    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "input_candidate",
        "occurred_at": "2026-09-07T00:00:00Z",
        "payload_version": 1,
        "adapter": "opencode-v1",
        "adapter_version": "0.1.0",
        "agent_session_id": "session-1",
        "input_id": "input-1",
        "execution_id": "execution-1",
        "project_id": project_id,
        "git_root": str(root),
        "workspace_path": str(root),
        "payload": {"delivery": "new"},
    }


def _coordinator_completion(
    project_id: str, root: Path, task_id: str
) -> dict[str, object]:
    import uuid

    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "task_completed",
        "occurred_at": "2026-09-07T00:01:00Z",
        "payload_version": 1,
        "adapter": "opencode-v1",
        "adapter_version": "0.1.0",
        "agent_session_id": "session-1",
        "input_id": "input-1",
        "execution_id": "execution-1",
        "project_id": project_id,
        "git_root": str(root),
        "workspace_path": str(root),
        "payload": {
            "task_id": task_id,
            "terminal_signal": "session_prompt_return",
            "terminal_outcome": "stop",
            "compatibility_profile": (
                "opencode-v1-1.18.28-write-stop-restricted"
            ),
            "terminal_observed_at": "2026-09-07T00:00:30Z",
            "capture_not_after": "2099-01-01T00:00:00Z",
        },
    }
