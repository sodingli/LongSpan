# Longspan 漫剧 · 演出约束与一致性分层 PRD

> 版本：v1.0
> 日期：2026-10-22
> 状态：待评审
> 上游依据：`MRD-Longspan.md` §0.2「价值不在生成，在约束与留痕」、§0.6「三条判定线」
> 前序文档：`PRD-人物一致性与风格统一整改.md`（锚点/风格锚，已完成）—— 本 PRD 是它的**续篇**，不替代
> 实测来源：run `233a8a68`（末日废土机械师，12 格链式视频，返工 1/12）

---

## 一、背景：一致性问题被归错了层

2026-10-22 复盘结论：**角色一致性不是单一模型缺陷，而是四层杠杆只用了一层半。**

| 层 | 手段 | 成本 | 收益 | 本项目现状 |
| --- | --- | --- | --- | --- |
| L1 提示词 | 风格锚、反写实条款、前后格上下文、锚点原文复用 | 极低 | 低（已衰减） | ✅ 已用满 |
| L2 素材条件 | 参考图（subject_reference）、定妆图、链式首尾帧 | 中 | 高 | ⚠️ 半覆盖：链式首尾帧已上；**参考图通道缺**（万相 t2i 不支持） |
| L3 演出与剪辑 | 镜头预算、场景收敛、同场景连续镜、遮挡转场、单格单动作 | 低 | **最高** | ❌ **完全空白** |
| L4 模型与训练 | LoRA、角色专属模型、专属微调 | 极高 | 高但慢 | ❌ 未使用（正确定位：最后一张牌） |

**核心判断**：当前分镜是「12 个不同景别的自由机位」，这恰恰是生成模型最不擅长的形态 —— 每换一次机位、每换一次景别，就是一次独立重采样，就是一次重新赌脸。传统动画早就用**有限动画（limited animation）**解决过这个问题：少张数、固定机位、重复利用、遮挡转场、循环口型，观众不觉得差，因为叙事节奏接住了。

> **所以：一致性差有一部分是「脚本规范与模型能力错配」，不全是幻觉。这一层能在流程里改，且几乎不花钱。**

这条判断直接兑现 MRD §0.2：**能用规范解决的，不要指望模型自觉。** 一致性不是模型的承诺，是流程的承诺。

---

## 二、目标与指标

| # | 指标 | 口径 | 现状 | 目标 |
| --- | --- | --- | --- | --- |
| M1 | **返工率** | 需重做的格数 ÷ 总格数 | 1/12（上轮 10/12 被审核拦，换画风后达标） | ≤ 1/12，且原因可归类 |
| M2 | **跨格一致性抽检** | 主角出现在 ≥3 个不同景别格时，脸型/发型/主色/标志配饰可辨识为同一人 | 8/12 格稳定，4 格轻微漂移 | ≥ 11/12 格稳定 |
| M3 | **场景数** | 单集出现的独立场景数 | 未约束（实测 6+ 个） | ≤ 3 个 / 12 格 |
| M4 | **镜头预算达成率** | 实际景别变化符合 manifest `shot_budget` 的格数 ÷ 总格数 | 无约束（易连续同景别） | 100%（机器校验） |
| M5 | **换题材成本** | 新开一集需改代码的行数 | 0 行（但缺约束字段） | 0 行，只改 manifest 词条 |

---

## 三、方案总览与顺序理由

```
P0 演出约束（本周，纯规范 + 校验，零外部成本）
   ├─ FR-01 镜头预算      ├─ FR-02 场景卡      ├─ FR-03 同场景连续镜
   ├─ FR-04 单格单动作    └─ FR-05 转场白名单
P1 素材条件补齐（下周，需选通道）
   ├─ FR-06 生图能力声明 + 人物格自动选路
   └─ FR-07 定妆图 / 首帧参考图贯通（含画布重绘）
P2 自动后检（观察期，只告警不阻断）
   └─ FR-08 一致性抽检 + 返工率看板
```

