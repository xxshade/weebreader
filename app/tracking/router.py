"""Endpoints for connecting an account and linking a series to it.

Everything here follows the conventions the rest of the app already uses:
every form keeps `method`/`action` and every endpoint answers a redirect
when the `hx-request` header is absent, so the whole feature works with
JavaScript switched off; htmx just swaps a panel instead of reloading.
"""

import logging
import secrets
import urllib.parse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.templating import templates
from app.auth.dependencies import get_current_user
from app.database import get_db, write_transaction
from app.library import events
from app.http_utils import is_htmx
from app.config import settings
from app.tracking import mangaupdates, models, myanimelist, service, store
from app.tracking.store import MANGAUPDATES, MYANIMELIST
from app.status_labels import STATUS_LABELS

logger = logging.getLogger(__name__)

router = APIRouter()


async def panel_context(db, user_id: int, *, series_id=None, novel_id=None,
                        tracker: str | None = None) -> dict:
    """Everything the tracking dialog draws, for a series or for a novel.

    Shared by the page that carries the icons and by every endpoint that
    swaps the dialog, so the two cannot describe different states -- the
    same rule the stats page's `_daily_context` follows.

    `tracker` names the ONE service the dialog is showing. The full list
    stays in the context beside it because the ICONS need it: there is one
    per service, they live outside the dialog, and a link changes what
    they say.
    """
    services = models.registry()
    accounts = {name: await store.get_account(db, user_id, name) for name in services}
    links = {
        row["tracker"]: row
        for row in await store.links_for(db, user_id, series_id=series_id, novel_id=novel_id)
    }
    # What only the OPEN dialog shows -- how far this is read, its status,
    # its dates, its kind, its title -- is asked only when a dialog is
    # being drawn. Every series and novel page view built all of it for a
    # dialog that starts closed (2.5-3.7 ms against 0.55 for the icons,
    # measured at 900 chapters and 200k spans); the click that opens it
    # asks this again with `tracker` set.
    open_dialog = tracker in services
    local = None
    if open_dialog:
        local = (
            await store.series_progress(db, user_id, series_id)
            if series_id is not None
            else await store.novel_progress(db, user_id, novel_id)
        )
    return {
        # The search box opens with the name this app shows, which is the
        # EDITED one where there is an edit: a scan often names a series
        # from its folder ("[Group] Series (2019)"), and the whole reason
        # that name is editable is that the reader knows better. Typing it
        # again to search for it would be asking twice.
        "tracker_default_query": await _local_title(db, series_id, novel_id) if open_dialog else None,
        # One row per SERVICE, in a fixed order, so the icons do not
        # reshuffle as accounts come and go.
        "trackers": [
            _row(name, module, accounts, links)
            for name, module in sorted(services.items(), key=lambda kv: kv[1].LABEL)
        ],
        # The one the dialog is showing, or None when nothing asked for
        # one -- which is how the page renders with an empty dialog that
        # the first click fills.
        "tracker_open": (
            _row(tracker, services[tracker], accounts, links)
            if tracker in services else None
        ),
        "tracker_local": local,
        # The READER's status for this shelf, which every service with a
        # word for it is told. Shown beside what is read, since "nothing
        # marked read" next to an entry about to be reported as completed
        # reads as a bug otherwise -- and a status the open service has NO
        # word for says so, rather than looking sent.
        "tracker_status": await store.local_status(
            db, user_id, series_id=series_id, novel_id=novel_id) if open_dialog else None,
        "tracker_status_labels": STATUS_LABELS,
        # When the reader started and finished it HERE, for the date boxes
        # to be checked against (they open with the service's own dates).
        "tracker_dates": await store.local_dates(
            db, user_id, series_id=series_id, novel_id=novel_id) if open_dialog else None,
        # What the SERVICE holds, read when the dialog was opened. Only
        # the panel endpoint fills these in: every other path either just
        # wrote them (so its own answer is what it sent) or has no editor
        # to fill.
        "tracker_remote": None,
        "tracker_remote_error": None,
        # Which family this shelf's own entry belongs to, so a search can
        # drop the other one: a manga's title routinely also names a light
        # novel, and picking the wrong one links a whole series to an entry
        # nothing will ever move. None where the app cannot tell (a mixed
        # library), and the panel then offers no filter rather than a
        # guessed one.
        "tracker_kind": await _local_kind(db, series_id, novel_id) if open_dialog else None,
        "tracker_all_types": False,
        "tracker_filtered_out": 0,
        # Set only by the link endpoint: what came back with the link.
        "tracker_caught_up": None,
        # And, when that included a description, the series row it was
        # written to -- so the paragraph under the title can be swapped
        # out of band instead of waiting for a reload.
        "tracker_summary": None,
        # Set by link and unlink, the two that change how many services
        # this is tied to -- which is what the icon beside the title
        # reports, from outside this panel.
        "tracker_link_changed": False,
        "tracker_series_id": series_id,
        "tracker_novel_id": novel_id,
        # Set only by the search endpoint, and only for the ONE service
        # that was searched -- the others keep their row unchanged.
        "tracker_searched": None,
        "tracker_results": None,
        "tracker_query": None,
        "tracker_message": None,
    }


