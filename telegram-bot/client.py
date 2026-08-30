from config import API_ID, API_HASH, BOT_TOKEN, SESSION
from pyrogram import Client

# Only create User client if SESSION is configured.
# If SESSION is empty/missing, User is None and search will be disabled.
if SESSION:
    User = Client(
        name="user",
        session_string=SESSION,
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True,
        sleep_threshold=60,
    )
else:
    User = None
