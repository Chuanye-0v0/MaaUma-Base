"""Mimo视觉模型客户端 - 用于截图分析和数据提取"""

import os
import json
import re
import base64
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class MimoClient:
    """调用mimo2.5模型进行图像分析"""

    def __init__(self):
        self.api_key = os.environ.get("MIMO_API_KEY", "")
        self.api_base = os.environ.get("MIMO_API_BASE", "https://token-plan-cn.xiaomimimo.com/anthropic")
        self.model = os.environ.get("MIMO_MODEL", "mimo-v2.5")

    def _call_api(self, image_data_b64: str, prompt: str) -> str:
        """底层 API 调用，返回 TextBlock 内容。

        只提取 TextBlock，跳过 ThinkingBlock。
        如果没有 TextBlock，返回空字符串并记录 warning。
        """
        if not self.api_key:
            logger.warning("MIMO_API_KEY 未设置，跳过视觉分析")
            return ""
        try:
            import anthropic

            client = anthropic.Anthropic(
                api_key=self.api_key,
                base_url=self.api_base
            )

            message = client.messages.create(
                model=self.model,
                max_tokens=4096,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": image_data_b64,
                                },
                            },
                            {
                                "type": "text",
                                "text": prompt
                            }
                        ],
                    }
                ],
            )

            # 只提取 TextBlock，跳过 ThinkingBlock
            result_text = ""
            has_thinking = False
            for block in message.content:
                if hasattr(block, 'text'):
                    result_text += block.text
                elif hasattr(block, 'thinking'):
                    has_thinking = True

            if not result_text.strip():
                if has_thinking:
                    logger.warning("MIMO 返回仅含 ThinkingBlock，无 TextBlock")
                else:
                    logger.warning("MIMO 返回空内容")

            return result_text

        except ImportError:
            logger.warning("需要安装anthropic: pip install anthropic")
            return ""
        except Exception as e:
            logger.warning(f"MIMO 调用失败: {e}")
            return ""

    def analyze_image(self, image_path: str, prompt: str) -> str:
        """分析图片文件并返回结果"""
        try:
            with open(image_path, "rb") as f:
                image_data = base64.standard_b64encode(f.read()).decode("utf-8")
            return self._call_api(image_data, prompt)
        except FileNotFoundError:
            logger.warning(f"图片文件不存在: {image_path}")
            return ""

    def analyze_image_base64(self, image_b64: str, prompt: str) -> str:
        """分析 base64 编码的图片并返回结果（用于裁剪区域）"""
        return self._call_api(image_b64, prompt)

    def extract_json_from_text(self, text: str) -> dict:
        """从文本中提取JSON"""
        try:
            # 尝试直接解析
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试从文本中提取JSON
        json_patterns = [
            r'\{[^{}]+\}',  # 简单JSON对象
            r'\{.*?\}',  # 更宽松的匹配
        ]

        for pattern in json_patterns:
            matches = re.findall(pattern, text, re.DOTALL)
            for match in matches:
                try:
                    return json.loads(match)
                except json.JSONDecodeError:
                    continue

        return {}

    def extract_training_data(self, image_path: str) -> dict:
        """从训练界面截图提取结构化数据"""
        prompt = """这是赛马娘训练界面截图。请提取以下信息并输出JSON格式：

{
    "训练名称": "速度/耐力/力量/毅力/智力",
    "人数": 数字(支援卡和马娘数量),
    "加成": {"速度": 数字, "耐力": 数字, "力量": 数字, "毅力": 数字, "智力": 数字},
    "体力消耗": 数字,
    "友情训练": true/false,
    "碎片类型": "速度/耐力/力量/毅力/智力/技能点",
    "失败率": 数字百分比
}

只输出JSON，不要其他文字。"""

        result = self.analyze_image(image_path, prompt)
        return self.extract_json_from_text(result)

    def extract_game_state(self, image_path: str) -> dict:
        """从游戏截图提取整体状态"""
        prompt = """这是赛马娘游戏截图。请提取以下信息并输出JSON格式：

{
    "界面类型": "训练选择/事件/技能学习/比赛/其他",
    "回合": 数字,
    "阶段": "初级出道前/经典级/...",
    "体力": 数字百分比,
    "状态": "上佳/普通/不佳",
    "距比赛回合": 数字,
    "下次比赛": "比赛名称",
    "碎片进度": [速度碎片数, 耐力碎片数, 力量碎片数, 毅力碎片数, 智力碎片数, 技能点碎片数],
    "女神等级": {"蓝": 数字, "红": 数字, "黄": 数字}
}

只输出JSON，不要其他文字。如果某个信息看不清，用null表示。"""

        result = self.analyze_image(image_path, prompt)
        return self.extract_json_from_text(result)


# 单例
_mimo_client = None


def get_mimo_client() -> MimoClient:
    global _mimo_client
    if _mimo_client is None:
        _mimo_client = MimoClient()
    return _mimo_client
