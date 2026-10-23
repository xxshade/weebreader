/**
 * Web novel detail page: read/unread toggles for the chapter list (single
 * chapter and whole-volume). Global functions -- referenced directly from
 * onclick="" attributes in detail.html.
 *
 * The reader page itself (font/size/theme, pagination, infinite scroll,
 * progress sync) used to live in this file too -- it's now part of the
 * shared engine in reader-text.js, which also powers the EPUB reader. See
 * that file's header comment for why the two were unified.
 */


// ---------- Read/unread toggle (chapter list rows) ----------

async function novelToggleRead(button) {
    const chapterId = button.dataset.chapterId;
    const nextRead = button.dataset.read !== "true";
    try {
        const res = await fetch(`/novels/chapters/${chapterId}/read-status`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ read: nextRead }),
        });
        const result = await res.json();
        novelApplyReadState(chapterId, result.read);
    } catch {
        // leave button as-is; user can retry
    }
}

function novelApplyReadState(chapterId, read) {
    document.querySelectorAll(`[data-chapter-id="${chapterId}"].read-toggle`).forEach((btn) => {
        btn.dataset.read = read ? "true" : "false";
        btn.textContent = read ? "✓ Read" : "Mark as read";
    });
    const row = document.getElementById(`chapter-row-${chapterId}`);
    if (row) {
        const link = row.querySelector("a");
        if (link) link.classList.toggle("text-neutral-500", read);
    }
}

async function novelToggleVolumeRead(button) {
    const volumeId = button.dataset.volumeId;
    const nextRead = button.dataset.read !== "true";
    try {
        const res = await fetch(`/novels/volumes/${volumeId}/read-status`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ read: nextRead }),
        });
        const result = await res.json();
        button.dataset.read = result.read ? "true" : "false";
        button.textContent = result.read ? "Mark all as unread" : "Mark all as read";
        (result.chapter_ids || []).forEach((chapterId) => novelApplyReadState(chapterId, result.read));
    } catch {
        // leave button as-is; user can retry
    }
}

