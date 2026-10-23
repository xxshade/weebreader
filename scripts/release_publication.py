"""A recoverable release publishes one immutable manifest under three aliases.

The draft Release is the durable journal. An occupied version without a matching
journal is a conflict; an interrupted matching publication is resumed, never
rebuilt. Registry operations copy bytes, including the complete OCI index.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scripts.release import Client, Release, preflight

MARKER = re.compile(r'<!-- ebookreader-publication\n(.*?)\n-->', re.S)
MEDIA = ', '.join(('application/vnd.oci.image.index.v1+json',
                   'application/vnd.docker.distribution.manifest.list.v2+json',
                   'application/vnd.oci.image.manifest.v1+json',
                   'application/vnd.docker.distribution.manifest.v2+json'))


def journal(item):
    matches = MARKER.findall(item.get('body') or '')
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError('Ambiguous publication journal.')
    state = json.loads(matches[0])
    if (state.get('format') != 1 or state.get('phase') not in ('prepared', 'built', 'announced', 'complete')
            or not isinstance(state.get('plan'), dict)):
        raise ValueError('Unsupported or invalid publication journal.')
    if (state['phase'] == 'prepared' and state.get('digest') is not None) or (
            state['phase'] != 'prepared' and not re.fullmatch(r'sha256:[0-9a-f]{64}', state.get('digest') or '')):
        raise ValueError('Publication phase does not match its recorded image digest.')
    return state


def completed_release(item):
    state = journal(item)
    return not item['draft'] and not item['prerelease'] and (state is None or state['phase'] == 'complete')


class PublicationClient(Client):
    def update(self, release, item, data):
        _, result = self.request(f'{self.github}/repos/{release.repository}/releases/{item["id"]}',
                                 method='PATCH', data=data,
                                 headers={'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github+json'})
        return result

    def registry_bytes(self, release, path, *, data=None, media=None, missing_ok=False):
        credentials = base64.b64encode(f'{self.actor}:{self.token}'.encode()).decode()
        _, access = self.request(self.registry + '/token?' + urlencode({
            'service': 'ghcr.io', 'scope': f'repository:{release.repository.lower()}:pull' + (',push' if data is not None else '')}),
            headers={'Authorization': f'Basic {credentials}'})
        bearer = access.get('token') or access.get('access_token')
        if not bearer:
            raise ValueError('Registry did not return an access token.')
        request = Request(f'{self.registry}/v2/{release.repository.lower()}/{path}',
                          data=data, method='PUT' if data is not None else 'GET',
                          headers={'Authorization': f'Bearer {bearer}', 'Accept': MEDIA,
                                   **({'Content-Type': media} if media else {})})
        try:
            with urlopen(request, timeout=60) as response:
                return response.read(), response.headers
        except HTTPError as error:
            error.close()
            if missing_ok and error.code == 404:
                return None
            raise RuntimeError(f'Registry {request.method} {path}: HTTP {error.code}') from None

    def image(self, release, reference, missing_ok=False):
        result = self.registry_bytes(release, 'manifests/' + reference, missing_ok=missing_ok)
        if result is None:
            return None
        raw, headers = result
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        if headers.get('Docker-Content-Digest') != digest:
            raise ValueError('Registry manifest bytes do not match their declared digest.')
        return digest, raw, json.loads(raw).get('mediaType') or headers['Content-Type'].split(';')[0]


def releases(client, release):
    from scripts.auto_release import pages
    return list(pages(client, release, 'releases'))


def find_release(client, release):
    # The tag endpoint can hide drafts. Listing with repository push access is
    # also how recovery finds a reserved draft whose Git tag does not exist yet.
    matches = [item for item in releases(client, release) if item['tag_name'] == release.tag]
    if len(matches) > 1:
        raise ValueError('Multiple Releases occupy the reserved version.')
    return matches[0] if matches else None


def write_journal(client, release, item, state, *, draft=None):
    body = MARKER.sub('', item.get('body') or '').rstrip()
    body += '\n\n<!-- ebookreader-publication\n' + json.dumps(state, sort_keys=True) + '\n-->'
    # Without tag_name, GitHub replaces a draft's reserved tag with untagged-*
    # when the Git ref has not been created yet. Preserve it on every update.
    return client.update(release, item, {'body': body, 'tag_name': release.tag,
        **({'draft': draft, 'make_latest': 'false'} if draft is not None else {}),
        **({'make_latest': 'true'} if state['phase'] == 'complete' else {})})


def matching_state(item, plan):
    if item is None:
        raise ValueError('Publication reservation is missing; inspect the draft Release before retrying. '
                         'The existing numbered image must not be rebuilt.')
    state = journal(item)
    if (not state or state['plan'] != plan or item['tag_name'] != 'v' + plan['version']
            or item.get('target_commitish') != plan['commit'] or item.get('prerelease')):
        raise ValueError('Occupied version does not match the verified publication plan.')
    return state


def verify_identity(client, release, image):
    """Also protects discovery after a crash between registry push and journal write."""
    manifest = json.loads(image[1])
    if 'manifests' in manifest:
        children = [m for m in manifest['manifests'] if m.get('platform') == {'architecture': 'amd64', 'os': 'linux'}]
        if len(children) != 1:
            raise ValueError('Expected one linux/amd64 application image in the index.')
        child = client.image(release, children[0]['digest'])
        if child[0] != children[0]['digest']:
            raise ValueError('Platform manifest digest mismatch.')
        manifest = json.loads(child[1])
    raw, _ = client.registry_bytes(release, 'blobs/' + manifest['config']['digest'])
    if 'sha256:' + hashlib.sha256(raw).hexdigest() != manifest['config']['digest']:
        raise ValueError('Image configuration digest mismatch.')
    labels = json.loads(raw).get('config', {}).get('Labels', {})
    expected = {'org.opencontainers.image.source': f'https://github.com/{release.repository}',
                'org.opencontainers.image.revision': release.commit,
                'org.opencontainers.image.version': release.version}
    if any(labels.get(k) != v for k, v in expected.items()):
        raise ValueError('Existing image source, commit or version does not match the reserved publication.')


def stage(release, client, plan):
    item = find_release(client, release)
    if item is None:
        preflight(release, client)
        item = client.api(release, 'releases', data={
            'tag_name': release.tag, 'target_commitish': release.commit,
            'name': release.tag, 'draft': True, 'prerelease': False,
            'body': '<!-- ebookreader-publication\n' + json.dumps({
                'format': 1, 'phase': 'prepared', 'plan': plan, 'digest': None}) + '\n-->'})
    state = matching_state(item, plan)
    image = client.image(release, release.version, missing_ok=True)
    if image:
        verify_identity(client, release, image)
        if state['digest'] and state['digest'] != image[0]:
            raise ValueError('The numbered image changed after it was recorded; refusing recovery.')
        if state['phase'] == 'prepared':
            state.update(phase='built', digest=image[0])
            write_journal(client, release, item, state)
    elif state['phase'] != 'prepared':
        raise ValueError('The recorded release image disappeared; rebuilding is forbidden.')
    elif not item['draft'] or client.api(release, f'git/ref/tags/{release.tag}', missing_ok=True):
        raise ValueError('An unbuilt reservation has an unexpected published release or Git tag.')
    return {'build': image is None, 'digest': image[0] if image else ''}


def refuse_backward_promotion(release, client):
    version = tuple(map(int, release.version.split('.')))
    for item in releases(client, release):
        tag = item['tag_name']
        if completed_release(item) and re.fullmatch(r'v[1-9][0-9]*\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag):
            if tuple(map(int, tag[1:].split('.'))) > version:
                raise ValueError('A newer stable release exists; latest must not move backwards.')


def copy_alias(client, release, name, image, *, immutable=False):
    old = client.image(release, name, missing_ok=True)
    if old and old[0] == image[0]:
        return
    if old and immutable:
        raise ValueError(f'Commit alias {name} already identifies a different image.')
    client.registry_bytes(release, 'manifests/' + name, data=image[1], media=image[2])
    if client.image(release, name)[0] != image[0]:
        raise ValueError(f'Alias {name} does not match the numbered image digest.')


def finish(release, client, plan, expected_digest=None):
    stage(release, client, plan)
    item = find_release(client, release)
    state = matching_state(item, plan)
    image = client.image(release, release.version)
    if not image or state['digest'] != image[0]:
        raise ValueError('A verified, recorded image is required before publication.')
    if expected_digest is not None and image[0] != expected_digest:
        raise ValueError('Registry image differs from the successful build or recovery digest.')
    refuse_backward_promotion(release, client)
    tag = client.api(release, f'git/ref/tags/{release.tag}', missing_ok=True)
    if tag is None:
        client.api(release, 'git/refs', data={'ref': 'refs/tags/' + release.tag, 'sha': release.commit})
    elif tag['object']['sha'] != release.commit:
        raise ValueError('The release tag points to another commit.')
    copy_alias(client, release, release.commit, image, immutable=True)
    if state['phase'] not in ('announced', 'complete'):
        notes = client.api(release, 'releases/generate-notes', data={
            'tag_name': release.tag, 'target_commitish': release.commit,
            **({'previous_tag_name': plan['previous_tag']} if plan['previous_tag'] else {})})
        item['body'] = f'''## Container image

`{release.image}:{release.version}`  
Exact digest: `{release.image}@{image[0]}`  
Source commit: `{release.commit}`  
Platform: `linux/amd64`

The numbered version, full commit alias and `latest` identify the same image
when publication completes. Numbered versions are never overwritten.

## Upgrade notes

{plan['migration_notes']}

Back up persistent data before upgrading. Downgrading can require restoring
the matching data backup. See the README for instructions.

{notes['body']}
'''
        state['phase'] = 'announced'
        item = write_journal(client, release, item, state, draft=False)
    copy_alias(client, release, 'latest', image)
    if any(client.image(release, name)[0] != image[0] for name in (release.version, release.commit, 'latest')):
        raise ValueError('Publication incomplete: image aliases differ.')
    state['phase'] = 'complete'
    item = write_journal(client, release, item, state)
    return item


def select_plan(client, repo):
    from scripts.auto_release import History, publication_plan
    pending = [item for item in releases(client, repo) if journal(item) and journal(item)['phase'] != 'complete']
    if len(pending) > 1:
        raise ValueError('Multiple unfinished publications require investigation.')
    if pending:
        item = pending[0]
        saved = journal(item)['plan']
        matching_state(item, saved)
        if not History().ancestor(saved['commit'], repo.commit):
            raise ValueError('Pending release commit is not in main history.')
        if (os.environ.get('MANUAL_VERSION') or os.environ.get('MANUAL_NOTES')):
            raise ValueError('Recover the pending publication with empty manual inputs before planning another version.')
        return saved
    return publication_plan(client, repo, History(), os.environ.get('MANUAL_VERSION', ''),
                            os.environ.get('MANUAL_NOTES', ''),
                            initial=os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch')


def reconcile(client, repo):
    """Adopt stable semantics on tooling/docs-only merges, including rollout."""
    from scripts.auto_release import History, VERSION, plan, stable_releases
    if plan(client, repo, History()) is not None:
        raise ValueError('Unreleased application changes require the publication path.')
    stable = stable_releases(client, repo)
    if not stable:
        print('Awaiting the initial manual release; no stable image to reconcile.')
        return
    item = max(stable, key=lambda r: tuple(map(int, VERSION.fullmatch(r['tag_name']).groups())))
    commit = History().git('rev-parse', item['tag_name'] + '^{commit}')
    release = Release(item['tag_name'][1:], repo.repository, commit, repo.ref)
    image = client.image(release, release.version)
    verify_identity(client, release, image)
    state = journal(item)
    if state and state['digest'] != image[0]:
        raise ValueError('Stable image no longer matches its completed publication.')
    if not state and f'{release.image}@{image[0]}' not in item.get('body', ''):
        raise ValueError('Legacy image digest is not recorded in the stable release notes.')
    refuse_backward_promotion(release, client)
    copy_alias(client, release, commit, image, immutable=True)
    copy_alias(client, release, 'latest', image)
    print(f'Stable {release.version}, latest and {commit} identify {image[0]}.')


def queue_pending(client, repo):
    """Recovery may consume the only event for a newer, still-unreleased merge."""
    head = client.api(repo, 'commits/main')['sha']
    if head == repo.commit:
        return False
    if not re.fullmatch(r'[0-9a-f]{40}', head):
        raise ValueError('GitHub returned an invalid main commit.')
    client.api(repo, 'actions/workflows/release.yml/dispatches', data={'ref': 'main'})
    return True


def main():
    command = sys.argv[1]
    client = PublicationClient(os.environ['GH_TOKEN'], os.environ['GITHUB_ACTOR'])
    path = Path(os.environ['RELEASE_PLAN'])
    repo = Release('1.0.0', os.environ['GITHUB_REPOSITORY'], os.environ['RELEASE_COMMIT'], os.environ['GITHUB_REF'])
    output = {}
    if command == 'advance':
        print('Queued remaining main changes.' if queue_pending(client, repo) else 'No newer main commit to publish.')
    elif command == 'reconcile':
        reconcile(client, repo)
    elif command == 'plan':
        from scripts.auto_release import stable_releases
        value = select_plan(client, repo)
        path.write_text(json.dumps(value, indent=2) + '\n')
        reconcile_ready = value is None and bool(stable_releases(client, repo))
        output = {'eligible': str(value is not None).lower(), 'commit': value['commit'] if value else repo.commit,
                  'version': value['version'] if value else '', 'image': repo.image,
                  'reconcile': str(reconcile_ready).lower()}
        if value is None and not reconcile_ready:
            message = ('Awaiting the initial manual release. Run Release on main with version 8.3.6 '
                       'and explicit migration_notes (or "No manual steps required"). No image was built or published.')
            print(message)
            if os.environ.get('GITHUB_STEP_SUMMARY'):
                with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
                    summary.write(message + '\n')
    else:
        value = json.loads(path.read_text())
        from scripts.auto_release import History
        if repo.commit != value['commit'] or History().git('rev-parse', 'HEAD') != value['commit']:
            raise ValueError('Publication checkout differs from the exact planned source commit.')
        repo = Release(value['version'], repo.repository, value['commit'], repo.ref)
        if command == 'stage':
            # Re-check the complete reviewed plan, also during recovery.
            from scripts.auto_release import publication_plan
            current = publication_plan(client, repo, History(), value['manual_version'], value['manual_notes'],
                                       initial=not value['previous_tag'])
            if current != value:
                raise ValueError('Release plan or PR metadata changed after validation.')
            result = stage(repo, client, value)
            output = {**result, 'build': str(result['build']).lower()}
        elif command == 'finish':
            item = finish(repo, client, value, os.environ['IMAGE_DIGEST'])
            print(item['html_url'])
        else:
            raise ValueError('Expected plan, stage or finish.')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.writelines(f'{key}={value}\n' for key, value in output.items())


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, KeyError, subprocess.CalledProcessError) as error:
        from scripts.ci_report import failure
        failure(str(error))
        sys.exit(str(error))
