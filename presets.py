"""面向 Python 配置文件的推荐预置入口。"""

from AIClientCenter.core.preset import ClientPreset, compose_presets
from AIClientCenter.providers.gemini_free import gemini_free
from AIClientCenter.providers.groq import groq_free
from AIClientCenter.providers.nvidia import nvidia_free

__all__ = [
    "ClientPreset",
    "compose_presets",
    "gemini_free",
    "groq_free",
    "nvidia_free",
]
