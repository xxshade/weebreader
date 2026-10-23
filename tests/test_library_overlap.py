"""No two libraries read the same files.

`create_library` refused only the identical string, so `/lib/` beside
`/lib`, a folder inside another library's or one holding it were all
accepted -- and a file under both belonged to whichever library's scan or
watcher saw it first, the other showing that folder empty for good
(reproduced: scanning the inner library first left the outer one without
the file it contains).
"""
import os
import unittest
from pathlib import Path

from app.library.router import LIBRARY_CREATE_ERRORS, _overlap
from tests import test_dialogs


class Refused(unittest.IsolatedAsyncioTestCase):
    # test_dialogs' setup and helpers, not its tests (a module-level alias
    # or a subclass would collect those again here).
    asyncSetUp = test_dialogs.ANewLibraryIsRefusedInWords.asyncSetUp
    create = test_dialogs.ANewLibraryIsRefusedInWords.create
    libraries = test_dialogs.ANewLibraryIsRefusedInWords.libraries

    async def refused_with(self, path):
        response = await self.create(path=path)
        self.assertEqual(response.status_code, 422, path)
        return response.context["library_form"]["error"]

    async def test_the_same_folder_spelled_another_way_is_taken(self):
        for path in (f"{self.other}/", f"{self.other}/../{self.other.name}"):
            self.assertEqual(await self.refused_with(path), LIBRARY_CREATE_ERRORS["taken"], path)

    async def test_a_folder_inside_another_library_overlaps(self):
        inner = self.other / "Series"
        inner.mkdir()
        self.assertEqual(await self.refused_with(str(inner)), LIBRARY_CREATE_ERRORS["overlaps"])

    async def test_a_folder_holding_another_library_overlaps(self):
        self.assertEqual(await self.refused_with(str(self.other.parent)),
                         LIBRARY_CREATE_ERRORS["overlaps"])
        self.assertEqual(await self.libraries(), ["Taken"])

    async def test_a_symlink_to_another_library_is_that_library(self):
        link = Path(self._tmp.name) / "link"
        os.symlink(self.other, link)
        self.assertEqual(await self.refused_with(str(link)), LIBRARY_CREATE_ERRORS["taken"])

    async def test_a_library_stored_through_a_symlink_is_its_folder(self):
        # Roots were stored as typed before this; one typed as a symlink
        # still names the folder it points at.
        real = Path(self._tmp.name) / "real"
        real.mkdir()
        link = Path(self._tmp.name) / "via-link"
        os.symlink(real, link)
        from app.database import get_connection
        db = await get_connection()
        await db.execute("INSERT INTO libraries (name, root_path, media_type) VALUES ('Old', ?, 'comic')",
                         (str(link),))
        await db.commit()
        await db.close()
        self.assertEqual(await self.refused_with(str(real)), LIBRARY_CREATE_ERRORS["taken"])

    async def test_a_sibling_whose_name_starts_the_same_is_fine(self):
        # "/x/comics-old" is not inside "/x/comics": paths, not prefixes.
        sibling = Path(self._tmp.name) / f"{self.other.name}-old"
        sibling.mkdir()
        response = await self.create(path=str(sibling))
        self.assertEqual(response.status_code, 302)

    async def test_the_path_is_stored_resolved(self):
        await self.create(path=f"{self.folder}/")
        db_rows = await self._roots()
        self.assertIn(str(self.folder.resolve()), db_rows)

    async def _roots(self):
        from app.database import get_connection
        db = await get_connection()
        try:
            return [r[0] for r in await (await db.execute("SELECT root_path FROM libraries")).fetchall()]
        finally:
            await db.close()


class TheRule(unittest.TestCase):
    def test_an_older_unresolved_root_is_compared_resolved(self):
        self.assertEqual(_overlap("/srv/lib/a", ["/srv/lib/"]), "overlaps")
        self.assertIsNone(_overlap("/srv/libx", ["/srv/lib"]))


if __name__ == "__main__":
    unittest.main()
