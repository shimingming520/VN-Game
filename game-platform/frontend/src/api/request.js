import axios from "axios";
import { message } from "ant-design-vue";

/**
 * 创建 axios 实例
 * baseURL 优先读取环境变量，本地开发走 vite proxy 转发
 */
const request = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || "/api",
  // 中台不少接口需要访问 CMS、OSS 或 novel-game；15 秒会在下游仍正常处理时
  // 由浏览器提前中断。普通请求统一给 30 秒，发布等慢接口在各 API 中单独放宽。
  timeout: 30000,
  headers: { "Content-Type": "application/json" },
});

// ==================== 请求拦截器 ====================
request.interceptors.request.use(
  (config) => {
    // FormData 必须由浏览器生成带 boundary 的 Content-Type，不能沿用实例默认 JSON 头。
    if (typeof FormData !== "undefined" && config.data instanceof FormData) {
      delete config.headers["Content-Type"];
    }
    return config;
  },
  (error) => Promise.reject(error),
);

// ==================== 响应拦截器 ====================
request.interceptors.response.use(
  (response) => {
    const { code, msg, data } = response.data;

    /** 业务状态码约定：200 为成功，其余均为业务异常 */
    if (code === 200 || code === undefined) {
      return data ?? response.data;
    }

    message.error(msg || "操作失败");
    return Promise.reject(new Error(msg || "操作失败"));
  },
  (error) => {
    const status = error.response?.status;
    const isTimeout =
      error.code === "ECONNABORTED" ||
      error.code === "ETIMEDOUT" ||
      /timeout/i.test(error.message || "");

    /**
     * silent: 调用方自己处理错误,不弹全局提示。
     * 用于「查不到是正常情况」的探测型请求,例如老链路任务在中台没有编排详情,
     * 每轮轮询都弹一次 404 会把页面刷满。
     */
    if (error.config?.silent) return Promise.reject(error);

    const statusMap = {
      400: "请求参数错误",
      403: "暂无权限，请联系管理员",
      404: "请求地址不存在",
      500: "服务器内部错误",
    };

    /** 后端给了具体原因就用它:通用文案说不清是哪个参数、哪条管线的实现不可用 */
    const detail = error.response?.data?.detail;
    const msg = isTimeout
      ? "服务仍在处理中，请稍后重试"
      : (typeof detail === "string" && detail) ||
        statusMap[status] ||
        error.message ||
        "网络异常，请稍后重试";
    message.error(msg);

    return Promise.reject(error);
  },
);

export default request;
