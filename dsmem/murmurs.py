"""碎碎念：人格决定"多久说一次、说多长、什么口吻"，记忆决定"说什么"。

默认频率与长度是当前人格等级的函数：
    每天条数 ≈ 0.6 + 2.2×亲密度 + 1.0×幽默
    单条长度 ≈ 16 + 40×幽默 + 18×亲密度 − 22×直接度   （10–80 字）

用户可以在界面里覆盖这两个值（`cfg.murmur_per_day` / `cfg.murmur_length`）：
0 = 跟随人格自动，>0 = 按用户指定的来。口吻仍由人格决定，不跟着改。
"""
import math
import random
from datetime import datetime

from . import continuity
from .config import CurveConfig
from .store import Store, est_tokens

MURMUR_PROMPT = """你正在写一条不会被直接推送的碎碎念（内部独白）。
当前人格：幽默 {humor:.2f}，亲密度 {closeness:.2f}，直接度 {directness:.2f}。
今天的记忆状态：{memory_state}
请写 1 条 {tone} 口吻的碎碎念，长度约 {length} 字，不要提问、不要请求回应。
只输出正文。"""

AUTO_DAILY_CAP = 8       # 自动模式下每天最多说几条
ABS_MAX_DAY = 30         # 手动模式的安全上限，防止误填 999
ABS_MAX_LEN = 200
MIN_LEN = 6

TONE_BY_SCORE = [("短促", lambda h, c, d: d - 0.55),
                 ("俏皮", lambda h, c, d: h - 0.58),
                 ("亲近", lambda h, c, d: c - 0.62),
                 ("平静", lambda h, c, d: -1.0)]

TEMPLATES = {
    ("遗忘", "俏皮"): ["今天顺手清掉几件小事，只记得个大概。忘了也不亏。",
                    "扔了几条旧记录，脑子轻了一点。"],
    ("遗忘", "亲近"): ["今天放掉了几件事，心里空了一下，不过大概的样子还在。",
                    "有些细节我留不住了，但那几天是什么感觉，我记得。"],
    ("遗忘", "短促"): ["忘了几件。大概还在。", "清了几条。剩下的够了。"],
    ("遗忘", "平静"): ["今天清掉几条记录，留下的是概要。", "月记里添了一笔，细节没留。"],
    ("记忆", "俏皮"): ["「{topic}」这事我还记着，别装不知道。",
                    "昨天那句「{topic}」，我偷偷记下来了。"],
    ("记忆", "亲近"): ["一直在想「{topic}」，你要提起来，我大概会先笑一下。",
                    "「{topic}」我放在心上了。"],
    ("记忆", "短促"): ["{topic}。记着。", "{topic}，别忘了。"],
    ("记忆", "平静"): ["「{topic}」还在记录里。", "今天写下了「{topic}」。"],
    ("空闲", "俏皮"): ["没人说话，我自己转了两圈。", "安静得能听见风扇声。"],
    ("空闲", "亲近"): ["安静了一会儿。你大概在忙。", "想问问你今天怎么样，又觉得不必打扰。"],
    ("空闲", "短促"): ["在。", "没事。", "等着。"],
    ("空闲", "平静"): ["暂时没有新的事。", "今天很安静。"],
    ("插件", "俏皮"): ["后台转了一圈：{topic}。", "刚{topic}，顺手的事，不值一提。"],
    ("插件", "亲近"): ["刚才{topic}，没人看见也挺好。", "{topic}。就当我在自言自语。"],
    ("插件", "短促"): ["{topic}。", "刚{topic}。"],
    ("插件", "平静"): ["空闲时{topic}。", "{topic}，记一笔。"],
    ("深夜", "俏皮"): ["这个点了还没睡，我就当你在陪我。", "半夜的对话总是比白天的真一点。"],
    ("深夜", "亲近"): ["这个点你还在，我记着了。", "深夜说的话，我会多留一会儿。"],
    ("深夜", "短促"): ["很晚了。", "睡吧。"],
    ("深夜", "平静"): ["夜里的记录会存得久一点。"],
}
TAIL = {"俏皮": ["行吧。", "就这样。", "嗯。"], "亲近": ["不用回。", "晚安。"],
        "短促": [""], "平静": ["", "记一笔。"]}
