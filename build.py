"""Build the Windows desktop executable using the active isolated environment."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


def main() -> int:
    """Run tests and freeze the app without installing packages globally."""
    if sys.platform != "win32":
        print("Windows 打包需要在 Windows 上运行。", file=sys.stderr)
        return 2
    if importlib.util.find_spec("PyInstaller") is None:
        print("请使用已在隔离环境安装 PyInstaller 的 Python 运行 build.py。", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parent
    icon = root / "assets" / "BestDPIFinder.ico"
    if not icon.is_file():
        raise RuntimeError(f"缺少软件图标：{icon}")
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
                    check=True, cwd=root)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
                    "--noupx", "--icon", str(icon), "--name", "BestDPIFinder",
                    "--distpath", str(root / "outputs"),
                    "--workpath", str(root / "build"), "--specpath", str(root),
                    str(root / "main.py")], check=True, cwd=root)
    executable = root / "outputs" / "BestDPIFinder.exe"
    if not executable.is_file():
        raise RuntimeError("打包未生成预期 exe。")
    print(f"已构建：{executable}\n独立运行，无需安装 Python。首次解包启动可能稍慢。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f"打包未成功：{error}", file=sys.stderr)
        raise SystemExit(1)
