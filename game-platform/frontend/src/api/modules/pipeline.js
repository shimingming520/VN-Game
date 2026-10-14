import request from "@/api/request";

/** 管线列表 */
export function getPipelinesApi() {
  return request.get("/v1/pipelines");
}

/**
 * 新建管线
 * @param {object} data - { name, display_name, impl_map, description }
 */
export function createPipelineApi(data) {
  return request.post("/v1/pipelines", data);
}

/**
 * 更新管线(部分字段)
 * @param {string} name - 管线名
 * @param {object} data - { display_name?, impl_map?, description? }
 */
export function updatePipelineApi(name, data) {
  return request.patch(`/v1/pipelines/${name}`, data);
}

/** 删除管线(预置不可删,被创作模式引用时也不可删) */
export function deletePipelineApi(name) {
  return request.delete(`/v1/pipelines/${name}`);
}

/** 三档创作模式 → 管线映射 */
export function getCreationModesApi() {
  return request.get("/v1/pipelines/creation-modes");
}

/**
 * 改某档创作模式指向的管线(仅超级管理员)
 * @param {string} mode - efficiency | balanced | quality
 * @param {string} pipeline_name - 管线名
 */
export function updateCreationModeApi(mode, pipeline_name) {
  return request.put(`/v1/pipelines/creation-modes/${mode}`, { pipeline_name });
}

/**
 * 已注册实现列表(注意:registry 路由前缀是 /api 不带 /v1)
 * @param {object} params - { capability?, enabled_only? }
 */
export function getImplsApi(params) {
  return request.get("/impls", { params });
}
