from services.story_planner import tasks


def test_book_metadata_uses_database_names_before_filename_and_task_id():
    metadata = tasks._build_story_tree_book_metadata(
        {
            "project_name": "数据库作品名",
            "generated_project_name": "模型作品名",
        },
        novel_filename="fallback.txt",
        task_id="task-123",
        narrative_arc="主角在暴雨中发现一桩旧案，随后踏上追查真相的旅程。",
    )
    assert metadata["book_title"] == "数据库作品名"
    assert "旧案" in metadata["book_summary"]


def test_book_metadata_falls_back_to_generated_name_then_filename_then_task():
    assert tasks._build_story_tree_book_metadata(
        {"project_name": "", "generated_project_name": "模型作品名"},
        novel_filename="fallback.txt",
        task_id="task-123",
    )["book_title"] == "模型作品名"
    assert tasks._build_story_tree_book_metadata(
        {"project_name": "", "generated_project_name": ""},
        novel_filename="fallback.txt",
        task_id="task-123",
    )["book_title"] == "fallback"
    assert tasks._build_story_tree_book_metadata(
        {"project_name": "", "generated_project_name": ""},
        novel_filename="",
        task_id="task-123",
    )["book_title"] == "task-123"


def test_source_chapter_title_uses_only_a_real_explicit_heading():
    assert tasks._extract_source_chapter_title("第一章 风暴\n正文", 1) == "风暴"
    assert tasks._extract_source_chapter_title("正文里提到第一章，但不是标题", 2) == "未命名章节"


def test_source_chapter_summary_uses_narrative_excerpt_not_a_heading_or_length_label():
    summary = tasks._extract_source_chapter_summary(
        "第一章 风暴\n\n林默推开门，看见雨水漫过台阶。随后他听见远处传来钟声。",
        1,
    )
    assert summary.startswith("林默推开门")
    assert "共" not in summary
