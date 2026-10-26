"""评级系统：初始分级由模型自己生成，然后靠真实使用信号逐步校准。

关键点：用户只给"倾向"（忘多快 / 细节多重要），从不手工写权重。
bootstrap_rubric() 是模型侧的生成结果；heuristic 版只是离线复现，
真实运行时用 RUBRIC_PROMPT 让 DeepSeek 输出同样结构的 JSON。
"""
import math
import re
import time
from .config import CurveConfig, UserNeeds
from .store import Store, jdumps, jloads

DIMENSIONS = ["self_ref", "affect", "novelty", "commitment", "goal", "recurrence", "explicit"]

DIM_LABEL = {
    "self_ref": "自我指涉", "affect": "情绪强度", "novelty": "信息新颖",
    "commitment": "承诺/时限", "goal": "目标相关", "recurrence": "反复出现",
    "explicit": "明确要求记住",
}

RUBRIC_PROMPT = """你是记忆系统的评级器。用户给出的偏好只有这些：
- 遗忘倾向 forget={forget}（0=几乎不忘，1=很快忘）
- 细节需求 detail={detail}（0=只要结论，1=要原话）
- 人格强度 personality={personality}（0=中性助手，1=强烈人设）
请为以下 7 个维度生成一套权重（总和为 1.0），并给出每个维度的一句话判据：
self_ref 自我指涉 / affect 情绪强度 / novelty 信息新颖 / commitment 承诺时限 /
goal 目标相关 / recurrence 反复出现 / explicit 明确要求记住。
只输出 JSON：{"weights": {...}, "criteria": {...}, "notes": "..."}
不要照抄示例数字，要根据用户偏好推理出合理的倾斜。"""


def normalize(w: dict) -> dict:
    s = sum(max(0.0, v) for v in w.values()) or 1.0
    return {k: round(max(0.0, v) / s, 4) for k, v in w.items()}


def bootstrap_rubric(needs: UserNeeds) -> dict:
    """初始分级（v1）。真实实现 = 调模型；此处是确定性近似。"""
    w = {"self_ref": 0.22, "affect": 0.16, "novelty": 0.12, "commitment": 0.16,
         "goal": 0.14, "recurrence": 0.08, "explicit": 0.12}
    if needs.detail > 0.7:              # 要细节的人，承诺和时限优先
        w["commitment"] += 0.09
        w["goal"] += 0.05
        w["affect"] -= 0.03
    if needs.forget > 0.6:              # 想忘得快的人，只有强信号才留得住
        w["explicit"] += 0.06
        w["novelty"] -= 0.03
        w["recurrence"] -= 0.03
    if needs.personality_strength > 0.7:
        w["self_ref"] += 0.06
        w["affect"] += 0.04
        w["novelty"] -= 0.02
    w = normalize(w)
    return {"version": 1, "weights": w, "criteria": {}, "source": "bootstrap",
            "notes": f"依据 forget={needs.forget} detail={needs.detail} personality={needs.personality_strength} 生成"}


SELF_WORDS = ["我", "咱", "自己", "本人", "我的"]
POS = ["喜欢", "开心", "太棒", "厉害", "舒服", "满意", "爱", "期待"]
NEG = ["讨厌", "烦", "难受", "崩", "糟糕", "焦虑", "累", "别", "不要", "痛苦", "生气"]
COMMIT = ["答应", "约定", "明天", "后天", "下周", "下个月", "截止", "deadline", "要交", "得去",
          "必须", "计划", "安排", "记得带", "帮我记", "提醒我", "以后"]
EXPLICIT = ["记住", "记一下", "别忘了", "帮我记", "以后都", "永远", "一直", "不要忘"]
STOP = set("的了是在和就都也很吧啊呢吗这那你我他她它不没要会能可对说着过还要给让把被从到")


def _keyterms(text: str) -> list[str]:
    latin = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", text)
    cjk_chunks = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    grams = []
    for ch in cjk_chunks:
        for n in (2, 3, 4):
            if len(ch) < n:
                continue
            for i in range(len(ch) - n + 1):
                g = ch[i:i + n]
                if sum(1 for c in g if c in STOP) * 2 >= len(g):
                    continue          # 一半以上是虚词就不要
                grams.append(g)
    grams = sorted(set(grams), key=lambda g: (-len(g), g))[:60]
    return list(dict.fromkeys([w.lower() for w in latin] + grams))


