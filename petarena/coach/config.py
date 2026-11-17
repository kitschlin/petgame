"""Coach 配置：环境变量优先，其次是项目根目录的 .env（不提交到仓库）。

    COACH_API_KEY      LLM 的 key（兼容旧名 DEEPSEEK_API_KEY）
    COACH_MODEL        默认 deepseek-flash
    COACH_BASE_URL     默认 https://api.deepseek.com；任何 OpenAI 兼容的 /chat/completions 都可以，
                       例如 SiliconFlow：https://api.siliconflow.cn/v1
    COACH_TIMEOUT      单次请求超时（秒），默认 20
    PETARENA_COACH     off / live（测试另有 fake）；不设置时有 key 为 live，否则 off。
                       没有 key 时不会退回假模型：AI 军师面板只提示「请配置 API」（setup_needed）；
                       显式设为 off 时整个 AI Coach 不出现

战斗指导、复盘和培养建议的计算量（都有默认值，一般不用改）：
    COACH_FAST_TIMEOUT      战斗指导单次请求超时（秒），默认 8：每次行动都要等它，不能太久
    COACH_ADVICE_ROLLOUTS   战斗指导时每个合法动作推演几局，默认按机器核数自动取 128～1024（见 matchup.auto_advice_games）
    COACH_REVIEW_ROLLOUTS   复盘时每个合法动作推演几局，默认 128
    COACH_REFLECT           对局结束后的后台任务（入库、讲解、定向推演、培养方案）怎么跑：
                            background（默认，后台线程）/ sync（当场跑完，测试用）/ off（只在打开复盘页时分析）
    COACH_REFLECT_GAMES     每场对局后的定向推演一共打多少局（照指导出招的整局模拟，比较贵），默认 160
    COACH_GROWTH            培养建议怎么给：light（默认，不做模拟，只按台阶给方向，立刻就有）/
                            full（逐项模拟，给出带胜算的路线；每一步要几十秒的后台计算）
    COACH_GROWTH_GAMES      full 模式下，培养方案粗筛时每个候选打几局，默认 120
    COACH_GROWTH_VERIFY     培养方案决选时，每个候选一共打几局（平摊到各套对手阵容上；照指导出招、对手按
                            真实打法，比较贵），默认 48；0 表示只按常规打法粗筛
    COACH_GROWTH_TARGET     培养建议的目标胜率，默认 0.6
    COACH_BATTLE_AGENT      战斗 Agent 的对话方式：step（默认，每次行动一段新对话）/ session（整局一段对话）
    COACH_SEED              on（默认）/ off：用不用随代码附带的内置经验（离线普查过的「照指导出招」的对阵战绩）

fake 模式不联网，用固定规则出方案，只给自动化测试用（必须显式设置，永远不会自动启用）。它还有两个调试开关：
    COACH_FAKE_FAILS   每一手的前 N 次请求故意返回无法解析的内容（演示重试）
    COACH_FAKE_DELAY   每次请求先等待这么多秒（演示「思考中」和取消）
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"

OFF = "off"
FAKE = "fake"
LIVE = "live"
MODES = (OFF, FAKE, LIVE)

BACKGROUND = "background"
SYNC = "sync"
REFLECT_MODES = (BACKGROUND, SYNC, OFF)

GROWTH_LIGHT = "light"
GROWTH_FULL = "full"
GROWTH_MODES = (GROWTH_LIGHT, GROWTH_FULL)

BATTLE_STEP = "step"
BATTLE_SESSION = "session"
BATTLE_MODES = (BATTLE_STEP, BATTLE_SESSION)

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_TIMEOUT = 20.0
DEFAULT_FAST_TIMEOUT = 8.0


@dataclass(frozen=True)
class CoachConfig:
    mode: str = OFF
    api_key: str | None = None
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT
    temperature: float = 0.4
    max_tokens: int = 1800
    fake_fails: int = 0
    fake_delay: float = 0.0
    fast_timeout: float = DEFAULT_FAST_TIMEOUT  # 战斗指导的单次请求超时
    fast_tokens: int = 420  # 战斗指导每次回复的输出上限：一句想法 + 一次工具调用
    battle_agent: str = BATTLE_STEP  # 战斗 Agent：step 每次行动一段新对话 / session 整局一段对话
    advice_games: int = 128
    review_games: int = 128
    reflect: str = BACKGROUND
    reflect_games: int = 160
    growth_games: int = 120
    growth_verify: int = 48
    growth_target: float = 0.6
    growth: str = GROWTH_LIGHT  # 培养建议：light 只按台阶给方向 / full 逐项模拟
    seed: bool = True  # 用不用内置经验（petarena/coach/data/experience-base.json）
    setup_needed: bool = False  # 没有配置 key 而关闭（不是玩家显式关掉的）：界面上提示去配置 API

    @property
    def enabled(self) -> bool:
        return self.mode != OFF


def read_env_file(path: str | Path) -> dict[str, str]:
    """读取 KEY=VALUE 形式的 .env；忽略空行、注释和格式不对的行。"""
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_config(
    env: Mapping[str, str] | None = None, env_file: str | Path | None = ENV_FILE
) -> CoachConfig:
    from .matchup import auto_advice_games  # 延迟导入：只有读配置时才需要推演模块

    env = os.environ if env is None else env
    file_values = read_env_file(env_file) if env_file else {}

    def get(name: str) -> str | None:
        value = env.get(name)
        if value is None:
            value = file_values.get(name)
        return value.strip() if value and value.strip() else None

    key = get("COACH_API_KEY") or get("DEEPSEEK_API_KEY")
    mode = (get("PETARENA_COACH") or "").lower()
    if mode not in MODES:
        mode = LIVE if key else OFF
    if mode == LIVE and not key:
        mode = OFF  # 没有 key 就无法调用真实模型（也不退回假模型）
    setup_needed = mode == OFF and not key and (get("PETARENA_COACH") or "").lower() != OFF
    def number(name: str, default: float) -> float:
        try:
            return float(get(name) or default)
        except ValueError:
            return default

    def count(name: str, default: int, low: int = 1) -> int:
        return max(low, int(number(name, default)))

    timeout = number("COACH_TIMEOUT", DEFAULT_TIMEOUT)
    fast = number("COACH_FAST_TIMEOUT", DEFAULT_FAST_TIMEOUT)
    reflect = (get("COACH_REFLECT") or BACKGROUND).lower()
    growth = (get("COACH_GROWTH") or GROWTH_LIGHT).lower()
    battle_agent = (get("COACH_BATTLE_AGENT") or BATTLE_STEP).lower()
    target = number("COACH_GROWTH_TARGET", 0.6)
    return CoachConfig(
        mode=mode,
        api_key=key,
        model=get("COACH_MODEL") or DEFAULT_MODEL,
        base_url=(get("COACH_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
        timeout=timeout if timeout > 0 else DEFAULT_TIMEOUT,
        fake_fails=max(0, int(number("COACH_FAKE_FAILS", 0))),
        fake_delay=max(0.0, number("COACH_FAKE_DELAY", 0.0)),
        fast_timeout=fast if fast > 0 else DEFAULT_FAST_TIMEOUT,
        advice_games=count("COACH_ADVICE_ROLLOUTS", auto_advice_games(), 2),
        review_games=count("COACH_REVIEW_ROLLOUTS", 128, 2),
        reflect=reflect if reflect in REFLECT_MODES else BACKGROUND,
        reflect_games=count("COACH_REFLECT_GAMES", 160, 0),
        growth_games=count("COACH_GROWTH_GAMES", 120, 4),
        growth_verify=count("COACH_GROWTH_VERIFY", 48, 0),
        growth_target=target if 0 < target < 1 else 0.6,
        growth=growth if growth in GROWTH_MODES else GROWTH_LIGHT,
        battle_agent=battle_agent if battle_agent in BATTLE_MODES else BATTLE_STEP,
        seed=(get("COACH_SEED") or "on").lower() not in ("off", "0", "false", "no"),
        setup_needed=setup_needed,
    )
