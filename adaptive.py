"""Score-driven sensitivity hill climbing inside a 120-second assessment.

Design (2026-09-17, per user request):
    Sensitivity is neither held constant nor swept on a fixed schedule. It *responds to your
    score*: after every scoring window the controller looks at how well you just did and
    decides where to move the sensitivity next. Play better and it keeps going the same way;
    play worse and it turns around and shortens its stride. The result is a sensitivity that
    wanders up and down on its own while you click, hunting for the setting you aim best at.

Plan: 2 s warm-up + 2 blocks x (3 tasks x 20 s) = 122 s of activity budget.

Each task is divided into 10 two-second scoring windows. A window is scored, then its score
feeds the climber, which sets the sensitivity for the *next* window. Because the gain of
window N+1 depends on the user's performance in window N, the recorded gain series is a
*consequence of the measurement* rather than a precomputed curve -- which is why the block
validates the direction of the feedback loop instead of recomputing the gains.

Click scoring now uses the interval between consecutive successful hits, so reaction speed
counts, with an explicit timeout penalty so that spraying cannot inflate the score.

Honesty constraints kept: the recommendation is a *provisional* candidate from a single
noisy session, never a statistically proven optimum; a flat or barely-moving trace is
reported honestly rather than dressed up as a peak; nothing here touches the real game,
system mouse or hardware DPI.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core import STAGES, Settings, clamp, positive, sensitivity_for

WARMUP_SECONDS: float = 2.0
TASK_SECONDS: float = 20.0
MAX_BLOCKS: int = 2
BLOCK_SECONDS: float = 3 * TASK_SECONDS
BUDGET_SECONDS: float = WARMUP_SECONDS + MAX_BLOCKS * BLOCK_SECONDS  # 122 s

# Scoring windows inside every task; window count divides TASK_SECONDS exactly. Two seconds
# per window is the shortest span that still contains enough clicks for the score to mean
# something, while staying short enough that the climber can react within a task.
WINDOW_SECONDS: float = 2.0
WINDOWS_PER_TASK: int = round(TASK_SECONDS / WINDOW_SECONDS)

# Every task carries a window curve of the same length, so the four window tuples always line
# up. Click and settle tasks have no tracking evidence and must use this zero curve rather
# than an empty tuple: an empty tuple fails TaskSample.validate at the first task boundary.
ZERO_WINDOW_CURVE: tuple[float, ...] = (0.0,) * WINDOWS_PER_TASK

# --- Hill-climb geometry, written as cm/360 degree multipliers of the entered setting --------
# Dividing cm/360 by HILL_FAST_FACTOR shortens the distance, i.e. the *fast, high-gain* end;
# dividing by HILL_SLOW_FACTOR lengthens it, i.e. the *slow, low-gain* end. The climber stays
# inside this band, so one run probes a bounded neighbourhood instead of wandering off.
HILL_FAST_FACTOR: float = 1.25   # 距离 ÷ 1.25 => 更快、增益更高的一端
HILL_SLOW_FACTOR: float = 0.8    # 距离 ÷ 0.80 => 更慢、增益更低的一端

# Gear sizes in *gain* ratio per step (multiplicative). The climber starts at COARSE and
# drops a gear every time it reverses. Ratios multiply, so a step of 0.88 means "12% slower".
HILL_GEARS: tuple[float, ...] = (0.88, 0.94, 0.97, 0.985)
# A window must beat the previous window by this many score points before the move counts as
# an improvement. Below it the change is treated as noise, which stops the climber from
# oscillating on a flat score landscape.
HILL_IMPROVEMENT: float = 2.0
# Improvement needed to promote back into a faster gear after a reversal. Larger than
# HILL_IMPROVEMENT so a single lucky window does not undo a well-earned retreat.
HILL_PROMOTE: float = 6.0

MODE: str = "adaptive_120s_score_hill_climb"
TASK_SEED: int = 31027
SCHEMA_VERSION: int = 6
TIME_TOLERANCE: float = 0.001

MIN_COMPOSITE: float = 30.0
MIN_TASK_SCORE: float = 10.0
MIN_CLICKS: int = 2
# A gain bucket needs this many scored windows before its average may be trusted; with 10
# windows per task and two blocks there is ample evidence per bucket, and a bucket backed by
# one lucky window is noise rather than a peak.
MIN_PEAK_WINDOWS: int = 2
PEAK_MARGIN: float = 2.0
# When this many consecutive windows all lose ground the trace is declared a plateau and the
# climber stops widening its search: it has already found the neighbourhood it likes.
MAX_AUDIT_EVENTS: int = 50
RECOMMENDATION_SOURCES: tuple[str, ...] = ("hill_peak", "baseline_fallback")

# --- Click scoring: speed x accuracy^2 -------------------------------------------------------
# Score for one hit whose interval was ``gap`` seconds: full marks for an instant hit,
# decaying with an exponential time constant. Tuned so ~0.35 s (a comfortable reaction to a
# target 10 degrees away) scores in the high 80s and 1 s scores around 30.
SPEED_DECAY_SECONDS: float = 0.45
# An interval longer than this is treated as a miss-by-timeout rather than a slow hit: the
# player lost the target. The interval is capped here before scoring so a single stall cannot
# wipe out the whole window, and the timeout is then punished through accuracy instead.
HIT_TIMEOUT_SECONDS: float = 1.5

if WINDOWS_PER_TASK < 2 or not math.isclose(WINDOWS_PER_TASK * WINDOW_SECONDS, TASK_SECONDS):
    raise RuntimeError("每项任务的时长必须能被等长计分窗口整除。")
if not math.isclose(WARMUP_SECONDS + MAX_BLOCKS * BLOCK_SECONDS, BUDGET_SECONDS):
    raise RuntimeError("计划时长必须正好等于活动预算。")


def aware_timestamp(value: str) -> datetime:
    """Parse a timezone-aware ISO datetime, rejecting naive or malformed dates."""
    try:
        stamp: datetime = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError("测试块日期时间必须是有效的带时区 ISO 格式。") from error
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("测试块日期时间缺少时区。")
    return stamp


@dataclass
class HillClimb:
    """Score-driven sensitivity search: move, then let the score decide the next move.

    The climber walks the gain axis in multiplicative steps. After each scoring window its
    score is compared with the previous window's:

    * better by ``HILL_IMPROVEMENT``  -> keep the direction, and promote back a gear if the
      gain was large enough (the slope is worth climbing faster),
    * worse by more than that         -> reverse direction and drop a gear (the peak is close),
    * in between                      -> treat as noise and hold everything steady.

    Gains are bounded to ``[floor, ceiling]`` derived from the entered setting, so the search
    can never run away from the band the user actually asked about.

    This object is deliberately mutable and stateful: it is driven once per scoring window
    while the user is clicking, and its decisions depend on measurements that do not exist
    until those clicks happen.
    """
    start_gain: float
    floor_gain: float
    ceiling_gain: float
    gain: float = 0.0
    direction: float = -1.0          # -1 => 增益下降（更慢）; +1 => 增益上升（更快）
    gear: int = 0
    best_score: float = -1.0
    best_gain: float = 0.0
    last_score: float = -1.0
    reversals: int = 0
    stalled: int = 0
    # Gains actually put into effect, in window order. The first entry is the starting gain;
    # every later one was chosen from the previous window's score, so this series is evidence
    # rather than a precomputed curve and is what gets stored per task.
    history: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        self.start_gain = positive(self.start_gain, "起始灵敏度")
        self.floor_gain = positive(self.floor_gain, "灵敏度下界")
        self.ceiling_gain = positive(self.ceiling_gain, "灵敏度上界")
        if self.floor_gain > self.ceiling_gain:
            raise ValueError("灵敏度上界不能低于下界。")
        if not self.floor_gain <= self.start_gain <= self.ceiling_gain:
            raise ValueError("起始灵敏度必须落在搜索区间内。")
        if self.best_score < 0:
            self.gain = self.start_gain
            self.best_gain = self.start_gain
            self.history = (self.start_gain,)

    @property
    def step_ratio(self) -> float:
        """Current gear size as a gain ratio, e.g. 0.88 for a 12% step."""
        return HILL_GEARS[min(self.gear, len(HILL_GEARS) - 1)]

    def observe(self, score: float) -> None:
        """Feed one finished window's score, then move the sensitivity for the next window.

        ``direction`` is the direction the *last* step took. Keeping it that way matters: the
        score tells us whether that step helped, and the next step is derived from that verdict.
        An earlier revision flipped ``direction`` here to mean "walk the other way next", but
        ``next_gain`` also flips it when pinned against a band edge, so the two flips cancelled
        and the climber walked back along the path it had just rejected.
        """
        if not math.isfinite(score) or not 0 <= score <= 100:
            raise ValueError("窗口得分无效。")
        if self.best_score < 0:
            # First window: nothing to compare against yet, so just record the baseline. The
            # starting direction stays as configured: that first step has not been judged.
            self.best_score = score
            self.best_gain = self.gain
            self.last_score = score
            self.history = self.history + (self.gain,)
            return
        delta: float = score - self.last_score
        if delta > HILL_IMPROVEMENT:
            # Last step helped, so walk the same way again -- and if it helped a lot, take a
            # longer stride again, because the peak has not been narrowed down yet.
            if delta >= HILL_PROMOTE and self.gear > 0:
                self.gear -= 1
            self.stalled = 0
        elif delta < -HILL_IMPROVEMENT:
            # Last step hurt, so the peak sits behind us: turn around and shorten the stride.
            self.direction = -self.direction
            self.gear += 1
            self.reversals += 1
            self.stalled = 0
        else:
            # Within the noise floor: hold position. Moving anyway would let a flat score
            # series march the sensitivity across the whole band and afterwards *look* like a
            # search, which is exactly the failure mode the dead-band exists to prevent.
            self.stalled += 1
        if score > self.best_score:
            self.best_score = score
            self.best_gain = self.gain
        self.last_score = score
        # Only a judged step moves the gain: an improvement keeps the current direction, a
        # drop reverses it, and anything inside the noise floor stays put.
        if delta > HILL_IMPROVEMENT or delta < -HILL_IMPROVEMENT:
            self.gain = self.next_gain(self.direction)
        self.history = self.history + (self.gain,)

    def next_gain(self, direction: float) -> float:
        """One multiplicative step from ``self.gain`` in ``direction``, clamped to the band.

        ``direction > 0`` walks toward the fast end, i.e. *multiplies* the gain by the gear
        ratio; ``direction < 0`` walks toward the slow end and divides by it. Getting that
        backwards is not a cosmetic slip: the first block deliberately starts pinned at the
        ceiling, so an inverted step would immediately clamp back onto the ceiling, trip the
        bounce guard, and oscillate between two values while ignoring every score it was fed.

        Taking the direction as an argument instead of reading ``self.direction`` keeps the
        "what did we just do" and "what do we do next" questions from sharing one field. A
        step that would land on the bound already occupied bounces off it rather than grinding,
        so the rest of the task never wastes its windows sitting on one value.
        """
        candidate: float = self._step(self.gain, direction)
        clamped: float = min(self.ceiling_gain, max(self.floor_gain, candidate))
        if math.isclose(clamped, self.gain, rel_tol=1e-12):
            # Already against the edge this direction leads to: bounce so the remaining
            # windows keep measuring different sensitivities.
            clamped = min(self.ceiling_gain, max(self.floor_gain, self._step(self.gain, -direction)))
        return clamped

    def _step(self, gain: float, direction: float) -> float:
        """One gear-sized move from ``gain`` toward the fast (``+``) or slow (``-``) end.

        Every entry in ``HILL_GEARS`` is below 1.0, so they are step *magnitudes*, not signed
        ratios: multiplying always descends, dividing always ascends. Hard-coding that here
        once -- instead of scattering ``ratio`` / ``1/ratio`` across the call sites -- is what
        keeps "which way does this gear go" from being re-derived (and mis-derived) per caller.
        """
        return gain / self.step_ratio if direction > 0 else gain * self.step_ratio


def band_for(settings: Settings) -> tuple[float, float]:
    """Gain bounds for the search, in gain units, from the entered sensitivity.

    Expressing the band in cm/360 degrees first keeps the *feel* symmetric: the fast end is
    the entered distance over ``HILL_FAST_FACTOR`` and the slow end over ``HILL_SLOW_FACTOR``.
    """
    base: float = positive(settings.distance, "cm/360°")
    fast_cm: float = base / HILL_FAST_FACTOR
    slow_cm: float = base / HILL_SLOW_FACTOR
    low: float = sensitivity_for(slow_cm, settings.dpi, settings.yaw)
    high: float = sensitivity_for(fast_cm, settings.dpi, settings.yaw)
    if not low < high:
        raise ValueError("灵敏度搜索区间无效。")
    return (low, high)


def tracking_target(elapsed: float) -> tuple[float, float]:
    return 12.0 * math.sin(elapsed * .8), 4.0 * math.sin(elapsed * 1.3 + .6)


class TrackingIntegral:
    """Fixed 1 ms midpoint samples of held raw-input view, independent of paint FPS.

    Tracking is accumulated per scoring window so the sweep yields a curve here too.
    """
    def __init__(self, started_at: float, duration: float = TASK_SECONDS) -> None:
        self.started_at: float = started_at
        self.duration: float = duration
        self.samples: int = 0
        self.on_seconds: float = 0.0
        self.window_on: list[float] = [0.0] * WINDOWS_PER_TASK

    def advance(self, now: float, view_x: float, view_y: float, radius: float = 1.2) -> float:
        end: int = min(round(self.duration * 1000), max(0, int((now - self.started_at) * 1000 + 1e-6)))
        while self.samples < end:
            local: float = (self.samples + .5) / 1000
            target_x, target_y = tracking_target(local)
            if math.hypot(target_x - view_x, target_y - view_y) <= radius:
                self.on_seconds += .001
                bucket: int = min(WINDOWS_PER_TASK - 1, int(local / WINDOW_SECONDS))
                self.window_on[bucket] += .001
            self.samples += 1
        return min(self.duration, self.on_seconds)


def hit_speed_score(gap: float) -> float:
    """Score for one successful hit whose interval since the previous hit was ``gap`` seconds.

    This is the ``function(lastTimeHit - nowTimeHit)`` idea: fast hits score near 100 and the
    value decays exponentially with ``SPEED_DECAY_SECONDS``. Intervals beyond
    ``HIT_TIMEOUT_SECONDS`` are capped rather than scored linearly, because past that point the
    target was effectively lost -- the loss is charged through accuracy instead, by counting it
    as a miss. Capping also stops one stall from erasing the rest of the window.
    """
    if not math.isfinite(gap) or gap < 0:
        raise ValueError("命中间隔无效。")
    return 100 * math.exp(-min(gap, HIT_TIMEOUT_SECONDS) / SPEED_DECAY_SECONDS)


def click_window_score(gaps: list[float], timeouts: int) -> float:
    """Window score = mean hit speed x accuracy^2.

    ``accuracy`` treats every timeout as a miss, so a player who sprays and misses is punished
    twice: their successful hits are slower *and* their accuracy collapses.
    """
    if not gaps:
        return 0.0
    if type(timeouts) is not int or timeouts < 0:
        raise ValueError("超时次数无效。")
    speed: float = sum(hit_speed_score(gap) for gap in gaps) / len(gaps)
    accuracy: float = len(gaps) / (len(gaps) + timeouts)
    return speed * accuracy * accuracy


def click_score(hits: int, misses: int, seconds: float) -> float:
    """Whole-task click score from aggregated counts, kept for the composite.

    Per-window scoring is the primary path (``click_window_score``); this aggregate exists so
    the three-task composite has one comparable number per task. Without the recorded gaps the
    speed term is unknown here, so it reports throughput x accuracy^2 as before.
    """
    accuracy: float = hits / max(1, hits + misses)
    throughput: float = hits / seconds
    return 100 * (1 - math.exp(-throughput / 1.5)) * accuracy * accuracy


@dataclass(frozen=True)
class TaskSample:
    """One scored task whose sensitivity was steered by the user's own scores."""
    name: str
    hits: int = 0
    misses: int = 0
    latencies: tuple[float, ...] = ()
    tracking_on: float = 0.0
    valid_seconds: float = TASK_SECONDS
    raw_counts: int = 0
    # Per-window evidence: one entry per scoring window.
    window_hits: tuple[int, ...] = ()
    window_misses: tuple[int, ...] = ()
    window_tracking: tuple[float, ...] = ()
    window_gains: tuple[float, ...] = ()
    # Window-level click detail. ``window_gaps`` flattens every hit's interval and
    # ``window_timeouts`` counts intervals that exceeded HIT_TIMEOUT_SECONDS, so a stored
    # record can be re-scored exactly instead of trusting a number written at the time.
    window_gaps: tuple[tuple[float, ...], ...] = ()
    window_timeouts: tuple[int, ...] = ()

    def validate(self) -> TaskSample:
        if self.name not in STAGES or type(self.hits) is not int or type(self.misses) is not int:
            raise ValueError("阶段名称或命中字段无效。")
        if not 0 <= self.hits <= 10000 or not 0 <= self.misses <= 10000:
            raise ValueError("命中/误击次数无效。")
        if type(self.raw_counts) is not int or self.raw_counts < 0:
            raise ValueError("原始移动计数无效。")
        if not math.isfinite(self.valid_seconds) or abs(self.valid_seconds - TASK_SECONDS) > 0.001:
            raise ValueError(f"自适应阶段必须完成固定 {TASK_SECONDS:g} 秒。")
        if not math.isfinite(self.tracking_on) or not 0 <= self.tracking_on <= TASK_SECONDS + 0.001:
            raise ValueError("跟踪时间无效。")
        if any(not math.isfinite(x) or not 0 <= x <= TASK_SECONDS + 0.001 for x in self.latencies):
            raise ValueError("命中耗时无效。")
        if self.name == STAGES[2]:
            if self.hits or self.misses or self.latencies:
                raise ValueError("跟踪阶段不计点击。")
        elif len(self.latencies) != self.hits:
            raise ValueError("命中次数与耗时数量不一致。")
        lengths: tuple[int, ...] = (len(self.window_hits), len(self.window_misses),
                                    len(self.window_tracking), len(self.window_gains))
        if any(length != WINDOWS_PER_TASK for length in lengths):
            raise ValueError(f"每个任务必须记录 {WINDOWS_PER_TASK} 个计分窗口的数据。")
        if any(type(value) is not int or value < 0 for value in self.window_hits + self.window_misses):
            raise ValueError("窗口命中/误击计数无效。")
        if sum(self.window_hits) != self.hits or sum(self.window_misses) != self.misses:
            raise ValueError("窗口命中数与任务总数不一致。")
        if any(not math.isfinite(value) or value < 0 for value in self.window_tracking):
            raise ValueError("窗口跟踪时间无效。")
        if self.name == STAGES[2] and abs(sum(self.window_tracking) - self.tracking_on) > 0.01:
            raise ValueError("窗口跟踪时间与任务总时间不一致。")
        if any(type(gain) is bool or not math.isfinite(gain) or gain <= 0
               for gain in self.window_gains):
            raise ValueError("窗口灵敏度无效。")
        if self.name != STAGES[2]:
            # Click tasks must carry the full per-window interval detail: the speed term is
            # computed from it, and a record without it could not be re-scored honestly.
            if (len(self.window_gaps) != WINDOWS_PER_TASK
                    or len(self.window_timeouts) != WINDOWS_PER_TASK):
                raise ValueError(f"点击任务必须记录 {WINDOWS_PER_TASK} 个窗口的命中间隔明细。")
            for gaps, window_hits in zip(self.window_gaps, self.window_hits):
                if len(gaps) != window_hits:
                    raise ValueError("窗口命中间隔数量与窗口命中数不一致。")
                if any(not math.isfinite(gap) or gap < 0 for gap in gaps):
                    raise ValueError("窗口命中间隔无效。")
            if any(type(count) is not int or count < 0 for count in self.window_timeouts):
                raise ValueError("窗口超时计数无效。")
        return self

    @property
    def score(self) -> float:
        """Whole-task score; retained for the composite, aggregated over all windows."""
        if self.name == STAGES[2]:
            return 100 * clamp(self.tracking_on / TASK_SECONDS)
        values: tuple[float, ...] = self.window_scores()
        return sum(values) / len(values) if values else 0.0

    def window_scores(self) -> tuple[float, ...]:
        """Per-window score aligned with ``window_gains``; this is the measured trace."""
        if self.name == STAGES[2]:
            return tuple(100 * clamp(value / WINDOW_SECONDS) for value in self.window_tracking)
        if self.window_gaps or self.window_timeouts:
            return tuple(click_window_score(list(gaps), timeouts)
                         for gaps, timeouts in zip(self.window_gaps, self.window_timeouts))
        return tuple(click_score(hit, miss, WINDOW_SECONDS)
                     for hit, miss in zip(self.window_hits, self.window_misses))


