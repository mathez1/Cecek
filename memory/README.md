# memory

The bot's diary. Every run reads these files, and the workflow commits them
back afterwards, which is how an hourly job with no database remembers what it
already said.

- `posts.jsonl` - one JSON object per post, append only. The last 40 are shown
  to the model each run so it does not repeat itself.
- `state.json` - counters: posts this month, failure streak, last run.

You can edit both by hand. Deleting a line from `posts.jsonl` makes the bot
willing to say that thing again; emptying the file gives it amnesia.
