import request from "@/api/request";

/**
 * 创作接口。当前本地 API 网关直接提供 /api/v1 下的创作路由，
 * 不再经过旧的 /api/v1/creation 代理前缀。
 */

const BASE = "/v1";
const INTERACTIVE_READ_TIMEOUT = 60000;
const INTERACTIVE_WRITE_TIMEOUT = 180000;
const REGENERATE_STATUS_TIMEOUT = 30000;

// ==================== 运行时配置 ====================

/**
 * 前端需要的运行时配置,目前只有 { game_base_url }。
 * 章节 iframe 地址靠它拼:域名必须由后端下发,不能写死在前端。
 */
export function getRuntimeConfigApi(config) {
  // 本地游戏服务固定暴露在 8088，生产环境可由真实网关覆盖此模块。
  return Promise.resolve({ game_base_url: "http://localhost:8088" });
}

// ==================== 模式与模型清单 ====================

/** 创作模式清单(效率/均衡/高质量)。返回 [{ mode, label, available }] */
export function getCreationModesApi() {
  return Promise.resolve([
    {
      mode: "default",
      label: "标准模式",
      available: true,
    },
  ]);
}

/**
 * 全量模型清单,返回 { text_model:{default,options}, image_model, audio_model, resolution }。
 * 上传页据此给出可选模型 —— 选项必须来自后端,写死的清单和后端校验一旦不一致就是 400。
 */
export function getModelOptionsApi(config) {
  return request.get(`${BASE}/models/options`, config);
}

/** 重新生图可选图像模型,返回 { default, options } */
export function getImageModelOptionsApi() {
  return request.get(`${BASE}/models/image-options`);
}

// ==================== 第 1 步:上传与信息确认 ====================

/**
 * 单文件上传剧本(仅落库 + 触发摘要/起名,不启动生成)。
 * 返回 { task_id, project_name, novel_filename }
 * @param {FormData} formData - novel(File) + mode/art_style/text_model/image_model/audio_model/resolution
 */
export function uploadStoryApi(formData) {
  return request.post(`${BASE}/story/upload`, formData, {
    // 不显式写 Content-Type:multipart 必须带 boundary,手写会丢掉它导致后端解析不出表单。
    // 交给 axios 按 FormData 自动生成(实例默认的 application/json 已在拦截器里为 FormData 清掉)。
    // 上传大文件比普通请求慢得多,沿用实例默认的 15s 会在正常文件上误报超时
    timeout: 120000,
  });
}

/**
 * 批量提交(最多 50 本),提交后按管线一路跑到底,无人工确认卡点。
 * @param {FormData} formData - novels(多个 File) + mode/art_style/story_mode/output_language 等
 */
export function uploadNovelsBatchApi(formData, idempotencyKey = "") {
  const headers = {};
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  return request.post(`${BASE}/novels/batch`, formData, {
    headers,
    timeout: 300000,
  });
}

/** 创建相似品：读取源作品可复用的模型、风格、玩法、剧情模式和管线配置。 */
export function getSimilarConfigApi(sourceTaskId, config = {}) {
  return request.get(`${BASE}/story/similar-config`, {
    params: { source_task_id: sourceTaskId },
    ...config,
  });
}

/** 创建相似品：下载源作品原始 TXT，前端会把 Blob 还原成 File 放回上传窗口。 */
export function getRawNovelApi(taskId, config = {}) {
  return request.get(`${BASE}/story/${taskId}/raw-novel`, {
    responseType: "blob",
    timeout: 60000,
    ...config,
  });
}

/**
 * 轮询摘要状态。
 * 返回 { task_id, status: pending|ready|failed, outline?, background?, one_liner?, error? }
 */
export function getStorySummaryApi(taskId, config) {
  return request.get(`${BASE}/story/${taskId}/summary`, config);
}

/** 重新提炼摘要(异步,提交后继续轮询 getStorySummaryApi) */
export function regenerateStorySummaryApi(taskId) {
  return request.post(`${BASE}/story/${taskId}/summary/regenerate`);
}

/**
 * 保存输出约束配置。字段取值由后端强校验,非法值返回 400。
 * @param {object} payload - { output_language, art_style, story_mode, gameplay,
 *                             text_model, image_model, audio_model, resolution }
 */
export function saveStoryConfigApi(taskId, payload) {
  return request.post(`${BASE}/story/${taskId}/config`, payload);
}

/** 保存多剧情改写的受众、视角、题材与情节约束。 */
export function saveStoryAdaptationApi(taskId, payload) {
  return request.post(`${BASE}/story/${taskId}/adaptation`, payload);
}

