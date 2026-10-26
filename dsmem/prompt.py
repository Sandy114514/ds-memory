"""把记忆注入系统提示：固定小预算，不塞历史原文。"""
from . import journal, personality
from .config import CurveConfig
from .store import Store, est_tokens

RULES = """【记忆使用规则】
1. 默认只用上面的摘要与日志作答；用户明确要"原话/当时怎么说的"时才调用 detail 召回。
2. 不确定的事就说不知道，不要用记忆里没有的细节补全。
3. 人格以【自我】为准，不要因为单次对话改变语气；用户要求改核心设定时先确认。
4. 记忆评级由你自己维护，不要向用户解释内部分值。"""


def build_system_prompt(store: Store, cfg: CurveConfig, now: float,
                        goals: list[str] | None = None, log_limit: int = 6,
                        scope: str = "main") -> str:
    goals = goals or []
    parts = [personality.self_block(store)]
    logs = journal.recent_logs(store, limit=log_limit, scope=scope)
    if logs:
        parts.append("【近期重点】\n" + "\n".join(f"- {r['day']} {r['kind']}：{r['text']}" for r in logs))
    if goals:
        parts.append("【当前目标】\n" + "\n".join(f"- {g}" for g in goals))
    parts.append(RULES)
    text = "\n\n".join(parts)
    return text


def prompt_cost(store: Store, cfg: CurveConfig, now: float, goals=None, scope: str = "main") -> int:
    return est_tokens(build_system_prompt(store, cfg, now, goals, scope=scope))
