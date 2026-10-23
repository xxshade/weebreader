import asyncio
import base64
import io
import logging
import time

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import qrcode


from app.templating import templates
from app.auth import service
from app.auth.dependencies import get_current_user
from app.config import settings
from app.covers.thumbnails import delete_avatar, save_avatar
from app.database import get_db, write_transaction
from app.http_utils import is_htmx, json_object, safe_redirect_target
from webauthn.helpers import bytes_to_base64url, options_to_json

logger = logging.getLogger("auth")

router = APIRouter(tags=["auth"])


# ---------- Password login rate limiting ----------
# Deliberately in-memory, not persisted to the database: this is a
# single-process app, and losing this state on a restart just gives a
# would-be attacker a fresh window, not a bypass of anything that matters.
# Keyed by username rather than client IP -- this app typically sits
# behind a reverse proxy/VPN where the "real" client IP isn't always
# reliably available, and the actual goal is protecting the one account
# that exists, not distinguishing between individual attackers.
_failed_login_attempts: dict[str, list[float]] = {}
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60  # attempts older than this no longer count toward the limit
LOGIN_LOCKOUT_SECONDS = 5 * 60  # once tripped, how long the account stays locked out
# How many names are remembered at most. Every failed attempt used to add
# its username for good, so a script trying names could grow this without
# end; names whose attempts have all expired are dropped first, then the
# ones tried longest ago.
LOGIN_TRACKED_NAMES = 1000


def _check_login_rate_limit(username: str) -> None:
    now = time.monotonic()
    key = username.lower()
    attempts = [t for t in _failed_login_attempts.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
    _failed_login_attempts[key] = attempts
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        retry_after = int(LOGIN_LOCKOUT_SECONDS - (now - attempts[-1])) + 1
        if retry_after > 0:
            raise HTTPException(429, f"Too many failed login attempts. Try again in {retry_after}s.")


def _record_failed_login(username: str) -> None:
    now = time.monotonic()
    _failed_login_attempts.setdefault(username.lower(), []).append(now)
    if len(_failed_login_attempts) <= LOGIN_TRACKED_NAMES:
        return
    for key in [k for k, times in _failed_login_attempts.items()
                if now - times[-1] >= LOGIN_WINDOW_SECONDS]:
        del _failed_login_attempts[key]
    while len(_failed_login_attempts) > LOGIN_TRACKED_NAMES:
        oldest = min(_failed_login_attempts, key=lambda k: _failed_login_attempts[k][-1])
        del _failed_login_attempts[oldest]


def _clear_login_attempts(username: str) -> None:
    _failed_login_attempts.pop(username.lower(), None)


async def _get_single_user(db):
    cursor = await db.execute("SELECT * FROM users LIMIT 1")
    return await cursor.fetchone()


# ---------- Sessions and passkey challenges ----------

def _set_session_cookie(response: Response, token: str) -> None:
    """The cookie lives exactly as long as the session row it names. It was
    fixed at 30 days whatever `session_lifetime_hours` said: shorter, the
    browser kept presenting a dead session; longer, it dropped a live one."""
    response.set_cookie(
        "session", token, httponly=True, samesite="lax", secure=settings.secure_cookies,
        max_age=settings.session_lifetime_hours * 3600,
    )


async def _start_session(wdb, user_id: int) -> str:
    """A new session for `user_id`, inside the caller's write transaction;
    returns the token for the cookie. Expired sessions are swept here: one
    is otherwise deleted only when it is presented again, and a device that
    never comes back left its row for good."""
    token, token_hash = service.new_session_token()
    await wdb.execute("DELETE FROM sessions WHERE expires_at <= ?", (service.utc_now_iso(),))
    await wdb.execute(
        "INSERT INTO sessions (user_id, token_hash, expires_at, totp_verified) VALUES (?, ?, ?, 1)",
        (user_id, token_hash, service.session_expiry()),
    )
    return token


# How long a passkey ceremony may take from begin to complete.
CHALLENGE_SECONDS = 5 * 60
# Which login challenge is THIS browser's. The table used to hold one, for
# everybody: each begin deleted the pending one, so anyone could keep the
# owner's passkey login failing with unauthenticated POSTs. Now each begin
# adds its own row and the browser names it, so another begin removes
# nothing but expired rows.
PASSKEY_CHALLENGE_COOKIE = "passkey_challenge"


async def _issue_challenge(wdb, user_id: int | None, purpose: str, challenge: str) -> None:
    await wdb.execute(
        "DELETE FROM webauthn_challenges WHERE created_at <= datetime('now', ?)",
        (f"-{CHALLENGE_SECONDS} seconds",))
    await wdb.execute(
        "INSERT INTO webauthn_challenges (user_id, challenge, purpose) VALUES (?, ?, ?)",
        (user_id, challenge, purpose))


async def _consume_challenge(purpose: str, *, challenge: str | None = None,
                             user_id: int | None = None) -> str | None:
    """Takes a pending, unexpired challenge out of the table and returns it,
    in one statement, so two completions cannot both use it."""
    clause, value = ("challenge = ?", challenge) if challenge is not None else ("user_id = ?", user_id)
    if value is None:
        return None
    async with write_transaction() as wdb:
        row = await (await wdb.execute(
            f"DELETE FROM webauthn_challenges WHERE purpose = ? AND {clause}"
            " AND created_at > datetime('now', ?) RETURNING challenge",
            (purpose, value, f"-{CHALLENGE_SECONDS} seconds"))).fetchone()
        await wdb.commit()
    return row["challenge"] if row else None


# ---------- First run: creates the system's single user ----------

@router.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request, db=Depends(get_db)):
    if await _get_single_user(db) is not None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "auth/setup.html", {})


