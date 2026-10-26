"""未定义名静态检查（自建小 lint）：专抓"名字写错/漏 import"这类必崩的错。

为什么要自己写：本项目 2026-10-23 踩过一次 —— `runner._run_stage` 里把
`panel_skeleton.render_table` 写成了 `skeleton.render_table`，`py_compile` 与
`ast.parse` 都通过，但真实跑批在 storyboard 阶段 NameError，整轮失败。
机器上没有 pyflakes，也装不了（无网/无 pip 权限），所以写一个够用的最小实现：

- 只看模块级：把所有 import / 模块级赋值 / 函数与类定义 / 内置名收集成"已知"；
- 对每个函数：参数、赋值目标、for/with/comprehension 变量都算局部名；
- 其余出现在 Load 位置的名字，若不在已知集合里 → 报"未定义名"。

刻意保守（宁可漏报不误报）：只检查 backend/app 下的项目代码，不检查第三方；
遇到 `from x import *` 的模块直接跳过。
"""
from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bound_names(node: ast.AST) -> set[str]:
    """函数体内所有被绑定的名字（赋值/for/with/except/comprehension/嵌套定义）。"""
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.arg):
            out.add(sub.arg)
        elif isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
            out.add(sub.id)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(sub.name)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            out.add(sub.name)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for alias in sub.names:
                out.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(sub, ast.Global):
            out.update(sub.names)
    return out


def check_file(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return [f"{path}: 语法错误 {exc}"]

    module_names: set[str] = set(dir(builtins)) | {
        "__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__",
    }
    star_import = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                module_names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if any(a.name == "*" for a in node.names):
                star_import = True
            for a in node.names:
                module_names.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            module_names.add(node.id)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    module_names.add(t.id)
    if star_import:
        return []

    problems: list[str] = []
    # 闭包：内层函数能看见外层函数的局部名（run_id / ctx / timeout 这类实测误报都源于此）。
    # 先建父子表，检查某函数时把它所有外层函数的绑定名一并算进"已知"。
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing_bindings(fn: ast.AST) -> set[str]:
        names: set[str] = set()
        cur = parents.get(fn)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names |= _bound_names(cur)
            cur = parents.get(cur)
        return names

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = _bound_names(fn) | enclosing_bindings(fn)
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id not in local and sub.id not in module_names:
                    problems.append(
                        f"{path.relative_to(ROOT)}:{sub.lineno} 未定义名 `{sub.id}`（在 {fn.name} 内）"
                    )
    return problems


def main() -> int:
    targets = sorted((ROOT / "app").rglob("*.py")) + sorted((ROOT / "scripts").rglob("*.py"))
    all_problems: list[str] = []
    for path in targets:
        all_problems.extend(check_file(path))
    if all_problems:
        for p in all_problems:
            print("[FAIL]", p)
        print(f"\n{len(all_problems)} 处问题")
        return 1
    print(f"通过：{len(targets)} 个文件未发现未定义名")
    return 0


if __name__ == "__main__":
    sys.exit(main())
