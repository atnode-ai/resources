# README — ABOUT ME folder

Context files Claude reads to work with `yourName`. Loaded automatically at the start of every session per the global preference setting (Settings > Profile).

## Files

**about-me.md**
Loaded in every project (Personal). Identity, professional background, interests. Not tied to any single project.

**anti-ai-writing-style.md**
Loaded wherever written output is being produced. Voice and banned-vocabulary rules for anything drafted or published.

**my-company.md**
`yourCompany` context only. Goals, focus, editorial standards, bets, channels. Load only inside the `yourCompany` project — not global, not relevant to unrelated work.

**session-preferences.md**
Operational rules for Claude that aren't identity or company context.

## Scoping logic

| File                     | Scope                      |
| ------------------------ | -------------------------- |
| about-me.md              | Global — every project     |
| anti-ai-writing-style.md | Global — any writing task  |
| my-company.md            | Your Company project only  |
| session-preferences.md   | Global — operational rules |

## Notes

