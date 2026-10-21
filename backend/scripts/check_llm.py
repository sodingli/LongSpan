#!/usr/bin/env python3
"""模型 / 接口连通性自检 —— 配好 Key 后先跑这个，别在网站里试错。

用法：
    cd backend
    python3 scripts/check_llm.py
    python3 scripts/check_llm.py --model deepseek-reasoner
    python3 scripts/check_llm.py --base-url https://api.moonshot.cn/v1 --api-key sk-xxx --model moonshot-v1-8k

它会真实发一条极短请求，验证「接口地址 + Key + 模型名」三者是否匹配。
退出码：0 = 通了；1 = 没通（原因在最后一行 ERROR 后）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.llm import llm_status, test_connection  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Longspan 模型连通性自检")
    parser.add_argument("--provider", default=None, help="auto / openai-compat / mock")
    parser.add_argument("--base-url", default=None, help="接口地址，填到 /v1 为止")
    parser.add_argument("--api-key", default=None, help="API Key（不填则用 .env / 环境变量里的）")
    parser.add_argument("--model", default=None, help="模型名")
    parser.add_argument("--fallback", default=None,
                        help="备用模型链（逗号分隔；可写 模型名@接口地址），不填则用已保存的配置")
    args = parser.parse_args()

    provider = args.provider or settings.llm_provider
    base_url = args.base_url or settings.llm_base_url
    model = args.model or settings.llm_model
    api_key = args.api_key if args.api_key is not None else settings.llm_api_key
    fallback = args.fallback if args.fallback is not None else settings.llm_fallback_models

    print("── 当前配置 ──────────────────────────────")
    st = llm_status()
    print(f"  接入方式     : {provider}")
    print(f"  接口地址     : {base_url}")
    print(f"  模型名       : {model}")
    print(f"  备用模型     : {fallback or '（未配置）'}")
    print(f"  API Key      : {st['api_key_masked'] or '（未设置）'}")
    print(f"  当前生效模式 : {'真实模型' if st['mode'] == 'real' else '演示模式（mock）'}")
    print()

    if (provider or "auto").lower() == "mock":
        print("当前是演示模式（mock），不调用外部接口，无需自检。")
        return 0

    if not api_key:
        print("未配置 API Key —— 现在会用演示模式（mock）跑通流程，不调用外部接口。")
        print("要接真实模型，二选一：")
        print("  1) cp backend/.env.example backend/.env 然后填入 LLM_API_KEY")
        print("  2) 打开网站，点左下角「配置」在面板里填")
        return 1

    print("── 发起一次真实请求 ──────────────────────")
    result = asyncio.run(
        test_connection(provider=provider, api_key=api_key, base_url=base_url,
                        model=model, fallback_models=fallback)
    )

    fallbacks = result.get("fallbacks") or []
    if fallbacks:
        print("  ── 备用模型探测 ──")
        for f in fallbacks:
            if f.get("ok"):
                print(f"    ✔ {f['model']}  {f.get('latency_ms')}ms")
            else:
                print(f"    ✘ {f['model']}  {f.get('error')}")
        print()

    if result.get("ok"):
        print("  ✔ 主模型连接成功")
        print(f"  延迟         : {result.get('latency_ms')} ms")
        print(f"  接口         : {result.get('endpoint')}")
        print(f"  返回模型名   : {result.get('model')}")
        print(f"  模型回复     : {result.get('reply') or '（空）'}")
        print()
        print("可以起服务跑真任务了：bash start.sh")
        return 0

    print("  ✘ 主模型连接失败")
    print(f"  接口         : {result.get('endpoint') or base_url}")
    print(f"  ERROR        : {result.get('error')}")
    if any(f.get("ok") for f in fallbacks):
        print("  提示         : 备用模型可用，长任务仍能跑完（会自动回退）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
