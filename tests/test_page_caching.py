"""A page is KEPT by the browser, and asked about before it is used.

Two directives were available and they are not the same trade. `no-store`
forbids keeping the page at all, which also forbids the **back/forward
cache** -- and that cache is what makes Back instant, restores the DOM
exactly as it was left, and lets the event stream reconnect and replay
what was published meanwhile. `no-cache` forbids only a STALE answer.

What `no-cache` alone costs is a full body on every navigation that is not
a restore, because the browser had nothing to revalidate against. So every
page carries a validator: the rendered body's hash as its `ETag`, and a
request that already holds it is answered 304 with no body -- 45 KB for
the home page, 46 KB for a series, 35 KB for /stats, measured. Two renders
of an unchanged page are byte-identical, which is what makes the hash hit
rather than a nice idea.

Driven in both browsers, a second forward navigation to a page already
seen: the server's own log says `304 Not Modified` in each. And the Back
after it is a bfcache restore with no request at all (Chromium; Playwright's
Firefox cannot bfcache, see CLAUDE.md).
"""
import asyncio
import time
import unittest

import httpx
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from app.main import revalidate_pages

PAGE = "<!doctype html><title>a</title><p>hello"
# Long enough that buffering the stream cannot be mistaken for speed.
STREAM_GAP = 1.0


def _app(body=PAGE):
    async def page(request):
        return HTMLResponse(body)

    async def data(request):
        return JSONResponse({"a": 1})

    async def own(request):
        return HTMLResponse(body, headers={"Cache-Control": "private, max-age=600"})

    async def slow(request):
        async def chunks():
            # Shaped like /library/events: the first frame is there at once
            # and the rest arrives later. Finite only so the test can end --
            # what is being measured is WHEN the first chunk arrives.
            yield b": ping\n\n"
            await asyncio.sleep(STREAM_GAP)
            yield b": ping\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    app = Starlette(routes=[
        Route("/page", page, methods=["GET", "POST"]),
        Route("/data", data),
        Route("/own", own),
        Route("/stream", slow),
    ])
    app.add_middleware(BaseHTTPMiddleware, dispatch=revalidate_pages)
    return app


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


class APageSaysHowToKeepIt(unittest.IsolatedAsyncioTestCase):
    async def test_it_may_be_kept_but_not_used_without_asking(self):
        async with _client(_app()) as c:
            r = await c.get("/page")
        self.assertEqual(r.headers["cache-control"], "private, no-cache")
        # `no-store` is the one that would forbid the back/forward cache.
        self.assertNotIn("no-store", r.headers["cache-control"])

    async def test_a_response_that_chose_its_own_policy_keeps_it(self):
        # The reader's pages and the SSE stream set their own.
        async with _client(_app()) as c:
            r = await c.get("/own")
        self.assertEqual(r.headers["cache-control"], "private, max-age=600")

    async def test_nothing_but_html_is_touched(self):
        async with _client(_app()) as c:
            r = await c.get("/data")
        self.assertNotIn("etag", r.headers)
        self.assertNotIn("cache-control", r.headers)


class TheValidatorIsTheBodyItself(unittest.IsolatedAsyncioTestCase):
    async def test_a_page_carries_one(self):
        async with _client(_app()) as c:
            r = await c.get("/page")
        self.assertTrue(r.headers["etag"].startswith('"'), r.headers.get("etag"))
        self.assertEqual(int(r.headers["content-length"]), len(PAGE))

    async def test_holding_it_gets_a_304_with_no_body(self):
        # THE point of the whole thing.
        async with _client(_app()) as c:
            first = await c.get("/page")
            again = await c.get("/page", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b"")
        self.assertEqual(again.headers["etag"], first.headers["etag"])
        self.assertNotIn("content-length", again.headers,
                         "a 304 with a content-length is lying about a body it has not got")

    async def test_a_page_that_changed_is_sent(self):
        # Control: the 304 above has to be about the CONTENT, not about
        # having been asked twice.
        async with _client(_app()) as c:
            first = await c.get("/page")
        async with _client(_app(PAGE + "<p>more")) as c:
            again = await c.get("/page", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(again.status_code, 200)
        self.assertNotEqual(again.headers["etag"], first.headers["etag"])
        self.assertIn(b"more", again.content)

    async def test_two_renders_of_the_same_page_agree(self):
        # Why the validator hits at all. A per-request nonce, a timestamp
        # or a CSRF token in the markup would make every ETag new and the
        # revalidation pure cost. Measured on the real pages too: /,
        # /series/{id} and /stats each rendered byte-identical twice.
        async with _client(_app()) as c:
            a = await c.get("/page")
            b = await c.get("/page")
        self.assertEqual(a.headers["etag"], b.headers["etag"])

    async def test_only_a_get_is_validated(self):
        # A POST's answer is not a page to come back to, and a 304 to one
        # would be nonsense.
        async with _client(_app()) as c:
            r = await c.post("/page")
        self.assertNotIn("etag", r.headers)


class AStreamIsNeverBuffered(unittest.IsolatedAsyncioTestCase):
    """The one way this middleware could break the whole app.

    It has to read the body to hash it, and `/library/events` has a body
    that does not end. The content-type guard comes first, so a stream is
    handed straight back -- untouched, and with nothing read from it.

    Asserted on the middleware itself rather than through a client:
    httpx's ASGITransport collects the whole response before returning,
    so over that transport a buffering middleware and a streaming one
    look exactly the same. The property that matters is that the same
    object comes back with its iterator still full.
    """

    async def dispatch(self, response):
        """Run the middleware over a response, with a request it ignores."""
        scope = {"type": "http", "method": "GET", "path": "/x",
                 "headers": [], "query_string": b""}
        request = Request(scope)

        async def call_next(_):
            return response

        return await revalidate_pages(request, call_next)

    async def test_a_stream_comes_back_the_same_object_unread(self):
        read = []

        async def chunks():
            # Records being read. Nothing should read it here; the client
            # does, later, one frame at a time.
            read.append(1)
            yield b": ping\n\n"

        stream = StreamingResponse(chunks(), media_type="text/event-stream")
        out = await self.dispatch(stream)
        self.assertIs(out, stream, "the stream was rebuilt, so it was consumed")
        self.assertEqual(read, [], "the middleware read from a body that never ends")
        self.assertNotIn("etag", out.headers)

    async def test_html_is_the_thing_that_does_get_read(self):
        # Control for the test above: it would also pass on a middleware
        # that had stopped doing anything at all.
        out = await self.dispatch(HTMLResponse(PAGE))
        self.assertNotIsInstance(out, StreamingResponse)
        self.assertIn("etag", out.headers)


if __name__ == "__main__":
    unittest.main()