@router.post("/setup")
async def setup_submit(username: str = Form(...), password: str = Form(...)):
    async with write_transaction() as db:
        if await _get_single_user(db) is not None:
            # A second submit, or a stale tab: the account exists, and the
            # page that answers that is the login page.
            return RedirectResponse("/login", status_code=302)
        await db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, await asyncio.to_thread(service.hash_password, password)),
        )
        await db.commit()
    return RedirectResponse("/login", status_code=302)


# ---------- Password login (+ TOTP if enabled) ----------

@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str | None = None, db=Depends(get_db)):
    if await _get_single_user(db) is None:
        return RedirectResponse("/setup", status_code=302)
    return templates.TemplateResponse(request, "auth/login.html", {"next": next})


def _safe_next_path(next: str | None) -> str:
    # Only ever redirects to a path within this app -- `next` comes from a
    # query string/form field an attacker could set. See
    # http_utils.safe_redirect_target, the one copy of that rule.
    return safe_redirect_target(next) or "/"


def _login_refused(request: Request, next: str | None, username: str, error: str, status: int):
    """The login page again, saying why. Rendered rather than raised: the
    form is a plain POST (login.html opts out of hx-boost), and an
    HTTPException is a JSON body -- which, boosted, htmx never swapped
    (4xx is in noSwap), so a wrong password, a bad code and the lockout
    all looked like nothing happening at all."""
    return templates.TemplateResponse(
        request, "auth/login.html", {"next": next, "username": username, "error": error},
        status_code=status)


@router.post("/login")
async def login_submit(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    totp_code: str | None = Form(default=None),
    next: str | None = Form(default=None),
    db=Depends(get_db),
):
    try:
        _check_login_rate_limit(username)
    except HTTPException as refused:
        return _login_refused(request, next, username, refused.detail, refused.status_code)

    cursor = await db.execute("SELECT * FROM users WHERE username = ?", (username,))
    user = await cursor.fetchone()
    # In a thread: argon2 is slow on purpose (tens of ms), and on the event
    # loop every request in the app waited for each attempt. A name that
    # matches nobody is checked against a dummy hash, so it costs the same.
    valid = await asyncio.to_thread(
        service.verify_password, password, user["password_hash"] if user else None)
    if not valid:
        _record_failed_login(username)
        return _login_refused(request, next, username, "Invalid username or password", 401)

    totp_cursor = await db.execute(
        "SELECT * FROM totp_credentials WHERE user_id = ? AND enabled = 1", (user["id"],)
    )
    totp = await totp_cursor.fetchone()
    step = service.totp_step(totp["secret"], totp_code or "") if totp is not None else None
    if totp is not None and step is None:
        _record_failed_login(username)
        return _login_refused(request, next, username, "Invalid TOTP code", 401)

    async with write_transaction() as wdb:
        if totp is not None:
            # Claimed in the same statement that checks it, so two logins
            # racing with one code cannot both see it unused.
            claimed = await wdb.execute(
                "UPDATE totp_credentials SET last_used_step = ?"
                " WHERE user_id = ? AND (last_used_step IS NULL OR last_used_step < ?)",
                (step, user["id"], step),
            )
            if claimed.rowcount != 1:
                _record_failed_login(username)
                return _login_refused(request, next, username,
                                      "That code was already used -- wait for the next one", 401)
        token = await _start_session(wdb, user["id"])
        await wdb.commit()
    _clear_login_attempts(username)

    response = RedirectResponse(_safe_next_path(next), status_code=302)
    _set_session_cookie(response, token)
    return response


