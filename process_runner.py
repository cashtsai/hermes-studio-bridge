"""Bounded subprocess boundary for maintenance commands; never log arguments."""
import json
import math
import os
import signal
import subprocess
import sys
import time


def _event(operation, outcome, started, **fields):
    print(json.dumps({"event": "maintenance_process", "operation": operation,
                      "outcome": outcome, "elapsed_s": round(time.monotonic() - started, 3),
                      **fields}), file=sys.stderr, flush=True)


def run(argv, *, operation, timeout, input=None):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("A positive finite timeout is required")
    started = time.monotonic()
    _event(operation, "started", started)
    try:
        # Single audited primitive: every caller gets a deadline, process-group
        # cleanup, reaping, and content-free outcome logging.
        with subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else None,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, start_new_session=True) as proc:
            try:
                stdout, stderr = proc.communicate(input=input, timeout=timeout)
            except BaseException as exc:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    _event(operation, "already_exited", started)
                proc.communicate()
                if isinstance(exc, subprocess.TimeoutExpired):
                    _event(operation, "timeout", started, timeout_s=timeout)
                    return subprocess.CompletedProcess(argv, 124, "", "command timed out")
                _event(operation, "cancelled", started, error=type(exc).__name__)
                raise
            _event(operation, "success" if proc.returncode == 0 else "failed", started,
                   returncode=proc.returncode)
            return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    except OSError as exc:
        _event(operation, "spawn_failed", started, error=type(exc).__name__)
        return subprocess.CompletedProcess(argv, 127, "", type(exc).__name__)
