# Reading statistics

EPUBs and web novels share one estimated words-per-minute indicator. The rate uses
all valid activity from the new measurement system: total sampled words divided
by their corresponding minutes. Rereading contributes words and time again;
this measures reading activity rather than unique words or completion.

The combined text samples need at least **1,000 words, five minutes and three valid samples**.
Until then, Statistics shows **Collecting data**. Font changes, different screens
and paragraph fragments do not change the underlying word count. The counting
convention uses Unicode letter/number groups, internal apostrophes and individual
Han characters; it is an estimate, not dictionary segmentation for every language.

Normal page turns and continuous scrolling can produce pace samples. Jumps,
layout changes, unfinished passages and passages reaching their time limit do
not. Unfinished reading still contributes recorded time. Internally, traversed
words include confirmed passages, including capped passages and rereading;
only eligible passages contribute to pace. Soft hyphens do not split a word
in new measurements. The application cannot determine
whether every displayed word was understood or actually read.

For comics, **Comic pages** includes rereading rather than counting only distinct
pages completed. **Time per comic page** uses the same unit. Text and comic
pages are not added together as a single page count. Daily time retains all
formats; **Comic pages per day** covers only comics, including in its tooltips.
Periods without comics retain the chart with zero pages.

The three Reading indicators are **Estimated text pace**, **Time per comic
page** and **Days with reading**, in one compact row. They describe the full
recorded history. **Time read** appears beside the daily time chart's peak and
totals only the selected date window, including custom ranges. Its counted
time uses the same original limits as the chart. The daily charts share an
area through **Time read per day** and **Comic pages per day** tabs; the
selected tab stays active when the date window changes. Their scales remain
separate.

The help beside **Estimated text pace** summarizes the calculation, minimum
evidence and excluded samples. It uses the same hover, focus and tap help
bubbles as Settings.

## Time limits and devices

Hidden readers pause timing. Lack of touch, keyboard or scroll activity alone
does not stop the clock. In Settings, the text limit defaults to **180 seconds
per 300 words**, with a minimum of 30 seconds per passage. At this setting a
900-word passage allows nine minutes. Comics retain their separate page-visit
limit. Changing a setting affects new visits, preserving recorded history.

Only one device's session contributes at a time for an account, prioritizing
the device most recently used. Delayed submissions do not take priority over
a newer session. Partially overlapping visits do not supply a full-passage
pace sample.

Ongoing visits are saved periodically. Unconfirmed activity stays in the
originating browser and can be retried after reconnecting or reopening without
double counting. Keep the browser's site data to retain pending activity.
Storage failures and permanently rejected submissions produce a notice;
submission to the browser during unload is not treated as server confirmation.

## Existing history and remaining-time predictions

Previous recorded times retain their original limits. The old history does not
identify the words traversed and cannot be converted reliably to words per
minute. New word-measurement coverage combines EPUBs and web novels.
Recorded measurements remain part of history when their content is removed.

Text predictions use validated word/time samples from EPUBs and web novels.
The same minimum evidence applies: 1,000 sampled words, five minutes and three
valid samples. Until then, cards show **Collecting data**; legacy durations
remain visible in Statistics but cannot provide a word-based pace.

The initial rate comes from the account's measured text history. As valid
samples accumulate for a work, its own rate gradually replaces the historical
rate, using a prior worth at most 3,000 words from other works. The estimate
counts actual words ahead of the saved section, block and character position;
it does not interpret an XHTML byte percentage as a word percentage. Rereading
can contribute a valid pace sample again without making the remaining text
larger. Jumps, layout changes and capped passages do not supply a new pace.

An EPUB card covers that book. A web novel in Reading covers the current
volume; its library card covers unread chapters in the whole novel. Missing
chapter text shows **Content unavailable · retry**; an old position that cannot
identify words shows **Position unavailable**. **Collecting data** means only
insufficient pace evidence. **Calculating…** means a result is still pending,
and **Estimate failed · retry** indicates a failed request. Unavailable content
and failed requests can be retried by click or keyboard activation; a position
needs a new save from the reader. No estimate operation resets reading progress. Reading cards request a fresh estimate
after progress changes or new valid pace evidence, without replacing covers or
reloading the page.

