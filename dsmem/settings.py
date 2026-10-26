"""最开始的人为 setting：4 个问题确定倾向，随后一切参数由系统推导。

不设空间上限，所以也不问「最多占多少 MB」——问了也做不到的事不问。
"""
import argparse
import json
import sys
import time
from . import personality, rating
from .config import PRESETS, UserNeeds, build_config
from .store import Store


QUESTIONS = [
    ("forget", "希望它多快忘掉琐事？（0=几乎不忘，1=聊完就忘）", 0.35),
    ("detail", "需要它记住细节到什么程度？（0=只要结论，1=要原话）", 0.6),
    ("journal", "日记风格？散文 / 要点 / 意识流 / 极简", "散文"),
    ("personality_strength", "人格要多强？（0=中性工具，1=稳定角色）", 0.75),
]


def interview(answers: dict | None = None, interactive: bool = False) -> UserNeeds:
    """交互式或程序化完成 setting。"""
    a = dict(answers or {})
    if interactive:
        print("\n=== 记忆初始化 setting（回车使用默认值）===\n")
        for key, q, default in QUESTIONS:
            raw = input(f"{q} [{default}] ").strip()
            if raw:
                a[key] = raw
    needs = UserNeeds(
        forget=float(a.get("forget", 0.35)),
        detail=float(a.get("detail", 0.6)),
        journal=str(a.get("journal", "散文")),
        personality_strength=float(a.get("personality_strength", 0.75)),
    )
    return needs


def init_memory(db_path: str, needs: UserNeeds, now: float | None = None) -> dict:
    now = now or time.time()
    cfg = build_config(needs)
    store = Store(db_path)
    existing = store.q1("SELECT version FROM rubric ORDER BY version DESC LIMIT 1")
    if existing:
        store.close()
        return {"created": False, "reason": "已初始化，跳过", "version": existing["version"]}
    rub = rating.bootstrap_rubric(needs)
    rating.save_rubric(store, rub)
    personality.seed(store, now)
    store.meta_set("config", cfg.to_json())
    store.meta_set("needs", {
        "forget": needs.forget, "detail": needs.detail, "journal": needs.journal,
        "personality_strength": needs.personality_strength})
    store.meta_set("created_at", now)
    store.close()
    return {"created": True, "rubric_version": rub["version"], "weights": rub["weights"],
            "config": cfg.to_json()}


def main(argv=None):
    ap = argparse.ArgumentParser(description="dsmem 记忆初始化")
    ap.add_argument("--db", default="memory.db")
    ap.add_argument("--preset", choices=list(PRESETS) + ["自定义"], default="自定义")
    ap.add_argument("--interactive", action="store_true")
    args = ap.parse_args(argv)

    if args.preset != "自定义":
        needs = PRESETS[args.preset]
    else:
        needs = interview(interactive=args.interactive)
    out = init_memory(args.db, needs)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
