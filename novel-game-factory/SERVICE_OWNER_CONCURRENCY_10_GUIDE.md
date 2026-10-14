# 30 本 × 50 万字、阶段并发 10：业务服务负责人执行指南

版本：2026-08-18

适用服务：

- `story_planner`
- `info_extractor`
- `char_designer`
- `scene_painter`
- `audio_producer`
- `sound_arranger`
- `game_builder`

这份文档可以直接发给服务负责人，也可以原样交给 Codex、Claude Code（CC）或其他代码代理执行。

## 一、整体目标、两跳队列与责任边界

目标不是让 30 本小说同时在一个阶段运行，而是：

1. 30 本、每本约 50 万字先全部被平台可靠接纳。
2. 每个业务阶段最多同时运行 10 个主链任务，其余任务留在队列中等待。
3. 顶层并发 10 不等于供应商并发 10；任务内部线程、协程和批量 SDK 会继续放大真实并发。
4. 主链消息只传 `task_id`、`step_id`、`attempt`、路径或 OSS key 等轻量引用，不把小说正文塞进消息。
5. 系统采用“至少一次投递 + 幂等入口 + lease fencing + 原子发布”，目标是只暴露一份有效最终结果。
6. 任何任务只有在必需产物持久化成功、阶段完成状态可靠写入后，才允许返回成功并 ACK。

### 1.1 必须分清的两跳队列

本系统不是一条“RabbitMQ/Celery 队列”，而是两跳：

```text
game-platform PostgreSQL
  -> transactional outbox
  -> RabbitMQ durable Job
  -> Platform Adapter
  -> Redis broker / Celery task
  -> 七个业务 Worker
  -> PostgreSQL / 共享 PVC / OSS 产物
```

- 第一跳 RabbitMQ 属于外部 `game-platform` 到 Platform Adapter 的 Job 通道。
- 第二跳 Celery 使用 Redis broker/backend，属于 Adapter 到七个业务服务的执行通道。
- 两跳必须分别定义稳定消息 ID、持久化、ACK、重投和恢复责任，不能用其中一跳的证据替代另一跳。
- Gateway 直接投递的起名、摘要、重生成等辅助 Celery task 可能绕过第一跳 RabbitMQ；它们必须单独列为 B/C 类，不能拿它们的 Redis 入队成功证明主链 Rabbit Job 已可靠发布。

### 1.2 压测前硬阻断

在以下条件完成前，任何人都不得承诺“30 本一定不丢单”：

1. `common` 提供并验证 lease ownership 检查、fenced complete 和 fenced fail；业务服务不得自己仿造第二套锁。
2. 外部 `game-platform` 使用 transactional outbox、publisher confirm、`mandatory` 和 returned-message 处理。
3. 目标 RabbitMQ 的 queue、exchange、binding durable，Job 消息 persistent，无短 TTL。
4. 当前 Adapter 会在等待长 Celery 任务期间持有 RabbitMQ delivery，且没有 durable detach/reattach，因此 `consumer_timeout` 必须至少为 `176400000ms`。`900000ms` 只有 15 分钟，明显不足以覆盖 48 小时任务与恢复余量。
5. 两个 PostgreSQL DSN 必须分别完成理论池统计和目标环境实测。完整静态审计后，当前平台 DSN 的配置容量约 `314`，业务 DSN 约 `405–420`；若两个 DSN 位于同一实例，静态池容量已约 `719–734`，且尚未计入函数内临时 `create_engine`、`psycopg2` 直连、迁移、监控和运维连接。`200` 和此前粗算的 `260–300` 都不能作为当前代码的容量保证。负责人必须先收敛私有 Engine（单槽 Worker 默认 `pool_size=1/max_overflow=0`）并实测，或使用 PgBouncer；不得只把 PostgreSQL `max_connections` 机械抬到 734 以上。

如果 Adapter 将来改为“可靠落地 dispatch 状态后立即 ACK RabbitMQ，并可在重启后 reattach Celery execution”，才允许重新评估更短的 `consumer_timeout`。在当前实现下不得用 900000ms 验收。

### 1.3 已由本仓库平台层负责

- 七个阶段 Deployment 均为 `replicas: 10`。
- 每个 Worker Pod 为 `--concurrency=1`、`--prefetch-multiplier=1`。
- 发布期间 `maxSurge: 0`，避免瞬时从 10 个槽膨胀到 11 个。
- Platform Adapter 每个 capability 的 admission concurrency 为 10。
- Celery 公共配置已有 late ACK、prefetch=1、48 小时任务硬上限、14 天 visibility timeout。
- Gateway 负责 30 本批量请求限制、正文落盘、幂等 task ID、批量建单和平台提交补偿。
- 项目内部 token/API/image/TTS/music 重复总额限制已关闭，由外部总闸门与账号/RPM负责。

### 1.4 业务服务负责人权限

业务服务负责人只可修改自己服务的主链可达文件，以及明确归属于该服务的新测试。不得修改：

- `services/api_gateway/**`
- `services/platform_adapter/**`
- `common/**`
- `k8s/**`
- `Jenkinsfile`
- 其他业务服务目录
- 与主入口不可达的旧脚本、`useless/`、实验代码和手工工具，除非负责人逐文件批准

不得自行 commit、push、部署或操作真实集群，除非服务负责人另行明确授权。不得读取、打印或提交 `.env`、secret YAML、凭据或令牌。

如确实需要修改公共接口，先提交“公共接口变更建议”，说明调用方、兼容性、回滚方式和测试，不要直接跨目录修改。

## 二、绝对禁止事项

本轮不得修改以下业务语义：

- Prompt、system prompt、模板文本。
- 模型选择、temperature、top_p、seed 等生成参数。
- 分章、剧情树、角色、场景、音频、构建算法。
- 输出 JSON 字段、数据库表结构和对外 Artifact 路径。
- 七阶段顺序和 capability 名。
- 为了“通过测试”吞异常、返回假成功或跳过真实调用。
- 在业务服务里再次创建顶层 Celery 重试循环，绕过平台 attempt/max_attempts。
- 删除或改变现有用户触发的 regenerate/redraw/rebuild/export 语义，除非该入口被单独批准进入本轮。
- 为了实现优雅退出而覆盖 Celery 的进程级 SIGTERM/SIGINT handler。
- 新增对外 Artifact 路径、manifest schema 或数据库字段；内部 staging 名称可以变化，但对外合同必须保持不变。
- 修改不在主链可达调用图内的旧脚本，只因为全目录搜索发现了相似代码。

如果可靠性修复必然改变上述内容，停止实施并交给负责人人工确认。

### 2.1 Celery 入口分级

每个负责人必须先把本服务全部 Celery task 分为四类，再开始编辑：

| 级别 | 含义 | 本轮动作 |
| --- | --- | --- |
| A | 七阶段主链入口 | 必须完整审计；负责人批准后做最小修复 |
| B | 上传 30 本时会自动产生、并与主链共享队列的任务 | 必须计入并发、RPM、超时和排队测试；需要修复时也须逐文件批准 |
| C | 用户手动触发的 regenerate/redraw/rebuild/export | 只做入口与容量清单；未经逐文件批准不得修改 |
| D | CLI、测试、实验、备份、`useless/` | 不纳入本轮，不得顺手修改 |

当前最低入口清单如下。负责人仍需用代码搜索确认没有遗漏：

