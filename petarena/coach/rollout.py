"""推演内核：从当前局面出发，逐个评估合法动作。

战斗指导、战后复盘、后台定向推演和对手方「智能模式」的出招共用这一套。我方的评估（战斗指导、复盘）只有一种做法：

    对当前宠物的每个合法动作（技能 × 目标，legal_moves()）：
        克隆局面（换一条随机数序列）→ 执行这个动作
        → 之后我方每次都在合法动作里等概率随机出招（policy.uniform），对手按它的模型出招
        → 打完这一局，记结果（赢 1 / 平 0.5 / 输 0）
    每个动作打 ROLLOUT_GAMES 局，取平均 = 这个动作的价值

- 换种子：推演用的随机数与本局真实的随机数无关（Battle.clone(reseed=...)），所以
  建议不会「预知」之后的冻结、追击、同速结果，也不占用本局的随机数。
- 配对比较：各个动作共用同一批种子，比较时噪声更小。
- 价值 = 胜率 + 0.5 × 平局率，站在当前行动宠物所属一方的角度。我方的评估排序只看价值，同分按 legal_moves() 的顺序。
- 智能对手（它自己出招，以及推演里给它建模的往前看）保留原来的判断方式：价值相同时再比终局优势
  （双方剩余生命占比之差，赢得越快、输得越慢越好），即 evaluate_moves(..., tiebreak=True)。
- 我方之后随机出招，所以胜率的绝对值偏低，只用来比较这一手各个动作的好坏。

对手的模型（draft.OpponentProfile.model）保持原样：随机模式 = 它真实的随机打法；手动模式 = 常规打法掺 15% 随机；
智能模式 = ("reply", 12, "heuristic")，即它接下来 AHEAD 次应对把每个动作各推演 12 局再选，之后按常规打法。

策略描述（spec）是可以直接传给子进程、写进 JSON 的普通数据：

    "heuristic" / "random" / "skill_first" / "expert" / "uniform"   policy.POLICIES 里的策略名
    ("noisy", 0.15, spec)        以这个概率随机出招，其余时候按 spec 出招（给真人对手建模）
    ("rollout", 局数, 自己之后的打法, 对方的打法)   逐动作推演，取最好的（「照指导出招」的整局模拟）
    ("reply", 局数, spec[, 次数])  只用来描述对手：它接下来的几次决策（默认 AHEAD 次）会把每个动作各推演
                                 这么多局再选，之后按 spec 出招。只在逐动作评估（evaluate_moves）里起作用。
"""

from __future__ import annotations

import random
import zlib
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..engine import Battle
from ..engine.pets import DOG_PURSUIT_CHANCE
from .matchup import FAST, FAST_WORKERS, MAIN, Roster, _get_pool, _lineup, shutdown_pool
from .policy import HEURISTIC, POLICIES, UNIFORM, Move, Policy

Spec = Any

NOISY = "noisy"
ROLLOUT = "rollout"
REPLY = "reply"

ROLLOUT_GAMES = 128  # 复盘时每个动作推演几局（战斗指导的局数由 config / matchup.auto_advice_games 决定）
EXPERT_GAMES = 16  # 智能对手出招时每个动作推演几局
REPLY_GAMES = 12  # 给「对手接下来的应对」建模时，它的每个动作推演几局
AHEAD = 3  # 对手往前看几次决策：大约是接下来的一整轮。("reply", …) 里没写次数时就用它
OWN = UNIFORM  # 逐动作评估时我方之后的打法：战斗指导、复盘、整局模拟都用它
NEAR_MARGIN = 0.10  # 价值与最优相差超过这么多的动作一定不是「候选」
NEAR_SIGMA = 0.5  # ……而且相差不能超过这么多倍的标准误（逐局配对）
NEAR_CAP = 3  # 候选最多几个
HUMAN_NOISE = 0.15  # 给真人对手建模时掺入的随机出招比例
TEMPO = 0.002  # 智能对手：价值相同时偏好「赢得快、输得慢」，每多一次行动折算成这么多的血量差

_ME_SALT = 0x9E3779B9  # 双方用不同的随机数序列：我方换打法不会改变对手方的随机选择


# ---------------------------------------------------------------- 策略

def resolve(spec: Spec) -> Policy:
    """策略描述 → (battle, rng) -> (技能, 目标)。"""
    if isinstance(spec, str):
        return POLICIES[spec]
    kind = spec[0]
    if kind == NOISY:
        _, chance, base = spec
        inner = resolve(base)

        def noisy(b: Battle, rng: random.Random) -> Move:
            if rng.random() < chance:
                return rng.choice(b.legal_moves())
            return inner(b, rng)

        return noisy
    if kind == ROLLOUT:
        _, games, own, other = spec
        return lambda b, rng: rollout_move(b, rng, own=own, other=other, games=games)
    if kind == REPLY:  # 「接下来几次应对」由 evaluate_moves 处理；除此之外就是后面那个打法
        return resolve(spec[2])
    raise ValueError(f"未知的策略描述：{spec!r}")


