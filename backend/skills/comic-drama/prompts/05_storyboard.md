# C5 · 分镜绘制

## 目标

逐格产出画面描述 + 分镜图。**先跑样片格，再铺满。**

## 执行顺序（重要）

1. 先做 `spec.json` 里 `sample_panels` 指定的前 N 格（默认 3 格）
2. 这 3 格要覆盖**不同景别**（至少一个全景、一个特写），这样才能检验画风在远近景下都成立
3. 自检通过后再铺剩余格

理由：画风与一致性的风险在铺量时会被放大 N 倍。先用 3 格验证，成本几乎为零。

## 每格必须输出的内容

- **场景号**（`S1`/`S2`/`S3`，必须来自 `scenes.json`）
- **景别**（全景/中景/近景/特写/大特写）
- **画面**：具体到**一个**动作、视线落点、前景遮挡、光线方向
- **锚点复用**：写明本格用了哪个角色的锚点 + 追加了什么动作描述
- **镜头运动**：推/拉/摇/移/跟/环绕/静止，以及幅度
- **音效**（若该格有声音设计）

## 六条硬纪律

1. **锚点原文复用，一个字不改。**
   先用 `read_artifact` 读 `characters.md`，把每个角色的「一致性锚点（冻结字符串）」
   代码块里的**整串原文**复制出来。凡是该角色出现的格，`prompt` 必须以这条锚点原文开头，
   再追加本格动作与场景。** paraphrase 式转述（如 "Short hair, thin-rimmed glasses, loose
   beige trench coat" 这种缩写摘要）= 不合格**，锚点里的细节（耳机、随身物、鞋、年龄感、
   表情）一个都不能丢 —— 丢细节就是人物每格长得不一样的直接原因。
2. **画风锚必须出现在每一格的开头，且必须带反写实条款。**
   从 brief 的 `style` 字段生成一条统一的英文画风描述（如 国漫赛璐璐 →
   "2D anime cel-shaded illustration, Chinese donghua style, clean ink lines, flat cel colors, anime screenshot frame"），
   并**必须追加**否定式条款："NOT a photograph, NOT photorealistic, NOT live-action, NOT 3D render"。
   原因：角色锚点里的年龄/服装/肤质描述词会把生图模型拉向照片写实，只有正向风格词压不住，
   必须显式否定。每格 `prompt` 都以 **画风锚（含反写实条款） + 角色锚点** 开头。
3. **景别必须变化；运动必须标注。**
   相邻两格不要用同一景别（"中景→中景→中景"要重排）。
   每一格都要说明镜头怎么动（"静止"也是一种运动决策，但必须是刻意的）。

4. **场景必须收敛，换场必须回到上一个场景（A-B-A）。**
   场景总数、切换次数、主场景占比都有预算（见上方「本阶段硬性约束」的 `shot_budget`），
   超出会被机器校验打回。为什么：换一次场 = 模型重新建一次世界 = 一致性重新赌一次。
   禁止 A→B→C→D 线性推进；需要离开主场景时，处理完就**回到主场景**再往下走。
   换场优先用**遮挡/背影/道具特写**过渡（这类镜头不露正脸，天然规避换脸风险）。

5. **一格只演一个动作。**
   `action` 字段只写一个动作（"他弯腰捡起管子"），不要写"捡起管子，然后抬头望向远处"——
   模型只能挑一个演，或画出一个尴尬的中间态。
   **出现"然后 / 接着 / 同时 / 并且 / 随后 / ；"就算两个动作，机器校验会直接打回。**
   注意：格数由骨架固定，所以**不要为了拆动作去改格数**——正确做法是二选一：
   删掉第二个动作（只留主动作），或把两个动作压成一个连贯动作。

6. **取景必须自洽：画框里有什么、没有什么，你要说清楚。**
   近景/大特写拍**手、脚、物件**时，如果把躯干或衣着也写进了画面，就**必须**声明画框边界，
   否则模型只能把头顶裁掉 —— 成片里会出现一个**没有脑袋的半身人**（实测事故：
   "extreme close-up of a weathered hand touching the inner pocket of his shirt" →
   胸口占满画框、衣领上方是空的）。
   - 只拍局部时，`prompt` 里加上这句（原文照抄）：
     `only the hand and the fabric fill the frame, no face, no head, no torso in frame`
   - 或者**升一档景别**（close → medium / medium close-up），让人物完整入画。
   - 判定口径：同一格里既有 `hand` 等局部词、又有 `shirt/pocket/chest/torso` 等衣着词、
     却没有 `face/head` 词或边界声明 → 机器校验直接打回（`framing_consistency`）。

## 关于分镜图（M1）

分镜图由 Harness 在你交出 `panels.json` 之后**自动生成**（生图服务不可用时降级为程序化分镜卡，并在画面上标注"占位图"）。
所以你**不需要**调用工具画图，但必须把"画什么"写清楚 —— 这是你这一阶段最核心的价值。

### 分镜骨架由系统给定，你只填内容（结构不要自己发明）

**骨架表在本次提示词末尾由系统给出**（标题「本阶段结构骨架」）——它按 `spec.json` 的
实际格数生成，格数可能不是 12。**以系统给的那张表为准**，不要用别处的示例表。

规则（系统表已经排好了，你照着写就行）：
- 格号、`scene_id`、`shot_size` 三列**原样照抄**。这三列在你交卷后会被系统统一回填，
  你写的偏离值会被改回来并留痕 —— 所以别在这三列上创作。
- 主场景占约 70%，另外两个场景各占 2 格并**夹在主场景之间**（A-B-A、A-C-A，禁止 A→B→C）。
- 相邻格景别已经错开，**不要**为了"电影感"自行升降景别：换一次景别就是重新赌一次脸。

