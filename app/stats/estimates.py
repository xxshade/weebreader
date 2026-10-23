"""How much longer this book will take, from the user's own reading speed.

Shown on the cards next to the percentage (see library/_reading_grid.html
and library/_item_card.html), and under the name of every series and novel
on the home grid (library/index.html). It is always the time LEFT of
whatever that card is about -- one item for a book or a comic, one volume
on the "Reading" strip's web-novel card, everything still unread for a
whole shelf entry -- which is also the whole of it for something not
started yet, so one number means the same thing on the strip and on a
series page full of unread volumes.

The SPAN is the caller's choice and the arithmetic is the same for all of
them: what is left of the thing in hand, plus every measured byte or page
behind it that has not been read. `for_item` answers for one item,
`total_remaining` sums that over a series, and `for_novel` takes the
unread bytes its caller decided on -- one volume for the strip, the whole
novel for the grid.

Everything here is derived from `page_reads`, so a brand-new install has no
basis for any of it. **That case is answered with None, never with a guess:**
a made-up estimate on every card would be worse than no estimate, because
there is nothing on screen to tell the reader which one it is. The templates
simply render nothing when this returns None.

Four sources of a rate, in order of how much they actually know:

1. **This book's own pace.** If time has been logged against it and the
   reader is far enough in, `spent / fraction` is the projected total, and
   the estimate is what is left of that. Nothing else can account for a
   book being unusually dense or unusually fast for this particular reader.
2. **Seconds per byte of TEXT, over everything of that format the reader
   has spent time on.** For a book not started yet, this is what scales the
   estimate to ITS length instead of handing every volume in a series the
   same number.

   It counts bytes actually READ, not only finished books -- a book you are
   30% into contributes 30% of its text and the time you spent on it. That
   distinction is the whole reason the estimate appears at all: page_reads
   only starts on the day the timing feature is installed, so a library
   whose books were all finished before that has no finished-and-timed book
   to learn from, and every card stays blank until the reader happens to
   finish a whole one. Reproduced exactly that way -- 42 minutes logged
   against a book at 30%, and every other book on the shelf still showing
   nothing.

   The earlier version restricted this to finished books, borrowing the
   rule from the statistics page's "how long does one volume take". That
   rule is right THERE and wrong here, and the difference is what is being
   averaged: a half-read volume genuinely cannot say how long a whole
   volume takes, but it is a perfectly good sample of a RATE -- it says "I
   read this many bytes in this many seconds", which is exactly the datum
   wanted. Finished books still contribute their whole text, so this is
   strictly more data, not different data.

   The unit is `items.text_size` -- the uncompressed byte total of the
   reading order -- and emphatically NOT the file's size on disk. An EPUB
   is a compressed zip that also carries images, fonts and a cover, so
   file size measures illustration and compression far more than prose:
   measured on two synthetic books, an art book of 2,400 words came to
   25 MB while a 240,000-word novel came to 18 KB. Scaling by file size
   would have called the art book 1382x the longer read; by text size it
   is correctly about a hundredth of it. See parsers/epub.spine_text_size.

   It is also the same unit the reader already weights the progress
   PERCENTAGE by, which is what keeps the estimate and the percentage on
   the card talking about the same book.
3. **Seconds per PAGE for a comic, learned within ONE SERIES.** A comic
   has no text to measure -- how much reading a page holds is inside the
   image -- so the unit is the page, and a page is only comparable to
   another page of the same series. Lettering density varies by more than
   an order of magnitude between series (a wordless art book against a
   dialogue-heavy one), so a library-wide seconds-per-page would describe
   neither and be confidently wrong on every card; within a series it is
   the same author, letterer and format throughout.

   A series nobody has read yet has no rate of its own, and borrows the
   95th PERCENTILE of the series rates that do exist (see
   COMIC_FALLBACK_PERCENTILE) -- the reader's own pace, taken from the slow
   end of their series rather than the middle, because an unread series
   could be any of them and an estimate that runs out before the volume
   does is the worse surprise. The moment the series has a rate of its
   own it switches to that. Only a reader who has no series rate at all
   gets no comic estimate, the same answer a brand-new install gets.

4. **The same thing for a web novel**, learned the same way, and spent on
   a stretch of chapters the CALLER picks rather than on the whole novel
   by default. The "Reading" strip asks about the VOLUME the reader is in,
   because a volume is the unit a novel is followed in -- it is what "mark
   all as read" acts on and what the statistics page measures, so "about
   2 hours left" there is a claim about this volume. The home grid asks
   about everything in the novel still unread, because that card is a
   shelf entry and the question in front of it is "how much of this is
   left". `for_novel` does not know which it was handed: both are the
   chapter in front of the reader plus a byte count of chapters they have
   not read.

   Every byte of that is measured. `chapters.content_size` is the length
   written beside each chapter's HTML when it was downloaded -- the very
   column the novel manifest reports as each chapter's `size`, kept there
   rather than measured per query, which on a TEXT column means reading
   every chapter this rate was ever learned from. The one gap is a chapter
   whose body has not been downloaded yet (a failed fetch, or one
   discovered since the last sync): it has no length, and it is sized by
   the AVERAGE of the measured chapters in the same stretch -- the
   volume's own mean on the strip, the novel's on the grid. That is a
   tight inference at either scope (same author, same serialisation
   cadence) and the alternative was worse: leaving it out understates the
   time by exactly that chapter, silently, and a number wrong downwards
   looks identical to a right one.

   The rate itself is still learned across whole novels, because a rate
   wants as much text as it can get; only what it is applied TO is that
   stretch. The chapter the reader is actually in keeps its own-pace path
   on top of that -- `page_reads` carries `chapter_id`, so "this chapter
   is heavy going for me" is knowable and worth more than any average.
   The unread chapters behind it have no pace to offer, which is exactly
   what makes them unread.

   The rate is learned SEPARATELY from the ebook one rather than shared,
   even though both are bytes of HTML: an EPUB chapter is a whole XHTML
   document and a scraped chapter is a run of `<p>` tags, so their markup
   overhead per word differs. A rate per kind cancels that out; one shared
   rate would bake the difference into every estimate.

   It is also learned chapter by chapter -- each chapter's seconds against
   that same chapter's bytes -- and NOT against the bytes implied by the
   novel's saved position. See the query for why: a novel is one row
   carrying a position built up over the whole history of reading it,
   while page_reads begins on the day timing was installed, so pairing the
   two divides a few chapters of time by dozens of chapters of text.
"""

