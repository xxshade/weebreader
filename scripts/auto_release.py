"""Plan releases from reviewed PR metadata and the exact, unreleased main history.

Read-only GitHub calls and local Git inspection; publication stays in release.py.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlencode

from scripts.release import Client, Release

VERSION = re.compile(r'v([1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')
IMPACT = {'release:patch': 0, 'release:minor': 1, 'release:major': 2}
RUNTIME_FILES = {'requirements.txt', 'Dockerfile', '.dockerignore', 'entrypoint.sh',
                 'docker-compose.yml', 'docker-compose.dev.yml', 'compose.yml',
                 'compose.yaml', '.env.example'}


def eligible(path):
    return path.startswith('app/') or path in RUNTIME_FILES or path.startswith('docker-compose.')


def pages(client, repo, path):
    for page in range(1, 10001):
        separator = '&' if '?' in path else '?'
        batch = client.api(repo, path + separator + urlencode({'per_page': 100, 'page': page}))
        if not isinstance(batch, list):
            raise ValueError(f'Expected a list from {path}.')
        yield from batch
        if len(batch) < 100:
            return
    raise ValueError(f'Pagination limit exceeded for {path}.')


def upgrade_notes(body):
    # Ignore template comments, including the example text inside them.
    body = re.sub(r'<!--.*?-->', '', body or '', flags=re.S)
    sections = re.findall(r'^##[ \t]+Upgrade notes[ \t]*\r?\n(.*?)(?=^#{1,2}[ \t]|\Z)',
                          body, flags=re.M | re.S | re.I)
    if len(sections) != 1 or not sections[0].strip():
        raise ValueError('Provide one nonempty "## Upgrade notes" section (or "No manual steps required").')
    return sections[0].strip()


def pr_policy(pr):
    labels = {label['name'] for label in pr.get('labels', []) if label['name'].startswith('release:')}
    if len(labels) != 1 or not labels <= IMPACT.keys():
        raise ValueError(f'PR #{pr["number"]} needs exactly one release:patch, release:minor or release:major label.')
    return IMPACT[next(iter(labels))], upgrade_notes(pr.get('body'))


def check_pr(client, repo, number):
    pr = client.api(repo, f'pulls/{number}')
    files = list(pages(client, repo, f'pulls/{number}/files'))
    if len(files) != pr['changed_files']:
        raise ValueError('GitHub did not return the complete PR file list; refusing to guess eligibility.')
    if not any(eligible(f['filename']) or eligible(f.get('previous_filename', '')) for f in files):
        return False
    pr_policy(pr)
    return True


class History:
    def __init__(self, directory='.'):
        self.directory = directory

    def git(self, *args):
        result = subprocess.run(['git', *args], cwd=self.directory, check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.stdout.decode().strip()

    def ancestor(self, older, newer):
        status = subprocess.run(['git', 'merge-base', '--is-ancestor', older, newer],
                                cwd=self.directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if status.returncode not in (0, 1):
            raise ValueError('Could not verify release ancestry; fetch the complete main history and tags.')
        return status.returncode == 0

    def commits(self, baseline, target):
        return self.git('rev-list', '--first-parent', '--reverse', f'{baseline}..{target}').splitlines()

    def files(self, commit):
        return self.git('diff', '--name-only', '--no-renames', '-z', commit + '^1', commit).split('\0')


@dataclass(frozen=True)
class Plan:
    version: str
    commit: str
    previous_tag: str
    prs: tuple
    migration_notes: str

    def as_dict(self):
        return {**self.__dict__, 'prs': list(self.prs)}


def stable_releases(client, repo):
    from scripts.release_publication import completed_release
    return [r for r in pages(client, repo, 'releases')
            if completed_release(r) and VERSION.fullmatch(r['tag_name'])]


def plan(client, repo, history, *, initial_version='', initial_notes=''):
    releases = stable_releases(client, repo)
    if not releases:
        if not initial_version and not initial_notes:
            return None
        Release(initial_version, repo.repository, repo.commit, repo.ref)
        notes = re.sub(r'<!--.*?-->', '', initial_notes, flags=re.S).strip()
        if not notes:
            raise ValueError('The initial release requires explicit upgrade notes (or "No manual steps required").')
        return Plan(initial_version, repo.commit, '', (), notes)
    previous = max(releases, key=lambda r: tuple(map(int, VERSION.fullmatch(r['tag_name']).groups())))
    tag = previous['tag_name']
    baseline = history.git('rev-parse', f'{tag}^{{commit}}')
    if not history.ancestor(baseline, repo.commit):
        raise ValueError('The last stable release is not an ancestor of the selected main commit.')
    commits = history.commits(baseline, repo.commit)
    pending = set(commits)
    included = {}
    for commit in commits:
        if not any(eligible(path) for path in history.files(commit)):
            continue
        candidates = []
        for item in pages(client, repo, f'commits/{commit}/pulls'):
            number = item['number']
            pr = included.get(number) or client.api(repo, f'pulls/{number}')
            if (pr.get('merged_at') and pr['base']['ref'] == 'main'
                    and pr['base']['repo']['full_name'].lower() == repo.repository.lower()
                    and pr.get('merge_commit_sha') in pending):
                candidates.append(pr)
        if len(candidates) != 1:
            raise ValueError(f'Commit {commit} must belong to exactly one merged main PR; direct runtime pushes cannot be released automatically.')
        included[candidates[0]['number']] = candidates[0]
    if not included:
        return None
    impacts, notes = [], []
    for number, pr in included.items():
        impact, note = pr_policy(pr)
        impacts.append(impact)
        notes.append(f'### PR #{number}\n\n{note}')
    major, minor, patch = map(int, VERSION.fullmatch(tag).groups())
    impact = max(impacts)
    version = f'{major + 1}.0.0' if impact == 2 else f'{major}.{minor + 1}.0' if impact == 1 else f'{major}.{minor}.{patch + 1}'
    return Plan(version, repo.commit, tag, tuple(included), '\n\n'.join(notes))


def serialized(result, manual_version='', manual_notes=''):
    if result is None:
        if manual_version or manual_notes:
            raise ValueError('No eligible changes to release; a manual override cannot bypass PR policy.')
        return None
    value = result.as_dict()
    if manual_version:
        match = VERSION.fullmatch('v' + manual_version)
        if not match or tuple(map(int, match.groups())) < tuple(map(int, result.version.split('.'))):
            raise ValueError('A manual override must be a stable version at least as high as the planned version.')
        value['version'] = manual_version
    if manual_notes.strip() and result.previous_tag:
        value['migration_notes'] += '\n\n### Additional maintainer instructions\n\n' + manual_notes.strip()
    return {**value, 'manual_version': manual_version, 'manual_notes': manual_notes}


def publication_plan(client, repo, history, manual_version='', manual_notes='', *, initial=False):
    """Only an explicit manual request or its saved journal can start history."""
    result = plan(client, repo, history,
                  initial_version=manual_version if initial else '',
                  initial_notes=manual_notes if initial else '')
    return serialized(result, manual_version, manual_notes)


def main():
    command = sys.argv[1]
    repo = Release('1.0.0', os.environ['GITHUB_REPOSITORY'],
                   os.environ.get('RELEASE_COMMIT') or os.environ['GITHUB_SHA'],
                   'refs/heads/main' if command == 'check-pr' else os.environ['GITHUB_REF'])
    client = Client(os.environ['GH_TOKEN'], os.environ['GITHUB_ACTOR'])
    if command == 'check-pr':
        result = check_pr(client, repo, int(os.environ['PR_NUMBER']))
        print('Release metadata is valid.' if result else 'No application changes: no numbered release required.')
    elif command in ('plan', 'verify'):
        result = plan(client, repo, History())
        path = Path(os.environ.get('RELEASE_PLAN', 'release-plan.json'))
        if command == 'verify':
            saved = json.loads(path.read_text())
            value = serialized(result, saved['manual_version'], saved['manual_notes'])
            if value != saved:
                raise ValueError('Release history or PR metadata changed after validation. Retry with a new plan.')
        else:
            value = serialized(result, os.environ.get('MANUAL_VERSION', ''), os.environ.get('MANUAL_NOTES', ''))
            path.write_text(json.dumps(value, indent=2) + '\n')
            outputs = {'eligible': str(bool(result)).lower(), 'commit': repo.commit,
                       'version': value['version'] if value else ''}
            if os.environ.get('GITHUB_OUTPUT'):
                with open(os.environ['GITHUB_OUTPUT'], 'a') as target:
                    target.writelines(f'{key}={value}\n' for key, value in outputs.items())
            print(f'Release plan: {json.dumps(value)}')
    else:
        raise ValueError('Expected plan, verify or check-pr.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, KeyError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