**为什么 P0 排第一**：L3 是四层里唯一「成本低、收益最高、当天可验证」的一层，而且它把一致性问题从**模型问题**转成了**规范问题** —— 后者正是本产品的架构擅长的事（manifest 声明 + 校验器）。反过来，先去做 L4（训练）是用最贵的手段解决最便宜的问题。

---

## 四、功能需求

### FR-01 镜头预算（shot budget）★ P0

**现状**：`storyboard` 阶段的 `review_focus` 只有一句软提示「相邻格之间是否有镜头景别变化」，模型可以不理。景别、机位数量完全自由。

**做法**：manifest 新增 `directing` 块，由 `StageDef` 解析（`harness/skill.py` 加一个字段），同时供给两处：
1. **提示词侧**：`prompts/05_storyboard.md` 里的占位符渲染进去（模型知道预算，不用猜）；
2. **校验侧**：新增 `consistency_checks.kind: shot_budget`，在 `harness/consistency.py` 加校验器，`runner._run_consistency_checks` 加一个 kind 分支 —— 与 `panel_anchor_consistency` 同构，**不动流程骨架**。

**示例声明**：

```yaml
    directing:
      shot_budget:
        max_distinct_scenes: 3          # 12 格最多 3 个场景
        max_scene_cuts: 5               # 场景切换次数上限（A-B-A 计 2 次）
        shot_size_cycle: [medium, close, wide]   # 禁止连续同景别；连续 2 格同景别即告警
        max_camera_moves_per_panel: 1
        one_action_per_panel: true
      transitions:
        allow: [occlusion, back_view, prop_closeup, dissolve]
        forbid: [hard_cut_between_scenes]
      scene_cards: scenes.json
    consistency_checks:
      - kind: panel_anchor_consistency
        plan: panels.json
        anchors: characters.md
        slow_only: true
      - kind: shot_budget            # 预算取自同一份 directing.shot_budget，不重复写数字
        plan: panels.json
      - kind: scene_reference        # 场景引用完整性
        plan: panels.json
        scenes: scenes.json
```

**校验规则（确定性，不做审美判断）**：
- 场景切换次数 > `max_scene_cuts` → 失败，报出第几格到第几格切了场景；
- 连续两格 `shot_size` 相同且非 `wide` → 失败（要求改成不同景别或合并为一格）；
- 单格 `action` 字段出现多个动作（`and then` / `同时` / `，然后`）→ 失败；
- `motion` 不在白名单 → 失败（已有复合 motion 归一化的先例，见 `compose.py`）。

**验收**：一集 12 格，场景数 ≤3、场景切换 ≤5、无连续同景别、无多动作格；且失败原因是**具体格号**，不是"不合格"。

---

### FR-02 场景卡（scene cards）★ P0

**现状**：同一场景在不同格里被模型重新描述一遍，背景元素每次都不一样（废土那集的开场废料堆、车间、门口，三次描述三种样子）。**角色有锚点，场景没有。**

**做法**：`storyboard` 阶段新增第二个产物 `scenes.json`，与 `characters.md` 的锚点机制**完全同构**：

```json
{
  "scenes": [
    {
      "id": "S1",
      "name": "废料场",
      "anchor": "Wide shot of a vast scrap-metal wasteland, rusted vehicle husks stacked in the middle ground, a tilted rusty lamppost on the left, cracked dust ground, gloomy overcast sky, muted grey-green palette",
      "palette": "grey-green",
      "lighting": "overcast, low contrast"
    }
  ]
}
```

- 每格 `panels.json` 增加 `scene_id`；
- **场景锚点由生成工具确定性拼到提示词末尾**（`tools._apply_scene_anchor`），
  **不要求模型在 prompt 里重复背景描述** —— 能机器保证的事不交给模型自觉；
- 校验器 `kind: scene_reference`：只做**引用完整性**（`scene_id` 必须真实存在、
  场景卡必须有 `anchor`），不做文本指纹比对（避免误伤）。

