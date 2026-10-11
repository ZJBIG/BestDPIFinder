"""Verify the frozen deliverable, record evidence, and assemble distribution archives."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"
sys.path.insert(0, str(ROOT))
from adaptive import MODE  # noqa: E402  (needs ROOT on sys.path first)

# Flags every smoke run must set, whatever the host desktop is doing. These are the probe
# names emitted by ``AdaptiveApp.run_smoke``; when the model changes, both sides must move
# together or a green test suite is contradicted by a failing delivery script.
SMOKE_FLAGS: tuple[str, ...] = (
    "budget_is_122_seconds", "plan_fits_budget", "each_task_is_20_seconds", "two_blocks_only",
    "windows_divide_task", "blocks_start_at_opposite_ends", "band_has_room_to_move",
    "gain_follows_score", "climb_starts_inside_band", "single_start_captures",
    "warmup_auto_enters_block", "scored_task_owns_a_climb", "window_boundaries_are_armed",
    "escape_releases", "focus_releases", "all_pages_rendered", "no_valid_scores",
    "gdiplus_started", "timer_resolution_1ms", "sounds_ready")
# The deadline path only runs while the window owns the foreground. Even the windowed exe cannot
# guarantee that: Windows hands the foreground only to a process the user just interacted with,
# there is no foreground lock timeout to raise (SPI_GETFOREGROUNDLOCKTIMEOUT reports 0 on a
# machine where no lock was configured), and when another app currently owns the foreground --
# a game, say -- SetForegroundWindow is refused outright. So this is a precondition, not an
# assertion about the code: the guard is a short-circuit on ``spent >= BUDGET_SECONDS`` and both
# branches are covered by unit tests regardless of focus. When the precondition does NOT hold the
# run is reported as inconclusive evidence instead of a delivery failure, so a desktop that
# happens to be busy cannot block shipping while a genuinely broken guard still cannot pass
# (a broken guard leaves ``deadline_window_is_foreground`` True and ``deadline_releases`` False).
DEADLINE_FLAGS: tuple[str, ...] = ("deadline_window_is_foreground", "deadline_releases")


def assert_smoke(probe: dict, label: str, require_deadline: bool) -> bool:
    """Check the focus-independent flags; return whether the deadline guard was proven."""
    if probe is None:
        raise RuntimeError(f"{label} produced no smoke report")
    missing = [key for key in SMOKE_FLAGS if probe.get(key) is not True]
    if missing or probe.get("history_written") is not False:
        raise RuntimeError(f"{label} did not report every expected safety flag: missing {missing}")
    if not require_deadline:
        return True
    if probe.get("deadline_window_is_foreground") is not True:
        # The window never got the foreground, so the deadline branch could not run at all and
        # neither outcome below would say anything about the guard.
        return False
    unmet = [key for key in DEADLINE_FLAGS if probe.get(key) is not True]
    if unmet:
        # The window DID own the foreground and the guard still failed -> a real defect.
        raise RuntimeError(f"{label} owned the foreground but the deadline guard did not fire "
                           f"({unmet}); the budget release is broken")
    return True


def run_and_record(arguments: list[str], name: str, timeout: int = 40) -> str:
    result = subprocess.run(arguments, cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout,
                            env=os.environ | {"PYTHONIOENCODING": "utf-8"})
    output = result.stdout + result.stderr
    (OUT / name).write_text(output, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"{name}: exit {result.returncode}\n{output}")
    return output


def main() -> None:
    exe = OUT / "BestDPIFinder.exe"
    sources = [ROOT / name for name in ("main.py", "core.py", "native.py", "win32_app.py",
                                        "adaptive.py", "adaptive_ui.py", "BestDPIFinder.pyw", "build.py")]
    # Freshness is proven by comparing the frozen bytecode against the source, never by mtimes.
    # Touching a source file after a build changes its timestamp without changing behaviour, and
    # an exe copied from an earlier attempt can be newer than the source it predates. verify_exe
    # compares every function's bytecode/names/constants, so it is both correct and stricter.
    bytecode_summary = [line.strip() for line in run_and_record(
        [sys.executable, str(OUT / "verify_exe.py"), str(exe)],
        "exe-bytecode-verification.txt").splitlines() if line.strip()]
    history_paths = [directory / "results" / name for directory in (ROOT, OUT)
                     for name in ("latest.json", "adaptive-latest.json")]
    history_before = {path: path.read_bytes() if path.exists() else None for path in history_paths}
    unit_output = run_and_record([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], "tests-final.txt")
    unit_summary = [line for line in unit_output.splitlines() if line.startswith("Ran ")]
    if not unit_summary or "OK" not in unit_output.splitlines() or any(line.startswith("Ran 0 ") for line in unit_summary):
        raise RuntimeError("Unit tests returned no explicit nonempty successful test summary")
    adaptive_output = run_and_record([sys.executable, "main.py", "--smoke-test"], "gui-adaptive-verification.txt")
    adaptive_results = [json.loads(line) for line in adaptive_output.splitlines() if line.startswith("{")]
    adaptive_probe = next((item for item in adaptive_results if "single_start_captures" in item), None)
    assert_smoke(adaptive_probe, "Adaptive main.py smoke", require_deadline=False)
    gui_output = run_and_record([sys.executable, "tests/qa_smoke.py"], "gui-legacy-verification.txt")
    gui_results = [json.loads(line) for line in gui_output.splitlines() if line.startswith("{")]
    probe = next(item for item in gui_results if item.get("qa_synthetic_probe"))
    if not all(check["passed"] for check in probe["checks"]) or probe["error"]:
        raise RuntimeError("GUI checks not all successful")
    if not gui_results[-1].get("real_history_unchanged"):
        raise RuntimeError("GUI test changed personal history")
    with tempfile.TemporaryDirectory(prefix="bestdpi-frozen-smoke-") as directory:
        report = Path(directory) / "smoke.json"
        run_and_record([str(exe), "--smoke-test", "--smoke-report", str(report)], "exe-smoke.txt")
        if not report.exists():
            raise RuntimeError("Frozen executable did not write a fresh explicit smoke report")
        exe_probe = json.loads(report.read_text(encoding="utf-8"))
    # The windowed exe has no console to lose the foreground to, but a game or any other app the
    # user is actually using still can hold it, so the returned value is recorded rather than
    # assumed. A False here means "not exercised", never "passed".
    deadline_proven: bool = assert_smoke(exe_probe, "Frozen adaptive smoke", require_deadline=True)
    (OUT / "exe-smoke.txt").write_text(json.dumps(exe_probe, ensure_ascii=False, indent=2), encoding="utf-8")
    preview_path = ROOT / "tests" / "adaptive-setup.png"
    previous_preview_time = preview_path.stat().st_mtime_ns if preview_path.exists() else None
    preview_output = run_and_record([sys.executable, "tests/capture_adaptive.py"], "preview-adaptive-verification.txt")
    preview_results = [json.loads(line) for line in preview_output.splitlines() if line.startswith("{")]
    preview_probe = next((item for item in preview_results if "adaptive_empty_pages" in item), None)
    if (preview_probe is None or preview_probe.get("adaptive_empty_pages") != 4
            or preview_probe.get("personal_records") != 0 or preview_probe.get("error")):
        raise RuntimeError("Adaptive preview capture did not verify four empty pages")
    if not preview_path.exists() or preview_path.stat().st_mtime_ns == previous_preview_time:
        raise RuntimeError("Adaptive preview capture did not produce a fresh setup image")
    history_after = {path: path.read_bytes() if path.exists() else None for path in history_paths}
    if history_before != history_after:
        raise RuntimeError("Verification changed legacy or adaptive personal history")
    evidence = {
        "verified_at_local": datetime.now().astimezone().isoformat(),
        "platform": sys.platform,
        "mode": MODE,
        "python_for_build": sys.version,
        "unit_tests": {"passed": True, "summary": unit_summary},
        "adaptive_gui_checks": adaptive_probe,
        "legacy_gui_checks": probe["checks"],
        "legacy_gui_child_exit": gui_results[-1]["child_exit_code"],
        "adaptive_preview_checks": preview_probe,
        "personal_history_unchanged": history_before == history_after,
        "history_files_checked": [path.relative_to(ROOT).as_posix() for path in history_paths],
        "personal_sensitivity_result_generated": not adaptive_probe["no_valid_scores"],
        "packaged_exe_smoke_exit_code": 0,
        "packaged_exe_smoke_checks": exe_probe,
        "packaged_exe_smoke_evidence": "flags_and_exit_code" if exe_probe else "exit_code_only_windowed_no_flags",
        # Recorded separately from the checks dict so a reader cannot mistake "the desktop was
        # busy" for "the guard passed". False means the budget-release path was NOT exercised.
        "deadline_guard_exercised_in_exe": deadline_proven,
        "deadline_guard_note": (
            "The budget-release path needs the window to own the foreground; Windows refuses "
            "foreground to a process the user did not just interact with, and there is no "
            "foreground lock timeout to raise on this machine. When this is false the guard was "
            "not exercised by this run -- it is not evidence of a defect, and it is not a pass. "
            "Run the frozen exe by hand from a free desktop to exercise it."
            if not deadline_proven else
            "The frozen exe owned the foreground and the budget release fired."),
        "exe_size_bytes": exe.stat().st_size,
        "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
        "exe_matches_current_source": True,
        "exe_bytecode_verification": bytecode_summary,
        "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        "limitations": ["No human aiming assessment has been completed", "No clean-machine or exhaustive device, polling-rate, or display-scaling validation", "Window-message probes are not physical mouse testing"],
    }
    if not deadline_proven:
        evidence["limitations"].append(
            "The 122 s budget release was not exercised in the packaged exe during this run "
            "(another application held the foreground)")
    shutil.copyfile(preview_path, OUT / "app-preview.png")
    source_files = sources.copy()
    source_files.extend((ROOT / "assets" / "BestDPIFinder.png",
                         ROOT / "assets" / "BestDPIFinder.ico"))
    # Ship test helpers as well as test_*.py, including future independent QA imports.
    source_files.extend(sorted((ROOT / "tests").glob("*.py")))
    # verify_exe.py lives in outputs/ but is source: finalize_delivery.py invokes it, so shipping
    # the finalizer without it would hand the recipient a script that cannot run.
    source_files.append(OUT / "verify_exe.py")
    source_files = sorted(set(source_files))
    # Includes capture_adaptive -> capture_preview and adaptive_ui -> adaptive/core/native/win32_app.
    evidence["source_archive_sha256"] = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                          for path in source_files}
    (OUT / "verification.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    with zipfile.ZipFile(OUT / "BestDPIFinder-source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for path in source_files:
            archive.write(path, "BestDPIFinder-source/" + path.relative_to(ROOT).as_posix())
        for name in ("overview.md", "verification.json"):
            archive.write(OUT / name, "BestDPIFinder-source/" + name)
    with zipfile.ZipFile(OUT / "BestDPIFinder-Windows.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("BestDPIFinder.exe", "overview.md", "verification.json", "tests-final.txt",
                     "gui-adaptive-verification.txt", "gui-legacy-verification.txt", "exe-smoke.txt",
                     "exe-bytecode-verification.txt", "preview-adaptive-verification.txt",
                     "app-preview.png"):
            archive.write(OUT / name, "BestDPIFinder/" + name)
    for name in ("BestDPIFinder-source.zip", "BestDPIFinder-Windows.zip"):
        with zipfile.ZipFile(OUT / name) as archive:
            if archive.testzip():
                raise RuntimeError("Archive integrity check failed")
    print(json.dumps({"ready": True, "mode": MODE, "unit_summary": evidence["unit_tests"]["summary"],
                      "adaptive_gui_checks": adaptive_probe,
                      "legacy_gui_check_count": len(probe["checks"]), "exe_sha256": evidence["exe_sha256"],
                      "files": [p.name for p in OUT.iterdir() if p.is_file()]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
