# Reproducing the README screenshots

The gallery shows the real application with a disposable account, temporary
SQLite database, original fictional stories and programmatically drawn
sample covers/comic pages. No production library, tracker account, external
scraping or real credentials are needed. No UI elements are edited for the
screenshots.

Install the application dependencies and `requests`, then run:

```bash
./.venv/bin/python scripts/capture_readme.py \
  --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver \
  --firefox-binary /path/to/firefox --geckodriver /path/to/geckodriver \
  --output-dir /tmp/publication-reader-screenshots
```

Pillow needs the DejaVu Sans and Serif fonts (`fonts-dejavu-core` on Debian).
The script always runs **both browsers**. It checks library/volume counts,
loaded covers, real comic and EPUB page turns, text and settings, and reports
JavaScript errors using probes installed before navigation. Fullscreen is
stubbed so a click cannot change the capture viewport. Temporary account,
server and media files are cleaned up after the run.

Inspect both browser directories before selecting the gallery images:

```bash
cp /tmp/publication-reader-screenshots/chrome/*.png docs/images/
```

The README uses one image per screen, captured in Chrome at a 1440×1000
window; Firefox exercises the same flows and provides comparison captures.
Regenerate intentionally when the pictured UI changes. Screenshots are not
automatically rewritten by a release. This keeps release diffs focused and
avoids replacing real screenshots with synthetic UI mockups.
