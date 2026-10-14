import request from "@/api/request";

/**
 * 获取用户列表
 * @param {object} params - 查询参数
 */
export function getUserListApi(params) {
  return request.get("/user/list", { params });
}
