import os
import json
import asyncio
import sys
from sqlalchemy import create_engine, text
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_random_exponential
from tqdm.asyncio import tqdm

# 基础路径配置
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
utils_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "utils")
for p in (project_root, os.path.normpath(utils_dir)):
    if p not in sys.path:
        sys.path.insert(0, p)

from logger_config import setup_logger

logger = setup_logger("Step1_Atmosphere", "step1_atmosphere.log")

# 引入数据库与统一输出路径配置
from common.config import DATABASE_URL, OUTPUTS_PATH
from common.db_pool import worker_pool_kwargs

try:
    from common.usage_tracker import tracker as _usage_tracker
    from common.usage_tracker import init_usage_table

    init_usage_table()
except Exception:
    _usage_tracker = None

DB_URL = DATABASE_URL
OUTPUT_ROOT = OUTPUTS_PATH  # 统一使用全局配置的路径

API_KEY = os.getenv("GPT_IMAGE_API_KEY") or os.getenv("OPENAI_API_KEY")
BASE_URL = os.getenv("GPT_IMAGE_BASE_URL", "https://ai.farmmx.com").rstrip("/")
if not BASE_URL.endswith("/v1"):
    BASE_URL = f"{BASE_URL}/v1"
MODEL_NAME = "gpt-5.6-sol"

# 客户端不在导入时创建：AsyncOpenAI 会绑定创建时的事件循环。
# 在 Celery worker 中每个任务用独立的 asyncio.run() 新建循环，
# 复用绑在旧（已关闭）循环上的 client 会触发 "Event loop is closed"。
# 因此改为每次 process_task 内重建并在结束时关闭（见下方 process_task）。
client = None
engine = create_engine(
    DB_URL,
    **worker_pool_kwargs(default_pool_size=5, default_max_overflow=10),
)

# plots 已迁平台库(agent_platform); atmosphere 仍在业务库
from common.config import PLATFORM_DATABASE_URL
platform_engine = create_engine(
    PLATFORM_DATABASE_URL,
    **worker_pool_kwargs(default_pool_size=5, default_max_overflow=10),
)


def get_plots_from_db(task_id):
    """从数据库读取剧情数据"""
    query = text("SELECT data FROM plots WHERE task_id = :task_id")
    try:
        with platform_engine.connect() as conn:
            result = conn.execute(query, {"task_id": task_id}).fetchone()
            if result and result[0]:
                return result[0]
            return None
    except Exception as e:
        logger.error(f"❌ 读取数据库 plots 表失败: {e}")
        return None


def save_to_atmosphere_table_and_file(task_id, atmosphere_data):
    """双写输出：1. 存入数据库 atmosphere 表  2. 输出到全局 OUTPUTS_PATH/task_id 目录下"""
    # 1. 写入本地文件 (吸取配置)
    try:
        task_dir = os.path.join(OUTPUT_ROOT, str(task_id)) if task_id else os.path.join(OUTPUT_ROOT, "default_task")
        os.makedirs(task_dir, exist_ok=True)
        file_path = os.path.join(task_dir, "atmophere.json")

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(atmosphere_data, f, ensure_ascii=False, indent=2)
        logger.info(f"\n📁 任务 {task_id} 的氛围数据已成功导出到: {file_path}")
    except Exception as e:
        logger.error(f"\n❌ 本地文件写入失败: {e}")

    # 2. 写入数据库
    query = text("""
                 INSERT INTO atmosphere (task_id, data, create_time, update_time)
                 VALUES (:task_id, :data, CURRENT_TIMESTAMP(0), CURRENT_TIMESTAMP(0)) ON CONFLICT (task_id) DO
                 UPDATE
                     SET data = EXCLUDED.data,
                     update_time = CURRENT_TIMESTAMP (0);
                 """)
    try:
        with engine.connect() as conn:
            with conn.begin():
                conn.execute(query, {
                    "task_id": task_id,
                    "data": json.dumps(atmosphere_data, ensure_ascii=False)
                })
        logger.info(f"✅ 任务 {task_id} 的氛围数据已成功入库 (atmosphere 表)。")
    except Exception as e:
        logger.error(f"\n❌ 数据库写入失败: {e}")


