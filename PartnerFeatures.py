"""Partner server settings for Gemcord."""
import json
import os
import discord
from discord import app_commands
from discord.ext import commands
import Gemini_Integration as G
from Moderation import OWNER_ID

SETTINGS_FILE = "partner_settings.json"
DEFAULT_MODELS = (
    "gemini-3.5-flash", "gemini-3.5-flash-lite",
    "gemini-3.1-flash", "gemini-3.1-flash-lite",
)
GEMINI_EMOJI = discord.PartialEmoji(name="GeminiIcon", id=1562975823391232031)
CAPPUCINO_MODEL = "Cappucino-AI-B1"
CAPPUCINO_EMOJI = discord.PartialEmoji(name="Cappucino", id=1562985358969282611)

# Guild IDs whose partner message relay mode is enabled. Persisted in partner_settings.json.


def read_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("partners", [])
            data.setdefault("servers", {})
            return data
    except (OSError, ValueError):
        pass
    return {"partners": [], "servers": {}}


def write_settings(data):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def is_partner(user_id):
    return str(user_id) in {str(x) for x in read_settings()["partners"]}


def is_partner_or_owner(user_id):
    return int(user_id) == OWNER_ID or is_partner(user_id)


def guild_settings(guild_id):
    return read_settings()["servers"].get(str(guild_id), {})


def available_models(api_key=None):
    """Fetch Gemini models that support content generation; default key is restricted."""
    if not api_key:
        return list(DEFAULT_MODELS)
    try:
        client = G._client(api_key)
        names = []
        for item in client.models.list():
            name = (getattr(item, "name", "") or "").removeprefix("models/")
            actions = getattr(item, "supported_actions", None)
            if name.startswith("gemini-") and (not actions or "generateContent" in actions):
                names.append(name)
        return sorted(set(names)) or list(DEFAULT_MODELS)
    except Exception:
        return list(DEFAULT_MODELS)


class ApiKeyModal(discord.ui.Modal, title="Gemcord partner settings · 1/2"):
    """Collect the key first, then open a second modal with the model selector."""
    def __init__(self, guild_id):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        current = guild_settings(guild_id)
        self.api_key = discord.ui.TextInput(
            label="Gemini API key (blank = default)",
            placeholder="Paste your key, or leave blank for Gemcord default",
            required=False, max_length=500, style=discord.TextStyle.short,
            default=(current.get("api_key") or "").strip(),
        )
        self.add_item(self.api_key)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not is_partner_or_owner(interaction.user.id):
            return await interaction.response.send_message("Partner access required.", ephemeral=True)
        key = self.api_key.value.strip()
        current = guild_settings(self.guild_id)
        # Discord does not permit opening a modal directly as the response to a
        # modal-submit interaction. This ephemeral button is the supported bridge;
        # clicking it opens the model-selection modal immediately.
        await interaction.response.send_message(
            "API key received. Continue to choose your model.",
            ephemeral=True,
            view=ContinueToModelView(
                self.guild_id, interaction.user.id, key,
                current.get("model", DEFAULT_MODELS[0]),
            ),
        )


class ContinueToModelView(discord.ui.View):
    def __init__(self, guild_id, user_id, api_key, selected_model):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        self.user_id = int(user_id)
        self.api_key_value = api_key
        self.selected_model = selected_model

    @discord.ui.button(label="Continue to model selection", style=discord.ButtonStyle.primary)
    async def continue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id or not is_partner_or_owner(interaction.user.id):
            return await interaction.response.send_message(
                "Only the partner who opened these settings can continue.", ephemeral=True
            )
        await interaction.response.send_modal(ModelSelectModal(
            self.guild_id, self.api_key_value, self.selected_model
        ))


