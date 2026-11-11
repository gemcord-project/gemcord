"""Server whitelist for Gemcord.

- Non-whitelisted servers get TRIAL_REQUESTS AI requests, then a "trial ended" card (the bot stays).
- `.ticket` (bot owner) posts a panel with a "Whitelist me!" button -> modal -> review card for the owner.
- Everything lives in whitelisted.json: {"guilds": {...}, "applications": {...}, "trials": {...}, "pardoned": {...}}
  The file is re-read automatically when it changes, so hand edits work without restarting the bot.
- Server bans (.serverban / .usb, and the automatic "rejoined to reset the trial" ban) live in server_bans.json.

Loaded from MainBot with: await bot.load_extension("Whitelist")
"""
import re
import time
import itertools
import traceback

import discord
from discord.ext import commands

import Gemini_Integration as G
from Moderation import LiveJSON, LogView, NO_PINGS, OWNER_ID, can_send, clip, is_owner

WHITELIST_FILE = "whitelisted.json"
SERVER_BANS_FILE = "server_bans.json"
TRIAL_REQUESTS = 5
SUPPORT_INVITE = "https://discord.gg/aJjxFWUQPK"
# Where application review cards go. None = DM the bot owner.
APPLICATIONS_CHANNEL_ID = None
NOTICE_COOLDOWN = 60  # seconds between "trial ended" cards per server

data = LiveJSON(WHITELIST_FILE, ("guilds", "applications", "trials", "pardoned"))
server_bans = LiveJSON(SERVER_BANS_FILE)  # guild id -> {"at", "reason", "by", "name", "auto"}
_last_notice = {}

INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9-]+)", re.I)


def save():
    data.save()


# ---------------------------------------------------------------- whitelist / trial logic

def is_whitelisted(guild_id):
    data.refresh()  # picks up manual edits to whitelisted.json without a restart
    return str(guild_id) in data["guilds"]


def can_use(guild_id):
    """True if the server is whitelisted or still has trial requests left (doesn't consume one)."""
    if is_server_banned(guild_id):
        return False
    return is_whitelisted(guild_id) or data["trials"].get(str(guild_id), 0) < TRIAL_REQUESTS


def use_request(guild_id):
    """Consume one request. False means the trial is over and the server isn't whitelisted."""
    if is_server_banned(guild_id):
        return False
    if is_whitelisted(guild_id):
        return True
    used = data["trials"].get(str(guild_id), 0)
    if used >= TRIAL_REQUESTS:
        return False
    data["trials"][str(guild_id)] = used + 1
    save()
    return True


def whitelist_guild(guild_id, **info):
    data.refresh()
    data["guilds"][str(guild_id)] = {"at": time.time(), **info}
    save()


def unwhitelist_guild(guild_id):
    data.refresh()
    removed = data["guilds"].pop(str(guild_id), None)
    save()
    return removed is not None


# ---------------------------------------------------------------- server bans

def is_server_banned(guild_id):
    server_bans.refresh()
    return str(guild_id) in server_bans


def get_server_ban(guild_id):
    server_bans.refresh()
    return server_bans.get(str(guild_id))


def ban_server(guild_id, reason, by=None, name=None, auto=False):
    """Permanently ban a server from adding Gemcord. Also removes it from the whitelist."""
    server_bans.refresh()
    data.refresh()
    entry = {"at": time.time(), "reason": reason, "by": by, "name": name, "auto": auto}
    server_bans[str(guild_id)] = entry
    data["guilds"].pop(str(guild_id), None)
    data["pardoned"].pop(str(guild_id), None)
    server_bans.save()
    save()
    return entry


def unban_server(guild_id):
    """Lift a server ban. Returns False if the server wasn't banned.

    The server is pardoned once: its next join won't be treated as a "rejoin to reset the trial",
    but whatever trial requests it already used stay used.
    """
    server_bans.refresh()
    if server_bans.pop(str(guild_id), None) is None:
        return False
    data.refresh()
    data["pardoned"][str(guild_id)] = time.time()
    server_bans.save()
    save()
    return True


