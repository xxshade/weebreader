# Reading statistics and estimated text pace

Status: implementation authorized by the user after Q1–Q14.

## Findings from the current implementation

- `app/stats/queries.py` counts `page_reads` rows as pages. Those rows are
  timed visits, including repeated visits to the same position.
- `reader-text.js` reports the topmost block without its character offset to
  `reading-time.js`. Several reading pages within one paragraph can share one
  timer and one cap, undercounting time when their combined duration is capped.
- Hidden tabs stop timing, but visible inactivity is counted up to the cap.
  Concurrent devices can both contribute time.
- The periodic flush sends ended spans, not the currently running span.
  Failed requests and unsuccessful beacon submissions can lose activity.
- EPUB sizes measure XHTML bytes; web novel sizes measure HTML characters.
  Neither is a word count. Historical rows lack the passages and offsets needed
  to reconstruct reliable words-per-minute measurements.
- `app/stats/estimates.py` can pair newly recorded time with older absolute
  progress. Its remaining-time estimates can therefore be distorted by prior
  progress, jumps and rereading. This finding is documented; replacing that
  estimator is explicitly outside this implementation.

## Agreed behavior

Audit recorded time, statistics and remaining-time estimates. Implement the
recording/statistics changes below while preserving the remaining-time
estimator's algorithm. Correcting its recorded-time inputs may indirectly
change its outputs; the new words-per-minute metric must not become its input.

Replace time-per-page statistics for EPUBs and web novels with estimated words
per minute. Combine their words and corresponding time into one text-reading rate. For comics, retain a time-per-visit
metric with an explicit label. Replace the mixed total Pages with separate
text words traversed and comic page visits; do not silently relabel historical
text visits as words or unique pages.

Use all valid samples collected by the new measurement, not a rolling window.
Compute `60 * total_sample_words / total_sample_seconds`, not an unweighted
average of individual speeds. Require at least 1,000 words, 300 seconds and
three valid samples across EPUBs and web novels before displaying the combined rate.
Until then, show insufficient evidence rather than an invented speed.

Measure normal navigation in paginated and continuous EPUB/web-novel reading.
Pair traversed text with its corresponding time. Include reread passages with
their own time; words traversed are not unique words or book completion.
Exclude index/progress jumps and reflow from pace samples. A change of font,
orientation or screen size must not itself create credited words.
An unfinished passage may retain recorded time without supplying a pace sample.
Use a consistent definition of words in readable text, excluding HTML markup
and non-reading content; do not estimate words from bytes or rendered pages.

Retain legacy recorded times and their historical cap semantics. Start the
word-based measurement with new activity and make its coverage clear. Do not
fabricate retrospective pace. Preserve recorded activity when content is
removed, following the existing historical-data contract.

Do not pause solely because no touch, scroll or key event occurred. Continue
to pause when the reader is hidden. Add a separate configurable text limit:
180 seconds per 300 words, with a 30-second minimum for a passage. For example,
900 words allow 540 seconds at the default setting. Keep the existing comic
page limit. Exclude capped passages from pace estimation; capped time must
never inflate the estimated speed. Words counted for the total and words
eligible for pace must be distinguished where eligibility differs.

Only one reading session per account contributes at a time; using another
device gives it priority. Keep the words and time for pace in the same session.
Delayed submission alone must not take session priority or double-count
overlapping activity. Preserve the configured passage cap across checkpoints,
retries and hide/show cycles within that passage's timed visit.

Save ongoing measured activity periodically and retain unconfirmed submissions
in the originating browser across reconnection and reopening. Retry safely
without duplicates. Checkpoints are updates to the same activity, not extra
page visits or independent pace samples. Pending data remains associated with
its original account; another login must not receive it. Browser storage loss
or permanent server-side rejection must not be reported as successful delivery.

## Implementation boundaries

Keep recorded time, word totals, valid pace evidence, reading position and
remaining-time predictions distinct. Introduce new recording data through
compatible schema migrations, preserving historical rows and summaries.
Persist enough passage, session and delivery identity to enforce the agreed
rules. Validate submitted activity against its authenticated account and
content; never infer activity from a completion declaration or a jump.

Keep application text and PR titles/descriptions in English. Update statistics
labels, empty states, settings and documentation together. Preserve the existing
reading-position and resume behavior and the separate comic estimator.
Do not inject scripts into production EPUB or novel content frames.

## Required validation

- Use deterministic fixtures to check weighted rates, all three evidence
  thresholds, rereading, combined text formats and legacy/new history coexistence.
- Verify complete text traversal across paragraph fragments, different display
  sizes, reflow, forward/backward navigation and continuous scrolling. Exclude
  jumps and unfinished samples without discarding their valid recorded time.
- Check proportional caps, capped-sample exclusion, hidden readers, checkpoint
  continuity and equivalent passage accounting across desktop and mobile.
- Exercise concurrent sessions, delayed/offline submissions, lost responses,
  retries, browser reopening, account changes and removed content. Duplicate
  delivery must not add time, words, visits or pace samples.
- Assert that remaining-time algorithms retain their existing behavior for
  equivalent recorded inputs; do not make them consume the new pace metric.
- Mutation-test every new test against the behavior it guards.
- Run strict backend/static checks and current stable Chrome and Firefox on
  desktop, including mobile viewport simulations in both engines. Record
  versions; skipped or unavailable desktop targets leave validation incomplete.
  Native Android checks are deferred; their scripts remain optional.

The implementation PR records the final validation evidence for its exact commit.
