"""Scan the runtime image and fail closed on critical CVEs or incomplete scans."""
import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile

TRIVY_VERSION = '0.76.1'
TRIVY_SHA256 = 'c7cbe3137fb8a48a17574bede3dd6beb16742d1e9772929940fc90082c39d9c0'
SEVERITIES = ('UNKNOWN', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL')


def evaluate(report):
    """Require both Debian/runtime and Python package inventories, even if clean."""
    if not isinstance(report, dict) or report.get('SchemaVersion') != 2:
        raise ValueError('Unsupported or missing vulnerability report schema.')
    metadata = report.get('Metadata')
    if not isinstance(metadata, dict) or not isinstance(metadata.get('OS'), dict) or not metadata['OS'].get('Family'):
        raise ValueError('Runtime operating system was not identified.')
    results = report.get('Results')
    if not isinstance(results, list):
        raise ValueError('Missing package scan results.')
    inventories = set()
    counts = Counter()
    critical = []
    for result in results:
        if not isinstance(result, dict):
            raise ValueError('Invalid package scan result.')
        packages = result.get('Packages')
        if isinstance(packages, list) and packages:
            if result.get('Class') == 'os-pkgs':
                inventories.add('os')
            if result.get('Class') == 'lang-pkgs' and result.get('Type') == 'python-pkg':
                inventories.add('python')
        vulnerabilities = result.get('Vulnerabilities')
        if vulnerabilities is None:
            vulnerabilities = []
        if not isinstance(vulnerabilities, list):
            raise ValueError('Invalid vulnerability list.')
        for issue in vulnerabilities:
            if not isinstance(issue, dict) or issue.get('Severity') not in SEVERITIES:
                raise ValueError('Invalid vulnerability severity.')
            counts[issue['Severity']] += 1
            if issue['Severity'] == 'CRITICAL':
                critical.append(issue)
    if inventories != {'os', 'python'}:
        raise ValueError('Incomplete runtime inventory: both OS and Python packages must be scanned.')
    return counts, critical


def command(binary, report, *, archive=None, image=None, cache=None):
    if (archive is None) == (image is None):
        raise ValueError('Select exactly one image archive or immutable image reference.')
    if image is not None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}', image):
        raise ValueError('Registry scans require an immutable image@sha256 digest.')
    args = [str(binary), 'image', '--config', '', '--ignorefile', '',
            '--scanners', 'vuln', '--pkg-types', 'os,library', '--ignore-unfixed=false',
            '--severity', ','.join(SEVERITIES), '--list-all-pkgs', '--format', 'json',
            '--db-repository', 'ghcr.io/aquasecurity/trivy-db:2',
            '--java-db-repository', 'ghcr.io/aquasecurity/trivy-java-db:1',
            '--output', str(report), '--exit-code', '0', '--platform', 'linux/amd64', '--timeout', '15m']
    if cache:
        args.extend(('--cache-dir', str(cache)))
    args.extend(('--input', str(archive)) if archive is not None else (image,))
    return args


def digest_file(path):
    checksum = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
    return 'sha256:' + checksum.hexdigest()


def verify_archive(archive, identity):
    if digest_file(archive) != Path(identity).read_text().strip():
        raise ValueError('Image archive differs from the artifact that passed its vulnerability scan.')


@contextmanager
def scanner_input(archive):
    """Trivy accepts Docker tar files but needs an OCI *layout directory*.

    Unpack only the outer OCI transport, never its filesystem layers. Keep
    the original archive unchanged for hashing and publication. The temporary
    layout is outside diagnostics and disappears even when scanning fails.
    """
    if archive is None:
        yield None
        return
    with tarfile.open(archive, 'r:*') as bundle:
        members = bundle.getmembers()
        names = {str(PurePosixPath(member.name)) for member in members}
        if 'oci-layout' not in names or 'index.json' not in names:
            if 'manifest.json' not in names:
                raise ValueError('Archive is neither a Docker image nor an OCI layout.')
            yield archive
            return
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('Unsafe OCI archive path.')
            if not (member.isfile() or member.isdir()):
                raise ValueError('OCI archive links and special files are forbidden.')
            if str(path) not in ('.', 'oci-layout', 'index.json', 'blobs') and path.parts[0] != 'blobs':
                raise ValueError('Unexpected file outside the OCI metadata and blobs.')
        with tempfile.TemporaryDirectory(prefix='weebreader-oci-') as directory:
            bundle.extractall(directory, members=members, filter='data')
            yield Path(directory)