| 服务 / 队列 | A：主链 | B：本轮自动任务 | C：共享队列辅助任务 |
| --- | --- | --- | --- |
| Story / `story_queue` | `parse_novel_to_draft` | `generate_project_name_only`；单文件上传还会触发 `generate_summary_only` | `regenerate_chapter_branch_nodes` |
| Info / `extract_queue` | `extract_info` | 无已知上传时辅助任务 | 当前无其他 Celery 入口 |
| Character / `char_queue` | `generate_characters` | 无 | `regenerate_characters`、`redraw_character` |
| Scene / `scene_queue` | `generate_scenes` | 无 | `generate_style_anchor`、`regenerate_scene_materials` |
| Audio / `audio_queue` | `generate_audio` | 无 | `regenerate_audio`、`regenerate_character_voice` |
| Sound / `sound_queue` | `generate_sound` | 无 | `regenerate_sound` |
| Builder / `build_queue` | `build_game_package` | `celery.chord_unlock` 会占用同队列 | `rebuild_online_package`、`export_offline_package` |

“禁止顶层 Celery 自动重试”只针对 A 类主链。C 类当前已有 `autoretry` 的，不得未经负责人批准删除；但必须在容量报告里计入它与主链同时到达时的影响。

## 三、给 Codex / Claude Code 的通用任务提示词

将下面整段复制给对应服务的代码代理，并把 `<SERVICE>`、`<STAGE>`、`<ENTRY_FUNCTION>` 替换为实际值：

```text
你只负责 novel-game-factory/services/<SERVICE>，不得修改 api_gateway、
platform_adapter、common、k8s、Jenkinsfile 或其他业务服务。

权限：
- 先记录当前 git status，保留所有既有改动。
- 不得 commit、push、部署或操作真实集群。
- 只允许修改负责人明确批准的主链可达文件，以及 tests/ 中明确属于本服务的测试。
- 不得读取或输出 .env、secret YAML、访问密钥或令牌。

背景：
- 系统会一次入队 30 本、每本约 50 万字。
- 当前阶段由 10 个 Pod 执行，每 Pod Celery concurrency=1、prefetch=1。
- 顶层阶段并发必须保持 10；任务内部 fan-out 必须被明确统计和限制。
- 消息按至少一次投递设计，同一 task_id/stage/attempt 可能重复到达。
- RabbitMQ 是 game-platform 到 Adapter 的第一跳；Redis/Celery 是 Adapter 到本服务的第二跳。
- 如果平台尚未提供 fenced complete/fail 与 lease ownership check，只能审计并报告，不能在服务内自建锁来绕过该阻断。

目标：
1. 不改变算法、Prompt、模型参数、输出结构和阶段顺序。
2. 审计 <ENTRY_FUNCTION> 的幂等、状态、异常、产物和资源释放。
3. 列出本服务全部 Celery 入口并按 A/B/C/D 分级；只修改获批的 A 类主链文件和负责人明确批准的 B 类自动任务文件。
4. 同一 task_id 并发投递 10 次时，只允许一个有效 owner 发布最终结果。
5. 供应商支持 idempotency key 时必须使用稳定业务键；不支持时必须报告最坏重复付费边界，不得声称零重复调用。
6. 所有外部 API 必须有有限 timeout 和有上限重试，并计算 SDK×wrapper×Celery×平台 attempt 的最坏调用次数。
7. 两个数据库 DSN 的 Session 都必须短事务并可靠关闭；禁止循环内重复 create_engine。
8. 最终产物必须临时写入、校验、原子替换；不能用“文件存在”代表成功。
9. 任务返回成功前，权威产物和 fenced complete 必须成功。
10. 收到异常或退出信号时，清理线程、子进程、临时文件和数据库连接；不得覆盖 Celery 的进程级 signal handler。

执行顺序：
A. 先只读生成从 <ENTRY_FUNCTION> 出发的可达调用图，并列出 A/B/C/D 任务和风险，不要编辑。
B. 标出可达路径中的 ThreadPoolExecutor、ProcessPoolExecutor、asyncio.gather、Semaphore、批处理、外部 API、create_engine、最终文件写和子进程。
C. 给出单 Pod 与 10 Pod 的真实峰值、两 DSN 连接预算、临时盘/PVC/inode 预算和最坏供应商调用次数。
D. 如果服务是 char_designer 或 scene_painter，到此停止并等待负责人逐文件批准；其他服务也只能编辑批准清单中的文件。
E. 只做最小可靠性修复，不修改未经批准的 B 类文件、任何 C/D 类任务、旧脚本或业务语义。
F. 添加离线 mock 的幂等、故障注入、产物损坏、DB 连接和 30任务排队合同测试；真实账号只做获批的小型 canary。
G. 运行编译、单测、服务镜像构建或等价启动检查。
H. 最终报告必须列出：修改文件、未修改的业务语义、内部并发峰值、DB 连接预算、
   API timeout/retry、产物合同、测试结果、剩余风险。

禁止：
- 不得用 sleep 增加“稳定性”。
- 不得添加无限 retry。
- 不得把失败降级成 warning 后继续 complete_stage。
- 不得删除或覆盖其他人的工作树改动。
- 不得读取、打印或提交任何 secret。
- 不得自行 commit、push 或部署。
- 不得用 mock 通过结果冒充真实 canary，也不得在离线故障测试中调用真实付费账号。
```

## 四、所有服务必须完成的审计与获批改造

### 4.0 公共 stage fencing 已交付，业务服务不得绕过

平台公共实现位于 `common/progress_utils.py` 与 `common/stage_fencing.py`，保持现有主链入口调用签名兼容，并已覆盖 Redis 原子 owner 校验、PostgreSQL 单调 fencing token、滚动升级旧 owner 拒绝及本地永久 lost fence。业务负责人不得另造第二套锁，必须继续使用公共入口，并在最终产物发布前调用公共 ownership check。

公共层已提供并通过合同测试的能力：

- 原子 claim：同一 `task_id + stage + attempt/execution_id` 只能有一个 owner。
- join：发现其他 owner 存活时等待其终态，不启动第二份昂贵主体。
- takeover：只有旧 lease 明确消失后才能由一个新 owner 接管。
- ownership check：每个不可逆提交前确认 owner 未丢失。
- fenced complete：只有当前 owner 才能把阶段标 COMPLETED。
- fenced fail：只有当前 owner 才能标 FAILED；丢 lease 的旧 Worker 不得覆盖新 owner 状态。
- lease renew 失败后在本进程设置本地 fence，即使 Redis 暂时恢复也不能继续发布旧结果。

部署前数据库账号必须有首次创建 `stage_execution_fences`、trigger function 和 trigger 的 DDL 权限。若权限不足，Worker 会 fail closed；不得临时绕过 fencing。

### 4.1 入口幂等与阶段状态

现有 A 类主链应保持以下兼容调用语义：

```python
from common.progress_utils import (
    StageLeaseLostError,
    complete_stage,
    ensure_stage_lease_owned,
    fail_stage,
    is_stage_already_running,
    wait_for_stage_completion,
)

if is_stage_already_running(task_id, STAGE):
    wait_for_stage_completion(task_id, STAGE)
    return task_id

try:
    # 原业务主体：不要改变算法。
    result = run_business_pipeline(task_id)
    validate_required_outputs(task_id, result)

    # 最终文件、OSS pointer、DB 完成态等不可逆提交前都要检查。
    ensure_stage_lease_owned(task_id, STAGE)
    publish_validated_outputs(result)

    complete_stage(task_id, STAGE, END_PROGRESS)
    return task_id
except StageLeaseLostError:
    # 不能 fail_stage；新 owner 可能已经接管。
    raise
except Exception as exc:
    fail_stage(task_id, STAGE, FAILED_PROGRESS, str(exc))
    raise
finally:
    cleanup_local_resources()
```

