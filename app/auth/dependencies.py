from datetime import datetime, timezone

from fastapi import Cookie, Depends, HTTPException, status

from app.auth.service import hash_token
from app.database import get_db, read_connection, write_transaction


def _parse_utc(timestamp: str) -> datetime:
    dt = datetime.fromisoformat(timestamp)
    if dt.tzinfo is None:
        # Defensive: every expires_at this app writes itself already carries
        # a UTC offset (see service.session_expiry), but don't let an
        # unexpected naive timestamp crash the comparison below with
        # "can't compare offset-naive and offset-aware datetimes" -- treat
        # it as UTC, consistent with every other timestamp in this schema.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def get_current_user(session: str | None = Cookie(default=None), db=Depends(get_db)):
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    token_hash = hash_token(session)
    cursor = await db.execute(
        """
        SELECT users.id AS user_id, users.username AS username,
               users.avatar_path AS avatar_path,
               users.page_time_cap_seconds AS page_time_cap_seconds,
               users.text_time_cap_seconds AS text_time_cap_seconds,
               users.cover_blur_px AS cover_blur_px,
               sessions.expires_at AS expires_at
        FROM sessions
        JOIN users ON users.id = sessions.user_id
        WHERE sessions.token_hash = ?
        """,
        (token_hash,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session")

    # expires_at was always computed and stored (see service.session_expiry)
    # but never actually checked here -- a session was valid forever
    # regardless of session_lifetime_hours. Expired rows are deleted on
    # the way out instead of just left to accumulate, since this is the
    # one place guaranteed to notice a session has gone stale.
    if row["expires_at"] and _parse_utc(row["expires_at"]) <= datetime.now(timezone.utc):
        async with write_transaction() as wdb:
            await wdb.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            await wdb.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")

    # avatar_path, page_time_cap_seconds and cover_blur_px ride along on
    # this one query rather than being fetched per page: base.html renders
    # the avatar in the header of EVERY authenticated page, the readers
    # need the cap on every page-timing flush, and the home grid publishes
    # the blur radius before it draws a single cover. All three are single
    # columns on a row this join already has in hand.
    #
    # This dict is what the templates read, and a key missing from it
    # renders as NOTHING rather than failing -- `--cover-blur: px` is a
    # declaration the browser drops in silence, taking the blur with it
    # (measured, adding this very column).
    return {
        "id": row["user_id"],
        "username": row["username"],
        "avatar_path": row["avatar_path"],
        "page_time_cap_seconds": row["page_time_cap_seconds"],
        "text_time_cap_seconds": row["text_time_cap_seconds"],
        "cover_blur_px": row["cover_blur_px"],
    }


async def get_streaming_user(session: str | None = Cookie(default=None)):
    """get_current_user for a route that STREAMS its response.

    Through get_db, the connection that checked the session would stay
    borrowed for as long as the stream stays open -- FastAPI closes a yield
    dependency only once the response has been sent, and an event stream
    is sent for as long as the tab is open. Four open tabs held the whole
    pool, and every other request in the app then waited forever
    (measured: the home page stopped answering at the fourth stream).
    This borrows one for the check alone.
    """
    async with read_connection() as db:
        return await get_current_user(session=session, db=db)