@dataclass(frozen=True)
class BlockRequest:
    index: int
    reason: str
    phase: str
    task_seed: int = TASK_SEED


@dataclass(frozen=True)
class BlockRecord:
    """Finished or explicitly invalidated block of three score-driven tasks."""
    index: int
    dpi: float
    yaw: float
    started_at: str
    ended_at: str
    active_start: float
    active_end: float
    tasks: tuple[TaskSample, ...]
    valid: bool
    invalid_reason: str
    adjustment_reason: str
    phase: str
    task_seed: int = TASK_SEED
    # Search band actually available to the climber per task, so the explored region stays
    # fully auditable even though the gains themselves follow the user's scores.
    task_band_lows: tuple[float, ...] = ()
    task_band_highs: tuple[float, ...] = ()

    @property
    def score(self) -> float | None:
        if not self.valid:
            return None
        values: list[float] = [task.score for task in self.tasks]
        geometric: float = math.prod(values) ** (1 / 3)
        return .75 * geometric + .25 * min(values)

    def curve(self) -> tuple[tuple[float, float], ...]:
        """Merge every window of every task into (gain, score) points, best first."""
        points: list[tuple[float, float]] = []
        for task in self.tasks:
            for gain, score in zip(task.window_gains, task.window_scores()):
                points.append((gain, score))
        points.sort(key=lambda item: item[1], reverse=True)
        return tuple(points)

    def peak_gain(self) -> float | None:
        """Gain of the best-scoring window, or ``None`` when the block is invalid."""
        if not self.valid:
            return None
        curve: tuple[tuple[float, float], ...] = self.curve()
        return curve[0][0] if curve else None

    def band(self) -> tuple[float, float]:
        """Lowest and highest gain actually visited across the whole block."""
        gains: list[float] = [gain for task in self.tasks for gain in task.window_gains]
        if not gains:
            return (0.0, 0.0)
        return (min(gains), max(gains))

    def travelled(self) -> float:
        """How far the climber actually moved, in ratios of the starting gain.

        A climber that never moves cannot have found a peak, so this is what lets the results
        page tell "we explored and found a peak" apart from "we sat still and the score is
        meaningless". Summing absolute log-steps makes it direction-agnostic.
        """
        total: float = 0.0
        for task in self.tasks:
            gains: tuple[float, ...] = task.window_gains
            if len(gains) < 2:
                continue
            total += sum(abs(math.log(b) - math.log(a)) for a, b in zip(gains, gains[1:]))
        return total

    def validate(self, settings: Settings) -> BlockRecord:
        if type(self.index) is not int or not 1 <= self.index <= MAX_BLOCKS:
            raise ValueError("测试块序号无效。")
        numbers: tuple[float, ...] = (self.dpi, self.yaw, self.active_start, self.active_end)
        if not all(math.isfinite(value) for value in numbers):
            raise ValueError("测试块包含非有限数字。")
        if not 0 <= self.active_start <= self.active_end <= BUDGET_SECONDS + TIME_TOLERANCE:
            raise ValueError("测试块预算时间无效。")
        if self.valid and self.active_end - self.active_start + TIME_TOLERANCE < BLOCK_SECONDS:
            raise ValueError(f"有效测试块必须包含三项各 {TASK_SECONDS:g} 秒测量。")
        if aware_timestamp(self.ended_at) < aware_timestamp(self.started_at):
            raise ValueError("测试块结束日期时间不能早于开始时间。")
        if not math.isclose(self.dpi, settings.dpi) or not math.isclose(self.yaw, settings.yaw):
            raise ValueError("测试块与硬件设置不一致。")
        if type(self.valid) is not bool or self.task_seed != TASK_SEED:
            raise ValueError("测试有效性或任务难度标识无效。")
        for task in self.tasks:
            task.validate()
        if self.valid and (tuple(task.name for task in self.tasks) != STAGES
                           or any(task.raw_counts <= 0 for task in self.tasks)):
            raise ValueError("有效块需要三阶段完整记录且每阶段有真实相对移动。")
        if self.valid and (len(self.task_band_lows) != len(STAGES)
                           or len(self.task_band_highs) != len(STAGES)):
            raise ValueError("有效块缺少三项搜索区间的完整记录。")
        if any(type(gain) is bool or not math.isfinite(gain) or gain <= 0
               for gain in self.task_band_lows + self.task_band_highs):
            raise ValueError("搜索区间记录无效。")
        if self.valid:
            # Unlike the previous sweep model, window_gains is NOT reconstructible from the band:
            # every gain after the first was chosen by the climber from the score the user
            # actually produced. What can be checked is that the recorded gains respect the
            # declared band, and that the feedback loop visibly ran -- a block whose gains never
            # leave their starting value means no adjustment happened at all.
            for task, low, high in zip(self.tasks, self.task_band_lows, self.task_band_highs):
                if low >= high:
                    raise ValueError("搜索区间不能是单点，否则测不到灵敏度变化。")
                tolerance: float = 1e-9
                if any(not low - tolerance <= gain <= high + tolerance for gain in task.window_gains):
                    raise ValueError("窗口灵敏度超出了声明的搜索区间。")
            if all(len(set(task.window_gains)) < 2 for task in self.tasks):
                raise ValueError("有效块必须体现灵敏度调整，但三项任务的灵敏度全程未变。")
        if not self.valid and not self.invalid_reason:
            raise ValueError("无效块缺少原因。")
        return self


