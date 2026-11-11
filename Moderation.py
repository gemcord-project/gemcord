"""Gemcord moderation: /logs, message reports, AI bans (global + per server), log cards.

Needs discord.py 2.7+ (modal radio groups / selects) and the Server Members intent
(for join/leave logs). Loaded from MainBot with: await bot.load_extension("Moderation")
"""
import os
import json
import time
import traceback

import discord
from discord import app_commands
from discord.ext import commands

import Gemini_Integration as G

OWNER_ID = 1089127632471339038


class LiveJSON(dict):
    """A dict backed by a JSON file that re-reads itself whenever the file changes on disk.

    So editing the file by hand (e.g. whitelisted.json) takes effect right away, no restart needed.
    Call .refresh() before reading, .save() after changing. A half-written / invalid file is ignored
    (the in-memory data is kept), so a typo can never wipe the list.
    """

    def __init__(self, path, defaults=(), interval=1.0):
        super().__init__()
        self.path, self.defaults, self.interval = path, tuple(defaults), interval
        self._mtime = None
        self._checked = 0.0
        self.refresh(force=True)

    def _stat(self):
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return None

    def refresh(self, force=False):
        now = time.monotonic()
        if not force and now - self._checked < self.interval:  # don't stat the disk on every single message
            return
        self._checked = now
        mtime = self._stat()
        if force or mtime != self._mtime:
            self._mtime = mtime
            if mtime is not None:
                try:
                    with open(self.path) as f:
                        loaded = json.load(f)
                    if not isinstance(loaded, dict):
                        raise ValueError("top level must be an object")
                except (OSError, ValueError) as e:  # JSONDecodeError is a ValueError
                    print(f"[{self.path}] couldn't reload, keeping the data already in memory: {e}")
                else:
                    self.clear()
                    self.update(loaded)
        for key in self.defaults:
            self.setdefault(key, {})

    def save(self):
        G._save(self.path, dict(self))
        self._mtime = self._stat()  # our own write isn't a "change" we need to reload


# Supermods: users the bot owner gave full owner powers (.supermod). id -> {"by": ..., "at": ...}
supermods = LiveJSON("supermods.json")


def is_supermod(user_id):
    supermods.refresh()
    return str(user_id) in supermods


def is_owner(user_id):
    """The bot owner or a supermod. Use this for every 'owner only' check."""
    return user_id == OWNER_ID or is_supermod(user_id)

# Channel (in your support server) where "File a global ban" reports land.
# Leave as None to get them in your DMs instead.
GLOBAL_REPORT_CHANNEL_ID = None

LOG_CONFIG_FILE = "log_config.json"
REPORTS_FILE = "reports.json"
LOG_KINDS = {
    "join": "Join logs",
    "leave": "Leave logs",
    "report": "Report logs",
    "aiban": "AI-ban logs",
}
REPORT_COOLDOWN = 30

log_config = G._load(LOG_CONFIG_FILE)  # guild id -> {kind: channel id}
reports = G._load(REPORTS_FILE)        # report id -> record
_last_report = {}                      # reporter id -> timestamp
NO_PINGS = discord.AllowedMentions.none()


def save_log_config():
    G._save(LOG_CONFIG_FILE, log_config)


def save_reports():
    G._save(REPORTS_FILE, reports)


# ---------------------------------------------------------------- helpers

def can_send(channel, me):
    perms = channel.permissions_for(me)
    if isinstance(channel, discord.Thread):
        return perms.send_messages_in_threads
    return perms.view_channel and perms.send_messages


def is_staff(member):
    p = member.guild_permissions
    return p.administrator or p.manage_guild or p.moderate_members or p.ban_members


def length_text(days, bold=False):
    if days is None:
        text = "permanently"
    else:
        n = int(days) if float(days).is_integer() else days
        text = f"for {n} day{'' if n == 1 else 's'}"
    if bold:
        return text.replace("permanently", "**permanently**") if days is None else f"for **{text[4:]}**"
    return text


