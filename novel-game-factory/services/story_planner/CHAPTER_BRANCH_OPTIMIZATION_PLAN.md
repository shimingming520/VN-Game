# Chapter Branch 优化实施顺序

## 分支与合并规则

- 开发分支：`feat/story-planner-gpt55-chapter-branch`
- `main` 只接收经过本地单测、代理实测和 chapter_branch 回归测试的代码。
- 每个行为修改遵循 RED → GREEN → REFACTOR：先添加能复现问题的失败测试，再修改生产代码。
- 未填写代理 API Key 前，只执行无密钥配置测试和静态检查，不合并 `main`。

## 阶段 0：统一 LLM 基线

1. 移除源码中的默认 API Key，并轮换历史泄漏凭证。
2. story_planner 所有 `LLMClient` 默认使用：
   - 模型：`gpt-5.6-sol`
   - 协议：OpenAI compatible
   - 本地中转：`https://ai.farmmx.com/v1`
   - K8s 中转：`http://172.16.30.31:32691/v1`
3. 提供 `.env.local.example` 和 `.env.k8s.example`，真实 `.env.local/.env.k8s` 不入库。
4. 运行时只读取 `STORY_PLANNER_LLM_API_KEY`、`STORY_PLANNER_LLM_BASE_URL`、
   `STORY_PLANNER_LLM_TIMEOUT`；忽略通用 `OPENAI_*`、历史 `LLM_*` 和官方服务 Key，
   模型与协议固定为 `gpt-5.6-sol` / `openai`。
5. 本地验证两条接口：
   - Responses API：确认中转和模型权限。
   - Chat Completions API：确认 story_planner 实际调用路径。

验收条件：配置单测通过；源码无默认密钥；填入 Key 后 GPT-5.6-SOL 返回有效文本。

## 阶段 1：P0 拓扑正确性

1. 修复 `_validate_topology()` 中 `warnings` 先使用后定义的问题。
2. 将 fallback 按类型拆分：
   - default
   - death_corridor
   - romance
   - fantasy
   - modular
3. 增加完整 DAG 校验：
   - `node_id` 唯一
   - 所有 `next` 指向存在节点
   - 只有一个根节点
   - 所有节点从根可达
   - 不存在循环
   - 原著章节完整且不重复
   - ending/bad_ending 必须是终止节点
   - branch variable 必须存在于 `variable_defs`
4. 拓扑排序失败时禁止继续写正文，必须重试或进入对应模式 fallback。
5. 统一保证原著末章为 `type=ending`。

验收条件：五种 branch_type 均有独立拓扑测试；无效 DAG 在任何 LLM 正文调用前被拒绝。

## 阶段 2：P0 落库一致性与幂等性

1. 检查每次 `save_story_md_to_db()` 的返回值，失败立即终止当前任务。
2. 检查 `save_story_json_to_db()` 返回值，JSON 未成功保存不得 `complete_stage()`。
3. 为生成任务增加发布前完整性检查：
   - JSON 中每个节点都有对应 MD
   - 每个 child_id 都存在
   - root_id 存在
   - 非结局空 children 需要明确允许或报错
4. 解决重跑残留：优先采用确定性节点 ID；必要时生成前清理旧 MD。
5. 避免每次写 MD/JSON 都重置数据库 sequence，改为迁移或单独运维修复。
6. 世界观、拓扑计划等非核心保存失败要明确记录降级状态，不能只打印日志。

验收条件：模拟任一节点写库失败时，任务状态必须失败；相同 task_id 重跑不产生孤儿 MD。

## 阶段 3：P1 玩家变量与软分支 effects

1. 对 LLM effects 做程序化白名单过滤，只保留 `variable_defs` 中定义的 key。
2. 将数值强制转换为整数并按每个变量的动态 cap 截断。
3. 校验 A/B 使用同一个主导变量并形成相反方向。
4. 禁止 A/B effects 完全相同，禁止所有值为 0。
5. 恋爱向校验选项点名角色与 `affection_*` 正值对象一致。
6. 校验阈值可达性，确保正常玩家路径可以触发目标结局。
7. 解决同一变量多个结局时顶层 `branch_thresholds` 只能保存一个阈值的问题。

验收条件：非法 key、超限值、假选择、不可达阈值均有自动测试。

## 阶段 4：P1 What-If 与结局质量门禁

1. 生成正文为空或低于最低长度时禁止落库。
2. 移除“故事在此延伸”等占位文案，失败时重试或明确失败。
3. 将一致性检查从“只报警”升级为：检查 → 定点修复 → 再检查。
4. 对角色末态进行结构校验：角色齐全、alive 类型正确、死亡不可逆、位置/伤情合法。
5. What-If 强制验证：
   - 起点兼容父节点末态
   - 过程不复制原著兄弟章
   - 终点兼容收束章
6. 结局强制验证核心人物生死、事件走向和情感基调至少两项区别于原著。
7. 修复 `NovelContext.character_secrets_text` 计算后未返回的问题。

验收条件：明显穿帮内容不能直接成为最终产物；修复失败时任务进入可诊断失败状态。

## 阶段 5：P1 断点恢复

1. 扩展 `chapter_branch_plan`，记录：
   - 拓扑版本
   - real_id_map
   - 每个节点的 pending/running/completed/failed 状态
   - terminal_states
   - 实际摘要和错误原因
2. 任务重启时读取计划，跳过已经完整生成并落库的节点。
3. 节点写入采用状态更新和正文写入的一致性事务边界。
4. 支持从单个 What-If/ending 节点重新生成，而不重跑整本书。

验收条件：人为中断中间节点后，重新运行能从失败节点继续，已完成节点内容不变。

## 阶段 6：P2 LLM 成本和性能

1. 接入现有 `ResultCache`，缓存世界观分段、章节摘要、末态和稳定校验结果。
2. 并行生成相互独立的章节摘要和世界观分段分析。
3. 末态扫描保持顺序，但减少重复传输完整世界观。
4. 将软分支重复使用的整章/全书上下文压缩为一次生成的 context package。
5. 合并 What-If 的状态快照与累计摘要调用。
6. 对同层、无依赖节点进行受控并行，限制并发和速率。
7. 按阶段记录 token、耗时、重试次数和缓存命中率。

验收条件：相同样本的 LLM 调用数和 token 消耗有可比较的前后统计，质量门禁不下降。

## 阶段 7：P2 可观测性和运维

1. 将进度细分为预分析、拓扑、末态、原著节点、What-If、结局、发布校验。
2. 日志统一携带 task_id、topo_id、real_id、branch_type 和 attempt。
3. 区分网络错误、认证错误、模型错误、JSON 错误、质量不达标和数据库错误。
4. 为长时间无进展设置节点级超时和明确失败原因。
5. 清理过期入口和文档：`start_novel_processing.py` 参数、manifest 输出说明、重复软分支常量。

## 阶段 8：完整测试与发布

1. 单元测试：配置、拓扑、effects、状态合并、节点 ID、发布完整性。
2. 组件测试：Mock LLM 固定响应，跑完整 chapter_branch 小样本。
3. 本地代理测试：GPT-5.6-SOL Responses API 和 Chat Completions API。
4. 本地真实小说冒烟测试：短篇，覆盖 default 和至少一种非 default 类型。
5. K8s 测试：使用集群中转 URL，确认网络、Secret 和模型权限。
6. 下游兼容测试：info_extractor 能解析软分支，game_builder 能读取变量和结局路由。
7. 整理变更和测试报告，人工确认后合并 `main`。