def guided(model: Spec = HEURISTIC, games: int = ROLLOUT_GAMES) -> tuple:
    """「照战斗指导出招」的玩家：每次行动取推演最好的动作；model 是推演时对对手打法的假设。"""
    return (ROLLOUT, games, OWN, model)


def human(base: Spec = HEURISTIC) -> tuple:
    """真人对手的模型：大体按常规打法，偶尔出其不意。"""
    return (NOISY, HUMAN_NOISE, base)


def _ahead_salt(salt: int | str, key: tuple) -> str:
    """对手往前看时用哪一批种子：只取决于局面本身，所以各个动作可以分给不同的进程去算，结果与顺序无关。"""
    return f"{salt}|ahead|{zlib.crc32(repr(key).encode())}"


# ---------------------------------------------------------------- 逐动作评估

@dataclass(frozen=True)
class MoveValue:
    move: Move
    value: float  # 胜率 + 0.5 × 平局率
    win: int
    draw: int
    games: int
    outcomes: tuple[float, ...]  # 每一局的结果（1 / 0.5 / 0），与其他动作逐局配对
    margin: float = 0.0  # 终局优势，只有 tiebreak=True（智能对手）时才算；价值相同时的第二排序依据

    @property
    def win_rate(self) -> float:
        return self.win / self.games

    @property
    def draw_rate(self) -> float:
        return self.draw / self.games


def _seeds(salt: int | str, games: int, start: int = 0) -> list[int]:
    return [zlib.crc32(f"{salt}|{k}".encode()) for k in range(start, start + games)]


def _health(b: Battle, side: str) -> float:
    pets = b.side_pets(side)
    return sum(p.hp + p.shield for p in pets) / sum(p.max_hp for p in pets)


def _state_key(b: Battle) -> tuple:
    """局面的指纹：对手往前看时，同一个局面只推演一次。"""
    teams = []
    for team in b.teams.values():
        barrier, chain = team.barrier, team.chain
        teams.append((
            team.beans,
            None if barrier is None else (barrier.atk, barrier.hit, barrier.expires),
            None if chain is None else (tuple(chain.members), chain.expires),
        ))
    return (b.round, b.actor, tuple(b.queue or ()), tuple(teams),
            tuple((p.hp, p.shield, p.alive) for p in b.pets.values()))


def evaluate_moves(
    battle: Battle, *, own: Spec = HEURISTIC, other: Spec = HEURISTIC, games: int = ROLLOUT_GAMES,
    salt: int | str = 0, moves: Iterable[Move] | None = None, start: int = 0, tiebreak: bool = False,
) -> list[MoveValue]:
    """逐个评估当前行动宠物的合法动作（或指定的几个动作），顺序与 legal_moves() 一致。

    tiebreak=True 时另算终局优势（margin），价值相同时 rank 用它排先后：智能对手的判断方式。

    own 是这一方执行完这个动作之后的打法（战斗指导、复盘传 OWN，即等概率随机），other 是对方的打法。
    other 是 ("reply", …) 时，对方接下来的几次决策用推演来选（智能对手的模型）。
    salt 决定用哪一批种子：同一个 salt 得到同样的结果；换一个 salt（或 start）就是一批没用过的新种子。
    """
    side = battle.pets[battle.actor].side
    foe = Battle.enemy(side)
    mine = resolve(own)
    theirs = resolve(other)
    if isinstance(other, str) or other[0] != REPLY:
        reply_games, reply_times = 0, 0
    else:
        reply_games, reply_times = other[1], (other[3] if len(other) > 3 else AHEAD)
    replies: dict[tuple, Move] = {}  # 对手往前看：局面 → 它选的动作
    seeds = _seeds(salt, games, start)
    done = len(battle.actions)
    result = []
    for move in (battle.legal_moves() if moves is None else moves):
        outcomes = []
        margin = 0.0
        for seed in seeds:
            sim = battle.clone(reseed=seed)
            rng = random.Random(seed)
            sim.act(*move)
            left = reply_times  # 对手还有几次决策要用推演来选
            while not sim.finished:
                if sim.pets[sim.actor].side == side:
                    sim.act(*mine(sim, rng))
                elif left:
                    left -= 1
                    key = _state_key(sim)
                    if key not in replies:
                        options = sim.legal_moves()
                        replies[key] = options[0] if len(options) == 1 else rank(evaluate_moves(
                            sim, games=reply_games, salt=_ahead_salt(salt, key), moves=options, tiebreak=True))[0].move
                    sim.act(*replies[key])
                else:
                    sim.act(*theirs(sim, rng))
            outcome = 1.0 if sim.winner == side else 0.5 if sim.winner == "draw" else 0.0
            outcomes.append(outcome)
            if tiebreak:
                margin += _health(sim, side) - _health(sim, foe) - TEMPO * (2 * outcome - 1) * (len(sim.actions) - done)
        win, draw = outcomes.count(1.0), outcomes.count(0.5)
        result.append(MoveValue(
            move=tuple(move), value=(win + 0.5 * draw) / games, win=win, draw=draw, games=games,
            outcomes=tuple(outcomes), margin=margin / games,
        ))
    return result