from collections import namedtuple

# Which item formats get an estimate at all, and what each one is SCALED
# BY. A rate is seconds per unit of that thing, so the unit is the whole
# design: `text_size` is the uncompressed bytes of an EPUB's reading order,
# `page_count` is a comic's pages.
#
# The comic unit is a page and not its text, because a comic has no text
# size to measure -- how much reading a page holds is inside the image.
# What makes a page usable anyway is the SCOPE the rate is learned at:
# see COMIC_FORMAT below.
SIZE_COLUMN = {"epub": "text_size", "cbz": "page_count"}
ESTIMATED_FORMATS = tuple(SIZE_COLUMN)

# The one format whose rate is learned PER SERIES rather than across the
# library, and the reason the comic estimate can exist at all.
#
# Seconds per page is meaningless averaged over a whole shelf: a wordless
# art book and a dialogue-heavy series differ by more than an order of
# magnitude per page, so one global number describes neither and would be
# confidently wrong on every card. Within ONE series the lettering density
# is near enough constant -- it is the same author, letterer and format
# throughout -- so "how long a page of THIS takes" is a question the
# reader's own history with that series can answer.
#
# The cost of that scope is that a series nobody has read yet has no rate
# of its own. It borrows COMIC_FALLBACK_PERCENTILE of the others until it
# does.
COMIC_FORMAT = "cbz"

