"""
Scraper for cardboardtranslation.com novel pages.

Ported from a standalone companion app -- this module has no database
dependency at all (it returns plain dataclasses to the caller), so the port
only touches the settings import; the parsing/Selenium logic is unchanged.

The chapter index (div#episodeNime > ul.myUL) is populated client-side after
page load, so we render the page with a real headless browser (Chromium via
Selenium) and wait for that container before reading its DOM. Individual
chapter pages render their text server-side (div.post), so those are
fetched with plain HTTP requests, which is much cheaper than spinning up a
browser per chapter.

NOTE: the exact markup inside #episodeNime could not be inspected ahead of
time (it only exists after JS runs). The parser below is deliberately
defensive: volume/chapter numbers are read primarily from each entry's own
listing label ("[Vol. X] Ch. Y" or "Ch. Y.Z" for a multi-part chapter — see
CHAPTER_LABEL_RE), falling back to the chapter URL slug pattern
"...-v{volume}c{chapter}.html" (VOLUME_CHAPTER_URL_RE) — confirmed stable on
real chapter links — for anything the label regex can't resolve. If the live
markup differs from what's assumed here, adjust CHAPTER_LIST_CONTAINER,
CHAPTER_LABEL_RE, or VOLUME_CHAPTER_URL_RE below after inspecting the real
DOM with browser devtools (Elements tab, after the page finishes loading).
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
from dataclasses import dataclass, field

import bleach
import httpx
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from app.config import settings

logger = logging.getLogger("novels.scraper.cardboard")

CHAPTER_LIST_CONTAINER = "#episodeNime"
VOLUME_CHAPTER_URL_RE = re.compile(r"-v(\d+)c(\d+)(?:[.-]|$)", re.IGNORECASE)

# The site's listing label is "[Vol. X] Ch. Y" — and for multi-part chapters
# (e.g. an interlude split into several posts), "Ch. Y.Z" with a decimal
# sub-part (confirmed from a real listing: "[Vol. 3] Ch. 45.3 ..."). The URL
# slug pattern above only ever captures the integer part, which collapsed
# every part of a split chapter onto the same chapter_number — this label
# regex is the primary source of truth for both volume and chapter number
# specifically because it's the only place the decimal sub-part appears at
# all; the URL is kept only as a fallback for links that don't match this.
CHAPTER_LABEL_RE = re.compile(
    r"^\[?\s*Vol\.?\s*(\d+)\s*\]?\s*Ch(?:apter|\.)?\s*(\d+(?:\.\d+)?)\.?\s*", re.IGNORECASE
)
# Same label pattern but not anchored to the start — some listing entries
# render the whole "[Vol.X] Ch.Y Title" block twice in a row (looks like a
# responsive mobile/desktop duplicate pair in the source markup), so after
# stripping the leading label a second one can still be sitting in the
# middle of what's left. Used to strip that second occurrence too.
CHAPTER_LABEL_ANYWHERE_RE = re.compile(
    r"\[?\s*Vol\.?\s*\d+\s*\]?\s*Ch(?:apter|\.)?\s*\d+(?:\.\d+)?\.?\s*", re.IGNORECASE
)

# The site's Blogger template reports locale id_ID, so dates render with
# Indonesian month names (e.g. "02 Januari 2025") even though the rest of the
# content is in English. Translated so everything shown in the app is English.
INDONESIAN_MONTHS = {
    "januari": "January", "februari": "February", "maret": "March",
    "april": "April", "mei": "May", "juni": "June", "juli": "July",
    "agustus": "August", "september": "September", "oktober": "October",
    "november": "November", "desember": "December",
}
# (?<!\d) / (?<![A-Za-z]) instead of a leading "\b": \b doesn't create a
# boundary between a letter and a digit (both count as \w), so a date
# glued directly onto the end of a word with no space -- exactly the site's
# documented "no whitespace between sibling text nodes" markup pattern --
# was never matched at all by a plain \b\d{1,2}. These lookbehinds require
# the character right before the date to not be part of a longer digit-run
# or word, which is what a leading boundary was actually meant to express
# here; it only happened to work before when a space already separated the
# two, which defeated the entire point of stripping a glued-on date.
DATE_PATTERN = re.compile(
    r"(?<!\d)\d{1,2}\s+[A-Za-zÀ-ÿ]+\s+\d{4}\b|(?<![A-Za-z])[A-Za-z]+\s+\d{1,2},?\s+\d{4}\b"
)


@dataclass
class ScrapedChapterRef:
    title: str
    url: str
    volume_number: int
    chapter_number: float | None
    release_date: str | None = None


@dataclass
class ScrapedNovelMeta:
    title: str
    cover_url: str | None
    synopsis: str | None
    status: str | None
    chapters: list[ScrapedChapterRef] = field(default_factory=list)


def _guess_volume_chapter_from_url(url: str) -> tuple[int | None, float | None]:
    m = VOLUME_CHAPTER_URL_RE.search(url)
    if not m:
        return None, None
    return int(m.group(1)), float(m.group(2))


def _translate_date(raw_date: str) -> str:
    parts = raw_date.split()
    return " ".join(INDONESIAN_MONTHS.get(p.lower(), p) for p in parts)


def _clean_chapter_entry(raw_text: str) -> tuple[str, str | None, int | None, float | None]:
    """Turn a concatenated listing entry like
    "[Vol. 1] Ch. 59Little Gift and the Princess's Gratitude02 Januari 2025 02 Januari 2025"
    (site markup has no whitespace between sibling text nodes) into
    (title, release_date, volume_number, chapter_number): strips and parses
    the leading "[Vol.X] Ch.Y" or "[Vol.X] Ch.Y.Z" label, pulls out the
    release date (which sometimes repeats) and translates it to English, and
    returns whatever text remains as the title.
    """
    text = re.sub(r"\s+", " ", raw_text).strip()

    volume_number = None
    chapter_number = None
    label_match = CHAPTER_LABEL_RE.match(text)
    if label_match:
        volume_number = int(label_match.group(1))
        chapter_number = float(label_match.group(2))
        text = text[label_match.end():].strip()

    release_date = None
    dates_found = DATE_PATTERN.findall(text)
    if dates_found:
        release_date = _translate_date(dates_found[0])
        text = DATE_PATTERN.sub("", text)

    # Strip a second, embedded copy of the "[Vol.X] Ch.Y" label if one
    # exists (see CHAPTER_LABEL_ANYWHERE_RE above).
    text = CHAPTER_LABEL_ANYWHERE_RE.sub(" ", text)

    title = re.sub(r"\s+", " ", text).strip(" -–—:\t")
    title = _collapse_exact_duplicate(title)
    return title, release_date, volume_number, chapter_number


def _collapse_exact_duplicate(text: str) -> str:
    """If `text` is two copies of the same string glued together (e.g. a
    title that appears twice because the source listing rendered a
    duplicate block — see CHAPTER_LABEL_ANYWHERE_RE above), return just one
    copy. Tries splitting at the midpoint and a couple of nearby offsets to
    tolerate a stray extra space between the two copies."""
    t = text.strip()
    n = len(t)
    if n < 6:
        return t
    mid = n // 2
    for offset in (0, 1, -1, 2, -2):
        split = mid + offset
        if split <= 0 or split >= n:
            continue
        first, second = t[:split].strip(), t[split:].strip()
        if first and first == second:
            return first
    return t


def _parse_chapter_list_html(list_html: str) -> list[ScrapedChapterRef]:
    """Parse the rendered contents of #episodeNime into chapter refs.

    Volume/chapter numbers are read primarily from each entry's own listing
    label ("[Vol. X] Ch. Y" or "Ch. Y.Z" for a multi-part chapter — see
    CHAPTER_LABEL_RE), falling back to the URL slug pattern
    (`...-v8c344.html`) only for entries whose label doesn't parse, since
    the URL never encodes a decimal sub-part. The URL is still used to
    confirm an <a> is actually a chapter link (filters out stray nav/share
    links sharing the same container) even when the label supplies the
    numbers. This site's chapter-list markup mixes sibling text nodes with
    no whitespace between them (label, title, and date all run together)
    and repeats its "Vol/Ch | Title | Release Date" table header noise near
    each volume's rows rather than a clean per-volume heading — hence
    parsing the label text per-entry instead of trying to infer grouping
    from DOM structure. Results are returned sorted by (volume, chapter)
    ascending.
    """
    soup = BeautifulSoup(list_html, "lxml")
    container = soup.select_one(CHAPTER_LIST_CONTAINER) or soup

    chapters: list[ScrapedChapterRef] = []

    for a in container.find_all("a", href=True):
        href = a["href"].strip()
        url_vol, url_chap = _guess_volume_chapter_from_url(href)
        if url_vol is None:
            # Not a chapter link (e.g. a stray nav/share link inside the same container)
            continue

        raw_text = a.get_text(separator=" ", strip=True)
        parent = a.find_parent(["li", "tr"])
        if parent is not None:
            parent_text = parent.get_text(separator=" ", strip=True)
            if len(parent_text) > len(raw_text):
                raw_text = parent_text

        title, release_date, label_vol, label_chap = _clean_chapter_entry(raw_text)
        vol = label_vol if label_vol is not None else url_vol
        chap = label_chap if label_chap is not None else url_chap
        if not title:
            title = f"Chapter {chap}" if chap is not None else href

        chapters.append(
            ScrapedChapterRef(
                title=title, url=href, volume_number=vol, chapter_number=chap, release_date=release_date
            )
        )

    if not chapters:
        # Fallback: no vNcN pattern matched anything -> bucket every link
        # inside the container into volume 1, numbered by DOM order.
        for idx, a in enumerate(container.find_all("a", href=True), start=1):
            title, release_date, _, _ = _clean_chapter_entry(a.get_text(separator=" ", strip=True))
            chapters.append(
                ScrapedChapterRef(
                    title=title or a["href"],
                    url=a["href"].strip(),
                    volume_number=1,
                    chapter_number=float(idx),
                    release_date=release_date,
                )
            )

    # One entry per URL. A chapter linked twice in the list (a "latest
    # chapter" link inside the same container, say) used to reach the sync
    # twice and break UNIQUE(novel_id, url) on the second insert -- every
    # sync of that novel then failed with nothing written, not even the
    # time it was checked. The first occurrence is kept.
    seen: set[str] = set()
    chapters = [c for c in chapters if not (c.url in seen or seen.add(c.url))]
    chapters.sort(key=lambda c: (c.volume_number, c.chapter_number if c.chapter_number is not None else 0))
    return chapters


def _build_driver() -> webdriver.Chrome:
    logger.info(
        "Launching headless Chromium (binary=%s, chromedriver=%s)",
        settings.novel_chromium_binary_path or "(default PATH)",
        settings.novel_chromedriver_path or "(default PATH)",
    )
    options = ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"user-agent={settings.novel_scrape_user_agent}")
    # A fresh, guaranteed-writable profile directory for every launch,
    # explicit rather than left to Chrome's own default resolution
    # (normally $HOME/.config/chromium). In the container, the app runs as
    # a non-root user dropped into via `setpriv`, which doesn't reset
    # $HOME -- if it's ever wrong or unset, Chrome fails to create its
    # default profile dir and exits immediately with an unhelpful "session
    # not created: Chrome instance exited" and no real explanation (see
    # entrypoint.sh for the actual fix; this is a second, independent
    # safety net so a headless-browser launch never again depends on
    # $HOME being right). tempfile.mkdtemp() always returns a directory
    # under $TMPDIR (falling back to /tmp), which is writable by any user
    # by default regardless of $HOME. Not cleaned up afterward -- an
    # empty/small leftover profile dir per sync is a negligible cost, and
    # deleting it reliably would need to handle the fact that Chrome may
    # still be flushing files to it right as the process exits.
    options.add_argument(f"--user-data-dir={tempfile.mkdtemp(prefix='chromium-profile-')}")
    # "normal" (Selenium's default) waits for the browser's full load event,
    # i.e. every subresource on the page — including ads/trackers on a page
    # like this one — which is what was causing "Timed out receiving message
    # from renderer" after 45s. "eager" only waits for DOMContentLoaded
    # (HTML parsed, DOM built) which is all we actually need before reading
    # #episodeNime, and is far less likely to hang on slow third-party assets.
    options.page_load_strategy = "eager"
    if settings.novel_chromium_binary_path:
        options.binary_location = settings.novel_chromium_binary_path

    service = (
        ChromeService(executable_path=settings.novel_chromedriver_path)
        if settings.novel_chromedriver_path
        else ChromeService()
    )
    driver = webdriver.Chrome(options=options, service=service)
    logger.info("Chromium started successfully.")
    return driver


def _fetch_rendered_html_sync(novel_url: str) -> str:
    """Blocking Selenium call: loads the page in headless Chromium, waits for
    the JS-populated chapter list container, and returns the rendered DOM.
    Runs off the event loop via asyncio.to_thread (see fetch_novel_index).
    Raises RuntimeError with a specific message on failure instead of
    letting a raw Selenium/WebDriver exception (or a silently empty result)
    propagate — this is what surfaces in the settings page's error banner."""
    try:
        driver = _build_driver()
    except Exception as exc:
        logger.exception("Failed to launch headless Chromium")
        raise RuntimeError(
            f"Could not start headless Chromium ({exc}). Check that chromium and "
            f"chromium-driver are installed and that APP_NOVEL_CHROMIUM_BINARY_PATH / "
            f"APP_NOVEL_CHROMEDRIVER_PATH point to valid binaries."
        ) from exc

    try:
        driver.set_page_load_timeout(45)
        logger.info("Loading %s ...", novel_url)
        try:
            driver.get(novel_url)
        except TimeoutException:
            # Even with page_load_strategy="eager", a slow third-party
            # resource can occasionally still trip Chromium's internal
            # load-event wait. The DOM itself has very likely already
            # parsed by this point, so stop the browser's network activity
            # and keep going instead of throwing the whole sync away.
            logger.warning(
                "Page load timed out for %s — stopping further loading and continuing with "
                "whatever DOM is already present.",
                novel_url,
            )
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        except Exception as exc:
            logger.exception("driver.get() failed for %s", novel_url)
            raise RuntimeError(f"Failed to load {novel_url}: {exc}") from exc
        logger.info("Page navigation complete, waiting for %s ...", CHAPTER_LIST_CONTAINER)

        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, CHAPTER_LIST_CONTAINER))
            )
            logger.info("%s appeared in the DOM.", CHAPTER_LIST_CONTAINER)
        except Exception:
            # Container may load slower, or under a different selector than
            # assumed; give any async JS a bit more time before giving up.
            logger.warning(
                "%s did not appear within 20s, waiting 5 more seconds before giving up.",
                CHAPTER_LIST_CONTAINER,
            )
            time.sleep(5)

        html = driver.page_source
        logger.info("Retrieved page_source: %d bytes.", len(html or ""))
        if not html or len(html) < 500:
            raise RuntimeError(
                f"Page loaded but returned suspiciously little HTML ({len(html or '')} bytes) "
                f"for {novel_url} — the browser may not have rendered the page correctly."
            )
        return html
    finally:
        driver.quit()
        logger.info("Chromium closed.")


