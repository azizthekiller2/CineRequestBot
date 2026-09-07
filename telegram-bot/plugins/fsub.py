import html
import logging
from config import LOG_CHANNEL, OWNER_ID, CHANNEL
from utils import get_group, update_group
from database.db import get_setting, set_setting, save_dlt_message
from pyrogram import Client, filters, enums
from pyrogram.errors import UserNotParticipant, FloodWait, ChatAdminRequired
from pyrogram.types import ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton
from time import time

logger = logging.getLogger(__name__)

# ── PM Force-Subscribe Helpers ────────────────────────────────────────────────

_pm_fsub_cache = {}  # ch_id -> {"title": str, "link": str, "ts": float}


async def _get_channel_meta(bot, ch):
    now = time()
    cached = _pm_fsub_cache.get(ch)
    if cached and (now - cached.get("ts", 0) < 600):
        return cached["title"], cached["link"]

    title = str(ch)
    link = ""
    try:
        chat = await bot.get_chat(ch)
        title = chat.title or str(ch)
        link = chat.invite_link
        if not link and chat.username:
            link = f"https://t.me/{chat.username}"
        if not link:
            try:
                link = await bot.export_chat_invite_link(ch)
            except Exception:
                cid_str = str(ch)
                if cid_str.startswith("-100"):
                    cid_str = cid_str[4:]
                link = f"https://t.me/c/{cid_str}"
    except Exception as e:
        logger.warning("Could not fetch invite link for fsub channel %s: %s", ch, e)
        cid_str = str(ch)
        if cid_str.startswith("-100"):
            cid_str = cid_str[4:]
        link = f"https://t.me/c/{cid_str}"

    _pm_fsub_cache[ch] = {"title": title, "link": link, "ts": now}
    return title, link


async def get_pm_fsub_channels() -> list:
    channels = await get_setting("pm_fsub_channels", None)
    if channels is not None:
        return channels
    fallback = []
    if CHANNEL:
        try:
            fallback.append(int(CHANNEL))
        except ValueError:
            fallback.append(CHANNEL)
    return fallback


async def check_pm_fsub(bot, user_id: int):
    channels = await get_pm_fsub_channels()
    if not channels:
        return True, [], False

    unjoined = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch, user_id)
            if member.status in (enums.ChatMemberStatus.BANNED, enums.ChatMemberStatus.RESTRICTED):
                return False, [], True
        except UserNotParticipant:
            title, link = await _get_channel_meta(bot, ch)
            unjoined.append({"id": ch, "title": title, "link": link})
        except FloodWait as e:
            logger.warning("FloodWait checking member %s: sleeping %ds", ch, e.value)
            await asyncio.sleep(e.value + 1)
        except Exception as e:
            logger.warning("Error checking chat member %s for user %s: %s", ch, user_id, e)

    if unjoined:
        return False, unjoined, False
    return True, [], False

def build_pm_fsub_markup(unjoined_channels: list) -> InlineKeyboardMarkup:
    buttons = []
    for idx, ch in enumerate(unjoined_channels, 1):
        title = ch.get("title", f"Channel {idx}")
        link = ch.get("link", "https://t.me/")
        buttons.append([InlineKeyboardButton(f"📢 Join {title[:25]}", url=link)])
    buttons.append([InlineKeyboardButton("✅ I have Joined", callback_data="pm_fsub_verify")])
    return InlineKeyboardMarkup(buttons)

# ── PM Force-Sub Callback (Verification) ───────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^pm_fsub_verify$"))
async def pm_fsub_verify_cb(bot, update):
    user_id = update.from_user.id
    is_joined, unjoined, is_banned = await check_pm_fsub(bot, user_id)
    if is_banned:
        return await update.answer("❌ You are banned from our channel.", show_alert=True)
    if not is_joined:
        return await update.answer("❌ You have not joined all channels yet! Please join and try again.", show_alert=True)

    await update.answer("✅ Verified!", show_alert=True)
    try:
        await update.message.delete()
    except Exception:
        pass
    try:
        prompt = await bot.send_message(
            chat_id=user_id,
            text="🎉 <b>Verification successful!</b>\n\nNow send me the name of the movie or series you want to search.",
        )
        await save_dlt_message(prompt, int(time()) + 120)
    except Exception:
        pass

# ── PM Admin /fsub Management ─────────────────────────────────────────────────

