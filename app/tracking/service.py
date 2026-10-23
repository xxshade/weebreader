"""What the routers call: connect an account, link a series, push progress.

The push rule, which is the whole feature in six lines:

  * the number sent is a MAX over what is marked read, in the unit the
    shelf is numbered in (store.LocalProgress);
  * nothing is sent when it is not higher than what this app last sent --
    that costs no request at all;
  * the service's OWN number is read before writing, and a service already
    further ahead is left alone. The reader may have moved the entry on the
    site, and a tracker that can walk backwards is worse than no tracker:
    the damage is invisible and lands on data this app does not own.

Failure never propagates. A push happens because somebody marked a volume
read, and that has already succeeded by the time this runs -- making the
button fail, or wait on MangaUpdates, would be paying for the tracker with
the feature the reader actually pressed. Failures are written to the link's
`last_error` and shown on the series page instead.
"""

import asyncio
import weakref
import logging
from datetime import datetime, timedelta, timezone

from app.background import run_background
from app.database import get_connection, write_transaction
from app.reading_progress import mark_novel_read_up_to, mark_series_read_up_to
from app.tracking import models, myanimelist, store
from app.tracking.models import TrackerAuthError, TrackerError
from app.tracking.store import MYANIMELIST

logger = logging.getLogger(__name__)

# The pushes still running, for drain(). run_background already keeps each
# from being collected mid-flight, and logs one that raises.
_in_flight: set[asyncio.Task] = set()


# Pushes running now, each mapped to whether another was asked for while
# it ran. Two for the same shelf at once -- the last page's save and the
# beacon sent on leaving, a volume and the next one marked read together --
# both read "not sent yet" before either had recorded sending, and both
# sent.
_pushing: dict[tuple, bool] = {}


def queue_push(user_id: int, *, series_id: int | None = None, novel_id: int | None = None) -> None:
    """Fire-and-forget. Call AFTER the mark-read has been committed -- the
    push reads what is marked read back out of the database, so it has to
    be able to see it.

    One push per shelf at a time. Asked for while one runs, it runs once
    more afterwards, which reads everything committed meanwhile -- so no
    request is lost and none races the other."""
    key = (user_id, series_id, novel_id)
    if key in _pushing:
        _pushing[key] = True
        return
    _pushing[key] = False
    task = run_background(_push_until_settled(key))
    _in_flight.add(task)
    task.add_done_callback(_in_flight.discard)


async def _push_until_settled(key: tuple) -> None:
    user_id, series_id, novel_id = key
    try:
        while True:
            await push(user_id, series_id=series_id, novel_id=novel_id)
            if not _pushing[key]:
                return
            _pushing[key] = False
    finally:
        del _pushing[key]


async def drain() -> None:
    """Waits for every queued push to finish.

    A fire-and-forget task that is still running when its event loop goes
    away is not merely lost: each one holds an aiosqlite connection, whose
    worker thread is NOT a daemon, so an abandoned push keeps the process
    alive at shutdown with nothing left to run it (measured -- the test
    suite stopped exiting, hanging in threading._shutdown with two live
    aiosqlite threads while every test had already passed).

    Called from main.py's lifespan on the way down, so `docker stop`
    finishes the pushes it started, and from tests that exercise a
    mark-read handler, so each one ends with nothing in flight.
    """
    while _in_flight:
        await asyncio.gather(*tuple(_in_flight), return_exceptions=True)


# One push per shelf at a time, whoever asks: queue_push coalesces the
# requests a page turn makes, and this holds the line for the paths that
# call push() directly -- the startup backfill and linking a shelf -- which
# could otherwise run beside a queued push for the same shelf, both read
# "not sent yet", and both send. Weak, so a shelf's lock lives only while
# a push for it does.
_shelf_locks: "weakref.WeakValueDictionary[tuple, asyncio.Lock]" = weakref.WeakValueDictionary()


async def push(user_id: int, *, series_id: int | None = None, novel_id: int | None = None,
               tracker: str | None = None) -> str:
    key = (user_id, series_id, novel_id)
    lock = _shelf_locks.get(key)
    if lock is None:
        lock = _shelf_locks[key] = asyncio.Lock()
    async with lock:
        return await _push(user_id, series_id=series_id, novel_id=novel_id, tracker=tracker)


