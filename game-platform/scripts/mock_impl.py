"""Mock 实现:注册自己 + 消费 Job + 假装干活 + 回调。联调用。

用法:
    python scripts/mock_impl.py char_designer group-b        # 单能力
    python scripts/mock_impl.py --all group-ref              # 一进程跑全 7 能力(线程)
可选: --fail-once 首次收到 Job 回 failed(retryable) 验证重试
"""
import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from orchestrator.dag import DEFAULT_DAG
from protocol.sdk import ImplWorker

PLATFORM_API = "http://localhost:8000"

_failed_once: set = set()


def make_handler(capability: str, fail_once: bool):
    def handle(job: dict, report_progress):
        key = job["step_id"]
        if fail_once and key not in _failed_once:
            _failed_once.add(key)
            raise RuntimeError("mock transient failure (testing retry)")
        report_progress(30, f"mock {capability} 干活中")
        time.sleep(1)
        report_progress(80, f"mock {capability} 快好了")
        return {
            "artifacts": [{
                "role": f"{capability}_result",
                "oss_key": job["output"]["oss_prefix"] + "mock_result.json",
            }],
            "summary": {"mock": True},
            "usage": [{
                "api_type": "llm", "model": "mock-model",
                "prompt_tokens": 1000, "completion_tokens": 200, "requests": 1,
            }],
        }
    return handle


def start_impl(capability: str, group: str, fail_once: bool):
    impl_id = f"{group}.{capability}.mock"
    queue = f"q.{group}.{capability}"
    r = requests.post(f"{PLATFORM_API}/api/impls", json={
        "group_name": group, "capability": capability,
        "impl_id": impl_id, "queue": queue,
        "manifest": {"mock": True}, "enabled": True,
    })
    r.raise_for_status()
    print(f"✅ registered {impl_id}")

    def beat():
        while True:
            try:
                requests.post(f"{PLATFORM_API}/api/impls/{impl_id}/heartbeat",
                              timeout=5)
            except Exception as e:
                print(f"heartbeat error: {e}")
            time.sleep(20)

    threading.Thread(target=beat, daemon=True).start()
    ImplWorker(queue=queue, impl_id=impl_id, group_name=group,
               capability=capability,
               handler=make_handler(capability, fail_once)).run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("capability", nargs="?")
    parser.add_argument("group", nargs="?", default="group-mock")
    parser.add_argument("--all", metavar="GROUP",
                        help="一个进程内跑全部 7 个能力的 mock")
    parser.add_argument("--fail-once", action="store_true")
    args = parser.parse_args()

    if args.all:
        caps = DEFAULT_DAG["nodes"]
        for cap in caps[:-1]:
            threading.Thread(
                target=start_impl, args=(cap, args.all, args.fail_once),
                daemon=True,
            ).start()
            time.sleep(0.3)
        start_impl(caps[-1], args.all, args.fail_once)  # 主线程占最后一个
    elif args.capability:
        start_impl(args.capability, args.group, args.fail_once)
    else:
        parser.error("需要 capability 参数或 --all GROUP")
