
import io
import gc
import ctypes
from concurrent.futures import ThreadPoolExecutor

try:  # glibc: fewer malloc arenas + give freed memory back to the OS (big RAM saver with threads)
    _libc = ctypes.CDLL("libc.so.6")
    _libc.mallopt(-8, 2)  # M_ARENA_MAX
except Exception:
    _libc = None


def trim_memory():
    gc.collect()
    if _libc is not None:
        try:
            _libc.malloc_trim(0)
        except Exception:
            pass


import Gemini_Integration
from Gemini_Integration import run
from Moderation import BanNoticeView, OWNER_ID, is_owner, is_supermod
import Whitelist
import discord
import asyncio
import time
import base64
import typing
from discord.ext import commands
from discord import app_commands
# Only what the bot really uses. Intents.default() also caches voice states, scheduled events, etc.
intents = discord.Intents.none()
intents.guilds = True
intents.guild_messages = True
intents.dm_messages = True
intents.message_content = True
intents.emojis_and_stickers = True  # list_emojis
intents.members = True              # join/leave logs (enable Server Members Intent in the dev portal)
from discord import ui
bot = commands.Bot(
    command_prefix='.',
    intents=intents,
    help_command=None,
    max_messages=None,                                  # no message cache (replies are fetched on demand)
    member_cache_flags=discord.MemberCacheFlags.none(), # don't keep every member of every server in RAM
    chunk_guilds_at_startup=False,
)

_banned_inviter_task = None


async def _check_existing_guilds_for_banned_inviters():
    """Periodically eject servers whose bot-add audit entry belongs to an AI-banned user.

    This also catches bans applied after Gemcord joined the server.
    Requires View Audit Log permission in each guild to identify the inviter.
    """
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in list(bot.guilds):
            if Gemini_Integration.get_ban(guild.owner_id, guild.id):
                culprit = guild.owner_id
            else:
                culprit = None
                try:
                    async for entry in guild.audit_logs(
                        limit=100, action=discord.AuditLogAction.bot_add
                    ):
                        if (entry.target is not None and entry.target.id == bot.user.id
                                and entry.user is not None
                                and Gemini_Integration.get_ban(entry.user.id, guild.id)):
                            culprit = entry.user.id
                            break
                except (discord.Forbidden, discord.HTTPException) as exc:
                    print(f"[banned-inviter-check] Cannot inspect {guild.name} ({guild.id}) audit log: {exc}")
            if culprit is not None:
                print(f"[banned-inviter-check] Leaving {guild.name} ({guild.id}): added by AI-banned user {culprit}")
                try:
                    await guild.leave()
                except (discord.Forbidden, discord.HTTPException) as exc:
                    print(f"[banned-inviter-check] Failed to leave {guild.id}: {exc}")
        await asyncio.sleep(120)


@bot.event
async def on_ready():
    global _banned_inviter_task
    if _banned_inviter_task is None or _banned_inviter_task.done():
        _banned_inviter_task = asyncio.create_task(_check_existing_guilds_for_banned_inviters())
    for guild in bot.guilds:
        print(f"- {guild.name} (ID: {guild.id}, members: {guild.member_count}")
    await bot.tree.sync()
    print(f'We have logged in as {bot.user}')
    await bot.change_presence(
        status=discord.Status.do_not_disturb,
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{len(bot.guilds)} servers"
        )

    )
        
async def load_extensions():
    await bot.load_extension("Moderation")
    await bot.load_extension("Whitelist")
    await bot.load_extension("PartnerFeatures")
    await bot.load_extension("cogs.ban")

    await bot.load_extension("cogs.invite")
    await bot.load_extension("cogs.kick")
    await bot.load_extension("cogs.mute")

async def memory_janitor():
    while True:
        await asyncio.sleep(300)
        trim_memory()


async def main():
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=6, thread_name_prefix="gemcord"))
    async with bot:
        asyncio.create_task(memory_janitor())
        await load_extensions()
        await bot.start("MTU0NTgwMjk1Nzg4OTQ3MDU3NA.Gn6Tpc.x4hsh-GrCH-A2k0JJI6l3Ie6Cl8N-JNxS2DXMI")