async def _push(user_id: int, *, series_id: int | None = None, novel_id: int | None = None,
                tracker: str | None = None) -> str:
    """Returns a short word for what happened, for tests and logs:
    "unlinked", "disconnected", "nothing-read", "up-to-date",
    "remote-ahead", "sent", or "failed".

    `tracker` narrows it to ONE service, which is what the button inside
    that service's dialog means: it sits under that service's own numbers,
    and a press of it that also wrote to the other one would be doing
    something the screen does not say. Everything automatic leaves it
    None and tells them all.
    """
    db = await get_connection()
    try:
        links = await store.links_for(db, user_id, series_id=series_id, novel_id=novel_id)
        if tracker is not None:
            links = [link for link in links if link["tracker"] == tracker]
        if not links:
            return "unlinked"
        local = (
            await store.series_progress(db, user_id, series_id)
            if series_id is not None
            else await store.novel_progress(db, user_id, novel_id)
        )
        status = await store.local_status(db, user_id, series_id=series_id, novel_id=novel_id)
        dates = await store.local_dates(db, user_id, series_id=series_id, novel_id=novel_id)
        accounts = {
            link["tracker"]: await store.get_account(db, user_id, link["tracker"])
            for link in links
        }
    finally:
        await db.close()

    # A status is a claim in its own right, so it does NOT need a number
    # behind it: a series marked finished with nothing marked read is
    # somebody saying they are done with it, and one filed under "plan to
    # read" has by definition nothing read. Without this, the shelves most
    # worth reporting -- read elsewhere, or not started -- would be the
    # ones that never spoke.
    if not local.anywhere and status is None:
        return "nothing-read"

    # Every tracker this thing is linked to, independently: one being
    # signed out or refusing must not stop the others being told.
    outcomes = [await _push_one(user_id, link, accounts.get(link["tracker"]), local, status, dates)
                for link in links]
    return _worst(outcomes)


async def catch_up(user_id: int, tracker: str, *,
                   series_id: int | None = None, novel_id: int | None = None) -> dict:
    """What a freshly linked entry already knew, brought over here.

    Two things arrive with a link, and both are the service answering a
    question this app could not:

    * How far the entry says the reader got. A shelf that is BEHIND the
      site stays behind forever otherwise -- the push refuses to send a
      number lower than the service's, so it would never speak again, and
      the reader would see "nothing sent yet" against an entry that is
      already at chapter 40. Only ever upwards: nothing is unmarked, and
      a local number that is already higher is left alone to be pushed.
    * The description. A scanned folder has none, and the catalogue that
      was just pointed at has a good one.

    Failures are swallowed to a word in the returned dict rather than
    raised: this runs from the Link button, and a service being slow or
    unreachable must not turn "linked" into an error page -- the link
    itself is already saved, and the ordinary push that follows reports
    the service's health in the panel.
    """
    outcome = {"marked": 0, "described": False, "remote": None}
    service = models.tracker(tracker)
    if service is None:
        return outcome

    db = await get_connection()
    try:
        account = await store.get_account(db, user_id, tracker)
        link = await store.get_link(db, user_id, tracker, series_id=series_id, novel_id=novel_id)
        local = (
            await store.series_progress(db, user_id, series_id)
            if series_id is not None
            else await store.novel_progress(db, user_id, novel_id)
        )
    finally:
        await db.close()
    if account is None or link is None:
        return outcome

    try:
        token, remote = await _read_with_token(
            service, account, user_id,
            lambda token: service.get_progress(token, link["remote_id"]))
        description = await service.describe(link["remote_id"], token=token)
    except (models.TrackerError, models.TrackerAuthError) as exc:
        logger.info("tracking: could not read %s on link: %s", tracker, exc)
        return outcome

    outcome["remote"] = remote
    async with write_transaction() as wdb:
        if remote is not None:
            outcome["marked"] = await _adopt(wdb, user_id, local, remote,
                                             series_id=series_id, novel_id=novel_id)
        if description and series_id is not None:
            outcome["described"] = await store.save_summary(wdb, series_id, description)
        await wdb.commit()
    return outcome


