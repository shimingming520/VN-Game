from services.game_builder.converter import materialize_local_visual_sources


def test_materialize_local_visual_sources_creates_subtitles_for_each_chapter(tmp_path):
    plot_data = {
        "chapter_1": [
            {
                "beat_id": "1",
                "speaker_id": "PangBai",
                "speaker_name": "旁白",
                "scene_id": "scene_port",
                "content": "雾港的广播再次响起。",
            },
            {
                "beat_id": "2",
                "speaker_id": "LaoZhou",
                "speaker_name": "老周",
                "scene_id": "scene_port",
                "content": "你还在等那艘船？",
            },
        ]
    }

    created = materialize_local_visual_sources(
        tmp_path,
        plot_data,
        story_tree={
            "nodes": {"chapter_1": {"depth": 1}},
        },
    )

    chapter_dir = tmp_path / "chapter_1_chapter_1"
    subtitle_dir = chapter_dir / "audios" / "en-US-subtitle"
    assert created == [chapter_dir]
    assert sorted(path.name for path in subtitle_dir.glob("*.txt")) == [
        "scene_port-1.txt",
        "scene_port-2.txt",
    ]
    assert (subtitle_dir / "scene_port-1.txt").read_text(encoding="utf-8") == (
        "PangBai：雾港的广播再次响起。"
    )
    assert (subtitle_dir / "scene_port-2.txt").read_text(encoding="utf-8") == (
        "LaoZhou：你还在等那艘船？"
    )
