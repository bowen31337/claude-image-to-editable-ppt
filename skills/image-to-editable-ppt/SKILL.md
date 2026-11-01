---
name: image-to-editable-ppt
description: Rebuild slide images, screenshots, scanned/image-based PPT or PPTX files, and PDF decks into object-level editable PowerPoint (.pptx) — real text boxes, native shapes, native tables, and icons/illustrations re-traced as SVG — preserving page order and speaker notes. Runs fully inside Claude Code with no image-generation API. Use this whenever the user wants to "make this slide editable", convert a slide screenshot/PNG/JPG/PDF/flattened PPTX into a PPT they can edit, recover text from an image-only deck, or reconstruct an infographic as PowerPoint objects, even if they just say "turn this image into a PPT" or "I can't edit the text in this deck". Not for authoring new presentations from an outline, article, or idea.
---
# Image to Editable PPT (Claude Code port)

Use the bundled `editppt` runtime to decompose, reconstruct, validate, and assemble visual slides as editable `.pptx`. Inputs may be one or more images, a PDF, or an image-based PPT/PPTX. Output is always one `.pptx`, one slide per input page, in input order.

`<skill-root>` below means the directory containing this `SKILL.md`.

## How this skill is organized

This file is read by you, the **orchestrator**. Page-level work is done by **page reconstructors** (background subagents, or you yourself for a single page) driven by a generated prompt. Each rule has one authoritative home — follow the pointer rather than guessing:

- `prompts/page-worker.md` — page reconstructor template: ownership boundary, reading route, execution order, required outputs, return format. Filled in by `scripts/build-page-worker-prompt.py`.
- `references/cli-helper.md` — `editppt` install check, command tree, syntax. Read when choosing a command.
- `references/manifest-schema.md` — JSON contracts for every run/page artifact. Read when writing or validating run/page files.
- `references/page-decision-tree.md` — what becomes background vs. image asset vs. native text/shape/table, text-hints usage, final self-check, fix-vs-warning. Page reconstructors read this; you rarely need to.

## Claude Code runtime mapping

The upstream skill targets Codex. These are the substitutions that make it work here; they apply everywhere the references mention the Codex equivalents.

| Upstream (Codex) concept | In Claude Code |
| --- | --- |
| Built-in `image_gen.imagegen` + image-model asset sheets | **Replaced by the `claude-native` backend.** Claude can see but not generate bitmaps, so it traces icons/illustrations as SVG, keeps photos as rectangular source regions, and inpaints clean bases locally (`references/page-decision-tree.md` section 2.4). Always `prepare` with `--image-backend claude-native`; never `builtin-imagegen`. |
| CLI image API (`editppt image generate/edit`) | Optional upgrade only, used when `editppt doctor --check-api` reports a ready backend and an object fits no native source. Never required. |
| `view_image` | `Read` the image file path (`source.png`, `text_hints.png`, zoom and `.compare.png` images, `preview.png`, `split_assets_contact.png`). |
| Spawn a page worker / subagent | `Agent` tool, `subagent_type: "general-purpose"`, `run_in_background: true`, `name: "ppt-<page_id>"` (e.g. `ppt-page_003`). |
| Worker id for `--agent-id` | The same name you gave the agent (`ppt-page_003`); `main` for single-page local mode. |
| Wait for workers | Background agents notify you when they finish. Don't poll in a loop. |
| Send a repair back to the worker | `SendMessage` to the worker's name, so it keeps its context. |
| Sandbox / network approval | Claude Code permission prompts. A claude-native run with local RapidOCR hints makes no external calls at all. PaddleOCR is called only when a token is configured (`editppt prepare` / `editppt run hints`); `editppt image generate/edit` also calls out if the optional upgrade is used. |

## Entry Contract

These orchestrator-level rules are stated once here.

