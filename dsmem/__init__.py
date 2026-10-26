"""dsmem —— 可配置记忆曲线 + 三层记忆 + 自演化评级的插件式记忆内核。"""
__version__ = "1.0.0"

from .config import CurveConfig, UserNeeds, build_config, config_for_preset, PRESETS  # noqa: F401
from .store import Store  # noqa: F401
