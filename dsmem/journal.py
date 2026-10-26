"""日记 -> 重点提炼 -> 日志。日记是"自我叙事"，日志是"检索索引"，两者分开存。"""
from . import rating
from .config import CurveConfig
from .store import Store, jloads

MOOD_WORDS = {
    "up": ["开心", "顺", "喜欢", "太好了", "满意", "轻松"],
    "down": ["累", "烦", "焦虑", "崩", "难受", "糟"],
    "focus": ["做", "写", "改", "调试", "推进", "方案", "代码"],
}


def detect_mood(text: str) -> str:
    for mood, kws in MOOD_WORDS.items():
        if any(k in text for k in kws):
            return mood
    return "calm"


VERB = {"prune": "顺手整理了一下记忆", "review": "回头翻了翻旧事",
        "reflect": "自己坐着想了会儿", "murmur": "自言自语了几句",
        "note": "顺手做了点事"}


def activity_text(store: Store, acts: list[dict], style: str, cfg: CurveConfig) -> str:
    """把插件行为写进日记。语气跟人格走，风格跟 setting 走。"""
    from . import personality
    if not acts:
        return ""
    if style == "极简":
        return "\n".join(f"- {a['title']}" for a in acts[:3])
    if style == "要点":
        return "另外：" + "；".join(a["title"] for a in acts[:3]) + "。"

    h = personality.level(store, "humor") or 0.35
    c = personality.level(store, "closeness") or 0.50
    d = personality.level(store, "directness") or 0.50
    if d > 0.7:                                   # 直接度高：只留事实
        return "；".join(a["title"] for a in acts[:2]) + "。"
    head = VERB.get(acts[0]["kind"], "顺手做了点事")
    s = f"{head}，{acts[0]['title']}"
    if len(acts) > 1:
        s += f"；{acts[1]['title']}"
    s += "。"
    if h > 0.65:
        s += "反正也没人看见。"
    elif c > 0.7:
        s += "做完心里松了一点。"
    return s


def write_journal(store: Store, cfg: CurveConfig, day: str, now: float,
                  fragments: list[dict], extra_notes: list[str] | None = None,
                  scope: str = "main", activities: list[dict] | None = None) -> dict:
    """由模型用自己的口吻写日记；素材 = 当天情节 + 其它插件的行为。"""
    style = cfg.journal_style
    extra_notes = extra_notes or []
    acts = activities if activities is not None else store.activities_for(day, scope)
    if not fragments and not acts and not extra_notes:
        body = f"{day}：今天没什么值得记的，聊了几句就散了。这样也好。"
        mood = "calm"
    else:
        ranked = sorted(fragments, key=lambda f: f["salience"], reverse=True)
        if not ranked:                            # 只有插件在干活的一天
            body = f"{day}。" + activity_text(store, acts, style, cfg)
            mood = "calm"
        else:
            lines = []
            for f in ranked[:3]:
                lines.append(f"{f['what']}")
            joined = "；".join(lines)
            mood = detect_mood(joined)
            if style == "极简":
                body = f"{day}\n" + "\n".join(f"- {f['what']}" for f in ranked[:3])
            elif style == "要点":
                body = f"{day}｜{len(ranked)} 件事\n" + "\n".join(
                    f"{i+1}. {f['what']}（{f['whn']}）" for i, f in enumerate(ranked[:3]))
            elif style == "意识流":
                body = (f"{day}。{ranked[0]['what']}——这件事在我脑子里绕了一会儿。"
                        f"{'另外' + ranked[1]['what'] + '，也算个转折。' if len(ranked) > 1 else ''}"
                        f"我记得当时的感觉，比记得内容更清楚。")
            else:
                first = ranked[0]
                body = (f"{day}。今天最清楚的一件事是「{first['what']}」。")
                if len(ranked) > 1:
                    body += f"还有「{ranked[1]['what']}」。"
                body += "这些我都留着，剩下的就不占地方了。"
            if acts:
                body += "\n" + activity_text(store, acts, style, cfg)
            if extra_notes:
                body += "\n（空闲时想到：)" + "；".join(extra_notes)
    store.x("INSERT OR REPLACE INTO journal(day,body,mood,bytes,created_at,scope) "
            "VALUES(?,?,?,?,?,?)",
            (day, body, mood, len(body.encode("utf-8")), now, scope))
    logs = rating.make_logs(store, day, fragments, now)
    return {"day": day, "mood": mood, "body": body, "logs": logs,
            "activities": [a["title"] for a in acts]}


def recent_logs(store: Store, days: int = 14, now: float = 0, limit: int = 8,
                scope: str = "main") -> list[dict]:
    rows = store.q("SELECT day,kind,text,weight FROM logs WHERE scope=? "
                   "ORDER BY day DESC, weight DESC LIMIT ?", (scope, limit))
    return [dict(r) for r in rows]


def journals(store: Store, limit: int = 5, scope: str = "main") -> list[dict]:
    return [dict(r) for r in store.q(
        "SELECT day,body,mood FROM journal WHERE scope=? ORDER BY day DESC LIMIT ?", (scope, limit))]
