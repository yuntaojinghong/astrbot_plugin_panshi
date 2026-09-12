"""把插件打包成可直接安装的压缩包。

用法:
    python scripts/build_release.py

输出（写入仓库根目录的 dist/）:
    dist/astrbot_plugin_panshi.zip      —— 解压即得插件文件夹，放入 AstrBot/data/plugins/
    dist/astrbot_plugin_panshi.tar.gz   —— 同样的内容，供 tar 用户

注意：压缩包外层套一层 astrbot_plugin_panshi/ 目录，符合 AstrBot 的安装习惯。
"""

from __future__ import annotations

import os
import tarfile
import zipfile

# 仓库根目录（本脚本位于 <repo>/scripts/ 下）
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_NAME = "astrbot_plugin_panshi"
SOURCE = REPO_ROOT
DIST = os.path.join(REPO_ROOT, "dist")

EXCLUDE_DIRS = {
    "__pycache__", ".git", ".idea", ".vscode", "dist", "scripts",
    "panshi_data", ".ruff_cache", ".pytest_cache",
}
EXCLUDE_FILES = {
    ".DS_Store", "Thumbs.db",
    "_selftest.py",   # 开发自测脚本，不随插件分发
    "_preview.html",  # 面板离线预览，仅开发期使用
    "logo.svg",       # 保留源矢量图不必要，分发 PNG 即可
}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".log", ".tmp")


def should_skip(name: str) -> bool:
    if name in EXCLUDE_DIRS or name in EXCLUDE_FILES:
        return True
    if name.startswith(".") and name != ".gitignore":
        return True
    return name.endswith(EXCLUDE_SUFFIX)


def collect() -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(SOURCE):
        dirs[:] = [d for d in dirs if not should_skip(d)]
        for f in files:
            if should_skip(f):
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(full, SOURCE)
            items.append((full, f"{PKG_NAME}/{rel}".replace("\\", "/")))
    return sorted(items, key=lambda x: x[1])


def main() -> None:
    os.makedirs(DIST, exist_ok=True)
    items = collect()

    zip_path = os.path.join(DIST, f"{PKG_NAME}.zip")
    tgz_path = os.path.join(DIST, f"{PKG_NAME}.tar.gz")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for full, arc in items:
            zf.write(full, arc)

    with tarfile.open(tgz_path, "w:gz") as tf:
        for full, arc in items:
            tf.add(full, arcname=arc)

    print(f"packaged {len(items)} files")
    for path in (zip_path, tgz_path):
        print(f"  {os.path.basename(path):36s} {os.path.getsize(path) / 1024:7.1f} KB")


if __name__ == "__main__":
    main()
