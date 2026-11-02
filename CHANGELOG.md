# Changelog

## 1.0.0 — 2026-11-02

First release of the Claude Code port of [ningzimu/image-to-editable-ppt-skill](https://github.com/ningzimu/image-to-editable-ppt-skill) (upstream commit `b7be494`).

### Features

- **Claude Code orchestration.** Single pages are rebuilt in the main session. Multi-page inputs fan out to background `Agent` workers, one per page, and repairs are sent back to the same worker with `SendMessage`. Credentials can be entered with `! editppt config …` so they never appear in the chat.
- **`claude-native` asset backend.** Pages rebuild with no image-generation API:
  - `editppt image zoom` writes a magnified source crop with a coordinate grid and exact dominant colours, as a reference for tracing.
  - `editppt image svg` validates an SVG trace, renders a PNG fallback, compares it with the source, and reports fidelity (`ink_iou`).
  - `editppt image photo-region` keeps photo-like objects as rectangular image regions.
  - `editppt image inpaint` cleans complex backgrounds with local OpenCV inpainting.
- **SVG embedded in the PPTX.** Icons use the Office `svgBlip` extension with a PNG fallback, so PowerPoint's **Convert to Shape** makes traced icons fully editable.
- **Local OCR text hints.** RapidOCR (PP-OCR models on ONNX Runtime) reads text on-device, with no token and no upload. The fallback order is PaddleOCR-VL (with a token), then RapidOCR, then geometry only.
- **Opt-in Indra OCR backend.** Install it with the `[indra]` extra and enable it with `editppt config --text-hints-backend indra` or `run hints --backend indra`. Its lines carry advisory paragraph `layout_block`s.
- **`editppt page merge-fragments`** merges asset fragments into `manifest.json` idempotently and seeds a new manifest from `page_request.json`.
- **Paragraph line spacing.** `line_height` is emitted as `a:lnSpc`, so each paragraph stays in one text box and reflows when edited.

### Validation and safety

- New provenance types (`claude-svg`, `source-photo-region`, `local-inpaint-clean-base`) each have their own evidence guards:
  - Photo regions are accepted only for photo-like objects, and never for a whole page.
  - Native text over source pixels that weren't inpainted is rejected, because it leaves the original text baked in underneath.
  - An SVG trace must embed its SVG.
  - SVG media hashes are checked.
- SVG traces may not contain embedded rasters, scripts, or external references, so a source crop can't pass as a vector. Features PowerPoint renders poorly (`<style>`, filters, masks) produce warnings.

### Fixes

- Slides with no background now default to white. Keynote rendered a missing background as black, which hid dark text.
- The final deck is copied to the location the user asked for, and working files stay in the run directory.
- Labels keep their source line breaks, and body paragraphs wrap at the source column width.

### Quality

- 153 tests: the upstream suite plus Claude-native, OCR backend, and merge tests.
- Evals: the skill passed 19/19 assertions against 15/19 for a no-skill Opus baseline. Speaker notes from a flattened PPTX were preserved byte-for-byte. Decks open in Keynote and parse strictly with `python-pptx` and `lxml`.

### Known limitations

- Not yet opened in Microsoft PowerPoint itself.
- A complex page costs roughly $2–6 and takes 5–15 minutes.
- Photographic objects with irregular outlines keep a rectangular background unless an image API is configured.
