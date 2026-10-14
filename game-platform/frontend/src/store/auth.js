import { reactive } from 'vue'

const STORAGE_KEY = 'platform_token'
const USER_KEY = 'platform_user'
export const LOCAL_USER = Object.freeze({
  id: 0,
  main_site_uid: 'local-anonymous',
  username: '本地用户',
  email: '',
  role: 'admin',
})

/** 本地生产模式使用固定匿名身份，不依赖登录或远程账号。 */
export const authState = reactive({
  token: '',
  user: LOCAL_USER,
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
  authState.user = LOCAL_USER
  localStorage.removeItem(STORAGE_KEY)
  localStorage.removeItem(USER_KEY)
}

/** 兼容旧调用方；本地模式始终视为可用。 */
export function isLoggedIn() {
  return true
}

/** 本地模式允许管理管线和运行生产任务。 */
export function isAdmin() {
  return true
}
