"""Test suite for the parts of this app that are logic rather than plumbing.

Run everything from the repository root:

    python3 -m unittest discover -s tests -t .

Deliberately built on the standard library's `unittest` rather than pytest:
this project has no test dependency and adding one would mean either a
second requirements file to keep in sync or a test framework inside the
runtime image. `unittest.IsolatedAsyncioTestCase` covers the async helpers,
which was the only real reason to want pytest here.

What belongs in here is what the history in AGENTS.md says actually breaks:
the scanner's decisions about messy third-party filenames and archives.
Those are functions over inputs, so they can be pinned cheaply -- unlike
the reader UIs, which need a real browser and stay verified by hand.
"""
