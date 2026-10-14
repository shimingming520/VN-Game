import os
import json
import re
import sys
from sqlalchemy import create_engine, text

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
utils_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "utils")
for p in (project_root, os.path.normpath(utils_dir)):
    if p not in sys.path:
        sys.path.insert(0, p)

from logger_config import setup_logger

# 初始化日志
logger = setup_logger("Step_Ultimate", "step_ultimate.log")

# 引入 OUTPUTS_PATH 用于输出本地文件
from common.config import DATABASE_URL, OUTPUTS_PATH
from common.artifact_io import load_local_json_artifact

DB_URL = DATABASE_URL
# 建议保留 pool_pre_ping=True 以防止数据库连接意外断开
engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=1800)

# plots/sound 已迁平台库(agent_platform); character_position/ultimate 仍在业务库
from common.config import PLATFORM_DATABASE_URL

platform_engine = create_engine(PLATFORM_DATABASE_URL, pool_pre_ping=True, pool_recycle=1800)


def fetch_source_data(task_id):
    """获取 plots, sound 和 character_position 数据，逐项支持本地降级产物。

    上游任一步入库失败时数据只存在于 storage/outputs/{task_id}/ 下的降级文件里，
    这里按表 -> 本地文件的顺序补读，避免一次入库失败连带整个聚合步骤空转。
    """
    plots_data = None
    sound_data = None
    try:
        with platform_engine.connect() as conn:
            sql_plots = text("SELECT data FROM plots WHERE task_id = :tid")
            res_plots = conn.execute(sql_plots, {"tid": task_id}).fetchone()
            plots_data = res_plots[0] if res_plots else None

            sql_sound = text("SELECT data FROM sound WHERE task_id = :tid")
            res_sound = conn.execute(sql_sound, {"tid": task_id}).fetchone()
            sound_data = res_sound[0] if res_sound else None
    except Exception as e:
        logger.warning(f"⚠️ 读取平台库 plots/sound 失败，将尝试本地降级产物: {e}")

    if not plots_data:
        plots_data = load_local_json_artifact(task_id, "plots.json", logger=logger)
    if not sound_data:
        sound_data = load_local_json_artifact(task_id, "sound.json", logger=logger)
    if not sound_data:
        logger.warning("⚠️ 无 sound 数据，所有 beat 不带 bgm/sfx/ambience")

    # character_position 在业务库，属于可选增强数据：读不到就按单人格式输出，
    # 不能让站位缺失或该库不可用连带整个聚合步骤失败。
    position_data = None
    try:
        with engine.connect() as conn:
            sql_pos = text("SELECT data FROM character_position WHERE task_id = :tid")
            res_pos = conn.execute(sql_pos, {"tid": task_id}).fetchone()
            position_data = res_pos[0] if res_pos else None
    except Exception as e:
        logger.warning(f"⚠️ 读取 character_position 失败，将尝试本地降级产物: {e}")
    if not position_data:
        position_data = load_local_json_artifact(
            task_id, "character_position.json", logger=logger
        )
    if not position_data:
        logger.warning("⚠️ 无 character_position 数据，所有 beat 按单人站位格式输出")

    return plots_data, sound_data, position_data


def parse_sound_events(sound_chapter_data):
    """
    解析某一章节的 sound 数据
    返回:
      bgm_ranges: list of tuples (start_beat, end_beat, bgm_id)
      sfx_points: dict {beat_id: sfx_id}
      ambience_ranges: list of tuples (start_beat, end_beat, ambience_id)
    """
    bgm_ranges = []
    sfx_points = {}
    ambience_ranges = []

    if not sound_chapter_data:
        return bgm_ranges, sfx_points, ambience_ranges

    # 1. 解析 BGM (匹配结尾类似 _18-25)
    for bgm_event in sound_chapter_data.get("bgm_events", []):
        bgm_id = bgm_event.get("bgm_id")
        if bgm_id:
            match = re.search(r'_(\d+)-(\d+)$', bgm_id)
            if match:
                start_beat, end_beat = int(match.group(1)), int(match.group(2))
                bgm_ranges.append((start_beat, end_beat, bgm_id))

    # 2. 解析 SFX (匹配结尾类似 _7)
    for sfx_event in sound_chapter_data.get("sfx_events", []):
        sfx_id = sfx_event.get("sfx_id")
        if sfx_id:
            match = re.search(r'_(\d+)$', sfx_id)
            if match:
                beat_id = int(match.group(1))
                sfx_points[beat_id] = sfx_id

    # 3. 解析 Ambience (匹配结尾类似 _1-22)
    for amb_event in sound_chapter_data.get("ambience_events", []):
        amb_id = amb_event.get("ambience_id")
        if amb_id:
            match = re.search(r'_(\d+)-(\d+)$', amb_id)
            if match:
                start_beat, end_beat = int(match.group(1)), int(match.group(2))
                ambience_ranges.append((start_beat, end_beat, amb_id))

    return bgm_ranges, sfx_points, ambience_ranges


