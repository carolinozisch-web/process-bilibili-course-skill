from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "process-bilibili-course"


class SkillStructureTests(unittest.TestCase):
    def test_required_files_exist(self):
        for relative in (
            "SKILL.md",
            "agents/openai.yaml",
            "references/failure-handling.md",
            "references/note-standard.md",
            "references/output-layout.md",
            "scripts/bilibili_pipeline.py",
            "scripts/video_pipeline.py",
            "scripts/course_utils.py",
        ):
            self.assertTrue((SKILL / relative).is_file(), relative)

    def test_frontmatter_has_only_name_and_description(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        frontmatter = text.split("---", 2)[1]
        keys = re.findall(r"^([a-z_]+):", frontmatter, flags=re.MULTILINE)
        self.assertEqual(keys, ["name", "description"])
        self.assertIn("name: process-bilibili-course", frontmatter)

    def test_public_files_contain_no_personal_windows_path(self):
        path_parts = ("C:", "Users", "Caroline")
        prohibited = ("\\".join(path_parts), "/".join(path_parts))
        for path in ROOT.rglob("*"):
            if ".git" in path.parts:
                continue
            if not path.is_file() or path.suffix.lower() in {".pyc", ".png", ".jpg"}:
                continue
            text = path.read_text(encoding="utf-8")
            for value in prohibited:
                self.assertNotIn(value, text, str(path))

    def test_repository_contains_no_media(self):
        media_extensions = {".m4s", ".mp3", ".mp4", ".wav", ".webm"}
        found = [path for path in ROOT.rglob("*") if path.suffix.lower() in media_extensions]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