async def remote_state(user_id: int, tracker: str, *,
                       series_id: int | None = None, novel_id: int | None = None) -> dict:
    """What the service holds right now, for the editor to open with.

    Read at the moment the editor is opened rather than cached, because
    the whole point of an editor is to change what is there -- and
    something stale would be worse than nothing: the reader would press
    Send on a box pre-filled from last week and put a number BACK.

    Failure is a word rather than an exception, like everything else the
    panel renders: a service having a bad day must leave the rest of the
    editor -- the link, the unlink button -- on screen and usable.
    """
    answer = {"state": None, "error": None}
    service = models.tracker(tracker)
    if service is None:
        return answer
    db = await get_connection()
    try:
        account = await store.get_account(db, user_id, tracker)
        link = await store.get_link(db, user_id, tracker, series_id=series_id, novel_id=novel_id)
    finally:
        await db.close()
    if account is None or link is None:
        return answer
    try:
        _, answer["state"] = await _read_with_token(
            service, account, user_id,
            lambda token: service.get_state(token, link["remote_id"]))
    except (TrackerAuthError, TrackerError) as exc:
        answer["error"] = str(exc)
    return answer


async def send_manually(user_id: int, tracker: str, *,
                        series_id: int | None = None, novel_id: int | None = None,
                        chapter: float | None = None, volume: float | None = None,
                        score: float | None = None, status: str | None = None,
                        start_date: str | None = None, finish_date: str | None = None) -> str:
    """Exactly what was typed, to exactly one service.

    The one path that does NOT consult the never-regress rule, and that is
    deliberate: the rule exists because an automatic push cannot tell a
    correction from a mistake, and a person typing a lower number into a
    box labelled with the current one is doing neither -- they are fixing
    something the automatic path got wrong, which is the only reason this
    editor exists at all. Nothing else in the app can walk a tracker
    backwards, and nothing else asks first.

    One service, not both, because the numbers here belong to a service:
    each holds its own, and "send what I typed" has no meaning spread
    across two entries that disagree. `status` is in THAT service's
    vocabulary (a value from its STATUSES), since it is what the reader
    picked from that service's own list.
    """
    service = models.tracker(tracker)
    if service is None:
        return "unlinked"
    db = await get_connection()
    try:
        account = await store.get_account(db, user_id, tracker)
        link = await store.get_link(db, user_id, tracker, series_id=series_id, novel_id=novel_id)
        shelf_status = await store.local_status(
            db, user_id, series_id=series_id, novel_id=novel_id)
    finally:
        await db.close()
    if link is None:
        return "unlinked"
    if account is None:
        return "disconnected"

    try:
        # Still read first, and still for the same reason as a push: this
        # service REPLACES the record it is handed, so every field nobody
        # typed into has to be re-sent as it stands.
        token, remote = await _read_with_token(
            service, account, user_id,
            lambda token: service.get_progress(token, link["remote_id"]))
        if remote is None:
            await service.ensure_listed(token, link["remote_id"], status=status)
        await service.set_state(
            token, link["remote_id"], chapter=chapter, volume=volume,
            score=score, status=status, start_date=start_date,
            finish_date=finish_date, remote=remote,
        )
    except TrackerAuthError as exc:
        await _record_failure(user_id, tracker, link["id"], str(exc), auth=True)
        return "failed"
    except TrackerError as exc:
        await _record_failure(user_id, tracker, link["id"], str(exc), auth=False)
        return "failed"
    except Exception:
        logger.exception("manual tracker send failed for link %s", link["id"])
        await _record_failure(
            user_id, tracker, link["id"], "unexpected error, see the server log", auth=False)
        return "failed"

    async with write_transaction() as wdb:
        await store.record_push(
            wdb, link["id"], chapter=chapter, volume=volume,
            # What is recorded is the SHELF's status, not what was picked:
            # the column says which of this app's statuses the service has
            # heard about, and a push re-sends the shelf's whenever the two
            # differ. Recording the pick instead would have the next page
            # turn put the shelf's status straight back over a choice
            # somebody just made by hand.
            status=shelf_status if status is not None else None,
        )
        await wdb.commit()
    return "sent"


