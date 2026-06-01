"""SmarTel — Telegram Business Managed AI Bot — entrypoint.

Lifecycle: parse CLI -> configure logging -> (--reset) -> load/wizard config ->
init DB & services -> init bot -> validate token -> register handlers (order
matters) -> notify owner -> long-poll with the business update types in
``allowed_updates`` (omitting them is THE classic pitfall — business handlers
would silently never fire).

Run:
    python main.py            # normal run (first launch shows the wizard)
    python main.py --reset    # reconfigure / wipe runtime state
    python main.py --debug    # verbose logging
"""

from __future__ import annotations

import logging
import sys
import time

import telebot

import config
import database
import handlers
import security
import telegram_api
import utils
from handlers import (
    approvals,
    business_updates,
    knowledge_base,
    owner_commands,
    settings as settings_handler,
)
from services import file_service

log = logging.getLogger("smartel")

# The single source of truth for which updates we receive. Business types MUST
# be present or the corresponding handlers never fire.
ALLOWED_UPDATES = [
    "message",
    "callback_query",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
]

_redaction_filter = utils.SecretRedactionFilter()


# =============================================================================
# Logging
# =============================================================================
def configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S"))
    handler.addFilter(_redaction_filter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("smartel").setLevel(level)
    # Quiet noisy third-party loggers unless debugging.
    if not debug:
        logging.getLogger("TeleBot").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("openai").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)


# =============================================================================
# Bot setup
# =============================================================================
def build_bot(cfg: config.Config) -> telebot.TeleBot:
    return telebot.TeleBot(
        cfg.bot_token,
        parse_mode="HTML",
        threaded=True,
        num_threads=cfg.worker_threads,
    )


def validate_bot(bot: telebot.TeleBot) -> dict:
    try:
        me = bot.get_me()
        return {"id": me.id, "username": me.username}
    except Exception as e:
        log.error("Could not authenticate with Telegram (check BOT_TOKEN): %s", type(e).__name__)
        raise SystemExit(2)


def register_handlers(bot: telebot.TeleBot, cfg: config.Config) -> None:
    # Order = match priority for `message` updates. The pending-input catcher
    # (approvals) must precede the catch-all fallback; the fallback is last.
    owner_commands.register(bot, cfg)
    settings_handler.register(bot, cfg)
    approvals.register(bot, cfg)
    knowledge_base.register(bot, cfg)
    business_updates.register(bot, cfg)
    owner_commands.register_fallback(bot, cfg)


def set_bot_commands(bot: telebot.TeleBot) -> None:
    cmds = [
        ("start", "Control panel"),
        ("status", "Bot, OpenAI & connection status"),
        ("settings", "Open settings"),
        ("connect_openai", "Add/update OpenAI key"),
        ("auto_on", "Enable automatic replies"),
        ("auto_off", "Disable automatic replies"),
        ("approval_on", "Require approval before sending"),
        ("approval_off", "Allow automatic sending"),
        ("kb", "Knowledge base menu"),
        ("memory_clear", "Clear conversation memory"),
        ("pause_chat", "Pause a customer chat"),
        ("resume_chat", "Resume a customer chat"),
        ("help", "Usage guide"),
    ]
    try:
        bot.set_my_commands([telebot.types.BotCommand(c, d) for c, d in cmds])
    except Exception:
        log.debug("set_my_commands failed", exc_info=True)


# =============================================================================
# Polling
# =============================================================================
def start_polling(bot: telebot.TeleBot) -> None:
    log.info("Polling started. Allowed updates: %s", ", ".join(ALLOWED_UPDATES))
    while True:
        try:
            bot.infinity_polling(allowed_updates=ALLOWED_UPDATES,
                                 timeout=30, long_polling_timeout=30)
            break  # clean exit
        except KeyboardInterrupt:
            log.info("Interrupted; shutting down.")
            break
        except Exception:
            log.exception("Polling crashed; restarting in 5s.")
            time.sleep(5)


