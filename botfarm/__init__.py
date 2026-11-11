"""botfarm - sich selbst vermehrende Trading-Bots mit Gewinnsicherung (Paper-Trading)."""

from .config import FarmConfig
from .farm import Farm

__all__ = ["Farm", "FarmConfig"]
