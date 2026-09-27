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

## Pricing (per server)

| | Free | Pro |
|---|---|---|
| Auto-answers | 50 / month | Unlimited |
| Help channels / forums | 1 | Unlimited |
| Knowledge base (`/kb add`, `/kb import-pins`) | ✓ | ✓ |
| File import (`/kb upload`) | | ✓ |
| Daily staff digest | | ✓ |

Pro is sold as a Discord **Premium App guild subscription**, so Discord handles
checkout, billing, and tax. When a server hits the free limit, Relay stops
auto-answering and posts one upgrade notice per month in the staff channel.
Members never see upsells.

If you run Relay without `PREMIUM_SKU_ID`, every server gets unlimited
everything. Use that for self-hosting and development.

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
cp .env.example .env   # fill in DISCORD_TOKEN and ANTHROPIC_API_KEY
uv run relay
```

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

### 4. Monetization (optional)

1. In the Developer Portal, go to **Monetization**. Check the current eligibility requirements and revenue share there, since Discord sets them.
2. Create a **guild subscription SKU** (for example "Relay Pro") and copy its id into `PREMIUM_SKU_ID`.
3. Restart Relay. Servers now start on Free and upgrade via the premium button in
   `/relay status` or the limit notice. Relay picks up new subscriptions immediately
   from Discord's entitlement events.

## Commands

| Command | Who | What |
|---|---|---|
| `/ask question` | Everyone | Private answer from the docs, with the same Solved / Need-a-human buttons |
| `/relay setup` | Manage Server | Staff role, digest channel, digest hour (UTC) |
| `/relay channel-add` / `channel-remove` | Manage Server | Choose where Relay auto-answers (it warns about missing permissions) |
| `/relay status` | Manage Server | Plan, answers used this month, configuration |
| `/relay stats [days]` | Manage Server | Deflection rate and estimated time saved |
| `/kb add` · `upload` · `import-pins` · `list` · `remove` | Manage Server | Manage the knowledge base |
| `/digest-now` | Manage Server | Preview today's digest (Pro) |

## How it works

- **Retrieval:** docs are split into ~1,200-character chunks and indexed with SQLite
  FTS5 (BM25, titles weighted 2×). There's no embedding service and no extra cost, and each
  server's docs are strictly isolated.
- **Answering:** the top 5 chunks and the question go to Claude with a strict
  "answer only from these sources, otherwise decline" prompt, and the output is
  structured JSON. The model defaults to `claude-opus-5`. Set `RELAY_MODEL=claude-sonnet-5`
  for lower cost per answer at high volume. The digest's topic grouping uses
  `RELAY_FAST_MODEL` (default `claude-haiku-4-5`).
- **Skipped messages:** Relay never replies to bots, to members with the staff role (or
  Manage Server), to messages under 15 characters, to follow-up messages inside
  threads, or to someone who asked less than 60 s ago.
- **Safety:** question and doc text are treated as untrusted input. Answers are
  posted with all mentions disabled. The only ping Relay ever sends is the
  staff role, when a member presses "Need a human".

### Privacy

For each question, Relay sends the question text and the matching doc excerpts to
the Anthropic API to write the answer. It stores question text in its local SQLite database
for the digest and stats and deletes it after 30 days. It does not store other
channel messages.

### Troubleshooting

- **Slash commands don't show up:** set `DEV_GUILD_ID`, or wait for global sync.
- **Relay never answers:** check `/relay status` (is there a help channel? any docs?)
  and make sure the Message Content intent is on.
- **Every answer fails with an API error:** `RELAY_REFUSAL_FALLBACK` sends Anthropic's
  server-side refusal-fallback beta on Opus/Fable models. Set it to `false` to rule
  that out.

## Development

```bash
uv sync --extra dev
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

The tests use an in-memory SQLite database and a fake Anthropic client. They make
no network calls.

### Manual live-test checklist

1. Run `/relay setup` with a staff role and digest channel, then `/relay channel-add #help`.
2. Run `/kb add` with a short "Refund policy" doc.
3. From a non-staff account, post "How do I get a refund?" in `#help`. An answer should appear in a thread with sources.
4. Press **Need a human**. The staff role should be pinged and the buttons removed.
5. Post an unrelated question. There should be no reply.
6. Run `/relay stats`, then `/digest-now`. The unrelated question should show up under "Docs to write" or "Unanswered".

---

*This repo used to hold `pumpbot`, a pump.fun sniper. It was retired after its own
evidence showed no edge. The code is in git history at `a164525`.*
