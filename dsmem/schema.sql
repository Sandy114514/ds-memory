-- dsmem: 三层记忆 + 自生成评级 + 人格版本库
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);

-- L0 缓冲层：原始对话轮，按 TTL 清空
CREATE TABLE IF NOT EXISTS buffer (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  ts REAL NOT NULL,
  promoted INTEGER NOT NULL DEFAULT 0,
  scope TEXT NOT NULL DEFAULT 'main'
);
CREATE INDEX IF NOT EXISTS idx_buffer_ts ON buffer(ts);
CREATE INDEX IF NOT EXISTS idx_buffer_sess ON buffer(session_id);

-- L2 核心锚点：保留当时完整对话（zlib 压缩），默认不读，仅细节召回时解压
CREATE TABLE IF NOT EXISTS anchors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT,
  created_at REAL,
  title TEXT,
  salience REAL,
  dims TEXT,          -- json: 各维度得分
  reason TEXT,        -- 评级理由（一行）
  tags TEXT,
  entities TEXT,
  transcript BLOB,    -- zlib(json)
  raw_bytes INTEGER,
  comp_bytes INTEGER,
  retrievals INTEGER DEFAULT 0,
  last_access REAL,
  state TEXT DEFAULT 'hot',   -- hot | cold
  pinned INTEGER DEFAULT 0,
  scope TEXT NOT NULL DEFAULT 'main'
);

-- L1 情节层：六要素骨架 + 一句提要，随时间降级
CREATE TABLE IF NOT EXISTS fragments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  anchor_id INTEGER,
  created_at REAL,
  day TEXT,
  who TEXT, what TEXT, whn TEXT, whr TEXT, why TEXT, how TEXT,
  gist TEXT,
  salience REAL,
  dims TEXT,
  stability_h REAL,
  last_review REAL,
  retrievals INTEGER DEFAULT 0,
  state TEXT DEFAULT 'full',  -- full | skeleton | cold
  pinned INTEGER DEFAULT 0,
  tags TEXT,
  entities TEXT,
  scope TEXT NOT NULL DEFAULT 'main'
);
CREATE INDEX IF NOT EXISTS idx_frag_day ON fragments(day);
CREATE INDEX IF NOT EXISTS idx_frag_scope ON fragments(scope, day);
CREATE INDEX IF NOT EXISTS idx_frag_state ON fragments(state);

-- 日志层：日记完成后提炼的重点，召回主入口
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT,
  kind TEXT,      -- event | insight | commitment | preference | lesson | relation
  text TEXT,
  weight REAL,
  refs TEXT,      -- json: fragment ids
  tags TEXT,
  created_at REAL,
  scope TEXT NOT NULL DEFAULT 'main'
);
CREATE INDEX IF NOT EXISTS idx_logs_day ON logs(day);
CREATE INDEX IF NOT EXISTS idx_logs_kind ON logs(kind);

CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT,
  body TEXT,
  mood TEXT,
  bytes INTEGER,
  created_at REAL,
  scope TEXT NOT NULL DEFAULT 'main',
  UNIQUE(day, scope)
);

-- 人格：当前状态。不是封闭设定，而是长期积分的结果
CREATE TABLE IF NOT EXISTS self_model (
  key TEXT PRIMARY KEY,
  value TEXT,
  layer TEXT,          -- core（慢且难） | evolving（可较快演化）
  inertia REAL,        -- 0..1，越大越难改
  level REAL,          -- 可量化特质的当前等级 0..1
  version INTEGER DEFAULT 1,
  updated_at REAL
);

-- 人格证据：每段对话捐一点，会衰减，靠积累推动等级
CREATE TABLE IF NOT EXISTS self_evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trait TEXT,
  ts REAL,
  day TEXT,
  intensity REAL,      -- 强度 0..1
  context REAL,        -- 情境权重 0..1（深夜/长谈/情绪浓度）
  direction INTEGER,   -- +1 / -1
  explicit INTEGER DEFAULT 0,
  note TEXT,
  ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_ev_trait ON self_evidence(trait, ts);

CREATE TABLE IF NOT EXISTS self_deltas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL,
  trait TEXT,
  layer TEXT,
  before TEXT,
  after TEXT,
  reason TEXT,
  evidence TEXT,
  applied INTEGER DEFAULT 1,   -- 0 = 待批准 / 被预算拦截
  version INTEGER,
  d_before REAL,
  d_after REAL,
  pressure REAL,
  evidence_n INTEGER,
  span_days REAL
);

-- 评级体系自身的版本（初始由模型生成，之后自校准）
CREATE TABLE IF NOT EXISTS rubric (
  version INTEGER PRIMARY KEY,
  created_at REAL,
  weights TEXT,
  notes TEXT,
  source TEXT          -- bootstrap | calibrate
);

CREATE TABLE IF NOT EXISTS recall_receipts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL,
  query TEXT,
  tokens INTEGER,
  layers TEXT,
  hit_ids TEXT
);

CREATE TABLE IF NOT EXISTS plugins (
  name TEXT PRIMARY KEY,
  version TEXT,
  capabilities TEXT,
  tier_access TEXT,
  token_budget INTEGER,
  priority INTEGER,
  cooldown_s INTEGER,
  last_run REAL,
  storage_quota INTEGER,
  enabled INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS idle_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL,
  plugin TEXT,
  task TEXT,
  tokens INTEGER,
  note TEXT
);

CREATE TABLE IF NOT EXISTS discard_stats (
  day TEXT PRIMARY KEY,
  sessions INTEGER,
  fragments INTEGER,
  bytes INTEGER
);

-- 连续性章节：被删掉的记忆先折进月度章节，保证时间线不断档
CREATE TABLE IF NOT EXISTS chapters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL DEFAULT 'main',
  period TEXT,               -- 2026-10
  text TEXT,
  tags TEXT,
  count INTEGER DEFAULT 0,   -- 当年这个月发生过多少件事
  deleted_n INTEGER DEFAULT 0,
  salience REAL DEFAULT 0,
  created_at REAL,
  updated_at REAL,
  UNIQUE(period, scope)
);

-- 碎碎念：一天可以很多条，按日期索引
CREATE TABLE IF NOT EXISTS murmurs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT,
  ts REAL,
  text TEXT,
  tone TEXT,
  topic TEXT,
  trigger TEXT,
  tokens INTEGER
);
CREATE INDEX IF NOT EXISTS idx_murmur_day ON murmurs(day);

-- 空间内的成员：群聊 / 个人用户
CREATE TABLE IF NOT EXISTS members (
  id TEXT PRIMARY KEY,        -- g:xxx / u:xxx
  kind TEXT NOT NULL,         -- group | user
  name TEXT NOT NULL,
  ext TEXT,                   -- 外部 chat_id / user_id
  note TEXT,
  private INTEGER DEFAULT 0,  -- 1 = 即使在群里也只用自己的私密池
  home_group TEXT,            -- 属于多个群时，手动钉一个
  created_at REAL
);

-- 用户 <-> 群 的归属关系：在群里就共享群记忆
CREATE TABLE IF NOT EXISTS membership (
  group_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  joined_at REAL,
  PRIMARY KEY(group_id, user_id)
);

-- 插件行为总线：日记/碎碎念从这里取素材
CREATE TABLE IF NOT EXISTS activities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL,
  day TEXT,
  scope TEXT NOT NULL DEFAULT 'main',
  plugin TEXT,
  kind TEXT,        -- prune | review | reflect | murmur | note
  title TEXT,       -- 一句话结果，直接可读
  detail TEXT,
  weight REAL DEFAULT 0.5,
  consumed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_act_day ON activities(day, scope);