检查点：

- 入口只接收 `task_id` 或轻量引用。
- claim 必须在任何付费 API、GPU、TTS、图片生成前完成。
- 如果阶段已经 COMPLETED，直接返回并验证已有结果，不重新生成。
- 如果另一个 owner 正在执行，等待或 reattach；不能偷偷开始第二份。
- 产物发布与状态提交之间必须有明确顺序，并在每个不可逆边界检查 ownership。
- 丢 lease、fenced complete 失败和 fenced fail 失败必须重新抛出，让 Adapter/平台看到真实状态。
- 不要在业务服务里把 FAILED 自动改回 PENDING；新业务重试由平台 attempt 负责。

### 4.2 任务内部 fan-out 预算

Deployment 的“并发 10”只限制顶层任务。真实供应商并发为：

```text
真实 API 峰值 = 10 个 Pod × 每任务同时进行的 API 调用数
```

例如每个任务内部 `Semaphore(5)`，真实峰值就是 50，而不是 10。

负责人必须：

1. 找出所有 `ThreadPoolExecutor`、`ProcessPoolExecutor`、`asyncio.gather`、`Semaphore` 和批量 SDK 调用。
2. 为每个 fan-out 写出环境变量，不允许只留硬编码。
3. 默认值保持当前业务行为；如果要降低或升高，必须单独说明。
4. 给出账号/RPM允许的计算：

```text
单任务内部并发上限 <= floor(账号允许并发 / 10)
单任务每分钟请求上限 <= floor(账号允许 RPM / 10)
```

5. 对不同供应商分别算账，不能把文本、图片、TTS 的额度混为一个值。
6. 记录运行时实际峰值，不只检查配置值。

建议环境变量命名：

```text
<SERVICE>_MAX_INTERNAL_CONCURRENCY
<SERVICE>_<PROVIDER>_MAX_CONCURRENCY
<SERVICE>_<PROVIDER>_REQUESTS_PER_MINUTE
```

### 4.3 外部 API timeout、供应商幂等与重试乘法

每个外部请求必须同时满足：

- connect timeout 有限。
- read timeout 有限。
- 总重试次数有限。
- 仅重试连接失败、408、429、5xx 等暂态错误。
- 遵守 `Retry-After`。
- 使用指数退避和 jitter。
- 401、403、参数错误、响应 schema 错误不得盲目重试。
- 供应商支持 idempotency key 时，使用稳定的 `task_id + stage + attempt + business_unit_id`，同一业务单元重放不得生成新 key。
- 记录 provider request id、提交时间和响应摘要，便于 Worker 在“供应商已受理、本地未落盘”窗口死亡后查询或对账。

推荐形式：

```python
for attempt in range(1, max_attempts + 1):
    try:
        response = client.call(
            ...,
            timeout=(connect_timeout, read_timeout),
        )
        validate_response(response)
        return response
    except PermanentRequestError:
        raise
    except RetryableRequestError:
        if attempt == max_attempts:
            raise
        sleep(backoff_with_jitter(attempt))
```

禁止无 `timeout` 的 `requests.get/post`，禁止 `while True` 重试。

负责人必须列出所有重试层并计算：

```text
最坏供应商调用次数 =
  provider SDK attempts
  × 业务 wrapper attempts
  × Celery autoretry attempts
  × game-platform business attempts
```

- A 类主链不得再增加 Celery `autoretry`；业务请求级暂态重试和平台业务 attempt 必须分别有界。
- C 类任务若保留现有 `autoretry`，必须单独计算，不得把它套用到主链结论。
- 如果供应商不支持幂等键，也没有可查询 request id，则不能承诺“零重复付费调用”；只能承诺旧 owner 不能覆盖新 owner 的最终产物，并报告最坏重复次数。
- 正常报告必须同时给出“单次 task attempt 的 API 上限”和“跨平台 attempt 的总上限”。

### 4.4 PostgreSQL 连接预算

当前七阶段共有 70 个 Worker Pod，代码同时使用业务库 DSN 与平台库 DSN。若每个进程默认创建多个 SQLAlchemy Engine，默认池大小可能把连接数放大到数百。两个 DSN 必须分别统计；如果它们指向同一 PostgreSQL 实例，还要在实例层汇总。

此前的 `229` 只统计了 70 个 Worker 的两个公共一槽池，以及 API Gateway、Adapter、Manifest Portal、creation-engine 和 game-platform 的受控池，遗漏了 Story Planner、Info Extractor 等主链导入的私有全局 Engine。完整静态审计结果为：平台 DSN 约 `314`，业务 DSN 约 `405–420`；两个 DSN 同实例时约 `719–734`。这仍未计入函数内临时 Engine、`psycopg2` 直连、迁移、监控、人工连接及故障余量。

负责人必须执行：

```bash
rg -n "create_engine\\(" <APPROVED_REACHABLE_FILES>
rg -n "Session\\(|sessionmaker\\(" <APPROVED_REACHABLE_FILES>
```

改造要求：

- 每个 Worker 进程、每个数据库 URL 复用一个 Engine；业务库和平台库分开报告。
- 禁止在章节循环、角色循环、场景循环和每次函数调用里反复 `create_engine`。
- Session 必须使用 `with Session(engine)` 或 `try/finally close()`。
- 外部 API、OSS、TTS、图片生成、sleep 期间不得持有数据库事务。
- 事务只包含必要查询/写入，尽快 commit/rollback。
- 单槽 Worker 的建议默认：`pool_size=1`、`max_overflow=0`、`pool_pre_ping=True`。
- 如果内部并发确实需要更多连接，负责人必须给出峰值公式和证据。

预算公式：

```text
单个 DSN 连接峰值 =
  replicas
  × worker concurrency
  × 每进程 Engine 数
  × (pool_size + max_overflow)
实例总峰值 = 同实例上所有 DSN 峰值之和 + Gateway/Adapter/运维预留
```

容量验收规则：

- 业务 DSN 与平台 DSN 分别给出 Engine 数、`pool_size`、`max_overflow`、理论峰值和实测峰值。
- 两个 DSN 若在同一实例，`260–300` 已低于当前静态池配置容量，不能通过验收。优先把主链私有 Engine 复用并收敛为单槽池，或部署 PgBouncer 后根据池化模式和实测峰值确定后端连接预算；`max_connections` 还要另留超级用户、备份和运维连接。禁止在未核算 PostgreSQL 内存成本时直接把 `max_connections` 抬到 734 以上。
- 两个 DSN 若在不同实例，每个实例都必须独立满足自己的理论峰值、实测峰值和故障余量；不得把另一实例的空闲连接算进本实例预算。
- 无论采用直连还是 PgBouncer，正常实测峰值都应保留明确余量，不能以“没有立刻报 too many connections”作为通过标准。

负责人不能只交理论值，还必须提供：

1. 压测前、10 个并发任务峰值、任务结束后各一次 `pg_stat_activity` 快照。
2. 按 `datname`、`application_name`、state 汇总的连接数；不得输出密码或完整 DSN。
3. 峰值期间 `idle in transaction = 0`。
4. 任务结束后连接数回落到稳定基线，没有逐任务增长。
5. 两个 DSN 是否落在同一实例的结论；若同实例，给出 Engine 收敛后的实测合并峰值、PgBouncer/直连方案和明确余量；若不同实例，分别给出最终预算和余量。

