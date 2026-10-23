"""Idempotent reading visits, with an append-only accounting ledger.

Session claims use server time. A delayed delivery never makes an old device
the active reader. Each checkpoint carries cumulative visible intervals; only
the new accounting delta is appended to page_reads, keeping its totals triggers
and the existing remaining-time estimator unchanged.
"""
import asyncio
import hashlib
import json
import math
import re
import time
import unicodedata
from datetime import datetime, timedelta

from lxml import html

from app import text_replacements
from app.library.parsers.epub import list_spine_resources
from app.reader.streaming import epub_resource_bytes

TOKEN = re.compile(r'[A-Za-z0-9_-]{16,80}\Z')
SKIP = 'script,style,noscript,template,svg,math,video,audio,canvas,iframe,object,embed'


def word_starts(text):
    """Unicode letter/number groups, internal apostrophes and Han characters.

    Keep source offsets intact, including combining accents and surrogate
    pairs. This is a consistent word-count convention, not a claim about
    dictionary segmentation or comprehension in every language.
    """
    inside = False
    for index, char in enumerate(text):
        category = unicodedata.category(char)[0]
        if '\u3400' <= char <= '\u9fff':
            yield index
            inside = False
        elif category in ('L','N'):
            if not inside:
                yield index
            inside = True
        elif category == 'M' and inside:
            continue
        elif char in "'’" and inside and index + 1 < len(text) and unicodedata.category(text[index+1])[0] in ('L','N'):
            continue
        else:
            inside = False


def token(value):
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise ValueError('Invalid activity identity.')
    return value


async def row(db, sql, args=()):
    return await (await db.execute(sql, args)).fetchone()


async def claim_session(db, user_id, session_id, claim_id, now=None):
    now = time.time() if now is None else now
    token(session_id)
    token(claim_id)
    session = await row(db, 'SELECT * FROM reading_sessions WHERE id=?', (session_id,))
    if session and session['user_id'] != user_id:
        raise ValueError('Session belongs to another account.')
    previous = await row(db, 'SELECT * FROM reading_claims WHERE id=?', (claim_id,))
    if previous:
        if previous['user_id'] != user_id or previous['session_id'] != session_id:
            raise ValueError('Claim belongs to another session.')
        return {'now': now, 'started': previous['started']}
    if not session:
        await db.execute('INSERT INTO reading_sessions VALUES (?,?,?)', (session_id, user_id, now))
    active = await row(db,'SELECT session_id FROM reading_claims WHERE user_id=? AND ended IS NULL',(user_id,))
    if active and active['session_id'] == session_id:
        # Retain the request's idempotency receipt without fragmenting the
        # ownership interval on every touch or page turn in the same reader.
        await db.execute('INSERT INTO reading_claims VALUES (?,?,?,?,?)',(claim_id,session_id,user_id,now,now))
        return {'now':now,'started':now}
    await db.execute('UPDATE reading_claims SET ended=? WHERE user_id=? AND ended IS NULL', (now, user_id))
    await db.execute('INSERT INTO reading_claims VALUES (?,?,?,?,NULL)', (claim_id, session_id, user_id, now))
    return {'now': now, 'started': now}


def normalize(text):
    return ' '.join(text.split())


def readable_text(source, rules):
    # Match the reader's display-only replacements before parsing HTML.
    def replace(match):
        text = match.group(2)
        if text is None:
            return match.group(0)
        for rule in rules:
            text = text.replace(rule['find'], rule['replace'] or '')
        return '>' + text + '<'
    source = re.sub(r'<(script|style)\b[\s\S]*?</\1\s*>|>([^<]+)<', replace, source, flags=re.I)
    doc = html.document_fromstring(source.encode('utf-8'), parser=html.HTMLParser(encoding='utf-8'))
    for node in doc.xpath('//' + '|//'.join(SKIP.split(','))):
        node.drop_tree()
    body = doc.find('body')
    return normalize((body if body is not None else doc).text_content())


def utf16_slice(text, start, end):
    raw = text.encode('utf-16-le')
    if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end <= len(raw) // 2:
        raise ValueError('Invalid passage offsets.')
    return raw[start * 2:end * 2].decode('utf-16-le')


def passage_positions(parts, source):
    if not isinstance(parts, list) or len(parts) > 100:
        raise ValueError('Invalid passage.')
    positions = []
    size = 0
    cursor = 0
    for part in parts:
        text = part['text']
        if not isinstance(text, str):
            raise ValueError('Invalid passage text.')
        size += len(text)
        normalized = normalize(text)
        found = source.find(normalized, cursor)
        if size > 200_000 or found < 0:
            raise ValueError('Passage does not match the reading content.')
        cursor = found + len(normalized)
        start, end = part['start'], part['end']
        utf16_slice(text, start, end)  # Also rejects a split surrogate pair.
        # Count each word by its start, so a word split across pages is not
        # counted twice. UTF-16 is the reader's stable character-offset unit.
        offsets = [0]
        for char in text:
            offsets.append(offsets[-1] + (2 if ord(char) > 0xffff else 1))
        positions.append([offsets[index] for index in word_starts(text) if start <= offsets[index] < end])
    return positions


