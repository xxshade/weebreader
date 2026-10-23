"""Chapter cleanup preserves illustrations and stable paragraph anchors."""
import unittest

from bs4 import BeautifulSoup

from app.novels.scraper import tag_paragraphs


class ChapterContent(unittest.TestCase):
    def test_an_illustration_is_content_but_a_blank_line_is_not(self):
        original = '<p>Before</p><p><img src="cover.png" alt="Illustration"></p><p><br></p><p>After</p>'
        cleaned = tag_paragraphs(original)
        soup = BeautifulSoup(cleaned, 'lxml')
        self.assertIsNotNone(soup.find('img', src='cover.png'))
        self.assertEqual([p.get('data-p') for p in soup.find_all('p')], ['0', '1', '2'])
        self.assertIsNone(soup.find('br'))
        self.assertEqual(tag_paragraphs(cleaned), cleaned)
