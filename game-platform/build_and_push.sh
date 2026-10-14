#!/bin/bash
# 构建并推送平台镜像 + adapter 镜像 + 前端镜像
#   ./build_and_push.sh            # 三个都构建
#   ./build_and_push.sh platform   # 只构建平台
#   ./build_and_push.sh adapter    # 只构建 adapter
#   ./build_and_push.sh frontend   # 只构建前端
set -e

HARBOR="harbor.farmmx.com"
PLATFORM_IMAGE="${HARBOR}/agent-platform/agent-platform"
ADAPTER_IMAGE="${HARBOR}/game/platform_adapter"
FRONTEND_IMAGE="${HARBOR}/agent-platform/platform-frontend"

read -p "Harbor 用户名: " HARBOR_USER
read -s -p "Harbor 密码: " HARBOR_PASS
echo
read -p "镜像 Tag(默认 latest): " TAG
TAG=${TAG:-latest}

echo "$HARBOR_PASS" | docker login "$HARBOR" -u "$HARBOR_USER" --password-stdin

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FILTER=${1:-all}

if [ "$FILTER" = "all" ] || [ "$FILTER" = "platform" ]; then
  echo "== Building platform: ${PLATFORM_IMAGE}:${TAG}"
  docker build --platform linux/amd64 -t "${PLATFORM_IMAGE}:${TAG}" \
    -f "${SCRIPT_DIR}/docker/Dockerfile" "${SCRIPT_DIR}"
  docker push "${PLATFORM_IMAGE}:${TAG}"
fi

if [ "$FILTER" = "all" ] || [ "$FILTER" = "adapter" ]; then
  NGF="${SCRIPT_DIR}/../novel-game-factory"
  echo "== Building adapter: ${ADAPTER_IMAGE}:${TAG}"
  docker build --platform linux/amd64 -t "${ADAPTER_IMAGE}:${TAG}" \
    -f "${NGF}/services/platform_adapter/Dockerfile" "${NGF}"
  docker push "${ADAPTER_IMAGE}:${TAG}"
fi

if [ "$FILTER" = "all" ] || [ "$FILTER" = "frontend" ]; then
  echo "== Building frontend: ${FRONTEND_IMAGE}:${TAG}"
  docker build --platform linux/amd64 -t "${FRONTEND_IMAGE}:${TAG}" \
    -f "${SCRIPT_DIR}/frontend/Dockerfile" "${SCRIPT_DIR}/frontend"
  docker push "${FRONTEND_IMAGE}:${TAG}"
fi

echo "完成"
