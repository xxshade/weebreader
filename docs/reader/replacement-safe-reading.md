# Reading positions across text replacements

EPUB and novel progress retains the legacy displayed block/UTF-16 coordinate and
an optional reference to the original source. Changing a literal replacement can
shift displayed offsets, introduce markup, remove blocks or change entity
spelling. The source reference lets the reader translate the saved point into the
current display. Inside a rewritten or removed passage, reading resumes at its
beginning; unchanged rules preserve the exact existing displayed point.

The reference stores hashes and numeric coordinates, not replacement history or
book text. It is accepted only with the expected schema and work/section identity.
Translation checks the original source hash and validates the parsed tree; an
unsupported or changed source retains the legacy fallback. Older clients may
continue saving legacy coordinates, clearing a now-inapplicable source reference.

Existing positions are captured before Add/Edit/Delete changes their rules. File
and HTML preparation runs outside the writer lock. The mutation rechecks the
rules and selected progress under the writer, retrying preparation if either
changed; metadata and the rule change commit together. An unavailable source does
not prevent editing a rule. Word estimates include the source reference in their
cache identity and use the translated point when rebuilding counts.

For structural HTML, the browser and backend track raw-text provenance through
ordered replacements, entity decoding and HTML5 parsing. The parser uses the
HTML5 bundle from the already pinned Bleach dependency. Ordinary substitutions
retain the simpler block-preserving path. Structural conversions allocate
transient parser trees and cost more CPU, especially for large chapters; they
are not paid by responses that reuse a prepared estimate. There is no new input
size limit. Repaired activity text has a bounded, transient cache (16 entries,
500,000 characters total); larger values are parsed without being cached.

History restores compare current rules and content identity even when the saved
position is unchanged. A novel's first opening supplies its current chapter as
context without creating artificial progress. These checks do not require a
stream or a persistent connection in the reader.

The paginated cache holds up to four prepared chapters and two prefetched
resources. Maps follow those entries; the visible and requested chapters are
protected. Continuous scroll preserves mounted chapters for backward scrolling
and native Find. Departing a boosted reader releases maps, resume watchers,
scroll observers and the fullscreen button after saving the exit position.
Native page transitions preserve the state needed for bfcache.

A natural one-tab activity failure was reproduced with malformed table HTML:
HTML5 foster parenting moved text before the table while the previous backend
parser expected a different order. The server now validates the repaired HTML5
order. Forged, reversed or out-of-range passages remain invalid. This fixes a
reproduced cause of the warning; it does not hide legitimate activity refusals.
Queued activity also drains when a successful in-flight request overlaps exit.

## Validation evidence

Before integration, synthetic fixtures passed in Chrome 157 and Firefox 159:

- Nine structural mapping cases matched the backend; actual EPUB/novel Settings
  edits preserved the reading passage at desktop and 320px widths.
- Continuous-scroll resume kept the saved point on the first visible line.
- Native Back/Forward observed `pageshow.persisted=true`, refreshed changed rules
  and files, and continued working after another Forward/Back traversal.
- Forty prepared chapters stayed at four cached entries. Chrome JS heap after GC
  was about 2.83 MB versus 7.12 MB with eviction disabled; this is isolated fixture
  heap, not physical browser memory or a measurement on the user's server.
- Four repeated scroll-reader visits retained no departed reader roots in the
  final Chrome heap inspection. Firefox released the watcher/fullscreen control
  and reported no ghost windows. Scroll memory still grows with mounted content.
- An isolated Python process converting approximately 1 MiB of structural HTML
  peaked near 118 MiB RSS; retained RSS stabilized near 93 MiB over repeated
  conversions. The local diagnostic conversion took roughly 0.63–0.98 seconds,
  not an HTTP latency prediction for another machine.
- The natural activity scenario recorded real activity without a warning in both
  browsers. A controlled successful-response overlap drained pending visits.

The required CI suite keeps portable regression coverage and deliberate-defect
controls. Desktop viewport simulations are not native Android or LibreWolf tests.
No manual migration is required: startup adds the optional progress columns;
legacy positions are captured lazily before their replacement rules change.


## Replacement settings and upgrade cleanup

Add and Edit reject an exact duplicate within one account and series or novel;
case, whitespace and a different replacement remain meaningful distinctions.
The check and write share the serialized transaction, so repeated concurrent
requests cannot insert identical rules. Settings displays pending, saved and
failed outcomes. A refused edit stays open for correction; saving an earlier
request cannot erase a newer draft typed while it was pending. A library resync
restores the Add draft and rebuilds its searchable control.

Startup removes later existing exact duplicates before estimate warmup, keeping
the earliest rule and remaining order. Because applying an identical rule twice
can produce different text, cleanup prepares available legacy source anchors
outside the writer and revalidates them atomically before deleting rules. It does
not change book files or the persisted display coordinates. A later startup with
no duplicates does no cleanup writes.

HTML5 repair now uses the etree builder before HTML serialization, avoiding the
XML-name coercion of EPUB namespace attributes. Interrupted estimate request
bodies return 499 without starting estimation; malformed JSON still returns 400.
