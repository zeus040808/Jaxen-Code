"""为 NiceEval 打上 Windows 目录 fsync 兼容补丁（幂等）。

## 为什么需要

Windows 的 ``FlushFileBuffers`` 不支持目录句柄，Node 对目录调用 ``fsync``
必然返回 ``EPERM``。NiceEval 0.13.1 在两处对目录做了**不可失败**的 fsync，
导致整个 ``niceeval exp`` 在 Windows + Node 26 下无法启动：

    RecordPermissionError: { "operation": "sync-directory",
                             "cause": Error: EPERM: operation not permitted, fsync }

该库自己在 ``dist/shared/entry-file-store.cjs`` 里已经处理过同一问题：

    /** 目录 fsync,尽力而为——部分平台/文件系统不支持目录 fsync(如 Windows),静默降级。 */

本脚本把同样的降级补到漏掉的两处。降级的代价仅是极端断电场景下
lease/record 目录项可能未落盘，对评测无实质影响。

## 用法

    python scripts/patch_niceeval_windows.py          # 打补丁
    python scripts/patch_niceeval_windows.py --check  # 只检查状态，不修改

每次 ``pnpm install`` 之后需要重跑。等 pnpm 可用后，应改用
``pnpm patch niceeval@0.13.1`` 生成正规补丁并写入
``package.json`` 的 ``pnpm.patchedDependencies``，届时本脚本可以删除。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MARKER = "HH patch: 目录 fsync"
DEGRADE = ".pipe(effect_1.Effect.catchAll(() => effect_1.Effect.void))"

REPO_ROOT = Path(__file__).resolve().parents[1]


class Target:
    """一处需要打补丁的位置。"""

    def __init__(self, relative_path, anchor, note):
        self.relative_path = relative_path
        self.anchor = anchor
        self.note = note


TARGETS = (
    Target(
        "dist/coordination/platform/node.cjs",
        "function syncDirectory(path) {",
        "coordination 层：租约目录提交",
    ),
    Target(
        "dist/record/platform/node.cjs",
        "// Durable publication requires an explicit directory fsync.",
        "record 层：记录目录提交",
    ),
)


def find_package_root():
    """定位 pnpm 展开后的 niceeval 包目录。"""

    candidates = sorted(
        REPO_ROOT.glob("node_modules/.pnpm/niceeval@*/node_modules/niceeval")
    )
    if not candidates:
        direct = REPO_ROOT / "node_modules" / "niceeval"
        if direct.is_dir():
            return direct
        raise SystemExit("找不到 niceeval 包，请先安装依赖")
    if len(candidates) > 1:
        raise SystemExit(
            "发现多个 niceeval 版本，请确认依赖树后再手动处理：\n  "
            + "\n  ".join(str(path) for path in candidates)
        )
    return candidates[0]


def locate_return_statement(source, anchor):
    """返回 anchor 之后第一条 ``return Effect.scoped(...)`` 语句的区间。"""

    anchor_at = source.find(anchor)
    if anchor_at == -1:
        return None
    start = source.find("return effect_1.Effect.scoped(", anchor_at)
    if start == -1:
        return None
    end = source.find("\n", start)
    if end == -1:
        return None
    return start, end


def patch_one(package_root, target, apply_changes):
    path = package_root / target.relative_path
    if not path.is_file():
        return "missing", f"{target.relative_path} 不存在"

    source = path.read_text(encoding="utf-8")
    if MARKER in source:
        return "already", f"{target.relative_path} 已打补丁"

    span = locate_return_statement(source, target.anchor)
    if span is None:
        return "anchor-missing", f"{target.relative_path} 未找到锚点（版本可能已变）"

    start, end = span
    statement = source[start:end].rstrip()
    if not statement.endswith(";"):
        return "unexpected", f"{target.relative_path} 语句形态异常，已跳过"
    if DEGRADE in statement:
        return "already", f"{target.relative_path} 已包含降级"

    patched = statement[:-1] + DEGRADE + ";"
    comment = f"    // {MARKER}：{target.note}。Windows 不支持目录 fsync，静默降级。\n"
    updated = source[:start] + comment + "    " + patched + source[end:]

    if apply_changes:
        backup = path.with_suffix(path.suffix + ".hh-orig")
        if not backup.exists():
            backup.write_text(source, encoding="utf-8", newline="")
        path.write_text(updated, encoding="utf-8", newline="")
    return "patched", f"{target.relative_path} {'已打补丁' if apply_changes else '需要打补丁'}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="只报告状态，不修改任何文件（供 CI 使用）",
    )
    args = parser.parse_args(argv)

    package_root = find_package_root()
    print(f"niceeval: {package_root.relative_to(REPO_ROOT)}")

    outcomes = []
    for target in TARGETS:
        status, message = patch_one(package_root, target, apply_changes=not args.check)
        outcomes.append(status)
        print(f"  [{status:14s}] {message}")

    broken = any(status in {"anchor-missing", "unexpected", "missing"} for status in outcomes)
    if broken:
        print("\n补丁未能完整应用。NiceEval 版本可能已升级，请重新核对上游实现。")
        # 作为 postinstall 钩子运行时绝不能阻断 pnpm install——依赖装不上
        # 比缺一个平台兼容补丁严重得多。只有显式 --check 才把它当失败。
        return 1 if args.check else 0
    if args.check and "patched" in outcomes:
        print("\n检查未通过：仍有位置需要打补丁。运行 python scripts/patch_niceeval_windows.py")
        return 1
    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
