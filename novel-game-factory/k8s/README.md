# novel-game-factory K8s 部署指南

## 文件清单

| 文件 | 说明 |
|------|------|
| `namespace.yaml` | 命名空间 `novel-game` |
| `configmap.yaml` | 公共非敏感配置 |
| `secret.yaml` | 敏感配置（API Key、DB 密码）|
| `storage-pvc.yaml` | 共享存储 PVC（需 ReadWriteMany）|
| `redis-deployment.yaml` | Redis + Service |
| `api-gateway-deployment.yaml` | API 网关 + Service |
| `story-planner-deployment.yaml` | 故事规划 Worker |
| `sound-arranger-deployment.yaml` | 音效编排 Worker |
| `char-designer-deployment.yaml` | 角色设计 Worker |
| `audio-producer-deployment.yaml` | 音频制作 Worker |
| `scene-painter-deployment.yaml` | 场景绘制 Worker |
| `game-builder-deployment.yaml` | 游戏构建 Worker |
| `nginx-configmap.yaml` | Nginx 配置 |
| `game-server-deployment.yaml` | 静态游戏 Nginx 服务 + Service |

## 部署步骤

### 1. 修改 secret.yaml

将 `secret.yaml` 中的占位符替换为真实值：

```yaml
stringData:
  DB_PASSWORD: "your_actual_password"
  DASHSCOPE_API_KEY: "your_actual_key"
  STABILITY_API_KEY: "your_actual_key"
```

### 2. 修改镜像地址

各服务 Deployment 中的 `image: your-registry/...` 替换为你实际的镜像仓库地址，例如：

```yaml
image: registry.cn-hangzhou.aliyuncs.com/your-namespace/novel-api-gateway:latest
```

### 3. 确认 StorageClass

`storage-pvc.yaml` 默认不指定 StorageClass，K8s 将使用集群默认值。
若需 ReadWriteMany，请确保 StorageClass 支持（如 NFS、CephFS），并在 PVC 中指定：

```yaml
storageClassName: "nfs-client"
```

### 4. 应用所有配置

```bash
# 按顺序应用
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml
kubectl apply -f k8s/storage-pvc.yaml
kubectl apply -f k8s/nginx-configmap.yaml
kubectl apply -f k8s/redis-deployment.yaml
kubectl apply -f k8s/api-gateway-deployment.yaml
kubectl apply -f k8s/story-planner-deployment.yaml
kubectl apply -f k8s/sound-arranger-deployment.yaml
kubectl apply -f k8s/char-designer-deployment.yaml
kubectl apply -f k8s/audio-producer-deployment.yaml
kubectl apply -f k8s/scene-painter-deployment.yaml
kubectl apply -f k8s/game-builder-deployment.yaml
kubectl apply -f k8s/game-server-deployment.yaml

# 或一次性应用整个目录
kubectl apply -f k8s/
```

### 5. 查看状态

```bash
kubectl get all -n novel-game
```

## 访问服务

- **API 网关**: `http://<node-ip>:<NodePort>`（service `api-gateway` 的 NodePort）
- **游戏服务器**: `http://<node-ip>:<NodePort>`（service `game-server` 的 NodePort）

可通过 `kubectl get svc -n novel-game` 查看实际端口。

## 注意事项

- `secret.yaml` 包含敏感信息，**不要提交到 Git 仓库**，已在 `.gitignore` 中排除。
- 所有 Worker 服务（story-planner 等）通过 Celery + Redis 消费任务，无需对外暴露 Service。
- `story-planner` 使用离线翻译模型，需提前将模型文件放入 PVC 的 `models/` 目录。
