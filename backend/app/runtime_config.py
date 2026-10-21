"""运行时模型配置：把「模型 / 接口」写进 backend/.env 并热启用。

为什么要有这个文件：
- 用户不该为了换个模型去翻代码或重启服务；
- 但配置又要真的持久化（下次启动还在），所以既改内存里的 settings，也回写 .env。

约定：
- 只管理 LONGSPAN_LLM_* 这几个键，其余行（注释、别的配置）原样保留；
- 不回显明文 Key；日志里也不打印。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .config import ENV_FILE, settings
from .llm import reset_provider

logger = logging.getLogger("longspan.runtime_config")

# settings 字段名 → .env 变量名
ENV_KEYS = {
    "llm_provider": "LONGSPAN_LLM_PROVIDER",
    "llm_base_url": "LONGSPAN_LLM_BASE_URL",
    "llm_api_key": "LONGSPAN_LLM_API_KEY",
    "llm_model": "LONGSPAN_LLM_MODEL",
    "llm_temperature": "LONGSPAN_LLM_TEMPERATURE",
    "llm_fallback_models": "LONGSPAN_LLM_FALLBACK_MODELS",
}

# 三类媒体配置的字段表（字段名, 环境变量名, 类型）。
# 为什么用同一张表驱动：新增一类媒体（比如 M3 的音乐）只要在这里加一行，
# 持久化 / 热启用 / 脱敏 / 校验全部自动可用，不用再写第三套代码。
MEDIA_SPEC: dict[str, list[tuple[str, str, type]]] = {
    "image": [
        ("provider", "LONGSPAN_IMAGE_PROVIDER", str),
        ("base_url", "LONGSPAN_IMAGE_BASE_URL", str),
        ("api_key", "LONGSPAN_IMAGE_API_KEY", str),
        ("model", "LONGSPAN_IMAGE_MODEL", str),
        ("service", "LONGSPAN_IMAGE_SERVICE", str),
        ("negative_prompt", "LONGSPAN_IMAGE_NEGATIVE_PROMPT", str),
        ("size", "LONGSPAN_IMAGE_SIZE", str),
        ("seed", "LONGSPAN_IMAGE_SEED", int),
        ("params_json", "LONGSPAN_IMAGE_PARAMS_JSON", str),
    ],
    "tts": [
        ("provider", "LONGSPAN_TTS_PROVIDER", str),
        ("base_url", "LONGSPAN_TTS_BASE_URL", str),
        ("api_key", "LONGSPAN_TTS_API_KEY", str),
        ("model", "LONGSPAN_TTS_MODEL", str),
        ("voice", "LONGSPAN_TTS_VOICE", str),
        ("speed", "LONGSPAN_TTS_SPEED", float),
        ("params_json", "LONGSPAN_TTS_PARAMS_JSON", str),
    ],
    "video": [
        ("provider", "LONGSPAN_VIDEO_PROVIDER", str),
        ("base_url", "LONGSPAN_VIDEO_BASE_URL", str),
        ("api_key", "LONGSPAN_VIDEO_API_KEY", str),
        ("model", "LONGSPAN_VIDEO_MODEL", str),
        ("models_json", "LONGSPAN_VIDEO_MODELS_JSON", str),
        ("duration", "LONGSPAN_VIDEO_DURATION", int),
        ("mode", "LONGSPAN_VIDEO_MODE", str),
        ("resolution", "LONGSPAN_VIDEO_RESOLUTION", str),
        ("watermark", "LONGSPAN_VIDEO_WATERMARK", bool),
        ("params_json", "LONGSPAN_VIDEO_PARAMS_JSON", str),
    ],
}

# 每类媒体在 Settings 上的字段前缀（image_* / tts_* / video_*）
MEDIA_PREFIX = {"image": "image_", "tts": "tts_", "video": "video_"}


_HEADER = (
    "# Longspan 模型配置（由「模型配置」面板自动生成，可手动编辑）\n"
    "# 模板与说明见 backend/.env.example\n"
)


def env_file() -> Path:
    return ENV_FILE


def env_file_exists() -> bool:
    return ENV_FILE.exists()


def persist_model_config(
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float | None = None,
    fallback_models: str | None = None,
) -> Path:
    """把模型配置合并写入 backend/.env（保留其它行与注释）。"""
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    if not existing:
        existing = [_HEADER.rstrip("\n"), ""]

    updates: dict[str, str] = {
        ENV_KEYS["llm_provider"]: provider,
        ENV_KEYS["llm_base_url"]: base_url,
        ENV_KEYS["llm_api_key"]: api_key,
        ENV_KEYS["llm_model"]: model,
    }
    if temperature is not None:
        updates[ENV_KEYS["llm_temperature"]] = str(temperature)
    if fallback_models is not None:
        updates[ENV_KEYS["llm_fallback_models"]] = fallback_models.strip()

    written: set[str] = set()
    out: list[str] = []
    for line in existing:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                written.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in written:
            out.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    logger.info("模型配置已写入 %s（provider=%s model=%s key=%s）",
                ENV_FILE, provider, model, "已设置" if api_key else "未设置")
    return ENV_FILE


def apply_model_config(
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float | None = None,
    fallback_models: str | None = None,
) -> None:
    """更新内存中的 settings 并重置 provider —— 下一次调用即生效，无需重启。"""
    settings.llm_provider = provider
    settings.llm_base_url = base_url
    settings.llm_model = model
    settings.llm_api_key = api_key
    if temperature is not None:
        settings.llm_temperature = temperature
    if fallback_models is not None:
        settings.llm_fallback_models = fallback_models.strip()
    reset_provider()


# --------------------------------------------------------------------- 媒体配置


def _coerce(kind: str, field: str, value: Any, typ: type) -> Any:
    """把前端传来的值转成 settings 期望的类型；转不了就抛 ValueError（不悄悄吞掉）。"""
    if typ is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off", ""}:
            return False
        raise ValueError(f"{kind}.{field} 需要布尔值，收到 {value!r}")
    if typ is int:
        try:
            return int(float(str(value).strip() or 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{kind}.{field} 需要整数，收到 {value!r}") from exc
    if typ is float:
        try:
            return float(str(value).strip() or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{kind}.{field} 需要数字，收到 {value!r}") from exc
    return str(value if value is not None else "").strip()


def _validate_json_field(kind: str, field: str, value: str) -> str:
    """高级参数是「合并进请求体的 JSON」，写错了要当场拒绝，不能等运行时才炸。"""
    text = (value or "").strip()
    if not text:
        return ""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{kind}.{field} 不是合法 JSON：{exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"{kind}.{field} 必须是 JSON 对象（{{...}}）")
    return json.dumps(obj, ensure_ascii=False)


def _validate_models_field(kind: str, field: str, value: str) -> str:
    """视频可选模型清单：JSON 数组，元素是模型名字符串或 {name, params?, note?} 对象。"""
    text = (value or "").strip()
    if not text:
        return ""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{kind}.{field} 不是合法 JSON：{exc}") from exc
    if not isinstance(obj, list):
        raise ValueError(f"{kind}.{field} 必须是 JSON 数组（[...]），如 [\"wan3.0-video\"]")
    norm: list = []
    for i, item in enumerate(obj, 1):
        if isinstance(item, str):
            if not item.strip():
                raise ValueError(f"{kind}.{field} 第 {i} 项是空模型名")
            norm.append(item.strip())
        elif isinstance(item, dict) and str(item.get("name") or "").strip():
            entry: dict = {"name": str(item["name"]).strip()}
            if isinstance(item.get("params"), dict) and item["params"]:
                entry["params"] = item["params"]
            if str(item.get("note") or "").strip():
                entry["note"] = str(item["note"]).strip()
            norm.append(entry)
        else:
            raise ValueError(
                f"{kind}.{field} 第 {i} 项既不是模型名字符串也不是含 name 的对象：{item!r}"
            )
    return json.dumps(norm, ensure_ascii=False)


def _env_updates_for(kind: str, values: dict[str, Any]) -> dict[str, str]:
    spec = MEDIA_SPEC.get(kind)
    if spec is None:
        raise ValueError(f"未知的媒体类别：{kind}")
    updates: dict[str, str] = {}
    for field, env_key, typ in spec:
        if field not in values:
            continue
        raw = values[field]
        if field == "params_json":
            updates[env_key] = _validate_json_field(kind, field, str(raw or ""))
            continue
        if field == "models_json":
            updates[env_key] = _validate_models_field(kind, field, str(raw or ""))
            continue
        val = _coerce(kind, field, raw, typ)
        if typ is bool:
            updates[env_key] = "true" if val else "false"
        else:
            updates[env_key] = str(val)
    return updates


def persist_media_config(kind: str, values: dict[str, Any]) -> Path:
    """把某一类媒体的配置合并写入 backend/.env（保留其它行与注释）。"""
    updates = _env_updates_for(kind, values)
    if not updates:
        return ENV_FILE
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    if not existing:
        existing = [_HEADER.rstrip("\n"), ""]

    written: set[str] = set()
    out: list[str] = []
    for line in existing:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                written.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in written:
            out.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    logger.info("媒体配置已写入 %s（%s：%s）", ENV_FILE, kind, ", ".join(sorted(written)))
    return ENV_FILE


def apply_media_config(kind: str, values: dict[str, Any]) -> dict[str, Any]:
    """更新内存中的媒体配置 —— 下一次生图 / 配音 / 生视频即生效，无需重启。"""
    spec = MEDIA_SPEC.get(kind)
    if spec is None:
        raise ValueError(f"未知的媒体类别：{kind}")
    prefix = MEDIA_PREFIX[kind]
    applied: dict[str, Any] = {}
    for field, _env, typ in spec:
        if field not in values:
            continue
        raw = values[field]
        if field == "params_json":
            val = _validate_json_field(kind, field, str(raw or ""))
        elif field == "models_json":
            val = _validate_models_field(kind, field, str(raw or ""))
        else:
            val = _coerce(kind, field, raw, typ)
        setattr(settings, prefix + field, val)
        applied[field] = val
    return applied


def media_config_snapshot(kind: str) -> dict[str, Any]:
    """当前生效的媒体配置（Key 脱敏，不回显明文）。"""
    spec = MEDIA_SPEC.get(kind)
    if spec is None:
        return {}
    prefix = MEDIA_PREFIX[kind]
    out: dict[str, Any] = {}
    for field, _env, typ in spec:
        val = getattr(settings, prefix + field, None)
        if field == "api_key":
            key = str(val or "")
            out["api_key_set"] = bool(key)
            out["api_key_masked"] = f"{key[:4]}…{key[-4:]}" if len(key) >= 12 else ("已设置" if key else "")
            continue
        out[field] = val if val is not None else ("" if typ is str else (False if typ is bool else 0))
    return out


def media_env_keys(kind: str) -> dict[str, str]:
    return {field: env for field, env, _t in MEDIA_SPEC.get(kind, [])}
