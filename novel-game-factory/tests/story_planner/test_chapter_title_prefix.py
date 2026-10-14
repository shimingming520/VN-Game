from services.story_planner.core.chapter_title import (
    normalize_chapter_title,
    untitled_chapter_title,
)


def test_normalize_chapter_title_removes_chinese_number_prefix():
    assert normalize_chapter_title("第1章：死亡创伤与重生契机") == "死亡创伤与重生契机"
    assert normalize_chapter_title("第五章 最好莫过陌生人") == "最好莫过陌生人"


def test_normalize_chapter_title_removes_what_if_number_prefix_but_keeps_semantics():
    assert normalize_chapter_title("第10章（What-If：当面说明身份）") == "What-If：当面说明身份"


def test_normalize_chapter_title_removes_english_and_no_prefixes():
    assert normalize_chapter_title("Chapter 10: Honest Disclosure") == "Honest Disclosure"
    assert normalize_chapter_title("NO.141 - A New Beginning") == "A New Beginning"


def test_normalize_chapter_title_uses_non_number_fallback_for_bare_heading():
    assert normalize_chapter_title("第1章") == "未命名章节"
    assert normalize_chapter_title("Chapter 2") == "Untitled Chapter"


def test_untitled_chapter_fallback_matches_player_language():
    assert untitled_chapter_title("zh-CN") == "未命名章节"
    assert untitled_chapter_title("ja-JP") == "章タイトル未設定"
    assert untitled_chapter_title("en-US") == "Untitled Chapter"
