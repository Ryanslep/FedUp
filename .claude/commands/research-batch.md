---
description: Run the restaurant-researcher agent over places in the FedUp database
argument-hint: [limit]
---

Research places in the FedUp database using the `restaurant-researcher` subagent — one agent per place, run in parallel batches, writing straight to the `places` table.

1. Run `python get_research_batch.py --limit $ARGUMENTS` (drop `--limit` entirely if no argument was given, to cover every place) to get the JSON list of places to research.
2. For each place in the list, spawn a `restaurant-researcher` subagent (Agent tool, `subagent_type: restaurant-researcher`) with its `place_id`, `name`, `lat`/`lon`, and `website` if known, plus "Charlotte, NC" as the city context. Send a handful of agent calls per message rather than the whole batch at once.
3. After each round completes, check each agent's final line for `RESEARCH_COMPLETE` or `RESEARCH_FAILED` and tally results.
4. Report a short summary: how many places were researched, how many failed and why.
