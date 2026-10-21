"""任务目录表：把一次任务的工作区整理成一张可读、可筛选、可导出的清单。

三条设计取舍（对应项目铁律）：

1. **文件系统是事实来源，DB 只作补充。**
   文件在不在、多大、什么时候改的，一律实测 —— `artifacts` 表里的 size/mtime 会过期
   （成片重渲染后 DB 还记着旧体积，人工重画的图根本没登记）。
   DB 只用来回答一个问题：**这个文件是哪个阶段产出的**。

2. **来源标注不猜。**
   "谁人改的、谁人上传的"只从**真实留痕**里读：`canvas_edits.json` 记了谁被编辑过，
   `media/images.json` 记了每一格**当前生效**的是哪张图（人工上传的会自报 `source: upload`）。
   两者都没有的，才认为是模型生成。宁可少标，不编。

3. **阶段名、阶段顺序都读 manifest，不硬编码。**
   换一个技能包（比如 `ppt-deck`），目录表自动按那套阶段分组 —— 代码里没有一处
   写着 "storyboard" / "compose" 这样的阶段 id。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("longspan.file_index")

# 扩展名 → 面向使用者的类型标签
KIND_BY_EXT: dict[str, str] = {
    "md": "markdown", "markdown": "markdown", "txt": "text",
    "json": "json", "yaml": "yaml", "yml": "yaml", "csv": "table",
    "png": "image", "jpg": "image", "jpeg": "image", "webp": "image", "svg": "image", "gif": "image",
    "mp4": "video", "webm": "video", "mov": "video",
    "mp3": "audio", "wav": "audio", "m4a": "audio", "aac": "audio", "flac": "audio",
    "pptx": "pptx", "docx": "docx", "xlsx": "xlsx", "pdf": "pdf",
}

KIND_LABEL: dict[str, str] = {
    "markdown": "文档", "text": "文本", "json": "数据", "yaml": "配置", "table": "表格",
    "image": "图片", "video": "视频", "audio": "音频",
    "pptx": "演示文稿", "docx": "文档", "xlsx": "表格", "pdf": "PDF",
    "other": "文件",
}

# 来源标记：人一眼能看出"这版是谁写的"
SOURCE_LABEL: dict[str, str] = {
    "model": "模型生成",
    "human_edit": "人工修改",
    "human_upload": "人工上传",
    "human_redo": "人工重画",
    "stale": "历史残留",
    "backup": "模型原件",
    "system": "系统留痕",
    "aux": "中间产物",
}

# 渲染参数文件：记录"这次成片实际用了哪几张图"。与生图索引一起构成"当前生效"集合。
# 这是技能包约定的产物路径；新增技能包若换名，在这里补一条即可。
RENDER_PROPS_CANDIDATES = ("video/render_props.json",)

# 内部记账文件：不该被当成交付内容，但要出现在目录表里并标明性质
SYSTEM_FILES = {
    "canvas_edits.json": "画布编辑留痕",
    "canvas_overrides.json": "阶段级模型覆盖",
}

IMAGE_EXT = {"png", "jpg", "jpeg", "webp", "gif", "svg"}
INDEX_JSON_MAX_DEPTH = 4


def _kind_of(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return KIND_BY_EXT.get(ext, "other")


def _stem_of(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def _fmt_size(n: int) -> str:
    v = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if v < 1024 or u == "GB":
            return f"{v:.0f} {u}" if u == "B" else f"{v:.1f} {u}"
        v /= 1024
    return f"{v:.1f} GB"


def _fmt_time(iso: str) -> str:
    """ISO(UTC) → 本地 'YYYY-MM-DD HH:MM'。后端与本机同源，用本地时区更直观。"""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return iso or ""


def _read_json(storage: Any, run_id: str, rel: str, default: Any = None) -> Any:
    if not storage.exists(run_id, rel):
        return default
    try:
        return json.loads(storage.read_text(run_id, rel))
    except Exception as exc:  # noqa: BLE001
        logger.warning("目录表读取 %s 失败：%s", rel, exc)
        return default


# --------------------------------------------------------------------- 真实留痕


def _human_marks(storage: Any, run_id: str) -> dict:
    """从真实编辑留痕里还原"哪些文件被人动过"。没有留痕就是没有，不推测。"""
    edits = _read_json(storage, run_id, "canvas_edits.json", {}) or {}

    uploads: dict[str, str] = {}
    for ent in edits.get("upload") or []:
        p = ent.get("path")
        if p:
            uploads[str(p)] = str(ent.get("note") or "")

    # 人工重画过的主干（panel-03 这类）。注意：重画只覆盖**当前生效**的那个扩展名，
    # 同主干的历史文件（如早期占位 .svg）不算重画产物，另行标为历史残留。
    redo_stems: set[str] = set()
    for ent in edits.get("regenerate") or []:
        idx = ent.get("index")
        try:
            redo_stems.add(f"panel-{int(idx):02d}")
        except (TypeError, ValueError):
            continue

    edited_roots: dict[str, str] = {}
    if edits.get("panels"):
        edited_roots["panels.json"] = "分镜提示词经人工修改"
    if edits.get("characters"):
        edited_roots["characters.md"] = "角色卡经人工修改"

    return {
        "uploads": uploads,
        "redo_stems": redo_stems,
        "edited_roots": edited_roots,
        "counts": {k: len(v) for k, v in edits.items() if isinstance(v, list)},
    }


def _active_paths(storage: Any, run_id: str) -> set[str]:
    """当前**生效**的产物路径集合：生图索引里的每一格 + 成片实际用到的图。

    这是区分"现在用的"与"上一版残留"的依据 —— 目录里同时躺着 panel-01.svg 和
    panel-01.jpg 是常态，但只有一个是成片真正用的那个。
    """
    active: set[str] = set()

    idx = _read_json(storage, run_id, "media/images.json", None)
    if isinstance(idx, dict):
        for v in idx.values():
            if isinstance(v, dict) and v.get("path"):
                active.add(str(v["path"]))
            elif isinstance(v, str):
                active.add(v)

    for rel in RENDER_PROPS_CANDIDATES:
        props = _read_json(storage, run_id, rel, None)
        if isinstance(props, dict):
            for p in props.get("panels") or []:
                if isinstance(p, dict) and p.get("src"):
                    active.add(str(p["src"]))

    return active


def _upload_sources(storage: Any, run_id: str) -> dict[str, str]:
    """生图索引里自报 `source: upload` 的格 —— 人工上传的第二重证据。"""
    out: dict[str, str] = {}
    idx = _read_json(storage, run_id, "media/images.json", None)
    if isinstance(idx, dict):
        for v in idx.values():
            if isinstance(v, dict) and str(v.get("source") or "") == "upload" and v.get("path"):
                out[str(v["path"])] = str(v.get("note") or "")
    return out


# --------------------------------------------------------------------- 阶段归属兜底


def _index_inherited_stage(
    storage: Any, run_id: str, entries: list[dict], reg: dict[str, dict]
) -> dict[str, str]:
    """索引文件里登记的产物，继承**索引自身**的阶段。

    例：`media/images.json` 登记在分镜阶段，它里面写的 `panels/panel-01.jpg`
    就属于分镜阶段 —— 哪怕这张图是后来人工重画的、根本没进 DB 登记表。
    这样就不必把任何阶段名或产物路径写死在代码里。
    """
    known = {e["path"] for e in entries}
    out: dict[str, str] = {}
    for e in entries:
        if not e["path"].endswith(".json"):
            continue
        owner = reg.get(e["path"])
        if not owner or not owner.get("stage_id"):
            continue
        data = _read_json(storage, run_id, e["path"], None)
        for rel in _collect_string_values(data, depth=0):
            if rel == e["path"] or rel in reg or rel not in known:
                continue
            # 只继承**媒体产物**：索引文件登记的就是图/音/视频。
            # 否则规格文档里随手提一句 "storyboard.md" 就会把那个文件吸进错误的阶段。
            if _kind_of(rel) in ("image", "audio", "video"):
                out.setdefault(rel, str(owner["stage_id"]))
    return out


def _collect_string_values(node: Any, depth: int) -> list[str]:
    """递归收集 JSON 里"看起来像工作区相对路径"的字符串值。"""
    if depth > INDEX_JSON_MAX_DEPTH:
        return []
    out: list[str] = []
    if isinstance(node, dict):
        for v in node.values():
            out.extend(_collect_string_values(v, depth + 1))
    elif isinstance(node, list):
        for v in node:
            out.extend(_collect_string_values(v, depth + 1))
    elif isinstance(node, str):
        s = node.strip()
        # 只认"含目录分隔符 + 有扩展名"的短字符串，避免把散文误当路径
        if "/" in s and "." in s and len(s) < 160 and "\n" not in s:
            out.append(s.lstrip("./"))
    return out


def _origin_inherited_stage(entries: list[dict], reg: dict[str, dict]) -> dict[str, str]:
    """`X.origin.EXT`（人工编辑前的原件备份）继承**被备份文件** `X.EXT` 的阶段。

    备份文件天然不在 DB 登记表里，但它属于哪个阶段是确定的 —— 跟着它备份的那个文件走。
    """
    out: dict[str, str] = {}
    for e in entries:
        path: str = e["path"]
        name = path.rsplit("/", 1)[-1]
        if ".origin." not in name:
            continue
        stem, _, rest = name.partition(".origin.")
        dirname = path.rsplit("/", 1)[0] if "/" in path else ""
        pref = f"{dirname}/" if dirname else ""
        for cand in (f"{pref}{stem}.{rest}", f"{pref}{stem}"):
            owner = reg.get(cand)
            if owner and owner.get("stage_id"):
                out[path] = str(owner["stage_id"])
                break
    return out


def _stage_by_sibling(files: list[dict]) -> dict[str, str]:
    """同目录 + 同扩展名的多数票兜底（`panels/*.svg` 已归属，同目录的 `*.jpg` 跟着走）。

    **平票不猜**：根目录下 json 文件每样一个（constraints/spec/panels/lines），
    硬取一个当多数只会把文件塞进错误的阶段，那不如老实留在"其他文件"里。
    """
    votes: dict[tuple[str, str], dict[str, int]] = {}
    for f in files:
        if not f["stage_id"]:
            continue
        key = (f["dir"], f["ext"])
        votes.setdefault(key, {})
        votes[key][f["stage_id"]] = votes[key].get(f["stage_id"], 0) + 1

    out: dict[str, str] = {}
    for f in files:
        if f["stage_id"]:
            continue
        vote = votes.get((f["dir"], f["ext"])) or {}
        if not vote:
            continue
        ranked = sorted(vote.values(), reverse=True)
        if len(ranked) == 1 or ranked[0] > ranked[1]:
            out[f["path"]] = max(vote.items(), key=lambda kv: kv[1])[0]
    return out


# --------------------------------------------------------------------- 主入口


def build_file_index(
    *,
    storage: Any,
    run_id: str,
    run: Any,
    skill: Any,
    artifacts: list[Any],
) -> dict:
    """构建任务目录表。`run`/`skill`/`artifacts` 由调用方从 DB 与注册表取好传入。"""
    entries = storage.list_entries(run_id)

    # 阶段：id → 名称、顺序（全部读自 manifest）
    stages = list(getattr(skill, "stages", []) or [])
    stage_name = {s.id: s.name for s in stages}
    stage_order = {s.id: i for i, s in enumerate(stages)}

    # artifacts 表：rel_path → 该文件的阶段归属与登记信息
    reg: dict[str, dict] = {}
    for a in artifacts:
        d = a.to_dict() if hasattr(a, "to_dict") else dict(a)
        if d.get("rel_path"):
            reg[str(d["rel_path"])] = d

    human = _human_marks(storage, run_id)
    uploads: dict[str, str] = human["uploads"]  # type: ignore[assignment]
    uploads.update(_upload_sources(storage, run_id))
    redo_stems: set[str] = human["redo_stems"]  # type: ignore[assignment]
    edited_roots: dict[str, str] = human["edited_roots"]  # type: ignore[assignment]

    active = _active_paths(storage, run_id)
    inherited = _index_inherited_stage(storage, run_id, entries, reg)
    inherited.update(_origin_inherited_stage(entries, reg))

    # 第一遍：基本属性 + 阶段（DB > 索引继承 > 待定）
    files: list[dict] = []
    for e in entries:
        path: str = e["path"]
        name = path.rsplit("/", 1)[-1]
        dirname = path.rsplit("/", 1)[0] if "/" in path else ""
        stem = _stem_of(name)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

        info = reg.get(path) or {}
        kind = info.get("kind") if info.get("kind") not in (None, "", "file") else _kind_of(path)
        stage_id = str(info.get("stage_id") or inherited.get(path) or "")

        files.append(
            {
                "path": path,
                "name": name,
                "dir": dirname,
                "stem": stem,
                "ext": ext,
                "kind": kind,
                "size": e["size"],
                "mtime": e["mtime"],
                "stage_id": stage_id,
                "stage_name": stage_name.get(stage_id, ""),
                "source": "",
                "source_label": "",
                "note": "",
                "in_use": path in active,
                "registered": path in reg,
                "stage_inherited": bool(not info.get("stage_id") and inherited.get(path)),
            }
        )

    # 第二遍：阶段兜底 + 来源判定（顺序即优先级，先命中先算）
    sibling_stage = _stage_by_sibling(files)
    active_stems = {(f["dir"], f["stem"]) for f in files if f["in_use"]}

    for f in files:
        path, name, stem, dirname = f["path"], f["name"], f["stem"], f["dir"]

        if name in SYSTEM_FILES:
            # 记账文件（编辑留痕 / 阶段覆盖）不属于任何创作阶段 —— 它们横跨全部阶段，
            # 硬塞进某个阶段只会让人误以为"这是某阶段的产物"。
            f["stage_id"], f["stage_name"] = "", ""
        elif not f["stage_id"] and path in sibling_stage:
            f["stage_id"] = sibling_stage[path]
            f["stage_name"] = stage_name.get(f["stage_id"], "")
            f["stage_inherited"] = True

        # ---- 来源 ----
        if path in uploads:
            f["source"], f["note"] = "human_upload", "人工上传替换"
        elif path in edited_roots:
            f["source"], f["note"] = "human_edit", edited_roots[path]
        elif "panels" in dirname.split("/") and stem in redo_stems and f["in_use"]:
            f["source"], f["note"] = "human_redo", "在画布上单独重画"
        elif ".origin." in name:
            f["source"], f["note"] = "backup", "首次人工编辑前的模型原件"
        elif name in SYSTEM_FILES:
            f["source"], f["note"] = "system", SYSTEM_FILES[name]
        elif not f["in_use"] and (dirname, stem) in active_stems:
            # 判定放在"DB 登记"之前：同格已经有新图生效，这就是上一版残留，
            # 哪怕它当年也是模型正经生成的产物。
            f["source"], f["note"] = "stale", "同格已有新图，这是上一版残留"
        elif f["registered"] or f["in_use"]:
            # in_use 也算数：本次成片真正启用的画面就是核心产物，
            # 哪怕它是后来重新生成、没进 DB 登记表的那一版。
            f["source"] = "model"
        else:
            f["source"], f["note"] = "aux", "未登记的中间产物"

        f["source_label"] = SOURCE_LABEL.get(f["source"], f["source"])

    files.sort(key=lambda f: (f["dir"], f["name"]))

    def _group(key: str, label: str, order: int, items: list[dict]) -> dict:
        return {
            "id": key,
            "label": label,
            "order": order,
            "count": len(items),
            "bytes": sum(i["size"] for i in items),
            "files": items,
        }

    # ---- 分组一：按阶段（顺序读 manifest，未归属的排最后）----
    by_stage: dict[str, list[dict]] = {}
    for f in files:
        by_stage.setdefault(f["stage_id"] or "_unassigned", []).append(f)
    stage_groups = [
        _group(
            sid,
            f"{stage_order[sid] + 1}. {stage_name[sid]}" if sid in stage_name else "其他文件",
            stage_order.get(sid, 999),
            items,
        )
        for sid, items in by_stage.items()
    ]
    stage_groups.sort(key=lambda g: (g["order"], g["label"]))

    # ---- 分组二：按目录 ----
    by_dir: dict[str, list[dict]] = {}
    for f in files:
        by_dir.setdefault(f["dir"] or "(根目录)", []).append(f)
    dir_groups = [_group(d, d, 0 if d == "(根目录)" else 1, items) for d, items in sorted(by_dir.items())]

    # ---- 类型分布 ----
    kinds: dict[str, dict] = {}
    for f in files:
        k = kinds.setdefault(
            f["kind"],
            {"kind": f["kind"], "label": KIND_LABEL.get(f["kind"], f["kind"]), "count": 0, "bytes": 0},
        )
        k["count"] += 1
        k["bytes"] += f["size"]

    human_touched = [f for f in files if f["source"].startswith("human")]

    return {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "files": len(files),
            "bytes": sum(f["size"] for f in files),
            "dirs": len([d for d in by_dir if d != "(根目录)"]),
            "stages": len([g for g in stage_groups if g["id"] != "_unassigned"]),
            "human_touched": len(human_touched),
            "active": len([f for f in files if f["in_use"]]),
            "unassigned": len(by_stage.get("_unassigned", [])),
            "kinds": sorted(kinds.values(), key=lambda k: -k["bytes"]),
            "edit_counts": human["counts"],
        },
        "groups_by_stage": stage_groups,
        "groups_by_dir": dir_groups,
        "files": files,
    }


# --------------------------------------------------------------------- 导出清单


def render_markdown(index: dict, run: Any, *, group_by: str = "stage") -> str:
    """把目录表渲染成一份可交付的 Markdown 清单。"""
    r = run.to_dict() if hasattr(run, "to_dict") else dict(run)
    st = index["stats"]
    groups = index["groups_by_stage"] if group_by == "stage" else index["groups_by_dir"]

    L: list[str] = []
    L.append(f"# 任务目录表 · {r.get('title') or '(无标题)'}")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append(f"| 任务 ID | `{r.get('id')}` |")
    L.append(f"| 技能包 | {r.get('skill_id')}（{r.get('skill_name') or ''}） |")
    L.append(f"| 通道 | {'精修' if r.get('channel') == 'slow' else '快'} |")
    L.append(f"| 状态 | {r.get('status')} |")
    L.append(f"| 创建时间 | {_fmt_time(str(r.get('created_at') or ''))} |")
    if r.get("finished_at"):
        L.append(f"| 结束时间 | {_fmt_time(str(r.get('finished_at')))} |")
    if r.get("ttfv_seconds") is not None:
        L.append(f"| TTFV（首个成品落盘） | {r.get('ttfv_seconds')} s |")
    L.append(f"| 累计成本 | ${float(r.get('cost_usd') or 0):.4f} |")
    L.append(f"| token | {r.get('tokens_used') or 0} |")
    L.append(f"| 清单生成时间 | {_fmt_time(index['generated_at'])} |")
    L.append("")

    L.append("## 总览")
    L.append("")
    L.append("| 项目 | 值 |")
    L.append("|---|---|")
    L.append(f"| 文件数 | {st['files']} |")
    L.append(f"| 总体积 | {_fmt_size(st['bytes'])} |")
    L.append(f"| 目录数 | {st['dirs']} |")
    L.append(f"| 阶段数 | {st['stages']} |")
    L.append(f"| 人工改动文件 | {st['human_touched']} |")
    if st.get("active"):
        L.append(f"| 本次成片实际启用 | {st['active']} 个文件 |")
    L.append("")

    L.append("### 按类型")
    L.append("")
    L.append("| 类型 | 个数 | 体积 |")
    L.append("|---|---|---|")
    for k in st["kinds"]:
        L.append(f"| {k['label']} | {k['count']} | {_fmt_size(k['bytes'])} |")
    L.append("")

    L.append(f"## 目录清单（按{'阶段' if group_by == 'stage' else '目录'}）")
    for g in groups:
        L.append("")
        L.append(f"### {g['label']} · {g['count']} 个文件 · {_fmt_size(g['bytes'])}")
        L.append("")
        L.append("| 文件 | 类型 | 大小 | 来源 | 修改时间 | 备注 |")
        L.append("|---|---|---|---|---|---|")
        for f in g["files"]:
            note = f["note"] or ("本次成片启用" if f["in_use"] else "")
            if f.get("stage_inherited") and not note:
                note = "阶段按索引归属"
            L.append(
                f"| `{f['path']}` | {KIND_LABEL.get(f['kind'], f['kind'])} | {_fmt_size(f['size'])} "
                f"| {f['source_label']} | {_fmt_time(f['mtime'])} | {note} |"
            )
    L.append("")
    L.append("---")
    L.append("")
    L.append("> 本清单由 Longspan 自动生成，数据取自任务工作区的实际文件（非登记缓存）。")
    L.append("> 「模型生成 / 人工修改 / 人工上传 / 人工重画 / 历史残留」的区分来自画布编辑留痕与生图索引，不是推测。")
    L.append("")
    return "\n".join(L)
