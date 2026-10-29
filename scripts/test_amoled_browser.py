"""AMOLED migration and removed controls, in both desktop engines and widths."""
import argparse
import sys
import unittest
from browser_checks import add_browser_argument, selected_browsers, baseline_passed
import test_audit_browser as audit


class AmoledChecks(audit.AuditChecks):
    def test_legacy_preferences_and_readers(self):
        for width in (1280, 320):
            self.driver.set_window_size(width, 900)
            for theme in ('dark', 'light', 'amoled'):
                self.driver.get(self.base + '/')
                self.driver.execute_script("document.cookie='app-theme=dark;path=/';localStorage.setItem('app-theme','dark');localStorage.setItem('text-reader-prefs',JSON.stringify({theme:arguments[0],font:'original',fontSize:110,width:720,lineHeight:1.6,flow:'paginated',bottomGap:0}))", theme)
                self.driver.get(self.base + '/settings')
                self.assertEqual(self.js('return document.documentElement.dataset.appTheme'), 'amoled')
                self.assertEqual(self.js('return getComputedStyle(document.body).backgroundColor'), 'rgb(0, 0, 0)')
                self.assertFalse(self.js("return !!document.getElementById('app-theme-select')"))
                for path in ('/reader/100', '/novels/100/read/1/1'):
                    self.driver.get(self.base + path)
                    self.wait("document.querySelector('#paginated-viewer iframe')?.style.visibility==='visible'")
                    saved = self.js("return JSON.parse(localStorage.getItem('text-reader-prefs'))")
                    self.assertEqual(saved['theme'], 'amoled')
                    self.assertEqual(saved['fontSize'], 110)
                    self.assertFalse(self.js("return !!document.getElementById('theme-select')"))
                    self.assertEqual(self.js("const f=document.querySelector('#paginated-viewer iframe');return f.contentWindow.getComputedStyle(f.contentDocument.body).backgroundColor"), 'rgb(0, 0, 0)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    add_browser_argument(parser)
    parser.add_argument('--chrome-binary', required=True)
    parser.add_argument('--chromedriver', required=True)
    parser.add_argument('--firefox-binary', required=True)
    parser.add_argument('--geckodriver', required=True)
    audit.ARGS = parser.parse_args()
    suite = unittest.TestSuite(type(browser.title() + 'Amoled', (AmoledChecks,), {'browser': browser})('test_legacy_preferences_and_readers') for browser in selected_browsers(audit.ARGS))
    expected = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not baseline_passed(result, expected))
