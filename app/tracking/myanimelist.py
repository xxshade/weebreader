"""The MyAnimeList API v2, and nothing else.

Same shape as mangaupdates.py -- no database, no app types -- but the
authentication could hardly be more different, and three of its quirks are
load-bearing:

  * OAuth2 with PKCE, and MAL supports only the `plain` challenge method,
    so the challenge IS the verifier. Nothing is lost by saying so: the
    point of PKCE here is that the code cannot be redeemed by whoever
    intercepts the redirect, and `plain` still requires the verifier this
    app kept to itself. It is what MAL accepts.
  * Each installation supplies its own public client ID. A client secret
    is sent only when its registered application requires one.
  * `redirect_uri` must match a URI registered on the MAL side EXACTLY,
    which is why it is a setting and not a value derived from the request:
    the app can be reached on a LAN address, through a reverse proxy, and
    by its public name, and only one of those is the registered one.

The list-status call REPLACES the record rather than patching it, so a
field this app is not moving has to be re-sent as it stands -- hence
`set_state` taking the RemoteProgress it is moving from. Getting that
wrong silently resets a score or a status the reader set on the site. It
is also why chapter, volume, score and status are written by ONE call
here: two calls would each re-send the other's fields from a `remote` that
the first had already made stale.
"""

import math
import secrets
import urllib.parse

import httpx

from app.config import settings
from app.tracking.models import (
    RemoteProgress,
    iso_date,
    SearchResult,
    TrackerAuthError,
    raise_for_status,
    send,
    number,
    plain,
    score_in_range,
    shorten,
)

NAME = "myanimelist"
LABEL = "MyAnimeList"
# What fits in the 26x16 box the icon beside a series title draws (see
# _tracker_icon.html). Here rather than in the template because it is a
# property of the SERVICE, next to the name it abbreviates.
SHORT = "MAL"

# MAL's own words for the two list statuses this app ever writes. Named
# rather than spelled out at each site, because "completed" is also the
# value this app stores for a reader's own status and for a work's
# publication status (app/status_labels.py) -- three unrelated columns
# sharing one word is exactly how a find-and-replace goes wrong.
READING = "reading"
COMPLETED = "completed"
ON_HOLD = "on_hold"
DROPPED = "dropped"
PLAN_TO_READ = "plan_to_read"

# MAL's five, as the editor offers them.
STATUSES = [
    (READING, "Reading"),
    (COMPLETED, "Completed"),
    (ON_HOLD, "On hold"),
    (DROPPED, "Dropped"),
    (PLAN_TO_READ, "Plan to read"),
]

# This app's statuses that HAVE one here. "Stopped reading" does not --
# it is neither on hold (coming back) nor dropped (not coming back) -- so
# it is not sent and the entry keeps whatever status it has.
STATUS_FOR = {
    "reading": READING,
    "completed": COMPLETED,
    "on_hold": ON_HOLD,
    "dropped": DROPPED,
    "plan_to_read": PLAN_TO_READ,
}

# The list-status record carries `start_date` and `finish_date`.
KEEPS_DATES = True

BASE_OAUTH_URL = "https://myanimelist.net/v1/oauth2"
BASE_API_URL = "https://api.myanimelist.net/v2"

# MAL answers 400 to a query over 64 characters.
MAX_QUERY = 64

# What a search needs to be pickable, and what a push needs to know.
SEARCH_FIELDS = "id,title,synopsis,main_picture,media_type,start_date,num_chapters,num_volumes"
# The two dates are NOT among my_list_status' default fields: they have to
# be named inside it, and the defaults (status, score, both counts) still
# come back alongside -- the same query Mihon's MAL tracker makes.
STATUS_FIELDS = "num_chapters,num_volumes,my_list_status{start_date,finish_date}"

TIMEOUT = httpx.Timeout(15.0)


def _client() -> httpx.AsyncClient:
    """Every request in this module goes through here, so the whole client
    can be exercised against an httpx.MockTransport."""
    return httpx.AsyncClient(timeout=TIMEOUT)


def _raise_for_status(response: httpx.Response) -> None:
    raise_for_status(response, LABEL)


async def _request(method: str, url: str, **kwargs) -> httpx.Response:
    return await send(_client, LABEL, method, url, **kwargs)


# ------------------------------------------------------------------ OAuth


def new_verifier() -> str:
    """A PKCE code verifier. MAL requires 43-128 characters; 64 random
    bytes of url-safe base64 lands at 86."""
    return secrets.token_urlsafe(64)[:128]