async def fetch_novel_index(novel_url: str) -> ScrapedNovelMeta:
    """Render the novel page with a headless browser and extract metadata + chapter list."""
    full_html = await asyncio.to_thread(_fetch_rendered_html_sync, novel_url)
    # Parsed in a thread too: BeautifulSoup over a 900-chapter index is
    # 140 ms, and on the event loop every request in the app waited for it.
    return await asyncio.to_thread(_parse_novel_page, full_html, novel_url)


def _parse_novel_page(full_html: str, novel_url: str) -> ScrapedNovelMeta:
    soup = BeautifulSoup(full_html, "lxml")

    title_el = soup.select_one("h1")
    title = title_el.get_text(strip=True) if title_el else novel_url

    # Prefer the og:image meta tag: it's always present in the initial HTML
    # (never lazy-loaded) and is exactly the thumbnail the site itself
    # designates for previews. Fall back to the visible <img>, checking
    # data-src/data-lazy-src first — this site's thumbnail uses a lazy-load
    # library (its alt text literally says "thumbnail lazyload"), so the
    # live src attribute is often an empty/placeholder data: URI until the
    # image scrolls into view in a real browser, which headless scraping
    # never triggers.
    cover_url = None
    og_image = soup.select_one('meta[property="og:image"]')
    if og_image and og_image.get("content"):
        cover_url = og_image["content"].strip()
    else:
        cover_el = soup.select_one("img.post-thumbnail, article img, .post-body img, h1 ~ a img")
        if cover_el:
            src = (cover_el.get("src") or "").strip()
            if not src or src.startswith("data:"):
                cover_url = cover_el.get("data-src") or cover_el.get("data-lazy-src") or None
            else:
                cover_url = src

    synopsis = None
    synopsis_label = soup.find(string=re.compile("Synopsis", re.IGNORECASE))
    if synopsis_label:
        block = synopsis_label.find_parent()
        if block:
            texts = []
            node = block.find_next_sibling()
            steps = 0
            while node is not None and steps < 15:
                text = node.get_text(" ", strip=True)
                if text:
                    if re.match(r"^(Year|Author|Artist|Serialization|Status|Type)\b", text):
                        break
                    texts.append(text)
                node = node.find_next_sibling()
                steps += 1
            synopsis = " ".join(texts).strip() or None

    status = None
    status_label = soup.find(string=re.compile(r"^\s*Status\s*$", re.IGNORECASE))
    if status_label:
        parent = status_label.find_parent()
        if parent and parent.find_next_sibling():
            status = parent.find_next_sibling().get_text(strip=True)

    list_container = soup.select_one(CHAPTER_LIST_CONTAINER)
    if list_container is None:
        raise RuntimeError(
            f"'{CHAPTER_LIST_CONTAINER}' was not found in the rendered page for {novel_url}. "
            f"The site's chapter-list markup may have changed — inspect it with browser "
            f"devtools and update CHAPTER_LIST_CONTAINER in app/novels/scraper.py."
        )

    chapters = _parse_chapter_list_html(str(list_container))
    logger.info("Parsed %d chapter link(s) from %s.", len(chapters), CHAPTER_LIST_CONTAINER)
    if not chapters:
        raise RuntimeError(
            f"'{CHAPTER_LIST_CONTAINER}' was found but no chapter links matched the expected "
            f"URL pattern (-vNcN-) inside it for {novel_url}. The site's link format may have "
            f"changed — inspect the real markup and update VOLUME_CHAPTER_URL_RE / "
            f"_parse_chapter_list_html in app/novels/scraper.py."
        )

    return ScrapedNovelMeta(
        title=title, cover_url=cover_url, synopsis=synopsis, status=status, chapters=chapters
    )


