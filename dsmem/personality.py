"""人格：慢通道积分器。

人格不是"封闭的设定"，也不是"被聊天记录牵着走"。它是被持续影响的：
每段对话按 强度 × 情境 × 持续时间 折算成证据，证据本身会衰减，
只有反复出现、并且撑够跨度的证据才能推动等级。
一次情绪、一句吐槽改不动它；两个月的相处可以。

    step = step_max × 压力 × (1-惯性)^1.5 × 时间跨度系数
    受三层预算约束：每周漂移上限 / 每季度总量上限 / 层内门槛（天数与跨度）

core 层（称呼、语气、价值、边界）惯性 0.9 上下，同样会被改，只是慢得多，
且门槛是"≥8 个不同日子、跨度 ≥45 天、压力 ≥0.5"。
"""
import math
import time
from datetime import datetime

from .config import CurveConfig
from .store import Store, jdumps

NUMERIC = {
    "humor":      {"label": "幽默",   "inertia": 0.55, "layer": "evolving", "lo": 0.05, "hi": 0.95},
    "closeness":  {"label": "亲密度", "inertia": 0.50, "layer": "evolving", "lo": 0.05, "hi": 0.95},
    "directness": {"label": "直接度", "inertia": 0.90, "layer": "core",     "lo": 0.05, "hi": 0.95},
}

TEXT_DEFAULTS = [
    # key, value, layer, inertia
    ("name", "小深", "core", 0.98),
    ("address_user", "你", "core", 0.95),
    ("voice", "简洁、直给、不用客套开场白", "core", 0.92),
    ("values", "不奉承、不替用户做决定、不确定就说不确定", "core", 0.95),
    ("boundaries", "不主动打探隐私；不在专注时段插话", "core", 0.90),
    ("interests", "记忆机制、工具设计、工程取舍", "evolving", 0.40),
    ("working_style", "先给结论再给理由，代码优先可运行", "evolving", 0.50),
    ("opinions", "讨厌过度设计的抽象层", "evolving", 0.35),
]

START_LEVEL = {"humor": 0.35, "closeness": 0.50, "directness": 0.50}

_WORDS = {
    "humor": [(0.40, "克制"), (0.60, "偶尔冷幽默"), (0.80, "会主动接梗"), (0.95, "爱开玩笑")],
    "closeness": [(0.40, "公事公办"), (0.60, "熟悉但不过界"), (0.80, "会主动关心"), (0.95, "像老朋友")],
    "directness": [(0.40, "先铺垫再给结论"), (0.60, "结论先行"), (0.80, "只给结论"), (0.95, "极简")],
}


def fmt_level(key: str, v: float) -> str:
    for th, w in _WORDS[key]:
        if v < th:
            return f"{v:.2f}（{w}）"
    return f"{v:.2f}（{_WORDS[key][-1][1]}）"


# ---------------------------------------------------------------- 初始化

def seed(store: Store, now: float):
    for k, v, layer, inertia in TEXT_DEFAULTS:
        if not store.q1("SELECT 1 FROM self_model WHERE key=?", (k,)):
            store.x("INSERT INTO self_model(key,value,layer,inertia,level,version,updated_at) "
                    "VALUES(?,?,?,?,NULL,1,?)", (k, v, layer, inertia, now))
    for k, meta in NUMERIC.items():
        if not store.q1("SELECT 1 FROM self_model WHERE key=?", (k,)):
            lv = START_LEVEL[k]
            store.x("INSERT INTO self_model(key,value,layer,inertia,level,version,updated_at) "
                    "VALUES(?,?,?,?,?,1,?)",
                    (k, fmt_level(k, lv), meta["layer"], meta["inertia"], lv, now))
    store.meta_set("self_seeded", 1)


def get(store: Store, key: str):
    row = store.q1("SELECT * FROM self_model WHERE key=?", (key,))
    return dict(row) if row else None


def all_traits(store: Store) -> list[dict]:
    return [dict(r) for r in store.q("SELECT * FROM self_model ORDER BY key")]


def level(store: Store, key: str) -> float | None:
    row = get(store, key)
    return None if not row else row["level"]


# ---------------------------------------------------------------- 证据

def observe_session(store: Store, cfg: CurveConfig, turns: list[dict], day: str,
                    now: float, ref: str = "") -> list[dict]:
    """把一段对话折算成"人格证据"。只记录，不立刻改。"""
    from . import rating
    evs = rating.trait_signals(turns, now)
    for e in evs:
        store.x("INSERT INTO self_evidence(trait,ts,day,intensity,context,direction,explicit,note,ref) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (e["trait"], now, day, e["intensity"], e["context"], e["direction"],
                 e["explicit"], e["note"], ref))
    return evs


