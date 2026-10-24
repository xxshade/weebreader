"""Run the existing validation suites and preserve their failure status."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


BROWSER_SUITES = ('resync', 'audit', 'cache', 'header', 'home_unread',
                  'pagination', 'reading_statistics', 'home_preload',
                  'lazy_covers', 'tracking', 'comic_keyboard')


def verify_checkout(commit):
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    if not re.fullmatch(r'[0-9a-f]{40}', commit) or actual != commit:
        raise ValueError('Validation checkout differs from the exact requested source commit.')
    return actual


def require_success(results, *, build_image):
    expected = {'backend', 'versions', 'browsers', 'docker'}
    if set(results) != expected:
        raise ValueError('Missing or unexpected validation jobs.')
    failures = {name: status for name, status in results.items()
                if status != ('skipped' if name == 'docker' and not build_image else 'success')}
    if failures:
        raise ValueError('Validation incomplete: ' + json.dumps(failures, sort_keys=True))


def run_commands(commands, log_dir):
    if not commands:
        raise ValueError('No validation commands were selected.')
    log_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for label, command in commands:
        started = time.monotonic()
        print(f'Running {label}', flush=True)
        with (log_dir / (label + '.log')).open('w') as log:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, errors='replace')
            try:
                for line in process.stdout:
                    print(line, end='', flush=True)
                    log.write(line)
            finally:
                process.stdout.close()
            status = process.wait()
        records.append({'check': label, 'status': status, 'seconds': round(time.monotonic() - started, 2)})
        (log_dir / 'results.json').write_text(json.dumps(records, indent=2) + '\n')
        if status:
            return status if status > 0 else 128 - status
    return 0


def backend_commands():
    commands = [('backend', [sys.executable, '-m', 'scripts.test_backend']),
                ('python-lint', [sys.executable, '-m', 'pyflakes', 'app', 'scripts'])]
    for path in sorted(Path('app/static').rglob('*.js')):
        commands.append(('javascript-' + path.name, ['node', '--check', str(path)]))
    commands.append(('webauthn', [sys.executable, 'scripts/test_webauthn_isolated.py']))
    return commands


def browser_command(browser, suite, environment):
    values = dict(line.split('=', 1) for line in environment.read_text().splitlines() if line)
    prefix = browser.upper()
    binary, driver = values[prefix + '_BINARY'], values[prefix + '_DRIVER']
    if not Path(binary).is_file() or not Path(driver).is_file():
        raise ValueError('The selected browser and matching driver must be provisioned before validation.')
    return [sys.executable, f'scripts/test_{suite}_browser.py', '--browser', browser,
            '--chrome-binary' if browser == 'chrome' else '--firefox-binary', binary,
            '--chromedriver' if browser == 'chrome' else '--geckodriver', driver]


def summary(log_dir, status):
    path = log_dir / 'results.json'
    records = json.loads(path.read_text()) if path.exists() else []
    lines = [f'Validation job: **{status}**', '',
             f'Source: `{subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}`', '',
             '| Check | Exit status | Seconds |', '| --- | --- | --- |']
    lines.extend(f'| {r["check"]} | {r["status"]} | {r["seconds"]} |' for r in records)
    if not records:
        lines.append('No test results were recorded; inspect provisioning or checkout failures.')
    message = '\n'.join(lines) + '\n'
    print(message)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a') as target:
            target.write(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    checkout = sub.add_parser('verify-checkout')
    checkout.add_argument('--commit', required=True)
    sub.add_parser('suites')
    backend = sub.add_parser('backend')
    backend.add_argument('--logs', type=Path, required=True)
    browser = sub.add_parser('browser')
    browser.add_argument('--browser', choices=('chrome', 'firefox'), required=True)
    browser.add_argument('--suite', choices=BROWSER_SUITES, required=True)
    browser.add_argument('--environment', type=Path, required=True)
    browser.add_argument('--logs', type=Path, required=True)
    report = sub.add_parser('summary')
    report.add_argument('--logs', type=Path, required=True)
    report.add_argument('--status', required=True)
    gate = sub.add_parser('gate')
    gate.add_argument('--build-image', choices=('true', 'false'), required=True)
    args = parser.parse_args()
    if args.command == 'verify-checkout':
        print(verify_checkout(args.commit))
    elif args.command == 'suites':
        print(json.dumps(BROWSER_SUITES))
    elif args.command == 'backend':
        return run_commands(backend_commands(), args.logs)
    elif args.command == 'browser':
        return run_commands([(args.suite, browser_command(args.browser, args.suite, args.environment))], args.logs)
    elif args.command == 'summary':
        summary(args.logs, args.status)
    else:
        require_success({name: os.environ.get(name.upper() + '_RESULT', '')
                         for name in ('backend', 'versions', 'browsers', 'docker')},
                        build_image=args.build_image == 'true')
        print('All required validation jobs passed for the selected source commit.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        from scripts.ci_report import failure
        failure(str(error))
        sys.exit(str(error))
