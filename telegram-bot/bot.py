import asyncio
import html
import logging
import os
import signal
import sys

from pyrogram import Client
from pyrogram.errors import FloodWait
from config import API_ID, API_HASH, BOT_TOKEN, SESSION, LOG_CHANNEL, RESULTS_CHANNEL
from database import create_indexes
from plugins.scheduler import start_daily_summary_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

_MAX_DUP_ATTEMPTS = 3
_bot_ref = None

_PERMANENT_REVOKE_KEYWORDS = [
    "SESSION_REVOKED",
    "AUTH_KEY_UNREGISTERED",
    "You must delete your session file",
    "SessionRevoked",
    "AuthKeyUnregistered",
    "USER_DEACTIVATED",
]


def _is_auth_key_duplicated(exc: Exception) -> bool:
    return "AUTH_KEY_DUPLICATED" in str(exc)


def _is_permanent_revocation(exc: Exception) -> bool:
    msg = str(exc)
    return any(k in msg for k in _PERMANENT_REVOKE_KEYWORDS)


def _is_connection_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in (
        "connection lost", "connection reset", "broken pipe",
        "eof detected", "socket.send", "send exception",
    )) or isinstance(exc, (OSError, ConnectionResetError, ConnectionError, EOFError))


def _session_name():
    sessions_dir = os.environ.get("SESSIONS_DIR", "sessions")
    try:
        os.makedirs(sessions_dir, exist_ok=True)
        test = os.path.join(sessions_dir, ".write_test")
        open(test, "w").close()
        os.remove(test)
        return os.path.join(sessions_dir, "bot")
    except Exception:
        return ":memory:"


async def _notify_session_issue(text: str):
    """Send an alert to LOG_CHANNEL when the user session has a problem."""
    global _bot_ref
    if _bot_ref is None or not LOG_CHANNEL:
        logger.warning("Session alert (no bot ref): %s", text.replace('\n', ' '))
        return
    try:
        await _bot_ref.send_message(LOG_CHANNEL, text, disable_web_page_preview=True)
    except Exception as e:
        logger.warning("Failed to send session alert to LOG_CHANNEL: %s", e)


class Bot(Client):
    def __init__(self):
        name = _session_name()
        kwargs = dict(
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            plugins={"root": "plugins"},
            sleep_threshold=60,
        )
        if name == ":memory:":
            kwargs["in_memory"] = True
            name = "bot"
        super().__init__(name=name, **kwargs)

    async def start(self):
        await super().start()
        await create_indexes()

        if SESSION:
            started = await _start_user_session()
            if not started:
                asyncio.create_task(_session_retry_loop())
        else:
            logger.warning("⚠️  SESSION not set — search will not work.")

        if not RESULTS_CHANNEL:
            logger.warning("⚠️  RESULTS_CHANNEL not set.")
        else:
            await _warmup_results_channel(self)

        _start_autodelete_worker(self)
        await start_daily_summary_scheduler(self)

        from pyrogram.types import BotCommand
        await self.set_bot_commands([
            BotCommand("start",       "Check if I'm alive"),
            BotCommand("id",          "Get channel/group ID"),
            BotCommand("verify",      "Request group verification"),
            BotCommand("connect",     "Connect a channel for searching"),
            BotCommand("disconnect",  "Disconnect a channel"),
            BotCommand("connections", "List connected channels"),
            BotCommand("fsub",        "Set force-subscribe channel"),
            BotCommand("nofsub",      "Remove force-subscribe"),
            BotCommand("autodelete",  "Set result auto-delete timer"),
            BotCommand("addsource",   "Add/remove/list source channels"),
            BotCommand("ping",        "Check bot speed"),
            BotCommand("stats",       "Bot statistics (owner only)"),
            BotCommand("setbackup",   "Update backup channel link (owner only)"),
            BotCommand("help",        "Show all commands"),
        ])

        me = await self.get_me()
        logger.info("✅ CineRequestBot started as @%s (%d)", me.username, me.id)

        if LOG_CHANNEL:
            try:
                await self.send_message(
                    LOG_CHANNEL,
                    f"✅ <b>CineRequestBot Started</b>\n\n"
                    f"🤖 @{me.username} (<code>{me.id}</code>)\n"
                    f"📺 Results channel: <code>{RESULTS_CHANNEL or 'NOT SET'}</code>\n"
                    f"🔑 Session: {'✅ active' if SESSION else '❌ missing'}",
                )
            except Exception:
                pass

    async def stop(self, *args):
        try:
            from client import User
            if User is not None and User.is_connected:
                await User.stop()
                logger.info("✅ User session disconnected cleanly on shutdown")
        except Exception:
            pass
        await super().stop()
        logger.info("Bot stopped")


