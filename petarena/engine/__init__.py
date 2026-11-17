"""纯规则引擎：零 I/O、确定性、可克隆。"""

from .battle import Battle, IllegalAction
from .draft import Draft, DraftError

__all__ = ["Battle", "IllegalAction", "Draft", "DraftError"]
