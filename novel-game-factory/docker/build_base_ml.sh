#!/bin/bash
# build_base_ml.sh - 构建并推送 ML 基础镜像
set -e

REGISTRY="harbor.farmmx.com/game"

echo "=== 构建 ML 基础镜像（包含 torch、transformers、ctranslate2）==="
echo "⚠️  注意：这个镜像很大（1.5GB+），首次构建和推送会比较慢（10-20分钟）"
echo "⚠️  但构建一次后，后续 sound_arranger 和 story_planner 构建会非常快"
echo

read -p "确认继续？(y/n): " confirm
if [ "$confirm" != "y" ]; then
    echo "已取消"
    exit 0
fi

read -p "Harbor 用户名: " HARBOR_USER
read -s -p "Harbor 密码: " HARBOR_PASS
echo
echo "$HARBOR_PASS" | docker login harbor.farmmx.com -u "$HARBOR_USER" --password-stdin

echo
echo "开始构建 ML 基础镜像: ${REGISTRY}/base:ml"
echo "预计耗时: 10-20 分钟（下载 torch 等大包）"
echo

START_TIME=$(date +%s)

cd ..
DOCKER_BUILDKIT=1 docker build \
  --platform linux/amd64 \
  -t "${REGISTRY}/base:ml" \
  -f docker/Dockerfile.base.ml .

echo
echo "开始推送 ML 基础镜像..."
docker push "${REGISTRY}/base:ml"

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
MINUTES=$((DURATION / 60))
SECONDS=$((DURATION % 60))

echo
echo "✅ ML 基础镜像构建完成: ${REGISTRY}/base:ml"
echo "耗时: ${MINUTES}分${SECONDS}秒"
echo
echo "接下来修改 sound_arranger 和 story_planner 的 Dockerfile："
echo "  FROM harbor.farmmx.com/game/base:ml"
