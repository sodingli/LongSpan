#!/usr/bin/env python3
"""生图通道机制的离线自测（不发任何网络请求）。

覆盖三块：
  1. 审核错误识别（is_moderation_error）：真实案例必须命中，普通错误不能误判；
  2. 软化函数（soften_prompt）：敏感词逐级替换、从原始 prompt 重生不叠加；
  3. 通道解析与路由（parse_channels / generate_image 内部排序）：
     fallbacks 形态宽容、能力矩阵如实、参考图 auto 路由稳定排序。

用法：python scripts/selftest_image_channels.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.media.image import (  # noqa: E402
    ImageChannel,
    is_moderation_error,
    parse_channels,
    soften_prompt,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))


def main() -> int:
    # ---- 1. 审核识别 ----
    cases = [
        ("万相：Output data may contain inappropriate content", True),
        ("京东云：InputImageSensitiveContentDetected 疑似真人", True),
        ("DashScope：DataInspectionFailed", True),
        ("网络抖动：Server disconnected without sending a response", False),
        ("配置错：京东云生图缺少预置服务标识", False),
        ("HTTP 429：rate limited", False),
    ]
    for text, expect in cases:
        got = is_moderation_error(text)
        check(f"审核识别: {text[:36]}… → {expect}", got == expect, f"got {got}")

    # ---- 2. 软化 ----
    raw = "A wounded soldier with blood on his uniform holds a knife near a corpse, horror atmosphere"
    l1 = soften_prompt(raw, 1)
    check("软化 L1 替换 blood", "blood" not in l1.lower(), l1[:80])
    check("软化 L1 替换 knife", "knife" not in l1.lower(), "")
    check("软化 L1 替换 corpse", "corpse" not in l1.lower(), "")
    check("软化 L1 保留语义（tired）", "tired" in l1.lower(), "")
    check("软化 L1 追加安全声明", "family-friendly" in l1.lower(), "")
    l2 = soften_prompt(raw, 2)
    check("软化 L2 压制 horror", "horror" not in l2.lower(), "")
    check("软化 L2 追加安全声明", "safe for all audiences" in l2.lower(), "")
    check("软化从原始重生（不叠加 L1 后缀）", l2.count("family-friendly") == 0 or "family-friendly" not in l2, "")
    check("软化 level=0 原样", soften_prompt(raw, 0) == raw, "")
    # 正常画面不被误伤
    clean = "a young mechanic standing in a scrapyard at dawn, flat 2D anime style"
    check("正常画面 L1 不被误伤", soften_prompt(clean, 1).startswith(clean), "")

    # ---- 3. 通道解析 ----
    from app.config import settings

    old_fb, old_provider = settings.image_fallbacks, settings.image_provider
    try:
        settings.image_fallbacks = ""
        settings.image_provider = "dashscope-mm"
        settings.image_base_url = "https://example.com/api/v1"
        settings.image_api_key = "sk-test"
        settings.image_model = "qwen-image-3.0-pro"
        settings.image_service = ""
        chans = parse_channels()
        check("仅主通道：解析出 1 条", len(chans) == 1, f"got {len(chans)}")
        if chans:
            c = chans[0]
            check("主通道能力（dashscope-mm 支持参考图）", c.supports_reference and c.supports_negative and c.supports_seed, "")
            check("主通道名 main", c.label() == "main", c.label())

        settings.image_fallbacks = (
            '[{"name":"wan22","provider":"dashscope","base_url":"https://a.com/api/v1","api_key":"sk-a","model":"wan2.2-t2i-plus"},'
            '{"name":"jd","provider":"jdcloud","base_url":"https://b.com/v1","api_key":"pk-b","model":"image-01","service":"minimax_image01"},'
            '{"provider":"","api_key":"x"},'
            '{"provider":"unknown-proto","base_url":"https://c.com","api_key":"k"}]'
        )
        chans = parse_channels()
        check("fallbacks：完整项收、残缺/未知项跳", len(chans) == 3, f"got {[c.label() for c in chans]}")
        check("通道能力各如实声明",
              chans[1].supports_reference is False and chans[2].supports_reference is True,
              f"{[(c.supports_reference) for c in chans]}")

        # 单对象也接受
        settings.image_fallbacks = '{"provider":"jdcloud","base_url":"https://b.com/v1","api_key":"pk","model":"image-01"}'
        chans = parse_channels()
        check("fallbacks 单对象形态", len(chans) == 2, f"got {len(chans)}")

        # 坏 JSON 容错（只丢备用链，不炸）
        settings.image_fallbacks = "not-json"
        chans = parse_channels()
        check("fallbacks 坏 JSON 只丢备用链", len(chans) == 1, f"got {len(chans)}")

        # auto 路由：参考图会把支持参考图的备用通道排到前面（稳定排序）
        settings.image_fallbacks = (
            '[{"name":"noRef","provider":"dashscope","base_url":"https://a.com/api/v1","api_key":"sk-a","model":"wan2.2-t2i-plus"},'
            '{"name":"withRef","provider":"jdcloud","base_url":"https://b.com/v1","api_key":"pk-b","model":"image-01","service":"svc"}]'
        )
        chans = parse_channels()
        routed = sorted(chans, key=lambda c: 0 if c.supports_reference else 1)
        check("auto 路由：参考图能力通道排在不支持者之前",
              [c.name for c in routed].index("withRef") < [c.name for c in routed].index("noRef"),
              f"{[c.name for c in routed]}")
        # 主通道本身支持参考图时顺序不变（稳定排序不打破主备）
        settings.image_fallbacks = '[{"name":"noRef","provider":"dashscope","base_url":"https://a.com/api/v1","api_key":"sk-a","model":"wan2.2-t2i-plus"}]'
        chans = parse_channels()
        routed = sorted(chans, key=lambda c: 0 if c.supports_reference else 1)
        check("auto 路由：主通道支持参考图时仍是第一", routed[0].name == "main", f"{[c.name for c in routed]}")
    finally:
        settings.image_fallbacks, settings.image_provider = old_fb, old_provider

    # ---- 4. 审核拦截→软化→换通道 的编排逻辑（mock 请求函数）----
    from app.media import image as img_mod

    calls: list[str] = []

    async def fake_request(ch, prompt, *a, **kw):
        calls.append(f"{ch.label()}|{prompt}")
        if "jd" in ch.label():
            return b"png-bytes", "png", ["ok"]
        raise RuntimeError("Output data may contain inappropriate content")

    orig = img_mod._request_via_channel
    img_mod._request_via_channel = fake_request
    orig_parse = img_mod.parse_channels
    try:
        settings.image_provider = "dashscope"
        settings.image_base_url = "https://a.com/api/v1"
        settings.image_api_key = "sk-a"
        settings.image_model = "wan2.2-t2i-plus"
        settings.image_fallbacks = '{"name":"jd","provider":"jdcloud","base_url":"https://b.com/v1","api_key":"pk","model":"image-01","service":"svc"}'

        class FakeStorage:
            def write_bytes(self, run_id, p, data):
                return p, len(data)

            def write_text(self, run_id, p, text):
                return p, len(text)

        img_mod.parse_channels = img_mod.parse_channels  # 保持真实解析（读 settings）
        out = asyncio.run(img_mod.generate_image(
            FakeStorage(), "run-x", "a bloody knife scene", "panels/p.svg", "9:16",
        ))
        check("审核三级：最终成功（换通道）", out.get("is_placeholder") is False, str(out)[:120])
        check("审核三级：trace 记录了软化与换通道", any("软化" in t for t in out.get("channel_trace", [])),
              str(out.get("channel_trace"))[:160])
        check("审核三级：软化重试确实发生过", sum(1 for c in calls if c.startswith("main")) >= 3,
              f"main 调用 {sum(1 for c in calls if c.startswith('main'))} 次")
        check("审核三级：最终用的是备用通道", "jd" in str(out.get("channel", "")), out.get("channel", ""))
        # 软化是每通道独立的：main 被拦时用软化词重试；jd 首次原始 prompt 即成功
        main_calls = [c for c in calls if c.startswith("main")]
        jd_calls = [c for c in calls if c.startswith("jd")]
        check("审核三级：main 逐级软化重试", any("family-friendly" in c.lower() for c in main_calls),
              str(main_calls)[:120])
        check("审核三级：jd 用原始 prompt 一次成功", jd_calls and "bloody" in jd_calls[0],
              str(jd_calls)[:120])

        # 全部通道失败 → 占位 + 逐通道留痕
        calls.clear()

        async def always_fail(ch, prompt, *a, **kw):
            calls.append(ch.label())
            raise RuntimeError(f"{ch.label()} 网络错误")

        img_mod._request_via_channel = always_fail
        out = asyncio.run(img_mod.generate_image(
            FakeStorage(), "run-x", "anything", "panels/p.svg", "9:16",
        ))
        check("全败：占位图", out.get("is_placeholder") is True, str(out)[:100])
        check("全败：fallback_reason 逐通道列出", "main" in str(out.get("fallback_reason", ""))
              and "jd" in str(out.get("fallback_reason", "")), str(out.get("fallback_reason", ""))[:160])
    finally:
        img_mod._request_via_channel = orig
        settings.image_fallbacks, settings.image_provider = old_fb, old_provider

    # ---- 汇总 ----
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"\n通过 {len(RESULTS) - len(failed)}/{len(RESULTS)}")
    for n, d in failed:
        print(f"  FAIL: {n}  {d}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
