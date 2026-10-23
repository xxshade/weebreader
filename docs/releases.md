# Publishing a release

The Release workflow runs on every push to `main`, including direct pushes.
Numbered releases are created when eligible unpublished changes are found. A single serialized publisher builds one image per release; its numbered tag,
`latest` and full source-commit alias identify the same complete manifest/index.
Images are currently `linux/amd64` only; there are no moving major/minor aliases.
WeebReader has its own version history and image, `ghcr.io/xxshade/weebreader`.
The initial stable version is **v8.3.6**. After its first publication, automatic
numbering continues from the highest completed stable version.

## Publish the initial version

Before the first stable release, pushes to `main` finish planning successfully
with an explicit "Awaiting the initial manual release" message. They do not
build, publish or reconcile an image.

After validating locally and merging the release setup, open **Actions →
Release → Run workflow**, select **main**, enter **`8.3.6`** as `version` and
provide explicit `migration_notes`. Use **`No manual steps required`** when
applicable; blank notes or template comments are rejected. This manual request
selects the exact main commit inside the publication queue. It does not require
a previous release, tag or associated PR.

The same durable journal, immutable image checks and recovery path used for
later releases apply to this first publication. If interrupted, run **Release**
again on main with both inputs empty. Recovery retains the initially selected
commit and version, even before an image exists; newer main changes wait for
a subsequent release. Do not change the reservation to include a newer commit.
Any intentional replacement of an unbuilt reservation requires a separate,
explicitly reviewed operation; the retry workflow does not replace it.

## Prepare a pull request

Write the title and description in English. For a PR that changes the application,
runtime dependencies or installation configuration:

1. Apply exactly one label: **release:patch** for fixes, **release:minor** for
   compatible features, or **release:major** for incompatible changes.
2. Fill in **`## Upgrade notes`** in the PR description with actionable database
   or configuration migration instructions. Use **`No manual steps required`**
   when applicable. The template's comment alone does not count.
3. Run backend/static checks and **Chrome and Firefox desktop** suites, including mobile viewport scenarios. Unexpected
   coroutine, task, resource and error-log diagnostics fail the backend runner;
   deliberate negative tests must capture and assert their expected failures.

Create those three labels in the repository if they are not already available.
GitHub Actions validates PR release metadata and runs the full backend,
static, WebAuthn and Chrome/Firefox suites. PRs also build the Docker image
without publishing. Correct missing labels or upgrade notes in the PR before
merging; metadata changes rerun the policy check without repeating unchanged
source tests. Release planning checks the metadata again before publication.

Documentation, screenshots, tests, CI and release tooling **alone** do not
create releases. Eligibility is based on paths, not PR labels or title wording:
`app/`, `requirements.txt`, `Dockerfile`, `.dockerignore`, `entrypoint.sh`,
Compose files and `.env.example`. Renaming or deleting runtime files still
counts. A mixed application/documentation PR is eligible.

The protected `main` branch requires an up-to-date PR with successful
**Checks / Validation passed** and **Release metadata** checks, including for
administrators. No additional reviewer approval is required. The aggregate
validation check rejects failed, cancelled, skipped or missing required jobs.
Only the disposable PR image build may be skipped during release validation:
the publisher builds or recovers the actual versioned image after validation.

Workflow files do not themselves configure branch protection. Register these
checks in repository settings after a successful run, preserving any other
existing protection requirements. Application changes must still belong to an
associated merged PR; passing tests do not bypass version labels or upgrade
notes. Documentation/test/CI changes do not create a numbered release.

## How automatic publication works

The **Release** workflow serializes stable publishers without cancelling an
active run. Once admitted to the queue it resolves the current `main` commit,
so pending merges can be consolidated even when GitHub replaces an intermediate
pending run. It examines the complete first-parent history since the last
published stable tag, including ordinary merges, squash merges and rebased PRs.

All unreleased eligible PRs must have valid labels and upgrade notes. The largest
impact wins: from `8.0.0`, an unpublished minor PR followed by a patch produces
`8.1.0`, with both sets of upgrade instructions. Failed releases do not forget
pending PRs. Drafts and prereleases do not advance the stable baseline.

The workflow then:

1. Saves the version, exact commit and collected notes as a release-plan artifact.
2. Rejects any existing Git tag, GitHub Release or numbered registry image, and
   any version at or below an already published stable version. API/authentication
   errors fail the check instead of being interpreted as missing versions.
3. Runs the complete reusable validation on the exact selected source commit,
   then revalidates the plan and PR metadata and reserves a draft Release containing
   the durable publication journal before the build.
4. Builds/pushes `ghcr.io/xxshade/weebreader:X.Y.Z` once, or discovers the verified
   original image when recovering an interrupted publication.
