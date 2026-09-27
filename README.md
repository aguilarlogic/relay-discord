# Relay

**An AI support desk for Discord communities.** Relay answers the questions your
staff answer twenty times a day, using only your own docs. It hands anything it
isn't sure about to a human, and every morning it tells you which docs are
missing.

Built for servers where support costs real staff time: game studios, SaaS and
open-source projects, and creators with big communities.

## What it does

| | |
|---|---|
| **Auto-answers** | New posts in your help channels or forums get an answer in a thread within seconds, citing the docs it used. |
| **Knows when to stay quiet** | Relay only answers from your knowledge base. No match means no reply, so staff pick it up as usual. A wrong answer is worse than no answer. |
| **Clean hand-off** | Every answer has **✅ Solved** and **🙋 Need a human** buttons. "Need a human" pings your staff role in the thread. |
| **Daily staff digest** | Posts what was escalated, what went unanswered, and **the docs you should write**: unanswered questions grouped into topics. |
| **Proves its value** | `/relay stats` shows questions answered, deflection rate, and estimated staff hours saved. |

## Plans (per server)

These are the defaults in [`tiers.toml`](tiers.toml). Prices, limits, which AI each
tier uses, and the answer packs can all be changed there without touching code.

| | Free | Starter | Pro | Business |
|---|---|---|---|---|
| Price | Free | $4.99/mo | $19.99/mo | $59.99/mo |
| AI answers / month | 50 | 300 | 750 | 2,000 |
| Help channels / forums | 1 | 2 | 5 | Unlimited |
| AI | Free third-party API (Gemini by default) | Claude Haiku | Claude Sonnet | Claude Sonnet |
| Knowledge base (`/kb add`, `/kb import-pins`) | ✓ | ✓ | ✓ | ✓ |
| File import (`/kb upload`) | | ✓ | ✓ | ✓ |
| Daily staff digest | | | ✓ | ✓ |

**Answer packs:** +200 answers for $4.99 and +1,000 for $19.99. These are one-time
purchases for a server that runs out mid-month. Pack answers never expire and are
used after the monthly allowance.

**What counts as an answer:** every question Relay sends to the AI, including the
ones where the AI then decides the docs don't cover it, because those calls cost
the same. Questions that match no docs never reach the AI and are free.

**Why these numbers:** each paid tier stays profitable even if a server uses its
whole allowance at a pessimistic ~3,000 input and ~800 output tokens per answer.
Worst-case AI cost is about 40–50% of the price, before Discord's revenue share.
Real answers are usually about half that. A test (`tests/test_tiers.py`) fails if
an edit to `tiers.toml` breaks this margin. Run `relay costs` to see what each tier
actually costs you (see [Know your margins](#know-your-margins)).

Everything is sold through Discord's **Premium Apps**, so Discord handles
checkout, billing, and tax. Tiers are *guild subscriptions* and answer packs are
*consumable* SKUs. When a server runs out, Relay stops auto-answering and posts one
notice per month in the staff channel with upgrade and pack buttons. Members
never see upsells.

Until you set a `sku_id` on a paid tier, Relay runs **self-hosted**: every server
gets the top tier with no answer limit. Use that for your own server and for
development.

## Setup

### 1. Create the Discord app

1. Go to <https://discord.com/developers/applications>, click **New Application**, then open **Bot** and reset and copy the token.
2. Under **Bot → Privileged Gateway Intents**, turn on **Message Content Intent**. Relay needs it to read questions in help channels.
3. Under **OAuth2 → URL Generator**, pick the scopes `bot` and `applications.commands`, and these bot permissions:
   View Channels, Read Message History, Send Messages, Send Messages in Threads,
   Create Public Threads, Embed Links. Open the URL to invite the bot.

### 2. Run it

```bash
uv sync
cp .env.example .env   # fill in DISCORD_TOKEN, ANTHROPIC_API_KEY, and the FREE_LLM_* settings
uv run relay
```

**Free-tier AI:** create a Gemini API key at <https://aistudio.google.com/apikey>,
then set `FREE_LLM_API_KEY` and `FREE_LLM_MODEL` to a model that is currently on
Gemini's free tier (a "flash" or "flash-lite" model; check AI Studio, since Google
changes the free lineup). Any OpenAI-compatible API works: `.env.example` shows
Groq. If `FREE_LLM_MODEL` is unset, free-tier servers get no answers, and Relay
logs a warning at startup.

Free quotas are shared across **all** your free-tier servers. As of 2026, Gemini's
free tier allows roughly 100–1,000 requests a day depending on the model. When the
quota runs out, free servers' questions go unanswered until it resets, and staff
still see them in their channel. If your free user base outgrows that, either put
the free tier on a paid Gemini key or switch it to `claude-haiku-4-5` in
`tiers.toml` (about $0.003 per answer).

Set `DEV_GUILD_ID` while developing so slash commands appear in your test server
right away. Global commands can take a while to propagate.

### 3. Configure a server (needs Manage Server)

```
/relay setup staff_role:@Support digest_channel:#staff-log
/relay channel-add channel:#help          (a text channel or a forum)
/kb add                                   (paste an FAQ, policy, or guide)
/kb import-pins channel:#faq              (turn existing pinned answers into docs)
```

Then post a question in `#help` from a non-staff account.

### 4. Start charging (optional)

1. In the Developer Portal, open your app and go to **Monetization**. Check the current eligibility requirements and revenue share there, since Discord sets them.
2. Create one **guild subscription** SKU per paid tier (Starter, Pro, Business)
   and one **consumable** SKU per answer pack. Set the prices there.
3. Paste each SKU id into its `sku_id` in `tiers.toml`, and make `price_label`
   and `monthly_price_usd` match what you set.
4. Restart Relay. Servers start on Free and upgrade from `/relay plans`,
   `/relay status`, or the limit notice. New subscriptions apply immediately
   through Discord's entitlement events. After buying an answer pack, staff run
   `/relay redeem` in the server that should get the answers.

### Know your margins

Relay logs the token usage of every AI call (not the message text). To see what
each tier really costs:

```bash
uv run relay costs            # last 30 days
uv run relay costs --days 7
```

It prints calls, tokens, AI cost, cost per server per month, and margin against
each tier's `monthly_price_usd` (before Discord's cut). If a tier's margin is
thin, lower its `monthly_answers`, move it to a cheaper model, or raise the price.

