import asyncio
import html
import logging
import re
import uuid
from time import time

from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, ChannelInvalid, ChannelPrivate, PeerIdInvalid
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import (
    RESULTS_CHANNEL, SEARCH_REPLY_TTL, SESSION, BACKUP_CHANNEL,
    LOG_CHANNEL, OWNER_ID, PM_SEARCH_USER_TTL, PM_SEARCH_BOT_TTL
)
from database.db import (
    get_group, get_groups, force_sub, save_dlt_message,
    get_setting, record_failed_search
)
from plugins.fsub import check_pm_fsub, build_pm_fsub_markup
from utils.spell import google_spell_check
from utils.imdb import search_imdb

logger = logging.getLogger(__name__)

_TG_LIMIT = 4096
_RESULTS_PER_PAGE = 4
_SESSION_TTL = 3600
_page_sessions: dict = {}

_user_start_lock = asyncio.Lock()
_rate_limit: dict = {}
_RATE_LIMIT_COUNT = 3
_RATE_LIMIT_WINDOW = 30

# Per-user cooldown for Request Here (60 s)
_req_cooldown: dict = {}

def _is_rate_limited(user_id: int) -> bool:
    now = time()
    hits = [t for t in _rate_limit.get(user_id, []) if now - t < _RATE_LIMIT_WINDOW]
    if len(hits) >= _RATE_LIMIT_COUNT:
        _rate_limit[user_id] = hits
        return True
    hits.append(now)
    _rate_limit[user_id] = hits
    return False

def _prune_sessions():
    now = time()
    expired = [k for k, v in _page_sessions.items() if now > v.get("ttl", 0)]
    for k in expired:
        _page_sessions.pop(k, None)
    expired_rl = [k for k, v in _rate_limit.items() if not any(now - t < _RATE_LIMIT_WINDOW for t in v)]
    for k in expired_rl:
        _rate_limit.pop(k, None)
    expired_rc = [k for k, v in _req_cooldown.items() if now - v > 60]
    for k in expired_rc:
        _req_cooldown.pop(k, None)

async def _get_backup_link() -> str:
    try:
        link = await get_setting("backup_link")
        if link:
            return link
    except Exception:
        pass
    return BACKUP_CHANNEL or ""

async def _results_link(bot, channel_id: int, message_id: int) -> str:
    try:
        chat = await bot.get_chat(channel_id)
        if getattr(chat, "username", None):
            return f"https://t.me/{chat.username}/{message_id}"
    except Exception:
        pass
    cid = str(channel_id)
    if cid.startswith("-100"):
        cid = cid[4:]
    return f"https://t.me/c/{cid}/{message_id}"

async def _schedule_delete(bot, message, ttl: int):
    try:
        await save_dlt_message(message, int(time()) + ttl)
    except Exception as e:
        logger.debug("save_dlt_message: %s", e)

async def _get_pm_sources(bot) -> list:
    sources = list(await get_setting("global_sources", []))
    if sources:
        return sources
    try:
        groups, _ = await get_groups()
        for g in groups:
            for ch in g.get("channels", []):
                if ch not in sources:
                    sources.append(ch)
    except Exception as e:
        logger.warning("Error fetching fallback group sources: %s", e)
    return sources

async def _search_channels(user_client, channels: list, query: str, backup_link: str = "") -> list:
    results = []
    for ch_id in channels:
        try:
            async for msg in user_client.search_messages(ch_id, query=query, limit=50):
                text = (msg.text or msg.caption or "").strip()
                if backup_link:
                    text = re.sub(
                        r"https?://t\.me/(?:\+|%2B|c/|joinchat/)[a-zA-Z0-9_-]+|https?://t\.me/[a-zA-Z0-9_]+",
                        backup_link, text, flags=re.IGNORECASE
                    )
                if text:
                    results.append(text)
                    if len(results) >= 30:
                        return results
        except (ChannelInvalid, ChannelPrivate):
            logger.debug("Channel %s invalid/private -- skipping", ch_id)
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception as e:
            logger.debug("Search error ch=%s: %s", ch_id, e)
    return results

