"""Statistics layout, range totals and chart tabs in both real browsers."""
import argparse
from contextlib import closing
import io
from pathlib import Path
import sqlite3
import sys
import unittest

from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

import test_audit_browser as audit
from browser_checks import add_browser_argument, baseline_passed, mutation_rejected, selected_browsers
from ci_report import unittest_failures

ROOT = Path(__file__).resolve().parent.parent


class StatisticsLayoutChecks(audit.AuditChecks):
    @classmethod
    def seed_readers(cls):
        with closing(sqlite3.connect(cls.fixture.data / 'library.db')) as db, db:
            for ago, kind, seconds, cap in ((0, 'cbz', 90, 180), (0, 'epub', 45, 180),
                                            (20, 'epub', 300, 60)):
                db.execute("""INSERT INTO page_reads
                    (user_id,item_id,kind,page_index,started_at,seconds,cap_seconds)
                    VALUES(1,1,?,0,datetime('now','localtime',?),?,?)""",
                           (kind, f'-{ago} days', seconds, cap))

    def test_statistics_layout(self):
        self.driver.get(self.base + '/stats?days=7')
        self.wait('window.statsDailyTooltip && window.statsRangeBound')
        cards = self.driver.find_elements('css selector', '.stats-kpis > div')
        self.assertEqual(len(cards), 3, 'Only the three retained reading indicators belong in Reading')
        self.assertEqual([card.text.splitlines()[0] for card in cards],
                         ['Estimated text pace', 'Time per comic page', 'Days with reading'])
        geometry = self.js("""return [...document.querySelectorAll('.stats-kpis > div')].map(e=>{
            const r=e.getBoundingClientRect();return {y:r.y,left:r.left,right:r.right,overflow:e.scrollWidth>e.clientWidth}})""")
        self.assertLess(max(r['y'] for r in geometry) - min(r['y'] for r in geometry), 2,
                        'Reading indicators must share one row')
        self.assertTrue(all(not r['overflow'] for r in geometry),
                        'Reading indicators must not clip values: ' + repr(geometry))
        self.assertTrue(all(r['left'] >= 0 and r['right'] <= self.js('return innerWidth') for r in geometry))
        self.driver.find_element('css selector', '.stats-kpis .tip-btn').click()
        help_box = self.driver.find_element('css selector', '.stats-kpis .tip-body')
        self.assertTrue(help_box.is_displayed())
        self.assertGreaterEqual(help_box.rect['x'], 0, 'Reading pace help must fit the viewport')
        self.assertLessEqual(help_box.rect['x'] + help_box.rect['width'], self.js('return innerWidth'),
                             'Reading pace help must fit the viewport')
        self.driver.find_element('css selector', '.stats-kpis .tip-btn').click()
        self.driver.find_element('css selector', '.stats-kpis .tip-btn').send_keys(Keys.TAB)
        ActionChains(self.driver).move_to_element(self.driver.find_element('css selector', 'h1')).perform()
        self.wait("getComputedStyle(document.querySelector('.stats-kpis .tip-body')).display==='none'")

        def selection(measure):
            self.assertEqual(self.js(f"return document.querySelector('[data-chart-tab={measure}]').getAttribute('aria-selected')"),
                             'true', 'Exactly the selected chart must be visible')
            shown = self.js("return [...document.querySelectorAll('[data-chart-panel]')].filter(p=>!p.hidden).map(p=>p.dataset.chartPanel)")
            self.assertEqual(shown, [measure], 'Exactly the selected chart must be visible')
            visible_charts = self.driver.find_elements('css selector', '[data-chart-panel]:not([hidden]) [data-daily-chart]')
            self.assertEqual(len(visible_charts), 1)
            self.assertTrue(visible_charts[0].is_displayed())

        selection('seconds')
        total = self.driver.find_element('css selector', '[data-window-time]')
        self.assertEqual(total.text, 'Time read 2m', 'Window total must include only selected, capped time')
        self.assertLess(abs(total.rect['y'] - self.driver.find_element('css selector', '.stats-chart-numbers span:last-child').rect['y']), 2,
                        'Time read must sit beside the peak')
        self.driver.find_element('id', 'daily-tab-comic_visits').click()
        selection('comic_visits')
        old_start = self.js("return document.querySelector('#daily-activity').dataset.windowStart")
        self.driver.find_element('css selector', 'a[hx-get="/stats/daily-activity?days=30"]').click()
        self.wait("document.querySelector('#daily-activity').dataset.windowStart!==" + repr(old_start))
        selection('comic_visits')
        self.assertEqual(self.js("return document.querySelector('[data-window-time]').textContent.trim()"),
                         'Time read 3m', 'Window total must change with the selected range')

        # A tab is reachable by keyboard; arrows switch measures and focus.
        self.driver.find_element('id', 'daily-tab-comic_visits').send_keys(Keys.ARROW_LEFT)
        selection('seconds')
        self.assertEqual(self.js('return document.activeElement.id'), 'daily-tab-seconds')
        columns = self.driver.find_elements('css selector', '#daily-panel-seconds [data-tip]')
        columns[-1].send_keys(Keys.ARROW_LEFT)
        self.assertEqual(self.js('return document.activeElement'), columns[-2])
        self.assertTrue(self.driver.find_element('css selector', '.viz-tip').is_displayed())
        self.driver.find_element('id', 'daily-tab-comic_visits').click()
        self.assertFalse(self.driver.find_element('css selector', '.viz-tip').is_displayed(),
                         'Switching charts must dismiss the previous tooltip')

        # Custom dates share the same partial and total as the presets.
        self.driver.find_element('id', 'range-custom-toggle').click()
        self.wait("!document.querySelector('#range-picker').hidden")
        today = self.js("return document.querySelector('#daily-activity').dataset.today")
        self.driver.find_element('css selector', f'[data-cal-day="{today}"]').click()
        self.driver.find_element('css selector', f'[data-cal-day="{today}"]').click()
        self.driver.find_element('css selector', '[data-cal-action=apply]').click()
        self.wait(f"document.querySelector('#daily-activity').dataset.windowStart==='{today}'")
        selection('comic_visits')
        self.assertEqual(self.js("return document.querySelector('[data-window-time]').textContent.trim()"), 'Time read 2m')
        self.assertLessEqual(self.js('return document.documentElement.scrollWidth'), self.js('return innerWidth'),
                             'Statistics must fit the viewport')
        self.assertEqual(self.js('return audit.errors'), [])


