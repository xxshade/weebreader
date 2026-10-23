"""Renaming or moving a file keeps what was read of it; a cover is never a
reason to half-import a file.

- An item used to BE its path: renaming "Serie 1.cbz" to "Serie 01.cbz"
  imported a new, unread item and deleted the old one with its progress,
  and renaming a series folder did that to every item and stranded the
  series' status on an empty row. The content (hash) is followed now.
- A cover Pillow cannot decode raised inside the import's transaction,
  which committed the item half-written and never parsed it again.
- A new version with no cover kept the old version's thumbnail.
"""
import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from app.config import settings
from app.database import close_pool, get_connection, init_db
from app.library import events, scanner


def png(color):
    buffer = io.BytesIO()
    Image.new("RGB", (40, 60), color).save(buffer, format="PNG")
    return buffer.getvalue()


def write_cbz(path: Path, pages=(b"",), cover=None, tag="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("000.png", cover if cover is not None else png((10, 20, 30)))
        z.writestr("zz-tag.txt", tag)  # different content, different hash


class Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        original = settings.data_dir
        settings.data_dir = tmp / "data"
        settings.data_dir.mkdir()
        (settings.data_dir / "covers").mkdir()
        self.addCleanup(lambda: setattr(settings, "data_dir", original))
        self.published = []
        original_publish = events.publish
        events.publish = self.published.append
        self.addCleanup(lambda: setattr(events, "publish", original_publish))
        await close_pool()
        self.addAsyncCleanup(close_pool)
        await init_db()
        self.root = tmp / "lib"
        self.root.mkdir()
        db = await get_connection()
        try:
            await db.execute("INSERT INTO users (id, username, password_hash) VALUES (1,'u','x')")
            await db.execute("INSERT INTO libraries (id, name, root_path, media_type) VALUES (1, 'L', ?, 'comic')",
                             (str(self.root),))
            await db.commit()
        finally:
            await db.close()

    async def scan(self):
        await scanner.scan_library(1, str(self.root))

    async def rows(self, sql, params=()):
        db = await get_connection()
        try:
            return [dict(r) for r in await (await db.execute(sql, params)).fetchall()]
        finally:
            await db.close()

    async def execute(self, sql, params=()):
        db = await get_connection()
        try:
            await db.execute(sql, params)
            await db.commit()
        finally:
            await db.close()


class RenamingKeepsTheItem(Case):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        write_cbz(self.root / "Serie" / "Serie 1.cbz", tag="1")
        write_cbz(self.root / "Serie" / "Serie 2.cbz", tag="2")
        await self.scan()
        self.items = {Path(r["file_path"]).name: r["id"] for r in await self.rows("SELECT * FROM items")}
        await self.execute("INSERT INTO reading_progress (user_id, item_id, status, progress_percent)"
                           " VALUES (1, ?, 'reading', 50)", (self.items["Serie 1.cbz"],))
        await self.execute("INSERT INTO series_status (user_id, series_id, status)"
                           " SELECT 1, id, 'reading' FROM series")

    async def test_a_renamed_file_is_the_same_item(self):
        (self.root / "Serie" / "Serie 1.cbz").rename(self.root / "Serie" / "Serie 01.cbz")
        await self.scan()
        rows = await self.rows("SELECT id, file_path FROM items ORDER BY id")
        self.assertEqual(len(rows), 2)
        renamed = [r for r in rows if r["file_path"].endswith("Serie 01.cbz")][0]
        self.assertEqual(renamed["id"], self.items["Serie 1.cbz"])
        progress = await self.rows("SELECT item_id, progress_percent FROM reading_progress")
        self.assertEqual(progress, [{"item_id": renamed["id"], "progress_percent": 50}])
        self.assertEqual(await self.rows("SELECT * FROM removed_items"), [])

    async def test_a_renamed_folder_takes_the_series_with_it(self):
        series_before = (await self.rows("SELECT id FROM series"))[0]["id"]
        (self.root / "Serie").rename(self.root / "Serie (2020)")
        await self.scan()
        series = await self.rows("SELECT id, name, folder_path FROM series")
        self.assertEqual(series, [{"id": series_before, "name": "Serie (2020)", "folder_path": "Serie (2020)"}])
        status = await self.rows("SELECT series_id, status FROM series_status")
        self.assertEqual(status, [{"series_id": series_before, "status": "reading"}])
        self.assertEqual(len(await self.rows("SELECT * FROM reading_progress")), 1)
        self.assertEqual({r["series_id"] for r in await self.rows("SELECT series_id FROM items")}, {series_before})

    async def test_a_name_the_reader_typed_survives_the_folder_rename(self):
        await self.execute("UPDATE series SET name = 'My Name'")
        (self.root / "Serie").rename(self.root / "Serie (2020)")
        await self.scan()
        self.assertEqual([r["name"] for r in await self.rows("SELECT name FROM series")], ["My Name"])

    async def test_a_file_moved_to_another_series_goes_with_its_progress(self):
        target = self.root / "Other" / "Serie 1.cbz"
        target.parent.mkdir()
        (self.root / "Serie" / "Serie 1.cbz").rename(target)
        await self.scan()
        rows = await self.rows("SELECT items.id, series.folder_path FROM items JOIN series ON series.id = items.series_id")
        moved = [r for r in rows if r["id"] == self.items["Serie 1.cbz"]]
        self.assertEqual(moved, [{"id": self.items["Serie 1.cbz"], "folder_path": "Other"}])
        self.assertEqual(len(await self.rows("SELECT * FROM reading_progress")), 1)

    async def test_a_copy_is_a_new_item(self):
        shutil.copy(self.root / "Serie" / "Serie 1.cbz", self.root / "Serie" / "Serie 1 copy.cbz")
        await self.scan()
        self.assertEqual(len(await self.rows("SELECT * FROM items")), 3)
        progress = await self.rows("SELECT item_id FROM reading_progress")
        self.assertEqual(progress, [{"item_id": self.items["Serie 1.cbz"]}])

    async def test_the_watcher_path_moves_too(self):
        # What the watcher does with a rename: the new path, then the old
        # one forgotten if it is still gone (watcher._fire_move).
        old = self.root / "Serie" / "Serie 1.cbz"
        new = self.root / "Serie" / "Serie 01.cbz"
        old.rename(new)
        await scanner.process_file(1, str(self.root), new)
        await scanner.mark_missing(1, str(old))
        self.assertEqual(len(await self.rows("SELECT * FROM reading_progress")), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM items")), 2)


class Covers(Case):
    async def cover_of(self, name):
        rows = await self.rows("SELECT cover_path FROM items WHERE file_path LIKE ?", (f"%{name}",))
        return rows[0]["cover_path"] if rows else "no row"

    async def test_an_undecodable_cover_imports_the_item_without_one(self):
        write_cbz(self.root / "S" / "Bad.cbz", cover=b"not an image at all")
        await self.scan()
        self.assertIsNone(await self.cover_of("Bad.cbz"))
        self.assertEqual(await self.rows("SELECT * FROM scan_events WHERE event_type = 'error'"), [])
        self.assertIn("item_added", [e["type"] for e in self.published])

    async def test_a_new_version_with_no_cover_drops_the_old_one(self):
        path = self.root / "S" / "V.cbz"
        write_cbz(path, tag="one")
        await self.scan()
        old_cover = await self.cover_of("V.cbz")
        self.assertTrue(old_cover)
        write_cbz(path, cover=b"broken", tag="two")
        await self.scan()
        self.assertIsNone(await self.cover_of("V.cbz"))
        self.assertFalse((settings.covers_dir / old_cover).exists())
        self.assertEqual([r["cover_path"] for r in await self.rows("SELECT cover_path FROM series")], [None])

    async def test_a_failure_inside_the_import_keeps_nothing(self):
        write_cbz(self.root / "S" / "V.cbz")
        original = scanner._renumber_series

        async def boom(db, series_id):
            raise RuntimeError("renumbering failed")

        scanner._renumber_series = boom
        self.addCleanup(lambda: setattr(scanner, "_renumber_series", original))
        with self.assertLogs(scanner.logger, level='ERROR') as logs:
            await scanner.process_file(1, str(self.root), self.root / "S" / "V.cbz")
        self.assertEqual(len(logs.records), 1)
        self.assertIn('RuntimeError: renumbering failed', logs.output[0])
        self.assertEqual(await self.rows("SELECT * FROM items"), [])
        errors = await self.rows("SELECT detail FROM scan_events WHERE event_type = 'error'")
        self.assertEqual([e["detail"] for e in errors], ["renumbering failed"])


class WhatTheWalkSkips(Case):
    async def test_hidden_folders_and_recycle_bins_are_not_series(self):
        write_cbz(self.root / "Series" / "S 1.cbz", tag="keep")
        for folder in (".Trash-1000/files/Old", ".stversions/Series", "#recycle/Gone", "@eaDir/S"):
            write_cbz(self.root / folder / "x.cbz", tag=folder)
        await self.scan()
        self.assertEqual([r["folder_path"] for r in await self.rows("SELECT folder_path FROM series")],
                         ["Series"])

    async def test_a_volume_moved_into_the_trash_inside_the_library_leaves_it(self):
        # What "delete" is on a USB drive: a move into .Trash-1000.
        write_cbz(self.root / "Series" / "S 1.cbz", tag="1")
        write_cbz(self.root / "Series" / "S 2.cbz", tag="2")
        await self.scan()
        trash = self.root / ".Trash-1000" / "files"
        trash.mkdir(parents=True)
        (self.root / "Series" / "S 1.cbz").rename(trash / "S 1.cbz")
        await self.scan()
        self.assertEqual([Path(r["file_path"]).name for r in await self.rows("SELECT file_path FROM items")],
                         ["S 2.cbz"])

    async def test_a_link_back_up_the_tree_is_walked_once(self):
        write_cbz(self.root / "Series" / "S 1.cbz")
        (self.root / "Series" / "up").symlink_to("..")
        await self.scan()
        self.assertEqual(len(await self.rows("SELECT * FROM items")), 1)


class FolderErrors(Case):
    async def test_an_unlistable_folder_is_one_error_and_it_clears(self):
        # Root ignores permissions, so the walk's report of an unlistable
        # folder is what is simulated: the same OSError os.walk hands its
        # onerror for a folder it may not enter.
        write_cbz(self.root / "Ok" / "o.cbz")
        locked = self.root / "Locked"
        original = scanner._walk

        def walk(root):
            found, errors = original(root)
            if self.locked:
                errors.append(PermissionError(13, "Permission denied", str(locked)))
            return found, errors

        scanner._walk = walk
        self.addCleanup(lambda: setattr(scanner, "_walk", original))
        self.locked = True
        for _ in range(3):
            await self.scan()
        errors = await self.rows("SELECT file_path FROM scan_events WHERE event_type = 'error'")
        self.assertEqual(errors, [{"file_path": str(locked)}])
        self.locked = False
        await self.scan()
        self.assertEqual(await self.rows("SELECT * FROM scan_events WHERE event_type = 'error'"), [])


class AnUnchangedRescan(Case):
    async def test_does_not_look_at_unchanged_files_one_by_one(self):
        # Decided for the whole library in one query and one pass of stats:
        # per file it was a connection, two queries and a thread hop each
        # (7.9 s for 8,500 unchanged files; 0.98 s now).
        for n in range(5):
            write_cbz(self.root / "S" / f"S {n}.cbz", tag=str(n))
        await self.scan()
        calls = []
        original = scanner.process_file

        async def counting(*args):
            calls.append(args[2])
            return await original(*args)

        scanner.process_file = counting
        self.addCleanup(lambda: setattr(scanner, "process_file", original))
        write_cbz(self.root / "S" / "S 0.cbz", tag="changed")
        await self.scan()
        self.assertEqual([p.name for p in calls], ["S 0.cbz"])
