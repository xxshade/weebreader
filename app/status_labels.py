"""Manual tracking-status vocabulary (MyAnimeList/AniList-style), shared by
series (library/router.py) and novels (novels/router.py) -- both use the
exact same set of values (also enforced by the CHECK constraints on
series_status/novel_status in schema.sql), so keeping one copy avoids the
two drifting apart."""

# The stored VALUES are what schema.sql's CHECK constraints, the saved
# filter selections and every hidden-statuses list already hold, so they
# do not move; only the words on screen do. "completed" reads as
# "Finished" because the publication vocabulary below has a "Completed" of
# its own, and the two appear as badges on the same cover -- one saying
# the reader finished it, the other saying the author did.
STATUS_LABELS = {
    "reading": "Reading",
    "completed": "Finished",
    "dropped": "Dropped",
    "stopped_reading": "Stopped reading",
    "on_hold": "On hold",
    "plan_to_read": "Plan to read",
}


# Where the WORK itself is in its publication, which is a different
# question from the tracking status above: one is "have I read this", the
# other is "is there more coming". They are deliberately separate
# vocabularies rather than more values on the same list -- a series can be
# both, either, or neither. They also share the VALUE "completed" while
# reading as "Finished" and "Completed" on screen, which is the point: the
# two are stored independently and only the labels have to tell a reader
# which of the two questions a badge is answering.
#
# It belongs to the series, not to a reader, so it is a column on `series`
# rather than a per-user table: whether a manga is still running is a fact
# about the manga.
PUBLICATION_STATUS_LABELS = {
    "ongoing": "Ongoing",
    "completed": "Completed",
    "hiatus": "Hiatus",
    "canceled": "Canceled",
}

# A web novel already carries this fact, scraped from its source page as
# free text ("Ongoing", "Completed", ...), so it is read rather than
# asked for. Anything the source words differently than these gets NO
# badge: guessing "ongoing" for a status we did not understand would put
# a confident label on the screen that nothing behind it supports.
_SOURCE_PUBLICATION_ALIASES = {
    "ongoing": "ongoing",
    "on going": "ongoing",
    "publishing": "ongoing",
    "releasing": "ongoing",
    "serializing": "ongoing",
    "completed": "completed",
    "complete": "completed",
    "finished": "completed",
    "hiatus": "hiatus",
    "on hiatus": "hiatus",
    "paused": "hiatus",
    "canceled": "canceled",
    "cancelled": "canceled",
    "dropped": "canceled",
    "discontinued": "canceled",
}


def publication_status_from_source(text: str | None) -> str | None:
    """Map a source site's own wording onto this vocabulary, or None."""
    if not text:
        return None
    return _SOURCE_PUBLICATION_ALIASES.get(" ".join(text.split()).lower())


def novel_publication_status(override: str | None, source_text: str | None) -> str | None:
    """What a web novel's publication badge should say.

    The reader's own answer wins, and NULL -- the ordinary state -- means
    "keep following the source page". A scraped novel already knows
    whether it is finished, so asking on every novel would be noise; what
    this exists for is the case the mapping cannot read. An unrecognised
    wording gets no badge rather than a guessed one, and this is how
    somebody says what the site failed to say.

    Only a value this vocabulary knows is honoured: a stale override from
    an older vocabulary falls back to the source rather than rendering as
    an unlabelled badge in the fallback grey.
    """
    if override in PUBLICATION_STATUS_LABELS:
        return override
    return publication_status_from_source(source_text)