async def backfill_statuses(*, pause: float = None) -> int:
    """Shelves filed BEFORE this app told the trackers about statuses.

    The same shape as `backfill_summaries` below and for the same reason:
    nothing revisits a link, so a shelf put on hold last year would go on
    being reported as "reading" on both sites until its status changed
    again. It follows the startup backfills' two rules -- cheap once it
    has run, and a no-op on the second start -- and what enforces both is
    `tracker_links.last_pushed_status`, which a successful push writes and
    never has to write again until the shelf's status changes.

    It does not touch a number the ordinary push would not, and a status
    the service has no word for ("Stopped reading") is skipped without a
    request.
    """
    if pause is None:
        pause = BACKFILL_PAUSE_SECONDS
    db = await get_connection()
    try:
        links = [
            link for link in await store.links_status_unreported(db)
            if link["local_status"] in getattr(models.tracker(link["tracker"]), "STATUS_FOR", {})
        ]
    finally:
        await db.close()
    if not links:
        return 0
    logger.info("tracking: %d link(s) with a status never reported", len(links))

    reported = 0
    # One signed-out account would otherwise produce one doomed request
    # per link it owns, exactly as in backfill_summaries.
    silent: set[tuple[int, str]] = set()
    for link in links:
        account_key = (link["user_id"], link["tracker"])
        if account_key in silent:
            continue
        outcome = await push(
            link["user_id"],
            series_id=link["series_id"], novel_id=link["novel_id"],
            tracker=link["tracker"],
        )
        await asyncio.sleep(pause)
        if outcome == "disconnected":
            silent.add(account_key)
            continue
        if outcome == "sent":
            reported += 1
    logger.info("tracking: %d status(es) reported", reported)
    return reported


# A gap between requests, so a library with three hundred linked series
# does not arrive at a catalogue as three hundred requests in a second.
# Nothing is waiting on this -- it runs in the background after startup --
# so the polite pace costs nobody anything.
BACKFILL_PAUSE_SECONDS = 1.0


async def backfill_summaries(*, pause: float = BACKFILL_PAUSE_SECONDS) -> int:
    """Descriptions for links that were made before anything fetched them.

    `catch_up` brings a description over with the link, which does nothing
    for a series linked before that existed: nothing revisits a link, the
    same way a scan never revisits an unchanged file, so those series
    would stay blank forever unless somebody unlinked and linked again.
    This is the tracker's version of the startup backfills in main.py, and
    it follows their two rules -- it must be cheap once it has run, and it
    must be a no-op on the second start.

    What makes it cheap is `source_summary`: NULL means never asked, and a
    catalogue that answered with NOTHING is recorded as an empty string
    rather than left NULL, so a series with no description is asked once
    and not on every boot. A FAILURE is not recorded that way -- an
    unreachable service must be asked again next time -- and the first
    failure from an account stops this asking it about anything else,
    since a signed-out account would otherwise produce one doomed request
    per linked series.

    Unlike the backfills in main.py this one makes network requests, so it
    runs in the BACKGROUND rather than in front of the app coming up: a
    slow catalogue would otherwise hold the whole install closed.
    """
    db = await get_connection()
    try:
        links = await store.links_missing_summary(db)
    finally:
        await db.close()
    if not links:
        return 0

    by_series: dict[int, list] = {}
    for link in links:
        by_series.setdefault(link["series_id"], []).append(link)
    logger.info("tracking: fetching descriptions for %d earlier links", len(by_series))

    described = 0
    silent: set[tuple[int, str]] = set()
    for series_id, group in by_series.items():
        text, answered = None, False
        for link in group:
            account = (link["user_id"], link["tracker"])
            if account in silent:
                continue
            reply = await _describe_one(link)
            await asyncio.sleep(pause)
            if reply is _UNREACHABLE:
                silent.add(account)
                continue
            answered = True
            if reply:
                text = reply
                break
        # Nothing was even asked (no account, or every service refused):
        # leave the NULL alone, so the next start tries again.
        if not answered:
            continue
        async with write_transaction() as wdb:
            # Written either way -- an empty string is what records that
            # this series has been asked -- but only a real description
            # counts as one filled in.
            changed = await store.save_summary(wdb, series_id, text or "")
            if text and changed:
                described += 1
            await wdb.commit()
    logger.info("tracking: %d description(s) filled in", described)
    return described


