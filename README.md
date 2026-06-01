# SmarTel — Telegram Business Managed AI Bot

**Languages:** English (this file) · [فارسی / Persian](README_FA.md)

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

- 🤖 **AI auto-replies** to customers, or **approval-before-send** (the bot
  proposes a reply and you tap **Send / Edit / Reject / Pause Chat**).
- 🧩 **Three AI backends:** the **OpenAI API**, your **ChatGPT subscription via
  the Codex CLI**, or **any OpenAI-compatible endpoint** (local model / proxy).
- 📝 **Custom instructions** (`/instructions`) — a standing rule added to every
  reply (e.g. language, tone, persona).
- 📚 **Knowledge base** (text, `.txt`, `.md`, `.pdf`, `.docx`) to ground answers,
  with an optional **strict mode**.
- 🧠 **Per-customer memory**, isolated per conversation.
- 🙋 **Owner takeover**: when you reply to a customer yourself, auto-replies for
  that chat pause automatically (default 30 min).
- 🚦 **Rate limiting** per customer (default 20 msgs/hour) with auto-pause.
- 🛡️ **Safety layer**: sensitive messages (legal / medical / financial / refunds /
  anger / personal data / credentials) are routed to approval even in auto mode.
- 🔒 **Owner-only controls**, structured logging with secret redaction, and robust
  error handling (one bad message never crashes the bot).

### Normal bot vs. Business managed bot

| | Normal bot | **SmarTel (Business managed bot)** |
| --- | --- | --- |
| Who messages it | Users chat the bot directly | Customers message **your account**; the bot acts on your behalf |
| How it connects | Users press *Start* | You connect it in **Telegram Business → Chatbots** |
| Update types | `message` | `business_connection`, `business_message`, `edited_business_message`, `deleted_business_messages` |
| Replies | from the bot | **from your business account** (via `business_connection_id`) |

If a non-owner messages the SmarTel bot **directly**, it simply explains that it's
a managed assistant — it does not chat there.

---

## Requirements

- **Python 3.10+** (developed on 3.14).
- A **Telegram Business** account. Telegram Business features require a
  **Telegram Premium** subscription on the account that connects the bot.
- A **bot** created via **@BotFather** with **Business Mode enabled**.
- One AI backend: an **OpenAI API key**, **or** the **Codex CLI** signed into a
  ChatGPT plan, **or** a custom OpenAI-compatible endpoint.

---

## 1. Create the bot in BotFather

1. Open **@BotFather** → `/newbot` → choose a name and `@username`. Copy the
   **token**.
