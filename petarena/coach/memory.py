"""经验库：AI Coach 从真实对局里记下来、之后会用到的东西（SQLite 单文件）。

    battles     每场对局的结论：对阵、结果、四项归因、玩家口径、引导口径下可达到的上限、分析明细和讲解
    decisions   我方每个决策点：实际动作 / 推演最优 / 常规打法会选的动作各自的价值、评级、当时的建议与是否采纳
    experience  对阵经验：某种对手、某两套有序阵容、我方当时的属性下，实战（real）和照指导推演（guided）的战绩
    lessons     经验条目：复盘时沉淀的「宜 / 忌」，被再次印证时累加
    advice      选将和战斗的每条建议，以及玩家是否采纳
    cache       算好的方案（例如培养方案）

为什么用数据库而不是继续堆 JSON 文件：选将时要按「对手模式 + 对手阵容 + 我方成员」
聚合几百条经验、按宠物检索经验条目，每场对局结束又要一次写进好几张表（必须同时成功）。

并发：服务是多线程的，这里只开一个连接，所有读写都在一把进程内的锁里；一场对局的
入库是一个事务。AI Coach 关闭时不会创建这个文件。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS battles (
    id TEXT PRIMARY KEY,
    ended_at TEXT, opponent TEXT, winner TEXT, rounds INTEGER,
    my_lineup TEXT, opp_lineup TEXT, my_sig TEXT,
    status TEXT,                       -- ready / legacy
    outcome REAL,                      -- 1 胜 / 0.5 平 / 0 负
    p0 REAL, best_p0 REAL, best_lineup TEXT,   -- 常规打法口径：实际阵容、对这套对手阵容最好的阵容
    shown REAL,                        -- 选将时面板显示的胜率（最后一手建议）
    attr_stats REAL, attr_lineup REAL, attr_play REAL, attr_luck REAL,   -- 四项归因，相加 = outcome − 0.5
    n_decisions INTEGER, n_good INTEGER, n_advised INTEGER, n_adopted INTEGER,   -- 有选择余地的决策数、其中打得好的、有建议的、采纳的
    gap REAL, captured REAL, missed REAL,      -- Σ(最优 − 常规)、Σ(实际 − 常规)、Σ(最优 − 实际)
    ceiling REAL, ceiling_lineup TEXT, ceiling_games INTEGER,   -- 照指导出招时可达到的最好结果（后台推演后填写）
    reflected INTEGER DEFAULT 0,
    verdict TEXT,                      -- win / train / lineup / play / luck
    facts TEXT, report TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    battle_id TEXT, idx INTEGER, round INTEGER, actor TEXT, species TEXT,
    played TEXT, best TEXT, heur TEXT,
    v_played REAL, v_best REAL, v_heur REAL, loss REAL, se REAL, grade TEXT,
    advised TEXT, adopted INTEGER,
    PRIMARY KEY (battle_id, idx)
);
CREATE TABLE IF NOT EXISTS experience (
    opponent TEXT, my_lineup TEXT, opp_lineup TEXT, my_sig TEXT, source TEXT,
    win REAL, draw REAL, games REAL, p0 REAL, updated TEXT,
    PRIMARY KEY (opponent, my_lineup, opp_lineup, my_sig, source)
);
CREATE TABLE IF NOT EXISTS lessons (
    key TEXT PRIMARY KEY, opponent TEXT, scope TEXT, pets TEXT, against TEXT, polarity TEXT,
    text TEXT, support INTEGER, contra INTEGER, first_battle TEXT, last_battle TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS advice (
    token TEXT PRIMARY KEY, match_id TEXT, kind TEXT, idx INTEGER,
    advised TEXT, chosen TEXT, adopted INTEGER, shown REAL, time TEXT
);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT);
CREATE INDEX IF NOT EXISTS experience_by_opponent ON experience (opponent, opp_lineup);
CREATE INDEX IF NOT EXISTS advice_by_match ON advice (match_id);
"""

REAL = "real"  # 真实对局的结果
GUIDED = "guided"  # 照指导出招的整局推演