**收益**：即使角色漂，观众也会觉得"这个世界是同一个世界"—— 场景一致性对观感的权重被长期低估。

**验收**：12 格引用的 `scene_id` 全部存在于 `scenes.json`，且每格生成的提示词里能看到同一段场景锚点。

---

### FR-03 同场景连续镜（A-B-A 编排）★ P0

**现状**：分镜按"事件顺序"排，容易变成 A→B→C→D 每次换场，等于让模型每次都重新建立世界。

**做法**：写进 `prompts/05_storyboard.md` 的硬规则 + FR-01 的 `max_scene_cuts` 校验：

- 相邻格若必须换场，**下一格必须回到前一个场景**（A-B-A），禁止 A-B-C-D 线性推进；
- 一集 12 格里，至少 6 格落在同一主场景（"主场景配额"）；
- 换场用 `transitions.allow` 里的手段（遮挡/背影/道具特写），不用硬切。

**理由**：链式首尾帧的上限是"相邻两格像"，不是"第 2 格和第 11 格像"。把相邻格尽量留在同一场景，等于让误差**不跨场景累积**。

**验收**：场景序列无三连换场；主场景占比 ≥ 50%。

---

### FR-04 单格单动作 ★ P0

**现状**：一格 prompt 里常写"他弯腰捡起管子，抬头望向远处"—— 两个动作，模型只能挑一个，或者画出中间态。

**做法**：`panels.json` 每格增加 `action`（单值，一句话）+ `motion`（白名单枚举）；校验器拒绝多动作。`motion` 白名单收敛为 `[static, zoom-in, zoom-out, pan-left, pan-right, tilt-up, tilt-down]`，复合运动（`zoom-in+dissolve`）在 storyboard 阶段就归一化并上报，而不是留到 `compose.py` 兜底。

**验收**：全格 `action` 为单动作；`motion` 100% 命中白名单。

---

### FR-05 转场白名单 ★ P0

**现状**：格间统一 `crossfade 0.5s`（已实现），但**画面内容**没有转场设计。

**做法**：把"用遮挡/背影/道具特写做转场"写进分镜规则，并在 `review_focus` 里列出可检查项。这类镜头还有额外好处：**不露正脸 = 天然规避一致性风险**，且降低视频审核拦截概率（实测半写实人脸最易被判"疑似真人"）。

**验收**：每集至少 2 个"不露正脸"的过渡格（背影/手部/道具特写）。

---

### FR-06 生图能力声明 + 人物格自动选路 ★ P1（需决策 D15）

**现状**：`image.py` 只有一个 `provider` 开关，全局生效。万相画风好人像差参考（**不支持 subject_reference**）；京东云 image-01 支持参考图但画风弱、且负面词不支持。**目前是二选一，而不是按格选路。**

**做法**：
1. 在 provider 实现里声明能力矩阵：`supports_reference` / `supports_negative` / `supports_seed`；
2. `generate_images` 按格判定：**含角色 → 走支持参考图的通道（附带该角色定妆图）；空镜/道具 → 走万相**；
3. 选路结果写进 `media/images.json` 的 `provider` 字段（已有），并在事件流里说明"本格因含角色改用 X 通道"。

**待决策 D15（三选一）**：
- **A**：人物格回切京东云 image-01（现成可用，画风落差明显）；
- **B**：控制台创建 Seedream 预置服务，人物格走 Seedream（画风+参考图兼得，需建服务）；
- **C**：接万相 imageedit 同步端点做"定妆图 + 提示词 → 重绘"（最贴合，但网关目前只有异步探不通）。

**验收**：含角色的格 100% 携带参考图；`images.json` 里能看出每格用了哪个通道、为什么。

---

### FR-07 画布重绘走参考图 ★ P1

**现状**：`CanvasEditor` 已有单格重绘和 AI 建议，但重绘请求**不带定妆图** —— 人工修一次，脸可能又变一次。

