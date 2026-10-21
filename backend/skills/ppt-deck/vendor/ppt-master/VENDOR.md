# vendor/ppt-master

Longspan 直接内嵌（vendor）的第三方 PPT 生成引擎，**未做任何源码修改**。

## 1. 来源与许可

| 项 | 值 |
|---|---|
| 上游 | https://github.com/hugohe3/ppt-master |
| 分支 | `main` |
| 获取方式 | `https://ghfast.top/https://github.com/<repo>/archive/refs/heads/main.zip`（本机 GitHub 直连不通，走镜像） |
| 获取时间 | 2026-10-12 |
| 归档体积 | 56,573,188 bytes（zip） |
| **许可** | **MIT** —— 见同目录 `LICENSE`，允许商用、修改、闭源再分发 |
| 决策依据 | MRD §15 **D13**：直接 vendor（省掉整套 SVG → DrawingML 编译器） |

> MIT 要求保留版权声明与许可全文。**`LICENSE` 必须随产物一起分发，不可删除。**

## 2. 目录内容

从上游仓库的 `skills/ppt-master/` 整包复制，**一个文件都没删**：

| 目录 | 作用 |
|---|---|
| `SKILL.md` | 技能主说明（上游原文）。我们的 `../../SKILL.md` 是 Longspan 侧的适配层 |
| `scripts/` | 引擎本体：SVG ⇄ PPTX 双向转换、质量检查、图标/音效同步、模板注册、TTS、视频合成等 |
| `workflows/` | 工作流定义（`generate-pptx.md`、`create-template.md`、`profiles/quick-generate.md` 等） |
| `references/` | **L3 外部技术知识包**：`plan-core.md`、`strategist.md`、`executor-base.md`、`animations.md`、`native-shape-authoring.md` 等 |
| `templates/` | 模板资产：`layouts/`、`decks/`、`brands/`、`charts/`、`styles/`、`tables/`、`schemas/` |
| `templates/icons/` | 三套图标库（`chunk-filled` 641 / `phosphor-duotone` 1518 / `simple-icons` 1241），由 `scripts/icon_sync.py` 物化到项目 |
| `templates/sounds/` | 音效库 + `sound-vocabulary.md`（音效**默认关闭**，需显式选 cue 才物化） |
| `requirements.txt` | 上游依赖清单（含许可证提示） |

**规模**：4,389 个文件 / 43 MB。

## 3. 依赖与许可证注意

`requirements.txt` 里的依赖分两类，**按需安装**：

**核心（M2 做 PPT 技能时必装）**
```
python-pptx>=0.6.21      # 产物是「可编辑的原生 DrawingML 形状」，不是图片
XlsxWriter>=3.0.0
skia-pathops>=0.9.2      # Merge Shapes 物化
uharfbuzz>=0.50.0        # 文字排版与轮廓
PyYAML>=6.0
edge-tts>=7.2.8          # 逐页旁白音频
```

**⚠️ AGPL 隔离项 —— 不要安装**
```
PyMuPDF>=1.23.0          # AGPL-3.0
```
它**只被 `scripts/source_to_md/pdf_to_md.py` 引用**（PDF 转 Markdown）。上游在 `requirements.txt` 第 37-41 行也自己标注了这一点。
**我们的处理**：PDF 摄入改用 `pypdf`（BSD）自实现，或在 Longspan 侧走已有的文档解析通道，**绝不把 PyMuPDF 打进分发产物**。

**可选（用到哪个装哪个）**：`Pillow` / `numpy`（图片处理）、`requests` + `beautifulsoup4` + `curl_cffi`（网页转 MD）、`mammoth` / `markdownify` / `ebooklib` / `nbconvert` / `openpyxl`（文档转 MD）、`google-genai`（Gemini 生图后端）、`flask`（SVG 标注编辑器）。

## 4. 与 Longspan 的集成约定（M2 落地）

1. **不改 vendor 内任何文件。** 需要变更时，优先在 `../../manifest.yaml` 与 `../../prompts/` 里做适配；确实要改引擎则在上层包一层调用脚本。
2. **引擎路径**：`SKILL_DIR = backend/skills/ppt-deck/vendor/ppt-master`，与上游 `workflows/*.md` 里 `${SKILL_DIR}` 的约定一致。
3. **Stage 顺序不写进 Python**：`generate-pptx.md` / `quick-generate.md` 的阶段语义，最终要翻译成 `../../manifest.yaml` 的 `stages[]`（遵守 MRD §9「Harness 不设中央编排器」）。
4. **两条上游纪律必须继承**（已记入 MRD §7.1）：
   - `spec_lock.md` **每页重读** —— 抵抗长任务里的上下文漂移
   - `page_rhythm` **强制节奏变化** —— 治「信息罗列感」的工程手段，不靠提示词劝
   - 逐页手写，**禁止脚本批量生成页面**（上游作者试过并废弃）

## 5. 升级方式

上游更新时，重新下载 archive 覆盖本目录，然后同步更新本文件第 1 节的「获取时间」与 zip 体积。
