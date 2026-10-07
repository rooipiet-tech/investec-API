"""Run a callable on hostile input in a throw-away subprocess with a hard kill.

A super-linear regex would otherwise stall the whole suite for a minute; here the
child is killed after ``kill_after`` seconds and the test fails fast.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

LIMIT_S = 2.0


def assert_fast(setup: str, call: str, *, limit: float = LIMIT_S, kill_after: float = 15.0) -> None:
    """``setup`` builds names (incl. ``data``); ``call`` is the timed expression."""
    code = textwrap.dedent(
        """
        import time
        {setup}
        _t0 = time.perf_counter()
        {call}
        print(time.perf_counter() - _t0)
        """
    ).format(setup=textwrap.indent(textwrap.dedent(setup), "").strip(), call=call)
    try:
        res = subprocess.run(
            [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=kill_after
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(f"super-linear: {call!r} not finished in {kill_after}s") from None
    assert res.returncode == 0, res.stderr[-500:]
    elapsed = float(res.stdout.strip().splitlines()[-1])
    assert elapsed < limit, f"{call!r} took {elapsed:.2f}s (limit {limit}s)"
