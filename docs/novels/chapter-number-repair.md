# Source chapter numbers and stable reader links

Cardboard Translation can list a novel newest first, using `Ch. N` labels and
`-cN.html` URLs without a volume. Those labels now supply the chapter number;
missing volumes default to 1. Explicit labels still take priority over URL
numbers, including decimal parts. Newest-first HTML is not a numbering scheme.

The next normal or manual sync repairs existing chapter numbers and titles by
matching their source URLs. It preserves chapter IDs, downloaded bodies, read
marks and saved paragraph/character positions. Natural order is recalculated;
manual ordering remains as chosen by the user. This does not require downloading
those chapter bodies again.

Generated reader links use `/novels/{novel_id}/read/{chapter_id}`. Before a sync
changes numeric addresses, it retains their existing destinations in an additive
alias table. Old `/read/{volume}/{chapter}` bookmarks continue to open the chapter
they resolved to immediately before that repair, even if the number now belongs
to another chapter. Further repairs do not overwrite retained destinations.
Already ambiguous numeric links retain their existing lookup result; the app
cannot reconstruct the meaning of bookmarks predating earlier unrecorded repairs.
Aliases are removed with their chapter or novel.

Chapter insertion, deletion or metadata/order changes update a reader revision.
Restored readers compare it before applying the usual forward-progress check.
Active readers check when crossing chapter boundaries. An active reader saves its
current chapter and passage before refreshing the order, waiting for older
progress requests to settle. Continuous scroll retains its last confirmed chapter
rather than a pre-mounted neighbour from the old order. Failed saves keep the
current reader open and show a retry warning. An open chapter list refreshes on
the metadata-only sync event as well.

Older restored reader markup also sees an order marker in its existing rule
comparison. That transport marker is ignored by replacement rendering and rule
signatures. New shared reader assets force one normal navigation when a boosted
page still has incompatible cached modules.

Validation covers the attached 63-chapter listing, existing/manual repairs,
alias ownership and deletion, read positions and no redownload. Browser checks
exercise numeric bookmarks, live chapter lists, chapter-boundary refresh,
backward-moving history, continuous-scroll neighbours and held older saves in
Chrome and Firefox at desktop and 320px widths. Each new regression test has a
deliberate-defect control; mutation runs use an isolated copy.
