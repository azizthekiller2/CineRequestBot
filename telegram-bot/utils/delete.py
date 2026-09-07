"""
Auto-delete worker — runs as an asyncio task inside the main bot process.
Previously this was a subprocess with its own Pyrogram client (same bot token),
which caused AUTH_KEY_DUPLICATED and FloodWait on every restart.
Now it reuses the already-connected bot client passed from bot.py.
"""
import asyncio
import logging
from time import time
from pyrogram.errors import FloodWait

logger = logging.getLogger(__name__)


async def run_autodelete_loop(bot) -> None:
    """Continuously delete expired messages using the main bot client.
    Call this as an asyncio.create_task() from bot.py — no subprocess needed.
    Batches deletions by chat_id up to 100 messages per call to avoid FloodWait.
    """
    from database import get_all_dlt_data, delete_all_dlt_data

    logger.info("✅ Auto-delete loop started (in-process, batched)")
    while True:
        try:
            _time = int(time())
            all_data = await get_all_dlt_data(_time)
            if all_data:
                # Group message IDs by chat_id
                grouped = {}
                for data in all_data:
                    cid = data.get("chat_id")
                    mid = data.get("message_id")
                    if cid is not None and mid is not None:
                        grouped.setdefault(cid, []).append(mid)

                for cid, mids in grouped.items():
                    # Telegram permits up to 100 message IDs per delete_messages call
                    for i in range(0, len(mids), 100):
                        batch = mids[i:i + 100]
                        try:
                            await bot.delete_messages(chat_id=cid, message_ids=batch)
                        except FloodWait as e:
                            logger.warning("FloodWait in autodelete: sleeping %ds", e.value)
                            await asyncio.sleep(e.value + 1)
                        except Exception as e:
                            logger.debug("Delete batch error chat=%s: %s", cid, e)

                await delete_all_dlt_data(_time)
        except asyncio.CancelledError:
            logger.info("Auto-delete loop cancelled")
            return
        except Exception as e:
            logger.warning("Auto-delete loop error: %s", e)

        await asyncio.sleep(5)
