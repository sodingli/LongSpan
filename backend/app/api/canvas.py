"""画布接口：让"看得见"变成"改得动"。

四类能力：
  1. 提示词编辑 —— 改分镜每格的画面描述，或整体改角色卡与一致性锚点；
  2. 单格重生 —— 只重画第 N 格（其余不动），可以指定用哪个生图模型；
  3. 上传替换 —— 人工画好/挑好的图直接盖掉某一格；
  4. 阶段级模型 —— 这个阶段单独用哪个模型，不动全局配置。

所有改动都会在 `canvas_edits.json` 留痕，并在首次改动前自动备份模型原件。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import canvas_store as cs
from ..config import settings
from ..db import get_db
from ..events import EventType, bus
from ..models import Run
from ..store import storage

logger = logging.getLogger("longspan.canvas.api")

router = APIRouter(prefix="/api/runs", tags=["canvas"])

_DATA_URL_RE = re.compile(r"^data:image/([a-zA-Z0-9.+-]+);base64,(.*)$", re.DOTALL)
_VIDEO_DATA_URL_RE = re.compile(r"^data:video/([a-zA-Z0-9.+-]+);base64,(.*)$", re.DOTALL)
_EXT_BY_MIME = {"png": ".png", "jpeg": ".jpg", "jpg": ".jpg", "webp": ".webp"}
_VIDEO_EXT_BY_MIME = {"mp4": ".mp4", "quicktime": ".mov", "webm": ".webm"}

# 可编辑的文本字段（分镜单格）
_EDITABLE_PANEL_FIELDS = (
    "prompt", "scene", "subtitle", "speaker", "motion", "duration_seconds", "ratio",
    "video_prompt", "frame_prompts",
)

# 中间帧 / 尾帧没有独立提示词时，从首帧提示词派生。
# 这不是"编一句更好听的"，而是把「同一场景、同一角色、动作推进」这三个
# 多帧一致性的必要条件写死，避免生成出三个互不相干的场景。
FRAME_PROMPT_SUFFIX = {
    "first": "",
    "mid": "。同一场景、同一角色、同一机位，动作进行到一半的瞬间（承接首帧，人物姿态有推进，不要换场景、不要换服装）",
    "last": "。同一场景、同一角色、同一机位，动作结束时的定格（承接首帧，人物姿态已完成，不要换场景、不要换服装）",
}

# 后台视频任务要留引用，否则可能被 GC 掉（asyncio 只持弱引用）
_VIDEO_JOBS: set[asyncio.Task] = set()


def _require_run(db: Session, run_id: str) -> Run:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return run


def _decode_image(payload: dict) -> tuple[bytes, str]:
    """从 data URL 解出图片字节与扩展名。只接受图片，别的一律拒绝。"""
    raw = str(payload.get("data_url") or payload.get("data") or "")
    m = _DATA_URL_RE.match(raw.strip())
    if not m:
        raise HTTPException(status_code=400, detail="只接受 data:image/...;base64, 格式的图片数据")
    mime, b64 = m.group(1).lower(), m.group(2)
    ext = _EXT_BY_MIME.get(mime)
    if not ext:
        raise HTTPException(status_code=400, detail=f"不支持的图片格式：{mime}（支持 png / jpeg / webp）")
    try:
        data = base64.b64decode(b64)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"图片数据解析失败：{exc}") from exc
    if not data:
        raise HTTPException(status_code=400, detail="图片内容为空")
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="图片超过 12MB，请压缩后再上传")
    return data, ext


def _decode_video(payload: dict) -> tuple[bytes, str]:
    raw = str(payload.get("data_url") or payload.get("data") or "")
    m = _VIDEO_DATA_URL_RE.match(raw.strip())
    if not m:
        raise HTTPException(status_code=400, detail="只接受 data:video/...;base64, 格式的视频数据")
    mime, b64 = m.group(1).lower(), m.group(2)
    ext = _VIDEO_EXT_BY_MIME.get(mime) or ".mp4"
    try:
        data = base64.b64decode(b64)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"视频数据解析失败：{exc}") from exc
    if not data:
        raise HTTPException(status_code=400, detail="视频内容为空")
    if len(data) > 80 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="视频超过 80MB，请压缩后再上传")
    return data, ext


def _panel_key_of(p: dict, fallback: int) -> str:
    """格号取值与 harness.tools._item_index 保持同一套兜底。

    血泪教训：模型的 panels.json 里格号字段名并不统一 —— 实测既有 `index`，
    也有只写 `id` 的（`{"id":1,"duration":4,...}`）。这里只认 `index` 的话，
    画布上看得见第 1 格、却"上传图片 404 说没有第 1 格"，是最难查的那种不一致。
    """
    for field in ("index", "panel", "no", "id"):
        val = p.get(field)
        if val is None or val == "":
            continue
        try:
            return str(int(val))
        except (TypeError, ValueError):
            return str(val)
    return str(fallback)


def _find_panel(doc: dict, index: str) -> tuple[str, dict]:
    panels = doc.get("panels") or []
    for i, p in enumerate(panels, 1):
        if not isinstance(p, dict):
            continue
        key = _panel_key_of(p, i)
        if key == str(index):
            return key, p
    if panels:
        raise HTTPException(status_code=404, detail=f"分镜清单里没有第 {index} 格")
    return str(index), {}


def _panel_index(doc: dict, index: str) -> int:
    """校验格号存在，返回用于文件命名的整数编号。"""
    key, _panel = _find_panel(doc, index)
    try:
        return int(str(key))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"格号必须是整数：{index}") from exc


def _entry_for(images: dict, index: str) -> dict:
    entry = images.get(str(index)) or {}
    return entry if isinstance(entry, dict) else {}


def _frames_view(entry: dict) -> dict:
    """给前端的多帧视图：每个帧位都返回（没图就是 null），前端不用自己补空。"""
    frames = cs.frames_of(entry)
    out: dict[str, dict] = {}
    for role in cs.FRAME_ROLES:
        item = frames.get(role)
        out[role] = (
            {
                "path": item.get("path"),
                "provider": item.get("provider") or "",
                "source": item.get("source") or ("placeholder" if item.get("is_placeholder") else "model"),
                "size": item.get("size") or 0,
                "is_placeholder": bool(item.get("is_placeholder")),
                "note": item.get("note") or "",
            }
            if item
            else None
        )
    return out


# --------------------------------------------------------------------- 读取


@router.get("/{run_id}/canvas")
def get_canvas(run_id: str, db: Session = Depends(get_db)) -> dict:
    """画布编辑所需的全量数据 —— 一次拿齐，避免画布上点一下转三圈。"""
    run = _require_run(db, run_id)
    doc = cs.load_panels(storage, run_id)
    images = cs.load_image_index(storage, run_id)

    panels: list[dict] = []
    for i, p in enumerate(doc.get("panels") or [], 1):
        if not isinstance(p, dict):
            continue
        key = str(p.get("index", i))
        entry = _entry_for(images, key)
        img = entry or {}
        frames = _frames_view(entry)
        panels.append(
            {
                **p,
                "index": p.get("index", i),
                "image": img.get("path") or "",
                "image_provider": img.get("provider") or "",
                "image_source": img.get("source") or ("placeholder" if img.get("is_placeholder") else "model"),
                "image_bytes": img.get("size") or 0,
                "is_placeholder": bool(img.get("is_placeholder")),
                "frames": frames,
                "frame_count": sum(1 for v in frames.values() if v),
                "video": cs.video_state(entry),
                "video_status": (img.get("video") or {}).get("status") or "",
                "reference_video": (img.get("reference_video") or {}).get("path")
                or (img.get("reference_video") or {}).get("url")
                or "",
            }
        )

    from ..media.video import model_options as video_model_options, video_ready

    v_ok, v_why = video_ready()
    return {
        "run_id": run_id,
        "skill_id": run.skill_id,
        "panels": panels,
        "panel_count": len(panels),
        "panels_human_edited": bool(doc.get("human_edited")),
        "characters": cs.characters_state(storage, run_id),
        "overrides": cs.load_overrides(storage, run_id),
        "edits": cs.count_edits(storage, run_id),
        "frame_roles": [{"id": r, "label": cs.FRAME_LABEL[r]} for r in cs.FRAME_ROLES],
        "video": {
            "ready": v_ok,
            "reason": v_why,
            "model": settings.video_model if v_ok else "",
            "models": [e["name"] for e in video_model_options()] if v_ok else [],
            "durations": [5, 10],
            "default_duration": settings.video_duration,
            "mode": settings.video_mode,
        },
    }


@router.get("/{run_id}/canvas/models")
async def canvas_models(run_id: str, db: Session = Depends(get_db)) -> dict:
    """阶段级模型覆盖的可选项。LLM 从接口拉，生图/配音来自当前配置。"""
    _require_run(db, run_id)
    from ..config import settings
    from ..llm.providers import list_models
    from ..media.video import model_options as video_model_options

    llm_options: list[str] = []
    llm_error = ""
    try:
        res = await list_models(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
        if res.get("ok"):
            llm_options = [str(x) for x in (res.get("models") or [])]
        else:
            llm_error = str(res.get("error") or "")
    except Exception as exc:  # noqa: BLE001
        llm_error = str(exc)[:200]

    return {
        "llm": {
            "current": settings.llm_model,
            "base_url": settings.llm_base_url,
            "options": llm_options,
            "error": llm_error,
            "note": "留空 = 用全局模型",
        },
        "image": {
            "current": settings.image_model,
            "service": settings.image_service,
            "provider": settings.image_provider,
            "options": sorted({settings.image_model} - {""}) if settings.image_model else [],
            "note": "生图模型名来自服务商预置服务标识，可手动填写",
        },
        "tts": {
            "current": "system" if not settings.tts_api_key else settings.tts_voice,
            "options": sorted({settings.tts_voice} - {""}) if settings.tts_voice else [],
            "note": "留空 = 用全局配音配置",
        },
        "video": {
            "current": settings.video_model,
            "options": [
                {"value": e["name"], "label": e["note"] or e["name"], "params": e.get("params") or {}}
                for e in video_model_options()
            ],
            "note": "留空 = 用全局视频模型；可选清单在「配置 → 视频」里维护",
        },
    }


# --------------------------------------------------------------------- 提示词


@router.put("/{run_id}/panels")
def update_panels(run_id: str, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    """保存分镜提示词编辑。按 index 匹配，只改传上来的字段。"""
    _require_run(db, run_id)
    incoming = payload.get("panels")
    if not isinstance(incoming, list):
        raise HTTPException(status_code=400, detail="panels 必须是数组")

    doc = cs.load_panels(storage, run_id)
    existing = doc.get("panels") or []
    if not existing:
        raise HTTPException(status_code=409, detail="本任务还没有 panels.json，先在分镜阶段跑出分镜清单再编辑")

    by_key: dict[str, dict] = {}
    for i, p in enumerate(existing, 1):
        if isinstance(p, dict):
            by_key[_panel_key_of(p, i)] = p

    edited: list[str] = []
    unknown: list[str] = []
    for item in incoming:
        if not isinstance(item, dict):
            continue
        key = str(item.get("index"))
        target = by_key.get(key)
        if target is None:
            unknown.append(key)
            continue
        for field in _EDITABLE_PANEL_FIELDS:
            if field not in item:
                continue
            val = item[field]
            if field == "duration_seconds":
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    continue
            elif field == "index":
                continue
            else:
                val = str(val)
            if target.get(field) != val:
                target[field] = val
                if key not in edited:
                    edited.append(key)

    if not edited:
        if unknown:
            raise HTTPException(
                status_code=404,
                detail=f"分镜清单里没有这些格：{', '.join(sorted(unknown))}（当前共 {len(existing)} 格）",
            )
        return {"ok": True, "edited": [], "note": "没有检测到改动"}

    res = cs.save_panels(storage, run_id, existing, edited)
    bus.emit(
        db, run_id, EventType.AGENT_THOUGHT,
        f"人工修改了 {len(edited)} 格分镜提示词（第 {', '.join(sorted(edited, key=lambda x: int(x) if x.isdigit() else 0))} 格）",
        stage_id="storyboard",
        payload={"human_edit": "panels", "keys": edited, "backed_up_origin": res.get("backed_up_origin")},
    )
    return {**res, "edited": edited, "unknown": unknown}


# --------------------------------------------------------------------- 单格重生


@router.post("/{run_id}/panels/{index}/regenerate")
async def regenerate_panel(
    run_id: str, index: str, payload: dict = Body(default={}), db: Session = Depends(get_db)
) -> dict:
    """只重画第 N 格。可临时指定 prompt / 生图模型，不改全局配置。"""
    run = _require_run(db, run_id)
    stage_id = str(payload.get("stage_id") or "storyboard")

    doc = cs.load_panels(storage, run_id)
    panels = doc.get("panels") or []
    target = None
    for p in panels:
        if isinstance(p, dict) and str(p.get("index")) == str(index):
            target = p
            break
    if target is None:
        raise HTTPException(status_code=404, detail=f"分镜清单里没有第 {index} 格")

    prompt = str(payload.get("prompt") or target.get("prompt") or target.get("scene") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail=f"第 {index} 格没有画面提示词，请先填写")

    ratio = str(payload.get("ratio") or target.get("ratio") or "9:16")
    eff = cs.effective_image_config(storage, run_id, stage_id)
    model = str(payload.get("image_model") or "").strip() or eff["model"]
    service = str(payload.get("image_service") or "").strip() or eff["service"]

    # 把提示词改动先落盘（用户可能改了 prompt 直接点重新生成）
    if prompt != str(target.get("prompt") or ""):
        target["prompt"] = prompt
        cs.save_panels(storage, run_id, panels, [str(index)])

    from ..media.image import COMIC_STYLE_SKILLS, attach_note, ensure_style_anchor, generate_image

    # FR-07（2026-10-22）：重绘也走「参考图」—— 手改一格，脸不再变。
    # 复用管线同一套三级查找（标签文件名 → 锚点内容匹配 → 任意定妆图兜底），
    # 保证画布重绘与自动生成拿到的是同一张定妆图（同一张源头，风格才对等）。
    ref_path: str | None = None
    ref_role: str | None = None
    ref_notes: list[str] = []
    try:
        from types import SimpleNamespace

        from ..harness.tools import _find_reference_art

        ref_path, ref_role, ref_notes = _find_reference_art(
            SimpleNamespace(storage=storage, run_id=run_id), prompt
        )
    except Exception as exc:  # noqa: BLE001 —— 参考图是增强，找不到就退回纯文生图
        ref_notes.append(f"参考图查找失败（退回纯文生图）：{exc}")

    # 锚点标签是机器路标不是画面内容（管线同款处理）：送生图前剥掉。
    # 注意 panels.json 里落盘的仍是原文（管线重跑还要靠它匹配锚点）。
    from ..harness.tools import _apply_empty_shot_guard, _strip_anchor_tags

    send_prompt = _strip_anchor_tags(prompt) or prompt

    # 空镜守卫（与管线同款，且必须在风格锚之前）：手改一格空镜同样会被凭空补出
    # 人物；而风格锚会补 "anime face with large expressive eyes"，一句话就让空镜
    # 判定失效（先注入声明，风格锚才会走"无脸条款"）。
    send_prompt, empty_note = _apply_empty_shot_guard(send_prompt)

    # 风格锚校验（FR-04 补充）：漫剧类任务的提示词缺「反写实 / 角色风格化」条款，
    # 在生图入口补齐并留痕 —— 不让写实漂移从画布重绘这条路溜进来。
    style_note = None
    if run.skill_id in COMIC_STYLE_SKILLS:
        send_prompt, style_note = ensure_style_anchor(send_prompt)
    notes = [n for n in (style_note, empty_note) if n] + list(ref_notes)
    style_note = "；".join(notes) or None
    bus.emit(
        db, run_id, EventType.TOOL_CALL,
        f"重画第 {index} 格（模型 {model or '默认'}"
        + (f"，参考图 {ref_role}" if ref_path else "，无参考图") + "）",
        stage_id=stage_id,
        payload={"human_edit": "regenerate", "index": index, "model": model or "", "service": service or ""},
    )

    out = await generate_image(
        storage, run_id, send_prompt, f"panels/panel-{int(index):02d}.svg", ratio,
        model_override=model or None, service_override=service or None,
        subject_reference=ref_path,
    )
    out["index"] = str(index)
    out["source"] = "model"
    attach_note(out, style_note or "")

    idx = cs.load_image_index(storage, run_id)
    # **合并**而不是整体覆盖（2026-10-22 修）：旧代码 `idx[index] = out` 会把该格已有的
    # 中/尾帧与视频片段一起静默抹掉 —— 用户重画一张首帧，另外两帧和视频凭空消失，
    # 成片里那格变回静态图，且没有任何提示。
    entry = dict(idx.get(str(index)) or {})
    frames = dict(entry.get("frames") or {}) if isinstance(entry.get("frames"), dict) else {}
    frames["first"] = out
    entry.update(out)
    entry["frames"] = frames
    # 首帧变了 → 该格旧视频片段的首帧已过期，必须作废（否则成片还是旧画面）
    stale_video = isinstance(entry.get("video"), dict)
    if stale_video:
        entry.pop("video", None)
    idx[str(index)] = entry
    cs.save_image_index(storage, run_id, idx)
    cs.record_edit(
        storage, run_id, "regenerate",
        {"index": str(index), "model": model or "", "video_invalidated": stale_video},
    )

    ok = not out.get("is_placeholder")
    bus.emit(
        db, run_id,
        EventType.ARTIFACT_CREATED if ok else EventType.AGENT_THOUGHT,
        f"第 {index} 格已重画：{out.get('path')}" if ok else f"第 {index} 格重画降级为占位图：{out.get('fallback_reason', '')[:120]}",
        stage_id=stage_id,
        payload={
            "human_edit": "regenerate", "index": index, "path": out.get("path"),
            "degraded": not ok, "video_invalidated": stale_video,
        },
    )
    result = {"ok": ok, "panel": out, "model": model, "service": service}
    if stale_video:
        result["note"] = "该格视频片段已作废（首帧已变），需要重新生成视频"
    return result


# --------------------------------------------------------------------- 上传替换


@router.post("/{run_id}/panels/{index}/image")
def upload_panel_image(
    run_id: str, index: str, payload: dict = Body(...), db: Session = Depends(get_db)
) -> dict:
    """人工上传的图直接盖掉第 N 格 —— 并标注为「人工上传」，不冒充模型产物。"""
    _require_run(db, run_id)
    doc = cs.load_panels(storage, run_id)
    keys = {str(p.get("index")) for p in (doc.get("panels") or []) if isinstance(p, dict)}
    if keys and str(index) not in keys:
        raise HTTPException(status_code=404, detail=f"分镜清单里没有第 {index} 格")

    data, ext = _decode_image(payload)
    rel = f"panels/panel-{int(index):02d}{ext}"
    saved = cs.save_uploaded_image(storage, run_id, rel, data, note=str(payload.get("note") or ""))

    idx = cs.load_image_index(storage, run_id)
    idx[str(index)] = {
        "path": saved["path"],
        "size": saved["size"],
        "provider": "人工上传",
        "is_placeholder": False,
        "source": "upload",
        "index": str(index),
        "note": str(payload.get("note") or ""),
    }
    cs.save_image_index(storage, run_id, idx)

    bus.emit(
        db, run_id, EventType.ARTIFACT_CREATED,
        f"第 {index} 格已由人工上传替换：{saved['path']}",
        stage_id="storyboard",
        payload={"human_edit": "upload", "index": str(index), "path": saved["path"], "bytes": saved["size"]},
    )
    return {"ok": True, **saved, "index": str(index)}


# --------------------------------------------------------------------- 角色卡


@router.put("/{run_id}/characters")
def update_characters(run_id: str, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    _require_run(db, run_id)
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=400, detail="角色卡内容不能为空")
    res = cs.save_characters(storage, run_id, text)
    bus.emit(
        db, run_id, EventType.AGENT_THOUGHT,
        f"人工修改了角色卡与一致性锚点（{res['chars']} 字）—— 下一轮生图会采用新锚点",
        stage_id="characters",
        payload={"human_edit": "characters", "chars": res["chars"], "backed_up_origin": res.get("backed_up_origin")},
    )
    return res


@router.get("/{run_id}/characters/origin")
def get_characters_origin(run_id: str, db: Session = Depends(get_db)) -> dict:
    """模型原始版本 —— 人改完之后还能对照，也可以一键回退。"""
    _require_run(db, run_id)
    text = cs.read_text(storage, run_id, cs.CHARACTERS_ORIGIN_REL, "")
    if not text:
        raise HTTPException(status_code=404, detail="没有备份的原件（说明还没被人工改过）")
    return {"text": text}


# --------------------------------------------------------------------- 阶段覆盖


@router.put("/{run_id}/stages/{stage_id}/overrides")
def set_stage_override(
    run_id: str, stage_id: str, payload: dict = Body(...), db: Session = Depends(get_db)
) -> dict:
    """阶段级模型覆盖。空字符串 = 清除该槽位、回到全局配置。"""
    _require_run(db, run_id)
    res = cs.save_stage_override(storage, run_id, stage_id, payload or {})
    if res.get("changed"):
        bus.emit(
            db, run_id, EventType.AGENT_THOUGHT,
            f"阶段「{stage_id}」的模型已单独指定：{', '.join(res['changed'])}",
            stage_id=stage_id,
            payload={"human_edit": "stage_override", **res},
        )
    return res


# --------------------------------------------------------------------- 参考资料


@router.post("/{run_id}/stages/{stage_id}/refs")
def upload_stage_ref(
    run_id: str, stage_id: str, payload: dict = Body(...), db: Session = Depends(get_db)
) -> dict:
    """给某个阶段上传参考图（风格参考 / 角色参考）。存到 refs/<stage_id>/ 下。"""
    _require_run(db, run_id)
    data, ext = _decode_image(payload)
    existing = [
        f for f in storage.list_files(run_id)
        if f.startswith(f"refs/{stage_id}/")
    ]
    rel = f"refs/{stage_id}/ref-{len(existing) + 1:02d}{ext}"
    saved = cs.save_uploaded_image(storage, run_id, rel, data, note=str(payload.get("note") or ""))
    bus.emit(
        db, run_id, EventType.ARTIFACT_CREATED,
        f"为阶段「{stage_id}」上传了参考图：{saved['path']}",
        stage_id=stage_id,
        payload={"human_edit": "ref_upload", "path": saved["path"]},
    )
    return {"ok": True, **saved, "stage_id": stage_id}


# --------------------------------------------------------------------- 多帧（首 / 中 / 尾）


@router.post("/{run_id}/panels/{index}/frames/{role}")
def upload_frame(
    run_id: str, index: str, role: str, payload: dict = Body(...), db: Session = Depends(get_db)
) -> dict:
    """人工上传某一帧（首帧 / 中间帧 / 尾帧），并标注为「人工上传」。"""
    _require_run(db, run_id)
    if role not in cs.FRAME_ROLES:
        raise HTTPException(status_code=400, detail=f"帧位只能是 {' / '.join(cs.FRAME_ROLES)}")
    doc = cs.load_panels(storage, run_id)
    n = _panel_index(doc, index)

    data, ext = _decode_image(payload)
    rel = f"panels/panel-{n:02d}-{role}{ext}"
    saved = cs.save_uploaded_image(
        storage, run_id, rel, data, note=str(payload.get("note") or f"人工上传{cs.FRAME_LABEL[role]}")
    )
    res = cs.set_frame(
        storage, run_id, index, role,
        {
            "path": saved["path"], "size": saved["size"], "provider": "人工上传",
            "is_placeholder": False, "source": "upload",
            "note": str(payload.get("note") or ""),
        },
    )
    cs.record_edit(storage, run_id, "frame_upload", {"index": str(index), "role": role, "path": saved["path"]})
    bus.emit(
        db, run_id, EventType.ARTIFACT_CREATED,
        f"第 {index} 格{cs.FRAME_LABEL[role]}已由人工上传：{saved['path']}",
        stage_id="storyboard",
        payload={"human_edit": "frame_upload", "index": str(index), "role": role, "path": saved["path"]},
    )
    return {"ok": True, "index": str(index), "role": role, **saved, "frames": res["frames"]}


@router.post("/{run_id}/panels/{index}/frames/{role}/regenerate")
async def regenerate_frame(
    run_id: str, index: str, role: str, payload: dict = Body(default={}), db: Session = Depends(get_db)
) -> dict:
    """生成某一帧。

    三帧共用同一段首帧提示词（保证是同一个场景同一个角色），
    差异只来自追加的「动作时刻」描述 —— 这是多帧一致性的最低成本做法。
    用户也可以显式给 `prompt` 完全覆盖。
    """
    run = _require_run(db, run_id)
    if role not in cs.FRAME_ROLES:
        raise HTTPException(status_code=400, detail=f"帧位只能是 {' / '.join(cs.FRAME_ROLES)}")
    stage_id = str(payload.get("stage_id") or "storyboard")

    doc = cs.load_panels(storage, run_id)
    n = _panel_index(doc, index)
    _key, target = _find_panel(doc, index)
    base_prompt = str(payload.get("prompt") or target.get("prompt") or target.get("scene") or "").strip()
    if not base_prompt:
        raise HTTPException(status_code=400, detail=f"第 {index} 格没有画面提示词，请先填写")

    frame_prompts = target.get("frame_prompts") if isinstance(target.get("frame_prompts"), dict) else {}
    is_derived = not str(frame_prompts.get(role) or "").strip()  # 是否由首帧提示词派生（风格补丁前判定）
    prompt = str(frame_prompts.get(role) or "").strip() or (base_prompt + FRAME_PROMPT_SUFFIX[role])
    ratio = str(payload.get("ratio") or target.get("ratio") or "9:16")

    eff = cs.effective_image_config(storage, run_id, stage_id)
    model = str(payload.get("image_model") or "").strip() or eff["model"]
    service = str(payload.get("image_service") or "").strip() or eff["service"]

    from ..media.image import COMIC_STYLE_SKILLS, attach_note, ensure_style_anchor, generate_image

    # 风格锚校验（FR-04 补充）：同 regenerate_panel，缺条款自动补齐并留痕。
    style_note = None
    if run.skill_id in COMIC_STYLE_SKILLS:
        prompt, style_note = ensure_style_anchor(prompt)

    bus.emit(
        db, run_id, EventType.TOOL_CALL,
        f"生成第 {index} 格{cs.FRAME_LABEL[role]}（模型 {model or '默认'}）",
        stage_id=stage_id,
        payload={"human_edit": "frame_regenerate", "index": str(index), "role": role, "model": model or ""},
    )

    suffix = "" if role == "first" else f"-{role}"
    out = await generate_image(
        storage, run_id, prompt, f"panels/panel-{n:02d}{suffix}.svg", ratio,
        model_override=model or None, service_override=service or None,
    )
    out["index"] = str(index)
    out["source"] = "model"
    out["note"] = "由首帧提示词派生的动作时刻描述" if is_derived else ""
    attach_note(out, style_note or "")
    cs.set_frame(storage, run_id, index, role, out)
    cs.record_edit(storage, run_id, "frame_regenerate", {"index": str(index), "role": role, "model": model or ""})

    ok = not out.get("is_placeholder")
    bus.emit(
        db, run_id,
        EventType.ARTIFACT_CREATED if ok else EventType.AGENT_THOUGHT,
        f"第 {index} 格{cs.FRAME_LABEL[role]}已生成：{out.get('path')}" if ok
        else f"第 {index} 格{cs.FRAME_LABEL[role]}降级为占位图：{out.get('fallback_reason', '')[:120]}",
        stage_id=stage_id,
        payload={"human_edit": "frame_regenerate", "index": str(index), "role": role, "degraded": not ok},
    )
    return {"ok": ok, "index": str(index), "role": role, "frame": out, "model": model, "service": service}


@router.delete("/{run_id}/panels/{index}/frames/{role}")
def delete_frame(run_id: str, index: str, role: str, db: Session = Depends(get_db)) -> dict:
    _require_run(db, run_id)
    if role not in cs.FRAME_ROLES:
        raise HTTPException(status_code=400, detail=f"帧位只能是 {' / '.join(cs.FRAME_ROLES)}")
    cs.drop_frame(storage, run_id, index, role)
    cs.record_edit(storage, run_id, "frame_delete", {"index": str(index), "role": role})
    bus.emit(
        db, run_id, EventType.AGENT_THOUGHT,
        f"已删除第 {index} 格的{cs.FRAME_LABEL[role]}",
        stage_id="storyboard",
        payload={"human_edit": "frame_delete", "index": str(index), "role": role},
    )
    return {"ok": True, "index": str(index), "role": role}


# --------------------------------------------------------------------- 参考视频 + 视频片段


@router.post("/{run_id}/panels/{index}/reference-video")
def upload_reference_video(
    run_id: str, index: str, payload: dict = Body(...), db: Session = Depends(get_db)
) -> dict:
    """给第 N 格设置参考视频（动作 / 构图参考）。

    两种来源：本地上传（走内联 base64，上限 6MB），或直接给一个公网 URL。
    之所以要留 URL 这条路：一段 5 秒 720p 的参考视频通常就超过内联上限，
    只给上传等于把这个功能做成了摆设。
    """
    _require_run(db, run_id)
    doc = cs.load_panels(storage, run_id)
    n = _panel_index(doc, index)

    url = str(payload.get("url") or "").strip()
    if url:
        if not re.match(r"^https?://", url):
            raise HTTPException(status_code=400, detail="参考视频 URL 必须是 http(s) 地址")
        ref_value = {"url": url, "path": "", "size": 0, "at": datetime.now(timezone.utc).isoformat()}
        shown = url
        saved = {"path": "", "size": 0}
    else:
        data, ext = _decode_video(payload)
        rel = f"refs/panels/panel-{n:02d}-reference{ext}"
        saved = cs.save_uploaded_image(storage, run_id, rel, data, note="参考视频")
        ref_value = {
            "path": saved["path"], "size": saved["size"], "at": datetime.now(timezone.utc).isoformat()
        }
        shown = saved["path"]

    idx = cs.load_image_index(storage, run_id)
    entry = dict(idx.get(str(index)) or {})
    entry["reference_video"] = ref_value
    idx[str(index)] = entry
    cs.save_image_index(storage, run_id, idx)
    cs.record_edit(storage, run_id, "reference_video", {"index": str(index), "ref": shown})

    bus.emit(
        db, run_id, EventType.ARTIFACT_CREATED,
        f"第 {index} 格已设置参考视频：{shown}"
        + (f"（{saved['size'] / 1024 / 1024:.1f}MB）" if saved["size"] else "（公网地址）"),
        stage_id="storyboard",
        payload={"human_edit": "reference_video", "index": str(index), "ref": shown},
    )
    return {"ok": True, "index": str(index), **saved, "reference_video": shown}


@router.delete("/{run_id}/panels/{index}/reference-video")
def delete_reference_video(run_id: str, index: str, db: Session = Depends(get_db)) -> dict:
    _require_run(db, run_id)
    idx = cs.load_image_index(storage, run_id)
    entry = dict(idx.get(str(index)) or {})
    entry.pop("reference_video", None)
    idx[str(index)] = entry
    cs.save_image_index(storage, run_id, idx)
    return {"ok": True}


async def _video_job(
    run_id: str, index: str, out_rel: str, prompt: str, duration: int, mode: str, model: str | None = None
) -> None:
    """后台跑一个视频片段任务：提交 → 轮询 → 下载 → 记账。

    为什么放后台：一次生成要几分钟。同步等会把 HTTP 连接挂住，
    用户刷新页面就看不到进度了。状态写在 media/images.json 里，
    所以刷新后依然能看到"生成中 / 成功 / 失败＋原因"。
    """
    from ..db import SessionLocal
    from ..media.video import generate_clip

    workspace = storage.run_workspace(run_id)
    idx = cs.load_image_index(storage, run_id)
    entry = dict(idx.get(index) or {})
    frames = cs.frames_of(entry)
    first = frames.get("first") or {}
    last = frames.get("last") or {}
    mid = frames.get("mid") or {}
    ref_entry = entry.get("reference_video") or {}
    ref = ref_entry.get("path") or ref_entry.get("url")

    try:
        out = await generate_clip(
            workspace,
            prompt=prompt,
            first_frame=first.get("path"),
            last_frame=last.get("path"),
            mid_frame=mid.get("path"),
            reference_video=ref,
            duration=duration,
            mode=mode,
            out_rel=out_rel,
            model=model,
        )
        out.update(
            {
                "status": "ready",
                "prompt": prompt,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        cs.set_panel_video(storage, run_id, index, out)
        cs.record_edit(
            storage, run_id, "video",
            {
                "index": index,
                "path": out["path"],
                "task_id": out["task_id"],
                "frames": out.get("frames") or [],
                "reference": out.get("reference") or "",
                "dropped_frames": out.get("dropped_frames") or [],
            },
        )
        msg = f"第 {index} 格视频片段已生成：{out['path']}（{out['size'] / 1024 / 1024:.1f}MB）"
        if out.get("note"):
            # 中间帧被丢下这件事必须说出来，不能让人以为三帧都用上了
            msg += f" · {out['note']}"
        ev = EventType.ARTIFACT_CREATED
    except Exception as exc:  # noqa: BLE001
        logger.warning("视频生成失败 run=%s index=%s: %s", run_id, index, exc)
        cs.set_panel_video(
            storage, run_id, index,
            {
                "status": "failed", "error": str(exc)[:600], "prompt": prompt,
                # 保留提交时的参数，失败重试和排查都要用（之前整条覆盖丢了 model）
                "model": model or settings.video_model,
                "duration": duration, "mode": mode,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        msg = f"第 {index} 格视频生成失败：{str(exc)[:300]}"
        ev = EventType.RUN_ERROR

    db = SessionLocal()
    try:
        bus.emit(db, run_id, ev, msg, stage_id="compose", payload={"human_edit": "video", "index": index})
        db.commit()
    finally:
        db.close()


@router.post("/{run_id}/panels/{index}/video")
async def generate_panel_video(
    run_id: str, index: str, payload: dict = Body(default={}), db: Session = Depends(get_db)
) -> dict:
    """以第 N 格首帧（+尾帧/参考视频）生成一段视频。异步执行，返回任务状态。"""
    _require_run(db, run_id)
    from ..media.video import VideoConfigError, video_ready

    ok, why = video_ready()
    if not ok:
        raise HTTPException(status_code=409, detail=f"视频生成不可用：{why}")

    doc = cs.load_panels(storage, run_id)
    n = _panel_index(doc, index)
    _key, target = _find_panel(doc, index)
    idx = cs.load_image_index(storage, run_id)
    entry = _entry_for(idx, index)
    frames = cs.frames_of(entry)
    if not frames.get("first"):
        raise HTTPException(status_code=409, detail=f"第 {index} 格还没有首帧，先生成或上传一张")

    if (entry.get("video") or {}).get("status") == "running":
        raise HTTPException(status_code=409, detail=f"第 {index} 格已有视频任务在跑，等它结束再提交")

    prompt = str(
        payload.get("prompt") or target.get("video_prompt") or target.get("prompt") or target.get("scene") or ""
    ).strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="缺少视频提示词")

    try:
        duration = int(payload.get("duration") or settings.video_duration or 5)
        mode = str(payload.get("mode") or settings.video_mode or "std").lower()
        # 模型：留空 = 全局主模型；可指定 models_json 清单里的任一模型
        from ..media.video import model_options as video_model_options, build_payload

        chosen_model = str(payload.get("model") or "").strip()
        if chosen_model and chosen_model not in {e["name"] for e in video_model_options()}:
            known = "、".join(e["name"] for e in video_model_options())
            raise HTTPException(
                status_code=400,
                detail=f"未知视频模型 {chosen_model}（可选：{known}；新增模型请在配置面板的「可选模型清单」里登记）",
            )
        # 用 build_payload 做一次只校验不请求的预检：非法参数在花钱之前就要拦住
        build_payload(prompt=prompt, duration=duration, mode=mode, first_frame="https://placeholder.local/a.jpg", model=chosen_model or None)
    except VideoConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    out_rel = f"video/clips/panel-{n:02d}.mp4"
    cs.set_panel_video(
        storage, run_id, index,
        {
            "status": "running", "prompt": prompt, "duration": duration, "mode": mode,
            "model": chosen_model or settings.video_model,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "reference": (entry.get("reference_video") or {}).get("path") or "",
            "frames": [r for r in cs.FRAME_ROLES if frames.get(r)],
        },
    )
    cs.record_edit(storage, run_id, "video_request", {"index": str(index), "duration": duration, "mode": mode, "model": chosen_model or settings.video_model})

    bus.emit(
        db, run_id, EventType.TOOL_CALL,
        f"提交第 {index} 格视频生成（{duration}s / {mode}，首帧+{len([r for r in cs.FRAME_ROLES if frames.get(r)]) - 1} 帧）",
        stage_id="compose",
        payload={"human_edit": "video", "index": str(index), "duration": duration, "mode": mode},
    )

    task = asyncio.create_task(_video_job(run_id, str(index), out_rel, prompt, duration, mode, chosen_model or None))
    _VIDEO_JOBS.add(task)
    task.add_done_callback(_VIDEO_JOBS.discard)

    return {
        "ok": True,
        "index": str(index),
        "status": "running",
        "duration": duration,
        "mode": mode,
        "model": chosen_model or settings.video_model,
        "out": out_rel,
        "message": "已提交后台任务：几分钟后回来看这一格的视频状态（刷新不丢）",
    }


@router.delete("/{run_id}/panels/{index}/video")
def delete_panel_video(run_id: str, index: str, db: Session = Depends(get_db)) -> dict:
    _require_run(db, run_id)
    cs.set_panel_video(storage, run_id, index, None)
    cs.record_edit(storage, run_id, "video_delete", {"index": str(index)})
    return {"ok": True, "index": str(index)}
