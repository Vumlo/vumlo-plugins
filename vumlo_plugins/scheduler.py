"""Time-based work (APScheduler). Jobs start in the FastAPI lifespan, are idempotent (use dedupe keys and `State`),
and can be run once on demand through the plugin's `POST /jobs/{name}` route, so nobody waits for a clock."""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .logs import Log

JOB_NAME = re.compile(r"[a-z][a-z0-9_-]{0,39}")
JobFn = Callable[[], Awaitable[dict[str, Any] | None]]


@dataclass
class Job:
    name: str
    fn: JobFn
    cron: str | None
    every: float | None


class Scheduler:
    def __init__(self, log: Log, timezone: str | None = None):
        self._log = log
        self._timezone = timezone or os.getenv("VUMLO_TZ", "UTC")
        self.jobs: dict[str, Job] = {}
        self._scheduler: AsyncIOScheduler | None = None

    def add(self, name: str, fn: JobFn, *, cron: str | None = None, every: float | None = None) -> None:
        """`cron` is a five-field crontab line in the plugin's timezone (`VUMLO_TZ`, default UTC); `every` is seconds.
        With neither, the job only runs on demand."""
        if not JOB_NAME.fullmatch(name):
            raise ValueError("a job name is a slug: lowercase letters, digits, `_` and `-`")
        if name in self.jobs:
            raise ValueError(f"job {name} is declared twice")
        if cron and every:
            raise ValueError("a job runs on a cron line or every so many seconds, not both")
        if cron:
            CronTrigger.from_crontab(cron, timezone=self._timezone)  # fail at import, not at 06:00
        self.jobs[name] = Job(name, fn, cron, every)

    async def run(self, name: str) -> dict[str, Any]:
        """Runs a job once now. The result is `{"job", "ok", ...what the job returned}`; an exception is logged and
        reported as `ok: false` with its type."""
        job = self.jobs[name]
        try:
            result = await job.fn()
        except Exception as error:
            self._log.exception("job failed", job=name)
            return {"job": name, "ok": False, "error": type(error).__name__}
        self._log.info("job done", job=name)
        return {"job": name, "ok": True, **(result or {})}

    def start(self) -> None:
        if self._scheduler is not None or not any(j.cron or j.every for j in self.jobs.values()):
            return
        scheduler = AsyncIOScheduler(timezone=self._timezone)
        for job in self.jobs.values():
            if job.cron:
                trigger = CronTrigger.from_crontab(job.cron, timezone=self._timezone)
            elif job.every:
                trigger = IntervalTrigger(seconds=job.every, timezone=self._timezone)
            else:
                continue
            scheduler.add_job(self.run, trigger, args=[job.name], id=job.name, max_instances=1, coalesce=True,
                              misfire_grace_time=300)
        scheduler.start()
        self._scheduler = scheduler

    def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
