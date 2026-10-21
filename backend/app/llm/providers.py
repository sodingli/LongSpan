"""模型 Provider —— 长任务的"大脑"接口。

关键设计：
- 无 API Key 也必须能跑通完整流水线（否则无法验收"实时看到进度"），
  因此内置 MockProvider —— 输出由技能包预置（demo fixtures）驱动，确定、可复现。
- 配置了真实 Key 时切换到 OpenAI 兼容协议（DeepSeek / OpenAI / Moonshot /
  DashScope / 智谱 / 本地 vLLM / Ollama 全都走这一套），业务代码零改动。
- provider 实例可热重载（reset_provider），支撑前端「模型配置」面板改完即时生效。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger("longspan.llm")

# 网络层抖动的重试次数：长任务不能因为一次连接被掐断就整体失败
MAX_RETRIES = 3

# 服务端"暂时不可用"，重试就有戏（实测中转站过载会返回 503 no_available_workers）
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# "这个模型名根本不存在/当前 Key 没有它的通道" —— 重试纯属浪费，直接换下一个模型。
# 实测：中转站对不存在的模型名也回 503（body 里写 model_not_found），
# 若不识别就会白等 3 轮退避（约 8 秒）才回退。
MODEL_ABSENT_RE = re.compile(
    r"model_not_found|no available channel for model|model\s+(?:does\s+not\s+exist|not\s+found)"
    r"|invalid\s+model|unknown\s+model|模型不存在",
    re.IGNORECASE,
)

# 输出退化检测：同一片段连续重复 N 次即判为退化
_DEGENERATE_RE = re.compile(r"(.{4,80}?)\1{20,}", re.DOTALL)


class RetryableLLMError(RuntimeError):
    """可重试的模型接口故障（服务端过载 / 限流 / 输出退化）。"""


class ModelUnavailableError(RuntimeError):
    """该模型在当前 Key/接口下不可用（名字不存在或没有通道）——不重试，直接回退。"""


def looks_degenerate(text: str) -> bool:
    """检测模型输出退化（陷入重复循环）。

    实测：真实模型在长输出下会退化，吐了 3 万多字符的重复 `"**"`。
    这类输出必须判失败并重试，绝不能落盘当产物 —— 否则"验收通过"就成了笑话。
    """
    if len(text) < 400:
        return False
    return bool(_DEGENERATE_RE.search(text))


@dataclass
class LLMResponse:
    text: str
    tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    # 若本次结果是靠备用模型跑出来的，记录主模型名 —— 让前端/事件流能如实告知用户
    fallback_from: str = ""
    fallback_reason: str = ""


class FatalConfigError(RuntimeError):
    """配置层面的致命错误（没 Key / 没地址）—— 换模型也没用，直接失败。"""


def _one_line(err: Exception | str, limit: int = 160) -> str:
    """把异常压成一行，便于塞进事件与报错文案。"""
    text = re.sub(r"\s+", " ", str(err)).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def estimate_tokens(text: str) -> int:
    """粗略估算：中文约 1.5 字符/token。仅用于成本观感，不做精确计费。"""
    return max(1, int(len(text) / 1.5))


def mask_key(key: str) -> str:
    """脱敏展示：sk-abc… 只留头尾，避免接口把明文 Key 吐回前端。"""
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "***"
    return f"{key[:6]}…{key[-4:]}"


def normalize_base_url(url: str) -> str:
    """容忍用户直接粘贴完整端点地址。

    很多人是从服务商文档里复制 `.../v1/chat/completions` 过来的，
    这里统一裁回 `.../v1`，由代码自己拼端点，避免拼出 `.../chat/completions/models`。
    """
    u = (url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/chat"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
            break
    return u.rstrip("/")


def _split_entries(raw: str | list[str] | None) -> list[str]:
    if not raw:
        return []
    items = raw if isinstance(raw, list) else re.split(r"[,;\n]", str(raw))
    return [str(i).strip() for i in items if str(i).strip()]


def parse_fallback_chain(
    raw: str | list[str] | None,
    primary_model: str,
    primary_base_url: str,
) -> list[dict[str, str]]:
    """解析备用模型链，得到「主模型之外」按顺序尝试的节点列表。

    支持两种写法（可混用，用逗号 / 分号 / 换行分隔）：
    - `DeepSeek-V4-Flash`          → 同接口地址下的另一个模型名（最常见）
    - `V3@https://other.host/v1`   → 同时换接口地址（换服务商 / 换中转）

    会自动去重，并剔除与主模型完全相同的项 —— 否则等于白试一次。
    """
    primary_base = normalize_base_url(primary_base_url)
    seen: set[tuple[str, str]] = {(primary_model, primary_base)}
    chain: list[dict[str, str]] = []
    for item in _split_entries(raw):
        if "@" in item:
            model, _, base = item.rpartition("@")
            model, base = model.strip(), normalize_base_url(base)
        else:
            model, base = item, primary_base
        if not model:
            continue
        key = (model, base)
        if key in seen:
            continue
        seen.add(key)
        chain.append({"model": model, "base_url": base})
    return chain


# 常见服务商预设：前端「模型配置」面板一键填充，避免用户手抄地址。
# 模型名变动频繁（例：DeepSeek 的 deepseek-chat 已于 2026-07 退役），
# 因此只有确认过的才预填，其余留空由「拉取模型列表」从服务商实时获取。
PROVIDER_PRESETS: list[dict[str, str]] = [
    {
        "id": "deepseek",
        "label": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-flash",
        "hint": "长任务首选：1M 上下文、便宜、国内直连。deepseek-flash 为现行主模型，deepseek-v4-pro 更强也更贵",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "",
        "hint": "需要能访问 api.openai.com；也可换成任意中转地址，模型名点「拉取模型列表」获取",
    },
    {
        "id": "moonshot",
        "label": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "",
        "hint": "长上下文友好，适合论文类长任务；模型名请在平台模型列表确认后再填",
    },
    {
        "id": "dashscope",
        "label": "阿里通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "",
        "hint": "走 OpenAI 兼容模式；模型名点「拉取模型列表」获取",
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "",
        "hint": "国内直连；模型名点「拉取模型列表」获取",
    },
    {
        "id": "ollama",
        "label": "本地 Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "",
        "hint": "本机跑，Key 随便填一个非空值（如 ollama）；模型名填你 ollama list 里的名字",
    },
    {
        "id": "custom",
        "label": "自定义 / 中转",
        "base_url": "",
        "model": "",
        "hint": "任何 OpenAI 兼容接口都能接：填到 /v1 为止",
    },
]


class BaseProvider:
    name = "base"
    kind = "mock"  # mock | real

    async def chat(
        self,
        system: str,
        user: str,
        demo: str | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        raise NotImplementedError


class MockProvider(BaseProvider):
    """确定性 Mock：优先返回技能包预置的 demo 内容。

    这让 M0/M0.5 的"3 分钟出草稿"在没有模型额度时也能真实验收。
    """

    name = "mock"
    kind = "mock"

    async def chat(
        self,
        system: str,
        user: str,
        demo: str | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        await asyncio.sleep(0.15)  # 模拟一点延迟，让前端事件流有节奏感

        if demo:
            text = demo
        elif json_mode:
            text = json.dumps(
                {"note": "mock 模式：未配置模型 API Key，返回占位结构", "input": user[:200]},
                ensure_ascii=False,
                indent=2,
            )
        else:
            text = (
                "（mock 模式输出）\n\n"
                f"系统角色：{system.strip()[:80]}\n\n"
                f"任务输入摘要：{user.strip()[:200]}\n\n"
                "配置 LONGSPAN_LLM_PROVIDER 与 API Key 后可获得真实模型输出。"
            )
        return LLMResponse(
            text=text,
            tokens=estimate_tokens(system + user + text),
            cost_usd=0.0,
            model="mock",
        )


class OpenAICompatProvider(BaseProvider):
    """OpenAI 兼容协议（DeepSeek / Moonshot / OpenAI / 本地 vLLM / Ollama 均可）。"""

    name = "openai-compat"
    kind = "real"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.7,
        timeout: int | None = None,
        fallback_models: str | list[str] | None = None,
    ) -> None:
        self.api_key = api_key
        # 规范化：用户可能贴的是 .../v1/chat/completions（含第三方中转的完整端点）
        self.base_url = normalize_base_url(base_url)
        self.model = model
        self.temperature = temperature
        self.timeout = timeout or settings.llm_timeout_seconds
        # 主模型之外的回退链（主模型挂了这个顶上）
        self.fallbacks = parse_fallback_chain(fallback_models, model, self.base_url)

    def endpoint(self) -> str:
        return self._endpoint_for(self.base_url)

    @staticmethod
    def _endpoint_for(base_url: str) -> str:
        return f"{base_url}/chat/completions"

    def model_chain(self) -> list[dict[str, str]]:
        """实际会被尝试的节点顺序：主模型在前，备用模型依次在后。"""
        return [{"model": self.model, "base_url": self.base_url}, *self.fallbacks]

    @staticmethod
    def _label(node: dict[str, str], primary: dict[str, str]) -> str:
        return node["model"] if node["base_url"] == primary["base_url"] else f"{node['model']}@{node['base_url']}"

    async def chat(
        self,
        system: str,
        user: str,
        demo: str | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        """默认走**流式**，并在主模型不可用时自动回退到备用模型。

        为什么必须是流式（实测教训）：非流式请求在长输出时，服务端/中转网关会因为
        "长时间没有数据"而掐断连接，报 `Server disconnected without sending a response`。
        流式每几毫秒就有 chunk，既避开网关空闲超时，也让长任务不再被一次抖动废掉。

        为什么要有回退（实测教训）：中转站会给某个模型名恒返 503
        （`No available workers — all circuits open or unhealthy`），
        而同系列另一个名字是通的。没有回退链，整轮长任务就白跑。
        """
        if not self.api_key:
            raise FatalConfigError("未配置 API Key（LONGSPAN_LLM_API_KEY 为空）")
        if not self.base_url:
            raise FatalConfigError("未配置接口地址（LONGSPAN_LLM_BASE_URL 为空）")

        payload: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "stream": True,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        chain = self.model_chain()
        primary = chain[0]
        failures: list[str] = []

        for idx, node in enumerate(chain):
            label = self._label(node, primary)
            try:
                resp = await self._chat_with_retry(payload, node["model"], node["base_url"])
            except (httpx.TransportError, RetryableLLMError, RuntimeError) as e:
                failures.append(f"{label} → {_one_line(e)}")
                if idx + 1 < len(chain):
                    nxt = self._label(chain[idx + 1], primary)
                    logger.warning("模型 %s 不可用（%s），自动回退到 %s", label, _one_line(e, 80), nxt)
                    continue
                break
            if idx > 0:
                reason = failures[-1] if failures else ""
                resp.fallback_from = primary["model"]
                resp.fallback_reason = reason
                logger.warning("已由备用模型 %s 完成本次生成（主模型 %s 不可用）", label, primary["model"])
            return resp

        raise RuntimeError(
            "主模型与备用模型均不可用：" + "；".join(failures) +
            "。请在「模型配置」里更换模型，或补一个可用的备用模型。"
        )

    async def _chat_with_retry(
        self, payload: dict[str, Any], model: str, base_url: str
    ) -> LLMResponse:
        """对**单个模型**做退避重试；耗尽重试仍失败就抛出去，交给回退链换下一个。"""
        full = {**payload, "model": model}
        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await self._chat_stream(full, base_url)
            except RetryableLLMError as e:
                last_err = e
                logger.warning("模型 %s 第 %d/%d 次未成功（服务端过载/输出退化）：%s",
                               model, attempt, MAX_RETRIES, _one_line(e, 100))
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2.0 * attempt)
            except httpx.TransportError as e:
                last_err = e
                logger.warning("模型 %s 第 %d/%d 次网络中断：%s",
                               model, attempt, MAX_RETRIES, _one_line(e, 100))
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2.0 * attempt)
        raise RuntimeError(
            f"连续 {MAX_RETRIES} 次未成功：{type(last_err).__name__}: {_one_line(last_err, 200)}"
        )

    async def _chat_stream(self, payload: dict[str, Any], base_url: str | None = None) -> LLMResponse:
        if not self.api_key:
            raise FatalConfigError("未配置 API Key（LLM_API_KEY 为空）")
        base_url = base_url or self.base_url
        if not base_url:
            raise FatalConfigError("未配置接口地址（LLM_BASE_URL 为空）")

        # read timeout 管的是"两个 chunk 之间的间隔"，不是总时长：
        # 流式下只要模型还在吐字就不会超时，长输出因此不再受限。
        timeout = httpx.Timeout(self.timeout, connect=15.0)
        pieces: list[str] = []
        usage: dict[str, Any] = {}
        model = payload.get("model") or self.model

        async with httpx.AsyncClient(timeout=timeout) as cli:
            async with cli.stream(
                "POST",
                self._endpoint_for(base_url),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    msg = f"接口返回 {resp.status_code}：{_one_line(body, 300)}"
                    if MODEL_ABSENT_RE.search(body):
                        # 名字不存在 —— 重试无意义，交给回退链换模型
                        raise ModelUnavailableError(msg)
                    if resp.status_code in RETRYABLE_STATUS:
                        raise RetryableLLMError(msg)  # 服务端过载/限流 —— 值得重试
                    raise RuntimeError(msg)

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue  # 兼容个别中转站的心跳/脏行
                    if obj.get("usage"):
                        usage = obj["usage"]
                    if obj.get("model"):
                        model = obj["model"]
                    for choice in obj.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            pieces.append(piece)

        text = "".join(pieces)
        if looks_degenerate(text):
            raise RetryableLLMError(
                f"模型输出疑似退化（{len(text)} 字符中出现长时间重复片段），已丢弃本次结果并重试"
            )
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        if not pt and not ct:
            # 部分中转站流式不返回 usage，按字符估个大概，保证成本面板有数
            joined = "".join(str(m.get("content", "")) for m in payload.get("messages", []))
            pt, ct = estimate_tokens(joined), estimate_tokens(text)
        if not text.strip() and ct == 0:
            raise RetryableLLMError("接口返回了空内容（可能是模型名不匹配或被中转站拦截）")

        cost = (pt + ct) / 1_000_000 * 0.5  # 参考价 $0.5/1M token 粗算
        return LLMResponse(text=text, tokens=pt + ct, cost_usd=cost, model=model)

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """统一发请求 + 把服务端错误体读成人话。"""
        if not self.api_key:
            raise RuntimeError("未配置 API Key（LLM_API_KEY 为空）")
        if not self.base_url:
            raise RuntimeError("未配置接口地址（LLM_BASE_URL 为空）")

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                resp = await cli.post(
                    self.endpoint(),
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.ConnectError as e:
            raise RuntimeError(f"连不上接口地址 {self.base_url}：{e}") from e
        except httpx.TimeoutException as e:
            raise RuntimeError(f"请求超时（{self.timeout}s）：{e}") from e

        if resp.status_code >= 400:
            detail = resp.text[:400]
            raise RuntimeError(f"接口返回 {resp.status_code}：{detail}")

        try:
            return resp.json()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"接口返回不是合法 JSON：{resp.text[:200]}") from e

    async def probe(self) -> dict[str, Any]:
        """连通性自检：发一条极短请求，验证 Key / 地址 / 模型名三者都对。"""
        started = time.perf_counter()
        data = await self._post(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "你是连通性自检探针。"},
                    {"role": "user", "content": "只回复两个字：通了"},
                ],
                "temperature": 0,
                "max_tokens": 64,  # 别给太小：思考型模型会把额度全花在推理上，正文为空
            }
        )
        latency = int((time.perf_counter() - started) * 1000)
        text = (data["choices"][0]["message"]["content"] or "").strip()
        usage = data.get("usage") or {}
        return {
            "ok": True,
            "latency_ms": latency,
            "reply": text[:100],
            "model": data.get("model") or self.model,
            "tokens": usage.get("total_tokens", 0),
        }


_provider: BaseProvider | None = None


def build_provider(
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    fallback_models: str | list[str] | None = None,
) -> BaseProvider:
    """按配置构造 provider；provider=auto 时按"有没有 Key"决定。"""
    mode = (provider or "auto").strip().lower()
    if mode == "mock":
        return MockProvider()
    if mode == "auto" and not api_key:
        return MockProvider()
    return OpenAICompatProvider(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=settings.llm_temperature,
        fallback_models=fallback_models if fallback_models is not None else settings.llm_fallback_models,
    )


def get_provider() -> BaseProvider:
    global _provider
    if _provider is not None:
        return _provider
    _provider = build_provider(
        settings.llm_provider, settings.llm_api_key, settings.llm_base_url, settings.llm_model
    )
    return _provider


def reset_provider() -> BaseProvider:
    """改完配置后调用：丢掉缓存实例，下一次调用用新配置重建。"""
    global _provider
    _provider = None
    return get_provider()


def get_provider_for(model: str | None) -> BaseProvider:
    """取「某个阶段」要用的 provider —— 支撑画布上的阶段级模型覆盖。

    为什么要有它：长任务里不同阶段的诉求不一样（剧本要强、结构化清单要稳、改写要便宜）。
    允许按阶段换模型，但**不做全局改动**，也不缓存覆盖实例 ——
    一个任务的实验不该污染整站，更不该影响另一个正在跑的任务。
    """
    base = get_provider()
    want = (model or "").strip()
    if not want:
        return base
    if getattr(base, "kind", "") != "real":
        return base  # 演示模式下覆盖没有意义
    if want == getattr(base, "model", None):
        return base
    return OpenAICompatProvider(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=want,
        temperature=settings.llm_temperature,
        fallback_models=settings.llm_fallback_models,
    )


def llm_status() -> dict[str, Any]:
    """当前生效的模型状态（Key 脱敏）—— 前端徽章与配置面板都读这个。"""
    p = get_provider()
    real = p.kind == "real"
    chain = parse_fallback_chain(settings.llm_fallback_models, settings.llm_model, settings.llm_base_url)
    return {
        "mode": "real" if real else "mock",
        "provider": p.name,
        "configured_provider": settings.llm_provider,
        "configured": bool(settings.llm_api_key),
        "model": getattr(p, "model", settings.llm_model),
        "base_url": getattr(p, "base_url", settings.llm_base_url),
        "fallback_models": [f"{c['model']}@{c['base_url']}" if c["base_url"] != settings.llm_base_url else c["model"] for c in chain],
        "fallback_raw": settings.llm_fallback_models,
        "api_key_set": bool(settings.llm_api_key),
        "api_key_masked": mask_key(settings.llm_api_key),
    }


async def chat(
    system: str,
    user: str,
    demo: str | None = None,
    json_mode: bool = False,
) -> LLMResponse:
    return await get_provider().chat(system=system, user=user, demo=demo, json_mode=json_mode)


async def test_connection(
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    timeout: int = 25,
    fallback_models: str | list[str] | None = None,
) -> dict[str, Any]:
    """用一份"未保存的配置"直接测连通，不动当前生效的 provider。

    这样用户可以先验证再保存，避免把坏配置写进去导致整站不可用。
    备用模型也会一并探测 —— "配了但不可用"比"没配"更危险。
    """
    if (provider or "auto").strip().lower() == "mock":
        return {
            "ok": False,
            "mode": "mock",
            "error": "当前选择的是演示模式（mock），不会调用外部接口；请选择真实服务商后再测试。",
        }
    if not api_key:
        return {"ok": False, "mode": "real", "error": "请先填写 API Key。"}
    if not base_url:
        return {"ok": False, "mode": "real", "error": "请先填写接口地址（Base URL）。"}
    if not model:
        return {"ok": False, "mode": "real", "error": "请先填写模型名（Model）。"}

    probe = OpenAICompatProvider(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=0,
        timeout=timeout,
        fallback_models=fallback_models,
    )
    result: dict[str, Any] = {"base_url": probe.base_url, "endpoint": probe.endpoint()}
    try:
        result.update({"ok": True, **(await probe.probe())})
    except Exception as e:  # noqa: BLE001 —— 把任何异常都转成给用户看的一句话
        result.update({"ok": False, "mode": "real", "error": str(e)})

    if probe.fallbacks:
        result["fallbacks"] = [
            await _probe_node(probe, node) for node in probe.fallbacks
        ]
    return result


async def _probe_node(probe: OpenAICompatProvider, node: dict[str, str]) -> dict[str, Any]:
    """探测一个备用节点，只回报结论，不抛异常。"""
    item: dict[str, Any] = {"model": node["model"], "base_url": node["base_url"]}
    alt = OpenAICompatProvider(
        api_key=probe.api_key,
        base_url=node["base_url"],
        model=node["model"],
        temperature=0,
        timeout=probe.timeout,
    )
    try:
        r = await alt.probe()
        item.update({"ok": True, "latency_ms": r.get("latency_ms"), "model_returned": r.get("model")})
    except Exception as e:  # noqa: BLE001
        item.update({"ok": False, "error": _one_line(e, 200)})
    return item


async def list_models(base_url: str, api_key: str, timeout: int = 20) -> dict[str, Any]:
    """拉取服务商实时可用的模型列表（GET /v1/models）。

    为什么需要它：模型名变动比文档快（DeepSeek 的 deepseek-chat 已于 2026-07 退役），
    与其在文档里抄一个可能过时的名字，不如直接从服务商问"你现在有哪些模型"。
    """
    if not api_key:
        return {"ok": False, "error": "请先填写 API Key。"}
    if not base_url:
        return {"ok": False, "error": "请先填写接口地址（Base URL）。"}

    url = normalize_base_url(base_url)
    if not url.endswith("/models"):
        url = f"{url}/models"

    try:
        async with httpx.AsyncClient(timeout=timeout) as cli:
            resp = await cli.get(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.ConnectError as e:
        return {"ok": False, "error": f"连不上 {base_url}：{e}", "endpoint": url}
    except httpx.TimeoutException as e:
        return {"ok": False, "error": f"请求超时：{e}", "endpoint": url}

    if resp.status_code >= 400:
        return {
            "ok": False,
            "error": f"接口返回 {resp.status_code}：{resp.text[:300]}",
            "endpoint": url,
        }
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": f"返回不是合法 JSON：{resp.text[:200]}", "endpoint": url}

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {
            "ok": False,
            "error": f"返回结构里没有 data 列表：{str(data)[:200]}",
            "endpoint": url,
        }
    ids = sorted({str(m.get("id")) for m in items if isinstance(m, dict) and m.get("id")})
    return {"ok": True, "models": ids, "count": len(ids), "endpoint": url}


def _repair_json(text: str) -> str:
    """修复模型产出 JSON 的常见瑕疵。

    实测：真实模型会写出 `"reason": "reason": "不符合..."` 这种重复 key
    （严格解析直接报 Expecting ',' delimiter），以及尾随逗号。
    产物 JSON 一旦解析失败，验收标准和决策留痕会跟着一起垮，所以必须能修。
    """
    s = text
    # ① 重复 key：`"k": "k": value` → `"k": value`（模型偶发，连续多次）
    for _ in range(3):
        new = re.sub(r'("(?P<k>[\w\-]+)"\s*:\s*)"(?P=k)"\s*:\s*', r'"\g<k>": ', s)
        if new == s:
            break
        s = new
    # ② 尾随逗号：`,}` / `,]` → `}` / `]`
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def extract_json(text: str) -> Any | None:
    """从模型输出里稳健地抠出 JSON。

    容忍：```json 围栏、前后缀说明文字、重复 key、尾随逗号。
    """
    if not text:
        return None
    raw = text.strip()

    candidates: list[str] = []
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.DOTALL)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(raw)

    # 每个候选取"原文"与"第一个 { 到最后一个 } / [ 到最后一个 ]"的切片
    expanded: list[str] = []
    for c in candidates:
        expanded.append(c)
        s, e = c.find("{"), c.rfind("}")
        if s != -1 and e > s:
            expanded.append(c[s : e + 1])
        s, e = c.find("["), c.rfind("]")
        if s != -1 and e > s:
            expanded.append(c[s : e + 1])

    for chunk in expanded:
        for attempt in (chunk, _repair_json(chunk)):
            try:
                return json.loads(attempt)
            except (json.JSONDecodeError, ValueError):
                continue
    return None
