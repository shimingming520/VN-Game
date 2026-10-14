import axios from "axios";
import { message } from "ant-design-vue";

/**
 * 创建 axios 实例
 * baseURL 优先读取环境变量，本地开发走 vite proxy 转发
 */
const request = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || "/api",
  timeout: 15000,
  headers: { "Content-Type": "application/json" },
});

// ==================== 请求拦截器 ====================
request.interceptors.request.use(
  (config) => config,
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

    const statusMap = {
      400: "请求参数错误",
      403: "暂无权限，请联系管理员",
      404: "请求地址不存在",
      500: "服务器内部错误",
    };

    const msg = statusMap[status] || error.message || "网络异常，请稍后重试";
    message.error(msg);

    return Promise.reject(error);
  },
);

export default request;
