import pytest
from app.utils.progress_bar import colored_progress_bar


@pytest.mark.parametrize('percent,filled', [(0,0),(1,1),(60,6),(99,9),(100,10),(-1,0),(120,10)])
def test_ten_cells_and_boundaries(percent, filled):
    bar = colored_progress_bar(percent)
    assert bar.count('▰') == filled
    assert bar.count('▱') == 10-filled
    assert bar.startswith('\u2066') and bar.splitlines()[0].endswith('\u2069')


@pytest.mark.parametrize('percent,color', [(75,'🟢'),(30,'🟡'),(10,'🔴')])
def test_time_remaining_warning_colors(percent, color):
    assert color in colored_progress_bar(percent, color='green', remaining=True)


def test_trial_and_elapsed_colors():
    assert '🟣' in colored_progress_bar(50, color='purple', remaining=True)
    assert '🔵' in colored_progress_bar(10)
