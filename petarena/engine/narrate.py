"""把结构化事件转成中文战斗日志。没有文字的事件返回 None。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .pets import SIDE_NAMES, SPECIES

if TYPE_CHECKING:
    from .battle import Battle


def _n(value: int | float) -> str:
    """数值展示：整数不带小数，否则固定两位小数。"""
    if isinstance(value, int):
        return str(value)
    return f"{value:.2f}"


def _damage(b: "Battle", e: dict[str, Any]) -> str:
    target = b.label(e["target"])
    kind = e["kind"]
    if kind == "barrier":
        prefix = "寒毒结界："
    elif kind == "overflow":
        prefix = "【连环收割】溢出伤害转移："
    elif kind == "pursuit":
        prefix = f"{b.label(e['source'])}【协同补刀】追击："
    else:
        prefix = ""
    text = f"{prefix}{target}受到 {_n(e['amount'])} 点伤害"
    parts = e["parts"]
    chained = e["reduced"] is not None
    if chained:
        text += f"，水链减免后为 {_n(e['reduced'])}，{len(parts)} 只均分各 {_n(parts[0]['amount'])}"

    details = []
    for part in parts:
        bits = []
        if part["absorbed"]:
            bits.append(f"护盾抵扣 {_n(part['absorbed'])}")
        if part["lost"]:
            bits.append(f"生命 -{_n(part['lost'])}（剩余 {_n(part['hp'])}）")
        if not bits:
            bits.append("未受伤害")
        name = b.label(part["pet"]) + " " if chained else ""
        details.append(name + "，".join(bits))
    return text + "：" + "；".join(details)


def narrate(b: "Battle", e: dict[str, Any]) -> str | None:
    kind = e["type"]

    if kind == "battle_start":
        return f"战斗开始（随机种子 {e['seed']}）"

    if kind == "energy_init":
        extra = "，含小鼠【提前备粮】+3" if e["mouse"] else ""
        return f"{SIDE_NAMES[e['side']]}初始能量豆 {e['beans']} 点{extra}"

    if kind == "speed_bonus":
        passive = SPECIES[b.pets[e["pet"]].species].passive_name
        return f"{b.label(e['pet'])}【{passive}】速度 +100，最终速度 {e['speed']}"

    if kind == "shield":
        text = f"{b.label(e['source'])}【{e['skill']}】为{b.label(e['pet'])}提供 {_n(e['amount'])} 点护盾"
        if not e["replaced"]:
            text += f"，不高于现有护盾 {_n(e['shield'])}，保留现有护盾"
        return text

    if kind == "round_start":
        text = f"—— 第 {e['round']} 回合 ——"
        if e["regen"]:
            beans = e["beans"]
            text += f" 双方各恢复 2 点能量豆（对手方 {beans['opp']}，我方 {beans['me']}）。"
        order = " → ".join(b.label(pid) for pid in e["order"])
        text += f" 行动顺序：{order}"
        if e["tied"]:
            text += "（同速随机）"
        return text

    if kind == "turn_start":
        return None

    if kind == "action":
        pet = b.label(e["pet"])
        if e["skill"] == "basic":
            return f"{pet}向{b.label(e['target'])}发动普通攻击"
        cost = f"（消耗 {e['cost']} 点能量豆，剩余 {e['beans']}）" if e["cost"] else ""
        if e["target"] is None:
            return f"{pet}使用【{e['name']}】{cost}"
        same_side = b.pets[e["target"]].side == b.pets[e["pet"]].side
        word = "为" if same_side else "向"
        who = "自己" if e["target"] == e["pet"] else b.label(e["target"])
        return f"{pet}{word}{who}使用【{e['name']}】{cost}"

    if kind == "damage":
        return _damage(b, e)

    if kind == "ko":
        return f"{b.label(e['pet'])}倒下了！"

    if kind == "pursuit_check":
        result = "触发" if e["success"] else "未触发"
        return f"{b.label(e['pet'])}【协同补刀】判定（{e['chance']}%）：{result}"

    if kind == "freeze_check":
        if e["frozen"]:
            return None  # 紧接着的 frozen 事件负责提示
        return f"{b.label(e['pet'])}冻结判定（{e['chance']}%）：未冻结"

    if kind == "frozen":
        return f"{b.label(e['pet'])}被冻结，跳过本次操作"

    if kind == "heal":
        if not e["actual"]:
            return f"{b.label(e['pet'])}【{e['skill']}】生命已满，没有恢复"
        return (
            f"{b.label(e['pet'])}【{e['skill']}】恢复 {_n(e['actual'])} 点生命"
            f"（当前 {_n(e['hp'])}）"
        )

    if kind == "hp_cost":
        return f"{b.label(e['pet'])}【{e['skill']}】消耗 {_n(e['amount'])} 点生命（剩余 {_n(e['hp'])}）"

    if kind == "energy":
        return f"{SIDE_NAMES[e['side']]}能量豆 +{e['gain']}，当前 {e['beans']}"

    if kind == "pull":
        return f"{b.label(e['target'])}将紧接着行动"

    if kind == "barrier_cast":
        verb = "刷新" if e["refreshed"] else "展开"
        return f"{SIDE_NAMES[e['side']]}寒毒结界{verb}，持续到第 {e['expires']} 回合结束"

    if kind == "barrier_end":
        if e["reason"] == "snake_down":
            return f"{SIDE_NAMES[e['side']]}小蛇倒下，寒毒结界立即失效"
        return f"{SIDE_NAMES[e['side']]}寒毒结界到期消失"

    if kind == "chain_cast":
        members = "、".join(b.label(pid) for pid in e["members"])
        return f"同心水链连接了{members}，持续到第 {e['expires']} 回合结束"

    if kind == "chain_end":
        return f"{SIDE_NAMES[e['side']]}同心水链到期消失"

    if kind == "battle_end":
        if e["winner"] == "draw":
            return "游戏结束：10 个大回合未分胜负，平局"
        return f"游戏结束：{SIDE_NAMES[e['winner']]}获胜！"

    return None
