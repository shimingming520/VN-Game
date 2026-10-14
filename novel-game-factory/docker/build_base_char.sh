#!/bin/bash
# build_base_char.sh - 构建并推送 Char Designer 基础镜像
set -e

REGISTRY="harbor.farmmx.com/game"

echo "=== 构建 Char Designer 基础镜像 ==="
echo "包含: rembg、opencv、scipy、scikit-image、numpy"
echo "用于: char_designer"
echo "⚠️  注意：这个镜像很大（1GB+），首次构建和推送会比较慢（10-15分钟）"
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
echo "开始构建 Char Designer 基础镜像: ${REGISTRY}/base:char"
echo "预计耗时: 10-15 分钟"
echo

START_TIME=$(date +%s)

cd ..
DOCKER_BUILDKIT=1 docker build \
  --platform linux/amd64 \
  -t "${REGISTRY}/base:char" \
  -f docker/Dockerfile.base.char .

echo
echo "开始推送基础镜像..."
docker push "${REGISTRY}/base:char"

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
MINUTES=$((DURATION / 60))
SECONDS=$((DURATION % 60))

echo
echo "✅ Char Designer 基础镜像构建完成: ${REGISTRY}/base:char"
echo "耗时: ${MINUTES}分${SECONDS}秒"
echo
echo "接下来需要修改 char_designer 的 Dockerfile"
