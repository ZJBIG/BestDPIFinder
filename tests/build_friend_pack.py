"""Assemble the end-user package: the verified exe plus a plain-language readme.

Separate from ``finalize_delivery.py`` on purpose. That script produces the evidence bundle for
the author (test logs, verification.json, bytecode proof). This one produces what a recipient
actually needs -- one exe and one readme -- because shipping a friend nine verification
artifacts they cannot interpret is noise, not diligence.

The exe hash is asserted against ``verification.json`` so an unverified build cannot be sent.

Run with the isolated build interpreter:
    .venv-build/Scripts/python.exe tests/build_friend_pack.py
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[1]
OUT: Path = ROOT / "outputs"
EXE: Path = OUT / "BestDPIFinder.exe"
README: Path = OUT / "pack-readme" / "使用说明.txt"
TARGET: Path = OUT / "BestDPIFinder-免安装版.zip"
FOLDER: str = "BestDPIFinder"
BOM: bytes = b"\xef\xbb\xbf"


def main() -> int:
    for required in (EXE, README):
        if not required.is_file():
            raise SystemExit(f"缺少打包所需文件：{required}")

    # Refuse to ship anything that has not passed bytecode verification against the current
    # source. A stale exe here would be handed to someone who cannot check it.
    record: dict = json.loads((OUT / "verification.json").read_text(encoding="utf-8"))
    live: str = hashlib.sha256(EXE.read_bytes()).hexdigest()
    if live != record["exe_sha256"]:
        raise SystemExit(f"exe 与 verification.json 记录不一致（{live}），请先重跑 finalize_delivery.py")
    if record.get("exe_matches_current_source") is not True:
        raise SystemExit("verification.json 未确认 exe 与当前源码一致，拒绝打包")

    # A BOM keeps older Windows Notepad from mis-detecting UTF-8 and showing mojibake, which
    # would make the instructions useless to exactly the person they are written for.
    text: str = README.read_text(encoding="utf-8")
    payload: bytes = BOM + text.encode("utf-8")

    if TARGET.exists():
        TARGET.unlink()
    with zipfile.ZipFile(TARGET, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(EXE, f"{FOLDER}/BestDPIFinder.exe")
        archive.writestr(f"{FOLDER}/使用说明.txt", payload)

    with zipfile.ZipFile(TARGET) as archive:
        if archive.testzip():
            raise SystemExit("压缩包完整性校验失败")
        names: list[str] = archive.namelist()
        if archive.read(f"{FOLDER}/使用说明.txt").decode("utf-8-sig") != text:
            raise SystemExit("说明文档经压缩往返后内容不一致")
    if any("/bin/" in name or "/obj/" in name for name in names):
        raise SystemExit("压缩包混入了 bin/ 或 obj/ 目录")

    print(f"已构建：{TARGET}")
    print(f"  条目：{names}")
    print(f"  大小：{TARGET.stat().st_size} 字节")
    print(f"  exe sha256：{live}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
