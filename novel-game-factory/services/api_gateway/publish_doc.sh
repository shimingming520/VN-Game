#!/bin/bash
# 手动推送 swagger.json 到文档中心
set -e

DOC_CENTER_HOST="http://172.16.30.31:30481"
PROJECT_CODE="agent1"
DOC_FILE="$(dirname "$0")/docs/swagger.json"
VERSION=$(git describe --tags --always 2>/dev/null || echo "latest")
COMMIT_ID=$(git rev-parse --short HEAD 2>/dev/null || echo "")

if [ ! -f "$DOC_FILE" ]; then
    echo "未找到 $DOC_FILE"
    exit 1
fi

PAYLOAD=$(jq -n \
  --arg pc "$PROJECT_CODE" \
  --arg ct "$(cat "$DOC_FILE")" \
  --arg ver "$VERSION" \
  --arg cid "$COMMIT_ID" \
  '{
    projectCode: $pc,
    content: $ct,
    contentFormat: "json",
    version: $ver,
    commitId: $cid
  }')

RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" \
  "${DOC_CENTER_HOST}/api/spec-upload")

HTTP_STATUS=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [ "$HTTP_STATUS" -eq 200 ]; then
    echo "文档发布成功"
    echo "$BODY"
else
    echo "发布失败: HTTP $HTTP_STATUS"
    echo "$BODY"
    exit 1
fi
