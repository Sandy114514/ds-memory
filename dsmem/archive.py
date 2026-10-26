"""日记与碎碎念的长期归档：分成两条流，只追加，永不删除。

    <root>/diary/<分组>/journal/2026-10.md
    <root>/diary/<分组>/murmurs/2026-10.md

为什么按月而不是按天：一年 730 个小文件，inode 和目录遍历的开销远大于内容本身。
按月一个文件、内部按天分节，天粒度照样可检索，文件数压到 12/年。

为什么"只追加"：库里改写同一天的日记时，归档不覆盖旧内容，而是补一段"修订"。
这样即使数据库被回滚或重建，文字记录也不会倒退。GC 不碰这两张表，
备份清理也不碰这个目录。
"""
import hashlib
from datetime import datetime
from pathlib import Path

from .store import Store

INDEX_NAME = ".index.json"


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _read_index(stream_dir: Path) -> dict:
    p = stream_dir / INDEX_NAME
    if p.exists():
        try:
            import json
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _write_index(stream_dir: Path, idx: dict):
    import json
    (stream_dir / INDEX_NAME).write_text(
        json.dumps(idx, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def months(store: Store, scope: str | None = None) -> list[str]:
    if scope:
        rows = store.q("SELECT DISTINCT substr(day,1,7) p FROM journal WHERE scope=? "
                       "UNION SELECT DISTINCT substr(day,1,7) FROM murmurs WHERE scope=? ORDER BY p",
                       (scope, scope))
    else:
        rows = store.q("SELECT DISTINCT substr(day,1,7) p FROM journal "
                       "UNION SELECT DISTINCT substr(day,1,7) FROM murmurs ORDER BY p")
    return [r["p"] for r in rows if r["p"]]


# ---------------------------------------------------------------- 条目

def _journal_entries(store: Store, month: str, scope: str | None) -> list[tuple]:
    sql = ("SELECT day,body,mood,scope FROM journal WHERE day LIKE ?"
           + (" AND scope=?" if scope else "") + " ORDER BY day")
    args = (month + "%", scope) if scope else (month + "%",)
    out = []
    for r in store.q(sql, args):
        tag = "" if scope else f" · {r['scope']}"
        header = f"## {r['day']}{tag} ｜ 心情 {r['mood'] or '—'}"
        out.append((f"{r['day']}|{r['scope']}", header, r["body"] or ""))
    return out


def _murmur_entries(store: Store, month: str, scope: str | None) -> list[tuple]:
    sql = ("SELECT day,ts,text,tone,scope FROM murmurs WHERE day LIKE ?"
           + (" AND scope=?" if scope else "") + " ORDER BY ts")
    args = (month + "%", scope) if scope else (month + "%",)
    grouped: dict[tuple, list] = {}
    for r in store.q(sql, args):
        grouped.setdefault((r["day"], r["scope"]), []).append(r)
    out = []
    for (day, sc), rows in sorted(grouped.items()):
        tag = "" if scope else f" · {sc}"
        header = f"## {day}{tag}（{len(rows)} 条）"
        body = "\n".join(f"- `{datetime.fromtimestamp(r['ts']).strftime('%H:%M')}` {r['text']}"
                         for r in rows)
        out.append((f"{day}|{sc}", header, body))
    return out


# ---------------------------------------------------------------- 写入

def _append_stream(store: Store, stream_dir: Path, month: str, kind: str,
                   scope: str | None) -> dict:
    stream_dir.mkdir(parents=True, exist_ok=True)
    path = stream_dir / f"{month}.md"
    title = "日记" if kind == "journal" else "碎碎念"
    entries = (_journal_entries(store, month, scope) if kind == "journal"
               else _murmur_entries(store, month, scope))
    if not entries:
        return {"written": 0, "revised": 0, "bytes": 0, "path": str(path)}

    idx = _read_index(stream_dir)
    index_key = f"{kind}:{month}"
    seen: dict = idx.get(index_key) or {}
    chunks, written, revised = [], 0, 0
    stamp = datetime.now().strftime("%H:%M")

    for key, header, body in entries:
        h = _hash(header + "\n" + body)
        if key not in seen:
            chunks.append(f"{header}\n{body}\n")
            seen[key] = h
            written += 1
        elif seen[key] != h:
            # 内容变了：不覆盖旧段落，补一段修订
            chunks.append(f"## {header[3:]} ｜修订 {stamp}\n{body}\n")
            seen[key] = h
            revised += 1
    if not chunks:
        return {"written": 0, "revised": 0, "bytes": path.stat().st_size if path.exists() else 0,
                "path": str(path)}

    head = "" if path.exists() else f"# {month} · {title}\n\n"
    with path.open("a", encoding="utf-8") as f:
        if head:
            f.write(head)
        f.write("\n".join(chunks))
    idx[index_key] = seen
    _write_index(stream_dir, idx)
    return {"written": written, "revised": revised, "bytes": path.stat().st_size,
            "path": str(path)}


def sync_space(store: Store, out_dir: str | Path, scope: str | None = None) -> dict:
    """把一个分组的日记/碎碎念补进归档目录（只写新增与修订）。"""
    out = Path(out_dir)
    stats = {"journal": {"written": 0, "revised": 0, "bytes": 0},
             "murmurs": {"written": 0, "revised": 0, "bytes": 0}}
    for month in months(store, scope):
        for kind in ("journal", "murmurs"):
            r = _append_stream(store, out / kind, month, kind, scope)
            s = stats[kind]
            s["written"] += r["written"]
            s["revised"] += r["revised"]
            s["bytes"] += r["bytes"]
    return stats


def sync_all(reg) -> dict:
    """所有分组都同步一次，给每日任务和备份用。"""
    total = {"spaces": 0, "journal": {"written": 0, "revised": 0, "bytes": 0},
             "murmurs": {"written": 0, "revised": 0, "bytes": 0}}
    for sp in reg.list(include_archived=True):
        if not reg.db_path(sp["id"]).exists():
            continue
        st = Store(str(reg.db_path(sp["id"])))
        try:
            r = sync_space(st, reg.export_dir(sp["id"]))
        finally:
            st.close()
        total["spaces"] += 1
        for k in ("journal", "murmurs"):
            total[k]["written"] += r[k]["written"]
            total[k]["revised"] += r[k]["revised"]
            total[k]["bytes"] += r[k]["bytes"]
    return total


def files(reg) -> list[dict]:
    """列出归档文件，给界面和备份用。"""
    out = []
    for p in sorted(reg.diary_dir.rglob("*.md")):
        out.append({"path": str(p), "name": p.name,
                    "rel": p.relative_to(reg.diary_dir).as_posix(),
                    "bytes": p.stat().st_size,
                    "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
    return out


# ---------------------------------------------------------------- 读取

def read_month(store: Store, month: str, scope: str = "main") -> dict:
    """给界面用：不读文件，直接读库，避免文件和库不一致。"""
    js = [dict(r) for r in store.q(
        "SELECT day,body,mood,scope FROM journal WHERE day LIKE ? AND scope=? ORDER BY day DESC",
        (month + "%", scope))]
    ms: dict[str, list] = {}
    for r in store.q("SELECT * FROM murmurs WHERE day LIKE ? AND scope=? ORDER BY ts",
                     (month + "%", scope)):
        ms.setdefault(r["day"], []).append({"ts": r["ts"], "text": r["text"],
                                            "tone": r["tone"], "topic": r["topic"]})
    return {"month": month, "scope": scope, "journals": js, "murmurs": ms}
