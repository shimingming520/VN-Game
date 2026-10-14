import request from "@/api/request";

const CMS_READ_TIMEOUT = 60000;
const CMS_PUBLISH_TIMEOUT = 180000;

/** 发布弹窗预填数据(解析 story_tree.json) */
export function getPublishInfoApi(taskId) {
  return request.get(`/v1/admin/tasks/${taskId}/publish-info`, {
    timeout: CMS_READ_TIMEOUT,
  });
}

/** 发布到 CMS */
export function publishCmsApi(taskId, data) {
  return request.post(`/v1/admin/tasks/${taskId}/publish-cms`, data, {
    timeout: CMS_PUBLISH_TIMEOUT,
  });
}

/** CMS 标签列表(后端带 token 代理) */
export function getCmsTagsApi() {
  return request.get("/v1/admin/cms/tags", { timeout: CMS_READ_TIMEOUT });
}

/** CMS 游戏分类列表(后端带 token 代理,已转 cascader options) */
export function getCmsClassesApi() {
  return request.get("/v1/admin/cms/classes", { timeout: CMS_READ_TIMEOUT });
}
