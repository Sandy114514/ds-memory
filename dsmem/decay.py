"""遗忘曲线与分层迁移规则。

R(t) = 0.5 ** (Δt / S)
S 由 salience 决定初始值（越重要忘得越慢），每次成功召回按间隔效应增长：
S' = S * (1 + boost * (1 - R))
R 越小（越接近遗忘边缘）被回忆起来，增益越大 —— 这就是"间隔重复"。
"""
from .config import CurveConfig


def initial_stability(salience: float, cfg: CurveConfig, core: bool = False) -> float:
    base = cfg.half_life_base_h + cfg.half_life_gain_h * (max(0.0, salience) ** cfg.salience_gamma)
    if core:
        base *= cfg.core_multiplier
    return min(base, cfg.max_stability_h)


def retrievability(stability_h: float, last_review: float, now: float) -> float:
    dt_h = max(0.0, (now - last_review) / 3600.0)
    if stability_h <= 0:
        return 0.0
    return 0.5 ** (dt_h / stability_h)


def reinforce(stability_h: float, r: float, cfg: CurveConfig) -> float:
    return min(cfg.max_stability_h, stability_h * (1.0 + cfg.retrieval_boost * (1.0 - r)))


def decay_stability(stability_h: float, factor: float = 0.985) -> float:
    """长期未被触碰时的缓慢侵蚀，防止"永不忘"的僵尸记忆。"""
    return max(1.0, stability_h * factor)


def decide_tier(salience: float, cfg: CurveConfig) -> str:
    """nightly 归档决策：discard / l1 / l2"""
    if salience >= cfg.t_core:
        return "l2"
    if salience >= cfg.t_discard:
        return "l1"
    return "discard"


def next_state(state: str, r: float, cfg: CurveConfig, age_days: float) -> str:
    """L1 降级状态机：full -> skeleton -> cold -> (delete 由 gc 判定)"""
    if state == "full" and r < cfg.r_skeleton:
        return "skeleton"
    if state == "skeleton" and r < cfg.r_delete:
        return "cold"
    if state == "cold" and r < cfg.r_delete * 0.5 and age_days > cfg.delete_after_days:
        return "dead"
    return state


def should_delete(state: str, r: float, cfg: CurveConfig, age_days: float, pinned: int) -> bool:
    if pinned:
        return False
    return state in ("cold", "dead") and r < cfg.r_delete and age_days > cfg.delete_after_days


def review_interval_h(stability_h: float, target_r: float = 0.85) -> float:
    """下次主动回顾的时间点（空闲期任务会用到）。"""
    import math
    return stability_h * (math.log(target_r) / math.log(0.5))
