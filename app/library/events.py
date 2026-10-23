"""
Broadcaster for library-change events (item removed/added, series
removed/added) -- a simple in-process-memory pub/sub, no external queue
(Redis etc.). For this app's scale of use (self-hosted, few clients
connected at a time, a single process), a list of async queues is already
enough and doesn't add any new dependency.

Consumed by the SSE endpoint in library/router.py -- every connected
tab/browser has its own queue, fed by publish() whenever the watcher or a
scan changes something in the library (see scanner.py).

## What a page that was away is told

A stream drops all the time -- a backgrounded tab (the hx-sse extension
closes it on purpose), a phone sleeping, the server restarting -- and
whatever was published meanwhile never reached the page. It is not
replayed. Each event carries an id, the browser sends the last one it saw
back as `Last-Event-ID` when it reconnects (the extension does this by
itself), and the stream compares it with the CURRENT id: equal, and the
page missed nothing; different, and it is told `events_gap`, which it
answers by fetching itself again and MORPHING that into place (see
`resync` in library-events.js) -- scroll, focus, typed text and open
dialogs survive it, so being told is cheap.

The id is `{EPOCH}-{n}`, and the EPOCH is drawn on every boot. That is
what keeps a restart honest without anything on disk: the counter starts
again at 1, so without the epoch a page that saw 5 would read as current
once the new process had published 5 events. With it, every id from the
old process is foreign, and each open page morphs once after a restart.

Nothing is kept for a reconnecting client, so nothing bounds how long it
may have been away: a page a week old and one a second old get the same
answer.
"""
import asyncio
import itertools
import json
import secrets
from collections import namedtuple

# Identifies this PROCESS's id sequence; see the module docstring.
EPOCH = secrets.token_hex(4)

# Published live but never given an id: they describe a job's progress,
# not a change of state, and a page asks /library/job-status for the
# current picture on load anyway. Without an id they leave the browser's
# Last-Event-ID where it was, so a page is never told it is behind because
# a scan made progress.
TRANSIENT_TYPES = frozenset({"job_progress"})

# One published event, as it travels to every open stream.
#
# `name` is what goes on the SSE `event:` line, and it is load-bearing in a
# way the payload is not: the hx-sse extension DISPATCHES a named message
# as a DOM event and SWAPS an unnamed one into the page as HTML. An event
# that lost its name would therefore paint its own JSON onto the screen, so
# nothing here may write a frame without one -- see `_name_of`.
Event = namedtuple("Event", "number name payload")

_subscribers: set[asyncio.Queue] = set()
_counter = itertools.count(1)
_last_id = 0

# The event name a page is told it is behind under. Named rather than
# inferred from the payload because the NAME is what reaches the browser as
# a DOM event (see Event above); the `type` inside the JSON is the same
# word, kept so the frame reads the same either way.
GAP_EVENT = "events_gap"

# Put into a stream's queue in place of events it was too far behind to
# receive. Not an event: it carries no id, and the stream answers it the
# way it answers a reconnect from a page that is behind.
GAP_MARKER = Event(-1, "", "")


def event_id(number: int) -> str:
    return f"{EPOCH}-{number}"


def current_id() -> int:
    """Where the sequence stands right now. Zero before anything was
    published, which is a real position and not a missing one."""
    return _last_id


def is_current(seen: str) -> bool:
    """Whether a page that last saw `seen` has missed nothing.

    Equality and nothing looser: an id from another epoch is another
    process's, and one ahead of this sequence cannot be placed in it either.
    """
    return seen == event_id(_last_id)


def rendered_at() -> str:
    """The id to stamp into a page as it is rendered.

    A document served from the browser's HTTP CACHE never touches the
    server, so nothing about it says how old it is -- and its stream opens
    with no `Last-Event-ID`. So the page carries the point it was rendered
    at in its stream's URL (`?since=`, base.html), and the stream's first
    connection compares THAT.

    Measured in the field, Firefox, Back out of a reader:
    `performance.getEntriesByType("navigation")[0]` reported
    `type: "back_forward", transferSize: 0, decodedBodySize: 318370` --
    the document came from cache with `Cache-Control: private, no-cache`
    on it, and the server never saw the request. `no-cache` does not bind
    a session-history navigation; only `no-store` would, and that is what
    the back/forward cache is traded away for.
    """
    return event_id(current_id())


def register(templates) -> None:
    """Expose it to templates, on one Jinja env.

    base.html stamps EVERY page, so an env that skips this raises at render
    time rather than quietly dropping the stamp -- same arrangement, and
    same reason, as app/ui_prefs.py. A test walks the routers.
    """
    templates.env.globals["library_events_rendered_at"] = rendered_at


# How many undelivered events one stream may hold before it is declared
# behind. A scan publishes one event per file, so a stream that has stopped
# draining -- a phone that slept with the tab open, a connection a proxy is
# no longer carrying but has not closed either -- would otherwise grow a
# queue the size of the library, in memory, for as long as the socket stays
# half-open. That is the one shape where "keep everything" is wrong.
#
# Generous next to what any real client leaves outstanding: the reader
# drains as fast as events arrive, and only a stalled one gets near it.
MAX_PENDING_PER_STREAM = 256


def subscribe() -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_PENDING_PER_STREAM)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    _subscribers.discard(queue)


# The name a frame falls back to when an event carries no `type`.
#
# Never the empty string, and that is the whole point: an SSE frame with no
# `event:` line is one the hx-sse extension swaps into the page as HTML, so
# a publisher that forgot its type would put raw JSON on screen. This turns
# that into a DOM event nothing listens for, which is invisible instead.
# `tests/test_events.py` also scans every publish() call site for a type,
# so reaching this at all means a caller was added wrong.
UNNAMED = "unknown_event"


def _name_of(event: dict) -> str:
    return event.get("type") or UNNAMED


def publish(event: dict) -> None:
    """Deliver `event` to every open stream.

    Queue items are `Event(number, name, payload)`; a transient event
    carries number 0, which the stream writes without an `id:` line so the
    browser's Last-Event-ID stays on the last event that actually changed
    something.
    """
    global _last_id
    payload = json.dumps(event)
    name = _name_of(event)
    if event.get("type") in TRANSIENT_TYPES:
        number = 0
    else:
        number = _last_id = next(_counter)
    for queue in list(_subscribers):
        try:
            queue.put_nowait(Event(number, name, payload))
        except asyncio.QueueFull:
            # This stream is not keeping up, so the page on it is already
            # behind -- the thing to protect is the memory of the process
            # it is attached to. Everything queued is dropped and one
            # marker put in its place; the stream turns that into the
            # `events_gap` a reconnecting page that is behind gets.
            # Dropping the OLD events rather than the new one is what
            # makes the queue bounded rather than merely capped.
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(GAP_MARKER)