async def fetch_image(url: str) -> tuple[bytes, str] | None:
    """Download an image's raw bytes + content-type. Used for the novel
    cover so the app stores and serves it itself instead of the browser
    hotlinking the source site directly on every page view — which breaks
    for anyone blocking third-party domains client-side (ad/tracker
    blockers), since the browser is what fetches it, not the server."""
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": settings.novel_scrape_user_agent}, timeout=30)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            if not content_type.startswith("image/"):
                content_type = "image/jpeg"
            return resp.content, content_type
    except Exception as exc:
        logger.warning("Failed to download cover image %s: %s", url, exc)
        return None


# Real (potential) bug found: the old "sanitizer" here only ever removed a
# fixed list of chrome tags/attributes -- it was an allowlist of *styling*
# attributes to drop, not a real HTML sanitizer. It never touched event
# handler attributes (onerror, onclick, onload, ...), javascript:/data: URLs
# in href/src, or dangerous tags (svg, object, embed, form, ...). This
# content is scraped from a third-party site the app doesn't control and
# later served as-is to `/novels/{id}/resource/{chapter_id}`, which the
# reader injects via innerHTML -- so anything the source page returns (an ad
# network's payload, a compromised page, a malicious edit upstream) would
# execute in the logged-in user's session. `bleach` enforces a real
# allowlist: only tags/attributes below survive, everything else (including
# all "on*" attributes and non-http(s) URL schemes) is stripped outright.
#
# Tables are allowed because a table is content, not chrome: a stripped one
# kept its text and lost its shape, so a character sheet arrived as one
# run-together line. The reader paginates a table as one block.
_ALLOWED_TAGS = [
    "p", "br", "b", "strong", "i", "em", "u", "s", "sub", "sup",
    "blockquote", "ul", "ol", "li", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "a", "img", "span", "div",
    "table", "caption", "colgroup", "col", "thead", "tbody", "tfoot", "tr", "th", "td",
]
_ALLOWED_ATTRS = {
    "a": ["href", "title"],
    "img": ["src", "alt"],
    "th": ["colspan", "rowspan"],
    "td": ["colspan", "rowspan"],
    "col": ["span"],
    "colgroup": ["span"],
}
_ALLOWED_PROTOCOLS = ["http", "https"]