@router.post("/logout")
async def logout(response: Response, session: str | None = Cookie(default=None)):
    # session used to be a plain function parameter here (not decorated
    # with Cookie(...)), so FastAPI read it as a query param -- it was
    # always None in practice, and the session row in the database was
    # never actually deleted, only the browser's cookie was cleared. A
    # copied/leaked token would keep authenticating forever even after
    # "logging out". Cookie(default=None) is what makes this read the
    # real cookie.
    if session:
        async with write_transaction() as db:
            await db.execute("DELETE FROM sessions WHERE token_hash = ?", (service.hash_token(session),))
            await db.commit()

    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session")
    return response


# ---------- Passkey login (usernameless -- consistent with a single-user system) ----------

@router.post("/login/passkey/begin")
async def login_passkey_begin(db=Depends(get_db)):
    cursor = await db.execute("SELECT credential_id FROM webauthn_credentials")
    credential_ids = [row["credential_id"] for row in await cursor.fetchall()]
    if not credential_ids:
        raise HTTPException(400, "No passkey registered")

    options = service.build_authentication_options(credential_ids)
    challenge = bytes_to_base64url(options.challenge)

    async with write_transaction() as wdb:
        # user_id is NULL here on purpose: in usernameless login we don't yet
        # know which credential will be used until the authenticator responds.
        await _issue_challenge(wdb, None, "authentication", challenge)
        await wdb.commit()

    response = Response(content=options_to_json(options), media_type="application/json")
    response.set_cookie(
        PASSKEY_CHALLENGE_COOKIE, challenge, httponly=True, samesite="strict",
        secure=settings.secure_cookies, max_age=CHALLENGE_SECONDS,
    )
    return response


@router.post("/login/passkey/complete")
async def login_passkey_complete(request: Request, db=Depends(get_db),
                                 passkey_challenge: str | None = Cookie(default=None)):
    # NOT rate limited. It used to share one bucket for everyone, and there
    # is nothing here to guess -- an assertion is the authenticator's
    # signature over a challenge this server just issued -- so all the
    # bucket did was let anyone lock passkey login out: five bogus POSTs
    # with no session answered 429 to the owner for five minutes, renewable
    # forever (measured with curl). A password guess is still limited by
    # username in login_submit.
    try:
        credential_json = json_object(await request.json())
    except ValueError:
        raise HTTPException(400, "Invalid authentication response")
    credential_id = credential_json.get("id")
    if not credential_id:
        raise HTTPException(400, "Invalid authentication response")

    cred_cursor = await db.execute(
        "SELECT * FROM webauthn_credentials WHERE credential_id = ?", (credential_id,)
    )
    credential = await cred_cursor.fetchone()
    if credential is None:
        raise HTTPException(401, "Unknown passkey")

    challenge = await _consume_challenge("authentication", challenge=passkey_challenge)
    if challenge is None:
        raise HTTPException(400, "No pending challenge -- reload the page and try again")

    try:
        verification = service.verify_authentication(
            credential_json, challenge, credential["public_key"], credential["sign_count"]
        )
    except Exception:
        raise HTTPException(401, "Passkey verification failed")

    user_cursor = await db.execute("SELECT * FROM users WHERE id = ?", (credential["user_id"],))
    user = await user_cursor.fetchone()

    async with write_transaction() as wdb:
        await wdb.execute(
            "UPDATE webauthn_credentials SET sign_count = ?, last_used_at = datetime('now') WHERE id = ?",
            (verification.new_sign_count, credential["id"]),
        )
        token = await _start_session(wdb, user["id"])
        await wdb.commit()

    response = JSONResponse({"status": "ok", "redirect": "/"})
    response.delete_cookie(PASSKEY_CHALLENGE_COOKIE)
    _set_session_cookie(response, token)
    return response


