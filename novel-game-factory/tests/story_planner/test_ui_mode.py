import importlib.util
from pathlib import Path
import unittest


def _load_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "services"
        / "story_planner"
        / "core"
        / "ui_mode.py"
    )
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("ui_mode_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ui_mode = _load_module()


class UIModeInferenceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ui_mode, "UI_mode 推断模块尚未实现")

    def test_existing_values_are_normalized_to_public_contract(self):
        self.assertEqual("Ancient", ui_mode.infer_ui_mode(existing="古风"))
        self.assertEqual("Ancient", ui_mode.infer_ui_mode(existing="ancient"))
        self.assertEqual("Other", ui_mode.infer_ui_mode(existing="现代都市"))
        self.assertEqual("Other", ui_mode.infer_ui_mode(existing="Other"))

    def test_worldview_genre_inferred_from_original_has_priority(self):
        self.assertEqual(
            "Ancient",
            ui_mode.infer_ui_mode(
                metadata={"genre": "仙侠修真/宗门成长"},
                text="他在公司拿出手机查看消息。",
            ),
        )
        self.assertEqual(
            "Other",
            ui_mode.infer_ui_mode(
                metadata={"genre": "现代重生校园纯爱"},
                text="她偶尔读到古代王朝的故事。",
            ),
        )

    def test_original_mode_can_infer_ancient_or_other_from_text(self):
        ancient_text = (
            "陛下召群臣入朝，丞相与将军在御书房议事。"
            "王爷命侍卫备马，江湖门派则派弟子御剑赶往宗门。"
        ) * 8
        modern_text = (
            "她拿起手机回复微信，随后乘地铁去大学。"
            "下课后她开车前往医院，又回公司参加会议。"
        ) * 8

        self.assertEqual("Ancient", ui_mode.infer_ui_mode(text=ancient_text))
        self.assertEqual("Other", ui_mode.infer_ui_mode(text=modern_text))

    def test_unknown_or_empty_content_fails_closed_to_other(self):
        self.assertEqual("Other", ui_mode.infer_ui_mode())
        self.assertEqual("Other", ui_mode.infer_ui_mode(existing="unknown-mode"))


if __name__ == "__main__":
    unittest.main()
