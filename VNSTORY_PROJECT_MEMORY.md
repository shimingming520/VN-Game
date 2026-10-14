# VNStory 项目记忆文档

更新时间：2026-10-11  
项目类型：AI 小说转视觉小说游戏生产平台  
当前运行方式：Windows 本机 Docker Compose  
当前目标：不登录、不访问 Harbor、不使用 OSS，完成“小说上传 -> AI 剧情 -> 人物/场景图片 -> 视觉小说游戏预览”流程。

## 1. 项目结构

工作区根目录：

```text
F:\PyCharm_Project\VNStory
```

主要模块：

| 模块 | 路径 | 作用 |
|---|---|---|
| 创作前端 | `creation-engine-frontend` | Vue/Vite 页面、小说上传、项目列表、游戏仓库 |
| 小说生产后端 | `novel-game-factory` | API Gateway、Celery 生产服务、游戏构建服务 |
| 游戏平台代码 | `game-platform` | 原平台编排相关代码，当前本地视觉生产链不依赖其远程编排 |
| 旧创作网关 | `creation-engine` | 旧的创作引擎接口，当前 Docker 本地启动主链路不使用 |

当前本地启动主链路只需要：

```text
creation-engine-frontend
        |
        | HTTP /api
        v
novel-game-factory/api_gateway
        |
        +--> Redis/Celery
        +--> story_planner
        +--> info_extractor
        +--> char_designer
        +--> scene_painter
        +--> game_builder
        |
        +--> 本地 PostgreSQL: host.docker.internal:15432
        +--> 本地 storage 目录

game_server
        |
        +--> storage/built_games
```

## 2. 当前运行约束

这些约束是当前本地版本的设计前提：

- 不访问 Harbor。
- Docker 镜像直接拉取或构建到本机 Docker。
- 不使用远程 OSS 作为文件存储。
- 文件统一保存在 `F:\PyCharm\_Project\VNStory\storage`。
- 不需要登录验证，前端路由默认允许直接进入首页和创作流程。
- 当前重点是视觉小说生产。
- 音频阶段可以跳过，当前本地视觉模式不要求生成音频。
- 数据库仍用于任务元数据、阶段状态和生产过程数据；“本地存储”并不等于完全移除数据库。
- 当前游戏仓库只展示本地已经完成并存在游戏包的作品。

项目内另有一套中台前端：

```text
F:\PyCharm_Project\VNStory\game-platform\frontend
```

它用于任务日志、视觉小说管线管理、创作测试、素材库和平台游戏管理。
当前本地开发端口为 `5175`，中台后端使用 `8001`，已关闭登录验证，
访问 `/login` 会直接跳转到 `/tasks`，默认使用 `local-anonymous` 本地身份。

注意：当前系统依赖本机 PostgreSQL 的 `agent_platform` 数据库。Docker Compose 会通过：

```text
DB_HOST=host.docker.internal
DB_PORT=15432
DB_NAME=agent_platform
DB_USER=platform
DB_PASSWORD=platform
```

连接数据库。数据库服务不是由当前 Compose 文件创建的，启动前必须确认本机数据库可访问。

## 3. Docker 启动方式

进入生产服务目录：

```powershell
cd F:\PyCharm_Project\VNStory\novel-game-factory
```

启动本地模式：

```powershell
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d
```

查看服务状态：

```powershell
docker compose -f docker-compose.yml -f docker-compose.local.yml ps
```

停止服务：

```powershell
docker compose -f docker-compose.yml -f docker-compose.local.yml down
```

只重建前端：

```powershell
docker compose -f docker-compose.yml -f docker-compose.local.yml build creation_frontend
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --no-deps creation_frontend
```

重启 API Gateway：

```powershell
docker compose -f docker-compose.yml -f docker-compose.local.yml restart api_gateway
```

重启后不要立即判断失败。API Gateway 重新初始化期间可能短暂返回 502，先等待健康检查恢复：

```powershell
curl.exe -sS http://localhost:8000/health
```

预期结果：

```json
{"status":"healthy"}
```

## 4. 服务地址和端口

