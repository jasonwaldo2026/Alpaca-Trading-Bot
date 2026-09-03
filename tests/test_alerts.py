"""
Alert scenarios: message rendering, and deciding what is actually new.

The dedupe logic matters more than the delivery. A setup that stays true for
an hour must be one alert, not twelve — otherwise a scan every five minutes
turns a good signal into noise you learn to ignore.
"""

import json

import pytest

from core.alerts import (
    DEFAULT_LINK_TEMPLATE,
    AlertError,
    AlertTemplate,
    build_context,
)
from core.indicators import IndicatorParams
from core.rules import Condition, Rule
from scanner.alerts import AlertNotifier, AlertState, PushoverCredentials
from scanner.engine import Match


VWAP_ALERT = AlertTemplate(
    title="Strong above VWAP",
    message="{symbol} stock is strong and trading above VWAP",
)


def _rule(name="vwap hold", alert=VWAP_ALERT) -> Rule:
    return Rule(
        name=name,
        conditions=[Condition("close", ">", field2="vwap", for_bars=3)],
        params=IndicatorParams(bar_minutes=5),
        alert=alert,
    )


def _match(symbol="AAPL", rule="vwap hold", price=184.21) -> Match:
    return Match(
        symbol=symbol, rule_name=rule, asset_class="stock", price=price,
        values={"vwap": 183.64, "rsi": 61.2, "atr": 1.43},
    )


class FakeTransport:
    def __init__(self, succeed=True):
        self.sent = []
        self.succeed = succeed

    def send(self, payload):
        self.sent.append(payload)
        return self.succeed


# ── Message rendering ────────────────────────────────────────────────────────

def test_symbol_is_substituted_into_the_message():
    context = build_context("NVDA", 184.21, "vwap hold", VWAP_ALERT)
    payload = VWAP_ALERT.render(context)
    assert payload["message"] == "NVDA stock is strong and trading above VWAP"
    assert payload["title"] == "Strong above VWAP"


def test_indicator_values_are_available_as_placeholders():
    template = AlertTemplate(message="{symbol} at ${price}, VWAP ${vwap}, RSI {rsi}")
    context = build_context("AAPL", 184.21, "r", template,
                            {"vwap": 183.64, "rsi": 61.2})
    assert template.render(context)["message"] == (
        "AAPL at $184.21, VWAP $183.64, RSI 61.2"
    )


def test_suggested_limit_price_sits_above_the_trigger():
    """A marketable limit — behind the spread it simply would not fill."""
    template = AlertTemplate(limit_offset_pct=0.001)
    assert template.limit_price(100.0) == 100.10
    context = build_context("AAPL", 100.0, "r", template)
    assert context["limit_price"] == 100.10


def test_the_link_opens_the_robinhood_app_for_that_symbol():
    """The custom scheme was verified on a real iPhone: it opens the app
    directly, already signed in."""
    context = build_context("TSLA", 250.0, "r", VWAP_ALERT)
    payload = VWAP_ALERT.render(context)
    assert payload["url"] == "robinhood://instrument/TSLA"
    assert "TSLA" in payload["url_title"]


def test_a_web_fallback_form_is_available():
    """A custom scheme does nothing without the app; the web link at least
    reaches the site."""
    from core.alerts import WEB_LINK_TEMPLATE
    template = AlertTemplate(link_template=WEB_LINK_TEMPLATE)
    payload = template.render(build_context("TSLA", 250.0, "r", template))
    assert payload["url"] == "https://robinhood.com/us/en/stocks/TSLA/"


def test_alternative_link_forms_are_offered():
    """All of these are undocumented, so they must be swappable by config
    rather than assumed permanent."""
    from core.alerts import ALTERNATIVE_LINK_TEMPLATES, WEB_LINK_TEMPLATE
    assert DEFAULT_LINK_TEMPLATE in ALTERNATIVE_LINK_TEMPLATES.values()
    assert WEB_LINK_TEMPLATE in ALTERNATIVE_LINK_TEMPLATES.values()
    for template in ALTERNATIVE_LINK_TEMPLATES.values():
        assert "{symbol}" in template


def test_the_link_template_is_configurable():
    """Robinhood publishes no pre-filled-order deep link, so if a better one
    is found it must be a config change, not a code change."""
    template = AlertTemplate(link_template="myapp://buy?sym={symbol}&limit={limit_price}")
    payload = template.render(build_context("AAPL", 100.0, "r", template))
    assert payload["url"] == "myapp://buy?sym=AAPL&limit=100.1"


