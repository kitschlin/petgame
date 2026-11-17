"""对阵引擎：用规则引擎把两套阵容打若干局，得到胜 / 平 / 负，并缓存结果。

- 阵容是**有序**的（顺序即站位）。站位会明显影响胜负：集火目标的先后、小猫溢出
  伤害的传递都按站位来，所以对阵结果按有序阵容记录，不做「同一组宠物」的合并。
- 我方固定用启发式出招；对手方的打法可选：启发式（同屏手动 / 智能对手）或
  「随机模式」的真实打法（电脑随机对手）。
- 种子由 crc32(阵容 + 局号) 生成，同一组对阵的结果可以复现。不用 Python 的
  hash()：它在不同进程之间是随机化的。
- 基础属性下的全部对阵由 scripts/prewarm_matchups.py 离线算好（预算表）；属性有
  PVE 成长时现场计算，并按属性哈希写入存档目录，下一局直接复用。
"""

from __future__ import annotations

import atexit
import hashlib
import itertools
import json
import os
import random
import threading
import time
import zlib
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from ..engine import Battle
from ..engine.pets import BASE_STATS, SIDES, SPECIES_ORDER, STAT_KEYS, TEAM_SIZE
from .policy import HEURISTIC, POLICIES

Lineup = tuple[str, ...]
Roster = dict[str, dict[str, dict[str, int]]]

ORDERED_LINEUPS: list[Lineup] = list(itertools.permutations(SPECIES_ORDER, TEAM_SIZE))  # 336
LINEUP_INDEX: dict[Lineup, int] = {lineup: i for i, lineup in enumerate(ORDERED_LINEUPS)}

BASE_TABLE_PATH = Path(__file__).resolve().parent / "data" / "matchups-base.json"
DEFAULT_GAMES = 16  # 现场计算时每组对阵打几局
POOL_THRESHOLD = 1500  # 现场计算超过这么多局才动用多进程
FRESH_START = 1_000_000  # 复核用的局号从这里开始：与预算表、缓存用过的种子都不重合


# ---------------------------------------------------------------- 属性

def normalize_roster(roster: Roster) -> Roster:
    """只保留四项永久属性（去掉 battle_speed 之类的派生字段）。"""
    return {
        side: {key: {stat: int(roster[side][key][stat]) for stat in STAT_KEYS} for key in SPECIES_ORDER}
        for side in SIDES
    }


def base_roster() -> Roster:
    return normalize_roster(BASE_STATS)


