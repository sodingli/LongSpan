"""全步骤产物编辑：任何阶段产出的东西，人都能改。

为什么不再只围绕分镜做编辑：
「画布能改分镜」只覆盖了交付链的一段。真实使用里，人最想改的往往是**别的阶段**的产物 ——
约束理解错了、剧本某句台词太蠢、角色锚点写偏了、配音方案排错了。要求用户"回去重跑整个阶段"
来改一句话，既慢又贵（还可能顺手把别的产物改坏）。

四条设计取舍（与项目铁律一致）：

1. **文件系统是事实来源。** 阶段归属读 `file_index`（它已经把 DB 登记、索引继承、
   多数票推断都算过了），这里不重新猜一遍。
2. **改之前先留原件。** 与画布编辑同一套约定：`xxx.origin.md`，只备份第一次，
   之后无论改多少版，第一版模型原文永远查得到。
3. **JSON 必须先过语法关。** 这些清单是下游工具的唯一输入，放一个坏 JSON 进去，
   下一次生成会以"解析失败"整段崩，而用户只看到"生成失败"四个字。
4. **能力边界如实说。** 二进制（图片/视频/音频）不走文本编辑器 —— 它们只能整份替换，
   而且替换后要标注"人工上传"，不冒充模型产物。
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import canvas_store as cs
from ..db import get_db
from ..events import EventType, bus
from ..file_index import build_file_index
from ..harness.skill import registry
from ..models import Artifact, Run
from ..store import storage

logger = logging.getLogger("longspan.edit.api")

router = APIRouter(prefix="/api/runs", tags=["edit"])

# 注意：路径前缀用 /artifact/ 而不是 /files/ —— runs.py 已有 `/{run_id}/files/{rel_path:path}`，
# 它会把 `/files/content` 当成"名为 content 的产物文件"抢先匹配（实测 404）。


_DATA_URL_RE = re.compile(r"^data:([a-zA-Z0-9.+/-]+);base64,(.*)$", re.DOTALL)

# 这些是系统记账文件，人改了会让幂等复用/渲染对不上账，一律不给入口
SYSTEM_REL = {
    "media/images.json",
    "media/audio.json",
    "video/render_props.json",
    "canvas_edits.json",
    "canvas_overrides.json",
}


def _require_run(db: Session, run_id: str) -> Run:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return run


def _stage_files(db: Session, run_id: str, stage_id: str) -> tuple[list[dict], dict]:
    run = _require_run(db, run_id)
    artifacts = db.query(Artifact).filter(Artifact.run_id == run_id).all()
    index = build_file_index(
        storage=storage, run_id=run_id, run=run, skill=registry.get(run.skill_id), artifacts=artifacts
    )
    stage = next((s for s in getattr(registry.get(run.skill_id), "stages", []) or [] if s.id == stage_id), None)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"技能里没有这个阶段：{stage_id}")

    out: list[dict] = []
    for f in index["files"]:
        if f["stage_id"] != stage_id:
            continue
        path = f["path"]
        kind = f["kind"]
        is_system = path in SYSTEM_REL
        editable = (not is_system) and f["ext"] in {e.lstrip(".") for e in cs.EDITABLE_TEXT_EXT}
        replaceable = (not is_system) and kind in {"image", "video", "audio"}
        out.append(
            {
                "path": path,
                "name": f["name"],
                "dir": f["dir"],
                "ext": f["ext"],
                "kind": kind,
                "size": f["size"],
                "mtime": f["mtime"],
                "source": f["source"],
                "source_label": f["source_label"],
                "note": f["note"],
                "in_use": f["in_use"],
                "editable": editable,
                "replaceable": replaceable,
                "system": is_system,
                "has_origin": storage.exists(run_id, cs.origin_rel(path)),
            }
        )

    # 阶段声明要产出的文件还没出现 —— 明确列出来，而不是让用户以为都在这儿了
    produced = {x["path"] for x in out}
    missing = [p for p in (getattr(stage, "produces", []) or []) if p not in produced]
    return out, {"id": stage.id, "name": stage.name, "produces": list(getattr(stage, "produces", []) or []), "missing": missing}


@router.get("/{run_id}/stages/{stage_id}/files")
def list_stage_files(run_id: str, stage_id: str, db: Session = Depends(get_db)) -> dict:
    files, stage = _stage_files(db, run_id, stage_id)
    return {
        "run_id": run_id,
        "stage": stage,
        "files": files,
        "count": len(files),
        "editable_count": sum(1 for f in files if f["editable"]),
        "replaceable_count": sum(1 for f in files if f["replaceable"]),
        "limits": {
            "text_bytes": cs.MAX_TEXT_EDIT_BYTES,
            "image_bytes": 12 * 1024 * 1024,
            "media_bytes": 80 * 1024 * 1024,
            "text_ext": sorted(cs.EDITABLE_TEXT_EXT),
        },
    }


def _resolve_visible(run_id: str, path: str) -> str:
    rel = str(path or "").strip().lstrip("/")
    if not rel or ".." in rel.split("/"):
        raise HTTPException(status_code=400, detail="非法路径")
    if not storage.exists(run_id, rel):
        raise HTTPException(status_code=404, detail=f"文件不存在：{rel}")
    return rel


@router.get("/{run_id}/artifact/content")
def read_content(run_id: str, path: str, db: Session = Depends(get_db)) -> dict:
    _require_run(db, run_id)
    rel = _resolve_visible(run_id, path)
    name = rel.rsplit("/", 1)[-1]
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if ext not in cs.EDITABLE_TEXT_EXT:
        raise HTTPException(status_code=415, detail=f"{ext} 不是文本文件，不能当文本编辑")
    size = storage.abs_path(run_id, rel).stat().st_size
    if size > cs.MAX_TEXT_EDIT_BYTES:
        raise HTTPException(status_code=413, detail=f"文件 {size / 1024 / 1024:.1f}MB 超过文本编辑上限")
    text = storage.read_text(run_id, rel)
    return {
        "path": rel,
        "text": text,
        "bytes": size,
        "lines": text.count("\n") + 1,
        "ext": ext,
        "has_origin": storage.exists(run_id, cs.origin_rel(rel)),
        # 前端要据此决定「给不给编辑框」，而不是先让人写一堆字再被拒
        "system": rel in SYSTEM_REL,
        "editable": rel not in SYSTEM_REL,
    }


@router.put("/{run_id}/artifact/content")
def write_content(run_id: str, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    _require_run(db, run_id)
    rel = _resolve_visible(run_id, str(payload.get("path") or ""))
    # 系统记账文件在 API 层也要拦。之前只有列表中标记为 system、前端不给入口，
    # 但直接构造一个 PUT 请求照样能改坏 media/images.json。
    if rel in SYSTEM_REL:
        raise HTTPException(
            status_code=403,
            detail=f"{rel} 是系统记账文件（索引/渲染参数），不能直接编辑；"
            "改画面请用画布的分镜编辑，换素材请用上传替换。",
        )
    text = payload.get("content")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="content 必须是字符串")
    before = storage.read_text(run_id, rel)
    if before == text:
        return {"ok": True, "path": rel, "changed": False, "note": "内容没有变化"}
    try:
        res = cs.save_artifact_text(storage, run_id, rel, text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    bus.emit(
        db, run_id, EventType.AGENT_THOUGHT,
        f"人工修改了 {rel}（{res['chars']} 字）",
        stage_id=str(payload.get("stage_id") or "") or None,
        payload={"human_edit": "artifact", "path": rel, "chars": res["chars"],
                 "backed_up_origin": res.get("backed_up_origin")},
    )
    return {**res, "changed": True}


@router.post("/{run_id}/artifact/upload")
def upload_file(
    run_id: str, payload: dict = Body(...), db: Session = Depends(get_db)
) -> dict:
    """整份替换一个二进制产物（图片 / 视频 / 音频）。"""
    _require_run(db, run_id)
    rel = str(payload.get("path") or "").strip().lstrip("/")
    if not rel or ".." in rel.split("/"):
        raise HTTPException(status_code=400, detail="非法路径")
    ext = "." + rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
    if ext not in (cs.ALLOWED_IMAGE_EXT | cs.ALLOWED_VIDEO_EXT | cs.ALLOWED_AUDIO_EXT):
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型：{ext or '（无扩展名）'}（支持图片 / 视频 / 音频）",
        )
    raw = str(payload.get("data_url") or "")
    m = _DATA_URL_RE.match(raw.strip())
    if not m:
        raise HTTPException(status_code=400, detail="只接受 data:<mime>;base64, 格式的数据")
    try:
        data = base64.b64decode(m.group(2))
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"数据解析失败：{exc}") from exc
    if not data:
        raise HTTPException(status_code=400, detail="文件内容为空")
    limit = 12 * 1024 * 1024 if ext in cs.ALLOWED_IMAGE_EXT else 80 * 1024 * 1024
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"文件超过 {limit // 1024 // 1024}MB")

    try:
        res = cs.save_artifact_bytes(storage, run_id, rel, data, note=str(payload.get("note") or "人工上传替换"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    bus.emit(
        db, run_id, EventType.ARTIFACT_CREATED,
        f"人工上传替换了 {rel}（{len(data) / 1024:.0f}KB）",
        stage_id=str(payload.get("stage_id") or "") or None,
        payload={"human_edit": "artifact_upload", "path": rel, "bytes": len(data)},
    )
    # 换图之后，若这文件正是某格的主图，索引里的尺寸/来源要跟着改（否则目录表会报旧账）
    _sync_index_after_upload(run_id, rel, len(data))
    return {**res, "bytes": len(data)}


def _sync_index_after_upload(run_id: str, rel: str, size: int) -> None:
    idx = cs.load_image_index(storage, run_id)
    touched = False
    for key, entry in list(idx.items()):
        if not isinstance(entry, dict):
            continue
        changed = False
        frames = entry.get("frames") if isinstance(entry.get("frames"), dict) else {}
        for role, item in frames.items():
            if isinstance(item, dict) and item.get("path") == rel:
                item.update({"size": size, "provider": "人工上传", "source": "upload", "is_placeholder": False})
                changed = True
        if entry.get("path") == rel:
            entry.update({"size": size, "provider": "人工上传", "source": "upload", "is_placeholder": False})
            changed = True
        if changed:
            idx[key] = entry
            touched = True
    if touched:
        cs.save_image_index(storage, run_id, idx)


@router.post("/{run_id}/artifact/restore")
def restore_file(run_id: str, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    _require_run(db, run_id)
    rel = _resolve_visible(run_id, str(payload.get("path") or ""))
    try:
        res = cs.restore_artifact(storage, run_id, rel)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    bus.emit(
        db, run_id, EventType.AGENT_THOUGHT,
        f"已用备份原件还原 {rel}",
        stage_id=str(payload.get("stage_id") or "") or None,
        payload={"human_edit": "artifact_restore", "path": rel},
    )
    return res
