"""What every parser returns, whichever format it read."""

from dataclasses import dataclass, field


@dataclass
class ParsedItem:
    title: str
    # Uncompressed bytes of the reading order -- how much book there is
    # (see epub.spine_text_size for why not the file's size on disk). Always
    # None for a comic: "how much text is there" is not answerable from a
    # stack of images, which is also why comics get no reading-time estimate
    # (see stats/estimates.ESTIMATED_FORMATS).
    text_size: int | None = None
    metadata: dict = field(default_factory=dict)
    cover_bytes: bytes | None = None
    page_count: int | None = None  # reflowable EPUB has no page count
