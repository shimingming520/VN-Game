#!/bin/bash
set -e

REGISTRY="harbor.farmmx.com/game"

read -p "Harbor 用户名: " HARBOR_USER
read -s -p "Harbor 密码: " HARBOR_PASS
echo
read -p "镜像 Tag（默认 latest）: " TAG
TAG=${TAG:-latest}

echo "$HARBOR_PASS" | docker login harbor.farmmx.com -u "$HARBOR_USER" --password-stdin

# 服务列表（镜像名: Dockerfile路径）
SERVICES=(
  "api_gateway:services/api_gateway/Dockerfile"
  "story_planner:services/story_planner/Dockerfile"
  "audio_producer:services/audio_producer/Dockerfile"
  "scene_painter:services/scene_painter/Dockerfile"
  "char_designer:services/char_designer/Dockerfile"
  "game_builder:services/game_builder/Dockerfile"
  "sound_arranger:services/sound_arranger/Dockerfile"
)

# 支持只构建指定服务：./build_and_push.sh story_planner
FILTER=${1:-}

for entry in "${SERVICES[@]}"; do
  NAME="${entry%%:*}"
  DOCKERFILE="${entry##*:}"

  if [ -n "$FILTER" ] && [ "$NAME" != "$FILTER" ]; then
    continue
  fi

  echo
  echo "======================================"
  echo "Building: $NAME"
  echo "======================================"
  docker build --platform linux/amd64 -t "${REGISTRY}/${NAME}:${TAG}" \
    -f "${DOCKERFILE}" .

  echo "Pushing: ${REGISTRY}/${NAME}:${TAG}"
  docker push "${REGISTRY}/${NAME}:${TAG}"
done

echo
echo "全部完成！"
