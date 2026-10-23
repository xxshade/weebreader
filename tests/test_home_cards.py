"""One shape for a home-grid card, wherever it comes from.

The grid renders these cards, `/library/{kind}/{id}/card` answers them as
JSON, and library-events.js builds a live card from that JSON -- so a card
that arrives live has to be indistinguishable from one the server
rendered. Three hand-written copies of the same fields is how that stops
being true, quietly: the cover URL was already "the third place the same
URL is built" before `_series_card` / `_novel_card` existed.

The novel half of that pair is newer than the series half, and exists for
one reason: a tracker link made on a novel's own page changes the dot on
its home-grid card, and until it existed there was no way to bring a novel
card up to date without a reload.
"""
import tempfile
import unittest
from pathlib import Path

from starlette.requests import Request

from app.config import settings
from app.database import get_connection, init_db
from app.library.router import home, novel_card, series_card

ROOT = Path(__file__).resolve().parents[1]
EVENTS_JS = ROOT / "app/static/js/library-events.js"
HOME_JS = ROOT / "app/static/js/home.js"

USER = {"id": 1, "username": "u", "avatar_path": None,
        "page_time_cap_seconds": 180, "cover_blur_px": 12}


def _request(path="/"):
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [], "client": ("test", 1), "server": ("test", 80),
    })


class _Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self._tmp.name)
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        # app.main mounts /covers from this directory at import time.
        (Path(self._tmp.name) / "covers").mkdir(parents=True, exist_ok=True)
        await init_db()
        self.db = await get_connection()
        self.addAsyncCleanup(self.db.close)
        await self.db.execute("INSERT INTO users (id, username, password_hash) VALUES (1,'u','x')")
        await self.db.execute(
            "INSERT INTO libraries (id, name, root_path, media_type) VALUES (1,'L','/l','comic')")
        await self.db.execute(
            "INSERT INTO series (id, library_id, name, sort_name, folder_path) "
            "VALUES (1, 1, 'S', 's', 's')")
        # A series is only on the shelf while it holds something (see
        # library/series_scope.py), so the card fixtures need one item.
        await self.db.execute(
            "INSERT INTO items (id, library_id, series_id, title, sort_title, file_path,"
            " file_format, file_hash, file_size) VALUES"
            " (1, 1, 1, 'V1', 'v1', '/f/v1.cbz', 'cbz', 'h1', 1)")
        await self.db.execute(
            "INSERT INTO novels (id, title, source_url) VALUES (1, 'N', 'https://x/1')")
        await self.db.commit()


class TheCardsAllHaveTheSameShape(_Case):
    async def _home_cards(self):
        context = (await home(_request(), USER, self.db)).context
        return {c["kind"]: c for c in context["cards"]}

    async def test_a_series_card_is_the_same_from_the_grid_and_the_route(self):
        rendered = (await self._home_cards())["series"]
        served = await series_card(1, USER, self.db)
        self.assertEqual(set(rendered), set(served))
        self.assertEqual(rendered, served)

    async def test_a_novel_card_is_the_same_from_the_grid_and_the_route(self):
        rendered = (await self._home_cards())["novel"]
        served = await novel_card(1, USER, self.db)
        self.assertEqual(set(rendered), set(served))
        self.assertEqual(rendered, served)

    async def test_both_kinds_answer_in_one_vocabulary(self):
        # The live builder is ONE function for both kinds, so a key that
        # exists on only one of them is a card that renders differently
        # depending on where it came from.
        cards = await self._home_cards()
        self.assertEqual(set(cards["series"]), set(cards["novel"]))

    async def test_each_kind_carries_the_id_attribute_its_grid_matches_on(self):
        cards = await self._home_cards()
        self.assertEqual(cards["series"]["kind"], "series")
        self.assertEqual(cards["novel"]["kind"], "novel")
        self.assertEqual(cards["novel"]["library_id"], "novels")


def _mounted_paths(routes) -> set[str]:
    """Every path the app actually serves.

    `app.routes` is not a flat list of routes: an `include_router` leaves a
    wrapper behind that carries the real ones on `original_router`, and the
    wrapper has no `.path` at all. Reading `route.path` off the top level
    therefore raised before it could check anything -- the test was red
    against the pinned FastAPI rather than saying whether the routes exist.
    """
    paths = set()
    for route in routes:
        path = getattr(route, "path", None)
        if path:
            paths.add(path)
        inner = getattr(route, "original_router", None)
        if inner is not None:
            paths |= _mounted_paths(inner.routes)
    return paths


class TheRoutesAreActuallyMounted(_Case):
    """Both card endpoints are reached by URL, not just by import: a
    decorator on the wrong function leaves the helper importable and the
    route gone, and every test that calls the function directly stays
    green."""

    def test_both_paths_exist(self):
        from app.main import app
        paths = _mounted_paths(app.routes)
        self.assertIn("/library/series/{series_id}/card", paths)
        self.assertIn("/library/novel/{novel_id}/card", paths)

    def test_the_browser_asks_for_exactly_those(self):
        # The URL is built from the kind in the event, so a rename on
        # either side has to be a rename on both.
        js = EVENTS_JS.read_text()
        self.assertIn("`/library/${kind}/${id}/card`", js)
        self.assertIn('refreshCard("novel"', js)
        self.assertIn('refreshCard("series"', js)


class WhatTheGridCanFind(_Case):
    """The grid holds both kinds and they carry different id attributes,
    so every lookup has to say which one it means -- one that assumed
    data-series-id would never find a novel card."""

    def test_the_grid_api_is_keyed_by_kind(self):
        js = HOME_JS.read_text()
        for name in ("hasCard(kind, id)", "replaceCard(kind, id, cardEl)", "removeCard(kind, id)"):
            self.assertIn(name, js)
        self.assertIn('kind === "novel" ? "novelId" : "seriesId"', js)

    def test_nothing_still_calls_the_series_only_api(self):
        js = EVENTS_JS.read_text() + HOME_JS.read_text()
        for gone in ("hasSeriesCard", "addSeriesCard", "removeSeriesCard", "replaceSeriesCard"):
            self.assertNotIn(gone + "(", js)


if __name__ == "__main__":
    unittest.main()