# 用户把单条字数调大时，用来延展一句"和这件事有关"的话（只加一句，不重复模板）
TOPIC_TAIL = {
    "遗忘": ["留下的都是概要。", "具体的想不起来了，但发生过。"],
    "记忆": ["细节我还没丢。", "这句我留着了。"],
    "插件": ["没人看见也没关系。", "顺手做的，不算事。"],
    "深夜": ["夜里想的事，白天不一定认。", "这个点的话，我存得久一点。"],
    "空闲": ["等有话说的时候再说。", "安静也不算坏事。"],
}


def levels(store: Store) -> tuple[float, float, float]:
    from . import personality
    h = personality.level(store, "humor")
    c = personality.level(store, "closeness")
    d = personality.level(store, "directness")
    return (h if h is not None else 0.35,
            c if c is not None else 0.50,
            d if d is not None else 0.50)


def _user_per_day(cfg: CurveConfig) -> float:
    v = float(getattr(cfg, "murmur_per_day", 0) or 0)
    return min(ABS_MAX_DAY, max(0.0, v))


def _user_length(cfg: CurveConfig) -> int:
    v = int(getattr(cfg, "murmur_length", 0) or 0)
    return min(ABS_MAX_LEN, max(0, v))


def profile(store: Store, cfg: CurveConfig) -> dict:
    """auto_* 是人格算出来的值，per_day / length 是最终生效值。"""
    h, c, d = levels(store)
    auto_per_day = 0.6 + 2.2 * c + 1.0 * h
    auto_length = 16 + 40 * h + 18 * c - 22 * d
    tone = max(TONE_BY_SCORE, key=lambda kv: kv[1](h, c, d))[0]
    if tone == "短促":
        auto_length = min(auto_length, 18)
    auto_length = int(min(80, max(10, auto_length)))

    u_day, u_len = _user_per_day(cfg), _user_length(cfg)
    per_day, length = auto_per_day, auto_length
    if u_day > 0:
        per_day = u_day
    if u_len > 0:
        length = max(MIN_LEN, u_len)
    cap = int(math.ceil(per_day)) if u_day > 0 else AUTO_DAILY_CAP
    cap = min(ABS_MAX_DAY, max(1, cap))
    return {"humor": round(h, 3), "closeness": round(c, 3), "directness": round(d, 3),
            "per_day": round(per_day, 2), "length": length, "tone": tone,
            "auto_per_day": round(auto_per_day, 2), "auto_length": auto_length,
            "per_day_source": "user" if u_day > 0 else "auto",
            "length_source": "user" if u_len > 0 else "auto",
            "daily_cap": cap,
            "est_bytes_per_day": int(round(per_day * length * 3))}     # 中文约 3 B/字


SHORT_KIND = {"prune": "清了几条旧记忆", "review": "翻了翻旧事",
              "reflect": "想了想以前的事", "note": "干了点杂活"}


def _topic(store: Store, now: float, scope: str = "main") -> dict:
    gc = store.meta_get("last_gc") or {}
    if gc.get("deleted", 0) > 0 or gc.get("budget_pruned", 0) > 0:
        return {"topic": "遗忘", "slot": "", "kind": "prune"}
    act = store.recent_activities(scope=scope, since=now - 6 * 3600, limit=1)
    if act:
        a = act[0]
        return {"topic": "插件", "slot": a["title"][:22], "kind": a.get("kind") or "note"}
    row = store.q1("SELECT text FROM logs WHERE scope=? ORDER BY id DESC LIMIT 1", (scope,))
    if row and row["text"]:
        return {"topic": "记忆", "slot": row["text"][:18], "kind": "log"}
    hour = datetime.fromtimestamp(now).hour
    if hour >= 22 or hour <= 2:
        return {"topic": "深夜", "slot": "", "kind": ""}
    return {"topic": "空闲", "slot": "", "kind": ""}