# Distinct from None, which is a service saying there is no description.
_UNREACHABLE = object()


async def _describe_one(link):
    """One catalogue's description for one link, or _UNREACHABLE."""
    service = models.tracker(link["tracker"])
    if service is None:
        return _UNREACHABLE
    db = await get_connection()
    try:
        account = await store.get_account(db, link["user_id"], link["tracker"])
    finally:
        await db.close()
    if account is None:
        # Signed out. Its links are about to be dropped anyway, and there
        # is no token to ask with.
        return _UNREACHABLE
    try:
        _, description = await _read_with_token(
            service, account, link["user_id"],
            lambda token: service.describe(link["remote_id"], token=token))
        return description
    except (models.TrackerError, models.TrackerAuthError) as exc:
        logger.info("tracking: no description from %s for series %s: %s",
                    link["tracker"], link["series_id"], exc)
        return _UNREACHABLE


async def _adopt(db, user_id: int, local, remote, *, series_id, novel_id) -> int:
    """Mark local reading up to the service's numbers, per FIELD.

    Per field because the two can disagree legitimately: a web novel
    partway through volume 3 is "vol 2, ch 41", and the reader can be
    further ahead on the site in one unit and not the other. A scanned
    series moves in exactly one of them (series.number_kind), and
    `local.chapter`/`local.volume` is already None for the one it does not
    count in -- so a number the shelf cannot express is not adopted into
    the wrong field.
    """
    if series_id is not None:
        # The unit comes from the SHELF (`local.kind`, which is
        # series.number_kind), never from which local field happens to
        # hold a number. Both hold None until something is marked read, so
        # inferring it that way got the answer wrong on exactly the
        # shelves this runs on most -- a series just added and linked.
        # Reported from a real install: a chapter-numbered series with
        # nothing read adopted the service's VOLUME count as chapters, so
        # linking an entry sitting at volume 1 silently marked chapter 1
        # read and pushed that number straight back. It failed the other
        # way too, ignoring a real chapter count on the same shelf.
        if local.kind == "volume":
            ours, theirs = local.volume, remote.volume
        else:
            ours, theirs = local.chapter, remote.chapter
        if _higher(theirs, ours) and theirs:
            return await mark_series_read_up_to(db, user_id, series_id, theirs)
        return 0
    # The same `and theirs` as above: an entry listed but not started is
    # chapter 0, volume 0 on the service, and adopting THAT marked every
    # chapter numbered 0 (a prologue) and every chapter of a volume 0 read.
    return await mark_novel_read_up_to(
        db, user_id, novel_id,
        chapter=remote.chapter if _higher(remote.chapter, local.chapter) and remote.chapter else None,
        volume=remote.volume if _higher(remote.volume, local.volume) and remote.volume else None,
    )


# Ordered by how much a reader needs to know about it, worst first, so a
# panel reporting one word for several trackers reports the one that
# needs doing something about.
_OUTCOME_ORDER = ["failed", "disconnected", "sent", "remote-ahead", "up-to-date", "unlinked"]


def _worst(outcomes: list[str]) -> str:
    return min(outcomes, key=lambda o: _OUTCOME_ORDER.index(o) if o in _OUTCOME_ORDER else 99)


