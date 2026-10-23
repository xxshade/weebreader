# Mobile validation and release aliases

Status: publication contract implemented. Native Android CI is deferred by the
subsequent user decision. Current required coverage is Chrome/Chromium and Firefox
desktop, including mobile viewport simulations in both. Android scripts remain
optional for future work. The original native validation plan below is retained
as historical context, not as an active requirement.
GitHub Actions now runs only release/package publication; all required desktop
validation runs locally. See [the current release process](../releases.md).

## Agreed validation contract

- Require Chrome and Firefox on desktop, Vivaldi and Firefox on Android. iOS is outside this scope.
- Use actual Android browser builds on devices or emulators, with browser and
  operating-system versions recorded. Desktop viewport emulation is additional
  coverage, not Android evidence.
- Resolve current stable browser versions once per pipeline, then retain those
  exact versions throughout its execution. A failed installation blocks the
  check; silently substituting an old browser is not allowed.
- Make the four targets mandatory in PR and release validation. Cover core
  application flows and affected regressions: EPUB/comic/web-novel reading,
  pagination and resume, typography, touch controls, navigation and reconnection.
  Opening the home page alone is insufficient.
- Failed, skipped or unexecuted required targets block publication. Keep desktop
  coverage intact while adding Android coverage and test failure propagation.

## Agreed publication contract

- `latest` means the latest successfully published stable release.
- Build once per release after validation of its exact source commit. Publish
  the numbered tag, `latest`, and a commit alias from that same image.
- Compare all three references at the same registry manifest/index level. They
  must have the same digest at successful completion; a local image ID or a
  platform child manifest is not an interchangeable comparison.
- Preserve numbered tags and existing commit tags. Promotion and recovery must
  reuse the original image, including its manifest and attestations, without
  rebuilding it or changing its metadata.
- GitHub and the registry cannot update atomically. A brief transition where the
  numbered release exists while `latest` still points to the previous version
  is accepted. An interrupted transition is incomplete and must be recoverable
  using verified release identity, source commit and the original image digest.
- Failure before promotion leaves `latest` unchanged. Recovery must not move
  `latest` backwards if a newer release has already completed.

## Implementation outline for review

Consolidate the standalone rolling publisher into the serialized stable-release
path. Keep checks on PRs and main, including documentation-only changes, while
avoiding duplicate validation of the same selected commit within publication.
Preserve existing release eligibility, accumulated version impact, reviewed
upgrade notes and pending-merge consolidation. Documentation/tests/CI-only
changes do not independently publish a new image. The rollout aligns `latest`
with the completed stable image without rebuilding it; subsequent tooling-only
changes retain that stable digest.

Add durable publication state and retry checks sufficient to distinguish a
verified partial publication from an unrelated occupied version. Exercise
failures between each external write, interrupted promotion, repeated recovery,
conflicting identities, and prevention of backward promotion. Finish only after
registry digest equality is verified. Update release documentation accordingly.

Provision a compatible Android OS and current official browser distributions
in CI, automate real interactions, and publish version and test evidence. Verify
that missing browsers and failed tests cannot result in a passing release gate.

## Current environment evidence

Mozilla downloads initially failed during design, then succeeded during
implementation. Official Firefox158.0 and Vivaldi8.2.4147.163 APKs were downloaded
and their signing identities verified. The user approved Vivaldi Android in
place of Chrome Android, retaining both desktop browsers and Firefox Android.
The existing Android9 fixture cannot run current Vivaldi (minimum Android10).
CI uses an Android15 AVD with KVM; cloud software emulation and CI validation
remain separate claims. The user's successful check of the affected EPUB on
their Firefox mobile is recorded as user-reported evidence.

On 2026-10-18, strict local backend validation passed 1,465 tests. Seventeen deliberate
defects were rejected by publication and stable-version regression tests.
Current Chrome155.0.8059.84 and Firefox158.0 desktop suites passed in CI;
the six pagination checks also passed locally with those same versions.
Successful APK installation alone does not complete the four-target validation
contract. See the required Android jobs on PR #211 for execution and deliberate
defect-control results for the current commit.