def test_a_typo_in_a_placeholder_is_caught_at_save_time():
    """Not at 4am when the alert should have fired."""
    template = AlertTemplate(message="{symbol} broke {vwsp}")
    with pytest.raises(AlertError, match="Unknown placeholder"):
        template.validate({"vwap"})


def test_rule_validation_checks_its_alert_against_its_own_fields():
    rule = _rule(alert=AlertTemplate(message="{symbol} {macd_hist}"))
    rule.validate()          # macd_hist is a real column for these params

    bad = _rule(alert=AlertTemplate(message="{symbol} {not_a_column}"))
    with pytest.raises(AlertError, match="Unknown placeholder"):
        bad.validate()


def test_empty_message_is_rejected():
    with pytest.raises(AlertError, match="message must not be empty"):
        AlertTemplate(message="  ").validate()


def test_emergency_priority_is_rejected():
    """Priority 2 requires acknowledgement and retries; not supported here."""
    with pytest.raises(AlertError, match="priority must be"):
        AlertTemplate(priority=2).validate()


def test_message_is_truncated_to_the_pushover_limit():
    template = AlertTemplate(message="x" * 2000)
    assert len(template.render(build_context("A", 1.0, "r", template))["message"]) == 1024


def test_alert_survives_a_rule_json_round_trip():
    rule = _rule()
    restored = Rule.from_json(rule.to_json())
    assert restored.alert.message == VWAP_ALERT.message
    assert restored.alert.link_template == DEFAULT_LINK_TEMPLATE


def test_a_rule_without_an_alert_stays_a_clean_file():
    rule = Rule(name="detect only",
                conditions=[Condition("close", ">", value=1)])
    assert "alert" not in json.loads(rule.to_json())
    assert Rule.from_json(rule.to_json()).alert is None


# ── What counts as new ───────────────────────────────────────────────────────

def test_a_standing_match_alerts_once(tmp_path):
    transport = FakeTransport()
    notifier = AlertNotifier(transport, AlertState(tmp_path / "state.json"))
    rules = {"vwap hold": _rule()}
    matches = [_match()]

    assert notifier.notify(matches, rules) == 1
    assert notifier.notify(matches, rules) == 0, "still true is not news"
    assert notifier.notify(matches, rules) == 0
    assert len(transport.sent) == 1


def test_a_setup_that_returns_alerts_again(tmp_path):
    """Gone and back is a new signal, not a duplicate."""
    transport = FakeTransport()
    notifier = AlertNotifier(transport, AlertState(tmp_path / "state.json"))
    rules = {"vwap hold": _rule()}

    assert notifier.notify([_match()], rules) == 1
    assert notifier.notify([], rules) == 0            # condition lapses
    assert notifier.notify([_match()], rules) == 1    # and comes back


def test_a_new_symbol_alerts_while_an_old_one_stays_quiet(tmp_path):
    transport = FakeTransport()
    notifier = AlertNotifier(transport, AlertState(tmp_path / "state.json"))
    rules = {"vwap hold": _rule()}

    notifier.notify([_match("AAPL")], rules)
    transport.sent.clear()

    notifier.notify([_match("AAPL"), _match("NVDA")], rules)
    assert len(transport.sent) == 1
    assert "NVDA" in transport.sent[0]["message"]


def test_the_same_symbol_on_two_rules_alerts_twice(tmp_path):
    transport = FakeTransport()
    notifier = AlertNotifier(transport, AlertState(tmp_path / "state.json"))
    rules = {"vwap hold": _rule(), "other": _rule("other")}
    sent = notifier.notify(
        [_match(rule="vwap hold"), _match(rule="other")], rules
    )
    assert sent == 2


def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "state.json"
    rules = {"vwap hold": _rule()}

    first = AlertNotifier(FakeTransport(), AlertState(path))
    assert first.notify([_match()], rules) == 1

    # A fresh process reading the same file must not re-announce.
    second = AlertNotifier(FakeTransport(), AlertState(path))
    assert second.notify([_match()], rules) == 0