# =============================================================================
# Entry
# =============================================================================
def main(argv: list[str]) -> int:
    do_reset = "--reset" in argv
    debug_flag = "--debug" in argv

    if do_reset:
        configure_logging(debug=debug_flag)
        config.run_reset_wizard()
        return 0

    cfg = config.load_or_run_wizard()
    configure_logging(debug=cfg.debug or debug_flag)

    # Defense-in-depth: never let the token/key appear in logs.
    _redaction_filter.add_secret(cfg.bot_token)
    _redaction_filter.add_secret(cfg.openai_api_key)

    log.info("Starting SmarTel (token %s, owner %s)",
             utils.mask_token(cfg.bot_token), utils.mask_id(cfg.owner_user_id))

    database.init_db(cfg)
    file_service.init(cfg.uploads_dir)
    telegram_api.init(cfg.bot_token)

    # Mirror the OpenAI key into the encrypted at-rest store on first run.
    if cfg.openai_api_key and not database.get_api_key_row("openai"):
        try:
            from services import openai_service
            openai_service.store_api_key(cfg.openai_api_key, persist_env=False)
        except Exception:
            log.debug("could not seed api_keys table", exc_info=True)

    bot = build_bot(cfg)
    me = validate_bot(bot)
    log.info("Authenticated as @%s (id %s)", me["username"], utils.mask_id(me["id"]))

    security.init(bot, cfg.owner_user_id)
    handlers.init(bot, cfg)
    register_handlers(bot, cfg)
    set_bot_commands(bot)
    _check_backend_ready(cfg)

    _notify_startup(bot, cfg, me)
    _print_banner(cfg, me)

    start_polling(bot)
    return 0


def _check_backend_ready(cfg: config.Config) -> None:
    if cfg.model_backend == "codex":
        from services import codex_service
        if not codex_service.is_available():
            log.warning("MODEL_BACKEND=codex but the Codex CLI was not found on PATH. "
                        "Install it (npm i -g @openai/codex) and run `codex login`.")
        elif not codex_service.is_logged_in():
            log.warning("Codex CLI found but not logged in — run `codex login` "
                        "(Sign in with ChatGPT).")


def _notify_startup(bot: telebot.TeleBot, cfg: config.Config, me: dict) -> None:
    try:
        from services import openai_service
        text = (
            "✅ <b>SmarTel is online</b> as @" + utils.escape(me["username"]) + ".\n"
            f"Auto-reply: <b>{'ON' if database.get_bool('auto_reply_enabled') else 'OFF'}</b> · "
            f"Approval: <b>{'ON' if database.get_bool('approval_mode_enabled') else 'OFF'}</b>\n"
            f"AI backend: <b>{'ready' if openai_service.is_ready() else 'not configured'}</b> "
            f"({utils.escape(openai_service.backend_label())})\n\n"
            "Connect me in <b>Telegram → Settings → Business → Chatbots</b>, then /start."
        )
        bot.send_message(cfg.owner_user_id, text, parse_mode="HTML")
    except Exception:
        log.warning("Could not message the owner at startup "
                    "(send /start to the bot once, and verify OWNER_USER_ID).")


def _print_banner(cfg: config.Config, me: dict) -> None:
    from services import openai_service
    auto = database.get_bool("auto_reply_enabled")
    appr = database.get_bool("approval_mode_enabled")
    print("\n" + "=" * 60)
    print(" SmarTel — Telegram Business Managed AI Bot")
    print("=" * 60)
    print(f" Bot:          @{me['username']}")
    print(f" Owner ID:     {utils.mask_id(cfg.owner_user_id)}")
    print(f" AI backend:   {openai_service.backend_label()}")
    print(f" Model:        {openai_service.active_model_label()}")
    print(f" Auto-reply:   {'ON' if auto else 'OFF'}")
    print(f" Approval:     {'ON' if appr else 'OFF'}")
    print(f" DB:           {cfg.db_path}")
    print("=" * 60)
    print(" Press Ctrl-C to stop.\n")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(0)
