"""Publication recovery across disposable GitHub and registry HTTP services."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import contextlib
import io
import json
import os
from pathlib import Path
from threading import Thread
import unittest
from unittest.mock import patch

from scripts.release import Release
from scripts.release_publication import PublicationClient, stage, finish, journal, completed_release, reconcile, queue_pending, select_plan

COMMIT = 'a' * 40
PLAN = {'version': '8.2.0', 'commit': COMMIT, 'previous_tag': 'v8.1.0',
        'prs': [210], 'migration_notes': 'No manual steps required.',
        'manual_version': '', 'manual_notes': ''}
CONFIG = json.dumps({'config': {'Labels': {
    'org.opencontainers.image.source': 'https://github.com/owner/Reader',
    'org.opencontainers.image.revision': COMMIT,
    'org.opencontainers.image.version': '8.2.0'}}}).encode()


class PublicationHTTP(unittest.TestCase):
    def setUp(self):
        self.tags, self.releases, self.manifests, self.writes = {}, {}, {}, []
        self.config = CONFIG
        self.main_commit = COMMIT
        self.github_data = {}
        self.fail_path = None
        self.fail_write = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def handle_request(self):
                path = self.path.split('?')[0]
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                writing = self.command in ('POST', 'PATCH', 'PUT')
                if writing:
                    owner.writes.append((self.command, path, body))
                    if owner.fail_path == path or len(owner.writes) == owner.fail_write:
                        self.reply(503, {})
                        return
                if path == '/token':
                    self.reply(200, {'token': 'fixture'})
                elif path.endswith('/commits/main'):
                    self.reply(200, {'sha': owner.main_commit})
                elif path.endswith('/actions/workflows/release.yml/dispatches'):
                    self.send_response(204)
                    self.end_headers()
                elif '/manifests/' in path:
                    name = path.split('/manifests/')[1]
                    if self.command == 'PUT':
                        owner.manifests[name] = body
                    payload = owner.manifests.get(name)
                    self.reply(200 if payload else 404, payload or {}, raw=bool(payload),
                               headers={'Docker-Content-Digest': 'sha256:' + hashlib.sha256(payload).hexdigest()} if payload else {})
                elif '/blobs/' in path:
                    self.reply(200, owner.config, raw=True)
                elif path.endswith('/releases/generate-notes'):
                    self.reply(200, {'body': 'Changes'})
                elif '/git/ref/tags/' in path:
                    tag = path.split('/tags/')[1]
                    self.reply(200 if tag in owner.tags else 404, {'object': {'sha': owner.tags.get(tag)}})
                elif path.endswith('/git/refs'):
                    value = json.loads(body)
                    owner.tags[value['ref'].removeprefix('refs/tags/')] = value['sha']
                    self.reply(201, value)
                elif path.endswith('/releases'):
                    if self.command == 'POST':
                        value = json.loads(body)
                        value.update(id=len(owner.releases) + 1, html_url='https://example.invalid/release')
                        owner.releases[value['id']] = value
                        self.reply(201, value)
                    else:
                        self.reply(200, list(owner.releases.values()))
                elif '/releases/' in path:
                    if '/tags/' in path:
                        value = next((r for r in owner.releases.values() if r['tag_name'] == path.split('/tags/')[1]), None)
                    else:
                        value = owner.releases.get(int(path.rsplit('/', 1)[1]))
                    if value is not None and self.command == 'PATCH':
                        changes = json.loads(body)
                        # GitHub substitutes an untagged-* placeholder when a
                        # draft without a Git ref is PATCHed without tag_name.
                        if value['draft'] and value['tag_name'] not in owner.tags and 'tag_name' not in changes:
                            value['tag_name'] = 'untagged-fixture'
                        value.update(changes)
                    self.reply(200 if value else 404, value or {})
                elif path.removeprefix('/repos/owner/Reader/') in owner.github_data:
                    self.reply(200, owner.github_data[path.removeprefix('/repos/owner/Reader/')])
                else:
                    self.reply(404, {})

            def reply(self, status, value, raw=False, headers=None):
                payload = value if raw else json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/vnd.oci.image.manifest.v1+json' if raw else 'application/json')
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(payload)

            do_GET = do_HEAD = do_POST = do_PATCH = do_PUT = handle_request

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        url = f'http://127.0.0.1:{self.server.server_port}'
        self.client = PublicationClient('fixture', 'fixture', github=url, registry=url)
        self.release = Release('8.2.0', 'owner/Reader', COMMIT, 'refs/heads/main')

    def push_image(self):
        manifest = json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                               'config': {'digest': 'sha256:' + hashlib.sha256(self.config).hexdigest()}, 'layers': []}).encode()
        digest = 'sha256:' + hashlib.sha256(manifest).hexdigest()
        self.manifests[self.release.version] = self.manifests[digest] = manifest
        return manifest

    def initial_environment(self):
        from tests.test_auto_release import AutomaticRelease
        fixture = AutomaticRelease()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.git('tag', '-d', 'v8.0.0')
        commit = fixture.repository().commit
        self.release = Release('8.3.6', 'owner/Reader', commit, 'refs/heads/main')
        config = json.loads(CONFIG)
        config['config']['Labels'].update({'org.opencontainers.image.revision': commit,
                                           'org.opencontainers.image.version': '8.3.6'})
        self.config = json.dumps(config).encode()
        self.main_commit = commit
        env = {'GH_TOKEN': 'fixture', 'GITHUB_ACTOR': 'fixture', 'GITHUB_REPOSITORY': 'owner/Reader',
               'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
               'RELEASE_COMMIT': commit, 'RELEASE_PLAN': str(fixture.root / 'plan.json'),
               'GITHUB_OUTPUT': str(fixture.root / 'output'),
               'GITHUB_STEP_SUMMARY': str(fixture.root / 'summary'),
               'MANUAL_VERSION': '8.3.6', 'MANUAL_NOTES': 'No manual steps required.'}
        return fixture, env

    def run_command(self, command):
        from scripts import release_publication as publication
        with patch.object(publication, 'PublicationClient', return_value=self.client), \
                patch('sys.argv', ['publication', command]):
            publication.main()

    def test_initial_push_waits_without_build_publication_or_reconciliation(self):
        fixture, env = self.initial_environment()
        env.update(GITHUB_EVENT_NAME='push', MANUAL_VERSION='', MANUAL_NOTES='')
        with contextlib.chdir(fixture.root), patch.dict(os.environ, env), \
                contextlib.redirect_stdout(io.StringIO()):
            self.run_command('plan')
            self.assertIsNone(json.loads(Path(env['RELEASE_PLAN']).read_text()))
            output = Path(env['GITHUB_OUTPUT']).read_text()
            self.assertIn('eligible=false\n', output)
            self.assertIn('reconcile=false\n', output)
            self.assertIn('Awaiting the initial manual release', Path(env['GITHUB_STEP_SUMMARY']).read_text())
            self.run_command('reconcile')
        self.assertEqual(self.writes, [])

    def test_initial_release_requires_manual_version_and_explicit_notes(self):
        fixture, env = self.initial_environment()
        invalid = [
            {'MANUAL_NOTES': ''}, {'MANUAL_NOTES': ' <!-- template instructions --> '},
            {'MANUAL_VERSION': ''}, {'MANUAL_VERSION': 'v8.3.6'},
            {'MANUAL_VERSION': '8.3.6-rc.1'}, {'GITHUB_EVENT_NAME': 'push'},
        ]
        with contextlib.chdir(fixture.root), patch.dict(os.environ, env):
            for inputs in invalid:
                with self.subTest(inputs=inputs), patch.dict(os.environ, inputs), self.assertRaises(ValueError):
                    self.run_command('plan')
        self.assertEqual(self.writes, [])

    def test_initial_release_revalidates_recovers_exact_source_and_advances_normally(self):
        fixture, env = self.initial_environment()
        original = env['RELEASE_COMMIT']
        with contextlib.chdir(fixture.root), patch.dict(os.environ, env), \
                contextlib.redirect_stdout(io.StringIO()):
            self.run_command('plan')
            saved = json.loads(Path(env['RELEASE_PLAN']).read_text())
            self.assertEqual((saved['version'], saved['commit'], saved['previous_tag'], saved['prs']),
                             ('8.3.6', original, '', []))
            self.assertEqual(saved['migration_notes'], env['MANUAL_NOTES'])
            self.assertIn('image=ghcr.io/owner/reader\n', Path(env['GITHUB_OUTPUT']).read_text())
            self.run_command('stage')
            newer = fixture.commit('app/main.py', 'later fix')
            fixture.pr(newer, 10)
            self.github_data = fixture.api.responses
            # A push after a failed initial build recovers the original decision.
            with patch.dict(os.environ, {'RELEASE_COMMIT': newer, 'MANUAL_VERSION': '', 'MANUAL_NOTES': '',
                                         'GITHUB_EVENT_NAME': 'push'}):
                self.run_command('plan')
                self.assertEqual(json.loads(Path(env['RELEASE_PLAN']).read_text()), saved)
                with self.assertRaisesRegex(ValueError, 'checkout differs'):
                    self.run_command('stage')
            fixture.git('checkout', original)
            with patch.dict(os.environ, {'MANUAL_VERSION': '', 'MANUAL_NOTES': '', 'GITHUB_EVENT_NAME': 'push'}):
                self.run_command('stage')
                image = self.push_image()
                digest = 'sha256:' + hashlib.sha256(image).hexdigest()
                with patch.dict(os.environ, {'IMAGE_DIGEST': digest}):
                    self.run_command('finish')
            self.assertEqual(self.tags['v8.3.6'], original)
            self.assertEqual(self.manifests['latest'], image)
            self.assertEqual(self.manifests[original], image)
            self.assertTrue(completed_release(self.releases[1]))
            notes = next(json.loads(body) for _, path, body in self.writes if path.endswith('generate-notes'))
            self.assertNotIn('previous_tag_name', notes)
            self.assertEqual(notes['target_commitish'], original)
            fixture.git('tag', 'v8.3.6', original)
            fixture.git('checkout', 'main')
            with patch.dict(os.environ, {'RELEASE_COMMIT': newer, 'MANUAL_VERSION': '', 'MANUAL_NOTES': '',
                                         'GITHUB_EVENT_NAME': 'push'}):
                self.run_command('plan')
            following = json.loads(Path(env['RELEASE_PLAN']).read_text())
            self.assertEqual((following['version'], following['previous_tag'], following['prs']),
                             ('8.3.7', 'v8.3.6', [10]))

    def test_initial_stage_rejects_changed_notes_before_reserving_a_version(self):
        fixture, env = self.initial_environment()
        with contextlib.chdir(fixture.root), patch.dict(os.environ, env):
            self.run_command('plan')
            path = Path(env['RELEASE_PLAN'])
            saved = json.loads(path.read_text())
            saved['manual_notes'] = 'Instructions changed after approval.'
            path.write_text(json.dumps(saved))
            with self.assertRaisesRegex(ValueError, 'Release plan or PR metadata changed'):
                self.run_command('stage')
        self.assertEqual(self.writes, [])

    def test_initial_publication_recovers_each_interrupted_write_without_rebuilding(self):
        fixture, env = self.initial_environment()
        with contextlib.chdir(fixture.root), patch.dict(os.environ, env), \
                contextlib.redirect_stdout(io.StringIO()):
            self.run_command('plan')
            saved = json.loads(Path(env['RELEASE_PLAN']).read_text())
            for failure in range(1, 7):
                with self.subTest(failure=failure):
                    self.tags.clear()
                    self.releases.clear()
                    self.manifests.clear()
                    self.writes.clear()
                    self.fail_write = None
                    self.run_command('stage')
                    image = self.push_image()
                    digest = 'sha256:' + hashlib.sha256(image).hexdigest()
                    self.writes.clear()
                    self.fail_write = failure
                    with patch.dict(os.environ, {'IMAGE_DIGEST': digest}), self.assertRaises(RuntimeError):
                        self.run_command('finish')
                    self.fail_write = None
                    with patch.dict(os.environ, {'MANUAL_VERSION': '', 'MANUAL_NOTES': ''}):
                        self.run_command('plan')
                    self.assertEqual(json.loads(Path(env['RELEASE_PLAN']).read_text()), saved)
                    self.run_command('stage')
                    self.assertIn('build=false\n', Path(env['GITHUB_OUTPUT']).read_text())
                    with patch.dict(os.environ, {'IMAGE_DIGEST': digest}):
                        self.run_command('finish')
                    self.assertTrue(completed_release(self.releases[1]))
                    self.assertEqual(journal(self.releases[1])['digest'], digest)
                    self.assertEqual(self.manifests['latest'], image)
                    self.assertEqual(self.manifests[self.release.commit], image)
                    self.assertFalse(any(path.endswith('/manifests/8.3.6') for _, path, _ in self.writes))

    def test_one_image_finishes_with_identical_aliases(self):
        reservation = stage(self.release, self.client, PLAN)
        self.assertTrue(reservation['build'])
        original = self.push_image()
        finish(self.release, self.client, PLAN)
        self.assertEqual(self.manifests['latest'], original)
        self.assertEqual(self.manifests[COMMIT], original)
        self.assertFalse(self.releases[1]['draft'])
        self.assertEqual(self.tags['v8.2.0'], COMMIT)

    def test_recording_the_built_image_preserves_the_reserved_draft_tag(self):
        stage(self.release, self.client, PLAN)
        original = self.push_image()
        recovered = stage(self.release, self.client, PLAN)
        self.assertFalse(recovered['build'])
        self.assertEqual(self.releases[1]['tag_name'], self.release.tag)
        self.assertEqual(self.releases[1]['target_commitish'], COMMIT)
        self.assertEqual(journal(self.releases[1])['phase'], 'built')
        self.assertFalse(stage(self.release, self.client, PLAN)['build'])
        finish(self.release, self.client, PLAN, recovered['digest'])
        self.assertEqual(self.manifests['latest'], original)
        self.assertTrue(completed_release(self.releases[1]))

    def test_a_missing_reservation_stops_before_publishing_aliases(self):
        stage(self.release, self.client, PLAN)
        self.push_image()
        original = self.client.update

        def remove_after_update(*args, **kwargs):
            result = original(*args, **kwargs)
            self.releases.clear()
            return result

        with patch.object(self.client, 'update', side_effect=remove_after_update):
            with self.assertRaisesRegex(ValueError, 'Publication reservation is missing'):
                finish(self.release, self.client, PLAN)
        self.assertNotIn('latest', self.manifests)
        self.assertNotIn(COMMIT, self.manifests)
        self.assertFalse(self.tags)

    def test_each_interrupted_external_write_resumes_without_rebuilding(self):
        for failure in range(1, 7):
            with self.subTest(failure=failure):
                self.tags.clear()
                self.releases.clear()
                self.manifests.clear()
                self.writes.clear()
                self.fail_write = None
                stage(self.release, self.client, PLAN)
                original = self.push_image()
                self.manifests['latest'] = b'{"schemaVersion":2,"old":true}'
                self.writes.clear()
                self.fail_write = failure
                with self.assertRaises(RuntimeError):
                    finish(self.release, self.client, PLAN)
                if failure < 6:
                    self.assertEqual(self.manifests['latest'], b'{"schemaVersion":2,"old":true}')
                self.fail_write = None
                self.assertFalse(stage(self.release, self.client, PLAN)['build'])
                finish(self.release, self.client, PLAN)
                self.assertEqual(self.manifests['latest'], original)
                self.assertEqual(self.manifests[COMMIT], original)
                self.assertTrue(completed_release(self.releases[1]))
                finish(self.release, self.client, PLAN)
                self.assertEqual(journal(self.releases[1])['phase'], 'complete')
                self.assertFalse(any(path.endswith('/manifests/8.2.0') for _, path, _ in self.writes))

    def test_an_unrelated_occupied_version_or_plan_is_never_adopted(self):
        self.push_image()
        with self.assertRaises(ValueError):
            stage(self.release, self.client, PLAN)
        self.manifests.clear()
        stage(self.release, self.client, PLAN)
        changed = {**PLAN, 'migration_notes': 'Changed without review'}
        with self.assertRaises(ValueError):
            stage(self.release, self.client, changed)

    def test_a_recorded_image_cannot_disappear_or_change(self):
        stage(self.release, self.client, PLAN)
        original = self.push_image()
        stage(self.release, self.client, PLAN)
        del self.manifests['8.2.0']
        with self.assertRaises(ValueError):
            stage(self.release, self.client, PLAN)
        self.manifests['8.2.0'] = original + b' '
        with self.assertRaises(ValueError):
            stage(self.release, self.client, PLAN)
        self.assertNotIn('latest', self.manifests)

    def test_different_git_tag_or_commit_alias_blocks_latest(self):
        for conflict in ('git', 'image'):
            with self.subTest(conflict=conflict):
                self.tags.clear()
                self.manifests.clear()
                self.releases.clear()
                stage(self.release, self.client, PLAN)
                self.push_image()
                if conflict == 'git':
                    self.tags['v8.2.0'] = 'f' * 40
                else:
                    self.manifests[COMMIT] = b'{"schemaVersion":2,"conflict":true}'
                with self.assertRaises(ValueError):
                    finish(self.release, self.client, PLAN)
                self.assertNotIn('latest', self.manifests)

    def test_recovery_cannot_move_latest_backwards(self):
        stage(self.release, self.client, PLAN)
        self.push_image()
        self.releases[2] = {'tag_name': 'v9.0.0', 'draft': False, 'prerelease': False, 'body': ''}
        with self.assertRaises(ValueError):
            finish(self.release, self.client, PLAN)
        self.assertNotIn('latest', self.manifests)

    def test_announced_but_incomplete_release_is_not_a_stable_baseline(self):
        stage(self.release, self.client, PLAN)
        self.push_image()
        self.fail_path = '/v2/owner/reader/manifests/latest'
        with self.assertRaises(RuntimeError):
            finish(self.release, self.client, PLAN)
        self.assertFalse(self.releases[1]['draft'])
        self.assertFalse(completed_release(self.releases[1]))
        self.assertEqual(journal(self.releases[1])['phase'], 'announced')

    def test_index_and_attestations_are_copied_as_a_whole(self):
        stage(self.release, self.client, PLAN)
        application = self.push_image()
        child_digest = 'sha256:' + hashlib.sha256(application).hexdigest()
        index = json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json',
                            'manifests': [{'digest': child_digest, 'platform': {'architecture': 'amd64', 'os': 'linux'}},
                                          {'digest': 'sha256:' + 'e' * 64, 'platform': {'architecture': 'unknown', 'os': 'unknown'}}]}).encode()
        self.manifests['8.2.0'] = index
        finish(self.release, self.client, PLAN)
        self.assertEqual(self.manifests['latest'], index)
        self.assertEqual(self.manifests[COMMIT], index)

    def test_build_output_digest_must_match_before_any_alias_is_published(self):
        stage(self.release, self.client, PLAN)
        self.push_image()
        with self.assertRaises(ValueError):
            finish(self.release, self.client, PLAN, 'sha256:' + 'f' * 64)
        self.assertNotIn('latest', self.manifests)
        self.assertNotIn(COMMIT, self.manifests)

    def test_rollout_reuses_the_recorded_stable_image_and_preserves_old_short_aliases(self):
        from tests.test_auto_release import AutomaticRelease
        fixture = AutomaticRelease()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        stable_commit = fixture.git('rev-parse', 'HEAD')
        fixture.git('tag', 'v8.2.0')
        latest_commit = fixture.commit('README.md', 'Documentation only')
        config = json.loads(CONFIG)
        config['config']['Labels']['org.opencontainers.image.revision'] = stable_commit
        self.config = json.dumps(config).encode()
        original = self.push_image()
        digest = 'sha256:' + hashlib.sha256(original).hexdigest()
        self.releases[1] = {'tag_name': 'v8.2.0', 'draft': False, 'prerelease': False,
                            'body': 'Exact image: ghcr.io/owner/reader@' + digest}
        self.manifests[stable_commit[:7]] = b'{"schemaVersion":2,"old":true}'
        repo = Release('1.0.0', 'owner/Reader', latest_commit, 'refs/heads/main')
        with contextlib.chdir(fixture.root), contextlib.redirect_stdout(io.StringIO()):
            reconcile(self.client, repo)
            self.assertEqual(self.manifests['latest'], original)
            self.assertEqual(self.manifests[stable_commit], original)
            self.assertEqual(self.manifests[stable_commit[:7]], b'{"schemaVersion":2,"old":true}')
            self.assertEqual(len(self.releases), 1)
            self.assertFalse(any(path.endswith('/manifests/8.2.0') for _, path, _ in self.writes))
            self.releases[1]['body'] = 'Unknown image; no recorded digest'
            with self.assertRaises(ValueError):
                reconcile(self.client, repo)

    def test_an_unbuilt_reservation_preserves_its_approved_source_and_version(self):
        from tests.test_auto_release import AutomaticRelease
        from scripts.auto_release import serialized
        fixture = AutomaticRelease()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        first = fixture.commit('app/main.py', 'feature with a broken build')
        fixture.pr(first, 7, 'minor')
        saved = serialized(fixture.planned())
        self.releases[1] = {'tag_name': 'v8.0.0', 'draft': False, 'prerelease': False, 'body': ''}
        self.release = Release('8.1.0', 'owner/Reader', first, 'refs/heads/main')
        stage(self.release, self.client, saved)
        fixture.pr(fixture.commit('requirements.txt', 'later build fix'), 8)
        fixture.pr(fixture.commit('docker-compose.yml', 'breaking change'), 9, 'major')
        with contextlib.chdir(fixture.root), patch.dict(os.environ, {'MANUAL_VERSION': '', 'MANUAL_NOTES': ''}):
            self.assertEqual(select_plan(self.client, fixture.repository()), saved)
            self.assertEqual(self.releases[2]['target_commitish'], first)
            self.assertEqual(self.releases[2]['tag_name'], 'v8.1.0')
            self.assertEqual(journal(self.releases[2])['plan'], saved)
            self.assertTrue(stage(self.release, self.client, saved)['build'])
            for inputs in ({'MANUAL_VERSION': '9.0.0'}, {'MANUAL_NOTES': 'Change the approved instructions'}):
                with patch.dict(os.environ, inputs), self.assertRaisesRegex(ValueError, 'empty manual inputs'):
                    select_plan(self.client, fixture.repository())
            self.tags['v8.1.0'] = first
            with self.assertRaisesRegex(ValueError, 'unexpected published release or Git tag'):
                stage(self.release, self.client, saved)
            self.tags.clear()
            config = json.loads(CONFIG)
            config['config']['Labels'].update({'org.opencontainers.image.revision': first,
                                               'org.opencontainers.image.version': '8.1.0'})
            self.config = json.dumps(config).encode()
            self.push_image()
            self.assertEqual(select_plan(self.client, fixture.repository()), saved)
            self.assertFalse(stage(self.release, self.client, saved)['build'])

    def test_newer_main_changes_after_recovery_are_queued_without_republishing(self):
        self.assertFalse(queue_pending(self.client, self.release))
        self.assertEqual(self.writes, [])
        self.main_commit = 'f' * 40
        self.assertTrue(queue_pending(self.client, self.release))
        self.assertEqual([(method, path, json.loads(body)) for method, path, body in self.writes], [
            ('POST', '/repos/owner/Reader/actions/workflows/release.yml/dispatches', {'ref': 'main'})])


class PublicationPlanRecovery(unittest.TestCase):
    def test_cli_recovers_the_original_commit_and_rechecks_its_reviewed_metadata(self):
        from tests.test_auto_release import AutomaticRelease
        from scripts.auto_release import serialized
        from scripts import release_publication as publication
        fixture = AutomaticRelease()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        original = fixture.commit('app/main.py', 'feature')
        pr = fixture.pr(original, 7, 'minor')
        saved = serialized(fixture.planned())
        state = {'format': 1, 'phase': 'built', 'plan': saved, 'digest': 'sha256:' + 'a' * 64}
        fixture.api.responses['releases'].append({
            'tag_name': 'v8.1.0', 'target_commitish': original, 'draft': True, 'prerelease': False,
            'body': '<!-- ebookreader-publication\n' + json.dumps(state) + '\n-->'})
        newer = fixture.commit('requirements.txt', 'fix')
        fixture.pr(newer, 8)
        path = fixture.root / 'plan.json'
        output = fixture.root / 'output'
        env = {'GH_TOKEN': 'fixture', 'GITHUB_ACTOR': 'fixture', 'GITHUB_REPOSITORY': 'owner/Reader',
               'GITHUB_REF': 'refs/heads/main', 'RELEASE_COMMIT': newer, 'RELEASE_PLAN': str(path),
               'GITHUB_OUTPUT': str(output), 'MANUAL_VERSION': '', 'MANUAL_NOTES': ''}
        with contextlib.chdir(fixture.root), patch.dict(os.environ, env), \
                patch.object(publication, 'PublicationClient', return_value=fixture.api):
            with patch('sys.argv', ['publication', 'plan']):
                publication.main()
            self.assertEqual(json.loads(path.read_text()), saved)
            self.assertIn('commit=' + original, output.read_text())
            # A release image cannot be built from a different checkout.
            with patch('sys.argv', ['publication', 'stage']), self.assertRaises(ValueError):
                publication.main()
            fixture.git('checkout', original)
            pr['body'] = '## Upgrade notes\nInstructions changed during validation.'
            with patch.dict(os.environ, {'RELEASE_COMMIT': original}), \
                    patch('sys.argv', ['publication', 'stage']), self.assertRaises(ValueError):
                publication.main()
            # There is no publication write method on this read-only fixture.
            self.assertFalse(any('git/refs' in call for call in fixture.api.calls))


if __name__ == '__main__':
    unittest.main()
