# Domain glossary

Library entities and reading statuses are defined in [GLOSSARY.md](GLOSSARY.md).

## Library navigation

| Term | Meaning |
| --- | --- |
| Application header | The shared navigation area containing the library link, the current listing's search and available filter or cover-size controls, and the account menu. It is distinct from library tabs, listing sort controls, and the reader's controls. |
| Work details | The overview of a series or web novel, from which the user can choose an item or chapter to read. Opening this overview is distinct from starting or resuming reading. |
| Anticipated navigation | Preparing a destination before the user completes activation of its link. This does not itself open the destination or start reading. |
| Destination viewport | The portion of a destination visible immediately after opening, with saved display and listing preferences applied. |
| Visible cover readiness | The point when the destination viewport's covers can be displayed. It is distinct from the arrival of the destination's HTML and from loading all covers in a work. |
| Page usability | The point when the destination is visible and its relevant controls respond. It is distinct from completion of estimate updates and from completion of every network request. |
| “Unread only” filter | A view restricted to content that has not been completed, including both unread and partially read content. A grouped work qualifies when at least one item or listed chapter remains unfinished. It is distinct from the individual Unread status. |

## Text reading

| Term | Meaning |
| --- | --- |
| Reading section | One continuous division of content presented by the text reader, such as a section of an EPUB or a web novel chapter. It can occupy many reading pages. |
| Paragraph | A unit of prose within a reading section. Its identity does not change when the reader changes the font or screen size. |
| Reading page | The portion of content presented at one step in paginated reading. It can contain whole paragraphs and paragraph fragments. Its boundaries depend on the available reading area and reading preferences. |
| Paragraph fragment | A contiguous portion of a paragraph presented on one reading page when that paragraph continues across pages. The fragments remain parts of the same paragraph. |
| Saved reading position | The place in a book from which reading resumes. It is distinct from a page number and from a declaration that the book has been finished. |
| Page-start position | The place in the text where a reading page begins, including a place within a paragraph. It identifies the displayed passage, not the last word the reader has actually read. |
| Reflow | Redistribution of the same content across reading pages when the available reading area or reading preferences change. A position in the text remains meaningful even when page boundaries change. |
| Estimate basis | The known amount of reading remaining at a saved position, distinct from a duration predicted using reading pace. |
| Effective text reading pace | The estimated speed for one work, combining valid evidence from that work and other text works. It need not be identical for every work. |
| Prepared estimate | A prediction already calculated for a particular account and reading scope, whose freshness is still subject to changes in its inputs. |
| Estimated reading pace | An approximate measure of text reading speed, expressed in words per minute rather than rendered pages per unit of time. It uses accumulated valid reading activity, combining EPUBs and web novels. |
| Rereading | A repeated passage through previously encountered text. Its words and reading time contribute to estimated reading pace independently of advancement through the book. |
| Legacy reading history | Reading activity recorded before word-based measurement. Its recorded time is retained, but it does not provide a word-based reading pace. |
| Reading pace sample | A passage traversed during ordinary reading, paired with its corresponding reading time. Jumps and reflow do not constitute samples, and passages whose counted time reached the abandonment limit are excluded from pace estimation. |
| Reading session | A period of reading on one device. Only one session contributes activity for an account at a time; using another device gives its session priority. |
| Abandonment limit | A configurable upper bound on counted time for a passage left open, proportional to the amount of text rather than the number of rendered pages. It does not require touches or scrolling during reading. |
| Words traversed | The amount of text covered by measured reading passages, including repeated passages during rereading. It is distinct from unique words and advancement through a book. |
| Comic pages | The count of recorded comic image pages, including repeated reading of the same page. It is distinct from unique pages completed and from rendered text pages. |
| Sufficient pace evidence | At least 1,000 words, five minutes and three valid reading pace samples across EPUBs and web novels together. |
| Pending reading activity | Measured reading activity awaiting confirmation that it has been recorded. It belongs to the original account and remains the same activity when submitted again. |
| Orphan line | The first line of a paragraph left alone at the bottom of a reading page while the rest continues on the next page. |
| Widow line | The last line of a paragraph left alone at the top of the next reading page. |
| Original typography | The book's intended fonts, relative text sizes, emphasis, and spacing, as distinct from adjustments made by the reader or browser. |
| Visual hyphenation | Displaying a word across lines with a hyphen where the language permits, without inserting characters into the underlying text. |
| Hyphenation preference | The reader’s choice to enable visual hyphenation for all text books in one browser. It is distinct from each work’s language. |
| Work language | The language used to interpret one book or web novel, supplied by its metadata or chosen by the reader for that work. |
| Remaining reading time | An approximate duration for the text still ahead of the saved reading position, based on valid pace evidence. It is distinct from the time already recorded and from a promise about wall-clock completion. |
| Last valid estimate | The most recent successful reading-time prediction for the same account, content, reading position and text replacements. It remains useful while reading pace is refreshed. |
| Estimate freshness | Whether a prediction reflects the current content, saved reading position, text replacements and measured reading pace for its account and scope. A last valid estimate may remain useful while a change in pace is incorporated. |
| Partial remaining time | The sum of known remaining times in a series, explicitly accompanied by the count of unfinished items without estimates. It is not a prediction for the entire series. |
| Calculating estimate | A requested prediction that has not yet produced a result. It is distinct from insufficient reading pace evidence or unavailable content. |
| Unavailable estimate | A prediction prevented by unreadable or missing content, an incompatible saved position, or a failed request. Its cause is distinct from insufficient pace evidence. |

