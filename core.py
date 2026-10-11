"""Pure measurement, scheduling, recommendation and JSON persistence logic."""
from __future__ import annotations

import json
import math
import os
import random
import statistics
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION: int = 1
GAME_YAWS: dict[str, float] = {"VALORANT / 无畏契约": 0.07, "Counter-Strike 2": 0.022}
STAGES: tuple[str, ...] = ("定位点击", "小目标微调", "连续跟踪")
STAGE_SECONDS: float = 10.0
REQUIRED_ROUNDS: int = 2


def positive(value: float, name: str) -> float:
    """Return a finite positive value, or raise a user-readable error."""
    number: float = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{name}必须是大于 0 的有限数字。")
    return number


def cm360(dpi: float, sensitivity: float, yaw: float) -> float:
    """Convert relative counts to physical distance for one horizontal turn."""
    product: float = positive(dpi, "DPI") * positive(sensitivity, "灵敏度") * positive(yaw, "Yaw")
    return positive(914.4 / positive(product, "输入乘积"), "cm/360°")


def sensitivity_for(distance_cm: float, dpi: float, yaw: float) -> float:
    """Convert a target cm/360 distance to the game's hip-fire sensitivity."""
    product: float = positive(distance_cm, "cm/360°") * positive(dpi, "DPI") * positive(yaw, "Yaw")
    return positive(914.4 / positive(product, "输入乘积"), "灵敏度")


def raw_angle(counts: int, sensitivity: float, yaw: float) -> float:
    """Map raw relative device counts to degrees; DPI is deliberately absent."""
    return counts * positive(sensitivity, "灵敏度") * positive(yaw, "Yaw")


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, value))


@dataclass(frozen=True)
class Settings:
    """Explicit user-entered settings, never inferred from the operating system."""
    game: str = "VALORANT / 无畏契约"
    dpi: float = 800.0
    sensitivity: float = 0.35
    yaw: float = 0.07

    def validate(self) -> Settings:
        if self.game not in GAME_YAWS:
            raise ValueError("请选择支持的游戏。")
        if not 50.0 <= positive(self.dpi, "DPI") <= 64000.0:
            raise ValueError("DPI 请填写 50～64000 之间的鼠标硬件设置值。")
        if not 0.0001 <= positive(self.sensitivity, "灵敏度") <= 100.0:
            raise ValueError("灵敏度请填写 0.0001～100 之间的数值。")
        if not 0.0001 <= positive(self.yaw, "Yaw") <= 1.0:
            raise ValueError("Yaw 请填写 0.0001～1 之间的数值。")
        if self.game == "VALORANT / 无畏契约" and not math.isclose(self.yaw, 0.07):
            raise ValueError("VALORANT 使用常见第三方 Yaw 0.07。自定义 Yaw 仅适用于 CS2。")
        if not 0.5 <= self.distance <= 500.0:
            raise ValueError("当前设置超出本测评可用范围（0.5～500 cm/360°），请检查输入。")
        return self

    @property
    def distance(self) -> float:
        return cm360(self.dpi, self.sensitivity, self.yaw)


@dataclass(frozen=True)
class Candidate:
    code: str
    distance_cm: float
    sensitivity: float


@dataclass(frozen=True)
class Trial:
    round_number: int
    candidate: Candidate


def candidates(settings: Settings) -> list[Candidate]:
    settings.validate()
    return [Candidate(code, settings.distance * factor,
                      sensitivity_for(settings.distance * factor, settings.dpi, settings.yaw))
            for code, factor in zip(("A", "B", "C"), (0.8, 1.0, 1.25))]


def make_plan(settings: Settings, seed: int | None = None) -> list[Trial]:
    """Balance three candidates across two independently shuffled repetitions."""
    rng: random.Random = random.Random(seed)
    options: list[Candidate] = candidates(settings)
    result: list[Trial] = []
    for round_number in range(1, REQUIRED_ROUNDS + 1):
        order: list[Candidate] = options.copy()
        rng.shuffle(order)
        if result and order[0].code == result[-1].candidate.code:
            order[0], order[1] = order[1], order[0]
        result.extend(Trial(round_number, candidate) for candidate in order)
    return result


