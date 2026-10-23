"""An item description as plain text, whatever markup the file wrapped it in.

EPUB converters routinely put HTML in `dc:description` -- escaped
(`&lt;p&gt;...`) or, less validly, as child elements -- and some ComicInfo
`Summary` fields carry it too. The description is shown as text (never as
markup: it comes out of a file, and rendering it would hand that file the
page), so the tags go and the paragraphs they marked stay, one blank line
apart.
"""

import re

from lxml import html

# Elements whose end starts a new paragraph. A <br> is a line break inside
# one, carried through as U+2028 because every other single newline in the
# text is only the file's own wrapping (a pretty-printed OPF indents the
# description across as many lines as it likes) and collapses into a space.
_BLOCKS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "tr"}
_LINE_BREAK = " "
_PARAGRAPHS = re.compile(r"\n[ \t\r\f\v ]*\n\s*")
_SPACES = re.compile(r"[ \t\n\r\f\v ]+")


def plain_text(value: str | None) -> str | None:
    if not value:
        return None
    if "<" in value or "&" in value:
        try:
            root = html.fragment_fromstring(value, create_parent="div")
        except Exception:
            root = None
        if root is not None:
            for element in root.iter():
                if not isinstance(element.tag, str):
                    continue  # a comment or processing instruction
                tag = element.tag.lower()
                if tag == "br":
                    element.tail = _LINE_BREAK + (element.tail or "")
                elif tag in _BLOCKS:
                    element.tail = "\n\n" + (element.tail or "")
            value = root.text_content()
    paragraphs = []
    for block in _PARAGRAPHS.split(value):
        lines = (_SPACES.sub(" ", line).strip() for line in block.split(_LINE_BREAK))
        text = "\n".join(line for line in lines if line)
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs) or None