async def _warmup_results_channel(bot):
    try:
        if isinstance(RESULTS_CHANNEL, str):
            chat = await bot.get_chat(RESULTS_CHANNEL)
            logger.info("✅ Results channel resolved: %s", chat.title)
        else:
            from pyrogram.raw.functions.channels import GetFullChannel
            from pyrogram.raw.types import InputChannel
            bare_id = abs(RESULTS_CHANNEL) - 1_000_000_000_000
            try:
                result = await bot.invoke(
                    GetFullChannel(channel=InputChannel(channel_id=bare_id, access_hash=0))
                )
                title = result.chats[0].title if result.chats else str(RESULTS_CHANNEL)
                logger.info("✅ Results channel resolved via raw API: %s", title)
            except Exception:
                chat = await bot.get_chat(RESULTS_CHANNEL)
                logger.info("✅ Results channel resolved via get_chat: %s", chat.title)
    except Exception as e:
        logger.warning("⚠️  Could not resolve RESULTS_CHANNEL %s: %s", RESULTS_CHANNEL, e)


async def _start_user_session() -> bool:
    """Try to start the user session. Returns True on success, False if capped out on
    AUTH_KEY_DUPLICATED (old container still alive). Raises on permanent revocation.
    """
    try:
        from client import User
        if User is None:
            logger.error("User is None — SESSION env var set but client failed to init")
            return False
        if User.is_connected:
            return True
        dup_attempts = 0
        dup_wait = 30
        while True:
            try:
                await User.start()
                await asyncio.wait_for(User.get_me(), timeout=15)
                me = await User.get_me()
                logger.info("✅ User session active: @%s (id=%d)", me.username or me.first_name, me.id)
                count = 0
                async for _ in User.get_dialogs():
                    count += 1
                    if count >= 200:
                        break
                logger.info("✅ Peer cache warmed (%d dialogs loaded)", count)
                return True
            except asyncio.TimeoutError:
                logger.warning("⚠️  User session ping timed out — retrying")
                try:
                    await User.stop()
                except Exception:
                    pass
                await asyncio.sleep(10)
            except Exception as e:
                if _is_permanent_revocation(e):
                    logger.error("❌ User session permanently revoked: %s", e)
                    raise
                if _is_auth_key_duplicated(e):
                    dup_attempts += 1
                    if dup_attempts >= _MAX_DUP_ATTEMPTS:
                        logger.warning(
                            "⚠️  AUTH_KEY_DUPLICATED after %d attempts — old Railway container still alive. "
                            "Bot will start without search; retrying in background every 5 min.",
                            dup_attempts,
                        )
                        try:
                            await User.stop()
                        except Exception:
                            pass
                        return False
                    logger.warning(
                        "⚠️  AUTH_KEY_DUPLICATED (attempt %d/%d) — waiting %ds",
                        dup_attempts, _MAX_DUP_ATTEMPTS, dup_wait,
                    )
                    try:
                        await User.stop()
                    except Exception:
                        pass
                    await asyncio.sleep(dup_wait)
                    dup_wait = min(dup_wait + 30, 60)
                elif isinstance(e, FloodWait):
                    wait = e.value + 5
                    logger.warning("⚠️  FloodWait on user session — waiting %ds", wait)
                    await asyncio.sleep(wait)
                else:
                    raise
    except Exception as e:
        if _is_permanent_revocation(e):
            raise
        logger.warning("⚠️  User session failed to start: %s", e)
        return False


async def _session_retry_loop():
    """Background task: retry starting the user session every 5 minutes.

    - Sends a LOG_CHANNEL alert if session is still down after 30 minutes.
    - Sends an immediate alert and stops if the session is permanently revoked.
    """
    logger.info("🔄 Session retry loop started — will retry every 5 min until session is up")
    retry_count = 0
    alerted = False
    while True:
        await asyncio.sleep(300)
        retry_count += 1
        try:
            from client import User
            if User is not None and User.is_connected:
                logger.info("✅ Session retry loop: user session is now connected — stopping loop")
                if alerted:
                    await _notify_session_issue(
                        "✅ <b>User session recovered!</b>\n"
                        "Search is working again."
                    )
                return
            success = await _start_user_session()
            if success:
                logger.info("✅ Session retry loop: user session recovered — search now available")
                if alerted:
                    await _notify_session_issue(
                        "✅ <b>User session recovered!</b>\n"
                        "Search is working again."
                    )
                return
            if retry_count >= 6 and not alerted:
                alerted = True
                await _notify_session_issue(
                    "⚠️ <b>Search has been unavailable for 30+ minutes.</b>\n\n"
                    "This usually means the old Railway container is still alive holding the session.\n"
                    "It should recover automatically. If search is still broken after 1 hour:\n\n"
                    "1. Generate a new SESSION string from your account\n"
                    "2. Update it in Railway → Variables → SESSION\n"
                    "3. Railway will redeploy automatically"
                )
        except Exception as e:
            if _is_permanent_revocation(e):
                logger.error("❌ Session permanently revoked — sending alert and stopping retry loop")
                await _notify_session_issue(
                    "❌ <b>User session has been permanently revoked by Telegram!</b>\n\n"
                    "🔴 <b>Search is disabled.</b>\n\n"
                    "<b>To fix this:</b>\n"
                    "1. Open Telegram on your phone/PC\n"
                    "2. Run the session generator script\n"
                    "3. Copy the new SESSION string\n"
                    "4. Go to Railway → Variables → SESSION → paste it → Save\n\n"
                    f"<i>Error: <code>{html.escape(str(e))}</code></i>"
                )
                return
            logger.warning("Session retry loop error: %s — will retry in 5 min", e)


