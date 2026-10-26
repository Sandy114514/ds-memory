# 贡献

## 本地跑起来

```bash
git clone <your-repo-url> ds-memory && cd ds-memory
python -m pip install -e .
python selfcheck.py          # 74 项自检，应全绿
python demo.py               # 单分组生命周期
python demo_spaces.py --keep # 多分组 / 碎碎念 / 备份，保留数据供界面查看
python -m dsmem.webui        # http://127.0.0.1:8765/
```

需要 Python 3.10+，无第三方依赖。

## 提交前

1. `python selfcheck.py` 必须全过；
2. 新增行为请同时在 `selfcheck.py` 里加一条断言（这个项目靠它防回归）；
3. 改动涉及设计取舍时，同步更新 `docs/DESIGN.md`；
4. 不要提交 `*.db`、`*.zip`、`.dsmem/`、`__pycache__/`（`.gitignore` 已覆盖）。

## 写一个插件

见 README「加一个空闲插件」。要点：

- `manifest.json` 里 `api_version` 必须 ≤ 宿主版本，否则会被静默跳过；
- `hook.py` 的 `run(ctx)` 返回 `{"note", "tokens", "activities"}`；
- 只读自己声明过的层级（`tier_access`），不申请 `transcript` 就碰不到原话；
- 产出型任务（例如生成碎碎念）请显式返回 `"activities": []`，否则"我刚说了两句话"会变成日记素材。

## 代码风格

- 只用标准库，不引入依赖（这是项目能单文件跑起来的前提）；
- 注释写"为什么"，不写"是什么"；
- 面向用户的中文文案避免夸张语气词。
