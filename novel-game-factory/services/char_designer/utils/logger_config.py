# logger_config.py
import logging
import logging.handlers
import os
import sys

# ================= 配置区域 =================

# 1. 确定日志目录 (项目根目录的 log 文件夹)
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)  # 项目根目录
LOG_DIR = os.path.join(PROJECT_ROOT, "log")

if not os.path.exists(LOG_DIR):
    try:
        os.makedirs(LOG_DIR)
        print(f"📁 [System] 已创建日志目录: {LOG_DIR}")
    except Exception as e:
        print(f"❌ [System] 创建日志目录失败: {e}")

# 全局格式
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ================= 核心工厂函数 =================

def setup_logger(name, log_filename=None, level=logging.INFO):
    """
    初始化并返回一个 logger 对象
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # 1. 控制台输出
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)
    logger.addHandler(console_handler)

    # 2. 文件输出 (自动切割)
    if log_filename:
        log_path = os.path.join(LOG_DIR, log_filename)
        # 10MB 切割，保留 5 个备份
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        logger.addHandler(file_handler)

    return logger

# ================= 屏蔽第三方库噪声 =================
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("dashscope").setLevel(logging.WARNING)
logging.getLogger("multipart").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