def clip(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def quote(text):
    return "\n".join("> " + line for line in text.splitlines()) if text else ""


# ---------------------------------------------------------------- ban notice

class BanNoticeView(discord.ui.LayoutView):
    """Components V2 'you've been banned' card (shown once per ban)."""

    def __init__(self, ban, guild_name=None):
        super().__init__()
        local = ban.get("guild_id") is not None
        dur = length_text(ban.get("days"), bold=True)
        if local:
            head = f"<:ban:1561708869946966016> You've been banned from using Gemcord in **{guild_name or 'this server'}** {dur}."
            rules = (
                "* Talking to the AI in this server\n"
                "* The AI no longer recognizes you in this server"
            )
        else:
            head = f"<:ban:1561708869946966016> You've been banned {dur}."
            rules = (
                "* Adding Gemcord in your server\n"
                "* Talking to the AI\n"
                "* The AI no longer recognizes you"
            )

        box = discord.ui.Container(accent_colour=discord.Colour.red())
        box.add_item(discord.ui.TextDisplay("# You've been banned"))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(f"{head}\n**What does this mean?** You will be restricted from:\n{rules}"))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(f"**Reason:** {ban.get('reason') or 'No reason provided'}"))
        self.add_item(box)


# ---------------------------------------------------------------- log cards

class LogView(discord.ui.LayoutView):
    def __init__(self, title, body, colour=None):
        super().__init__()
        box = discord.ui.Container(accent_colour=colour or discord.Colour.blurple())
        box.add_item(discord.ui.TextDisplay(f"## {title}"))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(body))
        box.add_item(discord.ui.TextDisplay(f"-# {discord.utils.format_dt(discord.utils.utcnow(), 'F')}"))
        self.add_item(box)


async def send_log(guild, kind, view):
    cid = log_config.get(str(guild.id), {}).get(kind)
    if not cid:
        return
    ch = guild.get_channel_or_thread(int(cid))
    if ch is None or not can_send(ch, guild.me):
        return
    try:
        await ch.send(view=view, allowed_mentions=NO_PINGS)
    except discord.HTTPException:
        traceback.print_exc()


def ban_log_view(guild, moderator_id, target_id, entry=None, global_scope=False):
    scope = "Global (all servers)" if global_scope else f"This server ({guild.name})"
    if entry is None:
        return LogView(
            "AI unban",
            f"**User:** <@{target_id}> (`{target_id}`)\n**Moderator:** <@{moderator_id}>\n**Scope:** {scope}",
            discord.Colour.green(),
        )
    return LogView(
        "AI ban",
        f"**User:** <@{target_id}> (`{target_id}`)\n**Moderator:** <@{moderator_id}>\n"
        f"**Scope:** {scope}\n**Duration:** {length_text(entry['days'])}\n**Reason:** {entry['reason']}",
        discord.Colour.red(),
    )


# ---------------------------------------------------------------- banning

def apply_ban(guild, moderator, target_id, global_scope, mode, days_text, reason):
    """Returns (entry, error). mode is 'perm' or 'temp'."""
    if target_id in (OWNER_ID, int(G.BOT_ID), guild.owner_id, moderator.id) or is_supermod(target_id):
        return None, "You can't ban that user."
    days = None
    if mode == "temp":
        try:
            days = float((days_text or "").strip())
        except ValueError:
            return None, "Temporary bans need a number of days."
        if not 0 < days <= G.MAX_BAN_DAYS:
            return None, f"Days must be between 0 and {G.MAX_BAN_DAYS}."
    reason = clip(reason, 500) or "No reason provided"
    entry = G.ban_user(
        target_id, days, reason, guild_id=None if global_scope else guild.id, by=moderator.id
    )
    return entry, None


