# AMOLED text palette

The theme on `body` does not override a foreground declared directly on a
paragraph or span. An EPUB can therefore retain black glyphs on the AMOLED black
background even though the chapter content and layout loaded successfully.
Explicit `-webkit-text-fill-color` can cause the same symptom independently of
`color` in both Chrome and Firefox.

The text reader checks ordinary HTML text against its effective solid background
and repairs glyph fill when contrast is below 4.5:1. It retains readable colors,
black text on white boxes, intentionally hidden/transparent text and artwork.
Changing glyph fill instead of `color` avoids repainting `currentColor` SVGs and
backgrounds. Descendant fills are preserved where repairing a parent would change
a readable child. Restoration records belong weakly to their iframe document.

This runs before exposing a page or mounted scroll section, after theme updates,
and after late stylesheet loads. It changes neither text, structure nor progress
coordinates. It does add browser work proportional to mounted text; large scroll
sections cost more than small paginated documents.

Background images/gradients, effects involving opacity, filters or blending, and
unsupported computed color formats are left unchanged when their effective
contrast cannot be determined reliably. This is not a guarantee for every EPUB
visual composition. Tests exercise desktop and 320px viewport simulations in
Chrome and Firefox; those are distinct from testing the user's original EPUB in
LibreWolf or native Android.
