"""Ten-cell Telegram progress bars, isolated from surrounding RTL text."""
import math


def colored_progress_bar(percent: float, *, color: str = 'blue', remaining: bool = False) -> str:
    percent = float(percent)
    percent = max(0.0, min(100.0, percent)) if math.isfinite(percent) else 0.0
    if remaining:
        color = 'red' if percent <= 20 else 'yellow' if percent <= 40 else color
    cells = {'blue': '🟦', 'green': '🟩', 'purple': '🟪', 'yellow': '🟨', 'red': '🟥'}
    filled = int(percent / 10 + 0.5)
    if 0 < percent < 100:
        filled = max(1, min(9, filled))
    return '\u2066' + cells[color] * filled + '⬜' * (10 - filled) + '\u2069'
