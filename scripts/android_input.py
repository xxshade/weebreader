"""Native Android input calibrated against a visible DOM/accessibility anchor.

This adapter is tested without an emulator. Browser regressions still have to
verify the resulting interaction on actual Android; command delivery is no
evidence that a page moved or a panel opened.
"""
import json
import re
import time
from xml.etree import ElementTree


class NativeInput:
    def __init__(self, driver, adb, output):
        self.driver, self.adb, self.output = driver, adb, output

    def anchor(self, resource_id):
        deadline = time.monotonic() + 15
        while True:
            self.adb('shell', 'rm', '-f', '/sdcard/header-native.xml')
            dump_error = None
            try:
                self.adb('shell', 'uiautomator', 'dump', '/sdcard/header-native.xml')
            except RuntimeError as error:
                # Android can report a nonzero exit after writing the dump.
                # The removed device file, not a previous local snapshot,
                # must provide a fresh, valid hierarchy before any input.
                dump_error = str(error)
            try:
                xml = self.adb('shell', 'cat', '/sdcard/header-native.xml')
                hierarchy = ElementTree.fromstring(xml)
                if hierarchy.tag != 'hierarchy':
                    raise ValueError('Unexpected native capture root')
            except (RuntimeError, ElementTree.ParseError, ValueError) as error:
                raise ValueError('Could not read a fresh native hierarchy: '
                                 + str(error) + ('; ' + dump_error if dump_error else '')) from error
            (self.output / 'header-native.xml').write_text(xml)
            if dump_error:
                self.record({'hierarchyExitWarning': dump_error, 'freshHierarchyValidated': True})
            nodes = [node for node in hierarchy.iter('node')
                     if node.get('resource-id') == resource_id]
            if nodes or time.monotonic() >= deadline:
                break
            time.sleep(.25)
        if len(nodes) != 1:
            raise ValueError(f'{resource_id}: expected one native coordinate anchor, found {len(nodes)}')
        bounds = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', nodes[0].get('bounds', ''))
        if not bounds:
            raise ValueError('Native anchor bounds are unavailable')
        left, top, right, bottom = map(int, bounds.groups())
        if right <= left or bottom <= top:
            raise ValueError('Native anchor bounds are empty')
        return ((left + right) / 2, (top + bottom) / 2)

    def points(self, resource_id, *, selector=None, points=None):
        native_x, native_y = self.anchor(resource_id)
        view = self.driver.execute_script("""const anchor=document.getElementById(arguments[0]);
            const target=arguments[1] ? document.querySelector(arguments[1]) : null;
            const rect=target?.getBoundingClientRect();
            const x=rect && rect.x+rect.width/2, y=rect && rect.y+rect.height/2;
            const hit=rect && document.elementFromPoint(x,y);
            return {anchor:anchor.getBoundingClientRect().toJSON(),
                target:rect?.toJSON(), hit:!!target && (hit===target || target.contains(hit)),
                hitElement:hit && {tag:hit.tagName,id:hit.id},
                frame:target?.tagName==='IFRAME' ? {url:target.contentDocument?.URL,
                    ready:target.contentDocument?.readyState,
                    textLength:target.contentDocument?.body?.textContent?.length} : null,
                scale:devicePixelRatio*(visualViewport?.scale || 1),
                visible:document.visibilityState==='visible',width:innerWidth,height:innerHeight};
            """, resource_id, selector)
        self.record({'anchor': resource_id, 'nativeCentre': [native_x, native_y], 'view': view})
        if not view['visible']:
            raise ValueError('Native input requires the application tab to be visible')
        if selector:
            rect = view['target']
            if not rect or rect['width'] <= 0 or rect['height'] <= 0 or not view['hit']:
                raise ValueError(f'Native input target is hidden or covered: {selector}')
            points = [(rect['x'] + rect['width'] / 2, rect['y'] + rect['height'] / 2)]
        scale, anchor = view['scale'], view['anchor']
        if scale <= 0:
            raise ValueError('Browser pixel scale must be positive')
        result = []
        for x, y in points:
            if not (0 <= x < view['width'] and 0 <= y < view['height']):
                raise ValueError('Native input target is outside the application viewport')
            result.append((round(native_x + (x - anchor['x'] - anchor['width'] / 2) * scale),
                           round(native_y + (y - anchor['y'] - anchor['height'] / 2) * scale)))
        return result

    def tap(self, selector, resource_id='btn-settings'):
        [(x, y)] = self.points(resource_id, selector=selector)
        self.adb('shell', 'input', 'tap', str(x), str(y))
        self.record({'selector': selector, 'point': [x, y]})

    def swipe(self, start, end, duration):
        points = self.points('btn-settings', points=[start, end])
        self.adb('shell', 'input', 'swipe', *map(str, (*points[0], *points[1], duration)))
        self.record({'start': points[0], 'end': points[1], 'duration': duration})

    def record(self, value):
        with (self.output / 'native-input.jsonl').open('a') as stream:
            stream.write(json.dumps(value) + '\n')
