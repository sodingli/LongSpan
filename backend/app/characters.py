"""角色资产库：把角色从「某一集的产物」提升为「工作台的资产」。

产品理由（不是工程细节）：
    漫剧是系列产品，系列的资产是角色。角色放在 run workspace 里，第二集想用
    第一集的主角只能重新生图、重新赌一致性 —— 这是产品缺陷。角色应该归属于
    工作台，跨集复用，且复用必须是**同一张定妆图**（确定性一致），不是「锚点
    文字相近」的概率一致。

实现策略（关键取舍：不改下游链路）：
    复用时把角色物化回目标 Run 的 workspace：定妆图落到 characters/<key>.<ext>，
    角色卡以带标记的块写进 characters.md。下游 `_find_reference_art` 的四级查找、
    `extract_anchors`、`_read_character_art` 全部照旧工作，不需要任何改动 ——
    这也意味着复用不会给生图链路引入新的分支和回归风险。

    <key> 不是 slug，而是「characters.md 里锚点标签解析出来的 key」（可能是
    ANCHOR_1，也可能是 CHENMO）。物化后立刻按当前文件解析一次，为每个 key
    落一份同内容图，写 characters/_index.json 留痕「哪个 key 对应库里哪个角色」。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .harness.consistency import extract_anchors, is_character_anchor
from .models import Character, CharacterUsage

# 角色资产库的物理目录（独立于 run workspace —— 删 run 不该删资产）
ASSET_DIR = settings.storage_root.parent / "characters"

# 角色卡在 characters.md 里的包裹标记：合并时靠它精确定位「哪一块是库里的角色」
MARK_BEGIN = "<!-- longspan:character slug={slug} source={source} -->"
MARK_END = "<!-- /longspan:character -->"

_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_HEAD_RE = re.compile(r"^(#{2,4})\s+(.+?)\s*$")

# 这些标题是角色卡内部的子节，不是角色本身（模型的实际写法见 split_cards 注释）
_NONCHAR_TITLE_RE = re.compile(
    r"锚点|画风|风格|可变|禁止|视觉|内核|场景|世界观|说明|备注|提示|概述|关系|"
    r"anchor|style|variab|forbid|visual|core|scene|note",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def _clean_name(raw: str) -> str:
    """标题 → 角色名。'角色一：阿杰（28岁）' → '阿杰'；'老渔民（约65岁）' → '老渔民'。"""
    s = re.sub(r"^角色[一二三四五六七八九十百零\d]*\s*[：:]\s*", "", (raw or "").strip())
    s = re.sub(r"[（(].*?[）)]", "", s).strip()
    s = re.sub(r"^[\d.、\s]+", "", s)
    return s[:24]


def split_cards(characters_md: str) -> list[dict]:
    """把 characters.md 拆成角色卡块。

    不靠「标题层级」判定，因为层级是模型随手写的（实测同一份文档里
    `## 角色卡` / `### 老渔民` / `#### 一致性锚点` 三层混用）。判定规则改成
    **按锚点归属**：

      1. 标题本身是「锚点/画风/可变/禁止/视觉/内核」这类子节 → 不是角色卡；
      2. 块内出现 ≥2 个不同锚点 → 是容器（如「## 角色卡」装着一堆角色），跳过；
      3. 块内恰好 1 个锚点 → 是角色卡；
      4. 同一锚点被父子块同时命中时，取范围最小的那个（最深、最精确）。

    返回 [{name, card, anchor}]，card 是原文块（含标题），复用时原样注入。
    """
    text = (characters_md or "").replace("\r\n", "\n")
    lines = text.split("\n")
    heads: list[tuple[int, int, str]] = []  # (行号, 级别, 标题原文)
    for i, line in enumerate(lines):
        m = _HEAD_RE.match(line.strip())
        if not m:
            continue
        level = len(m.group(1))
        if level < 2 or level > 4:
            continue
        heads.append((i, level, m.group(2).strip()))

    sections: list[dict] = []
    for j, (start, level, raw) in enumerate(heads):
        end = len(lines)
        for k in range(j + 1, len(heads)):
            if heads[k][1] <= level:
                end = heads[k][0]
                break
        sections.append({"start": start, "end": end, "level": level, "raw": raw})

    # 小块优先认领锚点，避免父容器抢走子角色的锚点
    sections.sort(key=lambda s: (s["end"] - s["start"], -s["level"]))
    claimed: set[str] = set()
    cards: list[dict] = []
    for sec in sections:
        title = sec["raw"]
        if _NONCHAR_TITLE_RE.search(title):
            continue  # 子节标题（锚点/画风/可变/禁止…）
        block = "\n".join(lines[sec["start"]: sec["end"]]).strip()
        if not block:
            continue
        got = extract_anchors(block)
        anchors = [v for v in got.values() if is_character_anchor(v)]
        if len(anchors) != 1 or not anchors[0]:
            continue  # 0 个 = 无角色；≥2 个 = 容器
        anchor = anchors[0]
        if anchor in claimed:
            continue
        name = _clean_name(title)
        if not name or len(name) > 24:
            continue
        claimed.add(anchor)
        cards.append({"name": name, "card": block, "anchor": anchor, "title": title, "_start": sec["start"]})

    # 还原文档顺序（上面的排序是为了认领锚点，输出必须按原文顺序 —— 下游按顺序对齐）
    cards.sort(key=lambda c: c["_start"])
    for c in cards:
        c.pop("_start", None)
    return cards


def _card_anchor(block: str) -> str:
    """从单个角色卡块里取锚点原文（与 extract_anchors 同源，保证口径一致）。"""
    got = extract_anchors(block)
    if not got:
        return ""
    # 优先形态 A/B（角色自身标签），否则取最长的那个（裸代码块编号 anchor_N）
    vals = [v for v in got.values() if is_character_anchor(v)]
    return max(vals, key=len) if vals else ""


def _slug_for(name: str, db: Session | None = None) -> str:
    """稳定 slug：中文名不可直接做文件名，用短哈希。改名不改 slug（资产身份不变）。"""
    if db is not None:
        hit = db.scalar(select(Character).where(Character.name == name))
        if hit:
            return hit.slug
    digest = hashlib.sha1((name or "角色").encode("utf-8")).hexdigest()[:10]
    return f"char_{digest}"


def _safe_key(role: str) -> str:
    """与 tools._read_character_art 的归一规则保持一致（否则找得到表、找不到图）。"""
    return re.sub(r"[^a-z0-9_\-]", "", (role or "").lower()) or "unknown"


# --------------------------------------------------------------------------- #
# 注册：run → 库
# --------------------------------------------------------------------------- #
def _run_art(storage: Any, run_id: str, key: str) -> Path | None:
    for ext in _IMG_EXTS:
        rel = f"characters/{_safe_key(key)}{ext}"
        try:
            if storage.exists(run_id, rel):
                return storage.abs_path(run_id, rel)
        except Exception:  # noqa: BLE001
            continue
    return None


def _pair_cards_with_keys(characters_md: str, cards: list[dict]) -> list[tuple[dict, str]]:
    """给每张角色卡找到它的锚点 key（= 定妆图文件名）。

    确定性做法：先解析全文件的 {key: anchor}，再拿角色卡里的锚点原文去**逐字比对**。
    不用「标题顺序 == 图片排序」这种顺序对齐 —— 一旦模型少写一个角色卡就会整体错位，
    错位的结果是「参考图传错人 = 确定性换脸」，代价远高于直接放弃。
    """
    anchors = extract_anchors(characters_md)
    pairs: list[tuple[dict, str]] = []
    used: set[str] = set()
    for card in cards:
        hit = ""
        for key, val in anchors.items():
            if key in used:
                continue
            if val and card["anchor"] and (val == card["anchor"] or card["anchor"][:80] in val):
                hit = key
                break
        if hit:
            used.add(hit)
        pairs.append((card, hit))
    return pairs


def scan_run(db: Session, storage: Any, run_id: str, *, skill_id: str = "", style: str = "") -> dict:
    """把一个 Run 的角色登记进库。幂等：同名角色不重复铸造，只记一次出场。"""
    try:
        characters_md = storage.read_text(run_id, "characters.md") or ""
    except Exception:  # noqa: BLE001
        return {"registered": [], "skipped": "无 characters.md"}

    cards = split_cards(characters_md)
    if not cards:
        return {"registered": [], "skipped": "characters.md 里没有可解析的角色卡"}

    pairs = _pair_cards_with_keys(characters_md, cards)
    registered: list[str] = []
    updated: list[str] = []
    missing_art: list[str] = []

    for card, key in pairs:
        art = _run_art(storage, run_id, key) if key else None
        if art is None:
            missing_art.append(card["name"])
            continue

        existing = db.scalar(select(Character).where(Character.name == card["name"]))
        slug = existing.slug if existing else _slug_for(card["name"])
        ASSET_DIR.mkdir(parents=True, exist_ok=True)
        dest = ASSET_DIR / f"{slug}{art.suffix.lower()}"
        try:
            if not dest.exists() or dest.stat().st_size != art.stat().st_size:
                shutil.copy2(art, dest)
        except Exception as exc:  # noqa: BLE001
            missing_art.append(f"{card['name']}（复制失败：{exc}）")
            continue

        if existing:
            # 已有角色：**不覆盖**锚点与定妆图 —— 后来的图未必更好，覆盖等于悄悄改人设。
            # 只补空字段 + 记出场；要换图走 PATCH（用户显式操作）。
            changed = False
            if not existing.card:
                existing.card, changed = card["card"], True
            if not existing.art_rel:
                existing.art_rel, changed = f"characters/{_safe_key(key)}{art.suffix.lower()}", True
            if changed:
                existing.updated_at = _now()
            updated.append(existing.name)
            char = existing
        else:
            char = Character(
                name=card["name"],
                slug=slug,
                anchor=card["anchor"],
                card=card["card"],
                style=style,
                skill_id=skill_id,
                art_rel=f"characters/{_safe_key(key)}{art.suffix.lower()}",
                source_run_id=run_id,
            )
            db.add(char)
            db.flush()
            registered.append(char.name)

        _mark_usage(db, char.id, run_id, reused=False)
        char.usage_count = int(char.usage_count or 0) + 1
        char.last_used_at = _now()

    db.commit()
    return {
        "registered": registered,
        "existing": updated,
        "missing_art": missing_art,
        "summary": f"角色入库：新增 {len(registered)}、已存在 {len(updated)}、缺定妆图 {len(missing_art)}",
    }


def _mark_usage(db: Session, character_id: str, run_id: str, *, reused: bool) -> None:
    hit = db.scalar(
        select(CharacterUsage).where(
            CharacterUsage.character_id == character_id, CharacterUsage.run_id == run_id
        )
    )
    if hit:
        hit.reused = bool(hit.reused or reused)
        return
    db.add(CharacterUsage(character_id=character_id, run_id=run_id, reused=reused))


def scan_all(db: Session, storage: Any, *, limit: int = 200) -> dict:
    """扫描历史 Run，把散落各处的角色资产一次性导入（存量数据的迁移入口）。"""
    from .models import Run

    runs = db.scalars(select(Run).order_by(Run.created_at.desc()).limit(limit)).all()
    total = {"registered": 0, "existing": 0, "runs": 0, "missing_art": 0}
    for run in runs:
        out = scan_run(
            db, storage, run.id, skill_id=run.skill_id,
            style=str((run.brief or {}).get("style") or ""),
        )
        if out.get("registered") or out.get("existing"):
            total["runs"] += 1
        total["registered"] += len(out.get("registered") or [])
        total["existing"] += len(out.get("existing") or [])
        total["missing_art"] += len(out.get("missing_art") or [])
    return total


# --------------------------------------------------------------------------- #
# 复用：库 → run
# --------------------------------------------------------------------------- #
def materialize(db: Session, storage: Any, run_id: str, character_ids: list[str]) -> dict:
    """把选中的角色写进目标 Run：定妆图 + 角色卡 + key 索引。

    幂等：重复调用不会产生重复块或重复文件。
    """
    ids = [i for i in (character_ids or []) if i]
    if not ids:
        return {"materialized": [], "note": "未选择复用角色"}

    chars = db.scalars(select(Character).where(Character.id.in_(ids))).all()
    if not chars:
        return {"materialized": [], "note": "选中的角色在库里不存在"}

    # 1) characters.md：写入/替换带标记的角色卡块
    try:
        md = storage.read_text(run_id, "characters.md") or ""
    except Exception:  # noqa: BLE001
        md = ""
    if not md.strip():
        md = "# 角色设计（本集锁定角色来自角色库）\n"

    names: list[str] = []
    for ch in chars:
        block = (
            MARK_BEGIN.format(slug=ch.slug, source=ch.source_run_id or "-")
            + "\n"
            + (ch.card or f"### {ch.name}\n\n```\n{ch.anchor}\n```")
            + "\n"
            + MARK_END
        )
        md = _replace_block(md, ch.slug, block, ch.name)
        names.append(ch.name)
    storage.write_text(run_id, "characters.md", md)

    # 2) 按当前文件解析出的 key 为每个角色落图（下游按键名找图，不认 slug）
    keys_meta = _materialize_art(db, storage, run_id, chars)

    # 3) 留痕：哪个 key 是库里哪个角色（参考图定位排查的唯一依据）
    storage.write_json(
        run_id,
        "characters/_index.json",
        {
            "locked": [
                {"slug": c.slug, "name": c.name, "source_run_id": c.source_run_id}
                for c in chars
            ],
            "keys": keys_meta,
        },
    )
    for ch in chars:
        _mark_usage(db, ch.id, run_id, reused=True)
    db.commit()
    return {"materialized": names, "keys": keys_meta}


def _materialize_art(db: Session, storage: Any, run_id: str, chars: list[Character]) -> dict:
    """把库内定妆图落到 characters/<key>.<ext>。key 取自当前 characters.md 的解析结果。"""
    md = storage.read_text(run_id, "characters.md") or ""
    anchors = extract_anchors(md)
    out: dict[str, str] = {}

    for ch in chars:
        src = _asset_path(ch.slug)
        if src is None:
            out[ch.slug] = "缺资产文件"
            continue
        target_keys: list[str] = []
        for key, val in anchors.items():
            if val and ch.anchor and (val == ch.anchor or ch.anchor[:80] in val):
                target_keys.append(key)
        if not target_keys:
            # 合并后仍解析不到（模型改写了锚点）→ 用 slug 兜底，至少一张图在
            target_keys = [ch.slug]
        for key in target_keys:
            rel = f"characters/{_safe_key(key)}{src.suffix.lower()}"
            try:
                storage.write_bytes(run_id, rel, src.read_bytes())
                out[ch.slug] = rel
            except Exception as exc:  # noqa: BLE001
                out[ch.slug] = f"落图失败：{exc}"
    return out


def merge_locked(db: Session, storage: Any, run_id: str, character_ids: list[str]) -> dict:
    """阶段执行后强制合并：模型覆写 characters.md 时，把锁定角色的卡改回库版。

    为什么必须在阶段后做：模型产出 characters.md 时会整份重写，光靠提示词说
    「必须复用」是建议不是契约。这里做的是**机器回填**，与骨架回填同一个思路。
    """
    ids = [i for i in (character_ids or []) if i]
    if not ids:
        return {"merged": [], "drift": []}
    chars = db.scalars(select(Character).where(Character.id.in_(ids))).all()
    if not chars:
        return {"merged": [], "drift": []}

    try:
        md = storage.read_text(run_id, "characters.md") or ""
    except Exception:  # noqa: BLE001
        md = ""

    merged: list[str] = []
    drift: list[str] = []
    for ch in chars:
        block = (
            MARK_BEGIN.format(slug=ch.slug, source=ch.source_run_id or "-")
            + "\n"
            + (ch.card or f"### {ch.name}\n\n```\n{ch.anchor}\n```")
            + "\n"
            + MARK_END
        )
        before = md
        had_slot = (ch.slug in md) or (ch.name in md)
        md = _replace_block(md, ch.slug, block, ch.name)
        if not had_slot:
            drift.append(f"{ch.name}：模型未产出该角色，已按库版补回")
        elif ch.anchor and ch.anchor[:60] not in before:
            drift.append(f"{ch.name}：模型改写了角色卡，已按库版回填")
        merged.append(ch.name)

    storage.write_text(run_id, "characters.md", md)
    _materialize_art(db, storage, run_id, chars)
    return {"merged": merged, "drift": drift}


def _replace_block(md: str, slug: str, block: str, name: str = "") -> str:
    """把角色卡放进文档：有锁定标记 → 原地替换；有同名角色卡 → 替换那一块；都没有 → 追加。

    三级兜底是必要的：模型产出 characters.md 时可能保留我们的标记（续跑），也可能
    整份重写成自己的格式（首轮），还可能压根没写这个角色（漏写）。三种情况都要
    收敛到「库版角色卡在文件里」，否则复用等于没做。
    """
    pat = re.compile(
        r"<!--\s*longspan:character\s+slug=" + re.escape(slug) + r".*?-->.*?"
        + re.escape(MARK_END),
        re.DOTALL,
    )
    if pat.search(md):
        return pat.sub(lambda _m: block, md, count=1)

    if name:
        lines = md.split("\n")
        for i, line in enumerate(lines):
            m = _HEAD_RE.match(line.strip())
            if not m:
                continue
            if _clean_name(m.group(2)) != name:
                continue
            level = len(m.group(1))
            end = len(lines)
            for j in range(i + 1, len(lines)):
                m2 = _HEAD_RE.match(lines[j].strip())
                if m2 and len(m2.group(1)) <= level:
                    end = j
                    break
            return "\n".join(lines[:i] + block.split("\n") + lines[end:])

    return md.rstrip() + "\n\n" + block + "\n"


def _asset_path(slug: str) -> Path | None:
    for ext in _IMG_EXTS:
        p = ASSET_DIR / f"{slug}{ext}"
        if p.exists():
            return p
    return None


def asset_path(slug: str) -> Path | None:
    return _asset_path(slug)


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def list_characters(db: Session, *, skill_id: str | None = None) -> list[dict]:
    stmt = select(Character).order_by(Character.updated_at.desc())
    if skill_id:
        stmt = stmt.where(Character.skill_id == skill_id)
    out: list[dict] = []
    for ch in db.scalars(stmt).all():
        d = ch.to_dict()
        runs = db.scalars(
            select(CharacterUsage).where(CharacterUsage.character_id == ch.id)
        ).all()
        d["appearances"] = [u.run_id for u in runs]
        d["has_art"] = _asset_path(ch.slug) is not None
        out.append(d)
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
