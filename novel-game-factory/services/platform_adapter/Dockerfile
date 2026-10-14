# 平台 adapter:消费平台 Job → send_task 调现有 Celery 服务 → 回调
# 构建上下文 = novel-game-factory 根目录:
#   docker build --platform linux/amd64 -f services/platform_adapter/Dockerfile \
#     -t harbor.farmmx.com/game/platform_adapter:latest .
FROM mirror.gcr.io/library/python:3.10-slim

WORKDIR /app

RUN pip install --no-cache-dir --timeout=1800 --retries=5 \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    "celery==5.6.3" "kombu==5.6.2" "billiard==4.2.4" \
    "vine==5.1.0" "amqp==5.3.1" "redis==8.1.0" "pika==1.4.4" \
    "requests==2.34.2" "SQLAlchemy==2.0.52" "sqlmodel==0.0.39" \
    "pydantic==2.13.4" "psycopg2-binary==2.9.12" "python-dotenv==1.2.3"

COPY common/ /app/common/
COPY services/__init__.py /app/services/__init__.py
COPY services/platform_adapter/ /app/services/platform_adapter/

RUN PYTHONPATH=/app python -c "import services.platform_adapter.adapter"

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "services.platform_adapter.adapter"]
