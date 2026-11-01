#!/usr/bin/env python3
"""Local, content-aware text hints via RapidOCR (PP-OCR models on ONNX Runtime).

No token and no upload: the model files ship with the `rapidocr` package. Each
detected line is re-measured with the same local ink metrics as the PaddleOCR-VL
path (paddle_text_hints.build_page_hints), so the hints format is identical.
"""

from __future__ import annotations

from pathlib import Path

from paddle_text_hints import build_page_hints

_ENGINE = None


def available() -> bool:
    try:
        import rapidocr  # noqa: F401
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def _engine():
    global _ENGINE
    if _ENGINE is None:
        import logging

        from rapidocr import RapidOCR

        _ENGINE = RapidOCR()
        # RapidOCR logs every model load at INFO; keep prepare's output readable.
        for name in ("RapidOCR", "rapidocr"):
            logging.getLogger(name).setLevel(logging.WARNING)
    return _ENGINE


def page_hints(page_dir: Path, min_score: float = 0.5) -> dict:
    page_dir = Path(page_dir)
    result = _engine()(str(page_dir / "source.png"))
    blocks = []
    for points, text, score in zip(result.boxes if result.boxes is not None else [], result.txts or [], result.scores or []):
        if score < min_score or not str(text).strip():
            continue
        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
        blocks.append({"block_label": "text", "block_bbox": [min(xs), min(ys), max(xs), max(ys)], "block_content": str(text)})
    hints = build_page_hints(page_dir, {"parsing_res_list": blocks})
    hints["backend"] = "rapidocr"
    return hints
