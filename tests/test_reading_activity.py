"""Reading measurements through the actual database and content boundary."""
import copy
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from unittest.mock import patch

from starlette.requests import Request

from app.stats.activity import claim_session, record_activity, passage_words
from app.stats import queries
from app.stats.router import save_reading_time
from app.config import settings
from app.database import close_pool
from tests.test_stats import StatsCase

SESSION = 'session-a-000000000000'


class ReadingActivity(StatsCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.novel_chapter(1, novel_id=1)
        self.text = ' '.join('word' for _ in range(1000))
        await self.db.execute('UPDATE chapters SET content_html=? WHERE id=1', ('<p>'+self.text+'</p>',))
        await claim_session(self.db, 1, SESSION, 'claim-a-000000000000', now=1000)

    def visit(self, identity='visit-a-000000000000', *, words=300, start=1000, seconds=60):
        text = ' '.join('word' for _ in range(words))
        return {'id':identity, 'session_id':SESSION, 'revision':1, 'chapter_id':1,
                'page_index':0, 'character_offset':0,
                'passage':[{'text':text,'start':0,'end':len(text)}],
                'intervals':[[start,start+seconds]], 'closed':False, 'completed':False}

    async def record(self, value, **kwargs):
        await record_activity(self.db, 1, 180, 180, value, now=3000, **kwargs)

    async def test_checkpoints_and_retries_count_one_visit_without_resetting_its_cap(self):
        value = self.visit(seconds=20)
        await self.record(value)
        await self.record(value)  # Response lost after commit.
        value.update(revision=2, intervals=[[1000,1020],[1040,1100]])
        await self.record(value)
        value.update(revision=3, intervals=[[1000,1020],[1040,1100],[1200,1340]], closed=True, completed=True)
        await self.record(value)
        value['revision'] = 4  # A retried final delivery cannot credit words twice.
        await self.record(value)
        totals = await queries.reading_totals(self.db, 1)
        self.assertEqual((totals['seconds'], totals['pages']), (180,1))
        rate = (await queries.text_pace(self.db, 1))
        self.assertEqual((rate['words'], rate['sample_words'], rate['samples']), (300,0,0))

    async def test_weighted_text_pace_combines_formats_and_requires_all_thresholds(self):
        cases = [(999,300,3,None),(1000,299,3,None),(1000,300,2,None),(1000,300,3,200)]
        for n, (words,seconds,samples,expected) in enumerate(cases, 2):
            with self.subTest(words=words,seconds=seconds,samples=samples):
                await self.db.execute('INSERT INTO users(id,username,password_hash) VALUES (?,?,?)', (n,str(n),'x'))
                for index in range(samples):
                    # Unequal sample speeds expose an unweighted mean.
                    w = words // samples if index < samples-1 else words - words//samples*(samples-1)
                    s = 60 if index == 0 else (seconds-60)/(samples-1)
                    await self.db.execute('''INSERT INTO page_reads
                        (user_id,kind,started_at,seconds,cap_seconds,visit_id,words,pace_words,pace_seconds,pace_samples)
                        VALUES (?,?,'2026-10-19',?,?,?, ?,?,?,1)''', (n,'epub' if index % 2 else 'novel',s,s,str(index),w,w,s))
                await self.db.execute("INSERT INTO page_reads(user_id,kind,started_at,seconds,cap_seconds,visit_id) VALUES (?,'cbz','2000-01-01',10,10,'comic')", (n,))
                rate = await queries.text_pace(self.db,n)
                self.assertEqual(rate['wpm'],expected)
                self.assertEqual(rate['sample_words'],words)
                self.assertEqual(rate['samples'],samples)
                self.assertEqual(rate['since'],'2026-10-19')

    async def test_latest_session_wins_and_late_delivery_cannot_take_priority(self):
        await claim_session(self.db,1,'session-b-000000000000','claim-b-000000000000',now=1060)
        await claim_session(self.db,1,'session-b-000000000000','claim-b-repeat-000000',now=1061)
        a = self.visit(seconds=120)
        a.update(closed=True,completed=True)
        b = self.visit('visit-b-000000000000',start=1060,seconds=60)
        b.update(session_id='session-b-000000000000',closed=True,completed=True)
        await self.record(b)
        await self.record(a)
        self.assertEqual((await queries.reading_totals(self.db,1))['seconds'],120)
        self.assertEqual((await queries.text_pace(self.db,1))['sample_words'],300)
        active = await (await self.db.execute('SELECT session_id FROM reading_claims WHERE ended IS NULL')).fetchone()
        self.assertEqual(active[0],b['session_id'])
        await claim_session(self.db,1,SESSION,'claim-a-return-000000',now=1200)
        await claim_session(self.db,1,b['session_id'],'claim-b-repeat-000000',now=1300)
        active = await (await self.db.execute('SELECT session_id FROM reading_claims WHERE ended IS NULL')).fetchone()
        self.assertEqual(active[0],SESSION,'Replaying an acknowledged claim must not take session priority')
        await self.db.execute("INSERT INTO users(id,username,password_hash) VALUES (2,'other','x')")
        with self.assertRaisesRegex(ValueError,'another account'):
            await claim_session(self.db,2,SESSION,'claim-c-000000000000',now=1200)
        with self.assertRaisesRegex(ValueError,'another session'):
            await record_activity(self.db,2,180,180,a,now=3000)

    async def test_partial_reading_and_source_validation_do_not_invent_words(self):
        incomplete = self.visit(seconds=60)
        incomplete.update(closed=True)
        await self.record(incomplete)
        self.assertEqual((await queries.text_pace(self.db,1))['words'],0)
        reread = self.visit('visit-reread-00000000',start=1100,seconds=60)
        reread.update(closed=True,completed=True,traversed_end={'part':0,'offset':749})
        await self.record(reread)
        self.assertEqual((await queries.text_pace(self.db,1))['sample_words'],150)
        invalid = self.visit('visit-invalid-0000000')
        invalid['passage'][0]['text']='Words absent from the source'
        invalid['passage'][0]['end']=len(invalid['passage'][0]['text'])
        with self.assertRaisesRegex(ValueError,'does not match'):
            await self.record(invalid)
        invalid['passage'][0]['text'] = 42
        with self.assertRaisesRegex(ValueError,'Invalid passage'):
            await self.record(invalid)
        changed = self.visit('visit-version-0000000')
        changed['source_version'] = 'a different chapter version'
        with self.assertRaisesRegex(ValueError,'content has changed'):
            await self.record(changed)
        # Surrogates and a word split between pages must not duplicate words.
        text = '😀 wonderful words'
        first = passage_words([{'text':text,'start':0,'end':8}],text)
        second = passage_words([{'text':text,'start':8,'end':len(text)+1}],text)
        self.assertEqual((first,second),(1,1))
        text = 'élan don’t 你好'
        self.assertEqual(passage_words([{'text':text,'start':0,'end':len(text)}],text),4)

    async def test_legacy_time_and_new_activity_survive_deleted_content(self):
        await self.db.execute('''INSERT INTO page_reads(user_id,chapter_id,novel_id,kind,started_at,seconds,cap_seconds)
            VALUES (1,1,1,'novel','2026-01-01',500,180)''')
        value = self.visit(seconds=30)
        await self.record(value)
        await self.db.execute('DELETE FROM chapters WHERE id=1')
        value.update(revision=2,intervals=[[1000,1060]],closed=True,completed=True)
        await self.record(value)
        self.assertEqual((await queries.reading_totals(self.db,1))['seconds'],240)
        rate = (await queries.text_pace(self.db,1))
        self.assertEqual((rate['words'],rate['sample_seconds']), (300,60))
        self.assertIsNone(rate['wpm'])

    async def test_an_older_checkpoint_cannot_rewind_a_visit(self):
        value = self.visit(seconds=60)
        old = copy.deepcopy(value)
        value.update(revision=2, intervals=[[1000,1120]])
        await self.record(value)
        await self.record(old)
        self.assertEqual((await queries.reading_totals(self.db,1))['seconds'],120)
        self.assertEqual((await queries.reading_totals(self.db,1))['pages'],1)

    async def test_delayed_activity_keeps_time_on_each_original_day(self):
        midnight = datetime(2026,10,20).timestamp()
        value = self.visit(start=midnight-10,seconds=40)
        value['intervals'].append([midnight+100,midnight+110])
        await record_activity(self.db,1,180,180,value,now=midnight+500)
        today = datetime.fromtimestamp(midnight).date()
        days = await queries.daily_activity(self.db,1,start=today-timedelta(days=1),end=today)
        self.assertEqual([d['seconds'] for d in days],[10,40])
        totals = await queries.reading_totals(self.db,1)
        self.assertEqual((totals['seconds'],totals['pages'],totals['days']),(50,1,2))

    async def test_only_committed_new_pace_evidence_refreshes_estimates(self):
        self.addAsyncCleanup(close_pool)
        await self.db.commit()
        user = {'id': 1, 'page_time_cap_seconds': 180, 'text_time_cap_seconds': 180}
        published = []

        def publish(event):
            # A separate connection can see the evidence only after commit.
            with closing(sqlite3.connect(settings.db_path)) as connection:
                words = connection.execute('SELECT SUM(pace_words) FROM page_reads').fetchone()[0]
            self.assertGreater(words or 0, 0, 'Estimate refresh must follow the committed pace evidence')
            published.append(event)

        async def send(value):
            async def receive():
                return {'type': 'http.request', 'body': json.dumps({'activities': [value]}).encode()}
            request = Request({'type': 'http', 'method': 'POST', 'path': '/reading-time',
                               'headers': [], 'query_string': b''}, receive)
            result = await save_reading_time(request, user=user)
            self.assertNotIn('pace_changed', result)
            self.assertEqual(result['rejected'], [])

        with patch('app.stats.router.events.publish', side_effect=publish):
            value = self.visit(seconds=20)
            await send(value)
            self.assertEqual(published, [])
            value.update(revision=2, intervals=[[1000,1060]], closed=True, completed=True)
            await send(value)
            self.assertEqual(published, [{'type': 'reading_estimate_updated', 'user_id': 1}])
            await send(value)
            value['revision'] = 3
            await send(value)
            capped = self.visit('visit-capped-event-0000', start=1100, seconds=240)
            capped.update(closed=True, completed=True)
            await send(capped)
            self.assertEqual(len(published), 1, 'Retries and capped passages must not refresh the learned pace')