def _row(name: str, module, accounts: dict, links: dict) -> dict:
    """One service, as the icons and the dialog both read it."""
    return {
        "name": name,
        "label": module.LABEL,
        "short": module.SHORT,
        "account": accounts.get(name),
        "link": links.get(name),
        # The service's own status vocabulary, for the editor's select, and
        # which of this app's statuses it has a word for.
        "statuses": module.STATUSES,
        "status_for": module.STATUS_FOR,
        "keeps_dates": module.KEEPS_DATES,
    }


async def _series_summary(db, series_id: int) -> dict | None:
    """What the description paragraph needs, read back after the link
    wrote it. A dict rather than the row, because the macro that renders
    it is the series page's and takes only this one field."""
    row = await (
        await db.execute("SELECT summary FROM series WHERE id = ?", (series_id,))
    ).fetchone()
    return None if row is None else {"summary": row["summary"] or ""}


async def _local_title(db, series_id, novel_id) -> str:
    if series_id is not None:
        row = await (await db.execute("SELECT name FROM series WHERE id = ?", (series_id,))).fetchone()
        return row["name"] if row else ""
    row = await (await db.execute("SELECT title FROM novels WHERE id = ?", (novel_id,))).fetchone()
    return row["title"] if row else ""


# A library's media_type against the two families a catalogue answers in.
# 'mixed' is deliberately absent: it means the library holds both, so
# there is nothing to filter by.
_KIND_BY_MEDIA_TYPE = {"comic": models.COMIC, "ebook": models.NOVEL}


async def _local_kind(db, series_id, novel_id) -> str | None:
    """Prose or pictures, for the thing being linked.

    A web novel is prose by construction. A scanned series takes it from
    its LIBRARY rather than from anything about the series itself -- that
    is the one place this app is ever told, and it is told once per shelf
    instead of once per folder.
    """
    if series_id is None:
        return models.NOVEL
    row = await (
        await db.execute(
            "SELECT libraries.media_type AS media_type FROM series "
            "JOIN libraries ON libraries.id = series.library_id WHERE series.id = ?",
            (series_id,),
        )
    ).fetchone()
    return _KIND_BY_MEDIA_TYPE.get(row["media_type"]) if row else None


def _target(series_id: int | None, novel_id: int | None) -> tuple[int | None, int | None]:
    """Exactly one of the two, which is the shape `tracker_links` enforces."""
    if (series_id is None) == (novel_id is None):
        raise HTTPException(status_code=400, detail="one of series_id or novel_id")
    return series_id, novel_id


def _back_to(series_id: int | None, novel_id: int | None) -> str:
    return f"/series/{series_id}" if series_id is not None else f"/novels/{novel_id}"