/** 查询大纲/世界观状态(仅 multi 模式有内容),返回 { status: pending|ready|confirmed } */
export function getStoryOutlineApi(taskId) {
  return request.get(`${BASE}/story/${taskId}/outline`);
}

/** 用 LLM 从摘要生成作品名,返回 { project_name } */
export function generateProjectNameApi(taskId) {
  return request.post(`${BASE}/story/${taskId}/generate-name`);
}

/**
 * 信息确认并提交生成(此步之后任务真正开跑)。
 * 已提交过的任务再调会返回 400「任务已提交生成」。
 * @param {object} payload - { project_name, outline?, source_task_id? }
 */
export function confirmStoryApi(taskId, payload) {
  return request.post(`${BASE}/story/${taskId}/confirm`, payload);
}

// ==================== 任务状态与恢复 ====================

/**
 * 任务详细状态。stages 为各阶段中文描述,overall_status 也是中文描述,
 * 需要状态码请用 getTaskEntryApi 的 overall_status_code。
 */
export function getTaskStatusApi(taskId, config) {
  return request.get(`${BASE}/tasks/${taskId}/status`, config);
}

/**
 * 作品「进入接口」:后端算好该跳到哪一步,前端据此恢复现场。
 * 返回 { task_id, stage, overall_status_code, progress_percentage,
 *        info_confirm_stage, is_published, error_message }
 * stage: create|config|confirm|material|performance|publish。
 * info_confirm_stage=performance 时直接进入导出与发布；游戏界面由
 * story_tree.json.UI_mode(Ancient/Other) 自动决定。
 */
export function getTaskEntryApi(taskId, config) {
  return request.get(`${BASE}/tasks/${taskId}/entry`, config);
}

/** 任务的生成参数(3 模型 + 分辨率 + 视觉风格),用于信息确认页回显 */
export function getTaskGenerationParamsApi(taskId) {
  return request.get(`${BASE}/tasks/${taskId}/generation-params`);
}

/** 四项任务数量统计 */
export function getTaskStatusCountsApi(params) {
  return request.get(`${BASE}/tasks/status-counts`, { params });
}

// ==================== 第 2 步:角色与场景素材 ====================

/** 人物场景确认完成,推进到 material 阶段(幂等) */
export function confirmMaterialApi(taskId) {
  return request.post(`${BASE}/tasks/${taskId}/confirm-material`);
}

/**
 * 角色立绘清单。清单以 OSS 运行时路径为准,只列玩家真能看到的那些图,
 * 提示词由 character_prompts 表补上。
 * 返回 { task_id, status: ready|pending, total,
 *        characters: [{ speaker_id, name, items: [{ node_id, material_id,
 *          combination, emotion, shot, prompt, url }] }] }
 * items[].material_id 不含后缀,可直接传给 regenerateCharacterApi。
 * @param {object} params - { node_id? } 只看某章节时传
 */
export function getTaskCharactersApi(taskId, params = {}, config = {}) {
  return request.get(`${BASE}/tasks/${taskId}/characters`, {
    params,
    ...config,
  });
}

/**
 * 场景图 + 章节封面清单。两类素材的重生成入口不同,用 material_type 区分:
 * scene 传 scene_ids,background 传 background_node_ids。
 * 返回 { task_id, status, total, scenes: [...], backgrounds: [...] }
 * @param {object} params - { node_id? }
 */
export function getTaskScenesApi(taskId, params = {}, config = {}) {
  return request.get(`${BASE}/tasks/${taskId}/scenes`, { params, ...config });
}

/** 角色素材重生成,返回 { regenerate_id } 供轮询 */
export function regenerateCharacterApi(payload, config = {}) {
  return request.post(`${BASE}/materials/char_designer/regenerate`, payload, {
    timeout: INTERACTIVE_WRITE_TIMEOUT,
    ...config,
  });
}

/** 场景素材重生成,返回 { regenerate_id } 供轮询 */
export function regenerateSceneApi(payload, config = {}) {
  return request.post(`${BASE}/materials/scene_painter/regenerate`, payload, {
    timeout: INTERACTIVE_WRITE_TIMEOUT,
    ...config,
  });
}

/** 查询重生成进度 */
export function getRegenerateStatusApi(regenerateId, config) {
  return request.get(`${BASE}/materials/regenerate/${regenerateId}/status`, {
    timeout: REGENERATE_STATUS_TIMEOUT,
    ...config,
  });
}

/**
 * 素材重生成历史(版本管理)。
 * @param {object} params - { node_id?, material_id? },空值不传
 */
