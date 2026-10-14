#!/usr/bin/env python3
"""简单测试API Gateway"""
import requests
import time

API_URL = "http://localhost:8000"

print("=" * 60)
print("测试 API Gateway")
print("=" * 60)

# 1. 健康检查
print("\n1. 健康检查...")
try:
    r = requests.get(f"{API_URL}/health", timeout=5)
    print(f"✅ 健康检查: {r.json()}")
except Exception as e:
    print(f"❌ 连接失败: {e}")
    print("请先启动API Gateway: docker-compose up api_gateway")
    exit(1)

# 2. 上传小说
print("\n2. 上传小说...")
try:
    with open("storage/raw_novels/douluodalu.txt", "rb") as f:
        files = {"novels": ("douluodalu.txt", f, "text/plain")}
        data = {"output_language": "zh-CN"}
        r = requests.post(f"{API_URL}/api/v1/novels/batch", files=files, data=data, timeout=30)

    if r.status_code == 200:
        result = r.json()
        print(f"✅ 上传成功: {result['message']}")
        task_id = result['tasks'][0]['task_id']
        print(f"   Task ID: {task_id}")

        # 3. 查询状态
        print("\n3. 查询任务状态...")
        time.sleep(2)
        r = requests.get(f"{API_URL}/api/v1/tasks/{task_id}/status", timeout=10)
        status = r.json()
        print(f"   进度: {status['progress_percentage']}%")
        print(f"   状态: {status['overall_status']}")
        print(f"   各阶段: {status['stages']}")

        print("\n✅ 测试完成！")
    else:
        print(f"❌ 上传失败: {r.status_code} - {r.text}")

except FileNotFoundError:
    print("❌ 找不到测试文件: storage/raw_novels/douluodalu.txt")
except Exception as e:
    print(f"❌ 测试失败: {e}")
    import traceback
    traceback.print_exc()