@Client.on_message(filters.private & filters.command(["fsub", "fsub_pm"]))
async def pm_fsub_admin_cmd(bot, message):
    if message.from_user.id != OWNER_ID:
        return await message.reply("❌ Only the bot owner can configure PM Force-Subscribe.")

    args = message.command[1:]
    if not args or args[0].lower() == "list":
        channels = await get_pm_fsub_channels()
        if not channels:
            return await message.reply(
                "📭 <b>No PM Force-Subscribe channels configured.</b>\n\n"
                "Use: <code>/fsub add -100xxxxxxxxxx</code> or <code>/fsub add @channelusername</code>"
            )
        lines = ["<b>📢 PM Force-Subscribe Channels:</b>\n"]
        buttons = []
        for i, ch_id in enumerate(channels, 1):
            try:
                chat = await bot.get_chat(ch_id)
                title = chat.title or str(ch_id)
            except Exception:
                title = str(ch_id)
            lines.append(f"{i}. <b>{html.escape(title)}</b> (<code>{ch_id}</code>)")
            buttons.append([
                InlineKeyboardButton(
                    f"❌ Remove: {title[:25]}",
                    callback_data=f"fsubpm_rm_{ch_id}",
                )
            ])
        return await message.reply(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    sub = args[0].lower()
    if sub == "add":
        if len(args) < 2:
            return await message.reply("Usage: <code>/fsub add -100xxxxxxxxxx</code> or <code>@username</code>")
        target = args[1]
        try:
            target = int(target)
        except ValueError:
            pass
        try:
            chat = await bot.get_chat(target)
            ch_id = chat.id
            title = chat.title or str(ch_id)
        except Exception as e:
            return await message.reply(
                f"❌ Could not access channel: <code>{html.escape(str(e))}</code>\n"
                "Make sure the bot is an <b>admin</b> in the channel with invite link permissions."
            )

        channels = list(await get_pm_fsub_channels())
        if ch_id in channels:
            return await message.reply("⚠️ That channel is already in PM Force-Subscribe list.")
        channels.append(ch_id)
        await set_setting("pm_fsub_channels", channels)
        _pm_fsub_cache.clear()
        return await message.reply(
            f"✅ Added PM Force-Subscribe channel: <b>{html.escape(title)}</b> (<code>{ch_id}</code>)\n"
            f"Total channels: <b>{len(channels)}</b>"
        )

    elif sub == "remove":
        if len(args) < 2:
            return await message.reply("Usage: <code>/fsub remove -100xxxxxxxxxx</code>")
        target = args[1]
        try:
            target = int(target)
        except ValueError:
            pass
        channels = list(await get_pm_fsub_channels())
        if target not in channels:
            return await message.reply("⚠️ Channel not found in PM Force-Subscribe list.")
        channels.remove(target)
        await set_setting("pm_fsub_channels", channels)
        _pm_fsub_cache.clear()
        return await message.reply(
            f"✅ Removed channel <code>{target}</code> from PM Force-Subscribe.\n"
            f"Remaining: <b>{len(channels)}</b>"
        )

    elif sub == "clear":
        await set_setting("pm_fsub_channels", [])
        _pm_fsub_cache.clear()
        return await message.reply("✅ Cleared all PM Force-Subscribe channels.")

    else:
        return await message.reply(
            "📋 <b>PM Force-Subscribe Usage:</b>\n"
            "• <code>/fsub add &lt;id or @username&gt;</code>\n"
            "• <code>/fsub remove &lt;id&gt;</code>\n"
            "• <code>/fsub list</code>\n"
            "• <code>/fsub clear</code>"
        )

@Client.on_callback_query(filters.regex(r"^fsubpm_rm_"))
async def pm_fsub_remove_cb(bot, update):
    if update.from_user.id != OWNER_ID:
        return await update.answer("Only the bot owner can do this.", show_alert=True)
    raw_target = update.data.replace("fsubpm_rm_", "")
    try:
        target = int(raw_target)
    except ValueError:
        target = raw_target

    channels = list(await get_pm_fsub_channels())
    if target in channels:
        channels.remove(target)
        await set_setting("pm_fsub_channels", channels)
        _pm_fsub_cache.clear()
    await update.answer("✅ Removed!", show_alert=True)

    if not channels:
        try:
            return await update.message.edit("📭 No PM Force-Subscribe channels remaining.")
        except Exception:
            return

    lines = ["<b>📢 PM Force-Subscribe Channels:</b>\n"]
    buttons = []
    for i, ch_id in enumerate(channels, 1):
        try:
            chat = await bot.get_chat(ch_id)
            title = chat.title or str(ch_id)
        except Exception:
            title = str(ch_id)
        lines.append(f"{i}. <b>{html.escape(title)}</b> (<code>{ch_id}</code>)")
        buttons.append([
            InlineKeyboardButton(
                f"❌ Remove: {title[:25]}",
                callback_data=f"fsubpm_rm_{ch_id}",
            )
        ])
    try:
        await update.message.edit(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception:
        pass

# ── Group Force-Subscribe Commands ────────────────────────────────────────────

@Client.on_message(filters.group & filters.command("fsub"))
async def f_sub_cmd(bot, message):
    if not message.from_user:
        return
    m = await message.reply("ᴘʟᴇᴀꜱᴇ ᴡᴀɪᴛ...")
    try:
        group     = await get_group(message.chat.id)
        user_id   = group["user_id"]
        user_name = group["user_name"]
        verified  = group["verified"]
    except Exception:
        return await bot.leave_chat(message.chat.id)

    if message.from_user.id != user_id:
        return await m.edit(f"Only {user_name} can use this command 😁")

    if not verified:
        return await m.edit("ᴛʜɪꜱ ᴄʜᴀᴛ ɪꜱ ɴᴏᴛ ᴠᴇʀɪꜰɪᴇᴅ 🚫\nᴜꜱᴇ /verify")

    try:
        f_sub = int(message.command[1])
    except Exception:
        return await m.edit("ɪɴᴄᴏʀʀᴇᴄᴛ ꜰᴏʀᴍᴀᴛ 🚫\nᴜꜱᴇ `/fsub` ᴄʜᴀɴɴᴇʟ ɪᴅ")

    try:
        chat       = await bot.get_chat(f_sub)
        group_chat = await bot.get_chat(message.chat.id)
        c_link     = chat.invite_link
        g_link     = group_chat.invite_link
    except Exception as e:
        text = (
            f"🚫 ᴇʀʀᴏʀ - `{str(e)}`\n\n"
            "ᴍᴀᴋᴇ ꜱᴜʀᴇ ᴛʜᴀᴛ ɪ ᴀᴍ ᴀᴅᴍɪɴ ɪɴ ᴄʜᴀɴɴᴇʟ ᴀɴᴅ ɢʀᴏᴜᴘ ᴡɪᴛʜ ᴀʟʟ ᴘᴇʀᴍɪꜱꜱɪᴏɴꜱ"
        )
        return await m.edit(text)

    await update_group(message.chat.id, {"f_sub": f_sub})
    await m.edit(
        f"ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ᴀᴛᴛᴀᴄʜᴇᴅ ꜰᴏʀᴄᴇꜱᴜʙ ᴛᴏ [{chat.title}]({c_link})!",
        disable_web_page_preview=True
    )
    text = (
        f"#NewFsub\n\n"
        f"User: {message.from_user.mention}\n"
        f"Group: [{group_chat.title}]({g_link})\n"
        f"Channel: [{chat.title}]({c_link})"
    )
    if LOG_CHANNEL:
        await bot.send_message(chat_id=LOG_CHANNEL, text=text)

@Client.on_message(filters.group & filters.command("nofsub"))
async def nf_sub_cmd(bot, message):
    if not message.from_user:
        return
    m = await message.reply("ᴅɪꜱᴀᴛᴛᴀᴄʜɪɴɢ...")
    try:
        group     = await get_group(message.chat.id)
        user_id   = group["user_id"]
        user_name = group["user_name"]
        verified  = group["verified"]
        f_sub     = group["f_sub"]
    except Exception:
        return await bot.leave_chat(message.chat.id)

    if message.from_user.id != user_id:
        return await m.edit(f"Only {user_name} can use this command 😁")

    if not verified:
        return await m.edit("ᴛʜɪꜱ ᴄʜᴀᴛ ɪꜱ ɴᴏᴛ ᴠᴇʀɪꜰɪᴇᴅ 🚫\nᴜꜱᴇ /verify")

    if not f_sub:
        return await m.edit("ᴛʜɪꜱ ᴄʜᴀᴛ ᴅᴏᴇꜱ ɴᴏᴛ ʜᴀᴠᴇ ᴀɴʏ ꜰᴏʀᴄᴇ ꜱᴜʙ\nᴜꜱᴇ /fsub")

    try:
        chat       = await bot.get_chat(f_sub)
        group_chat = await bot.get_chat(message.chat.id)
        c_link     = chat.invite_link
        g_link     = group_chat.invite_link
    except Exception as e:
        text = (
            f"🚫 ᴇʀʀᴏʀ - `{str(e)}`\n\n"
            "ᴍᴀᴋᴇ ꜱᴜʀᴇ ᴛʜᴀᴛ ɪ ᴀᴍ ᴀᴅᴍɪɴ ɪɴ ᴄʜᴀɴɴᴇʟ ᴀɴᴅ ɢʀᴏᴜᴘ ᴡɪᴛʜ ᴀʟʟ ᴘᴇʀᴍɪꜱꜱɪᴏɴꜱ"
        )
        return await m.edit(text)

    await update_group(message.chat.id, {"f_sub": False})
    await m.edit(
        f"ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ʀᴇᴍᴏᴠᴇᴅ ꜰᴏʀᴄᴇ ꜱᴜʙ ꜰʀᴏᴍ [{chat.title}]({c_link})",
        disable_web_page_preview=True
    )
    text = (
        f"#RemoveFsub\n\n"
        f"User: {message.from_user.mention}\n"
        f"Group: [{group_chat.title}]({g_link})\n"
        f"Channel: [{chat.title}]({c_link})"
    )
    if LOG_CHANNEL:
        await bot.send_message(chat_id=LOG_CHANNEL, text=text)

def _unrestrict_permissions() -> ChatPermissions:
    kwargs = dict(can_send_messages=True)
    for field in (
        "can_send_audios", "can_send_documents", "can_send_photos",
        "can_send_videos", "can_send_video_notes", "can_send_voice_notes",
        "can_send_polls", "can_send_other_messages", "can_add_web_page_previews",
        "can_change_info", "can_invite_users", "can_pin_messages",
        "can_send_media_messages",
    ):
        try:
            kwargs[field] = True
        except Exception:
            pass
    try:
        return ChatPermissions(**kwargs)
    except TypeError:
        return ChatPermissions(can_send_messages=True)

@Client.on_callback_query(filters.regex(r"^checksub"))
async def f_sub_callback(bot, update):
    user_id = int(update.data.split("_")[-1])
    group   = await get_group(update.message.chat.id)
    if not group:
        return
    f_sub = group.get("f_sub")
    if not f_sub:
        return await update.answer("Force-sub is no longer active.", show_alert=True)
    if update.from_user.id != user_id:
        return await update.answer("ᴛʜɪꜱ  ɪꜱ  ɴᴏᴛ  ꜰᴏʀ  ʏᴏᴜ  😊", show_alert=True)
    try:
        member = await bot.get_chat_member(f_sub, user_id)
        if member.status == enums.ChatMemberStatus.BANNED:
            return await update.answer(
                "ʏᴏᴜ ᴀʀᴇ ʙᴀɴɴᴇᴅ ꜰʀᴏᴍ ᴛʜᴇ ᴄʜᴀɴɴᴇʟ.", show_alert=True
            )
    except UserNotParticipant:
        return await update.answer(
            "ꜰɪʀꜱᴛ ᴊᴏɪɴ ᴜᴘᴅᴀᴛᴇ ᴄʜᴀɴɴᴇʟ ᴛʜᴇɴ ᴄʟɪᴄᴋ ᴏɴ ᴛʜɪꜱ ʙᴜᴛᴛᴏɴ",
            show_alert=True
        )
    except Exception:
        pass
    try:
        await bot.restrict_chat_member(
            chat_id=update.message.chat.id,
            user_id=user_id,
            permissions=_unrestrict_permissions(),
        )
    except Exception:
        pass
    await update.answer("✅ ᴡᴇʟᴄᴏᴍᴇ! ʏᴏᴜ ᴄᴀɴ ɴᴏᴡ ꜱᴇɴᴅ ᴍᴇꜱꜱᴀɢᴇꜱ.", show_alert=True)
    try:
        await update.message.delete()
    except Exception:
        pass