# What a comic series with no rate of its own is timed at: this percentile
# of the per-series rates the reader DOES have, one sample per series.
#
# Per series and not per volume, because the spread worth describing is
# between series (see COMIC_FORMAT) -- counted by volume, the one series
# read forty volumes deep would be most of the distribution. And the slow
# end rather than the median: an unread series could be as dense as the
# densest one read so far, and an estimate that runs out with pages still
# to go is the more misleading of the two errors. With one series read it
# is simply that series' rate; with two, most of the way to the slower.
#
# Only ever a stand-in. The first volume read far enough to be a sample
# (MIN_SAMPLE_FRACTION, MIN_SAMPLE_SECONDS) gives the series a rate of its
# own, and that replaces this on every card of it.
COMIC_FALLBACK_PERCENTILE = 0.95

# What counts as enough of a sample to learn anything from. Both floors are
# used in two places, because both places are asking the same question:
#
# - projecting a book's total from its OWN pace, where too small a fraction
#   divides by almost nothing and turns a couple of minutes into an absurd
#   total (30s at 1% projects 50 minutes; at 0.5%, an hour and a half);
# - and admitting a book into the seconds-per-byte RATE, where a book barely
#   opened contributes its seconds against almost no bytes and drags the
#   rate for every other book with it.
#
# Under either floor a book is simply not a sample: the own-pace path falls
# through to the rate, and the rate ignores the book.
MIN_SAMPLE_FRACTION = 0.05
MIN_SAMPLE_SECONDS = 60

# A namedtuple rather than a dict on purpose: the templates reach for
# `eta.remaining` and `eta.total`, and Jinja resolves attribute access with
# getattr BEFORE the mapping -- which is how `finish.items` silently became
# `dict.items` on the statistics page. A tuple has no such surface.
#
# `total` is the projected time for the WHOLE book, which is what
# library-events.js needs: a percentage arriving over SSE can be turned
# back into a remaining time without asking the server again.
#
# `rest` is time that percentage does NOT move -- the rest of a web novel's
# VOLUME, beyond the chapter the card is open at. The split is what keeps
# the live update working now that the two describe different spans: the
# percentage arriving over SSE is the CHAPTER's, so it scales `total` and
# leaves `rest` alone, and the moment the reader crosses into another
# chapter the event carries a new chapter_id and the card is re-fetched
# anyway. Zero for everything else, which makes `rest + total * (1 - f)`
# the same arithmetic it always was.
Estimate = namedtuple("Estimate", "remaining total rest", defaults=(0.0,))