def _build_page_message(query: str, page_results: list, total: int,
                        page: int, total_pages: int,
                        offset: int, ttl_secs: int,
                        backup_link: str = "") -> str:
    mins = max(1, ttl_secs // 60)
    header = (
        "🔍 <b>Search:</b> " + html.escape(query) + "\n"
        + "📄 <b>Page " + str(page) + "/" + str(total_pages) + "</b>  ·  "
        + "<b>" + str(total) + " result" + ("s" if total != 1 else "") + " total</b>\n\n"
    )
    join_line = "📢 Join: " + backup_link + "\n" if backup_link else ""
    footer = (
        "\n" + "─" * 32 + "\n"
        + join_line
        + "⏳ <i>Auto-deletes in " + str(mins) + " min" + ("s" if mins != 1 else "") + "</i>"
    )
    budget = _TG_LIMIT - len(header) - len(footer) - 20
    body_lines = []
    used = 0
    for i, text in enumerate(page_results, offset + 1):
        full = html.escape(text)
        remaining = budget - used
        if remaining <= 10 and used > 0:
            break
        if len(full) <= remaining - 10:
            snippet = full
        else:
            snippet = full[:max(remaining - 1, 50)] + "…"
        entry = "<b>" + str(i) + ".</b> " + snippet + "\n\n"
        body_lines.append(entry)
        used += len(entry)
        if used >= budget:
            break
    return header + "".join(body_lines) + footer

def _page_keyboard(session_id: str, page: int, total_pages: int,
                   current_url: str) -> InlineKeyboardMarkup:
    rows = []
    if total_pages > 1:
        half = 2
        start = max(1, min(page - half, total_pages - 3))
        end = min(total_pages, start + 3)
        start = max(1, end - 3)
        pg_row = []
        for p in range(start, end + 1):
            label = "• Pg " + str(p) + " •" if p == page else "Pg " + str(p)
            pg_row.append(InlineKeyboardButton(label, callback_data="pg_" + session_id + "_" + str(p)))
        rows.append(pg_row)
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data="pg_" + session_id + "_" + str(page - 1)))
        if page < total_pages:
            nav.append(InlineKeyboardButton("Next ➡️", callback_data="pg_" + session_id + "_" + str(page + 1)))
        if nav:
            rows.append(nav)
    rows.append([InlineKeyboardButton("🎬 Click here to Get movie/Series", url=current_url)])
    return InlineKeyboardMarkup(rows)

async def _send_to_results_channel(bot, text: str):
    try:
        return await bot.send_message(chat_id=RESULTS_CHANNEL, text=text, disable_web_page_preview=True)
    except FloodWait as e:
        logger.warning("FloodWait of %ds when posting to RESULTS_CHANNEL", e.value)
        await asyncio.sleep(e.value + 1)
        return await bot.send_message(chat_id=RESULTS_CHANNEL, text=text, disable_web_page_preview=True)
    except (PeerIdInvalid, ValueError):
        logger.warning("Peer id invalid for RESULTS_CHANNEL -- re-resolving")
        try:
            if isinstance(RESULTS_CHANNEL, str):
                await bot.get_chat(RESULTS_CHANNEL)
            else:
                from pyrogram.raw.functions.channels import GetFullChannel
                from pyrogram.raw.types import InputChannel
                bare_id = abs(RESULTS_CHANNEL) - 1_000_000_000_000
                try:
                    await bot.invoke(GetFullChannel(channel=InputChannel(channel_id=bare_id, access_hash=0)))
                except Exception:
                    await bot.get_chat(RESULTS_CHANNEL)
        except Exception as resolve_err:
            logger.warning("Peer re-resolution failed: %s", resolve_err)
        return await bot.send_message(chat_id=RESULTS_CHANNEL, text=text, disable_web_page_preview=True)

# ── Unified Search Handler (PM + Groups) ──────────────────────────────────────

@Client.on_message(
    filters.text & (filters.group | filters.private) & filters.incoming
    & ~filters.via_bot & ~filters.bot
    & ~filters.command([
        "start", "help", "about", "id", "verify", "connect", "disconnect",
        "connections", "fsub", "nofsub", "fsub_pm", "autodelete", "broadcast",
        "broadcast_groups", "ping", "stats", "setbackup",
        "addsource", "summary", "clearfailed",
    ]))
