# Deferred native Android browser validation

Native Android CI is paused. Required validation is Chrome/Chromium and Firefox
on desktop, including mobile viewport simulations in both. Native Android does
not gate PRs or releases. The scripts below are retained for a future return to
real-device/emulator testing; viewport simulations are not native execution.

Default version selection resolves only desktop browsers. Use `--android` to
opt into the four-target manifest for the optional commands below. Each browser
must match its selected version during such an experiment.

The previous CI used disposable Google APIs Android 15/API35 x86_64 AVDs with
KVM acceleration, four virtual CPUs and 4 GB memory. That environment is a
reference for future investigation, not an active CI job.

## Official downloads and identity

Firefox is downloaded directly from Mozilla's release archive at the selected
version. Vivaldi's official download page provides a versioned x86-64 APK hosted
on downloads.vivaldi.com; that URL is saved when selecting stable versions.
Neither browser requires a third-party APK mirror or a Google Play account.

Before installation, SDK35 `apksigner` verifies the APK and its expected vendor
signing identity. SDK `aapt` checks the package and version. Evidence records the
source URL, APK SHA-256, signing certificate, Android version and installed
browser version. Signing keys were bootstrapped from the official Firefox158.0
and Vivaldi8.2.4147.163 APKs obtained over authenticated vendor HTTPS. A changed
key fails validation and requires investigation instead of silent acceptance.

Vivaldi's product version differs from its Chromium engine version. The installer
reads the upstream version constant from the signed x86-64 native library and
selects a ChromeDriver for that Chromium build. ChromeDriver's branded-product
build check is disabled for Vivaldi; independent APK, package, installed-version
and running-version checks remain required. The engine version is recorded.
Ambiguous native version constants fail setup instead of guessing a driver.

## Automation preflight

Run `python -m unittest tests.test_android_input tests.test_android_controls`
before emulator runs. These fast checks validate native coordinate mapping and
mutation evidence classification; they are not Android browser execution.
Accessibility hit rectangles can include padding: use their centre as an
anchor and the browser pixel ratio as scale, never the rectangle's size ratio.
Native input rejects hidden tabs, covered controls and points outside the web
viewport, and saves geometry in `native-input.jsonl` for diagnosis.
Android can exit the hierarchy-dump command with an error after writing its XML.
The adapter removes the old device file first, then requires a readable new
Android hierarchy and the unique anchor before input. A validated dump with a
nonzero exit is recorded as a diagnostic warning; missing or invalid output
still fails. A previous local snapshot is never a fallback.

## Local execution

Start a compatible disposable AVD. Provide `adb`, Java, SDK build-tools35 and
current official APK download access. For each browser (`vivaldi`, then
`firefox`), use a separate evidence directory:

```bash
python scripts/browser_versions.py --android --output browser-versions.json
python scripts/provision_android_browsers.py --browser vivaldi \
  --versions browser-versions.json --output android-evidence/browser.json
python scripts/provision_webdrivers.py --android vivaldi \
  --versions browser-versions.json --evidence android-evidence/browser.json \
  --directory /path/to/drivers --env /path/to/driver-env
python scripts/test_android_browser.py --browser vivaldi \
  --versions browser-versions.json --installation android-evidence/browser.json \
  --chromedriver /path/to/drivers/chromedriver --geckodriver /path/to/geckodriver \
  --output android-evidence
```

When running these optional experiments, use separate browser environments. Test accounts,
books and servers are disposable. `adb reverse` connects the device to its
fixture server without publishing a preview. Native browser first-run screens
are initialized before the WebDriver session; they are outside the application
under test. Firefox starts with GeckoView's automation configuration, an
isolated profile in its own private app-data directory and the public URL intent.
The disposable rooted AVD assigns that profile Firefox's actual UID and SELinux
label; Android's sandbox remains enabled. Geckodriver attaches to
that actual Android process through forwarded Marionette and BiDi ports using
`--connect-existing`; it does not launch a desktop browser. The regular Android
launcher uses `am start -W` with a fixed five-second ADB deadline, which can
expire while the browser is starting. Package/version, Android user-agent and
touch-capability checks remain mandatory. Vivaldi's existing tab is explicitly
brought to the foreground before layout or touch checks; Android can suspend
animation callbacks in background tabs.

The header regression changes the actual Android display to a 320px layout,
opens the native keyboard, and exercises search, filters, cover sizing and the
account menu on home, series and web-novel pages. It verifies normal-text row
geometry, long account names, avatars and enlarged-text reflow. Display and IME
settings are restored after the test. Header taps use Android's input dispatcher,
with screen coordinates calibrated against the account button's native
accessibility bounds; this transfers native browser-view and keyboard focus.
Desktop coverage additionally checks
320/360/390/414px layouts and the unchanged desktop account presentation.

Tests use native Android taps for text-reader navigation, paragraph continuation,
resume and settings, and W3C touch input for comic navigation. Reading scrolls
use Android input-dispatcher swipes calibrated against the native settings
button, and must change the scroll position; an initially nonzero offset is
not evidence of a successful gesture. They
measure typography with production iframe styles and preserved heading sizes.
The reconnection test backgrounds the Android browser, restarts its actual
server, and verifies document/image preservation on return. Desktop suites
retain the broader reflow, cache and README-flow coverage.

The reading-statistics scenarios run independently: recorder upgrade, paginated
word accounting, checkpoint recovery, continuous word accounting and session
priority. Each mutation runs only its corresponding scenario; an unrelated
scroll failure cannot be mistaken for a session-priority regression. The
complete Android baseline contains eleven tests.
A shared regression delays srcdoc navigation and replays the initial blank
document load in the parent page. EPUBs and novels must retain their text in
both reading modes; paginated chapters must also retain their tap handlers.
The probe never inserts scripts into the sandboxed book document. Continuous
chapters must fit their measured iframe height; classic scrollbar width is
reserved before measurement so creating the scrollbar cannot rewrap and clip
the chapter afterward.

After the complete baseline passes, the optional local runner tests deliberate defects against each new
Android regression: disabled paragraph splitting, unchanged font size, lost comic
progress, unnecessary cover downloads on reconnect, a forced second header row,
disabled word accounting or continuous measurement, a stale recorder bundle,
and missing unfinished-visit checkpoints. Each control must produce
an assertion failure in its corresponding real Android test. Driver/setup errors
do not count as rejected defects. Source files are restored after every control;
logs and machine-readable counts are written to the configured Android evidence directory.