def is_rejoin_bypass(guild_id):
    """A non-whitelisted server that already burned trial requests and is now adding the bot again."""
    if is_whitelisted(guild_id):
        return False
    return data["trials"].get(str(guild_id), 0) > 0


class ServerBannedView(discord.ui.LayoutView):
    """Components V2 card posted in a server right before Gemcord leaves it for good."""

    def __init__(self, ban, guild_name=None):
        super().__init__()
        box = discord.ui.Container(accent_colour=discord.Colour.red())
        box.add_item(discord.ui.TextDisplay("# <:ban:1561708869946966016> Server banned"))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(
            f"**{clip(guild_name, 100) or 'This server'}** has been **permanently** banned from adding Gemcord.\n"
            "**What does this mean?** You will be restricted from:\n"
            "* Adding Gemcord to this server\n"
            "* Using Gemcord in this server\n"
            "Gemcord is leaving this server now."
        ))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(f"**Reason:** {ban.get('reason') or 'No reason provided'}"))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.ActionRow(
            discord.ui.Button(label="Appeal", style=discord.ButtonStyle.link, url=SUPPORT_INVITE)
        ))
        self.add_item(box)


async def eject(bot, guild, view):
    """Post the card (a channel first, the server owner's DMs as a fallback), then leave."""
    me = guild.me
    channels = itertools.chain([guild.system_channel], guild.text_channels)
    sent = False
    for ch in itertools.islice((c for c in channels if c is not None), 6):
        if me is not None and not can_send(ch, me):
            continue
        try:
            await ch.send(view=view)
            sent = True
            break
        except discord.HTTPException:
            continue
    if not sent:
        try:
            await (await bot.fetch_user(guild.owner_id)).send(view=view)
        except discord.HTTPException:
            pass
    try:
        await guild.leave()
    except discord.HTTPException:
        traceback.print_exc()


async def handle_join(bot, guild):
    """Call from on_guild_join. Returns True if the bot just left because the server is banned."""
    ban = get_server_ban(guild.id)
    if ban is None:
        data.refresh()
        if data["pardoned"].pop(str(guild.id), None) is not None:
            save()  # unbanned earlier: this join is allowed
        elif is_rejoin_bypass(guild.id):
            ban = ban_server(guild.id, "Tried to bypass the request limit by re-adding Gemcord",
                             name=guild.name, auto=True)
            print(f"Auto-banned {guild.name} ({guild.id}): rejoined after using its trial requests")
            try:
                owner = await bot.fetch_user(OWNER_ID)
                await owner.send(view=LogView(
                    "Server auto-banned",
                    f"**Server:** {guild.name} (`{guild.id}`)\n**Owner:** <@{guild.owner_id}>\n"
                    f"**Why:** re-added Gemcord after using its trial requests\n"
                    f"Lift it with `.usb {guild.id}`",
                    discord.Colour.red()))
            except discord.HTTPException:
                pass
    if ban is None:
        return False
    await eject(bot, guild, ServerBannedView(ban, guild.name))
    return True


# ---------------------------------------------------------------- trial ended card

class TrialEndedView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__()
        box = discord.ui.Container(accent_colour=discord.Colour.orange())
        box.add_item(discord.ui.TextDisplay("# <:limit:1561709184393682954> Trial ended"))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(
            f"This server has used its **{TRIAL_REQUESTS} requests**, so the trial is over.\n"
            "To keep using Gemcord here, the server needs to get **whitelisted**.\n"
            "Join our server and whitelist here its completely free."
        ))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.ActionRow(
            discord.ui.Button(label="Get whitelisted", style=discord.ButtonStyle.link, url=SUPPORT_INVITE)
        ))
        self.add_item(box)