**做法**：重绘接口透传 `subject_reference`（定妆图路径由 `characters/` 目录推断），与管线生图共用同一套选路逻辑。

**验收**：画布重绘后的人物格与定妆图相似度不低于管线生成结果（人工抽检 3 例）。

---

### FR-08 一致性抽检 + 返工率看板 ★ P2（只告警，不阻断）

**做法**：
- **抽检**：对人物格裁人脸区域，算直方图 + SSIM 与定妆图比对，低于阈值打 `consistency_warning` 事件（**只告警**，交人判断）。若网关后续有视觉模型，再升级为语义判定。
- **返工率**：`steps.attempt` 已有；格级重绘在 `media/images.json` 记 `redraw_count`，`GET /runs/{id}/metrics` 汇总 M1–M5。

**为什么只告警**：指标一旦能阻断流程，就会诱导"改判定标准"而不是"改质量"。MRD §0.6 的三条判定线是**决策依据**，不是门禁。

---

## 五、manifest 变更影响面

| 文件 | 变更 | 风险 |
| --- | --- | --- |
| `backend/skills/comic-drama/manifest.yaml` | 新增 `directing` 块、`scenes.json` 产物、`shot_budget` / `scene_anchor_consistency` 校验声明 | 低（纯声明） |
| `backend/app/harness/skill.py` | `StageDef` 加 `directing: dict` 字段解析 | 低 |
| `backend/app/harness/consistency.py` | 新增两个校验器 | 低（与既有同构） |
| `backend/app/harness/runner.py` | `_run_consistency_checks` 加两个 kind 分支 | 低（每加一个 kind 一行） |
| `backend/skills/comic-drama/prompts/05_storyboard.md` | 注入镜头预算 + 场景卡规则 | **中**：提示词变长会挤压输出预算，需要实测 |
| `backend/app/harness/tools.py` | 生图时拼接场景锚点、按格选路 | 中 |
| `backend/app/media/image.py` | provider 能力矩阵 | 中 |

**架构约束不变**：Python 里依然没有任何"漫剧"专属流程逻辑 —— 新规则全部以**数据**（manifest + prompt 模板）形式存在，校验器只做确定性判断。

---

## 六、排期建议

| 阶段 | 内容 | 验证方式 |
| --- | --- | --- |
| 第 1 步（当天） | FR-01 + FR-04：`directing` 解析 + 两个校验器 + 提示词注入 | 用现有 `panels.json` 反跑校验器，看能否复现"6 场景 / 连续同景别"的问题 |
| 第 2 步（1 天） | FR-02 + FR-03：场景卡 + A-B-A 规则 | 重跑一集，对比场景数与切换次数 |
| 第 3 步（2 天） | 端到端跑一集，测 M1–M4 | 返工率是否 ≤1/12、一致性抽检是否 ≥11/12 |
| 第 4 步（决策后） | FR-06/FR-07（L2 补素材层） | 含角色格参考图覆盖率 100% |
| 观察期 | FR-08 指标看板 | 连续两集不达标 → 停止加功能，回规范层 |

---

## 七、风险与对策

| 风险 | 表现 | 对策 |
| --- | --- | --- |
| 提示词膨胀 | 05_storyboard 规则太多，模型只执行前几条 | 规则不超过 8 条，且全部**可机器校验**；不能校验的规则不进提示词 |
| 校验误伤 | 场景锚点指纹匹配失败 → 空耗重做 | 与锚点同款策略：前 80 字符指纹 + 空镜豁免 + 失败原因带格号；连续 2 轮失败降级为告警 |
| 预算过紧伤叙事 | 3 个场景讲不完 12 格 | `max_distinct_scenes` 允许按题材在 manifest 里调；先测 3，不够就 4 |
| 表演呆板 | 全片"克制镜头"变成 PPT | 用 L1 的镜头运动（zoom/pan）补动感，不靠换机位制造节奏 |
| 与平台正面竞争 | 平台内置一致性 + 一键成片 | 按 MRD §0.5：不比生成质量，比"同规范下可复现"，靠留痕与可换供应商取胜 |

