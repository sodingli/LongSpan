# C2 · 生产规格

## 目标

把创意反推成**可执行的生产规格**——让后面的每个阶段都有明确数字可依。

规格不是"设定说明"，它必须能回答：做几格？每格几秒？大概花多少钱？

## 用户输入是硬契约（第一位）

`read_brief` 里的 `duration`（单集时长）和 `episodes`（集数）是**用户亲手填的数字**，
它们是这一集所有下游阶段的数字来源。规则只有一条：

> **用户填了，就照着做；用户没填，就用默认值；无论哪种，都不能由你改判。**

- 用户填 `duration: 38` → 规格就是 38 秒，不是"38 秒不太好，我改成 60 秒"；
- 用户填 `episodes: 3` → 规格就是 3 集，不是"3 集太长，先做 1 集"；
- 用户没填集数（或填 1）→ 就按 **1 集**做，不得自行扩集、也不得写"建议做成系列"来偷换；
- 用户填了明显不合理的值（例如负数、0）→ **如实写进 `decisions` 说明"该值不可执行"，
  并在 `constraints.json` 里标注需要用户确认**，但**不要**悄悄替换成你喜欢的数字。

为什么这条要写死：实测有一轮，用户填的 `episodes=-18` 被模型判成"无效输入"、
自行改成单集，规格与用户输入从此对不上，后面每一格都建在错的数字上。
**数字契约不是建议，是契约。**

## 关键计算

```
格数 = 用户时长 ÷ 每格时长
```

每格 4~6 秒是竖屏动态漫的舒适区间：
- 低于 4 秒 → 观众来不及看清画面
- 高于 6 秒 → 节奏塌，注意力流失

默认取 5 秒。**把这个算式写进输出**，让用户看到格数是算出来的，不是猜的。

除不尽时按四舍五入取整，并让 `格数 × 每格时长` 尽量贴近用户时长
（例如 38 秒 ÷ 5 秒 = 7.6 → 8 格，每格 4.75 秒）。**总时长以用户输入为准，
每格时长是推导出来的，不是反过来拿每格时长去改总时长。**

## 风格锚（spec.json 的 `style_anchor`）

`style_anchor` 是**这一集画风的冻结字符串**，会被原文粘进每一格生图提示词 ——
生图阶段不再自己决定画风，只认这一句。

写它的要求：
1. **英文**，一段话，可直接当提示词前缀；
2. 说清画种与质感（线条、上色方式、色彩倾向），对应 `brief.style`；
3. **必须自带反写实条款**（`NOT a photograph, NOT photorealistic, NOT live-action`）——
   半写实人脸过不了视频内容审核，这是审核底线，不是审美偏好；
4. 不写具体角色、不写具体场景（那是 C4/C5 的事）。

示例（国漫赛璐璐）：

```
Flat 2D Chinese comic cel-shading style, thick clean outlines, uniform flat colors,
simplified facial shading, large expressive eyes, NOT a photograph, NOT photorealistic,
NOT live-action, NOT 3D render, no realistic skin texture, no painterly brushwork
```

## 节奏表

不要 12 格都一个节奏。按叙事功能分段，每段给不同的平均时长：

| 段 | 功能 | 特点 |
|---|---|---|
| 开场 | 抛钩子 | 最快，前 5 秒定生死 |
| 升级 | 建立冲突 | 稍慢，让台词有重量 |
| 爆发 | 反转 | 由慢转快 |
| 收尾 | 留悬念 | 定格 |

## 最小可信路径

标记 `sample_panels`（前 N 格先做样片）。
默认 3 格：成本几乎为零，但能避免铺完 12 格才发现画风不对。

## 输出

写入 `spec.json`，字段：

- 数字契约：`total_duration_seconds` / `grid_count` / `seconds_per_panel` /
  `tolerance` / `episodes` / `user_duration_seconds` / `user_episodes`
- 画风契约：`style`（中文画风名，取自 brief）/ `style_anchor`（上面的冻结英文串）
- 制作参数：`resolution` / `aspect_ratio` / `fps` / `sample_panels`
- 说明：`rhythm_table` / `visual_pipeline` / `budget_estimate` / `decisions`

`user_duration_seconds` / `user_episodes` 是**用户原样输入**（没填就写默认值），
与 `total_duration_seconds` / `episodes` 分开记 —— 这样"规格有没有偏离用户输入"
一眼可查，也方便机器对账。

## 自检

- 格数是不是算出来的？如果不是，重做。
- `total_duration_seconds` 是不是**等于用户输入的时长**（在容差内）？不是就重做。
- `episodes` 是不是**等于用户输入的集数**（没填 = 1）？不是就重做。
- `style_anchor` 有没有反写实条款？没有就补 —— 缺了它，生图会往写实漂。
- `budget_estimate` 是否给了**区间**和**假设**？给一个精确到分的假数字是不诚实的。
- 有没有偷偷把"节奏"设计成每格一样长？