# ---------- TOTP: setup ----------

@router.get("/settings/totp", response_class=HTMLResponse)
async def totp_setup_form(request: Request, user=Depends(get_current_user)):
    return _totp_setup_page(request, service.new_totp_secret(), user["username"])


def _totp_setup_page(request: Request, secret: str, username: str, error: str | None = None,
                     status: int = 200):
    """The setup page for `secret` -- the SAME secret again after a wrong
    code, so the authenticator the reader already scanned stays valid."""
    uri = service.totp_provisioning_uri(secret, username)
    # `uri` was already being computed and passed to the template, but the
    # template never rendered it -- the page just showed the raw secret as
    # text to type in by hand, even though `qrcode` (already a dependency,
    # never actually imported anywhere) makes turning that URI into a
    # scannable code a couple of lines of work.
    qr_buf = io.BytesIO()
    qrcode.make(uri).save(qr_buf, format="PNG")
    qr_data_uri = "data:image/png;base64," + base64.b64encode(qr_buf.getvalue()).decode("ascii")
    return templates.TemplateResponse(
        request, "auth/setup_totp.html",
        {"secret": secret, "uri": uri, "qr_data_uri": qr_data_uri, "error": error},
        status_code=status,
    )


@router.post("/settings/totp")
async def totp_setup_submit(
    request: Request, secret: str = Form(...), code: str = Form(...), user=Depends(get_current_user)
):
    step = service.totp_step(secret, code)
    if step is None:
        return _totp_setup_page(request, secret, user["username"],
                                "Invalid code -- check your device's clock", 400)
    async with write_transaction() as db:
        # The code that confirmed the setup counts as used: it cannot then
        # sign in as well.
        await db.execute(
            """
            INSERT INTO totp_credentials (user_id, secret, enabled, last_used_step) VALUES (?, ?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET secret = excluded.secret, enabled = 1,
                last_used_step = excluded.last_used_step
            """,
            (user["id"], secret, step),
        )
        await db.commit()
    return RedirectResponse("/", status_code=302)


# ---------- Passkeys (WebAuthn) ----------

@router.post("/settings/passkey/register/begin")
async def passkey_register_begin(user=Depends(get_current_user), db=Depends(get_db)):
    cursor = await db.execute(
        "SELECT credential_id FROM webauthn_credentials WHERE user_id = ?", (user["id"],)
    )
    existing = [row["credential_id"] for row in await cursor.fetchall()]
    options = service.build_registration_options(user["id"], user["username"], existing)

    async with write_transaction() as wdb:
        # One pending registration per account: only its own sessions can
        # replace it.
        await wdb.execute(
            "DELETE FROM webauthn_challenges WHERE user_id = ? AND purpose = 'registration'", (user["id"],)
        )
        await _issue_challenge(wdb, user["id"], "registration", bytes_to_base64url(options.challenge))
        await wdb.commit()

    return Response(content=options_to_json(options), media_type="application/json")