BATTLE_COLUMNS = (
    "id", "ended_at", "opponent", "winner", "rounds", "my_lineup", "opp_lineup", "my_sig", "status", "outcome",
    "p0", "best_p0", "best_lineup", "shown", "attr_stats", "attr_lineup", "attr_play", "attr_luck",
    "n_decisions", "n_good", "n_advised", "n_adopted", "gap", "captured", "missed",
    "ceiling", "ceiling_lineup", "ceiling_games", "reflected", "verdict", "facts", "report",
)
DECISION_COLUMNS = (
    "battle_id", "idx", "round", "actor", "species", "played", "best", "heur",
    "v_played", "v_best", "v_heur", "loss", "se", "grade", "advised", "adopted",
)
JSON_FIELDS = {"facts", "report", "played", "best", "heur", "advised", "chosen", "pets", "against"}
LINEUP_FIELDS = {"my_lineup", "opp_lineup", "best_lineup", "ceiling_lineup"}

QUALITY_PRIOR = 0.5  # 估计玩家口径时的先验强度（以「常规打法到最优之间的价值差」计，大约相当于一场对局）
QUALITY_RECENT = 5  # 只看最近几场
QUALITY_TRUST = 10  # AI 给过这么多次建议之后，「采纳了多少」才完全算数
QUALITY_NEW = 1.0  # 一局都还没打的玩家：先假设他会照着战斗指导出招


def join(lineup: Sequence[str] | None) -> str | None:
    return None if lineup is None else ",".join(lineup)


def split(text: str | None) -> tuple[str, ...] | None:
    return None if text is None else tuple(key for key in text.split(",") if key)


def signature(roster: dict[str, Any], lineup: Sequence[str]) -> str:
    """我方这几只宠物当时的属性（按宠物名排序，与站位无关）。属性一变，签名就变。"""
    return ";".join(
        f"{key}:{roster['me'][key]['max_hp']}/{roster['me'][key]['atk']}/{roster['me'][key]['speed']}/{roster['me'][key]['hit']}"
        for key in sorted(lineup)
    )


def _encode(column: str, value: Any) -> Any:
    if value is None:
        return None
    if column in LINEUP_FIELDS:
        return value if isinstance(value, str) else join(value)
    if column in JSON_FIELDS:
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return int(value)
    return value


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for column, value in data.items():
        if value is None:
            continue
        if column in LINEUP_FIELDS:
            data[column] = list(split(value))
        elif column in JSON_FIELDS:
            data[column] = json.loads(value)
        elif column in ("adopted", "reflected"):
            data[column] = bool(value)
    return data


def lesson_key(opponent: str, scope: str, polarity: str, pets: Iterable[str], against: Iterable[str]) -> str:
    return f"{opponent}|{scope}|{polarity}|{','.join(sorted(pets))}|{','.join(sorted(against))}"


