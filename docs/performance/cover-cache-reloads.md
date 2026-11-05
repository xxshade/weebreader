# Private cover cache and initial image display

Series covers and avatars use content-addressed files under `/covers`. Successful
responses serve the same bytes for a URL regardless of session or presentation
cookies. They use `Cache-Control: private, max-age=31536000, immutable` without
varying on Cookie. Every request reaching the server still authenticates before
file delivery or a conditional 304 response. A browser can reuse a fresh private
entry after logout or another login on that device; this is the same accepted
local-cache trade-off as versioned novel covers, not a shared/public cache.

The URLs use `?private=2` to move existing browsers away from fresh immutable
entries that retain the old Cookie-varying response metadata. The new URL is
fetched once per image when accessed. It then remains reusable; a changed image
still receives a new content-addressed filename. Old URLs remain authenticated
and functional. No cache clearing or data migration is required. Image lazy
loading and novel cover URLs are unchanged.

## Investigation

Both card types already shared the same lazy image markup. A local real-browser
probe, installed before navigation, observed incomplete visible images in frames
after first contentful paint. It also tracked image URL/node changes and bytes
transferred. With trusted local HTTPS, normal `location.reload()`, warm isolated
profiles and synthetic 400×600 WebP images, the following comparison changed only
the static image response's `Vary: Cookie` header:

| Scenario | Cookie-varying series response | Cookie-independent series response |
| --- | ---: | ---: |
| Firefox desktop, first order | 19/24 reloads with pending series covers | 0/24 |
| Firefox desktop, reverse order | 8/12 | 0/12 |
| Firefox 320px viewport | 1/12 | 0/12 |
| Chrome desktop | 0/12 | 0/12 |
| Chrome 320px viewport | 0/12 | 0/12 |

There were no cover transfers or observed image replacements/URL changes in
these warm comparisons. Novel covers remained ready. The narrow Firefox case
observed Reading; Library was below the first viewport. Explicit eager/high
priority and decoding for visible images did not eliminate the interval in a
separate small experiment.

This isolates a modifiable response-header difference in the local experiment,
not a guarantee against every possible paint/decode delay in every browser.
The probe measures incomplete images after FCP, not pixels or GPU uploads.
These are Firefox 159.0.1 and Chrome 157.0.8103.38 desktop observations, including
320px simulations; they are not native LibreWolf/Android measurements.
A delayed-image control was detected, and an intentionally blind probe was
rejected in both engines and widths.

Firefox WebDriver `refresh()` bypasses the cache and was not used as a proxy for
normal reload. The valid experiment used `location.reload()` and checked actual
document replacement. Server-timing from the estimate API does not measure cover
painting or prove a download.

## Regression coverage

Backend tests check equal representations across valid sessions/preferences,
private annual caching, no Cookie variant, authenticated conditional responses
and rejection of unauthenticated reads with or without cached validators.
The maintained cover-cache suite warms the previous URL generation before the
updated page, requires a first fetch for the new marker, and checks subsequent
reuse across navigation, preference changes, restart and reading updates. It
also verifies new cover/avatar content fetches and network rejection after
logout, in Chrome and Firefox at desktop and 320px widths.