def _publish_card_changed(series_id, novel_id) -> None:
    """Tell every open home grid that this card's tracking dot moved.

    The dot is the one thing on that grid a link changes, and the grid is
    a different page from the one the Link button is on -- so without this
    a home page open in another tab goes on showing "not linked" until it
    is reloaded. The icon beside the title is handled by the out-of-band
    swap on the response itself; this is for the screens that did not make
    the request.

    The event carries no count, unlike a progress event: a link belongs to
    a READER, so what each browser should draw is that reader's own
    number, which is exactly what refetching the card answers. Other
    readers' browsers refetch and find their own count unchanged -- one
    wasted request, and never somebody else's tracking on their screen.
    """
    events.publish(
        {"type": "series_updated", "series_id": series_id}
        if series_id is not None
        else {"type": "novel_updated", "novel_id": novel_id})


async def _panel(request: Request, db, user_id: int, series_id, novel_id, tracker, **extra):
    context = await panel_context(
        db, user_id, series_id=series_id, novel_id=novel_id, tracker=tracker)
    context.update(extra)
    return templates.TemplateResponse(request, "_tracker_panel.html", context)


# --------------------------------------------------------------- account


@router.post("/settings/tracking/mangaupdates/connect")
async def connect_mangaupdates(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    user=Depends(get_current_user),
):
    """The one request that ever sees the password.

    It goes straight to MangaUpdates in exchange for a session token, and
    only that token is written (store.save_account has no password
    parameter at all). Failure comes back as a code in the URL, the same
    way the profile forms report theirs.
    """
    try:
        token, name = await mangaupdates.authenticate(username.strip(), password)
    except models.TrackerAuthError:
        return RedirectResponse("/settings?tracker_error=bad-credentials#tracking", status_code=302)
    except models.TrackerError:
        return RedirectResponse("/settings?tracker_error=unreachable#tracking", status_code=302)

    async with write_transaction() as wdb:
        await store.save_account(wdb, user["id"], MANGAUPDATES, access_token=token, username=name)
        await wdb.commit()
    return RedirectResponse("/settings#tracking", status_code=302)


@router.post("/settings/tracking/{tracker}/disconnect")
async def disconnect_tracker(tracker: str, request: Request, user=Depends(get_current_user)):
    if models.tracker(tracker) is None:
        raise HTTPException(status_code=404, detail="unknown tracker")
    async with write_transaction() as wdb:
        await store.delete_account(wdb, user["id"], tracker)
        await wdb.commit()
    return RedirectResponse("/settings#tracking", status_code=302)


# ------------------------------------------------------- MyAnimeList OAuth


@router.post("/settings/tracking/myanimelist/connect")
async def connect_myanimelist(request: Request, user=Depends(get_current_user)):
    """Starts the handshake. A POST, because it is an action with a side
    effect (it writes the pending row) and a GET would be followable by a
    link somebody else planted."""
    if not settings.myanimelist_client_id or not settings.myanimelist_redirect_uri:
        return RedirectResponse("/settings?tracker_error=not-configured#tracking", status_code=302)
    verifier = myanimelist.new_verifier()
    state = secrets.token_urlsafe(24)
    async with write_transaction() as wdb:
        await store.save_pending_oauth(wdb, user["id"], MYANIMELIST, state=state, verifier=verifier)
        await wdb.commit()
    return RedirectResponse(myanimelist.authorize_url(state, verifier), status_code=302)