@router.post("/settings/passkey/register/complete")
async def passkey_register_complete(
    request: Request, nickname: str = "Passkey", user=Depends(get_current_user)
):
    try:
        credential_json = json_object(await request.json())
    except ValueError:
        raise HTTPException(400, "Invalid registration response")
    challenge = await _consume_challenge("registration", user_id=user["id"])
    if challenge is None:
        raise HTTPException(400, "No pending challenge")

    try:
        verification = service.verify_registration(credential_json, challenge)
    except Exception:
        # A malformed or forged response was an unhandled 500.
        raise HTTPException(400, "Passkey registration failed")

    async with write_transaction() as wdb:
        await wdb.execute(
            """
            INSERT INTO webauthn_credentials (user_id, credential_id, public_key, sign_count, nickname)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                bytes_to_base64url(verification.credential_id),
                bytes_to_base64url(verification.credential_public_key),
                verification.sign_count,
                nickname,
            ),
        )
        await wdb.commit()
    return {"status": "ok"}


# ---------- Security page: manage TOTP and passkeys ----------

@router.get("/settings/security", response_class=HTMLResponse)
async def security_settings(request: Request, user=Depends(get_current_user), db=Depends(get_db)):
    cursor = await db.execute("SELECT * FROM totp_credentials WHERE user_id = ?", (user["id"],))
    totp = await cursor.fetchone()

    cursor = await db.execute(
        "SELECT * FROM webauthn_credentials WHERE user_id = ? ORDER BY created_at", (user["id"],)
    )
    passkeys = await cursor.fetchall()

    return templates.TemplateResponse(
        request, "auth/security.html", {"user": user, "totp": totp, "passkeys": passkeys}
    )


@router.post("/settings/totp/disable")
async def totp_disable(user=Depends(get_current_user)):
    async with write_transaction() as db:
        await db.execute("UPDATE totp_credentials SET enabled = 0 WHERE user_id = ?", (user["id"],))
        await db.commit()
    return RedirectResponse("/settings/security", status_code=302)


@router.post("/settings/passkey/{credential_pk}/delete")
async def passkey_delete(credential_pk: int, user=Depends(get_current_user)):
    async with write_transaction() as db:
        await db.execute(
            "DELETE FROM webauthn_credentials WHERE id = ? AND user_id = ?", (credential_pk, user["id"])
        )
        await db.commit()
    return RedirectResponse("/settings/security", status_code=302)


# ---------- Profile: avatar and reading-time preferences ----------
# Both are columns on `users`, and this module already owns the user's own
# account settings (/settings/totp, /settings/security). The forms that
# drive them render on the main settings page, which library/router.py
# serves -- the split follows the data, not the template.

# The same ceiling the series cover upload uses. An avatar is re-encoded to
# 128x128 either way, so this only bounds what has to be read into memory
# before that happens.
MAX_AVATAR_UPLOAD_BYTES = 10 * 1024 * 1024

# The per-page reading-time cap (see schema.sql's page_reads). The floor is
# a real limit rather than politeness: below a few seconds almost every
# page would be truncated and the statistics would measure the cap instead
# of the reading. The ceiling keeps one absurd value from making a single
# forgotten tab outweigh a month of real reading.
MIN_PAGE_TIME_CAP = 5
MAX_PAGE_TIME_CAP = 3600

# How hard a blurred cover is blurred, in CSS pixels of blur radius (see
# series.blur_cover). The floor is where the setting stops meaning
# anything -- at 1px a cover is still perfectly recognisable, which is a
# blur that lies about what it does. The ceiling is where more stops
# changing the picture: past it every cover is already one flat colour,
# and the cost is only that more of the image is thrown away at the edges.
MIN_COVER_BLUR = 4
MAX_COVER_BLUR = 40


def _settings_saved(request: Request, error: str | None = None):
    """The answer to a settings save.

    A setting that changes nothing on the page it lives on has no reason
    to reload it: htmx gets a 204 and the form's own "Saved" flash, which
    is the standard every plain preference on that page now follows.
    Losing your place in a long page to be told a slider moved is the
    whole complaint this fixes.

    Without htmx it is still the redirect it always was, so the forms keep
    working as plain posts -- and an ERROR is always the redirect, because
    the message lives in the re-rendered page and there is nowhere to put
    it in a 204.
    """
    if error is None and is_htmx(request):
        return Response(status_code=204)
    return _settings_redirect(error)


def _settings_redirect(error: str | None = None) -> RedirectResponse:
    # The no-JavaScript answer (and every refusal): the settings page is
    # server-rendered, so the result of a save is the re-rendered page.
    # The code is what the template turns into a message.
    return RedirectResponse(
        "/settings" + (f"?profile_error={error}" if error else "#profile"), status_code=302
    )


@router.post("/settings/avatar")
async def upload_avatar(avatar: UploadFile = File(...), user=Depends(get_current_user)):
    """Replaces the user's avatar with an uploaded image.

    Goes through the same encoder as every other image this app stores
    (centre-cropped square, 128x128 WebP) rather than being kept as sent:
    it renders in a slot a couple of dozen pixels tall, and the source is
    whatever the user happened to have on hand -- typically a photo orders
    of magnitude larger than the slot.
    """
    data = await avatar.read(MAX_AVATAR_UPLOAD_BYTES + 1)
    if not data:
        return _settings_redirect("no-file")
    if len(data) > MAX_AVATAR_UPLOAD_BYTES:
        return _settings_redirect("too-large")

    try:
        relative_path = await save_avatar(data, user["id"])
    except Exception:
        # Not an image, or one Pillow cannot decode. The current avatar is
        # untouched, so there is nothing to roll back.
        logger.exception("Could not store uploaded avatar for user %s", user["id"])
        return _settings_redirect("not-an-image")

    async with write_transaction() as db:
        cursor = await db.execute("SELECT avatar_path FROM users WHERE id = ?", (user["id"],))
        previous = (await cursor.fetchone())["avatar_path"]
        await db.execute(
            "UPDATE users SET avatar_path = ?, updated_at = datetime('now') WHERE id = ?",
            (relative_path, user["id"]),
        )
        await db.commit()

    # Only when it was actually replaced: the name is a hash of the encoded
    # bytes, so re-uploading the same picture resolves to the same file and
    # deleting it would remove the one just written.
    if previous != relative_path:
        delete_avatar(previous)
    return _settings_redirect()


@router.post("/settings/avatar/remove")
async def remove_avatar(user=Depends(get_current_user)):
    """Back to the username in the header."""
    async with write_transaction() as db:
        cursor = await db.execute("SELECT avatar_path FROM users WHERE id = ?", (user["id"],))
        previous = (await cursor.fetchone())["avatar_path"]
        await db.execute(
            "UPDATE users SET avatar_path = NULL, updated_at = datetime('now') WHERE id = ?",
            (user["id"],),
        )
        await db.commit()
    delete_avatar(previous)
    return _settings_redirect()


@router.post("/settings/cover-blur")
async def set_cover_blur(request: Request, blur_px: int = Form(...),
                         user=Depends(get_current_user)):
    """How hard the home grid blurs the covers that asked to be blurred.

    One number per READER, not per cover: which covers to blur is a fact
    about those books (`series.blur_cover`), but how hard to blur them is
    about who might be looking over your shoulder. Rejected rather than
    clamped when it is out of range, like the page-time cap above -- the
    slider cannot produce such a value, so anything else came from
    somewhere that should be told it was refused.
    """
    if not MIN_COVER_BLUR <= blur_px <= MAX_COVER_BLUR:
        return _settings_saved(request, "bad-blur")
    async with write_transaction() as db:
        await db.execute(
            "UPDATE users SET cover_blur_px = ?, updated_at = datetime('now') WHERE id = ?",
            (blur_px, user["id"]),
        )
        await db.commit()
    return _settings_saved(request)


@router.post("/settings/text-time-cap")
async def set_text_time_cap(request: Request, cap_seconds: int = Form(...),
                            user=Depends(get_current_user)):
    if not MIN_PAGE_TIME_CAP <= cap_seconds <= MAX_PAGE_TIME_CAP:
        return _settings_saved(request, "bad-cap")
    async with write_transaction() as db:
        await db.execute("UPDATE users SET text_time_cap_seconds=? WHERE id=?", (cap_seconds, user['id']))
        await db.commit()
    return _settings_saved(request)


@router.post("/settings/page-time-cap")
async def set_page_time_cap(request: Request, cap_seconds: int = Form(...),
                            user=Depends(get_current_user)):
    """Changes the per-page reading-time cap from here on.

    Deliberately not retroactive. Every page_reads row stores the cap it was
    recorded under, so the statistics keep counting old rows exactly as they
    were counted before -- changing this setting cannot rewrite what the
    history says. (The raw duration is stored alongside it, so recomputing
    the whole history under one cap remains possible; it is just not what
    this button does.)
    """
    if not MIN_PAGE_TIME_CAP <= cap_seconds <= MAX_PAGE_TIME_CAP:
        return _settings_saved(request, "bad-cap")
    async with write_transaction() as db:
        await db.execute(
            "UPDATE users SET page_time_cap_seconds = ?, updated_at = datetime('now') WHERE id = ?",
            (cap_seconds, user["id"]),
        )
        await db.commit()
    return _settings_saved(request)
