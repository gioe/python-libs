"""Unit tests for CronJob harness (happy path and failure path)."""

import json
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cron_runner.cron_job import CronJob


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_job(
    tmp_path: Path,
    work_fn=None,
    alert_manager=None,
) -> CronJob:
    """Build a CronJob wired to mock observability and a temp heartbeat file."""
    obs = MagicMock()
    obs.capture_error = MagicMock()
    obs.record_metric = MagicMock()

    heartbeat_path = str(tmp_path / "heartbeat.json")

    return CronJob(
        name="test-job",
        schedule=timedelta(minutes=5),
        work_fn=work_fn or (lambda: {"generated": 1, "inserted": 1, "errors": 0}),
        observability=obs,
        alert_manager=alert_manager,
        heartbeat_path=heartbeat_path,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_run_once_happy_path_returns_zero(tmp_path: Path) -> None:
    """run_once() returns 0 when work_fn succeeds."""
    with patch("cron_runner.cron_job.setup_logging"):
        job = _make_job(tmp_path)
        assert job.run_once() == 0


def test_run_once_heartbeat_started_then_completed(tmp_path: Path) -> None:
    """Heartbeat file records 'started' before work_fn and 'completed' after."""
    heartbeat_path = tmp_path / "heartbeat.json"
    started_statuses = []

    original_work = lambda: {"generated": 1, "inserted": 1, "errors": 0}

    def capturing_work():
        # Read heartbeat inside work_fn — it should be 'started' at this point
        data = json.loads(heartbeat_path.read_text())
        started_statuses.append(data["status"])
        return original_work()

    with patch("cron_runner.cron_job.setup_logging"):
        obs = MagicMock()
        job = CronJob(
            name="test-job",
            schedule=timedelta(minutes=5),
            work_fn=capturing_work,
            observability=obs,
            heartbeat_path=str(heartbeat_path),
        )
        job.run_once()

    assert started_statuses == ["started"], "Heartbeat should be 'started' before work_fn runs"

    data = json.loads(heartbeat_path.read_text())
    assert data["status"] == "completed"
    assert data["exit_code"] == 0
    assert data["job"] == "test-job"
    assert "stats" in data


def test_run_once_happy_path_metrics_recorded(tmp_path: Path) -> None:
    """job.duration and job.exit_code metrics are recorded on success."""
    with patch("cron_runner.cron_job.setup_logging"):
        job = _make_job(tmp_path)
        job.run_once()

    calls = {
        call.args[0]: call for call in job.observability.record_metric.call_args_list
    }
    assert "job.duration" in calls
    assert "job.exit_code" in calls

    duration_call = calls["job.duration"]
    assert duration_call.kwargs["metric_type"] == "histogram"
    assert duration_call.kwargs["labels"] == {"job": "test-job"}

    exit_code_call = calls["job.exit_code"]
    assert exit_code_call.kwargs["value"] == 0.0  # exit_code value


def test_run_once_happy_path_alert_sent(tmp_path: Path) -> None:
    """alert_manager.send_notification() is called on success."""
    alert_manager = MagicMock()
    with patch("cron_runner.cron_job.setup_logging"):
        job = _make_job(tmp_path, alert_manager=alert_manager)
        job.run_once()

    alert_manager.send_notification.assert_called_once()
    _, kwargs = alert_manager.send_notification.call_args
    assert kwargs["severity"] == "info"
    assert "Success" in kwargs["title"]


def test_run_once_happy_path_no_alert_when_not_configured(tmp_path: Path) -> None:
    """No alert is sent when alert_manager is None."""
    with patch("cron_runner.cron_job.setup_logging"):
        job = _make_job(tmp_path, alert_manager=None)
        job.run_once()  # should not raise


def test_run_once_captures_run_summary_in_alert(tmp_path: Path) -> None:
    """run_summary returned by work_fn is converted to fields for send_notification."""
    summary = {"generated": 42, "inserted": 40, "errors": 2}
    alert_manager = MagicMock()
    with patch("cron_runner.cron_job.setup_logging"):
        job = _make_job(
            tmp_path,
            work_fn=lambda: summary,
            alert_manager=alert_manager,
        )
        job.run_once()

    _, kwargs = alert_manager.send_notification.call_args
    field_labels = [label for label, _ in kwargs["fields"]]
    assert "Generated" in field_labels
    assert "Inserted" in field_labels
    assert "Duration Seconds" in field_labels


def test_run_once_heartbeat_stats_excludes_duration_seconds(tmp_path: Path) -> None:
    """Heartbeat stats must not include duration_seconds injected for alert_manager."""
    alert_manager = MagicMock()
    with patch("cron_runner.cron_job.setup_logging"):
        job = _make_job(
            tmp_path,
            work_fn=lambda: {"generated": 5},
            alert_manager=alert_manager,
        )
        job.run_once()

    data = json.loads(Path(job.heartbeat_path).read_text())
    assert "stats" in data
    assert "duration_seconds" not in data["stats"]


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


def test_run_once_failure_path_returns_nonzero(tmp_path: Path) -> None:
    """run_once() returns 1 when work_fn raises an exception."""

    def failing_fn():
        raise RuntimeError("boom")

    with patch("cron_runner.cron_job.setup_logging"):
        job = _make_job(tmp_path, work_fn=failing_fn)
        assert job.run_once() == 1


def test_run_once_failure_path_heartbeat_failed(tmp_path: Path) -> None:
    """Heartbeat file records 'failed' status when work_fn raises."""

    def failing_fn():
        raise RuntimeError("something broke")

    with patch("cron_runner.cron_job.setup_logging"):
        job = _make_job(tmp_path, work_fn=failing_fn)
        job.run_once()

    data = json.loads(Path(job.heartbeat_path).read_text())
    assert data["status"] == "failed"
    assert data["exit_code"] == 1
    assert data["error_message"] == "something broke"
    # stats are now included on failure (contains at least error_message)
    assert "stats" in data


def test_run_once_failure_path_heartbeat_includes_partial_stats(tmp_path: Path) -> None:
    """Heartbeat stats include partial run_summary attached to the exception."""

    def failing_fn():
        err = RuntimeError("partial failure")
        err.run_summary = {"questions_inserted": 3, "approval_rate": 75.0}  # type: ignore[attr-defined]
        raise err

    with patch("cron_runner.cron_job.setup_logging"):
        job = _make_job(tmp_path, work_fn=failing_fn)
        job.run_once()

    data = json.loads(Path(job.heartbeat_path).read_text())
    assert data["status"] == "failed"
    assert data["exit_code"] == 1
    assert data["error_message"] == "partial failure"
    assert data["stats"]["questions_inserted"] == 3
    assert data["stats"]["approval_rate"] == 75.0
    assert data["stats"]["error_message"] == "partial failure"


def test_run_once_failure_path_capture_error_called(tmp_path: Path) -> None:
    """observability.capture_error() is called with the exception on failure."""
    exc = ValueError("test error")

    def failing_fn():
        raise exc

    with patch("cron_runner.cron_job.setup_logging"):
        job = _make_job(tmp_path, work_fn=failing_fn)
        job.run_once()

    job.observability.capture_error.assert_called_once()
    call_args = job.observability.capture_error.call_args
    assert call_args.args[0] is exc
    assert call_args.kwargs["context"]["job"] == "test-job"


def test_run_once_failure_path_metrics_still_recorded(tmp_path: Path) -> None:
    """Metrics are recorded even when work_fn raises."""

    def failing_fn():
        raise RuntimeError("oops")

    with patch("cron_runner.cron_job.setup_logging"):
        job = _make_job(tmp_path, work_fn=failing_fn)
        job.run_once()

    metric_names = [
        call.args[0] for call in job.observability.record_metric.call_args_list
    ]
    assert "job.duration" in metric_names
    assert "job.exit_code" in metric_names


def test_run_once_failure_path_alert_sent_with_error(tmp_path: Path) -> None:
    """send_notification is called with severity='critical' when work_fn raises."""

    def failing_fn():
        raise RuntimeError("critical failure")

    alert_manager = MagicMock()
    with patch("cron_runner.cron_job.setup_logging"):
        job = _make_job(tmp_path, work_fn=failing_fn, alert_manager=alert_manager)
        job.run_once()

    alert_manager.send_notification.assert_called_once()
    _, kwargs = alert_manager.send_notification.call_args
    assert kwargs["severity"] == "critical"
    assert "Failed" in kwargs["title"]

    field_map = dict(kwargs["fields"])
    assert field_map.get("Error Message") == "critical failure"


# ---------------------------------------------------------------------------
# run_once() non-blocking
# ---------------------------------------------------------------------------


def test_run_once_does_not_block(tmp_path: Path) -> None:
    """run_once() returns before a long timeout (i.e. it is non-blocking)."""

    def fast_work():
        return {}

    start = time.monotonic()
    with patch("cron_runner.cron_job.setup_logging"):
        job = _make_job(tmp_path, work_fn=fast_work)
        job.run_once()

    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"run_once() took too long: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# run_loop() schedule requirements
# ---------------------------------------------------------------------------


def test_run_loop_raises_for_cron_string(tmp_path: Path) -> None:
    """run_loop() raises ValueError when schedule is a cron expression string."""
    obs = MagicMock()
    job = CronJob(
        name="test-job",
        schedule="0 * * * *",
        work_fn=lambda: {},
        observability=obs,
        heartbeat_path=str(tmp_path / "heartbeat.json"),
    )
    with pytest.raises(ValueError, match="timedelta"):
        job.run_loop()


def test_run_loop_raises_import_error_without_schedule_lib(tmp_path: Path) -> None:
    """run_loop() raises ImportError if the 'schedule' library is not installed."""
    obs = MagicMock()
    job = CronJob(
        name="test-job",
        schedule=timedelta(minutes=5),
        work_fn=lambda: {},
        observability=obs,
        heartbeat_path=str(tmp_path / "heartbeat.json"),
    )
    with patch.dict("sys.modules", {"schedule": None}):
        with pytest.raises(ImportError, match="schedule"):
            job.run_loop()


def test_run_loop_schedules_and_runs(tmp_path: Path) -> None:
    """run_loop() uses an isolated Scheduler, registers run_once as the callback."""
    obs = MagicMock()
    job = CronJob(
        name="test-job",
        schedule=timedelta(seconds=1),
        work_fn=lambda: {},
        observability=obs,
        heartbeat_path=str(tmp_path / "heartbeat.json"),
    )

    # Build a mock scheduler returned by schedule_lib.Scheduler()
    mock_scheduler = MagicMock()
    mock_every = MagicMock()
    mock_scheduler.every.return_value = mock_every
    mock_every.seconds = mock_every

    # Break the infinite loop after one run_pending call
    call_count = 0

    def fake_run_pending():
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise StopIteration

    mock_scheduler.run_pending = fake_run_pending

    mock_schedule_lib = MagicMock()
    mock_schedule_lib.Scheduler.return_value = mock_scheduler

    with patch("cron_runner.cron_job.setup_logging"):
        with patch.dict("sys.modules", {"schedule": mock_schedule_lib}):
            with pytest.raises(StopIteration):
                job.run_loop()

    # Isolated scheduler was created (not the global default)
    mock_schedule_lib.Scheduler.assert_called_once()
    # run_once registered as the callback with the correct interval
    mock_scheduler.every.assert_called_once_with(1.0)
    mock_every.do.assert_called_once_with(job.run_once)


# ---------------------------------------------------------------------------
# No imports from question-service or backend
# ---------------------------------------------------------------------------


def test_no_service_imports() -> None:
    """libs/cron_runner has no imports from question-service or backend packages."""
    import importlib
    import sys

    # Reload the module to inspect its imports
    if "cron_runner.cron_job" in sys.modules:
        del sys.modules["cron_runner.cron_job"]

    import cron_runner.cron_job as mod

    source_file = mod.__file__
    assert source_file is not None

    with open(source_file) as f:
        source = f.read()

    forbidden = [
        "from question",
        "import question",
        "from backend",
        "import backend",
        "from app.",
        "import app.",
    ]
    for pattern in forbidden:
        assert pattern not in source, (
            f"Found forbidden import pattern {pattern!r} in libs/cron_runner/cron_job.py"
        )
