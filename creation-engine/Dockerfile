# 基础镜像默认从自建 harbor 拉(见 mirror_base_images.sh)。公共 Docker Hub 代理源现在
# 基本都要认证或限流,官方源在国内极慢 —— 本机 arm64 而集群要 amd64,跨架构构建命中不了
# 缓存,每次都得重拉基础镜像。走内网 harbor 可避开。
# 想临时走官方源:docker build --build-arg BASE_REGISTRY=docker.io/library ...
ARG BASE_REGISTRY=harbor.farmmx.com/agent-platform
FROM ${BASE_REGISTRY}/python:3.10-slim

ENV TZ=Asia/Shanghai
WORKDIR /app
COPY requirements.txt .
# 先走清华源,失败回退官方源
RUN pip install --no-cache-dir --timeout=300 --retries=10 \
    -i https://pypi.tuna.tsinghua.edu.cn/simple/ \
    -r requirements.txt || \
    pip install --no-cache-dir --timeout=3600 --retries=10 \
    -r requirements.txt

COPY . .

# 在构建阶段就校验 FastAPI 入口及所有路由模块可导入。这会把
# gateway/creation.py、gateway/projects.py、common/schema.py 这类本地漏提交
# 问题前移到镜像构建，避免 Deployment 上线后才 CrashLoopBackOff。
RUN PYTHONPATH=/app python -c "import main"

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
