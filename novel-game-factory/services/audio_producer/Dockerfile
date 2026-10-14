FROM mirror.gcr.io/library/python:3.10-slim

WORKDIR /app

# 只安装必要的编译工具（使用阿里云镜像源加速）
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80retries && \
    sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources && \
    sed -i 's/security.debian.org/mirrors.aliyun.com\/debian-security/g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends tzdata gcc g++ libpq-dev ffmpeg && \
    rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai

# 先复制 requirements.txt（利用 Docker 缓存）
COPY services/audio_producer/requirements.txt /app/services/audio_producer/requirements.txt

# 安装 Python 依赖（从 requirements.txt，包含 celery 和 redis）
RUN pip install --no-cache-dir --timeout=1800 --retries=5 \
    -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com \
    -r /app/services/audio_producer/requirements.txt

# 复制通用模块
COPY common/ /app/common/

# 复制 services __init__.py
COPY services/__init__.py /app/services/__init__.py

# 复制服务代码
COPY services/audio_producer/ /app/services/audio_producer/

# 设置 Python 路径
ENV PYTHONPATH=/app

WORKDIR /app/services/audio_producer

CMD ["celery", "-A", "tasks", "worker", "--loglevel=info", "-Q", "audio_queue"]