class ReadingEstimator:
    """Built once per request, then asked about each card on the page.

    The per-book totals are loaded in full rather than per id: they are one
    row per thing the user has ever READ, which is bounded by the size of
    their library and is two numbers wide -- far cheaper than threading a
    list of ids through every template that renders a card, and it means
    the htmx endpoints that re-render a single card need no special case.
    """

    def __init__(self, *, spent_by_item, spent_by_chapter, rate_by_format,
                 rate_by_series, novel_rate, comic_fallback_rate=None):
        self.spent_by_item = spent_by_item
        self.spent_by_chapter = spent_by_chapter
        # Seconds per byte of text, one per format -- the library-wide
        # rate, which only the text formats use.
        self.rate_by_format = rate_by_format
        # Seconds per PAGE, one per series, and only for comics. See
        # COMIC_FORMAT for why the scope is the series and not the shelf.
        self.rate_by_series = rate_by_series
        # Seconds per page for a comic whose series has no rate yet. See
        # COMIC_FALLBACK_PERCENTILE.
        self.comic_fallback_rate = comic_fallback_rate
        self.novel_rate = novel_rate

    @staticmethod
    def _total_from_own_pace(spent, fraction):
        """The whole book, projected from what this reader has spent so far."""
        if (spent < MIN_SAMPLE_SECONDS
                or fraction < MIN_SAMPLE_FRACTION):
            return None
        return spent / fraction

    @staticmethod
    def _estimate(total, fraction, rest=0.0):
        return Estimate(remaining=rest + total * (1 - fraction), total=total, rest=rest)

    def for_item(self, item, progress) -> "Estimate | None":
        """Time left on one library item, or None if there is no basis."""
        if item["file_format"] not in ESTIMATED_FORMATS:
            return None
        fraction = 0.0
        if progress is not None:
            if progress["status"] == "completed":
                return None
            fraction = min(1.0, max(0.0, (progress["progress_percent"] or 0) / 100))
        if fraction >= 1:
            return None

        total = self._total_from_own_pace(self.spent_by_item.get(item["id"], 0), fraction)
        if total is None:
            total = self._total_from_rate(item)
            if total is None:
                return None
        return self._estimate(total, fraction)

    def _total_from_rate(self, item):
        """The whole item, from a learned rate times the item's own size.

        Which rate and which size both follow the format: a book is bytes
        of text against the library-wide rate for its format, a comic is
        pages against its OWN SERIES' rate (see COMIC_FORMAT), or the
        stand-in for one while the series has none. An item
        whose size was never measured -- a book the backfill could not
        open, a comic whose archive would not list -- gets no estimate
        rather than one scaled by the wrong thing.
        """
        fmt = item["file_format"]
        if fmt == COMIC_FORMAT:
            rate = self._comic_rate(item)
        else:
            rate = self.rate_by_format.get(fmt)
        size = item[SIZE_COLUMN[fmt]]
        if not rate or not size:
            return None
        return rate * size

    def _comic_rate(self, item):
        """The series' own seconds per page, else the stand-in."""
        return self.rate_by_series.get(item["series_id"]) or self.comic_fallback_rate

    def total_remaining(self, items) -> "float | None":
        """Seconds left across a whole shelf entry -- every item of one
        series -- or None when not one of them has a basis.

        Seconds rather than an `Estimate`, and deliberately: an Estimate
        carries the PROJECTED TOTAL so that a percentage arriving over SSE
        can be turned back into a remaining time without a round trip (see
        the namedtuple), and a series has no one percentage to scale. The
        home grid's card is re-fetched whole when its numbers change, so
        the only thing it needs is the number it prints.

        None and 0.0 are different answers and the caller must keep them
        apart. None is "nothing here can be estimated" -- a comic series
        when no comic series has a rate yet, so there is neither its own
        nor a stand-in -- and prints nothing, per the module docstring's
        rule that a guess is worse than a blank. An EMPTY `items` is None by the same
        route, which is how a series read to the end arrives here: its
        caller leaves finished items out entirely (see _series_reading),
        and a finished shelf is exactly the card with nothing to say.
        """
        total = None
        for item in items:
            # Both halves of the pair are the same row: the query joins
            # reading_progress to items, so the format and the size are on
            # one side of it and the status and the percentage on the
            # other. Same call the "Reading" strip makes (see
            # _reading_items), and a LEFT JOIN's NULL status reads as the
            # unread item it is.
            estimate = self.for_item(item, item)
            if estimate is None:
                continue
            total = (total or 0.0) + estimate.remaining
        return total

    def can_estimate(self, item) -> bool:
        """Whether this item COULD carry an estimate, ignoring how far in
        the reader is.

        This is what the series page's sort control asks (see
        can_sort_by_eta), and it deliberately does not consult the
        estimate itself: that is absent for an item already finished, so
        keyed off it the control would come and go as the series is read.
        For a comic it does have to consult the rate, because with no
        series rate anywhere there is neither its own nor a stand-in, and
        offering an order that cannot order anything is the same empty
        control, arrived at from the other side.
        """
        fmt = item["file_format"]
        if fmt not in ESTIMATED_FORMATS:
            return False
        if fmt == COMIC_FORMAT:
            return bool(self._comic_rate(item))
        # The same for a book: with no ebook rate yet, an unstarted book has
        # nothing to be ordered by, and the control ordered nothing.
        return bool(self.rate_by_format.get(fmt))

    def for_novel(self, chapter_id, chapter_size, percent,
                  rest_bytes=0) -> "Estimate | None":
        """Time left in a stretch of a web novel: the chapter in hand, plus
        `rest_bytes` of chapters behind it that have not been read.

        WHICH chapters those are is the caller's decision, and this cannot
        tell. The "Reading" strip hands it one VOLUME, the unit a novel is
        followed in -- what the statistics measure and what "mark all as
        read" acts on, so "about 2 hours left" there is about this volume.
        The home grid hands it the whole novel, because that card is a
        shelf entry. `percent` is the fraction of the CHAPTER either way
        (see chapter_percent in schema.sql), because that is the only
        position a novel stores.

        `chapter_id` may be None: a novel with no saved position has no
        chapter in hand at all, and is then `rest_bytes` alone.

        Both halves are MEASURED, not guessed: `chapters.content_size` is
        the byte length written beside each chapter's HTML when it was
        downloaded, and the rate is this reader's own seconds per byte.
        So this is the same number the per-chapter estimate always was,
        summed over more text -- if anything steadier, because an error in
        the rate averages out over more of it.

        The chapter in hand keeps its OWN-PACE path -- unchanged from when
        this estimate was only ever about that chapter. Nothing else can
        know that this particular chapter is heavy going for this reader,
        and it is the part of the volume they are looking at. The chapters
        behind it have no pace to offer (that is what makes them unread),
        so those are the rate.

        `rest_bytes` and `chapter_size` come from the caller, which is
        already grouping the chapters of its own stretch to build them (see
        _reading_items and _novel_reading); it is also where a chapter whose
        body has not been downloaded yet is sized by the average of the
        measured chapters in that same stretch.
        """
        fraction = min(1.0, max(0.0, (percent or 0) / 100))
        # A `chapter_size` of zero is the caller saying this chapter is
        # done (ticked off, see _reading_items), not that it could not be
        # measured -- so the own-pace path is skipped with it. Projecting a
        # total for a chapter with nothing left in it would put its whole
        # time back on a card that should be showing only what is behind.
        total = (self._total_from_own_pace(self.spent_by_chapter.get(chapter_id, 0), fraction)
                 if chapter_size else None)
        if total is None:
            total = self.novel_rate * chapter_size if (self.novel_rate and chapter_size) else 0.0
        rest = self.novel_rate * rest_bytes if (self.novel_rate and rest_bytes) else 0.0
        # Nothing measurable anywhere in the stretch -- no rate learned yet,
        # or not one byte of it downloaded. No estimate rather than a zero,
        # which would render as "~0s left" on something nobody has read.
        if not total and not rest:
            return None
        # A finished chapter with unread ones behind it still has the rest
        # of the stretch to go, which is the whole point -- so fraction >= 1
        # is NOT the giving-up condition it was when this estimate covered
        # one chapter. Nothing behind AND nothing left of the chapter is,
        # though: that is a stretch with no time in it, and "~0s left" is
        # not worth a line on a card.
        if fraction >= 1 and not rest:
            return None
        return self._estimate(total, fraction, rest)


