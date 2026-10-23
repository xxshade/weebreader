"""What the two tracker clients have in common.

Each service module (`mangaupdates.py`, `myanimelist.py`) speaks only its
own API and returns these types, so `service.py` can push to either
without knowing which. The registry at the bottom is what turns a stored
`tracker_links.tracker` string back into the module that can act on it.

A module in TRACKERS provides:

    NAME, LABEL                             identity
    async search(query, *, token)           -> list[SearchResult]
    async get_progress(token, remote_id)    -> RemoteProgress | None
    async get_state(token, remote_id)       -> RemoteProgress | None
    async set_state(token, remote_id, *, chapter, volume, score,
                    status, start_date, finish_date, remote)
    async ensure_listed(token, remote_id, status=None)
                                            called when get_progress is None
    STATUSES                                [(value, label)], the service's
                                            own list/status vocabulary
    STATUS_FOR                              this app's status -> one of those
    KEEPS_DATES                             whether it records reading dates

`set_state` takes the RemoteProgress it is moving from because the two
services need different things from it: MangaUpdates has to be told which
list the entry is on, and MyAnimeList overwrites the whole list-status
record, so a field this app is not moving has to be re-sent as it stands
or the service reads its absence as a change.

It is ONE call and not one per field because of that same replacement:
MyAnimeList writes chapter, volume, score and status in a single PUT, and
splitting them would make the second call re-send what the first had just
changed from the `remote` it was handed, which is now stale -- setting a
score would silently walk the chapter count back. Every field is
independently optional, and None means "leave whatever is there alone".

`get_progress` is what a PUSH reads, and it is the cheap one: whatever
that service answers in a single request. `get_state` is what the EDITOR
reads, and it is allowed to cost more -- MangaUpdates keeps a rating
somewhere else entirely, so the editor pays for a second request that no
push should.
"""

import html
import re
from dataclasses import dataclass

import httpx


class TrackerAuthError(Exception):
    """The service refused the credentials, or the session has ended.

    Kept apart from every other failure because it is the one the reader
    can do something about, and the only one worth showing as "sign in
    again" rather than as "try later"."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class TrackerError(Exception):
    """Anything else: the service was unreachable, refused the request, or
    answered something unparseable."""


async def send(client_factory, label: str, method: str, url: str, **kwargs):
    """One request to a service, an unreachable one raised as TrackerError.

    The status is left to the caller: a 404 is "not listed" or "unrated" to
    some of them and a failure to the rest. client_factory is the module's
    own `_client`, the seam its tests put a MockTransport behind."""
    async with client_factory() as client:
        try:
            return await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise TrackerError(f"could not reach {label}: {exc}") from exc


def raise_for_status(response, label: str) -> None:
    if response.status_code in (401, 403):
        raise TrackerAuthError(f"{label} rejected the session", status_code=response.status_code)
    if response.status_code >= 400:
        raise TrackerError(f"{label} answered {response.status_code}")


@dataclass(frozen=True)
class SearchResult:
    remote_id: str
    title: str
    # The entry's own page. Rendered as a link so the right one can be
    # opened and checked before linking: two entries often differ only in
    # ways a title and a year cannot show.
    url: str
    # Shown beside the title so two entries with the same name can be told
    # apart -- which is the whole difficulty of picking the right one.
    year: str | None = None
    kind: str | None = None
    # The cover and the opening of the description, for the same reason.
    # A catalogue routinely answers in romaji, or with three entries whose
    # titles differ by a subtitle, and a cover settles it at a glance.
    cover_url: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class RemoteProgress:
    """What the service already records for an entry.

    `raw` carries whatever else that service's own update call has to
    round-trip (MyAnimeList's status and score, which its API replaces
    wholesale rather than patching).
    """

    chapter: float | None = None
    volume: float | None = None
    list_id: int | None = None
    raw: dict | None = None
    # The reader's own rating, 0-10, or None for unrated. NOT filled in by
    # `get_progress` on every service -- MangaUpdates keeps it behind
    # another request -- so only `get_state` promises it.
    score: float | None = None
    # Where the entry is, in the SERVICE's own vocabulary (a value from the
    # module's STATUSES): MyAnimeList's status word, MangaUpdates' list id
    # as a string. None when it said nothing.
    status: str | None = None
    # When the reader started and finished it, as YYYY-MM-DD. Only a
    # service with KEEPS_DATES fills these in.
    start_date: str | None = None
    finish_date: str | None = None


# Both services take a rating out of ten and both spell "no rating" as 0,
# which is why a score is stored and compared as None rather than as zero:
# sending 0 is how a rating is REMOVED, and it must not be what an empty
# box does by accident.
MAX_SCORE = 10


def score_in_range(value) -> float | None:
    """A rating, or None for anything that is not one.

    Out-of-range is None rather than clamped: a 50 typed into a box that
    takes ten is somebody meaning something else, and quietly recording 10
    would tell the service a rating it was never given.
    """
    parsed = number(value)
    if parsed is None or not 0 < parsed <= MAX_SCORE:
        return None
    return parsed


# Roughly a line and a half at the width the panel gives it. Enough to
# tell two entries apart, short enough that ten results stay scannable.
DESCRIPTION_CHARS = 220


def plain(value) -> str:
    """Both catalogues answer in HTML: titles come back entity-escaped
    (`&quot;`, `&amp;`, `&sim;`) and a synopsis carries `<br>` and
    formatting. Rendered as-is a title shows its entities and a description
    its tags, so the markup is dropped and the entities resolved -- once,
    and in this order, since unescaping first could turn `&lt;b&gt;` into a
    tag that the stripper would then eat."""
    if not value:
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(value))).split())


def shorten(text: str, limit: int = DESCRIPTION_CHARS) -> str | None:
    """Cut on a word boundary, so a description does not end mid-word."""
    if not text:
        return None
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip(" .,;:") + "…"


def number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# The two things a catalogue entry can be, as far as this app is able to
# tell them apart -- and as far as it needs to.
COMIC = "comic"
NOVEL = "novel"


def kind_family(kind) -> str | None:
    """Which family a search result's own type word belongs to.

    Both catalogues answer with a vocabulary of their own -- MangaUpdates
    says Manga, Manhwa, Manhua, Artbook, Doujinshi, Novel; MyAnimeList says
    manga, manhwa, one_shot, doujinshi, novel, light_novel -- and neither
    is a fixed list this app could keep in step. The one division that
    matters here is prose against pictures, and every word for prose in
    both vocabularies contains "novel", so that is the test rather than a
    table that would silently drop a word either service added later.

    None means the entry did not say what it is, which is kept in every
    filter: a result with nothing to judge it by is not evidence AGAINST
    it, and dropping it would hide the one entry that matters with no way
    to find out why.
    """
    if not kind:
        return None
    return NOVEL if "novel" in str(kind).lower() else COMIC


def iso_date(value) -> str | None:
    """A YYYY-MM-DD date, or None for anything that is not one -- a box
    left empty, a partial date MAL answers for a vague entry ("2019" or
    "2019-04"), or text typed into a browser with no date picker."""
    from datetime import date

    try:
        return date.fromisoformat(str(value)).isoformat() if value else None
    except ValueError:
        return None


def registry() -> dict:
    """Imported lazily: the service modules import this one for its types,
    so building the registry at import time would be a cycle."""
    from app.tracking import mangaupdates, myanimelist

    return {m.NAME: m for m in (mangaupdates, myanimelist)}


def tracker(name: str):
    """The module for a stored tracker name, or None for a row written by
    a version that knew a service this one does not."""
    return registry().get(name)
