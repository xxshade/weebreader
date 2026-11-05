"""Revision reads remain coherent across commits and cancelled API leases."""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import aiosqlite
from app.stats.ready_estimates import _revisions as grouped_revisions

def database(path):
    with closing(sqlite3.connect(path)) as db:
        db.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE generation(id INTEGER PRIMARY KEY,epoch TEXT,revision INTEGER);
        INSERT INTO generation VALUES(1,'old',1);
        CREATE VIEW estimate_revision AS SELECT id,hold(epoch) AS epoch,revision FROM generation;
        CREATE TABLE estimate_account_revisions(user_id INTEGER PRIMARY KEY,revision INTEGER);
        INSERT INTO estimate_account_revisions VALUES(1,1);
        CREATE TABLE estimate_work_revisions(user_id INTEGER,kind TEXT,identity INTEGER,revision INTEGER);
        INSERT INTO estimate_work_revisions VALUES(1,'series',2,1);''')


class RevisionReads(unittest.IsolatedAsyncioTestCase):
    async def test_account_isolation_missing_pace_and_generation(self):
        async with aiosqlite.connect(':memory:') as db:
            await db.executescript("""CREATE TABLE estimate_revision(id INTEGER PRIMARY KEY,epoch TEXT,revision INTEGER);
                INSERT INTO estimate_revision VALUES(1,'epoch',2);
                CREATE TABLE estimate_account_revisions(user_id INTEGER PRIMARY KEY,revision INTEGER);
                INSERT INTO estimate_account_revisions VALUES(1,3),(2,99);
                CREATE TABLE estimate_work_revisions(user_id INTEGER,kind TEXT,identity INTEGER,revision INTEGER);
                INSERT INTO estimate_work_revisions VALUES(1,'series',10,4),(1,'novel',20,5),(2,'series',10,999);""")
            self.assertEqual(await grouped_revisions(db,1),(('epoch',2),3,{('series',10):4,('novel',20):5}))
            self.assertEqual(await grouped_revisions(db,3),(('epoch',2),0,{}))
            await db.execute('DELETE FROM estimate_revision')
            with self.assertRaisesRegex(sqlite3.DatabaseError,'Missing estimate generation'):
                await grouped_revisions(db,1)

    async def test_external_commit_during_statement_never_mixes_revisions(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db';database(path)
            started=threading.Event();release=threading.Event();armed=[True]
            def hold(value):
                if armed[0]:
                    armed[0]=False;started.set()
                    if not release.wait(3):raise RuntimeError('test release timed out')
                return value
            async with aiosqlite.connect(path) as db:
                await db.create_function('hold',1,hold)
                task=asyncio.create_task(grouped_revisions(db,1))
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait,2),'query did not reach concurrent boundary')
                    with closing(sqlite3.connect(path)) as writer:
                        writer.execute("UPDATE generation SET epoch='new',revision=2")
                        writer.execute('UPDATE estimate_account_revisions SET revision=2')
                        writer.execute('UPDATE estimate_work_revisions SET revision=2')
                        writer.commit()
                finally:release.set()
                first=await asyncio.wait_for(task,3)
                self.assertEqual(first,(('old',1),1,{('series',2):1}),'mixed revision snapshots')
                self.assertEqual(await grouped_revisions(db,1),(('new',2),2,{('series',2):2}),'external commit not observed next time')
                self.assertFalse(db.in_transaction,'read transaction retained')

    async def test_cancelled_query_connection_recovers_and_reads_external_update(self):
        from app import database as app_database
        await app_database._close_estimate_reader()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db';database(path)
            started=threading.Event();release=threading.Event();armed=[True];opened=[]
            def hold(value):
                if armed[0]:
                    armed[0]=False;started.set()
                    if not release.wait(3):raise RuntimeError('test release timed out')
                return value
            async def connect():
                db=await aiosqlite.connect(path)
                await db.create_function('hold',1,hold)
                opened.append(db)
                return db
            async def request():
                async with app_database.estimate_connection() as db:
                    return await grouped_revisions(db,1)
            with patch.object(app_database,'get_connection',connect):
                task=asyncio.create_task(request())
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait,2))
                    task.cancel()
                    with closing(sqlite3.connect(path)) as writer:
                        writer.execute("UPDATE generation SET epoch='new',revision=2")
                        writer.commit()
                finally:release.set()
                try:
                    with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,3)
                    self.assertIsNone(app_database._estimate_reader,'cancelled reader was retained')
                    result=await asyncio.wait_for(request(),3)
                    self.assertEqual(result[0],('new',2),'new borrow returned old snapshot')
                    self.assertEqual(len(opened),2,'cancelled connection was reused')
                    self.assertFalse(app_database._estimate_reader.in_transaction)
                finally:await app_database._close_estimate_reader()

