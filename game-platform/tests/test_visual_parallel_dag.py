from common.models import StepStatus
from orchestrator.dag import VISUAL_PARALLEL_DAG, ready_capabilities


def test_visual_parallel_dag_dispatches_character_and_scene_together():
    status = {node: StepStatus.PENDING for node in VISUAL_PARALLEL_DAG["nodes"]}
    status["story_planner"] = StepStatus.SUCCEEDED
    status["info_extractor"] = StepStatus.SUCCEEDED

    ready = set(ready_capabilities(VISUAL_PARALLEL_DAG, status))

    assert {"char_designer", "scene_painter"} <= ready
    assert {"audio_producer", "sound_arranger"} <= ready
    assert "game_builder" not in ready


def test_visual_parallel_game_builder_waits_for_both_visual_branches():
    status = {
        node: StepStatus.SUCCEEDED for node in VISUAL_PARALLEL_DAG["nodes"]
    }
    status["game_builder"] = StepStatus.PENDING
    status["char_designer"] = StepStatus.RUNNING

    assert "game_builder" not in ready_capabilities(VISUAL_PARALLEL_DAG, status)

    status["char_designer"] = StepStatus.SUCCEEDED
    assert "game_builder" in ready_capabilities(VISUAL_PARALLEL_DAG, status)
