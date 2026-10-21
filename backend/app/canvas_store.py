"""画布编辑：人在画布上改过的东西，全部要留痕。

三条设计取舍（对应项目铁律"禁止静默降级"）：

1. **改工作稿，但先留原件。**
   panels.json / characters.md 是工作稿，人改了就直接改它 —— 不做"覆盖层合并"
   那种复杂机制。但**首次人工编辑前自动备份原件**（`*.origin.*`），
   这样"模型原本写了什么"永远查得到，证据不会被人改没了。

2. **每次编辑都记账。**
   `canvas_edits.json` 记谁在什么时候改了哪一段。画布上据此显示"人工修改"标记 ——
   否则交付时说不清"这版到底是谁写的"。

3. **阶段级模型覆盖独立存放。**
   `canvas_overrides.json` 存"这个阶段单独用哪个模型"。它不改全局配置，
   只影响该 Run 该阶段 —— 一个任务的实验不该污染整站。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("longspan.canvas")

PANELS_REL = "panels.json"
PANELS_ORIGIN_REL = "panels.origin.json"
CHARACTERS_REL = "characters.md"
CHARACTERS_ORIGIN_REL = "characters.origin.md"
IMAGES_REL = "media/images.json"
LINES_REL = "lines.json"
OVERRIDES_REL = "canvas_overrides.json"
EDITS_REL = "canvas_edits.json"

# 一格里能挂的三帧。顺序即播放顺序：首帧 → 中间帧 → 尾帧。
# 只挂首帧 = 现在的单图 + 运镜；挂多帧 = 帧间交叉过渡（动作的"起承转"）。
FRAME_ROLES: tuple[str, ...] = ("first", "mid", "last")
FRAME_LABEL: dict[str, str] = {"first": "首帧", "mid": "中间帧", "last": "尾帧"}

# 一个阶段可以被单独覆盖的模型槽位
OVERRIDE_SLOTS = ("llm_model", "image_model", "image_service", "tts_voice", "tts_speed", "video_model")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------- 基础读写


def read_json(storage: Any, run_id: str, rel: str, default: Any = None) -> Any:
    if not storage.exists(run_id, rel):
        return default
    try:
        return json.loads(storage.read_text(run_id, rel))
    except Exception as exc:  # noqa: BLE001
        logger.warning("画布读取失败 %s: %s", rel, exc)
        return default


def read_text(storage: Any, run_id: str, rel: str, default: str = "") -> str:
    if not storage.exists(run_id, rel):
        return default
    try:
        return storage.read_text(run_id, rel)
    except Exception:  # noqa: BLE001
        return default


def _backup_once(storage: Any, run_id: str, rel: str, backup_rel: str) -> bool:
    """首次编辑前把原件存一份。已备份就跳过（绝不覆盖第一版原件）。"""
    if storage.exists(run_id, backup_rel) or not storage.exists(run_id, rel):
        return False
    try:
        raw = storage.read_text(run_id, rel)
        storage.write_text(run_id, backup_rel, raw)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("原件备份失败 %s: %s", rel, exc)
        return False


# --------------------------------------------------------------------- 编辑留痕


def load_edits(storage: Any, run_id: str) -> dict:
    return read_json(storage, run_id, EDITS_REL, {}) or {}


def record_edit(storage: Any, run_id: str, kind: str, detail: dict) -> None:
    """记一笔人工编辑。kind: panels / characters / overrides / upload / regenerate。"""
    data = load_edits(storage, run_id)
    entries = data.setdefault(kind, [])
    if not isinstance(entries, list):
        entries = data[kind] = []
    entries.append({"at": _now(), **detail})
    data["updated_at"] = _now()
    try:
        storage.write_json(run_id, EDITS_REL, data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("编辑留痕写入失败：%s", exc)


def count_edits(storage: Any, run_id: str) -> dict:
    data = load_edits(storage, run_id)
    return {k: (len(v) if isinstance(v, list) else 0) for k, v in data.items() if k != "updated_at"}


# --------------------------------------------------------------------- 分镜清单


def load_panels(storage: Any, run_id: str) -> dict:
    data = read_json(storage, run_id, PANELS_REL, {}) or {}
    if isinstance(data, list):
        data = {"panels": data}
    if not isinstance(data.get("panels"), list):
        data["panels"] = []
    return data


def save_panels(storage: Any, run_id: str, panels: list[dict], edited_keys: list[str]) -> dict:
    data = load_panels(storage, run_id)
    backed_up = _backup_once(storage, run_id, PANELS_REL, PANELS_ORIGIN_REL)
    data["panels"] = panels
    data["human_edited"] = True
    data["human_edited_at"] = _now()
    storage.write_json(run_id, PANELS_REL, data)
    record_edit(storage, run_id, "panels", {"keys": edited_keys, "count": len(edited_keys)})
    return {"ok": True, "backed_up_origin": backed_up, "edited": len(edited_keys)}


def load_image_index(storage: Any, run_id: str) -> dict:
    return read_json(storage, run_id, IMAGES_REL, {}) or {}


def save_image_index(storage: Any, run_id: str, index: dict) -> None:
    storage.write_json(run_id, IMAGES_REL, index)


def drop_image_entry(storage: Any, run_id: str, key: str) -> dict:
    """把某一格从索引里摘掉 —— 下一次生成就是"重新画这一格"。"""
    index = load_image_index(storage, run_id)
    return index.pop(str(key), None) or {}


# --------------------------------------------------------------------- 多帧 / 视频

_KEEP_ON_FRAME = ("path", "provider", "size", "is_placeholder", "source", "note", "ratio", "prompt_hint")


def frames_of(entry: dict) -> dict[str, dict]:
    """取一格的多帧。

    兼容旧数据：老索引只有顶层 `path`，没有 frames —— 那就把它当**首帧**。
    这样"给已经跑完的老任务补中间帧"不需要迁移脚本，历史成片也不会因为
    这次改动而重渲。
    """
    frames: dict[str, dict] = {}
    raw = entry.get("frames") if isinstance(entry.get("frames"), dict) else {}
    for role in FRAME_ROLES:
        item = raw.get(role)
        if isinstance(item, dict) and item.get("path"):
            frames[role] = item
    if not frames and entry.get("path"):
        frames["first"] = {k: entry[k] for k in _KEEP_ON_FRAME if k in entry}
    return frames


def ordered_frame_paths(entry: dict) -> list[str]:
    """按播放顺序给出可用帧路径（去重，保持顺序）。"""
    frames = frames_of(entry)
    out: list[str] = []
    for role in FRAME_ROLES:
        p = (frames.get(role) or {}).get("path")
        if p and p not in out:
            out.append(p)
    return out


def set_frame(storage: Any, run_id: str, index: str, role: str, payload: dict) -> dict:
    """写入某格某一帧。首帧同时更新顶层 path（保持老代码与老成片兼容）。"""
    if role not in FRAME_ROLES:
        raise ValueError(f"未知的帧位：{role}（只支持 {' / '.join(FRAME_ROLES)}）")
    index_data = load_image_index(storage, run_id)
    key = str(index)
    entry = dict(index_data.get(key) or {})
    frames = frames_of(entry)
    frames[role] = {k: payload[k] for k in _KEEP_ON_FRAME if k in payload}
    frames[role]["role"] = role
    entry["frames"] = frames
    if role == "first" or not entry.get("path"):
        entry["path"] = frames[role].get("path")
        for field in ("provider", "size", "is_placeholder", "source"):
            if field in frames[role]:
                entry[field] = frames[role][field]
    index_data[key] = entry
    save_image_index(storage, run_id, index_data)
    return {"index": key, "role": role, "frame": frames[role], "frames": frames}


def drop_frame(storage: Any, run_id: str, index: str, role: str) -> dict:
    """删掉某一帧。首帧删掉后，若还有其它帧就自动顶上来（不留空壳）。"""
    index_data = load_image_index(storage, run_id)
    key = str(index)
    entry = dict(index_data.get(key) or {})
    frames = frames_of(entry)
    frames.pop(role, None)
    if role == "first" and frames:
        promoted_role = next((r for r in FRAME_ROLES if r in frames), None)
        if promoted_role:
            entry["path"] = frames[promoted_role].get("path")
            for field in ("provider", "size", "is_placeholder", "source"):
                if field in frames[promoted_role]:
                    entry[field] = frames[promoted_role][field]
    if frames:
        entry["frames"] = frames
        index_data[key] = entry
    else:
        index_data.pop(key, None)
    save_image_index(storage, run_id, index_data)
    return {"index": key, "role": role, "frames": frames_of(entry) if frames else {}}


def set_panel_video(storage: Any, run_id: str, index: str, video: dict | None) -> dict:
    """记录/清除某格的视频片段。video=None 表示清除。"""
    index_data = load_image_index(storage, run_id)
    key = str(index)
    entry = dict(index_data.get(key) or {})
    if video is None:
        entry.pop("video", None)
    else:
        entry["video"] = video
    index_data[key] = entry
    save_image_index(storage, run_id, index_data)
    return {"index": key, "video": entry.get("video")}


def panel_video(entry: dict) -> dict | None:
    """只返回「有成品文件」的片段 —— 给成片渲染用，running/failed 不该进片。"""
    v = entry.get("video")
    return v if isinstance(v, dict) and v.get("path") else None


def video_state(entry: dict) -> dict | None:
    """给前端看的完整视频状态。

    和 panel_video 的区别：running / failed 也要返回。生成中没有 path，
    如果也按 path 判空，前端就看不到"生成中"和失败原因，只剩一个空区域。
    """
    v = entry.get("video")
    if not isinstance(v, dict) or not v.get("status"):
        return None
    return {
        "status": str(v.get("status") or ""),
        "path": str(v.get("path") or ""),
        "error": str(v.get("error") or ""),
        "duration": v.get("duration") or 0,
        "mode": str(v.get("mode") or ""),
        "model": str(v.get("model") or ""),
        "task_id": str(v.get("task_id") or ""),
        "started_at": str(v.get("started_at") or ""),
        "finished_at": str(v.get("finished_at") or ""),
        "prompt": str(v.get("prompt") or ""),
        "reference": str(v.get("reference") or ""),
        "frames": [str(x) for x in (v.get("frames") or []) if x],
        "dropped_frames": [str(x) for x in (v.get("dropped_frames") or []) if x],
        "note": str(v.get("note") or ""),
    }


# --------------------------------------------------------------------- 角色卡


def load_characters(storage: Any, run_id: str) -> str:
    return read_text(storage, run_id, CHARACTERS_REL, "")


def save_characters(storage: Any, run_id: str, text: str) -> dict:
    backed_up = _backup_once(storage, run_id, CHARACTERS_REL, CHARACTERS_ORIGIN_REL)
    storage.write_text(run_id, CHARACTERS_REL, text)
    record_edit(storage, run_id, "characters", {"chars": len(text)})
    return {"ok": True, "backed_up_origin": backed_up, "chars": len(text)}


def characters_state(storage: Any, run_id: str) -> dict:
    """角色卡状态：当前文本 + 从锚点里抽出的角色名（供画布渲染角色标签）。"""
    text = load_characters(storage, run_id)
    # 角色标题形如「陈默（主角）」；不带括号的二级标题通常是"自检 / 留痕"等说明段，不算角色
    names: list[str] = []
    loose: list[str] = []
    for line in text.splitlines():
        if not line.startswith("## "):
            continue
        raw = line[3:].strip()
        if not raw:
            continue
        bare = raw.split("（")[0].split("(")[0].strip()
        if "（" in raw or "(" in raw:
            names.append(bare)
        elif bare:
            loose.append(bare)
    return {
        "text": text,
        # 一个带括号的都没抽到，就退回全部二级标题（不猜、但也不空手）
        "roles": names or loose,
        "has_origin": storage.exists(run_id, CHARACTERS_ORIGIN_REL),
        "origin_text": read_text(storage, run_id, CHARACTERS_ORIGIN_REL, ""),
    }


# --------------------------------------------------------------------- 阶段覆盖


def load_overrides(storage: Any, run_id: str) -> dict:
    data = read_json(storage, run_id, OVERRIDES_REL, {}) or {}
    return data.get("stages", {}) if isinstance(data.get("stages"), dict) else {}


def save_stage_override(storage: Any, run_id: str, stage_id: str, patch: dict) -> dict:
    """写某个阶段的模型覆盖。空字符串 = 清除该槽位，回到全局配置。"""
    data = read_json(storage, run_id, OVERRIDES_REL, {}) or {}
    stages = data.setdefault("stages", {})
    cur = dict(stages.get(stage_id) or {})
    changed: list[str] = []
    for slot in OVERRIDE_SLOTS:
        if slot not in patch:
            continue
        val = patch.get(slot)
        val = str(val).strip() if val is not None else ""
        if val:
            if cur.get(slot) != val:
                cur[slot] = val
                changed.append(slot)
        else:
            if slot in cur:
                cur.pop(slot, None)
                changed.append(slot)
    if cur:
        stages[stage_id] = cur
    else:
        stages.pop(stage_id, None)
    data["stages"] = stages
    data["updated_at"] = _now()
    storage.write_json(run_id, OVERRIDES_REL, data)
    if changed:
        record_edit(storage, run_id, "overrides", {"stage_id": stage_id, "slots": changed})
    return {"stage_id": stage_id, "override": cur, "changed": changed}


def effective_image_config(storage: Any, run_id: str, stage_id: str) -> dict:
    """某阶段生效的生图配置（阶段覆盖 > 全局默认）。工具执行时读这个。"""
    from .config import settings

    ov = load_overrides(storage, run_id).get(stage_id) or {}
    return {
        "model": ov.get("image_model") or settings.image_model,
        "service": ov.get("image_service") or settings.image_service,
        "overridden": bool(ov.get("image_model") or ov.get("image_service")),
    }


def effective_llm_model(storage: Any, run_id: str, stage_id: str) -> str | None:
    ov = load_overrides(storage, run_id).get(stage_id) or {}
    return ov.get("llm_model") or None


# --------------------------------------------------------------------- 上传


ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".webm"}
ALLOWED_AUDIO_EXT = {".mp3", ".m4a", ".wav", ".aac"}

# 「可以直接在画布上编辑」的文本类文件
EDITABLE_TEXT_EXT = {".md", ".markdown", ".json", ".txt", ".yaml", ".yml", ".csv", ".srt"}
MAX_TEXT_EDIT_BYTES = 4 * 1024 * 1024


def origin_rel(rel: str) -> str:
    """`script.md` → `script.origin.md`；`x/y.json` → `x/y.origin.json`。

    与既有约定保持一致（panels.origin.json / characters.origin.md），
    这样目录表里"历史残留"的识别规则不用改。
    """
    if "/" in rel:
        head, name = rel.rsplit("/", 1)
        prefix = head + "/"
    else:
        prefix, name = "", rel
    if "." in name:
        stem, ext = name.rsplit(".", 1)
        return f"{prefix}{stem}.origin.{ext}"
    return f"{prefix}{name}.origin"


def save_artifact_text(storage: Any, run_id: str, rel: str, text: str) -> dict:
    """人工改写任意文本产物。JSON 会先解析再规范化写回（写坏了当场拒绝）。

    为什么 JSON 必须先解析：这些清单是**下游工具的唯一输入**，
    放一个语法错误的 panels.json 进去，下一次生成会以"解析失败"整段崩掉，
    而用户只看到"生成失败"四个字。宁可在保存这一刻就拦住并说清哪一行错了。
    """
    rel = str(rel or "").strip().lstrip("/")
    if not rel or ".." in rel.split("/"):
        raise ValueError("非法路径")
    name = rel.rsplit("/", 1)[-1]
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if suffix not in EDITABLE_TEXT_EXT:
        raise ValueError(
            f"{suffix or '（无扩展名）'} 不是可编辑的文本类型（支持 {' '.join(sorted(EDITABLE_TEXT_EXT))}）"
        )
    if len((text or "").encode("utf-8")) > MAX_TEXT_EDIT_BYTES:
        raise ValueError(f"文本超过 {MAX_TEXT_EDIT_BYTES // 1024 // 1024}MB，请拆分后再编辑")

    normalized = text
    if suffix == ".json":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 语法错误（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}") from exc
        normalized = json.dumps(obj, ensure_ascii=False, indent=2)

    backed_up = _backup_once(storage, run_id, rel, origin_rel(rel))
    storage.write_text(run_id, rel, normalized)
    record_edit(storage, run_id, "artifact", {"path": rel, "chars": len(normalized)})
    return {"ok": True, "path": rel, "chars": len(normalized), "backed_up_origin": backed_up}


def save_artifact_bytes(
    storage: Any, run_id: str, rel: str, data: bytes, note: str = ""
) -> dict:
    """人工上传任意二进制产物（图 / 视频 / 音频）。原件同样先备份。"""
    rel = str(rel or "").strip().lstrip("/")
    if not rel or ".." in rel.split("/"):
        raise ValueError("非法路径")
    suffix = "." + rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
    if suffix not in (ALLOWED_IMAGE_EXT | ALLOWED_VIDEO_EXT | ALLOWED_AUDIO_EXT):
        raise ValueError(f"不支持的上传类型：{suffix or '（无扩展名）'}")
    if not data:
        raise ValueError("文件内容为空")
    if storage.exists(run_id, rel):
        _backup_once(storage, run_id, rel, origin_rel(rel))
    saved = save_uploaded_image(storage, run_id, rel, data, note=note)
    return {"ok": True, **saved}


def restore_artifact(storage: Any, run_id: str, rel: str) -> dict:
    """用备份原件覆盖回去 —— 人改坏了有路可退（原件始终是第一版）。"""
    bk = origin_rel(rel)
    if not storage.exists(run_id, bk):
        raise ValueError("这个文件没有备份原件（说明还没被人工改过）")
    if rel.rsplit(".", 1)[-1].lower() in {e.lstrip(".") for e in EDITABLE_TEXT_EXT}:
        text = storage.read_text(run_id, bk)
        storage.write_text(run_id, rel, text)
    else:
        data = storage.abs_path(run_id, bk).read_bytes()
        storage.write_bytes(run_id, rel, data)
    record_edit(storage, run_id, "artifact_restore", {"path": rel})
    return {"ok": True, "path": rel, "restored_from": bk}


def artifact_state(storage: Any, run_id: str, rel: str) -> dict:
    return {
        "has_origin": storage.exists(run_id, origin_rel(rel)),
        "origin_path": origin_rel(rel),
    }


def save_uploaded_image(
    storage: Any, run_id: str, rel_path: str, data: bytes, note: str = ""
) -> dict:
    rel, size = storage.write_bytes(run_id, rel_path, data)
    record_edit(storage, run_id, "upload", {"path": rel, "bytes": size, "note": note})
    return {"path": rel, "size": size}
