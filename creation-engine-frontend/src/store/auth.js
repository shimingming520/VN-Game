import { reactive } from 'vue'

const STORAGE_KEY = 'engine_token'
const USER_KEY = 'engine_user'
const AUTH_RETURN_KEY = 'engine_auth_return_to'

/** 简单的全局登录态(localStorage 持久化,刷新不掉线) */
export const authState = reactive({
  token: localStorage.getItem(STORAGE_KEY) || '',
  user: JSON.parse(localStorage.getItem(USER_KEY) || 'null'),
})

/**
 * 登录成功后写入 token + 用户信息
 * @param {string} token - 平台 JWT
 * @param {object} user - 用户信息
 */
export function setAuth(token, user) {
  authState.token = token
  authState.user = user
  localStorage.setItem(STORAGE_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

/** 退出登录,清空本地态 */
export function clearAuth() {
  authState.token = ''
  authState.user = null
  localStorage.removeItem(STORAGE_KEY)
  localStorage.removeItem(USER_KEY)
}

/** 登录失效前记住当前站内页面，CMS 授权完成后回到原位置。 */
export function rememberAuthReturn(path = '') {
  const target = path || `${window.location.pathname}${window.location.search}${window.location.hash}`
  if (!target.startsWith('/') || target.startsWith('//')) return
  try {
    sessionStorage.setItem(AUTH_RETURN_KEY, target)
  } catch {
    // 浏览器禁用 sessionStorage 时回落到登录后的默认首页。
  }
}

/** 读取并一次性消费登录返回地址，拒绝站外跳转。 */
export function consumeAuthReturn() {
  try {
    const target = sessionStorage.getItem(AUTH_RETURN_KEY) || ''
    sessionStorage.removeItem(AUTH_RETURN_KEY)
    return target.startsWith('/') && !target.startsWith('//') ? target : '/home'
  } catch {
    return '/home'
  }
}

/** 是否已登录 */
export function isLoggedIn() {
  return !!authState.token
}
