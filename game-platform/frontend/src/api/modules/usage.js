import request from "@/api/request";

/**
 * token 用量明细列表(直查平台库 api_usage)
 * 支持管线名/任务ID/服务/模型模糊查询 + api_type精确筛选 + 时间范围
 * @param {object} params - { group_name, task_id, service, model, api_type, start_time, end_time, page, page_size }
 */
export function getUsageListApi(params) {
  return request.get("/v1/admin/usage", { params });
}

/**
 * 按管线聚合 token 用量(直查 api_usage,SQL group by)
 * @param {object} params - { group, start_time, end_time }
 */
export function getUsageSummaryApi(params) {
  return request.get("/v1/usage/summary", { params });
}

/**
 * 单组用量分布(按 service+api_type 聚合,组详情图表用)
 * @param {string} groupName - 管线名
 * @param {object} params - { start_time, end_time }
 */
export function getUsageGroupDetailApi(groupName, params) {
  return request.get(`/v1/usage/summary/${groupName}`, { params });
}
