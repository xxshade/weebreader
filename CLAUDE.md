# Claude Code notes

**Read `AGENTS.md` first** — it holds the project's invariants, traps and
decisions, and applies to any agent working here. This file is only what is
specific to the Claude Code sandbox.

Keep both files short. Write the rule and the measurement that proves it, not
the story; the narrative lives in `git log`.

## Skills

`/grill-with-docs` (with `grilling` and `domain-modeling`, which it calls)
is Matt Pocock's, copied unchanged into `.claude/skills/` -- see the licence
file there. It writes `GLOSSARY.md` and `docs/adr/` lazily, when a term or
decision is settled; `AGENTS.md` stays where the invariants and traps live.

## Pull requests

**Before pushing to a branch that has had a PR, check whether that PR was
merged** (`pull_request_read`, `merged`). The owner merges while work
continues, so a PR open a moment ago may already be merged. If it was, do
not reuse it: move the unmerged commits onto the current `main` (`git rebase
--onto origin/main <merged head> <branch>` -- with `HEAD` in place of the
branch name it leaves HEAD detached and the branch where it was), push with
`--force-with-lease`, and open a new PR. Never edit a merged PR's description to cover new work.

## This sandbox

- Chromium is at `/opt/pw-browsers/chromium` (`executable_path=`).
- `dockerd` runs here (start it as root). Registry pulls and `apt-get`
  work; the build dies at `pip install`, because the egress proxy
  re-terminates TLS and the build container does not trust its CA. Add
  it in a scratchpad copy of the Dockerfile rather than the real one --
  `COPY --from=certs ca-bundle.crt /tmp/ca.crt` and
  `ENV PIP_CERT=/tmp/ca.crt` above the pip line, built with
  `-f <copy> --build-context certs=/root/.ccr` -- and the image builds,
  runs and answers 200 on `/setup`. Egress is per-environment: where a
  registry or `deb.debian.org` 403s there is no build at all, so render a
  template and drive it with Playwright instead. No Android device
  either, so the WebAPK behaviour in `AGENTS.md` cannot be exercised.
- The app's dependencies are not installed. `python3 -m venv` plus
  `pip install -r requirements.txt` works; installing into the system
  Python does not (`cryptography` is a Debian package with no RECORD, and
  pip rolls the whole transaction back when it cannot uninstall it).
- **Both browsers.** Chromium is above; Firefox is at
  `/opt/pw-browsers/firefox-1543` (`pw.firefox.launch()`, no
  `executable_path` needed). They disagree about things this app depends
  on -- a torn-down `fetch` is an `AbortError` in Chrome and a plain
  `TypeError` in Firefox, and Firefox kills it between `beforeunload` and
  `pagehide` while Chrome does not raise at all -- so a browser
  measurement means both. **Firefox is mandatory: a change not measured in
  it counts as failing, never as "Chromium only".** If `/opt/pw-browsers`
  has no `firefox-*`, install it -- `playwright install firefox` from a
  venv with `playwright==1.63.0` (that version's build is `firefox-1543`;
  `cdn.playwright.dev` is reachable).
- **A tap enters the app's fullscreen** (`fullscreen.js`), and headless
  Firefox then grows the window to 1366px whatever the viewport -- a
  390px measurement after a tap is silently a desktop one. Stub
  `Element.prototype.requestFullscreen` in an init script, and assert
  `innerWidth` after the tap.
- **Memory: read RSS after an idle pause, not mid-run.** Paging a text book
  every 60 ms, Firefox's RSS climbed ~4.6 MB per chapter (+929 MB over 200)
  and looked like a leak; 60 s idle took it back from 1600 to 769 MB with one
  iframe on the page -- uncollected garbage from the page turns' iframes, not
  retained memory. Continuous scroll does RETAIN, by design: every mounted
  chapter stays (~0.7 MB/chapter Firefox, ~1.5 Chromium); left as is.
- **The back/forward cache needs a flag off.** Playwright launches
  Chromium with `--disable-back-forward-cache`; pass
  `ignore_default_args=["--disable-back-forward-cache"]` and it restores
  normally (`pageshow` persisted true). Without that it never restores and
  reads as the browser being incapable, which is wrong. `go_back` must
  then use `wait_until="commit"` -- a restore fires no
  `domcontentloaded`, so the default wait times out. Playwright's FIREFOX
  cannot: it never restores, and `browser.sessionhistory.max_total_viewers`
  does not change that (`fission.bfcacheInParent` breaks navigation
  outright). So bfcache is a Chromium-only measurement here.
- `pkill -f` exits 144 and kills a compound command — run it alone; a
  `pgrep -f` pattern matches its own shell too, so bracket one letter
  (`pgrep -f "[u]vicorn ..."`). A server a driver restarts must be
  `Popen(..., start_new_session=True, stdout=DEVNULL)`, or the `| tail` of
  the driver waits on the server's inherited pipe forever.
- In Playwright's Firefox, navigating again the moment the login redirect
  lands left the new page's SSE stream unconnected (8s, no
  `/library/events`); wait ~1.5s after login before the next `goto`. A
  SECOND tab in the same context never connects its stream at all (main
  too) -- count streams from the server's access log, one tab per context.
- Chromium opens at most 6 connections per host over HTTP/1.1, and every
  open home/series tab holds one for its stream: a 7th request queues in
  the BROWSER. Test "many tabs" server-side with curl, not tabs.
- `sleep N && cat` is blocked; use `until <check>; do sleep 2; done`.
- `python3 -u` for unbuffered server logs.
- Follow the validation commands in `AGENTS.md`. PR CI runs the reusable
  `Checks` workflow; stable publication waits for backend and both browsers.
