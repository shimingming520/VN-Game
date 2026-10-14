from pathlib import Path
import unittest


class GeneratedContentContractTests(unittest.TestCase):
    def test_branch_writer_never_emits_placeholder_prose(self):
        source = (
            Path(__file__).resolve().parents[2]
            / "services"
            / "story_planner"
            / "core"
            / "branch_designer.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn('黑暗吞噬了一切。"', source)
        self.assertNotIn('故事在此延伸。"', source)


if __name__ == "__main__":
    unittest.main()
