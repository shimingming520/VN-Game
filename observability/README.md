# Novel Game 集中日志

这套清单直接部署到现有 `agent-platform` namespace，不新增 namespace：

- Vector DaemonSet：采集每个节点上 `novel-game`、`agent-platform` 的容器 stdout/stderr。
- Loki：日志集中存储，独立 PVC，保留 14 天。
- Grafana：按 namespace、app、Pod 和 `error/warn/info/debug` 查询。Grafana 本身无状态，重启后从 ConfigMap 自动恢复预置 Dashboard。

它不会修改七个业务服务，也不会占用 `novel-game-storage` 业务产物 PVC。真正的日志数据由 Loki 写入独立的 `loki-data` NFS PVC，不写 PostgreSQL，也不依赖容器本地盘长期保存。

## 部署

### Kuboard 单文件导入

不使用命令行时，打开 [kuboard-all-in-one.yaml](kuboard-all-in-one.yaml)，先把文件顶部的：

```yaml
admin-password: CHANGE_ME_TO_A_STRONG_PASSWORD
```

替换成强密码。然后在 Kuboard 选择目标集群，通过“从 YAML 创建/导入资源”粘贴整个文件并应用。资源已经固定在现有 `agent-platform` namespace，不需要新建 namespace。

如果 Kuboard 报 `ClusterRole`、`ClusterRoleBinding` 或 `hostPath` 无权限，说明当前 Kuboard 账号没有集群级日志采集权限；只有这种情况才需要管理员授权。其余资源不需要运维参与。

### kubectl

先确认集群存在 `storage-nfs`。该 StorageClass 不能扩容，因此 Loki 首次创建即申请 50Gi；需要其它容量时必须在首次部署前修改 `k8s/01-loki.yaml`。

```bash
cd /Users/qy11/PycharmProjects/SJ_novel2

kubectl get storageclass storage-nfs

kubectl create secret generic grafana-admin \
  -n agent-platform \
  --from-literal=admin-user=admin \
  --from-literal=admin-password='替换成强密码'

kubectl apply -k observability/k8s
```

等待：

```bash
kubectl rollout status deployment/loki -n agent-platform --timeout=10m
kubectl rollout status daemonset/vector -n agent-platform --timeout=10m
kubectl rollout status deployment/grafana -n agent-platform --timeout=10m
kubectl get pods,pvc -n agent-platform
```

如果 Loki 因 NFS 权限无法写入，停止部署并让运维给 `loki-data` 对应目录授予 UID/GID `10001` 写权限；不要把数据目录改到业务 PVC。

## 打开 Grafana

Grafana Service 使用固定的内网 NodePort `30300`。浏览器打开任一 Kubernetes
节点地址，例如 `http://172.16.30.33:30300`，然后进入：

```text
Dashboards → Pressure Test → Novel Game Pressure Test Logs
```

Dashboard 已预置：

- Error/Warning 数量趋势。
- ERROR 日志。
- WARNING 日志。
- 全部日志。
- namespace、app、Pod 下拉筛选。

## 日志等级规则

Vector 不要求修改现有业务代码，按日志正文归类：

- `error`：`ERROR`、`CRITICAL`、`FATAL`、`Traceback`、`Exception`、`❌`、`错误`、`失败`、`崩溃`。
- `warn`：`WARN`、`WARNING`、`⚠️`、`警告`。
- `debug`：`DEBUG`。
- 其余：`info`。

`task_id` 不作为 Loki label，避免高基数拖垮索引；需要定位任务时直接在日志正文中搜索 task ID。

## 常用 LogQL

持续重试：

```logql
{namespace=~"novel-game|agent-platform"}
|~ "(?i)(retry|重试|仍失败|recovery cycle failed)"
```

API Gateway：

```logql
{namespace="novel-game", app="api-gateway"}
|~ "batch-reconciler|提交中台编排失败|Traceback|Exception"
```

平台 outbox：

```logql
{namespace="agent-platform", app="platform-api"}
|~ "job publish failed|outbox recovery cycle failed|quarantining corrupt outbox"
```

回调消费者：

```logql
{namespace="agent-platform", app="orchestrator-consumer"}
|~ "callback processing failed|failed to hand callback|Traceback|Exception"
```

## 边界

这套日志栈只负责持久化和检索容器 stdout/stderr。RabbitMQ 队列数量、PostgreSQL 连接、Pod OOM/restart 和 NFS 容量属于指标/数据库状态，不能只靠日志准确判断，具体命令见 [PRESSURE_TEST_CHECKS.md](PRESSURE_TEST_CHECKS.md)。
