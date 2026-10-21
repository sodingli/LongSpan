# Longspan · 长任务工坊

> 把一句话变成成品的长任务引擎。后端是 **Harness 执行内核 + 可插拔技能包**，前端是三栏工作台。
>
> 需求文档见 `docs/MRD-Longspan.md`；技术继承方案见 `docs/技术复用与技术继承方案.md`。

---

## 这是什么

用户只给约束（甚至只给一句话），系统通过**加载不同技能包**跑完一条长流水线，产出成品：

| 技能 | 状态 | 产物 |
|---|---|---|
| 🎬 **漫剧（动态漫）** | **M0.5 已跑通** | 剧本 / 分镜 / 配音方案 → 竖屏短视频 |
| 📊 PPT | M2（引擎已 vendor） | 可编辑的原生 pptx |
| 📝 论文 | M3 | docx + 参考文献库 |

**核心设计**：Harness 循环里**没有任何技能名**。阶段顺序、验收标准、是否要人确认、最多重做几次，
全部读自 `backend/skills/*/manifest.yaml`。**新增任务类型 = 新增一个目录，不动主程序。**

---

## 快速开始

### 1. 启动后端

```bash
cd backend
PY=/Users/lidashuai5/.workbuddy/binaries/python/envs/default/bin/python
$PY -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

首次运行会自动建库（SQLite，位于 `backend/.data/longspan.db`）并扫描技能目录。

**不配置任何模型 Key 也能跑通全流程** —— 内置 Mock provider，各阶段产物由技能包预置（`skills/*/fixtures/`），
保证「3 分钟出草稿」这条北极星指标可被真实验收。

### 接真实模型（OpenAI 兼容协议：DeepSeek / Kimi / 通义 / 智谱 / OpenAI / 本地 Ollama 均可）

三种方式任选其一，**推荐第一种**：

**A. 在网站里配（推荐）** —— 打开 <http://localhost:5173>，点左下角「配置」：
选服务商一键填充 → 填 API Key →（模型名不确定就点「拉取」，直接从服务商拿当前可用列表）→
点「测试连接」（会真实发一条请求）→ 通过后「保存并启用」。
**改完立即生效、不用重启**；配置写进 `backend/.env`，重启后依然有效（Key 全程脱敏，不回显明文）。

> ⚠️ **模型名要填对**。模型名变动比文档快，例如 DeepSeek 的 `deepseek-chat` / `deepseek-reasoner`
> 已于 2026-07 退役，现行是 `deepseek-flash`（1M 上下文）与 `deepseek-v4-pro`。
> 面板里的「拉取」按钮会调 `GET /v1/models` 问服务商要真实列表，别凭记忆填。

#### 备用模型：主模型挂了自动顶上（强烈建议配）

长任务的痛点是「跑到第 4 阶段主模型挂了，整轮白跑」。配一条**备用模型链**就能兜住：

```
面板里填「备用模型」→ DeepSeek-V4-Flash-0731,Qwen3.8-Flash-Next-W8A8
```

- 逗号 / 分号 / 换行分隔，**按顺序**尝试；
- 只写模型名 = 同一个接口地址下的另一个模型；
- 写 `模型名@https://别的地址/v1` = **连接口一起换**（换服务商 / 换中转）。

判定规则（`providers.py`）：

| 情况 | 行为 |
|---|---|
| 网络抖动 / 5xx / 429 / 输出退化 | 对**当前模型**退避重试 3 次，仍失败才换下一个 |
| 模型名不存在（`model_not_found`、no available channel） | **不重试**，直接换下一个（省掉约 8 秒空等） |
| 备用都挂 | 报错并列出**每个模型各自的失败原因**，不含糊 |

回退发生时不是静默的：事件流里会写明「主模型 X 不可用，已自动回退到 Y」，产物里那个模型名也是真实调用的。

**B. 编辑配置文件**

```bash
cp backend/.env.example backend/.env
# 编辑 backend/.env：至少填 LONGSPAN_LLM_API_KEY / LONGSPAN_LLM_BASE_URL / LONGSPAN_LLM_MODEL
```

**C. 临时环境变量**

```bash
export LONGSPAN_LLM_PROVIDER=openai-compat
export LONGSPAN_LLM_API_KEY=sk-xxx
export LONGSPAN_LLM_BASE_URL=https://api.deepseek.com/v1
export LONGSPAN_LLM_MODEL=deepseek-flash
export LONGSPAN_LLM_FALLBACK_MODELS=deepseek-v4-pro   # 可选：备用模型链
```

配好后**先自检再跑任务**（真实发一条请求，验证地址 + Key + 模型名三者匹配）：

```bash
cd backend
$PY scripts/check_llm.py
$PY scripts/check_llm.py --base-url https://api.moonshot.cn/v1 --api-key sk-xxx --model moonshot-v1-8k
$PY scripts/check_llm.py --fallback "deepseek-v4-pro,xxx@https://other.host/v1"   # 顺带探测备用模型
```

### 2. 启动前端

```bash
cd frontend
export PATH=/Users/lidashuai5/.workbuddy/binaries/node/versions/22.22.2-6/bin:$PATH
npm install     # 若装不动：已内置 .npmrc 指向 registry.npmmirror.com
npm run dev     # → http://localhost:5173
```

> 前端把 `/api` 代理到 `127.0.0.1:8000`（见 `vite.config.ts`），无需处理跨域。

### 3. 命令行冒烟（不启前端也能验证主干）

```bash
cd backend
PY=/Users/lidashuai5/.workbuddy/binaries/python/envs/default/bin/python
$PY scripts/check_llm.py         # 模型/接口连通性自检（配 Key 后先跑这个）
$PY scripts/smoke.py fast        # 主干冒烟：跑完快通道
$PY scripts/e2e_http.py          # 25 项端到端验收（含 SSE、关卡、草稿、升级续跑）
$PY scripts/seed_demo.py         # 灌入 3 个演示任务，覆盖三种状态
```

> 改动数据模型后需要 `rm -rf .data` —— SQLAlchemy 的 `create_all` 不会迁移已有表。

---

## 目录结构

```
backend/
├── app/
│   ├── harness/            # ★ Harness 执行内核（里面没有任何业务流程）
│   │   ├── skill.py        #   技能加载器：解析 manifest，热插拔
│   │   ├── tools.py        #   工具注册表：每个工具声明 failure_modes 替代方案
│   │   ├── agent.py        #   Agent 循环：模型决策 / Harness 安全执行
│   │   └── runner.py       #   Run 状态机：阶段推进 / 门禁 / 检查点 / 配额
│   ├── api/                # runs / skills / events(SSE) / settings(模型配置)
│   ├── llm/providers.py    # 模型 Provider（真实 + 无 Key 的 Mock，可热重载）
│   ├── runtime_config.py   # 把模型配置写进 backend/.env 并热启用（无需重启）
│   ├── models.py           # Run / Step / Event / Artifact / Checkpoint / Gate / Decision
│   ├── events.py           # 事件总线（先落库再广播 → 断线可续传）
│   └── store.py            # StorageAdapter（本地 → S3 可切换）
├── skills/                 # ★ 技能包（新增目录即新增能力）
│   └── comic-drama/        #   漫剧（动态漫）
│       ├── SKILL.md        #     给模型看的流程与铁律
│       ├── manifest.yaml   #     给机器看的：阶段 / 验收 / 门禁 / 配额
│       ├── prompts/        #     各阶段作业指导
│       ├── fixtures/       #     无模型时的确定性演示产物
│       └── references/     #     L3 技术知识包（按需加载）
└── scripts/smoke.py
frontend/
└── src/
    ├── App.tsx             # 三栏工作台
    ├── lib/                # api / types / markdown 渲染 / 格式化
    └── components/         # 侧栏 / 创建面板 / 阶段树 / 关卡卡 / 产物 / 决策留痕 / 事件流
```

---

## 关键机制（都在代码里可查）

| 机制 | 在哪 | 说明 |
|---|---|---|
| **门禁硬校验** | `runner._open_gate` | 关卡不是 UI 按钮，是落盘校验：没有人工批准不能写 completed |
| **断线续传** | `api/events.py` | SSE 用 `Last-Event-ID` 补齐丢失事件；心跳告知"是活着不是卡住" |
| **检查点协议** | `runner._run_stage` | 进阶段先写 `in_progress`；被取代的归档不销毁 |
| **配额治理** | `runner._execute` | 重做 / 打回 / 墙钟 / 预算四重上限（没有配额的自校验就是烧钱循环） |
| **决策留痕** | `decisions` 表 + 前端面板 | 模型每个自主决定记 依据 + 备选 + 置信度，可推翻 |
| **数据化验收** | `runner.evaluate_criteria` | 把「包含"Logline"」「≥900 字」这类自然语言标准变成可机器校验的规则 |
| **禁止静默降级** | `tools.py` 的 `failure_modes` | 任何降级必须写决策留痕 + 产物标注 |
| **北极星指标可计量** | `runner._register_artifact` | 首个成品落盘时记 `ttfv_seconds`，并与目标比较 —— 指标不可计量就等于没有指标 |
| **草稿诚实性** | `runner.build_draft_info` + `DraftCard` | 差距清单由技能声明（`orchestration.draft_gaps`），前端只负责渲染，不替模型道歉 |
| **续跑语义** | `runner._execute` 的 `done_ids` | 已完成的阶段不重跑，产物直接作下游上下文 |
| **模型回退链** | `providers.OpenAICompatProvider.chat` | 主模型挂 → 按链换模型/换接口；回退不静默，事件流如实说明 |
| **草稿 → 精修** | `POST /runs/{id}/upgrade` | 同一份产物上接着深化，不重开任务（D9 的留存动线入口） |

---

## M0 验收结果

```
✔ 能跑完一条完整流水线（快通道 5 阶段，1.6s）
✔ 每个阶段都有事件推送（60 条事件）
✔ 人工关卡能挂起、能恢复（剧本关卡）
✔ 检查点协议正确（in_progress → superseded → completed）
✔ 决策留痕落库（模型 4 条 + 用户 1 条）
✔ 产物落盘并通过验收标准（8 个文件，含 3 张分镜图）
```

## M0.5 验收结果

```
首次体验（零输入 → 草稿）：
✔ 草稿被如实标注（is_draft）
✔ 首次成品用时已计量并与目标可比（ttfv_seconds / within_target）
✔ 技能声明了草稿差距（6 条，来自 manifest，非前端硬编码）
✔ 列出了未覆盖阶段（角色设计、合成）
✔ 提供继续优化入口（can_upgrade）
✔ SSE 覆盖草稿就绪事件（draft.ready）

草稿 → 精修续跑：
✔ 通道切到精修
✔ 只补跑未完成阶段（characters + compose）
✔ 复用了草稿产物、未重跑（attempt 无增长）
✔ 精修后 7 个阶段全完成，草稿产物未丢失
✔ SSE 覆盖续跑事件（run.resumed）

合计 25 项全绿（scripts/e2e_http.py）
```

---

## 下一步

- **M1** 漫剧精修：接真实生图、TTS、Remotion 合成 → 产出竖屏 mp4
- **M2** PPT 技能：已 vendor ppt-master（MIT）于 `backend/skills/ppt-deck/vendor/`，待写 `manifest.yaml`
- **M3** 论文技能
- **M4** 留存与规模化：技能中心、模板库、成本统计
