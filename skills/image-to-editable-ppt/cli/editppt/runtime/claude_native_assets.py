#!/usr/bin/env python3
"""Claude-native asset commands: no external image model required.

These commands let an agent that cannot generate bitmaps (e.g. Claude Code)
produce page assets deterministically and with recorded provenance:

  zoom          Magnify a source box with a labelled coordinate grid and its
                dominant colours, to trace from.
  svg           Validate an agent-authored SVG trace of one foreground object,
                render its PNG fallback, write a source-vs-trace comparison and
                fidelity metrics, and emit a manifest fragment. The PPTX embeds
                the SVG itself (PowerPoint "Convert to Shape" works on it).
  photo-region  Extract a rectangular photo/screenshot-like object from
                source.png. Restricted to object types that cannot be traced as
                vectors; the validator rejects overlapping rebuilt text unless it
                was inpainted out of the region first.
  inpaint       Build a clean base by locally inpainting rebuilt text/objects out
                of source.png (OpenCV Telea), for complex backgrounds.

Each command records a job in imagegen-jobs.json and prints a JSON summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

SVG_NS = "http://www.w3.org/2000/svg"
PHOTO_OBJECT_TYPES = (
    "photo",
    "screenshot",
    "video-cover",
    "map",
    "textured-image-block",
    "complex-illustration",
)
FORBIDDEN_SVG_TAGS = {"image", "foreignObject", "script", "iframe", "video", "audio"}


# ---------------------------------------------------------------- helpers


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_box(value: str) -> list[float]:
    parts = [part.strip() for part in str(value).split(",")]
    if len(parts) != 4:
        raise SystemExit(f"box must be x,y,width,height: {value}")
    box = [float(part) for part in parts]
    if box[2] <= 0 or box[3] <= 0:
        raise SystemExit(f"box width/height must be positive: {value}")
    return box


def parse_boxes(value: str | None) -> list[list[float]]:
    if not value:
        return []
    return [parse_box(chunk) for chunk in value.split(";") if chunk.strip()]


def resolve_in_page(page_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    path = path if path.is_absolute() else page_dir / path
    path = path.resolve()
    try:
        path.relative_to(page_dir)
    except ValueError:
        raise SystemExit(f"path must live inside the page dir: {path}")
    return path


def rel(page_dir: Path, path: Path) -> str:
    return path.resolve().relative_to(page_dir).as_posix()


def load_source(page_dir: Path):
    from PIL import Image

    source = page_dir / "source.png"
    if not source.exists():
        raise SystemExit(f"missing source.png in page dir: {page_dir}")
    return Image.open(source).convert("RGB")


def clamp_box(box: list[float], size: tuple[int, int]) -> tuple[int, int, int, int]:
    x, y, w, h = box
    left = max(0, int(round(x)))
    top = max(0, int(round(y)))
    right = min(size[0], int(round(x + w)))
    bottom = min(size[1], int(round(y + h)))
    if right <= left or bottom <= top:
        raise SystemExit(f"box is outside the source image: {box}")
    return left, top, right, bottom


def record_job(page_dir: Path, job: dict) -> None:
    jobs_path = page_dir / "imagegen-jobs.json"
    jobs = json.loads(jobs_path.read_text(encoding="utf-8")) if jobs_path.exists() else {"schema_version": 1, "jobs": []}
    jobs.setdefault("jobs", [])
    jobs["jobs"] = [item for item in jobs["jobs"] if item.get("job_id") != job["job_id"]]
    jobs["jobs"].append({**job, "status": "recorded", "recorded_at": now_iso()})
    jobs["updated_at"] = now_iso()
    jobs_path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_fragment(page_dir: Path, value: str | None, default_name: str, fragment: dict) -> Path:
    path = resolve_in_page(page_dir, value or f"assets/{default_name}.fragment.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fragment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def border_color(image) -> tuple[int, int, int]:
    """Median colour of a crop's 2px border: the local background estimate."""
    import numpy as np

    arr = np.asarray(image.convert("RGB"))
    border = np.concatenate([arr[:2].reshape(-1, 3), arr[-2:].reshape(-1, 3), arr[:, :2].reshape(-1, 3), arr[:, -2:].reshape(-1, 3)])
    return tuple(int(v) for v in np.median(border, axis=0))


