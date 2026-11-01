# image-to-editable-ppt for Claude Code

A Claude Code skill that rebuilds slide images, screenshots, image-only PDFs, and flattened PPTX decks into **editable PowerPoint**. The output has native text boxes, native tables, native shapes and lines, and icons that are re-traced as **SVG vectors**. Page order and `.pptx` speaker notes are preserved.

It is a port of [ningzimu/image-to-editable-ppt-skill](https://github.com/ningzimu/image-to-editable-ppt-skill) (MIT, Codex) at upstream commit `b7be494`, adapted so it runs entirely inside Claude Code with **no image-generation API**.

## Install

```bash
# 1. make the skill visible to Claude Code (all projects)
ln -sfn "$PWD/skills/image-to-editable-ppt" ~/.claude/skills/image-to-editable-ppt

# 2. install the bundled editppt CLI (the skill also does this itself on first use)
uv tool install --force --editable skills/image-to-editable-ppt/cli
editppt doctor
```

Optional: a free PaddleOCR token (https://aistudio.baidu.com/account/accessToken) makes the text-size and position hints content-aware: `editppt config --paddle-ocr-token <token>`.

## Use

Ask in plain language. The skill triggers on its own:

```text
turn inputs/status-report.png into an editable PowerPoint, save it to outputs/
convert deck.pdf into an editable pptx
this pptx is all flattened pictures - make it editable again and keep my speaker notes
```

A single page is rebuilt by the main session. Multi-page inputs are dispatched to parallel background agents, one per page. Expect roughly 5-15 minutes and a few dollars per complex page.

## What changed from upstream

| Upstream (Codex) | This port (Claude Code) |
| --- | --- |
| `image_gen.imagegen` / OpenAI image API separates icons and cleans backgrounds | `claude-native` backend: Claude **traces** icons, logos, and flat illustrations as SVG (`editppt image zoom` → `editppt image svg`). Photos are kept as rectangular regions (`photo-region`), and backgrounds are cleaned by local OpenCV inpainting (`inpaint`). |
| Assets are bitmaps | SVG is embedded via the Office `svgBlip` extension with a PNG fallback. In PowerPoint, **Convert to Shape** makes the icons editable. |
| Codex page workers, `view_image` | `Agent` background subagents, `SendMessage` for repairs, `Read` for images |
| Image API key required | No key needed. The API path remains an optional upgrade. |

Runtime additions: `cli/editppt/runtime/claude_native_assets.py`, `editppt page merge-fragments`, `svgBlip` emission in the builder, paragraph `line_height` → `a:lnSpc`, a default white slide background (Keynote renders a missing background as black), and validator guards for the new provenance types. The guards accept photo regions only for photo-like objects, reject native text drawn over un-inpainted source pixels, and require an SVG trace to embed its SVG. Upstream deliberately removed a generic crop command because agents used it to fake editability; these guards keep that door closed.

## Layout

```text
skills/image-to-editable-ppt/   the installable skill (SKILL.md, references/, prompts/, scripts/, cli/, evals/)
tests/                          upstream test suite + tests/test_claude_native.py
image-to-editable-ppt-workspace/  eval runs, grader, Keynote preview renderer (git-ignored)
```

## Test

```bash
uv run --no-project --with pytest --with pillow --with numpy --with pymupdf --with pyyaml \
  --with requests --with openai --with resvg-py --with opencv-python-headless python -m pytest -q tests
```

Eval results (3 inputs, with the skill vs. Opus without it): the skill passed 19/19 checks against 15/19 for the baseline. Beyond the checks, the skill keeps the source geometry and produces vector icons; the baseline used raster crops or native-shape stand-ins for icons. Details are in `image-to-editable-ppt-workspace/iteration-3/review.html`.

## Known limits

- The output is a close reconstruction, not a pixel replica. Traced icons are faithful but not identical.
- Photographic objects with irregular outlines keep their rectangular background unless an image API is configured.
- Verified in Keynote and with `python-pptx`/strict XML parsing. Not yet opened in Microsoft PowerPoint itself.
