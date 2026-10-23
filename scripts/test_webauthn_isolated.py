"""Run the existing passkey handshake against an isolated account/server."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_resync_browser import BrowserChecks


if __name__ == '__main__':
    fixture = type('PasskeyFixture', (BrowserChecks, unittest.TestCase), {})
    try:
        fixture.setUpClass()
        fixture.stop_server()
        from app.auth.service import hash_password
        with sqlite3.connect(fixture.data / 'library.db') as db:
            db.execute('UPDATE users SET username=?,password_hash=? WHERE id=1',
                       ('admin', hash_password('password12345')))
        os.environ['APP_WEBAUTHN_ORIGIN'] = fixture.base
        os.environ['APP_WEBAUTHN_RP_ID'] = 'localhost'
        fixture.start_server()
        subprocess.run([sys.executable, 'scripts/test_webauthn_flow.py'], check=True,
                       env={**os.environ, 'WEBAUTHN_TEST_BASE': fixture.base})
    finally:
        fixture.doClassCleanups()
