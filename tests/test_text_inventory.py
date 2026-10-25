"""Remaining words follow stable block/UTF-16 anchors, never byte progress."""
import json
import gzip
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from lxml import html

from app.stats.text_inventory import inventory_epub, inventory_epub_total, inventory_html, inventory_html_total


ROOT = Path(__file__).resolve().parent.parent


class TextInventory(unittest.TestCase):
    def test_template_content_does_not_occupy_saved_source_offsets(self):
        inventory = inventory_html('<p>first<template>hidden template words</template> second</p>')
        self.assertEqual(inventory.total_words, 2)
        self.assertEqual(inventory.sections[0][0].positions, (0, 6))
        self.assertEqual(inventory.sections[0][0].length, 12)
        self.assertEqual(inventory.remaining(0, 0, 6), 1)

    def test_nonreading_subtrees_preserve_saved_source_offset_gaps(self):
        inventory = inventory_html('<p>First <svg><text>caption</text></svg> second third</p>')
        self.assertEqual(inventory.total_words, 3)
        self.assertEqual(inventory.sections[0][0].positions, (0, 14, 21))
        self.assertEqual(inventory.sections[0][0].length, 26)
        self.assertEqual(inventory.remaining(0, 0, 14), 2)
        inventory = inventory_html('<p>One <math><mi>😀 hidden</mi></math><script>long script</script> next</p>')
        self.assertEqual(inventory.total_words, 2)
        self.assertEqual(inventory.remaining(0, 0, 25), 1)
        self.assertEqual(inventory.sections[0][0].positions, (0, 25))

    def test_entities_combining_marks_and_han_keep_source_coordinates(self):
        inventory = inventory_html('<p>Caf&eacute; e&#x301;lan don&rsquo;t 你好 😀 next</p>')
        self.assertEqual(inventory.total_words, 6)
        self.assertEqual(inventory.sections[0][0].positions, (0, 5, 11, 17, 18, 23))
        self.assertEqual(inventory.remaining(0, 0, 23), 1)

    def test_total_only_disk_hit_does_not_load_offsets_or_parse_source(self):
        from app.config import settings
        from app.stats import text_inventory
        with tempfile.TemporaryDirectory() as directory, patch.object(settings, 'data_dir', Path(directory)):
            source = '<p>Several measured readable words</p>'
            self.assertEqual(inventory_html_total(source), 4)
            text_inventory._totals.clear()
            text_inventory._cache.clear()
            text_inventory._cache_units = 0
            with patch.object(text_inventory, '_read_disk', side_effect=AssertionError('Offset inventory loaded')), \
                    patch.object(text_inventory, '_section', side_effect=AssertionError('Source parsed')):
                self.assertEqual(inventory_html_total(source), 4)
            summary = next((Path(directory) / 'text-inventories').glob('*.total.json'))
            value = json.loads(summary.read_text())
            value['words'] = 99  # A syntactically valid but corrupted count.
            summary.write_text(json.dumps(value))
            text_inventory._totals.clear()
            self.assertEqual(inventory_html_total(source), 4)
            self.assertEqual(json.loads(summary.read_text())['words'], 4)

    def test_persistent_cache_quota_prunes_old_source_versions_as_pairs(self):
        from app.config import settings
        from app.stats import text_inventory
        with tempfile.TemporaryDirectory() as directory, patch.object(settings, 'data_dir', Path(directory)), \
                patch.object(text_inventory, '_MAX_CACHE_DISK_BYTES', 350):
            for number in range(4):
                inventory_html(f'<p>Version {number} has words</p>')
            files = list((Path(directory) / 'text-inventories').iterdir())
            self.assertLessEqual(sum(path.stat().st_size for path in files), 350)
            self.assertLess(len(files), 8)
            self.assertEqual({path.name[:64] for path in files if path.name.endswith('.json.gz')},
                             {path.name[:64] for path in files if path.name.endswith('.total.json')})

    def test_persistent_cache_survives_memory_eviction_and_rejects_invalid_offsets(self):
        from app.config import settings
        from app.stats import text_inventory

        def evict():
            text_inventory._cache.clear()
            text_inventory._cache_units = 0

        with tempfile.TemporaryDirectory() as directory, patch.object(settings, 'data_dir', Path(directory)):
            source = '<p>Persisted exact word offsets</p>'
            expected = inventory_html(source)
            files = list((Path(directory) / 'text-inventories').glob('*.json.gz'))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].suffixes, ['.json', '.gz'])
            self.assertNotIn('Persisted exact word offsets', gzip.decompress(files[0].read_bytes()).decode())
            evict()
            with patch.object(text_inventory, '_section', side_effect=AssertionError('Source parsed despite persistent cache')):
                self.assertEqual(inventory_html(source), expected)
            files[0].write_bytes(gzip.compress(json.dumps({'format': text_inventory._CACHE_VERSION,
                                                         'sections': [[[3, [-1, 999]]]]}).encode()))
            evict()
            with patch.object(text_inventory, '_section', wraps=text_inventory._section) as parse:
                self.assertEqual(inventory_html(source), expected)
                parse.assert_called_once()
            # A truncated compressed file is disposable too.
            files[0].write_bytes(b'not gzip')
            evict()
            self.assertEqual(inventory_html(source), expected)

    def test_remaining_uses_the_current_word_boundary_and_utf16_offset(self):
        inventory = inventory_html('<p>😀 alpha beta</p><p>Final words</p>')
        self.assertEqual(inventory.total_words, 4)
        self.assertEqual(inventory.sections[0][0].positions, (3, 9))
        self.assertEqual(inventory.remaining(0, 0, 3), 4)
        self.assertEqual(inventory.remaining(0, 0, 4), 3)
        self.assertEqual(inventory.remaining(0, 0, 9), 3)
        self.assertEqual(inventory.remaining(0, 1, 0), 2)
        self.assertEqual(inventory.remaining(0, 1, 11), 0)
        self.assertEqual(inventory.remaining(0, 2, 0), 0)

    def test_invalid_or_legacy_anchors_cannot_fabricate_remaining_words(self):
        inventory = inventory_html('<p>First words</p>')
        for anchor in ((-1, 0, 0), (1, 0, 0), (0, 3, 0), (0, 0, 99), (0, 1, 2),
                       (0, 0, 'epubcfi(...)'), (False, 0, 0)):
            with self.subTest(anchor=anchor):
                self.assertIsNone(inventory.remaining(*anchor))

    def test_formatting_bytes_and_nonreading_bodies_do_not_inflate_words(self):
        source = '''<html><head><style>several hidden words</style></head><body>
          <p title="more metadata">First <em>two</em> words<script>never count these</script></p>
          <figure><img src="data:image/png;base64,AAAAAA"/><figcaption>A caption</figcaption></figure>
          <svg><text>not reader text</text></svg><p>Last</p></body></html>'''
        inventory = inventory_html(source)
        self.assertEqual(inventory.total_words, 6)
        self.assertEqual(len(inventory.sections[0]), 4)
        self.assertEqual(inventory.remaining(0, 1, 0), 3)

    def test_replacements_apply_in_order_without_editing_the_source(self):
        source = '<p>cat and dog</p><style>cat</style>'
        original = inventory_html(source)
        rules = [{'find': 'cat', 'replace': 'small cat'}, {'find': 'small', 'replace': 'very small'}]
        replaced = inventory_html(source, rules)
        self.assertEqual(original.total_words, 3)
        self.assertEqual(replaced.total_words, 5)
        self.assertIs(original, inventory_html(source))
        self.assertIs(replaced, inventory_html(source, rules))
        self.assertIsNot(replaced, inventory_html(source.replace('dog', 'two dogs'), rules))

    def test_soft_hyphens_keep_one_word_and_preserve_original_offsets(self):
        inventory = inventory_html('<p>inter\u00adnational next</p>')
        self.assertEqual(inventory.total_words, 2)
        self.assertEqual(inventory.sections[0][0].positions, (0, 15))
        self.assertEqual(inventory.remaining(0, 0, 8), 1)
        self.assertEqual(inventory.remaining(0, 0, 15), 1)

    def test_block_numbering_matches_the_actual_reader_walker(self):
        # Exercise the actual JS collector on the same DOM, including loose
        # text, nested list paragraphs, whole tables, empty/media blocks and
        # ignored nodes. This detects a drift of either walker's block rules.
        source = '''<body>lead <b>inline</b><div><p>One <em>two</em></p>tail</div>
          <p>first<template>hidden template words</template> second</p>
          <ul><li><p>three</p><p>four</p></li><li>five</li></ul>
          <table><tr><td>six</td><td>seven</td></tr></table><hr/>
          <div><img src="x"/></div><p></p><details><summary>eight</summary>nine</details>
          <style>hidden</style><noscript>hidden</noscript><!--quiet-->end</body>'''
        doc = html.document_fromstring(source)
        body = doc.find('body')

        def nodes(element):
            result = []
            if element.text:
                result.append({'nodeType': 3, 'data': element.text})
            for child in element:
                if isinstance(child.tag, str):
                    # Browser templates keep descendants in .content, outside
                    # childNodes. Preserve that DOM distinction in the fixture.
                    result.append({'nodeType': 1, 'localName': child.tag,
                                   'childNodes': [] if child.tag == 'template' else nodes(child)})
                else:
                    result.append({'nodeType': 8})
                if child.tail:
                    result.append({'nodeType': 3, 'data': child.tail})
            return result

        program = '''const fs=require('fs');global.window={};
          eval(fs.readFileSync('app/static/js/reader-blocks.js','utf8'));
          const body=JSON.parse(fs.readFileSync(0,'utf8'));
          function wire(n){n.children=(n.childNodes||[]).filter(c=>c.nodeType===1);(n.childNodes||[]).forEach(wire)}
          wire(body);
          const skip=new Set('script style noscript template svg math video audio canvas iframe object embed'.split(' '));
          function text(n){return n.nodeType===3?n.data:n.nodeType!==1||skip.has(n.localName)?'':n.childNodes.map(text).join('')}
          console.log(JSON.stringify(window.readerBlocks.collect(body).map(b=>b.nodes.map(text).join(''))));'''
        result = subprocess.run(['node', '-e', program], input=json.dumps({'nodeType': 1,
            'localName': 'body', 'childNodes': nodes(body)}), cwd=ROOT,
            capture_output=True, text=True, check=True)
        expected = json.loads(result.stdout)
        inventory = inventory_html(source)
        self.assertEqual([block.length for block in inventory.sections[0]],
                         [len(text.encode('utf-16-le')) // 2 for text in expected])
        # Distinguish equal-size blocks carrying different word boundaries.
        for index, text in enumerate(expected):
            self.assertEqual(inventory.sections[0][index], inventory_html('<p>' + text + '</p>').sections[0][0])

    def test_epub_spine_order_and_content_changes_invalidate_cached_words(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'book.epub'

            def write(last):
                with zipfile.ZipFile(path, 'w') as archive:
                    archive.writestr('META-INF/container.xml', '''<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>
                      <rootfile full-path="content.opf"/></rootfiles></container>''')
                    archive.writestr('content.opf', '''<package xmlns="http://www.idpf.org/2007/opf">
                      <manifest><item id="a" href="a.xhtml"/><item id="b" href="b.xhtml"/></manifest>
                      <spine><itemref idref="b"/><itemref idref="a"/></spine></package>''')
                    archive.writestr('a.xhtml', '<p>' + last + '</p>')
                    archive.writestr('b.xhtml', '<p>Earlier chapter</p>')

            write('Final')
            first = inventory_epub(path, 'v1')
            self.assertEqual(first.total_words, 3)
            self.assertEqual(first.remaining(0, 0, 8), 2)
            self.assertEqual(first.remaining(1, 0, 0), 1)
            self.assertIs(first, inventory_epub(path, 'v1'))
            self.assertEqual(inventory_epub_total(path, 'v1'), 3)
            write('Final two words')
            second = inventory_epub(path, 'v2')
            self.assertEqual(second.total_words, 5)
            self.assertIsNot(first, second)
            self.assertEqual(inventory_epub_total(path, 'v2'), 5)


if __name__ == '__main__':
    unittest.main()