def parse_position_events(position_data):
    """
    解析 character_position 数据，建立 beat_id 到窗体状态的映射
    返回字典结构: {node_id: {beat_id_str: {'onstage': [], 'positions': {}}}}
    """
    pos_map = {}
    if not position_data:
        return pos_map
    if isinstance(position_data, str):
        try:
            position_data = json.loads(position_data)
        except json.JSONDecodeError as e:
            logger.warning(f"⚠️ character_position 不是合法 JSON，按单人站位格式输出: {e}")
            return {}
    if not isinstance(position_data, dict):
        logger.warning("⚠️ character_position 结构异常，按单人站位格式输出")
        return {}

    # 结构异常只影响该节点：其余节点照常使用多人站位，坏节点退回单人格式。
    for node_id, node_info in position_data.items():
        if not isinstance(node_info, dict):
            logger.warning(f"⚠️ 节点 {node_id} 站位结构异常，该节点按单人格式输出")
            continue
        pos_map[node_id] = {}
        try:
            for seg in node_info.get("segments", []):
                for win in seg.get("display_windows", []):
                    if not isinstance(win, dict):
                        logger.warning(f"⚠️ 节点 {node_id} 窗口结构异常，跳过该窗口")
                        continue
                    # 站位是上游 LLM 产物，字段类型不保证。这里一次性归一，
                    # 避免脏数据在下面的组装环节抛异常、连带整个聚合步骤失败。
                    onstage = win.get("onstage")
                    if isinstance(onstage, list):
                        # 保持模型给出的左右视觉次序，同时去掉重名：
                        # 重复项会让同一角色在一个 beat 里出现两次立绘。
                        onstage = list(dict.fromkeys(
                            c for c in onstage if isinstance(c, str) and c))
                    else:
                        onstage = []
                    positions = win.get("positions")
                    positions = ({k: v for k, v in positions.items()
                                  if isinstance(k, str) and isinstance(v, str)}
                                 if isinstance(positions, dict) else {})
                    beat_ids = win.get("beat_ids")
                    beat_ids = ([str(x) for x in beat_ids]
                                if isinstance(beat_ids, list) else [])
                    # 同一窗口的所有 beat 共享一份窗口信息，窗口级的派生字段
                    # （window_speakers）因此只需计算一次。
                    info = {
                        "onstage": onstage,
                        "positions": positions,
                        "window_beat_ids": beat_ids,
                    }
                    for b_id in beat_ids:
                        pos_map[node_id][b_id] = info
        except (AttributeError, TypeError) as e:
            logger.warning(f"⚠️ 节点 {node_id} 站位解析失败，该节点按单人格式输出: {e}")
            pos_map[node_id] = {}
    return pos_map


def attach_window_speakers(node_pos_map, beats_list):
    """给该章节每个 beat 的窗口信息补上"本窗口实际发言者"集合。

    站位产物只给 beat_ids，判断谁真在说话需要 plots 侧的 beat_type，
    因此在这里按章节合并，供第 6 步剔除未发言角色。
    """
    speaker_by_beat = {
        str(b.get("beat_id")): b.get("speaker_id")
        for b in beats_list
        if b.get("beat_type") == "dialogue" and b.get("speaker_id")
    }
    for info in node_pos_map.values():
        # 同窗口的 beat 共享同一个 dict，算过就跳过。
        if "window_speakers" in info:
            continue
        info["window_speakers"] = {
            speaker_by_beat[b_id]
            for b_id in info.get("window_beat_ids", [])
            if b_id in speaker_by_beat
        }


def parse_speaker_combination(speaker_combination):
    """解析 ``角色名-组合[-景别]``，未显式指定景别时统一使用 MS。"""
    if not speaker_combination:
        return None
    parts = str(speaker_combination).split('-')
    if len(parts) >= 3 and parts[-1].isalpha():
        combo = "-".join(parts[1:-1])
        shot = parts[-1]
    elif len(parts) >= 2:
        combo = "-".join(parts[1:])
        shot = "MS"
    else:
        combo = parts[-1] if parts else ""
        shot = "MS"
    return combo, shot