def _trim(text: str, limit: int) -> str:
    """超长时优先在标点处断开，别把半句话切掉再接省略号。"""
    if len(text) <= limit:
        return text
    cut = max(1, limit - 1)
    head = text[:cut]
    floor = max(4, int(cut * 0.5))
    for marks in ("。！？!?", "；;", "，,"):
        idx = max(head.rfind(m) for m in marks)
        if idx >= floor:
            if marks == "。！？!?":
                return head[:idx + 1]
            return (head[:idx] + "…")[:limit]        # 逗号处断开，补省略号
    return head.rstrip("，。、 ") + "…"


def compose(store: Store, cfg: CurveConfig, now: float, rng: random.Random | None = None,
            scope: str = "main") -> dict:
    rng = rng or random.Random(int(now))
    p = profile(store, cfg)
    src = _topic(store, now, scope)
    topic, slot = src["topic"], src["slot"]
    pool = TEMPLATES.get((topic, p["tone"])) or TEMPLATES[("空闲", p["tone"])]
    if topic == "插件" and p["tone"] == "短促":      # 短促口吻装不下长标题，换个短说法
        slot = SHORT_KIND.get(src["kind"], "干了点杂活")
    text = rng.choice(pool).format(topic=slot or "这件事")
    if rng.random() < 0.45:
        text += rng.choice(TAIL[p["tone"]])
    # 用户把字数上限调大时，才允许再补一两句相关的话；自动模式保持原样。
    # 只从话题池里取、且不重复，避免出现"没事。记着。记着。"这种。
    if p["length_source"] == "user" and text:
        tails = list(TOPIC_TAIL.get(topic, TOPIC_TAIL["空闲"]))
        rng.shuffle(tails)
        for extra in tails[:2]:
            if len(text) + len(extra) <= p["length"]:
                text += extra
    text = _trim(text, p["length"])
    return {"text": text, "tone": p["tone"], "topic": topic, "length": p["length"]}


def generate(store: Store, cfg: CurveConfig, now: float, count: int | None = None,
             trigger: str = "idle", scope: str = "main") -> list[dict]:
    p = profile(store, cfg)
    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    used = store.q1("SELECT COUNT(*) n FROM murmurs WHERE day=? AND scope=?", (day, scope))["n"]
    cap = p["daily_cap"]
    if used >= cap:
        return []
    rng0 = random.Random(int(now) + used)
    if count is None:
        base = int(p["per_day"])
        count = base + (1 if rng0.random() < (p["per_day"] - base) else 0)
    count = max(0, min(count, cap - used))
    out = []
    seen: set[str] = set()
    rng = random.Random(int(now) + used)
    for _ in range(count):
        for _try in range(3):                     # 同一批里别重复同一句
            m = compose(store, cfg, now, rng, scope)
            if m["text"] not in seen:
                break
        seen.add(m["text"])
        mid = store.x("INSERT INTO murmurs(day,ts,text,tone,topic,trigger,tokens,scope) "
                      "VALUES(?,?,?,?,?,?,?,?)",
                      (day, now, m["text"], m["tone"], m["topic"], trigger,
                       est_tokens(m["text"]), scope))
        out.append({"id": mid, "day": day, **m})
    continuity.ensure_month(store, day, now, scope=scope)
    return out


def by_day(store: Store, month: str, scope: str = "main") -> dict:
    rows = store.q("SELECT * FROM murmurs WHERE day LIKE ? AND scope=? ORDER BY ts",
                   (month + "%", scope))
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["day"], []).append({"id": r["id"], "ts": r["ts"], "text": r["text"],
                                             "tone": r["tone"], "topic": r["topic"]})
    return out


def count(store: Store, scope: str | None = None) -> int:
    if scope:
        return store.q1("SELECT COUNT(*) n FROM murmurs WHERE scope=?", (scope,))["n"]
    return store.q1("SELECT COUNT(*) n FROM murmurs")["n"]