async def handle_oauth_callback(request: Request, user) -> RedirectResponse | None:
    """The other end of that handshake, wherever it lands.

    MyAnimeList redirects to a URI registered on ITS side, and the one
    registered for this app is the app's own root -- so the callback
    arrives at the library page rather than at a path of this module's
    choosing. Rather than teach that page about OAuth, it calls here, and
    this returns None for the overwhelming majority of requests, which
    carry no `code` at all. `/settings/tracking/myanimelist/callback`
    below is the same function, for an install that can register a
    dedicated path (cleaner, and the only difference is which URI is
    registered).
    """
    code = request.query_params.get("code")
    state = request.query_params.get("state")

    # An OAuth provider reports a REFUSAL by redirecting back with
    # `error=` and no code. Treating that as "no callback here" is what
    # made a failed sign-in look like nothing happening at all: on the
    # app's root you landed on the library page, and on the dedicated path
    # you were bounced to settings with no message, while MyAnimeList had
    # said exactly what was wrong. Its words are carried through and shown.
    failure = request.query_params.get("error")
    if failure and not code:
        detail = request.query_params.get("message") or failure
        return RedirectResponse(
            "/settings?tracker_error=refused&tracker_detail="
            + urllib.parse.quote(detail[:200]) + "#tracking",
            status_code=302,
        )

    if not code or not state:
        return None

    async with write_transaction() as wdb:
        verifier = await store.take_pending_oauth(wdb, user["id"], MYANIMELIST, state)
        await wdb.commit()
    if verifier is None:
        # Either no handshake was started here, or the state does not
        # match the one that was -- which is what makes a callback
        # somebody else pointed at this app refuse to be redeemed.
        return RedirectResponse("/settings?tracker_error=bad-callback#tracking", status_code=302)

    try:
        tokens = await myanimelist.exchange_code(code, verifier)
        username = await myanimelist.whoami(tokens["access_token"])
    except models.TrackerAuthError as exc:
        # Same reasoning: the service's own words, not a generic refusal.
        # "invalid_client: Client authentication failed" names a missing
        # client secret; "bad-callback" named nothing.
        return RedirectResponse(
            "/settings?tracker_error=refused&tracker_detail="
            + urllib.parse.quote(str(exc)[:200]) + "#tracking",
            status_code=302,
        )
    except models.TrackerError:
        return RedirectResponse("/settings?tracker_error=unreachable#tracking", status_code=302)

    async with write_transaction() as wdb:
        await store.save_account(
            wdb, user["id"], MYANIMELIST,
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token"),
            expires_at=service.expiry_for(tokens.get("expires_in")),
            username=username,
        )
        await wdb.commit()
    events.publish({"type": "account_updated"})
    return RedirectResponse("/settings#tracking", status_code=302)


@router.get("/settings/tracking/myanimelist/callback")
async def myanimelist_callback(request: Request, user=Depends(get_current_user)):
    answer = await handle_oauth_callback(request, user)
    return answer or RedirectResponse("/settings#tracking", status_code=302)


# ------------------------------------------------------------------ links


@router.get("/tracking/{tracker}/panel", response_class=HTMLResponse)
async def open_panel(
    tracker: str, request: Request,
    series_id: int | None = None, novel_id: int | None = None,
    user=Depends(get_current_user), db=Depends(get_db),
):
    """What an icon beside the title fetches when it is clicked.

    Read fresh every time rather than rendered with the page, and that is
    the whole reason this endpoint exists: the editor inside opens with
    the SERVICE's own numbers, and a copy baked into the page an hour ago
    would send yesterday's position back the moment somebody pressed Send.
    It is also work nobody has asked for until they click -- two network
    requests to a catalogue, on a page whose dialog is shut.
    """
    if models.tracker(tracker) is None:
        raise HTTPException(status_code=404, detail="unknown tracker")
    series_id, novel_id = _target(series_id, novel_id)
    state = await service.remote_state(
        user["id"], tracker, series_id=series_id, novel_id=novel_id)
    return await _panel(request, db, user["id"], series_id, novel_id, tracker,
                        tracker_remote=state["state"], tracker_remote_error=state["error"])