# How much of each item the reader's logged seconds actually covered, one
# row per item. Used by every rate below.
#
# The seconds come from `item_read_totals`, which already holds exactly this
# sum -- one row per item, maintained by a trigger on page_reads (see
# schema.sql). Reading them from the spans instead meant re-aggregating the
# user's whole reading history every time a card needed an estimate, which
# is a cost that grows forever while the answer stays one number per item:
# measured at 200,000 spans, 224 ms to build the estimator against 2 ms,
# with both versions agreeing on every rate and every total.
# That is also why there is no GROUP BY here any more -- and so no aggregate
# around the CASE, which was only there to survive the grouping.
#
# For a completed row the fraction is completed_from_percent, NOT 100:
# "Mark as read" is a declaration (see reading_progress.mark_item_read) and
# it sets progress_percent to 100 from wherever the reader was, so trusting
# that here divides real seconds by a whole item and makes the reader look
# several times faster than they are -- on every card, since this rate is
# what every estimate for an unstarted item is built from. A NULL is a row
# completed before that was recorded and is taken at its word.
_PER_ITEM_FRACTION = """
    SELECT item_read_totals.item_id AS id,
           item_read_totals.seconds AS seconds,
           CASE WHEN reading_progress.status = 'completed'
                THEN MIN(1.0, MAX(0.0,
                     COALESCE(reading_progress.completed_from_percent, 100) / 100.0))
                ELSE MIN(1.0, MAX(0.0,
                     COALESCE(reading_progress.progress_percent, 0) / 100.0))
           END AS fraction
      FROM item_read_totals
 LEFT JOIN reading_progress
        ON reading_progress.item_id = item_read_totals.item_id
       AND reading_progress.user_id = item_read_totals.user_id
     WHERE item_read_totals.user_id = ?
"""


