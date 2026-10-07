from __future__ import annotations

import pytest

from bot.utils.anthropic.models import DEFAULT_MODEL_ID, MODELS, calc_cost, get_model
from tests.conftest import make_usage


def test_get_model_maps_legacy_and_unknown_ids():
    assert get_model("claude-haiku-4-5").id == "claude-haiku-5-5"
    assert get_model("claude-sonnet-4-6").id == "claude-sonnet-5-5"
    assert get_model("claude-opus-5-5").id == "claude-opus-5-5"
    assert get_model(None).id == DEFAULT_MODEL_ID
    assert get_model("no-such-model").id == DEFAULT_MODEL_ID


def test_haiku_short_prompt_pricing():
    haiku = MODELS["claude-haiku-5-5"]
    usage = make_usage(input_tokens=50_000, output_tokens=10_000)
    assert calc_cost(haiku, usage) == pytest.approx((50_000 * 0.10 + 10_000 * 0.50) / 1_000_000)


def test_haiku_long_prompt_uses_higher_rate_card():
    haiku = MODELS["claude-haiku-5-5"]
    # 20K некэшированных + 90K из кэша = 110K промпта > 100K → дорогой тариф
    usage = make_usage(input_tokens=20_000, output_tokens=1_000, cache_read=90_000)
    expected = (20_000 * 0.50 + 1_000 * 2.50 + 90_000 * 0.05) / 1_000_000
    assert calc_cost(haiku, usage) == pytest.approx(expected)


def test_sonnet_cache_and_web_search_cost():
    sonnet = MODELS["claude-sonnet-5-5"]
    usage = make_usage(input_tokens=1_000, output_tokens=2_000, cache_write=10_000, cache_read=50_000, searches=3)
    expected = (1_000 * 2.0 + 2_000 * 10.0 + 10_000 * 2.5 + 50_000 * 0.10) / 1_000_000 + 3 * 0.01
    assert calc_cost(sonnet, usage) == pytest.approx(expected)
