"""DAG 引擎:纯函数,从 task_step 状态推导"下一批就绪能力"。

Orchestrator 无状态化(A3)的关键:推进逻辑不依赖内存,任何时刻都能
从 DB 里的 step 状态重新算出该发什么。
"""
import json
from typing import Dict, List

from common.models import StepStatus

# 默认管线,等价现有 Celery 链:
# story_planner → info_extractor → {(char→scene), audio, sound} → game_builder
DEFAULT_DAG = {
    "nodes": [
        "story_planner", "info_extractor", "char_designer", "scene_painter",
        "audio_producer", "sound_arranger", "game_builder",
    ],
    "edges": [
        ["story_planner", "info_extractor"],
        ["info_extractor", "char_designer"],
        ["info_extractor", "audio_producer"],
        ["info_extractor", "sound_arranger"],
        ["char_designer", "scene_painter"],
        ["scene_painter", "game_builder"],
        ["audio_producer", "game_builder"],
        ["sound_arranger", "game_builder"],
    ],
}

# 美术并行模板：char_designer 与 scene_painter 在 info_extractor 后同时
# 下发。scene_painter 内部先生成不依赖人物的场景/封面，只在 CG
# 子阶段等待 char_designer 完成。game_builder 显式等待两个美术模块。
VISUAL_PARALLEL_DAG = {
    "nodes": list(DEFAULT_DAG["nodes"]),
    "edges": [
        ["story_planner", "info_extractor"],
        ["info_extractor", "char_designer"],
        ["info_extractor", "scene_painter"],
        ["info_extractor", "audio_producer"],
        ["info_extractor", "sound_arranger"],
        ["char_designer", "game_builder"],
        ["scene_painter", "game_builder"],
        ["audio_producer", "game_builder"],
        ["sound_arranger", "game_builder"],
    ],
}


def parse_dag(dag_json: str) -> dict:
    dag = json.loads(dag_json)
    nodes = set(dag["nodes"])
    for a, b in dag["edges"]:
        if a not in nodes or b not in nodes:
            raise ValueError(f"edge ({a},{b}) references unknown node")
    return dag


def upstreams_of(dag: dict, node: str) -> List[str]:
    return [a for a, b in dag["edges"] if b == node]


def ready_capabilities(dag: dict, step_status: Dict[str, str]) -> List[str]:
    """所有上游 succeeded 且自己还是 pending 的能力"""
    ready = []
    for node in dag["nodes"]:
        if step_status.get(node) != StepStatus.PENDING:
            continue
        if all(step_status.get(up) == StepStatus.SUCCEEDED
               for up in upstreams_of(dag, node)):
            ready.append(node)
    return ready


def all_succeeded(dag: dict, step_status: Dict[str, str]) -> bool:
    return all(step_status.get(n) == StepStatus.SUCCEEDED for n in dag["nodes"])
