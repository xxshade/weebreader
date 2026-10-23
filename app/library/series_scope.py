"""What counts as a series that is ON THE SHELF.

A series with no items is kept rather than deleted when the reader has
invested something in it -- a tracking link, a status, text replacements,
an edited name or cover (see `_series_holds_reader_state` in scanner.py).
That is what makes reading a series to the end and clearing the files
survivable: one chapter landing back in the folder reunites the series
with all of it, because `UNIQUE(library_id, folder_path)` is what the
scanner looks a series up by.

The price is that an empty series must not be OFFERED anywhere -- it has
nothing to open. Three places list or count series and they have to agree
about it, which is exactly the shape of drift this project keeps getting
bitten by, so the clause lives here once and
`tests/test_empty_series.py` checks that all three still use it.
"""


def has_items(alias: str = "series") -> str:
    """SQL for "this series still has at least one item", for a WHERE."""
    return f"EXISTS (SELECT 1 FROM items WHERE items.series_id = {alias}.id)"


# The order a series' items are shown in, read in, and take their cover
# from. Automatic is number, then the filename-derived sort_title; once the
# reader has dragged items into place (manual_sort, sticky even with the
# drag UI off) it is theirs. Written out in five places, one copy had
# drifted: the reader's "next item" followed the numbers in a series the
# reader had reordered by hand.
AUTOMATIC_ORDER = "(number_in_series IS NULL), number_in_series, sort_title"
MANUAL_ORDER = "(manual_order IS NULL), manual_order, sort_title"


def items_order(manual_sort) -> str:
    """The ORDER BY terms (without the keywords) for a series' items."""
    return MANUAL_ORDER if manual_sort else AUTOMATIC_ORDER