运维在两个 DSN 上分别执行以下只读 SQL，并在压测前、峰值、结束后三个时点留档：

```sql
SHOW max_connections;
SHOW superuser_reserved_connections;

SELECT current_database(), inet_server_addr(), inet_server_port(), current_user;

SELECT
    datname,
    usename,
    COALESCE(NULLIF(application_name, ''), '(empty)') AS application_name,
    state,
    count(*)
FROM pg_stat_activity
WHERE backend_type = 'client backend'
GROUP BY 1, 2, 3, 4
ORDER BY 1, 5 DESC;

SELECT
    datname,
    count(*) AS total,
    count(*) FILTER (WHERE state = 'active') AS active,
    count(*) FILTER (WHERE state = 'idle') AS idle,
    count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_tx
FROM pg_stat_activity
WHERE backend_type = 'client backend'
GROUP BY datname
ORDER BY total DESC;
```

私有 Engine 多数没有 `application_name`，因此 `(empty)` 分组不能忽略。结束后连接数必须回落，`idle in transaction` 必须为 0。

### 4.5 本地产物原子发布

不得直接写最终文件：

```python
final_path.write_bytes(payload)       # 禁止
open(final_path, "w").write(data)    # 禁止
```

应使用公共原子写：

```python
from common.artifact_io import atomic_write_bytes

payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
atomic_write_bytes(final_path, payload, validator=validate_json_file)
```

需要调用方自行提供格式校验器：

- JSON：可解析、必填字段存在、task_id/version 正确。
- 图片：能被 Pillow 解码、宽高大于 0、文件未截断。
- WAV/音频：header、时长、采样率和 data chunk 合法。
- HTML/JS/CSS：必需入口文件和引用资源存在。
- 多文件目录：先写 staging/version 目录，全部通过后最后切换 manifest/pointer。

只有“文件存在”不算成功。

staging/version 只能是内部实现细节：

- 对外最终路径、数据库字段和现有 manifest schema 不得改变。
- 如果当前合同没有 manifest/pointer，新增它之前必须提交公共合同变更建议。
- 不能原子替换非空目录时，优先使用既有 release 目录和既有稳定入口；不得擅自让下游改读新路径。

### 4.5.1 七服务权威产物合同

下面是负责人必须核对的最低集合，不是授权修改路径。负责人要从实际 producer/consumer 调用链补齐具体文件名、表名和必填字段；任何“是否必需”无法从代码确认的项目都标为 BLOCKED，交业务负责人决定，代码代理不得自行降级为可选。

| 服务 | 最低权威产物核对项 | 直接消费者 | OSS 默认角色 |
| --- | --- | --- | --- |
| Story | `story_tree_json` / `story_tree_md`、story tree JSON、节点 Markdown、summary/worldview/outline 状态 | Info、Gateway 前端状态 | 以实际读取路径为准；不得只因上传成功就替代 DB/PVC 完成条件 |
| Info | characters、name_mappings、plots、character_styles、character_prompts、character positions、scene prompts/scenes、backgrounds、cg、sound、ultimate，以及当前落地 JSON | Character、Scene、Audio、Sound、Builder | 当前镜像若失败可忽略，必须证明所有下游都可从共享 PVC/DB读取；否则升级为强制 |
| Character | `images/characters`、`images/references`、现有 character manifest 及其引用的全部图片 | Builder、重生成接口 | 共享 PVC 为权威时是镜像；任何下游只读 OSS 时必须强制验证上传 |
| Scene | `images/scenes`、`images/backgrounds`、`images/cg` 及对应 DB/调度数据 | Builder、重生成接口 | 同上；旧 OSS 不得倒灌覆盖较新本地版本 |
| Audio | 启用语言的 subtitles、voices/audio、voice mapping 和对应 DB 状态 | Builder、重生成接口 | 按启用语言逐项判定，不能用一个语言成功代表全部完成 |
| Sound | BGM、SFX、ambience 的必需集合与 sound/ultimate 引用 | Builder、重生成接口 | 负责人必须明确哪些类别允许为空，不能由代码代理猜测 |
| Builder | 在线包/离线包 release、入口 HTML、JS/CSS、全部引用资产、现有发布入口和 nginx 配置 | 最终用户/CDN | 一旦运行时 URL 被改写到 OSS/CDN，OSS 就是强制完成条件，不得吞上传失败 |

每项必须记录：producer、consumer、权威源、稳定版本标识、校验器、完成条件、回滚版本和清理规则。

### 4.6 OSS 发布

如果 OSS 是该阶段完成合同的一部分：

1. 先上传不可变版本 key。
2. 通过 HEAD/metadata 核对 size；可行时再校验 checksum/etag。
3. 最后更新 manifest/pointer。
4. 上传失败、超时或校验不一致必须使阶段失败。
5. 下载到临时文件，校验后原子替换本地文件。
6. 旧 OSS 产物不得覆盖更新的本地有效结果。
7. 清理只删未被当前 manifest 引用的历史版本。

如果 OSS 只是可选镜像，负责人必须用消费者调用链证明“本地/PVC 或 DB 才是权威源”，不能一边忽略 OSS 失败、一边让下游只读 OSS。无法证明时按强制完成条件处理并标为 BLOCKED，等待负责人确认。

### 4.7 子进程、GPU 与大内存对象

对 ffmpeg、浏览器、Node、模型脚本等子进程：

- 使用参数数组，禁止对不可信输入使用 `shell=True`。
- 启动独立 process group。
- 设置总超时。
- 超时或异常时先 TERM，宽限后 KILL。
- 必须 `wait()`/reap，不能留下孤儿进程。
- 检查 return code 和必需输出。
- 日志流式或限长，不能把数 GB stdout 全部留在内存。

模型/GPU：

- 10 个 Pod 会加载 10 份模型；负责人必须报告单 Pod 峰值内存、显存和冷启动时间。
- 不允许在单任务循环中重复加载同一个大模型。
- 大对象在 finally 中释放；必要时明确进程复用策略。

### 4.8 优雅退出

收到 SIGTERM 时：

- 不再领取新任务。
- 当前任务要么在 Deployment 宽限内完成，要么安全失败并依赖 late ACK/lease 恢复。
- 不要捕获退出后继续假装成功。
- 关闭线程池、Session、HTTP client、文件句柄、子进程。
- 临时文件可删除，已发布的有效版本不可回滚成旧版本。
- 不得在业务模块安装进程级 SIGTERM/SIGINT handler 覆盖 Celery；依靠 Celery warm shutdown、`finally` 和公共 managed-process 工具完成清理。

验收必须发送真实 SIGTERM 给 Worker/Pod，而不是只单测 cleanup 函数，并记录任务状态、Celery delivery、子进程列表和恢复结果。

### 4.9 内存、临时盘、PVC、inode 与吞吐预算

每个负责人必须同时报告：

```text
阶段运行工作集 = 单任务峰值临时字节 × 10 个 active
阶段保留工作集 = 单任务最终字节 × 30 本
最低空间预算 = 运行工作集 + 保留工作集 + 上一有效版本 + 30% 安全余量
最低 inode 预算 = 单任务峰值文件数 × 30 + staging/旧版本安全余量
```

