# Specs

One file per feature. Write the spec before asking Claude to build the
feature, and keep it after — a chat message scrolls away, a spec file gets
re-read on every future session and can be diffed against the PR that
implements it.

This replaces the "Ready-made Jules issues" block that used to live in the
top-level README. Same idea, better home: version-controlled, reviewable,
and close to the code it describes.

## Layout

```
docs/specs/
  core/      # changes to the shared library — land these first
  scanner/
  studio/
  bot/
```

## Template

```markdown
# <Feature name>

**Status:** proposed | in progress | done
**Touches:** core/indicators.py, scanner/engine.py

## Why
One paragraph. What is impossible or annoying today.

## What
The behavior, concretely. Inputs, outputs, edge cases.

## Done when
- [ ] Checkable statements, not vibes
- [ ] Tests named, or "no new tests, because <reason>"

## Explicitly out of scope
The adjacent thing someone will be tempted to also build.
```

## Working agreement

- A spec that touches `core/` gets its own PR, merged before the app PRs
  that depend on it.
- "Done when" is the acceptance criteria. If it can't be checked, rewrite it.
- If implementation reveals the spec is wrong, update the spec in the same
  PR — do not leave the two disagreeing.
