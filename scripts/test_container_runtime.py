"""Exercise the built container's real entrypoint and browser without networking."""
import argparse
import subprocess
import time
import uuid


def run(image):
    name = 'weebreader-smoke-' + uuid.uuid4().hex[:12]
    try:
        subprocess.run(['docker', 'run', '--detach', '--name', name, '--network', 'none', image], check=True)
        for attempt in range(30):
            probe = subprocess.run(['docker', 'exec', name, 'python', '-c',
                "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/setup',timeout=2); assert r.status==200"],
                capture_output=True, text=True)
            if probe.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('Container did not serve its initial setup page: ' + probe.stderr)
        subprocess.run(['docker', 'exec', '--user', '1000:1000', name, 'python', '-c', '''
import os, pathlib, tempfile
from lxml import etree
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
assert etree.LIBXML_VERSION >= (2,11,0), etree.LIBXML_VERSION
with tempfile.TemporaryDirectory(dir='/app/data') as directory:
    pathlib.Path(directory,'writable').write_text('ok')
with tempfile.TemporaryDirectory(dir='/app/data') as profile:
    options=Options()
    options.binary_location=os.environ['APP_NOVEL_CHROMIUM_BINARY_PATH']
    for flag in ('--headless=new','--no-sandbox','--disable-dev-shm-usage','--user-data-dir='+profile):
        options.add_argument(flag)
    with webdriver.Chrome(options=options,service=Service(os.environ['APP_NOVEL_CHROMEDRIVER_PATH'])) as browser:
        browser.get('data:text/html,<title>ready</title><script>document.title="browser-ok"</script>')
        assert browser.title=='browser-ok', browser.title
print('Container startup, non-root writable data, lxml and real Chromium: passed')
'''], check=True)
    finally:
        subprocess.run(['docker', 'logs', name], check=False)
        subprocess.run(['docker', 'rm', '--force', name], check=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    run(parser.parse_args().image)
