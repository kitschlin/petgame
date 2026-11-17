"""战斗状态机。

用法：
    battle = Battle.new(lineups, seed)      # 完成开局结算并推进到第一个决策点
    battle.options()                        # 当前行动宠物可做什么
    events = battle.act("basic", "opp1")    # 执行并自动推进到下一个决策点

引擎自己推进所有不需要玩家决定的步骤（回合开始、结界伤害、冻结、回合结束），
只在需要选择技能和目标时停下。整个状态是普通数据，可以 clone() 出来做推演。
"""

from __future__ import annotations

from typing import Any

from .damage import grant_shield, heal, strike
from .narrate import narrate
from .numeric import cents, mul_pct, num
from .pets import (
    BARRIER_DAMAGE_PCT,
    BASIC_NAME,
    DEER_PASSIVE_PCT,
    ENERGY_CAP,
    ENERGY_REGEN,
    ENERGY_START,
    MAX_ROUNDS,
    MOUSE_START_BONUS,
    SIDE_NAMES,
    SIDES,
    SPECIES,
    TARGET_ALLY,
    TARGET_ENEMY,
    TARGET_UNACTED_ALLY,
    TEAM_SIZE,
    TURTLE_PASSIVE_PCT,
    battle_speed,
)
from .rng import Rng
from .skills import SPECIALS, basic_attack, mouse_skill_cost
from .state import Pet, Team

BASIC = "basic"
SPECIAL = "special"

ACTIVE = "active"
FINISHED = "finished"


class IllegalAction(ValueError):
    """不合法的操作（技能不可用、目标不合法、还没轮到等）。"""