@app_commands.allowed_contexts(
    guilds=True,
    dms=True,
    private_channels=True
)
@app_commands.allowed_installs(
    guilds=True,
    users=True
)

        
@bot.tree.command(
    name="model",
    description="Change Gemini's model i guess"
)

@app_commands.describe(model="Model name NOW")
async def changemodel(interaction: discord.Interaction, model: str):
    if not is_owner(interaction.user.id):
        return await interaction.response.send_message("fuck you", ephemeral=True)
    Gemini_Integration.model = model
    
    await interaction.response.send_message(f"`Changed to {model}`")
    
    
funny = {1089127632471339038, 1490752875012292710, 1256973579908681809}


def can_owner(user_id):
    """The hardcoded `funny` users, the bot owner and supermods."""
    return user_id in funny or is_owner(user_id)


@bot.command(name="partner")
async def partner(ctx, userid: int):
    """Grant partner settings access to a Discord user. Bot owner only."""
    if not is_owner(ctx.author.id):
        return await ctx.send("no.")
    import PartnerFeatures
    data = PartnerFeatures.read_settings()
    partners = {str(x) for x in data["partners"]}
    if str(userid) in partners:
        return await ctx.send(f"{userid} is already a partner.")
    data["partners"].append(str(userid))
    PartnerFeatures.write_settings(data)
    await ctx.send(f"Granted partner access to `{userid}`.")


@bot.command()
async def list(ctx):
    if not can_owner(ctx.author.id):
        await ctx.send("no.")
        return
    for guild in bot.guilds:
        print(f"- {guild.name} (ID: {guild.id}, members: {guild.member_count}")
        await ctx.send(f"{guild.name}")

@bot.command()
async def leave(ctx, guild_id: int):
    if not can_owner(ctx.author.id):
            await ctx.send("no.")
            return
    gulid = bot.get_guild(guild_id)
    if gulid == None:
        return await ctx.send("im not in here gng")

    await gulid.leave()
    await ctx.send("Left the server i guess")

@bot.command()
async def dm(ctx, userid: int,*, msg: str):
    if not can_owner(ctx.author.id):
            await ctx.send("no.")
            return
    user = await bot.fetch_user(userid)
    
    if user == None:
        return await ctx.send("didnt found")

    
    emoji = msg.replace("emoticon_wave", "<a:wavey:1552716572207087797>")
    await user.send(emoji)
    await ctx.send("done")


@bot.command()
async def fetch(ctx, guild: int):
    if not can_owner(ctx.author.id):
        return await ctx.send("no.")

    gjuild = bot.get_guild(guild)
    if gjuild is None:
        return await ctx.send("didn't find")

    # find the first text channel the bot can create invites in
    channel = next(
        (c for c in gjuild.text_channels
         if c.permissions_for(gjuild.me).create_instant_invite),
        None,
    )
    if channel is None:
        return await ctx.send(
            f"Guild name: {gjuild.name}\nMembers: {gjuild.member_count}\n"
            "invite: no channel where I can create one"
        )

    try:
        invite = await channel.create_invite(max_age=3600, max_uses=1)
    except discord.Forbidden:
        return await ctx.send("no permission to create an invite")

    await ctx.send(
        f"Guild name: {gjuild.name}\n"
        f"Members: {gjuild.member_count}\n"
        f"invite: {invite.url}"
    )
import traceback

import Discord_Actions

last_defense = {}  # channel id -> timestamp of last friend defense
DEFENSE_COOLDOWN = 15

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024


async def send_ban_notice(message, ban):
    view = BanNoticeView(ban, message.guild.name if message.guild else None)
    if message.guild is None or can_send(message.channel, message.guild.me):
        await message.reply(view=view)
    else:
        await message.author.send(view=view)


def can_send(channel, me):
    perms = channel.permissions_for(me)
    if isinstance(channel, discord.Thread):
        return perms.send_messages_in_threads
    return perms.view_channel and perms.send_messages


MAX_IMAGE_SIDE = 1536


MAX_IMAGE_PIXELS = 40_000_000  # a decoded image costs ~3 bytes/pixel, bigger ones could OOM a small host


