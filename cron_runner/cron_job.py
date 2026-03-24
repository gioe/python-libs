"""CronJob harness: thin wrapper that wires cross-cutting concerns for scheduled work.

This module has no dependencies on any external service package — it can be
imported by any service that sets PYTHONPATH to include the repo root.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

try:
    from ..alerting.alerting import AlertManager, RunSummary
    from ..structured_logging.logging_config import setup_logging
    from ..observability.facade import ObservabilityFacade
except ImportError:
    from alerting.alerting import AlertManager, RunSummary  # type: ignore[no-redef]
    from structured_logging.logging_config import setup_logging  # type: ignore[no-redef]
    from observability.facade import ObservabilityFacade  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


def _run_summary_to_fields(run_summary: RunSummary) -> List[Tuple[str, Any]]:
    """Convert a run summary dict to (label, value) tuples for send_notification.

    Top-level scalar values are included first (with duration_seconds formatted
    as "Xs"), followed by any key/value pairs found in the optional ``details``
    sub-dict.  The ``details`` key itself is not included as a separate entry.
    """
    fields: List[Tuple[str, Any]] = []
    for key, value in run_summary.items():
        if key == "details" or value is None:
            continue
        label = key.replace("_", " ").title()
        if key == "duration_seconds" and isinstance(value, (int, float)):
            value = f"{float(value):.1f}s"
        fields.append((label, value))

    details = run_summary.get("details") or {}
    for key, value in details.items():
        if value is None:
            continue
        label = key.replace("_", " ").title()
        fields.append((label, value))

    return fields


class CronJob:
    """Thin harness for scheduled jobs that wires logging, observability, alerting,
    and heartbeat into a single reusable interface.

    Usage with an external scheduler (Railway, cron, EventBridge)::

        job = CronJob(
            name="question-generation",
            schedule="0 * * * *",  # cron expression — for documentation
            work_fn=run_generation,
            observability=observability,
            alert_manager=alert_manager,
            heartbeat_path="./logs/heartbeat.json",
        )
        exit_code = job.run_once()  # called by the external scheduler

    Usage with embedded scheduling (timedelta only)::

        job = CronJob(
            name="question-generation",
            schedule=timedelta(hours=1),
            work_fn=run_generation,
            observability=observability,
        )
        job.run_loop()  # blocks indefinitely
    """

    def __init__(
        self,
        name: str,
        schedule: Union[str, timedelta],
        work_fn: Callable[[], RunSummary],
        observability: ObservabilityFacade,
        alert_manager: Optional[AlertManager] = None,
        heartbeat_path: Optional[str] = None,
    ) -> None:
        """Initialise the CronJob harness.

        Args:
            name: Human-readable job name used in logs, metrics, and heartbeats.
            schedule: Cron expression string (for external schedulers) or
                timedelta (for run_loop() with the 'schedule' library).
            work_fn: Callable that performs the actual job work. Must return a
                RunSummary dict (see libs.alerting.alerting.RunSummary). All
                keys are optional — return {} if no summary is available.
            observability: Initialised ObservabilityFacade for error capture and
                metric recording.
            alert_manager: Optional AlertManager for run completion emails.
                Pass None to disable alerting.
            heartbeat_path: Path to the heartbeat JSON file. Defaults to
                ``./logs/heartbeat.json``.
        """
        self.name = name
        self.job_schedule = schedule
        self.work_fn = work_fn
        self.observability = observability
        self.alert_manager = alert_manager
        self.heartbeat_path = heartbeat_path or "./logs/heartbeat.json"
        self._logging_configured = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_heartbeat(
        self,
        status: str,
        exit_code: Optional[int] = None,
        error_message: Optional[str] = None,
        stats: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Write heartbeat to file and stdout for platform visibility.

        Args:
            status: One of "started", "completed", or "failed".
            exit_code: Job exit code (0 = success).
            error_message: Error message if the job failed.
            stats: Run statistics to include in the heartbeat (success path).
        """
        heartbeat_file = Path(self.heartbeat_path)
        heartbeat_file.parent.mkdir(parents=True, exist_ok=True)

        data: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "job": self.name,
            "status": status,
            "hostname": os.uname().nodename if hasattr(os, "uname") else "unknown",
        }
        if exit_code is not None:
            data["exit_code"] = exit_code
        if error_message:
            data["error_message"] = error_message
        if stats:
            data["stats"] = stats

        with open(heartbeat_file, "w") as f:
            json.dump(data, f, indent=2)

        # Also log to stdout so Railway/cloud platforms capture it in logs
        print(f"HEARTBEAT: {json.dumps(data)}", flush=True)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run_once(self) -> int:
        """Execute the job once and return an exit code.

        Designed for external schedulers (Railway, cron, EventBridge). Returns
        immediately after the job finishes — never blocks.

        Steps (in order):
        1. Sets up logging via setup_logging().
        2. Writes a "started" heartbeat.
        3. Calls work_fn() and captures the returned RunSummary.
        4. On exception: calls observability.capture_error() and records the
           failure in the run summary.
        5. Records job.duration (histogram) and job.exit_code (gauge) metrics.
        6. Calls alert_manager.send_notification() if an alert_manager is set.
        7. Writes a "completed" or "failed" heartbeat.

        Returns:
            0 on success, 1 if work_fn raised an exception.
        """
        if not self._logging_configured:
            setup_logging()
            self._logging_configured = True

        try:
            self._write_heartbeat(status="started")
        except Exception as exc:
            logger.warning("Failed to write started heartbeat: %s", exc)

        start_time = time.monotonic()
        run_summary: RunSummary = {}
        exit_code = 0

        try:
            run_summary = self.work_fn() or {}
        except Exception as exc:
            exit_code = 1
            error_message = str(exc)
            logger.error("Job '%s' failed: %s", self.name, exc, exc_info=True)
            self.observability.capture_error(exc, context={"job": self.name})
            partial_summary = getattr(exc, "run_summary", {})
            run_summary = {**partial_summary, "error_message": error_message}
        finally:
            duration = time.monotonic() - start_time

            self.observability.record_metric(
                "job.duration",
                value=duration,
                labels={"job": self.name},
                metric_type="histogram",
                unit="s",
            )
            self.observability.record_metric(
                "job.exit_code",
                value=float(exit_code),
                labels={"job": self.name},
                metric_type="gauge",
            )

            if self.alert_manager is not None:
                alert_summary = {**run_summary, "duration_seconds": duration}
                if exit_code == 0:
                    notif_title = f"\u2705 {self.name}: Success"
                    notif_severity = "info"
                else:
                    notif_title = f"\u274c {self.name}: Failed (exit {exit_code})"
                    notif_severity = "critical"
                self.alert_manager.send_notification(
                    title=notif_title,
                    fields=_run_summary_to_fields(alert_summary),
                    severity=notif_severity,
                )

            heartbeat_status = "completed" if exit_code == 0 else "failed"
            self._write_heartbeat(
                status=heartbeat_status,
                exit_code=exit_code,
                error_message=run_summary.get("error_message"),
                stats=run_summary,
            )

        return exit_code

    def run_loop(self) -> None:
        """Run the job repeatedly on the configured schedule.

        Blocks indefinitely. Suitable for embedded schedulers. Uses the
        ``schedule`` library for interval management.

        Requires job_schedule to be a timedelta. Cron expression strings are
        not supported for embedded scheduling — use run_once() with an
        external scheduler instead.

        Raises:
            ValueError: If job_schedule is a cron expression string rather
                than a timedelta.
            ImportError: If the ``schedule`` library is not installed.
        """
        if not isinstance(self.job_schedule, timedelta):
            raise ValueError(
                f"run_loop() requires a timedelta schedule; "
                f"cron expressions ({self.job_schedule!r}) are for external "
                "schedulers only. Use run_once() instead, or pass a timedelta."
            )

        try:
            import schedule as schedule_lib
        except ImportError as exc:
            raise ImportError(
                "The 'schedule' library is required for run_loop(). "
                "Install it with: pip install schedule"
            ) from exc

        interval_seconds = self.job_schedule.total_seconds()

        # Use an isolated Scheduler instance (not the global default) so that
        # multiple CronJob instances in the same process do not share state.
        scheduler = schedule_lib.Scheduler()
        scheduler.every(interval_seconds).seconds.do(self.run_once)

        logger.info(
            "Job '%s' starting loop — interval %.0fs", self.name, interval_seconds
        )
        while True:
            scheduler.run_pending()
            time.sleep(1)
