"""临时脚本：把 dist 里的压缩包解到临时目录，验证可正常安装与自测。

运行：python scripts/_installcheck.py
"""

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIP = os.path.join(ROOT, "dist", "astrbot_plugin_panshi.zip")
TGZ = os.path.join(ROOT, "dist", "astrbot_plugin_panshi.tar.gz")

# 必须包含的关键文件
REQUIRED = [
    "metadata.yaml",
    "main.py",
    "__init__.py",
    "_conf_schema.json",
    "README.md",
    "LICENSE",
    "pages/settings/index.html",
    "pages/settings/app.js",
    "pages/settings/style.css",
    "pages_api.py",
    "pages_service.py",
    "data/group_cache.py",
]

# 不应进包的文件
FORBIDDEN = ["_selftest.py", "scripts/build_release.py", "dist/", ".git/"]


def check_zip():
    with zipfile.ZipFile(ZIP) as zf:
        names = [n.replace("\\", "/") for n in zf.namelist()]
        zf.extractall(_TMP)
    return names


def check_tgz():
    with tarfile.open(TGZ, "r:gz") as tf:
        names = [n.replace("\\", "/") for n in tf.getnames()]
    return names


def main():
    assert os.path.exists(ZIP), f"缺少 {ZIP}"
    assert os.path.exists(TGZ), f"缺少 {TGZ}"

    names = check_zip()
    print(f"ZIP_ENTRIES {len(names)}")

    for req in REQUIRED:
        assert any(n.endswith(req) for n in names), f"缺少 {req}"
    print(f"REQUIRED_OK ({len(REQUIRED)})")

    for bad in FORBIDDEN:
        assert not any(bad in n for n in names), f"不该包含 {bad}"
    print("EXCLUDE_OK")

    names2 = check_tgz()
    assert len(names2) == len(names), (len(names2), len(names))
    print(f"TGZ_OK ({len(names2)})")

    # 解压出来的目录跑一遍自测
    pkg = None
    for d in os.listdir(_TMP):
        full = os.path.join(_TMP, d)
        if os.path.isdir(full) and os.path.exists(os.path.join(full, "metadata.yaml")):
            pkg = full
            break
    assert pkg, f"解压后未找到插件根目录: {os.listdir(_TMP)}"

    st = os.path.join(pkg, "_selftest.py")
    if os.path.exists(st):
        r = subprocess.run(
            [sys.executable, st],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=pkg,
        )
        out = (r.stdout or "") + (r.stderr or "")
        assert "ALL_SELFTEST_PASS" in out, out[-2000:]
        print("PACKAGED_SELFTEST_OK")

    print("INSTALL_TEST_PASS")


if __name__ == "__main__":
    _TMP = tempfile.mkdtemp(prefix="panshi_install_")
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
