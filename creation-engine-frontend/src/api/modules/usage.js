import request from "@/api/request";

/** 当前用户全部作品的 token 用量累计(账户面板 Token 账单总量) */
export function getMyUsageSummaryApi() {
  return request.get("/v1/usage/mine/summary");
}

/** 当前用户用量涉及的模型去重列表(账单详情模型下拉) */
export function getMyUsageModelsApi() {
  return request.get("/v1/usage/mine/models");
}

/**
 * 当前用户全部作品的用量明细(账单详情)
 * @param {object} params - { api_type, model, start_time, end_time, page, page_size }
 */
export function getMyUsageDetailApi(params) {
  return request.get("/v1/usage/mine/detail", { params });
}
