"""Prove the frozen exe really ships the current source, at bytecode level.

File timestamps are not evidence: a rebuild that fails halfway, or an exe copied from an
earlier attempt, can easily be newer than the sources while still containing the old code.
That has already produced a delivery where the user was told a feature existed but the exe
predated it. So this reads the PYZ archive embedded in the onefile exe, unmarshals the real
code objects, and asserts on constants and call graphs that only the current build has.

Run with the isolated build interpreter:
    .venv-build/Scripts/python.exe outputs/verify_exe.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import CodeType

ROOT: Path = Path(__file__).resolve().parents[1]
EXE: Path = ROOT / "outputs" / "BestDPIFinder.exe"
PYZ_NAME: str = "PYZ.pyz"   # PyInstaller 6.x names the embedded archive PYZ.pyz

# Constants only the current build defines. Values are compared exactly, not loosely.
# Derived values (BLOCK_SECONDS = 3 x TASK_SECONDS) are computed at runtime and never appear
# as literals, so only source-level literals belong here.
REQUIRED_CONSTANTS: dict[str, object] = {
    "TASK_SECONDS": 20.0,
    "WINDOW_SECONDS": 2.0,
    "WINDOWS_PER_TASK": 10,
    "WARMUP_SECONDS": 2.0,
    "SCHEMA_VERSION": 6,
    "MAX_BLOCKS": 2,
    "HILL_FAST_FACTOR": 1.25,
    "HILL_SLOW_FACTOR": 0.8,
    "HILL_IMPROVEMENT": 2.0,
    "HILL_PROMOTE": 6.0,
    "SPEED_DECAY_SECONDS": 0.45,
    "HIT_TIMEOUT_SECONDS": 1.5,
}
# BUDGET_SECONDS is deliberately absent above: it is assigned from
# ``WARMUP_SECONDS + MAX_BLOCKS * BLOCK_SECONDS`` at import time, so 122.0 never appears as a
# literal in the bytecode and a value lookup can never find it. Its value is asserted in the
# unit suite instead; what this script can still prove about the 120-second budget is that no
# forbidden superseded marker survives (see FORBIDDEN_STRINGS).
# Copy introduced by the score-driven hill-climb design. Matched as substrings because the
# same phrase is embedded in longer sentences and f-strings. The two start-side phrases belong
# to ``AdaptiveController.describe``, which lives in adaptive, not in the UI module.
REQUIRED_STRINGS: dict[str, tuple[str, ...]] = {
    "adaptive": ("adaptive_120s_score_hill_climb", "旧版 50 秒（灵敏度按固定曲线单向扫动）",
                 "从快侧爬坡", "从慢侧爬坡", "块1 从快侧起步，块2 从慢侧起步"),
    "adaptive_ui": ("分数变好就沿当前方向继续走", "cm/360° 始终在"),
}
# Markers of superseded builds; any hit means the exe is stale.
FORBIDDEN_STRINGS: tuple[str, ...] = (
    "adaptive_90s_two_candidates",
    "adaptive_50s_in_task_sweep",
    "每项14秒",
    "90 秒活动预算已用完",
    # On-screen status text left over from the fixed-sweep model: it told the user the
    # sensitivity slides continuously and that each task is scored in 8 s windows, neither of
    # which is true now (score-driven steps, 20 s tasks). It is user-visible, so it is worth
    # blocking here rather than trusting a future reader to notice it.
    "每项8秒计分",
    "灵敏度任务内连续滑动",
)
# The clock fix: the window grid must be anchored once per stage on a single clock domain.
# The earlier build derived the task-local index by subtracting ``task_start`` (an
# active_elapsed value) from an absolute perf_counter boundary, which pinned the index at the
# last window and scored every window empty. ``finish_window`` is the feedback loop: it must
# score the window and feed the climber before the next window's input is decoded.
# The windowed build must route console output through ``emit``: an unguarded print raises
# when sys.stdout is None, and the windowed bootloader then blocks on a modal error box.
# ``finish_stage`` must build its tracking curve from ZERO_WINDOW_CURVE. The earlier build
# defaulted it to an empty tuple for the click/settle tasks, which fails TaskSample.validate
# at the very first task boundary and kills the session the moment a task ends. Checking for
# WINDOWS_PER_TASK would not catch that: the buggy function referenced it too, for ``gains``.
REQUIRED_CALLS: dict[str, tuple[str, ...]] = {
    "adaptive_ui:start_stage": ("window_grid_start", "task_start"),
    "adaptive_ui:finish_window": ("observe", "update_sensitivity", "finished_window_index"),
    "adaptive_ui:finish_stage": ("ZERO_WINDOW_CURVE",),
    "win32_app:run": ("emit", "LoadIconW"),
}
# Names the UI must import from adaptive; a missing import only fails at paint time.
REQUIRED_UI_IMPORTS: tuple[str, ...] = ("HILL_FAST_FACTOR", "HILL_SLOW_FACTOR", "HILL_IMPROVEMENT",
                                       "WINDOWS_PER_TASK")

# Every shipped module must be bytecode-identical to the source next to this script. This is
# the strongest freshness proof available: it subsumes the marker checks above, because a
# frozen module that matches the source cannot be missing anything the source defines. The
# markers are kept because they assert a different thing -- that the *source itself* still has
# the required properties, which a fingerprint cannot catch (edit source and rebuild and the
# two still match each other while the property is gone).
SOURCE_MODULES: tuple[str, ...] = ("adaptive", "adaptive_ui", "core", "native", "win32_app")


def fingerprint(code: CodeType, path: str = "") -> tuple:
    """Structural identity of a code object, ignoring filename and line numbers.

    Filenames and line numbers legitimately differ between the frozen build and a fresh
    compile, so they must not take part in the comparison; everything that determines
    behaviour does.
    """
    nested = tuple(fingerprint(item, f"{path}.{item.co_name}")
                   for item in code.co_consts if isinstance(item, CodeType))
    return (path, code.co_name, code.co_code, code.co_names, code.co_varnames, code.co_flags,
            tuple(item for item in code.co_consts if not isinstance(item, CodeType)), nested)


def first_difference(frozen: CodeType, fresh: CodeType, path: str = "") -> str | None:
    """Describe the first behavioural difference between two code trees, or None."""
    if fingerprint(frozen, path) == fingerprint(fresh, path):
        return None
    label: str = path or "<module>"

    def comparable(code: CodeType) -> tuple:
        """co_consts with nested code objects replaced by their fingerprints."""
        return tuple(fingerprint(item) if isinstance(item, CodeType) else item
                     for item in code.co_consts)

    for field in ("co_name", "co_code", "co_names", "co_varnames"):
        if getattr(frozen, field) != getattr(fresh, field):
            return f"{label} 的 {field} 不一致"
    if comparable(frozen) != comparable(fresh):
        return f"{label} 的 co_consts 不一致"
    frozen_children = {item.co_name: item for item in frozen.co_consts if isinstance(item, CodeType)}
    fresh_children = {item.co_name: item for item in fresh.co_consts if isinstance(item, CodeType)}
    for name in sorted(set(frozen_children) | set(fresh_children)):
        if name not in frozen_children:
            return f"{label} 缺少函数 {name}"
        if name not in fresh_children:
            return f"{label} 多出函数 {name}"
        deeper = first_difference(frozen_children[name], fresh_children[name], f"{label}.{name}")
        if deeper:
            return deeper
    return f"{label} 存在未归类的差异"


def load_module(exe: Path, module: str) -> CodeType:
    """Unmarshal one module's code object out of the exe's embedded PYZ archive."""
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(exe))
    code = archive.open_embedded_archive(PYZ_NAME).extract(module)
    if not isinstance(code, CodeType):
        raise SystemExit(f"{module} 在 exe 中不是代码对象。")
    return code


def walk(code: CodeType):
    """Yield every nested code object, so assertions cover function bodies too."""
    yield code
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            yield from walk(constant)


def constants(code: CodeType) -> set[object]:
    """All constants of every nested code object, keyed by value."""
    values: set[object] = set()
    for item in walk(code):
        for constant in item.co_consts:
            if isinstance(constant, (str, int, float)) and not isinstance(constant, bool):
                values.add(constant)
    return values


def function(code: CodeType, name: str) -> CodeType:
    for item in walk(code):
        if item.co_name == name:
            return item
    raise SystemExit(f"exe 字节码中找不到函数 {name}。")


def main() -> int:
    exe: Path = Path(sys.argv[1]) if len(sys.argv) > 1 else EXE
    if not exe.is_file():
        raise SystemExit(f"找不到交付文件：{exe}")
    modules: dict[str, CodeType] = {
        name: load_module(exe, name) for name in ("adaptive", "adaptive_ui", "win32_app")}
    failures: list[str] = []

    # Strongest check first: is the frozen module the source that sits next to this script?
    matched: int = 0
    for name in SOURCE_MODULES:
        source: Path = ROOT / f"{name}.py"
        if not source.is_file():
            failures.append(f"源码缺失：{source} 不存在，无法核对 exe 是否为当前源码")
            continue
        frozen: CodeType = modules.get(name) or load_module(exe, name)
        fresh: CodeType = compile(source.read_text(encoding="utf-8"), str(source), "exec")
        difference: str | None = first_difference(frozen, fresh)
        if difference:
            failures.append(f"exe 与源码不一致（{name}）：{difference}")
        else:
            matched += 1

    adaptive_values = constants(modules["adaptive"])
    for name, expected in REQUIRED_CONSTANTS.items():
        if expected not in adaptive_values:
            failures.append(f"常量缺失：{name}={expected!r} 不在 exe 的 adaptive 字节码中")

    for module, wanted in REQUIRED_STRINGS.items():
        present = {value for value in constants(modules[module]) if isinstance(value, str)}
        for text in wanted:
            if not any(text in value for value in present):
                failures.append(f"文案缺失：{module} 中找不到 {text!r}")

    ui_strings = {value for value in constants(modules["adaptive_ui"]) if isinstance(value, str)}
    for marker in FORBIDDEN_STRINGS:
        if any(marker in value for value in ui_strings):
            failures.append(f"发现过期标记：adaptive_ui 仍包含 {marker!r}，exe 可能是旧版本")

    for target, expected_names in REQUIRED_CALLS.items():
        module, _, name = target.partition(":")
        names = set(function(modules[module], name).co_names)
        for required in expected_names:
            if required not in names:
                failures.append(f"关键调用缺失：{target} 未引用 {required}")

    ui_names = set(modules["adaptive_ui"].co_names)
    for required in REQUIRED_UI_IMPORTS:
        if required not in ui_names:
            failures.append(f"导入缺失：adaptive_ui 未从 adaptive 导入 {required}")

    if failures:
        print("exe 字节码核对失败：", file=sys.stderr)
        for line in failures:
            print("  - " + line, file=sys.stderr)
        return 1
    print(f"exe 字节码核对通过：{exe.name}（{exe.stat().st_size} 字节）")
    print(f"  已逐函数比对 {matched}/{len(SOURCE_MODULES)} 个模块，与当前源码完全一致。")
    print(f"  另核对常量 {len(REQUIRED_CONSTANTS)} 项、文案 {sum(len(v) for v in REQUIRED_STRINGS.values())} 项、"
          f"调用图 {len(REQUIRED_CALLS)} 项、导入 {len(REQUIRED_UI_IMPORTS)} 项，"
          f"并确认 {len(FORBIDDEN_STRINGS)} 个过期标记均不存在。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