def authorize_url(state: str, verifier: str) -> str:
    """Where to send the browser. `code_challenge_method` is omitted
    because MAL defaults to `plain` and accepts nothing else."""
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": settings.myanimelist_client_id,
        "code_challenge": verifier,
        "state": state,
        "redirect_uri": settings.myanimelist_redirect_uri,
    })
    return f"{BASE_OAUTH_URL}/authorize?{query}"


async def exchange_code(code: str, verifier: str) -> dict:
    return await _token_request({
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": settings.myanimelist_redirect_uri,
    })


async def refresh(refresh_token: str) -> dict:
    return await _token_request({
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })


def _credentials() -> dict:
    """The client half of a token request.

    The SECRET is included only when configured. MAL issues one for most
    app types and then refuses the exchange with `invalid_client: Client
    authentication failed` without it; an app registered without one has
    it empty, and sending an empty value is not the same as sending none.
    Passed in the request body, which MAL accepts alongside HTTP Basic.
    """
    if not settings.myanimelist_client_id:
        raise TrackerAuthError("Configure APP_MYANIMELIST_CLIENT_ID for this installation.")
    credentials = {"client_id": settings.myanimelist_client_id}
    if settings.myanimelist_client_secret:
        credentials["client_secret"] = settings.myanimelist_client_secret
    return credentials


async def _token_request(form: dict) -> dict:
    form = {**_credentials(), **form}
    response = await _request("POST", f"{BASE_OAUTH_URL}/token", data=form)
    # A refused code or a dead refresh token comes back 400 here, not
    # 401 -- and it is still something only the reader can fix, so it
    # has to be told apart from a service having a bad day.
    if response.status_code in (400, 401):
        # MAL answers a refused exchange with a JSON body naming the
        # reason ("invalid_client", "Client authentication failed").
        # Repeating it is the difference between a reader who can fix
        # this and one staring at a button that does nothing.
        # Just the reason: the settings page frames it ("The service
        # refused the sign-in: ..."), and framing it here too read as
        # "refused the sign-in: MyAnimeList refused the sign-in: ...".
        raise TrackerAuthError(_reason(response))
    _raise_for_status(response)
    body = response.json() or {}
    if not body.get("access_token"):
        raise TrackerAuthError("MyAnimeList returned no access token")
    return body


def _reason(response: httpx.Response) -> str:
    try:
        body = response.json() or {}
    except ValueError:
        return f"HTTP {response.status_code}"
    parts = [str(body[k]) for k in ("error", "message", "hint") if body.get(k)]
    return " — ".join(parts) or f"HTTP {response.status_code}"


async def whoami(token: str) -> str:
    response = await _request("GET",
        f"{BASE_API_URL}/users/@me", headers={"Authorization": f"Bearer {token}"})
    _raise_for_status(response)
    return (response.json() or {}).get("name") or ""


# ----------------------------------------------------------------- manga


async def search(query: str, *, token: str | None = None) -> list[SearchResult]:
    """MAL's search needs a token: unlike MangaUpdates there is no
    anonymous catalogue read, so this is only reachable once connected."""
    if not token:
        raise TrackerAuthError("MyAnimeList search needs a connected account")
    params = {"q": query[:MAX_QUERY], "limit": 15, "nsfw": "true", "fields": SEARCH_FIELDS}
    response = await _request("GET",
        f"{BASE_API_URL}/manga", params=params,
        headers={"Authorization": f"Bearer {token}"})
    _raise_for_status(response)
    return [
        _search_result(row.get("node") or {})
        for row in (response.json() or {}).get("data") or []
    ]


async def describe(remote_id: str, *, token: str | None = None) -> str | None:
    """The entry's synopsis, in full.

    MAL has no anonymous catalogue read, so unlike MangaUpdates this needs
    the connected account's token -- and answers None without one rather
    than raising, because the caller is a series page that must render
    either way.
    """
    if not token:
        return None
    response = await _request("GET",
        f"{BASE_API_URL}/manga/{remote_id}", params={"fields": "synopsis"},
        headers={"Authorization": f"Bearer {token}"})
    if response.status_code == 404:
        return None
    _raise_for_status(response)
    return plain((response.json() or {}).get("synopsis")) or None