class ModelSelectModal(discord.ui.Modal, title="Choose your AI model · 2/2"):
    def __init__(self, guild_id, api_key, selected_model):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        self.api_key_value = api_key
        models = available_models(api_key or None) if api_key else list(DEFAULT_MODELS)
        # Discord select menus allow at most 25 options; reserve one for Cappucino-AI-B1.
        models = [name for name in models if name != CAPPUCINO_MODEL][:24]
        if not models:
            models = list(DEFAULT_MODELS)
        options = [
            discord.SelectOption(
                label=name[:100], value=name,
                description=("Custom API key model" if api_key else "Gemcord default-key model"),
                emoji=GEMINI_EMOJI, default=(name == selected_model),
            ) for name in models
        ]
        options.append(discord.SelectOption(
            label=CAPPUCINO_MODEL, value=CAPPUCINO_MODEL,
            description="Hardcoded, intentionally simple offline AI",
            emoji=CAPPUCINO_EMOJI, default=(selected_model == CAPPUCINO_MODEL),
        ))
        self.model_select = discord.ui.Select(
            placeholder="Choose a Gemini model or Cappucino-AI-B1",
            min_values=1, max_values=1, options=options,
        )
        self.add_item(discord.ui.Label(
            text="AI model", description="Gemini models are fetched from the selected key",
            component=self.model_select,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not is_partner_or_owner(interaction.user.id):
            return await interaction.response.send_message("Partner access required.", ephemeral=True)
        key = self.api_key_value
        model_name = self.model_select.values[0]
        if not key and model_name not in DEFAULT_MODELS and model_name != CAPPUCINO_MODEL:
            return await interaction.response.send_message(
                "That Gemini model requires a custom API key. Reopen `/settings` and enter your key first.",
                ephemeral=True,
            )
        data = read_settings()
        cfg = data["servers"].setdefault(self.guild_id, {})
        cfg["api_key"] = key
        cfg["model"] = model_name
        write_settings(data)
        if model_name == CAPPUCINO_MODEL:
            message = f"Settings saved. Model: <:Cappucino:1562985358969282611> `{model_name}` (offline, hardcoded replies)."
        elif key:
            message = f"Settings saved. Model: <:GeminiIcon:1562975823391232031> `{model_name}` using your custom API key."
        else:
            message = f"Settings saved. Model: <:GeminiIcon:1562975823391232031> `{model_name}` using Gemcord's default key."
        await interaction.response.send_message(message, ephemeral=True)




async def relay_partner_message(message):
    """Relay partner-authored messages through Gemcord while control mode is enabled."""
    if message.guild is None or message.author.bot:
        return False
    data = read_settings()
    cfg = data.get("servers", {}).get(str(message.guild.id), {})
    if not cfg.get("fake_control_enabled") or not is_partner_or_owner(message.author.id):
        return False

    content = (message.content or "").strip()
    # Keep the toggle itself executable, and don't intercept any other bot commands.
    lowered = content.lower()
    if lowered in (".fake control", ".f control") or content.startswith("."):
        return False
    if not content and not message.attachments:
        return False

    me = message.guild.me
    if me is None:
        return False
    perms = message.channel.permissions_for(me)
    can_send = perms.view_channel and perms.send_messages
    if isinstance(message.channel, discord.Thread):
        can_send = perms.view_channel and perms.send_messages_in_threads
    if not can_send or not perms.manage_messages:
        # Never delete the original unless Gemcord can safely replace it.
        return False

    try:
        payload = content if content else None
        if payload:
            send_kwargs = {"allowed_mentions": discord.AllowedMentions.none()}
            # Preserve Discord reply context when relaying the partner's message.
            # This makes Gemcord's replacement appear as a reply to the same target.
            if message.reference is not None:
                send_kwargs["reference"] = message.reference
                send_kwargs["mention_author"] = False
            await message.channel.send(payload, **send_kwargs)
        for attachment in message.attachments:
            await message.channel.send(attachment.url, allowed_mentions=discord.AllowedMentions.none())
        await message.delete()
        return True
    except discord.HTTPException:
        # If a send failed, preserve the original message where possible.
        return False


class PartnerFeatures(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _require_partner(self, ctx):
        if not ctx.guild or not is_partner_or_owner(ctx.author.id):
            await ctx.send("This command is for Gemcord partners.")
            return False
        return True

    @commands.group(name="fake", aliases=["f"], invoke_without_command=True)
    async def fake(self, ctx):
        if not await self._require_partner(ctx):
            return
        await ctx.send("Usage: `.fake ban [@user] [reason]`, `.fake sban`, or `.fake control`.")

    @fake.command(name="ban")
    async def fake_ban(self, ctx, target: discord.User = None, *, reason: str = "No reason provided"):
        """Display Gemcord's normal AI-ban notice without applying a ban."""
        if not await self._require_partner(ctx):
            return
        from Moderation import BanNoticeView
        ban_record = {
            "guild_id": None,
            "days": None,
            "reason": reason[:1000] or "No reason provided",
        }
        # Use the exact Components V2 view used by real AI-ban notices.
        await ctx.send(view=BanNoticeView(ban_record, ctx.guild.name))

    @fake.command(name="sban")
    async def fake_server_ban(self, ctx, *, reason: str = "No reason provided"):
        """Display Gemcord's normal server-ban notice without banning or leaving."""
        if not await self._require_partner(ctx):
            return
        import Whitelist
        ban_record = {"reason": reason[:1000] or "No reason provided"}
        await ctx.send(view=Whitelist.ServerBannedView(ban_record, ctx.guild.name))

    @fake.command(name="control")
    async def fake_control(self, ctx):
        """Toggle partner message replacement mode for this server."""
        if not await self._require_partner(ctx):
            return
        data = read_settings()
        cfg = data["servers"].setdefault(str(ctx.guild.id), {})
        cfg["fake_control_enabled"] = not cfg.get("fake_control_enabled", False)
        write_settings(data)
        state = "enabled" if cfg["fake_control_enabled"] else "disabled"
        await ctx.send(f"Partner message control is now **{state}**. " +
                       ("Your regular messages will be deleted and reposted by Gemcord. Make sure it has Manage Messages and Send Messages." if cfg["fake_control_enabled"] else "Messages will no longer be relayed."))

    @commands.command(name="talk")
    async def partner_talk(self, ctx, *, message: str):
        """Make Gemcord post a message; requires a server admin who is also a partner."""
        if not ctx.guild or not ctx.author.guild_permissions.administrator or not is_partner_or_owner(ctx.author.id):
            return await ctx.send("This command requires a server administrator with partner access.")
        await ctx.send(message.replace("emoticon_wave", "<a:wavey:1552716572207087797>"),
                       allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="settings", description="Configure Gemini for this partner server")
    @app_commands.guild_only()
    async def settings(self, interaction: discord.Interaction):
        if not is_partner_or_owner(interaction.user.id):
            return await interaction.response.send_message("This command is for Gemcord partners.", ephemeral=True)
        await interaction.response.send_modal(ApiKeyModal(interaction.guild_id))


async def setup(bot):
    await bot.add_cog(PartnerFeatures(bot))
