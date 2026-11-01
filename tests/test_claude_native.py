"""Claude-native backend: SVG traces, local inpaint clean bases, photo regions."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
CLI_DIR = ROOT / "skills/image-to-editable-ppt/cli"
RUNTIME_DIR = CLI_DIR / "editppt/runtime"
sys.path.insert(0, str(RUNTIME_DIR))

from validate_pptx import ALLOWED_SOURCE_TYPES, claude_native_violations, quality_contract_violations  # noqa: E402

CLI_ENV = os.environ.copy()
CLI_ENV["PYTHONPATH"] = str(CLI_DIR) + (os.pathsep + CLI_ENV["PYTHONPATH"] if CLI_ENV.get("PYTHONPATH") else "")

CIRCLE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <circle cx="50" cy="50" r="40" fill="#cc3333"/>
</svg>
"""


def editppt(*args):
    return subprocess.run([sys.executable, "-m", "editppt.cli", *map(str, args)], env=CLI_ENV, text=True, capture_output=True)


def make_page(tmp):
    """A 1600x900 page: red disc 'icon' at (200,200,100,100), title text, photo-ish noise block."""
    page = Path(tmp) / "page_001"
    (page / "assets").mkdir(parents=True)
    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse([210, 210, 290, 290], fill="#cc3333")
    draw.rectangle([100, 50, 700, 120], fill="#222222")  # stands in for baked title text
    for x in range(900, 1400, 10):  # textured block
        for y in range(300, 700, 10):
            draw.rectangle([x, y, x + 9, y + 9], fill=((x * 7) % 255, (y * 3) % 255, 120))
    image.save(page / "source.png")
    return page


def page_manifest(images=(), provenance=(), inventory=(), text_boxes=None):
    return {
        "schema_version": 1,
        "source": {"width_px": 1600, "height_px": 900},
        "slide": {"width": 13.333, "height": 7.5},
        "content_box": {"left": 0, "top": 0, "width": 13.333, "height": 7.5},
        "text_boxes": text_boxes if text_boxes is not None else [{"text": "Title", "box_px": [100, 50, 600, 70], "font_size": 28}],
        "shapes": [],
        "images": list(images),
        "asset_provenance": list(provenance),
        "visual_inventory": list(inventory),
        "background_strategy": {"mode": "native-or-script", "comparison_note": "white background"},
        "quality_checks": {
            "font_size_calibrated": True,
            "visual_inventory_matched": True,
            "background_strategy_checked": True,
            "shape_corner_geometry_checked": True,
        },
    }


