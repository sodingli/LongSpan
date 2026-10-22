"""人脸区域相似度度量（FR-08 一致性抽检的度量层，只告警）。

为什么不引入 numpy/opencv/skimage（2026-10-22 决策）：
    本项目所在环境的硬约束是"能少一个依赖就少一个"（无 brew、无 Docker，
    依赖越少迁移越省事）。所需运算其实很小 —— 把人脸区域缩到 96×96 后，
    直方图相关与 SSIM 都是 O(像素) 的整数/浮点运算，纯 Python 在 12 格
    规模上是毫秒级。为人脸比对引进一个 20MB 的科学计算栈不划算。

两个度量为什么都要：
    - **颜色直方图**：对光照、构图不敏感，抓"肤色/发色/服装色"一致；
      但两个不同的人穿同色衣服也会高分（假阳性）。
    - **SSIM**：抓结构（五官位置/轮廓），但对光照和轻微位移敏感（假阴性）。
    两者相乘（几何平均）比任一单独用都稳：任一维度崩了分数就掉下来。

重要边界：**这是抽检，不是判决**。人脸区域框来自视觉模型，有抖动；
阈值只在"明显不对"时报警，宁漏报不误报 —— 误报会让用户对告警失去信任，
比不报更糟（路线图 W4 出口判据：误报率 <30%）。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PIL import Image

# 比对分辨率：人脸缩放到这个尺寸后算 SSIM。
# 96 是权衡 —— 太小丢失五官结构（不同人难区分），太大纯 Python 变慢且对
# 轻微位移更敏感（定妆图与分镜图的取景不会完全一致）。
FACE_SIZE = 96
# 颜色直方图的分箱（RGB 每通道）。
HIST_BINS = 16


def crop_box(img: Image.Image, box: Any) -> Image.Image | None:
    """按人脸框裁切；框非法返回 None。

    **坐标自适应（2026-10-22 实测踩坑）**：视觉模型返回的框有时是归一化
    （0~1），有时是像素坐标（对 720×1280 图返回 [231, 256, 468, 419]）——
    统一按归一化处理会把框算到画面外、裁出一片空白（表现为"人脸裁切失败"，
    10 格里 4 格误跳）。这里按数值量级判定：任一分量 > 1.5 即视为像素坐标，
    除以图宽高后再夹取。
    """
    if not box or not isinstance(box, (list, tuple)) or len(box) < 4:
        return None
    try:
        x, y, w, h = (float(v) for v in box[:4])
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    W, H = img.size
    if max(abs(x), abs(y), abs(w), abs(h)) > 1.5:  # 像素坐标 → 归一化
        x, w = x / W, w / W
        y, h = y / H, h / H
    left = max(0, min(W - 1, int(x * W)))
    top = max(0, min(H - 1, int(y * H)))
    right = max(left + 1, min(W, int((x + w) * W)))
    bottom = max(top + 1, min(H, int((y + h) * H)))
    if right - left < 8 or bottom - top < 8:  # 太小的框没有可比信息
        return None
    return img.crop((left, top, right, bottom))


def _to_gray_small(img: Image.Image) -> list[int]:
    g = img.convert("L").resize((FACE_SIZE, FACE_SIZE), Image.BILINEAR)
    return list(g.getdata())


def ssim(a: Image.Image, b: Image.Image) -> float:
    """结构相似度（0~1，1 = 完全相同）。纯 Python 实现，不做高斯加权。

    用均匀窗口（而非 11×11 高斯）对"同一张脸换个尺度"更宽容，代价是对
    极端噪声略敏感 —— 但输入是模型生成的人脸缩略图，噪声本就不占主导。
    """
    pa, pb = _to_gray_small(a), _to_gray_small(b)
    n = len(pa)
    if n == 0:
        return 0.0
    ma = sum(pa) / n
    mb = sum(pb) / n
    va = sum((v - ma) ** 2 for v in pa) / n
    vb = sum((v - mb) ** 2 for v in pb) / n
    cov = sum((pa[i] - ma) * (pb[i] - mb) for i in range(n)) / n
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    num = (2 * ma * mb + c1) * (2 * cov + c2)
    den = (ma ** 2 + mb ** 2 + c1) * (va + vb + c2)
    return max(0.0, min(1.0, num / den)) if den else 0.0


def _hist_small(img: Image.Image) -> list[float]:
    """RGB 直方图（每通道 16 箱，归一到 0~1）。"""
    rgb = img.convert("RGB").resize((FACE_SIZE, FACE_SIZE), Image.BILINEAR)
    counts = [0.0] * (3 * HIST_BINS)
    step = 256 / HIST_BINS
    for r, g, b in rgb.getdata():
        counts[int(r // step)] += 1.0
        counts[HIST_BINS + int(g // step)] += 1.0
        counts[2 * HIST_BINS + int(b // step)] += 1.0
    total = sum(counts) or 1.0
    return [c / total for c in counts]


def hist_similarity(a: Image.Image, b: Image.Image) -> float:
    """直方图相似度（Bhattacharyya 系数的补，0~1，1 = 分布完全相同）。"""
    ha, hb = _hist_small(a), _hist_small(b)
    bc = sum(math.sqrt(x * y) for x, y in zip(ha, hb))  # 0~1
    return max(0.0, min(1.0, bc))


def compare_faces(
    face_a: Image.Image, face_b: Image.Image
) -> dict[str, float]:
    """比对两张人脸裁切图，返回各度量与综合分（几何平均，任一维度差就拉低）。"""
    s = ssim(face_a, face_b)
    h = hist_similarity(face_a, face_b)
    return {
        "ssim": round(s, 4),
        "hist": round(h, 4),
        "score": round(math.sqrt(max(0.0, s) * max(0.0, h)), 4),
    }


def load_image(path: str | Path) -> Image.Image | None:
    try:
        img = Image.open(path)
        img.load()
        return img
    except Exception:  # noqa: BLE001
        return None