@router.post("/tracking/{tracker}/send", response_class=HTMLResponse)
async def send_to_tracker(
    tracker: str, request: Request,
    series_id: int | None = Form(None), novel_id: int | None = Form(None),
    chapter: str = Form(""), volume: str = Form(""), score: str = Form(""),
    status: str = Form(""), start_date: str = Form(""), finish_date: str = Form(""),
    user=Depends(get_current_user), db=Depends(get_db),
):
    """The editor's Send: exactly what is in the boxes, to this service.

    Every field arrives as a STRING and is parsed here rather than typed
    as a float in the signature, because an empty box is the difference
    between "leave this alone" and "set it to nothing", and FastAPI's
    coercion collapses both to a 422. Leaving a box empty must be the
    quiet option, not the one that fails the request.

    The same holds for the status and the two dates. The status must be
    one of THIS service's own values: the select offers exactly those,
    plus -- for an entry on a list it does not know, or on none -- a first
    option with an empty value, which leaves it where it is.
    """
    module = models.tracker(tracker)
    if module is None:
        raise HTTPException(status_code=404, detail="unknown tracker")
    series_id, novel_id = _target(series_id, novel_id)
    outcome = await service.send_manually(
        user["id"], tracker, series_id=series_id, novel_id=novel_id,
        chapter=models.number(chapter), volume=models.number(volume),
        score=models.score_in_range(score),
        status=status if status in dict(module.STATUSES) else None,
        start_date=models.iso_date(start_date) if module.KEEPS_DATES else None,
        finish_date=models.iso_date(finish_date) if module.KEEPS_DATES else None,
    )
    # Straight back out to the service: the panel that follows is an
    # editor showing what is there, and what is there is what was just
    # written. Reusing the values sent would show the answer this app
    # hoped for rather than the one it got.
    state = await service.remote_state(
        user["id"], tracker, series_id=series_id, novel_id=novel_id)
    if not is_htmx(request):
        return RedirectResponse(_back_to(series_id, novel_id), status_code=302)
    return await _panel(request, db, user["id"], series_id, novel_id, tracker,
                        tracker_outcome=outcome,
                        tracker_remote=state["state"], tracker_remote_error=state["error"])


@router.get("/tracking/{tracker}/search", response_class=HTMLResponse)
async def search_tracker(
    tracker: str, request: Request, q: str = "", all_types: bool = False,
    series_id: int | None = None, novel_id: int | None = None,
    user=Depends(get_current_user), db=Depends(get_db),
):
    """Search results, as the panel.

    An empty result list is NOT an error and is not drawn as one: a great
    deal of what this app holds is not in the catalogue, and the panel says
    so plainly and leaves the series untracked.

    By default the results are narrowed to the family this shelf holds --
    a comic library asks a catalogue that also indexes light novels, and
    the top hit for a manga's own title is regularly its novel. `all_types`
    is the way back out, because that division is the catalogue's opinion
    and it is sometimes wrong. The filter is applied HERE and not in the
    service modules: it is a property of what is being linked, and neither
    API is asked a narrower question, so the count below is what was
    dropped from the answer rather than what was never requested.
    """
    service_module = models.tracker(tracker)
    if service_module is None:
        raise HTTPException(status_code=404, detail="unknown tracker")
    series_id, novel_id = _target(series_id, novel_id)
    query = q.strip()
    results, message, dropped = [], None, 0
    if query:
        account = await store.get_account(db, user["id"], tracker)
        try:
            results = await service.search(service_module, account, user["id"], query)
        except (models.TrackerError, models.TrackerAuthError) as exc:
            message = str(exc)
        want = await _local_kind(db, series_id, novel_id)
        if want and not all_types:
            kept = [r for r in results if models.kind_family(r.kind) in (None, want)]
            # Counted rather than implied: with the filter on, "nothing
            # matches" and "eight results, all of the other kind" look
            # identical, and only one of them means the entry is not there.
            dropped = len(results) - len(kept)
            results = kept
    return await _panel(
        request, db, user["id"], series_id, novel_id, tracker,
        tracker_searched=tracker, tracker_results=results,
        tracker_query=query, tracker_message=message, tracker_default_query=query,
        tracker_all_types=all_types, tracker_filtered_out=dropped,
    )