def quality(record: BlockRecord | None) -> bool:
    """Absolute gate a block must clear before its curve may be reported as a candidate."""
    return bool(record is not None and record.valid and float(record.score) >= MIN_COMPOSITE
                and all(task.score >= MIN_TASK_SCORE for task in record.tasks)
                and sum(task.hits for task in record.tasks[:2]) >= MIN_CLICKS)


def describe(record: BlockRecord | None) -> dict[str, Any] | None:
    """Auditable single-block description; never a claim of statistical confidence."""
    if record is None:
        return None
    low, high = record.band()
    peak: float | None = record.peak_gain()
    return {"index": record.index, "phase": record.phase, "valid": record.valid, "score": record.score,
            "stage_scores": [task.score for task in record.tasks],
            "hits": sum(task.hits for task in record.tasks),
            "misses": sum(task.misses for task in record.tasks),
            "quality_sufficient": quality(record),
            "active_start": record.active_start, "active_end": record.active_end,
            "band_gain_low": low, "band_gain_high": high,
            "band_cm360": [round(914.4 / (record.dpi * record.yaw * high), 3),
                           round(914.4 / (record.dpi * record.yaw * low), 3)] if low > 0 else None,
            "travel_log_ratio": round(record.travelled(), 6),
            "peak_gain": peak,
            "peak_cm360": round(914.4 / (record.dpi * record.yaw * peak), 3) if peak else None,
            "task_bands": [{"low_gain": low_gain, "high_gain": high_gain}
                           for low_gain, high_gain in zip(record.task_band_lows, record.task_band_highs)],
            "curve": [{"gain": gain, "score": score} for gain, score in record.curve()]}


