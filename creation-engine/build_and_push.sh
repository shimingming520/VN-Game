#!/bin/bash
# 构建并推送创作引擎镜像(后端 + 前端)
#   ./build_and_push.sh            # 两个都构建
#   ./build_and_push.sh api        # 只构建后端
#   ./build_and_push.sh frontend   # 只构建前端
set -e

HARBOR="harbor.farmmx.com"
API_IMAGE="${HARBOR}/agent-platform/creation-engine-api"
FRONTEND_IMAGE="${HARBOR}/agent-platform/creation-engine-frontend"

read -p "Harbor 用户名: " HARBOR_USER
read -s -p "Harbor 密码: " HARBOR_PASS
echo
read -p "镜像 Tag(默认 latest): " TAG
TAG=${TAG:-latest}

echo "$HARBOR_PASS" | docker login "$HARBOR" -u "$HARBOR_USER" --password-stdin

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FRONTEND_DIR="${SCRIPT_DIR}/../creation-engine-frontend"
FILTER=${1:-all}

if [ "$FILTER" = "all" ] || [ "$FILTER" = "api" ]; then
  echo "== Building api: ${API_IMAGE}:${TAG}"
  docker build --platform linux/amd64 -t "${API_IMAGE}:${TAG}" \
    -f "${SCRIPT_DIR}/Dockerfile" "${SCRIPT_DIR}"
  docker push "${API_IMAGE}:${TAG}"
fi

if [ "$FILTER" = "all" ] || [ "$FILTER" = "frontend" ]; then
  echo "== Building frontend: ${FRONTEND_IMAGE}:${TAG}"
  docker build --platform linux/amd64 -t "${FRONTEND_IMAGE}:${TAG}" \
    -f "${FRONTEND_DIR}/Dockerfile" "${FRONTEND_DIR}"
  docker push "${FRONTEND_IMAGE}:${TAG}"
fi

echo "完成"
