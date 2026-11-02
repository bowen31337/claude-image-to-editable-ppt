#!/usr/bin/env python3
"""Optional local text hints via Indra OCR (datalab-to/indra, ONNX, CPU-only).

Opt-in only (`editppt config --text-hints-backend indra`). The code is Apache-2.0,
but the model weights use a modified OpenRAIL-M license that restricts some
commercial use; the user must choose it explicitly. Install with the `indra`
extra: `uv tool install --force --editable "<skill-root>/cli[indra]"`.

Lines are re-measured with the same local ink metrics as the other OCR backends
(paddle_text_hints.build_page_hints), so the hints format is identical. Indra's
layout blocks are attached to each line as advisory `layout_block` data: they
group paragraph lines well on document-style text, but can merge unrelated
labels on infographics.
"""

from __future__ import annotations

from pathlib import Path

from paddle_text_hints import build_page_hints

_CONVERTER = None


def available() -> bool:
    try:
        import indra_ocr  # noqa: F401
    except ImportError:
        return False
    return True


def _converter():
    global _CONVERTER
    if _CONVERTER is None:
        import indra_ocr

        _CONVERTER = indra_ocr.load(threads=4, mode="ocr")
    return _CONVERTER


def _block_for(box, blocks):
    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
    for index, block in enumerate(blocks):
        x0, y0, x1, y1 = block["bbox"]
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return index, block
    return None, None


def page_hints(page_dir: Path, min_conf: float = 0.5) -> dict:
    page_dir = Path(page_dir)
    page = next(iter(_converter().convert(str(page_dir / "source.png"))))[1]
    blocks = list(page.blocks or [])
    parsing = []
    for line in page.lines:
        text = str(line.text or "").strip()
        conf = getattr(line, "conf", 1.0)
        if not text or (conf is not None and conf < min_conf):
            continue
        x0, y0, x1, y1 = (float(v) for v in line.box)
        parsing.append({"block_label": "text", "block_bbox": [x0, y0, x1, y1], "block_content": text})
    hints = build_page_hints(page_dir, {"parsing_res_list": parsing})
    for hint in hints["lines"]:
        box = hint.get("box_px")
        if not box:
            continue
        index, block = _block_for([box[0], box[1], box[0] + box[2], box[1] + box[3]], blocks)
        if block is not None:
            hint["layout_block"] = {"id": f"B{index + 1:02d}", "label": block.get("label")}
    hints["backend"] = "indra"
    hints["layout_blocks"] = [
        {"id": f"B{i + 1:02d}", "label": b.get("label"), "bbox_px": [round(float(v)) for v in b["bbox"]]}
        for i, b in enumerate(blocks)
    ]
    return hints