- 分别报告容器临时层、共享 PVC 和 OSS 的字节、inode、读写吞吐和峰值延迟。
- 报告单 Pod RSS/显存与 10 Pod 总量，并与当前 Deployment requests/limits 逐项比较。
- 任一实测峰值超过 limit 的 80%，或 PVC 可用空间/inode 低于上述预算，本服务不得签字通过。
- 故障测试必须包含 ENOSPC/配额失败；不得把磁盘写失败降级为成功。
- Builder 还必须报告 10 个并发 staging、zip 临时文件和上一 release 同时存在时的峰值。

## 五、各服务的具体审计与改造清单

### 5.1 Story Planner

入口：

- A：`services/story_planner/tasks.py::parse_novel_to_draft`
- B：`generate_project_name_only`；单文件上传路径的 `generate_summary_only`
- C：`regenerate_chapter_branch_nodes`

重点文件：

- `core/llm_resilience.py`
- `core/llm_client.py`
- `core/prompt_budget.py`
- `core/runtime_config.py`
- `core/novel_reader.py`
- `core/hierarchical_processing.py`
- `core/story_generator.py`
- `utils/result_cache.py`
- `batch_translate.py`

操作：

1. 保留 Deployment 中本地 RPM=0、全局 limiter disabled 的平台决定，不在代码里重新加第二道总限流。
2. 批量新建 30 本会额外产生 30 个起名任务；把它们与 30 个主链任务共同计算 `story_queue` 等待时间、LLM 并发和 RPM。单文件路径还要计摘要任务。
3. 审计一次 50 万字主链、一次摘要、一次起名分别会产生多少 LLM 调用、最大输入 token、分段数和最长运行时间。
4. 所有 LLM 调用必须经过统一的 timeout/retry/idempotency 责任层；重点检查 `core/llm_client.py` 是否存在绕过 resilience 的直接路径。
5. 长文本分段、summary merge 和 worldview merge 必须有有界 batch size。
6. `generate_summary_only`、`generate_project_name_only` 要有稳定任务/业务键、有限 wall-clock deadline；失败语义保持现状，但不能无限占住 `story_queue`。
7. 翻译失败不得静默返回空字符串；是否允许回退原文属于业务决策，不能由可靠性改造擅自改变。
8. story tree、节点 Markdown、summary、cache 等主链最终写必须逐项识别；只把主链可达的直接写替换为原子写。
9. `batch_translate.py` 的 subprocess 必须有 timeout、process-group 和 TERM/KILL/reap；不得修改翻译算法。
10. 检查 `services/story_planner/table/models.py` 的业务/平台 Engine 是否进程级复用。

验收：

- 30 个主链任务加 30 个上传起名任务共同入 `story_queue` 时，总 active 不超过 10，主链最终全部 COMPLETED，起名失败不会阻塞主链。
- 同一 task 重复投递 10 次只暴露一份有效 story release；供应商支持幂等键时验证主 LLM 只受理一次，否则报告重复付费边界。
- 50 万字输入不会被完整塞入 Redis/Celery 消息；第一跳 RabbitMQ Job 也只携带引用。
- 429/5xx 注入后重试有界，无永久 PROCESSING。
- 离线固定 provider 下剧情 schema/关键不变量和 golden snapshot 不变；真实模型 canary 不要求逐字相同。

### 5.2 Info Extractor

入口：

- A：`services/info_extractor/tasks.py::extract_info`

已知 fan-out 热点：

- 多处 `Semaphore(5)`
- `scene_to_plot.py`
- `scene_prompts.py`
- `scene_extractor.py`
- `cg/cg_prompt.py`
- `plot/extract_character_position.py`
- `plot/extract_plots_to_db.py`
- `plot/extract_character_styles.py`
- `plot/name_anonymizer.py`
- `sound/extract_sound_to_db.py`
- `atmosphere/special_effects.py`
- `aggregation/integrate_material.py`

操作：

1. 把每个硬编码并发来源列成表，说明供应商和用途；除多处 5 外，重点包含 `extract_plots_to_db.py` 的 `MAX_POV_CONCURRENT=10`。
2. 计算顺序步骤与可能重叠步骤的真实峰值；10 Pod × 5 为 50，10 Pod × 10 为 100，不能只报单个 semaphore。
3. 给不同 API 独立环境变量，不使用一个全局变量误伤所有步骤。
4. 不允许一次 `asyncio.gather` 无界创建数千 coroutine；使用 semaphore 或分批。
5. 只在 `extract_info -> run_pipeline` 可达路径内搜索并消除循环/函数级 `create_engine`；分别报告业务 DSN 与平台 DSN。该服务目前是数据库连接风险最高的模块。
6. 已存在 JSON 只有通过完整性校验才能跳过；不能只看文件/DB 行存在。
7. 多张平台表的写入必须定义一致性边界：要么同事务，要么有明确 checkpoint/恢复规则。
8. 完成态之前按权威合同验证 characters、name_mappings、plots、character_styles、character_prompts、character positions、story tree、scene prompts/scenes、backgrounds、cg、sound、ultimate 和当前落地 JSON；如果某项按业务允许为空，必须由负责人书面确认。
9. `main.py` 和子模块现有凭据/DSN 日志不得出现在审计输出；发现硬编码凭据只报告文件与风险，不打印值。

验收：

- 输出表/JSON 与现有 golden 数据一致。
- 任意一个子步骤失败时，阶段不能被标 COMPLETED。
- 10 Pod 下记录真实 API 峰值和 PostgreSQL 峰值连接。
- DB 短断后无 Session 泄漏、无 idle-in-transaction。
- 中间步骤成功、ultimate/最后表失败时不得留下可被下游误认为完整的完成态；恢复后不得重复覆盖较新的表版本。

### 5.3 Character Designer

入口：

- A：`services/char_designer/tasks.py::generate_characters`
- C：`regenerate_characters`、`redraw_character`

当前原则：该服务状态不稳定，冻结算法改动。代码代理第一轮只能完成可达调用图、风险和测试方案，然后停止；负责人必须逐文件批准第二轮修改清单。没有批准清单时不得编辑任何 Character 源码。

重点文件：

- `main.py`
- `core/generate_calm.py`
- `core/generate_emotion.py`
- `core/generate_variant_calm.py`
- `core/generate_shots.py`
- `core/character_manifest.py`
- `core/regenerate.py`、`core/redraw_character.py`（C 类，仅做风险/容量清单）
- `image_providers/*.py`

操作：

1. 不改任何生图 Prompt、裁切/拼接逻辑、角色命名与 manifest schema。
2. 当前 Qwen batch 默认 1；负责人必须确认其他 provider、抠图和拼接路径没有隐藏线程池或子进程。
3. `main.py` 的 Popen 是主链阻断热点：第二轮获批后才可改为公共 managed process，加入总 timeout、process group、TERM/KILL/reap 和限长日志。
4. 只在 A 类可达路径检查函数内 `create_engine`；C 类 regenerate/redraw 和 `useless/` 不得顺手修改。
5. 图片下载必须检查 HTTP 状态、Content-Type、字节数和可解码性；`image_providers` 中直接写最终路径的位置必须列全。
6. calm、emotion、variant、shot 不得直接覆盖上一份有效图片。
7. manifest 最后发布；只有全部必需角色资源校验通过后才能 fenced complete。
8. `core/regenerate.py` 当前存在 OSS 主路径覆盖逻辑，只报告并隔离为 C 类；未经负责人批准不得改它的用户语义。
9. 重复任务不得因为“文件存在”跳过损坏的零字节/截断图片。

验收：

- 现有角色图片 golden/视觉抽检不变。
- 损坏 PNG/JPEG 不得被判成功。
- kill Worker 后无半张正式图片、无孤儿抠图/生图进程。
- 同 task 重投不产生两套互相覆盖的 manifest。
- 第二轮 diff 只包含负责人批准的 A 类文件；C/D 类文件 diff 必须为空。

