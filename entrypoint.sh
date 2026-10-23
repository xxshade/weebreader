#!/bin/sh
set -e

# /app/data comes from a bind mount on the host (see docker-compose.yml).
# The host usually creates that directory as root, so the non-root "app"
# user can't write to it by default. We fix the ownership here -- as root,
# only for this step -- before actually dropping privileges to run the
# application. Without this: PermissionError when creating /app/data/covers.
mkdir -p /app/data/covers /app/data/converted
chown -R app:app /app/data

# setpriv only changes uid/gid -- it does NOT touch $HOME, which is still
# whatever it was as root ("/root") at this point. Nothing else in the app
# cares about $HOME, but headless Chromium (app/novels/scraper.py) does:
# it tries to create its profile directory under $HOME/.config/chromium on
# startup, the "app" user has no write access to /root, and Chrome simply
# exits immediately with no useful error ("session not created: Chrome
# instance exited") -- this is what that failure actually was. Exporting a
# HOME the "app" user genuinely owns (already chown'd above, or always
# owned since Dockerfile's `useradd -d /app`) fixes it at the source; the
# explicit --user-data-dir Chrome is also launched with (see scraper.py)
# is a second, independent safety net so this doesn't come back if HOME
# ever stops being set correctly for some other reason.
export HOME=/app

# uvicorn's --log-level only accepts lowercase names (critical/error/
# warning/info/debug/trace) -- APP_LOG_LEVEL follows this project's usual
# uppercase convention (see app/config.py, consumed the same way by the
# app's own Python logger), so it's lowercased here for uvicorn specifically.
UVICORN_LOG_LEVEL=$(echo "${APP_LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')

# --timeout-graceful-shutdown: uvicorn's default is to wait *indefinitely*
# for every open connection to close before running the app's lifespan
# shutdown handler (see app/main.py's lifespan -- that's where the "Shutting
# down..." log line and the watcher/scheduler cleanup live). The SSE stream
# at /library/events (see app/library/router.py) is a long-lived connection
# by design -- it stays open for as long as a browser tab has the app open,
# and never closes on its own -- so without this flag that wait genuinely
# never finishes: `docker stop`'s SIGTERM never gets a chance to run our
# cleanup at all, and the default 10s grace period always elapses into a
# SIGKILL. 5s here forces uvicorn to cancel any still-open connections after
# that timeout and proceed straight to the lifespan shutdown handler, well
# within docker's grace period.
exec setpriv --reuid=app --regid=app --init-groups \
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level "$UVICORN_LOG_LEVEL" \
    --timeout-graceful-shutdown 5
