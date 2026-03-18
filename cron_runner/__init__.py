"""CronJob harness for AIQ services.

Wires logging, observability, alerting, and heartbeat for scheduled jobs.

Example::

    from datetime import timedelta
    from libs.cron_runner import CronJob
    from libs.observability import observability
    from libs.alerting.alerting import AlertManager, RunSummary

    def my_work() -> RunSummary:
        # ... do work ...
        return {"generated": 10, "inserted": 8, "errors": 2}

    job = CronJob(
        name="question-generation",
        schedule=timedelta(hours=1),
        work_fn=my_work,
        observability=observability,
    )

    # Called by external scheduler (Railway, cron, EventBridge)
    exit_code = job.run_once()
"""

from .cron_job import CronJob

__all__ = ["CronJob"]