- **CLI first.** `editppt` is a required runtime. If `editppt --help` fails, install it from `<skill-root>/cli` using the Pre-Run Check in `references/cli-helper.md` (prefer `uv tool install --force --editable <skill-root>/cli` when `uv` exists, then `pipx`, then `pip --user`), then run `editppt doctor`.
- **State only through the CLI.** Start with `editppt prepare <input...>`. After that, advance every state transition only through `editppt` commands; never hand-write run/page state JSON. This keeps the run deterministic and resumable.
- **Who rebuilds pages.** A run with exactly one page is rebuilt by you in local mode after `editppt run dispatch --local` claims it. Multi-page runs are rebuilt by background page workers — one per page. Don't rebuild multi-page artifacts yourself: each page burns a lot of context (images, manifests, repair loops), and keeping it in separate workers is what keeps your orchestration context usable and lets pages run in parallel.
- **Artifact ownership.** You don't write `manifest.json`, `page.pptx`, `preview.png`, `split_assets_contact.png`, `validation.json`, or `page_result.json` except in single-page local mode after the `--local` claim. Local mode follows the same page prompt, references, outputs, and `run record` validation as a worker.
- **Image backend.** See "Image backend in Claude Code" below. If no permitted source can reproduce an object, the page fails validation rather than substituting approximate shapes or icon crops (`references/page-decision-tree.md` section 2).
- **Authorized external calls.** A request to convert slides into editable PPT authorizes the OCR calls (and, if configured, image-API calls) needed for that conversion, unless the user asks for local-only processing or marks the input confidential. Send only task-local page images, prompts, masks, and reference images — never unrelated files or credentials. Third-party endpoints are allowed only when the user configured or named them.
- **Autonomy.** Run routine reconstruction, configured fallbacks, and page-local repairs without confirmation gates. Stop for the user only for something only they can supply (an OCR token decision, a missing input). Report concrete blockers instead of asking the user to debug.
- **Object decisions** follow `references/page-decision-tree.md`; deterministic validation is a structure gate and never waives an object-source decision.
- `manifest.json` is the authoritative page build source; `run record` validates `page.pptx` against it, and `run finalize` rebuilds the deck from recorded manifests.

### Credentials hygiene

Secrets (PaddleOCR token, optional image-API key) end up in the transcript if the user pastes them into chat. Suggest the user run the config command themselves with the `!` prefix so the value goes straight to the CLI, e.g.:

```
! editppt config --paddle-ocr-token <token>
! editppt config --api-key sk-... --base-url https://api.openai.com/v1   # optional image-API upgrade
```

If they paste a key into chat anyway, store it with `editppt config` and never echo it back; the CLI masks it in output. Never write keys into the project, the run directory, or the skill directory.

### Image backend in Claude Code

Claude has vision but no image generation, so every run uses the `claude-native` backend: `editppt prepare <input...> --image-backend claude-native`. Page reconstructors then pick each object's source per `references/page-decision-tree.md` section 2.4:

- **Icons, logos, badges, hand-drawn marks, flat illustrations** — Claude traces them as SVG (`editppt image zoom`, then `editppt image svg`). The PPTX embeds the real SVG, so in PowerPoint the user can recolor it or use **Convert to Shape** to edit its individual paths. That makes these objects *more* editable than the upstream skill's bitmap assets.
- **Photos, screenshots, textured blocks** — kept as rectangular source regions (`editppt image photo-region`). Vector tracing would turn them into cartoons.
- **Complex backgrounds behind rebuilt text** — cleaned by local inpainting (`editppt image inpaint`).

Don't ask the user for an image API. If `editppt doctor --check-api` happens to show a ready backend (Codex login or a configured key), workers may use `editppt image generate/edit` as an optional upgrade for objects that fit no native source well, such as a photographic cutout on a busy background. Asset commands within a page run serially.

## Workflow

### Phase 1: Prepare

Read the prepare examples in `references/cli-helper.md` and the run/page file descriptions in `references/manifest-schema.md`.

```bash
editppt prepare <input...> --image-backend claude-native
```

