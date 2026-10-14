FROM mirror.gcr.io/library/python:3.10-slim

WORKDIR /app

# 原生 RGBA 角色链路不再加载本地抠图模型；Python 依赖已覆盖运行所需组件。
ENV TZ=Asia/Shanghai

COPY services/char_designer/requirements.txt /app/services/char_designer/requirements.txt

RUN pip install --no-cache-dir --timeout=1800 --retries=5 \
    -r /app/services/char_designer/requirements.txt

# 复制通用模块
COPY common/ /app/common/

# 复制 services __init__.py
COPY services/__init__.py /app/services/__init__.py

# 复制服务代码
COPY services/char_designer/ /app/services/char_designer/

# 设置 Python 路径
ENV PYTHONPATH=/app

WORKDIR /app/services/char_designer

CMD ["celery", "-A", "tasks", "worker", "--loglevel=info", "-Q", "char_queue"]
