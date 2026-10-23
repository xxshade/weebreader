"""The MangaUpdates API, and nothing else.

No database, no app types: it takes a token and returns the dataclasses in
models.py, so it can be exercised against a fake transport without a
server or a schema.

Endpoints used (api.mangaupdates.com/v1), all of which the Suwayomi client
this was modelled on uses the same way:

    PUT  /account/login          username + password -> session token
    POST /series/search          catalogue search
    GET  /lists/series/{id}      what the account already records
    POST /lists/series           add the series to a list
    POST /lists/series/update    move the reader's position, and its list
    GET  /series/{id}/rating     the reader's own rating
    PUT  /series/{id}/rating     set it
    DEL  /series/{id}/rating     remove it

A rating lives at its OWN endpoint here rather than on the list entry,
which is the whole reason `get_state` exists apart from `get_progress`:
reading one costs a second request, and a push -- which happens every time
a page is turned -- must not pay for something it never sends.

The bearer token is the ONLY credential kept. The password is sent once, by
`authenticate`, in the request that trades it for that token, and is never
written down -- see store.save_account.
"""

import json
from dataclasses import replace

import httpx

from app.tracking.models import (
    RemoteProgress,
    SearchResult,
    TrackerAuthError,
    raise_for_status,
    send,
    number,
    plain,
    shorten,
)

NAME = "mangaupdates"
LABEL = "MangaUpdates"
# What fits in the 26x16 box the icon beside a series title draws (see
# _tracker_icon.html). Here rather than in the template because it is a
# property of the SERVICE, next to the name it abbreviates.
SHORT = "MU"

BASE_URL = "https://api.mangaupdates.com/v1"

# MangaUpdates answers 415 to application/json on these endpoints; the API
# is JSON:API-flavoured and wants this exact type.
CONTENT_TYPE = "application/vnd.api+json"

# The list a series goes on. MangaUpdates models "am I reading this" as
# WHICH LIST the series is on rather than as a status field, so moving
# between them is how a status changes.
READING_LIST = 0
WISH_LIST = 1
COMPLETE_LIST = 2
UNFINISHED_LIST = 3
ON_HOLD_LIST = 4

# The five standard lists, as the editor offers them. Values are strings
# because that is what a <select> posts and what RemoteProgress.status
# carries for both services; set_state turns one back into an id.
STATUSES = [
    (str(READING_LIST), "Reading list"),
    (str(WISH_LIST), "Wish list"),
    (str(COMPLETE_LIST), "Complete list"),
    (str(UNFINISHED_LIST), "Unfinished list"),
    (str(ON_HOLD_LIST), "On hold list"),
]

# This app's statuses (status_labels.py) that HAVE a list here. "Stopped
# reading" has none of its own -- "Unfinished" is already Dropped's -- so
# it is not sent, and the entry stays on whatever list it is on.
STATUS_FOR = {
    "reading": str(READING_LIST),
    "plan_to_read": str(WISH_LIST),
    "completed": str(COMPLETE_LIST),
    "dropped": str(UNFINISHED_LIST),
    "on_hold": str(ON_HOLD_LIST),
}

# A list entry here has no start or finish date, only a list and two
# counts, so the editor offers no date boxes for this service.
KEEPS_DATES = False

TIMEOUT = httpx.Timeout(15.0)


def _client() -> httpx.AsyncClient:
    """Every request in this module goes through here.

    One seam, so the whole client can be exercised against an
    httpx.MockTransport -- which is the only way to test what this code
    actually does (which field it sends, what it makes of a 404) without an
    account and without the network. The alternative was asserting on the
    functions' own arguments, which tests the test.
    """
    return httpx.AsyncClient(timeout=TIMEOUT)


def _raise_for_status(response: httpx.Response) -> None:
    raise_for_status(response, LABEL)


async def _request(method: str, url: str, **kwargs) -> httpx.Response:
    return await send(_client, LABEL, method, url, **kwargs)