Keep the default `--out-root`: it creates the working run directory `output/image-to-editable-ppt/<job-id>/` under the current directory. Don't point it at the folder where the user wants the result; that folder should receive only the final deck (Phase 4). Afterward there must be `deck_manifest.json`, `page_jobs.json`, `notes_manifest.json`, and each page must have `source.png` plus `page_request.json`.

Prepare also writes per-page text hints (`text_hints.json`/`text_hints.png`). With no token, they come from local RapidOCR, which reads the text on-device with no upload, so don't ask the user for anything. Only if prepare reports the geometry-only `builtin-ink` backend (RapidOCR unavailable) should you ask the user once before dispatching any page: a free token from https://aistudio.baidu.com/account/accessToken, stored with `editppt config --paddle-ocr-token <token>`, makes hints content-aware (it reads the text, not just measures where it is) and noticeably improves text fidelity; `editppt run hints <run>` then regenerates the current run's hints in place. The free personal quota is more than enough for this skill. Wait for their choice; if they decline, continue with offline hints and don't ask again.

If a token is configured but OCR fell back because a permission prompt or network failure blocked it, that's not the preferred quality path: get the call allowed and rerun `editppt run hints <run>` before page reconstruction. Continue offline only after the user declines OCR, after an allowed OCR attempt fails for a real service reason, or when the user asked for local-only handling.

### Phase 2: Rebuild or dispatch pages

Call repeatedly and act on the returned `stage`:

```bash
editppt run next <run> --json
```

**`stage=rebuild_page_locally`** (exactly one page). Claim it, then rebuild it yourself:

1. `python3 <skill-root>/scripts/build-page-worker-prompt.py <run> --page <page_id> --out <abs-run-dir>/pages/<page_id>/worker-prompt.md`
2. `editppt run dispatch <run> --page <page_id> --agent-id main --prompt-file <abs-run-dir>/pages/<page_id>/worker-prompt.md --local`
3. `Read` the generated prompt and follow it inside that page directory, producing the same outputs a worker would.

**`stage=dispatch_pages`**. For each suggested page, in this order:

1. Build the prompt with the script, as above.
2. Spawn the worker with the `Agent` tool — `subagent_type: "general-purpose"`, `run_in_background: true`, `name: "ppt-<page_id>"`, `description: "Rebuild <page_id> as editable PPT"`, and `prompt` set to:
   ```
   You are a page reconstructor for the image-to-editable-ppt skill (Claude Code port).
   Read <abs-run-dir>/pages/<page_id>/worker-prompt.md and follow it exactly.
   Claude Code notes: view images with the Read tool; you cannot generate
   bitmaps, so non-text visuals come from page-decision-tree.md section 2.4
   (SVG traces, photo regions, local inpaint); run shell commands with Bash.
   Do not touch files outside your page directory.
   ```
3. Only after the spawn succeeded: `editppt run dispatch <run> --page <page_id> --agent-id ppt-<page_id> --prompt-file <abs-run-dir>/pages/<page_id>/worker-prompt.md`

Spawn all suggested pages of a batch in a single message so they run concurrently. `--out` and `--prompt-file` must be absolute so the page directory isn't prepended twice. Concurrency slots come from `page_jobs.json.max_concurrent_pages` (default 6).

**`stage=wait`**. Dispatched pages are active leases, not idle slots. Wait for completion notifications. Don't stop, reset, or replace a worker because it is slow or quiet — complex pages legitimately take 10+ minutes.

**`stage=configure_backend`**. Run `editppt run backend <run> --mode claude-native` rather than the suggested `builtin-imagegen`/CLI contract.

### Phase 3: Record

When a worker's completion notification arrives (or you finish the local page):

```bash
editppt run record <run> --page <page_id> --agent-id <id>
```

This validates `page.pptx` against `manifest.json` and rejects the page if positioned objects lack source-pixel coordinates, the manifest can't independently rebuild the page, or `validation.json` lacks top-level `passed: true`. Then go back to `editppt run next`.