def _shrink(mime, data):
    """Downscale big images before sending them to Gemini. Returns None for images too large to decode safely."""
    try:
        from PIL import Image
    except ImportError:
        return mime, data
    try:
        Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            if w * h > MAX_IMAGE_PIXELS:
                return None
            if max(w, h) <= MAX_IMAGE_SIDE and len(data) < 1_000_000:
                return mime, data
            if im.format == "JPEG":
                im.draft("RGB", (MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))  # decode at 1/2, 1/4 or 1/8 size
            im.seek(0)
            small = im.convert("RGB")
        small.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        buf = io.BytesIO()
        small.save(buf, "JPEG", quality=85)
        return "image/jpeg", buf.getvalue()
    except Exception:
        return mime, data


async def get_referenced(message):
    """The message this one replies to (fetched if it isn't cached), or None."""
    ref = message.reference
    if ref is None:
        return None
    if isinstance(ref.resolved, discord.Message):
        return ref.resolved
    if ref.message_id is None:
        return None
    try:
        return await message.channel.fetch_message(ref.message_id)
    except discord.HTTPException:
        return None


async def collect_images(message, ref=None):
    """Images from the message itself and from the message it replies to."""
    images = []
    for msg in (message, ref):
        if msg is None:
            continue
        for att in msg.attachments:
            ctype = (att.content_type or "").split(";")[0]
            if ctype not in IMAGE_TYPES or att.size > MAX_IMAGE_BYTES:
                continue
            if att.width and att.height and att.width * att.height > MAX_IMAGE_PIXELS:
                continue  # don't even download it
            data = await att.read()
            result = await asyncio.to_thread(_shrink, ctype, data)
            del data
            if result is not None:
                images.append(result)
                if len(images) >= MAX_IMAGES:
                    break
        if len(images) >= MAX_IMAGES:
            break
    if images:
        trim_memory()
    return images


def build_prompt(text, images, ref):
    if not text:
        if images:
            text = "look at this image and react to it"
        elif ref is not None:
            text = "(they replied to that message and pinged you without saying anything, react to it)"
        else:
            text = "(they pinged you without saying anything)"
    if ref is not None:
        # When someone replies to Gemcord itself, don't feed Gemcord's previous
        # answer back as if it were the new request. Some models simply echo the
        # quoted text instead of answering the user's actual message (e.g. "how are you?").
        # Keep the prior bot message only when the user sent no text, so image/reaction
        # and empty-ping behavior can still use the referenced message as context.
        is_bot_reply = getattr(getattr(ref, "author", None), "id", None) == getattr(bot.user, "id", None)
        if not (is_bot_reply and text.strip()):
            body = (ref.content or "").strip()[:500]
            extra = f" [+{len(ref.attachments)} attachment(s)]" if ref.attachments else ""
            text = f"(replying to {ref.author.display_name}'s message: {body!r}{extra})\n{text}"
    return text


