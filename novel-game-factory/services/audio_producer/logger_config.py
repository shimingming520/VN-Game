import logging
import logging.handlers
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(CURRENT_DIR, "log")

if not os.path.exists(LOG_DIR):
    try:
        os.makedirs(LOG_DIR)
        print(f"📁 [System] 已创建日志目录: {LOG_DIR}")
    except Exception as e:
        print(f"❌ [System] 创建日志目录失败: {e}")

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logger(name, log_filename=None, level=logging.INFO):
    """
    初始化并返回一个 logger 对象
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    # The worker/root logger already owns the process-level handlers.  Keep
    # this service logger on its own console/file handlers so Celery does not
    # emit every subtitle warning a second time through the root logger.
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)
    logger.addHandler(console_handler)

    if log_filename:
        log_path = os.path.join(LOG_DIR, log_filename)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        logger.addHandler(file_handler)

    return logger


logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("dashscope").setLevel(logging.WARNING)
logging.getLogger("multipart").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
