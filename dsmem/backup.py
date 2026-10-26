"""备份与恢复：三种版本，尽量不重复。

    full     完整记忆：一个字都不删（冷数据、人格证据流水、人格历史版本都在）
    current  当前记忆：只留此刻还记得的内容 + 此刻的人格
    diary    日记与碎碎念：纯文本，单独一个目录、单独一摞备份

去重的三层做法：
1. zip 里不再放 Markdown 归档 —— 那是数据库内容的副本，属于纯重复；
2. 建备份前先算"内容指纹"，和上一次同类备份相同就直接跳过，不产生新文件；
3. 日记归档本身只追加（见 archive.py），不会因为重写同一天而堆出多份全文。

四件事保证它可信：
1. 用 SQLite 自己的 backup API 取一致快照（不是直接拷文件，避免 WAL 半写）；
2. 当前版在快照上删减 + VACUUM，原库不动；
3. manifest 记录每个文件的 sha256 与内容指纹，恢复前先校验；
4. 恢复前自动做一次完整备份；恢复日记时只增不删。
"""
import hashlib
import json
import secrets
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

from . import archive
from .store import Store

APP = "dsmem"
FMT = 4

KIND_FULL = "full"
KIND_CURRENT = "current"
KIND_DIARY = "diary"
KINDS = (KIND_FULL, KIND_CURRENT, KIND_DIARY)
MEMORY_KINDS = (KIND_FULL, KIND_CURRENT)
KIND_LABEL = {KIND_FULL: "完整记忆", KIND_CURRENT: "当前记忆", KIND_DIARY: "日记碎碎念"}


# ---------------------------------------------------------------- 指纹

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _db_digest(path: Path) -> str:
    """逻辑指纹：不哈希文件字节（页布局会变），而是哈希行内容。"""
    h = hashlib.sha256()
    c = sqlite3.connect(str(path))
    try:
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for t in tables:
            h.update(f"[{t}]".encode())
            try:
                for row in c.execute(f"SELECT * FROM {t}"):
                    h.update(repr(row).encode("utf-8", "replace"))
            except sqlite3.Error:
                continue
    finally:
        c.close()
    return h.hexdigest()


def payload_hash(reg, kind: str) -> str:
    """这一类备份当前的内容指纹。拿去和上一次比，就能判断"有没有新东西"。"""
    h = hashlib.sha256()
    h.update(f"{kind}|{FMT}|".encode())
    if kind == KIND_DIARY:
        root = reg.diary_dir
        for p in sorted(root.rglob("*.md")):
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
        return h.hexdigest()
    for sp in reg.list(include_archived=True):
        dbp = reg.db_path(sp["id"])
        if dbp.exists():
            h.update(sp["id"].encode())
            h.update(_db_digest(dbp).encode())
    return h.hexdigest()


def last_of_kind(dest_dir: str | Path | None, kind: str) -> dict | None:
    for item in list_backups(dest_dir):
        if item.get("kind") == kind and not item.get("broken"):
            return item
    return None


# ---------------------------------------------------------------- 快照