def passage_words(parts, source):
    return sum(map(len, passage_positions(parts, source)))


async def content(db, user_id, value):
    item_id, chapter_id = value.get('item_id'), value.get('chapter_id')
    if (item_id is None) == (chapter_id is None):
        raise ValueError('Specify one reading source.')
    if item_id is not None:
        item = await row(db, 'SELECT * FROM items WHERE id=?', (item_id,))
        if not item:
            raise ValueError('Reading item no longer exists.')
        if value.get('source_version') is not None and value['source_version'] != item['file_hash']:
            raise ValueError('Reading content has changed.')
        if item['file_format'] == 'cbz':
            page = value.get('page_index')
            if not isinstance(page, int) or page < 0 or (item['page_count'] and page >= item['page_count']):
                raise ValueError('Invalid comic page.')
            return 'cbz', item_id, None, None, []
        if item['file_format'] != 'epub':
            raise ValueError('Unsupported reading format.')
        section = value.get('section_index')
        spine = await asyncio.to_thread(list_spine_resources, item['file_path'])
        if not isinstance(section, int) or not 0 <= section < len(spine):
            raise ValueError('Invalid reading section.')
        raw, _ = await asyncio.to_thread(epub_resource_bytes, item['file_path'], spine[section]['href'])
        source = raw.decode('utf-8-sig', errors='replace')
        rules = await text_replacements.rules_for(db, user_id, series_id=item['series_id'])
        kind, novel_id = 'epub', None
    else:
        chapter = await row(db, 'SELECT novel_id,content_html,content_size,content_fetched_at FROM chapters WHERE id=?', (chapter_id,))
        if not chapter or not chapter['content_html']:
            raise ValueError('Reading chapter no longer exists.')
        kind, novel_id, source = 'novel', chapter['novel_id'], chapter['content_html']
        versions = (hashlib.sha256(source.encode('utf-8')).hexdigest(),
                    f'W/"{chapter_id}-{chapter["content_fetched_at"] or ""}-{chapter["content_size"] or 0}"')
        if value.get('source_version') is not None and value['source_version'] not in versions:
            raise ValueError('Reading content has changed.')
        rules = await text_replacements.rules_for(db, user_id, novel_id=novel_id)
    readable = await asyncio.to_thread(readable_text, source, rules)
    positions = await asyncio.to_thread(passage_positions, value.get('passage'), readable)
    return kind, item_id, novel_id, chapter_id, positions


def intervals(value, now, created):
    data = value.get('intervals')
    if not isinstance(data, list) or not 1 <= len(data) <= 512:
        raise ValueError('Invalid visible intervals.')
    previous = created - 2
    for start, end in data:
        if (not all(isinstance(n, (int, float)) and math.isfinite(n) for n in (start, end))
                or not previous <= start <= end <= now + 2):
            raise ValueError('Invalid interval timestamps.')
        previous = end
    remaining, bounded = 6 * 3600, []
    for start, end in data:
        bounded.append([start, min(end, start + remaining)])
        remaining -= bounded[-1][1] - start
        if remaining <= 0:
            break
    return bounded