def extract_signals(turns: list[dict], known: set[str], goals: list[str]) -> tuple[dict, list[str]]:
    text = "\n".join(t["content"] for t in turns)
    n_turns = max(1, len(turns))
    terms = _keyterms(text)
    new_terms = [t for t in terms if t not in known]
    novelty = len(new_terms) / max(1, len(terms)) if terms else 0.0
    recur = 1.0 - novelty if terms else 0.0
    self_ref = min(1.0, sum(text.count(w) for w in SELF_WORDS) / max(2, n_turns))
    aff_hits = sum(text.count(w) for w in POS + NEG)
    affect = min(1.0, aff_hits / 4)
    comm = min(1.0, sum(text.count(w) for w in COMMIT) / 3)
    expl = 1.0 if any(w in text for w in EXPLICIT) else 0.0
    goal = 0.0
    for g in goals:
        if g and any(tok in text for tok in _keyterms(g)):
            goal = min(1.0, goal + 0.6)
    # 问句 / 寒暄密度高 → 信息量低，压低整体
    small_talk = text.count("?") + text.count("？") + sum(text.count(w) for w in ["哈哈", "嗯嗯", "好的", "天气", "吃了吗"])
    if small_talk >= 3 and len(text) < 160:
        novelty *= 0.4
        affect *= 0.5
    return {
        "self_ref": round(self_ref, 3), "affect": round(affect, 3), "novelty": round(novelty, 3),
        "commitment": round(comm, 3), "goal": round(goal, 3), "recurrence": round(recur, 3),
        "explicit": expl,
    }, terms


def score(dims: dict, weights: dict) -> float:
    s = sum(dims.get(d, 0.0) * weights.get(d, 0.0) for d in DIMENSIONS)
    return round(min(1.0, max(0.0, s)), 3)


FLOORS = [
    (0.80, lambda d: d.get("explicit", 0) >= 1 and d.get("affect", 0) >= 0.7
                     and d.get("self_ref", 0) >= 0.6, "身份级偏好 + 明确要求，判为核心"),
    (0.72, lambda d: d.get("affect", 0) >= 0.8 and d.get("self_ref", 0) >= 0.6, "身份级偏好"),
    (0.58, lambda d: d.get("commitment", 0) >= 0.6, "含明确时限/承诺"),
    (0.50, lambda d: d.get("explicit", 0) >= 1, "用户要求记住，但信号单薄"),
]


def finalize(dims: dict, weights: dict) -> tuple[float, str]:
    """加权分 + 保底规则。保底不是"人为规定"，它是评级体系自己的一条策略，
    随 calibrate 一起演进；作用是防止"用户说记住但语义信号弱"的条目被误删。"""
    sal = score(dims, weights)
    note = ""
    for floor, cond, label in FLOORS:
        if cond(dims) and floor > sal:
            sal, note = floor, f"{label}，保底 {floor:.2f}"
            break
    return sal, note


def explain(dims: dict, weights: dict, top_n: int = 2) -> str:
    contrib = sorted(((dims.get(d, 0.0) * weights.get(d, 0.0), d) for d in DIMENSIONS), reverse=True)
    parts = [f"{DIM_LABEL[d]}{dims[d]:.2f}" for v, d in contrib[:top_n] if v > 0.01]
    return "、".join(parts) or "无显著信号"


def current_rubric(store: Store) -> dict:
    row = store.q1("SELECT * FROM rubric ORDER BY version DESC LIMIT 1")
    if not row:
        raise RuntimeError("rubric 未初始化")
    return {"version": row["version"], "weights": jloads(row["weights"], {}),
            "notes": row["notes"], "source": row["source"]}


def save_rubric(store: Store, rubric: dict):
    store.x("INSERT OR REPLACE INTO rubric(version,created_at,weights,notes,source) VALUES(?,?,?,?,?)",
            (rubric["version"], time.time(), jdumps(rubric["weights"]), rubric.get("notes", ""),
             rubric.get("source", "bootstrap")))