**你的工作**：给每一格填 `action`（一个动作）、`prompt`（角色锚点 + 英文画面）、`subtitle`、
`speaker`、`motion`、`duration_seconds`。内容是你的，结构不是。

### 必须额外产出的机器可读清单：`panels.json`

这是渲染器的输入，字段必须齐全、可直接消费（**不要**写注释、不要尾随逗号）：

```json
{
  "panels": [
    {
      "index": 1,
      "scene_id": "S1",
      "shot_size": "wide",
      "scene": "全景 · 跟拍推进",
      "action": "机械师蹲下身，把手伸进半埋的金属箱",
      "prompt": "CHENMO_ANCHOR: <锚点原文照抄> <本格动作与场景的英文描述>",
      "motion": "zoom-in",
      "duration_seconds": 5,
      "subtitle": "本格字幕（没有台词就留空字符串）",
      "speaker": "说话人（没有任何人说话就留空字符串）",
      "ratio": "9:16"
    }
  ]
}
```

字段规则（不合规会直接判失败重做）：

- `index`：格号，从 1 连续递增，且必须与 `storyboard.md` 里的格号一一对应。
- `scene_id`：**必填**，必须是 `scenes.json` 里存在的场景号（`S1`/`S2`…）。
  生图时工具会把该场景的 `anchor` 原文自动拼进提示词 —— 你不需要在 `prompt` 里重复背景描述，
  但**引用必须写对**，写错会被校验打回。
- `shot_size`：**必填**，只能取 `wide` / `medium` / `close` / `extreme-close`
  （对应 全景 / 中景 / 近景+特写 / 大特写）。相邻格不得相同。
- `action`：**必填**，一句话、**只含一个动作**（中文即可）。
- `prompt`：**生图提示词**。格式必须是 `角色锚点标签: 锚点原文 + 动作场景`：
  1. 先写 `角色锚点标签:`（如 `CHENMO_ANCHOR:`），冒号后**紧接 characters.md 里该角色的「一致性锚点」整串原文，逐字照抄、一个字都不能改**；
  2. 锚点原文之后，再追加本格的动作/景别/光线（英文）；
  3. 一格出现多个角色时，依次写多个 `标签: 原文`（如 `CHENMO_ANCHOR: ... L7_ANCHOR: ...`）。
  4. 纯场景/空镜格（无任何角色），写画面描述，但**不要**写角色锚点标签。
  - **锚点标签的作用**：机器用它来定位该格用到的角色，并取对应的定妆图做人脸参考。
  - **禁止转述**：锚点原文必须逐字照抄，`Short hair, thin-rimmed glasses` 这种缩写摘要 = 不合格，会被机器校验打回（丢细节就是换脸的直接原因）。
- `motion`：**必填**，只能取 `zoom-in` / `zoom-out` / `pan-left` / `pan-right` / `pan-up` / `pan-down` / `static`。
- `duration_seconds`：本格时长（秒），加起来要接近总时长。
- `subtitle` / `speaker`：字幕正文与说话人。**没有台词的格子留空字符串**，不要填"无"。
- 每一格都要有一条，**不能在快通道下偷懒只写前 3 格**（快通道只生成前 3 格图，但清单要写全）。

### 还必须产出场景卡：`scenes.json`

一集只有 2–3 个场景，每个场景只需要**一段冻结的背景描述**。这是"世界不漂移"的锚点，
与角色锚点同构：写一次、逐格复用、一个字不改。

```json
{
  "scenes": [
    {
      "id": "S1",
      "name": "废料场",
      "anchor": "Wide shot of a vast scrap-metal wasteland, rusted vehicle husks stacked in the middle ground, a tilted rusty lamppost at the left edge, cracked dust ground, gloomy overcast sky, muted grey-green palette",
      "palette": "grey-green",
      "lighting": "overcast, low contrast"
    },
    {
      "id": "S2",
      "name": "维修间",
      "anchor": "Interior of a cluttered repair shed, single hanging bulb, tool wall on the right, oil-stained workbench in the center, dust in the air, warm amber light",
      "palette": "amber-brown",
      "lighting": "single warm bulb, deep shadows"
    }
  ]
}
```

规则：

- `anchor` 是**英文冻结字符串**，写清空间结构（前景/中景/背景各有什么）、光线方向、色调；
- 同一场景的所有格共用同一段 `anchor`，**不要为不同格改写它**；
- 场景数超过 `shot_budget.max_distinct_scenes` → 判失败（场景就是在花一致性预算）。

## 自检

- **字符串级核对**：逐格检查 `prompt` 里 `标签:` 后面的内容是否就是 characters.md 里锚点代码块的整串原文
  （逐字比对，不是凭印象）。任何一格是转述/缩写 → 重写那一格。
- 有没有哪一格你重新描述了角色，而没用锚点标签 + 原文？→ 重做那一格。
- 把 12 格的景别列出来，是不是有节奏变化？**相邻两格有没有撞景别？**
- 把 12 格的 `scene_id` 列成一串（如 S1 S1 S2 S1 S1 S3 S1…），数三件事：
  去重后几个场景？切换了几次？有没有出现 A→B→C 三连换场？
- 逐格读 `action`：是不是每一格都只有一个动作？出现"然后/接着/同时"就拆格。
- 逐格看**取景**：有没有哪一格"写了手/物件 + 衣着躯干，却没写头在哪"？→ 补边界声明或升景别。
- 格与格之间的视线、动作方向是否连贯（别让角色上一格朝左、下一格毫无过渡地朝右）？