5. Verifies image source/version/commit, reserves the Git tag, copies the exact
   manifest bytes to the full commit alias, announces the Release and promotes
   the same bytes to `latest`. It marks publication complete only after all
   three registry digests match. Attestations remain in the copied index.

Source validation and artifact consistency are separate requirements. The
publication jobs depend on successful backend/static and both-browser validation
of the selected commit; failed or incomplete validation prevents publication.
Image identity and digest checks still run after the tests pass.

Changes to labels or upgrade notes after planning invalidate the plan; rerun
with the reviewed metadata. A failed run does not retry indefinitely. After
fixing the problem, use **Actions → Release → Run workflow** on **main**, with
empty inputs, or let the next push to main trigger a new plan. Documentation-only
merges can retry earlier unpublished application changes, but never bump a
version by themselves.

The credentials are the workflow's `GITHUB_TOKEN`; no PAT or external release
service is needed. The publisher requires `contents: write`, `packages: write`
and `pull-requests: read`, plus access to the repository's GHCR package. If the
package does not inherit repository permissions, grant the repository Actions
access in the package settings.

## Manual version override

On **main**, the Release workflow's optional `version` input accepts a stable
SemVer without `v`, suffixes or leading zeros. It may raise the automatically
planned version, but cannot lower its compatibility impact, bypass PR metadata,
or republish when there are no eligible pending changes. Optional
`migration_notes` supplement the collected PR instructions. Most retries should
leave both inputs empty.

## Version immutability and failed runs

Only `release.yml` publishes images. `latest` means the most recent completed
stable release, rather than the newest push. Docs/tests/CI changes alone do not
create a new numbered image or version; they still run validation in Actions. When no release
is pending and a stable release exists, the workflow verifies its aliases. This also aligns
the previous rolling `latest` with the last numbered stable image during rollout,
without rebuilding or changing an existing numbered or short commit tag.

A draft Release holds the exact reviewed plan and publication phase. If any
external operation fails, rerun **Release** on main with empty inputs. The
planner recovers the unfinished publication first, even when newer merges exist.
It checks the original selected commit and reviewed metadata again and reuses
its original image. Recovery reruns validation of that original source commit. When main has newer commits, completion
queues another Release run automatically, so pending changes are not stranded
after recovering an older image. This uses `GITHUB_TOKEN` with `actions: write`
only in the follow-up job; no PAT is needed. If the failure happened between push and journal update,
recovery requires matching image source, commit and version labels before
recording the discovered digest. An unrelated occupied version is a conflict.

The journal phases are prepared, built, announced and complete. An announced but
incomplete release does not advance automatic version planning. A brief interval
where the numbered Release exists but `latest` still identifies the prior stable
image is expected: GitHub and GHCR cannot be updated atomically. Completion
requires digest equality at the same registry manifest/index level.

Conflicting plans, changed numbered images, missing recorded images, different
Git tags or commit aliases, changed PR metadata, and attempts to move `latest`
backwards all fail. Investigate the reported identity mismatch rather than
rebuilding, overwriting or deleting the reserved version. Preserve draft journals
and their image digests during recovery. Existing historical short commit tags
remain unchanged; new commit aliases use the full SHA.

Every journal update explicitly preserves `tag_name`. GitHub can replace a
draft's tag with an `untagged-*` placeholder when the Git ref does not yet
exist and a PATCH omits that field. Such a draft is not a new version: retain
its journal and numbered image, and verify the recorded plan, source labels
and exact digest before restoring its reserved tag. Do not delete the draft
or rebuild the image to work around a missing reservation.

Local validation requires Chrome and Firefox desktop with mobile viewport
simulations. Native Android is deferred and optional; see
[Android validation](testing/android.md). Desktop suites run in Actions; native
Android checks and performance measurements remain optional and local.

Back up and restore the matching persistent data when rolling back a database
migration; image identity alone does not establish downgrade compatibility.

## Validate changes without publishing

```bash
APP_DATA_DIR=/tmp/publication-reader-tests ./.venv/bin/python -m scripts.test_backend
```

The release tests exercise a disposable Git repository and local HTTP boundaries
without publishing anything. They cover accumulated PRs, merge strategies,
metadata, pagination, version conflicts, API errors, digests and exact commit
selection. Run `actionlint` over `.github/workflows/*.yml` when changing the
orchestration, plus the normal static and both-browser checks.

A draft reservation preserves its approved version, notes and source commit,
including after a failed build with no image yet. Newer merges never silently
replace that decision. Once a numbered image exists, recovery reuses it; a
missing recorded image is never rebuilt. Later merges are queued for a
subsequent release after recovery completes.
