# Cecek

An X account that runs itself. Every hour a GitHub Action wakes up, lets Claude
read the news and search the web, and lets it post whatever it found worth
saying. Nobody approves anything. There is no server to run and nothing to
click.

You shape it by editing one file, [`prompts/persona.md`](prompts/persona.md).
Everything else is plumbing.

---

## Read this before you switch it on

**X's API is not free any more.** The free tier closed to new developers on
6 February 2026, and its write allowance was 17 posts per 24 hours anyway, so
hourly posting (24 a day) never fitted inside it. New accounts get prepaid
pay-per-use. Current rates:

| What | Price |
|---|---|
| A post | $0.015 |
| A post **containing a link** | $0.20 |

Check the current numbers at
[docs.x.com pricing](https://docs.x.com/x-api/getting-started/pricing) before
you budget: they have already changed twice in 2026.

That 13x jump for links is why this bot **strips links by default**. It writes
the interesting thing rather than pointing at it. Set `ALLOW_LINKS=true` if you
want sources in the posts and accept roughly $144/month instead of $11.

### What an hour-by-hour account costs to run

Hourly is about 720 posts a month. The X side is the cheap half at around $11.
Claude is the rest, and how much depends almost entirely on how far the model
decides to go when it searches, which is not knowable in advance.

**So the bot meters itself.** Every run reports its real token counts and price
in the Actions summary, and `memory/state.json` keeps a running monthly total:

> **Cost:** $0.287 (2 calls, 28,400 in, 3,900 out, 5 searches)
> At this rate: $6.89/day, $207/month at 24 runs a day.
> Model spend so far this month: $14.32.

Run it in dry-run mode a few times and you will know your number within a day.
Until then, the plausible range on the defaults, worked from the prompt sizes
this bot actually builds:

| Per run | Claude, monthly at hourly | When |
|---|---|---|
| ~$0.18 | ~$130 | short exploration, compact results |
| **~$0.30** | **~$215** | **the typical case** |
| ~$0.44 | ~$315 | heavy searching |
| ~$0.65 | ~$470 | long exploration and two compose retries |

On `claude-sonnet-5` every row is roughly halved, and `EFFORT=medium` takes
another third off the output side.

Cadence multiplies all of it. Every two hours halves the bill, every three
hours thirds it, and an account that posts eight good things a day reads better
than one that posts twenty-four mediocre ones.

GitHub Actions adds nothing. This repository is public, and public repos get
unmetered standard runners, so 24 runs a day costs nothing and consumes no
quota. (If you ever make it private, the Free plan's 2,000 minutes a month
against 24 runs a day at 2 minutes each leaves very little headroom.)

Being public also means the persona, the feed list and the whole post history
in `memory/posts.jsonl` are readable by anyone. The five secrets are not: they
live in Actions secrets, are masked in logs, and are never given to workflows
triggered from a fork.

The levers are all one-line changes and they are listed in
[Configuration](#configuration). Start with a dry run, look at what it writes,
then decide what it is worth to you.

---

> Prices in this file and in `bot/cost.py` are a snapshot. They are the first
> thing here to go stale, so they live in one table in that module and can be
> corrected without touching anything else.

## Setup

### 1. Get X API credentials

1. Go to [developer.x.com](https://developer.x.com) and create a Project, then
   an App inside it. An App that is not inside a Project cannot use API v2 at
   all.
2. In the App, open **User authentication settings** and set
   **App permissions** to **Read and write**. You also have to fill in a
   Callback URI and a Website URL before it will save; if you have nothing
   real, `https://example.com` is fine for both.
3. Go to **Keys and tokens** and generate all four values:
   *API Key*, *API Key Secret*, *Access Token*, *Access Token Secret*.

> **The one mistake everybody makes.** An access token carries whatever
> permission the app had *at the moment the token was minted*. If you generated
> tokens before switching the app to Read and write, they will keep returning
> 403 forever and no error message will tell you why. Regenerate the Access
> Token and Secret **after** changing the permission.

4. Load credit at [console.x.com](https://console.x.com). A drained balance
   shows up as a 403, not as an obvious billing error.

### 2. Get an Anthropic API key

From the [Claude Console](https://console.anthropic.com). Add credit there too.

### 3. Put them in this repository

**Settings → Secrets and variables → Actions → New repository secret**, five
times:

| Secret | From |
|---|---|
| `ANTHROPIC_API_KEY` | Claude Console |
| `X_API_KEY` | X portal, "API Key" |
| `X_API_SECRET` | X portal, "API Key Secret" |
| `X_ACCESS_TOKEN` | X portal, "Access Token" |
| `X_ACCESS_TOKEN_SECRET` | X portal, "Access Token Secret" |

A misspelled secret name does not raise an error, it silently becomes an empty
string, so copy the names exactly. The bot checks them at startup and tells you
which one is missing.

### 4. Try it without posting

Go to **Actions → post → Run workflow**, leave **dry run** ticked, and run it.
It will think, write a post, and show you the result in the run summary without
publishing anything. Do this a few times and read what it produces. If you do
not like the voice, edit `prompts/persona.md` and run it again.

### 5. Check the feeds resolve

Run **Actions → feeds → Run workflow** once. It reports any of the 43 feeds
that are dead, since publishers move them without warning. Delete what it
flags from `prompts/feeds.txt`. The bot skips dead feeds silently, so this is
about keeping its view of the world wide, not about preventing errors. It
re-runs itself monthly.

### 6. Let it loose

Two things have to be true before the hourly schedule fires:

1. **This code must be on your default branch.** GitHub only runs scheduled
   workflows from the default branch. A `schedule:` block on a feature branch
   fires nothing, reports no error, and is the single most common reason people
   think their cron is broken. So merge this branch into `main` first.
2. **The repository variable `DRY_RUN` must not be `true`.** It defaults to
   posting for real, so unless you set that variable there is nothing to do.

The first post lands at 23 minutes past the next hour. GitHub schedules run
late during load spikes and occasionally get dropped altogether, so treat "one
post an hour" as roughly, not exactly.

---

## How it works

```
 cron, every hour at :23
        │
        ├─ read a random dozen of the 43 feeds in prompts/feeds.txt
        │  (sends back ETags, so most fetches are empty 304s)
        │
        ├─ Claude, call one: explore
        │    web search and web fetch are on, output is free-form
        │    it wanders wherever it wants and returns notes plus a shortlist
        │
        ├─ Claude, call two: compose
        │    no tools, structured JSON out, one post
        │
        ├─ guards: length, duplicates, cliches, bait, links, self-reported
        │  confidence. A rejection goes back with reasons, up to 3 times
        │
        ├─ post to X
        │
        └─ append to memory/, commit it back to the repo
```

The two Claude calls are deliberately separate. Forcing a rigid output schema
onto the same call that is running web searches makes the model cut its
exploration short to satisfy the schema. Letting it think freely first and
formatting second produces better posts and a response that is trivial to
parse.

### Memory

`memory/posts.jsonl` and `memory/state.json` are committed back by the workflow
after every run. That is how an hourly job with no database remembers what it
already said: the last 40 posts go into the prompt each time, and the guards
check new drafts against them.

A push made with the built-in `GITHUB_TOKEN` cannot trigger another workflow
run, so this commit cannot start a loop.

You can edit both files by hand. Deleting a line from `posts.jsonl` makes the
bot willing to say that thing again. Emptying it gives it amnesia.

### The guards

The model has real freedom over *what* to say, so the guards only police
mechanical failures, never the idea:

- over 280 weighted characters (CJK and emoji count double, every link counts
  23 regardless of length, so `len()` is not the measure)
- too similar to one of the last 40 posts
- opens with a cliche, contains a hashtag, ends in engagement bait
- contains an em dash, or reads like a thread
- contains a link, unless `ALLOW_LINKS=true`
- the model marked its own confidence as `low`, meaning it was not sure its
  facts were true

A rejected draft goes back to the model with the specific reasons and it tries
again, up to three times.

### The circuit breaker

If six runs fail in a row, the seventh stops before calling Claude at all and
goes red. This exists because the expensive half of a run happens *before* the
half that can fail: a revoked X token or an empty credit balance would
otherwise let the bot spend a full month of model budget writing posts that can
never be published.

Fix the cause, then hit **Run workflow** with the dry-run box **unticked**. A
manual run always gets through the breaker, but only a post that actually
publishes clears the streak, because a dry run cannot prove that the
credentials work and those are usually what broke.

An X rate limit counts toward the streak even though the run ends green. The
model call happens before the post, so a run that gets rate limited has already
been paid for, and six of those in a row means the bot is buying posts it
cannot publish.

---

## Shaping it

### `prompts/persona.md`

This is the whole personality: what it cares about, how it writes, what it will
not do. It is loaded verbatim as the system prompt for both calls. Rewrite it
freely. The shipped version deliberately gives no topic list, because a topic
list is what makes an account boring.

If you want a narrower account ("only things about biology"), say so there. If
you want a weirder one, say that instead.

### `prompts/feeds.txt`

43 RSS and Atom feeds, one per line as `Name | URL`. A random twelve are read
each run. The model is told explicitly that this is optional reading, not an
assignment, so adding a niche feed widens what is available without narrowing
the account.

Dead feeds are skipped silently. Run `python tools/check_feeds.py`, or the
**feeds** workflow, to find them. It also runs itself monthly.

---

## Configuration

Optional, as **repository variables** (Settings → Secrets and variables →
Actions → Variables). All have working defaults.

| Variable | Default | What it does |
|---|---|---|
| `DRY_RUN` | `false` | Write a post but do not publish it. Setting this to `true` is a master pause switch: it also overrides an unticked dry-run box on a manual run. Unset it to post again. |
| `MODEL` | `claude-opus-5` | `claude-sonnet-5` is roughly 2.5x cheaper. |
| `EFFORT` | `high` | `low`, `medium`, `high`, `xhigh`, `max`. The biggest cost lever. The token ceiling scales with it, so `max` is genuinely expensive. |
| `ENABLE_WEB_SEARCH` | `true` | Off means it works from feeds and memory only. Much cheaper, noticeably less interesting. |
| `ALLOW_LINKS` | `false` | On costs about 13x per post. |
| `MONTHLY_POST_BUDGET` | `0` | `0` is unlimited. Set e.g. `400` to stop after 400 posts in a month. |
| `FEED_SAMPLE_SIZE` | `12` | Feeds read per run. |
| `FEED_ITEMS_PER_SOURCE` | `6` | Headlines taken from each one. |
| `MAX_SEARCH_ROUNDS` | `6` | How long it may keep searching within one run. |
| `ENABLE_REFUSAL_FALLBACK` | `true` | If Claude declines a request, retry it on a fallback model. Harmless to leave on: if your account is not enrolled, the bot notices once and carries on without it. |
| `RECENT_POSTS_IN_CONTEXT` | `40` | How much history the model sees. |
| `MAX_CONSECUTIVE_FAILURES` | `6` | Stop calling Claude after this many failed runs in a row. `0` disables it. |

To change how often it posts, edit the cron in
[`.github/workflows/post.yml`](.github/workflows/post.yml):

```yaml
- cron: '23 * * * *'      # hourly, at :23
- cron: '23 */3 * * *'    # every 3 hours
- cron: '23 9,13,17 * * *'  # three times a day
```

Keep the minute off zero. GitHub's own docs warn that scheduled runs are
delayed and sometimes dropped during the load spike at the top of each hour.

---

## When it goes wrong

A run ends red only when a human is actually needed. Things that fix themselves
end green with a note in the run summary.

| Symptom | Cause |
|---|---|
| Nothing happens at all, no runs appear | The workflow is not on the default branch. Scheduled workflows only run from there. |
| `403` from X, credentials look right | The access token was minted before you set the app to Read and write. Regenerate it. |
| `403` from X, mentions permission | The app is not inside a Project, or the credit balance is empty. |
| `401` from X | Wrong or revoked keys. |
| `UsageCapExceeded` | Out of X credit. Top up, or lower the cadence. |
| "The last 6 runs failed in a row" | The circuit breaker. Fix the underlying error shown beneath it, then **Run workflow** to resume. |
| "rate limited by X", green run | Self-healing. The next run will try again. |
| Duplicate content rejected | X's duplicate detection is fuzzy and undocumented. The bot rewrites once automatically. |
| Scheduled runs stopped after months | On public repos GitHub disables scheduled workflows after 60 days with "no repository activity". It does not define what counts as activity, so do not assume the bot's own memory commits reset the clock. Re-enable it in the Actions tab. Do not install a keepalive-commit action: GitHub has disabled repositories for using them to circumvent this policy. |
| Posts feel samey | Widen `prompts/persona.md`, add feeds, or raise `RECENT_POSTS_IN_CONTEXT` so it sees more of what it already said. |

Every run writes a summary to its Actions page showing the post, why it chose
it, its confidence, and the character count. That is usually faster than
reading the log.

## Stopping it

**Actions → post → ⋯ → Disable workflow.** Or set the `DRY_RUN` repository
variable to `true` to keep it thinking without publishing.

---

## Running it locally

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in your keys
set -a && source .env && set +a
python -m bot.main          # DRY_RUN=true in .env means nothing is published
```

Tests need no credentials and no network:

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```
