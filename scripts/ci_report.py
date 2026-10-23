"""Expose failures through GitHub check annotations when log storage is unavailable."""
import os
import sys
import traceback


def failure(message):
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        escaped = message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        print(f'::error title=Test failure::{escaped}', flush=True)


def unittest_failures(result):
    for test, trace in result.failures + result.errors:
        failure(f'{test}\n{trace}')


def report_exceptions():
    """Make setup failures visible through the check annotations as well as logs."""
    original = sys.excepthook

    def report(kind, value, trace):
        failure(''.join(traceback.format_exception(kind, value, trace)))
        original(kind, value, trace)

    sys.excepthook = report
