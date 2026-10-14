#!/bin/bash
# 快速启动/重启游戏服务器

cd "$(dirname "$0")"

echo "=========================================="
echo "游戏服务器管理脚本"
echo "=========================================="

# 检查配置文件
if [ ! -f "storage/built_games/nginx.conf" ]; then
    echo "❌ 错误: 统一配置文件不存在"
    echo "   请先运行游戏构建脚本生成配置文件"
    exit 1
fi

# 统计游戏数量
game_count=$(grep -c "# GAME_START:" storage/built_games/nginx.conf)
echo "📊 当前配置包含 $game_count 个游戏"

# 列出游戏
echo ""
echo "🎮 游戏列表:"
grep "# GAME_START:" storage/built_games/nginx.conf | sed 's/.*# GAME_START: /  - /'

# 检查服务状态
echo ""
echo "🔍 检查服务状态..."
if docker-compose ps game-server | grep -q "Up"; then
    echo "✅ 服务正在运行"

    # 询问是否重启
    read -p "是否重启服务以加载最新配置? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "🔄 重启服务..."
        docker-compose restart game-server
        echo "✅ 服务已重启"
    fi
else
    echo "⚠️  服务未运行"

    # 询问是否启动
    read -p "是否启动服务? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "🚀 启动服务..."
        docker-compose up -d game-server
        echo "✅ 服务已启动"
    fi
fi

# 显示访问信息
echo ""
echo "=========================================="
echo "🌐 访问地址:"
echo "=========================================="
grep "# GAME_START:" storage/built_games/nginx.conf | sed 's/.*# GAME_START: //' | while read game; do
    echo "  http://localhost:8080/$game/"
done

echo ""
echo "📝 查看日志: docker-compose logs -f game-server"
echo "🛑 停止服务: docker-compose stop game-server"
echo ""
