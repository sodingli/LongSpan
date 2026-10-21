"""视频生成 Provider —— 把「一张图 + 一句话」变成一段能动的镜头。

**协议与生图/配音完全不同，必须分开看：**

- 生图 / 配音是**同步**接口，一次请求一个结果；
- 视频是**异步任务**接口：提交拿 task_id → 轮询 → 下载 mp4。

支持两套协议（按 `LONGSPAN_VIDEO_PROVIDER` 切换）：

┌──────────┬───────────────────────────────┬──────────────────────────────────┐
│ provider │ 提交                          │ 查询                             │
├──────────┼───────────────────────────────┼──────────────────────────────────┤
│ jdcloud  │ POST /task/submit             │ GET /task/{id}                   │
│          │ content 数组，role 声明用途    │ result.status / result.task_id   │
│ dashscope│ POST /services/aigc/video-    │ GET /tasks/{id}                  │
│ (万相)   │ generation/video-synthesis    │ output.task_status / video_url   │
│          │ input.media 数组，X-DashScope- │                                  │
│          │ Async: enable 必带            │                                  │
└──────────┴───────────────────────────────┴──────────────────────────────────┘

wan3.0-video（阿里云百炼 / DashScope 协议）要点（来自官方 API 参考，非猜测）：

- 请求体 `{"model", "input": {"prompt", "media": [...]}, "parameters": {...}}`；
- `media` 元素是 `{"type": ..., "url": ...}`，type 取值 first_frame / last_frame /
  reference_image / reference_video / reference_audio …；**first_frame/last_frame 与
  reference_* 互斥，同一请求不能混用**（与京东云不同！）——有参考视频时不传首尾帧；
- 图片支持公网 URL 或 base64 data URL（≤20MB）；参考视频只收公网 URL（≤100MB，≤15s）；
- `parameters.duration` 无视频输入时取值 [2,30]（默认 5）；resolution 480P/720P/1080P
  （**默认 1080P，直接影响费用**——未配置时这里兜底 480P 并在 note 里说明）；
- 提交响应 `{"output": {"task_id", "task_status": "PENDING"}}`，失败在顶层 code/message；
- 查询响应 `output.task_status`: PENDING/RUNNING/SUCCEEDED/FAILED/CANCELED/UNKNOWN，
  成功时 `output.video_url`（24 小时有效，必须立即下载转存）。

三条不能踩的线（都是真金白银换来的）：

1. **不做"探测式提交"。** 视频网关可能先受理、后由下游校验 —— 用非法参数试探会真的
   建出一个计费任务。所以参数在这里先做本地校验，宁可直接报错，也不把可疑请求发出去。
2. **必须退避轮询。** 官方建议 15 秒一查；本地默认 10 秒，但**总时长有上限**，
   超时即失败，绝不无限等。
3. **结果提取要宽容、失败原因要具体。** 各家返回字段名不一致，所以递归找第一个
   视频 URL；找不到就把原始响应截断回传。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger("longspan.media.video")

# 单帧图片转 data URL 的上限：再大就别硬塞 JSON 了，网关会先超时
# （dashscope 官方上限 20MB，这里沿用更保守的 6MB，本地帧图远用不到那么大）
_MAX_INLINE_BYTES = 6 * 1024 * 1024
# 时长白名单按 provider 区分 —— 网关会先受理再校验，非法值在这里挡住，不花探路钱
_ALLOWED_DURATIONS = {
    "jdcloud": {5, 10},
    # wan3.0：无视频输入时 [2,30]，默认 5；-1 是"智能时长"（交给模型定）
    "dashscope": set(range(2, 31)) | {-1},
}
_VIDEO_PROVIDERS = {"jdcloud", "dashscope"}


class VideoConfigError(ValueError):
    """配置或参数不合法 —— 这类错误在提交前就该暴露，不花一分钱。"""


def provider() -> str:
    return (settings.video_provider or "off").strip().lower()


def api_key() -> str:
    """视频 Key 允许留空复用图片 Key —— 同一网关同一把 Key 的场景，
    逼用户再填一遍只会多一个填错的机会。dashscope 是独立网关，必须有自己的 Key。"""
    key = (settings.video_api_key or "").strip()
    if not key and provider() == "jdcloud":
        key = (settings.image_api_key or "").strip()
    return key


def video_ready() -> tuple[bool, str]:
    prov = provider()
    if prov == "off":
        return False, "视频生成未启用（LONGSPAN_VIDEO_PROVIDER=off）"
    if prov not in _VIDEO_PROVIDERS:
        return False, f"暂不支持的视频 provider：{prov}（目前支持 {' / '.join(sorted(_VIDEO_PROVIDERS))}）"
    if not api_key():
        if prov == "dashscope":
            return False, "缺少视频 API Key（dashscope 网关与图片不同源，不能复用图片 Key）"
        return False, "缺少视频 API Key（留空时会复用图片 Key）"
    if not settings.video_base_url or not settings.video_model:
        return False, "缺少视频接口地址或模型名"
    return True, ""


def _data_url(abs_path: Path) -> str:
    size = abs_path.stat().st_size
    if size > _MAX_INLINE_BYTES:
        raise VideoConfigError(
            f"参考素材 {abs_path.name} 有 {size / 1024 / 1024:.1f}MB，超过内联上限 6MB。"
            "视频接口需要可公开访问的 URL —— 请改用「参考视频 URL」而不是本地上传。"
        )
    mime = mimetypes.guess_type(abs_path.name)[0] or "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(abs_path.read_bytes()).decode()


def _extra_params() -> dict:
    raw = (settings.video_params_json or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VideoConfigError(f"视频高级参数不是合法 JSON：{exc}") from exc
    if not isinstance(obj, dict):
        raise VideoConfigError("视频高级参数必须是 JSON 对象")
    return obj


def _model_entries() -> list[dict]:
    """解析 models_json 为 [{name, params, note}]。

    容错读：配置写坏了这里不抛（读取路径崩了连错误提示都出不来），
    不合格条目直接跳过 —— 保存入口的 _validate_models_field 已经挡过一次。
    """
    raw = (settings.video_models_json or "").strip()
    if not raw:
        return []
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return []
    out: list[dict] = []
    for item in obj if isinstance(obj, list) else []:
        if isinstance(item, str) and item.strip():
            out.append({"name": item.strip(), "params": {}, "note": ""})
        elif isinstance(item, dict) and str(item.get("name") or "").strip():
            out.append(
                {
                    "name": str(item["name"]).strip(),
                    "params": dict(item["params"]) if isinstance(item.get("params"), dict) else {},
                    "note": str(item.get("note") or "").strip(),
                }
            )
    return out


def model_options() -> list[dict]:
    """画布模型下拉的选项：全局主模型在前，models_json 里的可选模型随后（去重）。"""
    primary = (settings.video_model or "").strip()
    seen: set[str] = set()
    out: list[dict] = []
    if primary:
        out.append({"name": primary, "params": {}, "note": "全局默认"})
        seen.add(primary)
    for e in _model_entries():
        if e["name"] not in seen:
            out.append(e)
            seen.add(e["name"])
    return out


def _model_params(model: str) -> dict:
    """某个模型在 models_json 里声明的专属参数（如 happyhorse 的 ratio）。"""
    for e in _model_entries():
        if e["name"] == model:
            return dict(e.get("params") or {})
    return {}


def _validate_duration(dur: int) -> None:
    allowed = _ALLOWED_DURATIONS.get(provider())
    if not allowed:
        raise VideoConfigError(f"未启用的视频 provider 无法校验时长：{provider()}")
    if dur not in allowed:
        hi = max(v for v in allowed if v > 0)
        raise VideoConfigError(
            f"时长只支持 {min(v for v in allowed if v > 0)}–{hi} 秒（收到 {dur}）。"
            "这个值不是「随便填个大的试试」的地方——网关会先受理再校验。"
        )


def build_payload(
    *,
    prompt: str,
    duration: int | None = None,
    mode: str | None = None,
    first_frame: str | None = None,
    last_frame: str | None = None,
    reference_video: str | None = None,
    model: str | None = None,
) -> dict:
    """按当前 provider 的文档结构组装请求体。所有必填项在这里就校验完。"""
    prompt = (prompt or "").strip()
    if not prompt:
        raise VideoConfigError("视频提示词不能为空")
    prov = provider()
    if prov == "dashscope":
        return _build_dashscope(
            prompt=prompt, duration=duration, first_frame=first_frame,
            last_frame=last_frame, reference_video=reference_video, model=model,
        )
    return _build_jdcloud(
        prompt=prompt, duration=duration, mode=mode, first_frame=first_frame,
        last_frame=last_frame, reference_video=reference_video, model=model,
    )


def _build_jdcloud(
    *,
    prompt: str,
    duration: int | None,
    mode: str | None,
    first_frame: str | None,
    last_frame: str | None,
    reference_video: str | None,
    model: str | None,
) -> dict:
    dur = int(duration or settings.video_duration or 5)
    _validate_duration(dur)
    mode_val = (mode or settings.video_mode or "std").strip().lower()
    if mode_val not in {"std", "pro"}:
        raise VideoConfigError("mode 只能是 std（标准）或 pro（专家）")

    content: list[dict] = [{"type": "text", "text": prompt}]
    if first_frame:
        content.append({"type": "image_url", "role": "first_frame", "image_url": {"url": first_frame}})
    if last_frame:
        content.append({"type": "image_url", "role": "last_frame", "image_url": {"url": last_frame}})
    if reference_video:
        # role 不能省：Seedance 的 content 数组靠 role 区分「首帧 / 尾帧 / 参考素材」，
        # 只给 type 和 url 等于把素材丢进去却不声明用途，多半被忽略 —— 而任务照样受理计费。
        content.append({"type": "video_url", "role": "reference_video", "video_url": {"url": reference_video}})
        # 参考素材还必须在提示词里「指名」。官方指引：光传不提，模型不会真的参考它。
        if "参考视频" not in prompt:
            content[0]["text"] = (
                f"{prompt}\n\n参考参考视频 1 的镜头运动、节奏与整体风格；"
                "画面主体与首帧保持一致，不要照搬参考视频里的人物身份。"
            )

    chosen = (model or settings.video_model).strip()
    params: dict = {"mode": mode_val, "duration": dur}
    if settings.video_resolution:
        params["resolution"] = settings.video_resolution
    if settings.video_watermark is not None:
        params["watermark"] = bool(settings.video_watermark)
    params.update(_extra_params())
    params.update(_model_params(chosen))

    return {
        "model": chosen,
        "content": content,
        "parameters": params,
    }


def _build_dashscope(
    *,
    prompt: str,
    duration: int | None,
    first_frame: str | None,
    last_frame: str | None,
    reference_video: str | None,
    model: str | None,
) -> dict:
    """wan3.0-video 的请求体。

    互斥规则是硬约束：first_frame/last_frame 与 reference_* 不能同请求混用。
    参考视频优先 —— 它表达了更明确的意图（照着这段动）。
    """
    dur = int(duration or settings.video_duration or 5)
    _validate_duration(dur)

    media: list[dict] = []
    dropped: list[str] = []
    note = ""
    if reference_video:
        # 参考视频只收公网 URL（官方文档：mp4/mov，≤100MB、≤15s）——
        # 本地文件转 base64 只会被拒，提前说清楚比让网关报一串英文强
        if not reference_video.startswith("http"):
            raise VideoConfigError(
                "万相的参考视频必须是公网 URL（不收内联数据）。"
                "在画布上用「或粘贴公网视频 URL」填写，不要上传本地文件。"
            )
        # 参考素材必须在提示词里「指名」（官方指引：光传不提，模型不会真的参考它）
        if "参考视频" not in prompt:
            prompt = (
                f"{prompt}\n\n参考参考视频 1 的镜头运动、节奏与整体风格；"
                "不要照搬参考视频里的人物身份。"
            )
        media.append({"type": "reference_video", "url": reference_video})
        for role, label in ((first_frame, "首帧"), (last_frame, "尾帧")):
            if role:
                dropped.append(label)
        if dropped:
            note = "参考视频与首尾帧在万相协议里互斥，本单只用了参考视频；" + "、".join(dropped) + "已忽略"
    else:
        if first_frame:
            media.append({"type": "first_frame", "url": first_frame})
        if last_frame:
            media.append({"type": "last_frame", "url": last_frame})

    chosen = (model or settings.video_model).strip()
    params: dict = {"duration": dur}
    # resolution 默认 1080P，直接影响费用 —— 不配置就按最低档走，并把这件事说出来
    params["resolution"] = (settings.video_resolution or "480P").strip().upper() or "480P"
    if not settings.video_resolution:
        note = (note + "；" if note else "") + "未配置分辨率，已按最低档 480P 计费（可配 LONGSPAN_VIDEO_RESOLUTION）"
    if settings.video_watermark:
        params["watermark"] = True
    params.update(_extra_params())
    # 模型专属参数最后合并（如 happyhorse-1.1-t2v 的 ratio），优先级高于全局配置
    params.update(_model_params(chosen))
    # t2v 后缀 = 纯文生视频模型：不吃帧素材。直接不发（发了轻则被忽略、重则被拒），
    # 并明确告知而不是静默丢弃 —— 帧的一致性此时只能靠提示词锚点。
    if media and "t2v" in chosen.lower():
        roles = [m.get("type") for m in media]
        media.clear()
        note = (note + "；" if note else "") + (
            f"{chosen} 是文生视频模型，不吃帧素材（{('、'.join(roles))}已忽略）；"
            "人物/画风一致性由提示词锚点保证"
        )

    inp: dict = {"prompt": prompt}
    if media:
        inp["media"] = media
    return {
        "model": chosen,
        "input": inp,
        "parameters": params,
        # generate_clip 会 pop 掉 —— 提交请求里不能带私有字段
        "_note": note,
    }


async def submit_task(payload: dict) -> str:
    # `_note` 等下划线开头的键是 build 阶段留给上层的说明，不发给网关
    wire = {k: v for k, v in payload.items() if not str(k).startswith("_")}
    prov = provider()
    if prov == "dashscope":
        url = (
            settings.video_base_url.rstrip("/")
            + "/services/aigc/video-generation/video-synthesis"
        )
        headers = {
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
            # 缺了它直接报 "current user api does not support synchronous calls"
            "X-DashScope-Async": "enable",
        }
        ok_key, err_key = "output", "task_id"
    else:
        url = settings.video_base_url.rstrip("/") + "/task/submit"
        headers = {
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
            "Trace-id": f"longspan-{os.getpid()}-{int(asyncio.get_event_loop().time() * 1000) % 100000}",
        }
        ok_key, err_key = "result", "task_id"

    async with httpx.AsyncClient(timeout=90) as cli:
        resp = await cli.post(url, headers=headers, json=wire)
    text = resp.text[:600]
    if resp.status_code >= 400:
        raise RuntimeError(f"视频任务提交失败 HTTP {resp.status_code}：{text}")
    data = resp.json()
    if data.get("error") or data.get("code"):
        raise RuntimeError(f"视频任务提交被拒：{str(data.get('error') or data.get('message') or data.get('code'))[:300]}")
    if prov == "dashscope":
        task_id = (data.get("output") or {}).get("task_id")
    else:
        result = data.get("result") or {}
        task_id = result.get("task_id") or result.get("taskId") or data.get("task_id")
    if not task_id:
        raise RuntimeError(f"提交成功但返回里没有 task_id：{text}")
    logger.info("视频任务已提交 task_id=%s model=%s provider=%s", task_id, payload.get("model"), prov)
    return str(task_id)


def _find_video_url(node: Any) -> str | None:
    """递归找第一个 mp4/视频地址 —— 各家字段名不一样，按值找比按 key 猜稳。"""
    if isinstance(node, str):
        low = node.lower()
        if low.startswith("http") and any(ext in low for ext in (".mp4", ".mov", ".webm", "/video")):
            return node
        return None
    if isinstance(node, dict):
        # 显式字段优先，避免被封面图之类干扰
        for key in ("video_url", "videoUrl", "url", "output_url", "download_url"):
            val = node.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
        for val in node.values():
            found = _find_video_url(val)
            if found:
                return found
    if isinstance(node, list):
        for item in node:
            found = _find_video_url(item)
            if found:
                return found
    return None


_STATUS_OK = {"succeeded", "success", "completed", "done", "finished", "ok"}
_STATUS_FAIL = {"failed", "failure", "error", "cancelled", "canceled", "rejected", "unknown"}


def _is_real_error(err: Any) -> bool:
    """error 字段存在 ≠ 真报错。

    京东云在任务正常运行时也返回 `error: {code:0, type:"", message:""}` 占位，
    只看"有没有 error"会把每个 running 的任务判成失败。
    """
    if not err:
        return False
    if isinstance(err, dict):
        code = err.get("code")
        if code not in (None, 0, "0", ""):
            return True
        return bool(str(err.get("message") or err.get("type") or "").strip())
    return bool(str(err).strip())


async def poll_task(task_id: str) -> dict:
    """轮询直到出结果。返回 {status: succeeded|failed, video_url?, raw?, error?}。"""
    if provider() == "dashscope":
        url = settings.video_base_url.rstrip("/") + f"/tasks/{task_id}"
    else:
        url = settings.video_base_url.rstrip("/") + f"/task/{task_id}"
    interval = max(3, int(settings.video_poll_seconds or 10))
    deadline = asyncio.get_event_loop().time() + max(30, int(settings.video_timeout_seconds or 600))
    last: Any = None

    while True:
        try:
            async with httpx.AsyncClient(timeout=45) as cli:
                resp = await cli.get(url, headers={"Authorization": f"Bearer {api_key()}"})
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        except Exception as exc:  # noqa: BLE001
            # 网络抖动不等于任务失败 —— 继续等，直到超时
            logger.warning("轮询视频任务 %s 出错（继续等）：%s", task_id, exc)
            body = {}
            resp = None  # type: ignore[assignment]

        if resp is not None and resp.status_code >= 400:
            # 404 可能是任务过期（结果只保留 24h~90min）；其余按失败报
            return {"status": "failed", "error": f"查询任务失败 HTTP {resp.status_code}：{resp.text[:300]}"}

        last = body
        if provider() == "dashscope":
            out = (body or {}).get("output") or {}
            status = str(out.get("task_status") or "").lower()
            if status == "succeeded":
                video_url = out.get("video_url") or _find_video_url(body)
                if video_url:
                    return {"status": "succeeded", "video_url": video_url, "raw": body}
                return {"status": "failed", "error": f"任务已完成但没有视频地址：{json.dumps(body, ensure_ascii=False)[:400]}"}
            if status in _STATUS_FAIL:
                # unknown = 任务不存在或已过期，别傻等
                reason = out.get("message") or out.get("code") or body
                return {"status": "failed", "error": str(reason)[:500]}
        else:
            result = (body or {}).get("result") or body or {}
            if isinstance(result, dict):
                # 京东云把状态放在**顶层** task_status；其它网关可能放在 result.status|state
                status = str(
                    result.get("status")
                    or result.get("state")
                    or (body or {}).get("task_status")
                    or (body or {}).get("status")
                    or ""
                ).lower()
                if status in _STATUS_OK:
                    video_url = _find_video_url(body)
                    if video_url:
                        return {"status": "succeeded", "video_url": video_url, "raw": body}
                    return {"status": "failed", "error": f"任务已完成但没有视频地址：{json.dumps(body, ensure_ascii=False)[:400]}"}
                if status in _STATUS_FAIL:
                    reason = result.get("message") or result.get("reason") or body
                    return {"status": "failed", "error": str(reason)[:500]}
                error = result.get("error") or (body or {}).get("error")
                if _is_real_error(error):
                    return {"status": "failed", "error": str(error)[:500]}

        if asyncio.get_event_loop().time() > deadline:
            return {
                "status": "failed",
                "error": (
                    f"等待超过 {settings.video_timeout_seconds}s 仍未出结果（task_id={task_id}）。"
                    "任务可能仍在服务端排队，可稍后重新查询；本次不算成功。"
                ),
                "raw": last,
            }
        await asyncio.sleep(interval)


async def download(url: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".part" + dest.suffix)
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as cli:
        async with cli.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise RuntimeError(f"下载视频失败 HTTP {resp.status_code}")
            with tmp.open("wb") as fh:
                async for chunk in resp.aiter_bytes():
                    fh.write(chunk)
    if not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("下载到的视频为空")
    tmp.replace(dest)
    return dest.stat().st_size


def _abs_ref(rel: str | None, workspace: Path) -> str | None:
    """把相对路径 / URL 统一成可以塞进请求的值。

    图片会转成 base64 data URL（两套协议都支持）；参考视频必须保持公网 URL，
    转成 data URL 只会把请求撑爆并被网关拒掉。
    """
    if not rel:
        return None
    if re.match(r"^https?://", rel):
        return rel
    p = workspace / rel
    if not p.exists():
        raise VideoConfigError(f"参考素材不存在：{rel}")
    return _data_url(p)


async def generate_clip(
    workspace: Path,
    *,
    prompt: str,
    first_frame: str | None,
    last_frame: str | None,
    reference_video: str | None,
    duration: int | None,
    mode: str | None,
    out_rel: str,
    mid_frame: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """端到端生成一个片段并落盘。失败抛 RuntimeError（调用方负责写留痕）。

    mid_frame 只用于「让上层知道它被丢弃了」，不会进 payload ——
    这两套协议都没有中间帧位置，硬塞会被服务端拒或静默忽略。
    model 留空 = 全局主模型；画布可按镜头指定 models_json 里的任一模型。
    """
    ready, why = video_ready()
    if not ready:
        raise VideoConfigError(why)

    payload = build_payload(
        prompt=prompt,
        duration=duration,
        mode=mode,
        first_frame=_abs_ref(first_frame, workspace),
        last_frame=_abs_ref(last_frame, workspace),
        reference_video=reference_video if (reference_video and re.match(r"^https?://", reference_video)) else _abs_ref(reference_video, workspace),
        model=model,
    )
    task_id = await submit_task(payload)
    result = await poll_task(task_id)
    if result["status"] != "succeeded":
        raise RuntimeError(f"视频生成失败：{result.get('error')}")
    size = await download(result["video_url"], workspace / out_rel)
    used = [p for p in (first_frame, last_frame) if p]
    dropped = ["mid"] if mid_frame else []
    params = payload.get("parameters") or {}
    build_note = payload.pop("_note", "")  # build 阶段的说明（互斥丢弃 / 分辨率兜底）
    if dropped and not build_note:
        build_note = "视频模型只吃首帧/尾帧，中间帧不参与生成（已在画布上保留，可换用尾帧）"
    return {
        "path": out_rel,
        "task_id": task_id,
        "size": size,
        "video_url": result["video_url"],
        "model": payload["model"],
        "duration": params.get("duration"),
        # jdcloud 的 mode 是 std/pro，dashscope 没有 —— 用分辨率代替，前端照样有信息可显示
        "mode": params.get("mode") or params.get("resolution") or "",
        # 前端要的是"这段片用了哪些帧、参考了什么"，不是一个布尔值
        "frames": used,
        "reference": reference_video or "",
        "has_reference": bool(reference_video),
        "dropped_frames": dropped,
        "note": build_note,
    }