def run_scanner(args, log_path):
    """Preserve scanner diagnostics in both Actions output and the report artifact."""
    with Path(log_path).open('w') as log:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors='replace', bufsize=1)
        try:
            for line in process.stdout:
                print(line, end='', flush=True)
                log.write(line)
                log.flush()
            return process.wait()
        finally:
            process.stdout.close()


def cell(value):
    return str(value or '—').replace('|', '\\|').replace('\n', ' ')[:250]


def scan(binary, directory, *, archive=None, image=None, cache=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / 'vulnerabilities.json'
    report_path.unlink(missing_ok=True)
    (directory / 'identity.txt').unlink(missing_ok=True)
    lines = ['## Runtime image vulnerability scan', '', f'Trivy `{TRIVY_VERSION}`; blocking severity: **CRITICAL**, including unfixed CVEs.', '']
    status = 2
    try:
        actual = subprocess.check_output([str(binary), '--version'], text=True).splitlines()
        if not actual or actual[0] != 'Version: ' + TRIVY_VERSION:
            raise ValueError('The scanner version differs from the pinned, verified release.')
        identity = digest_file(archive) if archive is not None else image
        lines.extend([f'Scanned artifact: `{identity}`', ''])
        with scanner_input(archive) as source:
            returncode = run_scanner(command(binary, report_path, archive=source, image=image, cache=cache),
                                     directory / 'scanner.log')
        if returncode:
            raise ValueError(f'Scanner failed with exit code {returncode}; no clean result is assumed.')
        report = json.loads(report_path.read_text())
        counts, critical = evaluate(report)
        lines.extend(['| Severity | Findings |', '| --- | ---: |'])
        lines.extend(f'| {severity} | {counts[severity]} |' for severity in reversed(SEVERITIES))
        if critical:
            lines.extend(['', '| Critical CVE | Package | Installed | Fixed version |', '| --- | --- | --- | --- |'])
            lines.extend('| ' + ' | '.join(cell(issue.get(key)) for key in
                         ('VulnerabilityID', 'PkgName', 'InstalledVersion', 'FixedVersion')) + ' |'
                         for issue in critical[:50])
            lines.extend(['', '**Blocked:** critical vulnerabilities were found.'])
            status = 1
        else:
            lines.extend(['', '**Passed:** complete OS and Python inventories contain no known critical CVEs.'])
            status = 0
        (directory / 'identity.txt').write_text(str(identity) + '\n')
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
        lines.extend(['', '**Blocked:** ' + cell(error)])
        with (directory / 'scanner.log').open('a') as log:
            log.write('\nScan blocked: ' + str(error) + '\n')
    summary = '\n'.join(lines) + '\n'
    (directory / 'summary.md').write_text(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write(summary)
    print(summary, flush=True)
    return status


def main():
    if sys.argv[1:2] == ['verify-archive']:
        parser = argparse.ArgumentParser(description='Verify the archive immediately before publishing it.')
        parser.add_argument('--archive', type=Path, required=True)
        parser.add_argument('--identity-file', type=Path, required=True)
        args = parser.parse_args(sys.argv[2:])
        verify_archive(args.archive, args.identity_file)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trivy', default='trivy')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--archive', type=Path)
    source.add_argument('--image')
    parser.add_argument('--report-directory', type=Path, required=True)
    parser.add_argument('--cache-directory', type=Path)
    args = parser.parse_args()
    return scan(args.trivy, args.report_directory, archive=args.archive,
                image=args.image, cache=args.cache_directory)


if __name__ == '__main__':
    sys.exit(main())