async def fetch_chapter_content(chapter_url: str, client: httpx.AsyncClient) -> str:
    """Fetch a single chapter page and extract the cleaned content of div.post."""
    resp = await client.get(chapter_url, headers={"User-Agent": settings.novel_scrape_user_agent}, timeout=30)
    resp.raise_for_status()
    # Parsing and cleaning off the event loop: 84 ms for a 300 KB page,
    # once per chapter of every sync, during which page turns and progress
    # saves all waited (AGENTS.md: nothing blocking on the loop).
    return await asyncio.to_thread(_clean_chapter_page, resp.text, chapter_url)


def _clean_chapter_page(page_html: str, chapter_url: str) -> str:
    soup = BeautifulSoup(page_html, "lxml")

    post = soup.select_one("div.post")
    if post is None:
        raise ValueError(f"div.post not found on {chapter_url}")

    # Strip share buttons, nav widgets, ads and other chrome that sometimes
    # nests inside the same content wrapper on Blogger themes.
    for junk in post.select(
        "script, style, .share, .sharing, .related, .nav-links, .navigation, iframe, ins"
    ):
        junk.decompose()

    cleaned = bleach.clean(
        str(post),
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )
    return cleaned


_CONTENT_CLEAN_MARKER = "<!--nv-clean-->"


