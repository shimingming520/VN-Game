import request from "@/api/request";

/**
 * 批量翻译素材文件名 → 中文展示名
 * @param {string[]} names 原始文件名数组
 * @returns {Promise<Record<string,string>>} { 原名: 译名 },缺失的键需自行回落
 */
export function translateAssetNamesApi(names) {
  return request.post("/v1/translate/asset-names", { names });
}
