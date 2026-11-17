"""推演内核：从当前局面出发，逐个评估合法动作。

战斗指导、战后复盘、后台定向推演和对手方「智能模式」的出招共用这一套：

    对每个合法动作：克隆局面（换一条随机数序列）→ 执行这个动作 → 双方按各自的
    常规打法把这一局打完 → 重复若干局，取平均价值

- 换种子：推演用的随机数与本局真实的随机数无关（Battle.clone(reseed=...)），所以
  建议不会「预知」之后的冻结、追击、同速结果，也不占用本局的随机数。
- 配对比较：各个动作共用同一批种子，比较时噪声更小。
- 价值 = 胜率 + 0.5 × 平局率，站在当前行动宠物所属一方的角度。

策略描述（spec）是可以直接传给子进程、写进 JSON 的普通数据：

    "heuristic" / "random" / "skill_first" / "expert"   policy.POLICIES 里的策略名
    ("noisy", 0.15, spec)        以这个概率随机出招，其余时候按 spec 出招（给真人对手建模）
    ("mix", p, spec_a, spec_b)   每次行动以概率 p 用 spec_a，否则用 spec_b（模拟「部分采纳建议」的玩家）
    ("rollout", 局数, 自己之后的打法, 对方的打法)   逐动作推演，取最好的
    ("reply", 局数, spec[, 次数])  往前看：这一方接下来的几次决策（默认 AHEAD 次）会把每个动作各推演这么多局
                                 再选，之后按 spec 出招。只在逐动作评估（evaluate_moves）里起作用。
                                 - 描述对方：给智能对手建模（它本来就是这样出招的）。实测全程把它当常规
                                   打法时，面板上的胜率会明显偏高（0.49 对实际 0.17）。
                                 - 描述己方：我方接下来的几次行动也按「推演出来的最好动作」来估计，而不是
                                   按常规打法——需要队友接着配合才有价值的动作（先放水链、先打掉护盾、
                                   攒能量给队友）不再被低估。

往前看多远是实测定的（基础属性，四组对阵各 220 局，对上智能对手；价值 = 胜率 + 0.5 × 平局率）：

    我方 0 次、对手 1 次（第二期最初的做法）   0.448
    我方 1 次、对手 1 次                    0.476
    我方 2 次、对手 2 次                    0.500
    我方 3 次、对手 3 次（现在的做法）        0.527      其中「小狗、小鱼、小猫」这套从 0.56 到 0.79
    我方 4 次、对手 4 次                    0.545
    我方 6 次、对手 6 次                    0.539

    双方各 3 只宠物，「各 3 次」大约就是把接下来的一整轮看完；再深就没有可见的提高了，计算量却一直涨。
    而只是把每个动作多推演几局（16 / 24 / 48 / 96 局）没有任何提高：瓶颈在「后续怎么打」的假设，不在抽样噪声。
    同一个局面只推演一次（按局面缓存），所以代价大约是不往前看时的 4 倍，而不是几十倍。
"""

from __future__ import annotations

import random
import zlib
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..engine import Battle
from ..engine.pets import DOG_PURSUIT_CHANCE
from .matchup import FAST, MAIN, Roster, _get_pool, _lineup, shutdown_pool
from .policy import HEURISTIC, POLICIES, Move, Policy

Spec = Any

NOISY = "noisy"
MIX = "mix"
ROLLOUT = "rollout"
REPLY = "reply"

ROLLOUT_GAMES = 24  # 战斗指导、复盘时每个动作推演几局
EXPERT_GAMES = 16  # 智能对手出招时每个动作推演几局
REPLY_GAMES = 12  # 给「对手接下来的应对」建模时，它的每个动作推演几局
AHEAD = 3  # 往前看几次决策（双方各自）：大约是接下来的一整轮。("reply", …) 里没写次数时就用它
OWN_GAMES = 8  # 给「我方接下来的行动」建模时，每个动作推演几局
OWN = (REPLY, OWN_GAMES, HEURISTIC)  # 逐动作评估时对我方后续出招的假设：战斗指导、复盘、整局模拟都用它
NEAR_MARGIN = 0.10  # 价值与最优相差超过这么多的动作一定不是「候选」
NEAR_SIGMA = 0.5  # ……而且相差不能超过这么多倍的标准误（逐局配对）：接近必胜或必败时，差一局也是实打实的差别
NEAR_EDGE = 0.05  # 价值完全相同时，终局优势（双方剩余生命占比之差、快慢）也不能比最优差这么多
NEAR_CAP = 3  # 候选最多几个
HUMAN_NOISE = 0.15  # 给真人对手建模时掺入的随机出招比例
TEMPO = 0.002  # 价值相同时偏好「赢得快、输得慢」：每多一次行动折算成这么多的血量差

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
    if kind == MIX:
        _, chance, first, second = spec
        a, b_ = resolve(first), resolve(second)
        return lambda b, rng: (a if rng.random() < chance else b_)(b, rng)
    if kind == ROLLOUT:
        _, games, own, other = spec
        return lambda b, rng: rollout_move(b, rng, own=own, other=other, games=games)
    if kind == REPLY:  # 「下一次应对」由 evaluate_moves 处理；除此之外就是后面那个打法
        return resolve(spec[2])
    raise ValueError(f"未知的策略描述：{spec!r}")