async def react_instead(message, text, ref):
    """Can't talk in this channel: react with an emoji that matches what the bot would have said."""
    perms = message.channel.permissions_for(message.guild.me)
    if not (perms.add_reactions and perms.read_message_history):
        print("Pinged in", message.channel, "but can't talk or react")
        return
    images = await collect_images(message, ref)
    executor = Discord_Actions.make_executor(message, asyncio.get_running_loop(), images, ref=ref)
    try:
        await asyncio.to_thread(
            Gemini_Integration.run,
            build_prompt(text, images, ref),
            message.author.id,
            message.author.display_name,
            executor,
            images,
            "normal",
            message.guild.id,
            True,   # react_only
            False,  # no list_emojis needed
        )
    except Gemini_Integration.AllKeysRateLimited:
        pass
    except Exception:
        traceback.print_exc()
    if not executor.reacted:
        try:
            await message.add_reaction("👀")
        except discord.HTTPException:
            pass


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    await bot.process_commands(message)

    try:
        import PartnerFeatures
        if await PartnerFeatures.relay_partner_message(message):
            return
    except Exception:
        import traceback
        traceback.print_exc()

    gid = message.guild.id if message.guild else None

    # --- AI-banned users (global or this server): show the notice ONCE, then ignore them
    ban = Gemini_Integration.get_ban(message.author.id, gid)
    if ban:
        if bot.user in message.mentions and not ban.get("notified"):
            Gemini_Integration.mark_notified(message.author.id, True, ban.get("guild_id"))  # first, so spam can't double-send
            try:
                await send_ban_notice(message, ban)
            except Exception:
                traceback.print_exc()
                Gemini_Integration.mark_notified(message.author.id, False, ban.get("guild_id"))
        return

    # --- normal chat: bot was mentioned ---
    if bot.user.mentioned_in(message):
        # servers that aren't whitelisted get a few free requests, then the "trial ended" card
        if message.guild and not Whitelist.use_request(message.guild.id):
            await Whitelist.trial_ended(message)
            return
        text = (
            message.content
            .replace(f"<@{bot.user.id}>", "")
            .replace(f"<@!{bot.user.id}>", "")
            .strip()
        )
        ref = await get_referenced(message)

        # can't talk here? react instead of pinging from another channel
        if message.guild and not can_send(message.channel, message.guild.me):
            await react_instead(message, text, ref)
            return

        try:
            images = await collect_images(message, ref)
            prompt = build_prompt(text, images, ref)

            mode = Gemini_Integration.get_mode(gid)
            executor = (
                Discord_Actions.make_executor(
                    message, asyncio.get_running_loop(), images, ref=ref
                )
                if message.guild
                else None
            )
            print("Got mention:", repr(prompt), "| images:", len(images), "| mode:", mode)

            async with message.channel.typing():
                reply = await asyncio.to_thread(
                    Gemini_Integration.run,
                    prompt,
                    message.author.id,
                    message.author.display_name,
                    executor,
                    images,
                    mode,
                    gid,
                    False,
                    bool(message.guild and message.guild.emojis),
                )
            print("Gemini reply:", repr(reply))
            if reply:
                await message.reply(content=reply[:2000])
            elif not (executor and executor.reacted):
                await message.reply(content="...")
        except Gemini_Integration.AllKeysRateLimited:
            await message.reply(content="im out of brainpower right now (all my api keys are rate limited), try again in a minute")
        except Exception:
            traceback.print_exc()
            try:
                await message.reply(content="my brain broke, try again")
            except Exception:
                traceback.print_exc()
        return

    # --- friend defense: someone mentions/replies to a friend ---
    if message.guild is None or message.mention_everyone or not Whitelist.can_use(gid):
        return

    friend_ids = [
        m.id for m in message.mentions
        if Gemini_Integration.is_friend(m.id, gid) and m.id != message.author.id
    ]
    ref = message.reference.resolved if message.reference else None
    if (
        isinstance(ref, discord.Message)
        and Gemini_Integration.is_friend(ref.author.id, gid)
        and ref.author.id != message.author.id
        and ref.author.id not in friend_ids
    ):
        friend_ids.append(ref.author.id)

    if not friend_ids:
        return

    now = time.time()
    if now - last_defense.get(message.channel.id, 0) < DEFENSE_COOLDOWN:
        return
    last_defense[message.channel.id] = now

    try:
        reply = await asyncio.to_thread(
            Gemini_Integration.defend,
            message.content,
            message.author.id,
            message.author.display_name,
            friend_ids,
            gid,
        )
        if reply and can_send(message.channel, message.guild.me):
            await message.reply(content=reply[:2000])
    except Gemini_Integration.AllKeysRateLimited:
        pass
    except Exception:
        traceback.print_exc()


# ------------------------------------------------------------------ AI bans

def _parse_user_id(raw):
    try:
        return int((raw or "").strip().strip("<@!>"))
    except ValueError:
        return None