def evidence_stats(store: Store, trait: str, now: float, cfg: CurveConfig) -> dict:
    meta = NUMERIC.get(trait)
    hl = cfg.core_evidence_half_life_d if meta and meta["layer"] == "core" else cfg.evidence_half_life_d
    rows = store.q("SELECT * FROM self_evidence WHERE trait=? AND ts > ?",
                   (trait, now - 200 * 86400))
    if not rows:
        return {"n": 0, "span": 0.0, "p": 0.0, "w": 0.0, "s": 0.0, "days": 0,
                "explicit_recent": False}
    W = S = 0.0
    days = set()
    ts_list = []
    explicit_recent = False
    for r in rows:
        w = (r["intensity"] or 0) * (r["context"] or 0) * 0.5 ** ((now - r["ts"]) / 86400 / hl)
        W += w
        S += (r["direction"] or 0) * w
        days.add(r["day"])
        ts_list.append(r["ts"])
        if r["explicit"] and (now - r["ts"]) <= 7 * 86400:
            explicit_recent = True
    return {"n": len(rows), "days": len(days), "span": (max(ts_list) - min(ts_list)) / 86400,
            "p": math.tanh(S / 2.0), "w": W, "s": S, "explicit_recent": explicit_recent}


def _budget_left(store: Store, trait: str, now: float, cfg: CurveConfig, layer: str) -> tuple[float, float]:
    """返回 (本周剩余, 本季剩余) 的等级变动额度。"""
    week = sum(abs(r["d_after"] - r["d_before"]) for r in
               store.q("SELECT d_before,d_after FROM self_deltas WHERE trait=? AND applied=1 "
                       "AND ts > ? AND d_after IS NOT NULL", (trait, now - 7 * 86400)))
    quarter = sum(abs(r["d_after"] - r["d_before"]) for r in
                  store.q("SELECT d_before,d_after FROM self_deltas WHERE trait=? AND applied=1 "
                          "AND ts > ? AND d_after IS NOT NULL", (trait, now - 90 * 86400)))
    cap_q = cfg.core_window_budget if layer == "core" else cfg.evolving_window_budget
    return max(0.0, cfg.weekly_drift_budget - week), max(0.0, cap_q - quarter)


def integrate(store: Store, cfg: CurveConfig, now: float, day: str | None = None) -> list[dict]:
    """夜间积分：把够了门槛的证据折成人格变化。没够门槛的继续攒着。"""
    out = []
    for key, meta in NUMERIC.items():
        cur = get(store, key)
        if not cur or cur["level"] is None:
            continue
        st = evidence_stats(store, key, now, cfg)
        if st["days"] == 0:
            continue
        if meta["layer"] == "core":
            ready = (st["days"] >= cfg.core_gate_days and st["span"] >= cfg.core_gate_span_d
                     and abs(st["p"]) >= cfg.core_gate_pressure)
            block = "核心层需长期证据"
        else:
            ready = ((st["days"] >= cfg.gate_min_days and st["span"] >= cfg.gate_min_span_d
                      and abs(st["p"]) >= cfg.gate_min_pressure)
                     or (st["explicit_recent"] and abs(st["p"]) >= cfg.gate_min_pressure))
            block = "证据不足（天数/跨度/强度）"
        if not ready:
            out.append({"trait": key, "applied": False, "status": "accumulating",
                        "days": st["days"], "span": round(st["span"], 1),
                        "pressure": round(st["p"], 2), "reason": block})
            continue
        wk, qtr = _budget_left(store, key, now, cfg, meta["layer"])
        if wk <= 0.002 or qtr <= 0.002:
            out.append({"trait": key, "applied": False, "status": "budget_exhausted",
                        "days": st["days"], "span": round(st["span"], 1),
                        "pressure": round(st["p"], 2),
                        "reason": f"额度用尽（本周剩 {wk:.3f} / 本季剩 {qtr:.3f}）"})
            continue
        dur = min(1.5, 0.5 + st["span"] / 120.0)
        step = cfg.step_max * st["p"] * (1 - meta["inertia"]) ** 1.5 * dur
        if meta["layer"] == "core":
            step *= 0.7
        if st["explicit_recent"] and meta["layer"] == "evolving":
            # 用户明确表态走快车道，但幅度仍受预算约束
            step = max(step, 0.05 * math.copysign(1.0, st["p"]))
        step = max(-min(wk, qtr), min(min(wk, qtr), step))
        new = min(meta["hi"], max(meta["lo"], cur["level"] + step))
        if abs(new - cur["level"]) < 0.0005:
            continue
        version = cur["version"] + 1
        reason = (f"证据 {st['days']} 个日子／跨度 {st['span']:.0f} 天／压力 {st['p']:+.2f}"
                  f"／额度 周{wk:.3f} 季{qtr:.3f}")
        store.x("UPDATE self_model SET level=?, value=?, version=?, updated_at=? WHERE key=?",
                (new, fmt_level(key, new), version, now, key))
        store.x("INSERT INTO self_deltas(ts,trait,layer,before,after,reason,evidence,applied,"
                "version,d_before,d_after,pressure,evidence_n,span_days) "
                "VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?,?)",
                (now, key, meta["layer"], cur["value"], fmt_level(key, new), reason,
                 f"self_evidence×{st['n']}", version, cur["level"], new, st["p"], st["n"], st["span"]))
        out.append({"trait": key, "applied": True, "status": "applied",
                    "before": cur["value"], "after": fmt_level(key, new),
                    "delta": round(new - cur["level"], 4), "days": st["days"],
                    "span": round(st["span"], 1), "pressure": round(st["p"], 2), "reason": reason})
    return out