async def authenticate(username: str, password: str) -> tuple[str, str]:
    """Trades a username and password for a session token.

    Returns (token, username as the service spells it). The password does
    not leave this function.
    """
    response = await _request("PUT",
        f"{BASE_URL}/account/login",
        content=_json({"username": username, "password": password}),
        headers={"Content-Type": CONTENT_TYPE},
    )
    _raise_for_status(response)
    context = (response.json() or {}).get("context") or {}
    token = context.get("session_token")
    if not token:
        raise TrackerAuthError("MangaUpdates returned no session token")
    return token, context.get("username") or username


async def search(query: str, *, token: str | None = None) -> list[SearchResult]:
    """Catalogue search. Unauthenticated works, so this is also what makes
    "is this even on MangaUpdates?" answerable before connecting."""
    headers = {"Content-Type": CONTENT_TYPE}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = await _request("POST",
        f"{BASE_URL}/series/search",
        content=_json({"search": query, "perpage": 15}),
        headers=headers,
    )
    _raise_for_status(response)
    return [_search_result(row.get("record") or {}) for row in (response.json() or {}).get("results") or []]


async def describe(remote_id: str, *, token: str | None = None) -> str | None:
    """The entry's own description, in full.

    A separate call from `search`, which carries only the first line or so
    -- enough to tell two hits apart, and not what you would want on the
    series page. Unauthenticated like the search, since the catalogue is
    public.

    Answers None rather than raising when there is nothing there: a series
    with no description on the site is an ordinary state, and a series
    page must not fail to load because a tracker had nothing to say.
    """
    headers = {"Content-Type": CONTENT_TYPE}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = await _request("GET", f"{BASE_URL}/series/{_int(remote_id)}", headers=headers)
    if response.status_code == 404:
        return None
    _raise_for_status(response)
    return plain((response.json() or {}).get("description")) or None


