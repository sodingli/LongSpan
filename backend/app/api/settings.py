"""模型配置接口：让用户在一个地方填「模型 + 接口」，改完即时生效。

- GET  /api/settings/model        当前生效配置（Key 脱敏）+ 服务商预设
- PUT  /api/settings/model        保存配置：写 backend/.env + 热启用（不重启）
- POST /api/settings/model/test   用"还没保存"的配置直接测连通，先验证再落库
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..llm import PROVIDER_PRESETS, list_models, llm_status, normalize_base_url, test_connection
from ..runtime_config import (
    MEDIA_SPEC,
    apply_media_config,
    apply_model_config,
    env_file,
    env_file_exists,
    media_config_snapshot,
    media_env_keys,
    persist_media_config,
    persist_model_config,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])

VALID_PROVIDERS = {"auto", "mock", "openai-compat"}

# 每类媒体的可选接入方式。取值不是随手写的：
# - auto：有 Key 就走真实服务，没有就走本地兜底（生图 SVG 分镜卡 / 配音 macOS say）
# - local：强制本地兜底（不想花钱、或想复现某次结果时用）
# - off：彻底关闭（视频默认 off —— 它是唯一按次计费且可能等几分钟的能力）
MEDIA_PROVIDERS = {
    "image": ["auto", "local", "openai-compat", "jdcloud"],
    "tts": ["auto", "local", "openai-compat"],
    "video": ["off", "jdcloud", "dashscope"],
}


class ModelConfigIn(BaseModel):
    provider: str = "auto"
    base_url: str = ""
    api_key: str | None = None   # None/空 → 沿用已保存的 Key（避免前端脱敏回显把真 Key 覆盖掉）
    model: str = ""
    temperature: float | None = None
    # 备用模型链：逗号分隔；可写 `模型名` 或 `模型名@接口地址`。留空字符串表示"清空回退链"。
    fallback_models: str | None = None


class ModelProbeIn(BaseModel):
    base_url: str = ""
    api_key: str | None = None


def _resolve(payload: ModelConfigIn) -> dict:
    """把请求里的空字段回退到当前已生效配置。"""
    provider = (payload.provider or "auto").strip().lower()
    if provider not in VALID_PROVIDERS:
        raise HTTPException(status_code=400, detail="provider 只能是 auto / mock / openai-compat")
    return {
        "provider": provider,
        "api_key": payload.api_key if payload.api_key else settings.llm_api_key,
        "base_url": normalize_base_url(payload.base_url) or settings.llm_base_url,
        "model": payload.model.strip() or settings.llm_model,
        "temperature": payload.temperature,
        # None → 沿用已保存的；给了值（哪怕是空串）→ 以新的为准
        "fallback_models": (
            settings.llm_fallback_models if payload.fallback_models is None else payload.fallback_models
        ),
    }


@router.get("/model")
def get_model_config() -> dict:
    return {
        "status": llm_status(),
        "presets": PROVIDER_PRESETS,
        "env_file": str(env_file()),
        "env_file_exists": env_file_exists(),
    }


@router.put("/model")
def save_model_config(payload: ModelConfigIn) -> dict:
    """保存并立即启用。写 .env 保证重启后仍在，热重载保证当前进程立刻生效。"""
    cfg = _resolve(payload)
    persist_model_config(**cfg)
    apply_model_config(**cfg)
    return {
        "ok": True,
        "status": llm_status(),
        "saved_to": str(env_file()),
        "message": "已保存并启用。下一次任务将使用新配置。",
    }


@router.post("/model/test")
async def test_model_config(payload: ModelConfigIn) -> dict:
    """连通性自检：真实发一条极短请求，验证 Key / 地址 / 模型名三者都对。

    配了备用模型时会一并探测 —— "配了但根本不可用"比"没配"更危险。
    """
    cfg = _resolve(payload)
    return await test_connection(
        provider=cfg["provider"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        model=cfg["model"],
        fallback_models=cfg["fallback_models"],
    )


@router.post("/model/models")
async def list_available_models(payload: ModelProbeIn) -> dict:
    """向服务商拉取当前可用的模型列表（GET /v1/models），避免模型名过时。"""
    api_key = payload.api_key.strip() if payload.api_key else settings.llm_api_key
    base_url = payload.base_url.strip() or settings.llm_base_url
    return await list_models(base_url=base_url, api_key=api_key)


# --------------------------------------------------------------------- 媒体配置


def _media_ready(kind: str, snap: dict) -> dict:
    """「配了」和「能用」是两件事 —— 这里给出可判定的结论，而不是让用户猜。"""
    if kind == "image":
        has_key = bool(snap.get("api_key_set") and snap.get("base_url"))
        provider = (snap.get("provider") or "auto").lower()
        if provider == "local":
            return {"ready": False, "mode": "local", "hint": "已强制本地兜底：出 SVG 分镜卡，不是真实画面"}
        if not has_key:
            return {"ready": False, "mode": "local", "hint": "未配置 Key / 接口地址：会用本地分镜卡兜底"}
        if provider == "jdcloud" and not snap.get("service"):
            return {"ready": False, "mode": "broken", "hint": "京东云需要填「预置服务标识」，否则无法调用"}
        return {"ready": True, "mode": "real", "hint": "真实生图已就绪"}
    if kind == "tts":
        provider = (snap.get("provider") or "auto").lower()
        if provider == "local":
            return {"ready": False, "mode": "local", "hint": "已强制本地语音（macOS say）"}
        if not (snap.get("api_key_set") and snap.get("base_url")):
            return {"ready": False, "mode": "local", "hint": "未配置 TTS：用本机语音，音色单一"}
        return {"ready": True, "mode": "real", "hint": "真实配音已就绪"}
    if kind == "video":
        # 权威判定交给 video_ready()：它知道 jdcloud 留空 Key 时会复用图片 Key，
        # 只看 api_key_set 会把"复用图片 Key"的正确配置误报成 broken
        from ..media.video import video_ready

        provider = (snap.get("provider") or "off").lower()
        if provider == "off":
            return {"ready": False, "mode": "off", "hint": "未启用：成片走静态图 + 运镜（零成本）"}
        ok, hint = video_ready()
        if not ok:
            return {"ready": False, "mode": "broken", "hint": hint}
        return {"ready": True, "mode": "real", "hint": "视频生成已就绪（按次计费，异步等待）"}
    return {"ready": False, "mode": "unknown", "hint": ""}


@router.get("/media")
def get_media_config() -> dict:
    """四类能力的当前配置。文本模型在 /settings/model，这里管图片 / 语音 / 视频。"""
    kinds: dict[str, dict] = {}
    for kind in MEDIA_SPEC:
        snap = media_config_snapshot(kind)
        kinds[kind] = {
            "kind": kind,
            "values": snap,
            "providers": MEDIA_PROVIDERS[kind],
            "ready": _media_ready(kind, snap),
            "env_keys": media_env_keys(kind),
        }
    return {
        "kinds": kinds,
        "env_file": str(env_file()),
        "env_file_exists": env_file_exists(),
        "notes": {
            "image": "支持 OpenAI 兼容 /v1/images/generations，以及京东云预置服务（同步返回图片 URL）",
            "tts": "支持 OpenAI 兼容 /v1/audio/speech；没配就用本机语音（音色单一，会如实标注）",
            "video": "视频为异步任务接口（京东云 task/submit 或万相 wan3.0 video-synthesis，二选一）：提交 → 轮询 → 下载。单片段按次计费，不会自动跑",
        },
    }


class MediaConfigIn(BaseModel):
    # 只传想改的字段；api_key 留空表示沿用已保存的（避免脱敏回显覆盖真 Key）
    values: dict = {}


@router.put("/media/{kind}")
def save_media_config(kind: str, payload: MediaConfigIn) -> dict:
    if kind not in MEDIA_SPEC:
        raise HTTPException(status_code=400, detail=f"不支持的媒体类别：{kind}")
    values = dict(payload.values or {})
    # 空提交必须拒绝。实测踩过：字段没包在 values 里时这里拿到空 dict，
    # persist 什么都不写、接口照样返回 ok=True —— 用户以为配好了，实际什么都没发生。
    if not values:
        raise HTTPException(
            status_code=400,
            detail='values 为空：配置字段要包在 {"values": {...}} 里提交（如 {"values": {"provider": "..."}}）',
        )
    provider = str(values.get("provider") or "").strip().lower()
    if provider and provider not in MEDIA_PROVIDERS[kind]:
        raise HTTPException(
            status_code=400,
            detail=f"{kind}.provider 只能是 {' / '.join(MEDIA_PROVIDERS[kind])}",
        )
    if values.get("api_key") in (None, ""):
        values.pop("api_key", None)   # 留空 = 沿用
    try:
        persist_media_config(kind, values)
        applied = apply_media_config(kind, values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    snap = media_config_snapshot(kind)
    return {
        "ok": True,
        "kind": kind,
        "applied": sorted(applied),
        "values": snap,
        "ready": _media_ready(kind, snap),
        "saved_to": str(env_file()),
        "message": "已保存并启用，下一次生成即生效（不改已完成的任务）",
    }


class MediaTestIn(BaseModel):
    # 图片 / 配音的测试会真的产生一次调用（会计费），所以必须显式确认
    confirm_cost: bool = False
    values: dict = {}


@router.post("/media/{kind}/test")
async def test_media_config(kind: str, payload: MediaTestIn = MediaTestIn()) -> dict:
    """连通性自检。

    三类能力的自检方式不同，这是刻意的：
    - 图片 / 配音：必须真发一次最小请求才能证明「模型名 + Key + 地址」三者都对
      （只看配置完整度会把 503 的模型判成可用）。因此要求 confirm_cost。
    - 视频：**绝不**为了测试而提交一个真实任务（按次计费且几分钟才出结果）。
      改为查一个不存在的任务 ID：鉴权失败会返回 401，鉴权通过会返回 404/参数错误。
    """
    if kind not in MEDIA_SPEC:
        raise HTTPException(status_code=400, detail=f"不支持的媒体类别：{kind}")
    values = {k: v for k, v in (payload.values or {}).items() if v not in (None, "")}
    if "api_key" not in values:
        values["api_key"] = None  # 用已保存的

    from ..media.probe import probe as run_probe

    try:
        return await run_probe(kind, values=values, confirm_cost=payload.confirm_cost)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