export function getRegenHistoryApi(taskId, params = {}) {
  return request.get(`${BASE}/materials/${taskId}/regen/history`, { params });
}

/** 激活指定版本 */
export function activateRegenVersionApi(taskId, payload) {
  return request.post(`${BASE}/materials/${taskId}/regen/activate`, payload);
}

/** 回滚到上一版本 */
export function rollbackRegenVersionApi(taskId, payload) {
  return request.post(`${BASE}/materials/${taskId}/regen/rollback`, payload);
}

/** 推荐音色 */
export function getVoiceRecommendApi(params) {
  return request.get(`${BASE}/materials/audio_producer/voice/recommend`, {
    params,
  });
}

/** 按需生成并缓存本地试听音频 */
export function generateVoiceAuditionApi(payload) {
  return request.post(
    `${BASE}/materials/audio_producer/voice/audition`,
    payload,
    { timeout: 120000 },
  );
}

/** 角色级换音色(联动重生成) */
export function regenerateVoiceApi(payload) {
  return request.post(
    `${BASE}/materials/audio_producer/voice/regenerate`,
    payload,
  );
}

// ==================== 第 3 步:剧情演绎 ====================

/**
 * 故事树节点列表。故事树未生成时返回 404。
 * 返回 { task_id, nodes: [{ node_id, title, depth, parent_ids, is_branch,
 *        is_original, summary, children }], variable_defs, player_variables,
 *        branch_thresholds }
 */
export function getStoryTreeApi(taskId, config) {
  return request.get(`${BASE}/story/${taskId}/tree`, config);
}

/**
 * 某章节按场景聚合的 beat 分段。
 * 返回 { task_id, node_id, segments: [{ scene_id, beats: string[] }] }
 */
export function getPerformanceScenesApi(taskId, nodeId, config) {
  return request.get(`${BASE}/tasks/${taskId}/performance/scenes`, {
    params: { node_id: nodeId },
    timeout: INTERACTIVE_READ_TIMEOUT,
    ...config,
  });
}

/**
 * 单个 beat 的演出状态。beat_id 跨章节不唯一,必须配合 node_id。
 * 返回 { beat_id, scene_id, scene_shot_size, speaker_id, cg_mode, characters }
 */
export function getPerformanceBeatApi(taskId, nodeId, beatId, config = {}) {
  return request.get(`${BASE}/tasks/${taskId}/performance/beat`, {
    params: { node_id: nodeId, beat_id: beatId },
    timeout: INTERACTIVE_READ_TIMEOUT,
    ...config,
  });
}

/**
 * 更新 beat 所在连续场景段的景别。作用域是整段而非单个 beat。
 * @param {object} payload - { node_id, beat_id, scene_shot_size: LS|MS|CS }
 */
export function updateSceneShotApi(taskId, payload, config = {}) {
  return request.patch(
    `${BASE}/tasks/${taskId}/performance/scene-shot`,
    payload,
    {
      timeout: INTERACTIVE_WRITE_TIMEOUT,
      ...config,
    },
  );
}

/** 更新指定 beat 的角色站位(立绘热更新) */
export function updateGameCastApi(payload, config = {}) {
  return request.patch(`${BASE}/game/cast`, payload, {
    timeout: INTERACTIVE_WRITE_TIMEOUT,
    ...config,
  });
}

/** 演绎微调完成,推进到 performance 阶段(幂等) */
export function confirmPerformanceApi(taskId) {
  return request.post(`${BASE}/tasks/${taskId}/confirm-performance`);
}

// ==================== 自动游戏界面构建（兼容接口）/ 第 4 步:导出 ====================

/**
 * 兼容旧流程：选择 UI 预设并重新生成在线包。当前创作流程不再调用此接口，
 * 正常游戏界面由 story_tree.json.UI_mode 自动决定。
 * @param {object} payload - { ui_preset: 1-5 }
 * 返回 { build_id, task_id, status: processing }
 */
export function buildOnlinePackageApi(taskId, payload) {
  return request.post(`${BASE}/tasks/${taskId}/build`, payload);
}

/** 导出单机包,返回 { build_id, task_id, status } */
export function exportOfflinePackageApi(taskId) {
  return request.post(`${BASE}/tasks/${taskId}/export`);
}

/** 构建/导出进度(两者共用),返回 { status: processing|success|failed, output_dir, error } */
export function getBuildStatusApi(taskId, buildId, config) {
  return request.get(`${BASE}/tasks/${taskId}/build/${buildId}/status`, config);
}
