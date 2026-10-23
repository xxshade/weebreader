"""Reading a file the library found on disk: is it an archive, is it all
there yet, and in what order are its pages.

The cases here are the ones that produced real, reported failures: a RAR
saved with a .cbz extension, a file read before it finished arriving, and
CBZ pages served in the wrong order.
"""
import asyncio
import io
import tempfile
import unittest
import zipfile
from unittest import mock
from pathlib import Path

from app.library.parsers.cbz import page_names_from, parse_cbz
from app.library.scanner import (
    UnknownArchiveFormat,
    _check_readable_archive,
    _detect_archive_kind,
    _folder_has_eligible_file,
    _is_eligible,
    _path_exists,
    _readable_archive_when_settled,
)

PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
    b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01"
    b"\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)


def write_cbz(path: Path, names: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, PNG_1PX)


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class PageOrdering(TempDirCase):
    def test_unpadded_page_names_are_ordered_naturally(self):
        # The reported bug: sorted() puts "10.jpg" straight after "1.jpg",
        # so page 2 of the reader served page 10 for the whole volume.
        names = [f"{i}.jpg" for i in range(1, 13)]
        self.assertEqual(page_names_from(list(reversed(names))), names)

    def test_zero_padded_archives_are_unaffected(self):
        names = [f"{i:03d}.jpg" for i in range(1, 13)]
        self.assertEqual(page_names_from(list(reversed(names))), names)

    def test_directories_and_macosx_junk_are_dropped(self):
        namelist = ["pages/", "__MACOSX/pages/._1.jpg", "pages/1.jpg", "pages/2.jpg"]
        self.assertEqual(page_names_from(namelist), ["pages/1.jpg", "pages/2.jpg"])

    def test_non_image_entries_are_dropped(self):
        namelist = ["ComicInfo.xml", "readme.txt", "1.jpg"]
        self.assertEqual(page_names_from(namelist), ["1.jpg"])

    def test_parse_cbz_counts_only_pages(self):
        cbz = self.tmp / "v01.cbz"
        write_cbz(cbz, ["ComicInfo.xml", "1.jpg", "2.jpg", "10.jpg"])
        self.assertEqual(parse_cbz(str(cbz)).page_count, 3)


class ArchiveDetection(TempDirCase):
    def test_zip_is_detected_from_its_magic_bytes(self):
        p = self.tmp / "v01.cbz"
        write_cbz(p, ["1.jpg"])
        self.assertEqual(_detect_archive_kind(p), "zip")

    def test_an_empty_zip_is_still_a_zip(self):
        # An archive with no entries starts with the end-of-central-directory
        # signature (PK\x05\x06) rather than a local file header.
        p = self.tmp / "empty.cbz"
        with zipfile.ZipFile(p, "w"):
            pass
        self.assertEqual(_detect_archive_kind(p), "zip")

    def test_rar_is_detected_even_behind_a_cbz_extension(self):
        # The reported case: manga scans renamed rather than repackaged.
        p = self.tmp / "actually-a-rar.cbz"
        p.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 64)
        self.assertEqual(_detect_archive_kind(p), "rar")

    def test_anything_else_is_unknown(self):
        p = self.tmp / "mystery.cbz"
        p.write_bytes(b"\x00" * 64)
        self.assertEqual(_detect_archive_kind(p), "unknown")

    def test_a_file_shorter_than_the_signature_does_not_crash(self):
        p = self.tmp / "tiny.cbz"
        p.write_bytes(b"PK")
        self.assertEqual(_detect_archive_kind(p), "unknown")