2. Enable Business Mode: `/mybots` → select your bot → **Bot Settings** →
   **Business Mode** → **Turn on**. (If a bot is not in Business Mode, Telegram
   won't let you connect it as a chatbot.)
3. (Recommended) Set a description / about text.

Get your **numeric user ID** from **@userinfobot** (send it any message). This is
the bot **owner/admin** — only this user can control SmarTel.

---

## 2. Install

```bash
git clone <this repo> SmarTel && cd SmarTel
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## 3. Configure

**A) First-launch wizard (recommended).** Run the bot and answer the prompts:

```bash
python main.py
```

It asks for the bot token, your owner user ID, an **AI backend** (see below),
the default model, and the default auto-reply / approval-mode toggles, then
writes a `.env` file. Later runs load `.env` automatically.

**B) Manual `.env`.** Copy and edit the template:

```bash
cp .env.example .env
# edit .env (see the .env reference near the end of this file)
```

**Reconfigure later** (terminal only — never from Telegram):

```bash
python main.py --reset
```

**API-key encryption (optional, on by default if available).** If the
`cryptography` package is installed and `SMARTEL_FERNET_KEY` is set in `.env`, the
at-rest copy of the OpenAI key in the database is **encrypted** (Fernet); the
wizard generates the key for you. `.env` is always the primary runtime source.

### Choosing an AI backend

The wizard's step 3 offers three choices; `/status` always shows the active one.

#### Backend 1 — OpenAI API key

Set `OPENAI_API_KEY` (from `platform.openai.com`). Pay-as-you-go; with the
default `gpt-4.1-mini` it costs roughly cents per hundred messages. You can also
update it later with `/connect_openai`.

#### Backend 2 — ChatGPT subscription via Codex (built-in)

Generate replies through your **ChatGPT plan** using the official **Codex CLI**
("Sign in with ChatGPT") — no API key, no proxy. In the wizard choose:

> **3) AI backend → 2) Sign in with ChatGPT**

The wizard checks for the Codex CLI, **installs it if missing** (`npm`, falling
back to Homebrew), runs **`codex login`** for you (browser, or `--device-auth` for
headless machines), and sets `MODEL_BACKEND=codex`. Each reply is then produced by
invoking `codex exec` with tools/sandbox disabled, authenticated by your login.

Manual setup instead of the wizard:

```bash
npm install -g @openai/codex      # or: brew install --cask codex
codex login                       # choose "Sign in with ChatGPT"
```

Relevant `.env` keys: `MODEL_BACKEND=codex`, optional `CODEX_MODEL` (e.g.
`gpt-5.4`; blank = Codex default), and advanced overrides `CODEX_BIN`,
`CODEX_EXEC_TIMEOUT`, `CODEX_EXEC_ARGS`.

**Caveats:** Codex is a *coding agent*, so replies are slower than an API call and
phrasing can differ; **ChatGPT-plan rate limits apply**; **embeddings are
unavailable** on this backend (see [Knowledge base](#knowledge-base) for how the
KB still works, and how to add embeddings). The mechanism (official Codex CLI +
official ChatGPT login) is legitimate, but automating a personal subscription for
a customer-facing service is a usage-policy gray area — review OpenAI's terms.

#### Backend 3 — Custom OpenAI-compatible endpoint

Set `OPENAI_BASE_URL` to point SmarTel at any OpenAI-compatible server:

- **Local model (no cost):** Ollama → `http://localhost:11434/v1`,
  LM Studio → `http://localhost:1234/v1`. Set `openai_model` (in `/settings`) to
  the local model name; leave `OPENAI_API_KEY` blank if the server needs no key.
- **Another provider:** any OpenAI-compatible base URL + that provider's key.
- **A subscription-backed proxy:** front your ChatGPT login with a third-party
  OpenAI-compatible proxy and point `OPENAI_BASE_URL` at it (you run/maintain it).

---

## 4. Run

```bash
python main.py            # normal
python main.py --debug    # verbose logs
```

On start it authenticates the token, initialises the SQLite DB, registers
handlers, and long-polls with the four Business update types in `allowed_updates`
(required — otherwise the Business handlers never fire). It also DMs you a startup
confirmation.

---

## 5. Connect the bot in Telegram Business

On the **account that will use the assistant**:

1. Open Telegram → **Settings**.
2. **Telegram Business** → **Chatbots**.
3. Enter the bot's **@username**.
4. **Allow it to reply** to messages, and choose **which chats** it can access.
5. **Save.**
6. From **another** Telegram account, message the business account to test.

When connected, SmarTel stores the `business_connection_id` and notifies you. Use
`/status` to confirm (it shows whether the connection can reply).

---

## Commands (owner only)

| Command | Action |
| --- | --- |
| `/start` | Control panel (inline buttons) |
| `/help` | Usage guide |
| `/status` | Bot, AI backend & Business connection status |
| `/settings` | Open the settings menu |
| `/connect_openai` | Add/update the OpenAI API key |
| `/disconnect_openai` | Remove the OpenAI API key |
| `/auto_on` · `/auto_off` | Enable/disable automatic replies |
| `/approval_on` · `/approval_off` | Require/skip approval before sending |
| `/instructions [text\|clear]` | Show / set / clear the standing instruction (e.g. language, persona) |
| `/kb` | Knowledge-base menu |
| `/kb_add <text>` | Add text to the KB |
| `/kb_upload` | Upload a KB file (`.txt`/`.md`/`.pdf`/`.docx`) |
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

## Settings

Editable at runtime via `/settings` (booleans toggle in place; numbers/enums open
a sub-screen). Stored in SQLite, seeded once from `.env` on first run.

| Setting | Default | Meaning |
| --- | --- | --- |
| `openai_model` | `gpt-4.1-mini` | Chat model (OpenAI / custom-endpoint backends) |
| `temperature` | `0.4` | Sampling temperature |
| `max_tokens` | `800` | Max output tokens |
| `auto_reply_enabled` | `false` | Send replies automatically |
| `approval_mode_enabled` | `true` | Propose replies for approval first |
| `kb_enabled` | `true` | Use the knowledge base |
| `strict_kb_mode` | `false` | Answer **only** from the KB |
| `memory_enabled` | `true` | Per-customer conversation memory |
| `owner_takeover_pause_minutes` | `30` | Pause length after you reply manually |
| `sensitive_requires_approval` | `true` | Force approval for sensitive topics |
| `monitoring_mode` | `true` | Notify you about incoming / auto activity |
| `max_messages_per_customer_per_hour` | `20` | Per-customer rate limit |
| `public_direct_bot_chat_enabled` | `false` | Safety flag for direct (non-business) DMs |
| `process_nontext_enabled` | `false` | Also process media captions (not just text) |
| `custom_instructions` | *(empty)* | Standing instruction added to every reply (set via `/instructions`) |

**Safe by default:** with `auto_reply_enabled=false` + `approval_mode_enabled=true`,
nothing reaches a customer until you act. Turn on `/auto_on` when ready.

---

## Custom instructions (persona & language)

`/instructions` sets a standing rule that is **always added to the system prompt**
on every reply — the right place for language, tone, or persona. Unlike the
knowledge base, it is not retrieval-dependent, so it always applies, on **all**
backends (including Codex).

```text
/instructions Your name is Charisma. You are from Iran. Always answer in Persian (فارسی).
/instructions            # show the current instruction
/instructions clear      # remove it
```

> Use `/instructions` for behavior (language/persona), and the **knowledge base**
> for facts. Putting "always reply in Persian" in the KB does **not** work — KB
> text is optional, retrieval-dependent context, not a command.

---

## Knowledge base

- Add text (`/kb_add`) or upload `.txt` / `.md` / `.pdf` / `.docx` (`/kb_upload`).
- Files are size-checked, extension-allowlisted, filename-sanitized, stored under
  `storage/uploads/`, and **never executed**. Text is extracted and chunked.
- **Retrieval:** OpenAI embeddings (`text-embedding-3-small`) stored as JSON in
  SQLite, ranked by **cosine similarity** (numpy), with an automatic
  **keyword-search fallback** when embeddings aren't available.
- **Small KB shortcut:** if the whole KB is small (≤ 12 chunks), **all** of it is
  included in the prompt and the model itself relates the question to your facts.
  This makes a handful of facts reliable even without embeddings.
- **Strict mode** (`strict_kb_mode`): if nothing relevant is found, the customer
  is told *"I don't have enough information about that in the knowledge base."* and
  the model is not called. Keep this **off** for general Q&A.

**Embeddings + the Codex backend.** Embeddings always go through OpenAI, while
chat can go through Codex. So for a **large** KB on the Codex backend, set
`OPENAI_API_KEY` *and* `MODEL_BACKEND=codex`: KB retrieval uses OpenAI embeddings
(extremely cheap) while replies still use your ChatGPT subscription. Items added
*before* a key was available are stored without embeddings (keyword-only) — re-add
them to embed.

---

## Approval mode

When approval mode is on (or a message is flagged sensitive), the bot DMs you a
card with the customer's name/chat ID, their message, and the proposed reply, plus
buttons:

- **Send** → delivers the reply via the business connection.
- **Edit** → send your edited text as the next message; it is delivered.
- **Reject** → discards it.
- **Pause Chat** → pauses automation for that customer.

Approvals are idempotent (double-taps are guarded). If the customer deletes the
original message, the approval is auto-cancelled. The Edit flow is restart-safe
(stored in the DB, not in memory).

---

## Owner takeover

When you (the business account owner) reply to a customer **yourself**, Telegram
also delivers that message to the bot. SmarTel detects it
(`message.from_user.id == connection owner id`) and **pauses auto-replies** for
that chat for `owner_takeover_pause_minutes` (default 30). You can also pause /
resume manually via the approval button or `/pause_chat` / `/resume_chat`.

---

## Rate limiting & sensitive handling

- **Rate limit:** a rolling 1-hour window per customer (`max_messages_per_customer_per_hour`,
  default 20). On exceed, that customer's auto-replies pause for an hour and you're
  notified.
- **Sensitive handling:** if a message matches a sensitive category (legal,
  medical, financial, refunds, anger/threats, personal data, credentials) and
  `sensitive_requires_approval` is on, the reply is routed to approval even when
  auto-reply is enabled.

---

## Security notes

- The bot **never** asks for your phone number, login code, 2FA password, or a
  session string. It uses only the official Bot API Business connection.
- Owner-only access on all commands, callbacks and approvals.
- Secrets are redacted from logs; full token / OpenAI key / customer conversations
  are not logged unless `DEBUG=true`.
- The OpenAI key's at-rest DB copy is Fernet-encrypted when `SMARTEL_FERNET_KEY`
  is set. `.env`, `*.db` and uploads are git-ignored.
- Safe by default — nothing is auto-sent until you enable it.

---

## .env reference

Bootstrap secrets/identity live in `.env`; behavioral settings live in the
`settings` table (and are edited via `/settings`).

| Variable | Required | Meaning |
| --- | --- | --- |
| `BOT_TOKEN` | yes | Bot token from @BotFather |
| `OWNER_USER_ID` | yes | Your numeric Telegram user ID (from @userinfobot) |
| `OPENAI_API_KEY` | for OpenAI backend / embeddings | OpenAI API key (`sk-...`) |
| `OPENAI_BASE_URL` | for custom endpoint | OpenAI-compatible base URL (e.g. `http://localhost:1234/v1`) |
| `MODEL_BACKEND` | no | `openai` (default) or `codex` |
| `CODEX_MODEL` | no | Model passed to `codex exec -m` (blank = Codex default) |
| `CODEX_BIN` | no | Path/name of the Codex binary (default `codex`) |
| `CODEX_EXEC_TIMEOUT` | no | Per-reply timeout in seconds (default 180) |
| `CODEX_EXEC_ARGS` | no | Override flags for `codex exec` (default `--skip-git-repo-check --sandbox read-only`) |
| `SMARTEL_FERNET_KEY` | no | Fernet key to encrypt the at-rest API-key copy |
| `DEFAULT_MODEL` | no | Seeds `openai_model` on first run |
| `DEFAULT_AUTO_REPLY` | no | Seeds `auto_reply_enabled` (default `false`) |
| `DEFAULT_APPROVAL_MODE` | no | Seeds `approval_mode_enabled` (default `true`) |
| `DB_PATH` | no | SQLite path (default `storage/smartel.db`) |
| `UPLOADS_DIR` | no | KB uploads dir (default `storage/uploads`) |
| `VECTOR_STORE_DIR` | no | Reserved (default `storage/vector_store`) |
| `WORKER_THREADS` | no | Polling worker threads (default 4) |
| `DEBUG` | no | Verbose logging; may log content (default `false`) |

---

## Testing (end-to-end)

1. `python main.py` and send `/start` to the bot as the owner.
2. Configure a backend (API key via `/connect_openai`, or pick Codex in the wizard).
3. (Optional) `/instructions Always reply in Persian.` and add facts with `/kb_add`.
4. Enable `/approval_on` and `/auto_on`.
5. Connect the bot in **Telegram → Settings → Business → Chatbots**.
6. From another account, message the business account.
7. You receive a proposed reply card → tap **Send**; confirm the customer gets it
   **from your business account**.
8. Reply to the customer yourself and confirm auto-replies pause for that chat.

---

## Troubleshooting

- **Business handlers never fire** → the bot must long-poll with the Business
  update types in `allowed_updates` (SmarTel does this). Also confirm the bot has
  **reply permission** in Business → Chatbots (`/status` shows `read-only` if not).
- **Can't connect the bot in Business settings** → enable **Business Mode** in
  @BotFather and ensure the account has **Telegram Premium**.
- **"Invalid token" at startup** → re-check `BOT_TOKEN` (`python main.py --reset`).
- **Every reply says "I don't have enough information…"** → **Strict KB mode** is
  on; turn it off in `/settings` (it answers only from the KB).
- **The bot ignores a KB fact** → on the Codex backend there are no embeddings, so
  retrieval is keyword-only; a small KB (≤ 12 items) is always included, but for a
  larger KB add `OPENAI_API_KEY` for embeddings. For language/persona use
  `/instructions`, not the KB.
- **`codex exec` flag error** (`unexpected argument …`) → SmarTel auto-retries with
  a minimal flag set; if it persists, set `CODEX_EXEC_ARGS` in `.env` to flags your
  Codex version accepts, and confirm `codex login` succeeded.
- **No AI replies** → check `/status` (*AI backend: ready*); re-add the key with
  `/connect_openai`, verify `openai_model`, or that Codex is logged in.
- **Owner doesn't receive messages** → verify `OWNER_USER_ID` and `/start` the bot once.
- **PDF added but no text** → scanned/image-only PDFs aren't supported (no OCR).

---

## Tech notes

- **Framework:** `pyTelegramBotAPI` (TeleBot), which natively supports the Telegram
  Business handlers and the `business_connection_id` send parameter (≥ 4.17.0 /
  Bot API 7.2). Raw HTTP (`telegram_api.py`) is used only for `getBusinessConnection`
  (no stable wrapper across versions) and as a fallback.
- **Storage:** SQLite (WAL, thread-local connections + a write lock for the
  multi-threaded polling model).
- **AI:** official OpenAI Python SDK (chat + embeddings, resilient to the
  `max_tokens` vs `max_completion_tokens` model difference); optional Codex CLI
  backend via `codex exec`.

Project layout: `main.py`, `config.py`, `database.py`, `telegram_api.py`,
`keyboards.py`, `security.py`, `utils.py`, `handlers/`, `services/`, `storage/`.
