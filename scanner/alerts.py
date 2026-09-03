"""
Phone alerts for scanner matches.

Two jobs, and the second is the harder one:

1. **Deliver** a message to Pushover.
2. **Decide what is new.** A setup that stays true for an hour is one alert,
   not twelve. Without that, a scan every five minutes turns a good signal
   into noise you learn to ignore — which defeats the purpose.

State lives in a small JSON file so a restart does not re-announce
everything that is currently true.
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from core.alerts import AlertError, build_context
from core.rules import Rule
from scanner.engine import Match

log = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
DEFAULT_STATE_PATH = Path(".scanner-alerts.json")

#: Pushover allows 2 concurrent requests and meters a monthly quota, so a
#: scan that suddenly matches hundreds of symbols must not fire hundreds of
#: notifications. Beyond this, the rest are summarised in one message.
MAX_ALERTS_PER_RUN = 8


@dataclass(frozen=True)
class PushoverCredentials:
    token: str
    user: str

    @classmethod
    def from_env(cls) -> "PushoverCredentials":
        return cls(
            token=os.getenv("PUSHOVER_TOKEN", ""),
            user=os.getenv("PUSHOVER_USER", ""),
        )

    def is_complete(self) -> bool:
        return bool(self.token and self.user)


class PushoverTransport:
    """Posts to Pushover. Isolated so tests can substitute a fake."""

    def __init__(self, credentials: PushoverCredentials, timeout: float = 10.0):
        self.credentials = credentials
        self.timeout = timeout

    def send(self, payload: Dict) -> bool:
        body = urllib.parse.urlencode({
            "token": self.credentials.token,
            "user": self.credentials.user,
            **payload,
        }).encode()
        request = urllib.request.Request(PUSHOVER_URL, data=body)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            # A delivery failure must never interrupt the scan loop.
            log.error("Pushover delivery failed: %s", exc)
            return False


class AlertState:
    """
    Which (rule, symbol) pairs were already alerted.

    Persisted so a restart mid-session does not re-announce every setup that
    happens to be true at that moment.
    """

    def __init__(self, path: Path = DEFAULT_STATE_PATH):
        self.path = Path(path)
        self.seen: Set[Tuple[str, str]] = set()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            self.seen = {tuple(pair) for pair in raw.get("seen", []) if len(pair) == 2}
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            # A corrupt state file must not stop alerting; the cost of
            # discarding it is one round of duplicate alerts.
            log.warning("Discarding unreadable alert state at %s: %s", self.path, exc)
            self.seen = set()

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps({
                "updated": datetime.now(timezone.utc).isoformat(),
                "seen": sorted(list(pair) for pair in self.seen),
            }, indent=2))
        except OSError as exc:
            log.warning("Could not persist alert state to %s: %s", self.path, exc)

    def new_matches(self, matches: Iterable[Match]) -> List[Match]:
        """Matches not present on the previous run."""
        return [m for m in matches if (m.rule_name, m.symbol) not in self.seen]

    def update(self, matches: Iterable[Match]) -> None:
        """
        Replace the seen set with what is true *now*.

        Replacing rather than accumulating is what lets a setup alert again
        after it stops being true and comes back — which is a new signal, not
        a duplicate.
        """
        self.seen = {(m.rule_name, m.symbol) for m in matches}


class AlertNotifier:
    """Turns new scanner matches into phone notifications."""

    def __init__(
        self,
        transport: Optional[PushoverTransport] = None,
        state: Optional[AlertState] = None,
        max_per_run: int = MAX_ALERTS_PER_RUN,
    ):
        self.transport = transport
        self.state = state or AlertState()
        self.max_per_run = max_per_run

    @property
    def enabled(self) -> bool:
        return self.transport is not None

    @classmethod
    def from_env(cls, state_path: Path = DEFAULT_STATE_PATH) -> "AlertNotifier":
        credentials = PushoverCredentials.from_env()
        if not credentials.is_complete():
            log.info(
                "Pushover credentials not set (PUSHOVER_TOKEN / PUSHOVER_USER) — "
                "alerting is off. Matches will still be printed."
            )
            return cls(transport=None, state=AlertState(state_path))
        return cls(PushoverTransport(credentials), AlertState(state_path))

    def notify(
        self,
        matches: List[Match],
        rules_by_name: Dict[str, Rule],
        session: str = "",
    ) -> int:
        """
        Send one notification per new match, and record what is now standing.

        Returns the number of notifications sent. Matches whose rule has no
        `alert` block are tracked but not sent, so a scenario can be detected
        while it is still being tuned.
        """
        new = self.state.new_matches(matches)

        # Deliver first, then record. A match whose notification failed to
        # send is left out of the seen set so it is tried again next scan —
        # recording it first would silently lose the alert until the setup
        # lapsed and came back.
        undelivered: Set[Tuple[str, str]] = set()
        sent = 0

        if self.enabled and new:
            alertable = [m for m in new if rules_by_name.get(m.rule_name, None)
                         and rules_by_name[m.rule_name].alert is not None]
            now = datetime.now(timezone.utc).strftime("%H:%M UTC")

            for match in alertable[: self.max_per_run]:
                rule = rules_by_name[match.rule_name]
                try:
                    context = build_context(
                        symbol=match.symbol, price=match.price,
                        rule_name=match.rule_name, template=rule.alert,
                        values=match.values, session=session, time_label=now,
                    )
                    payload = rule.alert.render(context)
                except AlertError as exc:
                    # A template that cannot render will not render next
                    # time either; retrying it would only repeat the error.
                    log.error("Could not render alert for %s: %s", match.symbol, exc)
                    continue

                if self.transport.send(payload):
                    sent += 1
                else:
                    undelivered.add((match.rule_name, match.symbol))

            overflow = alertable[self.max_per_run:]
            if overflow:
                symbols = ", ".join(m.symbol for m in overflow)
                if self.transport.send({
                    "title": f"{len(overflow)} more matches",
                    "message": f"Also matched: {symbols}",
                    "priority": -1,
                }):
                    sent += 1
                else:
                    undelivered.update((m.rule_name, m.symbol) for m in overflow)

        # Record current truth — even when delivery is off, so enabling
        # alerts later does not dump every standing setup at once — minus
        # whatever failed to send.
        self.state.update(
            m for m in matches if (m.rule_name, m.symbol) not in undelivered
        )
        self.state.save()
        return sent
