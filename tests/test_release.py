"""Release behavior at the GitHub/registry HTTP boundary, without publishing."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from scripts import release as release_module
from scripts.release import Client, Release, preflight, publish

SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64


class ReleaseValidation(unittest.TestCase):
    def test_only_stable_versions_without_shell_or_tag_syntax_are_accepted(self):
        self.assertEqual(Release('1.0.0', 'owner/Reader', SHA, 'refs/heads/main').tag, 'v1.0.0')
        for version in ('v1.0.0', '01.0.0', '1.00.0', '1.0', '0.1.0', '1.0.0-rc.1',
                        '1.0.0+build', '1.0.0\n', '$(echo 1.0.0)'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                Release(version, 'owner/Reader', SHA, 'refs/heads/main')

    def test_releases_require_main_and_an_exact_commit(self):
        for ref, sha in (('refs/heads/feature', SHA), ('refs/tags/v1.0.0', SHA),
                         ('refs/heads/main', 'main')):
            with self.subTest(ref=ref, sha=sha), self.assertRaises(ValueError):
                Release('1.0.0', 'owner/Reader', sha, ref)


class ReleaseHTTP(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.responses = {}
        calls, responses = self.calls, self.responses

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def handle_request(self):
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length)) if length else None
                calls.append((self.command, self.path, data))
                key = self.path.split('?')[0]
                status, payload, headers = responses.get(key, (404, {}, {}))
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(json.dumps(payload).encode())

            do_GET = do_HEAD = do_POST = handle_request

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        base = f'http://127.0.0.1:{self.server.server_port}'
        self.client = Client('disposable-token', 'fixture', github=base, registry=base)
        self.release = Release('1.0.0', 'owner/Reader', SHA, 'refs/heads/main')
        self.responses['/token'] = (200, {'token': 'disposable-registry-token'}, {})
        self.manifest = '/v2/owner/reader/manifests/1.0.0'
        self.repo = '/repos/owner/Reader/'
        self.responses[self.repo + 'releases'] = (200, [], {})

    def close_server(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def test_unused_version_checks_git_release_and_registry_without_writing(self):
        preflight(self.release, self.client)
        self.assertEqual([(method, path.split('?')[0]) for method, path, _ in self.calls], [
            ('GET', self.repo + 'git/ref/tags/v1.0.0'),
            ('GET', self.repo + 'releases/tags/v1.0.0'),
            ('GET', '/token'), ('HEAD', self.manifest),
            ('GET', self.repo + 'releases')])

    def test_reusing_any_published_reference_is_refused(self):
        for path in (self.repo + 'git/ref/tags/v1.0.0',
                     self.repo + 'releases/tags/v1.0.0', self.manifest):
            with self.subTest(path=path):
                self.responses[path] = (200, {'exists': True}, {'Docker-Content-Digest': DIGEST})
                with self.assertRaises(ValueError):
                    preflight(self.release, self.client)
                del self.responses[path]
        self.assertFalse(any(method == 'POST' for method, _, _ in self.calls))

    def test_unused_versions_cannot_go_backwards_and_api_failures_block_publication(self):
        self.responses[self.repo + 'releases'] = (200, [
            {'tag_name': 'v8.0.0', 'draft': False, 'prerelease': False}], {})
        with self.assertRaises(ValueError):
            preflight(self.release, self.client)
        self.responses[self.repo + 'releases'] = (503, {}, {})
        with self.assertRaises(RuntimeError):
            preflight(self.release, self.client)
        self.assertFalse(any(method == 'POST' for method, _, _ in self.calls))

    def test_automatic_notes_use_the_same_stable_boundary_as_the_version_plan(self):
        self.responses[self.manifest] = (200, {}, {'Docker-Content-Digest': DIGEST})
        self.responses[self.repo + 'releases/generate-notes'] = (200, {'body': 'Changes'}, {})
        self.responses[self.repo + 'git/refs'] = (201, {}, {})
        self.responses[self.repo + 'releases'] = (201, {'html_url': 'https://example.invalid/release'}, {})
        publish(self.release, self.client, DIGEST, 'No manual steps required', 'v8.0.0')
        note_request = next(data for _, path, data in self.calls if path.endswith('generate-notes'))
        self.assertEqual(note_request['previous_tag_name'], 'v8.0.0')
        self.assertEqual(note_request['target_commitish'], SHA)

    def test_artifact_version_and_commit_reach_preflight_and_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory, 'release-plan.json')
            artifact.write_text(json.dumps({'version': '1.0.0', 'commit': SHA,
                                            'migration_notes': 'Upgrade instruction from reviewed PR.',
                                            'previous_tag': 'v0.9.0'}))
            env = {'RELEASE_PLAN': str(artifact), 'RELEASE_COMMIT': SHA, 'GITHUB_SHA': 'c' * 40,
                   'GITHUB_REPOSITORY': 'owner/Reader', 'GITHUB_REF': 'refs/heads/main',
                   'GH_TOKEN': 'fixture', 'GITHUB_ACTOR': 'fixture', 'IMAGE_DIGEST': DIGEST,
                   'GITHUB_OUTPUT': str(Path(directory, 'outputs'))}
            with patch.dict(os.environ, env), patch.object(release_module, 'Client', return_value=self.client), \
                    contextlib.redirect_stdout(io.StringIO()):
                with patch('sys.argv', ['release.py', 'preflight']):
                    release_module.main()
                self.responses[self.manifest] = (200, {}, {'Docker-Content-Digest': DIGEST})
                self.responses[self.repo + 'releases/generate-notes'] = (200, {'body': 'Changes'}, {})
                self.responses[self.repo + 'git/refs'] = (201, {}, {})
                self.responses[self.repo + 'releases'] = (201, {'html_url': 'https://example.invalid/release'}, {})
                with patch('sys.argv', ['release.py', 'publish']):
                    release_module.main()
                tag = next(data for _, path, data in self.calls if path.endswith('git/refs'))
                self.assertEqual(tag['sha'], SHA)
                body = next(data['body'] for _, path, data in self.calls if path.endswith('releases') and data)
                self.assertIn('Upgrade instruction from reviewed PR.', body)
                with patch.dict(os.environ, {'RELEASE_COMMIT': 'd' * 40}), self.assertRaises(ValueError):
                    release_module.main()

    def test_authentication_and_service_errors_are_not_treated_as_unused_versions(self):
        for path in (self.repo + 'git/ref/tags/v1.0.0',
                     self.repo + 'releases/tags/v1.0.0', '/token', self.manifest):
            original = self.responses.get(path)
            for status in (401, 403, 429, 500):
                with self.subTest(path=path, status=status):
                    self.responses[path] = (status, {}, {})
                    with self.assertRaises(RuntimeError):
                        preflight(self.release, self.client)
            if original:
                self.responses[path] = original
            else:
                del self.responses[path]

    def test_a_manifest_without_a_digest_is_not_safe_to_replace(self):
        self.responses[self.manifest] = (200, {}, {})
        with self.assertRaises(RuntimeError):
            preflight(self.release, self.client)

    def test_release_announces_the_verified_digest_and_tags_the_checked_commit(self):
        self.responses[self.manifest] = (200, {}, {'Docker-Content-Digest': DIGEST})
        self.responses[self.repo + 'releases/generate-notes'] = (200, {'body': '## Changes\n* Add library tabs (#12)'}, {})
        self.responses[self.repo + 'git/refs'] = (201, {'ref': 'refs/tags/v1.0.0'}, {})
        self.responses[self.repo + 'releases'] = (201, {'html_url': 'https://example.invalid/v1.0.0'}, {})
        result = publish(self.release, self.client, DIGEST, 'Restore your backup before downgrading.')
        self.assertEqual(result['html_url'], 'https://example.invalid/v1.0.0')
        writes = [(path, data) for method, path, data in self.calls if method == 'POST']
        self.assertEqual(writes[1], (self.repo + 'git/refs', {'ref': 'refs/tags/v1.0.0', 'sha': SHA}))
        body = writes[2][1]
        self.assertEqual(body['target_commitish'], SHA)
        self.assertFalse(body['draft'])
        self.assertFalse(body['prerelease'])
        for value in ('ghcr.io/owner/reader:1.0.0', 'ghcr.io/owner/reader@' + DIGEST,
                      'linux/amd64', 'Add library tabs (#12)', 'Restore your backup before downgrading.'):
            self.assertIn(value, body['body'])

    def test_a_missing_or_mismatched_build_digest_cannot_create_a_release(self):
        self.responses[self.manifest] = (200, {}, {'Docker-Content-Digest': 'sha256:' + 'c' * 64})
        for digest in ('', 'latest', DIGEST):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                publish(self.release, self.client, digest)
        self.assertFalse(any(method == 'POST' for method, _, _ in self.calls))

    def test_failure_to_reserve_the_tag_cannot_announce_a_release(self):
        self.responses[self.manifest] = (200, {}, {'Docker-Content-Digest': DIGEST})
        self.responses[self.repo + 'releases/generate-notes'] = (200, {'body': 'Changes'}, {})
        self.responses[self.repo + 'git/refs'] = (422, {}, {})
        with self.assertRaises(RuntimeError):
            publish(self.release, self.client, DIGEST)
        self.assertFalse(any(path == self.repo + 'releases' for _, path, _ in self.calls))
