"""Prove Android regressions detect deliberate defects after a passing baseline."""
import json
import os
from pathlib import Path
import subprocess
import sys

if __package__:
    from .ci_report import report_exceptions
else:
    from ci_report import report_exceptions


def validate_control(label, test, assertion, exit_code, counts, evidence, log):
    """Accept only the intended application assertion after one real execution."""
    expected = {'run': 1, 'errors': 0, 'skipped': 0}
    valid = (exit_code != 0 and counts.get('failures', 0) >= 1
             and all(counts.get(key) == value for key, value in expected.items())
             and len(evidence) == 1 and evidence[0].get('test') == test
             and assertion in log)
    if not valid:
        raise ValueError(f'{label}: control did not reach its intended assertion: {assertion}\n'
                         f'counts={counts}, exit={exit_code}, executions={evidence}\n{log[-6000:]}')


def main():
    root = Path(__file__).resolve().parents[1]
    arguments = sys.argv[1:]
    output = Path(arguments[arguments.index('--output') + 1])
    names = {
        'paragraph': ('js/reader-pagination.js', 'function splittable(unit) {',
                      'function splittable(unit) { return false;',
                      'test_touch_pagination_preserves_epub_and_novel_text_and_resume',
                      'The paragraph must fill the space after the introduction'),
        'font': ('js/reader-text.js', 'prefs.fontSize = next;',
                 'prefs.fontSize = Number(prefs.fontSize);',
                 'test_typography_font_controls_and_scroll_gesture',
                 'The font control must increase the rendered text size'),
        'comic': ('js/reader-comic.js', 'current_page: targetPage,',
                  'current_page: 0,', 'test_comic_touch_and_resume',
                  'Comic resume must restore the touched page'),
        'reconnect': ('js/library-events.js', 'function resync() {',
                      "function resync() { document.querySelectorAll('img').forEach(img => { img.src += (img.src.includes('?') ? '&' : '?') + 'control=1'; });",
                      'test_background_restart_reconnects_without_navigation_or_image_fetch',
                      'Reconnection must reuse cached cover images'),
    }
    # Wrapping the controls reproduces the original mobile header defect.
    names['header'] = ('src/theme.css', '#header-controls {',
                       '#header-controls { flex-basis: 100%;',
                       'test_compact_header_and_panels',
                       'The mobile header must keep all controls on one row')
    names['native-scroll'] = ('src/theme.css', '/* ---- A reader is the page',
                              '#custom-scroll { overflow-y: hidden !important; }\n/* ---- A reader is the page',
                              'test_typography_font_controls_and_scroll_gesture',
                              'A native reading gesture must change the scroll position')
    names['srcdoc-commit'] = ('js/reader-text.js','doc?.URL === "about:srcdoc" && doc.body',
                              'doc?.body','test_srcdoc_commit',
                              'A late srcdoc commit must not erase the rendered chapter')
    baseline = json.loads((output / 'baseline.json').read_text())
    names['reading'] = ('js/reading-time.js', 'current.value.completed = completed;',
                        'current.value.completed = false;', 'test_paginated_measurements',
                        'Measured words must match the traversed text across paragraph fragments')
    names['continuous-reading'] = ('js/reader-text.js','function reportContinuousTiming(normal) {',
                                   'function reportContinuousTiming(normal) { if (normal) return;',
                                   'test_continuous_measurements',
                                   'Continuous reading must record words after a real scroll gesture')
    names['recorder-upgrade'] = ('js/reader-text.js','script.src = trackerUrl;',
                                 'resolve(); return;','test_recorder_upgrade',
                                 'An existing tab must upgrade its reading recorder without navigation')
    names['reading-checkpoint'] = ('js/reading-time.js','persist({...current.value, intervals, revision: ++current.revision});',
                                   'if (current.value.closed) persist({...current.value, intervals, revision: ++current.revision});',
                                   'test_checkpoint_recovery',
                                   'An unfinished reading visit must be checkpointed before leaving the page')
    names['reading-priority'] = ('js/reading-time.js','function onOnline() { if (anchor === null) activate(true); flush(); }',
                                 'function onOnline() { activate(true); flush(); }','test_reading_priority',
                                 'Reconnecting must not take reading priority without user activity')
    if baseline != {'run': 11, 'failures': 0, 'errors': 0, 'skipped': 0}:
        raise ValueError('All eleven baseline tests must pass before mutation controls.')
    # Expected assertion failures belong in artifacts, not Actions error annotations.
    environment = {**os.environ, 'GITHUB_ACTIONS': 'false'}
    for label, (filename, before, after, test, assertion) in names.items():
        source = root / 'app/static' / filename
        original = source.read_text()
        if original.count(before) != 1:
            raise ValueError(f'{label}: mutation no longer matches exactly once.')
        directory = output / ('control-' + label)
        directory.mkdir(parents=True, exist_ok=True)
        result_path = directory / 'result.json'
        result_path.unlink(missing_ok=True)
        (directory / 'executions.jsonl').unlink(missing_ok=True)
        command = [sys.executable, str(root / 'scripts/test_android_browser.py'), *arguments]
        command[command.index('--output') + 1] = str(directory)
        command += ['--test', test, '--result-json', str(result_path)]
        try:
            source.write_text(original.replace(before, after))
            with (directory / 'test.log').open('w') as stream:
                result = subprocess.run(command, cwd=root, env=environment,
                                        stdout=stream, stderr=subprocess.STDOUT, timeout=480)
        finally:
            source.write_text(original)
        log = (directory / 'test.log').read_text()
        if not result_path.exists():
            raise ValueError(f'{label}: process exited {result.returncode} without test results\n{log[-6000:]}')
        counts = json.loads(result_path.read_text())
        evidence_path = directory / 'executions.jsonl'
        evidence = [json.loads(line) for line in evidence_path.read_text().splitlines()] if evidence_path.exists() else []
        try:
            validate_control(label, test, assertion, result.returncode, counts, evidence, log)
        except ValueError as error:
            details = ''
            for name in ('native-input.jsonl', 'browser-state.json'):
                path = directory / name
                if path.exists():
                    details += '\n' + name + '\n' + path.read_text()[-2500:]
            raise ValueError(str(error) + details) from error
        print(f'{label}: deliberate defect rejected by the real Android test', flush=True)


if __name__ == '__main__':
    report_exceptions()
    main()