class BanModal(discord.ui.Modal, title="Ban from Gemcord"):
    def __init__(self, target_id, guild, global_scope=False, report_view=None):
        super().__init__()
        self.target_id = int(target_id)
        self.guild = guild
        self.global_scope = global_scope
        self.report_view = report_view

        self.duration = discord.ui.Label(
            text="Duration",
            component=discord.ui.RadioGroup(options=[
                discord.RadioGroupOption(label="Permanently", value="perm", default=True),
                discord.RadioGroupOption(label="Temporary", value="temp"),
            ]),
        )
        self.days = discord.ui.Label(
            text="Days (temporary only)",
            component=discord.ui.TextInput(placeholder="e.g. 7", required=False, max_length=6),
        )
        self.reason = discord.ui.Label(
            text="Reason",
            component=discord.ui.TextInput(
                style=discord.TextStyle.paragraph, required=False, max_length=500,
                placeholder="Why are they being banned?",
            ),
        )
        for item in (self.duration, self.days, self.reason):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        entry, err = apply_ban(
            self.guild, interaction.user, self.target_id, self.global_scope,
            self.duration.component.value or "perm",
            self.days.component.value, self.reason.component.value,
        )
        if err:
            return await interaction.response.send_message(err, ephemeral=True)

        scope = "globally" if self.global_scope else "in this server"
        line = (f"Banned <@{self.target_id}> from Gemcord {scope} {length_text(entry['days'])} "
                f"by <@{interaction.user.id}>. Reason: {entry['reason']}")
        if self.report_view is not None:
            rec = self.report_view.rec
            rec["status"] = "banned"
            rec["log"].append(line)
            save_reports()
            await interaction.response.edit_message(view=ReportView(rec))
        else:
            await interaction.response.send_message(line, ephemeral=True, allowed_mentions=NO_PINGS)
        await send_log(self.guild, "aiban", ban_log_view(
            self.guild, interaction.user.id, self.target_id, entry, self.global_scope))


class UnbanView(discord.ui.View):
    def __init__(self, invoker_id, guild, target_id, global_scope):
        super().__init__(timeout=60)
        self.invoker_id, self.guild = invoker_id, guild
        self.target_id, self.global_scope = target_id, global_scope

    @discord.ui.button(label="Unban", style=discord.ButtonStyle.success)
    async def unban(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.invoker_id:
            return await interaction.response.send_message("Not for you.", ephemeral=True)
        G.unban_user(self.target_id, None if self.global_scope else self.guild.id)
        await interaction.response.edit_message(content=f"Lifted the AI ban on `{self.target_id}`.", view=None)
        await send_log(self.guild, "aiban", ban_log_view(
            self.guild, interaction.user.id, self.target_id, None, self.global_scope))


# ---------------------------------------------------------------- reports

class GlobalFileModal(discord.ui.Modal, title="File a global ban"):
    def __init__(self, report_view, moderator_id):
        super().__init__()
        self.report_view = report_view
        self.moderator = discord.ui.Label(
            text="Moderator ID",
            component=discord.ui.TextInput(default=str(moderator_id), max_length=20),
        )
        self.reason = discord.ui.Label(
            text="Reason",
            component=discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=500),
        )
        self.extra = discord.ui.Label(
            text="Extra info",
            component=discord.ui.TextInput(style=discord.TextStyle.paragraph, required=False, max_length=1000),
        )
        self.links = discord.ui.Label(
            text="Message links",
            description="One link per line",
            component=discord.ui.TextInput(style=discord.TextStyle.paragraph, required=False, max_length=1000),
        )
        for item in (self.moderator, self.reason, self.extra, self.links):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        mod_id = self.moderator.component.value.strip()
        if not mod_id.isdigit():
            return await interaction.response.send_message("Moderator ID must be a number.", ephemeral=True)
        await interaction.response.defer()

        rec = self.report_view.rec
        reason = self.reason.component.value.strip()
        body = (
            f"**Reported user:** <@{rec['author_id']}> (`{rec['author_id']}`)\n"
            f"**Filed by moderator:** <@{mod_id}> (`{mod_id}`)\n"
            f"**From server:** {interaction.guild.name} (`{interaction.guild.id}`)\n"
            f"**Reason:** {reason}\n"
            f"**Extra info:** {self.extra.component.value.strip() or 'none'}\n"
            f"**Message links:**\n{self.links.component.value.strip() or 'none'}\n"
            f"**Reported message:** {rec['jump_url']}\n\n"
            f"To approve: `.ban {rec['author_id']} perm {clip(reason, 200)}`"
        )
        view = LogView("Global ban request", body, discord.Colour.dark_red())
        try:
            if GLOBAL_REPORT_CHANNEL_ID:
                ch = interaction.client.get_channel(GLOBAL_REPORT_CHANNEL_ID) \
                    or await interaction.client.fetch_channel(GLOBAL_REPORT_CHANNEL_ID)
                await ch.send(view=view, allowed_mentions=NO_PINGS)
            else:
                owner = await interaction.client.fetch_user(OWNER_ID)
                await owner.send(view=view)
        except discord.HTTPException:
            traceback.print_exc()
            return await interaction.followup.send("Couldn't deliver the global ban request.", ephemeral=True)

        rec["status"] = "global_filed"
        rec["log"].append(f"Global ban filed by <@{mod_id}>.")
        save_reports()
        await interaction.edit_original_response(view=ReportView(rec))


