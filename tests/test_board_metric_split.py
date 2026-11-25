"""Tile rate with received and sent apart (lanloom#26): the arrows are read out in words, an ordinary rate stays as it is."""
from _board_node import run_node


def test_arrows_become_words_and_plain_rates_stay():
    r = run_node("metric_split_test.js")
    assert r["split"] == "received 7.3, sent 434 kB/s"
    assert r["comma"] == "received 7,3, sent 434 kB/s"
    assert r["plain"] == ["1.5 MB/s", "", "12 msg/s", "↓ broken"]


def test_a_split_rate_on_a_narrow_tile_gives_up_the_per_second_before_a_letter():
    fit = run_node("metric_split_test.js")["fit"]
    assert fit[0] == ["↓2.8 ↑0.2 MB/s", "↓2.8 ↑0.2 MB", "↓2.8 ↑0.2…"]
    assert fit[1][1] == "↓986 ↑146 kB"
    assert fit[2] == ["12 MB/s"] * 3                      # an ordinary rate is never touched
