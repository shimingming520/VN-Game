import request from "@/api/request";

/**
 * 素材列表(后端代理列出本地 storage 或 OSS,分类见 gateway/assets.py CATEGORY_PREFIX)
 * @param {object} params - { category: character|scene|bgm|sfx, name?, start_time?, end_time?, page, page_size }
 */
export function getAssetsApi(params) {
  return request.get("/v1/assets", { params });
}

/** 素材下载地址(代理端点,强制 attachment;直接 window.open 或 <a href> 用) */
export function assetDownloadUrl(key) {
  const base = request.defaults.baseURL || "/api";
  return `${base}/v1/assets/download?key=${encodeURIComponent(key)}`;
}

/**
 * 按分类+时间范围清理本地素材或 OSS 素材
 * @param {object} data - { category, start_time?, end_time?, dry_run } dry_run=true 只预览数量
 */
export function cleanAssetsApi(data) {
  return request.post("/v1/assets/clean", data);
}
