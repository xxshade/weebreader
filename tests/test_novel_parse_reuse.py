"""A chapter list reuses the parsed page without losing its scope."""
import unittest
from unittest.mock import patch
from bs4 import BeautifulSoup
from app.novels import scraper

class ChapterListReuse(unittest.TestCase):
    def test_page_uses_one_parse_and_keeps_nested_duplicate_id_chapters(self):
        html = ('<html><h1>Book</h1><div id="episodeNime"><ul>'
                '<li><a href="https://x/n-v1c1.html">First</a></li>'
                '<li><div id="episodeNime"><a href="https://x/n-v1c2.html">Second</a></div></li>'
                '<li><a href="https://x/n-v1c3.html">Third</a></li>'
                '</ul></div></html>')
        with patch.object(scraper, 'BeautifulSoup', wraps=BeautifulSoup) as parser:
            novel = scraper._parse_novel_page(html, 'https://x/book')
        self.assertEqual([chapter.title for chapter in novel.chapters], ['First', 'Second', 'Third'])
        self.assertEqual(parser.call_count, 1, 'A parsed chapter list must not be parsed again')

    def test_ancestor_outside_list_does_not_change_chapter_title(self):
        html = ('<h1>Book</h1><ul><li>Unrelated outside text'
                '<div id="episodeNime"><a href="https://x/n-v1c1.html">First</a></div>'
                '</li></ul>')
        novel = scraper._parse_novel_page(html, 'https://x/book')
        self.assertEqual(novel.chapters[0].title, 'First')

    def test_string_and_parsed_container_produce_the_same_results(self):
        html = ('<div id="episodeNime"><a href="https://x/n-v1c1.html">First</a>'
                '<a href="https://x/n-v1c1.html">Duplicate</a>'
                '<a href="https://x/n-v1c2.html">Second</a></div>')
        parsed = BeautifulSoup(html, 'lxml').select_one('#episodeNime')
        self.assertEqual(scraper._parse_chapter_list_html(parsed), scraper._parse_chapter_list_html(html))