class AdaptiveController:
    """Two blocks; each block climbs toward the sensitivity the user scores best at."""
    def __init__(self, settings: Settings, probe: str | None = None) -> None:
        self.settings: Settings = settings.validate()
        self.records: list[BlockRecord] = []
        self.completed: int = 0
        self.pending: BlockRequest | None = None
        self.ended: bool = False
        self.end_reason: str = ""

    def request(self) -> BlockRequest | None:
        if self.ended or self.completed >= MAX_BLOCKS:
            return None
        if self.pending is not None:
            return self.pending
        # Block 2 starts from the opposite end of the band, so the two blocks approach the peak
        # from different directions and a systematically one-sided search cannot hide itself.
        start: str = "更快的一端" if self.completed == 0 else "更慢的一端"
        self.pending = BlockRequest(self.completed + 1,
                                    f"第{self.completed + 1}块：三项各 {TASK_SECONDS:g} 秒，"
                                    f"灵敏度从{start}起步，"
                                    f"每 {WINDOW_SECONDS:g} 秒按你这一窗口的成绩决定往哪边走、走多大一步",
                                    "从快侧爬坡" if self.completed == 0 else "从慢侧爬坡")
        return self.pending

    def climb_for(self, index: int, block_index: int,
                  start_gain: float | None = None) -> HillClimb:
        """Fresh climber for task ``index`` of ``block_index`` (1-based).

        The first task in a block starts from that block's band edge. Later tasks may inherit
        the previous task's last *applied* gain while receiving a fresh score history, so task
        changes do not cause a sensitivity jump or compare scores from different task types.
        Block 1 starts at the fast (high-gain) edge and block 2 at the slow edge.
        """
        if block_index not in (1, 2):
            raise ValueError("测试块序号无效。")
        low, high = band_for(self.settings)
        start: float = (high if block_index == 1 else low) if start_gain is None else start_gain
        # The first step always pushes *into* the band, i.e. away from the edge the task starts on.
        return HillClimb(start_gain=start, floor_gain=low, ceiling_gain=high,
                         direction=-1.0 if block_index == 1 else 1.0)

    def band_for_task(self, index: int, block_index: int) -> tuple[float, float]:
        """The search band a task was allowed to roam, for the audit trail."""
        if block_index not in (1, 2):
            raise ValueError("测试块序号无效。")
        return band_for(self.settings)

    def validate_current_record(self, record: BlockRecord, interrupted: bool = False) -> None:
        """Validate pending-slot identity and non-overlapping active chronology."""
        request: BlockRequest | None = self.request()
        if request is None:
            raise ValueError("测评已结束，不能继续计分。")
        record.validate(self.settings)
        if (record.index != request.index or record.task_seed != request.task_seed
                or record.phase != ("中断" if interrupted else request.phase)):
            raise ValueError("测试块不属于当前固定参数请求。")
        if self.records and record.active_start + TIME_TOLERANCE < self.records[-1].active_end:
            raise ValueError("测试块活动时间发生重叠或倒退。")

    def accept(self, record: BlockRecord) -> None:
        self.validate_current_record(record)
        self.records.append(record)
        self.completed += 1
        self.pending = None
        if self.completed >= MAX_BLOCKS:
            self.finish(f"{MAX_BLOCKS} 个爬坡块已完成")

    def interrupt(self, record: BlockRecord) -> None:
        """Keep an invalid audit event but do not advance or consume an evidence slot."""
        if record.valid:
            raise ValueError("中断记录必须标记为无效。")
        self.validate_current_record(record, interrupted=True)
        self.records.append(record)

    def finish(self, reason: str) -> None:
        self.ended = True
        self.end_reason = reason

    def _resolution_band(self, gains: list[float], best_gain: float) -> tuple[float, float]:
        """Smallest honest interval around the winning bucket, in cm/360°.

        Buckets are discrete sample points: the region between two neighbouring buckets was never
        measured, so even a perfectly clean peak cannot be pinned down better than half a bucket
        spacing. The caller widens this further with any bucket scoring within PEAK_MARGIN.
        """
        per_cm: float = self.settings.dpi * self.settings.yaw

        def to_cm(gain: float) -> float:
            return 914.4 / (per_cm * gain)

        best_cm: float = to_cm(best_gain)
        ordered: list[float] = sorted(to_cm(gain) for gain in gains)
        position: int = min(range(len(ordered)), key=lambda index: abs(ordered[index] - best_cm))
        low: float = (ordered[position - 1] + best_cm) / 2 if position > 0 else best_cm
        high: float = (ordered[position + 1] + best_cm) / 2 if position + 1 < len(ordered) else best_cm
        return (low, high)

    def _recommendation(self, records: list[BlockRecord]) -> dict[str, Any]:
        """Pick one concrete recommendation from every usable window of both sweeps.

        Each sweated gain bucket pools the windows that landed on that gain (a few per task
        per block), so a bucket is only trustworthy when enough scored windows back it.
        Unbacked buckets are dropped: a single 1 s window that happens to score well is
        noise, not a peak.
        """
        baseline_cm: float = self.settings.distance
        baseline_val: float = self.settings.sensitivity
        usable: list[BlockRecord] = [item for item in records if quality(item)]
        if not usable:
            return {"recommended_cm360": baseline_cm, "recommended_sensitivity": baseline_val,
                    "recommendation_source": "baseline_fallback", "recommendation_confidence": "none",
                    "recommended_window_score": None,
                    "recommended_band_cm360": None,
                    "recommendation_reason": ("没有达到质量门槛的完整测评块，无法给出测量结论；"
                                              "该值直接取自你填写的原始灵敏度，不是本次测得结果，"
                                              "请完整重测后再确认。")}
        buckets: dict[float, list[float]] = {}
        for record in usable:
            for task in record.tasks:
                for gain, score in zip(task.window_gains, task.window_scores()):
                    buckets.setdefault(round(gain, 10), []).append(score)
        backed: dict[float, list[float]] = {gain: values for gain, values in buckets.items()
                                            if len(values) >= MIN_PEAK_WINDOWS}
        if not backed:
            backed = buckets
        averaged: list[tuple[float, float]] = sorted(
            ((gain, sum(values) / len(values)) for gain, values in backed.items()),
            key=lambda item: item[1], reverse=True)
        best_gain, best_score = averaged[0]
        runner_up: float = averaged[1][1] if len(averaged) > 1 else 0.0
        # A single "best" value would claim precision the measurement does not have: buckets sit
        # ~1-3 cm/360 apart and a one-second window carries only a few clicks. Report the interval
        # the winner can actually be told apart within, and say so in the reason.
        tie_gains: list[float] = [gain for gain, score in averaged
                                  if best_score - score <= PEAK_MARGIN]
        low_cm, high_cm = self._resolution_band([gain for gain, _ in averaged], best_gain)
        for gain in tie_gains:
            gain_cm: float = 914.4 / (self.settings.dpi * self.settings.yaw * gain)
            low_cm, high_cm = min(low_cm, gain_cm), max(high_cm, gain_cm)
        band_cm360: list[float] = [round(low_cm, 3), round(high_cm, 3)]
        if best_score < MIN_COMPOSITE or best_score - runner_up < PEAK_MARGIN:
            return {"recommended_cm360": baseline_cm, "recommended_sensitivity": baseline_val,
                    "recommendation_source": "baseline_fallback", "recommendation_confidence": "low",
                    "recommended_window_score": best_score,
                    "recommended_band_cm360": None,
                    "recommendation_reason": (f"各灵敏度的成绩没有拉开差距（最佳窗口 {best_score:.1f} 分，"
                                              f"与次佳仅差 {best_score - runner_up:.1f} 分）；"
                                              "分数接近时不能断言哪个更好，建议保留当前灵敏度并复测。")}
        best_cm: float = round(min(500.0, max(0.5, 914.4 / (self.settings.dpi * self.settings.yaw * best_gain))), 8)
        return {"recommended_cm360": best_cm,
                "recommended_sensitivity": sensitivity_for(best_cm, self.settings.dpi, self.settings.yaw),
                "recommendation_source": "hill_peak", "recommendation_confidence": "high",
                "recommended_window_score": best_score,
                "recommended_band_cm360": band_cm360,
                "recommendation_reason": (f"按成绩自动收窄后，成绩最好的一档出现在 {best_cm:.2f} cm/360°"
                                          f"（该窗口 {best_score:.1f} 分，领先次佳 {best_score - runner_up:.1f} 分）；"
                                          f"本次采样下 {band_cm360[0]:.2f}–{band_cm360[1]:.2f} cm/360° "
                                          "与最好成绩不可区分（相邻档之间未测量，故不可能更精确），"
                                          "建议在该区间内按手感取舍；"
                                          "这是单次动态调整的待验证候选，请在真实游戏与复测中核对。")}

    def summary(self) -> dict[str, Any]:
        usable: list[BlockRecord] = [item for item in self.records if item.valid]
        result: dict[str, Any] = {
            "conclusion": "insufficient", "message": "", "ended": self.ended,
            "end_reason": self.end_reason, "completed_blocks": self.completed,
            "valid_blocks": len(usable), "budget_seconds": BUDGET_SECONDS,
            "task_seconds": TASK_SECONDS, "window_seconds": WINDOW_SECONDS,
            "hill_fast_factor": HILL_FAST_FACTOR, "hill_slow_factor": HILL_SLOW_FACTOR,
            "baseline_cm360": self.settings.distance, "baseline_sensitivity": self.settings.sensitivity,
            "candidate_cm360": None, "candidate_sensitivity": None,
            "retained_cm360": self.settings.distance, "retained_sensitivity": self.settings.sensitivity,
            "blocks": [describe(item) for item in self.records],
        }
        if usable:
            low = min(item.band()[0] for item in usable)
            high = max(item.band()[1] for item in usable)
            result["visited_band_gain"] = [low, high]
            result["visited_band_cm360"] = [round(914.4 / (self.settings.dpi * self.settings.yaw * high), 3),
                                            round(914.4 / (self.settings.dpi * self.settings.yaw * low), 3)]
        recommendation: dict[str, Any] = self._recommendation(self.records)
        result.update(recommendation)
        # The retained value is exactly what we tell the user to actually use.
        result["retained_cm360"] = recommendation["recommended_cm360"]
        result["retained_sensitivity"] = recommendation["recommended_sensitivity"]
        if not usable:
            result["message"] = ("没有完成的有效测评块；无法给出测量结论，"
                                 "仅保留你填写的原始灵敏度作为参考。")
        elif recommendation["recommendation_source"] == "hill_peak":
            result["conclusion"] = "candidate"
            result["candidate_cm360"] = recommendation["recommended_cm360"]
            result["candidate_sensitivity"] = recommendation["recommended_sensitivity"]
            result["message"] = ("按成绩自动调整后出现了明显更好的一档，已给出待验证候选；"
                                 "单次动态调整无法确认可重复性，请复测并与真实游戏手感对照。")
        else:
            result["conclusion"] = "keep"
            result["message"] = ("各灵敏度成绩没有拉开差距；"
                                 "建议保留当前灵敏度，换时间复测再看趋势。")
        if not self.ended:
            result["message"] = "测评进行中：" + result["message"]
        return result

    def payload(self, elapsed: float, comfort: str = "") -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "mode": MODE, "settings": asdict(self.settings),
                "budget_seconds": BUDGET_SECONDS,
                "active_elapsed": min(BUDGET_SECONDS, max(0.0, elapsed)),
                "completed_blocks": self.completed, "ended": self.ended, "end_reason": self.end_reason,
                "blocks": [asdict(item) | {"score": item.score, "curve": [list(pair) for pair in item.curve()],
                                           "stage_scores": [task.score for task in item.tasks],
                                           "window_scores": [list(task.window_scores()) for task in item.tasks]}
                           for item in self.records],
                "summary": self.summary(), "comfort_note": str(comfort)[:500],
                "method": {"blocks": MAX_BLOCKS, "block_seconds": BLOCK_SECONDS,
                           "task_seconds": TASK_SECONDS, "window_seconds": WINDOW_SECONDS,
                           "windows_per_task": WINDOWS_PER_TASK, "task_seed": TASK_SEED,
                           "warmup_seconds": WARMUP_SECONDS,
                           "search_band": (f"cm/360° ÷{HILL_FAST_FACTOR:g} ↔ ÷{HILL_SLOW_FACTOR:g}"
                                           "（÷1.25 为更快的高增益端，÷0.8 为更慢的低增益端）；"
                                           "块1 从快侧起步，块2 从慢侧起步"),
                           "hill_climb": (f"每 {WINDOW_SECONDS:g} 秒结算一次：比上一窗口提高 >={HILL_IMPROVEMENT:g} 分"
                                          "则同向继续并可能升档，下降超过该值则反向并缩小步长，"
                                          f"两者之间视为噪声原地不动；档位步长 {HILL_GEARS[0]:g}→{HILL_GEARS[-1]:g}"),
                           "click_score": ("100*exp(-min(命中间隔,超时)/衰减常数)*准确率²；"
                                           f"衰减常数 {SPEED_DECAY_SECONDS:g}s，间隔 >{HIT_TIMEOUT_SECONDS:g}s 记为超时并计入准确率分母"),
                           "tracking_score": f"100*跟踪秒数/{WINDOW_SECONDS:g}，每窗口独立计分",
                           "composite": "0.75*geometric_mean(three_task_scores)+0.25*min(three_task_scores)",
                           "peak_rule": (f"把所有窗口按生效灵敏度归桶取均分，取最高者；该桶均分需>={MIN_COMPOSITE:g}"
                                         f"且领先次佳>={PEAK_MARGIN:g}分，否则视为无差距并保留原设置"),
                           "ranking": "两次爬坡合并后的最好一档；单次运行不构成统计结论"},
                "limitations": ("仅本工具角度平面腰射任务；启发式而非学术最佳；灵敏度按成绩动态变化，"
                                f"单窗口仅 {WINDOW_SECONDS:g} 秒、样本量小，最好的一档可能是噪声；"
                                "不修改真实游戏、系统鼠标或 DPI；暂停时间不计活动预算。")}