def side_by_side(left_img, right_img, out_path: Path, labels=("source", "rebuilt")) -> None:
    from PIL import Image, ImageDraw

    target_h = max(256, left_img.height)
    def scaled(img):
        ratio = target_h / img.height
        return img.resize((max(1, int(img.width * ratio)), target_h), Image.NEAREST if ratio >= 2 else Image.LANCZOS)
    a, b = scaled(left_img.convert("RGB")), scaled(right_img.convert("RGB"))
    gap, head = 16, 22
    canvas = Image.new("RGB", (a.width + b.width + gap * 3, target_h + head + gap), "#e5e7eb")
    canvas.paste(a, (gap, head))
    canvas.paste(b, (a.width + gap * 2, head))
    draw = ImageDraw.Draw(canvas)
    draw.text((gap, 4), labels[0], fill="#111827")
    draw.text((a.width + gap * 2, 4), labels[1], fill="#111827")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)


# ---------------------------------------------------------------- svg


def validate_svg(svg_path: Path, allow_identity_text: bool) -> tuple[str, list[str]]:
    text = svg_path.read_text(encoding="utf-8")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SystemExit(f"SVG does not parse: {exc}")
    if root.tag not in (f"{{{SVG_NS}}}svg", "svg"):
        raise SystemExit("SVG root element must be <svg> in the SVG namespace")
    if root.tag == "svg":
        raise SystemExit('SVG root must declare xmlns="http://www.w3.org/2000/svg" (PowerPoint ignores namespace-less SVG)')
    if not root.get("viewBox"):
        raise SystemExit("SVG must declare a viewBox so it scales cleanly to the object box")
    problems = []
    for element in root.iter():
        local = element.tag.split("}")[-1]
        if local in FORBIDDEN_SVG_TAGS:
            # <image> is the important one: embedding a raster (e.g. a source
            # crop) inside an SVG would launder a crop as a vector trace.
            problems.append(f"forbidden <{local}> element: traces must be pure vector geometry")
        if local == "text" and not allow_identity_text:
            problems.append("<text> element: ordinary text must be a native text box; pass --allow-identity-text only for logo wordmarks")
        for key, value in element.attrib.items():
            if key.split("}")[-1] == "href" and not str(value).startswith("#"):
                problems.append(f"external reference {value!r}: SVG must be self-contained")
    if problems:
        raise SystemExit("SVG rejected:\n- " + "\n- ".join(sorted(set(problems))))
    view_box = str(root.get("viewBox")).replace(",", " ").split()
    if len(view_box) != 4:
        raise SystemExit("SVG viewBox must be four numbers: min-x min-y width height")
    return text, []


