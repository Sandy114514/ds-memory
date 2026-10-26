"""记忆曲线与分层策略的全部可调参数。

设计要点：用户只在最初的人为 setting 里回答 4 个"倾向"问题（忘多快、
细节多重要、日记什么风格、人格多强），其余数值全部由本模块推导，
用户不必懂任何公式。空间不设上限。
"""
from dataclasses import dataclass, asdict
import json


@dataclass
class UserNeeds:
    """人为 setting 阶段的输入：只表达倾向，不给具体参数。"""
    forget: float = 0.35          # 0=几乎不忘, 1=非常快忘
    detail: float = 0.60          # 0=只要结论, 1=要紧到原话
    journal: str = "散文"          # 散文 | 要点 | 意识流 | 极简
    personality_strength: float = 0.75   # 0=中性助手, 1=强烈稳定人设


@dataclass
class CurveConfig:
    """由 UserNeeds 推导出来的运行参数。"""
    # --- 遗忘曲线 ---
    half_life_base_h: float = 6.0
    half_life_gain_h: float = 240.0
    salience_gamma: float = 1.6
    core_multiplier: float = 2.4
    retrieval_boost: float = 1.4
    max_stability_h: float = 4000.0
    # --- 分层阈值 ---
    t_discard: float = 0.35
    t_core: float = 0.68
    # --- 降级 / 清理 ---
    r_skeleton: float = 0.25
    r_delete: float = 0.08
    delete_after_days: float = 120.0
    core_cold_days: float = 180.0
    l0_ttl_h: float = 36.0
    # --- 人格演化（慢通道）---
    evidence_half_life_d: float = 12.0        # 演化层：证据本身的衰减半衰期
    core_evidence_half_life_d: float = 45.0   # 核心层：证据记得更久
    step_max: float = 0.075                   # 单次夜间最大变动
    weekly_drift_budget: float = 0.044        # 每周等级变动总额度
    evolving_window_budget: float = 0.25      # 每 90 天最多变多少（演化层）
    core_window_budget: float = 0.10          # 每 90 天最多变多少（核心层）
    gate_min_days: int = 3                    # 演化层门槛：不同天数
    gate_min_span_d: float = 7.0              # 演化层门槛：时间跨度
    gate_min_pressure: float = 0.12           # 演化层门槛：压力
    core_gate_days: int = 8                   # 核心层门槛：不同天数
    core_gate_span_d: float = 45.0            # 核心层门槛：时间跨度
    core_gate_pressure: float = 0.5           # 核心层门槛：压力
    # --- 人格（计数）---
    drift_cap_per_week: int = 3
    # --- 召回 ---
    recall_budget_tokens: int = 700
    journal_style: str = "散文"
    personality_strength: float = 0.75
    # --- 碎碎念：0 = 跟随人格自动；>0 = 用户手动指定 ---
    murmur_per_day: float = 0.0     # 每天条数
    murmur_length: int = 0          # 单条字数上限

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


PRESETS = {
    "工具人": UserNeeds(forget=0.85, detail=0.25, journal="极简", personality_strength=0.25),
    "长期伴侣": UserNeeds(forget=0.25, detail=0.60, journal="散文", personality_strength=0.85),
    "工作助理": UserNeeds(forget=0.50, detail=0.90, journal="要点", personality_strength=0.40),
    "创作缪斯": UserNeeds(forget=0.35, detail=0.70, journal="意识流", personality_strength=0.90),
}


def build_config(needs: UserNeeds) -> CurveConfig:
    forget, detail = needs.forget, needs.detail
    return CurveConfig(
        half_life_gain_h=120.0 + 260.0 * (1.0 - forget),
        core_multiplier=1.5 + 2.0 * detail,
        t_discard=0.24 + 0.34 * forget,
        t_core=0.62 + 0.20 * detail,
        delete_after_days=20.0 + 220.0 * (1.0 - forget),
        r_delete=0.06 + 0.14 * forget,
        recall_budget_tokens=320 + int(600 * detail),
        journal_style=needs.journal,
        personality_strength=needs.personality_strength,
        drift_cap_per_week=max(1, int(1 + 4 * needs.personality_strength)),
        # 人设越强 → 越难被改：步长和预算同时收紧
        step_max=round(0.10 - 0.03 * needs.personality_strength, 4),
        weekly_drift_budget=round(0.03 + 0.09 * (1.0 - needs.personality_strength), 4),
        evolving_window_budget=round(0.34 - 0.10 * needs.personality_strength, 3),
        core_window_budget=round(0.14 - 0.05 * needs.personality_strength, 3),
    )


def config_for_preset(name: str = "长期伴侣") -> CurveConfig:
    return build_config(PRESETS.get(name, PRESETS["长期伴侣"]))