## Reading statistics

| Term | Meaning |
| --- | --- |
| Daily activity window | The inclusive range of calendar days selected for daily reading activity. |
| Window reading time | The sum of counted reading time across all media within the daily activity window. |
| Daily activity measure | Either reading time or comic page visits for each day. Each measure has its own scale and shares the selected window. |

## Comic reading

| Term | Meaning |
| --- | --- |
| Continuous comic scroll | Reading comic pages in a vertical strip, moving through the content rather than turning discrete pages. |
| Keyboard scrolling | Moving the visible portion of a continuous comic strip up or down in small increments, with continued movement while the direction key repeats. It is distinct from a page or chapter turn. |

## Releases

| Term | Meaning |
| --- | --- |
| Stable release | A numbered application version whose publication and artifact identity checks completed successfully. Artifact consistency and source validation are distinct requirements. |
| Latest | The moving reference to the most recent successfully published stable release. It identifies the same published image as that release's numbered version. |
| Commit alias | An additional reference to a released image, identifying the source commit from which it was built. It selects the same image as the corresponding numbered version. |
| Initial publication | The explicitly authorized first stable publication in WeebReader's independent version history. It establishes the baseline for subsequent automatic versions. |
| Interrupted publication | A publication that has created some release references but has not yet completed their required consistency checks. It is unfinished even if some references are already visible. |
| Publication recovery | Completion of an interrupted publication while preserving its approved version, source commit and any recorded image identity. It is distinct from selecting newer changes for a subsequent release. |

## Browser validation

| Term | Meaning |
| --- | --- |
| Browser validation target | One required browser at its selected stable version, evaluated across all required desktop and mobile viewport scenarios and deliberate-defect controls. |
| Required browser coverage | Chrome/Chromium and Firefox desktop, including mobile viewport simulations in both. Both targets must pass. Native Android checks are deferred and optional. |
| Android browser validation | Execution in an installed Android browser on a device or emulator, with the browser and operating system versions recorded. Desktop viewport emulation does not constitute Android browser validation. |
| Source validation | Successful completion of all required checks for one exact source commit and its selected browser versions. Skipped, cancelled or incomplete checks do not constitute successful validation. |
| Validation browser version | The current stable browser version selected for a local validation run and retained throughout that run. Its identity is part of the validation evidence. |

## Tracking configuration

| Term | Meaning |
| --- | --- |
| OAuth client registration | The MyAnimeList application registered for an installation, identified by its own public client ID and any required client secret. It is distinct from a reader's linked MyAnimeList account. |
| OAuth redirect URI | The explicitly configured callback address for this installation, matching the address registered with MyAnimeList exactly. It is required for a new connection and is independent of the address on the incoming request. |