async def _reconnect_user_session():
    from client import User
    if User is None:
        return
    try:
        if User.is_connected:
            await User.stop()
    except Exception:
        pass
    await asyncio.sleep(5)
    await _start_user_session()


async def _session_watchdog():
    while True:
        await asyncio.sleep(300)
        if not SESSION:
            continue
        try:
            from client import User
            if User is None:
                continue
            if not User.is_connected:
                logger.warning("Watchdog: User session disconnected — reconnecting")
                await _reconnect_user_session()
            else:
                try:
                    await asyncio.wait_for(User.get_me(), timeout=30)
                except asyncio.TimeoutError:
                    logger.warning("Watchdog: ping timed out — forcing reconnect")
                    await _reconnect_user_session()
                except Exception as e:
                    if _is_permanent_revocation(e):
                        logger.error("Watchdog: session permanently revoked")
                        await _notify_session_issue(
                            "❌ <b>User session has been permanently revoked by Telegram!</b>\n\n"
                            "🔴 <b>Search is disabled.</b>\n\n"
                            "<b>To fix:</b>\n"
                            "1. Generate a new SESSION string from your account\n"
                            "2. Update it in Railway → Variables → SESSION\n\n"
                            f"<i>Error: <code>{html.escape(str(e))}</code></i>"
                        )
                        return
                    elif _is_auth_key_duplicated(e):
                        logger.warning("Watchdog: AUTH_KEY_DUPLICATED — waiting 60s before reconnect")
                        await asyncio.sleep(60)
                        await _reconnect_user_session()
                    elif _is_connection_error(e):
                        logger.warning("Watchdog: connection error (%s) — reconnecting", e)
                        await _reconnect_user_session()
                    else:
                        logger.warning("Watchdog ping error: %s", e)
        except FloodWait as e:
            wait = e.value + 5
            logger.warning("Watchdog FloodWait — sleeping %ds", wait)
            await asyncio.sleep(wait)
        except Exception as e:
            logger.warning("Watchdog outer error: %s", e)


def _start_autodelete_worker(bot):
    from utils.delete import run_autodelete_loop
    asyncio.create_task(run_autodelete_loop(bot))
    logger.info("✅ Auto-delete loop started")


async def _start_bot_with_flood_retry() -> "Bot":
    while True:
        bot = Bot()
        try:
            await bot.start()
            return bot
        except FloodWait as e:
            wait = e.value + 10
            logger.warning("⚠️  FloodWait on bot auth — waiting %d seconds", wait)
            try:
                await bot.stop()
            except Exception:
                pass
            await asyncio.sleep(wait)
        except Exception:
            try:
                await bot.stop()
            except Exception:
                pass
            raise


async def main():
    global _bot_ref
    from health import start_health_server
    start_health_server()

    bot = await _start_bot_with_flood_retry()
    _bot_ref = bot
    watchdog = asyncio.create_task(_session_watchdog())
    stop_event = asyncio.Event()

    async def _graceful_shutdown():
        logger.info("SIGTERM — disconnecting user session before exit")
        try:
            from client import User
            if User is not None and User.is_connected:
                await asyncio.wait_for(User.stop(), timeout=10)
                logger.info("✅ User session disconnected on shutdown")
        except Exception as e:
            logger.warning("Could not cleanly stop user session: %s", e)
        stop_event.set()

    def _handle_signal():
        asyncio.ensure_future(_graceful_shutdown())

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass

    logger.info("Bot is running. SIGTERM/Ctrl+C to stop.")
    await stop_event.wait()

    watchdog.cancel()
    try:
        await watchdog
    except asyncio.CancelledError:
        pass
    await bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by user")
