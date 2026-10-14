import request from "@/api/request";

/** 发起 OAuth2 授权:拿 CMS 登录页地址,前端自行 window.location 跳转 */
export function getAuthorizeUrlApi() {
  return request.get("/v1/auth/authorize-url");
}

/**
 * 登录:CMS 回调带回的 code+state 换平台 JWT
 * @param {string} code - CMS OAuth2 授权码
 * @param {string} state - 发起授权时后端生成的 state(回调原样带回)
 */
export function loginApi(code, state) {
  return request.post("/v1/auth/login", { code, state });
}

/** 获取当前登录用户信息 */
export function getMeApi() {
  return request.get("/v1/auth/me");
}
