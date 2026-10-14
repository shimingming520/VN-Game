import request from "@/api/request";

const CMS_READ_TIMEOUT = 60000;

/** 视觉小说:拉 CMS 游戏列表(后端代理) */
export function getCmsGamesApi(params) {
  return request.get("/v1/admin/games/cms-list", {
    params,
    timeout: CMS_READ_TIMEOUT,
  });
}

/** 创作推广:平台库游戏列表 */
export function getPromoGamesApi(params) {
  return request.get("/v1/admin/promo-games", { params });
}

/** 创作推广:上传封面到 OSS,返回 { cover_url } */
export function uploadPromoCoverApi(file) {
  const fd = new FormData();
  fd.append("file", file);
  return request.post("/v1/admin/promo-games/cover", fd, {
    headers: { "Content-Type": "multipart/form-data" },
  });
}

/** 创作推广:新建游戏 */
export function createPromoGameApi(data) {
  return request.post("/v1/admin/promo-games", data);
}

/** 创作推广:删除游戏 */
export function deletePromoGameApi(id) {
  return request.delete(`/v1/admin/promo-games/${id}`);
}

/** 创作推广:已分发的 game_id 列表 */
export function getDistributedIdsApi() {
  return request.get("/v1/admin/promo-games/distributed");
}

/** 创作推广:批量分发到创作引擎游戏库 */
export function distributePromoGamesApi(gameIds) {
  return request.post("/v1/admin/promo-games/distribute", { game_ids: gameIds });
}

/** 创作推广:批量撤销分发 */
export function recallPromoGamesApi(gameIds) {
  return request.post("/v1/admin/promo-games/recall", { game_ids: gameIds });
}
