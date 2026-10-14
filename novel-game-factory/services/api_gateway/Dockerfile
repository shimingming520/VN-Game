FROM mirror.gcr.io/library/python:3.10-slim

WORKDIR /app

# 安装系统依赖
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80retries && \
    sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources && \
    sed -i 's/security.debian.org/mirrors.aliyun.com\/debian-security/g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && apt-get install -y --fix-missing \
    tzdata \
    postgresql-client \
    curl \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai

# 先只复制依赖清单并安装，使依赖层可被缓存（代码改动不会触发重装依赖）
COPY services/api_gateway/requirements.txt /app/services/api_gateway/requirements.txt
RUN pip install --no-cache-dir --timeout=1800 --retries=5 -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com -r services/api_gateway/requirements.txt

# 再复制代码（仅 api_gateway 运行所需的包）
COPY common/ /app/common/
# task_schema 是启动时必需模块。显式复制并在构建期导入，避免 Git/CI 漏文件后
# 镜像仍能构建成功、直到 Pod 启动才报 ModuleNotFoundError。
COPY common/task_schema.py /app/common/task_schema.py
COPY services/__init__.py /app/services/__init__.py
COPY services/story_planner/ /app/services/story_planner/
COPY services/api_gateway/ /app/services/api_gateway/
RUN PYTHONPATH=/app python -c "from common.task_schema import ensure_novel_tasks_schema; import services.api_gateway.main"

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["uvicorn", "services.api_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