class SvgTraceCommandTest(unittest.TestCase):
    def test_svg_trace_renders_records_and_scores_fidelity(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = make_page(tmp)
            (page / "assets/icon_dot.svg").write_text(CIRCLE_SVG, encoding="utf-8")
            result = editppt("image", "svg", page, "--svg", "assets/icon_dot.svg", "--box", "200,200,100,100", "--object-type", "icon")
            self.assertEqual(0, result.returncode, result.stderr)
            summary = json.loads(result.stdout)
            self.assertGreater(summary["fidelity"]["ink_iou"], 0.8)
            self.assertTrue((page / "assets/icon_dot.png").exists())
            self.assertTrue((page / "assets/icon_dot.compare.png").exists())
            fragment = json.loads((page / "assets/icon_dot.fragment.json").read_text())
            self.assertEqual("assets/icon_dot.svg", fragment["images"][0]["svg_path"])
            self.assertEqual("claude-svg", fragment["asset_provenance"][0]["source_type"])
            jobs = json.loads((page / "imagegen-jobs.json").read_text())
            self.assertEqual("claude-svg", jobs["jobs"][0]["backend"])

    def test_svg_with_embedded_raster_is_rejected(self):
        # An <image> inside the SVG could smuggle a source crop in as a "vector".
        with tempfile.TemporaryDirectory() as tmp:
            page = make_page(tmp)
            (page / "assets/bad.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><image href="data:image/png;base64,AAAA" width="10" height="10"/></svg>',
                encoding="utf-8",
            )
            result = editppt("image", "svg", page, "--svg", "assets/bad.svg", "--box", "200,200,100,100")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("forbidden <image>", result.stderr)

    def test_svg_text_requires_identity_flag_and_namespace_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = make_page(tmp)
            (page / "assets/t.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><text>Hi</text></svg>', encoding="utf-8")
            self.assertIn("<text>", editppt("image", "svg", page, "--svg", "assets/t.svg", "--box", "0,0,10,10").stderr)
            (page / "assets/n.svg").write_text('<svg viewBox="0 0 10 10"><circle r="3"/></svg>', encoding="utf-8")
            self.assertIn("xmlns", editppt("image", "svg", page, "--svg", "assets/n.svg", "--box", "0,0,10,10").stderr)


class ZoomCommandTest(unittest.TestCase):
    def test_zoom_reports_viewbox_and_exact_colors(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = make_page(tmp)
            result = editppt("image", "zoom", page, "--box", "200,200,100,100")
            self.assertEqual(0, result.returncode, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual("0 0 100 100", summary["svg_viewBox"])
            self.assertIn("#cc3333", [c["hex"] for c in summary["dominant_colors"]])
            self.assertEqual("#ffffff", summary["background_estimate"])
            self.assertTrue(Path(summary["zoom"]).exists())


class SvgPowerPointRiskTest(unittest.TestCase):
    def test_style_and_filter_produce_warnings_not_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = make_page(tmp)
            (page / "assets/s.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><style>.a{fill:#cc3333}</style>'
                '<filter id="f"><feGaussianBlur stdDeviation="1"/></filter><circle class="a" cx="50" cy="50" r="40" fill="#cc3333"/></svg>',
                encoding="utf-8",
            )
            result = editppt("image", "svg", page, "--svg", "assets/s.svg", "--box", "200,200,100,100")
            self.assertEqual(0, result.returncode, result.stderr)
            warnings = " ".join(json.loads(result.stdout)["warnings"])
            self.assertIn("<style>", warnings)
            self.assertIn("<filter>", warnings)
            self.assertIn("CSS class", warnings)


class SvgEmbeddingTest(unittest.TestCase):
    def test_page_build_embeds_svg_blip_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = make_page(tmp)
            (page / "assets/icon_dot.svg").write_text(CIRCLE_SVG, encoding="utf-8")
            self.assertEqual(0, editppt("image", "svg", page, "--svg", "assets/icon_dot.svg", "--box", "200,200,100,100").returncode)
            fragment = json.loads((page / "assets/icon_dot.fragment.json").read_text())
            manifest = page_manifest(fragment["images"], fragment["asset_provenance"], fragment["visual_inventory"])
            (page / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            build = editppt("page", "build", page)
            self.assertEqual(0, build.returncode, build.stderr)
            with zipfile.ZipFile(page / "page.pptx") as z:
                names = z.namelist()
                slide = z.read("ppt/slides/slide1.xml").decode()
                rels = z.read("ppt/slides/_rels/slide1.xml.rels").decode()
                types = z.read("[Content_Types].xml").decode()
            self.assertIn("ppt/media/image1.png", names)
            self.assertIn("ppt/media/svg1.svg", names)
            self.assertIn("asvg:svgBlip", slide)
            self.assertIn('r:embed="rIdSvg2"', slide)
            self.assertIn('Id="rIdSvg2"', rels)
            self.assertIn('Extension="svg"', types)
            report_path = page / "validation.json"
            validate = editppt("page", "validate", page, "--report", report_path)
            report = json.loads(report_path.read_text())
            self.assertTrue(report["passed"], json.dumps(report, indent=2)[:3000])
            self.assertEqual(0, validate.returncode)

    def test_svg_media_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = make_page(tmp)
            (page / "assets/icon_dot.svg").write_text(CIRCLE_SVG, encoding="utf-8")
            editppt("image", "svg", page, "--svg", "assets/icon_dot.svg", "--box", "200,200,100,100")
            fragment = json.loads((page / "assets/icon_dot.fragment.json").read_text())
            (page / "manifest.json").write_text(json.dumps(page_manifest(fragment["images"], fragment["asset_provenance"], fragment["visual_inventory"])), encoding="utf-8")
            editppt("page", "build", page)
            (page / "assets/icon_dot.svg").write_text(CIRCLE_SVG.replace("#cc3333", "#000000"), encoding="utf-8")
            editppt("page", "validate", page, "--report", page / "validation.json")
            report = json.loads((page / "validation.json").read_text())
            self.assertFalse(report["passed"])
            self.assertEqual("ppt/media/svg1.svg", report["media_hash_mismatches"][0]["media"])


class NativeProvenanceGuardTest(unittest.TestCase):
    def test_new_source_types_are_allowed(self):
        for source_type in ("claude-svg", "source-photo-region", "local-inpaint-clean-base"):
            self.assertIn(source_type, ALLOWED_SOURCE_TYPES)

    def test_claude_svg_foreground_passes_quality_contract(self):
        manifest = page_manifest(
            [{"path": "assets/i.png", "svg_path": "assets/i.svg", "box_px": [0, 0, 10, 10]}],
            [{"path": "assets/i.png", "source": "assets/i.svg", "source_type": "claude-svg", "provenance_note": "trace"}],
            [{"id": "i", "role": "foreground", "object_type": "icon", "source_type": "claude-svg", "asset": "assets/i.png"}],
        )
        self.assertEqual([], quality_contract_violations(manifest))
        self.assertEqual([], claude_native_violations(manifest))

    def test_claude_svg_must_embed_its_svg(self):
        manifest = page_manifest(
            [{"path": "assets/i.png", "box_px": [0, 0, 10, 10]}],
            [{"path": "assets/i.png", "source": "assets/i.svg", "source_type": "claude-svg", "provenance_note": "trace"}],
        )
        self.assertEqual(["images.svg_path"], [v["field"] for v in claude_native_violations(manifest)])

    def photo_manifest(self, object_type="photo", cleaned=(), text_box=(950, 350, 200, 50)):
        return page_manifest(
            [{"path": "assets/p.png", "box_px": [900, 300, 500, 400]}],
            [{"path": "assets/p.png", "source": "source.png", "source_type": "source-photo-region", "object_type": object_type,
              "inpainted_boxes_px": [list(b) for b in cleaned], "provenance_note": "photo"}],
            text_boxes=[{"text": "Caption", "box_px": list(text_box), "font_size": 14}],
        )

    def test_photo_region_with_baked_text_underneath_is_rejected(self):
        reasons = " ".join(v["reason"] for v in claude_native_violations(self.photo_manifest()))
        self.assertIn("baked duplicate text", reasons)

    def test_photo_region_with_inpainted_text_area_passes(self):
        self.assertEqual([], claude_native_violations(self.photo_manifest(cleaned=[(946, 346, 208, 58)])))

    def test_photo_region_rejects_non_photo_objects(self):
        # Icons must be traced, not cropped: that is the loophole upstream closed.
        violations = claude_native_violations(self.photo_manifest(object_type="icon", text_box=(0, 0, 10, 10)))
        self.assertEqual(["asset_provenance[0].object_type"], [v["field"] for v in violations])

    def test_photo_region_rejects_whole_page(self):
        manifest = self.photo_manifest(text_box=(0, 0, 10, 10))
        manifest["images"][0]["box_px"] = [0, 0, 1600, 900]
        self.assertIn("whole page", " ".join(v["reason"] for v in claude_native_violations(manifest)))

    def test_inpaint_clean_base_must_remove_something_and_cover_text(self):
        manifest = page_manifest(
            [{"path": "assets/clean_base.png", "box_px": [0, 0, 1600, 900]}],
            [{"path": "assets/clean_base.png", "source": "source.png", "source_type": "local-inpaint-clean-base", "inpainted_boxes_px": [], "provenance_note": "x"}],
        )
        fields = [v["field"] for v in claude_native_violations(manifest)]
        self.assertIn("asset_provenance[0].inpainted_boxes_px", fields)
        manifest["asset_provenance"][0]["inpainted_boxes_px"] = [[96, 46, 608, 78]]
        self.assertEqual([], claude_native_violations(manifest))


class InpaintAndPhotoCommandTest(unittest.TestCase):
    def test_inpaint_removes_box_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = make_page(tmp)
            result = editppt("image", "inpaint", page, "--boxes", "100,50,600,70")
            self.assertEqual(0, result.returncode, result.stderr)
            cleaned = Image.open(page / "assets/clean_base.png").convert("RGB")
            self.assertGreater(sum(cleaned.getpixel((400, 85))), 600)  # dark bar is gone
            fragment = json.loads((page / "assets/clean_base.fragment.json").read_text())
            self.assertEqual("local-inpaint-clean-base", fragment["asset_provenance"][0]["source_type"])

    def test_inpaint_without_boxes_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertNotEqual(0, editppt("image", "inpaint", make_page(tmp)).returncode)

    def test_photo_region_extracts_and_restricts_object_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = make_page(tmp)
            ok = editppt("image", "photo-region", page, "--box", "900,300,500,400", "--id", "texture", "--object-type", "textured-image-block")
            self.assertEqual(0, ok.returncode, ok.stderr)
            self.assertEqual((500, 400), Image.open(page / "assets/texture.png").size)
            bad = editppt("image", "photo-region", page, "--box", "200,200,100,100", "--id", "icon", "--object-type", "icon")
            self.assertNotEqual(0, bad.returncode)


class LineSpacingTest(unittest.TestCase):
    def test_line_height_is_emitted_as_paragraph_spacing(self):
        from build_pptx_from_manifest import text_box_xml
        xml = text_box_xml(2, {"text": "a\nb", "left": 0, "top": 0, "width": 2, "height": 1, "font_size": 12, "line_height": 1.8})
        self.assertEqual(2, xml.count('<a:spcPct val="150000"/>'))  # 1.8 pitch / 1.2 single = 150%
        plain = text_box_xml(2, {"text": "a", "left": 0, "top": 0, "width": 2, "height": 1, "font_size": 12})
        self.assertNotIn("lnSpc", plain)


class ClaudeNativeBackendContractTest(unittest.TestCase):
    def test_prepare_records_claude_native_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "slide.png"
            Image.new("RGB", (1600, 900), "white").save(source)
            result = editppt("prepare", source, "--out-root", Path(tmp) / "out", "--image-backend", "claude-native", "--no-text-hints")
            self.assertEqual(0, result.returncode, result.stderr)
            deck = json.loads(next((Path(tmp) / "out").glob("*/deck_manifest.json")).read_text())
            self.assertEqual("claude-native", deck["image_backend"]["backend_id"])
            self.assertFalse(deck["image_backend"]["requires_openai_api_key"])
            request = json.loads(next((Path(tmp) / "out").glob("*/pages/page_001/page_request.json")).read_text())
            self.assertEqual("claude-native", request["image_backend"]["backend_id"])


if __name__ == "__main__":
    unittest.main()
