"""空间（分组）：一个分组 = 一个 SQLite 文件。

为什么用"一空间一文件"而不是给每张表加 space_id：
漏写一个 WHERE 就会串记忆，而物理隔离不可能串。备份/恢复、删除整个分组、
把某个分组的记忆单独拷走，也都变成文件级操作。

    <root>/spaces.json          分组清单与当前选择
    <root>/spaces/<id>.db       每个分组的全部记忆
    <root>/exports/<id>/...     按月文字归档
    <root>/backups/*.zip        备份
"""
import hashlib
import json
import re
import time
from pathlib import Path

from .store import Store

DEFAULT_ID = "default"


def default_root() -> Path:
    return Path.home() / ".dsmem"


class SpaceRegistry:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else default_root()
        (self.root / "spaces").mkdir(parents=True, exist_ok=True)
        (self.root / "backups").mkdir(parents=True, exist_ok=True)
        self.reg_path = self.root / "spaces.json"
        self.data = self._load()
        if not self.data["spaces"]:
            self._bootstrap_default()

    # ---------- 清单 ----------
    def _load(self) -> dict:
        if self.reg_path.exists():
            try:
                d = json.loads(self.reg_path.read_text(encoding="utf-8"))
                if isinstance(d.get("spaces"), list):
                    return d
            except Exception:
                pass
        return {"version": 1, "active": DEFAULT_ID, "spaces": [], "settings": {}}

    def _save(self):
        self.reg_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _bootstrap_default(self):
        now = time.time()
        self.data["spaces"].append({"id": DEFAULT_ID, "name": "默认分组", "kind": "private",
                                    "note": "起始空间", "created_at": now, "archived": False})
        self.data["active"] = DEFAULT_ID
        self._save()
        self.db_path(DEFAULT_ID).touch()

    # ---------- 路径 ----------
    def db_path(self, space_id: str) -> Path:
        return self.root / "spaces" / f"{space_id}.db"

    def export_dir(self, space_id: str) -> Path:
        """日记 / 碎碎念的长期归档目录（只追加，永不删除）。"""
        d = self.diary_dir / space_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def diary_dir(self) -> Path:
        d = self.root / "diary"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def diary_backup_dir(self) -> Path:
        """日记备份单独一个地方，和记忆备份不混。"""
        base = (self.data.get("settings") or {}).get("diary_backup_dir")
        d = Path(base) if base else (self.root / "diary-backups")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def set_diary_backup_dir(self, path: str) -> Path:
        p = Path(path).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        self.data.setdefault("settings", {})["diary_backup_dir"] = str(p)
        self._save()
        return p

    @property
    def backup_dir(self) -> Path:
        return Path(self.data.get("settings", {}).get("backup_dir") or (self.root / "backups"))

    def set_backup_dir(self, path: str) -> Path:
        p = Path(path).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        self.data.setdefault("settings", {})["backup_dir"] = str(p)
        self._save()
        return p

    # ---------- 查询 ----------
    def get(self, space_id: str) -> dict | None:
        for s in self.data["spaces"]:
            if s["id"] == space_id:
                return s
        return None

    def list(self, include_archived: bool = False) -> list[dict]:
        out = []
        for s in sorted(self.data["spaces"], key=lambda x: x["created_at"]):
            if s.get("archived") and not include_archived:
                continue
            item = dict(s)
            item["db"] = str(self.db_path(s["id"]))
            item["size_kb"] = round(self.db_path(s["id"]).stat().st_size / 1024, 1) \
                if self.db_path(s["id"]).exists() else 0.0
            item["active"] = s["id"] == self.active()
            item["stats"] = self.stats(s["id"])
            out.append(item)
        return out

    def stats(self, space_id: str) -> dict:
        p = self.db_path(space_id)
        if not p.exists():
            return {}
        st = Store(str(p))
        try:
            base = st.stats()
            return {"total_kb": base["_total_kb"],
                    "fragments": st.q1("SELECT COUNT(*) n FROM fragments")["n"],
                    "anchors": st.q1("SELECT COUNT(*) n FROM anchors")["n"],
                    "logs": st.q1("SELECT COUNT(*) n FROM logs")["n"],
                    "journals": st.q1("SELECT COUNT(*) n FROM journal")["n"],
                    "murmurs": st.q1("SELECT COUNT(*) n FROM murmurs")["n"],
                    "chapters": st.q1("SELECT COUNT(*) n FROM chapters")["n"]}
        finally:
            st.close()

    def active(self) -> str:
        a = self.data.get("active")
        if not a or not self.get(a):
            a = self.data["spaces"][0]["id"] if self.data["spaces"] else DEFAULT_ID
        return a

    def set_active(self, space_id: str) -> dict:
        if not self.get(space_id):
            raise KeyError(space_id)
        self.data["active"] = space_id
        self._save()
        return self.get(space_id)

    # ---------- 增删改 ----------
    def _slug(self, name: str) -> str:
        # 保留中英文，只把空白和符号折成连字符，id 一眼能认出来
        base = re.sub(r"[^\w]+", "-", name.strip().lower(), flags=re.UNICODE).strip("-") or "space"
        h = hashlib.sha1(f"{name}{time.time()}".encode()).hexdigest()[:4]
        sid = f"{base}-{h}"
        while self.get(sid):
            h = hashlib.sha1(h.encode()).hexdigest()[:4]
            sid = f"{base}-{h}"
        return sid

    def create(self, name: str, kind: str = "group", note: str = "",
               copy_from: str | None = None, copy_persona: bool = True) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("分组名不能为空")
        sid = self._slug(name)
        info = {"id": sid, "name": name, "kind": kind, "note": note,
                "created_at": time.time(), "archived": False}
        self.data["spaces"].append(info)
        self._save()
        dst = Store(str(self.db_path(sid)))
        src_id = copy_from or self.active()
        src_path = self.db_path(src_id)
        try:
            if src_path.exists() and src_path != self.db_path(sid):
                src = Store(str(src_path))
                try:
                    self._inherit(src, dst, copy_persona)
                finally:
                    src.close()
            if not dst.q1("SELECT 1 FROM rubric LIMIT 1"):
                from . import rating
                from .config import PRESETS
                rating.save_rubric(dst, rating.bootstrap_rubric(PRESETS["长期伴侣"]))
            if not dst.q1("SELECT 1 FROM self_model LIMIT 1"):
                from . import personality
                personality.seed(dst, time.time())
        finally:
            dst.close()
        return info

    def _inherit(self, src: Store, dst: Store, copy_persona: bool):
        """新分组继承评级体系与配置，可选继承人格基线。"""
        rub = src.q1("SELECT * FROM rubric ORDER BY version DESC LIMIT 1")
        if rub:
            dst.x("INSERT OR REPLACE INTO rubric(version,created_at,weights,notes,source) "
                  "VALUES(?,?,?,?,?)",
                  (1, time.time(), rub["weights"], rub["notes"], "inherit"))
        for k in ("config", "needs"):
            v = src.q1("SELECT v FROM meta WHERE k=?", (k,))
            if v:
                dst.x("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v["v"]))
        if copy_persona:
            for r in src.q("SELECT * FROM self_model"):
                dst.x("INSERT OR REPLACE INTO self_model(key,value,layer,inertia,level,version,"
                      "updated_at) VALUES(?,?,?,?,?,?,?)",
                      (r["key"], r["value"], r["layer"], r["inertia"], r["level"],
                       r["version"], time.time()))

    def rename(self, space_id: str, name: str):
        s = self.get(space_id)
        if not s:
            raise KeyError(space_id)
        s["name"] = (name or s["name"]).strip()
        self._save()

    def archive(self, space_id: str, archived: bool = True) -> dict:
        s = self.get(space_id)
        if not s:
            raise KeyError(space_id)
        if s["id"] == DEFAULT_ID and archived:
            raise ValueError("默认分组不能归档")
        s["archived"] = archived
        if archived and self.data.get("active") == space_id:
            self.data["active"] = next((x["id"] for x in self.data["spaces"] if not x.get("archived")),
                                       DEFAULT_ID)
        self._save()
        return s

    def delete(self, space_id: str) -> dict:
        """只删注册信息，db 文件移到 trash/，可手工找回（不 rm）。"""
        if space_id == DEFAULT_ID:
            raise ValueError("默认分组不能删除")
        s = self.get(space_id)
        if not s:
            raise KeyError(space_id)
        trash = self.root / "trash"
        trash.mkdir(exist_ok=True)
        src = self.db_path(space_id)
        if src.exists():
            src.rename(trash / f"{space_id}-{int(time.time())}.db")
        self.data["spaces"] = [x for x in self.data["spaces"] if x["id"] != space_id]
        if self.data.get("active") == space_id:
            self.data["active"] = self.data["spaces"][0]["id"] if self.data["spaces"] else DEFAULT_ID
        self._save()
        return s

    # ---------- 会话绑定（给机器人接入用；人工也可在界面里直接切换） ----------
    def bind(self, chat_id: str, space_id: str) -> dict:
        if not self.get(space_id):
            raise KeyError(space_id)
        self.data.setdefault("bindings", {})[chat_id] = space_id
        self._save()
        return {"chat_id": chat_id, "space": space_id}

    def resolve(self, chat_id: str) -> str:
        b = (self.data.get("bindings") or {}).get(chat_id)
        return b if b and self.get(b) else self.active()

    def unbind(self, chat_id: str) -> dict:
        (self.data.get("bindings") or {}).pop(chat_id, None)
        self._save()
        return {"chat_id": chat_id, "space": self.active()}

    # ---------- 打开 ----------
    def open(self, space_id: str | None = None) -> Store:
        sid = space_id or self.active()
        if not self.get(sid):
            raise KeyError(sid)
        return Store(str(self.db_path(sid)))