| 服务 | 地址 | 用途 |
|---|---|---|
| 创作前端 | `http://localhost:5174` | 使用平台 |
| API Gateway | `http://localhost:8000` | 后端 API |
| API 健康检查 | `http://localhost:8000/health` | 判断后端是否就绪 |
| 游戏服务 | `http://localhost:8088` | 查看生成的视觉小说 |
| Redis | `localhost:6379` | Celery 消息队列/结果后端 |
| PostgreSQL | `localhost:15432` | 本机外部数据库，需预先运行 |

已验证的游戏预览：

```text
http://localhost:8088/d9d05f523f62/
```

## 5. 本地生产链配置

本地配置文件：

```text
F:\PyCharm_Project\VNStory\novel-game-factory\docker-compose.local.yml
```

关键环境变量：

```yaml
LOCAL_PIPELINE_MODE: "1"
OSS_DISABLED: "1"
STORAGE_PATH: /storage
```

含义：

- `LOCAL_PIPELINE_MODE=1`
  - API Gateway 确认任务后使用本地 Celery 视觉生产链。
  - 不依赖远程平台的创作模式映射。
  - 当前链路包含剧情解析、信息提取、人物设计、场景绘制、游戏构建。
  - 音频相关阶段不参与本地视觉实验链。

- `OSS_DISABLED=1`
  - 禁止 OSS 远程读写。
  - 兼容旧调用方的 `oss_read`/`oss_write` 会映射到本地 storage 逻辑。

- `STORAGE_PATH=/storage`
  - 容器内统一存储根目录。
  - 本地 Compose 将宿主机的 `F:\PyCharm\_Project\VNStory\storage` 挂载到容器 `/storage`。

本地文件目录：

```text
F:\PyCharm\_Project\VNStory\storage\raw_novels
F:\PyCharm\_Project\VNStory\storage\outputs
F:\PyCharm\_Project\VNStory\storage\built_games
```

## 6. 小说到游戏流程

前端上传接口：

```text
POST /api/v1/story/upload
```

对应前端调用：

```text
creation-engine-frontend/src/api/modules/creation.js
```

主要流程：

```text
选择 .txt 小说
  -> 前端提交 multipart/form-data
  -> API Gateway 写入 storage/raw_novels
  -> 创建 novel_tasks 任务记录
  -> 提交摘要任务
  -> 用户确认创作参数
  -> 提交本地视觉 Celery 链
  -> story_planner 解析剧情
  -> info_extractor 提取人物/场景/剧情信息
  -> char_designer 生成人物素材
  -> scene_painter 生成场景素材
  -> game_builder 构建视觉小说游戏包
  -> 写入 storage/built_games/<task_id>
  -> game_server 提供浏览器预览
```

当前成功验证的任务：

```text
task_id=d9d05f523f62
```

验证过的阶段状态：

```text
overall_status=已完成
story_planner=已完成
info_extractor=已完成
char_designer=已完成
scene_painter=已完成
game_builder=已完成
sound_arranger=已跳过
audio_producer=已跳过
```

## 7. 已完成的关键代码修改

### 7.1 本地视觉流水线

文件：

```text
novel-game-factory/services/game_builder/tasks.py
novel-game-factory/services/game_builder/converter.py
```

修改内容：

- 本地视觉模式下从本地剧情数据物化章节字幕目录。
- 构建时直接使用 `storage/outputs/<task_id>`。
- 不生成音频。
- 不访问 OSS。
- 补充本地 Nginx 配置兼容逻辑。

### 7.2 任务状态刷新

文件：

```text
novel-game-factory/common/progress_utils.py
```

修改内容：

- 增加 `_load_fresh_task()`。
- 针对性 UPDATE 后刷新 SQLAlchemy Session。
- 修复阶段实际完成但总状态仍停留在“处理中”的问题。

### 7.3 游戏包 MIME 类型

文件：

```text
novel-game-factory/nginx.conf
F:\PyCharm\_Project\VNStory\storage\built_games\nginx.conf
```

补充配置：

```nginx
include /etc/nginx/mime.types;
default_type application/octet-stream;
root /usr/share/nginx/html;
```

作用：

