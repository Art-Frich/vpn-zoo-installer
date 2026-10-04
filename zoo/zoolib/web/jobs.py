"""Фоновые задачи админки: пробник, smoke, рестарт сервиса, снятие трафика.

Задача — внешний процесс (обычно `zoo ... --json`); вывод копится в памяти, страница
задачи обновляется сама, пока процесс идёт. Одна задача каждого вида за раз.
"""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import paths

MAX_OUTPUT = 256 * 1024
KEEP = 30


def zoo_argv(*args: str) -> list[str]:
    """Команда zoo этим же интерпретатором (не зависит от PATH сервиса)."""
    return [sys.executable, str(paths.ZOO_PKG_ROOT / "zoo"), *args]


def outside_sandbox(argv: list[str]) -> list[str]:
    """Под systemd (zoo-web.service с ProtectSystem=strict) тяжёлые задачи — пробник с его
    TUN и netns, smoke — запускаются отдельным временным юнитом PID 1, вне песочницы."""
    if os.environ.get("INVOCATION_ID") and shutil.which("systemd-run"):
        return ["systemd-run", "--quiet", "--wait", "--pipe", "--collect",
                "--setenv=NO_COLOR=1", "--setenv=PYTHONIOENCODING=utf-8", "--", *argv]
    return argv


@dataclass
class Job:
    id: int
    kind: str
    title: str
    argv: list[str]
    timeout: float
    started: float = field(default_factory=time.time)
    finished: float | None = None
    rc: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    on_done: Callable[["Job"], None] | None = None

    @property
    def running(self) -> bool:
        return self.finished is None

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def duration(self) -> float:
        return (self.finished or time.time()) - self.started


class Jobs:
    def __init__(self) -> None:
        self._jobs: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def start(self, kind: str, title: str, argv: list[str], timeout: float = 900,
              on_done: Callable[[Job], None] | None = None) -> Job:
        with self._lock:
            for j in self._jobs.values():
                if j.kind == kind and j.running:
                    return j
            job = Job(next(self._ids), kind, title, argv, timeout, on_done=on_done)
            self._jobs[job.id] = job
            for old in sorted(self._jobs)[:-KEEP]:
                if not self._jobs[old].running:
                    del self._jobs[old]
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"job-{job.id}").start()
        return job

    def _run(self, job: Job) -> None:
        env = dict(os.environ, NO_COLOR="1", PYTHONIOENCODING="utf-8")
        try:
            cp = subprocess.run(job.argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=job.timeout, env=env, stdin=subprocess.DEVNULL)
            job.stdout, job.stderr, job.rc = cp.stdout[-MAX_OUTPUT:], cp.stderr[-MAX_OUTPUT:], cp.returncode
        except subprocess.TimeoutExpired:
            job.rc, job.error = 124, f"таймаут {job.timeout:.0f} с"
        except OSError as e:
            job.rc, job.error = 127, str(e)
        job.finished = time.time()
        if job.on_done:
            try:
                job.on_done(job)
            except Exception as e:  # обработчик результата не должен ронять поток
                job.error = f"обработка результата: {e}"

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self, n: int = 10) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: -j.id)[:n]

    def running(self, kind: str) -> Job | None:
        return next((j for j in self._jobs.values() if j.kind == kind and j.running), None)

    def wait(self, job: Job, timeout: float = 30) -> None:
        """Для тестов: дождаться завершения."""
        deadline = time.time() + timeout
        while job.running and time.time() < deadline:
            time.sleep(0.05)

    def to_dict(self, job: Job) -> dict[str, Any]:
        return {"id": job.id, "kind": job.kind, "title": job.title, "running": job.running, "rc": job.rc,
                "started": job.started, "duration": round(job.duration, 1)}