### 5.4 Scene Painter

入口：

- A：`services/scene_painter/tasks.py::generate_scenes`
- C：`generate_style_anchor`、`regenerate_scene_materials`

当前原则：与 Character 相同，第一轮只读审计后必须停止；负责人逐文件批准第二轮修改。不得因“当前状态不对”而扩大重构。

重点文件：

- `main_to_db.py`
- `core/scene_generator.py`
- `core/scene_instances.py`
- `core/cg_generator.py`
- `core/cover_generator.py`
- `core/retry_failed_images.py`
- `core/redraw_scene.py`、`core/regenerate.py`（C 类，仅做风险/容量清单）
- `image_providers/*.py`

操作：

1. 当前 `CONCURRENT_REQUESTS` 默认 1，10 Pod 时供应商峰值约 10；不要无证据调高。
2. 不改场景 Prompt、场景选择、CG 逻辑、图片命名和数据库 schema。
3. `main_to_db.py` 是主链实际子进程编排器；当前 Popen、无限 wait 和 stdout 累积必须作为第二轮首要阻断，获批后使用公共 managed process 与限长日志。
4. 所有 provider 统一 timeout、重试分类和响应校验，并列出直接写最终图片的位置。
5. 生成脚本/子进程非零 return code 不得静默标成功；当前主入口“非零仍继续 chain”必须列为明确失败点。若业务允许部分成功，最低产物合同必须由负责人先签字。
6. 图片和主调度 JSON 原子发布，DB 状态与文件版本必须一致。
7. 只在 A 类可达路径把 Engine 改为复用，外部 API 期间不持有事务；C 类 redraw/regenerate 不得顺手改。
8. `core/regenerate.py` 的 OSS live 覆盖只做风险清单，未经批准不改用户重生成语义。

验收：

- 10 Pod 下 provider 实测 active 不超过批准值。
- 任一必需场景图损坏时阶段失败或进入明确的部分失败状态，不能 COMPLETED。
- 旧 OSS 场景图不能覆盖新本地结果。
- 第二轮 diff 只包含批准的 A 类文件；C/D 类文件 diff 必须为空。

### 5.5 Audio Producer

入口：

- A：`services/audio_producer/tasks.py::generate_audio`
- C：`regenerate_audio`、`regenerate_character_voice`

已知内部并发：

- `generate_json_from_txt.py MAX_WORKERS=6`
- `generate_audio_from_speech.py MAX_WORKERS=3`
- `extract_subtitles_from_db.py TRANSLATE_MAX_WORKERS=5`
- `extract_plots_to_db.py MAX_CONCURRENT_REQUESTS=5`
- `auto_cast_voices_from_db.py` 的业务/平台 DB Engine
- `regenerate_audio.py` 的 OSS/DB 读写（C 类，不得未经批准修改）

这意味着 10 Pod 下不同路径可能达到 60 / 30 / 50 的真实并发。

操作：

1. 把 A 类可达路径中的上述常量改为环境变量读取，默认保持原值，分别记录 TTS、翻译、文本 API 的真实峰值；先确认这些步骤是顺序还是重叠，不能简单相加或遗漏。
2. 检查账号池是否按 provider 分流；不要把单账号 RPM 当作全池 RPM。
3. TTS 请求必须有 connect/read timeout、有界 retry、Retry-After 和响应音频校验。
4. WAV/音频正式文件发布前校验 header、时长、采样率和非空 data；字幕/JSON 直接写最终路径的位置必须列全。
5. 按实际启用语言逐一列字幕、语音映射、JSON 与音频完成合同；一个语言成功不能代表其他启用语言完成。
6. 搜索 A 类可达路径中的 `create_engine`，包括 `auto_cast_voices_from_db.py`；按业务/平台 DSN 复用并确保 Session 关闭。
7. 线程池使用 `with ThreadPoolExecutor(...)` 或 finally shutdown。
8. C 类 regenerate 的 OSS 主路径覆盖、字幕更新和 Celery autoretry 只计入风险与容量，未经批准不改。

验收：

- 30 本任务排队时不创建 30×无限线程。
- TTS 429、连接超时和损坏音频均能明确失败且重试有界。
- 退出后无线程/ffmpeg/音频子进程残留。
- 对每个启用语言跑完整性 oracle，缺失任一必需 subtitle/audio 不得 fenced complete。

### 5.6 Sound Arranger

入口：

- A：`services/sound_arranger/tasks.py::generate_sound`
- C：`regenerate_sound`

已知内部并发：

- `generate_sound.py MAX_WORKERS=3`

10 Pod 下真实峰值约 30。

操作：

1. 将 worker 数改为环境变量，默认保持 3。
2. 分开计算 BGM、SFX、ambience 的 API 和本地模型资源。
3. 检查 sentence-transformers/embedding 模型是否每任务重复加载。
4. `generate_sound.py` 当前包含 Batch-1 和 Batch-2-Retry；必须把两轮与 HTTP 重试、平台 attempt 一起计算最坏 API 次数，不能只报 `MAX_WORKERS=3`。
5. 音频输出必须校验后原子发布。
6. pipeline 返回值、异常和必需产物必须明确；不能无条件 complete。BGM、SFX、ambience 哪些允许为空必须由负责人签字。
7. A 类可达 Engine 按双 DSN 复用；C 类 regenerate 路径创建的连接和 OSS 覆盖只报告，未经批准不改。

验收：

- 10 Pod 下内存峰值可承受 10 份模型。
- 任一必需音频缺失/损坏不能标 COMPLETED。
- 同 task 重投不覆盖有效旧版本。
- provider HTTP retry × Batch-2 × 平台 attempt 的最坏调用数在批准预算内。

### 5.7 Game Builder

入口：

- A：`services/game_builder/tasks.py::build_game_package`
- B：路由到 `build_queue` 的 `celery.chord_unlock`
- C：`rebuild_online_package`、`export_offline_package`

重点文件：

- `converter.py`
- `converter_steam.py`（C 类离线导出，仅做容量/输出冲突清单）
- 构建模板与资源复制逻辑
- 在线包/离线包输出目录

操作：

1. 不改视觉小说模板行为、脚本生成语义和输出目录合同。
2. 每个 task/execution 使用独立 staging 构建目录，禁止重复 task、rebuild/export 与主任务共用可写临时目录。
3. 完整构建成功后再原子切换现有正式 release/manifest；不得改变对外输出合同。
4. 主路径当前主要是 Python 文件生成，不得凭假设新增 Node/npm 步骤；如果可达调用图实际发现子进程，再按公共 managed-process 合同处理。
5. `converter.py` 会更新统一 nginx 配置，这是 10 个 Builder 并发时的共享写热点；必须使用现有批准的跨进程锁、临时文件、校验和原子替换，不能丢其他 task 的 block。
6. `converter.py` 直接上传 OSS 并回写 `character_prompts.oss_route`；必须确认上传是否为运行时强制条件、版本前缀是否不可变，以及 DB 回写失败能否阻止发布。
7. 验证入口 HTML、JS、CSS、图片、音频、字体及其引用全部存在；如果运行时 URL 指向 OSS/CDN，还要逐项 HEAD/大小校验。
8. `converter.py` 中的 Engine 不得在章节/节点循环中重复创建，按平台 DSN 单独报告。
9. 构建失败保留上一份有效包，不发布半成品。
10. C 类 rebuild/export 与主链共享 `build_queue`，只做排队、输出目录和磁盘峰值清单，未经批准不改用户功能。

