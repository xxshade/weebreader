# Paginated text reading

Status: implemented and validated in Chrome and Firefox. Android typography was
validated on Firefox 125.3.0; current Firefox Android validation remains pending
because its download hosts were blocked in the cloud environment.

## Reported behavior

The previous reader kept whole content blocks on a page. An oversized paragraph
gets its own vertically scrollable page. Next advances beyond the entire block,
and the saved position cannot distinguish passages within that paragraph.

The user also reports inconsistent paragraph font sizes in paginated EPUBs on
Firefox Android, using Book's original and default Firefox zoom. The same books
look consistent in Vivaldi Android and Firefox desktop. Automatic text inflation
is a hypothesis, not a confirmed cause on the affected device. Source typography
and font loading remain alternative explanations.

## Agreed behavior

- Apply the new pagination to both EPUBs and web novels.
- Allow ordinary paragraphs to continue onto the next page to use available
  space, as well as paragraphs taller than a complete page.
- Split at complete rendered lines. Preserve all text, emphasis, links and other
  inline formatting. A continuation is part of the same paragraph and must not
  acquire another first-line indent or paragraph gap.
- Prefer readable breaks over completely filled pages: avoid isolated lines
  when at least two lines can fit, keep headings with following text, and respect
  explicit book page breaks. Keep chapter boundaries distinct.
- Respect feasible keep-together preferences; an oversized prose paragraph must
  still make progress across pages. Tiny viewports and large text must not cause
  blank-page loops, missing content or a forced font reduction.
- Keep illustrations and tables intact, with the existing vertical scrolling
  fallback when too tall. Redesigning wide-table layout is outside this change.
- Save the beginning of the displayed passage, including a point within a
  paragraph. Reopening or changing screen size, orientation or typography should
  preserve that point while recalculating page boundaries.
- Preserve existing saved positions. A position without an intra-paragraph
  component retains its existing paragraph-start meaning.
- Preserve Continuous scroll behavior. Switching modes must retain a meaningful
  reading location. Resume across devices must account for advances within the
  same paragraph, including when returning to an already open reader tab.
- Preserve intentional book typography and user sizing/accessibility choices.
  Diagnose the Firefox Android discrepancy before selecting its correction.

Positions use the existing block identity plus a UTF-16 offset in displayed DOM
text. EPUB locations append the offset to `elidx:N`; web novels use an additive
`character_offset` column. Legacy positions use zero. Offsets are clamped when
content is shorter; replacing an edition or changing replacement rules can move
the textual meaning of an existing offset. Rendered page numbers are never saved.

## Validation required before completion

- Run browser validation in both Chrome and Firefox; one engine alone is
  insufficient.
- Verify the reported font-size behavior on Firefox Android. Desktop Firefox
  with a narrow viewport or mobile user agent is not equivalent evidence.
- Exercise ordinary and oversized paragraphs, inline markup, headings, isolated
  lines, explicit breaks, illustrations and tables, at small viewport sizes and
  large font settings.
- Compare content across forward and backward page turns to detect omissions or
  duplication. Check paragraph continuation spacing and indentation.
- Verify resume within a paragraph after reopening, reflow, mode changes and
  cross-device progress, including old saved positions.
- Preserve existing completion behavior and run the relevant reader regressions.

Implementation evidence and reproducible commands are recorded in
[the validation note](../testing/text-pagination.md).