def calibrate(store: Store, cfg: CurveConfig, lr: float = 0.18) -> dict:
    """用真实召回行为反向校准权重。

    被回忆过的记忆 = 预测正确的"重要"；把这些记忆的维度均值，与全体均值比较，
    差异方向就是权重该移动的方向。评级体系因此是"长出来的"，不是人定的。
    """
    cur = current_rubric(store)
    rows = store.q("SELECT dims, retrievals, salience FROM fragments WHERE dims IS NOT NULL")
    if len(rows) < 3:
        return {"changed": False, "version": cur["version"], "reason": f"样本不足（{len(rows)} 条，至少 3 条）"}
    all_d, hit_d, n_hit = {d: 0.0 for d in DIMENSIONS}, {d: 0.0 for d in DIMENSIONS}, 0
    for r in rows:
        d = jloads(r["dims"], {})
        for k in DIMENSIONS:
            all_d[k] += d.get(k, 0.0)
        if (r["retrievals"] or 0) > 0:
            n_hit += 1
            for k in DIMENSIONS:
                hit_d[k] += d.get(k, 0.0)
    if n_hit == 0:
        return {"changed": False, "version": cur["version"], "reason": "尚无召回信号"}
    n_all = len(rows)
    w = dict(cur["weights"])
    delta = {}
    for k in DIMENSIONS:
        m_all, m_hit = all_d[k] / n_all, hit_d[k] / n_hit
        diff = m_hit - m_all
        delta[k] = diff
        w[k] = max(0.01, w[k] + lr * diff)
    w = normalize(w)
    new_v = cur["version"] + 1
    moved = sorted(delta.items(), key=lambda kv: abs(kv[1]), reverse=True)[:2]
    notes = f"样本 {n_all} 条/命中 {n_hit}；" + "、".join(
        f"{DIM_LABEL[k]}{'+' if v >= 0 else ''}{v:.2f}" for k, v in moved)
    save_rubric(store, {"version": new_v, "weights": w, "notes": notes, "source": "calibrate"})
    return {"changed": True, "version": new_v, "weights": w, "notes": notes, "n": n_all, "hits": n_hit}


# ---------- 结构化提炼（六要素 / 日志） ----------

META_PREFIX = ["有件事你记住：", "有件事记住：", "有件事你可能得记住：", "有件事你可能得记住",
               "帮我记一下：", "帮我记一下",
               "你记住：", "记住：", "记一下：", "我告诉你：", "说一下：", "另外，"]


def clean_what(s: str) -> str:
    s = s.strip()
    for p in META_PREFIX:
        if s.startswith(p):
            return s[len(p):].strip()
    return s


def six_elements(turns: list[dict], day: str, user_name: str = "用户", goal_hint: str = "") -> dict:
    text = "\n".join(t["content"] for t in turns)
    sents = [s.strip() for s in re.split(r"[。！？!?\n]+", text) if len(s.strip()) > 4]
    head = clean_what(sents[0]) if sents else text[:60]
    second = sents[1] if len(sents) > 1 else ""
    when = ""
    m = re.search(r"(今天|明天|后天|下周[一二三四五六日天]?|下个月|这周|周[一二三四五六日天]|\d{1,2}月\d{1,2}日)", text)
    if m:
        when = m.group(1)
    why_map = {"commitment": "有明确时限的待办", "preference": "长期协作偏好",
               "insight": "项目/方向决策", "lesson": "经验教训", "relation": "关系互动"}
    why = goal_hint or why_map.get(classify_log(text), "对话中提到")
    return {
        "who": user_name,
        "what": head[:48] + ("…" if len(head) > 48 else ""),
        "whn": when or day,
        "whr": "线上对话",
        "why": why,
        "how": (second[:40] + "…") if len(second) > 40 else (second or "口述"),
        "gist": (head[:70] + ("…" if len(head) > 70 else "")),
    }


LOG_RULES = [
    ("commitment", ["答应", "约定", "要交", "截止", "明天", "下周", "记得", "提醒我", "必须"]),
    ("preference", ["我喜欢", "我讨厌", "我偏好", "以后都", "别", "不要"]),
    ("insight", ["发现", "意识到", "想通", "结论", "所以", "决定"]),
    ("relation", ["我们", "你", "陪", "信任", "关系"]),
    ("lesson", ["教训", "下次", "记住", "别再"]),
]


