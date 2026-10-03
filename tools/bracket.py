"""
The three numbers to type, worked out before your pulse is up.

A bracket is an entry with both exits attached: a stop below and a
target above, sent as one order so the moment the entry fills both exits
are live and whichever triggers first cancels the other. The point is
that you never hold a position without a stop, because the stop was part
of entering rather than something you meant to do afterwards.

The stop distance is the one measured number this project produced. Of
the 318 SPCX signals that finished at least 0.5% up an hour later, the
median dipped only 0.27% first, so a 0.75% stop keeps 88% of them. That
came out of 90 days of the stock's own history.

The target is NOT measured. Nothing in this project beat a coin flip,
and no target here is a forecast -- it is the number that makes the
arithmetic of a trade add up, and you should change it when your own
reading of the tape says to.

The stop distance sets the size, never the other way round. Decide what
one trade may cost you, and the share count follows:

    shares = risk budget / (entry - stop price)

READ-ONLY. This prints numbers. It places nothing, cancels nothing, and
never touches a broker.

    python bracket.py --entry 152.40 --risk 50
    python bracket.py --entry 152.40 --risk 50 --stop 1.0 --target 2.5
    python bracket.py --entry 152.40 --shares 100
    python bracket.py --entry 152.40            # a table of risk budgets
    python bracket.py --self-test
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from typing import List, Optional

#: Measured on 90 days of SPCX: winners dipped 0.27% at the median, and
#: this keeps 88% of them. It corrected an earlier 1%, which came from
#: the all-signal median and was dominated by losers.
DEFAULT_STOP_PCT = 0.75

#: Not measured. The bracket the study scored, kept so the arithmetic
#: has a second leg -- not because anything says price gets there.
DEFAULT_TARGET_PCT = 2.00

#: Risk budgets to show when none is given. Enough spread to see how
#: sharply the share count moves.
LADDER = (25.0, 50.0, 100.0, 200.0, 500.0)


@dataclass
class Bracket:
    entry: float
    stop: float
    target: float
    shares: int
    stop_pct: float
    target_pct: float

    @property
    def risk_per_share(self) -> float:
        return self.entry - self.stop

    @property
    def reward_per_share(self) -> float:
        return self.target - self.entry

    @property
    def risk(self) -> float:
        return self.shares * self.risk_per_share

    @property
    def reward(self) -> float:
        return self.shares * self.reward_per_share

    @property
    def cost(self) -> float:
        """What the position ties up, which is not what it risks."""
        return self.shares * self.entry

    @property
    def r_multiple(self) -> Optional[float]:
        if self.risk_per_share <= 0:
            return None
        return self.reward_per_share / self.risk_per_share


def plan(entry: float, risk_budget: Optional[float] = None,
         shares: Optional[int] = None,
         stop_pct: float = DEFAULT_STOP_PCT,
         target_pct: float = DEFAULT_TARGET_PCT) -> Optional[Bracket]:
    """Work the bracket from an entry and either a budget or a size.

    Both prices are rounded away from the entry: the stop down and the
    target up, each to the cent. Rounding the stop the other way would
    quietly tighten it and stop you out earlier than you decided to be,
    which is the one direction this arithmetic must never drift.

    The share count is then worked from the ROUNDED stop, not the
    nominal percentage, so the money at risk is the money you said.
    """
    if entry <= 0 or stop_pct <= 0:
        return None

    stop = math.floor(entry * (1 - stop_pct / 100.0) * 100) / 100
    target = math.ceil(entry * (1 + target_pct / 100.0) * 100) / 100
    per_share = entry - stop
    if per_share <= 0:
        return None

    if shares is None:
        if risk_budget is None or risk_budget <= 0:
            return None
        shares = int(risk_budget // per_share)
    if shares <= 0:
        return None

    return Bracket(entry=entry, stop=stop, target=target, shares=shares,
                   stop_pct=stop_pct, target_pct=target_pct)


def money(value: float) -> str:
    return f"${value:,.2f}"


def report(plans: List[Bracket], buying_power: Optional[float] = None) -> None:
    first = plans[0]
    rule = "=" * 66
    print(f"\n{rule}")
    print(f"  BRACKET  --  entry {money(first.entry)}, "
          f"stop {first.stop_pct:.2f}%, target {first.target_pct:.2f}%")
    print(rule)
    print(f"  Stop      {money(first.stop):>10}   "
          f"{money(first.risk_per_share)} a share below your entry")
    print(f"  Target    {money(first.target):>10}   "
          f"{money(first.reward_per_share)} a share above")
    r = first.r_multiple
    if r:
        print(f"  Ratio     {r:>9.2f}x   you stand to make {r:.2f} for every "
              f"1 you risk")

    print(f"\n  {'Risk':>8}{'Shares':>9}{'Costs':>13}{'If stopped':>13}"
          f"{'If target':>13}")
    for bracket in plans:
        flag = ""
        if buying_power and bracket.cost > buying_power:
            flag = "  over buying power"
        print(f"  {money(bracket.risk):>8}{bracket.shares:>9}"
              f"{money(bracket.cost):>13}{money(-bracket.risk):>13}"
              f"{money(bracket.reward):>13}{flag}")

    print(f"\n  Type the stop as {money(first.stop)} and the target as "
          f"{money(first.target)}.")
    print("  Attach both to the entry so they are live the moment it fills.")
    print("\n  The stop distance is measured -- 0.75% keeps 88% of the")
    print("  signals that went on to work. The target is not: nothing in")
    print("  this project beat a coin flip, and no number here forecasts")
    print("  anything. 'Costs' is what the position ties up, which is not")
    print("  what it risks.")
    print(f"{rule}\n")


def self_test() -> int:
    print("Self-test: checking the arithmetic...\n")
    failures = []

    b = plan(entry=152.40, risk_budget=50.0)
    if b is None:
        failures.append("a plain entry and budget should produce a bracket")
    else:
        # 152.40 * 0.9925 = 151.257 -> floors to 151.25
        if b.stop != 151.25:
            failures.append(f"stop should floor to 151.25, got {b.stop}")
        # 152.40 * 1.02 = 155.448 -> ceils to 155.45
        if b.target != 155.45:
            failures.append(f"target should ceil to 155.45, got {b.target}")
        if abs(b.risk_per_share - 1.15) > 1e-9:
            failures.append(f"risk a share should be 1.15, got "
                            f"{b.risk_per_share}")
        if b.shares != 43:                      # 50 // 1.15
            failures.append(f"43 shares on a $50 budget, got {b.shares}")
        if b.risk > 50.0:
            failures.append(f"risk must not exceed the budget: {b.risk}")

    # Both prices move AWAY from the entry. A stop that rounds toward it
    # would stop you out earlier than you chose to be.
    for entry in (10.004, 99.999, 152.40, 7.77, 1000.005):
        got = plan(entry=entry, risk_budget=100.0)
        if got is None:
            failures.append(f"no bracket at {entry}")
            continue
        if got.stop > entry * (1 - DEFAULT_STOP_PCT / 100.0) + 1e-9:
            failures.append(f"stop rounded toward the entry at {entry}")
        if got.target < entry * (1 + DEFAULT_TARGET_PCT / 100.0) - 1e-9:
            failures.append(f"target rounded toward the entry at {entry}")
        if round(got.stop, 2) != got.stop or round(got.target, 2) != got.target:
            failures.append(f"prices must land on the cent at {entry}")

    # The size follows the stop, so a wider stop buys fewer shares on the
    # same budget -- the whole point of sizing this way round.
    tight = plan(entry=100.0, risk_budget=100.0, stop_pct=0.5)
    wide = plan(entry=100.0, risk_budget=100.0, stop_pct=2.0)
    if not tight or not wide or tight.shares <= wide.shares:
        failures.append("a wider stop should buy fewer shares")

    # Given a size instead of a budget, the risk is whatever it is.
    fixed = plan(entry=100.0, shares=200)
    if not fixed or fixed.shares != 200:
        failures.append("an explicit share count should be honoured")
    if fixed and abs(fixed.risk - 200 * fixed.risk_per_share) > 1e-9:
        failures.append("risk should be size times the per-share stop")

    # Nonsense in, nothing out -- never a bracket that cannot be traded.
    for bad in (dict(entry=0.0, risk_budget=50.0),
                dict(entry=-5.0, risk_budget=50.0),
                dict(entry=100.0, risk_budget=0.0),
                dict(entry=100.0, risk_budget=-50.0),
                dict(entry=100.0, stop_pct=0.0, risk_budget=50.0),
                dict(entry=100.0, shares=0),
                dict(entry=100.0)):
        if plan(**bad) is not None:
            failures.append(f"should refuse: {bad}")

    # A budget smaller than one share's risk buys nothing, and says so
    # rather than rounding up to one share you did not budget for.
    if plan(entry=1000.0, risk_budget=1.0) is not None:
        failures.append("a budget under one share's risk should refuse")

    ratio = plan(entry=100.0, risk_budget=100.0)
    if ratio and ratio.r_multiple and abs(ratio.r_multiple - 2.667) > 0.01:
        failures.append(f"2% target over 0.75% stop is ~2.67R, "
                        f"got {ratio.r_multiple}")

    source = open(__file__, encoding="utf-8").read()
    for forbidden in ("TradingClient", "submit_order", "MarketOrderRequest"):
        if forbidden in source.replace(f'"{forbidden}"', ""):
            failures.append(f"this file must not mention {forbidden}")

    sample = plan(entry=152.40, risk_budget=50.0)
    print(f"  Entry 152.40, risk $50         : {sample.shares} shares, "
          f"stop {money(sample.stop)}, target {money(sample.target)}")
    print("  Rounding                       : both prices away from the entry")
    print("  Sizing                         : follows the stop, not the other "
          "way round")
    print("  Nonsense input                 : refused, never a half-plan")
    print("  Trading client in this file    : none")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nAll checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="The stop, the target and the share count, worked out.")
    parser.add_argument("--entry", type=float, help="Your entry price")
    parser.add_argument("--risk", type=float, default=None,
                        help="What this trade may cost you, in dollars")
    parser.add_argument("--shares", type=int, default=None,
                        help="A fixed size instead of a risk budget")
    parser.add_argument("--stop", type=float, default=DEFAULT_STOP_PCT,
                        metavar="PCT",
                        help=f"Stop, %% below entry (default "
                             f"{DEFAULT_STOP_PCT}, the measured number)")
    parser.add_argument("--target", type=float, default=DEFAULT_TARGET_PCT,
                        metavar="PCT",
                        help=f"Target, %% above entry (default "
                             f"{DEFAULT_TARGET_PCT}; not measured)")
    parser.add_argument("--buying-power", type=float, default=None,
                        metavar="USD",
                        help="Flag sizes that cost more than this")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.entry:
        parser.error("--entry is the price you expect to get in at")

    if args.shares is not None:
        plans = [plan(args.entry, shares=args.shares, stop_pct=args.stop,
                      target_pct=args.target)]
    elif args.risk is not None:
        plans = [plan(args.entry, risk_budget=args.risk, stop_pct=args.stop,
                      target_pct=args.target)]
    else:
        plans = [plan(args.entry, risk_budget=r, stop_pct=args.stop,
                      target_pct=args.target) for r in LADDER]

    plans = [p for p in plans if p is not None]
    if not plans:
        print("\nNo tradeable bracket from those numbers. A budget smaller "
              "than one share's\nrisk buys nothing, and rounding it up to a "
              "share you did not budget for would\nbe the tool deciding "
              "something you did not.\n")
        return 1

    report(plans, args.buying_power)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