---

## 八、明确不做（本期）

- **不做 LoRA / 角色微调**：L3、L2 未用尽前不碰，成本高且与"可换模型"的定位冲突；
- **不做自动选片/自动剪辑节奏**：属 L3 之后的"叙事层"，先保证一致；
- **不把抽检做成阻断门禁**：见 FR-08 的理由。

---

## 九、验收汇总（交付前自查）

- [ ] `directing` 声明能被 `StageDef` 解析，并在提示词里生效（可在产物里看到预算被遵守）
- [ ] 新增两个校验器各有单测：正常通过 / 越界失败 / 格式残缺不崩
- [ ] 跑一集完整任务：M1 ≤1/12、M3 ≤3、M4 = 100%
- [ ] `media/images.json` 能看出每格通道选择与理由（FR-06 落地后）
- [ ] 换题材只改 manifest，git diff 里无 `backend/app/**` 改动（MRD §0.6 判定线 2）

---

## 十、实施状态（滚动更新）

### 10.1 FR 落地情况

| FR | 状态 | 落点 | 备注 |
| --- | --- | --- | --- |
| FR-01 镜头预算 | ✅ 已落地 | `skill.StageDef.directing` / `consistency.validate_shot_budget` / `runner._check_shot_budget` | 预算唯一来源是 manifest，check 只声明"强制它" |
| FR-02 场景卡 | ✅ 已落地 | `scenes.json` / `consistency.validate_scene_references` / `tools._apply_scene_anchor` | 场景锚点由工具注入，不靠模型复用 |
| FR-03 A-B-A | ✅ 已落地 | `shot_budget.aba_rule` + 提示词骨架表 | 三连换场直接判失败 |
| FR-04 单格单动作 | ✅ 已落地 | `shot_budget.one_action_per_panel` + `action`/`motion` 硬字段 | 命中"然后/接着/同时"即判失败 |
| FR-05 转场白名单 | ⚠️ 部分 | 写进提示词与 `review_focus` | 尚无机器校验（露脸率需多模态判断，归 FR-08） |
| FR-06 生图选路 | ✅ 已落地 | `media/image.py` 的 `ImageChannel` 能力矩阵 + `image_routing=auto` | D15 已决，见 §10.5 |
| FR-07 画布重绘参考图 | ✅ 已落地 | `api/canvas.regenerate_panel` → `_find_reference_art` → `subject_reference` | 与管线同一套三级查找 + 风格锚 + 空镜守卫；实测 `used_subject_reference=true` |
| FR-08 抽检 + 看板 | ⚠️ 部分 | `scripts/audit_consistency.py`（角色比对，只告警）+ `scripts/audit_vision.py`（构图体检） | 看板未做；实际误报率待多集统计，见 §10.6 |

### 10.2 伴随落地（不在原 PRD 里，但必须做）

1. **硬约束违约 = 阶段失败**：`orchestration.on_unmet_hard`（默认 `fail`）。
   原实现里一致性校验失败只写进 `step.detail`，阶段照样 `completed` —— 分镜带着 3 条结构违约进入下游，
   视频首尾帧、字幕对位、时间轴全部建立在错误结构上。**约束一旦能"带病通过"，就退回成建议。**
2. **零成本自检脚本**：`backend/scripts/audit_storyboard.py <run_id>`，生图之前就能答"这集结构有没有问题"。
3. **校验器自测**：`backend/scripts/selftest_directing.py`，13 条用例（正样本 + 逐规则反样本），
   防止"永远说通过"和"永远说不通过"两种坏校验器。
4. **提示词留痕**：`media/images.json` 每格记录 `prompt_used`（含自动补齐的风格锚与场景锚），
   回答"这一格为什么长这样"。

