# Measuring HTML request processing

HTML responses include `Server-Timing: app;dur=42.500`, with the elapsed
application time in milliseconds. The measurement starts in the existing page
middleware and includes authentication, handler work, HTML body preparation and
ETag calculation. Each conditional request receives its own measurement, even
when its response is 304 with no body. HTML fragments use the same measurement.
The value is a response header and never changes the HTML body or its ETag.

The metric is wall time, including waits during processing, not CPU time. It
excludes work before entering the middleware, network transit, Caddy processing,
body transfer and browser rendering. JSON, static assets and persistent SSE
streams do not receive this metric. There is no database write or per-request
logging for these measurements.

## Reading the measurement

1. Open browser developer tools, then Network.
2. Reload the home page and select its `/` document request.
3. Record its HTTP status (200 or 304), Waiting time and Server-Timing value.
   The header is available under Response Headers, and supporting browsers also
   expose the application metric in their timing details.
4. Repeat under comparable conditions. Compare medians and slow requests
   separately; retain the status and distinguish a normal reload from bypassing
   the HTTP cache.

For example, Waiting of 155 ms and an application measurement of 42.5 ms leave
roughly 112.5 ms outside the measured interval. This difference does not isolate
Caddy: it also includes the network, scheduling outside the measurement and
other unmeasured work. Browser session-history restores may make no HTTP request
at all, so they do not produce a fresh application measurement. Estimates,
images and SSE updates after HTML arrival are separate requests and activities;
this metric does not measure when the entire page becomes usable.

A local Caddy reverse-proxy check verifies header preservation for 200 and 304.
The deployed proxy configuration must also be checked: it can remove or replace
headers. No changes to Caddy are normally required to forward Server-Timing.

Only the total `app` duration is reported initially. Detailed query, rendering
and CPU diagnostics should be added only if this measurement points to relevant
application work; per-process CPU time cannot reliably be attributed to one
concurrent request.
