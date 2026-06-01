# SmarTel — Telegram Business Managed AI Bot

SmarTel is a **Telegram Business "managed bot"** AI assistant. A Telegram
Business user connects the bot to their personal/business account through
Telegram's native **Settings → Business → Chatbots** interface. Once connected,
the bot receives the account's **incoming private customer messages** and can
reply **on behalf of the business account** using AI.

It uses **only the official Telegram Bot API Business features** — no Telethon /
Pyrogram userbot login, no phone number / login code / 2FA / session string, no
scraping. The bot token comes from **@BotFather**.

---

## What it does

- 🤖 **AI auto-replies** to customers (OpenAI), or
- ✅ **Approval-before-send**: the bot proposes a reply and you tap **Send / Edit
  / Reject / Pause Chat**.
- 📚 **Knowledge base** (text, `.txt`, `.md`, `.pdf`, `.docx`) used to ground
  answers, with optional **strict mode** (answer only from the KB).
- 🧠 **Per-customer memory**, isolated per conversation.
- 🙋 **Owner takeover**: when you reply to a customer yourself, auto-replies for
  that chat pause automatically (default 30 min).
- 🚦 **Rate limiting** per customer (default 20 msgs/hour) with auto-pause.
- 🛡️ **Safety layer**: sensitive messages (legal/medical/financial/refunds/
  anger/personal-data/credentials) are routed to approval even in auto mode.
- 🔒 **Owner-only controls**, structured logging with secret redaction, robust
  error handling (one bad message never crashes the bot).

### Normal bot vs. Business managed bot

| | Normal bot | **SmarTel (Business managed bot)** |
|---|---|---|
| Who messages it | Users chat the bot directly | Customers message **your account**; the bot acts on your behalf |
| How it connects | Users press *Start* | You connect it in **Telegram Business → Chatbots** |
| Update types | `message` | `business_connection`, `business_message`, `edited_business_message`, `deleted_business_messages` |
| Replies | from the bot | **from your business account** (via `business_connection_id`) |

If a non-owner messages the SmarTel bot **directly**, it just explains that it's
a managed assistant — it does not chat there.

---

## Requirements

- **Python 3.10+** (developed on 3.14).
- A **Telegram Business** account. Telegram Business features require a
  **Telegram Premium** subscription on the account that connects the bot.
- A **bot** created via **@BotFather** with **Business Mode enabled**.
- An **OpenAI API key** (for AI replies and, optionally, embeddings).

---

## 1. Create the bot in BotFather

1. Open **@BotFather** → `/newbot` → choose a name and `@username`. Copy the
   **token**.