def build_ultimate_json(plots_data, sound_data, position_data):
    """组装最终的 Ultimate JSON"""
    if not plots_data:
        return {}

    ultimate_data = {}
    # 安全兜底
    sound_data = sound_data or {}
    pos_map = parse_position_events(position_data)

    # 全局状态缓存：用于记忆各角色的最新服装、景别和情绪
    character_states = {}

    for chapter_key, beats_list in plots_data.items():
        ultimate_data[chapter_key] = []

        if chapter_key in pos_map:
            attach_window_speakers(pos_map[chapter_key], beats_list)

        # 提取该章节对应的音频规则
        sound_chapter = sound_data.get(chapter_key, {})
        bgm_ranges, sfx_points, ambience_ranges = parse_sound_events(sound_chapter)

        # 记录已处理过的同框窗口，防止重复预扫描
        processed_windows = set()

        for beat in beats_list:
            raw_beat_id = beat.get("beat_id")  # 字符串类型，例如 "144" 或 "144-A1"

            # 保留原始的 scene_id 用于后续拼接 subtitle_id
            original_scene_id = beat.get("scene_id")
            scene_id = original_scene_id

            beat_type = beat.get("beat_type")
            speaker_id = beat.get("speaker_id")
            emotion = beat.get("emotion")
            speaker_combination = beat.get("speaker_combination")
            content = beat.get("content")
            cg_val = beat.get("cg")

            # ================= 0. 区间预扫描 (Look-ahead) 初始化情绪 =================
            beat_str_id = str(raw_beat_id) if raw_beat_id is not None else None
            window_info = pos_map.get(chapter_key, {}).get(beat_str_id)

            if window_info:
                win_signature = tuple(window_info['window_beat_ids'])
                # 如果这是一个新窗口的起点，则执行区间预扫描
                if win_signature not in processed_windows:
                    processed_windows.add(win_signature)
                    onstage_chars = window_info['onstage']
                    first_emotions = {}
                    first_combinations = {}

                    # 往后扫描，寻找本区间内出场角色的首次可见情绪
                    for ahead_beat in beats_list:
                        ahead_beat_id = str(ahead_beat.get("beat_id"))
                        if ahead_beat_id in window_info['window_beat_ids']:
                            ahead_speaker = ahead_beat.get("speaker_id")
                            ahead_emotion = ahead_beat.get("emotion")
                            ahead_type = ahead_beat.get("beat_type")

                            # 跳过旁白/独白类(不显示立绘，不能作为初始外在情绪)
                            if ahead_type in ["narration", "nvl_thought", "nvl_narration"]:
                                continue

                            if ahead_speaker in onstage_chars and ahead_speaker not in first_emotions:
                                if ahead_emotion:
                                    first_emotions[ahead_speaker] = ahead_emotion

                            # 同框窗口的非当前说话者也会立即显示立绘，因此必须提前读取
                            # 其在本窗口首次出现的素材组合。只预读情绪会导致首个 beat
                            # 生成 ``角色__calm_LS``，直到该角色自己开口后才恢复正常。
                            if ahead_speaker in onstage_chars and ahead_speaker not in first_combinations:
                                parsed_combination = parse_speaker_combination(
                                    ahead_beat.get("speaker_combination")
                                )
                                if parsed_combination:
                                    first_combinations[ahead_speaker] = parsed_combination

                    # 将首次情绪强行覆盖/写入全局缓存，重置掉上个场景可能残留的情绪。
                    # 本窗口未发言的角色不参与：给它写 combo="" 会把空状态留在全局
                    # 缓存里，污染它之后真正开口的 beat。
                    win_speakers = window_info.get('window_speakers') or set()
                    for char in onstage_chars:
                        if win_speakers and char not in win_speakers:
                            continue
                        initial_emo = first_emotions.get(char, "calm")
                        if char in character_states:
                            character_states[char]['emotion'] = initial_emo
                            if char in first_combinations:
                                combo, shot = first_combinations[char]
                                character_states[char]['combo'] = combo
                                character_states[char]['shot'] = shot
                        else:
                            combo, shot = first_combinations.get(char, ("", "MS"))
                            character_states[char] = {
                                'combo': combo,
                                'shot': shot,
                                'emotion': initial_emo,
                            }

            # ================= CG 模式判断与 scene_id 覆写 =================
            if cg_val and str(cg_val).strip().lower() not in ["null", "none", "false", ""]:
                cg_mode = True
                scene_id = str(cg_val)  # 强制使用 cg 字段的值作为 scene_id
            else:
                cg_mode = False

            # ================= 1. 角色状态实时追踪与更新 =================
            # 旁白/独白(narration/nvl_thought/nvl_narration)不带图片，不参与人物多状态：
            # 既不显示立绘，其 combination 也不写入缓存，避免污染后续可见台词的状态。
            if beat_type in ["narration", "nvl_thought", "nvl_narration"]:
                pass
            elif speaker_combination:
                combo, shot = parse_speaker_combination(speaker_combination)

                if speaker_id:
                    character_states[speaker_id] = {
                        'combo': combo,
                        'shot': shot,
                        'emotion': emotion or "calm"
                    }
            elif speaker_id and emotion:
                # 如果这句台词没有combination，但更新了情绪，则实时覆盖情绪缓存
                if speaker_id in character_states:
                    character_states[speaker_id]['emotion'] = emotion

            # ================= 2. 提取用于音频匹配的纯数字 =================
            numeric_beat_id = None
            if raw_beat_id is not None:
                match = re.match(r'^(\d+)', str(raw_beat_id))
                if match:
                    numeric_beat_id = int(match.group(1))

            # ================= 3. 生成 subtitle_id =================
            # 使用 original_scene_id 保证不论是否为 CG 模式，拼接的依据都是源剧情内的 scene_id
            if original_scene_id:
                subtitle_id = f"{original_scene_id}-{raw_beat_id}"
            else:
                subtitle_id = f"UnknownScene-{raw_beat_id}"

            # ================= 4. 匹配 bgm_id =================
            matched_bgm_id = None
            if numeric_beat_id is not None:
                for start_b, end_b, bgm_id in bgm_ranges:
                    if start_b <= numeric_beat_id <= end_b:
                        matched_bgm_id = bgm_id
                        break

            # ================= 5. 匹配 sfx_id =================
            matched_sfx_id = None
            if numeric_beat_id is not None:
                matched_sfx_id = sfx_points.get(numeric_beat_id, None)

            # ================= 5.5 匹配 ambience_id =================
            matched_ambience_id = None
            if numeric_beat_id is not None:
                for start_b, end_b, amb_id in ambience_ranges:
                    if start_b <= numeric_beat_id <= end_b:
                        matched_ambience_id = amb_id
                        break

            # ================= 6. 组装 character_position =================
            character_position_str = None

            if window_info:
                # (命中同框窗口) - 提取所有在场人物
                onstage = window_info.get('onstage', [])
                positions = window_info.get('positions', {})
                # 多人站位只表示"这几个 beat 里谁在说话"。上游产物若残留了本窗口
                # 未发言的角色，这里再挡一次：否则该角色取不到 combo，会输出
                # ``角色__calm_MS(Right)``，或沿用上一场残留的服装出图。
                # onstage/positions 已在 parse_position_events 归一为字符串。
                window_speakers = window_info.get('window_speakers') or set()
                char_strs = []

                for char in onstage:
                    if window_speakers and char not in window_speakers:
                        logger.warning(
                            f"⚠️ beat {raw_beat_id} 站位含本窗口未发言角色 {char}，已剔除"
                        )
                        continue
                    # 多人站位必须带档位，否则下游无法确定角色在画面中的位置
                    if char not in positions:
                        logger.warning(
                            f"⚠️ beat {raw_beat_id} 角色 {char} 无站位档位，已剔除"
                        )
                        continue
                    state = character_states.get(char)
                    if state:
                        if not state['combo']:
                            logger.warning(
                                f"⚠️ beat {raw_beat_id} 角色 {char} 无素材组合，已剔除"
                            )
                            continue
                        # 基础格式处理 (如果 shot 不为空则拼接 _shot)
                        shot_suffix = f"_{state['shot']}" if state['shot'] else ""
                        char_strs.append(
                            f"{char}_{state['combo']}_{state['emotion']}{shot_suffix}"
                            f"({positions[char]})"
                        )

                # 剔除后不足 2 人即不成立多人站位，回落为下游单人居中默认态。
                if len(char_strs) >= 2:
                    character_position_str = f"[{','.join(char_strs)}]"
                elif speaker_id and speaker_id in character_states:
                    state = character_states[speaker_id]
                    if state['combo']:
                        shot_suffix = f"_{state['shot']}" if state['shot'] else ""
                        character_position_str = (
                            f"[{speaker_id}_{state['combo']}_{state['emotion']}"
                            f"{shot_suffix}(Center)]"
                        )
            else:
                # (未涉及的孤立 beat) - 只输出当前发声者的最新状态，默认追加 (Center)
                if speaker_id and speaker_id in character_states:
                    state = character_states[speaker_id]
                    if state['combo']:
                        shot_suffix = f"_{state['shot']}" if state['shot'] else ""
                        character_position_str = f"[{speaker_id}_{state['combo']}_{state['emotion']}{shot_suffix}(Center)]"

            # ================= 6.5 无立绘类 beat 拦截 =================
            # 当 beat_type 类型是 narration、nvl_thought 和 nvl_narration 时，character_position 为 null
            if beat_type in ["narration", "nvl_thought", "nvl_narration",
                             "variable_update", "system_panel"]:
                character_position_str = None

            # ================= 7. 其他状态字段 =================
            nvl_mode = (beat_type == "nvl_narration")
            system_text = content if beat_type == "system_panel" else None

            # ================= CG 模式的最终强制拦截 =================
            if cg_mode:
                character_position_str = None
                nvl_mode = False
                system_text = None

            # ================= 8. 组装最终 Beat =================
            ultimate_beat = {
                "bgm_id": matched_bgm_id,
                "sfx_id": matched_sfx_id,
                "ambience_id": matched_ambience_id,
                "beat_id": raw_beat_id,
                "scene_id": scene_id,
                "speaker_id": speaker_id,
                "subtitle_id": subtitle_id,
                "character_position": character_position_str,
                "scene_shot_size": "LS",
                "nvl_mode": nvl_mode,
                "cg_mode": cg_mode,
                "system_text": system_text
            }

            ultimate_data[chapter_key].append(ultimate_beat)

    return ultimate_data