**Repairs.** If record fails, read the failure evidence and have the page's owner repair only the affected artifacts, revalidate (`editppt page validate`, see `references/cli-helper.md`), and record again. In multi-page mode, `SendMessage` the evidence to the existing worker (`ppt-<page_id>`) rather than spawning a new one — it already holds the page context and its verified assets. Don't regenerate compliant assets to fix an unrelated manifest or table error.

**Reset** only when a page needs a fresh execution: the worker ended in a terminal failed state, the user cancelled, or repeated reachability attempts show no page-local progress. Slow is not lost. After fixing what blocked it:

```bash
editppt run reset <run> --page <page_id> --agent-id <id> --confirm-lost
```

Reset returns the page to `pending`; resume Phase 2 with a new prompt and worker. Existing artifacts stay for provenance and selective reuse.

Retry only after changing an input or condition — never repeat an identical failing call. If a real prerequisite is missing, preserve progress and report the concrete blocker. Once outputs pass their checks, move on; don't repeat unchanged visual QA.

### Phase 4: Finalize

When `run next` returns the finalize stage:

```bash
editppt run finalize <run>
```

Finalize rebuilds the deck from each recorded `pages/page_NNN/manifest.json` in page order, copies `.pptx` speaker notes unchanged, and validates the result: valid package, slide count = input page count, correct PDF/PPTX page mapping, complete media relationships, all manifest assets present with matching hashes, speaker-note hashes match, and no "full-slide source raster with text overlaid" pattern (that is a fake editable slide and is never acceptable).

If the user named an output location or filename, copy `final/<origin>_edited.pptx` there; otherwise leave it in `final/`. Your final reply reports the delivered PPTX path, the validation result, and any per-page warnings or failed pages with their concrete causes. Mention the run directory only as where the intermediate files live.

## State principles

Continue only from file facts and `editppt run next`.

- `pending` — from `prepare`, or restored by `reset`.
- `dispatched` — `run dispatch` recorded a real spawned worker or the single-page `--local` claim. An active lease.
- `recorded` — `run record` validated the outputs; only pages with `validation.json` `passed: true` get here.
- `accepted` / `complete` — written by `run finalize`.

`imagegen-jobs.json` is the page-local image provenance record (`recorded` after `editppt image import`, `processed` after `editppt image process-sheet`).

## Delivery principles

- Each page is self-checked once by its reconstructor; evidence lives in `manifest.json` and `validation.json`.
- The final output must be an openable, structurally valid `.pptx`.
- Whether an imperfection must be fixed or may ship as a recorded warning is governed by "Fix versus Warning" in `references/page-decision-tree.md`. A warning never replaces a missing required step.

## Expectations to set with the user

Mention these briefly on a first run, not every time:

- It is slow and token-heavy: a complex single page can take 10+ minutes; a multi-page deck runs pages in parallel but costs proportionally more.
- Output is a close reconstruction, not a pixel-perfect replica. Icons and flat illustrations are re-traced as SVG, so they are faithful but not byte-identical to the source. Photos stay as movable images. Photographic objects with irregular outlines keep their rectangular background unless an image API is configured.
- Approving `editppt` once for the session (or allowing `Bash(editppt:*)` in settings) avoids being prompted on every page step, since background workers run many CLI calls.

## Upstream

Ported from [ningzimu/image-to-editable-ppt-skill](https://github.com/ningzimu/image-to-editable-ppt-skill) (MIT, see `LICENSE`), upstream commit `b7be494`. This SKILL.md is rewritten for Claude Code. The port adds the `claude-native` backend: `cli/editppt/runtime/claude_native_assets.py`, SVG embedding in the builder, new provenance guards in the validator, and the matching sections in the references and the worker prompt. Everything else is vendored upstream. When updating from upstream, merge rather than overwrite, and run the test suite in the repository's `tests/` directory.
