"""Critical CVEs and incomplete scans cannot authorize a runtime publication."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import tarfile
import unittest

import yaml

from scripts.container_security import (TRIVY_SHA256, TRIVY_VERSION, command,
                                        digest_file, evaluate, scan, scanner_input, verify_archive)

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
                          "print('fixture scanner diagnostic', file=sys.stderr)\n"
                          "if raise_on_scan: raise SystemExit(7)\n"
                          "output=pathlib.Path(sys.argv[sys.argv.index('--output')+1])\n"
                          "image=pathlib.Path(sys.argv[sys.argv.index('--input')+1])\n"
                          "if not image.is_dir() or not (image/'oci-layout').is_file(): raise SystemExit(9)\n"
                          f"output.write_text({json.dumps(payload)!r})\n")
        binary.chmod(0o755)
        return binary

    def archive(self, path, extra=None):
        with tarfile.open(path, 'w') as bundle:
            for name, data in (('oci-layout', b'{"imageLayoutVersion":"1.0.0"}'),
                               ('index.json', b'{"schemaVersion":2,"manifests":[]}')):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                bundle.addfile(info, io.BytesIO(data))
            if extra is not None:
                bundle.addfile(extra, io.BytesIO(b'x') if extra.isfile() else None)

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
                self.archive(archive)
                reports = root / 'reports'
                reports.mkdir()
                (reports / 'vulnerabilities.json').write_text(json.dumps(report()))
                with contextlib.redirect_stdout(io.StringIO()):
                    actual = scan(binary, reports, archive=archive)
                self.assertEqual(actual, expected)
                summary = (reports / 'summary.md').read_text()
                self.assertIn('Passed' if expected == 0 else 'Blocked', summary)
                if version == TRIVY_VERSION:
                    self.assertIn('fixture scanner diagnostic', (reports / 'scanner.log').read_text())

    def test_oci_input_is_temporary_safe_and_preserves_the_published_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'image.tar'
            self.archive(archive)
            identity = digest_file(archive)
            with scanner_input(archive) as layout:
                self.assertTrue((layout / 'oci-layout').is_file())
                self.assertEqual(json.loads((layout / 'index.json').read_text())['schemaVersion'], 2)
            self.assertFalse(layout.exists(), 'Temporary image data must not remain with the reports')
            self.assertEqual(digest_file(archive), identity)
            for name, kind in (('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                               ('blobs/link', tarfile.SYMTYPE), ('blobs/hardlink', tarfile.LNKTYPE),
                               ('blobs/device', tarfile.CHRTYPE), ('other-file', tarfile.REGTYPE)):
                entry = tarfile.TarInfo(name)
                entry.type = kind
                entry.size = 1 if entry.isfile() else 0
                entry.linkname = 'index.json' if entry.issym() or entry.islnk() else ''
                self.archive(archive, entry)
                with self.subTest(name=name), self.assertRaises((ValueError, tarfile.TarError)):
                    with scanner_input(archive):
                        pass

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
        smoke = next(i for i, step in enumerate(steps) if 'test_container_runtime.py' in step.get('run', ''))
        self.assertLess(scanner, smoke)
        self.assertEqual(steps[build]['with']['push'], 'false')
        self.assertIn('type=oci,dest=${{ runner.temp }}/ci-image.tar', steps[build]['with']['outputs'])
        self.assertIn('type=docker,name=weebreader-ci:validation', steps[build]['with']['outputs'])
        self.assertEqual(steps[scanner]['with']['archive'], '${{ runner.temp }}/ci-image.tar')
        self.assertNotIn('continue-on-error', steps[scanner])
        self.assertNotIn('if', steps[scanner])
        self.assertIn('docker', checks['jobs']['passed']['needs'])
        self.assertEqual(checks['permissions'], {'contents': 'read'})

    def test_publication_scans_before_checks_and_reuses_exact_artifact(self):
        jobs = self.workflow('release.yml')['jobs']
        self.assertIn('security', jobs['validate']['needs'])
        self.assertIn('security', jobs['publish']['needs'])
        steps = jobs['security']['steps']
        build = next(i for i, step in enumerate(steps) if step.get('id') == 'build')
        scans = [step for step in steps if step.get('uses') == './.github/actions/container-scan']
        self.assertEqual(steps[build]['with']['push'], 'false')
        self.assertEqual(steps[build]['with']['outputs'], 'type=oci,dest=${{ runner.temp }}/release-image.tar')
        self.assertEqual(len(scans), 3)
        self.assertEqual(scans[0]['with']['archive'], '${{ runner.temp }}/release-image.tar')
        self.assertEqual(scans[1]['with']['image'], '${{ needs.prepare.outputs.image }}@${{ steps.stage.outputs.digest }}')
        self.assertEqual(scans[2]['with']['image'], '${{ steps.image.outputs.image }}')
        for step in scans:
            self.assertNotIn('continue-on-error', step)
        publish = jobs['publish']['steps']
        self.assertFalse(any(step.get('uses', '').startswith('docker/build-push-action@') for step in publish))
        copy = next(step for step in publish if 'skopeo copy' in step.get('run', ''))
        self.assertIn('verify-archive', copy['run'])
        self.assertIn('--all --preserve-digests', copy['run'])
        self.assertIn('scanned-image/container-security/identity.txt', copy['run'])
        self.assertIn('scanned-image/release-image.tar', copy['run'])
        finish = next(step for step in publish if 'release_publication finish' in step.get('run', ''))
        self.assertEqual(finish['env']['IMAGE_DIGEST'], '${{ needs.security.outputs.digest }}')
        self.assertTrue(any(step.get('with', {}).get('name') == 'scanned-release-image' for step in publish))
        guard = next(step for step in publish if 'SCANNED_STAGE_DIGEST' in step.get('env', {}))
        self.assertIn('test "$CURRENT_DIGEST" = "$SCANNED_STAGE_DIGEST"', guard['run'])

    def test_reconciliation_rechecks_scanned_digest_after_validation(self):
        jobs = self.workflow('release.yml')['jobs']
        self.assertIn('security', jobs['reconcile']['needs'])
        steps = jobs['security']['steps']
        resolve = next(i for i, step in enumerate(steps) if 'release_publication security-target' in step.get('run', ''))
        scanner = next(i for i, step in enumerate(steps) if step.get('with', {}).get('image') == '${{ steps.image.outputs.image }}')
        self.assertLess(resolve, scanner)
        reconcile = next(step for step in jobs['reconcile']['steps'] if 'release_publication reconcile' in step.get('run', ''))
        self.assertEqual(reconcile['env']['SCANNED_IMAGE_DIGEST'], '${{ needs.security.outputs.digest }}')

    def test_pr_checks_wait_for_security_but_release_uses_its_external_gate(self):
        jobs = self.workflow('checks.yml')['jobs']
        for name in ('backend', 'versions'):
            self.assertEqual(jobs[name]['needs'], 'docker')
            self.assertIn("needs.docker.result == 'success'", jobs[name]['if'])
            self.assertIn("!inputs.build_image && needs.docker.result == 'skipped'", jobs[name]['if'])
        self.assertEqual(jobs['browsers']['needs'], 'versions')

    def test_installer_uses_literal_pinned_official_version_and_checksum(self):
        action = yaml.load((ROOT / '.github/actions/container-scan/action.yml').read_text(), Loader=yaml.BaseLoader)
        install = action['runs']['steps'][0]['run']
        self.assertIn(f'https://github.com/aquasecurity/trivy/releases/download/v{TRIVY_VERSION}/', install)
        self.assertIn(TRIVY_SHA256, install)
        self.assertIn('sha256sum --check --strict', install)
        self.assertNotIn('curl |', install)
