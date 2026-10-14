FROM mirror.gcr.io/library/python:3.10-slim

WORKDIR /app

ENV TZ=Asia/Shanghai

# 先复制 requirements.txt（利用 Docker 缓存）
COPY services/scene_painter/requirements.txt /app/services/scene_painter/requirements.txt

# 安装 Python 依赖（从 requirements.txt，包含 celery 和 redis）
RUN pip install --no-cache-dir --timeout=1800 --retries=5 \
    -r /app/services/scene_painter/requirements.txt

# 复制通用模块
COPY common/ /app/common/

# 复制 services __init__.py
COPY services/__init__.py /app/services/__init__.py

# 复制服务代码
COPY services/scene_painter/ /app/services/scene_painter/

# 设置 Python 路径
ENV PYTHONPATH=/app

WORKDIR /app/services/scene_painter

CMD ["celery", "-A", "tasks", "worker", "--loglevel=info", "-Q", "scene_queue"]