### 10.3 三集实测（2026-10-22）

| 任务 | 现象 | 结论 |
| --- | --- | --- |
| `c2770db2` | 分镜重做 4 次，仍有 3 条违约，阶段却 `completed` | 发现"带病完成"漏洞 → 已修 |
| `ac4f10a3` | 分镜仍违约，**阶段硬失败**，整轮停在分镜 | 硬闸生效；同时暴露主场景统计 bug（`distinct.count` 恒为 1）→ 已修 |
| `fe78692a` | 提示词改为**给定 12 格骨架**后，分镜**一次通过**（attempt 1），场景序列 `S1 S1 S1 S2 S2 S1 S1 S1 S3 S3 S1 S1`、景别无相邻相同 | 证明"先定结构、模型填内容"比"事后校验"有效得多 |

**方法论结论（重要）**：约束要**前置成骨架**，而不是**后置成罚单**。
同一份规则，只做校验 → 模型打地鼠（修 A 坏 B），4 次重做仍不过；
给出确定性骨架 → 一次通过。这条经验适用于后续所有技能。

### 10.4 暴露的新问题（进 P1 清单）

- **生图审核误杀**：`fe78692a` 第 1 格被万相判 `Output data may contain inappropriate content` → 降级为占位图（已如实留痕）。
  空镜 + "车内空无一人 + 司机"这类描述也会触发，说明审核偏保守。P1 需要"审核失败 → 软化重试 → 换通道"三级处理，而不是直接占位。

### 10.6 W4 一致性抽检与空镜守卫（2026-10-22 晚）

**抽检主判据换成「双图角色比对」（原计划的人脸框 + SSIM 降级为辅助）**

原计划：视觉模型给人脸框 → 裁出来算直方图 + SSIM。实测直接失败：

| 现象 | 数据 |
| --- | --- |
| 框不可靠 | 同一批图里模型**有时返回归一化坐标、有时返回像素坐标**（`[231,256,468,419]`），按归一化处理直接裁到画面外 |
| 裁切含大量背景 | 即使坐标正确，框也常偏到背景（实测裁出"船和天空"），SSIM 崩到 **0.00** |
| 误报率 | 10 格里 4 格"裁切失败"、其余 0.20~0.50 全在噪声区，**远超路线图允许的 30%** |

改为让视觉模型直接看**两张图**回答"是不是同一个角色"（1~5 分 + 依据）：

| 样本 | 判定 |
| --- | --- |
| 正样本（定妆图 vs 本集 9 格） | 全部 5 分，理由具体（"脸型、深陷眼窝、灰白短发、蓝色盘扣上衣一致"） |
| 负样本（老渔夫 vs 另一集分镜） | 1 分（"年轻黑发男性、灰色工装"）/ 空镜判 `found=false` |
| 负样本（老渔夫 vs 另一角色定妆图） | 1 分（"黑红发黄瞳年轻女性"） |

**抽检第一次上线就抓到一个真缺陷，而这是文本/构图校验都抓不到的**：

> `2942b7ea` 第 10 格 action 是"小船驶向海面薄雾深处"（纯空镜），prompt 里也确实没写人物，
> 但成图里画进一个**蓝发绿衣少女**——与本片角色毫无关系的陌生人，出现在成片结尾。
> 构图检查（`audit_vision`）判它"构图完整"，结构校验判它"景别合法"。

**根因与修法（进代码，不只是进规范）**：空镜不能靠"没写人物"隐式表达，必须显式
`no people` 声明；而"显式"这件事不能指望模型自觉（MRD §7.4 原则 4）。

1. `consistency.is_empty_shot()`：显式声明命中 → 空镜；否则出现任何人物词（中英文，
   含代词/身体部位）→ 非空镜；都没有 → 空镜。
2. `tools._apply_empty_shot_guard()`：判定空镜且缺声明 → **确定性注入**
   `Empty scene — absolutely no people, no characters…`；幂等。