# ---------- 人格证据：从对话里读出"这段相处在把我推向哪边" ----------

TRAIT_RULES = [
    ("humor", 1, ["哈哈", "笑死", "好笑", "幽默", "有意思", "梗", "段子", "逗"]),
    ("humor", -1, ["太冷", "别贫", "严肃", "无聊", "别开玩笑", "正经", "没人味", "冷冰冰"]),
    ("closeness", 1, ["陪我说", "跟你说说", "难受", "睡不着", "压力大", "焦虑",
                      "撑不住", "不想一个人", "聊聊", "没人说话"]),
    ("closeness", -1, ["别问了", "别管我", "保持距离", "别这么熟", "客气点"]),
    ("directness", 1, ["直接说", "别绕", "说重点", "要结论", "废话", "啰嗦", "简单点", "别铺垫"]),
    ("directness", -1, ["详细点", "慢慢说", "展开讲", "多讲", "别太简"]),
]
EXPLICIT_MARK = ["以后都", "以后别", "一直", "记住", "别再", "再也别", "从现在起"]


def _clauses(text: str) -> list[str]:
    return [c for c in re.split(r"[。！？!?；;\n]+", text) if c.strip()]


def context_weight(turns: list[dict], ts: float) -> float:
    """情境权重：深夜、长谈、情绪浓度高的对话，对人格的影响更大。"""
    from datetime import datetime
    text = "\n".join(t["content"] for t in turns)
    c = 0.45
    hour = datetime.fromtimestamp(ts).hour
    if hour >= 22 or hour <= 2:
        c += 0.18                      # 深夜说的话，分量更重
    if len(text) >= 300:
        c += 0.15                      # 长谈
    if sum(text.count(w) for w in POS + NEG) >= 3:
        c += 0.18                      # 情绪浓度
    if len(text) <= 20:
        c -= 0.15                      # 一句带过的，不当真
    return round(min(1.0, max(0.2, c)), 2)


def trait_signals(turns: list[dict], ts: float) -> list[dict]:
    """只读用户侧发言：人格由"用户怎么跟我相处"塑造。"""
    user_turns = [t for t in turns if t.get("role") == "user"] or turns
    text = "\n".join(t["content"] for t in user_turns)
    clauses = _clauses(text)
    ctx = context_weight(user_turns, ts)
    out = []
    for trait, direction, kws in TRAIT_RULES:
        hits = sum(text.count(k) for k in kws)
        if hits == 0:
            continue
        # 明确要求必须和关键词同句，避免"以后别这样"误伤到无关特质
        explicit = 1 if any(
            any(m in c for m in EXPLICIT_MARK) and any(k in c for k in kws)
            for c in clauses) else 0
        intensity = 0.4 if hits == 1 else (0.7 if hits == 2 else 1.0)
        if explicit:
            intensity = max(intensity, 0.9)
        matched = [k for k in kws if k in text][:2]
        out.append({"trait": trait, "direction": direction, "intensity": intensity,
                    "context": ctx, "explicit": explicit,
                    "note": ("明确要求：" if explicit else "") + "/".join(matched)})
    return out


def classify_log(text: str) -> str:
    for kind, kws in LOG_RULES:
        if any(k in text for k in kws):
            return kind
    return "event"


def make_logs(store: Store, day: str, fragments: list[dict], now: float) -> list[dict]:
    """日记写完之后执行：把当天内容压成 1-3 条重点日志（召回主入口）。"""
    out = []
    ranked = sorted(fragments, key=lambda f: f["salience"], reverse=True)[:3]
    for f in ranked:
        what = f["what"].rstrip("…")
        gist = f["gist"]
        body = gist if (what and what in gist) else f"{what}｜{gist}"
        kind = classify_log(f"{f['what']} {f['gist']}")
        w = round(min(1.0, f["salience"] + 0.05), 3)
        lid = store.x(
            "INSERT INTO logs(day,kind,text,weight,refs,tags,created_at,scope) VALUES(?,?,?,?,?,?,?,?)",
            (day, kind, body[:180], w, jdumps([f["id"]]), f.get("tags", "[]"), now,
             f.get("scope", "main")))
        out.append({"id": lid, "kind": kind, "text": body[:180], "weight": w})
    return out