@retry(wait=wait_random_exponential(min=2, max=15), stop=stop_after_attempt(3))
async def generate_atmosphere_for_scene(node_name, scene_id, scene_beats):
    """
    按场景提取氛围特效 (VFX)。
    """
    system_prompt = f"""
# 角色定义
你是一个经验丰富的视觉小说 (Visual Novel) 演出导演。当前正在处理节点：`{node_name}` 的场景演出。

# 任务目标
根据提供的剧情文本，精准感知情绪压迫感、环境变化和物理冲击，为特定台词（beat_id）埋点“氛围特效”。
如果没有适合加特效的平淡对话或常规旁白，请保持留白（直接跳过该 beat_id，无需返回空数组）。宁缺毋滥！

# 预设特效字典 (必须严格从以下列表中选择 effect_type，不要捏造)
【震动类 (即时)】
- `shake_camera_light`: 轻微镜头震动 (闷响、受惊、门被推开)
- `shake_camera_heavy`: 剧烈镜头震动 (地震、爆炸、物理冲撞)
- `shake_sprite`: 单体立绘震动 (极度恐惧、愤怒发抖)

【闪烁类 (即时)】
- `flash_red`: 红屏闪烁 (受伤、警告)
- `flash_white`: 白屏闪烁 (闪光弹、断片、强光)
- `flash_green`: 绿/紫屏闪烁 (中毒、恶心、魔法)

【自然天气类 (状态持续)】
- `weather_wind`: 刮风 (萧瑟、高处)
- `weather_snow`: 下雪 (寒冷、肃杀)
- `weather_fog`: 起雾 (悬疑、迷茫)
- `weather_leaves`: 落叶飘散 (时间流逝、唯美)
- `weather_ash`: 灰烬飘落 (葬礼、废墟、大火)
- `weather_dust`: 灰尘浮动 (废弃密室、破败室内)

【屏幕遮罩类 (状态持续)】
- `overlay_blood`: 血迹糊脸 (重伤、残酷视角)
- `overlay_crack`: 屏幕碎裂 (精神重创、被击中面门)
- `overlay_water`: 水雾/泪水 (痛哭、雨中视线受阻)

【滤镜类 (状态持续)】
- `filter_cold`: 冷色调 (深冬、葬礼、绝望)
- `filter_warm`: 暖黄色调 (夕阳、温馨、希望)
- `filter_sepia`: 泛黄老照片 (回忆、传说)
- `filter_bw`: 黑白滤镜 (瞬间震惊、时间静止)

*注：所有状态持续类特效将在场景切换时由系统自动重置，你只需负责在合适的时机开启它们。*

# 输出格式限制
必须输出严格的 JSON 格式。包含一个 `events` 数组。同一句台词如包含多个特效，需使用 `order` 字段从小到大排序。
示例：
{{
  "events": [
    {{
      "beat_id": "3",
      "effects": [
        {{
          "order": 1,
          "effect_type": "weather_snow",
          "note": "配合扫雪情节，开启下雪"
        }},
        {{
          "order": 2,
          "effect_type": "overlay_blood",
          "note": "手冻裂流血，屏幕边缘出现血迹"
        }}
      ]
    }},
    {{
      "beat_id": "146",
      "effects": [
        {{
          "order": 1,
          "effect_type": "flash_white",
          "note": "冷光刺眼，瞬间白屏"
        }},
        {{
          "order": 2,
          "effect_type": "shake_sprite",
          "note": "震动"
        }}
      ]
    }}
  ]
}}
"""

    compact_beats = [{"beat_id": b["beat_id"], "speaker": b.get("speaker_name"), "content": b["content"]} for b in
                     scene_beats]
    user_content = json.dumps(compact_beats, ensure_ascii=False)

    try:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"请提取本片段的氛围特效，必须以 JSON 格式输出：\n{user_content}"}
            ],
            temperature=0.3,  # 再次调低温度，确保大模型严格在预设字典内选择
            response_format={"type": "json_object"}
        )
        if _usage_tracker and response.usage:
            _usage_tracker.add_llm_usage(
                response.usage, MODEL_NAME, provider="openai_compatible"
            )

        raw_content = response.choices[0].message.content

        try:
            parsed_json = json.loads(raw_content)
            return node_name, parsed_json.get("events", [])
        except json.JSONDecodeError as e:
            logger.error(f"❌ 场景 {scene_id} Atmosphere JSON解析失败: {e}\n📦 原始输出:\n{raw_content}")
            raise e
    except Exception as e:
        logger.error(f"❌ 场景 {scene_id} 的 Atmosphere 提取失败: {e}")
        raise e


