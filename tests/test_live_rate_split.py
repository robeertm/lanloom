"""Received and sent rate in one short tile text (lanloom#26): one shared unit, taken from the larger direction."""
from lanloom import live


def test_unit_comes_from_the_larger_direction_and_is_written_once():
    assert live.human_rate_split(7_300, 434_000) == "↓7.3 ↑434 kB/s"
    assert live.human_rate_split(434_000, 7_300) == "↓434 ↑7.3 kB/s"
    assert live.human_rate_split(41e6, 2e6) == "↓41 ↑2.0 MB/s"


def test_a_tiny_opposite_direction_reads_zero_not_its_own_unit():
    assert live.human_rate_split(3, 300e6 / 8, "bits") == "↓0 ↑300 Mbit/s"
    assert live.human_rate_split(0, 5_000_000) == "↓0 ↑5.0 MB/s"


def test_bits_setting_and_small_values():
    assert live.human_rate_split(125, 2_500, "bits") == "↓1.0 ↑20 kbit/s"
    assert live.human_rate_split(5, 12) == "↓5 ↑12 B/s"
    assert live.human_rate_split(5, 12, "bits") == "↓40 ↑96 bit/s"
    assert live.human_rate_split(0, 0) == "↓0 ↑0 B/s"
    assert live.human_rate_split(-4, 2e9) == "↓0 ↑2.0 GB/s"


def test_format_constant_and_unchanged_helpers():
    assert live.RATE_SPLIT == "↓%s ↑%s %s"
    assert live.human_rate(41e6) == "41 MB/s"