async def search_handler(bot, message):
    if not message.from_user:
        return

    query = message.text.strip()
    if not query or len(query) < 2:
        return

    is_pm = (message.chat.type == enums.ChatType.PRIVATE)

    # 1. PM Auto-Delete: clean up user query after 5 minutes
    if is_pm:
        await _schedule_delete(bot, message, PM_SEARCH_USER_TTL)

    # 2. Rate limit check
    if _is_rate_limited(message.from_user.id):
        m = await message.reply("⏳ Please slow down! Wait a few seconds between searches.")
        await _schedule_delete(bot, m, 10)
        return

    # 3. User session check
    from client import User
    if User is None:
        m = await message.reply(
            "⚠️ <b>Search is currently unavailable.</b>\n"
            "<i>(User session not configured — set SESSION on Railway/Server)</i>"
        )
        await _schedule_delete(bot, m, 30)
        return
    if not getattr(User, "is_connected", False):
        m = await message.reply(
            "⚠️ <b>Search is temporarily reconnecting.</b>\n"
            "<i>Please wait a few seconds and try again.</i>"
        )
        await _schedule_delete(bot, m, 15)
        return

    # 4. Permissions & Channels Resolution
    if is_pm:
        is_subbed, unjoined, is_banned = await check_pm_fsub(bot, message.from_user.id)
        if is_banned:
            m = await message.reply("❌ You are banned from our updates channel.")
            await _schedule_delete(bot, m, 60)
            return

        if not is_subbed:
            kb = build_pm_fsub_markup(unjoined)
            lock_msg = await message.reply(
                "🔒 <b>Please join our updates channel(s) to search!</b>\n\n"
                "Due to copyright issues, you must join all our channels before searching for movies or series.\n\n"
                "Click the buttons below to join, then click <b>'✅ I have Joined'</b>.",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            await _schedule_delete(bot, lock_msg, 300)
            return

        channels = await _get_pm_sources(bot)
        if not channels:
            no_src = await message.reply(
                "📭 <b>No search sources configured yet.</b>\n\n"
                "Admin can add sources using <code>/addsource add -100xxxxxxxxxx</code> in PM."
            )
            await _schedule_delete(bot, no_src, 60)
            return
        ttl = PM_SEARCH_BOT_TTL
    else:
        if not await force_sub(bot, message):
            return
        group = await get_group(message.chat.id)
        if not group or not group.get("verified"):
            return
        channels = group.get("channels", [])
        if not channels:
            m = await message.reply("⚠️ No channels connected to this group yet. Use /connect to add one.")
            await _schedule_delete(bot, m, 30)
            return
        ttl = group.get("auto_delete", SEARCH_REPLY_TTL)

    # 5. Start search
    backup_link = await _get_backup_link()
    wait_msg = await message.reply("🔍 <i>Searching...</i>")
    results = await _search_channels(User, channels, query, backup_link)

    # Spell check correction fallback
    if not results:
        corrected = await google_spell_check(query)
        if corrected and corrected.lower() != query.lower():
            results = await _search_channels(User, channels, corrected, backup_link)
            if results:
                query = corrected

    # No results found
    if not results:
        imdb_hits = await search_imdb(query)
        imdb_text = ""
        if imdb_hits:
            imdb_text = "\n\n<b>Did you mean:</b>\n"
            imdb_text += "\n".join("• " + html.escape(h["title"]) for h in imdb_hits[:5])

        _cb_prefix = b"req_admin#"
        _cb_max = 64 - len(_cb_prefix)
        safe_query = query.encode("utf-8")[:_cb_max].decode("utf-8", errors="ignore")
        _google_url = "https://www.google.com/search?q=" + query.replace(" ", "+")
        _no_res_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Check Spelling on Google", url=_google_url)],
            [InlineKeyboardButton("📩 Request Here", callback_data=f"req_admin#{safe_query}")],
        ])

        no_res_footer = "\n\n<b>Please request the group admin 👇</b>" if not is_pm else "\n\n<b>Request content from the admin 👇</b>"
        await wait_msg.edit(
            "❌ <b>No results found for:</b> <i>" + html.escape(query) + "</i>"
            + imdb_text + no_res_footer,
            reply_markup=_no_res_kb,
        )
        await _schedule_delete(bot, wait_msg, 300 if is_pm else ttl)

        try:
            await record_failed_search(query, message.chat.id, message.chat.title or message.from_user.first_name or "")
        except Exception:
            pass

        if LOG_CHANNEL:
            try:
                user = message.from_user
                user_info = (
                    f"[{html.escape(user.first_name)}](tg://user?id={user.id})"
                    if user else "Unknown"
                )
                chat_title = message.chat.title or "PM Search"
                await bot.send_message(
                    chat_id=LOG_CHANNEL,
                    text=(
                        "#FailedSearch\n\n"
                        f"🔍 Query: <code>{html.escape(query)}</code>\n"
                        f"👤 User: {user_info} (<code>{user.id if user else 'N/A'}</code>)\n"
                        f"💬 Chat: <b>{html.escape(chat_title)}</b> (<code>{message.chat.id}</code>)"
                    ),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
        return

    # 6. Build and Post Results
    total = len(results)
    pages_data = [results[i:i + _RESULTS_PER_PAGE] for i in range(0, total, _RESULTS_PER_PAGE)]
    total_pages = len(pages_data)
    page_urls: list = []

    for pg_num, pg_results in enumerate(pages_data, 1):
        offset = (pg_num - 1) * _RESULTS_PER_PAGE
        text = _build_page_message(query, pg_results, total, pg_num, total_pages, offset, ttl, backup_link)
        try:
            sent = await _send_to_results_channel(bot, text)
            url = await _results_link(bot, RESULTS_CHANNEL, sent.id)
            page_urls.append(url)
            await _schedule_delete(bot, sent, ttl)
        except Exception as e:
            logger.error("Failed to send page %d to RESULTS_CHANNEL: %s", pg_num, e)
            if pg_num == 1:
                await wait_msg.edit(
                    "❌ <b>Failed to post results.</b>\n"
                    "<code>" + type(e).__name__ + ": " + html.escape(str(e)) + "</code>\n\n"
                    "ℹ️ Make sure the bot is admin in the results channel with "
                    "<b>Post Messages</b> permission."
                )
                return
            break

    if not page_urls:
        await wait_msg.edit("❌ <b>Failed to post results.</b> Please try again.")
        return

    actual_pages = len(page_urls)
    _prune_sessions()
    session_id = uuid.uuid4().hex[:8]
    _page_sessions[session_id] = {
        "urls": page_urls, "query": query, "total": total,
        "total_pages": actual_pages, "ttl": int(time()) + _SESSION_TTL, "reply_ttl": ttl,
    }

    mins_label = max(1, ttl // 60)
    reply_text = (
        "✅ <b>Found " + str(total) + " result" + ("s" if total != 1 else "") + " in "
        + "<a href=\"" + page_urls[0] + "\">Channel</a>.</b>\n"
        + "Page 1/" + str(actual_pages) + "\n"
        + "<i>(Results auto-delete in " + str(mins_label) + " min" + ("s" if mins_label != 1 else "") + ")</i>"
    )
    kb = _page_keyboard(session_id, 1, actual_pages, page_urls[0])
    await wait_msg.edit(reply_text, reply_markup=kb, disable_web_page_preview=True)
    await _schedule_delete(bot, wait_msg, ttl)

@Client.on_callback_query(filters.regex(r"^pg_[0-9a-f]{8}_\d+$"))
async def page_cb(bot, cb):
    parts = cb.data.split("_")
    session_id = parts[1]
    page = int(parts[2])
    session = _page_sessions.get(session_id)
    if not session:
        return await cb.answer("⌛ Session expired -- please search again.", show_alert=True)
    urls = session["urls"]
    total = session["total"]
    total_pages = session["total_pages"]
    query = session["query"]
    ttl = session.get("reply_ttl", SEARCH_REPLY_TTL)

    if page < 1 or page > total_pages:
        return await cb.answer("Invalid page.", show_alert=True)

    url = urls[page - 1]
    mins_label = max(1, ttl // 60)
    text = (
        "✅ <b>Found " + str(total) + " result" + ("s" if total != 1 else "") + " in "
        + "<a href=\"" + url + "\">Channel</a>.</b>\n"
        + "Page " + str(page) + "/" + str(total_pages) + "\n"
        + "<i>(Results auto-delete in " + str(mins_label) + " min" + ("s" if mins_label != 1 else "") + ")</i>"
    )
    kb = _page_keyboard(session_id, page, total_pages, url)
    try:
        await cb.message.edit(text, reply_markup=kb)
    except Exception:
        pass
    await cb.answer()

@Client.on_callback_query(filters.regex(r"^req_admin#"), group=-1)
async def request_to_admin(bot, cb):
    now = time()
    last = _req_cooldown.get(cb.from_user.id, 0)
    if now - last < 60:
        return await cb.answer(
            "⏳ Please wait 1 minute before sending another request.",
            show_alert=True,
        )
    _req_cooldown[cb.from_user.id] = now
    await cb.answer("✅ Your request has been sent to the admin!", show_alert=True)

    movie_name = cb.data.split("#", 1)[1] if "#" in cb.data else "Unknown"
    user = cb.from_user
    requester = f"@{user.username}" if user.username else (user.first_name or str(user.id))
    text = f"#RequestFromUser\n{movie_name}\n👤 {requester}"

    logger.info("REQUEST: movie=%r requester=%s OWNER_ID=%s LOG_CHANNEL=%s",
                movie_name, requester, OWNER_ID, LOG_CHANNEL)
    sent = False
    if OWNER_ID:
        try:
            await bot.send_message(chat_id=OWNER_ID, text=text)
            sent = True
        except Exception as e:
            logger.exception("REQUEST: failed to send to OWNER_ID=%s — %s", OWNER_ID, e)

    if LOG_CHANNEL:
        try:
            await bot.send_message(chat_id=LOG_CHANNEL, text=text)
            sent = True
        except Exception as e:
            logger.exception("REQUEST: failed to send to LOG_CHANNEL=%s — %s", LOG_CHANNEL, e)

    if not sent:
        try:
            await cb.message.reply_text(
                f"📩 <b>New Request</b>\n\n"
                f"🎬 {movie_name}\n"
                f"👤 {requester}\n\n"
                f"<i>(Admin PM not configured — set OWNER_ID env var)</i>",
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception:
            pass