async def _fetch_all(db, sql, params=()):
    return await (await db.execute(sql, params)).fetchall()


async def build_estimator(db, user_id: int) -> ReadingEstimator:
    spent_by_item = {
        r["item_id"]: r["seconds"]
        for r in await _fetch_all(
            db, "SELECT item_id, seconds FROM item_read_totals WHERE user_id = ?", (user_id,))
    }
    # Per CHAPTER, not per novel: the estimate on a card is about the
    # chapter the reader is in, so its own-pace path needs the time spent
    # on that chapter alone.
    spent_by_chapter = {
        r["chapter_id"]: r["seconds"]
        for r in await _fetch_all(
            db, "SELECT chapter_id, seconds FROM chapter_read_totals WHERE user_id = ?", (user_id,))
    }

    # Seconds per unit of item actually READ -- bytes of text for a book,
    # pages for a comic. An item at 30% contributes 30% of its size and
    # the time spent on it; a finished one contributes all of it. See the
    # module docstring for why this is NOT restricted to finished items:
    # a rate is a different question from an average per item, and
    # restricting it left a whole library blank until the reader happened
    # to finish something after the timing feature was installed.
    #
    # Summed rather than averaged per item, so a long book weighs more
    # than a short one in setting the rate, which is what makes it a rate.
    # Items with no measured size are excluded from both sides of the
    # division so they cannot skew it, and so are ones barely started: a
    # couple of minutes against 1% of a book divides seconds by almost no
    # bytes and produces a wild rate.
    #
    # ONE pass, not one per format. The two rates divide by different
    # columns and are grouped at different scopes -- the library for a
    # book, the SERIES for a comic (see COMIC_FORMAT) -- but they are
    # built from the same per-item seconds-and-fraction, and running
    # _PER_ITEM_FRACTION twice meant aggregating the whole of page_reads
    # twice for it. Measured at 200,000 rows: 37 ms each, against 37 ms
    # for both together. So the grouping carries both keys and the split
    # happens below, where it costs a dict lookup.
    #
    # `size` is per format, which is why it cannot simply be summed here:
    # each row reports whichever of the two columns its own format is
    # scaled by, and an item whose size was never measured is left out by
    # the WHERE, not by a zero -- a zero would still carry its seconds
    # into the numerator.
    rate_by_format = {}
    rate_by_series = {}
    totals: dict = {}
    size_columns = {fmt: f"items.{column}" for fmt, column in SIZE_COLUMN.items()}
    measurable = " OR ".join(
        f"(items.file_format = '{fmt}' AND {column} > 0)" for fmt, column in size_columns.items()
    )
    for row in await _fetch_all(db, f"""
        SELECT items.file_format AS fmt,
               items.series_id AS series_id,
               SUM(per_item.seconds) AS seconds,
               SUM(CASE items.file_format
                        {' '.join(f"WHEN '{fmt}' THEN {column}" for fmt, column in size_columns.items())}
                   END * per_item.fraction) AS size
          FROM ({_PER_ITEM_FRACTION}) AS per_item
          JOIN items ON items.id = per_item.id
         WHERE ({measurable})
           AND per_item.fraction >= ? AND per_item.seconds >= ?
      GROUP BY items.file_format, items.series_id
    """, (user_id, MIN_SAMPLE_FRACTION, MIN_SAMPLE_SECONDS)):
        if not row["size"]:
            continue
        # A book is pooled across the library; a comic only within its own
        # series, and a comic with no series has no scope to be pooled in.
        key = ("series", row["series_id"]) if row["fmt"] == COMIC_FORMAT else ("format", row["fmt"])
        if key[1] is None:
            continue
        seconds, size = totals.get(key, (0.0, 0.0))
        totals[key] = (seconds + row["seconds"], size + row["size"])
    # Removed items teach the same rate (see removed_items in schema.sql):
    # a reader who reads a volume and then deletes it would otherwise
    # leave the next, unread volume of that series with nothing of its
    # own to go on. Their seconds, fraction and size were kept when they
    # went, under the same rules as above. A comic is scoped to whatever
    # series lives in its folder NOW -- the series row may have gone and
    # come back with a new id. One that has not come back has nothing of
    # its own left to estimate -- but its pace is still the reader's pace
    # on a comic, and it still belongs in the stand-in every unread series
    # borrows: read a whole series, delete it, and without this the
    # fallback went with it, leaving every other comic with no estimate.
    removed_size = " ".join(
        f"WHEN '{fmt}' THEN removed_items.{column}" for fmt, column in SIZE_COLUMN.items())
    for row in await _fetch_all(db, f"""
        SELECT fmt, series_id, library_id, folder_path,
               SUM(seconds) AS seconds, SUM(size * fraction) AS size
          FROM (SELECT removed_items.file_format AS fmt,
                       series.id AS series_id,
                       removed_items.library_id AS library_id,
                       removed_items.folder_path AS folder_path,
                       removed_item_reads.seconds AS seconds,
                       CASE removed_items.file_format {removed_size} END AS size,
                       CASE WHEN removed_item_reads.status = 'completed'
                            THEN MIN(1.0, MAX(0.0,
                                 COALESCE(removed_item_reads.completed_from_percent, 100) / 100.0))
                            ELSE MIN(1.0, MAX(0.0,
                                 COALESCE(removed_item_reads.progress_percent, 0) / 100.0))
                       END AS fraction
                  FROM removed_item_reads
                  JOIN removed_items ON removed_items.id = removed_item_reads.removed_item_id
             LEFT JOIN series ON series.library_id = removed_items.library_id
                             AND series.folder_path = removed_items.folder_path
                 WHERE removed_item_reads.user_id = ?)
         WHERE size > 0 AND fraction >= ? AND seconds >= ?
      GROUP BY fmt, series_id, library_id, folder_path
    """, (user_id, MIN_SAMPLE_FRACTION, MIN_SAMPLE_SECONDS)):
        if not row["size"]:
            continue
        if row["fmt"] != COMIC_FORMAT:
            key = ("format", row["fmt"])
        elif row["series_id"] is not None:
            key = ("series", row["series_id"])
        elif row["folder_path"] is not None:
            key = ("gone", (row["library_id"], row["folder_path"]))
        else:
            continue
        seconds, size = totals.get(key, (0.0, 0.0))
        totals[key] = (seconds + row["seconds"], size + row["size"])

    # A series no longer on disk has a rate that only the fallback uses.
    gone_series_rates = []
    for (kind, scope), (seconds, size) in totals.items():
        if not size:
            continue
        if kind == "gone":
            gone_series_rates.append(seconds / size)
        else:
            (rate_by_series if kind == "series" else rate_by_format)[scope] = seconds / size

    # The same rate, for web novels. Learned apart from the ebook rate
    # because the two kinds of HTML carry different markup overhead per
    # word, and learned CHAPTER BY CHAPTER: each chapter's seconds are
    # paired with that same chapter's own bytes.
    #
    # The obvious-looking alternative -- seconds against the bytes implied
    # by the novel's saved position -- is wrong, and wrong by a factor
    # that grows with how long the novel has been followed. A novel is one
    # row holding one position accumulated over the whole history of
    # reading it; page_reads only starts on the day the timing feature was
    # installed. Someone 40 chapters into a novel with two chapters' worth
    # of recorded time had those 600 seconds divided by 40 chapters of
    # text, and the card offered to read the next chapter in 15 seconds --
    # measured at 20x too fast, and worse the further into the novel.
    #
    # Items never had this: an item's rate pairs an item's seconds with
    # that same item's bytes and progress, and a volume is small enough
    # that having its time means having most of it. The novel side simply
    # was not written the same way. It is now -- a chapter is the unit
    # with both halves of the ratio measured over the same span.
    #
    # A chapter counts for as much of it as was actually read: its
    # `read_from_percent` once marked (~100 when the reader crossed out of
    # it, 0 when it was ticked off a list -- see set_read_status), or its
    # chapter_percent while it is the one being read. A chapter with time but neither of those was
    # abandoned partway with no record of where, so there is no fraction
    # to pair its seconds with and it is left out.
    #
    # Its seconds come from `chapter_read_totals` for the same reason the
    # item side reads item_read_totals: one row per chapter already
    # totalled, instead of re-summing every span ever recorded against a
    # novel. The novel a chapter belongs to is taken from `chapters` rather
    # than from the span's own novel_id -- the join to `chapters` is needed
    # for content_size anyway, and chapters.novel_id is NOT NULL where
    # page_reads.novel_id goes NULL the moment a novel is deleted.
    novel_seconds = novel_bytes = 0.0
    for r in await _fetch_all(db, """
        SELECT SUM(per_chapter.seconds) AS seconds,
               SUM(per_chapter.size * per_chapter.fraction) AS bytes
          FROM (SELECT chapter_read_totals.seconds AS seconds,
                       chapters.content_size AS size,
                       CASE
                           WHEN novel_read_chapters.chapter_id IS NOT NULL
                               THEN MIN(1.0, MAX(0.0,
                                    COALESCE(novel_read_chapters.read_from_percent, 100) / 100.0))
                           WHEN novel_reading_progress.chapter_id = chapter_read_totals.chapter_id
                               THEN MIN(1.0, MAX(0.0,
                                    COALESCE(novel_reading_progress.chapter_percent, 0) / 100.0))
                           ELSE 0.0
                       END AS fraction
                  FROM chapter_read_totals
                  JOIN chapters ON chapters.id = chapter_read_totals.chapter_id
             LEFT JOIN novel_read_chapters
                    ON novel_read_chapters.chapter_id = chapter_read_totals.chapter_id
                   AND novel_read_chapters.user_id = chapter_read_totals.user_id
             LEFT JOIN novel_reading_progress
                    ON novel_reading_progress.novel_id = chapters.novel_id
                   AND novel_reading_progress.user_id = chapter_read_totals.user_id
                 WHERE chapter_read_totals.user_id = ?
                   AND chapters.content_size > 0) AS per_chapter
         WHERE per_chapter.fraction >= ? AND per_chapter.seconds >= ?
    """, (user_id, MIN_SAMPLE_FRACTION, MIN_SAMPLE_SECONDS)):
        novel_seconds = r["seconds"] or 0.0
        novel_bytes = r["bytes"] or 0.0
    novel_rate = (novel_seconds / novel_bytes) if novel_bytes else None

    return ReadingEstimator(
        spent_by_item=spent_by_item, spent_by_chapter=spent_by_chapter,
        rate_by_format=rate_by_format, rate_by_series=rate_by_series,
        novel_rate=novel_rate,
        comic_fallback_rate=percentile(list(rate_by_series.values()) + gone_series_rates,
                                       COMIC_FALLBACK_PERCENTILE),
    )


def percentile(values, q: float) -> "float | None":
    """The `q` quantile of `values`, interpolating between the two samples
    either side of it (numpy's default, "linear"), or None for no samples.

    Interpolated rather than nearest-rank because the samples here are few
    -- one per comic series read -- and nearest-rank on a handful of them
    is just the maximum, jumping to a new series' rate outright the moment
    it is added.
    """
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * q
    below = int(position)
    above = min(below + 1, len(ordered) - 1)
    return ordered[below] + (ordered[above] - ordered[below]) * (position - below)