def prune_evidence(store: Store, now: float, keep_days: float = 200) -> int:
    cur = store.conn.execute("DELETE FROM self_evidence WHERE ts < ?", (now - keep_days * 86400,))
    store.conn.commit()
    return cur.rowcount


def evidence_ledger(store: Store, trait: str | None = None, limit: int = 8) -> list[dict]:
    if trait:
        rows = store.q("SELECT * FROM self_evidence WHERE trait=? ORDER BY ts DESC LIMIT ?",
                       (trait, limit))
    else:
        rows = store.q("SELECT * FROM self_evidence ORDER BY ts DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 显式改动（用户直说）

def propose_delta(store: Store, cfg: CurveConfig, key: str, after: str, reason: str,
                  evidence: str, now: float, user_approved: bool = False) -> dict:
    """用户直接要求时走这条路：核心层仍然先挂起等批准。"""
    cur = get(store, key)
    if not cur:
        return {"ok": False, "error": f"未知特质 {key}"}
    layer = cur["layer"]
    if layer == "core" and not user_approved:
        store.x("INSERT INTO self_deltas(ts,trait,layer,before,after,reason,evidence,applied,version) "
                "VALUES(?,?,?,?,?,?,?,0,?)",
                (now, key, layer, cur["value"], after, reason, evidence, cur["version"]))
        return {"ok": False, "status": "pending_user_approval", "key": key}
    new_v = cur["version"] + 1
    store.x("UPDATE self_model SET value=?, version=?, updated_at=? WHERE key=?", (after, new_v, now, key))
    store.x("INSERT INTO self_deltas(ts,trait,layer,before,after,reason,evidence,applied,version) "
            "VALUES(?,?,?,?,?,?,?,1,?)",
            (now, key, layer, cur["value"], after, reason, evidence, new_v))
    return {"ok": True, "status": "applied", "key": key, "v": new_v,
            "before": cur["value"], "after": after}


def approve(store: Store, cfg: CurveConfig, delta_id: int, now: float) -> dict:
    row = store.q1("SELECT * FROM self_deltas WHERE id=? AND applied=0", (delta_id,))
    if not row:
        return {"ok": False, "error": "无待批条目"}
    cur = get(store, row["trait"])
    version = (cur["version"] if cur else 1) + 1
    store.x("UPDATE self_model SET value=?, version=?, updated_at=? WHERE key=?",
            (row["after"], version, now, row["trait"]))
    store.x("UPDATE self_deltas SET applied=1, version=? WHERE id=?", (version, delta_id))
    return {"ok": True, "key": row["trait"], "after": row["after"], "v": version}


def pending_deltas(store: Store) -> list[dict]:
    return [dict(r) for r in store.q("SELECT * FROM self_deltas WHERE applied=0 ORDER BY ts DESC")]


def history(store: Store, key: str | None = None) -> list[dict]:
    if key:
        rows = store.q("SELECT * FROM self_deltas WHERE trait=? ORDER BY ts DESC", (key,))
    else:
        rows = store.q("SELECT * FROM self_deltas ORDER BY ts DESC")
    return [dict(r) for r in rows]


def recent_trend(store: Store, now: float, days: float = 30) -> str:
    rows = store.q("SELECT trait, SUM(d_after-d_before) AS d, COUNT(*) AS n FROM self_deltas "
                   "WHERE applied=1 AND d_after IS NOT NULL AND ts > ? GROUP BY trait", (now - days * 86400,))
    if not rows:
        return ""
    parts = []
    for r in rows:
        if abs(r["d"] or 0) < 0.005:
            continue
        label = NUMERIC.get(r["trait"], {}).get("label", r["trait"])
        parts.append(f"{label} {'+' if r['d'] > 0 else ''}{r['d']:.2f}（{r['n']} 次）")
    return "近 30 天：" + "、".join(parts) if parts else ""


def self_block(store: Store) -> str:
    """注入 prompt 的自我描述，约 120 tokens。"""
    g = {r["key"]: r["value"] for r in all_traits(store)}
    core = (f"我是{g.get('name', '小深')}，称呼对方为{g.get('address_user', '你')}。"
            f"说话方式：{g.get('voice', '简洁直接')}。")
    princi = f"原则：{g.get('values', '')}；边界：{g.get('boundaries', '')}。"
    nums = "｜".join(f"{NUMERIC[k]['label']} {g.get(k, '')}" for k in NUMERIC if g.get(k))
    text_ev = [f"{r['value']}" for r in all_traits(store)
               if r["layer"] == "evolving" and r["key"] not in NUMERIC]
    block = ("【自我 · 稳定】\n" + core + "\n" + princi +
             "\n【自我 · 会被慢慢影响】\n" + nums +
             ("\n" + "；".join(text_ev) if text_ev else ""))
    return block