2. Enable Business Mode: `/mybots` → select your bot → **Bot Settings** →
   **Business Mode** → **Turn on**. (If a bot is not in Business Mode, Telegram
   won't let you connect it as a chatbot.)
3. (Recommended) Set a description/about text.

Get your **numeric user ID** from **@userinfobot** (send it any message). This
is the bot **owner/admin** — only this user can control SmarTel.

---

## 2. Install

```bash
git clone <this repo> SmarTel && cd SmarTel
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## 3. Configure

Two options:

**A) First-launch wizard (recommended).** Just run the bot and answer the
prompts:

```bash
python main.py
```

It asks for: bot token, owner Telegram user ID, OpenAI API key, default model,
default auto-reply on/off, default approval-before-send on/off — then writes a
`.env` file. On later runs it loads `.env` automatically.

**B) Manual `.env`.** Copy and edit the template:

```bash
cp .env.example .env
# edit .env: BOT_TOKEN, OWNER_USER_ID, OPENAI_API_KEY, ...
```

**API-key encryption (optional, on by default if available).** If the
`cryptography` package is installed (it's in `requirements.txt`) and
`SMARTEL_FERNET_KEY` is set in `.env`, the at-rest copy of the OpenAI key in the
database is **encrypted** (Fernet). The wizard generates this key for you. The
key in `.env` is always the primary runtime source; the database copy is a
backstop + masked display. Without the Fernet key it falls back to plain-local
storage.

To reconfigure later (terminal only — never from Telegram):

```bash
python main.py --reset
```

---

### Use your ChatGPT subscription via Codex (built-in)

Instead of an API key, SmarTel can generate replies through your **ChatGPT
subscription** using the official **Codex CLI** ("Sign in with ChatGPT"). Pick
this in the first-launch wizard:

> **3) AI backend → 2) Sign in with ChatGPT**

The wizard will check for the Codex CLI, run **`codex login`** for you (browser or
`--device-auth` for headless machines), and set `MODEL_BACKEND=codex` in `.env`.
After that, each reply is produced by invoking `codex exec` with tools/sandbox
disabled, authenticated by your Codex login — no API key, no separate proxy.

Prerequisites:

```bash
npm install -g @openai/codex      # or: brew install --cask codex
codex login                       # the wizard can do this step for you
```

Relevant `.env` keys (the wizard writes these): `MODEL_BACKEND=codex`, optional
`CODEX_MODEL` (e.g. `gpt-5.4`; blank = Codex default), and advanced overrides
`CODEX_BIN`, `CODEX_EXEC_TIMEOUT`, `CODEX_EXEC_ARGS`. `/status` shows
`ChatGPT via Codex` plus whether the CLI is found/logged in.

**Caveats:** Codex is a *coding agent*, so replies are slower than an API call and
phrasing can differ; **ChatGPT-plan rate limits apply** (a busy bot can hit caps);
**embeddings are unavailable**, so the knowledge base falls back to keyword search.
The mechanism (official Codex CLI + official ChatGPT login) is legitimate, but
automating a personal subscription for a customer-facing service is a usage-policy
gray area — review OpenAI's terms before relying on it.

### Custom model endpoint (local models / other providers / proxies)

SmarTel talks to the model through a single layer (`services/openai_service.py`),
so you can point it at **any OpenAI-compatible endpoint** by setting
`OPENAI_BASE_URL` in `.env` (wizard option 3):

- **Local model (no cost):** Ollama → `OPENAI_BASE_URL=http://localhost:11434/v1`,
  LM Studio → `http://localhost:1234/v1`. Set `openai_model` in `/settings` to the
  local model name. If the server needs no key, leave `OPENAI_API_KEY` blank.
- **Another provider:** any OpenAI-compatible base URL + that provider's key.
- **A subscription-backed proxy:** if you'd rather front your ChatGPT login with a
  third-party OpenAI-compatible proxy than use the built-in Codex backend above,
  just point `OPENAI_BASE_URL` at that proxy (you run and maintain it).

`/status` shows the active backend (e.g. `OpenAI API`, `ChatGPT via Codex`, or
`custom endpoint (localhost:1234)`).

## 4. Run

```bash
python main.py            # normal
python main.py --debug    # verbose logs
```

On start it authenticates the token, initialises the SQLite DB, registers
handlers, and begins long-polling with the four Business update types in
`allowed_updates` (this is required — otherwise the Business handlers never
fire). It also DMs you a startup confirmation.

---

## 5. Connect the bot in Telegram Business

On the **account that will use the assistant**:

1. Open Telegram → **Settings**.
2. **Telegram Business** → **Chatbots**.
3. Enter the bot's **@username**.
4. **Allow it to reply** to messages, and choose **which chats** it can access.
5. **Save.**
6. From **another** Telegram account, send a message to the business account to
   test.

When connected, SmarTel stores the `business_connection_id` and notifies you.
Use `/status` to confirm.

---

## Commands (owner only)

| Command | Action |
|---|---|
| `/start` | Control panel (inline buttons) |
| `/help` | Usage guide |
| `/status` | Bot, OpenAI & Business connection status |
| `/settings` | Open the settings menu |
| `/connect_openai` | Add/update the OpenAI API key |
| `/disconnect_openai` | Remove the OpenAI API key |
| `/auto_on` · `/auto_off` | Enable/disable automatic replies |
| `/approval_on` · `/approval_off` | Require/skip approval before sending |
| `/kb` | Knowledge-base menu |
| `/kb_add <text>` | Add text to the KB |
| `/kb_upload` | Upload a KB file (.txt/.md/.pdf/.docx) |
| `/kb_search <query>` | Search the KB |
| `/kb_list` | List KB items (with delete buttons) |
| `/kb_delete <id>` | Delete a KB item |
| `/kb_clear` | Clear the KB (confirmation) |
| `/memory_clear` | Clear conversation memory (confirmation) |
| `/pause_chat <chat_id>` | Pause automation for a customer chat |
| `/resume_chat <chat_id>` | Resume automation for a customer chat |
| `/reset` | Clear pending approvals/pauses (confirmation) |
| `/cancel` | Cancel a pending input prompt |

The **control panel** (`/start`) has buttons for Status, Auto-reply on/off,
Approval on/off, Knowledge Base, Settings, Pending Approvals, Clear Memory and
Help.

---

## Settings (defaults)

Editable at runtime via `/settings`. Stored in SQLite (seeded once from `.env`).

| Setting | Default | Meaning |
|---|---|---|
| `openai_model` | `gpt-4.1-mini` | Chat model |
| `temperature` | `0.4` | Sampling temperature |
| `max_tokens` | `800` | Max output tokens |
| `auto_reply_enabled` | `false` | Send replies automatically |
| `approval_mode_enabled` | `true` | Propose replies for approval first |
| `kb_enabled` | `true` | Use the knowledge base |
| `strict_kb_mode` | `false` | Answer only from the KB |
| `memory_enabled` | `true` | Per-customer conversation memory |
| `owner_takeover_pause_minutes` | `30` | Pause after you reply manually |
| `sensitive_requires_approval` | `true` | Force approval for sensitive topics |
| `monitoring_mode` | `true` | Notify you about incoming/auto activity |
| `max_messages_per_customer_per_hour` | `20` | Per-customer rate limit |
| `public_direct_bot_chat_enabled` | `false` | Safety flag for direct DMs |

**Safe by default:** with `auto_reply_enabled=false` + `approval_mode_enabled=true`,
nothing is sent to a customer until you act. Turn on `/auto_on` when ready.

---

## Knowledge base

- Add text (`/kb_add`) or upload `.txt/.md/.pdf/.docx` (`/kb_upload`).
- Files are size-checked, extension-allowlisted, filename-sanitized, stored under
  `storage/uploads/`, and **never executed**. Text is extracted, chunked, and
  embedded (OpenAI `text-embedding-3-small`), with embeddings stored as JSON in
  SQLite.
- Retrieval uses **cosine similarity** (numpy) over stored vectors, with an
  automatic **keyword-search fallback** when embeddings are unavailable.
- **Strict mode:** if no relevant chunk is found, the customer is told *"I don't
  have enough information about that in the knowledge base."* and the model is
  not called.

---

## Approval mode

When approval mode is on (or a message is sensitive), the bot DMs you a card:
the customer's name/chat ID, their message, and the proposed reply, with buttons:

- **Send** → delivers the reply via the business connection.
- **Edit** → send your edited text as the next message; it's delivered.
- **Reject** → discards it.
- **Pause Chat** → pauses automation for that customer.

Approvals are idempotent (double-taps are guarded), and if the customer deletes
the original message the approval is auto-cancelled.

---

## Owner takeover

When you (the business account owner) reply to a customer **yourself**, Telegram
also delivers that message to the bot. SmarTel detects it
(`message.from_user.id == connection owner id`) and **pauses auto-replies** for
that chat for `owner_takeover_pause_minutes` (default 30). You can also pause /
resume manually via the approval button or `/pause_chat` / `/resume_chat`.

---

## Security notes

- The bot **never** asks for your phone number, login code, 2FA password, or a
  session string. It uses only the official Bot API Business connection.
- Owner-only access on all commands, callbacks and approvals.
- Secrets are redacted from logs; full token / OpenAI key / customer
  conversations are not logged unless `DEBUG=true`.
- The OpenAI key's at-rest DB copy is Fernet-encrypted when `SMARTEL_FERNET_KEY`
  is set. `.env` and `*.db` are git-ignored.
- The bot is safe by default (no auto-send until you enable it).

---

## Testing (end-to-end)

1. `python main.py` and send `/start` to the bot as the owner.
2. `/connect_openai` and add your key (or set it in `.env`).
3. Enable approval mode (`/approval_on`) and `/auto_on`.
4. Connect the bot in **Telegram → Settings → Business → Chatbots**.
5. From another account, message the business account.
6. You receive a proposed reply card → tap **Send**.
7. Confirm the customer receives the reply **from the business account**.
8. Reply to the customer yourself and confirm auto-replies pause for that chat.

---

## Troubleshooting

- **Business handlers never fire** → the bot must long-poll with the Business
  update types in `allowed_updates` (SmarTel does this automatically). Also
  confirm the bot has **reply permission** in your Business → Chatbots settings
  (`/status` shows `read-only` if not).
- **Can't connect the bot in Business settings** → enable **Business Mode** for
  the bot in @BotFather, and ensure the account has **Telegram Premium**.
- **"Invalid token" at startup** → re-check `BOT_TOKEN` (run `python main.py
  --reset`).
- **No AI replies** → check `/status` for *OpenAI: connected*; re-add the key
  with `/connect_openai`; verify the `openai_model` name in `/settings`.
- **Owner doesn't receive messages** → verify `OWNER_USER_ID` (from
  @userinfobot) and send the bot a `/start` once.
- **PDF added but no text** → scanned/image-only PDFs aren't supported (no OCR).

---

## Tech notes

- **Framework:** `pyTelegramBotAPI` (TeleBot), which natively supports the
  Telegram Business handlers and the `business_connection_id` send parameter
  (≥ 4.17.0 / Bot API 7.2). Raw HTTP (`telegram_api.py`) is used only for
  `getBusinessConnection` (no stable wrapper across versions) and as a fallback.
- **Storage:** SQLite (WAL, thread-local connections + a write lock for the
  multi-threaded polling model).
- **AI:** official OpenAI Python SDK; chat + embeddings; resilient to the
  `max_tokens` vs `max_completion_tokens` model difference.

Project layout: `main.py`, `config.py`, `database.py`, `telegram_api.py`,
`keyboards.py`, `security.py`, `utils.py`, `handlers/`, `services/`, `storage/`.