async def process_task(task_id):
    global client
    if _usage_tracker and task_id:
        _usage_tracker.set_context(task_id, "info_extractor")
    logger.info(f"========== 开始提取氛围特效数据: {task_id} ==========")

    client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)
    try:
        return await _process_task_inner(task_id)
    finally:
        await client.close()


async def _process_task_inner(task_id):
    plots_data = await asyncio.to_thread(get_plots_from_db, task_id)
    if not plots_data:
        logger.error("❌ 未能获取到剧情数据。")
        return

    # 数据预处理：转换为纯数字替身 ID，防止 LLM 幻觉
    mapped_plots_data = {}
    seq_to_real_map = {}

    for node_name, raw_beats in plots_data.items():
        valid_beats = []
        current_map = {}
        seq_id = 1
        for beat in raw_beats:
            beat_id_str = str(beat.get("beat_id", ""))
            if beat.get("beat_type") == "variable_update":
                continue
            if "-" in beat_id_str:
                continue

            beat_copy = beat.copy()
            current_map[seq_id] = beat_id_str
            beat_copy["beat_id"] = seq_id
            valid_beats.append(beat_copy)
            seq_id += 1

        mapped_plots_data[node_name] = valid_beats
        seq_to_real_map[node_name] = current_map

    sem = asyncio.Semaphore(5)

    # 初始化最终的数据结构：{ node_name: { beat_id: [effects...] } }
    final_atmosphere_data = {node: {} for node in mapped_plots_data.keys()}

    logger.info("🎬 正在启动视觉演出总监 (按场景切块提取氛围 VFX)...")
    atmo_tasks = []

    async def sem_atmo_task(node_name, scene_id, scene_beats):
        async with sem:
            atmo_pbar.set_postfix_str(f"当前: {node_name} | 场景: {scene_id}")
            try:
                return await generate_atmosphere_for_scene(node_name, scene_id, scene_beats)
            except Exception as e:
                return e

    # 按场景分组组装任务
    for node_name, all_beats in mapped_plots_data.items():
        scene_groups = {}
        for beat in all_beats:
            s_id = beat.get("scene_id", "default")
            if s_id not in scene_groups:
                scene_groups[s_id] = []
            scene_groups[s_id].append(beat)

        for s_id, s_beats in scene_groups.items():
            atmo_tasks.append(sem_atmo_task(node_name, s_id, s_beats))

    print("\n")
    atmo_pbar = tqdm(total=len(atmo_tasks), desc="特效解析进度", ncols=100)

    async def wrapped_atmo_task(t):
        res = await t
        atmo_pbar.update(1)
        return res

    atmo_results = await asyncio.gather(*(wrapped_atmo_task(t) for t in atmo_tasks), return_exceptions=True)
    atmo_pbar.close()
    print("\n")

    # 数据合并与真实 ID 还原
    for res in atmo_results:
        if not isinstance(res, Exception) and res:
            node_name, events = res
            current_map = seq_to_real_map.get(node_name, {})

            for event in events:
                b_id_raw = str(event.get("beat_id", ""))
                effects = event.get("effects", [])
                if not effects:
                    continue

                # 还原回真实的 beat_id
                if b_id_raw.isdigit():
                    real_beat_id = current_map.get(int(b_id_raw), b_id_raw)
                else:
                    real_beat_id = b_id_raw

                # 按照 { beat_id: [effects] } 的格式拼装
                if real_beat_id not in final_atmosphere_data[node_name]:
                    final_atmosphere_data[node_name][real_beat_id] = []
                final_atmosphere_data[node_name][real_beat_id].extend(effects)

                # 确保最后还原出来的列表严格按照 order 排序
                final_atmosphere_data[node_name][real_beat_id].sort(key=lambda x: x.get("order", 99))

    logger.info("✨ 特效数据合并与 ID 映射完毕，开始写入文件与数据库...")
    await asyncio.to_thread(save_to_atmosphere_table_and_file, task_id, final_atmosphere_data)
    logger.info(f"========== 任务 {task_id} 处理完成 ==========")
    if _usage_tracker:
        _usage_tracker.flush()


if __name__ == "__main__":
    if sys.platform.startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    test_task_id = "test_e85cff11"
    asyncio.run(process_task(task_id=test_task_id))
