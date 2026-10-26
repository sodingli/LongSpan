"""角色库接口：资产层的读写入口。

产品语义：
    「角色」属于工作台，不属于某一次任务。这里提供角色的列表、详情、改名/换图、
    以及把角色复用到某一集的入口。复用是显式动作（用户选），不是自动猜测 ——
    自动猜错等于换脸，代价比多一次点击高得多。
"""

from __future__ import annotations

import shutil

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import characters as lib
from ..db import get_db
from ..models import Character, Run
from ..store import storage

router = APIRouter(prefix="/api/characters", tags=["characters"])


@router.get("")
def list_characters(skill_id: str | None = None, db: Session = Depends(get_db)):
    items = lib.list_characters(db, skill_id=skill_id)
    return {"characters": items, "total": len(items)}


@router.post("/scan")
def scan(db: Session = Depends(get_db), limit: int = 200):
    """把历史 Run 里散落的角色资产导入角色库（存量迁移，可重复执行）。"""
    return lib.scan_all(db, storage, limit=limit)


@router.get("/{character_id}")
def get_character(character_id: str, db: Session = Depends(get_db)):
    ch = db.get(Character, character_id)
    if not ch:
        raise HTTPException(404, "角色不存在")
    d = ch.to_dict(with_card=True)
    d["has_art"] = lib.asset_path(ch.slug) is not None
    return d


@router.get("/{character_id}/art")
def get_art(character_id: str, db: Session = Depends(get_db)):
    ch = db.get(Character, character_id)
    if not ch:
        raise HTTPException(404, "角色不存在")
    p = lib.asset_path(ch.slug)
    if p is None:
        raise HTTPException(404, "该角色没有定妆图（源 Run 可能已清理）")
    return FileResponse(p)


@router.patch("/{character_id}")
def update_character(character_id: str, body: dict = Body(...), db: Session = Depends(get_db)):
    """改名 / 改锚点 / 换定妆图（data URL）/ 补 meta。

    注意：**改锚点会影响后续复用的所有集**。这是用户显式操作，接口不做静默修正，
    但会在 meta 里记录改动历史，便于回溯「第 5 集开始主角变了」这种问题。
    """
    ch = db.get(Character, character_id)
    if not ch:
        raise HTTPException(404, "角色不存在")

    history = list((ch.meta or {}).get("anchor_history") or [])
    if "anchor" in body and str(body["anchor"]).strip() and body["anchor"] != ch.anchor:
        history.append({"at": lib._now().isoformat(), "from": ch.anchor[:200]})
        ch.anchor = str(body["anchor"]).strip()
        # 锚点变了，卡片里的锚点段必须同步，否则复用出去的还是旧锚点
        ch.card = _rebuild_card(ch.card, ch.name, ch.anchor)

    if "name" in body and str(body["name"]).strip():
        ch.name = str(body["name"]).strip()[:128]
    if "card" in body and str(body["card"]).strip():
        ch.card = str(body["card"])

    if body.get("art_data_url"):
        import base64
        import re

        m = re.match(r"^data:image/([a-zA-Z0-9.+-]+);base64,(.*)$", str(body["art_data_url"]), re.DOTALL)
        if not m:
            raise HTTPException(400, "art_data_url 不是合法的图片 data URL")
        ext = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "webp": ".webp"}.get(
            m.group(1).lower(), ".png"
        )
        lib.ASSET_DIR.mkdir(parents=True, exist_ok=True)
        for old in lib._IMG_EXTS:  # 换图时清掉旧扩展名，避免两张图并存被随机选中
            oldp = lib.ASSET_DIR / f"{ch.slug}{old}"
            if oldp.exists():
                oldp.unlink()
        (lib.ASSET_DIR / f"{ch.slug}{ext}").write_bytes(base64.b64decode(m.group(2)))

    meta = dict(ch.meta or {})
    if history:
        meta["anchor_history"] = history[-10:]
    if isinstance(body.get("meta"), dict):
        meta.update(body["meta"])
    ch.meta = meta
    ch.updated_at = lib._now()
    db.commit()
    return ch.to_dict(with_card=True)


@router.delete("/{character_id}")
def delete_character(character_id: str, db: Session = Depends(get_db)):
    """删除角色资产（不删历史 Run 里的产物副本，那些属于已完成的集）。"""
    ch = db.get(Character, character_id)
    if not ch:
        raise HTTPException(404, "角色不存在")
    p = lib.asset_path(ch.slug)
    if p is not None:
        p.unlink(missing_ok=True)
    db.delete(ch)
    db.commit()
    return {"deleted": character_id}


# --------------------------------------------------------------------------- #
# 复用：把角色写进某一集
# --------------------------------------------------------------------------- #
@router.post("/apply/{run_id}")
def apply_to_run(run_id: str, body: dict = Body(...), db: Session = Depends(get_db)):
    """把选中的角色物化到指定 Run（未开跑或已跑完都可以，用于补做一致性）。

    产物：characters/<key>.<ext> + characters.md 里的锁定角色卡 + _index.json 留痕。
    """
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "任务不存在")
    ids = [str(i) for i in (body.get("character_ids") or [])]
    if not ids:
        raise HTTPException(400, "character_ids 为空")
    out = lib.materialize(db, storage, run_id, ids)
    return {"run_id": run_id, **out}


def _rebuild_card(card: str, name: str, anchor: str) -> str:
    """把角色卡里的锚点代码块替换为新锚点（找不到代码块则整卡重建）。"""
    import re

    if not card:
        return f"### {name}\n\n```\n{anchor}\n```"
    if re.search(r"```[a-zA-Z]*\s*\n.*?\n```", card, re.DOTALL):
        return re.sub(
            r"```[a-zA-Z]*\s*\n.*?\n```",
            lambda _m: f"```\n{anchor}\n```",
            card,
            count=1,
            flags=re.DOTALL,
        )
    return f"{card.rstrip()}\n\n```\n{anchor}\n```"