def tag_paragraphs(content_html: str) -> str:
    """Clean up stored chapter content and give every substantive <p> a
    stable data-p="N" index (document order). Applied at read time (not
    baked into what's stored — see router.py, which persists the result
    back after the first pass) so it also works retroactively on chapters
    that were scraped before this existed. Safe to call repeatedly: once
    content starts with _CONTENT_CLEAN_MARKER it's returned as-is rather
    than re-parsed.

    Two things happen here:
    - Empty/whitespace-only spacer paragraphs (this site interleaves real
      text with blank <p><br/></p> spacers) are removed outright. They
      don't just get skipped when numbering paragraphs — each one used to
      still render as its own blank line stacking on top of every
      paragraph's own CSS bottom margin, which got worse the more the
      reader's line-spacing control was turned up. The remaining
      paragraphs' own margin already provides the visual gap between them.
    - Every remaining <p> gets a data-p="N" index, used by the reader's
      infinite-scroll JS as a cross-device resume anchor: "paragraph 42"
      means the same content whether the screen is a phone or a desktop
      monitor, unlike a raw scroll percentage.
    """
    if not content_html:
        return content_html
    if content_html.startswith(_CONTENT_CLEAN_MARKER):
        return content_html

    soup = BeautifulSoup(content_html, "lxml")
    for p in soup.find_all("p"):
        if not p.get_text(strip=True) and p.find("img") is None:
            p.decompose()

    for idx, p in enumerate(soup.find_all("p")):
        p["data-p"] = str(idx)

    # BeautifulSoup's lxml parser always wraps a fragment in <html><body>;
    # unwrap back to just the inner content, or it'd nest a whole extra
    # document inside the page every time a chapter is rendered.
    cleaned = soup.body.decode_contents() if soup.body else str(soup)
    return _CONTENT_CLEAN_MARKER + cleaned


async def fetch_all_chapter_contents(
    chapter_urls: list[str],
    delay_seconds: float | None = None,
    on_chapter_done=None,
) -> int:
    """Sequentially fetch chapter contents with a polite delay between
    requests. If given, on_chapter_done(index, total, url, success, content)
    is awaited immediately after each attempt — the sync job uses this to
    write each chapter's content to the database as soon as it's fetched,
    instead of waiting for the whole (possibly very long, for novels with
    hundreds of chapters) batch to finish before anything becomes readable.
    Returns the count of chapters successfully fetched."""
    delay = settings.novel_scrape_request_delay_seconds if delay_seconds is None else delay_seconds
    total = len(chapter_urls)
    fetched_count = 0
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for i, url in enumerate(chapter_urls, start=1):
            content = None
            success = False
            try:
                content = await fetch_chapter_content(url, client)
                success = True
                fetched_count += 1
                logger.info("[%d/%d] downloaded %s (%d bytes)", i, total, url, len(content))
            except Exception as exc:
                logger.warning("[%d/%d] FAILED %s: %s", i, total, url, exc)
            if on_chapter_done:
                await on_chapter_done(i, total, url, success, content)
            await asyncio.sleep(delay)
    return fetched_count