STATUS_COLOURS = {
    "open": discord.Colour.orange(),
    "banned": discord.Colour.red(),
    "ignored": discord.Colour.light_grey(),
    "global_filed": discord.Colour.dark_red(),
}


class ReportView(discord.ui.LayoutView):
    """The report card: everything (text + buttons) lives inside one container."""

    def __init__(self, rec):
        super().__init__(timeout=None)
        self.rec = rec
        status = rec["status"]

        box = discord.ui.Container(accent_colour=STATUS_COLOURS.get(status))
        box.add_item(discord.ui.TextDisplay("## 🚩 Message report"))
        box.add_item(discord.ui.TextDisplay(
            f"**Reported:** <@{rec['author_id']}> (`{rec['author_id']}`)\n"
            f"**Reporter:** <@{rec['reporter_id']}>\n"
            f"**Where:** <#{rec['channel_id']}> · [jump to message]({rec['jump_url']})"
        ))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay(quote(rec["content"]) or "*no text*"))
        if rec["log"]:
            box.add_item(discord.ui.Separator())
            box.add_item(discord.ui.TextDisplay("\n".join(rec["log"])))

        buttons = []
        rid = rec["id"]
        if status == "open":
            ban = discord.ui.Button(label="Ban", style=discord.ButtonStyle.danger, custom_id=f"gc:rep:ban:{rid}")
            ban.callback = self.on_ban
            ignore = discord.ui.Button(label="Ignore", style=discord.ButtonStyle.secondary, custom_id=f"gc:rep:ignore:{rid}")
            ignore.callback = self.on_ignore
            buttons += [ban, ignore]
        if status in ("open", "banned"):
            glob = discord.ui.Button(label="File a global ban", style=discord.ButtonStyle.primary, custom_id=f"gc:rep:global:{rid}")
            glob.callback = self.on_global
            buttons.append(glob)
        if buttons:
            box.add_item(discord.ui.Separator())
            box.add_item(discord.ui.ActionRow(*buttons))
        self.add_item(box)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member) and is_staff(interaction.user):
            return True
        await interaction.response.send_message("Only staff can use these buttons.", ephemeral=True)
        return False

    async def on_ban(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            BanModal(self.rec["author_id"], interaction.guild, global_scope=False, report_view=self)
        )

    async def on_ignore(self, interaction: discord.Interaction):
        self.rec["status"] = "ignored"
        self.rec["log"].append(f"Ignored by <@{interaction.user.id}>.")
        save_reports()
        await interaction.response.edit_message(view=ReportView(self.rec))

    async def on_global(self, interaction: discord.Interaction):
        await interaction.response.send_modal(GlobalFileModal(self, interaction.user.id))