@dataclass(frozen=True)
class StageRecord:
    """Only a finished stage has a record; aborted trials are discarded in full."""
    name: str
    hits: int = 0
    misses: int = 0
    latencies: tuple[float, ...] = ()
    tracking_on: float = 0.0
    valid_seconds: float = STAGE_SECONDS

    def validate(self) -> StageRecord:
        if (self.name not in STAGES or type(self.hits) is not int or type(self.misses) is not int
                or self.hits < 0 or self.misses < 0 or self.hits > 10000 or self.misses > 10000):
            raise ValueError("测评阶段数据无效。")
        if not math.isfinite(self.valid_seconds) or not 9.9 <= self.valid_seconds <= 10.1:
            raise ValueError("测评阶段时长无效或尚未完成。")
        if (not math.isfinite(self.tracking_on)
                or not 0.0 <= self.tracking_on <= self.valid_seconds + 0.001):
            raise ValueError("跟踪时长无效。")
        if any(not math.isfinite(value) or value < 0.0 or value > 10.1 for value in self.latencies):
            raise ValueError("命中耗时数据无效。")
        if self.name != STAGES[2] and len(self.latencies) != self.hits:
            raise ValueError("命中次数与耗时记录不一致。")
        if self.name == STAGES[2] and (self.hits or self.misses or self.latencies):
            raise ValueError("跟踪阶段不能包含点击计分。")
        return self

    @property
    def accuracy(self) -> float:
        return self.hits / max(1, self.hits + self.misses)

    @property
    def median_latency(self) -> float | None:
        return statistics.median(self.latencies) if self.latencies else None

    @property
    def score(self) -> float:
        """Explainable 0–100 score; inactivity never creates a good click score."""
        if self.name == STAGES[2]:
            return 100.0 * clamp(self.tracking_on / max(self.valid_seconds, 0.001))
        latency: float = self.median_latency if self.median_latency is not None else 2.0
        speed: float = clamp(1.0 - latency / 2.0)
        throughput: float = clamp(self.hits / 6.0)
        return 100.0 * throughput * self.accuracy * (0.55 + 0.45 * speed)


@dataclass(frozen=True)
class TrialRecord:
    candidate_code: str
    round_number: int
    distance_cm: float
    sensitivity: float
    stages: tuple[StageRecord, ...]

    def validate(self) -> TrialRecord:
        if (self.candidate_code not in ("A", "B", "C") or type(self.round_number) is not int
                or self.round_number not in (1, 2)):
            raise ValueError("候选或重复轮次无效。")
        positive(self.distance_cm, "候选距离")
        positive(self.sensitivity, "候选灵敏度")
        if tuple(stage.name for stage in self.stages) != STAGES:
            raise ValueError("正式记录必须包含三个完整阶段。")
        for stage in self.stages:
            stage.validate()
        return self

    @property
    def score(self) -> float:
        return statistics.mean(stage.score for stage in self.stages)


