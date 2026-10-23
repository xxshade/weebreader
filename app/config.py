from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_")

    # Directory for persistent data (database, generated covers)
    data_dir: Path = Path("/app/data")

    # Name and origin expected by WebAuthn (passkeys). Adjust to your domain.
    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "Publication Reader"
    webauthn_origin: str = "http://localhost:8000"

    session_lifetime_hours: int = 24 * 30  # 30 days, for home use

    # Python root logger level (see app/main.py's logging.basicConfig) --
    # also passed to uvicorn's own --log-level in entrypoint.sh, so this one
    # setting controls both the app's own logger.info(...) calls and
    # uvicorn's request/startup logs. One of the standard names: DEBUG,
    # INFO, WARNING, ERROR, CRITICAL (case-insensitive).
    log_level: str = "INFO"

    # File watcher debounce window (seconds)
    watcher_debounce_seconds: float = 2.5

    # Automatic library scan, as a 5-field crontab expression (default:
    # 05:00 every day). The watcher already picks up changes live, so this
    # is the safety net for whatever happened while the container was
    # stopped, or on a mount that delivers no filesystem events at all.
    # Interpreted in the CONTAINER's timezone -- set TZ in docker-compose
    # to have it mean your local 5am. Empty disables it.
    scheduled_scan_cron: str = "0 5 * * *"

    # ---- Web novel scraping (see app/novels/) ----
    novel_scrape_interval_minutes: int = 360  # 6 hours; the source site doesn't update often
    novel_scrape_user_agent: str = "publication-reader-personal/1.0 (+personal use, respectful rate limiting)"
    novel_scrape_request_delay_seconds: float = 2.0
    novel_chromium_binary_path: str | None = None  # None = search PATH (apt-installed default)
    novel_chromedriver_path: str | None = None      # None = search PATH (apt-installed default)

    # ---- MyAnimeList tracking (see app/tracking/myanimelist.py) ----
    # Each installation registers its own app at https://myanimelist.net/apiconfig.
    # The client ID is public, but there is no shared registration bundled here.
    myanimelist_client_id: str = ""
    # MAL issues a client SECRET alongside the id for most app types, and
    # its token endpoint then refuses the exchange with
    # `invalid_client: Client authentication failed` unless it is sent --
    # which is what "connecting" failed with here. Empty for an app
    # registered without one (MAL's docs: "if your client doesn't have a
    # client secret, client_secret will be empty"), so it is sent only
    # when set rather than always, and it is a SECRET: set it through the
    # environment (APP_MYANIMELIST_CLIENT_SECRET), never in this file.
    myanimelist_client_secret: str = ""
    # Must match a redirect URI registered on the MAL side EXACTLY, which
    # is why it is configured rather than derived from the request: this
    # app answers on a LAN address, through a reverse proxy and by its
    # public name, and only one of those is the registered one. The app's
    # own root works (the callback is recognised wherever it lands, see
    # tracking/router.py), but a dedicated path is cleaner if you can
    # register one.
    # No portable default: each installation must register its own URI.
    myanimelist_redirect_uri: str = ""

    # Compose passes unset variables as empty strings; whitespace is unset too.
    @field_validator("myanimelist_client_id", "myanimelist_redirect_uri", mode="before")
    @classmethod
    def _strip_oauth_settings(cls, value):
        return value.strip() if isinstance(value, str) else value

    @property
    def db_path(self) -> Path:
        return self.data_dir / "library.db"

    @property
    def covers_dir(self) -> Path:
        return self.data_dir / "covers"

    @property
    def secure_cookies(self) -> bool:
        # The session cookie only gets the `Secure` flag when the configured
        # origin is actually HTTPS -- forcing it unconditionally would break
        # login on a plain-HTTP LAN/localhost deployment (the common case
        # for this app), since browsers silently drop `Secure` cookies over
        # HTTP instead of just ignoring the flag.
        return self.webauthn_origin.startswith("https://")


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.covers_dir.mkdir(parents=True, exist_ok=True)
