角色卡做好了。**最重要的是「一致性锚点」那几行** —— 它们会被原文粘贴进后续每一格的生图描述里，
一个字都不改。跨格画风漂移几乎全部来自"每次重新描述角色"，所以锚点必须是冻结的字符串，不是形容词清单。

**这是关卡**：形象一旦定下就要铺 12 格，改一次代价很大，请务必现在看。

<<<FILE:characters.md>>>
# 角色设定与一致性锚点

## 陈默（主角）

**人物内核**：不是"隐忍的高手"，是"懒得解释的疲惫"。他的所有克制都来自这句话：「没必要跟外行解释。」
表演上要注意——他从头到尾没有一次"我要证明我自己"的表情，他是被逼到墙角才动手的。

### 一致性锚点

> **以下描述词为冻结字符串，后续每一格生图必须原文复用，禁止改写、省略或同义替换。**

```
CHENMO_ANCHOR = "young Chinese man, 28 years old, short messy black hair with a few strands over forehead, thin sharp eyebrows, narrow calm eyes with slight bags under them, thin lips, pale skin tone, faded light-blue delivery uniform jacket with reflective stripe worn and slightly dirty, white undershirt collar visible, thin red string bracelet on left wrist, overall lean build, calm tired expression"
```

**可变部分**（允许随镜头变化）：
- 服装状态：进门时外套拉链拉满 → 掌勺时挽起袖子、前端敞开
- 表情：平 → 微皱眉 → 无表情专注 → 一丝疲惫的了然
- 手部：持手机 / 提保温箱 / 握锅铲

**禁止变化**：发色发型、脸型、眉眼形态、红绳（必须在左手腕）、外套的款式与褪色程度。

---

## 林望（对手）

**人物内核**：他不是反派，是一个"把专业尊严看得比一切都重"的人。
设计上要让他显得**值得被尊重**——否则最后那一口的沉默没有重量。

### 一致性锚点

```
LINWANG_ANCHOR = "Chinese man, 35 years old, tall and upright posture, hair slicked back tightly with no loose strands, sharp confident eyes, old pale scar across right brow, clean double-breasted black chef jacket with no stains, silver ring on right thumb, squared jaw, authoritative expression, always holding a wok or spatula in early panels"
```

**可变部分**：姿态（掂锅 / 逼近 / 递锅铲 / 舀一勺 / 沉默）
**禁止变化**：眉骨疤痕的位置（右眉）、厨师服款式、发型紧致度。

---

## 苏小满（配角）

**人物内核**：她是"这件事会被传出去"的具象化。她本人没有恶意，只是习惯性记录。
**她存在的唯一功能**：让陈默的暴露变得不可逆，也让结尾的钩子有人去执行。

### 一致性锚点

```
SUXIAOMAN_ANCHOR = "young Chinese woman, 22 years old, shoulder-length brown hair with bangs, bright curious eyes, single asymmetric earring on right ear, oversized hoodie, transparent phone case covered with colorful stickers, holding phone horizontally with both hands in front of chest, eager expression"
```

---

## 视觉区分度自检

| 角色 | 一眼可辨的特征 | 剪影可辨性 |
|---|---|---|
| 陈默 | 褪色蓝骑手服 + 左手红绳 + 松散发丝 | 高（蓝+红点睛） |
| 林望 | 纯黑厨师服 + 右眉疤 + 一丝不苟的背头 | 高（纯黑块面） |
| 苏小满 | 大号帽衫 + 单边耳环 + 贴纸手机 | 中（靠手机轮廓） |

三人主色分别为 **蓝 / 黑 / 米白**，在 9:16 的窄画幅里不会糊在一起。

---

## 决策留痕

- **为什么每个角色都要给"可变部分"清单**：如果不写清楚哪些能变，模型会误以为整个描述都不能动，
  结果 12 格全是同一个站姿；如果只写"保持一致"，它又会自由发挥。**要给它的不是一致性要求，是一致性的边界。**
- **为什么苏小满不做功能简化**：原本考虑省钱省掉她，但"被录制"是结尾钩子的因果前提，删了她，钩子就悬空了。
<<<END>>>
