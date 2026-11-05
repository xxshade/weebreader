"""Cookie-independent image representations still authenticate every server read."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from app.main import PrivateImageFiles
from tests.test_archives import PNG_1PX


class PrivateImages(unittest.TestCase):
    def test_private_reuse_and_conditional_reads_never_bypass_authentication(self):
        async def authorize(session=None):
            if session not in ('first', 'second'):
                raise HTTPException(status_code=401)
            return {'id': 1 if session == 'first' else 2}

        with tempfile.TemporaryDirectory() as directory:
            for name in ('cover-hash.png', 'avatar-hash.png'):
                (Path(directory) / name).write_bytes(PNG_1PX)
            app = Starlette(routes=[Mount('/covers', PrivateImageFiles(directory=directory))])
            with patch('app.main.get_streaming_user', side_effect=authorize), TestClient(app) as client:
                for name in ('cover-hash.png', 'avatar-hash.png'):
                    url = '/covers/' + name + '?private=2'
                    first = client.get(url, headers={'Cookie': 'session=first; thumbnail-size=140'})
                    second = client.get(url, headers={'Cookie': 'session=second; thumbnail-size=220'})
                    self.assertEqual(first.status_code, 200)
                    self.assertEqual(first.content, second.content)
                    self.assertEqual(first.content, PNG_1PX)
                    self.assertEqual(first.headers['Cache-Control'], 'private, max-age=31536000, immutable')
                    self.assertNotIn('cookie', first.headers.get('Vary', '').lower(),
                                     'Image bytes must not vary by cookie')
                    tag = first.headers['ETag']
                    cached = client.get(url, headers={'Cookie': 'session=first', 'If-None-Match': tag})
                    self.assertEqual(cached.status_code, 304)
                    self.assertNotIn('cookie', cached.headers.get('Vary', '').lower())
                    self.assertEqual(cached.headers['Cache-Control'], first.headers['Cache-Control'])
                    for marker in (1, 2):
                        for conditional in ({}, {'If-None-Match': tag}):
                            denied = client.get('/covers/' + name + f'?private={marker}', headers=conditional)
                            self.assertEqual(denied.status_code, 401,
                                             'Cached validators must not bypass the server session check')
