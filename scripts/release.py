"""Release API boundary: fail closed on reused versions and registry/API errors.

The workflow owns tests and the Docker build. This script checks whether a
version is unused, then announces the successfully pushed digest on GitHub.
"""
import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Release:
    version: str
    repository: str
    commit: str
    ref: str

    def __post_init__(self):
        if not re.fullmatch(r'[1-9][0-9]*\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', self.version):
            raise ValueError('Use a stable version such as 1.0.0, without a v prefix or leading zeros.')
        if self.ref != 'refs/heads/main':
            raise ValueError('Stable releases must target main.')
        if not re.fullmatch(r'[0-9a-f]{40}', self.commit):
            raise ValueError('The release must name an exact commit.')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', self.repository):
            raise ValueError('Invalid repository.')

    @property
    def tag(self):
        return f'v{self.version}'

    @property
    def image(self):
        return f'ghcr.io/{self.repository.lower()}'


class Client:
    def __init__(self, token, actor, *, github='https://api.github.com', registry='https://ghcr.io'):
        self.token, self.actor = token, actor
        self.github, self.registry = github, registry

    def request(self, url, *, method='GET', headers=None, data=None, missing_ok=False):
        body = json.dumps(data).encode() if data is not None else None
        request = Request(url, data=body, method=method,
                          headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with urlopen(request, timeout=30) as response:
                content = response.read()
                return response.headers, json.loads(content) if content else None
        except HTTPError as error:
            error.close()
            if error.code == 404 and missing_ok:
                return None
            raise RuntimeError(f'{method} {request.host}{request.selector.split("?")[0]} failed: HTTP {error.code}') from None

    def api(self, release, path, *, data=None, missing_ok=False):
        result = self.request(f'{self.github}/repos/{release.repository}/{path}',
                              method='POST' if data is not None else 'GET', data=data,
                              missing_ok=missing_ok,
                              headers={'Authorization': f'Bearer {self.token}',
                                       'Accept': 'application/vnd.github+json',
                                       'X-GitHub-Api-Version': '2022-11-28'})
        return result[1] if result else None

    def manifest(self, release):
        package = release.repository.lower()
        credentials = base64.b64encode(f'{self.actor}:{self.token}'.encode()).decode()
        _, access = self.request(f'{self.registry}/token?' + urlencode({
            'service': 'ghcr.io', 'scope': f'repository:{package}:pull'}),
            headers={'Authorization': f'Basic {credentials}'})
        bearer = access.get('token') or access.get('access_token')
        if not bearer:
            raise RuntimeError('Registry did not return an access token.')
        result = self.request(f'{self.registry}/v2/{package}/manifests/{release.version}',
                              method='HEAD', missing_ok=True,
                              headers={'Authorization': f'Bearer {bearer}',
                                       'Accept': 'application/vnd.oci.image.index.v1+json, '
                                                 'application/vnd.docker.distribution.manifest.list.v2+json, '
                                                 'application/vnd.oci.image.manifest.v1+json, '
                                                 'application/vnd.docker.distribution.manifest.v2+json'})
        if result is None:
            return None
        digest = result[0].get('Docker-Content-Digest')
        if not digest:
            raise RuntimeError('Registry manifest has no digest; refusing to guess.')
        return digest


def preflight(release, client):
    if client.api(release, f'git/ref/tags/{release.tag}', missing_ok=True) is not None:
        raise ValueError(f'Tag {release.tag} already exists. Choose a new version.')
    if client.api(release, f'releases/tags/{release.tag}', missing_ok=True) is not None:
        raise ValueError(f'Release {release.tag} already exists. Choose a new version.')
    if client.manifest(release) is not None:
        raise ValueError(f'Image {release.image}:{release.version} already exists. Choose a new version.')
    page = 1
    while True:
        published = client.api(release, f'releases?per_page=100&page={page}')
        for item in published:
            tag = item['tag_name']
            if (not item['draft'] and not item['prerelease'] and tag.startswith('v')
                    and re.fullmatch(r'[1-9][0-9]*\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag[1:])
                    and tuple(map(int, tag[1:].split('.'))) >= tuple(map(int, release.version.split('.')))):
                raise ValueError(f'The version must be higher than published release {tag}.')
        if len(published) < 100:
            break
        page += 1


def publish(release, client, digest, migration_notes='', previous_tag=None):
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('The build must supply a sha256 image digest.')
    if client.manifest(release) != digest:
        raise ValueError('The published manifest does not match the successful build digest.')
    notes = client.api(release, 'releases/generate-notes', data={
        'tag_name': release.tag, 'target_commitish': release.commit,
        **({'previous_tag_name': previous_tag} if previous_tag else {})})
    body = f'''## Container image

```yaml
image: {release.image}:{release.version}
```

Exact digest: `{release.image}@{digest}`  
Source commit: `{release.commit}`  
Platform: `linux/amd64`

Numbered image tags are immutable in the release workflow. `latest` follows
`main` and may contain changes newer than this release.

## Upgrade notes

{migration_notes.strip() or 'No additional migration notes were supplied for this release.'}

Back up the complete persistent data directory before upgrading. An older
image may not understand a migrated database: a rollback requires restoring
the matching backup as well as the previous image. See the README for steps.

{notes['body']}
'''
    # Reserving the Git tag is also an atomic guard against another publisher.
    # A partial failure never justifies rebuilding/overwriting the image.
    client.api(release, 'git/refs', data={
        'ref': f'refs/tags/{release.tag}', 'sha': release.commit})
    return client.api(release, 'releases', data={
        'tag_name': release.tag, 'target_commitish': release.commit,
        'name': release.tag, 'body': body, 'draft': False,
        'prerelease': False, 'make_latest': 'true'})


def main():
    plan = json.loads(Path(os.environ['RELEASE_PLAN']).read_text()) if os.environ.get('RELEASE_PLAN') else None
    commit = os.environ.get('RELEASE_COMMIT') or os.environ['GITHUB_SHA']
    if plan and plan['commit'] != commit:
        raise ValueError('The release plan does not match the checked commit.')
    release = Release(plan['version'] if plan else os.environ['RELEASE_VERSION'], os.environ['GITHUB_REPOSITORY'],
                      commit, os.environ['GITHUB_REF'])
    client = Client(os.environ['GH_TOKEN'], os.environ['GITHUB_ACTOR'])
    command = sys.argv[1]
    if command == 'preflight':
        preflight(release, client)
        output = f'version={release.version}\ntag={release.tag}\nimage={release.image}\ncommit={release.commit}\n'
        if os.environ.get('GITHUB_OUTPUT'):
            with Path(os.environ['GITHUB_OUTPUT']).open('a') as target:
                target.write(output)
        print(f'Version {release.version} is available at commit {release.commit}.')
    elif command == 'publish':
        result = publish(release, client, os.environ['IMAGE_DIGEST'],
                         plan['migration_notes'] if plan else os.environ.get('MIGRATION_NOTES', ''),
                         plan['previous_tag'] if plan else None)
        print(result['html_url'])
    else:
        raise ValueError('Expected preflight or publish.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, KeyError) as error:
        sys.exit(str(error))
