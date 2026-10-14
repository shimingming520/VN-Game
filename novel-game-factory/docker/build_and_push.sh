#!/bin/bash
# build_and_push.sh - 并行构建和推送 Docker 镜像（优化版）
# 用法:
#   ./docker/build_and_push.sh [TAG] [SERVICE ...]
#   例如: ./docker/build_and_push.sh latest              # 构建所有服务
#   例如: ./docker/build_and_push.sh latest story_planner  # 只构建 story_planner
#   例如: ./docker/build_and_push.sh latest story_planner info_extractor platform_adapter
#   兼容: ./docker/build_and_push.sh latest 'story_planner，info_extractor'
set -e

# 切换到项目根目录
cd "$(dirname "$0")/.."

REGISTRY="harbor.farmmx.com/game"
TAG=${1:-latest}
MAX_JOBS=4  # 并行数，根据 CPU 核心数调整

# 支持多个空格参数、英文逗号和中文逗号。用首尾逗号包住，子 shell 中可安全
# 做精确成员匹配，避免 "api" 误匹配 "api_gateway"。
FILTER_CSV=","
for RAW_FILTER in "${@:2}"; do
  NORMALIZED_FILTER="${RAW_FILTER//，/,}"
  IFS=',' read -r -a FILTER_PARTS <<< "$NORMALIZED_FILTER"
  for PART in "${FILTER_PARTS[@]}"; do
    PART="${PART#"${PART%%[![:space:]]*}"}"
    PART="${PART%"${PART##*[![:space:]]}"}"
    if [ -n "$PART" ]; then
      FILTER_CSV+="${PART},"
    fi
  done
done
if [ "$FILTER_CSV" = "," ]; then
  FILTER_CSV=""
fi

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}=== Docker 并行构建和推送工具 ===${NC}"
echo

# 登录 Harbor
read -p "Harbor 用户名: " HARBOR_USER
read -s -p "Harbor 密码: " HARBOR_PASS
echo
echo "$HARBOR_PASS" | docker login harbor.farmmx.com -u "$HARBOR_USER" --password-stdin

if [ $? -ne 0 ]; then
    echo -e "${RED}❌ Harbor 登录失败${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Harbor 登录成功${NC}"
echo

# 服务列表
SERVICES=(
  "api_gateway:services/api_gateway/Dockerfile"
  "story_planner:services/story_planner/Dockerfile"
  "info_extractor:services/info_extractor/Dockerfile"
  "audio_producer:services/audio_producer/Dockerfile"
  "scene_painter:services/scene_painter/Dockerfile"
  "char_designer:services/char_designer/Dockerfile"
  "game_builder:services/game_builder/Dockerfile"
  "sound_arranger:services/sound_arranger/Dockerfile"
  "platform_adapter:services/platform_adapter/Dockerfile"
)

# 构建单个服务的函数
build_and_push() {
  local entry=$1
  local NAME="${entry%%:*}"
  local DOCKERFILE="${entry##*:}"

  # 如果指定了过滤器，只构建匹配的服务
  if [ -n "$FILTER_CSV" ] && [[ "$FILTER_CSV" != *",${NAME},"* ]]; then
    return 0
  fi

  local LOG_FILE="/tmp/docker_build_${NAME}.log"

  echo "[$(date +%H:%M:%S)] 🔨 开始构建: $NAME"

  # 使用 BuildKit 加速，--no-cache 确保代码变更被打入镜像
  DOCKER_BUILDKIT=1 docker build \
    --no-cache \
    --platform linux/amd64 \
    -t "${REGISTRY}/${NAME}:${TAG}" \
    -f "${DOCKERFILE}" . \
    > "$LOG_FILE" 2>&1

  if [ $? -ne 0 ]; then
    echo "[$(date +%H:%M:%S)] ❌ 构建失败: $NAME (日志: $LOG_FILE)"
    return 1
  fi

  echo "[$(date +%H:%M:%S)] 📤 开始推送: $NAME"
  docker push "${REGISTRY}/${NAME}:${TAG}" >> "$LOG_FILE" 2>&1

  if [ $? -ne 0 ]; then
    echo "[$(date +%H:%M:%S)] ❌ 推送失败: $NAME (日志: $LOG_FILE)"
    return 1
  fi

  echo "[$(date +%H:%M:%S)] ✅ 完成: $NAME"
  rm -f "$LOG_FILE"
  return 0
}

export -f build_and_push
export REGISTRY TAG FILTER_CSV

# 未知服务直接失败，不能再出现“一个镜像都没构建却显示全部完成”。
if [ -n "$FILTER_CSV" ]; then
  CHECK_CSV="$FILTER_CSV"
  for entry in "${SERVICES[@]}"; do
    NAME="${entry%%:*}"
    CHECK_CSV="${CHECK_CSV//,${NAME},/,}"
  done
  if [ "$CHECK_CSV" != "," ]; then
    UNKNOWN="${CHECK_CSV#,}"
    UNKNOWN="${UNKNOWN%,}"
    echo -e "${RED}❌ 未知服务: ${UNKNOWN}${NC}"
    exit 1
  fi
fi

# 记录开始时间
START_TIME=$(date +%s)

if [ -n "$FILTER_CSV" ]; then
    DISPLAY_FILTER="${FILTER_CSV#,}"
    DISPLAY_FILTER="${DISPLAY_FILTER%,}"
    echo -e "${YELLOW}只构建服务: $DISPLAY_FILTER${NC}"
else
    echo -e "${YELLOW}开始并行构建 (最大并发: $MAX_JOBS)...${NC}"
fi
echo

# 并行构建
printf '%s\n' "${SERVICES[@]}" | xargs -P $MAX_JOBS -I {} bash -c 'build_and_push "$@"' _ {}

# 检查是否有失败
if [ $? -ne 0 ]; then
    echo
    echo -e "${RED}❌ 部分服务构建失败，请检查日志${NC}"
    exit 1
fi

# 计算耗时
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
MINUTES=$((DURATION / 60))
SECONDS=$((DURATION % 60))

echo
echo -e "${GREEN}🎉 全部完成！${NC}"
echo -e "${GREEN}总耗时: ${MINUTES}分${SECONDS}秒${NC}"
echo

# 清理
docker logout harbor.farmmx.com > /dev/null 2>&1
