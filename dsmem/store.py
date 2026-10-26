"""SQLite 单文件存储层：零依赖、可整体拷贝、便于控制空间。"""
import json
import sqlite3
import zlib
from pathlib import Path
from typing import Any

_SCHEMA = Path(__file__).with_name("schema.sql")


def pack(obj: Any) -> bytes:
    return zlib.compress(json.dumps(obj, ensure_ascii=False).encode("utf-8"), 9)


def unpack(blob: bytes) -> Any:
    return json.loads(zlib.decompress(blob).decode("utf-8"))


def jdumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def jloads(s: str | None, default=None):
    if not s:
        return default if default is not None else []
    try:
        return json.loads(s)
    except Exception:
        return default if default is not None else []


def est_tokens(text: str) -> int:
    """粗略 token 估算：中文约 1 token/字，混排按 1.7 字符/token 折中。"""
    if not text:
        return 0
    return max(1, int(round(len(text) / 1.7)))


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
        self._migrate()
        self.conn.commit()

    def _cols(self, table: str) -> list[str]:
        return [r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")]

    def _migrate(self):
        """给旧库补列，保证升级不丢数据。"""
        for table, col, decl in (
            ("self_model", "level", "REAL"),
            ("self_deltas", "d_before", "REAL"),
            ("self_deltas", "d_after", "REAL"),
            ("self_deltas", "pressure", "REAL"),
            ("self_deltas", "evidence_n", "INTEGER"),
            ("self_deltas", "span_days", "REAL"),
            ("murmurs", "scope", "TEXT NOT NULL DEFAULT 'main'"),
            ("buffer", "scope", "TEXT NOT NULL DEFAULT 'main'"),
            ("anchors", "scope", "TEXT NOT NULL DEFAULT 'main'"),
            ("fragments", "scope", "TEXT NOT NULL DEFAULT 'main'"),
            ("logs", "scope", "TEXT NOT NULL DEFAULT 'main'"),
        ):
            if table not in [r["name"] for r in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]:
                continue
            if col not in self._cols(table):
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        # journal / chapters 的唯一键变成 (day|period, scope)，只能重建
        for table in ("journal", "chapters"):
            if "scope" not in self._cols(table):
                self._rebuild(table)
        self._drop_capacity_keys()

    def _drop_capacity_keys(self):
        """容量上限已取消：把旧配置里遗留的 max_bytes / space_mb 抹掉。

        只把默认值改成 0 并不够 —— 老库里存着 4194304，一加载就又回来了。
        """
        row = self.conn.execute("SELECT v FROM meta WHERE k='config'").fetchone()
        if not row or not row[0]:
            return
        try:
            cfg = json.loads(row[0])
        except Exception:
            return
        if not isinstance(cfg, dict):
            return
        hit = [k for k in ("max_bytes", "space_mb") if k in cfg]
        if not hit:
            return
        for k in hit:
            cfg.pop(k, None)
        self.conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('config',?)",
                          (json.dumps(cfg, ensure_ascii=False),))

    def _rebuild(self, table: str):
        old = f"{table}__old"
        self.conn.execute(f"DROP TABLE IF EXISTS {old}")
        self.conn.execute(f"ALTER TABLE {table} RENAME TO {old}")
        self.conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
        keep = [c for c in self._cols(old) if c in self._cols(table)]
        cols = ",".join(keep)
        self.conn.execute(f"INSERT INTO {table}({cols}) SELECT {cols} FROM {old}")
        self.conn.execute(f"DROP TABLE {old}")
        self.conn.commit()

    # ---------- 基础 ----------
    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def q1(self, sql: str, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args=()) -> int:
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur.lastrowid

    def activities_for(self, day: str, scope: str = "main", limit: int = 6) -> list[dict]:
        return [dict(r) for r in self.q(
            "SELECT * FROM activities WHERE day=? AND scope=? ORDER BY weight DESC, id DESC LIMIT ?",
            (day, scope, limit))]

    def recent_activities(self, scope: str = "main", since: float = 0, limit: int = 1) -> list[dict]:
        return [dict(r) for r in self.q(
            "SELECT * FROM activities WHERE scope=? AND ts>? ORDER BY weight DESC, id DESC LIMIT ?",
            (scope, since, limit))]

    def meta_set(self, k: str, v: Any):
        self.x("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (k, jdumps(v) if not isinstance(v, str) else v))

    def meta_get(self, k: str, default=None):
        row = self.q1("SELECT v FROM meta WHERE k=?", (k,))
        if not row:
            return default
        try:
            return json.loads(row["v"])
        except Exception:
            return row["v"]

    # ---------- L0 ----------
    def add_turn(self, session_id: str, role: str, content: str, ts: float, scope: str = "main"):
        self.x("INSERT INTO buffer(session_id,role,content,ts,scope) VALUES(?,?,?,?,?)",
               (session_id, role, content, ts, scope))

    def sessions_pending(self, until: float | None = None, scope: str = "main") -> list[str]:
        if until is None:
            rows = self.q("SELECT DISTINCT session_id FROM buffer WHERE promoted=0 AND scope=? "
                          "ORDER BY session_id", (scope,))
        else:
            rows = self.q("SELECT DISTINCT session_id FROM buffer WHERE promoted=0 AND scope=? "
                          "AND ts<=? ORDER BY session_id", (scope, until))
        return [r["session_id"] for r in rows]

    def session_turns(self, session_id: str, scope: str | None = None) -> list[dict]:
        if scope is None:
            rows = self.q("SELECT role,content,ts FROM buffer WHERE session_id=? ORDER BY id",
                          (session_id,))
        else:
            rows = self.q("SELECT role,content,ts FROM buffer WHERE session_id=? AND scope=? ORDER BY id",
                          (session_id, scope))
        return [dict(r) for r in rows]

    def drop_session(self, session_id: str, scope: str = "main"):
        self.conn.execute("DELETE FROM buffer WHERE session_id=? AND scope=?", (session_id, scope))
        self.conn.commit()

    def purge_expired(self, now: float, ttl_h: float) -> int:
        cur = self.conn.execute("DELETE FROM buffer WHERE promoted=0 AND ts < ?", (now - ttl_h * 3600,))
        self.conn.commit()
        return cur.rowcount

    # ---------- L2 ----------
    def add_anchor(self, *, day, created_at, title, salience, dims, reason, tags, entities,
                   transcript: list[dict], scope: str = "main") -> int:
        raw = json.dumps(transcript, ensure_ascii=False).encode("utf-8")
        comp = pack(transcript)
        return self.x(
            "INSERT INTO anchors(day,created_at,title,salience,dims,reason,tags,entities,transcript,"
            "raw_bytes,comp_bytes,retrievals,last_access,state,scope) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,'hot',?)",
            (day, created_at, title, salience, jdumps(dims), reason, jdumps(tags), jdumps(entities),
             comp, len(raw), len(comp), created_at, scope))

    def read_transcript(self, anchor_id: int, now: float) -> list[dict]:
        row = self.q1("SELECT transcript FROM anchors WHERE id=?", (anchor_id,))
        if not row:
            return []
        self.x("UPDATE anchors SET retrievals=retrievals+1, last_access=? WHERE id=?", (now, anchor_id))
        return unpack(row["transcript"])

    # ---------- L1 ----------
    def add_fragment(self, **kw) -> int:
        cols = ["anchor_id", "created_at", "day", "who", "what", "whn", "whr", "why", "how",
                "gist", "salience", "dims", "stability_h", "last_review", "tags", "entities",
                "state", "scope"]
        kw.setdefault("scope", "main")
        vals = [kw.get(c) for c in cols]
        return self.x(
            f"INSERT INTO fragments({','.join(cols)},retrievals,pinned) VALUES({','.join('?' * len(cols))},0,?)",
            (*vals, kw.get("pinned", 0)))

    # ---------- 空间统计 ----------
    _SIZE_MAP = {
        "L0": [("buffer", ["content"])],
        "L1": [("fragments", ["who", "what", "whn", "whr", "why", "how", "gist", "tags", "entities"]),
               ("logs", ["text", "tags"]),
               ("journal", ["body"]),
               ("murmurs", ["text"]),
               ("chapters", ["text", "tags"])],
        "L2": [("anchors", ["transcript", "title", "reason"])],
        "SELF": [("self_model", ["value"]), ("self_deltas", ["before", "after", "reason"]),
                 ("self_evidence", ["note"])],
        "META": [("rubric", ["weights", "notes"]), ("recall_receipts", ["query", "layers"]),
                 ("idle_log", ["note"]), ("plugins", ["name", "capabilities"]),
                 ("members", ["name", "note"]), ("membership", ["group_id", "user_id"]),
                 ("activities", ["plugin", "title", "detail"])],
    }

    def stats(self) -> dict:
        out = {}
        for tier, specs in self._SIZE_MAP.items():
            total, rows_n = 0, 0
            for table, cols in specs:
                expr = " + ".join(f"COALESCE(length({c}),0)" for c in cols)
                row = self.q1(f"SELECT COALESCE(SUM({expr}),0) AS b, COUNT(*) AS n FROM {table}")
                total += row["b"] or 0
                rows_n += row["n"] or 0
            out[tier] = {"bytes": int(total), "rows": int(rows_n)}
        out["_total"] = sum(v["bytes"] for k, v in out.items() if not k.startswith("_"))
        for k in list(out):
            if not k.startswith("_"):
                out[k]["kb"] = round(out[k]["bytes"] / 1024, 1)
        out["_total_kb"] = round(out["_total"] / 1024, 1)
        return out

    def close(self):
        self.conn.commit()
        self.conn.close()
