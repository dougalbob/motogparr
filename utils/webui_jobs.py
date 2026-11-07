#!/usr/bin/env python3
"""
The WebUI's single background job
=================================

The WebUI spawns the grabber and the overlay utility as subprocesses. Three
properties matter, and the previous string-based implementation had none of
them reliably:

* **One job at a time, reserved synchronously.** The old ``/run`` checked
  ``command_output["running"]`` and then started a thread which set it - two
  requests arriving together could both pass the check. Here the singleton
  is claimed under a lock *before* the worker thread exists, so the second
  request is refused while the first is still starting.
* **Bounded output.** Output used to accumulate in one string for as long as
  the job ran. It is now capped (``MAX_OUTPUT_CHARS``, oldest text dropped);
  the UI shows recent output, and a runaway job cannot grow the WebUI's
  memory without limit.
* **An ending.** The exit code is recorded, and a job that outlives its
  timeout is killed and says so in the output. "Still running" must never
  mean "hung forever with nobody told".

This state is deliberately **process-local**. That is safe today because the
Flask dev server is a single process; it is also exactly why Slice 6 of
``docs/IMPROVEMENT_PLAN.md`` must not add WSGI workers until this state is
externalised or the worker model is proven safe. Do not "fix" that by
pointing two processes at this module.
"""

from __future__ import annotations

import subprocess
import threading
from datetime import datetime
from typing import Callable, List, Optional, Sequence, Tuple

#: Retained output cap. A grabber run prints a few hundred lines; 100k chars
#: is generous headroom while still bounding memory.
MAX_OUTPUT_CHARS = 100_000

#: No WebUI job may run longer than this. Scheduled/backfill searches are
#: minutes, overlay runs are seconds; an hour means something is hung.
DEFAULT_TIMEOUT_SECONDS = 3600


class JobManager:
    """Owns the one-at-a-time job slot and its status dict."""

    def __init__(
        self,
        popen: Callable = subprocess.Popen,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_output_chars: int = MAX_OUTPUT_CHARS,
    ):
        self._popen = popen
        self._timeout_seconds = timeout_seconds
        self._max_output_chars = max_output_chars
        self._lock = threading.Lock()
        # The status dict the templates and /status render. Keys are kept
        # stable: dashboard.html reads status.output, other pages pass the
        # whole dict through.
        self.state = {
            "running": False,
            "command": "",
            "output": "",
            "timestamp": None,
            "exit_code": None,
            "timed_out": False,
            "batch_total": 0,
            "batch_current": 0,
        }

    # -- reservation ------------------------------------------------------

    def try_start(self, label: str, output: str = "Starting...\n") -> bool:
        """
        Claim the job slot synchronously. False if a job is already running.

        This must happen in the request handler, before any thread is
        created: claiming inside the worker is the race this replaces.
        """
        with self._lock:
            if self.state["running"]:
                return False
            self.state.update({
                "running": True,
                "command": label,
                "output": output,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "exit_code": None,
                "timed_out": False,
                "batch_total": 0,
                "batch_current": 0,
            })
            return True

    def start(self, argv: Sequence[str], label: str) -> bool:
        """Claim the slot and launch ``argv`` on a worker thread."""
        if not self.try_start(label):
            return False
        thread = threading.Thread(target=self.run_job, args=(list(argv),), daemon=True)
        thread.start()
        return True

    # -- status mutation (all under the lock) ------------------------------

    def append_output(self, text: str) -> None:
        with self._lock:
            combined = self.state["output"] + text
            if len(combined) > self._max_output_chars:
                combined = combined[-self._max_output_chars:]
            self.state["output"] = combined

    def set_output(self, text: str) -> None:
        with self._lock:
            self.state["output"] = text[-self._max_output_chars:]

    def update_command(self, label: str) -> None:
        with self._lock:
            self.state["command"] = label
            self.state["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def set_batch(self, total: int, current: int) -> None:
        with self._lock:
            self.state["batch_total"] = total
            self.state["batch_current"] = current

    def finish(self, exit_code: Optional[int] = None, timed_out: bool = False) -> None:
        with self._lock:
            self.state["running"] = False
            self.state["exit_code"] = exit_code
            self.state["timed_out"] = timed_out
            self.state["batch_total"] = 0
            self.state["batch_current"] = 0

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self.state["running"])

    # -- execution ---------------------------------------------------------

    def _run_one(self, argv: List[str], timeout: Optional[int]) -> Tuple[Optional[int], bool]:
        """
        Stream one subprocess into the bounded output. Returns (exit_code, timed_out).

        Assumes the slot is already claimed. The timeout is enforced by a
        timer that kills the process; the output loop then ends because the
        pipe closes, and the kill is reported rather than looking like a
        normal exit.
        """
        timed_out = threading.Event()

        process = self._popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        timer = None
        if timeout:
            def _kill():
                timed_out.set()
                try:
                    process.kill()
                except Exception:
                    pass
            timer = threading.Timer(timeout, _kill)
            timer.daemon = True
            timer.start()
        try:
            for line in process.stdout:
                self.append_output(line)
            exit_code = process.wait()
        finally:
            if timer is not None:
                timer.cancel()
        if timed_out.is_set():
            self.append_output(
                f"\n⏱️ Job killed after exceeding the {timeout}s time limit.\n"
            )
            return None, True
        return exit_code, False

    def run_job(self, argv: List[str]) -> None:
        """Worker body for a single command. The slot must already be claimed."""
        exit_code: Optional[int] = None
        timed_out = False
        try:
            exit_code, timed_out = self._run_one(list(argv), self._timeout_seconds)
            if exit_code not in (None, 0):
                self.append_output(f"\n⚠️ Command exited with status {exit_code}.\n")
        except Exception as exc:  # spawn failure etc: visible in the output pane
            self.append_output(f"\nError: {exc}\n")
        finally:
            self.finish(exit_code=exit_code, timed_out=timed_out)

    def run_sequence(self, jobs: List[Tuple[List[str], str]], intro: str) -> None:
        """
        Worker body for a batch: several argv lists, one claimed slot.

        Used by /download-selected, which runs one backfill per round. The
        slot stays claimed for the whole batch (releasing it between rounds
        would let another job interleave into the same output), and a failed
        round does not abandon the remaining ones.
        """
        total = len(jobs)
        exit_code: Optional[int] = 0
        try:
            self.set_output(intro)
            for i, (argv, label) in enumerate(jobs):
                self.set_batch(total, i + 1)
                self.update_command(label)
                self.append_output(f"\n{'=' * 60}\n>>> {label}\n\n")
                code, timed_out = self._run_one(list(argv), self._timeout_seconds)
                if timed_out:
                    exit_code = None
                    break
                if code not in (None, 0):
                    exit_code = code
            self.append_output(f"\n{'=' * 60}\n✅ All {total} searches completed.\n")
        except Exception as exc:
            self.append_output(f"\nError: {exc}\n")
            exit_code = None
        finally:
            self.finish(exit_code=exit_code)
