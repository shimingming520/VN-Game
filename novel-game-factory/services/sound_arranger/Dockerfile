FROM mirror.gcr.io/library/python:3.10-slim

WORKDIR /app

COPY services/sound_arranger/requirements.txt /app/services/sound_arranger/requirements.txt

# 只安装 requirements.txt 中的其他依赖（torch、sentence-transformers 已在基础镜像）
RUN pip install --no-cache-dir --timeout=1800 --retries=5 \
    -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com \
    -r /app/services/sound_arranger/requirements.txt

COPY common/ /app/common/
COPY services/__init__.py /app/services/__init__.py
COPY services/sound_arranger/ /app/services/sound_arranger/

ENV PYTHONPATH=/app

WORKDIR /app/services/sound_arranger

CMD ["celery", "-A", "tasks", "worker", "--loglevel=info", "-Q", "sound_queue"]