@bot.command(name="ban")
async def ai_ban(ctx, user_id: str = None, *, rest: str = ""):
    """.ban (userid) [days / perm] [reason]  -- bot owner only"""
    if not is_owner(ctx.author.id):
        return  # stay silent, '.' is a common prefix for other bots
    uid = _parse_user_id(user_id)
    if uid is None:
        return await ctx.send("usage: `.ban (userid) [days / perm] [reason]`")
    if uid in (OWNER_ID, bot.user.id) or is_supermod(uid):
        return await ctx.send("no.")
    try:
        days, reason = Gemini_Integration.parse_ban_args(rest)
    except ValueError as e:
        return await ctx.send(str(e))

    Gemini_Integration.ban_user(uid, days, reason)
    length = "permanently" if days is None else f"for {days:g} day(s)"
    await ctx.send(
        f"AI-banned `{uid}` {length}. reason: {reason}\n"
        "they'll get the notice once, the next time they ping me.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.command(name="unban")
async def ai_unban(ctx, user_id: str = None):
    """.unban (userid) -- bot owner only, lifts an AI ban"""
    if not is_owner(ctx.author.id):
        return
    uid = _parse_user_id(user_id)
    if uid is None:
        return await ctx.send("usage: `.unban (userid)`")
    if Gemini_Integration.unban_user(uid):
        await ctx.send(f"lifted the AI ban on `{uid}`")
    else:
        await ctx.send("that user isn't AI-banned")


@bot.event
async def on_guild_join(guild):
    """Banned servers (or ones re-adding the bot to reset their trial) get a card and the bot leaves.
    Banned users can't add the bot either: leave if one owns the server or is shown as the inviter."""
    if await Whitelist.handle_join(bot, guild):
        return
    culprit = guild.owner_id if Gemini_Integration.get_ban(guild.owner_id) else None
    if culprit is None:
        try:
            async for entry in guild.audit_logs(limit=100, action=discord.AuditLogAction.bot_add):
                if (
                    entry.target is not None
                    and entry.target.id == bot.user.id
                    and entry.user is not None
                    and Gemini_Integration.get_ban(entry.user.id)
                ):
                    culprit = entry.user.id
                    break
        except (discord.Forbidden, discord.HTTPException):
            pass  # no View Audit Log permission, can only check the owner
    if culprit is not None:
        print(f"Leaving {guild.name} ({guild.id}): added by AI-banned user {culprit}")
        await guild.leave()


# ------------------------------------------------------------------ /mode

CONFIRM_PHRASE = "im sure what im doing and i did a backup of my server"


class ConfirmModal(discord.ui.Modal, title="Enable DANGEROUS mode"):
    phrase = discord.ui.TextInput(
        label="Type the phrase exactly",
        placeholder=CONFIRM_PHRASE,
        max_length=100,
    )

    def __init__(self, guild):
        super().__init__()
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.guild.owner_id:
            await interaction.response.send_message("Only the server owner can do this.")
            return
        if self.phrase.value.strip().lower() != CONFIRM_PHRASE:
            await interaction.response.send_message("Wrong phrase. Nothing changed.")
            return

        Gemini_Integration.set_mode(self.guild.id, "dangerous")
        await interaction.response.send_message(
            f"DANGEROUS mode is now active in **{self.guild.name}**. "
            "Any admin can switch it back with `/mode normal`."
        )
        # let the server know
        try:
            ch = self.guild.system_channel
            if ch and can_send(ch, self.guild.me):
                await ch.send("⚠️ The owner enabled **DANGEROUS** mode for Gemcord. Behave.")
        except Exception:
            traceback.print_exc()


class ConfirmView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=300)
        self.guild = guild
        self.message = None

    @discord.ui.button(label="I understand, continue", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.guild.owner_id:
            await interaction.response.send_message("Only the server owner can do this.")
            return
        await interaction.response.send_modal(ConfirmModal(self.guild))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


MODE_CHOICES = [
    app_commands.Choice(name="normal", value="normal"),
    app_commands.Choice(name="annoying", value="annoying"),
    app_commands.Choice(name="dangerous", value="dangerous"),
]


@bot.tree.command(name="mode", description="Set how much power Gemcord has in this server")
@app_commands.describe(mode="normal, annoying (admins) or dangerous (owner only)")
@app_commands.choices(mode=MODE_CHOICES)
@app_commands.guild_only()
async def mode_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    guild = interaction.guild
    chosen = mode.value
    is_owner = interaction.user.id == guild.owner_id
    is_admin = interaction.user.guild_permissions.administrator

    if chosen == "dangerous":
        if not is_owner:
            await interaction.response.send_message(
                "Only the server owner can enable dangerous mode.", ephemeral=True
            )
            return
        if Gemini_Integration.get_mode(guild.id) == "dangerous":
            await interaction.response.send_message("Already in dangerous mode.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(
            title="⚠️ Enable DANGEROUS mode?",
            description=(
                f"You asked to give Gemcord dangerous powers in **{guild.name}**.\n\n"
                "**It will be able to:** change channel permissions, rename the server, change the "
                "server icon, timeout members for up to 28 days, kick, ban and unban.\n\n"
                "It is an AI and can make bad calls or be tricked by people in chat. "
                "Some of this is hard to undo."
            ),
            color=discord.Color.red(),
        )
        view = ConfirmView(guild)
        try:
            view.message = await interaction.user.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.followup.send(
                "I can't DM you. Enable DMs from server members and try again.", ephemeral=True
            )
            return
        await interaction.followup.send("Check your DMs for the confirmation.", ephemeral=True)
        return

    # normal / annoying
    if not is_admin:
        await interaction.response.send_message(
            "You need the Administrator permission to change the mode.", ephemeral=True
        )
        return
    Gemini_Integration.set_mode(guild.id, chosen)
    await interaction.response.send_message(f"Mode set to **{chosen}**.")
    
    
MAX_PROFILE_IMAGE_BYTES = 10 * 1024 * 1024
@bot.tree.command(
    name="serverprofile",
    description="Change Gemcord's avatar and/or banner in this server"
)


@app_commands.guild_only()
@app_commands.describe(
    avatar="New server-specific avatar (optional)",
    banner="New server-specific banner (optional)"
)
async def serverprofile(
    interaction: discord.Interaction,
    avatar: discord.Attachment = None,
    banner: discord.Attachment = None,
):
    # Bot owner and partners can set the profile for the current server.
    import PartnerFeatures
    if not PartnerFeatures.is_partner_or_owner(interaction.user.id):
        await interaction.response.send_message(
            "You need partner access to change the server profile.",
            ephemeral=True
        )
        return

    # Require at least one image.
    if avatar is None and banner is None:
        await interaction.response.send_message(
            "Upload an avatar, a banner, or both.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        edit_kwargs = {}

        for name, attachment in (
            ("avatar", avatar),
            ("banner", banner),
        ):
            if attachment is None:
                continue

            if attachment.size > MAX_PROFILE_IMAGE_BYTES:
                await interaction.followup.send(
                    f"The {name} must be 10 MB or smaller.",
                    ephemeral=True
                )
                return

            image_bytes = await attachment.read()

            # Verify that the upload is an actual supported image.
            import io

            try:
                from PIL import Image

                with Image.open(io.BytesIO(image_bytes)) as image:
                    if image.format not in {"PNG", "JPEG", "GIF", "WEBP"}:
                        raise ValueError("Unsupported image format")
                    image.verify()

            except ImportError:
                # discord.py will validate supported formats itself.
                pass
            except Exception:
                await interaction.followup.send(
                    f"That {name} isn't a valid PNG, JPEG, GIF, or WEBP image.",
                    ephemeral=True
                )
                return

            edit_kwargs[name] = image_bytes

        # Edit the bot's member profile in THIS server only.
        await interaction.guild.me.edit(**edit_kwargs)

        changed = []
        if avatar is not None:
            changed.append("avatar")
        if banner is not None:
            changed.append("banner")

        await interaction.followup.send(
            f"Successfully updated Gemcord's server-specific "
            f"{' and '.join(changed)} in **{interaction.guild.name}**! 👀",
            ephemeral=True
        )

    except discord.Forbidden:
        await interaction.followup.send(
            "Discord denied the profile update. Check your library version "
            "and whether this feature is available to your bot.",
            ephemeral=True
        )

    except discord.HTTPException as e:
        await interaction.followup.send(
            f"Discord rejected the update: {e}",
            ephemeral=True
        )

    except Exception:
        traceback.print_exc()
        await interaction.followup.send(
            "Something went wrong while updating the server profile.",
            ephemeral=True
        )

asyncio.run(main())