async def trial_ended(message):
    """Tell the server the trial is over (at most once a minute per server). Falls back to a lock reaction."""
    gid = message.guild.id
    now = time.time()
    if now - _last_notice.get(gid, 0) < NOTICE_COOLDOWN:
        return
    _last_notice[gid] = now
    try:
        if can_send(message.channel, message.guild.me):
            await message.reply(view=TrialEndedView(), mention_author=False)
        else:
            await message.add_reaction("🔒")
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------- application flow

class ApplyModal(discord.ui.Modal, title="Whitelist request"):
    def __init__(self):
        super().__init__()
        self.age = discord.ui.Label(
            text="Age", component=discord.ui.TextInput(max_length=3, placeholder="e.g. 16"))
        self.link = discord.ui.Label(
            text="Server link",
            description="A permanent invite to your server",
            component=discord.ui.TextInput(max_length=100, placeholder="https://discord.gg/..."))
        self.members = discord.ui.Label(
            text="Members", component=discord.ui.TextInput(max_length=10, placeholder="e.g. 150"))
        self.reason = discord.ui.Label(
            text="Reason why you should be whitelisted",
            component=discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=1000))
        for item in (self.age, self.link, self.members, self.reason):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        age = self.age.component.value.strip()
        members = self.members.component.value.strip()
        if not age.isdigit() or not members.isdigit():
            return await interaction.response.send_message(
                "Age and Members must be numbers.", ephemeral=True)
        m = INVITE_RE.search(self.link.component.value)
        if not m:
            return await interaction.response.send_message(
                "That doesn't look like a Discord invite link.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        try:
            invite = await interaction.client.fetch_invite(m.group(1), with_counts=True)
        except discord.HTTPException:
            return await interaction.followup.send("That invite is invalid or expired.", ephemeral=True)
        if invite.guild is None:
            return await interaction.followup.send("That invite isn't for a server.", ephemeral=True)

        gid = invite.guild.id
        if is_server_banned(gid):
            return await interaction.followup.send("That server is banned from Gemcord.", ephemeral=True)
        if is_whitelisted(gid):
            return await interaction.followup.send("That server is already whitelisted.", ephemeral=True)
        if any(a["guild_id"] == gid and a["status"] == "pending" for a in data["applications"].values()):
            return await interaction.followup.send("That server already has a pending request.", ephemeral=True)

        now = time.time()
        rec = {
            "id": str(int(now * 1000)),
            "user_id": interaction.user.id,
            "age": age,
            "invite": f"https://discord.gg/{m.group(1)}",
            "guild_id": gid,
            "guild_name": invite.guild.name,
            "members": members,
            "actual_members": invite.approximate_member_count,
            "reason": self.reason.component.value.strip(),
            "bot_in_guild": interaction.client.get_guild(gid) is not None,
            "status": "pending",
            "created": now,
        }
        view = ApplicationView(rec)
        try:
            if APPLICATIONS_CHANNEL_ID:
                ch = interaction.client.get_channel(APPLICATIONS_CHANNEL_ID) \
                    or await interaction.client.fetch_channel(APPLICATIONS_CHANNEL_ID)
                card = await ch.send(view=view, allowed_mentions=NO_PINGS)
            else:
                card = await (await interaction.client.fetch_user(OWNER_ID)).send(view=view)
        except discord.HTTPException:
            traceback.print_exc()
            return await interaction.followup.send("Couldn't deliver your request, try again later.", ephemeral=True)

        rec["card_message_id"] = card.id
        data["applications"][rec["id"]] = rec
        save()
        await interaction.followup.send(
            "Your request was sent! You'll get a DM when it's reviewed.", ephemeral=True)


STATUS_COLOURS = {
    "pending": discord.Colour.blurple(),
    "approved": discord.Colour.green(),
    "denied": discord.Colour.red(),
}


class ApplicationView(discord.ui.LayoutView):
    """Review card for the bot owner."""

    def __init__(self, rec):
        super().__init__(timeout=None)
        self.rec = rec
        actual = f" (invite says **{rec['actual_members']}**)" if rec.get("actual_members") else ""
        in_server = "yes" if rec.get("bot_in_guild") else "**no**, not added yet"

        box = discord.ui.Container(accent_colour=STATUS_COLOURS.get(rec["status"]))
        box.add_item(discord.ui.TextDisplay("## Whitelist request"))
        box.add_item(discord.ui.TextDisplay(
            f"**Applicant:** <@{rec['user_id']}> (`{rec['user_id']}`)\n"
            f"**Age:** {rec['age']}\n"
            f"**Server:** {rec['guild_name']} (`{rec['guild_id']}`)\n"
            f"**Link:** {rec['invite']}\n"
            f"**Members:** {rec['members']}{actual}\n"
            f"**Gemcord in server:** {in_server}"
        ))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(f"**Reason:**\n{clip(rec['reason'], 1000)}"))
        if rec["status"] == "pending":
            box.add_item(discord.ui.Separator())
            ok = discord.ui.Button(label="Approve", style=discord.ButtonStyle.success,
                                   custom_id=f"gc:wl:approve:{rec['id']}")
            ok.callback = self.on_approve
            no = discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger,
                                   custom_id=f"gc:wl:deny:{rec['id']}")
            no.callback = self.on_deny
            box.add_item(discord.ui.ActionRow(ok, no))
        else:
            box.add_item(discord.ui.Separator())
            box.add_item(discord.ui.TextDisplay(
                f"**{rec['status'].capitalize()}** by <@{rec.get('reviewer')}>"))
        self.add_item(box)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_owner(interaction.user.id):
            return True
        await interaction.response.send_message("Only the bot owner can review requests.", ephemeral=True)
        return False

    async def _finish(self, interaction, status, title, body, colour):
        self.rec["status"] = status
        self.rec["reviewer"] = interaction.user.id
        save()
        await interaction.response.edit_message(view=ApplicationView(self.rec))
        try:  # tell the applicant
            user = await interaction.client.fetch_user(self.rec["user_id"])
            await user.send(view=LogView(title, body, colour))
        except discord.HTTPException:
            pass

    async def on_approve(self, interaction: discord.Interaction):
        if is_server_banned(self.rec["guild_id"]):
            return await interaction.response.send_message(
                "That server is server-banned. `.usb` it first if you want to approve it.", ephemeral=True)
        whitelist_guild(self.rec["guild_id"], name=self.rec["guild_name"],
                        user_id=self.rec["user_id"], approved_by=interaction.user.id)
        await self._finish(
            interaction, "approved", "Whitelist approved",
            f"**{self.rec['guild_name']}** is now whitelisted. Add Gemcord to the server if you haven't yet!",
            discord.Colour.green())

    async def on_deny(self, interaction: discord.Interaction):
        await self._finish(
            interaction, "denied", "Whitelist denied",
            f"Your request for **{self.rec['guild_name']}** was denied.", discord.Colour.red())


