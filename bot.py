import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
import discord
from discord.ext import commands


TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("DISCORD_TOKEN environment variable is not set.")

IMAGE_EXEMPT_ROLE_IDS = [
    1542512535188803645,
]

TRIGGER_FILE = "triggers.txt"
WEBHOOK_NAME = "RelayBotHook"
MAX_MESSAGE_LEN = 2000   # Discord's hard limit per message
MAX_QUOTE_LEN = 20       # how much of the replied-to message to preview
NO_MENTIONS = discord.AllowedMentions.none()  # relayed text can never ping anyone
REPLY_MARK = "\u21a9"    # small arrow used to flag the reply-indicator line

# Matches "pzk <anything>" so the text after "pzk" can be forwarded on its
# own. Case-insensitive; DOTALL so a multi-line message is captured whole.
PZK_PREFIX_RE = re.compile(r"^pzk\s+(.+)$", re.IGNORECASE | re.DOTALL)

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
GIF_LINK_DOMAINS = ("tenor.com", "giphy.com")  # Discord's built-in GIF picker links here

if not os.path.exists(TRIGGER_FILE):
    with open(TRIGGER_FILE, "w", encoding="utf-8") as f:
        f.write("keyword1\nkeyword2\nalert\n")


def load_triggers():
    with open(TRIGGER_FILE, "r", encoding="utf-8") as f:
        return [line.strip().lower() for line in f if line.strip()]


def compile_trigger_regex(words):
    """Match each trigger word only as a whole word, e.g. 'hell' fires on
    'what the hell' but not on 'hello' or 'shell'. Returns None if there
    are no trigger words to match."""
    if not words:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b")


TRIGGER_WORDS = load_triggers()
TRIGGER_REGEX = compile_trigger_regex(TRIGGER_WORDS)


def refresh_trigger_regex():
    global TRIGGER_REGEX
    TRIGGER_REGEX = compile_trigger_regex(TRIGGER_WORDS)


def matched_triggers(content_lower: str) -> list[str]:
    """Every trigger word present in the message as a whole word."""
    if TRIGGER_REGEX is None:
        return []
    return TRIGGER_REGEX.findall(content_lower)


def has_image_or_gif(message: discord.Message) -> bool:
    """True for an uploaded image/gif attachment (caption or not - an
    attachment is an unambiguous signal), or a message that is nothing but
    a direct image/gif link or a Tenor/Giphy GIF link (Discord's built-in
    GIF picker sends these as plain links). A link mixed into a longer
    sentence does NOT count, so a message with real commentary isn't
    auto-deleted just because it happens to contain a gif link."""
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            return True
        if att.filename.lower().endswith(IMAGE_EXTENSIONS):
            return True

    content = message.content.strip().lower()
    if content.startswith("http://") or content.startswith("https://"):
        if content.endswith(IMAGE_EXTENSIONS):
            return True
        if any(domain in content for domain in GIF_LINK_DOMAINS):
            return True

    return False

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# dest_channel_id -> webhook (avoids an API call on every relay)
webhook_cache: dict[int, discord.Webhook] = {}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
async def get_webhook(dest_channel) -> discord.Webhook:
    cached = webhook_cache.get(dest_channel.id)
    if cached:
        return cached
    webhooks = await dest_channel.webhooks()
    # Discord only includes a token for webhooks this bot itself created -
    # a same-named webhook made some other way (manually, an older bot
    # instance, etc.) comes back tokenless and can never be sent through,
    # so skip it rather than cache something unusable.
    webhook = discord.utils.find(
        lambda w: w.name == WEBHOOK_NAME and w.token is not None, webhooks
    )
    if webhook is None:
        webhook = await dest_channel.create_webhook(name=WEBHOOK_NAME)
    webhook_cache[dest_channel.id] = webhook
    return webhook


def split_text(text: str) -> list[str]:
    chunks = [text[i:i + MAX_MESSAGE_LEN] for i in range(0, len(text), MAX_MESSAGE_LEN)]
    return chunks or [""]


async def resolve_reference(message: discord.Message):
    """Return the message that `message` replies to, or None if there is no
    reply, or if the original can no longer be fetched (e.g. deleted)."""
    ref = message.reference
    if not ref or not ref.message_id:
        return None
    if isinstance(ref.resolved, discord.Message):
        return ref.resolved
    try:
        return await message.channel.fetch_message(ref.message_id)
    except discord.HTTPException:
        return None


def strip_reply_indicator(text: str) -> str:
    """Drop a leading reply-indicator line. A relayed message already
    carries its own indicator line, so without this, quoting an
    already-relayed reply stacks indicators on top of each other."""
    lines = text.splitlines()
    if lines and (lines[0].startswith(REPLY_MARK) or lines[0].startswith(f"[{REPLY_MARK}]")):
        remainder = "\n".join(lines[1:]).strip()
        return remainder if remainder else text.strip()
    return text.strip()