- 正确返回 `.js`、`.css`、`.json`、`service-worker.js` 等文件的 MIME 类型。
- 避免视觉小说游戏页面资源加载失败。

### 7.4 前端 502 修复

文件：

```text
creation-engine-frontend/docker/nginx.local.conf
```

关键配置：

```nginx
resolver 127.0.0.11 ipv6=off valid=30s;
resolver_timeout 5s;

set $api_gateway_upstream api_gateway:8000;
proxy_pass http://$api_gateway_upstream;
```

根因：

- 前端 Nginx 直接使用 `api_gateway:8000` 时，Docker DNS 可能返回 IPv6 地址。
- API Gateway 的 Uvicorn 只监听 IPv4。
- Nginx 连接 IPv6 被拒绝后返回 `502 Bad Gateway`。
- 使用 Docker DNS 的 IPv4 动态解析后，前端容器可以跟随 API Gateway 容器重启后的地址。

### 7.5 本地游戏仓库

文件：

```text
creation-engine-frontend/src/api/modules/game.js
```

当前行为：

- 请求 `/v1/tasks?status=2`。
- 只读取状态为“已完成”的任务。
- 将本地游戏地址拼接为：

```text
http://localhost:8088/<task_id>/
```

- 首页“游戏仓库”直接展示本地已构建视觉小说。

当前限制：

- 游戏地址使用 `localhost`，适用于当前电脑本机访问。
- 如果未来需要局域网其他电脑访问，需要将游戏基础地址改成宿主机局域网 IP 或统一运行时配置。

## 8. 无登录设计

前端路由：

```text
creation-engine-frontend/src/router/index.js
```

当前路由守卫只设置页面标题，默认允许直接访问：

```text
/home
/library
/create
/projects
/assets/:category
/my-assets
```

登录页面文件仍可能存在于前端源码中，但当前默认路由不跳转登录，也不要求登录后才能创建作品。

不要重新接入以下远程登录或 CMS 发布依赖，除非明确恢复平台化部署：

```text
creation-engine-frontend/src/views/login
creation-engine-frontend/src/api/modules/auth.js
```

### game-platform 中台前端

文件：

```text
game-platform/frontend/src/router/index.js
game-platform/frontend/src/api/request.js
game-platform/frontend/src/store/auth.js
```

当前本地模式行为：

- 所有业务路由直接可访问，不再执行未登录跳转。
- `/login` 和 `/auth/callback` 仅作为旧链接兼容入口，自动跳转 `/tasks`。
- API 返回 `401` 时不再清理状态或强制跳转登录页。
- 页面显示固定的“本地模式 / 本地用户”身份。
- `game-platform` 后端默认 `PLATFORM_AUTH_DISABLED=1`，匿名请求使用
  `local-anonymous` 和管理员角色。
- 如需恢复 JWT/OAuth 鉴权，将 `PLATFORM_AUTH_DISABLED` 设置为 `0`。

## 9. 快速健康检查

### 检查 Docker 服务

```powershell
cd F:\PyCharm_Project\VNStory\novel-game-factory
docker compose -f docker-compose.yml -f docker-compose.local.yml ps
```

重点确认这些服务为运行状态：

```text
api_gateway
creation_frontend
game_server
redis
story_planner
info_extractor
char_designer
scene_painter
game_builder
```

### 检查 API

```powershell
curl.exe -sS http://localhost:8000/health
curl.exe -i -sS http://localhost:5174/api/v1/models/options
curl.exe -i -sS "http://localhost:5174/api/v1/tasks?limit=100&offset=0&status=2"
```

预期 HTTP 状态：

```text
200
200
200
```

### 检查前端

```powershell
curl.exe -i -sS http://localhost:5174/
```

预期：

```text
HTTP/1.1 200 OK
```

### 检查游戏

```powershell
curl.exe -i -sS http://localhost:8088/d9d05f523f62/
curl.exe -i -sS http://localhost:8088/d9d05f523f62/chapter_1_chapter_1/
```

预期：

```text
HTTP/1.1 200 OK
```

## 10. 502 排障顺序

如果页面提示：

```text
Request failed with status code 502
```