async def _push_one(user_id: int, link, account, local, status: str | None = None,
                    dates: store.LocalDates | None = None) -> str:
    service = models.tracker(link["tracker"])
    if service is None:
        # A row written by a version that knew a tracker this one does not.
        return "disconnected"
    if account is None:
        return "disconnected"
    if not _moved(local, link["last_pushed_chapter"], link["last_pushed_volume"]) \
            and not _status_news(service, status, link["last_pushed_status"]):
        return "up-to-date"

    try:
        outcome, sent = await _send(service, account, link, local, user_id, status, dates)
    except TrackerAuthError as exc:
        await _record_failure(user_id, link["tracker"], link["id"], str(exc), auth=True)
        return "failed"
    except TrackerError as exc:
        await _record_failure(user_id, link["tracker"], link["id"], str(exc), auth=False)
        return "failed"
    except Exception:
        # A push must not be able to take anything else down with it, and a
        # traceback here is a bug in this module rather than in the service.
        logger.exception("tracker push failed for link %s", link["id"])
        await _record_failure(
            user_id, link["tracker"], link["id"], "unexpected error, see the server log", auth=False)
        return "failed"

    async with write_transaction() as wdb:
        await store.record_push(wdb, link["id"], **sent)
        await wdb.commit()
    return outcome


def _moved(local, pushed_chapter, pushed_volume) -> bool:
    """Whether either field is higher than what this app last sent.

    Per field, because a web novel advances in both and they do not move
    together: finishing volume 3 moves the volume while the chapter has
    been climbing all along, and a single comparison would skip one of
    them."""
    return _higher(local.chapter, pushed_chapter) or _higher(local.volume, pushed_volume)


def _status_news(service, status: str | None, pushed_status) -> bool:
    """Whether the shelf's status is something this service has not heard.

    Said once per change. A push runs whenever a page is turned, and a
    shelf keeps being read under the same status, so without this the
    same claim would cost a request on every one of them. It is also what
    makes the backfill a no-op on the second start. A status the service
    has no word for is never news to it.
    """
    return status in service.STATUS_FOR and status != pushed_status


def _higher(ours, theirs) -> bool:
    return ours is not None and (theirs is None or ours > theirs)


async def _send(service, account, link, local, user_id: int,
                status: str | None = None, dates: store.LocalDates | None = None) -> tuple[str, dict]:
    """Returns (outcome, what was actually accepted), so the caller records
    only the fields that really moved."""
    token, remote = await _read_with_token(
        service, account, user_id,
        lambda token: service.get_progress(token, link["remote_id"]))

    # Where the entry should go, in the service's own word -- or None when
    # the shelf's status is not news to it. Only a status the reader gave
    # this shelf ever leaves here: no status is the absence of an opinion,
    # and the entry stays wherever it was filed on the site.
    target = service.STATUS_FOR[status] if _status_news(service, status, link["last_pushed_status"]) else None
    # Already there -- moved on the site, or told by another device. There
    # is nothing to send, but it is still true, so it is recorded rather
    # than asked about again on the next page turn.
    agreed_status = (status if status in service.STATUS_FOR and remote
                     and remote.status == service.STATUS_FOR[status] else None)
    if target is not None and agreed_status is not None:
        target = None

    if remote is None:
        # Not on any of the reader's lists yet -- straight onto the one it
        # belongs on, where the service can do that.
        await service.ensure_listed(token, link["remote_id"], status=target)

    # Field by field against the SERVICE's own numbers: the reader may be
    # further ahead there on one of them and not the other, and a whole-
    # record comparison would either skip a real advance or overwrite
    # their position.
    chapter = local.chapter if _higher(local.chapter, remote.chapter if remote else None) else None
    volume = local.volume if _higher(local.volume, remote.volume if remote else None) else None

    if chapter is None and volume is None and target is None:
        # Recorded as agreed anyway: not recording it would make every
        # later mark-read ask the service the same question again.
        #
        # But ONLY for a unit this shelf actually counts in. A
        # volume-numbered series has no chapter of its own, and taking the
        # service's chapter for it recorded a 0 that was never sent --
        # visible on the panel as "Last sent volume 5, chapter 0", which
        # reads as a bug and describes nothing (measured in a browser).
        return "remote-ahead", {
            "chapter": remote.chapter if remote and local.chapter is not None else None,
            "volume": remote.volume if remote and local.volume is not None else None,
            "status": agreed_status,
        }

    # The dates ride along with a write that is happening anyway, and only
    # fill a blank: a date on the site is somebody's own record, typed or
    # imported, and this app's guess at it is not better. (There is no
    # local finish at all until the shelf is marked Finished: see
    # store.local_dates.)
    start_date = finish_date = None
    if service.KEEPS_DATES and dates is not None:
        if dates.start and not (remote and remote.start_date):
            start_date = dates.start
        if dates.finish and not (remote and remote.finish_date):
            finish_date = dates.finish

    await service.set_state(
        token, link["remote_id"], chapter=chapter, volume=volume,
        status=target, start_date=start_date, finish_date=finish_date, remote=remote,
    )
    return "sent", {
        "chapter": chapter, "volume": volume,
        "status": status if target is not None else agreed_status,
    }


