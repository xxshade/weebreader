"""Critical CVEs and incomplete scans cannot authorize a runtime publication."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

import yaml

from scripts.container_security import (TRIVY_SHA256, TRIVY_VERSION, command,
                                        digest_file, evaluate, scan, verify_archive)

ROOT = Path(__file__).resolve().parents[1]


def report(*issues):
    return {'SchemaVersion': 2, 'Metadata': {'OS': {'Family': 'debian'}}, 'Results': [
        {'Class': 'os-pkgs', 'Type': 'debian', 'Packages': [{'Name': 'libc6'}],
         'Vulnerabilities': list(issues)},
        {'Class': 'lang-pkgs', 'Type': 'python-pkg', 'Packages': [{'Name': 'fastapi'}]},
    ]}


class ScanPolicy(unittest.TestCase):
    def test_critical_cves_block_even_without_a_patch_but_high_findings_do_not(self):
        for fixed in ('', '2.0'):
            issue = {'Severity': 'CRITICAL', 'VulnerabilityID': 'CVE-fixture', 'FixedVersion': fixed}
            counts, critical = evaluate(report(issue, {'Severity': 'HIGH'}))
            self.assertEqual(counts['CRITICAL'], 1)
            self.assertEqual(critical, [issue])
        counts, critical = evaluate(report({'Severity': 'HIGH'}))
        self.assertEqual(counts['HIGH'], 1)
        self.assertEqual(critical, [])

    def test_unrecognized_or_missing_os_python_and_malformed_reports_fail_closed(self):
        invalid = [None, {}, {**report(), 'SchemaVersion': 1},
                   {**report(), 'Metadata': {}}, {**report(), 'Results': []},
                   {**report(), 'Results': report()['Results'][:1]},
                   {**report(), 'Results': report()['Results'][1:]},
                   report({'Severity': 'not-a-severity'}), report('not-a-vulnerability')]
        empty_packages = copy.deepcopy(report())
        empty_packages['Results'][1]['Packages'] = []
        invalid.append(empty_packages)
        for incomplete in invalid:
            with self.subTest(report=incomplete), self.assertRaises(ValueError):
                evaluate(incomplete)

    def test_scanner_flags_cover_all_packages_without_config_ignorefiles_or_skipped_db_updates(self):
        args = command('trivy', Path('report.json'), archive=Path('image.tar'))
        for flag, value in (('--scanners', 'vuln'), ('--pkg-types', 'os,library'),
                            ('--config', ''), ('--ignorefile', ''), ('--format', 'json'),
                            ('--platform', 'linux/amd64'), ('--input', 'image.tar')):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn('--ignore-unfixed=false', args)
        self.assertIn('--list-all-pkgs', args)
        self.assertNotIn('--skip-db-update', args)
        self.assertNotIn('--skip-files', args)
        for image in ('repo:latest', 'repo@sha256:bad', None):
            with self.subTest(image=image), self.assertRaises(ValueError):
                command('trivy', Path('report.json'), image=image)

    def scanner(self, root, payload, *, version=TRIVY_VERSION, fail=False):
        binary = root / 'fixture-trivy'
        binary.write_text("#!/usr/bin/env python3\nimport pathlib,sys\n"
                          "if sys.argv[1]=='--version':\n"
                          f" print('Version: {version}');raise SystemExit(0)\n"
                          f"raise_on_scan={fail!r}\n"
                          "if raise_on_scan: raise SystemExit(7)\n"
                          "output=pathlib.Path(sys.argv[sys.argv.index('--output')+1])\n"
                          f"output.write_text({json.dumps(payload)!r})\n")
        binary.chmod(0o755)
        return binary

    def test_orchestrator_blocks_critical_scanner_errors_stale_reports_and_wrong_versions(self):
        cases = [(report(), TRIVY_VERSION, False, 0),
                 (report({'Severity': 'CRITICAL'}), TRIVY_VERSION, False, 1),
                 (report(), TRIVY_VERSION, True, 2),
                 (report(), '0.0.0', False, 2),
                 ({}, TRIVY_VERSION, False, 2)]
        for payload, version, fail, expected in cases:
            with self.subTest(version=version, fail=fail, status=expected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                binary = self.scanner(root, payload, version=version, fail=fail)
                archive = root / 'image.tar'
                archive.write_bytes(b'disposable image bytes')
                reports = root / 'reports'
                reports.mkdir()
                (reports / 'vulnerabilities.json').write_text(json.dumps(report()))
                with contextlib.redirect_stdout(io.StringIO()):
                    actual = scan(binary, reports, archive=archive)
                self.assertEqual(actual, expected)
                summary = (reports / 'summary.md').read_text()
                self.assertIn('Passed' if expected == 0 else 'Blocked', summary)

    def test_published_archive_must_be_the_same_bytes_that_were_scanned(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, identity = Path(directory) / 'image.tar', Path(directory) / 'identity.txt'
            archive.write_bytes(b'scanned artifact')
            identity.write_text(digest_file(archive) + '\n')
            verify_archive(archive, identity)
            archive.write_bytes(b'another artifact')
            with self.assertRaisesRegex(ValueError, 'differs'):
                verify_archive(archive, identity)


class SecurityWorkflow(unittest.TestCase):
    def workflow(self, name):
        return yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)

    def test_pr_scan_is_required_and_never_needs_registry_writes(self):
        checks = self.workflow('checks.yml')
        steps = checks['jobs']['docker']['steps']
        build = next(i for i, step in enumerate(steps) if step.get('uses', '').startswith('docker/build-push-action@'))
        scanner = next(i for i, step in enumerate(steps) if step.get('uses') == './.github/actions/container-scan')
        self.assertLess(build, scanner)
        self.assertEqual(steps[build]['with']['push'], 'false')
        self.assertEqual(steps[build]['with']['outputs'], 'type=oci,dest=${{ runner.temp }}/ci-image.tar')
        self.assertEqual(steps[scanner]['with']['archive'], '${{ runner.temp }}/ci-image.tar')
        self.assertNotIn('continue-on-error', steps[scanner])
        self.assertNotIn('if', steps[scanner])
        self.assertIn('docker', checks['jobs']['passed']['needs'])
        self.assertEqual(checks['permissions'], {'contents': 'read'})

    def test_publication_scans_exact_artifact_and_recovery_before_any_delivery(self):
        steps = self.workflow('release.yml')['jobs']['publish']['steps']
        build = next(i for i, step in enumerate(steps) if step.get('id') == 'build')
        scans = [(i, step) for i, step in enumerate(steps) if step.get('uses') == './.github/actions/container-scan']
        upload = next(i for i, step in enumerate(steps) if 'skopeo copy' in step.get('run', ''))
        finish = next(i for i, step in enumerate(steps) if 'release_publication finish' in step.get('run', ''))
        self.assertEqual(steps[build]['with']['push'], 'false')
        self.assertEqual(steps[build]['with']['outputs'], 'type=oci,dest=${{ runner.temp }}/release-image.tar')
        self.assertEqual(len(scans), 2)
        archive, recovery = scans
        self.assertLess(build, archive[0])
        self.assertLess(archive[0], upload)
        self.assertLess(recovery[0], upload)
        self.assertLess(upload, finish)
        self.assertEqual(archive[1]['if'], "steps.stage.outputs.build == 'true'")
        self.assertEqual(recovery[1]['if'], "steps.stage.outputs.build != 'true'")
        self.assertEqual(archive[1]['with']['archive'], '${{ runner.temp }}/release-image.tar')
        self.assertEqual(recovery[1]['with']['image'], '${{ needs.prepare.outputs.image }}@${{ steps.stage.outputs.digest }}')
        self.assertIn('--all --preserve-digests', steps[upload]['run'])
        self.assertIn('verify-archive', steps[upload]['run'])
        self.assertIn('oci-archive:$RUNNER_TEMP/release-image.tar', steps[upload]['run'])
        for _, scanner in scans:
            self.assertNotIn('continue-on-error', scanner)

    def test_reconciliation_scans_verified_digest_and_checks_it_again_before_alias_changes(self):
        steps = self.workflow('release.yml')['jobs']['reconcile']['steps']
        resolve = next(i for i, step in enumerate(steps) if 'release_publication security-target' in step.get('run', ''))
        scanner = next(i for i, step in enumerate(steps) if step.get('uses') == './.github/actions/container-scan')
        reconcile = next(i for i, step in enumerate(steps) if 'release_publication reconcile' in step.get('run', ''))
        self.assertLess(resolve, scanner)
        self.assertLess(scanner, reconcile)
        self.assertEqual(steps[scanner]['with']['image'], '${{ steps.image.outputs.image }}')
        self.assertEqual(steps[reconcile]['env']['SCANNED_IMAGE_DIGEST'], '${{ steps.image.outputs.digest }}')
        self.assertNotIn('if', steps[scanner])

    def test_installer_uses_literal_pinned_official_version_and_checksum(self):
        action = yaml.load((ROOT / '.github/actions/container-scan/action.yml').read_text(), Loader=yaml.BaseLoader)
        install = action['runs']['steps'][0]['run']
        self.assertIn(f'https://github.com/aquasecurity/trivy/releases/download/v{TRIVY_VERSION}/', install)
        self.assertIn(TRIVY_SHA256, install)
        self.assertIn('sha256sum --check --strict', install)
        self.assertNotIn('curl |', install)
