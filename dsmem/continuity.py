"""连续性：删除之前，先把它折进月度章节。

规则很简单：
1. 任何片段被删（老化、超预算、手动），先 absorb() 进当月章节；
2. 只要有活动，当月一定有一条 chapter 记录 —— 时间线不会出现"空白月"；
3. 章节本身永不删除，且长度有上限（默认 600 字），不会反向膨胀。

这样召回时即使细节全没了，也能回答"那段时间大概在干什么"。
"""
from datetime import datetime

from .store import Store, jdumps, jloads

MAX_CHAPTER_CHARS = 600


def period_of(day: str) -> str:
    return (day or "")[:7]


def absorb(store: Store, frag: dict, now: float) -> dict:
    """把一个即将被删的片段折进月度章节。"""
    day = frag.get("day") or datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    period = period_of(day)
    scope = frag.get("scope") or "main"
    clause = _clause(frag)
    row = store.q1("SELECT * FROM chapters WHERE period=? AND scope=?", (period, scope))
    if row:
        text = row["text"] or ""
        if clause not in text:
            text = (text + "；" + clause)[-MAX_CHAPTER_CHARS:]
        tags = list(dict.fromkeys(jloads(row["tags"], []) + jloads(frag.get("tags"), [])))[:12]
        store.x("UPDATE chapters SET text=?, tags=?, deleted_n=deleted_n+1, salience=?, updated_at=? "
                "WHERE period=? AND scope=?",
                (text, jdumps(tags), max(row["salience"] or 0, frag.get("salience") or 0), now,
                 period, scope))
        return {"period": period, "appended": clause}
    store.x("INSERT INTO chapters(scope,period,text,tags,count,deleted_n,salience,created_at,updated_at) "
            "VALUES(?,?,?,?,?,1,?,?,?)",
            (scope, period, clause, jdumps(jloads(frag.get("tags"), [])), 1,
             frag.get("salience") or 0, now, now))
    return {"period": period, "created": clause}


def _clause(frag: dict) -> str:
    day = (frag.get("day") or "")[5:]
    what = (frag.get("what") or "").rstrip("…")[:26]
    why = frag.get("why") or ""
    return f"{day} {what}" + (f"（{why}）" if why and why != "对话中提到" else "")


def ensure_month(store: Store, day: str, now: float, note: str = "", scope: str = "main"):
    """保证这个月有章节占位，时间线不留空。"""
    period = period_of(day)
    if not period:
        return
    row = store.q1("SELECT 1 FROM chapters WHERE period=? AND scope=?", (period, scope))
    if not row:
        store.x("INSERT INTO chapters(scope,period,text,tags,count,deleted_n,salience,"
                "created_at,updated_at) VALUES(?,?,?,?,0,0,0,?,?)",
                (scope, period, note or f"{period} 这个月开始有记录。", "[]", now, now))


def bump_month(store: Store, day: str, now: float, n: int = 1, scope: str = "main"):
    store.x("INSERT INTO chapters(scope,period,text,tags,count,deleted_n,salience,"
            "created_at,updated_at) VALUES(?,?,?,?,?,0,0,?,?) "
            "ON CONFLICT(period,scope) DO UPDATE SET count=count+excluded.count",
            (scope, period_of(day), "", "[]", n, now, now))


def timeline(store: Store, limit: int = 24, scope: str = "main") -> list[dict]:
    rows = store.q("SELECT * FROM chapters WHERE scope=? ORDER BY period DESC LIMIT ?", (scope, limit))
    out = []
    for r in rows:
        journals = store.q1("SELECT COUNT(*) n FROM journal WHERE day LIKE ? AND scope=?",
                            (r["period"] + "%", scope))["n"]
        murmurs = store.q1("SELECT COUNT(*) n FROM murmurs WHERE day LIKE ? AND scope=?",
                           (r["period"] + "%", scope))["n"]
        out.append({"period": r["period"], "text": r["text"], "count": r["count"],
                    "deleted": r["deleted_n"], "journals": journals, "murmurs": murmurs,
                    "scope": r["scope"]})
    return out


def coverage(store: Store, scope: str = "main") -> dict:
    """连续性的健康度：有记录的月份数 / 最早到最晚的月份跨度。"""
    rows = store.q("SELECT period FROM chapters WHERE scope=? ORDER BY period", (scope,))
    if not rows:
        return {"months": 0, "span": 0, "gaps": []}
    periods = [r["period"] for r in rows]
    y0, m0 = int(periods[0][:4]), int(periods[0][5:7])
    y1, m1 = int(periods[-1][:4]), int(periods[-1][5:7])
    span = (y1 - y0) * 12 + (m1 - m0) + 1
    have = set(periods)
    gaps = []
    y, m = y0, m0
    for _ in range(span):
        p = f"{y:04d}-{m:02d}"
        if p not in have:
            gaps.append(p)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return {"months": len(periods), "span": span, "gaps": gaps[:12]}
