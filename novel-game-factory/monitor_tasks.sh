#!/bin/bash
# 实时监控任务链执行状态

echo "=========================================="
echo "任务链监控"
echo "=========================================="
echo ""

# 检查活跃任务
echo "📋 活跃任务:"
docker exec novel-game-factory-story_planner-1 celery -A common.celery_app inspect active 2>/dev/null | grep -E "(name|args|empty)" | head -20

echo ""
echo "=========================================="
echo ""

# 检查队列长度
echo "📊 队列状态:"
echo "  char_queue: $(docker exec novel-game-factory-redis-1 redis-cli LLEN char_queue 2>/dev/null)"
echo "  audio_queue: $(docker exec novel-game-factory-redis-1 redis-cli LLEN audio_queue 2>/dev/null)"
echo "  scene_queue: $(docker exec novel-game-factory-redis-1 redis-cli LLEN scene_queue 2>/dev/null)"
echo "  build_queue: $(docker exec novel-game-factory-redis-1 redis-cli LLEN build_queue 2>/dev/null)"

echo ""
echo "=========================================="
echo ""

# 显示最近的日志
echo "📝 最近日志 (char_designer):"
docker logs --since 1m novel-game-factory-char_designer-1 2>&1 | grep -E "(✅|❌|🎉|Step|succeeded|failed)" | tail -5

echo ""
echo "📝 最近日志 (audio_producer):"
docker logs --since 1m novel-game-factory-audio_producer-1 2>&1 | grep -E "(✅|❌|🎉|Step|succeeded|failed|received)" | tail -5

echo ""
echo "📝 最近日志 (scene_painter):"
docker logs --since 1m novel-game-factory-scene_painter-1 2>&1 | grep -E "(✅|❌|🎉|Step|succeeded|failed|received)" | tail -5

echo ""
echo "📝 最近日志 (game_builder):"
docker logs --since 1m novel-game-factory-game_builder-1 2>&1 | grep -E "(✅|❌|🎉|Step|succeeded|failed|received)" | tail -5

echo ""
echo "=========================================="
echo "提示: 使用 Ctrl+C 退出，或运行 'watch -n 5 ./monitor_tasks.sh' 自动刷新"