async def get_progress(token: str, remote_id: str) -> RemoteProgress | None:
    response = await _request("GET",
        f"{BASE_API_URL}/manga/{remote_id}", params={"fields": STATUS_FIELDS},
        headers={"Authorization": f"Bearer {token}"})
    _raise_for_status(response)
    body = response.json() or {}
    status = body.get("my_list_status")
    # No my_list_status means the entry is not on the reader's list at
    # all -- the same "not there yet" MangaUpdates answers with a 404.
    if not status:
        return None
    return RemoteProgress(
        chapter=number(status.get("num_chapters_read")),
        volume=number(status.get("num_volumes_read")),
        raw=status,
        status=status.get("status") or None,
        # Only a whole date is carried: MAL keeps "2019" or "2019-04"
        # for a vague entry, and neither fits a date box -- nor is
        # worth overwriting with a guessed day.
        start_date=iso_date(status.get("start_date")),
        finish_date=iso_date(status.get("finish_date")),
        # 0 is how MAL says "unrated", and it has to come back out as
        # None: carried through as a number it would read as a rating
        # of zero, and the editor would offer to re-send it.
        score=score_in_range(status.get("score")),
    )


async def get_state(token: str, remote_id: str) -> RemoteProgress | None:
    """Everything the editor shows.

    The same single request as `get_progress`: MyAnimeList keeps the
    score, the status and both counts on one list-status record, so there
    is nothing extra to fetch. It exists as its own name because the
    OTHER service cannot say that, and service.py must not have to know
    which one it is holding.
    """
    return await get_progress(token, remote_id)


async def ensure_listed(token: str, remote_id: str, status: str | None = None) -> None:
    """MAL has no separate "add to list": the first PATCH of a list status
    creates the record. So there is nothing to do here, and saying that in
    one place beats a special case at the call site."""
    return None


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
    """PUT /manga/{id}/my_list_status REPLACES the record, so everything
    worth keeping has to be re-sent -- the two dates included.

    `status` is the big one: omitted, an entry the reader had marked
    on-hold or dropped comes back as plan_to_read. `score` likewise -- a
    push that quietly zeroed a rating would be the tracker damaging data
    this app does not own, which is exactly what the never-regress rule
    exists to prevent. An entry with no record yet becomes `reading`,
    since something is being reported as read about it.

    `status`, `score` and the dates are what this app can SAY rather than
    just preserve, and all keep the same convention as the counts: None
    means "whatever is there", not "clear it".
    """
    existing = (remote.raw if remote and remote.raw else {}) or {}
    form = {
        "status": status or existing.get("status") or READING,
        "score": _score_for(score, existing),
    }
    if chapter is not None:
        form["num_chapters_read"] = int(chapter)
    if volume is not None:
        form["num_volumes_read"] = int(volume)
    for field, value in (("start_date", start_date), ("finish_date", finish_date)):
        value = value or existing.get(field)
        if value:
            form[field] = value
    if (chapter is None and volume is None and status is None and score is None
            and start_date is None and finish_date is None):
        # Nothing was actually asked for, so the record is left alone
        # rather than rewritten with the values it already holds: that
        # write would be a no-op on a good day and a way to lose a field
        # this app misread on a bad one.
        return

    response = await _request("PUT",
        f"{BASE_API_URL}/manga/{remote_id}/my_list_status", data=form,
        headers={"Authorization": f"Bearer {token}"})
    _raise_for_status(response)


def _score_for(score: float | None, existing: dict) -> int:
    """MAL scores are WHOLE numbers out of ten, so a rating this app holds
    as 8.5 has to become one. Rounded half UP rather than with round(),
    whose half-to-even turns 8.5 into 8 and 9.5 into 10 -- two identical
    gestures landing on different sides is not something anybody would
    guess from the box they typed it into."""
    if score is None:
        return existing.get("score", 0)
    return int(math.floor(float(score) + 0.5))


def entry_url(remote_id: str) -> str:
    return f"https://myanimelist.net/manga/{remote_id}"


def _search_result(node: dict) -> SearchResult:
    remote_id = str(node.get("id") or "")
    start = node.get("start_date") or ""
    return SearchResult(
        remote_id=remote_id,
        title=plain(node.get("title")) or "(untitled)",
        url=entry_url(remote_id),
        year=start[:4] or None,
        kind=(node.get("media_type") or "").replace("_", " ") or None,
        cover_url=(node.get("main_picture") or {}).get("medium")
        or (node.get("main_picture") or {}).get("large"),
        description=shorten(plain(node.get("synopsis"))),
    )

