# dsmem

**给对话模型装一套"会忘、会分层、会自己学评级"的记忆。**

dsmem 不把聊天记录整段塞回上下文，也不依赖向量库。它让记忆按曲线衰减：重要的沉进长期层，
琐碎的连摘要都不留，被用到的会活得更久；人格由长期对话慢慢积分出来，单次情绪改不动它。
纯 Python 标准库，一个分组一个 SQLite 文件，离线可跑。

[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-3b6d11)](LICENSE)
[![Dependencies](https://img.shields.io/badge/dependencies-0-185fa5)](pyproject.toml)
[![Self checks](https://img.shields.io/badge/self--checks-74%20passed-3b6d11)](selfcheck.py)

- 项目简介（不同长度）：[`docs/INTRO.md`](docs/INTRO.md)
- 设计文档：[`docs/DESIGN.md`](docs/DESIGN.md)
- 实测输出：[`docs/sample_output.txt`](docs/sample_output.txt)、[`docs/sample_spaces_output.txt`](docs/sample_spaces_output.txt)
- 变更记录：[`CHANGELOG.md`](CHANGELOG.md)

## 安装

```bash
git clone <your-repo-url> ds-memory && cd ds-memory
python -m pip install -e .        # 需要 Python 3.10+
```

装完会得到三个命令，等价于 `python -m dsmem.<module>`：

```bash
dsmem-init  --db memory.db --preset 长期伴侣   # 初始化
dsmem-webui                                    # 管理界面 http://127.0.0.1:8765/
dsmem-daily                                    # 每日备份（可挂计划任务）
```

不装也能直接跑：所有模块都只依赖标准库。

## 快速开始

```bash
# 1) 初始化（只回答 4 个倾向问题，其余参数自动推导；不设容量上限）
python -m dsmem.settings --db memory.db --preset 长期伴侣

# 2) 跑完整生命周期演示（会重建 demo_memory.db）
python demo.py

# 3) 交互式自定义 setting
python -m dsmem.settings --db memory.db --interactive

# 4) 管理界面：分组 / 日记碎碎念 / 备份恢复
python -m dsmem.webui            # http://127.0.0.1:8765/

# 5) 自检 74 项（隔离 / 归属 / 连续性 / 备份恢复 / 界面 API）
python selfcheck.py
```

预设：`工具人` / `长期伴侣` / `工作助理` / `创作缪斯`。

## 分组：一个分组一个库

分组在界面顶部下拉框里手动选择（或 `POST /api/bind` 把某个群钉死到某组）。
每个分组是**独立的 `.db` 文件**，物理隔离，漏写 WHERE 也不可能串记忆。

```
~/.dsmem/spaces.json        分组清单 + 当前选择 + 会话绑定
~/.dsmem/spaces/<id>.db     该分组全部记忆
~/.dsmem/diary/<id>/        日记与碎碎念的按月归档（只追加）
~/.dsmem/backups/*.zip      备份
```

```python
from dsmem.spaces import SpaceRegistry
from dsmem import identity
reg = SpaceRegistry()                    # 默认 ~/.dsmem
sp  = reg.create("工作", "group")         # 分组是容器，里面可以有多个群聊和用户
store = reg.open(sp["id"])

g   = identity.add_member(store, "group", "技术群")
u1  = identity.add_member(store, "user", "小王")
u2  = identity.add_member(store, "user", "小李")
identity.link(store, g["id"], u1["id"])   # 小王在群里 → 和群共享记忆

identity.resolve(store, u1["id"])  # {'write': 'g:技术群', 'shared': True, ...}
identity.resolve(store, u2["id"])  # {'write': 'u:小李',   'shared': False, ...}
identity.advise(store, [g["id"]], [u2["id"]])   # 提醒：小李不在群里，只能拿到独立记忆
```

人格不跟着分池：一个分组只有一份 `self_model`，群里和私聊的证据都投到同一处，
所以"在群里更熟"会带到私聊，反之亦然。

## 碎碎念：人格给默认值，用户可以覆盖

```python
from dsmem import murmurs
murmurs.profile(store, cfg)      # {per_day: 3.05, length: 52, tone: '俏皮'}
murmurs.generate(store, cfg, now)  # 存库 + 按月归档
```

| 分组 | 人格 | 每天 | 每条 | 口吻 |
|---|---|---|---|---|
| 技术群 | 幽默 .80 / 亲密 .75 | 3.05 条 | 52 字 | 俏皮 |
| 家人群 | 幽默 .30 / 亲密 .90 | 2.88 条 | 35 字 | 亲近 |
| 项目A | 幽默 .20 / 直接 .85 | 1.46 条 | 10 字 | 短促 |

内容优先级：刚清理过 → 说"遗忘"；有日志 → 说那件事；深夜 → 说深夜；否则随便说两句。

**数量与体积可以自己调**（界面「日记 · 碎碎念」页的调节卡片，值按分组保存）：

```python
store.meta_set("config", ...)          # 或界面上直接填
# murmur_per_day: 0 = 跟随人格，>0 = 每天几条（上限 30）
# murmur_length : 0 = 跟随人格，>0 = 单条字数上限（上限 200）
```

条数是目标数量，字数是上限。字数调大时只补「和当前话题相关」的一句，不会为了凑字复读模板；
口吻仍由人格决定。自动模式下每天最多 8 条。

## 备份与恢复：三种版本，不带重复，每天一份

```python
from dsmem import backup, daily

backup.create(reg, "D:/我的备份", kind=backup.KIND_FULL)      # 完整：一个字不删
backup.create(reg, "D:/我的备份", kind=backup.KIND_CURRENT)   # 当前：只留还记得的 + 此刻人格
backup.create(reg, "D:/日记备份", kind=backup.KIND_DIARY)      # 日记：纯文本，单独一摞
backup.verify(path)                        # 校验 sha256，不是"看文件名对不对"
backup.restore(path, reg)                  # 先自动做一次完整备份，再覆盖；日记只增不删
daily.run(reg)                             # 当天没备过就各做一份，内容没变的跳过
```

| | 完整记忆 | 当前记忆 |
|---|---|---|
| 冷片段 / 冷锚点 | 保留 | 删 |
| 人格证据流水、历史版本 | 保留 | 清（只留当前值） |
| 原始轮次、召回审计、空闲流水 | 保留 | 清 |
| 日志 / 日记 / 碎碎念 / 月记 | 保留 | 保留 |

**去重**：zip 里不再夹 Markdown 副本；建备份前算内容指纹，和上一次相同的直接复用，不生成新文件。

**日记与碎碎念单独存放、只追加、不能删**：

```
~/.dsmem/diary/<分组>/journal/2026-10.md     # 日记
~/.dsmem/diary/<分组>/murmurs/2026-10.md     # 碎碎念
~/.dsmem/diary-backups/dsmem-diary-*.zip     # 独立备份
```

遗忘曲线只删 `fragments`，不碰这两张表；`prune()` 默认跳过日记备份；恢复日记只增不删，
冲突文件另存为 `*.restored-时间.md`。

每日自动有三种触发：界面开着时后台线程每 10 分钟检查一次；`python -m dsmem.daily` 可挂计划任务；
界面上有「立即执行今日备份」。同一天只备一次。

界面上备份页分成两摞列表（完整 / 当前），可分别校验、恢复，恢复前二次确认。

## 容量

**没有容量上限**，配置里也不存在这个字段。删记忆只剩遗忘曲线一个原因，不会因为"超预算"被淘汰。
想让它忘得快一点就调 `forget`，不想删就把 `forget` 调低。

旧版本存在 `meta.config` 里的 `max_bytes` 会在打开库时被 `Store._migrate()` 抹掉，
界面上不会再出现 "上限 4096 KB" 这类字样。

## 连续性：删得掉细节，删不掉时间

片段过期被删之前，先折进月度章节（`chapters`）。所以一年后问"三月在忙什么"，
即使细节全没了，也能答出"那次在比 SQLite 和 Postgres 的运维成本"。缺口会如实显示，不编造。

## 五个核心动作

```python
from dsmem import Store, config_for_preset
from dsmem.consolidate import nightly
from dsmem import recall, journal, prompt, idle

cfg   = config_for_preset("长期伴侣")
store = Store("memory.db")

# 每轮：先召回（按 token 预算分层读，默认不碰原文），再落 L0
r = recall.recall(store, cfg, "我讨厌什么？", now)      # r.text / r.tokens / r.layers
sys_prompt = prompt.build_system_prompt(store, cfg, now)
store.add_turn("sess-1", "user", "……", now)

# 每晚：固化 L0 → 丢弃/L1/L2，再写日记、提炼日志
nightly(store, cfg, now, "2026-10-27")
journal.write_journal(store, cfg, "2026-10-27", now, frags)

# 每周：用真实召回行为校准评级权重（v1 → v2 → …）
rating.calibrate(store, cfg)

# 空闲：插件自行运行，产出只进日记
idle.run_idle(store, cfg, now, "plugins")
```

## 三层记忆

| 层 | 存什么 | 何时消失 |
|---|---|---|
| L0 buffer | 原始对话轮 | 36 小时，或夜间固化后立刻删 |
| L1 fragments | 六要素 + 一句提要 | R<0.25 降级、R<0.08 且超期删除 |
| L2 anchors | 完整对话（zlib，约 6 折） | 常驻；仅"要原话"时解压 |
| logs | 日记后提炼的重点 | 长期，召回主入口 |
| self_model | 人格当前状态 + 等级 | 不自动删，全程版本化 |
| self_evidence | 人格证据（每段对话捐一点） | 200 天滚动清理 |

评定阈值（`长期伴侣`）：`<0.33` 直接删，`0.33–0.74` 进 L1，`≥0.74` 进 L2 并保留原话。

## 人格：会被改变，但很慢

人格不是封闭设定。每段对话都会折算成"人格证据"（强度 × 情境 × 持续时间），
攒够天数和跨度才推动等级，且受每周 / 每季度预算限制：

```python
from dsmem import personality
personality.observe_session(store, cfg, turns, "2026-10-27", now)   # 落证据
personality.integrate(store, cfg, now, "2026-10-27")                # 夜间积分
personality.evidence_ledger(store, "humor")                         # 看证据流水
personality.history(store, "directness")                            # 看改动记录
```

| 场景 | 结果 |
|---|---|
| 一次吐槽"你太冷了" | 不动（证据天数/跨度不足） |
| 明确说"以后别这么闷" | 立刻动 0.05（快车道，幅度仍受预算限） |
| 连续 60 天深夜长谈 | 幽默 0.35 → 0.605，亲密度 0.50 → 0.755 |
| 第 40 天单独发火一次 | 方向不变，只抵消约 0.8 天积累 |
| 每 3 天要求"别绕弯子"（核心层） | 满 45 天跨度才生效，60 天共挪 0.018 |

核心层（称呼/语气/价值/边界）惯性 0.9，每 90 天最多变 0.10；用户直接要求改核心设定仍走 `propose_delta` 挂起待批准。

## 加一个空闲插件

在 `plugins/<name>/` 放两个文件：

**manifest.json**
```json
{"name": "my-task", "api_version": 1, "version": "1.0.0",
 "capabilities": ["reflect"], "tier_access": ["L1"],
 "token_budget": 200, "priority": 5, "cooldown_s": 1800}
```

**hook.py**
```python
def run(ctx):          # ctx = {store, cfg, now, budget, scope}
    return {"note": "一句话产出", "tokens": 80,
            "activities": [{"kind": "reflect",          # prune|review|reflect|note
                            "title": "重读了「向量库选型」，想到它当时的分量",
                            "weight": 0.6}]}
```

`activities` 会写进行为总线，**日记和碎碎念会从中取材**：日记会把"今天整理了几条旧记忆"
写进正文，碎碎念会优先拿它当话题。不传就只留 `note`，做产出型任务（比如生成碎碎念本身）时传空列表 `[]`。

约束：不给 transcript 权限就碰不到原话；产出只写日志；异常被隔离；`api_version` 高于宿主则跳过。

## 仓库结构

```
ds-memory/
├── dsmem/                     # 内核（16 个模块 + schema.sql）
│   ├── store.py               # SQLite 存取、空间统计、旧库迁移
│   ├── spaces.py              # 分组注册表：一空间一库
│   ├── identity.py            # 群聊/用户成员与记忆池归属
│   ├── rating.py              # 7 维评级、自校准、六要素
│   ├── decay.py               # 遗忘曲线与状态机
│   ├── consolidate.py         # 夜间固化 / 降级 / GC
│   ├── continuity.py          # 月度章节，保证时间线不断
│   ├── personality.py         # 人格慢通道
│   ├── murmurs.py             # 碎碎念（可调条数与字数）
│   ├── journal.py             # 日记 + 日志
│   ├── archive.py             # 只追加归档
│   ├── backup.py              # 三种备份版本 + 去重
│   ├── daily.py               # 每日备份任务
│   ├── webui.py               # 零依赖管理界面
│   └── ...
├── plugins/example-reflection # 示例空闲插件
├── demo.py                    # 单分组生命周期演示
├── demo_spaces.py             # 多分组 / 碎碎念 / 备份演示
├── selfcheck.py               # 74 项自检
├── docs/DESIGN.md             # 设计文档
├── pyproject.toml
└── .github/workflows/ci.yml   # Python 3.10–3.13 跑自检
```

## 许可

[MIT](LICENSE)。可商用、可修改、可闭源分发，保留版权声明即可。