# ---------------------------------------------------------------- /logs

class LogsModal(discord.ui.Modal, title="Gemcord logs"):
    def __init__(self, guild):
        super().__init__()
        self.guild = guild
        cfg = log_config.get(str(guild.id), {})
        self.selects = {}
        for kind, label in LOG_KINDS.items():
            ch = guild.get_channel(int(cfg[kind])) if cfg.get(kind) else None
            select = discord.ui.ChannelSelect(
                channel_types=[discord.ChannelType.text, discord.ChannelType.news],
                placeholder="Search for a channel",
                min_values=0, max_values=1, required=False,
                default_values=[ch] if ch is not None else [],
            )
            self.selects[kind] = select
            self.add_item(discord.ui.Label(text=label, description="Leave empty to turn off", component=select))

    async def on_submit(self, interaction: discord.Interaction):
        cfg, lines = {}, []
        for kind, select in self.selects.items():
            if select.values:
                ch = self.guild.get_channel(select.values[0].id)
                cfg[kind] = select.values[0].id
                warn = "" if ch is not None and can_send(ch, self.guild.me) else " ⚠️ I can't send there yet"
                lines.append(f"**{LOG_KINDS[kind]}:** <#{select.values[0].id}>{warn}")
            else:
                lines.append(f"**{LOG_KINDS[kind]}:** off")
        log_config[str(self.guild.id)] = cfg
        save_log_config()
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------- cog

