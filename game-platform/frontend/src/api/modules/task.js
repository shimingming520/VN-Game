import request from "@/api/request";

export function getMyTaskListApi(params) {
  return request.get("/v1/admin/tasks/mine", { params });
}

/**
 * 跨组任务列表(直查平台库 novel_tasks)
 * 支持管线名/项目名/任务ID模糊查询 + 状态精确筛选 + 更新时间范围
 * @param {object} params - { group_name, project_name, task_id, overall_status, start_time, end_time, page, page_size }
 */
export function getTaskListApi(params) {
  return request.get("/v1/admin/tasks", { params });
}

/**
 * 按原 task_id 重新启动指定范围的模块。
 * mode: failed_only | running_and_failed
 */
export function restartTaskApi(taskId, mode = 'running_and_failed') {
  return request.post(
    `/v1/admin/tasks/${encodeURIComponent(taskId)}/restart`,
    { mode },
    {
      // 重启接口需要完成状态收敛和重新下发，单独给足时间。
      timeout: 60000,
      // 由任务页展示更明确的失败提示，避免和全局拦截器重复弹窗。
      silent: true,
    },
  );
}

/**
 * 沿用原 task_id，只运行一个指定模块；无论该模块当前状态如何，后端都会
 * 开启新的 attempt，模块自身继续复用已有断点/产物。
 */
export function runTaskStageApi(taskId, capability) {
  return request.post(
    `/v1/admin/tasks/${encodeURIComponent(taskId)}/stages/${encodeURIComponent(capability)}/run`,
    undefined,
    { timeout: 60000, silent: true },
  )
}
