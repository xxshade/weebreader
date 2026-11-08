"""Interrupted estimate requests and EPUB namespace repair regressions."""

import unittest
import warnings
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from app.library.router import reading_estimates
from app.reader.html_source import parse_html5
from app.stats.activity import readable_text

SOURCE = '<html xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en"><body><table><tr><td>inside marker</td></tr>outside marker</table><p epub:type="note">tail text</p></body></html>'


class Html5Names(unittest.TestCase):
    def test_real_epub_names_survive_html5_repair_without_coercion(self):
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            doc = parse_html5(SOURCE)
            text = readable_text(SOURCE, [])
        self.assertEqual([str(w.message) for w in seen], [])
        self.assertEqual(doc.get("xml:lang"), "en")
        self.assertEqual(doc.get("xmlns:epub"), "http://www.idpf.org/2007/ops")
        self.assertEqual(doc.xpath("//p")[0].get("epub:type"), "note")
        self.assertLess(text.index("outside marker"), text.index("inside marker"))


class InterruptedInput(unittest.IsolatedAsyncioTestCase):
    async def test_disconnection_during_body_read_is_not_an_asgi_error(self):
        messages = iter(
            [
                {"type": "http.request", "body": b'{"targets":', "more_body": True},
                {"type": "http.disconnect"},
            ]
        )

        async def receive():
            return next(messages)

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/library/reading-estimates",
                "headers": [],
            },
            receive,
        )
        with patch(
            "app.library.router.build_estimator", new_callable=AsyncMock
        ) as build:
            response = await reading_estimates(request, {"id": 1}, None)
        self.assertEqual(response.status_code, 499)
        build.assert_not_awaited()

    async def test_malformed_json_still_returns_400(self):
        async def receive():
            return {"type": "http.request", "body": b"{", "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/library/reading-estimates",
                "headers": [],
            },
            receive,
        )
        with self.assertRaises(HTTPException) as error:
            await reading_estimates(request, {"id": 1}, None)
        self.assertEqual(error.exception.status_code, 400)