def build_reply_indicator(ref_msg) -> str:
    """One compact line showing who a relayed message was replying to and a
    short preview of what they said. Webhooks can't carry Discord's native
    reply bar (that's only possible from a real bot/user account, and it
    can't also impersonate the original author's name/avatar), so this is
    the closest equivalent that keeps the real name and pfp on the relay."""
    if ref_msg is None:
        return f"{REPLY_MARK} Replying to a message (original unavailable)\n"

    text = strip_reply_indicator(ref_msg.content)
    text = " ".join(text.split())  # collapse newlines so it stays one line
    if not text:
        text = "[Attachment]" if ref_msg.attachments else "[Media/Embed]"
    if len(text) > MAX_QUOTE_LEN:
        text = text[:MAX_QUOTE_LEN] + "..."

    return f"[{REPLY_MARK}]({ref_msg.jump_url}) <@{ref_msg.author.id}>: {text}\n"


async def get_reply_indicator(msg: discord.Message):
    """If msg is itself a reply, return (indicator_line, allowed_mentions)
    that show and ping who it was replying to. Otherwise ("", NO_MENTIONS).
    Shared by all three relay paths so a pzk'd (or pzk-prefixed) message
    that was itself a reply still shows it."""
    if not (msg.reference and msg.reference.message_id):
        return "", NO_MENTIONS

    ref_msg = await resolve_reference(msg)
    indicator = build_reply_indicator(ref_msg)
    if ref_msg is None:
        return indicator, NO_MENTIONS

    # Only the person being replied to can be pinged here - not
    # @everyone/@here, not roles, and not anyone else mentioned in the text.
    mentions = discord.AllowedMentions(everyone=False, roles=False, users=[ref_msg.author])
    return indicator, mentions


async def send_via_webhook(dest_channel, chunks, files, author, allowed_mentions=NO_MENTIONS):
    for attempt in range(2):
        webhook = await get_webhook(dest_channel)
        try:
            for i, chunk in enumerate(chunks):
                is_last = i == len(chunks) - 1
                await webhook.send(
                    content=chunk or discord.utils.MISSING,
                    username=author.display_name[:80],
                    avatar_url=author.display_avatar.url,
                    allowed_mentions=allowed_mentions,
                    files=files if (is_last and files) else discord.utils.MISSING,
                )
            return
        except discord.NotFound:
            # Cached webhook was deleted -> drop it and retry once with a fresh one
            webhook_cache.pop(dest_channel.id, None)
            if attempt == 1:
                raise


async def dispatch_relay(target_message, dest_id: int, reply_indicator: str = "", allowed_mentions=None) -> bool:
    """Relay a message via webhook, impersonating target_message's author's
    name and avatar. Returns True only if it was actually delivered, so
    callers never delete an original that failed to relay."""
    allowed_mentions = allowed_mentions or NO_MENTIONS
    text = f"{reply_indicator}{target_message.content}".strip()
    attachments = target_message.attachments
    fallback_urls = "\n".join(a.url for a in attachments)

    # Re-upload attachments so they survive the original message being deleted
    files = []
    try:
        files = [await a.to_file() for a in attachments]
    except discord.HTTPException:
        files = []
        text = f"{text}\n{fallback_urls}".strip()

    if not text and not files:
        text = "[Attachment / Media Only]"

    try:
        dest_channel = bot.get_channel(dest_id) or await bot.fetch_channel(dest_id)
        try:
            await send_via_webhook(dest_channel, split_text(text), files, target_message.author, allowed_mentions)
        except discord.Forbidden:
            raise
        except discord.HTTPException:
            if not files:
                raise
            # e.g. file too large for the destination -> fall back to links
            text = f"{text}\n{fallback_urls}".strip()
            await send_via_webhook(dest_channel, split_text(text), [], target_message.author, allowed_mentions)
        print(f"Relayed message to channel {dest_id}")
        return True
    except discord.Forbidden:
        print(f"ERROR: Missing 'Manage Webhooks' permission in channel {dest_id}.")
    except Exception as e:
        print(f"ERROR dispatching to channel {dest_id}: {e}")
    return False


async def safe_delete(msg: discord.Message):
    try:
        await msg.delete()
    except discord.Forbidden:
        print(f"ERROR: Lacking 'Manage Messages' permission in {msg.channel.id}.")
    except discord.NotFound:
        pass


def is_mod(member: discord.Member) -> bool:
    """Same permission the addword/delword commands already require."""
    return isinstance(member, discord.Member) and member.guild_permissions.manage_messages


