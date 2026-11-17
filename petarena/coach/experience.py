"""经验反哺选将：把经验库里的战绩叠加到对阵表上，让军师的数字跟着真实对局走。

对阵表的口径是「我方按常规打法出招」。玩家照着战斗指导打，或者对手比表里假设的
更强（智能对手用推演出招）时，表上的数字就不准了，排名也会变。这里做两件事：

1. ExperienceBook：包在对阵表外面、接口相同，军师看到的每个数字都是修正后的：

       价值 = 表值 + q × 口径差，再把这组对阵的真实战绩并进去

   口径差 = 照指导出招的整局推演结果 − 当时的表值，来自经验库。没有推演过的对阵，
   依次借用「同一组成员、同一套对手阵容」「同一套对手阵容」「这类对手的全局平均」，
   逐级收缩（实测：同组成员对同一套对手阵容的口径差很接近；同一套阵容对不同对手的
   口径差几乎没有参考价值，所以不借）。借来的证据打折扣：上一级只有一两组独立的
   推演时不能全信，所以推演过的那组对阵，修正量总是不小于借用它的其他对阵。
   全局平均只是为了不让「推演过」本身成为加分。
   q 是玩家的操作离常规打法和推演最优各有多远（见 Memory.quality）。
   属性变了之后旧属性下的战绩不能直接用，但「口径差」可以：用新属性下的表值加上
   旧的口径差（实测误差接近抽样噪声），旧属性的记录权重减半。
   经验库是空的时，它原样返回对阵表的结果。

2. reflect：每场对局之后，针对刚遇到的那套对手阵容，用「照指导出招」把我方最有希望
   的十来套阵容各打几局，逐轮淘汰，结果写进经验库。真实对局决定算力花在哪里。
   对手的选将会跟着我方变（智能对手）时，「换一套阵容对上同一套对手阵容」是个假问题：
   我方换了，它也会换。这时每套我方阵容对上的是它按选将习惯最可能拿出的那几套（foreseen）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

from . import rollout
from .draft import PREDICTED, OpponentProfile, reply_model_for
from .matchup import ORDERED_LINEUPS, Lineup, MatchupBook, Result, base_roster, roster_hash
from .memory import GUIDED, QUALITY_NEW, REAL, Memory, signature
from .review import decide_verdict

SEED_PATH = Path(__file__).resolve().parent / "data" / "experience-base.json"  # 内置经验（scripts/prewarm_experience.py 生成）

SHRINK = 16.0  # 口径差的收缩强度：相当于多少局推演
# 借用上一级的证据时，按「有几组独立的推演」再收缩一次：相当于先验里已经有这么多组说「没有差别」
BORROW_ORDERS = 0.5  # 同一组成员的其他站位：很接近，一两组就基本可信
BORROW_MEMBERS = 1.0  # 同一套对手阵容下的其他成员组合
BORROW_OPPONENTS = 2.0  # 其他对手阵容（全局平均）
REAL_PRIOR = 16.0  # 并入真实战绩时，修正后的估计至少相当于这么多局；这组对阵推演过多少局，再加多少
STALE = 0.5  # 属性变了之后，旧属性下的记录打几折
GUIDED_GAMES = 16  # 整局推演时「照指导出招」每个动作推演几局（比战斗指导少一些，换速度）
CANDIDATES = 12  # 定向推演时考察多少组成员（每组取表上最好的站位）
REPLIES = 2  # 对手的选将会跟着我方变时，每套我方阵容对上它最可能拿出的这几套
REACTIVE_CANDIDATES = 8  # 这种情况下每套阵容要打好几套对手阵容，考察的组数相应少一些
REFLECT_UNIT = 160  # 预算是这么多局时，各轮的局数就是下面写的；预算不同就按比例缩放
ROUNDS = (("a", 3, 6), ("b", 5, 3), ("c", 8, 2))  # 逐轮淘汰：(名字, 每套阵容一共打几局, 这一轮之后留下几套)
FRESH = 16  # 最后用新种子重估时每套阵容一共打几局
KNOWN_CANDIDATES = 3  # 有内置经验的对手：复盘时拿现有证据里最好的这几组成员和实际阵容比
TOP_UP = 48  # ……一套阵容对上它会遇到的对手阵容，推演过的局数不到这么多就补齐
LESSON_LIMIT = 4


@dataclass(frozen=True)
class Adjusted:
    """修正后的对阵结果：接口与 matchup.Result 相同（value / win_rate / draw_rate / games）。"""

    value: float
    win_rate: float
    draw_rate: float
    games: int
    base: Result = field(compare=False)


def _clip(value: float) -> float:
    return min(1.0, max(0.0, value))


@lru_cache(maxsize=4)
def _load_seed(path: str, mtime: float) -> dict[str, tuple]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    roster = base_roster()
    if data.get("version") != 1 or data.get("roster") != roster_hash(roster):
        return {}  # 为另一套基础属性生成的：不能用
    return {
        mode: tuple({
            "opponent": mode, "my_lineup": row["my"], "opp_lineup": row["opp"], "my_sig": signature(roster, row["my"]),
            "source": GUIDED, "win": row["win"], "draw": row["draw"], "games": row["games"], "p0": row["p0"], "seed": True,
        } for row in entry["rows"])
        for mode, entry in data.get("modes", {}).items()
    }


def load_seed(opponent: str, path: str | Path = SEED_PATH) -> tuple:
    """内置经验：离线把「照战斗指导出招」对上这类对手的阵容普查了一遍，形状与 Memory.experience() 的行相同。

    新玩家的经验库是空的，而对上智能对手，对阵表（双方常规打法）几乎没有参考价值；有了这份
    内置经验，第 1 局的推荐就是按「照指导出招」算的。文件不存在、或不是按当前基础属性生成的，就没有。
    """
    path = Path(path)
    try:
        return _load_seed(str(path), path.stat().st_mtime).get(opponent, ())
    except (OSError, ValueError, KeyError, TypeError):
        return ()


class ExperienceBook:
    def __init__(self, book: MatchupBook, memory: Memory | None, opponent: str, seed: Sequence[dict[str, Any]] = (),
                 q: float | None = None, real: bool = True) -> None:
        """seed 是内置经验（load_seed），和玩家自己的经验一起用。
        q 给了就用它代替玩家口径（复盘下结论时按「照指导出招」算，q = 1）；real=False 表示不并入真实战绩。"""
        self.book = book
        self.opponent = opponent
        rows = list(seed) + (memory.experience(opponent) if memory is not None else [])
        if not real:
            rows = [row for row in rows if row["source"] != REAL]
        self.quality = memory.quality() if memory is not None else {"q": QUALITY_NEW, "battles": 0}
        self.q = self.quality["q"] if q is None else q
        self._pair: dict[tuple[Lineup, Lineup], list[float]] = {}  # 各级：[Σ 局数 × 口径差, 局数, 下一级有几组]
        self._members: dict[tuple[frozenset, Lineup], list[float]] = {}
        self._opp: dict[Lineup, list[float]] = {}
        self._all = [0.0, 0.0, 0.0]
        self._real: dict[tuple[Lineup, Lineup], list[float]] = {}  # 加权得分、加权局数、胜、平、负
        self._guided: dict[tuple[Lineup, Lineup], float] = {}
        self._seeded: dict[tuple[Lineup, Lineup], float] = {}  # 其中来自内置经验的局数
        self._current: dict[tuple[Lineup, Lineup], float] = {}  # 其中是在我方现在的属性下推演的局数
        self._scored: dict[tuple[Lineup, Lineup], float] = {}  # 推演里的得分（胜 + 0.5 × 平）
        for row in rows:
            my, opp = tuple(row["my_lineup"]), tuple(row["opp_lineup"])
            if not row["games"]:
                continue
            weight = 1.0 if row["my_sig"] == signature(book.roster, my) else STALE
            score = row["win"] + 0.5 * row["draw"]
            if row["source"] == GUIDED and row["p0"] is not None:
                games = weight * row["games"]
                entry = self._pair.setdefault((my, opp), [0.0, 0.0])
                entry[0] += games * (score / row["games"] - row["p0"])
                entry[1] += games
                self._guided[(my, opp)] = self._guided.get((my, opp), 0.0) + row["games"]
                self._scored[(my, opp)] = self._scored.get((my, opp), 0.0) + score
                if row.get("seed"):
                    self._seeded[(my, opp)] = self._seeded.get((my, opp), 0.0) + row["games"]
                if weight == 1.0:
                    self._current[(my, opp)] = self._current.get((my, opp), 0.0) + row["games"]
            elif row["source"] == REAL:
                entry = self._real.setdefault((my, opp), [0.0, 0.0, 0.0, 0.0, 0.0])
                entry[0] += weight * score
                entry[1] += weight * row["games"]
                entry[2] += row["win"]
                entry[3] += row["draw"]
                entry[4] += row["games"] - row["win"] - row["draw"]
        for (my, opp), (total, games) in self._pair.items():  # 逐级汇总，并数一数每一级下面有几组独立的推演
            members = self._members.setdefault((frozenset(my), opp), [0.0, 0.0, 0.0])
            if not members[2]:
                side = self._opp.setdefault(opp, [0.0, 0.0, 0.0])
                if not side[2]:
                    self._all[2] += 1
                side[2] += 1
            members[2] += 1
            for level in (members, self._opp[opp], self._all):
                level[0] += total
                level[1] += games
        self.active = bool(self._pair or self._real)

    def __getattr__(self, name: str) -> Any:  # complete / workers / lookups / hits / roster / opp_policy / save …
        return getattr(self.book, name)

    # ------------------------------------------------------------ 修正

    def residual(self, my: Lineup, opp: Lineup) -> float:
        """这组对阵的口径差估计：自己的推演结果优先，不够就向上一级借，逐级收缩。

        每一级只用「别人」的数据当先验（自己的那份已经单独算过了）；借来的平均值按
        局数收缩一次，再按有几组独立的推演收缩一次。
        """
        s1, g1 = self._pair.get((my, opp), (0.0, 0.0))
        s2, g2, n2 = self._members.get((frozenset(my), opp), (0.0, 0.0, 0.0))
        s3, g3, n3 = self._opp.get(opp, (0.0, 0.0, 0.0))
        s4, g4, n4 = self._all

        def blend(prior: float, total: float, games: float, groups: float, borrow: float) -> float:
            if games <= 0 or groups <= 0:
                return prior
            return prior + (total / games - prior) * groups / (groups + borrow) * games / (games + SHRINK)

        level = blend(0.0, s4 - s3, g4 - g3, n4 - (1 if g3 else 0), BORROW_OPPONENTS)
        level = blend(level, s3 - s2, g3 - g2, n3 - (1 if g2 else 0), BORROW_MEMBERS)
        level = blend(level, s2 - s1, g2 - g1, n2 - (1 if g1 else 0), BORROW_ORDERS)
        return level + (s1 / g1 - level) * g1 / (g1 + SHRINK) if g1 else level

    def evidence(self, my: Sequence[str], opp: Sequence[str]) -> float:
        """这组对阵在我方现在的属性下「照指导出招」推演过多少局。别的属性下的记录（包括属性练过之后的内置经验）
        仍然通过口径差起作用，但不算在这里：属性变了，就该在新属性下重新补证据。"""
        return self._current.get((tuple(my), tuple(opp)), 0.0)

    def adjust(self, my: Sequence[str], opp: Sequence[str], base: Result) -> Result | Adjusted:
        if not self.active:
            return base
        my, opp = tuple(my), tuple(opp)
        value = _clip(base.value + self.q * self.residual(my, opp))
        real = self._real.get((my, opp))
        if real is not None:
            # 真实对局一局就是一个样本，和推演的一局分量相同：这组对阵推演得越多，一两局的输赢越动摇不了它。
            # （先验给得太弱时，输一局就换阵容，推荐会在几套阵容之间来回摇摆）
            prior = REAL_PRIOR + self.q * self._pair.get((my, opp), (0.0, 0.0))[1]
            value = (prior * value + real[0]) / (prior + real[1])
        shift = value - base.value
        return Adjusted(value, _clip(base.win_rate + shift), base.draw_rate, base.games, base)

    def get_many(self, pairs: Sequence[tuple[Lineup, Lineup]], games: int | None = None) -> list[Any]:
        return [self.adjust(my, opp, base) for (my, opp), base in zip(pairs, self.book.get_many(pairs, games))]

    def get(self, my: Sequence[str], opp: Sequence[str], games: int | None = None) -> Any:
        return self.get_many([(tuple(my), tuple(opp))], games)[0]

    def verify(self, pairs: Sequence[tuple[Lineup, Lineup]], games: int) -> list[Any]:
        """复核也要带上修正：否则 decide() 一复核，修正就被没修正的数字覆盖了。"""
        return [self.adjust(my, opp, base) for (my, opp), base in zip(pairs, self.book.verify(pairs, games))]

    # ------------------------------------------------------------ 面板上的说明

    def explain(self, lineup: Sequence[str], replies: dict[Lineup, float]) -> dict[str, Any]:
        """推荐阵容对上对手可能的补选：修正前后的胜率，以及修正依据了多少推演和实战。"""
        lineup = tuple(lineup)
        support = [(reply, prob) for reply, prob in replies.items() if prob > 0]
        bases = self.book.get_many([(lineup, reply) for reply, _ in support])
        base = sum(prob * result.win_rate for (_, prob), result in zip(support, bases))
        adjusted = sum(prob * self.adjust(lineup, reply, result).win_rate for (reply, prob), result in zip(support, bases))
        real = [0.0, 0.0, 0.0]
        for reply, _ in support:
            entry = self._real.get((lineup, reply))
            if entry is not None:
                real = [a + b for a, b in zip(real, entry[2:])]
        return {
            "active": self.active, "quality": round(self.q, 3), "battles": self.quality.get("battles", 0),
            "base": round(base, 3), "adjusted": round(adjusted, 3),
            "guided_games": int(sum(self._guided.get((lineup, reply), 0.0) for reply, _ in support)),
            "seed_games": int(sum(self._seeded.get((lineup, reply), 0.0) for reply, _ in support)),
            "borrowed_games": int(sum(self._opp.get(reply, (0.0, 0.0, 0.0))[1] for reply, _ in support)),
            "real": [int(count) for count in real],
        }


# ---------------------------------------------------------------- 给 LLM 的经验

def relevant_lessons(memory: Memory | None, opponent: str, my_picked: Sequence[str], opp_picked: Sequence[str],
                     limit: int = LESSON_LIMIT) -> list[dict[str, Any]]:
    """按字段匹配检索经验条目：针对的对手宠物已经出现的优先，其次是涉及我方已选宠物的、印证次数多的、新的。"""
    if memory is None:
        return []
    mine, theirs = set(my_picked), set(opp_picked)
    scored = []
    for order, lesson in enumerate(memory.lessons(opponent)):
        against = set(lesson["against"])
        if against and not against & theirs:
            continue  # 针对的那只对手宠物还没出现
        if lesson["scope"] == "play":
            continue  # 操作习惯类的经验留给复盘，不进选将上下文
        score = 2 * len(against & theirs) + len(set(lesson["pets"]) & mine) + 0.5 * min(lesson["support"], 4) - lesson["contra"]
        scored.append((-score, order, lesson))
    scored.sort(key=lambda item: item[:2])
    return [lesson for _, _, lesson in scored[:limit]]


def real_record(memory: Memory | None, opponent: str, limit: int = 4) -> list[dict[str, Any]]:
    """我方各组成员对这类对手的实战战绩（局数多的在前）。"""
    if memory is None:
        return []
    totals: dict[tuple[str, ...], list[float]] = {}
    for row in memory.experience(opponent):
        if row["source"] != REAL:
            continue
        entry = totals.setdefault(tuple(sorted(row["my_lineup"])), [0.0, 0.0, 0.0])
        entry[0] += row["win"]
        entry[1] += row["draw"]
        entry[2] += row["games"] - row["win"] - row["draw"]
    ranked = sorted(totals.items(), key=lambda item: -sum(item[1]))[:limit]
    return [{"members": list(members), "win": int(w), "draw": int(d), "loss": int(l)} for members, (w, d, l) in ranked]


# ---------------------------------------------------------------- 定向推演

def foreseen(profile: OpponentProfile, met: Sequence[str], lineup: Sequence[str],
             include_met: bool = True) -> list[tuple[Lineup, float]]:
    """我方用这套阵容时会遇到的对手阵容和各自的权重（加起来是 1）。

    对手的补选不看我方（随机、真人）：就是这一局遇到的那套。
    对手按习惯针对我方（智能）：它的第一手在我方之前选，之后的补选跟着我方变——按它的选将习惯
    预测，取最可能的 REPLIES 套。include_met 表示这一局实际遇到的那套一定算进去（实际用的阵容）。
    """
    met, lineup = tuple(met), tuple(lineup)
    if profile.replies != PREDICTED:
        return [(met, 1.0)]
    chances = reply_model_for(profile, met[:1])(lineup)
    top = sorted(chances.items(), key=lambda item: -item[1])[:REPLIES]
    if include_met and all(reply != met for reply, _ in top):
        top = [(met, chances.get(met, top[-1][1]))] + top[:REPLIES - 1]
    total = sum(chance for _, chance in top)
    return [(reply, chance / total) for reply, chance in top]


def candidates_for(guide: MatchupBook, my: Lineup, opp: Lineup, limit: int = CANDIDATES,
                   profile: OpponentProfile | None = None) -> list[Lineup]:
    """值得推演的我方阵容：实际用的那套，加上表上最好的若干组成员（每组取表上最好的站位）。

    「表上最好」按每套阵容会遇到的对手阵容来算（见 foreseen）。
    """
    def targets(lineup: Lineup) -> list[tuple[Lineup, float]]:
        return [(opp, 1.0)] if profile is None else foreseen(profile, opp, lineup, include_met=False)

    faced = {lineup: targets(lineup) for lineup in ORDERED_LINEUPS}
    pairs = [(lineup, reply) for lineup, replies in faced.items() for reply, _ in replies]
    values = iter(guide.get_many(pairs))
    best: dict[frozenset, tuple[float, Lineup]] = {}
    for lineup, replies in faced.items():
        score = sum(weight * next(values).value for _, weight in replies)
        key = frozenset(lineup)
        if key not in best or score > best[key][0]:
            best[key] = (score, lineup)
    ranked = [lineup for _, lineup in sorted(best.values(), key=lambda item: -item[0])]
    chosen = [my] + [lineup for lineup in ranked if frozenset(lineup) != frozenset(my)]
    return chosen[:limit + 1]


def reflect_known(
    row: dict[str, Any], *, roster: dict[str, Any], memory: Memory, book: MatchupBook, guide: MatchupBook,
    profile: OpponentProfile, budget: int, seed: Sequence[dict[str, Any]], workers: int = 0, goal: float = 0.6,
) -> dict[str, Any]:
    """有内置经验的对手（智能对手）：该探索的阵容离线已经普查过了，这里只做两件事——

    1. 补证据：实际这套阵容、以及现有证据里最好的几套，对上各自会遇到的对手阵容，推演过的局数不够
       TOP_UP 就补齐（照着军师的推荐选的阵容通常早就够了，一局也不用打，结论立刻就有）。
    2. 下结论：最好的阵容和实际阵容的胜算都按「内置经验 + 这位玩家对局之后的推演」合起来算，
       口径是照指导出招（q = 1），不并入真实战绩（一局的输赢正是要归因的对象）。
    """
    started = time.monotonic()
    my, opp = tuple(row["my_lineup"]), tuple(row["opp_lineup"])
    opponent = row["opponent"]
    me = rollout.guided(profile.model, GUIDED_GAMES)

    def known(table: MatchupBook) -> ExperienceBook:
        return ExperienceBook(table, memory, opponent, seed=seed, q=1.0, real=False)

    faced: dict[Lineup, list[tuple[Lineup, float]]] = {}

    def targets(lineup: Lineup) -> list[tuple[Lineup, float]]:
        if lineup not in faced:
            faced[lineup] = foreseen(profile, opp, lineup, include_met=lineup == my)
        return faced[lineup]

    pool = candidates_for(known(guide), my, opp, KNOWN_CANDIDATES, profile)
    have = known(book)
    wanted: dict[tuple[Lineup, Lineup], int] = {}
    for lineup in pool:  # 实际那套排在最前面：预算不够时先保证它
        for reply, weight in targets(lineup):
            short = TOP_UP * weight - have.evidence(lineup, reply)
            if short >= 1:
                wanted[(lineup, reply)] = max(2, 2 * round(short / 2))
    spent = 0
    plan: dict[tuple[Lineup, Lineup], int] = {}
    for pair, count in wanted.items():
        count = min(count, 2 * ((budget - spent) // 2))
        if count >= 2:
            plan[pair] = count
            spent += count
    if plan:
        jobs = [(roster, lineup, reply, count, me, profile.foe, 0, f"reflect|{row['id']}|known")
                for (lineup, reply), count in plan.items()]
        tables = book.get_many(list(plan))
        for (pair, count), (win, draw), table in zip(plan.items(), rollout.run(jobs, workers), tables):
            memory.add_experience(opponent, pair[0], pair[1], signature(roster, pair[0]), GUIDED, win, draw, count, table.value)
        book.save()
        have = known(book)

    def value(lineup: Lineup) -> float:
        faces = targets(lineup)
        results = have.get_many([(lineup, reply) for reply, _ in faces])
        return sum(weight * result.value for (_, weight), result in zip(faces, results))

    values = {lineup: value(lineup) for lineup in pool}
    best = max(pool, key=lambda lineup: (values[lineup], lineup == my))
    ceiling, played = values[best], values[my]
    verdict = decide_verdict(row["outcome"], ceiling, played, row["missed"] or 0.0, goal)
    summary = {
        "lineup": list(best), "value": round(ceiling, 3), "played": round(played, 3),
        "games": int(sum(have.evidence(best, reply) for reply, _ in targets(best))),
        "replies": [list(reply) for reply, _ in targets(best)] if profile.replies == PREDICTED else None,
        "total_games": spent, "guided": True, "known": True, "ms": int((time.monotonic() - started) * 1000),
    }
    return {"ceiling": summary, "verdict": verdict}


def reflect(
    row: dict[str, Any], *, roster: dict[str, Any], memory: Memory, book: MatchupBook, guide: MatchupBook,
    profile: OpponentProfile, budget: int, workers: int = 0, goal: float = 0.6, seed: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """针对这场对局做定向推演，把结果写进经验库，并给这场对局下结论。

    row 是 battles 表里这场对局的一行；book 是这一局锁定属性的对阵表（提供当时的表值），
    guide 用来挑候选（属性有成长时用基础属性的完整表）。budget 是一共打多少局。
    逐轮淘汰：全部候选先各打几局，留下一半再加几局……最后用没用过的新种子把最好的两套和
    实际那套重估一遍（挑出来的最大值偏高，结论要用没参与挑选的数字）。
    这类对手有内置经验（seed）时不用再探索，见 reflect_known。
    """
    if seed and budget > 0:
        return reflect_known(row, roster=roster, memory=memory, book=book, guide=guide, profile=profile,
                             budget=budget, seed=seed, workers=workers, goal=goal)
    started = time.monotonic()
    my, opp = tuple(row["my_lineup"]), tuple(row["opp_lineup"])
    opponent = row["opponent"]
    reactive = profile.replies == PREDICTED
    me = rollout.guided(profile.model, GUIDED_GAMES)
    tag = f"reflect|{row['id']}"
    totals: dict[tuple[Lineup, Lineup], list[float]] = {}  # (我方阵容, 对手阵容) → [胜, 平, 局数]
    faced: dict[Lineup, list[tuple[Lineup, float]]] = {}
    spent = 0

    def targets(lineup: Lineup) -> list[tuple[Lineup, float]]:
        if lineup not in faced:
            faced[lineup] = foreseen(profile, opp, lineup, include_met=lineup == my)
        return faced[lineup]

    def play(lineups: Sequence[Lineup], games: float, label: str) -> dict[Lineup, float]:
        """这些阵容各自对上它会遇到的对手阵容；每套阵容一共打 games 局左右，平摊到各套对手阵容上
        （这样只会遇到一套对手阵容的我方阵容，样本不会比别人少一半）。返回这一轮里每套阵容的价值。"""
        nonlocal spent
        pairs = {(lineup, reply): max(1, round(games / len(targets(lineup))))
                 for lineup in lineups for reply, _ in targets(lineup)}
        jobs = [(roster, lineup, reply, count, me, profile.foe, 0, f"{tag}|{label}") for (lineup, reply), count in pairs.items()]
        results = dict(zip(pairs, rollout.run(jobs, workers)))
        for pair, (win, draw) in results.items():
            entry = totals.setdefault(pair, [0.0, 0.0, 0.0])
            entry[0] += win
            entry[1] += draw
            entry[2] += pairs[pair]
        spent += sum(pairs.values())
        return {lineup: sum(weight * (results[(lineup, reply)][0] + 0.5 * results[(lineup, reply)][1]) / pairs[(lineup, reply)]
                            for reply, weight in targets(lineup)) for lineup in lineups}

    def mean(lineup: Lineup) -> float:
        return sum(weight * (totals[(lineup, reply)][0] + 0.5 * totals[(lineup, reply)][1]) / totals[(lineup, reply)][2]
                   for reply, weight in targets(lineup))

    ceiling = played = None
    best = my
    fresh_games = 0
    if budget > 0:
        pool = candidates_for(guide, my, opp, REACTIVE_CANDIDATES if reactive else CANDIDATES, profile)
        scale = budget / REFLECT_UNIT
        alive = list(pool)
        for label, share, keep in ROUNDS:
            play(alive, max(1.0, share * scale), label)
            alive = sorted(alive, key=lambda lineup: -mean(lineup))[:keep]
        finalists = list(dict.fromkeys(alive + [my]))
        fresh_games = max(2, round(FRESH * scale))
        values = play(finalists, fresh_games, "fresh")
        best = max(finalists, key=lambda lineup: (values[lineup], lineup == my))
        ceiling, played = values[best], values[my]

        pairs = list(totals)
        for (lineup, reply), table in zip(pairs, book.get_many(pairs)):
            win, draw, games = totals[(lineup, reply)]
            memory.add_experience(opponent, lineup, reply, signature(roster, lineup), GUIDED, win, draw, games, table.value)
        book.save()
    else:  # 不做推演：只能按常规口径的表下结论
        ceiling, played, best = row["best_p0"], row["p0"], tuple(row["best_lineup"] or my)

    verdict = decide_verdict(row["outcome"], ceiling, played, row["missed"] or 0.0, goal)
    summary = {
        "lineup": list(best), "value": round(ceiling, 3), "played": round(played, 3),
        "games": (sum(max(1, round(fresh_games / len(targets(best)))) for _ in targets(best)) if budget > 0 else 0),
        # 对手的选将跟着我方变时：我方用这套阵容，它多半会拿出哪几套
        "replies": [list(reply) for reply, _ in targets(best)] if reactive and budget > 0 else None,
        "total_games": spent, "guided": budget > 0, "ms": int((time.monotonic() - started) * 1000),
    }
    return {"ceiling": summary, "verdict": verdict}
