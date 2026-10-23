"""Release decisions over real Git history and GitHub PR metadata."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scripts.auto_release import History, check_pr, pages, plan, pr_policy, serialized
from scripts.release import Release
from scripts import auto_release


class API:
    def __init__(self):
        self.responses = {'releases': [{'tag_name': 'v8.0.0', 'draft': False, 'prerelease': False}]}
        self.calls = []

    def api(self, repo, path):
        self.calls.append(path)
        return copy.deepcopy(self.responses[path.split('?')[0]])


class AutomaticRelease(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.history = History(self.root)
        self.git('init', '-b', 'main')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Release fixture')
        self.commit('app/main.py', 'initial')
        self.git('tag', 'v8.0.0')
        self.api = API()

    def git(self, *args):
        return self.history.git(*args)

    def commit(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.git('add', '--', path)
        self.git('commit', '-m', 'An ordinary title, without version syntax')
        return self.git('rev-parse', 'HEAD')

    def pr(self, commit, number, impact='patch', body='## Upgrade notes\nNo manual steps required'):
        pr = {'number': number, 'merged_at': '2026-10-15T00:00:00Z', 'merge_commit_sha': commit,
              'base': {'ref': 'main', 'repo': {'full_name': 'owner/Reader'}},
              'labels': [{'name': f'release:{impact}'}], 'body': body, 'changed_files': 1}
        self.api.responses[f'pulls/{number}'] = pr
        self.api.responses[f'commits/{commit}/pulls'] = [{'number': number}]
        return pr

    def repository(self):
        return Release('1.0.0', 'owner/Reader', self.git('rev-parse', 'HEAD'), 'refs/heads/main')

    def planned(self):
        return plan(self.api, self.repository(), self.history)

    def test_failed_release_then_new_merge_uses_the_largest_pending_impact_and_all_notes(self):
        first = self.commit('app/main.py', 'feature')
        self.pr(first, 7, 'minor', '## Upgrade notes\nChange setting A.\n\n## Validation\nTests passed.')
        self.assertEqual(self.planned().version, '8.1.0')  # Nothing published yet.
        second = self.commit('requirements.txt', 'dependency')
        self.pr(second, 8, 'patch', '## Upgrade notes\nBack up setting B.')
        result = self.planned()
        self.assertEqual((result.version, result.commit, result.prs), ('8.1.0', second, (7, 8)))
        self.assertIn('Change setting A.', result.migration_notes)
        self.assertIn('Back up setting B.', result.migration_notes)
        self.assertNotIn('Tests passed', result.migration_notes)
        self.git('tag', 'v8.1.0')
        self.api.responses['releases'].append({'tag_name': 'v8.1.0', 'draft': False, 'prerelease': False})
        third = self.commit('app/main.py', 'fix after release')
        self.pr(third, 9)
        self.assertEqual((self.planned().version, self.planned().prs), ('8.1.1', (9,)))

    def test_docs_tests_ci_and_release_tooling_alone_do_not_publish(self):
        for path in ('README.md', 'docs/a.png', 'tests/test_a.py', '.github/workflows/checks.yml',
                     'scripts/release.py', 'scripts/test_backend.py'):
            self.commit(path, 'maintenance')
        self.assertIsNone(self.planned())
        self.assertFalse(any('/pulls' in path for path in self.api.calls))

    def test_major_impact_and_numeric_version_order(self):
        self.git('tag', 'v8.9.9')
        self.api.responses['releases'].append({'tag_name': 'v8.9.9', 'draft': False, 'prerelease': False})
        commit = self.commit('docker-compose.yml', 'breaking configuration')
        pr = self.pr(commit, 3, 'major')
        self.assertEqual(self.planned().version, '9.0.0')
        pr['labels'] = [{'name': 'release:minor'}]
        self.assertEqual(self.planned().version, '8.10.0')

    def test_missing_ambiguous_or_unknown_labels_and_empty_notes_block_publication(self):
        commit = self.commit('app/main.py', 'feature')
        pr = self.pr(commit, 3)
        for labels in ([], [{'name': 'release:patch'}, {'name': 'release:minor'}], [{'name': 'release:unknown'}]):
            with self.subTest(labels=labels):
                pr['labels'] = labels
                with self.assertRaises(ValueError):
                    self.planned()
        pr['labels'] = [{'name': 'release:patch'}]
        for body in ('', '## Upgrade notes\n<!-- Explain steps -->', '## Upgrade notes\n## Validation\nPassed',
                     '## Upgrade notes\nOne\n## Upgrade notes\nTwo'):
            with self.subTest(body=body):
                pr['body'] = body
                with self.assertRaises(ValueError):
                    self.planned()

    def test_direct_application_push_or_wrong_pr_provenance_blocks_publication(self):
        commit = self.commit('app/main.py', 'unreviewed')
        self.api.responses[f'commits/{commit}/pulls'] = []
        with self.assertRaises(ValueError):
            self.planned()
        pr = self.pr(commit, 4)
        for path, value in (('ref', 'other'), ('repo', {'full_name': 'another/repository'})):
            original = pr['base'][path]
            pr['base'][path] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.planned()
            pr['base'][path] = original
        pr['merge_commit_sha'] = self.git('rev-parse', 'v8.0.0')
        with self.assertRaises(ValueError):
            self.planned()

    def test_rebased_pr_is_classified_once_for_multiple_first_parent_commits(self):
        first = self.commit('app/main.py', 'rebased one')
        last = self.commit('app/other.py', 'rebased two')
        self.pr(last, 5, 'minor')
        self.api.responses[f'commits/{first}/pulls'] = [{'number': 5}]
        self.assertEqual((self.planned().version, self.planned().prs), ('8.1.0', (5,)))

    def test_two_parent_merge_uses_the_merge_result_and_excludes_unmerged_branches(self):
        self.git('switch', '-c', 'feature')
        self.commit('app/new.py', 'feature')
        self.git('switch', 'main')
        self.commit('README.md', 'docs')
        self.git('merge', '--no-ff', 'feature', '-m', 'Merge feature')
        merged = self.git('rev-parse', 'HEAD')
        self.pr(merged, 6)
        self.git('switch', '-c', 'not-merged')
        self.commit('app/new.py', 'future breaking change')
        self.git('switch', 'main')
        self.assertEqual((self.planned().commit, self.planned().prs), (merged, (6,)))

    def test_missing_baseline_waits_but_rewritten_main_fails_closed(self):
        self.api.responses['releases'] = []
        self.assertIsNone(self.planned())
        self.api.responses['releases'] = [{'tag_name': 'v8.0.0', 'draft': False, 'prerelease': False}]
        self.git('switch', '--orphan', 'replacement')
        self.commit('app/new.py', 'rewritten main')
        with self.assertRaises(ValueError):
            self.planned()

    def test_drafts_and_prereleases_do_not_advance_the_stable_baseline(self):
        self.api.responses['releases'] += [
            {'tag_name': 'v99.0.0', 'draft': True, 'prerelease': False},
            {'tag_name': 'v9.0.0', 'draft': False, 'prerelease': True},
            {'tag_name': 'v9.0.0-rc.1', 'draft': False, 'prerelease': False}]
        commit = self.commit('entrypoint.sh', 'fix')
        self.pr(commit, 7)
        self.assertEqual(self.planned().version, '8.0.1')

    def test_manual_override_preserves_reviewed_notes_and_cannot_lower_the_impact(self):
        commit = self.commit('app/main.py', 'feature')
        self.pr(commit, 7, 'minor')
        result = self.planned()
        overridden = serialized(result, '9.0.0', 'Update the configuration.')
        self.assertEqual(overridden['version'], '9.0.0')
        self.assertEqual(overridden['commit'], commit)
        self.assertIn('No manual steps required', overridden['migration_notes'])
        self.assertIn('Update the configuration.', overridden['migration_notes'])
        for version in ('8.0.1', 'v9.0.0', '9.0.0-rc.1'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                serialized(result, version)
        with self.assertRaises(ValueError):
            serialized(None, '9.0.0')

    def test_pr_policy_detects_renames_out_of_runtime_and_incomplete_file_lists(self):
        pr = self.pr(self.git('rev-parse', 'HEAD'), 10)
        self.api.responses['pulls/10/files'] = [{'filename': 'docs/old.py', 'previous_filename': 'app/old.py'}]
        self.assertTrue(check_pr(self.api, self.repository(), 10))
        pr['labels'] = []
        with self.assertRaises(ValueError):
            check_pr(self.api, self.repository(), 10)
        self.api.responses['pulls/10/files'] = [{'filename': 'docs/example.md'}]
        self.assertFalse(check_pr(self.api, self.repository(), 10))
        pr['changed_files'] = 3001
        with self.assertRaises(ValueError):
            check_pr(self.api, self.repository(), 10)

    def test_notes_template_comment_does_not_count_as_upgrade_instructions(self):
        self.assertEqual(pr_policy({'number': 3, 'labels': [{'name': 'release:patch'}],
                                    'body': '## Upgrade notes\n<!-- Example -->\nNo manual steps required.\n'})[1],
                         'No manual steps required.')

    def test_saved_plan_pins_the_checked_commit_and_metadata_drift_blocks_publication(self):
        commit = self.commit('app/main.py', 'fix')
        pr = self.pr(commit, 7)
        artifact = self.root / 'release-plan.json'
        output = self.root / 'outputs'
        env = {'GITHUB_REPOSITORY': 'owner/Reader', 'GITHUB_SHA': 'b' * 40,
               'RELEASE_COMMIT': commit, 'GITHUB_REF': 'refs/heads/main',
               'GH_TOKEN': 'fixture', 'GITHUB_ACTOR': 'fixture', 'RELEASE_PLAN': str(artifact),
               'GITHUB_OUTPUT': str(output), 'MANUAL_VERSION': '', 'MANUAL_NOTES': ''}
        with patch.dict(os.environ, env), patch.object(auto_release, 'Client', return_value=self.api), \
                patch.object(auto_release, 'History', return_value=self.history), contextlib.redirect_stdout(io.StringIO()):
            self.api.responses['pulls/7/files'] = [{'filename': 'app/main.py'}]
            with patch.dict(os.environ, {'GITHUB_REF': 'refs/pull/7/merge', 'PR_NUMBER': '7'}), \
                    patch('sys.argv', ['auto_release', 'check-pr']):
                auto_release.main()
            with patch('sys.argv', ['auto_release', 'plan']):
                auto_release.main()
            self.assertEqual(json.loads(artifact.read_text())['commit'], commit)
            self.assertIn('version=8.0.1', output.read_text())
            with patch('sys.argv', ['auto_release', 'verify']):
                auto_release.main()
                pr['body'] = '## Upgrade notes\nNew instructions after tests started.'
                with self.assertRaises(ValueError):
                    auto_release.main()


class Pagination(unittest.TestCase):
    def test_all_pages_are_consumed_before_deciding(self):
        class Paginated:
            def api(self, repo, path):
                page = int(parse_qs(urlsplit(path).query)['page'][0])
                return list(range(100)) if page == 1 else [100]
        self.assertEqual(list(pages(Paginated(), None, 'releases')), list(range(101)))

    def test_service_failures_and_non_lists_are_not_an_empty_history(self):
        class Broken:
            def api(self, *_):
                return {'message': 'service failure'}
        with self.assertRaises(ValueError):
            list(pages(Broken(), None, 'releases'))
