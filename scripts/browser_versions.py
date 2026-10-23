"""Resolve stable desktop targets once; Android targets are opt-in."""
import argparse
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SOURCES = {
    'chrome': 'https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json',
    'firefox': 'https://product-details.mozilla.org/1.0/firefox_versions.json',
    'firefox_android': 'https://product-details.mozilla.org/1.0/mobile_versions.json',
    'vivaldi_android': 'https://vivaldi.com/download/',
}
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/155.0.0.0 Safari/537.36'


def resolve(sources=None, *, include_android=False):
    sources = {name: url for name, url in (SOURCES if sources is None else sources).items()
               if include_android or name in ('chrome', 'firefox')}
    data = {}
    for name, url in sources.items():
        try:
            request = Request(url, headers={'User-Agent': USER_AGENT, 'Accept': '*/*'})
            with urlopen(request, timeout=45) as response:
                data[name] = response.read().decode('utf-8') if name == 'vivaldi_android' else json.load(response)
        except HTTPError as error:
            code = error.code
            error.close()
            raise RuntimeError(f'Cannot resolve current stable {name}: HTTP {code}.') from None
    values = {
        'chrome': data['chrome']['channels']['Stable']['version'],
        'firefox': data['firefox']['LATEST_FIREFOX_VERSION'],
    }
    downloads = {}
    if include_android:
        apks = re.findall(r'https://downloads\.vivaldi\.com/stable/Vivaldi\.([0-9]+(?:\.[0-9]+){3})_x86-64\.apk', data['vivaldi_android'])
        if not apks:
            raise ValueError('The official Vivaldi download page has no stable Android x86-64 APK.')
        vivaldi = max(apks, key=lambda v: tuple(map(int, v.split('.'))))
        values.update(firefox_android=data['firefox_android']['version'], vivaldi_android=vivaldi)
        downloads['vivaldi_android'] = f'https://downloads.vivaldi.com/stable/Vivaldi.{vivaldi}_x86-64.apk'
    if any(not isinstance(v, str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}', v) for v in values.values()):
        raise ValueError('Stable browser metadata is missing or invalid; old-version fallbacks are forbidden.')
    return {'versions': values, 'sources': sources,
            'downloads': downloads}


def require_version(actual, expected):
    if actual != expected:
        raise ValueError(f'Browser version {actual!r} differs from selected stable version {expected!r}.')


if __name__ == '__main__':
    from ci_report import report_exceptions
    report_exceptions()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--android', action='store_true', help='Also select optional native Android targets')
    args = parser.parse_args()
    value = resolve(include_android=args.android)
    args.output.write_text(json.dumps(value, indent=2) + '\n')
    print(json.dumps(value['versions'], indent=2))
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        print('::notice title=Selected stable browser versions::' + json.dumps(value['versions']))
