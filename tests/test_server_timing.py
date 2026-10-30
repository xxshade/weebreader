"""HTML diagnostics measure this request without changing its cache identity."""
import unittest
from unittest.mock import patch

from starlette.requests import Request
from starlette.responses import HTMLResponse, StreamingResponse

from app.main import revalidate_pages
from tests.test_page_caching import PAGE, _app, _client


class PageTiming(unittest.IsolatedAsyncioTestCase):
    async def test_200_and_304_have_fresh_timings_but_the_same_body_validator(self):
        async with _client(_app()) as client:
            with patch('app.main.perf_counter', side_effect=[1, 1.010, 2, 2.125]):
                page = await client.get('/page')
                cached = await client.get('/page', headers={'If-None-Match': page.headers['etag']})
        self.assertEqual(page.headers.get('server-timing'), 'app;dur=10.000')
        self.assertEqual(cached.headers.get('server-timing'), 'app;dur=125.000')
        self.assertEqual(page.content, PAGE.encode())
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.content, b'')
        self.assertNotIn('content-length', cached.headers)
        self.assertEqual(page.headers['etag'], cached.headers['etag'])
        self.assertEqual(cached.headers['cache-control'], 'private, no-cache')

    async def dispatch(self, response, clock):
        request = Request({'type': 'http', 'method': 'GET', 'path': '/page',
                           'headers': [], 'query_string': b''})

        async def route(_):
            clock[0] += 0.020
            return response

        with patch('app.main.perf_counter', side_effect=lambda: clock[0]):
            return await revalidate_pages(request, route)

    async def test_timing_includes_html_body_preparation_for_revalidation(self):
        clock = [1.0]

        async def chunks():
            clock[0] += 0.030
            yield PAGE.encode()

        response = await self.dispatch(StreamingResponse(chunks(), media_type='text/html'), clock)
        self.assertEqual(response.headers.get('server-timing'), 'app;dur=50.000')
        self.assertEqual(response.body, PAGE.encode())

    async def test_json_and_sse_remain_uninstrumented_and_sse_is_not_consumed(self):
        async with _client(_app()) as client:
            response = await client.get('/data')
        self.assertNotIn('server-timing', response.headers)
        consumed = []

        async def events():
            consumed.append(True)
            yield b': heartbeat\n\n'

        stream = StreamingResponse(events(), media_type='text/event-stream')
        response = await self.dispatch(stream, [1.0])
        self.assertIs(response, stream)
        self.assertFalse(consumed)
        self.assertNotIn('server-timing', response.headers)

    async def test_existing_metrics_and_explicit_cache_policy_are_preserved(self):
        response = await self.dispatch(HTMLResponse(PAGE, headers={
            'Server-Timing': 'db;dur=5', 'Cache-Control': 'private, max-age=600',
        }), [1.0])
        self.assertEqual(response.headers.get('server-timing'), 'db;dur=5, app;dur=20.000')
        self.assertEqual(response.headers['cache-control'], 'private, max-age=600')