class ArchiveValidation(TempDirCase):
    def test_a_real_zip_passes(self):
        p = self.tmp / "v01.cbz"
        write_cbz(p, ["1.jpg"])
        _check_readable_archive(p)  # must not raise

    def test_a_rar_is_permanent_not_transient(self):
        p = self.tmp / "v01.cbz"
        p.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 64)
        with self.assertRaises(UnknownArchiveFormat) as caught:
            _check_readable_archive(p)
        self.assertFalse(caught.exception.transient)

    def test_a_zero_filled_preallocated_file_is_transient(self):
        # What a torrent client's pre-allocation looks like: full size, no
        # content yet, so the magic bytes aren't there.
        p = self.tmp / "arriving.cbz"
        p.write_bytes(b"\x00" * 4096)
        with self.assertRaises(UnknownArchiveFormat) as caught:
            _check_readable_archive(p)
        self.assertTrue(caught.exception.transient)

    def test_an_empty_file_is_transient(self):
        p = self.tmp / "arriving.cbz"
        p.write_bytes(b"")
        with self.assertRaises(UnknownArchiveFormat) as caught:
            _check_readable_archive(p)
        self.assertTrue(caught.exception.transient)

    def test_a_truncated_zip_is_transient(self):
        # Starts like a zip, so detection passes -- but the central
        # directory is written LAST, so opening it is what proves the copy
        # finished. This is the half-copied .epub from the real report.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for i in range(1, 6):
                z.writestr(f"{i}.jpg", PNG_1PX)
        whole = buf.getvalue()
        p = self.tmp / "half.epub"
        p.write_bytes(whole[: len(whole) // 2])
        with self.assertRaises(UnknownArchiveFormat) as caught:
            _check_readable_archive(p)
        self.assertTrue(caught.exception.transient)


class SettlingRetries(unittest.IsolatedAsyncioTestCase):
    """A file that isn't a valid archive YET is re-checked before being
    called broken; one that never will be is reported immediately."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    async def test_a_file_that_completes_mid_wait_is_imported(self):
        p = self.tmp / "arriving.cbz"
        p.write_bytes(b"\x00" * 4096)

        async def finish_copy():
            await asyncio.sleep(0.05)
            write_cbz(p, ["1.jpg", "2.jpg"])

        # Retry delays are patched down so the test doesn't sit for 17s;
        # what is under test is the retry BEHAVIOUR, not the constants.
        import app.library.scanner as scanner
        original = scanner._INCOMPLETE_RETRY_DELAYS
        scanner._INCOMPLETE_RETRY_DELAYS = (0.05, 0.1, 0.2)
        self.addCleanup(lambda: setattr(scanner, "_INCOMPLETE_RETRY_DELAYS", original))

        await asyncio.gather(finish_copy(), _readable_archive_when_settled(p))

    async def test_a_rar_is_not_retried(self):
        p = self.tmp / "v01.cbz"
        p.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 64)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with self.assertRaises(UnknownArchiveFormat) as caught:
            await _readable_archive_when_settled(p)
        self.assertFalse(caught.exception.transient)
        # No sleeping: a RAR is instantly identifiable and will never
        # become a zip on its own.
        self.assertLess(loop.time() - started, 1.0)


class FilesystemProbes(TempDirCase):
    """"Couldn't tell" must stay distinct from a confident "no": on a
    confident no, scan_library's reconciliation DELETES the item and its
    series permanently."""

    def test_eligible_extensions(self):
        self.assertTrue(_is_eligible(Path("/x/Serie v01.cbz")))
        self.assertTrue(_is_eligible(Path("/x/Serie v01.epub")))
        self.assertTrue(_is_eligible(Path("/x/Serie v01.CBZ")))
        self.assertFalse(_is_eligible(Path("/x/cover.jpg")))
        self.assertFalse(_is_eligible(Path("/x/.hidden.cbz")))

    def test_path_exists_answers_yes_and_no(self):
        present = self.tmp / "there.cbz"
        write_cbz(present, ["1.jpg"])
        self.assertIs(_path_exists(present), True)
        self.assertIs(_path_exists(self.tmp / "absent.cbz"), False)

    def test_folder_probe_answers_yes_and_no(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertIs(_folder_has_eligible_file(empty), False)
        write_cbz(empty / "v01.cbz", ["1.jpg"])
        self.assertIs(_folder_has_eligible_file(empty), True)

    def test_folder_probe_says_no_for_a_folder_of_junk(self):
        junk = self.tmp / "junk"
        junk.mkdir()
        (junk / "cover.jpg").write_bytes(PNG_1PX)
        self.assertIs(_folder_has_eligible_file(junk), False)

    def test_a_missing_folder_is_a_confident_no(self):
        self.assertIs(_folder_has_eligible_file(self.tmp / "gone"), False)

    def test_an_unreadable_folder_is_unknown_not_no(self):
        # The real failure: neither Path.exists() nor iterdir() returns False
        # when the containing folder isn't traversable by the container's
        # non-root user -- they raise, which used to propagate out of the
        # enclosing write_transaction and discard the whole scan's
        # reconciliation.
        #
        # The OSError is injected rather than produced with chmod because
        # the suite has to answer this the same way whatever user runs it,
        # and CI (like this project's own sandbox) commonly runs as root,
        # where permission bits simply don't apply. A chmod version of this
        # test skips there -- silently leaving the branch that DELETES a
        # series uncovered, which is the one branch least worth guessing at.
        locked = self.tmp / "locked"
        locked.mkdir()
        write_cbz(locked / "v01.cbz", ["1.jpg"])

        def denied(*_args, **_kwargs):
            raise PermissionError(13, "Permission denied")

        with mock.patch.object(Path, "iterdir", denied):
            self.assertIsNone(_folder_has_eligible_file(locked))
        with mock.patch.object(Path, "stat", denied):
            self.assertIsNone(_path_exists(locked / "v01.cbz"))

    def test_an_unreadable_folder_is_unknown_with_real_permissions(self):
        # Same property against the actual filesystem, for the environments
        # that can express it. The injected version above is what keeps this
        # covered everywhere else.
        import os
        if os.geteuid() == 0:
            self.skipTest("running as root: permission bits do not apply")
        locked = self.tmp / "locked"
        locked.mkdir()
        write_cbz(locked / "v01.cbz", ["1.jpg"])
        child = locked / "child"
        child.mkdir()
        locked.chmod(0o000)
        self.addCleanup(lambda: locked.chmod(0o755))
        self.assertIsNone(_folder_has_eligible_file(locked))
        self.assertIsNone(_folder_has_eligible_file(child))
        self.assertIsNone(_path_exists(locked / "v01.cbz"))


if __name__ == "__main__":
    unittest.main()
