# WeebReader

A self-hosted library and reader for EPUBs, comics and web novels.

## Library

**Series**:
A folder of items read as one work, one volume or chapter per item. A renamed folder is the same series.
_Avoid_: Collection, book (for the whole)

**Part**:
One piece of a volume published split in several ("Volume 8 Part 1", "Part 2"). It orders the pieces within their volume and is never the series' number.
_Avoid_: Section, half

**Item**:
One file in the library -- a single EPUB or comic, a volume or a chapter of its series. Renamed or moved within the library, it is still the same item; a copy, or a file put back after it was removed, is a new one.
_Avoid_: Book, file, entry

**Removed item**:
An item whose file has left the library. What the user read of it -- time spent, how far, whether it was read -- is remembered; nothing else is, and it is no longer counted as part of the library.
_Avoid_: Deleted, missing, archived item

**Web novel**:
A work followed from a website, downloaded chapter by chapter and grouped in volumes. Not an item: it lives outside every library folder.
_Avoid_: Online novel, serial

**Chapter**:
One installment of a web novel, and what its reading status is kept for.
_Avoid_: Episode, post

**Series modified**:
The last time the series' items changed: one added, removed, or its file replaced. Reading it and editing its name or cover are not modifications.
_Avoid_: Updated, last changed

**Blurred cover**:
The cover of a series the reader asked to hide, drawn blurred wherever it shows without having been opened on purpose: the series' card and its items in the home page's Reading section and search. The series' own page shows it plainly. A choice about the series, not about a library or a reader.
_Avoid_: Library blur, NSFW, hidden cover

## Reading

**Unread**:
An item (or a web novel's chapter) the user has not started, whether it was never opened or was put back. There is one unread, not two.
_Avoid_: Never opened, cleared, reset

**Reading**:
An item (or a web novel's chapter) started and not finished; it has a place to resume from.
_Avoid_: In progress, started

**Read**:
An item (or a web novel's chapter) finished, by reaching its end or by the user declaring it done.
_Avoid_: Completed, finished, done

## Describing things

**Series summary**:
The text under a series' title, written by the user or filled in from a tracker; editable.
_Avoid_: Series description, synopsis

**Embedded metadata**:
What an item's file declares about itself (author, description, publisher, ...); read-only, always what the file says.
_Avoid_: Item info, tags

**Item description**:
The description an item's embedded metadata gives for that one volume or chapter, not for the series. Always qualified "item", never confused with the series summary -- the difference is where it comes from: the file, versus the user or a tracker.
_Avoid_: Summary, synopsis, bare "description"

**Author**:
Whoever the embedded metadata credits with writing the item; illustrators and translators are not authors.
_Avoid_: Creator, writer