def _explicit(spec: Spec) -> Spec:
    """把「往前看几次」写明：任务要交给别的进程去算，不能依赖那边的默认值。"""
    if not isinstance(spec, str) and spec[0] == REPLY and len(spec) == 3:
        return (*spec, AHEAD)
    return spec


GAMES_PER_JOB = 64  # 逐动作评估交给进程池时，每个任务推演几局：一个动作的局数拆成几份，分给更多的进程


def _evaluate_one(job: tuple) -> MoveValue:
    """进程池任务：评估一个动作的一段局（种子编号 start 起的 games 局）。"""
    battle, move, own, other, games, salt, start = job
    return evaluate_moves(battle, own=own, other=other, games=games, salt=salt, moves=[move], start=start)[0]


def _merge(parts: Sequence[MoveValue]) -> MoveValue:
    """把同一个动作的几段局合起来：等同于一次推演全部局（种子按编号连续）。"""
    games = sum(part.games for part in parts)
    win, draw = sum(part.win for part in parts), sum(part.draw for part in parts)
    return MoveValue(
        move=parts[0].move, value=(win + 0.5 * draw) / games, win=win, draw=draw, games=games,
        outcomes=tuple(outcome for part in parts for outcome in part.outcomes),
        margin=sum(part.margin * part.games for part in parts) / games,
    )


def evaluate_all(
    battle: Battle, *, own: Spec = OWN, other: Spec = HEURISTIC, games: int = ROLLOUT_GAMES,
    salt: int | str = 0, moves: Iterable[Move] | None = None, start: int = 0, workers: int = 0, lane: str = FAST,
) -> list[MoveValue]:
    """evaluate_moves 的并行版本：每个动作的局数拆成 GAMES_PER_JOB 局一份，交给进程池（默认是战斗指导专用的
    那条通道），再按动作合起来。结果与单进程完全相同（各局的种子只取决于编号，往前看用的种子只取决于局面）。
    workers <= 1 时就在当前线程里算。"""
    moves = [tuple(move) for move in (battle.legal_moves() if moves is None else moves)]
    if workers > 1 and (len(moves) > 1 or games > GAMES_PER_JOB):
        own, other = _explicit(own), _explicit(other)
        jobs, owners = [], []
        for index, move in enumerate(moves):
            for at in range(0, games, GAMES_PER_JOB):
                jobs.append((battle, move, own, other, min(GAMES_PER_JOB, games - at), salt, start + at))
                owners.append(index)
        parts: list[list[MoveValue]] = [[] for _ in moves]
        for index, part in zip(owners, pool_map(_evaluate_one, jobs, workers, lane)):
            parts[index].append(part)
        return [_merge(chunks) for chunks in parts]
    return evaluate_moves(battle, own=own, other=other, games=games, salt=salt, moves=moves, start=start)


def rank(values: Sequence[MoveValue]) -> list[MoveValue]:
    """从好到差：先看价值，再看终局优势（只有智能对手的评估算了它，我方的都是 0），最后按原顺序（结果确定）。"""
    order = sorted(range(len(values)), key=lambda i: (-values[i].value, -values[i].margin, i))
    return [values[i] for i in order]


def near_best(values: Sequence[MoveValue], margin: float = NEAR_MARGIN, cap: int = NEAR_CAP) -> list[MoveValue]:
    """候选动作：推演结果与最优分不出高下的前几个（面板上标「与最优相当」）。

    差距不超过 margin，也不超过半个标准误（各动作用同一批种子，逐局相减）。
    """
    ranked = rank(values)
    best = ranked[0]

    def close(value: MoveValue) -> bool:
        gap = best.value - value.value
        if gap <= 1e-9:
            return True
        return gap <= margin + 1e-9 and gap <= NEAR_SIGMA * paired_diff(best, value)[1] + 1e-9

    return [value for value in ranked if close(value)][:cap]


