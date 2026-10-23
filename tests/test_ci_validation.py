"""A failed or incomplete validation must never authorize publication."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from scripts.ci_validation import require_success, run_commands, verify_checkout


class ValidationGate(unittest.TestCase):
    def test_missing_failed_cancelled_or_skipped_required_jobs_block_delivery(self):
        passed = dict.fromkeys(('backend', 'versions', 'browsers', 'docker'), 'success')
        require_success(passed, build_image=True)
        for job in passed:
            for status in ('failure', 'cancelled', 'skipped', ''):
                with self.subTest(job=job, status=status), self.assertRaises(ValueError):
                    require_success({**passed, job: status}, build_image=True)
            with self.subTest(missing=job), self.assertRaises(ValueError):
                require_success({k: v for k, v in passed.items() if k != job}, build_image=True)

    def test_release_can_skip_only_the_disposable_pr_docker_build(self):
        passed = dict.fromkeys(('backend', 'versions', 'browsers'), 'success')
        require_success({**passed, 'docker': 'skipped'}, build_image=False)
        for job in passed:
            with self.subTest(job=job), self.assertRaises(ValueError):
                require_success({**passed, job: 'skipped', 'docker': 'skipped'}, build_image=False)


class ValidationCommands(unittest.TestCase):
    def test_failure_is_reported_and_stops_later_commands(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            status = run_commands([
                ('probe', [sys.executable, '-c', 'print("failure evidence"); raise SystemExit(7)']),
                ('must-not-run', [sys.executable, '-c', f'open({str(root / "unexpected")!r}, "w").close()']),
            ], root / 'logs')
            self.assertEqual(status, 7)
            self.assertFalse((root / 'unexpected').exists())
            self.assertIn('failure evidence', (root / 'logs/probe.log').read_text())
            self.assertEqual(json.loads((root / 'logs/results.json').read_text())[0]['status'], 7)

    def test_no_commands_is_not_a_successful_validation(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            run_commands([], Path(directory))

    def test_checkout_must_match_the_requested_commit(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory), \
                patch.dict(os.environ, {'GIT_AUTHOR_NAME': 'Fixture', 'GIT_COMMITTER_NAME': 'Fixture',
                                       'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
                                       'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}):
            subprocess.run(['git', 'init', '-q'], check=True)
            subprocess.run(['git', '-c', 'commit.gpgsign=false', 'commit', '-q', '--allow-empty', '-m', 'fixture'], check=True)
            commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
            self.assertEqual(verify_checkout(commit), commit)
            for wrong in ('main', 'a' * 40, commit + '\n'):
                with self.subTest(commit=wrong), self.assertRaises(ValueError):
                    verify_checkout(wrong)


class PublicationOrchestration(unittest.TestCase):
    def test_both_publication_paths_wait_for_validation_of_the_selected_commit(self):
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.load((root / '.github/workflows/release.yml').read_text(), Loader=yaml.BaseLoader)
        jobs = workflow['jobs']
        for path in ('publish', 'reconcile'):
            self.assertIn('validate', jobs[path]['needs'], path)
        self.assertEqual(jobs['validate']['with']['commit'], '${{ needs.prepare.outputs.commit }}')
        self.assertEqual(jobs['validate']['uses'], './.github/workflows/checks.yml')

    def test_pr_validation_builds_without_registry_access_or_publication(self):
        root = Path(__file__).resolve().parents[1]
        ci = yaml.load((root / '.github/workflows/ci.yml').read_text(), Loader=yaml.BaseLoader)
        checks = yaml.load((root / '.github/workflows/checks.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(ci['jobs']['checks']['with']['build_image'], 'true')
        self.assertEqual(checks['permissions'], {'contents': 'read'})
        build = next(step for step in checks['jobs']['docker']['steps']
                     if step.get('uses', '').startswith('docker/build-push-action@'))
        self.assertEqual(build['with']['push'], 'false')
        self.assertNotIn('pull_request_target', ci['on'])
        self.assertNotIn('schedule', ci['on'])