def guided(model: Spec = HEURISTIC, games: int = ROLLOUT_GAMES) -> tuple:
    """「照战斗指导出招」的玩家：每次行动取推演最好的动作；model 是推演时对对手打法的假设。"""
    return (ROLLOUT, games, OWN, model)


def human(base: Spec = HEURISTIC) -> tuple:
    """真人对手的模型：大体按常规打法，偶尔出其不意。"""
    return (NOISY, HUMAN_NOISE, base)


def answering(base: Spec = HEURISTIC, games: int = REPLY_GAMES) -> tuple:
    """会推演的对手：接下来的几次应对用推演选，之后按 base 出招。"""
    return (REPLY, games, base)


def partly(chance: float, model: Spec = HEURISTIC) -> tuple:
    """以 chance 的概率照指导出招、其余时候按常规打法的玩家。"""
    return (MIX, chance, guided(model), HEURISTIC)


def _ahead_salt(salt: int | str, key: tuple) -> str:
    """往前看时用哪一批种子：只取决于局面本身，所以各个动作可以分给不同的进程去算，结果与顺序无关。"""
    return f"{salt}|ahead|{zlib.crc32(repr(key).encode())}"


# ---------------------------------------------------------------- 逐动作评估

@dataclass(frozen=True)
class MoveValue:
    move: Move
    value: float  # 胜率 + 0.5 × 平局率
    win: int
    draw: int
    games: int
    margin: float  # 终局优势：双方剩余生命占比之差，赢得越快越高、输得越慢越高。价值相同时的第二排序依据
    outcomes: tuple[float, ...]  # 每一局的结果（1 / 0.5 / 0），与其他动作逐局配对

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
    """局面的指纹：对手的应对只取决于它，同一个局面只推演一次。"""
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
    salt: int | str = 0, moves: Iterable[Move] | None = None, start: int = 0,
) -> list[MoveValue]:
    """逐个评估当前行动宠物的合法动作（或指定的几个动作），顺序与 legal_moves() 一致。

    own 是这一方之后的打法，other 是对方的打法。salt 决定用哪一批种子：同一个 salt
    得到同样的结果；换一个 salt（或 start）就是一批没用过的新种子。
    own / other 是 ("reply", …) 时，每一局里这一方接下来的几次决策用推演来选（见模块说明）；
    战斗指导和复盘传 own=OWN。不传就是常规打法：智能对手自己出招、以及往前看时的内层推演都是这样。
    """
    side = battle.pets[battle.actor].side
    foe = Battle.enemy(side)
    policies = {side: resolve(own), foe: resolve(other)}

    def looks_ahead(spec: Spec) -> tuple[int, int]:
        """("reply", 局数, 之后的打法[, 几次]) → (每个动作推演几局, 接下来几次决策用推演来选)。"""
        if isinstance(spec, str) or spec[0] != REPLY:
            return 0, 0
        return spec[1], (spec[3] if len(spec) > 3 else AHEAD)

    depth = {foe: looks_ahead(other), side: looks_ahead(own)}
    replies: dict[tuple, Move] = {}
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
            left = {who: times for who, (_, times) in depth.items()}  # 双方各还有几次决策要用推演来选
            while not sim.finished:
                actor = sim.pets[sim.actor].side
                if left[actor]:
                    left[actor] -= 1
                    key = _state_key(sim)
                    if key not in replies:
                        options = sim.legal_moves()
                        replies[key] = options[0] if len(options) == 1 else rank(evaluate_moves(
                            sim, games=depth[actor][0], salt=_ahead_salt(salt, key), moves=options))[0].move
                    sim.act(*replies[key])
                else:
                    sim.act(*policies[actor](sim, rng))
            outcome = 1.0 if sim.winner == side else 0.5 if sim.winner == "draw" else 0.0
            outcomes.append(outcome)
            margin += _health(sim, side) - _health(sim, foe) - TEMPO * (2 * outcome - 1) * (len(sim.actions) - done)
        win, draw = outcomes.count(1.0), outcomes.count(0.5)
        result.append(MoveValue(
            move=tuple(move), value=(win + 0.5 * draw) / games, win=win, draw=draw, games=games,
            margin=margin / games, outcomes=tuple(outcomes),
        ))
    return result