class Memory:
    def __init__(self, path: str | Path | None = None) -> None:
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path) if path is not None else ":memory:"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _all(self, sql: str, args: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [_decode(row) for row in self._db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: Sequence[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            return _decode(self._db.execute(sql, args).fetchone())

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S")

    # ------------------------------------------------------------ 建议与采纳

    def note_advice(self, token: str, match_id: str, kind: str, index: int, advised: Any,
                    shown: float | None = None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO advice (token, match_id, kind, idx, advised, shown, time) VALUES (?,?,?,?,?,?,?)",
                (token, match_id, kind, index, json.dumps(advised, ensure_ascii=False), shown, self._now()))

    def note_choice(self, token: str, chosen: Any, adopted: bool) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE advice SET chosen = ?, adopted = ? WHERE token = ?",
                             (json.dumps(chosen, ensure_ascii=False), int(adopted), token))

    def advice_of(self, match_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is None:
            return self._all("SELECT * FROM advice WHERE match_id = ? ORDER BY kind DESC, idx", (match_id,))
        return self._all("SELECT * FROM advice WHERE match_id = ? AND kind = ? ORDER BY idx", (match_id, kind))

    # ------------------------------------------------------------ 对局

    def battle(self, battle_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM battles WHERE id = ?", (battle_id,))

    def battles(self, limit: int | None = None, status: str | None = "ready") -> list[dict[str, Any]]:
        """最近的对局在前。facts / report 比较大，这里不取。"""
        columns = ", ".join(c for c in BATTLE_COLUMNS if c not in ("facts", "report"))
        sql = f"SELECT {columns} FROM battles"
        args: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY ended_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        return self._all(sql, args)

    def known_battles(self) -> set[str]:
        with self._lock:
            return {row[0] for row in self._db.execute("SELECT id FROM battles").fetchall()}

    def unfinished(self) -> list[str]:
        """入了库、但后面的步骤（定向推演、讲解）还没做完的对局：服务中途停掉时会留下这样的。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM battles WHERE status = 'ready' AND (reflected = 0 OR report IS NULL) ORDER BY ended_at, id")
            return [row[0] for row in rows.fetchall()]

    def save_battle(self, row: dict[str, Any], decisions: Sequence[dict[str, Any]] = (),
                    experience: Sequence[dict[str, Any]] = ()) -> None:
        """一场对局入库：对局结论、每步决策、实战战绩在同一个事务里写入（重复入库会覆盖，不会重复累加）。"""
        with self._lock, self._db:
            exists = self._db.execute("SELECT 1 FROM battles WHERE id = ?", (row["id"],)).fetchone()
            columns = [c for c in BATTLE_COLUMNS if c in row]
            self._db.execute(
                f"INSERT OR REPLACE INTO battles ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                [_encode(c, row[c]) for c in columns])
            self._db.execute("DELETE FROM decisions WHERE battle_id = ?", (row["id"],))
            for decision in decisions:
                data = {**decision, "battle_id": row["id"]}
                self._db.execute(
                    f"INSERT INTO decisions ({', '.join(DECISION_COLUMNS)}) VALUES ({', '.join('?' * len(DECISION_COLUMNS))})",
                    [_encode(c, data.get(c)) for c in DECISION_COLUMNS])
            if not exists:
                for item in experience:
                    self._add_experience(**item)

    def update_battle(self, battle_id: str, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - set(BATTLE_COLUMNS)
        if unknown:
            raise ValueError(f"battles 表没有这些列：{sorted(unknown)}")
        with self._lock, self._db:
            self._db.execute(
                f"UPDATE battles SET {', '.join(f'{c} = ?' for c in fields)} WHERE id = ?",
                [_encode(c, v) for c, v in fields.items()] + [battle_id])

    def decisions(self, battle_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM decisions WHERE battle_id = ? ORDER BY idx", (battle_id,))

    # ------------------------------------------------------------ 对阵经验

    def _add_experience(self, opponent: str, my_lineup: Sequence[str], opp_lineup: Sequence[str], my_sig: str,
                        source: str, win: float, draw: float, games: float, p0: float | None) -> None:
        self._db.execute(
            """INSERT INTO experience (opponent, my_lineup, opp_lineup, my_sig, source, win, draw, games, p0, updated)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (opponent, my_lineup, opp_lineup, my_sig, source) DO UPDATE SET
                   win = win + excluded.win, draw = draw + excluded.draw, games = games + excluded.games,
                   p0 = COALESCE(excluded.p0, p0), updated = excluded.updated""",
            (opponent, join(my_lineup), join(opp_lineup), my_sig, source, win, draw, games, p0, self._now()))

    def add_experience(self, opponent: str, my_lineup: Sequence[str], opp_lineup: Sequence[str], my_sig: str,
                       source: str, win: float, draw: float, games: float, p0: float | None) -> None:
        """累加一条对阵经验（同一组键的战绩相加）。"""
        with self._lock, self._db:
            self._add_experience(opponent, my_lineup, opp_lineup, my_sig, source, win, draw, games, p0)

    def experience(self, opponent: str | None = None) -> list[dict[str, Any]]:
        if opponent is None:
            return self._all("SELECT * FROM experience")
        return self._all("SELECT * FROM experience WHERE opponent = ?", (opponent,))

    # ------------------------------------------------------------ 经验条目

    def add_lesson(self, battle_id: str, opponent: str, scope: str, polarity: str, pets: Sequence[str],
                   against: Sequence[str], text: str) -> str:
        """记下一条经验；同一范围、同一对象的条目合并为「再次印证」，相反结论的条目记一次「被反驳」。"""
        key = lesson_key(opponent, scope, polarity, pets, against)
        opposite = lesson_key(opponent, scope, "avoid" if polarity == "do" else "do", pets, against)
        now = self._now()
        with self._lock, self._db:
            row = self._db.execute("SELECT last_battle FROM lessons WHERE key = ?", (key,)).fetchone()
            if row is None:
                self._db.execute(
                    """INSERT INTO lessons (key, opponent, scope, pets, against, polarity, text, support, contra,
                                            first_battle, last_battle, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key, opponent, scope, json.dumps(sorted(pets)), json.dumps(sorted(against)), polarity, text,
                     1, 0, battle_id, battle_id, now))
            elif row[0] != battle_id:  # 同一场对局重复讲解不重复计数
                self._db.execute(
                    "UPDATE lessons SET support = support + 1, text = ?, last_battle = ?, updated = ? WHERE key = ?",
                    (text, battle_id, now, key))
            if row is None or row[0] != battle_id:
                self._db.execute("UPDATE lessons SET contra = contra + 1, updated = ? WHERE key = ?", (now, opposite))
        return key

    def lessons(self, opponent: str | None = None, active: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM lessons"
        clauses, args = [], []
        if opponent is not None:
            clauses.append("opponent = ?")
            args.append(opponent)
        if active:
            clauses.append("support > contra")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return self._all(sql + " ORDER BY updated DESC, key", args)

    # ------------------------------------------------------------ 玩家口径

    def quality(self, recent: int = QUALITY_RECENT) -> dict[str, Any]:
        """玩家的操作离「常规打法」和「推演最优」各有多远。

        每个决策点有三个价值：推演最优、玩家实际、常规打法。q = Σ(实际 − 常规) / Σ(最优 − 常规)：
        q = 1 表示每一步都打到了推演最优（照着战斗指导打就是这样），q = 0 表示和常规打法一样。
        选将时用它在「常规口径的表值」和「照指导出招的推演值」之间取值。

        样本少时向一个先验收缩。大势已去的对局里各个动作的价值都差不多，Σ(最优 − 常规) 很小，
        这个比值说明不了什么；这时看玩家是不是照着建议打：先验从 0.5 起步，建议采纳得越多越靠近 1
        （不采纳不扣分——没照建议打不等于打得差，那要靠上面的比值来说话）。
        """
        rows = self.battles(limit=recent)
        gap = sum(row["gap"] or 0.0 for row in rows)
        captured = sum(row["captured"] or 0.0 for row in rows)
        decisions = sum(row["n_decisions"] or 0 for row in rows)
        good = sum(row["n_good"] or 0 for row in rows)
        advised = sum(row["n_advised"] or 0 for row in rows)
        adopted = sum(row["n_adopted"] or 0 for row in rows)
        prior = 0.5 + 0.5 * (adopted / advised) * min(1.0, advised / QUALITY_TRUST) if advised else 0.5
        if not rows:  # 一局都还没打：先假设玩家会照着战斗指导出招——军师的推荐和面板上的胜率都按这个前提给
            prior = QUALITY_NEW
        q = (captured + prior * QUALITY_PRIOR) / (max(gap, 0.0) + QUALITY_PRIOR)
        return {
            "q": min(1.0, max(0.0, q)), "battles": len(rows), "decisions": decisions, "good": good,
            "gap": round(gap, 3), "captured": round(captured, 3), "advised": advised, "adopted": adopted,
        }

    # ------------------------------------------------------------ 缓存

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def put(self, key: str, value: Any) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO cache (key, value, updated) VALUES (?,?,?)",
                             (key, json.dumps(value, ensure_ascii=False), self._now()))

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {table: self._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("battles", "decisions", "experience", "lessons", "advice")}
