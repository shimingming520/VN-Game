# Docker 构建加速方案

## 文件说明

### 1. Dockerfile.base.ml
ML 基础镜像，包含 torch、transformers、ctranslate2 等大包，专门给 sound_arranger 和 story_planner 使用。

### 2. build_base_ml.sh
构建并推送 ML 基础镜像的脚本。

**使用方法**：
```bash
cd docker
chmod +x build_base_ml.sh
./build_base_ml.sh
```

**注意**：
- 首次构建需要 10-20 分钟（下载 torch 等大包）
- 推送需要 5-10 分钟（镜像约 1.5GB）
- 只需要构建一次，后续除非依赖更新才需要重建

### 3. build_and_push.sh
并行构建和推送所有服务的脚本。

**使用方法**：
```bash
cd docker
chmod +x build_and_push.sh

# 构建所有服务
./build_and_push.sh latest

# 只构建指定服务
./build_and_push.sh latest story_planner
```

**特点**：
- 支持并行构建（默认 4 个并发）
- 支持指定单个服务构建
- 自动启用 BuildKit 加速
- 彩色输出，易于查看进度

### 4. Jenkinsfile
Jenkins CI/CD Pipeline 配置文件（位于项目根目录）。

**功能**：
- 支持测试环境（game-test）和生产环境（game）
- 自动从 Git 拉取代码
- 并行构建 7 个服务
- 自动推送到 Harbor
- 自动更新 K8s 部署

**使用方法**：
- 构建时选择环境：`test` 或 `prod`
- 测试环境：推送到 `harbor.farmmx.com/game-test`，部署到 `novel-game-test` namespace
- 生产环境：推送到 `harbor.farmmx.com/game`，部署到 `novel-game` namespace

**Jenkins 配置步骤**：
1. 安装插件：Docker Pipeline, Kubernetes CLI
2. 配置凭据：
   - ID: `harbor-credentials`
   - 类型: Username with password
   - 用户名: Harbor 用户名
   - 密码: Harbor 密码
3. 配置 kubectl：确保 Jenkins 节点能访问 K8s 集群
4. 创建 Pipeline 任务：
   - Pipeline script from SCM
   - Script Path: `Jenkinsfile`（根目录）
5. 构建时会提示选择环境（test/prod）

---

## 实施步骤

### 第1步：构建 ML 基础镜像（一次性，20分钟）
```bash
cd /Users/qy11/PycharmProjects/SJ_novel2/novel-game-factory/docker
./build_base_ml.sh
```

### 第2步：构建所有服务（5-10分钟）
```bash
cd /Users/qy11/PycharmProjects/SJ_novel2/novel-game-factory/docker
export DOCKER_BUILDKIT=1
./build_and_push.sh latest
```

### 第3步（可选）：配置 Jenkins CI/CD
按照上面的 Jenkins 配置步骤操作。

---

## 效果对比

| 场景 | 优化前 | 优化后 | 提速 |
|------|--------|--------|------|
| **首次构建所有服务** | 35-50分钟 | 30分钟（基础镜像）+ 5分钟（服务） | - |
| **代码改动后重新构建** | 35-50分钟 | 3-5分钟 | **90%** |
| **只构建单个服务** | 5-10分钟 | 1-2分钟 | **80%** |
| **推送（本地→Harbor）** | 10-15分钟 | 2-3分钟 | **80%** |

---

## CI/CD vs 手动构建

### 手动构建（build_and_push.sh）
**优点**：
- 灵活，可以指定构建单个服务
- 不需要额外配置
- 适合开发阶段快速迭代

**缺点**：
- 需要手动执行
- 需要输入 Harbor 密码
- 推送速度受本地网络影响

### Jenkins CI/CD
**优点**：
- 自动化，Git push 后自动构建部署
- 并行构建，速度更快
- 如果 Jenkins 在内网，推送速度提升 5-10 倍
- 构建历史和日志集中管理
- 不需要每个开发者都有 Harbor 和 K8s 权限

**缺点**：
- 需要搭建和配置 Jenkins
- 需要配置 Git webhook

### 建议
- **开发阶段**：使用 `build_and_push.sh`（快速迭代，可指定服务）
- **测试/生产**：使用 Jenkins CI/CD（自动化部署）

---

## 依赖更新

### 场景1：某个服务的依赖更新（常见）
例如 story_planner 的某个依赖升级：
```bash
# 只需要重新构建该服务
./build_and_push.sh latest story_planner
```

### 场景2：ML 基础镜像依赖更新（少见）
例如 torch 升级：
```bash
# 1. 修改 docker/Dockerfile.base.ml
# 2. 重新构建基础镜像
./build_base_ml.sh

# 3. 重新构建使用该基础镜像的服务
./build_and_push.sh latest sound_arranger
./build_and_push.sh latest story_planner
```

---

## 常见问题

### Q: 为什么只给 sound_arranger 和 story_planner 做基础镜像？
A: 因为这两个服务用了 torch 等大包（200MB+），其他服务依赖都很小，没必要做基础镜像。

### Q: 基础镜像需要经常更新吗？
A: 不需要。只有当 torch、transformers 等大包升级时才需要更新，一般几个月一次。

### Q: 并行构建会不会占用太多资源？
A: 默认并发数是 4，可以在 `build_and_push.sh` 中修改 `MAX_JOBS` 变量。建议设置为 CPU 核心数的一半。

### Q: Jenkins 推送会更快吗？
A: 如果 Jenkins 和 Harbor 在同一个内网（同机房或同云区域），推送速度会提升 5-10 倍。如果都在外网，速度差不多。
