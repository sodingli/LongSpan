"""Longspan 后端配置。

设计原则（对应 MRD §13 存储落地约定）：
- 开发期 SQLite，生产 Postgres —— 仅切换 DATABASE_URL 即可
- 不写任何 Postgres 专有特性（JSON 用通用 JSON 类型、不用数组类型）

模型配置（M1）：
- 优先读环境变量 LONGSPAN_*，其次读 backend/.env 文件；
- 三处优先级：进程环境变量 > backend/.env > 代码默认值；
- 允许运行时通过 /api/settings/model 覆盖并回写 backend/.env（见 app/runtime_config.py），
  因此这里不再把值写死为 os.getenv 调用，交给 pydantic-settings 统一处理。
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent

# 模型/接口配置落在这里；前端「模型配置」面板保存时会写这个文件
ENV_FILE = BACKEND_DIR / ".env"


class Settings(BaseSettings):
    # ---- 基础 ----
    app_name: str = "Longspan"
    debug: bool = True

    # ---- 存储 ----
    # 开发期 SQLite；上线改 postgresql+psycopg://user:pass@host/db（业务代码零改动）
    database_url: str = f"sqlite:///{PROJECT_DIR / '.data' / 'longspan.db'}"
    # 产物存储根目录（StorageAdapter 的本地实现）
    storage_root: Path = PROJECT_DIR / ".data" / "artifacts"

    # ---- 技能 ----
    skills_dir: Path = BACKEND_DIR / "skills"

    # ---- 模型 ----
    # provider: auto（有 Key 自动走真实模型）/ mock（强制演示）/ openai-compat（强制真实）
    llm_provider: str = "auto"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    # 注意：DeepSeek 已于 2026-07 退役 deepseek-chat / deepseek-reasoner，
    # 现行模型名为 deepseek-flash（V4.1-Flash，1M 上下文）与 deepseek-v4-pro。
    # 前端「模型配置」面板支持拉取服务商可用模型列表，避免模型名过时。
    llm_model: str = "deepseek-flash"
    llm_timeout_seconds: int = 120
    # 备用模型链（主模型不可用时自动回退），逗号/分号/换行分隔，两种写法可混用：
    #   模型名                      → 同一个接口地址下的另一个模型
    #   模型名@https://xxx/v1       → 同时换接口地址（换服务商）
    # 实测教训：中转站会给某个模型名恒返 503（no available workers），
    # 而同系列另一个名字是通的 —— 有回退链就不至于整轮长任务白跑。
    llm_fallback_models: str = ""

    # ---- 媒体（M1：生图 / 配音）----
    # provider: auto（有 Key 走真实服务）/ local（强制本地实现）
    # 本地兜底是**真实可用的实现**，不是假产物：
    #   生图 → 程序化电影感分镜卡（SVG，画面上标注"占位图"）
    #   配音 → macOS say 神经语音合成（真实人声，Tingting 等）
    # 降级必须在产物与事件里标注 —— 禁止静默降级（架构铁律 #2）。
    # 生图：OpenAI 兼容 POST /v1/images/generations
    # provider=jdcloud 时走京东云 modelservice 的「预置服务」生图：
    #   POST {base_url}/images/{image_service}/generations（同步返回图片 URL）
    #   实测模型 image-01（MiniMax 海螺生图）；Seedream 需在控制台创建预置服务后填 image_service
    image_provider: str = "auto"
    image_api_key: str = ""
    image_base_url: str = ""
    image_model: str = ""
    image_service: str = ""
    # 生图参数（与模型解耦：换模型不必重调这些）
    # negative_prompt 只对支持的模型生效，不支持时后端会在事件里说明，不静默丢
    image_negative_prompt: str = ""
    image_size: str = ""            # 留空 = 按比例推导（9:16 → 1024x1792 等）
    image_seed: int = 0             # 0 = 不固定种子（每格不同）
    image_params_json: str = ""     # 高级：额外合并进请求体的 JSON 字段
    # 备用生图通道链（JSON 数组，主通道失败/审核拦截时依序切换，D15 + 审核三级处理）：
    #   [{"provider":"dashscope","base_url":"https://…/api/v1","api_key":"sk-…","model":"wan2.2-t2i-plus"},
    #    {"provider":"jdcloud","base_url":"https://…/v1","api_key":"pk-…","model":"image-01","service":"minimax_image01"}]
    # provider 支持 dashscope-mm（多模态同步，支持参考图）/ dashscope（异步任务）/ jdcloud / openai-compat
    image_fallbacks: str = ""
    # 通道路由：auto（带参考图的格优先走支持参考图的通道，保持相对顺序）/
    #           main（永远主通道优先，参考图不支持就如实标注未生效）
    image_routing: str = "auto"
    # ---- 视觉自检（生图后的多模态检查）----
    # 唯一「看图」的一层：文本校验只能防「话没说对」，防不了「画没画对」。
    # 实测事故：extreme-close 拍手+衬衫内袋 → 结构校验全过，但成图没有脑袋。
    # mode: auto（有 Key 就检）/ on（强制检，缺配置则如实报未检查）/ off（关闭）
    vision_check_mode: str = "auto"
    vision_api_key: str = ""        # 留空 = 复用 image_api_key（同一中转）
    vision_base_url: str = ""       # 留空 = 复用 image_base_url
    vision_model: str = "qwen3-vl-plus"
    # 检出构图残缺后自动重画的次数上限（0 = 只标注不重画；1 次足够，两次都画错多半是提示词问题）
    vision_retry_limit: int = 1
    # Remotion 渲染并发 tab 数：本机 macOS < 15 时高并发会部分 tab 连不上
    # （"Visited localhost:3000 but got no response"），实测 2 稳定
    render_concurrency: int = 2
    # 配音：OpenAI 兼容 POST /v1/audio/speech
    tts_provider: str = "auto"
    tts_api_key: str = ""
    tts_base_url: str = ""
    tts_model: str = "tts-1"
    tts_voice: str = "alloy"
    tts_speed: float = 1.0          # 全局语速倍率（每句台词还能各自覆盖）
    tts_params_json: str = ""       # 高级：额外合并进请求体的 JSON 字段

    # ---- 视频（M2：图生视频 / 参考视频生视频）----
    # provider: off（不启用，成片走静态图 + 运镜）/ jdcloud（京东云异步任务接口）
    # 京东云：POST {base_url}/task/submit → GET {base_url}/task/{task_id}
    # 这是**异步任务**接口，与文本/生图的同步接口不是一套协议，必须分开配。
    video_provider: str = "off"
    video_api_key: str = ""
    video_base_url: str = "https://modelservice.jdcloud.com/v1"
    video_model: str = "Doubao-Seedance-2.5"
    video_models_json: str = ""     # 可选模型清单（JSON 数组）：["m2", {"name": "m3", "params": {...}}]，画布可按镜头切换
    video_duration: int = 5         # 单个片段秒数（京东云枚举 5/10）
    video_mode: str = "std"         # std（标准）/ pro（专家，画质更好、更贵）
    video_resolution: str = ""      # 留空 = 用服务端默认
    video_watermark: bool = False
    video_params_json: str = ""     # 高级：额外合并进 parameters 的 JSON 字段
    video_poll_seconds: int = 10    # 轮询间隔（官方建议 ≥15 分钟查一次，本地按需调小）
    video_timeout_seconds: int = 600
    # 生成温度：长任务偏工程，默认低一点更稳
    llm_temperature: float = 0.7

    # ---- 配额治理（MRD §14 / 继承自 OpenMontage 的配额模式）----
    # 没有配额的自校验就是烧钱循环
    max_revisions_per_stage: int = 3      # 单阶段最多重做次数
    max_send_backs: int = 3               # 最多打回次数
    max_wall_time_minutes: int = 20       # 单 Run 墙钟上限（M0 演示用短值）
    budget_default_usd: float = 10.0      # 单 Run 预算上限
    budget_approval_threshold_usd: float = 0.5   # 单笔超过需审批

    # ---- 北极星指标（MRD §5.1 / 决策 D9）----
    # 新用户从开始到拿到第一份「愿意继续用下去」的成品，目标耗时上限。
    # 这个数不是口号：runner 会记录真实 TTFV，前端拿它做「达标 / 超标」判定。
    fast_channel_target_seconds: int = 180

    # ---- 服务器 ----
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    model_config = SettingsConfigDict(
        env_prefix="LONGSPAN_",
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        # 运行时会改 llm_* 字段（热切换模型），允许赋值
        validate_assignment=False,
    )


settings = Settings()

# 确保运行期目录存在
settings.storage_root.mkdir(parents=True, exist_ok=True)
(settings.storage_root.parent).mkdir(parents=True, exist_ok=True)
