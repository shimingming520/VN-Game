# Style Anchor 独立验证 UI

该页面仅用于验证“用户预设人物/场景风格并确认 Anchor”的方案，不修改项目现有 UI。除上传小说跑完整本地流程外，还支持复用已完成的 story_planner / info_extractor 产物，只重新生成人物和场景美术资源。

## 本地 Docker 启动（推荐）

在 `novel-game-factory` 目录执行：

```bash
docker compose \
  -f experiments/style_anchor_ui/docker-compose.local.yml \
  up --pull never --build -d
```

浏览器打开：<http://127.0.0.1:17861>

查看日志：

```bash
docker compose \
  -f experiments/style_anchor_ui/docker-compose.local.yml \
  logs -f api_gateway style_ui story_planner info_extractor char_designer scene_painter game_builder
```

服务内部额外落盘的日志会通过本实验 Compose 的本地挂载，按服务归档到：

```text
storage/local_logs/<service_name>/
```

该目录已加入 `.gitignore`。日志汇总仅由本文件的挂载生效，不修改服务 logger 代码或 K8s 清单，不改变线上日志行为。

停止服务（保留本地数据库和生成结果）：

```bash
docker compose \
  -f experiments/style_anchor_ui/docker-compose.local.yml \
  down
```

该 Compose 仅用于本实验，具备以下硬边界：

- `OSS_DISABLED=1`，不会初始化 OSS 客户端，也不会上传或下载 OSS 文件。
- `LOCAL_PIPELINE_MODE=1`，确认任务后使用本地 Redis/Celery，不访问编排中台。
- 上传新小说时，Pipeline 固定为 `story_planner → info_extractor → char_designer → scene_painter → game_builder`。
- 复用预处理产物时，固定只执行 `char_designer → scene_painter`，不会重新调用 story_planner / info_extractor，也不会打包游戏。
- 不启动 `audio_producer` 和 `sound_arranger`，因此不生成语音、音效或 BGM。
- PostgreSQL、Redis、一次性数据库初始化、Gateway、独立 UI 和五个 Pipeline Worker 全部运行在本地 Docker 中。
- 源码与 `storage` 通过 bind mount 共享，容器直接运行当前工作区代码和本地产物。

五个算法 Worker 默认复用本机已有运行镜像 `sj-native-provider-runtime:20260807-180324`；Gateway 与独立 UI 使用实验目录内的轻量 Web Dockerfile 构建。如需为算法 Worker 使用其他兼容镜像：

```bash
STYLE_ANCHOR_RUNTIME_IMAGE=your-local-runtime:tag \
docker compose -f experiments/style_anchor_ui/docker-compose.local.yml up -d
```

## 行为边界

- `llm` 模式：全自动路线。story_planner 不提前生成画风；char_designer 在上游预检通过后生成唯一 `global_style` 和人物 Anchor，scene_painter 复用该风格并生成场景 Anchor。
- `custom` 模式：人物和场景 Anchor 均必须存在、非 stale 且已确认，否则不允许启动 Pipeline。
- Anchor 生成调用项目内的 GPT/Qwen/Wan Provider，不使用 Codex/ImageGen。
- 用户可直接上传 PNG/JPEG/WebP；上传后无需再生成，但仍需在预览后点击确认。
- 人物立绘可在启动前选择“启动抠图”或“关闭抠图”；美术风格验证默认关闭，旧任务未配置时仍保持原有开启行为。
- 配置与图片保存在 `storage/outputs/{task_id}/style_anchors/`；上述本地 Docker 模式不会执行 OSS 镜像。
- 本实验页不改动数据库 schema、现有 Gateway 接口或游戏打包逻辑。

## 快速复用已完成任务

适用于只验证 `character_global_style`、`scene_global_style` 和两张 Anchor 的效果。

1. 打开 <http://127.0.0.1:17861>，在“快速复用已完成预处理任务”填写源 `source_task_id`，例如 `c8bdd57b3e4c`。
2. 点击“只读预检”。源任务必须已完成 story_planner 和 info_extractor，并具备人物/场景提示词等必需产物。
3. 可先填写两类 global style，再点击“创建派生实验任务”。系统会创建新的 `style_<source>_<随机串>` task_id，不覆盖源任务。
4. 调用项目 Provider 生成或直接上传人物、场景 Anchor，分别审查并确认。
5. 在启动前选择人物立绘是否抠图。美术风格验证通常选择“关闭抠图”，可避免加载本地抠图模型并保留 Provider 原始背景。
6. 点击“仅启动人物与场景生图”。页面每 3 秒刷新状态，按本地已落盘资源/上游预期数量显示 0–100% 进度，并持续追加人物立绘、场景、CG 和封面预览。

每张资源卡片会展示：

- 可点击放大的本地预览图；
- 最终生图提示词；
- 文生图/图生图/本地裁剪或复制等生成方式；
- 实际使用的人物 Anchor、场景 Anchor、基础立绘、父场景图或 CG 多参考图；
- “开发者调试”入口，在独立窗口显示简要生图逻辑和原始调试 JSON。

新任务的提示词与参考图在 Provider 调用成功后写入本地 sidecar：

```text
storage/outputs/<task_id>/style_anchors/debug_assets/<category>/<filename>.json
```

sidecar 只记录提示词、相对参考图路径、模型和简要逻辑，不记录 API Key 或请求头；仅当下列三个本地隔离开关同时开启时写入。旧任务没有 sidecar 时，UI 会明确标记“旧任务重建”，依据当前风格配置与上游数据库产物提供可审查的重建信息。

浏览器刷新或稍后重新打开页面时，可在“加载已有派生 task_id”中输入派生任务 ID，直接恢复风格、Anchor 状态、运行状态和图片画廊。

派生任务会复用数据库中的以下预处理产物（存在时）：

```text
story_tree_json / story_tree_md / plots / characters / name_mappings
character_styles / character_prompts / character_position
scenes / scene_prompts / backgrounds / cg / ultimate / atmosphere
```

创建派生任务时会清除已经固化到以下字段开头的旧 global style，避免新旧风格叠加：

```text
character_prompts[].positive_prompt
backgrounds.generated_prompts.*.prompt
backgrounds.novel_cover_metadata.prompt
cg.*.cg_prompt
```

CG 优先恢复为 `init_description` 的纯内容提示词。不会复制源任务的旧图片、旧 Anchor、音频、声音映射、重生成记录或游戏包。

派生任务的结果保存到：

```text
storage/outputs/<derived_task_id>/
```

其中实验来源、克隆表和提示词净化计数记录在：

```text
storage/outputs/<derived_task_id>/style_anchors/reuse_manifest.json
```

该接口受三个本地开关共同保护，缺一不可：

```text
STYLE_ANCHOR_REUSE_ENABLED=1
LOCAL_PIPELINE_MODE=1
OSS_DISABLED=1
```

实验 Compose 已配置这些开关；没有修改任何 K8s、生产镜像、生产 API Gateway 接口或项目正式 UI。
