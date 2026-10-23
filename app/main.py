import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.datastructures import MutableHeaders
from starlette.responses import RedirectResponse, Response

from app.auth.router import router as auth_router
from app.asset_versioning import STATIC_DIR, digest_for
from app.config import settings
from app.database import close_pool, get_connection, init_db, write_transaction
from app.http_utils import is_htmx, same_app_path
from app.library.scanner import (
    _recompute_series_cover,
    backfill_metadata,
    backfill_number_in_series,
    backfill_sort_title,
    backfill_text_size,
)
from app.library.router import router as library_router
from app.library.watcher import watcher
from app.novels.router import router as novels_router
from app.library.schedule import start_scheduler as start_scan_scheduler
from app.library.schedule import stop_scheduler as stop_scan_scheduler
from app.novels.sync import backfill_chapter_content_size, backfill_novel_cover_thumbnails
from app.novels.sync import start_scheduler as start_novel_scheduler
from app.novels.sync import stop_scheduler as stop_novel_scheduler
from app.reader.router import router as reader_router
from app.account_events import AccountChangeNotifications
from app.stats.router import router as stats_router
from app.tracking import service as tracking
from app.tracking.router import router as tracking_router

# uvicorn configures its own loggers, but the Python root logger stays at
# WARNING by default -- without this, logger.info(...) calls used throughout
# the app's modules (e.g. reading-progress diagnostics) get silently
# discarded and never show up in `docker logs`.
_log_level = getattr(logging, settings.log_level.upper(), None)
if not isinstance(_log_level, int):
    # Invalid APP_LOG_LEVEL value -- fall back to INFO rather than crashing
    # the whole app over a typo in an environment variable.
    _log_level = logging.INFO
