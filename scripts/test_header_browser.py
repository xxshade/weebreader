"""Compact header geometry and interactions through the real application."""
import argparse

from browser_checks import add_browser_argument, selected_browsers, baseline_passed, mutation_rejected
from contextlib import closing
import io
from pathlib import Path
import sqlite3
import sys
import unittest

from selenium.webdriver.common.keys import Keys

import test_audit_browser as audit
from ci_report import unittest_failures


GEOMETRY = """const header=document.querySelector('#app-header');
const nodes=[header.querySelector(':scope > a'),...header.querySelectorAll('input[type=search],#filters-btn,#thumb-size-btn,#user-menu-btn')];
return {width:innerWidth,header:header.getBoundingClientRect().toJSON(),
    controls:nodes.map(e=>({id:e.id||'library',...e.getBoundingClientRect().toJSON()}))};"""


def check_geometry(case, *, one_row=True):
    geometry = case.js(GEOMETRY)
    controls = geometry['controls']
    for control in controls:
        case.assertGreaterEqual(control['left'], -1, str(geometry))
        case.assertLessEqual(control['right'], geometry['width'] + 1, str(geometry))
        case.assertGreaterEqual(control['width'], 39, str(geometry))
        case.assertGreaterEqual(control['height'], 39, str(geometry))
    if one_row:
        centers = [c['y'] + c['height'] / 2 for c in controls]
        case.assertLess(max(centers) - min(centers), 2,
                        'The mobile header must keep all controls on one row: ' + str(geometry))
    return geometry


def check_panel(case, selector):
    panel = case.js("return document.querySelector(" + repr(selector) + ").getBoundingClientRect().toJSON()")
    width = case.js('return innerWidth')
    case.assertGreaterEqual(panel['left'], 0)
    case.assertLessEqual(panel['right'], width)
    expected = case.js("return document.querySelector('#app-header').getBoundingClientRect().width - parseFloat(getComputedStyle(document.documentElement).fontSize)")
    case.assertAlmostEqual(panel['width'], expected, delta=1,
                           msg='Mobile panels must use the header width, not the narrow search group')
    case.assertTrue(case.js("const p=document.querySelector(" + repr(selector) + ");return p.scrollWidth<=p.clientWidth+1"),
                    'Panel contents must fit without horizontal scrolling')
    case.assertGreater(panel['height'], 0)


def check_compact_header(case, activate, *, keyboard=None):
    """Shared assertions; Android supplies real touch input and native IME checks."""
    original = case.row('SELECT username,avatar_path FROM users WHERE id=1')
    username = 'reader-with-a-long-account-name-' * 4
    try:
        with closing(sqlite3.connect(case.fixture.data / 'library.db')) as db, db:
            db.execute('UPDATE users SET username=?,avatar_path=NULL WHERE id=1', (username,))
        for path, search, rows in (
            ('/', '#series-search', '#series-grid .series-card'),
            ('/series/100', '#item-search', '#items-grid [data-item-id]'),
            ('/novels/100', '#chapter-filter', '[data-chapter-search]'),
        ):
            case.driver.get(case.base + path)
            case.wait("document.querySelector(" + repr(search) + ") && window.userMenuBound")
            check_geometry(case)
            case.assertTrue(case.driver.find_element('css selector', '[data-account-icon]').is_displayed())
            case.assertFalse(case.driver.find_element('css selector', '[data-account-name]').is_displayed())
            case.assertIn(username, case.driver.find_element('id', 'user-menu-btn').get_attribute('aria-label'))
            activate('#user-menu-btn')
            case.wait("document.querySelector('#user-menu-btn').getAttribute('aria-expanded')==='true'")
            identity = case.driver.find_element('css selector', '[data-account-identity]')
            case.assertTrue(identity.is_displayed())
            case.assertEqual(identity.text, username)
            activate('#user-menu-btn')
            activate('#filters-btn')
            case.wait("document.querySelector('#filters-btn').getAttribute('aria-expanded')==='true'")
            check_panel(case, '#filters-popover')
            # The real filter can be changed and its indicator stays active.
            activate('#filters-popover label:has(input[type=checkbox])')
            case.wait("document.querySelector('#filters-btn').classList.contains('text-indigo-400')")
            activate('#filters-popover label:has(input[type=checkbox])')
            activate('#filters-btn')
            if path != '/novels/100':
                activate('#thumb-size-btn')
                case.wait("document.querySelector('#thumb-size-btn').getAttribute('aria-expanded')==='true'")
                check_panel(case, '#thumb-size-popover')
                slider = case.driver.find_element('id', 'thumb-size-slider')
                before = int(slider.get_attribute('value'))
                slider.send_keys(Keys.ARROW_RIGHT)
                case.assertNotEqual(int(slider.get_attribute('value')), before)
                activate('#thumb-size-btn')
            # Native typing exercises the page's actual input/keyup filter.
            visible = "[...document.querySelectorAll(" + repr(rows) + ")].some(e=>e.getClientRects().length>0)"
            case.assertTrue(case.js('return ' + visible),
                            'The listing must contain visible results before searching')
            activate(search)
            if keyboard:
                keyboard(search)
            check_geometry(case)
            field = case.driver.find_element('css selector', search)
            field.send_keys('no-matching-title-987654321')
            case.wait('!(' + visible + ')')
            field.send_keys(Keys.CONTROL, 'a', Keys.NULL, Keys.BACKSPACE)
            case.wait(visible)
            case.assertEqual(case.js('return audit.errors'), [])
        # An avatar is retained without adding the full name to the row.
        with closing(sqlite3.connect(case.fixture.data / 'library.db')) as db, db:
            db.execute("UPDATE users SET avatar_path='series-1-fixture.webp' WHERE id=1")
        case.driver.get(case.base + '/')
        case.wait("document.querySelector('#user-menu-btn img')?.complete")
        check_geometry(case)
        case.assertTrue(case.driver.find_element('css selector', '#user-menu-btn img').is_displayed())
        # Enlarge the real page's text. Wrapping is allowed; clipping is not.
        case.js("document.documentElement.style.fontSize='200%'")
        check_geometry(case, one_row=False)
        activate('#filters-btn')
        case.wait("document.querySelector('#filters-btn').getAttribute('aria-expanded')==='true'")
        check_panel(case, '#filters-popover')
        # A wider fallback font must fit as well as the loaded UI font.
        case.js("const panel=document.getElementById('filters-popover');panel.style.fontFamily='monospace';panel.style.letterSpacing='0.06em'")
        check_panel(case, '#filters-popover')
    finally:
        case.js("document.documentElement.style.removeProperty('font-size')")
        with closing(sqlite3.connect(case.fixture.data / 'library.db')) as db, db:
            db.execute('UPDATE users SET username=?,avatar_path=? WHERE id=1',
                       (original['username'], original['avatar_path']))


