"""Fast checks for the automation boundary, not Android browser evidence."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from scripts.android_input import NativeInput


class NativeAndroidInput(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.driver = Mock()
        self.view = {'anchor': {'x': 300, 'y': 10, 'width': 33.5, 'height': 24},
                     'target': {'x': 200, 'y': 490, 'width': 20, 'height': 20},
                     'scale': 2.625, 'visible': True, 'hit': True, 'width': 414, 'height': 623}
        self.driver.execute_script.side_effect = lambda *args: copy.deepcopy(self.view)
        self.xml = '<hierarchy><node resource-id="btn-settings" bounds="[784,145][879,211]"/></hierarchy>'
        self.adb = Mock(side_effect=lambda *args: self.xml if args[1] == 'cat' else '')
        self.input = NativeInput(self.driver, self.adb, Path(directory.name))

    def commands(self):
        return [call.args for call in self.adb.call_args_list if call.args[1] == 'input']

    def test_padded_accessibility_bounds_do_not_distort_taps_or_swipes(self):
        # Known CSS viewport origin (0,120), pixel ratio 2.625. Android
        # enlarged the button's native hit area; its centre is rounded by
        # less than half a physical pixel. Size-ratio scaling misses the
        # lower target by roughly 100 physical pixels.
        self.input.tap('#target')
        self.input.swipe((210, 500), (210, 300), 700)
        self.assertEqual(self.commands(), [('shell', 'input', 'tap', '551', '1433'),
            ('shell', 'input', 'swipe', '551', '1433', '551', '908', '700')])

    def test_background_tabs_never_receive_native_input(self):
        self.view['visible'] = False
        with self.assertRaisesRegex(ValueError, 'tab to be visible'):
            self.input.tap('#target')
        self.assertEqual(self.commands(), [])

    def test_covered_controls_never_receive_native_input(self):
        self.view['hit'] = False
        with self.assertRaisesRegex(ValueError, 'hidden or covered'):
            self.input.tap('#target')
        self.assertEqual(self.commands(), [])

    def test_coordinates_outside_the_application_do_not_touch_browser_chrome(self):
        with self.assertRaisesRegex(ValueError, 'outside the application viewport'):
            self.input.swipe((210, 500), (210, 700), 700)
        self.assertEqual(self.commands(), [])

    def test_ambiguous_native_anchors_are_rejected(self):
        self.xml = '<hierarchy>' + '<node resource-id="btn-settings" bounds="[784,145][879,211]"/>' * 2 + '</hierarchy>'
        with self.assertRaisesRegex(ValueError, 'found 2'):
            self.input.tap('#target')
        self.assertEqual(self.commands(), [])

    def test_dump_exit_error_does_not_discard_a_fresh_valid_hierarchy(self):
        def capture(*args):
            if args[1] == 'uiautomator':
                raise RuntimeError('ADB dump: UI hierchary dumped to: /sdcard/header-native.xml')
            return self.xml if args[1] == 'cat' else ''
        self.adb.side_effect = capture
        try:
            self.input.tap('#target')
        except RuntimeError as error:
            self.fail('A fresh valid hierarchy must survive a dump exit error: ' + str(error))
        self.assertEqual(self.commands(), [('shell', 'input', 'tap', '551', '1433')])

    def test_a_failed_dump_cannot_reuse_the_previous_local_capture(self):
        (self.input.output / 'header-native.xml').write_text(self.xml)
        def capture(*args):
            if args[1] == 'uiautomator':
                raise RuntimeError('ADB dump failed')
            if args[1] == 'cat':
                raise RuntimeError('No such file')
            return ''
        self.adb.side_effect = capture
        with self.assertRaises((RuntimeError, ValueError)):
            self.input.tap('#target')
        self.assertEqual(self.commands(), [])

    def test_a_fresh_file_must_be_an_android_hierarchy_before_input(self):
        self.xml = '<unrelated><node resource-id="btn-settings" bounds="[784,145][879,211]"/></unrelated>'
        with self.assertRaisesRegex(ValueError, 'fresh native hierarchy'):
            self.input.tap('#target')
        self.assertEqual(self.commands(), [])