async def search(service, account, user_id: int, query: str):
    if account is None:
        return await service.search(query, token=None)
    _, results = await _read_with_token(
        service, account, user_id, lambda token: service.search(query, token=token))
    return results


async def _read_with_token(service, account, user_id: int, read):
    """Refresh before a read, and recover once if MAL rejects its token.

    A stored expiry is a prediction; a 401 can arrive earlier. Only retry
    reads, only on 401, and never loop when the replacement is refused.
    """
    token = await _usable_token(service, account, user_id)
    try:
        return token, await read(token)
    except TrackerAuthError as exc:
        if service.NAME != MYANIMELIST or exc.status_code != 401:
            raise
        replacement = await _usable_token(service, account, user_id, rejected_token=token)
        if replacement == token:
            raise
        return replacement, await read(replacement)


# Refresh tokens can rotate. Searches, panels and pushes for different
# shelves must not redeem the same old refresh token concurrently.
_refresh_locks: "weakref.WeakValueDictionary[tuple, asyncio.Lock]" = weakref.WeakValueDictionary()


async def _usable_token(service, account, user_id: int, *, rejected_token=None) -> str:
    if service.NAME != MYANIMELIST:
        return account["access_token"]
    key = (user_id, service.NAME)
    lock = _refresh_locks.setdefault(key, asyncio.Lock())
    async with lock:
        # The caller may have read this row before another request refreshed
        # it. Re-read under the lock, including the rotated refresh token.
        db = await get_connection()
        try:
            current = await store.get_account(db, user_id, MYANIMELIST)
        finally:
            await db.close()
        if current is None:
            raise TrackerAuthError("MyAnimeList account is disconnected")
        if rejected_token is not None and current["access_token"] != rejected_token:
            return current["access_token"]
        if not current["refresh_token"]:
            return current["access_token"]
        if rejected_token is None and current["expires_at"] and current["expires_at"] > _now():
            return current["access_token"]

        tokens = await myanimelist.refresh(current["refresh_token"])
        async with write_transaction() as wdb:
            # A disconnect/reconnect during the HTTP request must not be
            # undone by saving an old account's refreshed credentials.
            latest = await store.get_account(wdb, user_id, MYANIMELIST)
            if latest is None:
                raise TrackerAuthError("MyAnimeList account is disconnected")
            if (latest["access_token"], latest["refresh_token"]) != (
                    current["access_token"], current["refresh_token"]):
                return latest["access_token"]
            await store.save_account(
                wdb, user_id, MYANIMELIST,
                access_token=tokens["access_token"],
                refresh_token=tokens.get("refresh_token") or current["refresh_token"],
                expires_at=expiry_for(tokens.get("expires_in")),
                username=current["username"],
            )
            await wdb.commit()
        return tokens["access_token"]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def expiry_for(expires_in) -> str | None:
    """`expires_in` is seconds. Stored as a timestamp in the same format
    SQLite's datetime() writes, so the two compare as strings.

    A minute is taken off it: a token that expires while a request is in
    flight fails, and one refresh a month early costs nothing.
    """
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds - 60)).strftime(
        "%Y-%m-%d %H:%M:%S")


async def _record_failure(user_id: int, tracker: str, link_id: int, message: str, *, auth: bool) -> None:
    async with write_transaction() as wdb:
        await store.record_link_error(wdb, link_id, message)
        if auth:
            # The account, not just this link: every other push will fail
            # the same way until somebody signs in again, and the settings
            # page is where that is said.
            await store.record_account_error(wdb, user_id, tracker, message)
        await wdb.commit()