def has_image_exempt_role(member) -> bool:
    """True if the member holds any role listed in IMAGE_EXEMPT_ROLE_IDS.
    Used to skip the automatic image/gif relay for those members."""
    if not isinstance(member, discord.Member):
        return False
    return any(role.id in IMAGE_EXEMPT_ROLE_IDS for role in member.roles)


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}!")
    print(f"Active trigger words: {', '.join(TRIGGER_WORDS)}")
    print("Keyword/image triggers are active in every channel.")
    if IMAGE_EXEMPT_ROLE_IDS:
        print(f"Image/gif relay is skipped for role IDs: {', '.join(str(r) for r in IMAGE_EXEMPT_ROLE_IDS)}")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("\u26d4 You need the Manage Messages permission to use this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Usage: !{ctx.command.name} <word>")
    elif isinstance(error, commands.CommandNotFound):
        return
    else:
        print(f"Command error in '{ctx.command}': {error}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    # Real bot commands are handled as commands and never relayed
    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.invoke(ctx)
        return

    content_lower = message.content.strip().lower()

    # Manual relay via "pzk <text>" (works in ANY channel) - deletes this
    # message and relays just <text>, as if the user had posted that alone.
    pzk_prefix_match = PZK_PREFIX_RE.match(message.content.strip())
    if pzk_prefix_match:
        forwarded_text = pzk_prefix_match.group(1).strip()
        if forwarded_text:
            forwarded = SimpleNamespace(
                content=forwarded_text,
                attachments=message.attachments,
                author=message.author,
            )
            # If this "pzk ..." message was itself a reply, carry that along too
            reply_indicator, ping_mentions = await get_reply_indicator(message)
            if await dispatch_relay(forwarded, message.channel.id, reply_indicator=reply_indicator, allowed_mentions=ping_mentions):
                await safe_delete(message)
        return

    # Manual relay via "pzk" reply (works in ANY channel)
    if content_lower == "pzk" and message.reference and message.reference.message_id:
        target_msg = await resolve_reference(message)
        if target_msg is None:
            print("ERROR: could not fetch the message replied to with 'pzk'.")
            return

        if target_msg.author.id != message.author.id and not is_mod(message.author):
            await message.channel.send(
                f"\u26d4 {message.author.mention} you can only `pzk` your own messages.",
                delete_after=6,
            )
            return

        # If the pzk'd message was itself a reply, carry that context along
        reply_indicator, ping_mentions = await get_reply_indicator(target_msg)

        if await dispatch_relay(target_msg, message.channel.id, reply_indicator=reply_indicator, allowed_mentions=ping_mentions):
            await safe_delete(message)
            await safe_delete(target_msg)
        return

    # Automated keyword/image trigger - works in ANY channel, same as "pzk",
    # relaying back into the channel it was posted in. Whole-word match, so
    # a trigger word like "hell" won't fire on "hello" or "shell".
    #
    # Members with a role listed in IMAGE_EXEMPT_ROLE_IDS are skipped for the
    # image/gif trigger only - their images are left as normal messages.
    keyword_hit = bool(matched_triggers(content_lower))
    image_hit = has_image_or_gif(message) and not has_image_exempt_role(message.author)

    if keyword_hit or image_hit:
        reply_indicator, ping_mentions = await get_reply_indicator(message)
        if await dispatch_relay(message, message.channel.id, reply_indicator=reply_indicator, allowed_mentions=ping_mentions):
            await safe_delete(message)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
@bot.command()
@commands.has_permissions(manage_messages=True)
async def addword(ctx, word: str):
    word = word.lower()
    if word in TRIGGER_WORDS:
        await ctx.send(f"\u26a0\ufe0f {word} is already in the trigger list.")
        return

    TRIGGER_WORDS.append(word)
    with open(TRIGGER_FILE, "a", encoding="utf-8") as f:
        f.write(f"{word}\n")
    refresh_trigger_regex()
    await ctx.send(f"\u2705 Added {word} to the trigger list!")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def delword(ctx, word: str):
    word = word.lower()
    if word not in TRIGGER_WORDS:
        await ctx.send(f"\u26a0\ufe0f {word} was not found in the trigger list.")
        return

    TRIGGER_WORDS.remove(word)
    with open(TRIGGER_FILE, "w", encoding="utf-8") as f:
        for w in TRIGGER_WORDS:
            f.write(f"{w}\n")
    refresh_trigger_regex()
    await ctx.send(f"\U0001f5d1\ufe0f Removed {word} from the trigger list!")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def listwords(ctx):
    if not TRIGGER_WORDS:
        await ctx.send("The trigger list is currently empty.")
        return
    words_str = ", ".join(f"{word}" for word in TRIGGER_WORDS)
    await ctx.send(f"\U0001f4cb Current Trigger Words: {words_str}")


# --------------------------------------------------------------------------
# Tiny health-check server (Render Web Service support)
# --------------------------------------------------------------------------
# A Discord bot never opens its own port, but Render's free Web Service
# tier requires something listening on $PORT or it marks the deploy
# unhealthy. This has no effect if you deploy as a Background Worker
# instead, which needs no open port at all.
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass  # keep Render's logs free of per-request noise


def _run_health_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), _HealthHandler).serve_forever()


threading.Thread(target=_run_health_server, daemon=True).start()
bot.run(TOKEN)
