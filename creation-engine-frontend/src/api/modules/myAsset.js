import request from "@/api/request";

/** 我的资产:按 task_id 分组的生产历史(角色/场景/音频/游戏包等全部 OSS 产物) */
export function getMyAssetsApi() {
  return request.get("/v1/my-assets");
}

/** 用户自建文件夹列表 */
export function getFoldersApi() {
  return request.get("/v1/folders");
}

/** 新建文件夹 */
export function createFolderApi(name) {
  return request.post("/v1/folders", { name });
}

/** 删除文件夹 */
export function deleteFolderApi(id) {
  return request.delete(`/v1/folders/${id}`);
}

/** 已保存到我的资产的素材;传 folderId 只看某文件夹 */
export function getSavedMaterialsApi(folderId) {
  const params = folderId != null ? { folder_id: folderId } : undefined;
  return request.get("/v1/saved-materials", { params });
}

/** 移动已保存素材到另一个文件夹(targetFolderId 为 null=未分类) */
export function moveSavedMaterialApi(id, targetFolderId) {
  return request.post(`/v1/saved-materials/${id}/move`, { target_folder_id: targetFolderId ?? null });
}

/** 复制已保存素材到另一个文件夹 */
export function copySavedMaterialApi(id, targetFolderId) {
  return request.post(`/v1/saved-materials/${id}/copy`, { target_folder_id: targetFolderId ?? null });
}

/** 从我的资产移除一条已保存素材 */
export function deleteSavedMaterialApi(id) {
  return request.delete(`/v1/saved-materials/${id}`);
}
