FROM mirror.gcr.io/library/python:3.10-slim

WORKDIR /app

# 复制 requirements.txt 并安装核心业务依赖
COPY services/story_planner/requirements.txt /app/services/story_planner/requirements.txt
RUN pip install --no-cache-dir --timeout=1800 --retries=5 -r /app/services/story_planner/requirements.txt

# 复制通用模块和 __init__.py
COPY common/ /app/common/
COPY services/__init__.py /app/services/__init__.py

# 复制服务代码主体
COPY services/story_planner/ /app/services/story_planner/

# 设置 Python 路径和工作目录
ENV PYTHONPATH=/app
WORKDIR /app/services/story_planner

CMD ["celery", "-A", "tasks", "worker", "--loglevel=info", "-Q", "story_queue"]
