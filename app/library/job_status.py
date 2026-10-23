"""
Live progress of the background jobs currently running: library scans, and
the whole-collection renumber.

Held in memory only, exactly like the events broadcaster next door: a job
is a transient activity of the running process, so a restart mid-job
correctly means "nothing is running" rather than a stale row claiming
otherwise. Nothing here is worth a database write per unit of work, which
is precisely the traffic this would otherwise add to a hot path.

Two ways to read it, because they answer different questions:

- `_publish()` pushes changes over the existing SSE channel, so a screen
  that is already open follows a job as it happens.
- `snapshot()` backs a plain GET, for a screen that OPENS while a job is
  already under way -- SSE only ever delivers what happens next, so
  without this such a page would show nothing until the next unit.

Progress events are throttled (see `advance`): a few hundred messages over
a job is useful, one per file across a library of thousands is just noise
on the wire for a number the user cannot read that fast anyway.

Jobs are keyed by a string rather than a library id because they are not
all per-library: a scan is (`scan:{library_id}`), the renumber is one job
over the whole collection (`renumber`). The key is also what the page's
markup carries in `data-job-key`, which is how one renderer in
library-events.js draws either kind.
"""

import time

from app.library import events

RENUMBER_KEY = "renumber"

# job key -> dict (the shape returned by snapshot(), minus the key)
_jobs: dict[str, dict] = {}

# Roughly how many progress events one whole job should produce, however
# many units it works through.
_TARGET_EVENT_COUNT = 200


def scan_key(library_id: int) -> str:
    return f"scan:{library_id}"


def _publish(key: str) -> None:
    state = _jobs.get(key)
    if state is None:
        return
    events.publish({"type": "job_progress", "job_key": key, **_public(state)})


def _public(state: dict) -> dict:
    return {
        "kind": state["kind"],
        # What one unit of this job is, so the progress text can say
        # "12 / 30 series" without the frontend knowing what jobs exist.
        "unit": state["unit"],
        "running": state["running"],
        "total": state["total"],
        "processed": state["processed"],
        "current": state["current"],
        "started_at": state["started_at"],
        "finished_at": state["finished_at"],
    }


def start(key: str, kind: str, unit: str, total: int) -> None:
    _jobs[key] = {
        "kind": kind,
        "unit": unit,
        "running": True,
        "total": total,
        "processed": 0,
        "current": None,
        "started_at": time.time(),
        "finished_at": None,
        "_every": max(1, total // _TARGET_EVENT_COUNT),
    }
    _publish(key)


def advance(key: str, current: str) -> None:
    """Records that `current` is about to be processed."""
    state = _jobs.get(key)
    if state is None:
        return
    state["processed"] += 1
    state["current"] = current
    # Always report the final unit, so the bar reliably lands on 100%
    # rather than stopping a few short of it.
    if state["processed"] % state["_every"] == 0 or state["processed"] == state["total"]:
        _publish(key)


def finish(key: str) -> None:
    state = _jobs.get(key)
    if state is None:
        return
    state["running"] = False
    state["current"] = None
    state["finished_at"] = time.time()
    _publish(key)


def snapshot() -> list[dict]:
    """Every job this process knows about -- running or finished.

    A finished one is kept so a page loading right after a job ends can
    still say so, instead of showing nothing and leaving the user unsure
    whether it ran at all.
    """
    return [{"job_key": key, **_public(state)} for key, state in _jobs.items()]
