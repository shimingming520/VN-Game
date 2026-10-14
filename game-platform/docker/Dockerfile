FROM python:3.10-slim

ENV TZ=Asia/Shanghai
WORKDIR /app
COPY requirements.txt .
# 先走清华源(直连 pythonhosted 常超时),失败再回退官方源
RUN pip install --no-cache-dir --timeout=300 --retries=10 \
    -i https://pypi.tuna.tsinghua.edu.cn/simple/ \
    -r requirements.txt || \
    pip install --no-cache-dir --timeout=3600 --retries=10 \
    -r requirements.txt

COPY . .

# Fail the image build if a locally imported runtime module (especially the
# transactional outbox) was forgotten from Git or the build context.
RUN PYTHONPATH=/app python -c "import main; import orchestrator.outbox"

EXPOSE 8000
# API 进程(默认);回调消费者用同镜像换 command:
#   python -m orchestrator.consumer
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
