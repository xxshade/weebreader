# Text pagination validation

The paginated EPUB and web-novel readers use stable block and DOM-text offsets.
Pages are recalculated after resizing or changing settings. Earlier block-only
positions remain valid. New web-novel offsets use an additive database column;
older application versions ignore it and resume at the block start.

## Reproduce desktop checks

Run `scripts/test_pagination_browser.py` with the four browser/driver flags used
by `scripts/test_audit_browser.py`. Its default run always includes Chrome and
Firefox. Run it locally before merging. `--test` narrows the scenario while
still running both browsers.

The fixtures cover complete content in both directions, inline emphasis,
paragraph continuation indentation, resume after reload, legacy anchors,
resize/font reflow, switching reading modes, a one-word cross-device advance,
widows, headings, author breaks and keep-together rules, and a complete oversized
table reached using a real wheel gesture. The existing audit suite covers
completion and cross-chapter behavior. Atomic media and tables remain scrollable.
The fixtures also cover Unicode graphemes and a viewport shorter than one line.

Local validation passed 1,448 strict backend tests, six pagination browser tests,
ten existing reader/audit browser tests, and two reconnection browser tests. Every
browser suite included Chrome and Firefox. The final tiny-viewport extension was
also run in both browsers, and the final progress-validation changes passed the
39 relevant backend tests. Static checks and the isolated WebAuthn flow passed.
Mutation checks rejected disabled paragraph splitting, ignored text offsets,
ignored explicit breaks, discarded stored offsets and the removed Android fix.

The browser fixtures caught a Chromium scrollbar feedback loop: an overflowing
trial adds a scrollbar, narrowing the text enough to keep a previously fitting
fragment overflowing. Reserving the gutter throughout measurement and display
keeps those layouts equivalent.

## Firefox Android typography

Run `python scripts/probe_android_typography.py --port 8766 --output result.json`.
Open the probe inside the Android browser (the emulator reaches the host at
`http://10.0.2.2:8766/`). This serves only disposable text and public stylesheets;
it does not expose a library, database or authenticated application.

The probe reads `themeCssText()` from the production reader and compares it with
a control that removes the correction. Short and long flow-root paragraphs both
declare 16 px. Before the correction, the same ten letters measured 139.67 px in
the short paragraph and 163 px in the long one. With `-moz-text-size-adjust:none`
they both measured 139.67 px. The book's 2em heading remained twice as large, and
changing the reader size to 150% still enlarged the text to 24 px.

[Recorded measurements](firefox-android-typography.json) were obtained with
**Firefox Android 125.3.0 on an Android 9 emulator**, using the official Mozilla
GitHub release APK. This demonstrates Gecko Android font inflation and the
correction; it does not establish the exact CSS in the user's EPUB or validate
current Firefox Android. Current Mozilla download hosts were blocked by the
cloud environment's network policy. Desktop Firefox with a narrow viewport was
not counted as Android evidence.

After deployment, the user reported that the affected EPUB passage now renders
paragraphs at equal sizes in their current Firefox mobile. This is confirmation
from the user's device, separate from the automated emulator measurements above;
the exact installed version was not supplied.

The probe includes a failing control: Android must reproduce unequal paragraph
sizes with the correction removed. Its production assertions check equal prose
sizes, preserved heading size, and reader font scaling.

## Limits of the anchor

Offsets count UTF-16 code units in displayed DOM text, after text replacement
rules. Replacing an EPUB with a different edition or changing replacement rules
can change which words an existing offset identifies. Out-of-range positions
are clamped; the format does not attempt quote matching across content edits.
