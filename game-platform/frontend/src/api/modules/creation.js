import request from "@/api/request";

/**
 * 提交创作(简单入口):上传小说 + 选模式或管线,提交后一路跑到底
 * 中台转发给 novel-game-factory 的批量端点,用户身份由中台 JWT 决定
 * @param {FormData} formData - novels(多文件) + mode/pipeline_name + 基本配置
 */
export function createNovelsApi(formData, idempotencyKey = "") {
  const headers = {};
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  return request.post("/v1/creation/novels", formData, {
    headers,
    timeout: 300000,
  });
}

/**
 * 中台编排详情(每步用了哪个实现、失败原因)
 * 只有走中台的任务才有;老链路直发 Celery 的任务查不到,所以 silent 掉 404 不弹提示
 */
export function getPlatformTaskApi(taskId) {
  return request.get(`/v1/tasks/${taskId}`, { silent: true });
}

/**
 * 取消并从列表移除任务:中台标记 cancelled + 取消未下发的步骤,novel_tasks 软删除
 * 正在跑的那一步会跑完(worker 不会被强杀),之后不再往下派发
 */
export function deleteTaskApi(taskId) {
  return request.delete(`/v1/admin/tasks/${taskId}`);
}