def save_to_ultimate_table(task_id, ultimate_data):
    """存入 ultimate 表 (含 UPSERT 逻辑)"""
    if not ultimate_data:
        logger.warning(f"⚠️ 没有任何数据需要写入 (TaskID: {task_id})")
        return None

    try:
        with engine.begin() as conn:
            sql = text("""
                       INSERT INTO ultimate (task_id, data, create_time, update_time)
                       VALUES (:tid, :data, NOW(), NOW()) ON CONFLICT (task_id) DO
                       UPDATE SET
                           data = EXCLUDED.data,
                           update_time = NOW()
                           RETURNING id
                       """)
            result = conn.execute(sql, {
                "tid": task_id,
                "data": json.dumps(ultimate_data, ensure_ascii=False)
            })
            new_id = result.fetchone()[0]
            logger.info(f"💾 [Ultimate] 入库/更新成功! DB_ID: {new_id}, TaskID: {task_id}")
            return new_id
    except Exception as e:
        logger.error(f"❌ [Ultimate] 入库失败: {e}")
        return None


def process_ultimate(task_id):
    """主流程执行函数。返回 (入库是否成功, 本地文件是否落盘)。"""
    logger.info(f"🚀 开始整合 ultimate 数据, Task ID: {task_id}")

    # 同时获取 plots, sound 和 character_position 三张表的数据
    plots_data, sound_data, position_data = fetch_source_data(task_id)
    if not plots_data:
        logger.error(f"❌ 未找到 Task ID: {task_id} 对应的 plots 数据，流程终止。")
        return False, False

    # 1. 组装 JSON
    ultimate_json = build_ultimate_json(plots_data, sound_data, position_data)

    # 2. 写入数据库
    saved_id = save_to_ultimate_table(task_id, ultimate_json)

    # 3. 输出本地文件到 OUTPUTS_PATH/{task_id}/{task_id}.json
    # game_builder 与 api_gateway 都会先读这个文件，入库失败时它就是唯一产物。
    local_saved = False
    try:
        local_dir = os.path.join(OUTPUTS_PATH, str(task_id))
        os.makedirs(local_dir, exist_ok=True)
        local_file_path = os.path.join(local_dir, f"{task_id}.json")

        with open(local_file_path, 'w', encoding='utf-8') as f:
            json.dump(ultimate_json, f, ensure_ascii=False, indent=2)

        local_saved = True
        logger.info(f"📁 [Ultimate] 成功保存到本地文件: {local_file_path}")
    except Exception as e:
        logger.error(f"❌ [Ultimate] 保存本地文件失败: {e}")

    if saved_id is None and local_saved:
        logger.warning(
            "⚠️ [Ultimate] 入库失败，已降级为本地文件产物，下游按本地文件读取"
        )

    return saved_id is not None, local_saved


if __name__ == "__main__":
    test_task_id = ""
    if test_task_id:
        process_ultimate(test_task_id)
    else:
        logger.warning("未指定 test_task_id，请填写后再运行。")