验收：

- 同 task 同时构建两次只暴露一个有效 release。
- kill 构建进程后正式目录仍是上一份有效版本。
- 离线包可解压、入口可加载、引用资源无 404。
- 10 个不同 task 同时更新统一 nginx 配置后，10 个配置块全部存在、无交叉覆盖、语法校验通过。
- chord unlock、rebuild/export 干扰下总 Worker active 仍不超过 10，主链不会永久饥饿。

## 六、服务负责人必须提交的测试

测试分三层，不能互相冒充：

### 6.1 每服务离线确定性测试：必须

- 使用 fake provider 或录制的无敏感响应，不访问真实付费账号。
- 30 个不同 task 使用轻量引用；Story 额外使用本地 50 万字合成输入验证消息不携带正文。
- 正常场景 30 个任务必须全部 COMPLETED；“全部终态”不合格，因为 FAILED 也属于终态。
- 同一 `task_id + stage + attempt` 并发投递 10 次，只能有一个有效 owner 发布，最终产物 hash/manifest 一致。
- 生成类 golden 使用固定 fake provider 比较 schema、必填字段、排序和关键不变量；真实 LLM/图片不能要求逐字节稳定。
- 实测单 Pod 内部 fan-out、provider request count、业务/平台 DSN 连接数、RSS、临时字节和 inode。

### 6.2 每服务真实 canary：负责人批准后必须

- 每服务只选 1 个小规模代表任务，使用真实账号验证认证、timeout、响应解析和产物校验。
- canary 不做 30 本，不做破坏性故障注入，不承担容量结论。
- 报告真实 provider request id、请求数、耗时和成本摘要；不得记录 prompt 正文、密钥或完整响应中的敏感内容。

### 6.3 一次端到端真实压测：所有阻断解除后执行

- 只执行一次 30 本 × 50 万字真实全链压测，不允许七个负责人各烧一轮。
- 运行前必须满足第 1.2 节和第八节所有硬门槛。
- 30 本在正常账号/RPM条件下必须全部 COMPLETED，七阶段均无永久 PROCESSING、无假成功、无半成品。
- 压测结束后等待队列静默，再对 30 个 task_id 运行统一 artifact oracle。

### 6.4 必测故障窗口

| 故障点 | 注入方式 | 必须证明 |
| --- | --- | --- |
| DB 从 PENDING 切到 PROCESSING 后、首个 lease 建立前 | 真实 kill Worker/process | 单一恢复者可接管，不永久 PROCESSING |
| lease 正常执行中 | kill owner | 旧 owner 不再发布，新 owner fenced takeover |
| Redis lease 续租失败 | 断开/代理故障 | 本地 fence 生效，恢复连接后旧 owner 仍不能提交 |
| Redis/Celery broker 断线 | 真实连接中断 | 无两个 owner 并行烧额度；delivery 最终恢复或明确失败 |
| provider 已返回成功、本地未持久化 | 在响应后 kill | 有幂等键时不重复受理；无幂等键时重复次数符合报告上限 |
| 临时文件写到一半 | kill/截断 | 最终路径仍是旧有效版本，无半文件 |
| 产物发布后、DB COMPLETED 前 | kill | 恢复后复用/校验同一版本，不覆盖新版本 |
| DB COMPLETED 后、Celery ACK 前 | kill | 重投直接验证并返回，不再次执行昂贵主体 |
| OSS 上传/HEAD/manifest 任一步失败 | 429/5xx/timeout/错误 size | 强制 OSS 合同不得 complete；可选镜像必须有本地权威证据 |
| 旧 OSS 与新本地并存 | 注入旧版本 | 旧 OSS 不得倒灌覆盖新版本 |
| PostgreSQL 短断 | 断开连接 | 无 Session 泄漏，恢复后无 idle-in-transaction |
| ENOSPC/inode exhaustion | 受控配额/故障注入 | 不 complete、不破坏上一版本、临时资源可回收 |
| SIGTERM/warm shutdown | 真实 signal/Pod delete | 无孤儿线程/进程，任务恢复符合 ACK/lease 合同 |
| OOM/资源逼近 limit | 受控压力 | 不出现无证据丢单；峰值不超过批准阈值 |

### 6.5 基础验证

每个服务还必须完成编译/import、现有单测、本服务新增测试、`git diff --check`，并从 clean source context 构建镜像、启动 Worker、确认 Celery task 注册和队列名。镜像构建不得读取或打包本地 `.env`/secret。

## 七、负责人交付模板

```text
服务名：
负责人：
审计日期/环境：
基线 commit：
工作树初始状态：
是否获得第二阶段编辑授权：
负责人批准的文件清单：
实际修改文件清单：
未批准目录 diff 是否为 0：

是否修改算法、Prompt、模型参数、输出结构：必须为“否”

Celery 入口分级：
- A 主链：
- B 自动辅助：
- C 手工辅助：
- D 排除项：
- stage/queue：

单任务内部并发：
- 文本 API：
- 图片 API：
- TTS：
- 本地线程/进程：

10 Pod 理论峰值：
实测峰值：
账号/RPM依据：

重试乘法：
- provider SDK attempts：
- wrapper attempts：
- Celery autoretry attempts：
- platform business attempts：
- 最坏供应商调用次数：
- idempotency key/request id 支持：
- 不支持时的最坏重复付费边界：

数据库：
- 业务 DSN Engine/pool/overflow：
- 平台 DSN Engine/pool/overflow：
- 两 DSN 是否同实例：
- before/peak/after pg_stat_activity 证据：
- 业务 DSN 理论峰值 / 实测峰值 / 实例可用连接 / 余量：
- 平台 DSN 理论峰值 / 实测峰值 / 实例可用连接 / 余量：
- 同实例时 Engine 收敛后的合计实测峰值、PgBouncer/直连配置与后端池预算：
- idle in transaction 峰值：

外部 API：
- connect timeout：
- read timeout：
- wall-clock deadline：
- max attempts：
- 可重试状态：
- 不可重试状态：

权威产物合同：
- producer -> consumer：
- 必需文件/表/语言/类别：
- 权威源与版本标识：
- 校验器：
- 内部 staging 与不变的对外路径：
- OSS 强制/镜像及代码证据：
- 上一有效版本与回滚：

子进程/GPU：
- 启动方式：
- timeout：
- TERM/KILL/reap：
- 单 Pod 峰值内存/显存：

容量：
- 单任务临时/最终字节：
- 单任务文件/inode 数：
- 10 active + 30 retained + 上一版本 + 30% 预算：
- PVC 可用字节/inode/峰值延迟：
- K8s requests/limits 对比：

测试：
- 每条 exact command、exit code、日志/报告路径：
- offline fake provider 30任务：
- 重复投递/fencing：
- 故障窗口矩阵：
- DB before/peak/after：
- 产物 oracle/hash/manifest：
- 真实 canary（明确标注 LIVE）：
- clean image digest、Worker 启动和 task 注册：
- git diff --check：

剩余风险：
外部阻断：
回滚方式：
未执行的真实操作：commit/push/deploy/集群变更
```

所有证据必须能由复核人重新执行。只写“通过”“已测”或粘贴没有命令、时间、环境、exit code 的截图不合格。

## 八、平台硬门槛：不由七服务负责人修改

### 8.1 `common` stage fencing