def _snapshot(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(str(src))
    d = sqlite3.connect(str(dst))
    try:
        try:
            s.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")   # 单文件模式，备份里不需要 -wal
        d.commit()
    finally:
        d.close()
        s.close()


def _trim_current(path: Path) -> dict:
    """把完整快照削成"当前状态"。只动快照，不动原库。"""
    c = sqlite3.connect(str(path))
    dropped = {}
    try:
        def cut(sql: str, key: str):
            dropped[key] = c.execute(sql).rowcount

        cut("DELETE FROM buffer", "buffer")
        cut("DELETE FROM recall_receipts", "receipts")
        cut("DELETE FROM idle_log", "idle_log")
        cut("DELETE FROM discard_stats", "discard")
        cut("DELETE FROM self_evidence", "evidence")
        cut("DELETE FROM self_deltas", "deltas")
        cut("DELETE FROM fragments WHERE state='cold'", "cold_fragments")
        cut("DELETE FROM anchors WHERE state='cold'", "cold_anchors")
        cut("DELETE FROM anchors WHERE id NOT IN "
            "(SELECT anchor_id FROM fragments WHERE anchor_id IS NOT NULL)", "orphan_anchors")
        c.commit()
        c.execute("VACUUM")
    finally:
        c.close()
    return dropped


# ---------------------------------------------------------------- 创建

def create(reg, dest_dir: str | Path | None = None, note: str = "",
           kind: str = KIND_FULL, dedupe: bool = True) -> dict:
    kind = kind if kind in KINDS else KIND_FULL
    dest = Path(dest_dir) if dest_dir else (
        reg.diary_backup_dir if kind == KIND_DIARY else reg.backup_dir)
    dest.mkdir(parents=True, exist_ok=True)
    if kind == KIND_DIARY:
        archive.sync_all(reg)          # 先把最新的落进归档，指纹才和实际内容一致

    phash = payload_hash(reg, kind)
    if dedupe:
        prev = last_of_kind(dest, kind)
        if prev:
            try:
                with zipfile.ZipFile(prev["path"]) as z:
                    m = json.loads(z.read("manifest.json").decode("utf-8"))
                if m.get("payload_hash") == phash:
                    return {"ok": True, "deduped": True, "kind": kind,
                            "kind_label": KIND_LABEL[kind], "path": prev["path"],
                            "name": prev["name"], "bytes": prev["bytes"],
                            "reason": f"内容没变，复用 {prev['name']}"}
            except Exception:
                pass

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = dest / f"dsmem-{kind}-{stamp}-{secrets.token_hex(2)}.zip"
    while out.exists():
        out = dest / f"dsmem-{kind}-{stamp}-{secrets.token_hex(2)}.zip"

    tmp = Path(tempfile.mkdtemp(prefix="dsmem-bk-"))
    files: dict[str, dict] = {}
    trimmed: dict[str, dict] = {}
    try:
        if kind == KIND_DIARY:
            src = reg.diary_dir
            for p in sorted(src.rglob("*")):
                if p.is_file() and not p.name.startswith("."):
                    rel = ("diary/" + p.relative_to(src).as_posix())
                    target = tmp / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, target)
                    files[rel] = {"sha256": _sha256(target), "bytes": target.stat().st_size}
        else:
            for sp in reg.list(include_archived=True):
                dbp = reg.db_path(sp["id"])
                if not dbp.exists():
                    continue
                snap = tmp / "spaces" / f"{sp['id']}.db"
                _snapshot(dbp, snap)
                if kind == KIND_CURRENT:
                    trimmed[sp["id"]] = _trim_current(snap)
                files[f"spaces/{sp['id']}.db"] = {"sha256": _sha256(snap),
                                                  "bytes": snap.stat().st_size}
            shutil.copy2(reg.reg_path, tmp / "spaces.json")
            files["spaces.json"] = {"sha256": _sha256(tmp / "spaces.json")}

        manifest = {"app": APP, "format": FMT, "kind": kind, "kind_label": KIND_LABEL[kind],
                    "payload_hash": phash, "created_at": time.time(),
                    "created_readable": datetime.now().isoformat(timespec="seconds"),
                    "note": note, "files": files, "trimmed": trimmed,
                    "spaces": [s["id"] for s in reg.list(include_archived=True)]}
        (tmp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for p in sorted(tmp.rglob("*")):
                if p.is_file():
                    z.write(p, p.relative_to(tmp).as_posix())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return {"ok": True, "deduped": False, "path": str(out), "name": out.name,
            "bytes": out.stat().st_size, "kind": kind, "kind_label": KIND_LABEL[kind],
            "trimmed": trimmed, "spaces": len([k for k in files if k.startswith("spaces/")]),
            "files": len(files), "manifest": manifest}


# ---------------------------------------------------------------- 列表

def _read_manifest(z: zipfile.ZipFile) -> dict | None:
    try:
        return json.loads(z.read("manifest.json").decode("utf-8"))
    except Exception:
        return None


def _kind_from_name(name: str) -> str:
    if name.startswith("dsmem-current-"):
        return KIND_CURRENT
    if name.startswith("dsmem-diary-"):
        return KIND_DIARY
    return KIND_FULL


def list_backups(dest_dir: str | Path | None = None, limit: int = 120) -> list[dict]:
    if not dest_dir:
        return []
    d = Path(dest_dir)
    if not d.exists():
        return []
    out = []
    names = sorted([p for p in d.glob("dsmem-*.zip") if p.is_file()],
                   key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    for p in names:
        if not p.name.startswith(("dsmem-full-", "dsmem-current-", "dsmem-diary-",
                                  "dsmem-backup-")):
            continue
        item = {"name": p.name, "path": str(p), "bytes": p.stat().st_size,
                "kb": round(p.stat().st_size / 1024, 1),
                "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "kind": _kind_from_name(p.name)}
        try:
            with zipfile.ZipFile(p) as z:
                m = _read_manifest(z)
                if m:
                    item.update({"spaces": len(m.get("spaces", [])), "note": m.get("note", ""),
                                 "format": m.get("format"), "created": m.get("created_readable"),
                                 "kind": m.get("kind", item["kind"]),
                                 "files": len(m.get("files") or {})})
        except Exception:
            item["broken"] = True
        item.setdefault("kind_label", KIND_LABEL[item["kind"]])
        out.append(item)
    return out


def all_items(reg) -> list[dict]:
    """记忆备份 + 日记备份，两处目录合起来看。"""
    return list_backups(reg.backup_dir) + list_backups(reg.diary_backup_dir)


def group(items: list[dict]) -> dict:
    return {KIND_FULL: [i for i in items if i.get("kind") == KIND_FULL],
            KIND_CURRENT: [i for i in items if i.get("kind") == KIND_CURRENT],
            KIND_DIARY: [i for i in items if i.get("kind") == KIND_DIARY]}


# ---------------------------------------------------------------- 校验 / 恢复

def verify(zip_path: str | Path) -> dict:
    p = Path(zip_path)
    if not p.exists():
        return {"ok": False, "error": "文件不存在"}
    bad, checked = [], 0
    try:
        with zipfile.ZipFile(p) as z:
            m = _read_manifest(z)
            if not m:
                return {"ok": False, "error": "缺少 manifest，可能不是本程序生成的备份"}
            with tempfile.TemporaryDirectory() as td:
                for name, meta in (m.get("files") or {}).items():
                    if not name.endswith((".db", "spaces.json", ".md")):
                        continue
                    checked += 1
                    target = Path(td) / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(name) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    if _sha256(target) != meta.get("sha256"):
                        bad.append(name)
            return {"ok": not bad, "checked": checked, "bad": bad,
                    "spaces": m.get("spaces", []), "created": m.get("created_readable"),
                    "format": m.get("format"), "kind": m.get("kind", _kind_from_name(p.name)),
                    "kind_label": m.get("kind_label", "完整记忆")}
    except zipfile.BadZipFile:
        return {"ok": False, "error": "压缩包损坏"}


def _restore_diary(z: zipfile.ZipFile, reg) -> list[str]:
    """日记只增不删：已存在的文件不覆盖，改写后的存成同名 .restored-时间。"""
    added, kept = [], []
    root = reg.diary_dir
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for name in z.namelist():
        if not name.startswith("diary/") or not name.endswith(".md"):
            continue
        rel = name[len("diary/"):]
        if ".." in rel or rel.startswith("/"):
            continue
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = z.read(name)
        if not target.exists():
            target.write_bytes(data)
            added.append(rel)
        elif target.read_bytes() != data:
            alt = target.with_name(f"{target.stem}.restored-{stamp}.md")
            alt.write_bytes(data)
            kept.append(alt.relative_to(root).as_posix())
    return added + kept


def restore(zip_path: str | Path, reg, dest_dir: str | Path | None = None) -> dict:
    v = verify(zip_path)
    if not v.get("ok"):
        return {"ok": False, "error": "校验未通过", "detail": v}
    kind = v.get("kind", KIND_FULL)

    if kind == KIND_DIARY:
        with zipfile.ZipFile(zip_path) as z:
            touched = _restore_diary(z, reg)
        return {"ok": True, "kind": kind, "kind_label": v.get("kind_label"),
                "restored": touched, "spaces": len(reg.data["spaces"]),
                "pre_backup": None, "note": "日记只增不删，冲突文件另存为 *.restored-*"}

    pre = create(reg, dest_dir, note="pre-restore 自动备份", kind=KIND_FULL, dedupe=False)
    restored = []
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if name.startswith("spaces/") and name.endswith(".db"):
                sid = Path(name).stem
                if "/" in sid or "\\" in sid or ".." in sid:
                    continue
                target = reg.db_path(sid)
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(name) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                for suf in ("-wal", "-shm"):
                    stale = Path(str(target) + suf)
                    if stale.exists():
                        try:
                            stale.unlink()
                        except OSError:
                            pass
                restored.append(sid)
        if "spaces.json" in z.namelist():
            incoming = json.loads(z.read("spaces.json").decode("utf-8"))
            merged = {s["id"]: s for s in reg.data["spaces"]}
            for s in incoming.get("spaces", []):
                merged.setdefault(s["id"], s)
            reg.data["spaces"] = list(merged.values())
            if reg.data.get("active") not in merged:
                reg.data["active"] = next(iter(merged), "default")
            reg._save()
    return {"ok": True, "restored": restored, "pre_backup": pre["path"],
            "spaces": len(reg.data["spaces"]), "kind": kind, "kind_label": v.get("kind_label")}


def prune(dest_dir: str | Path, keep: int = 10, kind: str | None = None) -> dict:
    """只删自己生成的备份。日记备份默认不动（要删得显式指定 kind='diary'）。"""
    d = Path(dest_dir)
    files = sorted([p for p in d.glob("dsmem-*.zip") if p.is_file()],
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if kind:
        files = [p for p in files if _kind_from_name(p.name) == kind]
    else:
        files = [p for p in files if _kind_from_name(p.name) != KIND_DIARY]
    removed = []
    for p in files[keep:]:
        p.unlink()
        removed.append(p.name)
    return {"removed": removed, "kept": min(len(files), keep), "kind": kind or "memory"}
