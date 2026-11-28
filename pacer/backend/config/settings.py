"""项目全局配置"""

import os

# MuMu模拟器ADB连接配置（模拟器2 - 育成用）
ADB_HOST = "127.0.0.1"
ADB_PORT = int(os.environ.get("UMA_ADB_PORT", "16384"))  # 默认大号；小号实验机设 UMA_ADB_PORT=16416
ADB_ENDPOINT = f"{ADB_HOST}:{ADB_PORT}"
ADB_PATH = os.environ.get("UMA_ADB_PATH", "")

# 模拟器分辨率（竖屏1440x2560，游戏中会自动旋转为横屏2560x1440）
SCREEN_WIDTH = 1440
SCREEN_HEIGHT = 2560

# 截图保存路径
SCREENSHOT_DIR = "logs/screenshots"

# 操作间隔（秒）
TAP_DELAY = 0.3        # 点击后等待
SWIPE_DELAY = 0.5      # 滑动后等待
ACTION_DELAY = 1.0     # 操作间等待
LONG_WAIT = 3.0        # 加载/过场等待


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "off", "no", ""}


# 第二路只补主识别未接受的数值；留档独立于参与判定开关。
NUMERIC_OCR_CROSSCHECK = _flag("UMA_NUMERIC_OCR_CROSSCHECK", True)
NUMERIC_OCR_ARCHIVE = _flag("UMA_NUMERIC_OCR_ARCHIVE", True)
NUMERIC_OCR_ARCHIVE_DIR = os.environ.get("UMA_NUMERIC_OCR_ARCHIVE_DIR", "logs/numeric_ocr")
NUMERIC_OCR_SECOND_OPINION_MIN_SCORE = os.environ.get("UMA_NUMERIC_OCR_MIN_SCORE", "0.70")
