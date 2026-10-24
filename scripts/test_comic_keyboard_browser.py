"""Comic continuous-scroll keyboard regressions with real browser input."""
import argparse
from contextlib import closing
import io
from pathlib import Path
import sqlite3
import sys
import unittest
import zipfile

from PIL import Image
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

import test_audit_browser as audit
from browser_checks import add_browser_argument, baseline_passed, mutation_rejected, selected_browsers
from ci_report import unittest_failures

ROOT = Path(__file__).resolve().parent.parent


class ComicKeyboardChecks(audit.AuditChecks):
    # These checks use the real Fullscreen API, rather than the text audit stub.
    probe = audit.PROBE.replace('Element.prototype.requestFullscreen=()=>Promise.resolve();', '')

    @classmethod
    def seed_readers(cls):
        folder = cls.fixture.fixture / 'keyboard-comic'
        folder.mkdir()
        archive = folder / 'comic.cbz'
        image = io.BytesIO()
        Image.new('RGB', (800, 1200), (80, 100, 120)).save(image, format='PNG')
        with zipfile.ZipFile(archive, 'w') as book:
            for page in range(6):
                book.writestr(f'{page:02}.png', image.getvalue())
        with closing(sqlite3.connect(cls.fixture.data / 'library.db')) as db, db:
            db.execute("INSERT INTO series(id,library_id,name,sort_name,folder_path) VALUES(140,1,'Keyboard comic','keyboard',?)", (str(folder),))
            db.execute("INSERT INTO items(id,series_id,library_id,title,sort_title,file_path,file_hash,file_size,file_format,page_count) VALUES(140,140,1,'Comic','comic',?,'keyboard',?,'cbz',6)",
                       (str(archive), archive.stat().st_size))

    def key(self, key, count=1):
        actions = ActionChains(self.driver)
        for _ in range(count):
            actions.key_down(key).pause(.025)
        actions.key_up(key).perform()

    def position(self):
        return self.js("return document.querySelector('#page-scroll').scrollTop")

    def assert_scrolls(self):
        before = self.position()
        self.key(Keys.ARROW_DOWN)
        after = self.position()
        self.assertGreater(after, before, 'Down must scroll without first clicking the comic')
        self.assertLess(after - before, self.js("return document.querySelector('#page-scroll').clientHeight / 2"),
                        'An arrow must move a small portion, not turn a page')
        self.key(Keys.ARROW_UP)
        self.assertLess(self.position(), after, 'Up must scroll backwards')
        self.key(Keys.ARROW_DOWN, 5)
        self.assertGreater(self.position(), after, 'Repeated keydowns must keep scrolling')
        self.assertEqual(self.js('return location.pathname'), '/reader/140')
        progress = self.row('SELECT status FROM reading_progress WHERE item_id=140')
        self.assertNotEqual(progress, {'status': 'completed'}, 'Small scrolls must not complete the comic')

    def test_comic_scroll_keyboard(self):
        self.js("localStorage.setItem('comic-reader-prefs',JSON.stringify({mode:'scroll',rtl:false,fit:'width'}));localStorage.setItem('reader-fullscreen','0')")
        for boosted in (False, True):
            if boosted:
                self.driver.get(self.base + '/series/140')
                self.wait("document.querySelector('#library-events')?.libraryEventsBound")
                self.frames()
                marker = self.js('return audit.document')
                self.boosted('/reader/140')
                self.assertEqual(self.js('return audit.document'), marker,
                                 'The reader must open through a boosted navigation')
            else:
                self.driver.get(self.base + '/reader/140')
            self.wait("document.querySelector('#page-scroll img')?.naturalWidth > 0")
            self.wait("document.querySelector('#page-scroll').scrollHeight > innerHeight * 3")
            self.assert_scrolls()

        # Real toolbar clicks used to leave focus on a now-hidden mode button.
        self.driver.find_element('id', 'page-scroll').click()
        self.driver.find_element('css selector', '[data-mode=single]').click()
        self.driver.find_element('id', 'page-image').click()
        self.driver.find_element('css selector', '[data-mode=scroll]').click()
        self.wait("document.querySelector('#page-scroll img')?.naturalWidth > 0")
        self.assert_scrolls()

        # Fullscreen is entered by a trusted click, with no API mock.
        self.driver.find_element('id', 'page-scroll').click()
        self.driver.find_element('id', 'btn-fullscreen').click()
        self.wait('document.fullscreenElement !== null')
        self.js('document.activeElement.blur()')
        self.assert_scrolls()

        # A control retains its arrow behavior and never moves the comic.
        self.js("""const controls=document.querySelector('#controls');
            controls.classList.remove('opacity-0','pointer-events-none');
            controls.insertAdjacentHTML('beforeend', '<input id="keyboard-input" value="abc"><select id="keyboard-select"><option>A</option><option>B</option></select><div id="keyboard-edit" contenteditable="true">abc</div>');""")
        for selector in ('#keyboard-input', '#keyboard-select', '#keyboard-edit', '#btn-rtl'):
            self.js(f'document.querySelector({selector!r}).focus()')
            before = self.position()
            self.key(Keys.ARROW_DOWN)
            self.assertEqual(self.position(), before, 'Controls must keep their arrow keys: ' + selector)
            self.key(Keys.ARROW_UP)
            self.assertEqual(self.position(), before, 'Controls must keep their arrow keys: ' + selector)
        self.js('document.activeElement.blur()')

        # Reach the end using the same repeated arrow input as a reader.
        # Extra keydowns cover the end notice added after the last image.
        self.js("document.querySelector('#controls').classList.add('opacity-0','pointer-events-none');document.querySelector('#page-scroll').focus()")
        steps = self.js("return Math.ceil(document.querySelector('#page-scroll').scrollHeight / 40) + 20")
        self.key(Keys.ARROW_DOWN, steps)
        self.frames()
        self.key(Keys.ARROW_DOWN, 20)
        self.wait("document.querySelector('#page-scroll').scrollHeight - document.querySelector('#page-scroll').clientHeight - document.querySelector('#page-scroll').scrollTop < 2")
        end = self.position()
        self.key(Keys.ARROW_DOWN)
        self.assertAlmostEqual(self.position(), end, delta=1)
        self.key(Keys.ARROW_UP)
        self.assertLess(self.position(), end, 'Up must leave the end of the comic')
        self.assertEqual(self.js('return location.pathname'), '/reader/140')
        self.assertEqual(self.js('return audit.errors'), [])