## Commands

| Command | Who | What |
|---|---|---|
| `/ask question` | Everyone | Private answer from the docs, with the same Solved / Need-a-human buttons |
| `/relay setup` | Manage Server | Staff role, digest channel, digest hour (UTC) |
| `/relay channel-add` / `channel-remove` | Manage Server | Choose where Relay auto-answers (it warns about missing permissions) |
| `/relay status` | Manage Server | Plan, answers used this month, pack balance, configuration |
| `/relay plans` | Manage Server | Compare plans; buy an upgrade or answer pack |
| `/relay redeem` | Manage Server | Apply answer packs you bought to this server |
| `/relay stats [days]` | Manage Server | Deflection rate and estimated time saved |
| `/kb add` · `upload` · `import-pins` · `list` · `remove` | Manage Server | Manage the knowledge base |
| `/digest-now` | Manage Server | Preview today's digest (Pro and Business) |

## How it works

- **Retrieval:** docs are split into ~1,200-character chunks and indexed with SQLite
  FTS5 (BM25, titles weighted 2×). There's no embedding service and no extra cost, and each
  server's docs are strictly isolated.
- **Answering:** the top 5 chunks and the question go to the server's tier model
  with a strict "answer only from these sources, otherwise decline" prompt, and
  the output is structured JSON. Claude uses native structured output. The free
  provider uses `response_format` plus the schema in the prompt, and its output
  is parsed leniently. The digest's topic grouping uses `RELAY_FAST_MODEL`
  (default `claude-haiku-4-5`).
- **Skipped messages:** Relay never replies to bots, to members with the staff role (or
  Manage Server), to messages under 15 characters, to follow-up messages inside
  threads, or to someone who asked less than 60 s ago.
- **Safety:** question and doc text are treated as untrusted input. Answers are
  posted with all mentions disabled. The only ping Relay ever sends is the
  staff role, when a member presses "Need a human".

### Privacy

For each question that matches the docs, Relay sends the question text and the
matching doc excerpts to the server's AI provider: Anthropic on paid tiers, or the
free-tier provider on Free. **Free AI APIs may use requests to improve their
models** (Gemini's free tier does), which is why `/relay status` tells Free
servers this, and why paid tiers use Claude.

Relay stores question text in its local SQLite database for the digest and stats
and deletes it after 30 days. The per-call token log contains no message content
and is kept for 120 days. It does not store other channel messages.

### Troubleshooting

- **Slash commands don't show up:** set `DEV_GUILD_ID`, or wait for global sync.
- **Relay never answers:** check `/relay status` (is there a help channel? any docs?)
  and make sure the Message Content intent is on.
- **Free servers never get answers:** check that `FREE_LLM_MODEL` and `FREE_LLM_API_KEY`
  are set. Look for "free AI provider" errors in the logs: a 429 means the free
  quota ran out, a 400 often means the provider rejects `json_schema`, so set
  `FREE_LLM_JSON_MODE=json_object`.
- **Every Claude answer fails with an API error:** `RELAY_REFUSAL_FALLBACK` sends
  Anthropic's server-side refusal-fallback beta on Opus/Fable models. Set it to
  `false` to rule that out.
- **A pack purchase didn't add answers:** run `/relay redeem` in the server. It is
  safe to run repeatedly, and each pack is credited only once.

## Development

```bash
uv sync --extra dev
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

The tests use an in-memory SQLite database, a fake Anthropic client, and a local
HTTP server standing in for the free provider. They make no calls to external
services.

### Manual live-test checklist

1. Run `/relay setup` with a staff role and digest channel, then `/relay channel-add #help`.
2. Run `/kb add` with a short "Refund policy" doc.
3. From a non-staff account, post "How do I get a refund?" in `#help`. An answer should appear in a thread with sources.
4. Press **Need a human**. The staff role should be pinged and the buttons removed.
5. Post an unrelated question. There should be no reply.
6. Run `/relay stats`, then `/digest-now`. The unrelated question should show up under "Docs to write" or "Unanswered".
7. Monetized setup: in a test server with no subscription, check that answers come from the free provider (the `relay costs` tier shows `free`). Buy a tier with a test account; `/relay status` should show the new plan on the next command. Buy a pack, run `/relay redeem`, and check the balance in `/relay status`.

---

*This repo used to hold `pumpbot`, a pump.fun sniper. It was retired after its own
evidence showed no edge. The code is in git history at `a164525`.*