def test_a_corrupt_state_file_is_discarded_not_fatal(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    state = AlertState(path)
    assert state.seen == set()


# ── Delivery behavior ────────────────────────────────────────────────────────

def test_missing_credentials_disable_alerting_without_crashing(monkeypatch, tmp_path):
    monkeypatch.delenv("PUSHOVER_TOKEN", raising=False)
    monkeypatch.delenv("PUSHOVER_USER", raising=False)
    notifier = AlertNotifier.from_env(tmp_path / "state.json")
    assert not notifier.enabled
    assert notifier.notify([_match()], {"vwap hold": _rule()}) == 0


def test_state_is_recorded_even_when_alerting_is_off(tmp_path):
    """Otherwise turning alerts on later dumps every standing setup at once."""
    path = tmp_path / "state.json"
    off = AlertNotifier(transport=None, state=AlertState(path))
    off.notify([_match()], {"vwap hold": _rule()})

    on = AlertNotifier(FakeTransport(), AlertState(path))
    assert on.notify([_match()], {"vwap hold": _rule()}) == 0


def test_a_delivery_failure_does_not_raise(tmp_path):
    transport = FakeTransport(succeed=False)
    notifier = AlertNotifier(transport, AlertState(tmp_path / "state.json"))
    assert notifier.notify([_match()], {"vwap hold": _rule()}) == 0
    assert len(transport.sent) == 1, "it was attempted"


def test_a_rule_without_an_alert_is_tracked_but_not_sent(tmp_path):
    transport = FakeTransport()
    notifier = AlertNotifier(transport, AlertState(tmp_path / "state.json"))
    notifier.notify([_match()], {"vwap hold": _rule(alert=None)})
    assert transport.sent == []


def test_a_flood_is_capped_and_summarised(tmp_path):
    transport = FakeTransport()
    notifier = AlertNotifier(transport, AlertState(tmp_path / "state.json"), max_per_run=3)
    matches = [_match(f"SYM{i}") for i in range(10)]

    notifier.notify(matches, {"vwap hold": _rule()})

    assert len(transport.sent) == 4, "3 alerts plus one summary"
    assert "7 more matches" in transport.sent[-1]["title"]
    assert transport.sent[-1]["priority"] == -1


def test_credentials_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PUSHOVER_TOKEN", "t")
    monkeypatch.setenv("PUSHOVER_USER", "u")
    creds = PushoverCredentials.from_env()
    assert creds.is_complete()
    assert not PushoverCredentials("", "u").is_complete()


# ── Momentum criteria ────────────────────────────────────────────────────────

def test_roc_expresses_ten_percent_in_ten_minutes():
    """At 5-minute bars a 2-bar window is 10 minutes, so `roc >= 10` is
    literally 'up 10% in the last 10 minutes'."""
    import pandas as pd
    from core.indicators import roc

    params = IndicatorParams(roc_period=2, bar_minutes=5)
    assert params.duration_minutes("roc_period") == 10

    closes = pd.Series([100.0, 104.0, 110.0])
    assert roc(closes, 2).iloc[-1] == pytest.approx(10.0)


def test_roc_is_negative_on_a_fall():
    import pandas as pd
    from core.indicators import roc
    assert roc(pd.Series([100.0, 95.0, 90.0]), 2).iloc[-1] == pytest.approx(-10.0)


def test_roc_period_must_be_positive():
    import pandas as pd
    from core.indicators import roc
    with pytest.raises(ValueError, match="roc period must be >= 1"):
        roc(pd.Series([1.0, 2.0]), 0)


def test_relative_volume_is_a_multiple_not_a_flag():
    """A ratio lets a rule ask for 3x rather than only 'above average'."""
    import pandas as pd
    from core.indicators import relative_volume
    result = relative_volume(pd.Series([3000.0, 500.0]), pd.Series([1000.0, 1000.0]))
    assert result.tolist() == [3.0, 0.5]


def test_zero_baseline_volume_does_not_divide_by_zero():
    import pandas as pd
    from core.indicators import relative_volume
    assert relative_volume(pd.Series([100.0]), pd.Series([0.0])).isna().all()


def test_the_momentum_runner_scenario_loads_and_validates():
    from core.rules import load_rules
    rule = load_rules(["rules/momentum-runner.json"])[0]
    rule.validate()
    assert rule.params.roc_period == 2
    assert rule.params.bar_minutes == 5
    assert "roc >= 10" in rule.describe()
    assert "rvol >= 3" in rule.describe()


def test_the_momentum_alert_says_what_it_cannot_check():
    """An alert must not be mistaken for a full screen when two of the four
    criteria are not evaluated."""
    from core.rules import load_rules
    rule = load_rules(["rules/momentum-runner.json"])[0]
    payload = rule.alert.render(build_context(
        "ABCD", 5.20, rule.name, rule.alert,
        {"roc": 12.4, "rvol": 4.1, "vwap": 5.02},
    ))
    assert "ABCD is running" in payload["message"]
    assert "float" in payload["message"].lower()
    assert "12 months" in payload["message"]
