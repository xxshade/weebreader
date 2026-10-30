"""Persist action order within one reader opening, independently of page direction."""
from uuid import UUID
from fastapi import HTTPException


def parse_order(body):
    if 'save_session' not in body and 'save_sequence' not in body:
        return None  # Older tabs and comic clients retain their existing protocol.
    session, sequence = body.get('save_session'), body.get('save_sequence')
    try:
        valid_session = isinstance(session, str) and str(UUID(session)) == session
    except (ValueError, AttributeError):
        valid_session = False
    if not valid_session or type(sequence) is not int or not 1 <= sequence <= 9007199254740991:
        raise HTTPException(400, 'save_session must be a UUID and save_sequence a positive safe integer')
    return session, sequence


async def accept(db, user_id, item_id, order):
    """Run inside the progress write transaction; rollback also rolls back order."""
    if order is None:
        return True
    session, sequence = order
    cursor = await db.execute('''
        INSERT INTO item_progress_saves(user_id,item_id,save_session,save_sequence)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id,item_id,save_session) DO UPDATE
        SET save_sequence=excluded.save_sequence
        WHERE excluded.save_sequence > item_progress_saves.save_sequence
        RETURNING save_sequence
    ''', (user_id, item_id, session, sequence))
    try:
        return await cursor.fetchone() is not None
    finally:
        await cursor.close()