def suite(*, mobile=True):
    return unittest.TestSuite(type(browser.title() + 'ComicKeyboard' + str(width), (ComicKeyboardChecks,), {
        'browser': browser, 'viewport_width': width})('test_comic_scroll_keyboard')
        for browser in selected_browsers(ARGS) for width in ((1280, 320) if mobile else (1280,)))


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
    source = ROOT / 'app/static/js/reader-comic.js'
    for before, after, signal in (
        ('elScroll.scrollBy({ top:', 'if (false) elScroll.scrollBy({ top:',
         'Down must scroll without first clicking the comic'),
        ('? 40 : -40', '? 40 : 40', 'Up must scroll backwards'),
        ('if (mode === "scroll") elScroll.focus({ preventScroll: true });', '',
         'Down must scroll without first clicking the comic'),
        ('&& e.target.closest("input, select, textarea, button, [contenteditable]")) return;',
         '&& false) return;', 'Controls must keep their arrow keys'),
    ):
        original = source.read_text()
        if original.count(before) != 1:
            raise ValueError('Comic keyboard mutation must match exactly once.')
        try:
            source.write_text(original.replace(before, after))
            output = io.StringIO()
            control = unittest.TextTestRunner(stream=output, verbosity=2).run(suite(mobile=False))
        finally:
            source.write_text(original)
        if not mutation_rejected(control, selected_browsers(ARGS), 'ComicKeyboard', signal):
            print(output.getvalue())
            raise AssertionError('Every selected browser must reject the comic keyboard defect.')
        print(' and '.join(b.title() for b in selected_browsers(ARGS)) + ' rejected: ' + signal, flush=True)
