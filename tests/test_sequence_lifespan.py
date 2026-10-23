"""The file an earlier version persisted the SSE sequence in is removed.

Nothing reads `event-sequence.json` any more (the epoch is drawn on every
boot, see events.py), so a data directory upgraded from that version would
otherwise keep a file that looks like it means something. This runs the
real lifespan against a directory holding one.
"""
import tempfile
import unittest
from pathlib import Path

from app.config import settings


class TheLeftoverSequenceFileIsRemoved(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "covers").mkdir()

        original = settings.data_dir
        settings.data_dir = self.dir
        self.addCleanup(lambda: setattr(settings, "data_dir", original))

    async def test_a_boot_removes_it(self):
        # ONE boot per process: the scan scheduler is a module-level
        # APScheduler bound to the loop it was started on, so a second
        # lifespan in the same process dies on a closed loop -- a fact
        # about that singleton, not about anything here.
        from app.main import app, lifespan

        leftover = self.dir / "event-sequence.json"
        leftover.write_text('{"epoch": "a35d8ef2", "reserved": 3190}')
        async with lifespan(app):
            self.assertFalse(leftover.exists())
        self.assertFalse(leftover.exists(), "the shutdown wrote it back")


if __name__ == "__main__":
    unittest.main()
