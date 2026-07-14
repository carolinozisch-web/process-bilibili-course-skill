from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


pipeline = load_module(
    "video_pipeline",
    "process-bilibili-course/scripts/video_pipeline.py",
)
course_utils = load_module(
    "course_utils",
    "process-bilibili-course/scripts/course_utils.py",
)


class PipelineTests(unittest.TestCase):
    def test_extract_bvid(self):
        self.assertEqual(
            pipeline.extract_bvid("https://www.bilibili.com/video/BV19x411X7C6?p=2"),
            "BV19x411X7C6",
        )

    def test_share_text_extracts_xiaohongshu_url(self):
        text = "标题 https://xhslink.com/o/example123 复制后打开小红书"
        self.assertEqual(
            pipeline.extract_input_url(text),
            "https://xhslink.com/o/example123",
        )

    def test_detect_platform(self):
        self.assertEqual(pipeline.detect_platform("https://b23.tv/example"), "bilibili")
        self.assertEqual(pipeline.detect_platform("https://xhslink.com/o/example"), "xiaohongshu")

    def test_parse_xiaohongshu_durations(self):
        self.assertEqual(pipeline.parse_duration("05:01"), 301)
        self.assertEqual(pipeline.parse_duration(301100), 301.1)
        self.assertEqual(pipeline.parse_duration("PT5M1S"), 301)

    def test_xiaohongshu_share_tokens_are_not_persisted(self):
        input_url = "https://www.xiaohongshu.com/discovery/item/abc?xsec_token=secret"
        canonical = "https://www.xiaohongshu.com/discovery/item/abc"
        self.assertEqual(
            pipeline.manifest_source_url(input_url, "xiaohongshu", canonical),
            canonical,
        )
        self.assertEqual(
            pipeline.manifest_source_url("https://xhslink.com/o/example", "xiaohongshu", canonical),
            canonical,
        )

    def test_xiaohongshu_json_ld_is_parsed(self):
        source = '<script type="application/ld+json">{"@type":"VideoObject","name":"示例","duration":"00:42"}</script>'
        self.assertEqual(list(pipeline.iter_json_ld(source))[0]["name"], "示例")

    def test_safe_name_removes_windows_invalid_characters(self):
        self.assertEqual(pipeline.safe_name('Course: A/B?*'), "Course- A-B")

    def test_compilation_is_flagged(self):
        pages = [
            {"page": 1, "part": "第一课", "duration": 600},
            {"page": 2, "part": "第二课", "duration": 620},
            {"page": 3, "part": "全集", "duration": 4000},
        ]
        marked = pipeline.mark_compilations(pages)
        self.assertFalse(marked[0]["review_required"])
        self.assertTrue(marked[2]["review_required"])

    def test_workspace_environment_override(self):
        with patch.dict(os.environ, {"VIDEO_SUMMARY_HOME": "example-workspace"}):
            self.assertEqual(pipeline.default_workspace(), "example-workspace")

    def test_layout_creates_expected_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = pipeline.layout(Path(directory), "示例课程")
            self.assertTrue(paths["notes"].is_dir())
            self.assertTrue(paths["transcripts"].is_dir())
            self.assertEqual(paths["manifest"].name, "course-manifest.json")


class CourseUtilsTests(unittest.TestCase):
    def test_broken_markdown_link_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.md").write_text("[missing](<missing.txt>)\n", encoding="utf-8")
            problems = course_utils.markdown_broken_links(root)
            self.assertEqual(len(problems), 1)


if __name__ == "__main__":
    unittest.main()