@router.post("/tracking/{tracker}/link", response_class=HTMLResponse)
async def link_series(
    tracker: str, request: Request,
    remote_id: str = Form(...), remote_title: str = Form(...), remote_url: str = Form(...),
    series_id: int | None = Form(None), novel_id: int | None = Form(None),
    user=Depends(get_current_user), db=Depends(get_db),
):
    if models.tracker(tracker) is None:
        raise HTTPException(status_code=404, detail="unknown tracker")
    series_id, novel_id = _target(series_id, novel_id)
    async with write_transaction() as wdb:
        await store.save_link(
            wdb, user["id"], tracker, series_id=series_id, novel_id=novel_id,
            remote_id=remote_id, remote_title=remote_title, remote_url=remote_url,
        )
        await wdb.commit()
    # BEFORE the push, and this is the order that matters: the service may
    # already be ahead of this shelf, and the push refuses to send a
    # number lower than the service's -- so a shelf left behind would
    # never speak again. Catching up first means the push that follows
    # finds the two in agreement and sends nothing, which is the truth.
    # It also brings the entry's description over (see service.catch_up).
    caught_up = await service.catch_up(
        user["id"], tracker, series_id=series_id, novel_id=novel_id)
    if caught_up["marked"]:
        # What was just marked read is progress like any other, so every
        # other open screen has to hear about it -- one event for the
        # whole shelf, the same as "mark all read".
        events.publish(
            {"type": "series_progress_updated", "series_id": series_id}
            if series_id is not None
            else {"type": "novel_status_updated", "novel_id": novel_id, "status": None})
    _publish_card_changed(series_id, novel_id)

    # Linking something already partly read catches the service up at
    # once, and this one WAITS rather than queueing: somebody just pressed
    # a button and is looking at the panel it returns. Queued, that panel
    # renders before the push lands and says "Nothing sent yet" about work
    # already in flight (measured).
    outcome = await service.push(
        user["id"], series_id=series_id, novel_id=novel_id, tracker=tracker)
    # The panel that comes back is now the EDITOR -- this thing is linked --
    # so it has to carry what the service holds, exactly as opening the
    # dialog would. Measured in both browsers: without this the boxes
    # arrived empty over an entry already at chapter 12, and the first
    # Send moved the list and the rating while saying nothing about the
    # numbers the reader could see on the site.
    state = await service.remote_state(
        user["id"], tracker, series_id=series_id, novel_id=novel_id)
    if not is_htmx(request):
        return RedirectResponse(_back_to(series_id, novel_id), status_code=302)
    return await _panel(request, db, user["id"], series_id, novel_id, tracker,
                        tracker_outcome=outcome, tracker_caught_up=caught_up,
                        tracker_remote=state["state"], tracker_remote_error=state["error"],
                        tracker_link_changed=True,
                        # Only when the description actually CHANGED. The
                        # panel says "Description taken from X" in the same
                        # breath, and a page still showing the old text
                        # under a line claiming otherwise is worse than
                        # saying nothing -- so the two come from one flag.
                        tracker_summary=(
                            await _series_summary(db, series_id)
                            if caught_up["described"] else None))


@router.post("/tracking/{tracker}/unlink", response_class=HTMLResponse)
async def unlink_series(
    tracker: str, request: Request,
    series_id: int | None = Form(None), novel_id: int | None = Form(None),
    user=Depends(get_current_user), db=Depends(get_db),
):
    if models.tracker(tracker) is None:
        raise HTTPException(status_code=404, detail="unknown tracker")
    series_id, novel_id = _target(series_id, novel_id)
    async with write_transaction() as wdb:
        await store.delete_link(
            wdb, user["id"], tracker, series_id=series_id, novel_id=novel_id
        )
        await wdb.commit()
    _publish_card_changed(series_id, novel_id)
    if not is_htmx(request):
        return RedirectResponse(_back_to(series_id, novel_id), status_code=302)
    return await _panel(request, db, user["id"], series_id, novel_id, tracker,
                        tracker_link_changed=True)

