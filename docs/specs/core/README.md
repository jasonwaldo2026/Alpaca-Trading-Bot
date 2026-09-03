# Core specs

Changes to the shared library. These land **first**, in their own PR, before
any app PR that depends on them — a core change that ships alongside its
consumer is a change nobody can review in isolation.

Every core change must answer: *which apps' numbers does this move?* If the
answer is "the bot's signals and the dashboard's chart", that belongs in the
PR description, not discovered later.

`tests/test_indicators.py::test_matches_legacy_implementations` deliberately
pins indicator output. Changing it is allowed but never incidental — it means
every app's numbers change.