本仓库已交付第 4.0 节所述 claim/join/takeover、ownership check、fenced complete/fail 和本地 lost-lease fence。部署前仍须复跑以下公共合同测试：

- 两个 Worker 同时 claim 只有一个 owner。
- 旧 owner 丢 lease 后不能写完成、失败或最终产物。
- owner 在 DB PROCESSING 后、首个 lease 前死亡可被唯一接管。
- DB COMPLETED 后、ACK 前重投只验证已有产物，不重新执行昂贵主体。
- Redis 续租失败和恢复后，旧 owner 始终保持 fenced。

数据库账号必须有首次 DDL 权限；所有 Worker 必须完成新 `common` 镜像滚动并复跑合同测试后，才能签署“不重复终态覆盖”结论。供应商不支持幂等时，fencing 不能消除接管窗口中的重复付费调用。

### 8.2 `game-platform` transactional outbox：硬门槛

外部 `game-platform` 已落地 transactional outbox、稳定 message ID、publisher confirm/mandatory/persistent、lease 恢复及 callback retry/DLQ，并已在隔离 PostgreSQL 16 + RabbitMQ 3.13 环境通过 30 任务与故障测试。生产部署仍必须按下列状态机和目标集群证据验收，不能用 publisher confirm 单独代替 outbox：

1. 创建 Job/step 与写入 outbox intent 在同一 PostgreSQL 事务提交。
2. outbox 保存稳定 `message_id`，至少包含 job、step、attempt、routing key、payload hash 和创建时间。
3. publisher 以 lease/`SKIP LOCKED` 方式 claim `PENDING` 或过期 `PUBLISHING` 记录。
4. RabbitMQ publish 使用 durable exchange/queue、persistent message、`mandatory=true`。
5. 同时处理 publisher confirm ACK、NACK、confirm timeout 和 returned/unroutable message。
6. 只有收到 confirm ACK 且未 returned 后，才能把 outbox 标为 `PUBLISHED`；`DISPATCHED` 不能先于可靠确认代表成功。
7. 扫描器持续恢复 `PENDING`、NACK/timeout 和过期 `PUBLISHING`，使用相同 `message_id` 重投。
8. Adapter/下游按稳定 job/step/attempt/message ID 幂等，允许 confirm 后、DB 标记前崩溃造成的安全重复。

外部平台必须提供 commit、迁移、配置和以下真实 kill 测试证据：

- outbox 事务提交前/后 kill。
- publish 前 kill。
- broker 已接收、confirm 返回前 kill。
- confirm 已返回、outbox 标 PUBLISHED 前 kill。
- routing key 不存在、broker NACK、confirm timeout。

每种情况下最终都必须是“同一个稳定消息 ID 至少成功进入 RabbitMQ 一次，消费者只产生一次有效业务效果”，不能出现 DB 显示 DISPATCHED 但消息永远不存在。

### 8.3 RabbitMQ 目标集群实测

必须从目标集群导出不含 secret 的证据：

- queue、exchange、binding 均 durable，queue 非 auto-delete。
- Job 消息 `delivery_mode=2`/persistent。
- 没有短于最大排队时间加最大执行时间的 message/queue TTL。
- 无会提前删除 Job 的 max-length/overflow policy，或已证明容量足够。
- 当前 Adapter 没有 durable detach/reattach，故 `consumer_timeout >= 176400000ms`；`900000ms` 明确不通过。
- publisher confirms、returned、NACK、unroutable、redelivery 和 consumer channel close 有监控告警。

### 8.4 Redis/Celery、PostgreSQL 与存储

- Redis persistence、no-eviction、AOF/RDB 恢复和磁盘余量满足 30 本的 Celery 消息、结果、dispatch marker 和 stage lease；2 GiB 内存结论不能替代磁盘/AOF rewrite 余量实测。
- Celery Redis broker/backend 连接中断必须有限时失败，不能永久卡 socket。
- 两个 PostgreSQL DSN 必须分别统计理论池和 before/peak/after 实测；完整静态审计约为平台 DSN `314`、业务 DSN `405–420`，同实例合计 `719–734`，且仍未计函数内临时 Engine。负责人必须先收敛私有 Engine 或使用 PgBouncer，再按实测峰值与余量验收；`200`、`260–300` 和机械抬高到 `734+` 都不得作为“必然足够”的硬保证。
- PVC/OSS 的字节、inode、权限、吞吐和延迟满足第 4.9 节预算。
- 监控每阶段 active/reserved/queued、最老任务 age、Rabbit outbox age、DISPATCHED age、Celery execution age、失败率、lease、内部 API fan-out 和数据库连接。

8.1 与 8.2 的代码及隔离环境合同已完成；在新镜像尚未部署，或 8.3/8.4 尚未取得目标环境证据前，状态仍必须写为 BLOCKED。只能承诺 Gateway 已接纳和本地/隔离环境合同通过，不能把未验证的生产配置写成最终保证。

### 8.5 `creation-engine` 批量入口（平台侧已处理）

实际请求经过“浏览器 → `creation-engine` → `game-platform` → `api_gateway`”时，
三层必须连续透传同一个 `Idempotency-Key` 或 `batch_id`。平台侧现已在
`creation-engine/gateway/creation.py` 与 `game-platform/gateway/creation.py` 补齐该合同：

- 仅透传明确允许的幂等键，不透传外部伪造的服务令牌。
- `X-Service-Token` 和用户归属继续由各层可信鉴权逻辑生成。
- 空白幂等键不转发，未提供时保持旧客户端兼容。
- 上传代理与双层 Nginx/Ingress 的超时统一为 300 秒，body 上限统一为 80MiB。

七个业务服务负责人不需要修改这部分。整体验收仍须使用同一批文件和同一幂等键连续提交
两次，确认返回同一组 task ID；只有更换幂等键时才允许创建一组新任务。

## 九、最终判定标准

只有同时满足以下条件，才能说“30 本 × 50 万字、阶段并发 10 可以顺畅排队执行”：

- 第 8.1 节 common fencing 与第 8.2 节 transactional outbox 均已交付并通过真实故障测试。
- 目标 RabbitMQ `consumer_timeout >= 176400000ms`；不得用 900000ms 报告通过。
- 30 本批量请求成功写入正文和任务记录。
- 同一批次重放不产生第二套 task ID。
- outbox 证明每个 step 以稳定 message ID 被 RabbitMQ confirm，returned/NACK/timeout 均已处理。
- Adapter 证明每个 Rabbit Job 被可靠映射为稳定 Redis/Celery execution。
- 每阶段顶层 active 始终不超过 10。
- Story 的上传起名/摘要、Builder chord unlock 和其他共享队列任务已计入占槽与 RPM。
- 所有服务上报内部 fan-out、重试乘法、两个 DSN 的 DB 连接预算、RSS/显存、PVC/inode 预算。
- 正常 30 本真实压测全部 COMPLETED，不接受仅“全部终态”。
- 无无限 retry、无永久 PROCESSING、无假成功。
- 产物 oracle 对 30 个 task_id 全部通过，权威源、版本和下游引用一致，无 tmp/半成品被暴露。
- 第 6.4 节故障注入后能恢复或进入可解释失败态，旧 owner 不能覆盖新 owner。
- 每服务离线测试、真实小型 canary 和唯一一次端到端真实压测证据齐全。
- clean source context 构建的镜像包含所有运行时文件，记录 image digest，Worker 可启动并注册正确任务。
- 没有未解释的 BLOCKED 项；char_designer、scene_painter 的第二阶段修改均有负责人逐文件批准记录。
