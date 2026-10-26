"""角色库自测：解析 / 注册 / 物化 / 合并 的确定性验证。

为什么必须配反样本：split_cards 面对的是一份**模型自由书写**的 markdown，
层级、命名、锚点写法都在变。只测正样本会得到一个「看起来能用」的解析器，
真实数据一来就把子节标题当角色，把植物当人物，把容器当角色卡（这三种都发生过）。
"""

from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, delete  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import characters as lib  # noqa: E402
from app.db import init_db  # noqa: E402
from app.models import Base, Character, CharacterUsage  # noqa: E402
from app.store import storage  # noqa: E402

PASS, FAIL = [], []


def ok(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


ANCHOR_A = (
    "Stylized 2D anime cel-shaded illustration, clean ink lines, flat cel colors. "
    "A 28-year-old Chinese man, lean build, short black hair, grey work jacket. "
    "NOT a photograph, NOT photorealistic."
)
ANCHOR_B = (
    "Stylized 2D anime cel-shaded illustration. A 25-year-old Chinese woman, "
    "long dark hair tied back, white shirt, calm eyes. NOT photorealistic."
)
ANCHOR_PLANT = (
    "A single transparent nutrient solution tube about 30cm long, frosted glass, "
    "faintly glowing green liquid inside, a tiny sprout with two leaves emerging. "
    "Cinematic lighting, thick oil painting style, realistic texture."
)

MD_TWO = f"""# 角色设计

## 1. 陈末（机械维修师）

### 人物内核
一个不肯认输的人。

### 一致性锚点（生图描述词，冻结字符串）
```text
{ANCHOR_A}
```

### 禁止变化
- 发色：黑色短发

## 2. 苏晚（记者）

### 一致性锚点（冻结字符串）
```
{ANCHOR_B}
```

### 可变部分
- 表情
"""

MD_CONTAINER = f"""# 角色卡

{ANCHOR_A}

{ANCHOR_B}
"""

MD_PLANT = f"""## 1. 陈末（机械维修师）

### 一致性锚点
```text
{ANCHOR_A}
```

## 2. “绿光”（营养液管）

### 一致性锚点
```text
{ANCHOR_PLANT}
```
"""


def test_parse() -> None:
    print("[解析]")
    cards = lib.split_cards(MD_TWO)
    ok("两张角色卡都解析出来", [c["name"] for c in cards] == ["陈末", "苏晚"], str([c["name"] for c in cards]))
    ok("卡内锚点取到人物锚点", cards[0]["anchor"].startswith("Stylized 2D anime"), cards[0]["anchor"][:40])
    ok("角色卡保留原文（可原样注入）", "禁止变化" in cards[0]["card"])

    ok("子节标题「一致性锚点」不被当成角色", all("锚点" not in c["name"] for c in cards))

    container = lib.split_cards(MD_CONTAINER)
    ok("装多个锚点的容器不被当成角色卡", container == [], str([c["name"] for c in container]))

    plant = lib.split_cards(MD_PLANT)
    ok("非人物锚点（植物）不产生角色卡", [c["name"] for c in plant] == ["陈末"], str([c["name"] for c in plant]))

    ok("空文档返回空", lib.split_cards("") == [])


def test_slug() -> None:
    print("[slug]")
    a = lib._slug_for("陈末")
    b = lib._slug_for("陈末")
    ok("同名 slug 稳定", a == b, f"{a} vs {b}")
    ok("slug 只含安全字符", all(ch.isalnum() or ch in "_-" for ch in a), a)
    ok("不同名 slug 不同", a != lib._slug_for("苏晚"))


def test_materialize(db, tmp_run: str) -> None:
    print("[物化复用]")
    ch = Character(
        name="陈末",
        slug="selftest_chenmo",
        anchor=ANCHOR_A,
        card=f"### 陈末（机械维修师）\n\n```\n{ANCHOR_A}\n```\n",
        style="国漫赛璐璐",
        skill_id="comic-drama",
        art_rel="characters/chenmo.png",
        source_run_id="selftest",
    )
    db.add(ch)
    db.commit()

    # 造一张真实的小图作为资产
    lib.ASSET_DIR.mkdir(parents=True, exist_ok=True)
    src = Path(ROOT.parent / ".data" / "w3-tests" / "frame-mid.png")
    asset = lib.ASSET_DIR / "selftest_chenmo.png"
    if src.exists():
        shutil.copy2(src, asset)
    else:
        asset.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    out = lib.materialize(db, storage, tmp_run, [ch.id])
    ok("物化返回角色名", out.get("materialized") == ["陈末"], str(out))

    md = storage.read_text(tmp_run, "characters.md")
    ok("角色卡写入 md", "陈末" in md)
    ok("带锁定标记（合并时可定位）", "longspan:character" in md)

    files = storage.list_files(tmp_run)
    arts = [f for f in files if f.startswith("characters/") and f.endswith((".png", ".jpg"))]
    ok("定妆图落到 characters/", len(arts) >= 1, str(files))

    idx = json.loads(storage.read_text(tmp_run, "characters/_index.json"))
    ok("_index.json 留痕 key→角色", idx.get("locked") and idx["locked"][0]["slug"] == "selftest_chenmo", str(idx))

    # 幂等
    md1 = storage.read_text(tmp_run, "characters.md")
    lib.materialize(db, storage, tmp_run, [ch.id])
    md2 = storage.read_text(tmp_run, "characters.md")
    ok(
        "重复物化不产生重复块",
        md1.count("<!-- longspan:character slug=") == md2.count("<!-- longspan:character slug=") == 1,
        f"{md1.count('<!-- longspan:character slug=')} → {md2.count('<!-- longspan:character slug=')}",
    )

    # 下游参考图链路能吃到（关键：零改动复用）
    from app.harness.tools import _find_reference_art

    class Ctx:
        run_id = tmp_run
        storage = globals()["storage"]

    art, role, notes = _find_reference_art(Ctx(), f"{ANCHOR_A} 他站在车间里", "陈末站在车间")
    ok("下游 _find_reference_art 能找到复用定妆图", art is not None, str(notes))

    # 合并回填：模型覆写 md 把锚点改了
    storage.write_text(
        tmp_run,
        "characters.md",
        "# 角色设计\n\n### 陈末\n\n```\nA completely different person, blonde hair.\n```\n",
    )
    merged = lib.merge_locked(db, storage, tmp_run, [ch.id])
    md3 = storage.read_text(tmp_run, "characters.md")
    ok("模型改写的角色卡被回填为库版", ANCHOR_A[:40] in md3, md3[:80])
    ok("回填有偏离留痕", bool(merged.get("drift")), str(merged))


def main() -> int:
    init_db()
    real = sessionmaker(bind=create_engine(f"sqlite:///{ROOT.parent / '.data' / 'longspan.db'}", future=True))()
    tmp_run = f"selftest_chars_{uuid.uuid4().hex[:8]}"
    try:
        test_parse()
        test_slug()
        test_materialize(real, tmp_run)
    finally:
        # 清理：测试角色、来源记录、临时工作区、测试资产
        try:
            ids = [c.id for c in real.query(Character).filter(Character.slug.like("selftest_%")).all()]
            if ids:
                real.execute(delete(CharacterUsage).filter(CharacterUsage.character_id.in_(ids)))
                real.execute(delete(Character).filter(Character.id.in_(ids)))
                real.commit()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(storage.run_workspace(tmp_run), ignore_errors=True)
        for ext in lib._IMG_EXTS:
            (lib.ASSET_DIR / f"selftest_chenmo{ext}").unlink(missing_ok=True)
        real.close()

    print(f"\n结果：{len(PASS)} 通过，{len(FAIL)} 失败")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
