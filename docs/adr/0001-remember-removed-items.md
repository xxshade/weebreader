# Remember what was read of a removed item

Until this decision, an item whose file left the disk kept only its reading time. The time stayed in `page_reads`, with `item_id` set to NULL and the `kind` it had. Everything else about the item went with its row: its title and series, its completion verdict, and its size.

That made reading and then deleting look like never having read. The item vanished from "Most recent". It dropped out of the "start to finish" averages. It stopped teaching the reading rate, so the next, unread volume of a series lost its estimate. That is the normal way some users keep a library: read a volume, delete it.

We now keep a **removed item** (GLOSSARY.md). `scanner._delete_item` writes it just before deleting the item, and only when someone spent time on it. It has two parts:

- **`removed_items`** holds the title, the series as its folder, the format, and the text and page size.
- **`removed_item_reads`** holds, for each reader:
  - their time, pages and last read, summed from the spans;
  - their status and percentages.

What a removed item feeds:

- **It counts in** "Most recent", "start to finish" and the reading rate.
- **It does not count in** the Libraries counts, which describe what is in the library now.

## Considered options

- **Soft-deleting items** (keeping the row, marked missing). Rejected: every query in the app would have to exclude it. The scanner moved away from exactly that (`missing`) to real deletion.
- **Copying the title and series onto every `page_reads` row, or linking spans to the removed item.** Rejected:
  - It changes a table that is only ever inserted into, and the reading-totals trigger relies on that (`test_hot_path_costs.py`).
  - Everything wanted is already a sum that can no longer change, because nothing can be read on an item that is gone.

## Consequences

- **A file put back, renamed or moved is a new item.** Its earlier reading stays a separate removed item, so a series card can list the same title twice. That is why removed items are marked "removed" there.
- **Nothing removed before this change can be recovered.** Its title was never kept. Its time still counts in the totals and the daily chart.