class HeaderChecks(audit.AuditChecks):
    def test_compact_header_and_panels(self):
        for width in (320, 360, 390, 414):
            with self.subTest(width=width):
                if self.browser == 'chrome':
                    # Chrome headless enforces a minimum native window width.
                    self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',
                        {'width': width, 'height': 840, 'deviceScaleFactor': 1, 'mobile': False})
                else:
                    self.driver.browsing_context.set_viewport(context=self.driver.current_window_handle,
                        viewport={'width': width, 'height': 840})
                self.assertEqual(self.js('return innerWidth'), width)
                check_compact_header(self, lambda selector: self.driver.find_element('css selector', selector).click())
        if self.browser == 'chrome':
            self.driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
        else:
            self.driver.browsing_context.set_viewport(context=self.driver.current_window_handle, viewport=None)
        self.driver.set_window_size(1280, 900)
        self.driver.get(self.base + '/')
        self.assertTrue(self.driver.find_element('css selector', '[data-account-name]').is_displayed())
        self.assertFalse(self.driver.find_element('css selector', '[data-account-icon]').is_displayed())
        self.assertEqual(self.js("return getComputedStyle(document.querySelector('#app-header')).position"), 'sticky')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    add_browser_argument(parser)
    parser.add_argument('--chrome-binary', default='/usr/bin/chromium')
    parser.add_argument('--chromedriver', default='/usr/bin/chromedriver')
    parser.add_argument('--firefox-binary', default='/usr/bin/firefox')
    parser.add_argument('--geckodriver', default='/usr/bin/geckodriver')
    ARGS = parser.parse_args()
    audit.ARGS = ARGS
    def suite():
        return unittest.TestSuite(type(browser.title() + 'Header', (HeaderChecks,), {'browser': browser})(
            'test_compact_header_and_panels') for browser in selected_browsers(ARGS))

    result = unittest.TextTestRunner(verbosity=2).run(suite())
    unittest_failures(result)
    if not baseline_passed(result, len(selected_browsers(ARGS))):
        sys.exit(1)
    source = Path(__file__).resolve().parents[1] / 'app/static/src/theme.css'
    original = source.read_text()
    for before, after, signal in (
        ('#header-controls {', '#header-controls { flex-basis: 100%;',
         'The mobile header must keep all controls on one row'),
        ('#app-header #filters-popover .eye-label {\n        white-space: normal;',
         '#app-header #filters-popover .eye-label {\n        white-space: nowrap;',
         'Panel contents must fit without horizontal scrolling'),
    ):
        if original.count(before) != 1:
            raise ValueError('The header mutation must match exactly once.')
        try:
            source.write_text(original.replace(before, after))
            output = io.StringIO()
            control = unittest.TextTestRunner(stream=output, verbosity=2).run(suite())
        finally:
            source.write_text(original)
        if not mutation_rejected(control, selected_browsers(ARGS), 'Header', signal):
            print(output.getvalue())
            raise AssertionError('Every selected browser must reject the header layout defect.')
        print(' and '.join(b.title() for b in selected_browsers(ARGS)) + ' rejected: ' + signal, flush=True)
