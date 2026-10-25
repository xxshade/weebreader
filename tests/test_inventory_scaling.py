"""Cold chapter summaries must not rescan the entire cache on every write."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.stats import text_inventory as inventory
from app.config import settings


class CacheScaling(unittest.TestCase):
    def test_many_chapter_writes_amortize_directory_scans(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'text-inventories'
            original = Path.iterdir
            scans = []
            def listing(path):
                if path == root:
                    scans.append(path)
                return original(path)
            with patch.object(settings, 'data_dir', Path(directory)), patch.object(Path, 'iterdir', listing):
                for n in range(100):
                    self.assertEqual(inventory.inventory_html_total(f'<p>Chapter {n}</p>'), 2)
            self.assertLessEqual(len(scans), 3, 'Cold chapter writes must not repeatedly scan every cached file')
