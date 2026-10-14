import request from "@/api/request";

/**
 * 素材列表(后端代理列 OSS,分类见 gateway/assets.py CATEGORY_PREFIX)
 * @param {object} params - { category: character|scene|music|sound, name?, start_time?, end_time?, page, page_size }
 */
export function getAssetsApi(params) {
  return request.get("/v1/assets", { params });
}

/** 素材下载地址(代理端点,强制 attachment;直接 window.open 或 <a href> 用) */
export function assetDownloadUrl(key) {
  const base = request.defaults.baseURL || "/api";
  return `${base}/v1/assets/download?key=${encodeURIComponent(key)}`;
}

/** 软删除:仅在当前用户的素材列表隐藏该素材(不删 OSS) */
export function hideAssetApi(key) {
  return request.post("/v1/assets/hide", { key });
}

/** 保存到我的资产(folderId 为空=未分类) */
export function saveAssetApi(key, folderId) {
  return request.post("/v1/assets/save", { key, folder_id: folderId ?? null });
}