def summarize(settings: Settings, records: list[TrialRecord]) -> dict[str, Any]:
    """Return cautious candidate comparisons, not a statistically proven optimum."""
    options: list[Candidate] = candidates(settings)
    expected: dict[str, Candidate] = {candidate.code: candidate for candidate in options}
    seen: set[tuple[str, int]] = set()
    for record in records:
        record.validate()
        key: tuple[str, int] = (record.candidate_code, record.round_number)
        candidate: Candidate = expected[record.candidate_code]
        if (key in seen or not math.isclose(record.distance_cm, candidate.distance_cm, rel_tol=1e-6)
                or not math.isclose(record.sensitivity, candidate.sensitivity, rel_tol=1e-6)):
            raise ValueError("汇总包含重复或参数不一致的测评记录。")
        seen.add(key)
    rows: list[dict[str, Any]] = []
    for candidate in options:
        matching: list[TrialRecord] = [record for record in records if record.candidate_code == candidate.code]
        scores: list[float] = [record.score for record in matching]
        rows.append({"code": candidate.code, "distance_cm": candidate.distance_cm,
                     "sensitivity": candidate.sensitivity, "completed": len(matching),
                     "score": statistics.mean(scores) if scores else None,
                     "spread": max(scores) - min(scores) if len(scores) > 1 else None,
                     "stage_scores": [statistics.mean(record.stages[index].score for record in matching)
                                      for index in range(3)] if matching else [],
                     "hits": sum(stage.hits for record in matching for stage in record.stages[:2]),
                     "misses": sum(stage.misses for record in matching for stage in record.stages[:2])})
    complete: bool = all(row["completed"] == REQUIRED_ROUNDS for row in rows)
    tested: list[dict[str, Any]] = sorted([row for row in rows if row["score"] is not None],
                                        key=lambda row: row["score"], reverse=True)
    result: dict[str, Any] = {"complete": complete, "rows": rows, "leader": tested[0]["code"] if tested else None,
                              "recommended_range": None, "message": "尚无正式完成的测评数据。"}
    if not tested:
        return result
    if not complete:
        result["message"] = "样本不足；仅显示暂时领先候选，不给出最终推荐。"
        return result
    if tested[0]["score"] < 10.0:
        result["message"] = "有效表现不足，暂不推荐。请检查鼠标捕获并重新测评。"
        return result
    noisy: bool = any((row["spread"] or 0.0) > 12.0 for row in tested)
    near: list[dict[str, Any]] = tested if noisy else [row for row in tested if tested[0]["score"] - row["score"] <= 4.0]
    distances: list[float] = [row["distance_cm"] for row in near]
    result["recommended_range"] = [min(distances), max(distances)]
    result["message"] = ("重复表现波动较大，建议保留整个范围并再次验证。" if noisy else
                         "候选分差不超过 4 分，建议在此范围内按舒适度选择。" if len(near) > 1 else
                         "该候选在本次简化测评中领先；请到游戏训练场再次确认。")
    return result


def make_payload(settings: Settings, records: list[TrialRecord], comfort: str = "", aborted: int = 0) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "settings": asdict(settings),
            "records": [asdict(record) for record in records], "summary": summarize(settings, records),
            "comfort_note": str(comfort)[:1000], "aborted_trials": max(0, int(aborted)),
            "limitations": "WM_INPUT 相对计数；角度平面近似；普通腰射；不模拟游戏 FOV、开镜、拉伸、后坐力。分数是启发式，不是学术验证的最优值。"}


def save_payload(path: Path, payload: dict[str, Any]) -> None:
    """Atomically save UTF-8 JSON, preserving an existing file on write failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str = ""
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".bestdpi-", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def load_payload(path: Path) -> tuple[Settings, list[TrialRecord], str]:
    """Validate imported records and recompute summaries instead of trusting them."""
    try:
        if Path(path).stat().st_size > 2_000_000:
            raise ValueError("结果文件过大（最多 2 MB）。")
        data: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
        if data["schema_version"] != SCHEMA_VERSION:
            raise ValueError("不支持此结果文件版本。")
        settings: Settings = Settings(**data["settings"]).validate()
        expected: dict[str, Candidate] = {item.code: item for item in candidates(settings)}
        records: list[TrialRecord] = []
        seen: set[tuple[str, int]] = set()
        if len(data["records"]) > 6:
            raise ValueError("结果记录数量无效。")
        for entry in data["records"]:
            stages: tuple[StageRecord, ...] = tuple(StageRecord(
                name=item["name"], hits=item["hits"], misses=item["misses"],
                latencies=tuple(item["latencies"]), tracking_on=item["tracking_on"],
                valid_seconds=item["valid_seconds"]) for item in entry["stages"])
            record: TrialRecord = TrialRecord(entry["candidate_code"], entry["round_number"],
                                              entry["distance_cm"], entry["sensitivity"], stages).validate()
            key: tuple[str, int] = (record.candidate_code, record.round_number)
            candidate: Candidate = expected[record.candidate_code]
            if key in seen or not math.isclose(record.distance_cm, candidate.distance_cm, rel_tol=1e-6) or not math.isclose(record.sensitivity, candidate.sensitivity, rel_tol=1e-6):
                raise ValueError("结果包含重复记录或与设置不一致的候选。")
            seen.add(key)
            records.append(record)
        return settings, records, str(data.get("comfort_note", ""))[:1000]
    except (KeyError, TypeError, OverflowError, AttributeError, json.JSONDecodeError) as error:
        raise ValueError("结果文件损坏或结构不正确，请选择本工具导出的 JSON。") from error
