"""产物存储适配器。

薄抽象：开发期本地文件系统，上线可替换为 S3 兼容对象存储，
业务代码只依赖这个接口（对应 MRD §13 落地约定）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings


class StorageAdapter:
    """本地实现。生产环境实现同签名的 S3 版本即可无缝替换。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- 路径 ----
    def run_workspace(self, run_id: str) -> Path:
        ws = self.root / run_id
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def abs_path(self, run_id: str, rel_path: str) -> Path:
        return self.run_workspace(run_id) / rel_path

    # ---- 写入返回 (rel_path, size) ----
    def write_text(self, run_id: str, rel_path: str, content: str) -> tuple[str, int]:
        p = self.abs_path(run_id, rel_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return rel_path, p.stat().st_size

    def write_json(self, run_id: str, rel_path: str, obj: Any) -> tuple[str, int]:
        return self.write_text(
            run_id, rel_path, json.dumps(obj, ensure_ascii=False, indent=2)
        )

    def write_bytes(self, run_id: str, rel_path: str, data: bytes) -> tuple[str, int]:
        p = self.abs_path(run_id, rel_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return rel_path, p.stat().st_size

    # ---- 读取 ----
    def read_text(self, run_id: str, rel_path: str) -> str:
        return self.abs_path(run_id, rel_path).read_text(encoding="utf-8")

    def exists(self, run_id: str, rel_path: str) -> bool:
        return self.abs_path(run_id, rel_path).exists()

    def list_files(self, run_id: str) -> list[str]:
        ws = self.run_workspace(run_id)
        return sorted(str(p.relative_to(ws)) for p in ws.rglob("*") if p.is_file())

    def list_entries(self, run_id: str) -> list[dict]:
        """列出工作区全部文件，带真实大小与修改时间。

        目录表的事实来源是**文件系统**而不是数据库：文件被重新生成、被人工替换之后，
        `artifacts` 表里的 size/mtime 就是过期值（曾出现成片重渲染后 DB 仍记旧体积）。
        这里一律实测；DB 只用来补"这个文件属于哪个阶段"。
        """
        ws = self.run_workspace(run_id)
        out: list[dict] = []
        for p in sorted(ws.rglob("*")):
            if not p.is_file():
                continue
            name = p.name
            if name.startswith(".") or name == "Thumbs.db":
                continue  # 不把系统垃圾文件混进交付清单
            st = p.stat()
            out.append(
                {
                    "path": str(p.relative_to(ws)),
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                }
            )
        return out


storage = StorageAdapter(settings.storage_root)
