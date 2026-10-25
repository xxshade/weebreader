# Runtime vulnerability checks

The required **Checks / Docker build** job builds an unpublished `linux/amd64`
OCI image and scans it with Trivy. The scan covers both operating-system and
Python packages, including vulnerabilities without an available fix.

**Any CRITICAL finding blocks validation and publication.** HIGH, MEDIUM, LOW
and UNKNOWN findings remain visible in the report. Scanner failures, unavailable
databases, malformed reports and missing OS or Python inventories also block;
an incomplete scan is never treated as a clean result.

The scanner uses vulnerability detection only. Repository `trivy.yaml` and
`.trivyignore` files are disabled explicitly, and there are no CVE exclusions or
`ignore-unfixed` exceptions. A future policy exception requires a reviewed code
change with its justification; it must not be introduced just to make CI green.

## Publication and recovery

- Pull requests build and scan an OCI archive without registry credentials or
  publishing an image. The existing aggregate validation check requires that
  Docker job to pass.
- A release builds its exact planned source commit into an OCI archive, scans
  that archive, verifies its bytes again, then uploads it with
  `skopeo copy --all --preserve-digests`. It is not rebuilt between the scan and
  publication. The release journal's existing digest checks still apply,
  including the full OCI index and provenance.
- Recovery scans the original `image@sha256` reference before completing its
  publication; it does not rebuild or replace an existing numbered image.
- Reconciliation scans the verified current stable image before changing its
  aliases. It checks that the image digest still matches the scanned digest.

Trivy reads an OCI layout directory rather than an OCI transport tar file. The
wrapper safely extracts the OCI metadata and blobs into a temporary directory,
rejecting links, special files and unsafe paths. It scans that layout and removes
it afterwards; the original archive's bytes and identity remain unchanged.

Reports include all severities and package inventories in `vulnerabilities.json`,
a readable `summary.md`, scanner output in `scanner.log`, and the scanned artifact identity. GitHub retains these
diagnostics for seven days, on successful and failed scans. OCI archives and
registry credentials are not uploaded as diagnostic artifacts.

Each job obtains the current vulnerability database from the official
`ghcr.io/aquasecurity/trivy-db:2` repository; database-update skipping is not
enabled. Trivy's own normal freshness checks remain active. There is no scheduled
workflow: scans run when PR or release validation runs. New advisories can block
an older image that previously passed, including an interrupted release being
recovered. Fix or update the affected packages and validate a new source commit;
do not overwrite an immutable published version.

## Scanner pin and local use

The composite action installs **Trivy 0.76.1** from its
[official release](https://github.com/aquasecurity/trivy/releases/tag/v0.76.1).
Its Linux amd64 archive is verified against the literal SHA-256
`c7cbe3137fb8a48a17574bede3dd6beb16742d1e9772929940fc90082c39d9c0`,
confirmed against the release checksum file and GitHub's asset digest. Scanner
updates change the version and checksum together and rerun the policy tests.

With that verified scanner on `PATH`, scan a local Docker or OCI archive:

```sh
python scripts/container_security.py --archive /path/to/image.tar \
  --report-directory /tmp/weebreader-security-report
```

Existing registry images require an immutable reference:

```sh
python scripts/container_security.py \
  --image ghcr.io/xxshade/weebreader@sha256:FULL_MANIFEST_DIGEST \
  --report-directory /tmp/weebreader-security-report
```

Exit status `0` means a complete scan without known critical findings, `1` means
critical findings, and `2` means the scan or report could not be trusted. This
is a check of known package vulnerabilities at scan time; it does not certify
application logic or establish that every reported vulnerability is exploitable
through this application.