3. **顺序有讲究**：守卫必须在风格锚**之前**。风格锚会给提示词补
   `anime face with large expressive eyes`，一句话就把空镜判定打回"有人物"，守卫从此失效
   （第一版就是这么失效的，prompt_used 里查不到注入句）。
4. `ensure_style_anchor` 对空镜改用"无脸条款"（与局部特写同款）。
5. 空镜同时跳过人物参考图 —— 参考图的语义是"画面主体长这样"，空镜带它必然补出人。

实测：重画格 10 → `prompt_used` 同时含 no-people 声明、不含 anime face → 成图为纯海景空镜，
复检 `found=false`（"纯海景空镜，无任何人物"）。

**落地状态**：`scripts/audit_consistency.py <run_id|--all>`（只告警，报告落盘
`consistency_audit.json`，判定缓存 `vision_compare.json` 不重复计费）；
`--geometric` 保留几何度量作辅助参考，**不参与告警**。

### 10.5 生图通道机制落地（2026-10-22 下午，D15 决策 + 审核三级处理）

**D15 决策记录（三条路探完后合并成一条更好的）**：

| 选项 | 实测结论 |
| --- | --- |
| 回切 image-01 | 不需要了 —— 仅作备用通道保留在链里 |
| 建 Seedream 服务 | 放弃 —— 需要单独的京东云控制台服务/app_key，且探完后发现有更好的 |
| 万相接 imageedit | 不需要了 —— 同网关直接有更强能力 |

**真正的解**：万相中转网关的 `/api/v1/models` 实际有 357 个模型（此前只盲试了几个名字），
其中 **`qwen-image-3.0-pro` 走 DashScope `multimodal-generation` 同步端点**，实测：

- 支持参考图（messages 里 `{image: data-URL}` 多图输入，官方上限 3 张）——
  用定妆图实测：中年司机（圆寸/八字纹/蓝工装/05 胸牌）跨场景到便利店货架前**全要素保持**；
- 支持 `negative_prompt` + `seed`（parameters 原生）；
- 720*1280 尺寸、单张 30-90 秒同步返回；
- 之前被 wan2.2 审核误杀的「空车内无人」空镜**直接过审**。

**结论：主通道切 `dashscope-mm` + `qwen-image-3.0-pro`** —— 同通道同时解决
"画质高一档"与"人物格参考图"，风格天然统一，D15 三选项全部作废。

**机制落地（image.py 重构）**：

1. `ImageChannel` + 能力矩阵（`_CAPS`）：每条通道如实声明 supports_reference / negative / seed；
2. 通道链：主通道（`LONGSPAN_IMAGE_*`）+ `LONGSPAN_IMAGE_FALLBACKS`（JSON 数组），
   当前配置 wan22-async → jd-image01；
3. `image_routing=auto`：带参考图的格优先走支持参考图的通道（稳定排序不打乱主备）；
4. **审核三级处理**：审核拦截 → 同通道软化重试（L1 敏感词替换+安全声明 / L2 再压情绪词）→
   换下一通道 → 全败才占位，且 `fallback_reason` **逐通道列出失败原因**（铁律 #2）；
   `channel_trace` 记录每一步决策（"main：审核拦截，软化 Lv1 重试"）；
5. `prompt_used` 记录**软化后**实际送进模型的提示词（tools.py 改为 setdefault 不覆盖）；
6. 自测 `scripts/selftest_image_channels.py`：33 条用例全过
   （真实审核案例识别、软化逐级生效不叠加、fallbacks 三种形态宽容解析、auto 路由稳定排序、
   mock 三级编排与全败留痕）。

**附带发现（进 P1）**：该网关还有 vidu/kling 的 reference2image 与 start-end2video、
wan3.0-video-prime 等视频模型 —— W3 评估视频通道升级时优先实测 vidu start-end2video
（原生首尾帧协议，可能比现在的"图生视频+首尾帧"更贴合链式方案）。