def paired_diff(a: MoveValue, b: MoveValue) -> tuple[float, float]:
    """a − b 的平均差和它的标准误（两者用的是同一批种子，逐局相减）。"""
    diffs = [x - y for x, y in zip(a.outcomes, b.outcomes)]
    n = len(diffs)
    mean = sum(diffs) / n
    if n < 2:
        return mean, 1.0
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    return mean, (var / n) ** 0.5


def rollout_move(
    battle: Battle, rng: random.Random, *, own: Spec = HEURISTIC, other: Spec = HEURISTIC,
    games: int = ROLLOUT_GAMES, tiebreak: bool = False,
) -> Move:
    """推演出招：逐个评估合法动作，取最好的。只有一个合法动作时不用推演。"""
    moves = battle.legal_moves()
    if len(moves) == 1:
        return moves[0]
    values = evaluate_moves(battle, own=own, other=other, games=games, salt=rng.getrandbits(32), moves=moves,
                            tiebreak=tiebreak)
    return rank(values)[0].move


def expert_move(battle: Battle, rng: random.Random) -> Move:
    """对手方「智能模式」的出招（policy.EXPERT）：价值相同时比终局优势（它原来的判断方式）。"""
    return rollout_move(battle, rng, games=EXPERT_GAMES, tiebreak=True)


# ---------------------------------------------------------------- 动作的直接后果

def _own_events(sim: Battle, move: Move, actor: str) -> list[dict[str, Any]]:
    events = []
    for event in sim.act(*move):
        events.append(event)
        if event["type"] == "battle_end" or (event["type"] == "turn_end" and event["pet"] == actor):
            break
    return events


def fact_card(battle: Battle, move: Move) -> dict[str, Any]:
    """这个动作的直接后果：从出手到该宠物的行动机会结束为止。

    这段时间里唯一的随机事件是小狗的协同补刀（只在队友普攻后判定一次），所以最多
    两种情况：把「触发」和「未触发」各找出来一次，按规则里的概率加权，结果是精确的。
    """
    actor = battle.actor
    side = battle.pets[actor].side
    branches: dict[bool | None, list[dict[str, Any]]] = {}
    for seed in range(64):
        sim = battle.clone(reseed=seed, record=True)
        events = _own_events(sim, move, actor)
        check = next((e for e in events if e["type"] == "pursuit_check"), None)
        key = None if check is None else check["success"]
        branches.setdefault(key, events)
        if key is None or len(branches) == 2:
            break
    chance = DOG_PURSUIT_CHANCE / 100
    weights = {None: 1.0, True: chance, False: 1 - chance}
    if len(branches) == 1:
        weights = {key: 1.0 for key in branches}

    damage: dict[str, float] = {}
    ko: dict[str, float] = {}
    rows = []
    win = 0.0
    beans = battle.teams[side].beans
    for key in sorted(branches, key=lambda k: (k is not None, bool(k))):
        events, prob = branches[key], weights[key]
        downed = []
        for event in events:
            if event["type"] == "damage":
                for part in event["parts"]:
                    hit = part["absorbed"] + part["lost"]
                    if hit:
                        damage[part["pet"]] = damage.get(part["pet"], 0.0) + prob * hit
            elif event["type"] == "ko":
                ko[event["pet"]] = ko.get(event["pet"], 0.0) + prob
                downed.append(event["pet"])
            elif event["type"] == "battle_end" and event["winner"] == side:
                win += prob
            if event["type"] in ("action", "energy") and (event.get("side", side) == side):
                beans = event["beans"]
        lines = [e["text"] for e in events[1:] if e["text"] and e["type"] != "pursuit_check"]
        rows.append({"prob": prob, "pursuit": key, "lines": lines, "ko": downed})
    return {
        "move": list(move), "branches": rows, "beans": beans, "win": round(win, 3),
        "damage": {pet: round(amount, 2) for pet, amount in damage.items()},
        "ko": {pet: round(prob, 3) for pet, prob in ko.items()},
    }


def fact_text(card: dict[str, Any]) -> str:
    """把动作的直接后果写成一句话（给 LLM 和面板用）。"""
    parts = []
    for branch in card["branches"]:
        text = "；".join(branch["lines"]) or "没有立即可见的效果"
        if branch["pursuit"] is None:
            parts.append(text)
        else:
            label = "小狗补刀触发" if branch["pursuit"] else "补刀未触发"
            parts.append(f"{label}（{round(branch['prob'] * 100)}%）：{text}")
    return " ｜ ".join(parts)


