import asyncio
import tempfile
import unittest
import zipfile
from pathlib import Path
from app.reader import streaming

class ReaderStreaming(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = str(Path(self.temp.name) / 'book.cbz')
        self.content = b'x' * (200 * 1024)
        with zipfile.ZipFile(self.archive, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('page.jpg', self.content)
            z.writestr('Text/ch.xhtml', b'<p>Chapter</p>')

    async def run_response(self, resource, fail=False):
        response = streaming.ZipStreamingResponse(resource)
        messages = []
        async def send(message):
            if fail and message['type'] == 'http.response.body' and message.get('body'):
                raise OSError('disconnected')
            messages.append(message)
        try:
            await response({'type':'http', 'method':'GET', 'asgi':{'spec_version':'2.4'}}, None, send)
        except Exception:
            if not fail: raise
        return messages

    async def test_comic_page_is_sent_in_chunks_and_closed(self):
        resource = streaming.open_comic_page(self.archive, 0, 'hash')
        messages = await self.run_response(resource)
        chunks = [m.get('body', b'') for m in messages if m['type'] == 'http.response.body']
        self.assertEqual(b''.join(chunks), self.content)
        self.assertGreater(len([chunk for chunk in chunks if chunk]), 1)
        self.assertTrue(resource.closed)

    async def test_disconnect_closes_epub_archive(self):
        resource = streaming.open_epub_resource(self.archive, 'Text/ch.xhtml')
        await self.run_response(resource, fail=True)
        self.assertTrue(resource.closed)

    async def test_corrupt_entry_can_fail_after_response_has_started(self):
        import struct
        raw = bytearray(Path(self.archive).read_bytes())
        central = raw.index(b"PK\x01\x02")
        struct.pack_into("<I", raw, central + 16, 0)
        Path(self.archive).write_bytes(raw)
        resource = streaming.open_comic_page(self.archive, 0, 'corrupt')
        response = streaming.ZipStreamingResponse(resource)
        messages = []
        async def send(message): messages.append(message)
        with self.assertRaises(zipfile.BadZipFile):
            await response({'type':'http', 'method':'GET', 'asgi':{'spec_version':'2.4'}}, None, send)
        self.assertEqual(messages[0]['type'], 'http.response.start')
        self.assertTrue(resource.closed)

    async def test_cancelled_open_closes_eventual_resource(self):
        import threading
        started = threading.Event(); release = threading.Event(); created = []
        def slow_open():
            started.set(); release.wait(3)
            resource = streaming.open_comic_page(self.archive, 0, 'cancel-open')
            created.append(resource)
            return resource
        task = asyncio.create_task(streaming.open_in_thread(slow_open))
        try:
            await asyncio.wait_for(asyncio.to_thread(started.wait), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        finally:
            release.set()
        for _ in range(100):
            if created and created[0].closed: break
            await asyncio.sleep(.01)
        self.assertTrue(created and created[0].closed)

    async def test_missing_entries_do_not_leak(self):
        with self.assertRaises(IndexError): streaming.open_comic_page(self.archive, 2, 'hash')
        with self.assertRaises(KeyError): streaming.open_epub_resource(self.archive, 'missing')

class ReaderRouteStreaming(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from fastapi import FastAPI
        from app.auth.service import hash_token
        from app.config import settings
        from app.database import close_pool, get_connection, init_db
        from app.reader.router import router
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old = settings.data_dir; settings.data_dir = Path(self.temp.name)
        self.addCleanup(lambda: setattr(settings, 'data_dir', old))
        await close_pool(); self.addAsyncCleanup(close_pool)
        await init_db()
        path = Path(self.temp.name) / 'book.cbz'
        with zipfile.ZipFile(path, 'w') as z: z.writestr('1.jpg', b'x' * (200 * 1024))
        db = await get_connection()
        try:
            await db.execute("INSERT INTO users(id,username,password_hash) VALUES(1,'u','x')")
            await db.execute("INSERT INTO sessions(user_id,token_hash,expires_at,totp_verified) VALUES(1,?,'2999-01-01T00:00:00+00:00',1)",(hash_token('token'),))
            await db.execute("INSERT INTO libraries(id,name,root_path,media_type) VALUES(1,'L',?,'comic')",(self.temp.name,))
            await db.execute("INSERT INTO items(id,library_id,title,sort_title,file_path,file_format,file_hash,file_size) VALUES(1,1,'B','b',?,'cbz','h',?)",(str(path),path.stat().st_size))
            await db.commit()
        finally: await db.close()
        self.app = FastAPI(); self.app.include_router(router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)

    async def test_auth_content_and_conditional_request(self):
        self.assertEqual((await self.client.get('/reader/1/page/0')).status_code, 401)
        self.client.cookies.set('session','token')
        response = await self.client.get('/reader/1/page/0?v=h')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'x' * (200 * 1024))
        self.assertIn('immutable', response.headers['cache-control'])
        cached = await self.client.get('/reader/1/page/0',headers={'If-None-Match':response.headers['etag']})
        self.assertEqual(cached.status_code, 304)
        self.assertEqual((await self.client.get('/reader/1/page/3')).status_code, 404)

    async def test_four_slow_streams_do_not_hold_database_connections(self):
        from app.database import read_connection
        gate=asyncio.Event(); starts=asyncio.Queue()
        async def slow_request():
            scope={'type':'http','method':'GET','path':'/reader/1/page/0','raw_path':b'/reader/1/page/0',
                   'query_string':b'', 'root_path':'', 'scheme':'http', 'http_version':'1.1',
                   'client':('test',1),'server':('test',80),'headers':[(b'cookie',b'session=token')],
                   'asgi':{'version':'3.0','spec_version':'2.4'}}
            async def send(message):
                if message['type']=='http.response.body' and message.get('body'):
                    await starts.put(True); await gate.wait()
            async def receive(): return {'type':'http.request','body':b'','more_body':False}
            await self.app(scope,receive,send)
        from unittest.mock import patch
        resources=[]
        original=streaming.open_comic_page
        def capture(*args):
            resource=original(*args); resources.append(resource); return resource
        guard=patch.object(streaming, 'open_comic_page', side_effect=capture)
        guard.start(); self.addCleanup(guard.stop)
        tasks=[asyncio.create_task(slow_request()) for _ in range(4)]
        try:
            for _ in tasks: await asyncio.wait_for(starts.get(), 3)
            async def query():
                async with read_connection() as db: return (await (await db.execute('SELECT 1')).fetchone())[0]
            self.assertEqual(await asyncio.wait_for(query(), 1), 1)
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
        self.assertEqual(len(resources), 4)
        self.assertTrue(all(resource.closed for resource in resources))
