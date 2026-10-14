# Vue Template

基于 Vue 3 与 Vite 的后台管理前端模板，内置基础布局、路由、接口请求封装，以及 Ant Design Vue 组件的自动按需引入。

## 技术栈

| 类别 | 技术 | 当前版本 | 用途 |
| --- | --- | --- | --- |
| 前端框架 | Vue | 3.5.40 | 使用 Composition API 和 `<script setup>` 编写页面与组件 |
| 构建工具 | Vite | 8.1.4 | 本地开发服务器与生产构建 |
| 路由 | Vue Router | 5.2.0 | 基于 History 模式的前端路由 |
| UI 组件库 | Ant Design Vue | 4.2.6 | 管理端界面组件 |
| HTTP 请求 | Axios | 1.18.1 | 接口请求、响应处理与异常提示 |
| 自动导入 | unplugin-auto-import | 21.0.0 | 自动导入 Vue、Vue Router API |
| 组件自动注册 | unplugin-vue-components | 32.1.0 | 自动按需引入 Ant Design Vue 组件 |
| 包管理器 | npm | 10.8.2 | 依赖安装与脚本执行 |

## 环境要求

- Node.js：`20.19+` 或 `22.12+`（Vite 8 的运行要求）
- npm：建议使用项目已验证的 npm 10+

可通过以下命令确认本地版本：

```bash
node --version
npm --version
```

## 快速启动

在项目根目录执行：

```bash
# 1. 安装依赖
npm install

# 2. 启动本地开发服务
npm run dev
```

服务默认运行在 `http://localhost:5173`，启动后会自动打开浏览器。修改源码后，Vite 会自动热更新页面。

## 常用命令

```bash
# 启动开发服务
npm run dev

# 构建生产产物，输出至 dist/
npm run build

# 本地预览生产构建产物（需先执行 npm run build）
npm run preview
```

## 接口与环境配置

接口请求由 `src/api/request.js` 统一封装，默认超时为 15 秒，并提供业务异常与网络异常提示。

| 环境 | 配置文件 | `VITE_API_BASE_URL` | 说明 |
| --- | --- | --- | --- |
| 开发 | `.env.development` | `/api` | Vite 将 `/api` 转发到 `http://localhost:3000`，并移除 `/api` 前缀 |
| 生产 | `.env.production` | `https://api.example.com` | 请替换为实际生产 API 地址 |

开发联调时，后端服务需默认运行在 `http://localhost:3000`。若实际地址不同，请同步修改 `vite.config.js` 中的 `server.proxy['/api'].target`。

## 项目结构

```text
.
├── public/                 # 静态资源
├── src/
│   ├── api/                # Axios 实例与接口模块
│   ├── assets/             # 图片和全局样式
│   ├── components/         # 通用组件
│   ├── layout/             # 页面布局及头部、侧边栏
│   ├── router/             # 路由配置
│   ├── utils/              # 工具函数
│   ├── views/              # 页面模块（首页、测试页、404 等）
│   ├── App.vue             # 根组件
│   └── main.js             # 应用入口
├── .env.development        # 开发环境变量
├── .env.production         # 生产环境变量
├── vite.config.js          # Vite、别名、自动导入和代理配置
└── package.json            # 依赖和 npm 脚本
```

## 开发约定

- 使用 `@` 作为 `src` 目录别名，例如 `@/api/request`。
- Vue 与 Vue Router 常用 API 已自动导入；Ant Design Vue 组件支持按需自动引入。
- 路由采用懒加载；未匹配的地址会重定向至 `/404`。
- 新增接口请放在 `src/api/modules/` 中，并以 `Api` 作为异步接口函数后缀。
