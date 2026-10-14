# ML 基础镜像 - 只给 sound_arranger 和 story_planner 用
# 包含 torch、transformers、ctranslate2、celery、redis 等
# 构建命令: cd .. && docker build -t harbor.farmmx.com/game/base:ml -f docker/Dockerfile.base.ml .
# 推送命令: docker push harbor.farmmx.com/game/base:ml
FROM python:3.10-slim

WORKDIR /app

# 配置 apt 和 pip 镜像源
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80retries && \
    sed -i 's/deb.debian.org/mirrors.ustc.edu.cn/g' /etc/apt/sources.list.d/debian.sources && \
    sed -i 's/security.debian.org/mirrors.ustc.edu.cn\/debian-security/g' /etc/apt/sources.list.d/debian.sources && \
    pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ && \
    pip config set global.trusted-host mirrors.aliyun.com

# 安装系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc g++ libpq-dev python3-numpy python3-scipy && \
    rm -rf /var/lib/apt/lists/*

# 安装公共依赖
RUN pip install --no-cache-dir \
    celery==5.3.4 \
    redis==5.0.1 \
    psycopg2-binary==2.9.9 \
    sqlalchemy==2.0.23 \
    python-dotenv==1.0.0

# 安装 torch CPU 版本（最大的包，200MB+）
RUN pip install --no-cache-dir --timeout=3600 --retries=10 \
    "torch==2.6.0+cpu" \
    --index-url https://download.pytorch.org/whl/cpu

# 安装 ctranslate2（单独安装，增加重试次数）
RUN pip install --no-cache-dir --timeout=3600 --retries=10 \
    ctranslate2 || \
    pip install --no-cache-dir --timeout=3600 --retries=10 \
    -i https://pypi.tuna.tsinghua.edu.cn/simple/ \
    ctranslate2

# 安装 transformers 相关（依赖 torch）
RUN pip install --no-cache-dir --timeout=3600 --retries=10 \
    transformers \
    sentencepiece \
    tokenizers \
    protobuf \
    huggingface-hub \
    safetensors \
    sentence-transformers>=2.2.0

# 清理缓存
RUN apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* && \
    find /usr/local -type f -name '*.pyc' -delete && \
    find /usr/local -type d -name '__pycache__' -delete

ENV PYTHONPATH=/app
