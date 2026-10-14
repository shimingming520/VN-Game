FROM mirror.gcr.io/library/python:3.11-slim

WORKDIR /app

# Python 标准库和应用依赖已足够处理本地构建的时区信息。
ENV TZ=Asia/Shanghai

# 先复制 requirements.txt 并安装依赖（利用 Docker 缓存，代码改动不触发重装）
COPY services/game_builder/requirements.txt /app/services/game_builder/requirements.txt
RUN pip install --no-cache-dir --timeout=1800 --retries=5 -r /app/services/game_builder/requirements.txt

# Copy common module
COPY common/ /app/common/

# Copy services __init__.py
COPY services/__init__.py /app/services/__init__.py

# Copy service files
COPY services/game_builder/ /app/services/game_builder/

# Set Python path
ENV PYTHONPATH=/app

# Command - use tasks module directly to ensure task registration
CMD ["celery", "-A", "services.game_builder.tasks", "worker", "--loglevel=info", "-Q", "build_queue"]