按以下顺序检查：

1. 检查 API Gateway：

   ```powershell
   curl.exe -sS http://localhost:8000/health
   ```

2. 检查前端代理：

   ```powershell
   curl.exe -i -sS http://localhost:5174/api/v1/models/options
   ```

3. 查看前端 Nginx 错误：

   ```powershell
   docker exec novel-game-factory-creation_frontend-1 sh -lc "tail -80 /var/log/nginx/error.log"
   ```

4. 查看 API Gateway 日志：

   ```powershell
   docker logs --since 10m novel-game-factory-api_gateway-1
   ```

5. 检查服务网络：

   ```powershell
   docker exec novel-game-factory-creation_frontend-1 sh -lc "getent hosts api_gateway"
   docker exec novel-game-factory-creation_frontend-1 sh -lc "wget -S -O - http://api_gateway:8000/health"
   ```

6. 如果 API Gateway 刚刚重启，等待健康检查恢复 200。启动窗口内的短暂 502 不代表代码回归失败。

关键判断：

- `localhost:8000/health` 为 200，前端 `/api/...` 为 502：优先检查前端 Nginx 配置。
- API Gateway 健康检查也失败：检查 API 容器启动日志、数据库连接和 Redis。
- API 和前端都为 200，但游戏为空：检查任务状态是否为 2，以及 `storage/built_games/<task_id>` 是否存在。
- 游戏首页为 200，但章节资源失败：检查 `storage/built_games/nginx.conf` 的 MIME 和章节 location 配置。

## 11. 验证记录

截至 2026-10-11 已验证：

| 验证项 | 结果 |
|---|---|
| API Gateway 健康检查 | 200 |
| 前端首页 | 200 |
| 前端代理模型接口 | 200 |
| 前端代理任务列表接口 | 200 |
| 小说上传接口 | 200，并返回 `task_id` |
| 已完成任务列表 | 200，并返回 `d9d05f523f62` |
| 游戏首页 | 200 |
| 游戏章节页 | 200 |
| 浏览器控制台业务错误 | 0 |
| 游戏仓库卡片 | 能显示本地完成作品 |
| 游戏仓库 iframe | 指向 `http://localhost:8088/d9d05f523f62/` |
| Docker 前端镜像构建 | 成功 |
| Harbor 访问 | 未使用 |
| OSS 远程存储 | 本地模式已禁用 |

上传接口曾使用本地已有小说做回归测试，测试产生的临时任务已删除，没有保留额外测试作品。

## 12. 当前已知限制

- 宿主机直接执行前端 `npm run build` 可能提示 `vite is not recognized`，因为前端依赖安装在 Docker 构建阶段；应使用 Docker 构建。
- 当前 Docker Compose 不创建 PostgreSQL，必须提前准备本机数据库。
- 当前游戏仓库只显示已完成任务，不会展示处理中或失败任务。
- 当前游戏仓库封面为空时使用首字母占位。
- 当前游戏地址固定使用 `http://localhost:8088`，只适合本机浏览器。
- 音频生产阶段在本地视觉模式中跳过。
- 部分旧的资产、CMS 发布、登录和远程平台接口仍存在源码中，但不属于当前本地最小可运行链路。
- API Gateway 启动期间可能短暂返回 502，健康检查恢复后再继续使用。

## 13. 下次接手建议

优先保持以下边界：

1. 先确认 Docker 服务和 PostgreSQL，不要先改前端业务代码。
2. 先运行 `/health`、`/models/options`、`/tasks` 三个接口。
3. 修改 API 或前端后，重新构建对应 Docker 服务。
4. 不要把本地模式改回远程 OSS 或 Harbor 镜像。
5. 不要仅凭页面提示判断问题，结合 Nginx 日志、API 日志和实际 HTTP 状态定位。
6. 新增功能时优先复用当前 `storage`、Celery 和现有任务状态模型。

当前最重要的成功标志是：

```text
用户无需登录
用户上传 TXT 小说
任务进入本地生产链
人物和场景素材生成
game_builder 生成游戏包
游戏仓库显示作品
浏览器可以打开视觉小说并阅读章节对白
```
