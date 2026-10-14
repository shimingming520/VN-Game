# game-platform 中台

`platformization_design.md` 的可运行骨架:注册中心 + Orchestrator(DAG/状态机/幂等) + 网关 API + MQ envelope 协议 + 协议 SDK。

## 本地联调步骤

```bash
cd game-platform
pip install -r requirements.txt

# 本地免登录模式(默认开启);恢复 JWT/OAuth 时设置为 0
set PLATFORM_AUTH_DISABLED=1

# 1. 起 RabbitMQ + 平台 PG
docker compose -f docker-compose.dev.yml up -d

# 2. 起 API(自动建表 + 声明 exchange + 种子默认 DAG 模板)
uvicorn main:app --host 127.0.0.1 --port 8001

# 3. 起回调消费者(另开终端)
python -m orchestrator.consumer

# 4. 起 mock 实现(另开终端;一进程模拟一个组的全部 7 个能力)
python scripts/mock_impl.py --all group-ref

# 5. 创建任务,跑整条 DAG
curl -X POST localhost:8001/api/v1/tasks -H 'content-type: application/json' \
  -d '{"task_id":"demo_001","params":{"art_style":"ink_chinese"}}'

# 6. 查进度 / token / 按组出账
curl localhost:8001/api/v1/tasks/demo_001
curl localhost:8001/api/v1/tasks/demo_001/usage
curl localhost:8001/api/v1/usage/summary

# 热插拔:停用某实现(队列解绑,不改代码不重启)
curl -X PATCH localhost:8001/api/impls/group-ref.char_designer.mock \
  -H 'content-type: application/json' -d '{"enabled":false}'

# 取消任务
curl -X POST localhost:8001/api/v1/tasks/demo_001/cancel
```

RabbitMQ 管理台: http://localhost:15672 (guest/guest)

平台前端:

```bash
cd frontend
npm ci
npm run dev -- --host 127.0.0.1 --port 5175
```

打开 `http://localhost:5175` 即可直接进入任务日志，不需要登录。旧地址
`/login` 会自动跳转到 `/tasks`。本地匿名任务统一使用
`local-anonymous` 作为归属标识。

## 目录

```
common/        config / 平台库 models(9张表) / MQ 薄封装
protocol/      envelope.py(六章契约的 pydantic 版) + sdk.py(算法组接入 SDK)
registry/      注册中心 API(注册/查询/热插拔/心跳/pick_impl)
orchestrator/  dag.py(DAG引擎,无状态) + core.py(submit/advance/callback状态机) + consumer.py
gateway/       任务创建/查询/取消/usage API
scripts/       mock_impl.py 联调用假实现
docker/        Dockerfile(一个镜像,api 和 consumer 换 command)
k8s/           按组件拆分的部署文件
```

## K8s 部署

```
k8s/
├── 00-namespace.yaml            ns: agent-platform
├── 01-secret.example.yaml      敏感配置模板(复制为 01-secret.yaml 填真值,不进 git)
├── 02-configmap.yaml            非敏感配置
├── 03-rabbitmq.yaml             MQ(自建,带 PVC 持久化 + 自建账号)
├── 04-platform-api.yaml         网关+注册中心(Deployment + Service)
└── 05-orchestrator-consumer.yaml 回调消费者(同镜像换 command,无 Service)
```

平台库用 172.16.50.17 上新建的 `agent_platform`(与业务库 extreme 同实例不同库),连接串填进 secret。

镜像:`harbor.farmmx.com/agent-platform/agent-platform`(平台 api+consumer 同镜像);
adapter 镜像:`harbor.farmmx.com/game/platform_adapter`(部署在 novel-game ns,见
`novel-game-factory/k8s/platform-adapter-deployment.yaml`)。

```bash
./build_and_push.sh                                # 构建+推送两个镜像
cp k8s/01-secret.example.yaml k8s/01-secret.yaml   # 填真实密码
kubectl apply -f k8s/
```

## 骨架未含(后续按设计文档补)

- 主站 OAuth / JWT(十章)、回调验签(C2)、STS 临时凭证(C1)
- DLQ 死信(B1)、预算护栏(D1)、输出契约校验(A1)
- step 超时定时器、orchestrator 多副本抢锁