logging.basicConfig(level=_log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s")

logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # Where an earlier version persisted the SSE id sequence. Nothing reads
    # it any more (see events.py); removed so it does not sit in the data
    # directory looking meaningful.
    (settings.data_dir / "event-sequence.json").unlink(missing_ok=True)

    # One-time backfill for series that were already in manual-sort mode
    # before the fix that made _recompute_series_cover honor manual_order --
    # their series.cover_path was computed under the old (auto-order-only)
    # logic and, unlike series reordered/reset after the fix, nothing
    # since has touched it to trigger a recompute. Cheap and idempotent
    # (only series already in manual mode, safe to re-run every start), so
    # just always do it here rather than trying to migrate it once.
    async with write_transaction() as wdb:
        cursor = await wdb.execute("SELECT id FROM series WHERE manual_sort = 1")
        manual_series = await cursor.fetchall()
        for row in manual_series:
            await _recompute_series_cover(wdb, row["id"])
        await wdb.commit()

    # Same treatment, same reasoning as the block above: process_file() only
    # reprocesses a file whose hash changed, so an already-imported item
    # keeps whatever number it was given by the numbering rules in force
    # when it was imported -- a "Rescan" does not revisit it. Running this
    # on every start is what makes an improvement to those rules (chapter
    # markers, roman numerals, reading the filename when an EPUB's dc:title
    # doesn't name the volume) apply to a library that already exists,
    # without the user having to know to click anything. It writes only the
    # rows whose value actually changes, so the steady state (every start
    # after the first) does no writes at all -- measured at 357ms for a
    # 400-series/8000-item library, against 1.2s for the one start that
    # actually rewrites all of them.
    await backfill_number_in_series()

    # And for the key items with the same number are ordered by: it is the
    # filename's now (see process_file), and rows imported before that keep
    # their title's until this re-keys them. No file is opened; nothing is
    # written once every row agrees.
    await backfill_sort_title()

    # And once more, for how much text each book holds -- the number the
    # reading-time estimate scales by. Same reason as the two above:
    # process_file() skips a file whose hash has not changed, so without
    # this every book already in the library would keep no text size and
    # get no estimate. It opens each pending EPUB's central directory only
    # (no decompression), and does nothing at all once they all have one.
    await backfill_text_size()

    # And for what each item's file says about itself (author, description):
    # the rules for reading it changed, and the books already imported were
    # read under the old ones. Keyed on items.metadata_version, so it re-reads
    # the library once per change to those rules and nothing otherwise.
    await backfill_metadata()

    # Same idea once more, for web-novel covers: a novel's cover is only
    # downloaded when it has none, so a sync never revisits one already
    # stored -- this is what converts the ones kept at the source site's own
    # size and format. Tens of rows at most, and it does nothing at all on
    # every start after the first.
    await backfill_novel_cover_thumbnails()

    # And for how much text each scraped chapter holds -- the number the
    # manifest weights progress by and the reading-rate learning divides
    # seconds into. Both used to ask LENGTH(content_html), which reads the
    # whole chapter; the column is the same number, kept beside it. One
    # UPDATE, and a no-op on every start after the first.
    await backfill_chapter_content_size()

    # reconnects the watcher for every already-registered library when the process starts
    db = await get_connection()
    try:
        cursor = await db.execute("SELECT id, root_path FROM libraries")
        libraries = await cursor.fetchall()
    finally:
        await db.close()

    loop = asyncio.get_event_loop()
    for lib in libraries:
        watcher.start(lib["id"], lib["root_path"], loop)

    start_novel_scheduler()
    start_scan_scheduler()

    # The same idea as the six backfills above -- reaching rows nothing
    # revisits -- for the descriptions of series that were linked to a
    # tracker before a link fetched one. It is the one backfill that makes
    # NETWORK requests, so it is the one that does not run in front of the
    # app: a slow or unreachable catalogue would hold the whole install
    # closed. It does nothing at all once every linked series has been
    # asked once (see service.backfill_summaries).
    summaries = asyncio.create_task(tracking.backfill_summaries())
    # And the same again for a shelf the reader filed (finished, on hold,
    # dropped...) before this app told a tracker so. Its own column is what
    # makes it a no-op from the second start onwards (see
    # service.backfill_statuses). Its own task rather than one after the
    # other: they touch different rows, and a catalogue that will not
    # answer about descriptions must not also swallow this.
    statuses = asyncio.create_task(tracking.backfill_statuses())

    yield

    # Reached when uvicorn receives SIGTERM (`docker stop`) or SIGINT --
    # it stops accepting new connections and lets in-flight requests finish
    # first, then runs this. Without stopping these explicitly, `docker
    # stop` would still work (the OS kills the process outright once its
    # grace period elapses), but the watcher's background threads and the
    # novel scheduler's jobs would be torn down mid-operation instead of
    # cleanly, and the container would take the full grace period every
    # time instead of exiting as soon as it's actually done.
    logger.info("Shutting down: stopping library watchers and schedulers...")
    watcher.stop_all()
    stop_novel_scheduler()
    stop_scan_scheduler()
    # A queued tracker push holds a database connection whose worker thread
    # is not a daemon, so an abandoned one keeps the process alive with
    # nothing left to run it. Finishing them is also simply the right
    # thing: they exist because somebody marked something read.
    # Cancelled rather than awaited: it is a slow walk over a catalogue
    # with nothing waiting on it, and `docker stop` should not sit through
    # the rest of it. Whatever it had already written is committed, and
    # what it had not is picked up by the next start, which is the whole
    # point of the NULL it works from.
    summaries.cancel()
    statuses.cancel()
    with suppress(asyncio.CancelledError):
        await summaries
    with suppress(asyncio.CancelledError):
        await statuses
    await tracking.drain()
    # Last of all: the pooled read connections. Each one owns a thread, and
    # a thread that is not a daemon keeps the process alive after there is
    # nothing left to serve -- the same reason the tracker queue is drained
    # above. After this nothing can read, which is why it comes after
    # everything that still might.
    await close_pool()
    logger.info("Shutdown complete.")


app = FastAPI(title="Publication Reader", lifespan=lifespan)
app.add_middleware(AccountChangeNotifications)


class VersionedStaticFiles(StaticFiles):
    """`immutable` for the fingerprinted URL, and for nothing else.

    Every /static URL a template renders carries the file's content hash
    (`asset_url()` in asset_versioning.py), so `/static/js/home.js?v=9142fa4762`
    always answers with the same bytes -- the file changing is what changes
    the URL. On that request `immutable`, which tells a browser never to
    revalidate, is simply true.

    The BARE path is the case that must not get it. `/static/js/home.js`
    with no `v`, or with a `v` from a previous deploy, names a URL whose
    content does change underneath it; answering `immutable` there would
    pin a stale script in caches that no deploy could reach. Those get a
    short answer and revalidate against the ETag StaticFiles already sends.

    Same two answers, and the same reason, as the reader's `?v=`
    (`_content_cache_headers` in reader/router.py) -- `public` here rather
    than `private` because nothing under /static is personal to a session.

    What this is NOT worth: the browser already keeps these across loads on
    heuristic freshness alone. Measured before the change, 7 warm loads of
    `/`, counted in the server's own access log: each asset was fetched
    exactly once. The win is the cache that has aged past that heuristic
    and the one a deploy just invalidated, not the second page view.
    """

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        asked = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("v", [None])[0]
        try:
            named = Path(full_path).resolve().relative_to(STATIC_DIR.resolve()).as_posix()
        except ValueError:
            named = None
        if named and asked and asked == digest_for(named):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "public, max-age=60, must-revalidate"
        return response


app.mount("/static", VersionedStaticFiles(directory="app/static"), name="static")


class ImmutableStaticFiles(StaticFiles):
    """StaticFiles that lets the browser keep a cover for good.

    Every generated thumbnail's name carries the hash of the file it came
    from (see covers/thumbnails.py), so a given URL always answers with the
    same bytes: a cover that CHANGES gets a new name, and so does a series
    whose first item was removed -- its `cover_path` becomes the next item's
    file, a different URL, and the card that carries it is re-rendered by
    the server. Nothing under here is ever rewritten in place, which is what
    makes `immutable` safe.

    Without it, StaticFiles still sent an ETag, so a grid of 60 covers cost
    60 conditional requests on every page load -- 304s with no body, but a
    round trip each, which is what is actually felt on a phone.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


app.mount("/covers", ImmutableStaticFiles(directory=str(settings.covers_dir)), name="covers")


@app.middleware("http")
async def revalidate_pages(request: Request, call_next):
    """A page is KEPT, and asked about before it is used.

    Every screen here is live: SSE events change what is on it while it is
    open (see static/js/library-events.js), and those changes land in the
    DOM, never in the HTML the browser filed away. With no Cache-Control
    at all Chrome applies heuristic freshness and answers a back/forward
    navigation out of its own disk cache -- walking to /stats and pressing
    Back put a reading percentage of 50% back on a card that had been
    showing 88% for the last minute, and **the request never reached the
    server**, which is how that stayed hidden: the log showed nothing but
    the SSE reconnect.

    `no-cache` rather than `no-store`, which is what this used to send.
    Only `no-cache` lets the browser KEEP the page -- which is what the
    back/forward cache is, and where Back stops being a round trip at all.

    What it does NOT do is forbid a stale answer to a SESSION-HISTORY
    navigation, and an earlier version of this docstring said it did.
    Measured in the field, Firefox, Back out of a reader:
    `type: "back_forward", transferSize: 0, decodedBodySize: 318370` --
    served from the HTTP cache with this very header on it, and the
    request never reached here. Only `no-store` binds that, at the cost
    above. The page answers it instead, by carrying the point in the
    event sequence it was rendered at into its stream's URL (`?since=` in
    base.html), and the stream tells it when that is behind. A
    back/forward-cache restore is not a stale render either: the DOM comes
    back exactly as it was left, live, and the stream reconnects and says
    whether anything was published meanwhile.

    What `no-cache` costs is a full body on every navigation that is NOT a
    restore, because there was nothing to revalidate AGAINST. So this adds
    the validator: the page is hashed, the hash is its `ETag`, and a
    request that already holds it is answered **304 with no body** (45 KB
    for the home page, 36 KB for a series). Two renders of an unchanged
    page are byte-identical, which is what makes the hash hit rather than
    a nice idea -- measured on /, /series/{id} and /stats.

    Scoped by content type, so covers (immutable, hash-named) and static
    assets keep their long-lived caching, and anything that has already
    chosen its own policy -- the reader's own pages, the SSE stream --
    keeps it.
    """
    response = await call_next(request)
    if "text/html" not in response.headers.get("content-type", ""):
        return response
    if "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "private, no-cache"
    # Only a plain GET is worth a validator, and only a 200 has a body
    # whose hash means anything. A redirect or an error is answered again.
    if request.method != "GET" or response.status_code != 200:
        return response

    # Under BaseHTTPMiddleware every response arrives as a stream of
    # chunks, even one the route built whole; `.body` is the shape a
    # Response has when nothing wrapped it, which is what a direct call
    # hands over. Both, so this reads the same either way.
    body = getattr(response, "body", None)
    if body is None:
        body = b"".join([chunk async for chunk in response.body_iterator])
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = MutableHeaders(raw=list(response.raw_headers))
    headers["ETag"] = etag
    if request.headers.get("if-none-match") == etag:
        # A 304 carries no body, so its Content-Length would be a lie.
        del headers["content-length"]
        return Response(status_code=304, headers=dict(headers.items()))
    return Response(content=body, status_code=200, headers=dict(headers.items()))


@app.exception_handler(StarletteHTTPException)
async def redirect_unauthenticated_page_loads(request: Request, exc: StarletteHTTPException):
    """
    The bug this fixes: opening the app on a device with no session cookie
    (or an expired one) hit `get_current_user`'s plain
    `HTTPException(401, "Not authenticated")` -- FastAPI's default handler
    turns that straight into a JSON body, so instead of the login page the
    person just saw `{"detail":"Not authenticated"}` in their browser.

    Only redirects a real 401 whose `Accept` header includes `text/html` --
    that's what a top-level page load sends. `fetch()` (progress saves, page
    images, EPUB resources), `EventSource` (the SSE library-events stream),
    and `<img>`/`<link>`/`<script>` all default to something else, so they
    still get a plain JSON 401 that the calling JS can handle on its own
    instead of silently redirecting an in-page request.

    (This used to also skip any 401 carrying a `WWW-Authenticate` header,
    so an OPDS client challenged over Basic Auth wasn't handed an HTML
    login page. The OPDS feeds are gone and nothing in the app issues that
    challenge any more, so the exemption went with them.)

    Every other exception (all other status codes, and any 401 that fails
    the check above) falls through to Starlette's own default handler,
    unchanged.
    """
    if exc.status_code != 401:
        return await http_exception_handler(request, exc)
    # An htmx request is NOT a page load, though it says Accept: text/html
    # (htmx 4 sends that on every request). Redirected, fetch followed the
    # 302 to the login page and handed htmx a 200: a setting changed after
    # the session had ended flashed "Saved" while nothing was stored
    # (measured in both browsers). It gets its 401, plus HX-Redirect -- which
    # htmx follows whatever the status -- to the login page, returning to
    # the page the reader was ON (HX-Current-URL), not to the POST's path.
    if is_htmx(request):
        response = await http_exception_handler(request, exc)
        response.headers["HX-Redirect"] = _login_url(same_app_path(request.headers.get("hx-current-url")))
        return response
    if "text/html" in request.headers.get("accept", ""):
        # Only a GET can be returned to. A plain form POST answered with its
        # own path as `next` landed, after signing in, on
        # `GET /settings/page-time-cap` -> 405; the page it was posted from
        # is the one worth going back to.
        if request.method == "GET":
            here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        else:
            here = same_app_path(request.headers.get("referer"))
        return RedirectResponse(_login_url(here), status_code=302)
    return await http_exception_handler(request, exc)


def _login_url(next_path: str | None) -> str:
    if next_path and next_path not in ("/", "/login"):
        return f"/login?next={quote(next_path, safe='')}"
    return "/login"


app.include_router(auth_router)
app.include_router(library_router)
app.include_router(reader_router)
app.include_router(novels_router)
app.include_router(stats_router)
app.include_router(tracking_router)
