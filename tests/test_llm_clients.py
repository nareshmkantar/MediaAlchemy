from sia.utils.llm_clients import _is_temperature_restricted_model


def test_temperature_restricted_for_gpt53_chat():
    assert _is_temperature_restricted_model("gpt-5.3-chat") is True


def test_temperature_restricted_for_gpt5_mini():
    assert _is_temperature_restricted_model("gpt-5-mini") is True


def test_temperature_not_restricted_for_non_gpt5_models():
    assert _is_temperature_restricted_model("gpt-4o") is False
    assert _is_temperature_restricted_model("o3-mini") is False