Word inventories are derived caches under the installation's data directory;
they contain counts and offsets, not the source text. Valid pace totals are
cached in SQLite with a one-time backfill and updated with new ledger entries.
Recorded history and its original limits are preserved. Comic estimates retain
their existing page-based behavior. Predictions remain approximate: pace and
text difficulty can change during a book, and no fixed error bound is promised.

## Hyphenation and saved positions

Both text readers offer **Hyphenate this work**, with a language override.
These preferences are stored for that work and account on the current browser.
The reader override is off by default, preserving the book's original rules,
including any hyphenation already supplied by the EPUB.

When enabled, the reader uses the manual language override, the chapter's
language, or the EPUB's metadata language. With no language it does not enable
automatic hyphenation. The browser must have a dictionary for the language;
selecting a language does not install a dictionary. Code blocks retain manual
line breaking.

Hyphenation changes presentation without inserting characters into the source.
Saved positions remain section/block/UTF-16 character anchors, and changing the
setting reflows from that position. A visual hyphen at a paginated continuation
does not become part of copied text, word counts or passage validation.
After upgrading, reload reader tabs that were already open so their shared
paginator also uses the current version.

The displayed progress percentage keeps its existing chapter-size weighting;
the remaining-time estimate uses words, so the two need not decrease at the
same rate. Existing measurements are preserved, including word counts recorded
before the soft-hyphen correction; new passages use the corrected counting rule.

Library pages and card fragments render before any uncached word inventory is
prepared, including one-book shelves and small novel volumes. A changed reading
position may require a new inventory; that work never runs in the HTML request.
Their estimates fill through a serialized endpoint with a separate database
connection, keeping inventory work out of the interactive connection pool.
That separation does not isolate CPU work: warm endpoint requests also reuse
matching word counts instead of competing with navigation to repeat source work. Cache quota checks are amortized
across writes; the disposable cache can temporarily exceed its target by a small
write budget plus one inventory.

Exact remaining and total word counts are retained across reloads in an
account-scoped derived cache under `data/cache/reading-estimates.sqlite3`
(at most 4,096 snapshots). Each estimator reads the bounded snapshot index once.
Both the page and the authoritative endpoint multiply matching counts by the
current measured pace. A pace change therefore updates durations without
reopening EPUB inventories or fetching novel bodies. Unchanged visits perform
no inventory traversals or snapshot writes. Lightweight browser revalidation
remains enabled because inputs can change while a page is being generated.
Reading events and committed text replacement changes refresh visible estimates;
newly inserted card fragments also request their pending predictions.

Replacement rules are read once per series or novel within an estimator,
including empty rule sets. Books in the same series and volumes of the same
novel reuse those rules during that request. Each request builds a new estimator
for its account, so rule additions, edits and deletions remain visible on the
next request; rules are never shared between accounts or retained across visits.

A content, saved-position or replacement-rule change invalidates the counts;
completed content never resurrects an old result. Chapter revisions detect even
same-size content edits without reading novel source bodies. The cache contains
signatures and counts, not book text, and can be removed without affecting
reading history. Older duration-only snapshots remain visible while each scope
is upgraded once to exact counts. That first calculation can use CPU; subsequent
warm visits reuse its result. No cache deletion or history reset is required.

A series with some unknown estimates shows `~X known · N items without estimate`.
This is a partial total, not the duration of the entire series. Failed content
calculations log the account/item or novel/chapter ID and exception type at INFO
level, without file paths, book text or credentials. HTTP 200 for the estimate
endpoint means the request succeeded; its per-target `state` describes whether
the prediction is available. There is no persistent background calculation job.