def roster_hash(roster: Roster) -> str:
    text = json.dumps(normalize_roster(roster), sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(text.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- 模拟

def _lineup(roster: Roster, side: str, species: Sequence[str]) -> list[dict[str, Any]]:
    return [{"species": key, **{stat: roster[side][key][stat] for stat in STAT_KEYS}} for key in species]


def matchup_seed(my: Sequence[str], opp: Sequence[str], game: int) -> int:
    return zlib.crc32(f"{','.join(my)}|{','.join(opp)}|{game}".encode())


def play(
    roster: Roster, my: Sequence[str], opp: Sequence[str], seed: int,
    opp_policy: str = HEURISTIC, me_policy: str = HEURISTIC,
) -> str:
    """打一局，返回 "me" / "opp" / "draw"。"""
    policies = {"me": POLICIES[me_policy], "opp": POLICIES[opp_policy]}
    rng = random.Random(seed)
    battle = Battle.new({"me": _lineup(roster, "me", my), "opp": _lineup(roster, "opp", opp)}, seed, record=False)
    while not battle.finished:
        battle.act(*policies[battle.pets[battle.actor].side](battle, rng))
    return battle.winner


def simulate(
    roster: Roster, my: Sequence[str], opp: Sequence[str], games: int,
    opp_policy: str = HEURISTIC, me_policy: str = HEURISTIC, start: int = 0,
) -> tuple[int, int]:
    """打 games 局（局号从 start 开始），返回（我方胜场，平局数）。"""
    win = draw = 0
    for game in range(start, start + games):
        winner = play(roster, my, opp, matchup_seed(my, opp, game), opp_policy, me_policy)
        win += winner == "me"
        draw += winner == "draw"
    return win, draw


def _simulate_batch(args: tuple) -> list[tuple[int, int]]:
    roster, pairs, games, opp_policy, start = args
    return [simulate(roster, my, opp, games, opp_policy, start=start) for my, opp in pairs]


@dataclass(frozen=True)
class Result:
    win: int
    draw: int
    games: int

    @property
    def loss(self) -> int:
        return self.games - self.win - self.draw

    @property
    def win_rate(self) -> float:
        return self.win / self.games

    @property
    def draw_rate(self) -> float:
        return self.draw / self.games

    @property
    def value(self) -> float:
        """局面价值 = 胜率 + 0.5 × 平局率。"""
        return (self.win + 0.5 * self.draw) / self.games


# ---------------------------------------------------------------- 预算表

class BaseTable:
    """基础属性下全部 336 × 336 组有序对阵的结果，每种对手打法一张表。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self.hash: str = data["roster_hash"]
        self.games: int = data["games"]
        self.tables: dict[str, dict[str, list[list[int]]]] = data["tables"]
        self._values: dict[str, list[list[float]]] = {}
        if data.get("lineups") != [",".join(lineup) for lineup in ORDERED_LINEUPS]:
            raise ValueError("预算表的阵容顺序与当前宠物列表不一致")

    def has(self, policy: str) -> bool:
        return policy in self.tables

    def result(self, policy: str, my: Lineup, opp: Lineup) -> Result:
        table = self.tables[policy]
        i, j = LINEUP_INDEX[my], LINEUP_INDEX[opp]
        return Result(table["win"][i][j], table["draw"][i][j], self.games)

    def values(self, policy: str) -> list[list[float]]:
        """价值矩阵 [我方阵容序号][对手阵容序号]，第一次用到时算好（选将博弈树要查十几万次）。"""
        cached = self._values.get(policy)
        if cached is None:
            table, games = self.tables[policy], self.games
            cached = self._values[policy] = [
                [(win + 0.5 * draw) / games for win, draw in zip(wins, draws)]
                for wins, draws in zip(table["win"], table["draw"])
            ]
        return cached


@lru_cache(maxsize=4)
def _load_base_table(path: str, mtime: float) -> BaseTable | None:
    try:
        with open(path, encoding="utf-8") as f:
            return BaseTable(json.load(f))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def load_base_table(path: str | Path = BASE_TABLE_PATH) -> BaseTable | None:
    """读取预算表；文件不存在或格式不对时返回 None（退回现场计算）。"""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return _load_base_table(str(path), mtime)


# ---------------------------------------------------------------- 多进程

MAIN = "main"  # 后台和批量任务用的进程池：对阵推演、对局结束后的定向推演
FAST = "fast"  # 战斗指导用的进程池：玩家正等着，不能排在后台任务后面
FAST_CAP = 64  # 战斗指导的进程池最多开这么多个进程
ADVICE_GAMES_MIN, ADVICE_GAMES_MAX = 128, 1024  # 战斗指导每个动作推演局数的自动取值范围


def fast_workers() -> int:
    """战斗指导专用进程池的大小：玩家正等着，尽量用上机器的核（留一个给服务），最多 FAST_CAP 个。
    环境变量 COACH_FAST_WORKERS 可以改。"""
    try:
        configured = int(os.environ.get("COACH_FAST_WORKERS", ""))
    except ValueError:
        configured = -1
    if configured >= 1:
        return configured
    return max(1, min(FAST_CAP, (os.cpu_count() or 1) - 1))


FAST_WORKERS = fast_workers()


def auto_advice_games(workers: int | None = None) -> int:
    """战斗指导每个动作推演几局（没有用 COACH_ADVICE_ROLLOUTS 指定时）：机器越强推演越多。

    每个进程分到大约 16 局（取 64 的整数倍），限制在 128～1024 之间。实测（智能对手的模型最贵）：
    64 个进程时 1024 局约 2 秒；局数再翻倍到 2048 要 3 秒多，误差只从 ±1.6 降到 ±1.1 个百分点，
    不值得让玩家多等。手动、随机对手的推演便宜得多，1024 局也只要零点几秒。
    """
    workers = FAST_WORKERS if workers is None else workers
    return max(ADVICE_GAMES_MIN, min(ADVICE_GAMES_MAX, 16 * workers // 64 * 64))
_pools: dict[str, ProcessPoolExecutor] = {}
_pool_lock = threading.Lock()


def default_workers() -> int:
    try:
        configured = int(os.environ.get("COACH_WORKERS", ""))
    except ValueError:
        configured = -1
    if configured >= 0:
        return configured
    return max(0, min(16, (os.cpu_count() or 1) - 1))


def _worker_init(parent_pid: int) -> None:
    """推演进程盯着父进程：服务退出（哪怕是被强行结束）后跟着退出，不留孤儿进程。"""
    def watch() -> None:
        while os.getppid() == parent_pid:
            time.sleep(2)
        os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


def _get_pool(workers: int, lane: str = MAIN) -> ProcessPoolExecutor:
    with _pool_lock:
        pool = _pools.get(lane)
        if pool is None:
            import multiprocessing

            # spawn：服务是多线程的，不能在持锁状态下 fork
            pool = _pools[lane] = ProcessPoolExecutor(
                max_workers=FAST_WORKERS if lane == FAST else workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_worker_init, initargs=(os.getpid(),),
            )
        return pool


def shutdown_pool() -> None:
    with _pool_lock:
        for pool in _pools.values():
            pool.shutdown(wait=False, cancel_futures=True)
        _pools.clear()


atexit.register(shutdown_pool)


# ---------------------------------------------------------------- 对阵表

class MatchupBook:
    """某一套锁定属性、某一种对手打法下的对阵结果：查预算表 → 查缓存 → 现场模拟。"""

    def __init__(
        self,
        roster: Roster,
        *,
        opp_policy: str = HEURISTIC,
        games: int = DEFAULT_GAMES,
        base: BaseTable | None = None,
        cache_path: str | Path | None = None,
        workers: int = 0,
    ) -> None:
        if opp_policy not in POLICIES:
            raise ValueError(f"未知的对手打法：{opp_policy}")
        self.roster = normalize_roster(roster)
        self.hash = roster_hash(self.roster)
        self.opp_policy = opp_policy
        self.games = games
        self.workers = workers
        self._base = base if base is not None and base.hash == self.hash and base.has(opp_policy) else None
        self._cache: dict[str, tuple[int, int, int]] = {}
        self._verified: dict[tuple[str, int], Result] = {}
        self._cache_path = Path(cache_path) if cache_path else None
        self._dirty = False
        self._lock = threading.Lock()
        self.lookups = 0
        self.hits = 0
        self.simulated = 0  # 现场模拟的对阵数
        self._load()

    @property
    def complete(self) -> bool:
        """全部对阵都已算好（命中预算表），可以放心穷举。"""
        return self._base is not None

    @staticmethod
    def key(my: Sequence[str], opp: Sequence[str]) -> str:
        return f"{','.join(my)}|{','.join(opp)}"

    def get(self, my: Sequence[str], opp: Sequence[str], games: int | None = None) -> Result:
        return self.get_many([(tuple(my), tuple(opp))], games)[0]

    def get_many(self, pairs: Sequence[tuple[Lineup, Lineup]], games: int | None = None) -> list[Result]:
        """批量查询；没算过的对阵一次性模拟（数量多时用多进程）。

        games 是现场模拟时每组对阵至少要打的局数（默认 self.games）；缓存里局数
        不够的会按新的局数重算。命中预算表时忽略。
        """
        games = games or self.games
        results: list[Result | None] = [None] * len(pairs)
        missing: dict[str, tuple[Lineup, Lineup]] = {}
        with self._lock:
            for index, (my, opp) in enumerate(pairs):
                self.lookups += 1
                if self._base is not None:
                    results[index] = self._base.result(self.opp_policy, tuple(my), tuple(opp))
                    self.hits += 1
                    continue
                cached = self._cache.get(self.key(my, opp))
                if cached is not None and cached[2] >= games:
                    results[index] = Result(*cached)
                    self.hits += 1
                else:
                    missing.setdefault(self.key(my, opp), (tuple(my), tuple(opp)))
        if missing:
            computed = self._simulate(list(missing.values()), games)
            with self._lock:
                for key, (win, draw) in zip(missing, computed):
                    self._cache[key] = (win, draw, games)
                self.simulated += len(missing)
                self._dirty = True
                for index, (my, opp) in enumerate(pairs):
                    if results[index] is None:
                        results[index] = Result(*self._cache[self.key(my, opp)])
        return results  # type: ignore[return-value]

    def cost(self, pairs: int, games: int | None = None) -> int:
        """查询这么多组对阵最多要现场打多少局（命中预算表时为 0）。"""
        return 0 if self._base is not None else pairs * (games or self.games)

    def verify(self, pairs: Sequence[tuple[Lineup, Lineup]], games: int) -> list[Result]:
        """用从没用过的新种子把这几组对阵重打一遍。

        从很多候选里挑「估计值最高」的那个，它的估计值多半偏高（挑中的往往是运气好的）。
        选定之后再用新种子打一遍，得到的数字与挑选过程无关，才是可以展示给玩家的胜率。
        """
        todo = [(tuple(my), tuple(opp)) for my, opp in pairs
                if (self.key(my, opp), games) not in self._verified]
        if todo:
            for (my, opp), (win, draw) in zip(todo, self._simulate(todo, games, FRESH_START)):
                self._verified[(self.key(my, opp), games)] = Result(win, draw, games)
        return [self._verified[(self.key(my, opp), games)] for my, opp in pairs]

    def _simulate(self, pairs: list[tuple[Lineup, Lineup]], games: int, start: int = 0) -> list[tuple[int, int]]:
        if self.workers > 1 and len(pairs) * games >= POOL_THRESHOLD:
            try:
                pool = _get_pool(self.workers)
                size = max(1, -(-len(pairs) // (self.workers * 2)))
                chunks = [pairs[i:i + size] for i in range(0, len(pairs), size)]
                jobs = [(self.roster, chunk, games, self.opp_policy, start) for chunk in chunks]
                return [result for batch in pool.map(_simulate_batch, jobs) for result in batch]
            except Exception:  # 进程池不可用（例如受限环境）：退回单进程
                shutdown_pool()
        return _simulate_batch((self.roster, pairs, games, self.opp_policy, start))

    # ---- 持久化（只用于现场计算的结果）

    def _load(self) -> None:
        if self._cache_path is None or self._base is not None:
            return
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("roster_hash") != self.hash or data.get("opp_policy") != self.opp_policy:
            return
        for key, value in (data.get("results") or {}).items():
            if isinstance(value, list) and len(value) == 3:
                self._cache[key] = (int(value[0]), int(value[1]), int(value[2]))

    def save(self) -> None:
        if self._cache_path is None:
            return
        with self._lock:
            if not self._dirty:
                return
            data = {
                "roster_hash": self.hash, "opp_policy": self.opp_policy,
                "results": {key: list(value) for key, value in self._cache.items()},
            }
            self._dirty = False
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_name(self._cache_path.name + ".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self._cache_path)


# ---------------------------------------------------------------- 方案评估

def opp_completions(opp_picked: Sequence[str]) -> list[Lineup]:
    """对手所有可能的最终阵容：已选的在前，其余按可能的选择顺序排列（顺序即站位）。"""
    picked = tuple(opp_picked)
    rest = [key for key in SPECIES_ORDER if key not in picked]
    return [picked + extra for extra in itertools.permutations(rest, TEAM_SIZE - len(picked))]


def my_completions(my_picked: Sequence[str]) -> list[Lineup]:
    """我方所有合法的最终阵容：已选的在前，其余任意顺序。"""
    picked = tuple(my_picked)
    rest = [key for key in SPECIES_ORDER if key not in picked]
    return [picked + extra for extra in itertools.permutations(rest, TEAM_SIZE - len(picked))]


@dataclass(frozen=True)
class PlanScore:
    lineup: Lineup
    worst: float  # 对手最强应对时的局面价值
    worst_reply: tuple[str, ...]  # 对应的对手补选（第 6 手时为空）
    mean: float  # 对手所有补法的平均价值
    robust: float  # 排序依据
    win_rate: float  # 平均胜率（不含平局）
    worst_win_rate: float  # 对手最强应对时的胜率（不含平局）
    replies: int  # 对手补法数
    samples: int = 0  # 这个估计一共基于多少局


def robust_score(worst: float, mean: float, worst_weight: float) -> float:
    return worst_weight * worst + (1 - worst_weight) * mean


# 对手补选模型：给定我方计划的完整阵容，返回对手各种最终阵容的概率
ReplyModel = Callable[[Lineup], dict[Lineup, float]]


def uniform_replies(opp_picked: Sequence[str]) -> ReplyModel:
    """不知道对手会怎么补：所有补法等概率。"""
    replies = opp_completions(opp_picked)
    table = {reply: 1 / len(replies) for reply in replies}
    return lambda lineup: table


def evaluate_plans(
    book: MatchupBook, lineups: Sequence[Lineup], opp_picked: Sequence[str], worst_weight: float = 0.5,
    games: int | None = None, reply_model: ReplyModel | None = None,
) -> list[PlanScore]:
    """每套我方阵容对上对手可能的补选。

    reply_model 给出对手补选的概率分布，默认所有补法等概率。mean 是按概率的期望，
    worst 是有可能出现的补法里最差的一种。worst_weight 是 worst 在排序依据里的权重：
    对手会针对我方时取 0.5（最差和平均各一半）；对手随机选将时取 0（只看平均）。
    """
    model = reply_model or uniform_replies(opp_picked)
    done = len(tuple(opp_picked))
    supports = [[(reply, prob) for reply, prob in model(tuple(lineup)).items() if prob > 0] for lineup in lineups]
    results = iter(book.get_many(
        [(tuple(lineup), reply) for lineup, support in zip(lineups, supports) for reply, _ in support], games))
    scores = []
    for lineup, support in zip(lineups, supports):
        row = [next(results) for _ in support]
        values = [result.value for result in row]
        worst = min(values)
        at = values.index(worst)
        mean = sum(prob * value for (_, prob), value in zip(support, values))
        scores.append(PlanScore(
            lineup=tuple(lineup),
            worst=worst,
            worst_reply=support[at][0][done:],
            mean=mean,
            robust=robust_score(worst, mean, worst_weight),
            win_rate=sum(prob * result.win_rate for (_, prob), result in zip(support, row)),
            worst_win_rate=row[at].win_rate,
            replies=len(support),
            samples=sum(result.games for result in row),
        ))
    return scores


def best_plan(
    book: MatchupBook, my_picked: Sequence[str], opp_picked: Sequence[str], worst_weight: float = 0.5,
    games: int | None = None, reply_model: ReplyModel | None = None,
) -> PlanScore:
    """引擎穷举全部合法阵容，取 robust 最高的一套（并列时取靠前的，结果确定）。"""
    scores = evaluate_plans(book, my_completions(my_picked), opp_picked, worst_weight, games, reply_model)
    return max(scores, key=lambda score: score.robust)


def iter_pairs(lineups: Iterable[Lineup] = ORDERED_LINEUPS) -> Iterable[tuple[Lineup, Lineup]]:
    lineups = list(lineups)
    return ((my, opp) for my in lineups for opp in lineups)
