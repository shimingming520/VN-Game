import request from "@/api/request";

const PUBLISH_FORM_TIMEOUT = 180000;
const PUBLISH_SUBMIT_TIMEOUT = 180000;
const BATCH_PUBLISH_SUBMIT_TIMEOUT = 320000;
const PROJECT_PROGRESS_STORAGE_KEY = "creation_engine_project_progress";

const projectProgressHistory = new Map();

function restoreProjectProgressHistory() {
  if (typeof sessionStorage === "undefined") return;
  try {
    const stored = JSON.parse(sessionStorage.getItem(PROJECT_PROGRESS_STORAGE_KEY) || "{}");
    for (const [taskId, progress] of Object.entries(stored)) {
      projectProgressHistory.set(taskId, Number(progress) || 0);
    }
  } catch {
    sessionStorage.removeItem(PROJECT_PROGRESS_STORAGE_KEY);
  }
}

function persistProjectProgressHistory() {
  if (typeof sessionStorage === "undefined") return;
  sessionStorage.setItem(
    PROJECT_PROGRESS_STORAGE_KEY,
    JSON.stringify(Object.fromEntries(projectProgressHistory)),
  );
}

function stabilizeProjectProgress(response) {
  const items = Array.isArray(response?.items) ? response.items : [];
  let changed = false;
  const stableItems = items.map((item) => {
    const current = Math.max(0, Math.min(100, Number(item?.progress) || 0));
    const previous = projectProgressHistory.get(item.task_id) || 0;
    const stable = Math.max(previous, current);
    if (stable !== previous) {
      projectProgressHistory.set(item.task_id, stable);
      changed = true;
    }
    return stable === current ? item : { ...item, progress: stable };
  });
  if (changed) persistProjectProgressHistory();
  return { ...response, items: stableItems };
}

restoreProjectProgressHistory();

/** 我的项目列表:按最近更新倒序分页,返回 { items, total, limit, offset } */
export async function getProjectsApi({ limit = 20, offset = 0 } = {}) {
  const response = await request.get("/v1/tasks", { params: { limit, offset } });
  const data = response?.data ?? response ?? {};
  const tasks = Array.isArray(data.tasks) ? data.tasks : [];
  const items = tasks.map((task) => ({
    ...task,
    name: task.project_name || task.novel_filename || "未命名作品",
    cover: task.cover || "",
    progress: Number(task.progress_percentage) || 0,
    is_published: Boolean(task.is_published),
  }));
  return stabilizeProjectProgress({
    items,
    total: Number(data.total) || 0,
    limit,
    offset,
  });
}

/** 重命名项目(1-20 字,同一用户内不允许重名) */
export function renameProjectApi(taskId, name) {
  return Promise.reject(
    new Error("本地模式暂未提供项目重命名接口"),
  );
}

/** 删除项目(后端软删除,进回收站,不动 OSS 产物) */
export function deleteProjectApi(taskId) {
  return request.delete(`/v1/tasks/${taskId}`);
}

/**
 * 发布单个作品到 CMS。走中台已有的发布流程(章节结构/封面/游戏链接都由中台组装)。
 * @param {object} payload - { name?, description?, class_id?, tags?, cover?, is_recommend? }
 */
export function publishProjectApi(taskId, payload = {}) {
  return Promise.reject(
    new Error("本地模式暂未配置 CMS 发布接口"),
  );
}

/**
 * 批量发布:后端一次请求走 CMS 原生 batch_set(不是循环单发布)。
 * 每一项都要带全套表单字段 —— 批量前端逐项过表单就是为了凑出这个数组。
 * @param {Array<object>} items - [{ task_id, name, description, cover, class_id,
 *                                   tags, chapters, is_recommend }]
 * 返回 { code, msg, total_count, success_count, failed_count, results, skipped },
 * results/skipped 里带 task_id 与失败原因,便于逐项提示。
 */
export function batchPublishProjects(items = []) {
  return Promise.reject(
    new Error("本地模式暂未配置 CMS 批量发布接口"),
  );
}

/** 发布信息预填(作品名/自动封面/章节结构/游戏链接),中台 publish-info 的代理 */
export function getPublishInfoApi(taskId, config = {}) {
  return Promise.reject(
    new Error("本地模式暂未配置 CMS 发布信息接口"),
  );
}

/** CMS 标签清单 */
export function getCmsTagsApi(config = {}) {
  return Promise.reject(
    new Error("本地模式暂未配置 CMS 标签接口"),
  );
}

/** CMS 分类树(一级不可选,用 a-cascader 展示) */
export function getCmsClassesApi(config = {}) {
  return Promise.reject(
    new Error("本地模式暂未配置 CMS 分类接口"),
  );
}

/** 新建 CMS 标签,返回 { id, name } */
export function createCmsTagApi(payload) {
  return Promise.reject(
    new Error("本地模式暂未配置 CMS 标签接口"),
  );
}

/** 上传发布封面到 OSS,返回 { url } */
export function uploadCoverApi(formData) {
  return Promise.reject(
    new Error("本地模式暂未配置发布封面接口"),
  );
}
