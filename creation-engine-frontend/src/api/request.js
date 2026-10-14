import axios from "axios";
import { message } from "ant-design-vue";

/**
 * 创建 axios 实例
 * baseURL 优先读取环境变量，本地开发走 vite proxy 转发
 */
const request = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || "/api",
  timeout: 30000,
  headers: { "Content-Type": "application/json" },
});

// ==================== 请求拦截器 ====================
request.interceptors.request.use(
  (config) => {
    /** 上传剧本传的是 FormData。实例默认的 application/json 会顶掉 multipart 头，
     *  而 multipart 必须带 boundary 才能被后端解析出表单字段 —— 删掉这个头，
     *  让 axios 按 FormData 自己生成带 boundary 的 Content-Type。 */
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

    const statusMap = {
      400: "请求参数错误",
      403: "暂无权限，请联系管理员",
      404: "请求地址不存在",
      500: "服务器内部错误",
    };

    /** FastAPI 的错误正文是 { detail }，创作链路把下游 detail 原样透传上来。
     *  优先展示它：像「任务已提交生成」「摘要尚未生成」这类提示，
     *  换成按状态码套的泛化文案就没法指导用户下一步做什么了。 */
    const detail = error.response?.data?.detail;
    const detailMsg =
      typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : "";

    let msg = detailMsg || statusMap[status] || error.message || "网络异常，请稍后重试";
    if (isTimeout) msg = "服务仍在处理中，请稍后重试";
    if (status === 401) msg = "当前操作未被服务端接受，请检查服务配置后重试";

    /** skipErrorToast：调用方自己处理失败(探测性请求、带兜底的请求、轮询)。
     *  仍然 reject，只是不弹全局提示，避免把「已按预期回落」显示成故障。 */
    if (!error.config?.skipErrorToast) {
      /** 按内容去重：轮询失败会连续触发同一条提示 */
      message.error({ content: msg, key: `req-error-${status}-${msg}` });
    }

    return Promise.reject(error);
  },
);

export default request;
