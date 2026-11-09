"""Disposable browser-test instrumentation; production never imports this module."""

from pathlib import Path

from starlette.responses import Response

from app.main import app as application

ROOT = Path(__file__).resolve().parents[1]


class ReaderFixture:
    async def __call__(self, scope, receive, send):
        scripts = ("reader-text.js", "reader-resume.js", "fullscreen.js")
        if scope["type"] == "http" and scope["path"] in [
            "/static/js/" + name for name in scripts
        ]:
            name = scope["path"].rsplit("/", 1)[1]
            source = (ROOT / "app/static/js" / name).read_text()
            if name == "reader-text.js":
                marker = "    init().catch((err) => {"
                if source.count(marker) != 1:
                    raise AssertionError(
                        "Reader fixture must instrument exactly one initializer"
                    )
                source = source.replace(
                    marker,
                    """    root.readerFixture = {
                    prepare: async index => { await loadChapterBlocks(index); },
                    show: async index => { await initPaginated(index, {index:0,offset:0}); },
                    sizes: () => ({chapters:pgChapterCache.size,maps:sourceMaps.size}),
                    navigation: () => ({busy:pgNavigating, atEnd:pgPageEnd.index >= (pgChapterCache.get(pgSpineIndex)?.blocks.length || Infinity)}),
                    progress: () => ({refreshing:refreshingOrder,pending:progressRequests.size}),
                };
"""
                    + marker,
                )
            elif name == "reader-resume.js":
                source = source.replace(
                    "    window.readerResume = {",
                    "    window.readerResume = {\n        fixtureHasWatch: () => Boolean(watched),",
                    1,
                )
            else:
                source = source.replace(
                    "return { setupButton,",
                    "return { fixtureHasButton: () => Boolean(currentButton), setupButton,",
                    1,
                )
            await Response(
                source,
                media_type="application/javascript",
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        await application(scope, receive, send)


app = ReaderFixture()
