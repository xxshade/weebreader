"""Install the selected Firefox/Vivaldi Android APK from its official origin."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.browser_versions import require_version, USER_AGENT
from scripts.ci_report import report_exceptions

# Bootstrapped from the official stable APKs over authenticated vendor HTTPS.
SIGNERS = {
    'vivaldi': 'e8a78544655ba8c09817f732768f5689b1662ec4b2bc5a0bc0ec138d33ca3d1e',
    'firefox': 'a78b62a5165b4494b2fead9e76a280d22d937fee6251aece599446b2ea319b04',
}
PACKAGES = {'vivaldi': 'com.vivaldi.browser', 'firefox': 'org.mozilla.firefox'}


def adb(*args):
    result = subprocess.run(['adb', *args], text=True, capture_output=True, timeout=180)
    if result.returncode:
        message = 'ADB ' + ' '.join(args) + ': ' + (result.stdout + result.stderr)[-1800:]
        raise RuntimeError(message)
    return result.stdout.strip()


def installed_version(package):
    data = adb('shell', 'dumpsys', 'package', package)
    match = re.search(r'\bversionName=([^\s]+)', data)
    return match[1] if match else None


def download(url, path):
    if urlparse(url).scheme != 'https':
        raise ValueError('Browser downloads require HTTPS.')
    try:
        with urlopen(Request(url, headers={'User-Agent': USER_AGENT}), timeout=120) as response, path.open('wb') as stream:
            if urlparse(response.url).scheme != 'https':
                raise ValueError('Refusing a browser download redirected away from HTTPS.')
            while data := response.read(1024 * 1024):
                stream.write(data)
    except HTTPError as error:
        code = error.code
        error.close()
        raise RuntimeError(f'Browser download from {urlparse(url).netloc}: HTTP {code}.') from None


def android_tool(name):
    sdk = Path(os.environ.get('ANDROID_HOME') or os.environ['ANDROID_SDK_ROOT'])
    tool = sdk / 'build-tools/35.0.0' / name
    if not tool.is_file():
        raise ValueError(f'Android SDK build-tools35 with {name} are required.')
    return str(tool)


def signer(path):
    value = subprocess.check_output([android_tool('apksigner'), 'verify', '--print-certs', str(path)],
                                    text=True, timeout=60)
    digests = set(re.findall(r'certificate SHA-256 digest:\s*([0-9a-fA-F]{64})', value))
    if len(digests) != 1:
        raise ValueError('Expected exactly one verified APK signing identity.')
    return digests.pop().lower()


def chromium_version(path):
    # Vivaldi's branded product version differs from its Chromium version.
    # The signed native library contains the exact upstream version constant.
    with zipfile.ZipFile(path) as archive:
        data = archive.read('lib/x86_64/libchrome.so')
    versions = set(re.findall(rb'\b([0-9]{3,}\.[0-9]+\.[0-9]{4}\.[0-9]+)\x00', data))
    if len(versions) != 1:
        raise ValueError('Cannot unambiguously identify Vivaldi\'s signed Chromium version.')
    return versions.pop().decode('ascii')


def provision(browser, manifest, directory):
    directory.mkdir(parents=True, exist_ok=True)
    expected = manifest['versions'][browser + '_android']
    package = PACKAGES[browser]
    source = (f'https://archive.mozilla.org/pub/fenix/releases/{expected}/android/'
              f'fenix-{expected}-android-x86_64/fenix-{expected}.multi.android-x86_64.apk') if browser == 'firefox' else (
              f'https://downloads.vivaldi.com/stable/Vivaldi.{expected}_x86-64.apk')
    if browser == 'vivaldi' and manifest['downloads']['vivaldi_android'] != source:
        raise ValueError('Vivaldi APK URL differs from the selected official stable release.')
    path = directory / (browser + '.apk')
    download(source, path)
    fingerprint = signer(path)
    if fingerprint != SIGNERS[browser]:
        raise ValueError(f'{browser} APK signing identity changed; investigate before installing.')
    metadata = subprocess.check_output([android_tool('aapt'), 'dump', 'badging', str(path)], text=True, timeout=60)
    match = re.search(r"^package: name='([^']+)' versionCode='[^']+' versionName='([^']+)'", metadata)
    if not match or match[1] != package:
        raise ValueError('Downloaded APK package does not match the required browser.')
    require_version(match[2], expected)
    engine = chromium_version(path) if browser == 'vivaldi' else expected
    adb('install', '-r', str(path))
    require_version(installed_version(package), expected)
    activity = adb('shell', 'cmd', 'package', 'resolve-activity', '--brief', package).splitlines()[-1]
    if not activity.startswith(package + '/'):
        raise ValueError('Cannot resolve the installed browser launch activity.')
    # Vivaldi's native startup is initialized before attaching ChromeDriver.
    # Firefox startup belongs to its test profile and is cleared by that setup.
    if browser == 'vivaldi':
        adb('shell', 'am', 'start', '-W', '-n', activity,
            '-a', 'android.intent.action.VIEW', '-d', 'about:blank')
        adb('shell', 'am', 'force-stop', package)
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'browser': browser, 'package': package, 'version': expected, 'engine_version': engine,
            'activity': activity.split('/', 1)[1], 'source': source, 'signer_sha256': fingerprint,
            'apk_sha256': digest, 'android': adb('shell', 'getprop', 'ro.build.version.release'),
            'sdk': adb('shell', 'getprop', 'ro.build.version.sdk')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', choices=('vivaldi', 'firefox'), required=True)
    parser.add_argument('--versions', type=Path, required=True)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='android-browsers-') as temp:
        result = provision(args.browser, json.loads(args.versions.read_text()), args.directory or Path(temp))
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    report_exceptions()
    main()