# ---------------------------------------------------------------- ticket panel

class TicketView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        box = discord.ui.Container(accent_colour=discord.Colour.blurple())
        box.add_item(discord.ui.TextDisplay("# <:limit:1561709184393682954> Whitelist your server"))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(
            "Due to high usage of gemcord, we officially phased from public to whitelist only.\n"
            "Servers that are not verified can only do 5 requests before being prompted here."
        ))
        box.add_item(discord.ui.Separator())
        btn = discord.ui.Button(label="Whitelist me!", style=discord.ButtonStyle.secondary,
                                custom_id="gc:ticket:apply",
                                emoji=discord.PartialEmoji(name="whitelist", id=1561708868369780737),
                                )
        btn.callback = self.on_apply
        box.add_item(discord.ui.ActionRow(btn))
        self.add_item(box)

    async def on_apply(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ApplyModal())


# ---------------------------------------------------------------- cog

class Whitelist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="ticket")
    async def ticket(self, ctx):
        """Bot owner only: post the whitelist panel in this channel."""
        if not is_owner(ctx.author.id):
            return
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        await ctx.send(view=TicketView())

    @commands.command(name="whitelist")
    async def whitelist_cmd(self, ctx, guild_id: str = None):
        """Bot owner only: .whitelist [server id], defaults to this server."""
        if not is_owner(ctx.author.id):
            return
        gid = int(guild_id) if guild_id and guild_id.isdigit() else (ctx.guild.id if ctx.guild else None)
        if gid is None:
            return await ctx.send("usage: `.whitelist [server id]`")
        if is_server_banned(gid):
            return await ctx.send(f"`{gid}` is server-banned, lift it with `.usb {gid}` first")
        guild = self.bot.get_guild(gid)
        whitelist_guild(gid, name=guild.name if guild else None, approved_by=ctx.author.id)
        await ctx.send(f"whitelisted `{gid}`" + (f" ({guild.name})" if guild else ""))

    @commands.command(name="unwhitelist")
    async def unwhitelist_cmd(self, ctx, guild_id: str = None):
        """Bot owner only: .unwhitelist [server id], defaults to this server."""
        if not is_owner(ctx.author.id):
            return
        gid = int(guild_id) if guild_id and guild_id.isdigit() else (ctx.guild.id if ctx.guild else None)
        if gid is None:
            return await ctx.send("usage: `.unwhitelist [server id]`")
        await ctx.send(f"removed `{gid}` from the whitelist" if unwhitelist_guild(gid)
                       else "that server isn't whitelisted")


    # ---- server bans
    @commands.command(name="serverban", aliases=["sban", "sb"])
    async def serverban_cmd(self, ctx, guild_id: str = None, *, reason: str = ""):
        """.serverban [serverID] [reason]: permanently ban a server from adding Gemcord (and leave it)."""
        if not is_owner(ctx.author.id):
            return
        if not guild_id or not guild_id.isdigit():
            return await ctx.send("usage: `.serverban [serverID] [reason]`")
        gid = int(guild_id)
        if is_server_banned(gid):
            return await ctx.send(f"`{gid}` is already server-banned")
        guild = self.bot.get_guild(gid)
        entry = ban_server(gid, clip(reason, 500) or "No reason provided",
                           by=ctx.author.id, name=guild.name if guild else None)
        await ctx.send(
            f"server-banned `{gid}`" + (f" ({guild.name})" if guild else "")
            + (", leaving it now" if guild else ", im not in it so they just can't add me"),
            allowed_mentions=NO_PINGS)
        if guild:  # last, in case this command was run inside that very server
            await eject(self.bot, guild, ServerBannedView(entry, guild.name))

    @commands.command(name="usb", aliases=["unserverban", "unsban"])
    async def usb_cmd(self, ctx, guild_id: str = None):
        """.usb [serverID]: lift a server ban."""
        if not is_owner(ctx.author.id):
            return
        if not guild_id or not guild_id.isdigit():
            return await ctx.send("usage: `.usb [serverID]`")
        gid = int(guild_id)
        if unban_server(gid):
            await ctx.send(f"lifted the server ban on `{gid}`, they can add me again")
        else:
            await ctx.send("that server isn't banned")

    @commands.Cog.listener()
    async def on_ready(self):
        # a banned server may have re-added the bot while it was offline
        for guild in self.bot.guilds:
            if is_server_banned(guild.id):
                await handle_join(self.bot, guild)


async def setup(bot):
    await bot.add_cog(Whitelist(bot))
    # keep the panel button and pending review cards working after a restart
    bot.add_view(TicketView())
    for rec in data["applications"].values():
        if rec["status"] == "pending" and rec.get("card_message_id"):
            bot.add_view(ApplicationView(rec), message_id=rec["card_message_id"])
