import tempfile
import unittest
from pathlib import Path
import httpx
from fastapi import FastAPI
from app.auth.service import hash_token
from app.config import settings
from app.database import close_pool, get_connection, init_db
from app.main import PrivateImageFiles

class PrivateImages(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        previous = settings.data_dir
        settings.data_dir = Path(self.temp.name)
        self.addCleanup(lambda: setattr(settings, 'data_dir', previous))
        await close_pool()
        self.addAsyncCleanup(close_pool)
        folder = settings.data_dir / 'covers'
        folder.mkdir()
        (folder / 'avatar-test.webp').write_bytes(b'avatar')
        (folder / 'cover-test.webp').write_bytes(b'cover')
        await init_db()
        db = await get_connection()
        try:
            await db.execute("INSERT INTO users(id, username, password_hash) VALUES(1,'alice','x')")
            await db.execute("INSERT INTO sessions(user_id,token_hash,expires_at,totp_verified) VALUES(1,?,'2999-01-01T00:00:00+00:00',1)", (hash_token('secret'),))
            await db.commit()
        finally:
            await db.close()
        app = FastAPI()
        app.mount('/covers', PrivateImageFiles(directory=str(folder)))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)

    async def test_session_checked_before_cache_revalidation_for_cover_and_avatar(self):
        for path in ('/covers/avatar-test.webp', '/covers/cover-test.webp'):
            self.assertEqual((await self.client.get(path)).status_code, 401)
            self.client.cookies.set('session', 'secret')
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn('private', response.headers['cache-control'])
            self.assertIn('max-age=31536000', response.headers['cache-control'])
            self.assertIn('immutable', response.headers['cache-control'])
            self.assertNotIn('no-cache', response.headers['cache-control'])
            tag = response.headers['etag']
            cached = await self.client.get(path, headers={'If-None-Match': tag})
            self.assertEqual(cached.status_code, 304)
            self.assertIn('private', cached.headers['cache-control'])
            self.assertIn('max-age=31536000', cached.headers['cache-control'])
            self.client.cookies.clear()
            self.assertEqual((await self.client.get(path, headers={'If-None-Match': tag})).status_code, 401)
