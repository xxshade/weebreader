"""Provision drivers/browsers for the versions selected at pipeline start."""
import argparse

from browser_checks import add_browser_argument, selected_browsers
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
from urllib.request import urlopen
import zipfile

from selenium.webdriver.common.selenium_manager import SeleniumManager
from browser_versions import require_version


def get(url):
    with urlopen(url, timeout=120) as response:
        return response.read()


if __name__ == '__main__':
    from ci_report import report_exceptions
    report_exceptions()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--versions', type=Path, required=True)
    parser.add_argument('--env', type=Path, required=True)
    targets = parser.add_mutually_exclusive_group()
    add_browser_argument(targets)
    targets.add_argument('--android', choices=('vivaldi', 'firefox'))
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--directory', type=Path, default=Path('.browser-drivers'))
    args = parser.parse_args()
    versions = json.loads(args.versions.read_text())['versions']
    values = {}
    if args.android:
        args.directory.mkdir(parents=True, exist_ok=True)
        if args.android == 'firefox':
            release = json.loads(get('https://api.github.com/repos/mozilla/geckodriver/releases/latest'))
            asset = next(a for a in release['assets'] if a['name'].endswith('-linux64.tar.gz'))
            data = get(asset['browser_download_url'])
            if asset.get('digest') and asset['digest'] != 'sha256:' + hashlib.sha256(data).hexdigest():
                raise ValueError('Geckodriver checksum mismatch.')
            with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
                (args.directory / 'geckodriver').write_bytes(archive.extractfile('geckodriver').read())
            driver = args.directory / 'geckodriver'
        else:
            if args.evidence is None:
                parser.error('Vivaldi WebDriver requires the verified APK installation evidence.')
            evidence = json.loads(args.evidence.read_text())
            require_version(evidence['version'], versions['vivaldi_android'])
            build = '.'.join(evidence['engine_version'].split('.')[:3])
            metadata = json.loads(get('https://googlechromelabs.github.io/chrome-for-testing/latest-patch-versions-per-build-with-downloads.json'))
            asset = next(a for a in metadata['builds'][build]['downloads']['chromedriver'] if a['platform'] == 'linux64')
            with zipfile.ZipFile(io.BytesIO(get(asset['url']))) as archive:
                (args.directory / 'chromedriver').write_bytes(archive.read('chromedriver-linux64/chromedriver'))
            driver = args.directory / 'chromedriver'
        driver.chmod(0o755)
        values[args.android.upper() + '_DRIVER'] = str(driver.resolve())
    else:
        for browser in selected_browsers(args):
            paths = SeleniumManager().binary_paths([
                '--browser', browser, '--browser-version', versions[browser], '--force-browser-download'])
            actual = subprocess.check_output([paths['browser_path'], '--version'], text=True)
            match = re.search(r'\d+(?:\.\d+)+', actual)
            require_version(match[0] if match else None, versions[browser])
            values[browser.upper() + '_BINARY'] = paths['browser_path']
            values[browser.upper() + '_DRIVER'] = paths['driver_path']
    with args.env.open('a') as stream:
        stream.writelines(f'{key}={value}\n' for key, value in values.items())