class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        guild_only = dict(
            allowed_contexts=app_commands.AppCommandContext(guild=True),
            allowed_installs=app_commands.AppInstallationType(guild=True),
        )
        self.report_menu = app_commands.ContextMenu(
            name="Report to Gemcord", callback=self.report_message, **guild_only)
        self.ban_menu = app_commands.ContextMenu(
            name="AI Ban", callback=self.ai_ban_user, **guild_only)

    async def cog_load(self):
        self.bot.tree.add_command(self.report_menu)
        self.bot.tree.add_command(self.ban_menu)

    async def cog_unload(self):
        self.bot.tree.remove_command(self.report_menu.name, type=self.report_menu.type)
        self.bot.tree.remove_command(self.ban_menu.name, type=self.ban_menu.type)

    # ---- /logs
    @app_commands.command(name="logs", description="Choose where Gemcord posts join, leave, report and AI-ban logs")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def logs(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need Manage Server for this.", ephemeral=True)
        await interaction.response.send_modal(LogsModal(interaction.guild))

    # ---- message context menu: report
    async def report_message(self, interaction: discord.Interaction, message: discord.Message):
        guild = interaction.guild
        cid = log_config.get(str(guild.id), {}).get("report")
        ch = guild.get_channel_or_thread(int(cid)) if cid else None
        if ch is None:
            return await interaction.response.send_message(
                "This server has no report channel yet. An admin can set one with `/logs`.", ephemeral=True)
        if message.author.id in (self.bot.user.id, interaction.user.id):
            return await interaction.response.send_message("You can't report that message.", ephemeral=True)
        if any(r["guild_id"] == guild.id and r["message_id"] == message.id and r["status"] == "open"
               for r in reports.values()):
            return await interaction.response.send_message("That message is already reported.", ephemeral=True)
        now = time.time()
        if now - _last_report.get(interaction.user.id, 0) < REPORT_COOLDOWN:
            return await interaction.response.send_message("Slow down a bit before reporting again.", ephemeral=True)
        if not can_send(ch, guild.me):
            return await interaction.response.send_message("I can't post in the report channel.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        _last_report[interaction.user.id] = now
        content = clip(message.content, 500)
        if message.attachments:
            content = (content + "\n" if content else "") + f"[{len(message.attachments)} attachment(s)]"
        rec = {
            "id": str(int(now * 1000)),
            "guild_id": guild.id,
            "channel_id": message.channel.id,
            "message_id": message.id,
            "jump_url": message.jump_url,
            "author_id": message.author.id,
            "reporter_id": interaction.user.id,
            "content": content,
            "status": "open",
            "log": [],
            "created": now,
        }
        card = await ch.send(view=ReportView(rec), allowed_mentions=NO_PINGS)
        rec["card_channel_id"], rec["card_message_id"] = card.channel.id, card.id
        reports[rec["id"]] = rec
        save_reports()
        await interaction.followup.send("Report sent to the staff, thanks.", ephemeral=True)

    # ---- user context menu: AI ban
    async def ai_ban_user(self, interaction: discord.Interaction, user: discord.User):
        guild = interaction.guild
        owner = is_owner(interaction.user.id)
        if not (owner or is_staff(interaction.user)):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        if user.id in (OWNER_ID, int(G.BOT_ID), guild.owner_id, interaction.user.id) or is_supermod(user.id):
            return await interaction.response.send_message("You can't ban that user.", ephemeral=True)

        global_scope = owner  # the bot owner bans everywhere, staff only for this server
        existing = G.get_ban(user.id, None if global_scope else guild.id)
        if existing is not None and (global_scope or existing.get("guild_id") == guild.id):
            return await interaction.response.send_message(
                f"<@{user.id}> is already AI-banned ({length_text(existing['days'])}). Lift it?",
                view=UnbanView(interaction.user.id, guild, user.id, global_scope),
                ephemeral=True, allowed_mentions=NO_PINGS,
            )
        await interaction.response.send_modal(BanModal(user.id, guild, global_scope))

    # ---- .supermod (real bot owner only, so a supermod can't mint more supermods)
    @commands.command(name="supermod")
    async def supermod_cmd(self, ctx, user_id: str = None, action: str = None):
        """.supermod [userID] gives owner commands, .supermod [userID] remove takes them back."""
        if ctx.author.id != OWNER_ID:
            return
        supermods.refresh()
        if user_id is None:
            ids = ", ".join(f"`{u}`" for u in supermods) or "nobody yet"
            return await ctx.send(f"usage: `.supermod [userID]` (add `remove` to revoke)\nsupermods: {ids}")
        try:
            uid = int(user_id.strip().strip("<@!>"))
        except ValueError:
            return await ctx.send("that's not a valid user id")
        if uid in (OWNER_ID, self.bot.user.id):
            return await ctx.send("no.")

        if (action or "").lower() in ("remove", "revoke", "off", "delete"):
            if supermods.pop(str(uid), None) is None:
                return await ctx.send("that user isn't a supermod")
            supermods.save()
            return await ctx.send(f"`{uid}` is no longer a supermod")

        if str(uid) in supermods:
            return await ctx.send(f"`{uid}` is already a supermod (`.supermod {uid} remove` to revoke)")
        try:
            name = f" ({(await self.bot.fetch_user(uid)).name})"
        except discord.HTTPException:
            return await ctx.send("couldn't find a user with that id")
        supermods[str(uid)] = {"by": ctx.author.id, "at": time.time()}
        supermods.save()
        await ctx.send(f"`{uid}`{name} is now a supermod, they can use all owner commands",
                       allowed_mentions=NO_PINGS)

    # ---- member logs
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await send_log(member.guild, "join", LogView(
            "Member joined",
            f"{member.mention} (`{member.id}`)\n"
            f"**Account created:** {discord.utils.format_dt(member.created_at, 'R')}\n"
            f"**Members:** {member.guild.member_count}",
            discord.Colour.green(),
        ))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        joined = discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "unknown"
        await send_log(member.guild, "leave", LogView(
            "Member left",
            f"{member.mention} (`{member.id}`)\n**Joined:** {joined}\n**Members:** {member.guild.member_count}",
            discord.Colour.orange(),
        ))


async def setup(bot):
    await bot.add_cog(Moderation(bot))
    # keep report buttons working after a restart
    for rec in reports.values():
        if rec["status"] in ("open", "banned") and rec.get("card_message_id"):
            bot.add_view(ReportView(rec), message_id=rec["card_message_id"])