def suite(*, mobile=True, widths=None):
    return unittest.TestSuite(type(browser.title() + 'StatisticsLayout' + str(width), (StatisticsLayoutChecks,), {
        'browser': browser, 'viewport_width': width})('test_statistics_layout')
        for browser in selected_browsers(ARGS) for width in (widths or ((1280, 320) if mobile else (1280,))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    add_browser_argument(parser)
    parser.add_argument('--chrome-binary', default='/usr/bin/chromium')
    parser.add_argument('--chromedriver', default='/usr/bin/chromedriver')
    parser.add_argument('--firefox-binary', default='/usr/bin/firefox')
    parser.add_argument('--geckodriver', default='/usr/bin/geckodriver')
    ARGS = parser.parse_args()
    audit.ARGS = ARGS
    result = unittest.TextTestRunner(verbosity=2).run(suite())
    unittest_failures(result)
    if not baseline_passed(result, len(selected_browsers(ARGS)) * 2):
        sys.exit(1)
    for filename, before, after, signal in (
        ('app/static/src/theme.css', 'grid-template-columns: repeat(3, minmax(0, 1fr));',
         'grid-template-columns: repeat(2, minmax(0, 1fr));', 'Reading indicators must share one row'),
        ('app/stats/router.py', 'sum(day["seconds"] for day in daily)',
         '0', 'Window total must include only selected, capped time'),
        ('app/static/js/stats-daily.js', 'panel.hidden = panel.dataset.chartPanel !== measure;',
         'panel.hidden = false;', 'Exactly the selected chart must be visible'),
        ('app/static/js/stats-daily.js', '() => selectMeasure(selectedMeasure)',
         '() => selectMeasure("seconds")', 'Exactly the selected chart must be visible'),
        ('app/static/src/theme.css',
         '.viz .stats-kpis .tip-body {\n    left: 0;\n    right: auto;\n    width: min(24rem, 100%);\n}',
         '.stats-kpis .tip-row { position: relative; }\n.viz .stats-kpis .tip-body { left: auto; right: 0; width: min(24rem, calc(100vw - 3.5rem)); }',
         'Reading pace help must fit the viewport'),
    ):
        source = ROOT / filename
        original = source.read_text()
        if original.count(before) != 1:
            raise ValueError('Statistics layout mutation must match exactly once.')
        try:
            source.write_text(original.replace(before, after))
            output = io.StringIO()
            widths = (320,) if signal == 'Reading pace help must fit the viewport' else (1280,)
            control = unittest.TextTestRunner(stream=output, verbosity=2).run(suite(widths=widths))
        finally:
            source.write_text(original)
        if not mutation_rejected(control, selected_browsers(ARGS), 'StatisticsLayout', signal):
            print(output.getvalue())
            raise AssertionError('Every selected browser must reject the statistics defect.')
        print(' and '.join(b.title() for b in selected_browsers(ARGS)) + ' rejected: ' + signal, flush=True)