def load_adaptive(path: Path) -> tuple[AdaptiveController, float, str]:
    """Load history as read-only evidence; never trust stored aggregate scores."""
    try:
        if path.stat().st_size > 2_000_000:
            raise ValueError("结果文件超过 2 MB。")
        data = json.loads(path.read_text(encoding="utf-8"))
        version: Any = data.get("schema_version")
        if version in (2, 3, 4, 5):
            label = {2: "旧版四分钟", 3: "旧版 90 秒（每项任务 3 秒）",
                     4: "旧版 90 秒（每项任务 14 秒、固定灵敏度）",
                     5: "旧版 50 秒（灵敏度按固定曲线单向扫动）"}[version]
            raise ValueError(f"这是{label}结果；本版每项任务 {TASK_SECONDS:g} 秒且灵敏度按你的成绩动态调整，"
                             "不能混用或续测。")
        if version != SCHEMA_VERSION or data.get("mode") != MODE:
            raise ValueError("这不是本版动态调整测评结果。")
        if data.get("budget_seconds") != BUDGET_SECONDS:
            raise ValueError("历史测试预算与当前版本不一致。")
        method: Any = data.get("method")
        if not isinstance(method, dict) or (method.get("task_seconds") != TASK_SECONDS
                                            or method.get("window_seconds") != WINDOW_SECONDS
                                            or method.get("windows_per_task") != WINDOWS_PER_TASK):
            raise ValueError("历史测试的任务时长或计分窗口与当前版本不一致。")
        controller = AdaptiveController(Settings(**data["settings"]))
        if len(data["blocks"]) > MAX_AUDIT_EVENTS:
            raise ValueError("历史块数量超出限制。")
        elapsed: float = float(data["active_elapsed"])
        if not math.isfinite(elapsed) or not 0 <= elapsed <= BUDGET_SECONDS:
            raise ValueError("历史预算无效。")
        snapshot_ended: bool = data["ended"]
        snapshot_completed: int = data["completed_blocks"]
        if type(snapshot_ended) is not bool or type(snapshot_completed) is not int:
            raise ValueError("历史会话完成状态字段无效。")
        previous_wall_end: datetime | None = None
        for raw in data["blocks"]:
            fields = {key: value for key, value in raw.items()
                      if key not in ("score", "stage_scores", "curve", "window_scores")}
            fields["tasks"] = tuple(
                TaskSample(**(task | {"latencies": tuple(task["latencies"]),
                                      "window_hits": tuple(task["window_hits"]),
                                      "window_misses": tuple(task["window_misses"]),
                                      "window_tracking": tuple(task["window_tracking"]),
                                      "window_gains": tuple(task["window_gains"]),
                                      "window_gaps": tuple(tuple(gaps) for gaps in task.get("window_gaps", ())),
                                      "window_timeouts": tuple(task.get("window_timeouts", ()))}))
                for task in raw["tasks"])
            fields["task_band_lows"] = tuple(fields.get("task_band_lows", ()))
            fields["task_band_highs"] = tuple(fields.get("task_band_highs", ()))
            record = BlockRecord(**fields).validate(controller.settings)
            wall_start: datetime = aware_timestamp(record.started_at)
            wall_end: datetime = aware_timestamp(record.ended_at)
            if previous_wall_end is not None and wall_start < previous_wall_end:
                raise ValueError("历史测试块日期时间发生重叠或倒退。")
            if (wall_end - wall_start).total_seconds() + TIME_TOLERANCE < record.active_end - record.active_start:
                raise ValueError("历史块的日期时间跨度短于声称的活动时长。")
            if record.active_end > elapsed + TIME_TOLERANCE:
                raise ValueError("历史总活动时长未覆盖测试块结束时间。")
            previous_wall_end = wall_end
            if record.valid or record.phase != "中断":
                controller.accept(record)
            else:
                controller.interrupt(record)
        if snapshot_completed != controller.completed:
            raise ValueError("历史完成块数量与原始记录不一致。")
        if not snapshot_ended and controller.completed >= MAX_BLOCKS:
            raise ValueError("进行中快照不能同时声明全部测试块已经完成。")
        if snapshot_ended:
            controller.finish(str(data.get("end_reason", "已结束历史")))
        else:
            # Import is read-only in the UI; it must not promote evidence to a finished conclusion.
            controller.ended = False
            controller.end_reason = str(data.get("end_reason", ""))
        return controller, elapsed, str(data.get("comfort_note", ""))[:500]
    except (KeyError, TypeError, AttributeError, OverflowError, json.JSONDecodeError) as error:
        raise ValueError("自适应结果文件损坏或结构不正确。") from error