class Battle:
    # ------------------------------------------------------------------ 构造

    def __init__(self) -> None:
        self.seed = 0
        self.rng = Rng(0)
        self.pets: dict[str, Pet] = {}
        self.teams: dict[str, Team] = {}
        self.round = 0
        self.queue: list[str] | None = None  # 本回合尚未行动的宠物；None 表示回合未开始
        self.done: list[str] = []  # 本回合已结束行动机会的宠物
        self.current: str | None = None  # 行动机会进行中的宠物
        self.actor: str | None = None  # 正在等待玩家操作的宠物
        self.phase = ACTIVE
        self.winner: str | None = None  # "me" / "opp" / "draw"
        self.actions: list[list[str | None]] = []
        self.events: list[dict[str, Any]] = []
        self.record = True

    @classmethod
    def new(cls, lineups: dict[str, list[dict[str, Any]]], seed: int, record: bool = True) -> "Battle":
        """lineups[side] 是按站位排列的 3 只宠物：{species, max_hp, atk, speed, hit}。"""
        b = cls()
        b.seed = seed
        b.rng = Rng(seed)
        b.record = record
        for side in SIDES:
            lineup = lineups[side]
            if len(lineup) != TEAM_SIZE or len({p["species"] for p in lineup}) != TEAM_SIZE:
                raise ValueError(f"{SIDE_NAMES[side]}阵容必须是 3 只不同的宠物")
            b.teams[side] = Team(side)
            for slot, spec in enumerate(lineup, start=1):
                if spec["species"] not in SPECIES:
                    raise ValueError(f"未知宠物：{spec['species']}")
                pet_id = f"{side}{slot}"
                b.pets[pet_id] = Pet(
                    id=pet_id, side=side, slot=slot, species=spec["species"],
                    max_hp=cents(spec["max_hp"]), atk=spec["atk"],
                    base_speed=spec["speed"], speed=spec["speed"], hit=spec["hit"],
                    hp=cents(spec["max_hp"]),
                )
        b._open()
        b._advance()
        return b

    @classmethod
    def replay(
        cls,
        lineups: dict[str, list[dict[str, Any]]],
        seed: int,
        actions: list[list[str | None]],
        record: bool = True,
    ) -> "Battle":
        """用种子和行动序列重演一场战斗。"""
        b = cls.new(lineups, seed, record)
        for skill, target in actions:
            b.act(skill, target)
        return b

    def clone(self, reseed: int | None = None, record: bool = False) -> "Battle":
        """复制当前局面用于推演。传入 reseed 可换一条随机数序列，避免预知本局真实的随机结果。"""
        b = Battle()
        b.seed = self.seed
        b.rng = Rng(reseed) if reseed is not None else self.rng.copy()
        b.pets = {pid: pet.copy() for pid, pet in self.pets.items()}
        b.teams = {side: team.copy() for side, team in self.teams.items()}
        b.round = self.round
        b.queue = None if self.queue is None else list(self.queue)
        b.done = list(self.done)
        b.current = self.current
        b.actor = self.actor
        b.phase = self.phase
        b.winner = self.winner
        b.actions = [list(a) for a in self.actions]
        b.record = record
        return b

    # ------------------------------------------------------------------ 查询

    @property
    def finished(self) -> bool:
        return self.phase == FINISHED

    def side_pets(self, side: str) -> list[Pet]:
        return [self.pets[f"{side}{slot}"] for slot in range(1, TEAM_SIZE + 1)]

    def alive(self, side: str) -> list[Pet]:
        return [p for p in self.side_pets(side) if p.alive]

    def find(self, side: str, species: str) -> Pet | None:
        for pet in self.side_pets(side):
            if pet.species == species:
                return pet
        return None

    @staticmethod
    def enemy(side: str) -> str:
        return "opp" if side == "me" else "me"

    def next_alive_after(self, pet: Pet) -> Pet | None:
        """同一方站位中位于 pet 之后的下一名存活宠物（3 号位之后回到 1 号位）。"""
        for step in range(1, TEAM_SIZE):
            slot = (pet.slot - 1 + step) % TEAM_SIZE + 1
            other = self.pets[f"{pet.side}{slot}"]
            if other.alive:
                return other
        return None

    def label(self, pet_id: str) -> str:
        pet = self.pets[pet_id]
        return SIDE_NAMES[pet.side] + SPECIES[pet.species].name

    def options(self) -> list[dict[str, Any]]:
        """当前行动宠物的两个动作：是否可用、不可用原因、合法目标（None 表示无需选目标）。"""
        if self.actor is None:
            return []
        pet = self.pets[self.actor]
        team = self.teams[pet.side]
        info = SPECIES[pet.species]
        enemies = [p.id for p in self.alive(self.enemy(pet.side))]

        targets: list[str] | None = None
        reason: str | None = None
        if info.skill_target == TARGET_ENEMY:
            targets = enemies
        elif info.skill_target == TARGET_ALLY:
            targets = [p.id for p in self.alive(pet.side)]
        elif info.skill_target == TARGET_UNACTED_ALLY:
            targets = [
                pid for pid in self.queue or []
                if self.pets[pid].side == pet.side and self.pets[pid].alive
            ]
            if not targets:
                reason = "没有本回合尚未行动的队友"
        if pet.species == "fish" and team.chain is not None:
            reason = "水链存在期间不能再次释放"
        if pet.species == "mouse" and pet.hp <= mouse_skill_cost(pet):
            reason = "生命不足"
        if reason is None and team.beans < info.skill_cost:
            reason = "能量豆不足"

        return [
            {
                "skill": BASIC, "name": BASIC_NAME, "cost": 0,
                "enabled": True, "reason": None, "targets": enemies,
            },
            {
                "skill": SPECIAL, "name": info.skill_name, "cost": info.skill_cost,
                "enabled": reason is None, "reason": reason, "targets": targets,
            },
        ]

    def legal_moves(self) -> list[tuple[str, str | None]]:
        """把 options() 展开成（技能，目标）列表，方便 AI 枚举。"""
        moves: list[tuple[str, str | None]] = []
        for option in self.options():
            if not option["enabled"]:
                continue
            if option["targets"] is None:
                moves.append((option["skill"], None))
            else:
                moves.extend((option["skill"], target) for target in option["targets"])
        return moves

    # ------------------------------------------------------------------ 操作

    def act(self, skill: str, target: str | None = None) -> list[dict[str, Any]]:
        """执行当前行动宠物的操作，并推进到下一个决策点。返回本次产生的事件。"""
        if self.finished:
            raise IllegalAction("战斗已经结束")
        if self.actor is None:
            raise IllegalAction("当前没有等待操作的宠物")
        option = next((o for o in self.options() if o["skill"] == skill), None)
        if option is None:
            raise IllegalAction(f"未知动作：{skill}")
        if not option["enabled"]:
            raise IllegalAction(f"【{option['name']}】不可用：{option['reason']}")
        if option["targets"] is None:
            if target is not None:
                raise IllegalAction(f"【{option['name']}】无需选择目标")
        elif target not in option["targets"]:
            raise IllegalAction(f"【{option['name']}】的目标不合法")

        pet = self.pets[self.actor]
        target_pet = self.pets[target] if target is not None else None
        start = len(self.events)
        self.actor = None
        self.actions.append([skill, target])

        if skill == BASIC:
            self.emit("action", pet=pet.id, skill=BASIC, name=BASIC_NAME, target=target, cost=0,
                      beans=self.teams[pet.side].beans)
            basic_attack(self, pet, target_pet)
        else:
            team = self.teams[pet.side]
            team.beans -= option["cost"]
            self.emit("action", pet=pet.id, skill=SPECIAL, name=option["name"], target=target,
                      cost=option["cost"], beans=team.beans)
            SPECIALS[pet.species](self, pet, target_pet)

        if not self.finished:
            self._end_turn(pet)
            self._advance()
        return self.events[start:]

    # ------------------------------------------------------------------ 流程

    def _open(self) -> None:
        """开局结算：能量豆（含小鼠）→ 最终速度（含小兔、小鱼）→ 小乌护盾。"""
        self.emit("battle_start", seed=self.seed)
        for side in SIDES:
            mouse = self.find(side, "mouse")
            self.teams[side].beans = ENERGY_START + (MOUSE_START_BONUS if mouse else 0)
            self.emit("energy_init", side=side, beans=self.teams[side].beans,
                      mouse=mouse.id if mouse else None)
        for pet in self.pets.values():
            pet.speed = battle_speed(pet.species, pet.base_speed)
            if pet.speed != pet.base_speed:
                self.emit("speed_bonus", pet=pet.id, speed=pet.speed)
        for side in SIDES:
            turtle = self.find(side, "turtle")
            if turtle is None:
                continue
            for pet in self.side_pets(side):
                grant_shield(
                    self, pet, mul_pct(pet.max_hp, TURTLE_PASSIVE_PCT),
                    source=turtle.id, skill=SPECIES["turtle"].passive_name,
                )

    def _advance(self) -> None:
        """推进到下一个需要玩家决策的时刻，或战斗结束。"""
        while not self.finished and self.actor is None:
            if self.queue is None:
                self._start_round()
            elif not self.queue:
                self._end_round()
            else:
                pet = self.pets[self.queue.pop(0)]
                if pet.alive:  # 已倒下的宠物直接跳过
                    self._begin_turn(pet)

    def _start_round(self) -> None:
        self.round += 1
        regen = self.round >= 2
        if regen:
            for team in self.teams.values():
                team.beans = min(ENERGY_CAP, team.beans + ENERGY_REGEN)
        self.queue, tied = self._roll_order()
        self.done = []
        self.emit(
            "round_start", round=self.round, regen=regen,
            beans={side: self.teams[side].beans for side in SIDES},
            order=list(self.queue), tied=tied,
        )

    def _roll_order(self) -> tuple[list[str], bool]:
        """按最终速度从高到低排队；同速的一组用洗牌决定先后。没有同速时不消耗随机数。"""
        pets = sorted(
            (p for p in self.pets.values() if p.alive),
            key=lambda p: (-p.speed, SIDES.index(p.side), p.slot),
        )
        order: list[str] = []
        tied = False
        i = 0
        while i < len(pets):
            j = i
            while j < len(pets) and pets[j].speed == pets[i].speed:
                j += 1
            group = pets[i:j]
            if len(group) > 1:
                tied = True
                for k in range(len(group) - 1, 0, -1):
                    r = self.rng.below(k + 1)
                    group[k], group[r] = group[r], group[k]
            order.extend(p.id for p in group)
            i = j
        return order, tied

    def _begin_turn(self, pet: Pet) -> None:
        """行动机会开始：结界伤害 → 倒下检查 → 冻结判定 → 等待操作或跳过。"""
        self.current = pet.id
        self.emit("turn_start", pet=pet.id)
        barrier = self.teams[self.enemy(pet.side)].barrier
        if barrier is not None:
            amount = mul_pct(cents(barrier.atk), BARRIER_DAMAGE_PCT)
            outcome = strike(self, pet, amount, source=None, kind="barrier")
            if self.finished:
                return
            if not pet.alive:
                self.current = None  # 被结界击倒：行动机会直接结束
                return
            if outcome.hp_lost > 0:  # 只有造成了实际生命伤害才判定冻结
                roll = self.rng.roll100()
                frozen = roll < barrier.hit
                self.emit("freeze_check", pet=pet.id, chance=barrier.hit, roll=roll, frozen=frozen)
                if frozen:
                    pet.frozen = True
                    self.emit("frozen", pet=pet.id)
                    self._end_turn(pet)
                    return
        self.actor = pet.id
        self.emit("await_input", pet=pet.id)  # 决策点：等待该宠物所属方选择技能和目标

    def _end_turn(self, pet: Pet) -> None:
        """行动机会结束：冻结解除，再结算行动结束被动。"""
        pet.frozen = False
        if pet.species == "deer" and pet.alive:
            heal(
                self, pet, mul_pct(pet.max_hp, DEER_PASSIVE_PCT),
                source=pet.id, skill=SPECIES["deer"].passive_name,
            )
        self.done.append(pet.id)
        self.current = None
        self.emit("turn_end", pet=pet.id)

    def _end_round(self) -> None:
        for side in SIDES:
            team = self.teams[side]
            if team.barrier is not None and team.barrier.expires <= self.round:
                team.barrier = None
                self.emit("barrier_end", side=side, reason="expired")
            if team.chain is not None and team.chain.expires <= self.round:
                team.chain = None
                self.emit("chain_end", side=side)
        self.emit("round_end", round=self.round)
        self.queue = None
        self.done = []
        if self.round >= MAX_ROUNDS:
            self._finish("draw", "rounds")

    # ------------------------------------------------------- 供结算模块调用

    def knock_out(self, pet: Pet) -> None:
        pet.alive = False
        pet.hp = 0
        pet.shield = 0
        pet.frozen = False
        team = self.teams[pet.side]
        if team.chain is not None and pet.id in team.chain.members:
            team.chain.members.remove(pet.id)  # 倒下后退出水链
        self.emit("ko", pet=pet.id)
        if pet.species == "snake" and team.barrier is not None:
            team.barrier = None  # 小蛇倒下，结界立即失效
            self.emit("barrier_end", side=pet.side, reason="snake_down")

    def check_end(self) -> None:
        if self.finished:
            return
        for side in SIDES:
            if not self.alive(side):
                self._finish(self.enemy(side), "wipe")
                return

    def _finish(self, winner: str, reason: str) -> None:
        self.phase = FINISHED
        self.winner = winner
        self.current = None
        self.actor = None
        self.emit("battle_end", winner=winner, reason=reason, round=self.round)

    # ------------------------------------------------------------- 事件与视图

    def emit(self, type_: str, **data: Any) -> None:
        """记录一个事件，附带中文叙述和事件发生后的局面快照。"""
        if not self.record:
            return
        event: dict[str, Any] = {"i": len(self.events), "type": type_, **data}
        event["text"] = narrate(self, event)
        event["view"] = self.view()
        self.events.append(event)

    def view(self) -> dict[str, Any]:
        """随战斗变化的动态状态。"""
        teams = {}
        for side in SIDES:
            team = self.teams[side]
            barrier = team.barrier
            chain = team.chain
            teams[side] = {
                "beans": team.beans,
                "barrier": None if barrier is None else {
                    "remaining": barrier.expires - self.round + 1,
                    "atk": barrier.atk,
                    "hit": barrier.hit,
                },
                "chain": None if chain is None else {
                    "remaining": chain.expires - self.round + 1,
                    "members": list(chain.members),
                },
            }
        pets = {}
        for pet in self.pets.values():
            chain = self.teams[pet.side].chain
            pets[pet.id] = {
                "hp": num(pet.hp),
                "shield": num(pet.shield),
                "alive": pet.alive,
                "frozen": pet.frozen,
                "linked": chain is not None and pet.id in chain.members,
            }
        return {
            "round": self.round,
            "phase": self.phase,
            "winner": self.winner,
            "current": self.current,
            "awaiting": self.actor is not None,
            "done": list(self.done),
            "upcoming": [pid for pid in self.queue or [] if self.pets[pid].alive],
            "teams": teams,
            "pets": pets,
        }

    def static(self) -> dict[str, Any]:
        """整场战斗不变的信息。"""
        pets = {}
        for pet in self.pets.values():
            info = SPECIES[pet.species]
            pets[pet.id] = {
                "id": pet.id, "side": pet.side, "slot": pet.slot,
                "species": pet.species, "name": info.name, "emoji": info.emoji,
                "max_hp": num(pet.max_hp), "atk": pet.atk,
                "base_speed": pet.base_speed, "speed": pet.speed, "hit": pet.hit,
            }
        return {
            "seed": self.seed,
            "max_rounds": MAX_ROUNDS,
            "energy_cap": ENERGY_CAP,
            "pets": pets,
        }
