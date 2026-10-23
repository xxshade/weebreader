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
not. Unfinished reading still contributes recorded time. The total **Text words
traversed** counts confirmed passages, including capped passages and rereading;
only eligible passages contribute to pace. The application cannot determine
whether every displayed word was understood or actually read.

For comics, **Comic pages** includes rereading rather than counting only distinct
pages completed. **Time per comic page** uses the same unit. Text and comic
pages are not added together as a single page count. Daily time retains all
formats; **Comic pages per day** covers only comics, including in its tooltips.
Periods without comics retain the chart with zero pages.

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

**Remaining-time predictions continue using the existing estimator.** The new
words-per-minute indicator does not feed that algorithm. Fixing recorded time
can indirectly change its results. Its limitations around previous progress,
jumps and rereading are documented in the implementation plan for future work.
