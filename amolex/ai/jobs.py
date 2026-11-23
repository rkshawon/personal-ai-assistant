"""Hand big tasks to other AIs (Gemini, Claude, GPT...) and report back when they finish.

The local brain never does the heavy work itself. It starts the delegate's official
CLI in the background, keeps listening to you meanwhile, saves the full result to
E:\\Amolex\\jobs\\<id>.md, and announces "Task finished" when the process exits.
"""

from __future__ import annotations

import datetime as dt
import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import paths
from ..config import DelegateCfg, JobsCfg

log = logging.getLogger(__name__)

Notify = Callable[[str], None]


def build_argv(cfg: DelegateCfg, task: str) -> list[str]:
    """Fill "{task}" into the command; with stdin delivery the task is not put on the command line."""
    if not cfg.command:
        raise ValueError("delegate has no command")
    if cfg.stdin:
        return [part for part in cfg.command if part != "{task}"]
    return [part.replace("{task}", task) for part in cfg.command]


def short_title(task: str, words: int = 8) -> str:
    parts = task.split()
    return " ".join(parts[:words]) + ("..." if len(parts) > words else "")


@dataclass
class Job:
    id: str
    delegate: str
    task: str
    started: float = field(default_factory=time.time)
    status: str = "running"  # running | done | failed | timed out
    result_file: Path | None = None
    summary: str = ""


class JobManager:
    def __init__(self, delegates: dict[str, DelegateCfg], cfg: JobsCfg, notify: Notify) -> None:
        self.delegates = delegates
        self.cfg = cfg
        self.notify = notify
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(cfg.max_parallel)
        self._counter = 0

    def available(self) -> dict[str, DelegateCfg]:
        return {name: d for name, d in self.delegates.items() if d.enabled and d.command}

    def start(self, delegate: str, task: str) -> str:
        name = delegate.lower().strip()
        d = self.available().get(name)
        if d is None:
            names = ", ".join(self.available()) or "none set up yet"
            return f"No delegate called '{delegate}'. Available: {names}."
        if shutil.which(d.command[0]) is None and not Path(paths.expand(d.command[0])).exists():
            return f"The {name} command '{d.command[0]}' isn't installed or isn't on PATH."
        with self._lock:
            self._counter += 1
            job = Job(id=f"{dt.datetime.now():%Y%m%d-%H%M%S}-{self._counter}", delegate=name, task=task)
            self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job, d), name=f"job-{job.id}", daemon=True).start()
        return f"Sent to {name}. I'll tell you when it's done."

    def _run(self, job: Job, d: DelegateCfg) -> None:
        results = paths.expand(self.cfg.results_dir)
        results.mkdir(parents=True, exist_ok=True)
        workdir = paths.expand(d.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        job.result_file = results / f"{job.id}-{job.delegate}.md"
        argv = build_argv(d, job.task)
        with self._slots:
            log.info("job %s -> %s: %s", job.id, job.delegate, short_title(job.task))
            try:
                proc = subprocess.run(
                    argv,
                    input=job.task if d.stdin else None,
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=d.timeout_minutes * 60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                output = proc.stdout.strip() or proc.stderr.strip()
                job.status = "done" if proc.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                output, job.status = "", "timed out"
            except OSError as e:
                output, job.status = str(e), "failed"

        minutes = (time.time() - job.started) / 60
        job.result_file.write_text(
            f"# {job.delegate}: {short_title(job.task, 12)}\n\n"
            f"- Status: {job.status}\n- Took: {minutes:.1f} min\n- Task: {job.task}\n\n## Result\n\n{output}\n",
            encoding="utf-8",
        )
        job.summary = short_title(output.replace("\n", " "), 25) if output else ""
        log.info("job %s %s in %.1f min -> %s", job.id, job.status, minutes, job.result_file)
        what = short_title(job.task)
        if job.status == "done":
            self.notify(f"Task finished by {job.delegate}: {what}. The result is saved in the jobs folder.")
        else:
            self.notify(f"The {job.delegate} task {job.status}: {what}.")

    def status(self) -> str:
        with self._lock:
            jobs = list(self.jobs.values())[-5:]
        if not jobs:
            return "No delegated tasks yet."
        lines = []
        for j in jobs:
            mins = (time.time() - j.started) / 60
            lines.append(f"{j.delegate} '{short_title(j.task, 6)}' is {j.status} ({mins:.0f} min)")
        return "; ".join(lines) + "."

    def last_result(self) -> str:
        done = [j for j in self.jobs.values() if j.status == "done" and j.result_file]
        if not done:
            return "No finished task results yet."
        j = done[-1]
        text = j.result_file.read_text(encoding="utf-8")
        return text[:4000]
