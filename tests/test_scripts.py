from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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

    def prepare_course(self, root: Path, episode_count: int):
        course = "示例课程"
        course_root, work_root, manifest_path = course_utils.roots(root, course)
        (course_root / "01-详细笔记").mkdir(parents=True)
        (work_root / "时间戳逐字稿").mkdir(parents=True)
        episodes = []
        for number in range(1, episode_count + 1):
            episode_id = f"{number:02d}"
            episodes.append({"page": number, "title": f"第 {number} 课", "state": "notes_done"})
            (course_root / "01-详细笔记" / f"{episode_id}-notes.md").write_text(
                f"# [第 {number} 集：第 {number} 课（详细整理）](<../02-逐字稿/{episode_id}-transcript.txt>)\n\n正文。\n",
                encoding="utf-8",
            )
            (work_root / "时间戳逐字稿" / f"{episode_id}-transcript-timestamped.md").write_text(
                "[00:00] 示例\n", encoding="utf-8"
            )
        manifest_path.write_text(
            __import__("json").dumps({"episodes": episodes}, ensure_ascii=False), encoding="utf-8"
        )
        return course, course_root

    def test_index_adds_navigation_only_for_multi_episode_series(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            course, course_root = self.prepare_course(root, 2)
            args = SimpleNamespace(workspace=str(root), course=course)
            course_utils.command_index(args)
            first = (course_root / "01-详细笔记" / "01-notes.md").read_text(encoding="utf-8")
            last = (course_root / "01-详细笔记" / "02-notes.md").read_text(encoding="utf-8")
            self.assertIn("01-transcript-timestamped.md", first.splitlines()[0])
            self.assertEqual(first.count(course_utils.NAV_START), 2)
            self.assertIn("[下一篇 →](<02-notes.md>)", first)
            self.assertEqual(last.count(course_utils.NAV_START), 2)
            self.assertNotIn("下一篇", last)
            course_utils.command_index(args)
            rerun = (course_root / "01-详细笔记" / "01-notes.md").read_text(encoding="utf-8")
            self.assertEqual(rerun.count(course_utils.NAV_START), 2)

    def test_index_omits_navigation_for_single_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            course, course_root = self.prepare_course(root, 1)
            args = SimpleNamespace(workspace=str(root), course=course)
            course_utils.command_index(args)
            note = (course_root / "01-详细笔记" / "01-notes.md").read_text(encoding="utf-8")
            self.assertIn("01-transcript-timestamped.md", note.splitlines()[0])
            self.assertNotIn(course_utils.NAV_START, note)
            self.assertNotIn("回到目录", note)
            self.assertNotIn("下一篇", note)


if __name__ == "__main__":
    unittest.main()
