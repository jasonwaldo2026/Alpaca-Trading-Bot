"""
Alert scenarios — what to look for, and what to say when it happens.

A scenario is a `core.rules.Rule` (the detection) plus an `AlertTemplate`
(the message). Both live in the same JSON file, so adding a new thing you
want to be told about is adding a file — no code change:

    {
      "name": "vwap hold",
      "conditions": [
        {"field": "close", "op": ">", "field2": "vwap", "for_bars": 3}
      ],
      "alert": {
        "title": "Strong above VWAP",
        "message": "{symbol} stock is strong and trading above VWAP"
      }
    }

Message placeholders are `{name}` and are filled from the match: `symbol`,
`price`, `limit_price`, and every indicator value the scanner reports
(`vwap`, `rsi`, `ema_9`, `macd`, …). An unknown placeholder is caught by
`validate()` rather than at 4am when the alert should have fired.
"""

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set

#: Opens the Robinhood app to the stock's page on iOS (universal link), or
#: the website otherwise.
#:
#: The locale prefix (/us/en/) is Robinhood's canonical path and is used
#: deliberately: iOS matches a tapped URL against the app's associated-domain
#: paths *before* following redirects, so a URL that only works by
#: redirecting can open Safari instead of the app.
#:
#: The stock page is as deep as Robinhood links go. There is no published
#: deep link to a buy or order screen, and Robinhood's third-party
#: connections policy is explicit that outside apps cannot take action in the
#: app. From here it is one tap: Trade -> Buy -> switch order type to Limit.
#: The suggested limit price is put in the message text instead.
#:
#: This is a template, so alternatives can be tried without code changes —
#: see ALTERNATIVE_LINK_TEMPLATES.
DEFAULT_LINK_TEMPLATE = "https://robinhood.com/us/en/stocks/{symbol}/"

#: Undocumented alternatives worth testing on a real device. None of these is
#: published by Robinhood, so any of them may do nothing, open Safari, or
#: change without notice — which is exactly why the link is configurable
#: rather than hardcoded. Tap one and see what the app does.
ALTERNATIVE_LINK_TEMPLATES = {
    "stock page (default)": DEFAULT_LINK_TEMPLATE,
    "short form": "https://robinhood.com/stocks/{symbol}",
    "custom scheme": "robinhood://stocks/{symbol}",
    "custom scheme, instrument": "robinhood://instrument/{symbol}",
}

#: Placeholders always available, regardless of which indicators a rule uses.
BASE_PLACEHOLDERS = ("symbol", "price", "limit_price", "rule", "session", "time")

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# Pushover's documented limits.
MAX_TITLE = 250
MAX_MESSAGE = 1024
MAX_URL = 512
MAX_URL_TITLE = 100


class AlertError(ValueError):
    """Raised when an alert template cannot produce a message."""


def placeholders_in(text: str) -> Set[str]:
    return set(_PLACEHOLDER.findall(text or ""))


@dataclass
class AlertTemplate:
    """What to say when a rule matches."""

    #: Notification title. Short — it is the bold line on the lock screen.
    title: str = "Scanner match"

    #: Body. `{symbol}` is the stock; see BASE_PLACEHOLDERS and any indicator
    #: column the rule's params produce.
    message: str = "{symbol} matched {rule}"

    #: Pushover priority: -2 silent, -1 quiet, 0 normal, 1 high (bypasses
    #: quiet hours), 2 emergency (requires acknowledgement — not used here).
    priority: int = 0

    #: Where tapping the notification goes.
    link_template: str = DEFAULT_LINK_TEMPLATE
    link_title: str = "Open {symbol} in Robinhood"

    #: Suggested limit price as an offset above the trigger price, so a buy
    #: is marketable rather than sitting behind the spread. 0.001 = +0.1%.
    limit_offset_pct: float = 0.001

    #: Pushover sound name, or None for the account default.
    sound: Optional[str] = None

    def validate(self, known_fields: Optional[Set[str]] = None) -> None:
        """
        Check the template can actually be rendered.

        `known_fields` is the set of indicator columns the rule produces;
        pass it so a typo like `{vwsp}` is caught when the scenario is saved
        rather than when it should have alerted.
        """
        if not self.title.strip():
            raise AlertError("Alert title must not be empty.")
        if not self.message.strip():
            raise AlertError("Alert message must not be empty.")
        if self.priority not in (-2, -1, 0, 1):
            raise AlertError(
                f"priority must be -2, -1, 0 or 1; got {self.priority}. "
                f"(2 is emergency and needs acknowledgement — not supported.)"
            )
        if len(self.title) > MAX_TITLE:
            raise AlertError(f"Title exceeds {MAX_TITLE} characters.")

        allowed = set(BASE_PLACEHOLDERS) | set(known_fields or ())
        used = placeholders_in(self.title) | placeholders_in(self.message)
        used |= placeholders_in(self.link_template) | placeholders_in(self.link_title)
        unknown = used - allowed
        if unknown:
            raise AlertError(
                f"Unknown placeholder(s) {sorted(unknown)}. Available: "
                f"{', '.join(sorted(allowed))}"
            )

    def limit_price(self, price: float) -> float:
        """Suggested marketable limit, rounded to a cent."""
        return round(price * (1 + self.limit_offset_pct), 2)

    def render(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Produce the Pushover payload fields for one match.

        Numbers are formatted to a sane number of decimals here rather than
        in the template, so a message stays readable without every scenario
        repeating format specifiers.
        """
        values = {
            key: (round(value, 4) if isinstance(value, float) else value)
            for key, value in context.items()
        }

        try:
            title = self.title.format(**values)
            message = self.message.format(**values)
            url = self.link_template.format(**values)
            url_title = self.link_title.format(**values)
        except KeyError as exc:
            raise AlertError(
                f"Message references {exc} but the match does not provide it. "
                f"Available: {', '.join(sorted(values))}"
            ) from exc

        return {
            "title": title[:MAX_TITLE],
            "message": message[:MAX_MESSAGE],
            "url": url[:MAX_URL],
            "url_title": url_title[:MAX_URL_TITLE],
            "priority": self.priority,
            **({"sound": self.sound} if self.sound else {}),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "message": self.message,
            "priority": self.priority,
            "link_template": self.link_template,
            "link_title": self.link_title,
            "limit_offset_pct": self.limit_offset_pct,
            "sound": self.sound,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AlertTemplate":
        try:
            return cls(**{k: v for k, v in data.items() if v is not None})
        except TypeError as exc:
            raise AlertError(f"Malformed alert block: {exc}") from exc


def build_context(
    symbol: str,
    price: float,
    rule_name: str,
    template: AlertTemplate,
    values: Optional[Dict[str, float]] = None,
    session: str = "",
    time_label: str = "",
) -> Dict[str, Any]:
    """Assemble the placeholder values for one match."""
    context: Dict[str, Any] = {
        "symbol": symbol,
        "price": round(price, 4),
        "limit_price": template.limit_price(price),
        "rule": rule_name,
        "session": session,
        "time": time_label,
    }
    context.update(values or {})
    return context