async def get_progress(token: str, remote_id: str) -> RemoteProgress | None:
    """What the account already records, or None when the series is not on
    any of the reader's lists yet.

    A push consults this rather than trusting what this app last sent: the
    reader may have moved the entry on the site, and overwriting a higher
    number with ours would silently undo that.
    """
    response = await _request("GET",
        f"{BASE_URL}/lists/series/{remote_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    # Not on a list yet is a normal answer, not a failure.
    if response.status_code == 404:
        return None
    _raise_for_status(response)
    body = response.json() or {}
    status = body.get("status") or {}
    list_id = body.get("list_id")
    return RemoteProgress(
        chapter=number(status.get("chapter")),
        volume=number(status.get("volume")),
        list_id=list_id,
        # A status is not a field here: it is WHICH LIST the entry
        # sits on.
        status=str(list_id) if list_id is not None else None,
    )


async def get_state(token: str, remote_id: str) -> RemoteProgress | None:
    """Everything the editor shows, which here means a SECOND request.

    A rating and a list position are two different resources on this
    service, and the rating survives the entry not being on a list at all
    -- so a rating with no list entry is answered rather than thrown away
    with the None, or the editor would open with an empty box over a
    rating the reader can see on the site.
    """
    progress = await get_progress(token, remote_id)
    score = await get_rating(token, remote_id)
    if progress is None:
        return None if score is None else RemoteProgress(score=score)
    return replace(progress, score=score)


async def get_rating(token: str, remote_id: str) -> float | None:
    """The reader's own rating, or None when they have not given one.

    404 is the ordinary answer for unrated, exactly as it is for a series
    on no list -- not a failure.
    """
    response = await _request("GET",
        f"{BASE_URL}/series/{_int(remote_id)}/rating",
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code == 404:
        return None
    _raise_for_status(response)
    return number((response.json() or {}).get("rating"))


async def set_rating(token: str, remote_id: str, score: float | None) -> None:
    """A rating, or None to take it away.

    DELETE rather than a rating of 0: this API has a verb for removing one,
    and 0 is not in the range it accepts.
    """
    url = f"{BASE_URL}/series/{_int(remote_id)}/rating"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": CONTENT_TYPE}
    if score is None:
        response = await _request("DELETE", url, headers=headers)
    else:
        response = await _request(
            "PUT", url, content=_json({"rating": round(float(score), 1)}), headers=headers)
    # Removing a rating that was never there is not a failure: the
    # state asked for is the state that already holds.
    if score is None and response.status_code == 404:
        return
    _raise_for_status(response)


async def ensure_listed(token: str, remote_id: str, status: str | None = None) -> None:
    """Puts the series on one of the reader's lists, which is what makes
    its position writable at all -- straight onto the list it is headed
    for when there is one, rather than onto Reading and then moved."""
    list_id = _list_id(status)
    await _post_list(token, "/lists/series", [{
        "series": {"id": _int(remote_id)},
        "list_id": READING_LIST if list_id is None else list_id,
    }])


async def set_state(
    token: str,
    remote_id: str,
    *,
    chapter: float | None = None,
    volume: float | None = None,
    score: float | None = None,
    status: str | None = None,
    start_date: str | None = None,
    finish_date: str | None = None,
    remote: RemoteProgress | None = None,
) -> None:
    """Moves the reader's position, their list, and their rating.

    `start_date` and `finish_date` are accepted and ignored: a list entry
    here has nowhere to keep them (KEEPS_DATES), and service.py must not
    have to know which service it is holding.

    Only the field that was asked for is sent: a series numbered by volume
    has no chapter number to report, and sending 0 for it would tell the
    service the reader had read none. This API PATCHES -- an omitted field
    keeps whatever it held -- so `remote` is consulted only for which list
    the entry is on.

    A status here is a MOVE between lists rather than a field, which is
    why it goes in the same call as the numbers: the update endpoint takes
    a `list_id`, and sending the new one is the documented way to change
    it. The call is made even with no numbers to send, which is what "I
    finished this" alone has to be able to do.

    The RATING is a separate request because it is a separate resource on
    this service. It goes second, so a failed list update does not leave
    behind a rating for a position that never moved -- the opposite order
    would have to be undone, and nothing here can undo anything.
    """
    # MangaUpdates calls the two COUNTS "status"; the list is `list_id`.
    counts: dict[str, int] = {}
    if chapter is not None:
        counts["chapter"] = int(chapter)
    if volume is not None:
        counts["volume"] = int(volume)

    list_id = _list_for(status, remote)
    moving_list = _list_id(status) is not None and (remote is None or remote.list_id != list_id)
    if counts or moving_list:
        await _post_list(
            token,
            "/lists/series/update",
            [{"series": {"id": _int(remote_id)}, "list_id": list_id, "status": counts}],
        )
    if score is not None:
        await set_rating(token, remote_id, score)


def _list_id(status: str | None) -> int | None:
    """A status value (a list id as a string) back to the id, or None for
    "say nothing". A custom list is a number too, so any integer passes."""
    try:
        return int(status) if status not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _list_for(status: str | None, remote: RemoteProgress | None) -> int:
    """Which list the entry should be on after this write.

    None means nothing was said about where it belongs, so whatever list it
    is already on is kept -- that is an ordinary push of a number, which
    must not move a series the reader filed under "on hold" back to
    "reading".
    """
    list_id = _list_id(status)
    if list_id is not None:
        return list_id
    return remote.list_id if remote and remote.list_id is not None else READING_LIST


async def _post_list(token: str, path: str, payload: list[dict]) -> None:
    response = await _request("POST",
        f"{BASE_URL}{path}",
        content=_json(payload),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": CONTENT_TYPE,
        },
    )
    _raise_for_status(response)


def _search_result(record: dict) -> SearchResult:
    remote_id = str(record.get("series_id") or "")
    image = (record.get("image") or {}).get("url") or {}
    return SearchResult(
        remote_id=remote_id,
        title=plain(record.get("title")) or "(untitled)",
        url=record.get("url") or f"https://www.mangaupdates.com/series/{remote_id}",
        year=str(record.get("year")) if record.get("year") else None,
        kind=record.get("type"),
        cover_url=image.get("thumb") or image.get("original"),
        description=shorten(plain(record.get("description"))),
    )


def _int(value) -> int | str:
    """MangaUpdates series ids are numeric; a non-numeric one is passed
    through rather than crashing the push, so a bad row fails at the
    service with a message instead of here with a traceback."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _json(payload) -> bytes:
    return json.dumps(payload).encode()
