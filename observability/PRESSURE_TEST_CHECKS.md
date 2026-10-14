# 压测期间六项检查

日志统一在 Grafana 查看；RabbitMQ、PostgreSQL、Pod 状态和 NFS 使用下面的权威检查。

## 1. 重复任务或阶段接管

在 `agent_platform` 数据库执行，把时间改成压测开始时间：

```sql
SELECT
    s.task_id,
    s.capability,
    count(*) AS step_rows,
    array_agg(s.step_id ORDER BY s.step_id) AS step_ids
FROM task_step s
JOIN task t ON t.task_id = s.task_id
WHERE t.created_at >= timestamp '2026-08-18 00:00:00'
GROUP BY s.task_id, s.capability
HAVING count(*) > 1;
```

预期返回 0 行。

```sql
SELECT
    task_id,
    capability,
    step_id,
    status,
    attempt,
    error_code,
    left(error_msg, 300) AS error_msg
FROM task_step
WHERE attempt > 1
ORDER BY task_id, capability;
```

`attempt > 1` 表示发生了业务重试，需要结合 ERROR/WARNING 日志解释原因，但不等同于重复执行。

在 `novel_tasks` 所在平台数据库执行：

```sql
SELECT task_id, stage, owner, fencing_token, claimed_at, updated_at
FROM stage_execution_fences
WHERE updated_at >= timestamp '2026-08-18 00:00:00'
  AND fencing_token > 1
ORDER BY updated_at DESC;
```

`fencing_token > 1` 表示阶段发生过接管，需要核查对应 Pod 重启、Redis lease 或网络问题。

## 2. RabbitMQ ready/unacked

```bash
kubectl exec -n agent-platform deployment/rabbitmq -- \
  rabbitmqctl list_queues \
  name durable consumers messages messages_ready messages_unacknowledged arguments \
  --formatter=table
```

- 压测中 `messages_ready` 上升代表正常排队。
- 长任务运行时 `messages_unacknowledged` 可持续存在。
- `ready > 0` 且 `consumers = 0` 表示消费者未连接。
- 全部任务结束后，主队列、callback、retry 的 ready/unacked 应最终归零。
- DLQ 只要存在消息就需要人工核查。

## 3. PostgreSQL 连接

压测前、峰值和结束后各执行一次：

```sql
SELECT
    now() AS observed_at,
    datname,
    COALESCE(NULLIF(application_name, ''), '(empty)') AS application_name,
    state,
    count(*) AS connections
FROM pg_stat_activity
WHERE backend_type = 'client backend'
GROUP BY datname, application_name, state
ORDER BY datname, connections DESC;
```

```sql
SELECT
    current_setting('max_connections')::int AS max_connections,
    count(*) FILTER (WHERE backend_type = 'client backend') AS used,
    count(*) FILTER (WHERE state = 'active') AS active,
    count(*) FILTER (WHERE state = 'idle') AS idle,
    count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_transaction
FROM pg_stat_activity;
```

要求：结束后 active 回到基线、连接总量不再持续增长、`idle_in_transaction = 0`。

## 4. Pod OOM、CrashLoop 和重启

```bash
kubectl get pods -n novel-game \
  -o custom-columns='POD:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,RESTARTS:.status.containerStatuses[*].restartCount,LAST_REASON:.status.containerStatuses[*].lastState.terminated.reason'

kubectl get pods -n agent-platform \
  -o custom-columns='POD:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,RESTARTS:.status.containerStatuses[*].restartCount,LAST_REASON:.status.containerStatuses[*].lastState.terminated.reason'

kubectl get events -n novel-game --field-selector type=Warning --sort-by=.lastTimestamp
kubectl get events -n agent-platform --field-selector type=Warning --sort-by=.lastTimestamp
```

若 Pod 重启，查看上一个容器退出前日志：

```bash
kubectl logs -n novel-game POD名称 --previous --timestamps
```

## 5. `/storage` 容量和 inode

```bash
kubectl exec -n novel-game deployment/api-gateway -- \
  sh -c 'df -hP /storage; df -iP /storage'
```

压测前、每档结束后、30 本全部完成后执行。建议容量可用保持 20% 以上，inode 可用保持 10% 以上。

## 6. API Gateway、platform-api、consumer 持续重试

Grafana 中使用预置 ERROR/WARNING 面板，或在 Explore 执行：

```logql
{namespace=~"novel-game|agent-platform"}
|~ "(?i)(retry|重试|仍失败|recovery cycle failed|Traceback|Exception)"
```

数据库再确认没有滞留 outbox：

```sql
SELECT
    task_id,
    step_id,
    attempt,
    status,
    publish_attempts,
    available_at,
    claimed_at,
    updated_at,
    left(last_error, 500) AS last_error
FROM job_outbox
WHERE status IN ('pending', 'publishing', 'failed')
  AND updated_at < now() - interval '5 minutes'
ORDER BY updated_at;
```

预期返回 0 行。短暂 retry 后自行恢复属于正常；同一错误连续出现且 outbox 超过 5 分钟未排空才算持续故障。