def render_svg(svg_text: str, width: int, height: int):
    import io

    from PIL import Image

    try:
        import resvg_py
    except ImportError:
        raise SystemExit("SVG rendering needs resvg-py: reinstall the CLI (`uv tool install --force --editable <skill-root>/cli`)")
    png = bytes(resvg_py.svg_to_bytes(svg_string=svg_text, width=width, height=height))
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    if image.size != (width, height):
        # resvg keeps the viewBox aspect; fit it centred into the exact box.
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        image.thumbnail((width, height))
        canvas.paste(image, ((width - image.width) // 2, (height - image.height) // 2), image)
        image = canvas
    return image


def fidelity(source_crop, render_rgba, threshold: int = 48) -> dict:
    """Compare 'ink' (pixels that differ from the local background) masks."""
    import numpy as np
    from PIL import Image

    bg = border_color(source_crop)
    size = source_crop.size
    rendered = Image.new("RGB", size, bg)
    rendered.paste(render_rgba.resize(size, Image.LANCZOS), (0, 0), render_rgba.resize(size, Image.LANCZOS))
    src = np.asarray(source_crop.convert("RGB")).astype(int)
    out = np.asarray(rendered).astype(int)
    bg_arr = np.array(bg)
    src_ink = np.abs(src - bg_arr).sum(axis=2) > threshold
    out_ink = np.abs(out - bg_arr).sum(axis=2) > threshold
    union = np.logical_or(src_ink, out_ink).sum()
    inter = np.logical_and(src_ink, out_ink).sum()
    both = np.logical_and(src_ink, out_ink)
    color_delta = float(np.abs(src[both] - out[both]).mean()) if both.any() else None
    return {
        "ink_iou": round(float(inter / union), 3) if union else 1.0,
        "source_ink_px": int(src_ink.sum()),
        "trace_ink_px": int(out_ink.sum()),
        "mean_color_delta_on_shared_ink": round(color_delta, 1) if color_delta is not None else None,
        "background_estimate": "#%02x%02x%02x" % bg,
        "rendered_on_background": rendered,
    }


def cmd_svg(args) -> int:
    page_dir = Path(args.page_dir).resolve()
    svg_path = resolve_in_page(page_dir, args.svg)
    if not svg_path.exists():
        raise SystemExit(f"SVG not found: {svg_path}")
    box = parse_box(args.box)
    svg_text, warnings = validate_svg(svg_path, args.allow_identity_text)
    width, height = max(1, int(round(box[2] * args.scale))), max(1, int(round(box[3] * args.scale)))
    render = render_svg(svg_text, width, height)
    png_path = svg_path.with_suffix(".png")
    render.save(png_path)

    source = load_source(page_dir)
    crop = source.crop(clamp_box(box, source.size))
    metrics = fidelity(crop, render)
    compare_path = svg_path.with_name(svg_path.stem + ".compare.png")
    side_by_side(crop, metrics.pop("rendered_on_background"), compare_path, ("source", "svg trace"))
    if metrics["ink_iou"] < 0.5:
        warnings.append("ink_iou below 0.5: the trace differs substantially from the source object; inspect the compare image")

    asset_id = args.id or svg_path.stem
    png_rel, svg_rel = rel(page_dir, png_path), rel(page_dir, svg_path)
    record_job(page_dir, {
        "job_id": asset_id,
        "role": "svg_trace",
        "backend": "claude-svg",
        "output": png_rel,
        "output_sha256": sha256_file(png_path),
        "svg": svg_rel,
        "svg_sha256": sha256_file(svg_path),
        "source_box_px": box,
        "fidelity": metrics,
    })
    fragment = {
        "schema_version": 1,
        "type": "claude-svg-fragment",
        "images": [{
            "id": asset_id,
            "path": png_rel,
            "svg_path": svg_rel,
            "box_px": box,
            "alt": args.alt or f"{args.object_type} {asset_id}",
            "z_index": args.z_index,
        }],
        "asset_provenance": [{
            "path": png_rel,
            "source": svg_rel,
            "source_type": "claude-svg",
            "source_box_px": box,
            "provenance_note": "Vector trace of the source object authored by the agent; PNG is the rendered fallback, the SVG is embedded in the PPTX.",
        }],
        "visual_inventory": [{
            "id": asset_id,
            "role": "foreground",
            "object_type": args.object_type,
            "source_type": "claude-svg",
            "asset": png_rel,
            "box_px": box,
        }],
    }
    fragment_path = write_fragment(page_dir, args.fragment, asset_id, fragment)
    print(json.dumps({
        "id": asset_id,
        "png": str(png_path),
        "svg": str(svg_path),
        "compare": str(compare_path),
        "fragment": str(fragment_path),
        "fidelity": metrics,
        "warnings": warnings,
    }, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- inpaint


def inpaint_boxes(image, boxes: list[list[float]], pad: int, radius: int):
    import cv2
    import numpy as np
    from PIL import Image

    arr = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR)
    mask = np.zeros(arr.shape[:2], dtype=np.uint8)
    for x, y, w, h in boxes:
        left, top = max(0, int(x) - pad), max(0, int(y) - pad)
        right, bottom = min(arr.shape[1], int(x + w) + pad), min(arr.shape[0], int(y + h) + pad)
        mask[top:bottom, left:right] = 255
    out = cv2.inpaint(arr, mask, radius, cv2.INPAINT_TELEA)
    return Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))


def hint_boxes(page_dir: Path) -> list[list[float]]:
    hints_path = page_dir / "text_hints.json"
    if not hints_path.exists():
        raise SystemExit("--boxes-from-hints needs text_hints.json; run `editppt page hints <page_dir>`")
    hints = json.loads(hints_path.read_text(encoding="utf-8"))
    lines = hints.get("lines") or hints.get("text_lines") or []
    return [list(map(float, line["box_px"])) for line in lines if line.get("box_px")]


def cmd_inpaint(args) -> int:
    page_dir = Path(args.page_dir).resolve()
    boxes = parse_boxes(args.boxes)
    if args.boxes_from_hints:
        boxes += hint_boxes(page_dir)
    if not boxes:
        raise SystemExit("inpaint needs at least one box (--boxes and/or --boxes-from-hints); a clean base with nothing removed is just source.png")
    source = load_source(page_dir)
    region = parse_box(args.region) if args.region else [0, 0, source.width, source.height]
    cleaned = inpaint_boxes(source, boxes, args.pad, args.radius)
    crop_box = clamp_box(region, source.size)
    out_path = resolve_in_page(page_dir, args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned.crop(crop_box).save(out_path)
    compare_path = out_path.with_name(out_path.stem + ".compare.png")
    side_by_side(source.crop(crop_box), cleaned.crop(crop_box), compare_path, ("source", "inpainted clean base"))

    asset_id = args.id or out_path.stem
    out_rel = rel(page_dir, out_path)
    padded = [[b[0] - args.pad, b[1] - args.pad, b[2] + 2 * args.pad, b[3] + 2 * args.pad] for b in boxes]
    record_job(page_dir, {
        "job_id": asset_id,
        "role": "clean_base",
        "backend": "local-inpaint",
        "output": out_rel,
        "output_sha256": sha256_file(out_path),
        "inpainted_boxes_px": padded,
        "region_px": region,
    })
    fragment = {
        "schema_version": 1,
        "type": "local-inpaint-fragment",
        "images": [{"id": asset_id, "path": out_rel, "box_px": region, "alt": "clean base", "z_index": args.z_index}],
        "asset_provenance": [{
            "path": out_rel,
            "source": "source.png",
            "source_type": "local-inpaint-clean-base",
            "inpainted_boxes_px": padded,
            "provenance_note": "source.png with rebuilt text/objects removed by local OpenCV inpainting.",
        }],
    }
    fragment_path = write_fragment(page_dir, args.fragment, asset_id, fragment)
    print(json.dumps({"id": asset_id, "clean_base": str(out_path), "compare": str(compare_path), "fragment": str(fragment_path), "boxes": len(boxes)}, indent=2))
    return 0


# ---------------------------------------------------------------- photo-region


def cmd_photo_region(args) -> int:
    page_dir = Path(args.page_dir).resolve()
    box = parse_box(args.box)
    source = load_source(page_dir)
    if box[2] * box[3] >= 0.9 * source.width * source.height:
        raise SystemExit("photo-region covers >=90% of the page; a full-page source raster is not an editable reconstruction")
    clean = parse_boxes(args.clean_boxes)
    image = inpaint_boxes(source, clean, args.pad, 5) if clean else source
    out_path = resolve_in_page(page_dir, args.out or f"assets/{args.id}.png")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.crop(clamp_box(box, source.size)).save(out_path)

    out_rel = rel(page_dir, out_path)
    padded = [[b[0] - args.pad, b[1] - args.pad, b[2] + 2 * args.pad, b[3] + 2 * args.pad] for b in clean]
    record_job(page_dir, {
        "job_id": args.id,
        "role": "photo_region",
        "backend": "source-photo-region",
        "object_type": args.object_type,
        "output": out_rel,
        "output_sha256": sha256_file(out_path),
        "source_box_px": box,
        "inpainted_boxes_px": padded,
    })
    fragment = {
        "schema_version": 1,
        "type": "source-photo-region-fragment",
        "images": [{"id": args.id, "path": out_rel, "box_px": box, "alt": args.alt or f"{args.object_type} {args.id}", "z_index": args.z_index}],
        "asset_provenance": [{
            "path": out_rel,
            "source": "source.png",
            "source_type": "source-photo-region",
            "object_type": args.object_type,
            "source_box_px": box,
            "inpainted_boxes_px": padded,
            "provenance_note": f"Rectangular {args.object_type} region extracted from source.png; photographic content cannot be traced as vectors.",
        }],
        "visual_inventory": [{
            "id": args.id,
            "role": "foreground",
            "object_type": args.object_type,
            "source_type": "source-photo-region",
            "asset": out_rel,
            "box_px": box,
        }],
    }
    fragment_path = write_fragment(page_dir, args.fragment, args.id, fragment)
    print(json.dumps({"id": args.id, "image": str(out_path), "fragment": str(fragment_path)}, indent=2))
    return 0


# ---------------------------------------------------------------- zoom


def dominant_colors(image, count: int = 5) -> list[dict]:
    from PIL import Image

    quantized = image.convert("RGB").quantize(colors=count, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette() or []
    total = image.width * image.height
    colors = []
    for pixels, index in sorted(quantized.getcolors() or [], reverse=True):
        r, g, b = palette[index * 3: index * 3 + 3]
        colors.append({"hex": "#%02x%02x%02x" % (r, g, b), "share": round(pixels / total, 3)})
    return colors


def cmd_zoom(args) -> int:
    """Magnified crop with a labelled grid in box-local source pixels.

    Write the SVG with viewBox="0 0 <w> <h>" (the box size) and the grid labels
    are exactly the SVG coordinates to use.
    """
    from PIL import Image, ImageDraw

    page_dir = Path(args.page_dir).resolve()
    box = parse_box(args.box)
    source = load_source(page_dir)
    left, top, right, bottom = clamp_box(box, source.size)
    crop = source.crop((left, top, right, bottom))
    scale = max(1, int(args.size / max(crop.width, crop.height)))
    big = crop.resize((crop.width * scale, crop.height * scale), Image.NEAREST)
    margin = 28
    canvas = Image.new("RGB", (big.width + margin, big.height + margin), "white")
    canvas.paste(big, (margin, margin))
    draw = ImageDraw.Draw(canvas)
    step = args.grid or max(1, round(max(crop.width, crop.height) / 10))
    for value in range(0, max(crop.width, crop.height) + 1, step):
        if value <= crop.width:
            x = margin + value * scale
            draw.line([(x, margin), (x, canvas.height)], fill=(0, 160, 255), width=1)
            draw.text((x + 2, 4), str(value), fill=(0, 90, 200))
        if value <= crop.height:
            y = margin + value * scale
            draw.line([(margin, y), (canvas.width, y)], fill=(0, 160, 255), width=1)
            draw.text((2, y + 2), str(value), fill=(0, 90, 200))
    out_path = resolve_in_page(page_dir, args.out or f"assets/zoom_{left}_{top}_{right - left}x{bottom - top}.png")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    print(json.dumps({
        "zoom": str(out_path),
        "box_px": [left, top, right - left, bottom - top],
        "svg_viewBox": f"0 0 {right - left} {bottom - top}",
        "grid_step_px": step,
        "magnification": scale,
        "dominant_colors": dominant_colors(crop),
        "background_estimate": "#%02x%02x%02x" % border_color(crop),
    }, indent=2))
    return 0


# ---------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="editppt image", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    svg = sub.add_parser("svg", help="Validate/render an agent-authored SVG trace of one foreground object.")
    svg.add_argument("page_dir")
    svg.add_argument("--svg", required=True, help="SVG file inside the page dir, e.g. assets/icon_lock.svg")
    svg.add_argument("--box", required=True, help="Source-pixel box x,y,width,height of the traced object")
    svg.add_argument("--id", help="Asset id (default: SVG file stem)")
    svg.add_argument("--object-type", default="icon", help="icon, pictogram, logo-mark, badge, hand-drawn-mark, flat-illustration, ...")
    svg.add_argument("--alt")
    svg.add_argument("--z-index", type=int, default=30)
    svg.add_argument("--scale", type=float, default=4.0, help="PNG fallback pixel density relative to the source box (default 4)")
    svg.add_argument("--allow-identity-text", action="store_true", help="Permit <text> for logo wordmarks only")
    svg.add_argument("--fragment", help="Fragment output path (default assets/<id>.fragment.json)")
    svg.set_defaults(func=cmd_svg)

    zoom = sub.add_parser("zoom", help="Magnified source crop with a coordinate grid and dominant colours, for tracing.")
    zoom.add_argument("page_dir")
    zoom.add_argument("--box", required=True, help="Source-pixel box x,y,width,height")
    zoom.add_argument("--size", type=int, default=640, help="Target long-edge size of the magnified crop")
    zoom.add_argument("--grid", type=int, help="Grid step in source pixels (default: ~10 lines)")
    zoom.add_argument("--out")
    zoom.set_defaults(func=cmd_zoom)

    inpaint = sub.add_parser("inpaint", help="Build a clean base by inpainting rebuilt text/objects out of source.png.")
    inpaint.add_argument("page_dir")
    inpaint.add_argument("--boxes", help="Semicolon-separated source-pixel boxes: 'x,y,w,h;x,y,w,h'")
    inpaint.add_argument("--boxes-from-hints", action="store_true", help="Also remove every line box in text_hints.json")
    inpaint.add_argument("--region", help="Crop the clean base to this x,y,w,h region (default: full page)")
    inpaint.add_argument("--out", default="assets/clean_base.png")
    inpaint.add_argument("--id")
    inpaint.add_argument("--pad", type=int, default=4)
    inpaint.add_argument("--radius", type=int, default=5)
    inpaint.add_argument("--z-index", type=int, default=0)
    inpaint.add_argument("--fragment")
    inpaint.set_defaults(func=cmd_inpaint)

    photo = sub.add_parser("photo-region", help="Extract a rectangular photo/screenshot-like object from source.png.")
    photo.add_argument("page_dir")
    photo.add_argument("--box", required=True)
    photo.add_argument("--id", required=True)
    photo.add_argument("--object-type", required=True, choices=PHOTO_OBJECT_TYPES)
    photo.add_argument("--clean-boxes", help="Boxes of rebuilt text/objects inside the region to inpaint out first")
    photo.add_argument("--pad", type=int, default=4)
    photo.add_argument("--out")
    photo.add_argument("--alt")
    photo.add_argument("--z-index", type=int, default=20)
    photo.add_argument("--fragment")
    photo.set_defaults(func=cmd_photo_region)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