def _explicit(spec: Spec) -> Spec:
    """把「往前看几次」写明：任务要交给别的进程去算，不能依赖那边的默认值。"""
    if not isinstance(spec, str) and spec[0] == REPLY and len(spec) == 3:
        return (*spec, AHEAD)
    return spec


def _evaluate_one(job: tuple) -> MoveValue:
    """进程池任务：评估一个动作。"""
    battle, move, own, other, games, salt, start = job
    return evaluate_moves(battle, own=own, other=other, games=games, salt=salt, moves=[move], start=start)[0]


def evaluate_all(
    battle: Battle, *, own: Spec = OWN, other: Spec = HEURISTIC, games: int = ROLLOUT_GAMES,
    salt: int | str = 0, moves: Iterable[Move] | None = None, start: int = 0, workers: int = 0, lane: str = FAST,
) -> list[MoveValue]:
    """evaluate_moves 的并行版本：每个动作一个任务，交给进程池（默认是战斗指导专用的那条通道）。
    结果与单进程完全相同（各个动作互不影响，往前看用的种子只取决于局面）。workers <= 1 时就在当前线程里算。"""
    moves = [tuple(move) for move in (battle.legal_moves() if moves is None else moves)]
    if workers > 1 and len(moves) > 1:
        jobs = [(battle, move, _explicit(own), _explicit(other), games, salt, start) for move in moves]
        return pool_map(_evaluate_one, jobs, workers, lane)
    return evaluate_moves(battle, own=own, other=other, games=games, salt=salt, moves=moves, start=start)


def rank(values: Sequence[MoveValue]) -> list[MoveValue]:
    """从好到差：先看价值，再看终局血量差，最后按原顺序（结果确定）。"""
    order = sorted(range(len(values)), key=lambda i: (-values[i].value, -values[i].margin, i))
    return [values[i] for i in order]


def near_best(values: Sequence[MoveValue], margin: float = NEAR_MARGIN, cap: int = NEAR_CAP,
              edge: float = NEAR_EDGE) -> list[MoveValue]:
    """候选动作：推演结果与最优分不出高下的前几个。LLM 只能在这里面选，所以要保证「随便选哪个都不亏」。

    - 价值有差别的：差距不超过 margin，也不超过半个标准误（各动作用同一批种子，逐局相减）。胜率在中间
      时这大约是 24 局里差 1 局；接近必胜或必败时标准误很小，差 1 局也不算分不出高下。
    - 价值完全相同的（推演里全胜或全负的局面很常见）：终局优势不能差出 edge——这种局面里动作之间
      的区别全在「赢多少血、输得多慢」上。

    实测只按「价值相差不到 10 个百分点」来定候选时，在候选里随便选会把对上智能对手的胜率从 8% 掉到 1%。
    """
    ranked = rank(values)
    best = ranked[0]

    def close(value: MoveValue) -> bool:
        gap = best.value - value.value
        if gap <= 1e-9:
            return best.margin - value.margin <= edge + 1e-9
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
    games: int = ROLLOUT_GAMES,
) -> Move:
    """推演出招：逐个评估合法动作，取最好的。只有一个合法动作时不用推演。"""
    moves = battle.legal_moves()
    if len(moves) == 1:
        return moves[0]
    values = evaluate_moves(battle, own=own, other=other, games=games, salt=rng.getrandbits(32), moves=moves)
    return rank(values)[0].move


def expert_move(battle: Battle, rng: random.Random) -> Move:
    """对手方「智能模式」的出招（policy.EXPERT）。"""
    return rollout_move(battle, rng, games=EXPERT_GAMES)


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
            wave = workers * 2
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
            list(_get_pool(workers, FAST).map(_ready, range(2 * min(workers, 8))))
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