async def record_activity(db, user_id, comic_cap, text_cap, value, now=None):
    now = time.time() if now is None else now
    visit = token(value['id'])
    session_id = token(value['session_id'])
    revision = value['revision']
    if type(revision) is not int or not 1 <= revision <= 1_000_000:
        raise ValueError('Invalid activity revision.')
    existing = await row(db, 'SELECT * FROM reading_activity WHERE id=?', (visit,))
    if existing and (existing['user_id'] != user_id or existing['session_id'] != session_id):
        raise ValueError('Activity belongs to another session.')
    if existing and (existing['revision'] >= revision or existing['closed']):
        return
    for key in ('item_id','chapter_id','section_index','page_index','character_offset'):
        number = value.get(key)
        if number is not None and (type(number) is not int or number < 0):
            raise ValueError('Invalid reading position.')
    version = value.get('source_version')
    if version is not None and (not isinstance(version,str) or len(version)>256):
        raise ValueError('Invalid reading content version.')
    session = await row(db, 'SELECT * FROM reading_sessions WHERE id=? AND user_id=?', (session_id, user_id))
    if not session:
        raise ValueError('Unknown reading session.')
    visible = intervals(value, now, session['created'])
    identity_data = {k:value.get(k) for k in ('item_id','chapter_id','section_index','page_index','character_offset','source_version')}
    parts = value.get('passage') or []
    if not isinstance(parts,list) or len(parts) > 100:
        raise ValueError('Invalid passage.')
    if (any(not isinstance(p,dict) or not isinstance(p.get('text'),str)
            or type(p.get('start')) is not int or type(p.get('end')) is not int for p in parts)
            or sum(len(p['text']) for p in parts) > 200_000):
        raise ValueError('Invalid passage.')
    identity_data['passage'] = [{'hash':hashlib.sha256(p['text'].encode('utf-8')).hexdigest(),
                                'start':p['start'],'end':p['end']} for p in parts]
    identity = json.dumps(identity_data,sort_keys=True)
    if existing:
        if identity != existing['identity']:
            raise ValueError('A visit cannot change its reading passage.')
        old = json.loads(existing['intervals'])
        if (visible[:len(old) - 1] != old[:-1] or len(visible) < len(old)
                or visible[len(old) - 1][0] != old[-1][0] or visible[len(old) - 1][1] < old[-1][1]):
            raise ValueError('Checkpoints must extend the recorded visit.')
        kind, item, novel, chapter, words, cap = (existing[k] for k in
            ('kind', 'item_id', 'novel_id', 'chapter_id', 'word_count', 'cap_seconds'))
        word_positions = json.loads(existing['word_positions'])
    else:
        kind, item, novel, chapter, word_positions = await content(db, user_id, value)
        words = sum(map(len,word_positions))
        cap = comic_cap if kind == 'cbz' else max(30, words * text_cap / 300)
    claims = await (await db.execute('''SELECT started,ended FROM reading_claims WHERE session_id=?
        AND (ended IS NULL OR ended>started)''', (session_id,))).fetchall()
    windows = sorted((max(start,c['started']), min(end,c['ended'] if c['ended'] is not None else now))
                     for start,end in visible for c in claims
                     if min(end,c['ended'] if c['ended'] is not None else now) > max(start,c['started']))
    raw = sum(end-start for start,end in windows)
    counted = min(raw, cap)
    previous_raw = existing['raw_seconds'] if existing and existing['raw_seconds'] >= 1 else 0
    previous_counted = existing['counted_seconds'] if existing and existing['raw_seconds'] >= 1 else 0
    closed = value.get('closed') is True
    completed = closed and value.get('completed') is True
    full_session = raw >= sum(end - start for start, end in visible) - 1
    eligible = completed and full_session and raw >= 1
    completed_words = words
    if value.get('traversed_end') is not None:
        edge = value['traversed_end']
        parts = json.loads(identity)['passage']
        part_index, offset = edge['part'], edge['offset']
        if type(part_index) is not int or not 0 <= part_index < len(parts):
            raise ValueError('Invalid traversed passage.')
        if not parts[part_index]['start'] <= offset <= parts[part_index]['end']:
            raise ValueError('Invalid traversed passage offset.')
        completed_words = sum(map(len,word_positions[:part_index])) + sum(p < offset for p in word_positions[part_index])
    credited_words = completed_words if eligible else 0
    pace = eligible and raw < cap and words > 0
    async def append(start, seconds, credit, final_words=0):
        await db.execute('''INSERT INTO page_reads
            (user_id,item_id,novel_id,chapter_id,kind,section_index,page_index,started_at,
             seconds,cap_seconds,visit_id,words,pace_words,pace_seconds,pace_samples)
            VALUES (?,?,?,?,?,?,?,datetime(?,'unixepoch'),?,?,?,?,?,?,?)''',
            (user_id, item, novel, chapter, kind, value.get('section_index'), value.get('page_index'),
             start, seconds, credit, visit, final_words,
             final_words if pace else 0, raw if pace and final_words else 0, int(pace and final_words > 0)))
    # Preserve the actual visible timestamps, including a delayed delivery
    # spanning a hidden interval. Checkpoints consume an already-accounted
    # prefix; they do not move yesterday's time to the day of the retry.
    skip = previous_raw
    available = max(0, counted - previous_counted)
    if raw >= 1:
        for start,end in windows:
            duration = end-start
            if skip >= duration:
                skip -= duration
                continue
            start += skip
            duration -= skip
            skip = 0
            while start < end:
                midnight = datetime.combine(datetime.fromtimestamp(start).date()+timedelta(days=1),datetime.min.time()).timestamp()
                boundary = min(end,midnight)
                duration = boundary-start
                credit = min(duration,available)
                if credit > 0:
                    await append(start,duration,credit)
                available -= credit
                start = boundary
        if credited_words:
            await append(visible[-1][1],0,0,credited_words)
    await db.execute('''INSERT INTO reading_activity
        (id,user_id,session_id,identity,revision,intervals,raw_seconds,counted_seconds,
         cap_seconds,word_count,word_positions,closed,kind,item_id,novel_id,chapter_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,intervals=excluded.intervals,
        raw_seconds=excluded.raw_seconds,counted_seconds=excluded.counted_seconds,closed=excluded.closed''',
        (visit,user_id,session_id,identity,revision,json.dumps(visible),raw,counted,cap,words,
         json.dumps(word_positions),int(closed),kind,item,novel,chapter))


async def record_batch(db, user, activities):
    if not isinstance(activities, list) or len(activities) > 20:
        raise ValueError('Expected at most 20 activities.')
    accepted, rejected = [], []
    for value in activities:
        try:
            if not isinstance(value, dict):
                raise ValueError('Invalid activity.')
            await record_activity(db, user['id'], user['page_time_cap_seconds'],
                                  user['text_time_cap_seconds'], value)
            accepted.append({'id': value['id'], 'revision': value['revision']})
        except (ValueError, KeyError, TypeError, UnicodeError, IndexError, OSError, OverflowError):
            rejected.append(value.get('id') if isinstance(value, dict) else None)
    return {'accepted': accepted, 'rejected': rejected}
