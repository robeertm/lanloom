"""Model size advice."""
from lanloom.ai import hardware
from lanloom.ai.hardware import advise


def test_below_four_gb_is_unsuitable():
    a = advise(2, 4)
    assert a["speed"] == "unsuitable" and a["model"] == "" and "ai.hw.use_public" in a["notes"] and "ai.hw.too_small" in a["notes"]
    assert advise(3.9, 16, gpu="nvidia")["speed"] == "unsuitable"


def test_six_gb_small_model_with_notes():
    a = advise(6, 4)
    assert a["size"] == "small" and a["model"] == "llama3.2:3b"
    assert "ai.hw.small_model" in a["notes"] and "ai.hw.slow" in a["notes"] and "ai.hw.consider_other" in a["notes"]


def test_small_arm_board_gets_a_warning():
    assert "ai.hw.small_board" in advise(6, 4, arch="aarch64")["notes"]
    assert "ai.hw.small_board" not in advise(6, 4, arch="x86_64")["notes"]
    assert "ai.hw.small_board" not in advise(16, 4, arch="aarch64")["notes"]


def test_sixteen_gb_without_gpu_medium_and_slow_on_four_cores():
    a = advise(16, 4)
    assert a["size"] == "medium" and a["model"] == "llama3.1:8b" and a["speed"] == "slow"
    assert "ai.hw.slow" in a["notes"]
    assert advise(16, 8)["speed"] == "ok"


def test_large_model_needs_gpu():
    a = advise(32, 8, gpu="nvidia rtx")
    assert a["size"] == "large" and a["model"] == "qwen2.5:14b" and a["speed"] == "good" and a["notes"] == []
    assert advise(32, 16)["size"] == "medium"          # lots of memory but no graphics unit
    assert advise(12, 8, gpu="nvidia")["size"] == "medium"


def test_gpu_values_that_mean_none():
    for g in ("", "none", "unknown", "no", None):
        assert advise(16, 4, gpu=g)["speed"] == "slow"


def test_unknown_memory_defaults_to_medium_with_note():
    a = advise(0, 0)
    assert a["model"] == "llama3.1:8b" and "ai.hw.unknown_ram" in a["notes"]
    assert advise(None, "x")["model"] == "llama3.1:8b" and advise("junk", None)["speed"] != "unsuitable"


def test_demo_uses_sample_models_only():
    for ram, gpu in ((6, ""), (16, ""), (32, "nvidia"), (64, "nvidia")):
        a = advise(ram, 8, gpu=gpu, demo=True)
        assert a["model"].startswith("sample-")
    assert advise(32, 8, "nvidia", demo=True)["model"] == "sample-large"
    assert advise(2, 8, demo=True)["model"] == ""


def test_every_real_tier_has_its_demo_twin():
    assert [(t[0], t[2], t[3]) for t in hardware.TIERS] == [(t[0], t[2], t[3]) for t in hardware.DEMO_TIERS]


def test_this_host_reads_memory_and_cores():
    h = hardware.this_host()
    assert h["cores"] >= 1 and h["ram_gb"] >= 0 and set(h) == {"ram_gb", "cores", "gpu", "arch"}