# ---------------------------------------------------------------- 整局模拟

def play(roster: Roster, my: Sequence[str], opp: Sequence[str], seed: int,
         me: Spec = HEURISTIC, foe: Spec = HEURISTIC) -> str:
    """按指定的双方打法打一局，返回 "me" / "opp" / "draw"。"""
    policies = {"me": resolve(me), "opp": resolve(foe)}
    rngs = {"me": random.Random(seed ^ _ME_SALT), "opp": random.Random(seed)}
    battle = Battle.new({"me": _lineup(roster, "me", my), "opp": _lineup(roster, "opp", opp)}, seed, record=False)
    while not battle.finished:
        side = battle.pets[battle.actor].side
        battle.act(*policies[side](battle, rngs[side]))
    return battle.winner


def game_seed(tag: str, my: Sequence[str], opp: Sequence[str], game: int) -> int:
    return zlib.crc32(f"{tag}|{','.join(my)}|{','.join(opp)}|{game}".encode())


def simulate(roster: Roster, my: Sequence[str], opp: Sequence[str], games: int, me: Spec = HEURISTIC,
             foe: Spec = HEURISTIC, start: int = 0, tag: str = "sim") -> tuple[int, int]:
    """打 games 局（局号从 start 开始），返回（我方胜场，平局数）。tag 不同，种子就不同。"""
    win = draw = 0
    for game in range(start, start + games):
        winner = play(roster, my, opp, game_seed(tag, my, opp, game), me, foe)
        win += winner == "me"
        draw += winner == "draw"
    return win, draw


def batch(job: tuple) -> tuple[int, int]:
    """进程池任务（必须是模块顶层函数，参数只含普通数据）。"""
    roster, my, opp, games, me, foe, start, tag = job
    return simulate(roster, tuple(my), tuple(opp), games, me, foe, start, tag)


CHUNK = 2  # 进程池里每个任务打几局：任务小一点，选将时的现场推演不用排在后面等太久


def pool_map(fn, jobs: Sequence[Any], workers: int = 0, lane: str = MAIN) -> list[Any]:
    """在对阵引擎的进程池里执行一批任务（fn 必须是模块顶层函数），按顺序返回结果。

    进程池是先进先出的，一次塞满会让别的请求（例如选将时的现场推演）等很久，
    所以一波一波地提交。workers <= 1 或进程池不可用时就在当前线程里执行。
    lane 选哪个进程池：后台和批量任务用 MAIN，战斗指导用 FAST（玩家正等着，不排在后台任务后面）。
    """
    jobs = list(jobs)
    if workers > 1 and len(jobs) > 1:
        try:
            pool = _get_pool(workers, lane)
            results: list[Any] = []
            wave = (FAST_WORKERS if lane == FAST else workers) * 2  # 战斗指导的通道是专用的，一波塞满它
            for at in range(0, len(jobs), wave):
                results.extend(pool.map(fn, jobs[at:at + wave]))
            return results
        except Exception:  # 进程池不可用（例如受限环境）：退回单进程
            shutdown_pool()
    return [fn(job) for job in jobs]


def _ready(index: int) -> int:
    return index


def warm_up(workers: int) -> None:
    """把战斗指导用的进程池提前拉起来（进程是 spawn 出来的，每个要零点几秒导入代码）：
    不然第一次请求建议时，玩家要多等这一两秒。失败了就算了，用到时会再试。"""
    if workers > 1:
        try:
            list(_get_pool(workers, FAST).map(_ready, range(2 * FAST_WORKERS)))
        except Exception:
            pass


def run(jobs: Sequence[tuple], workers: int = 0) -> list[tuple[int, int]]:
    """执行一批 (roster, my, opp, games, me, foe, start, tag) 任务，返回每个任务的（胜，平）。

    workers > 1 时把每个任务拆成几局一份再交给进程池。
    """
    jobs = list(jobs)
    if workers <= 1 or not jobs:
        return [batch(job) for job in jobs]
    pieces: list[tuple[int, tuple]] = []
    for index, (roster, my, opp, games, me, foe, start, tag) in enumerate(jobs):
        for offset in range(0, games, CHUNK):
            size = min(CHUNK, games - offset)
            pieces.append((index, (roster, my, opp, size, me, foe, start + offset, tag)))
    totals = [[0, 0] for _ in jobs]
    for (index, _), (win, draw) in zip(pieces, pool_map(batch, [piece for _, piece in pieces], workers)):
        totals[index][0] += win
        totals[index][1] += draw
    return [tuple(total) for total in totals]
