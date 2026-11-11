import asyncio
import datetime
import discord

from Gemini_Integration import PERMISSION_NAMES

MAX_MUTE_SECONDS = 180
MAX_TIMEOUT = datetime.timedelta(days=28)  # Discord's hard limit
DISCORD_WAIT_SECONDS = 15
ICON_TYPES = {"image/png", "image/jpeg"}
MAX_WEBHOOKS = 15  # Discord's per-channel limit
MAX_EMOJIS = 100


def make_executor(message, loop, images=None, channel=None, ref=None):
    """Returns a sync execute(name, args) that is safe to call from a worker thread."""
    guild = message.guild
    channel = channel or message.channel
    author = message.author
    images = images or []
    reacted = []

    def reason_for(args):
        extra = (args.get("reason") or "").strip()
        text = f"Gemcord: {extra} (asked by {author})" if extra else f"Gemcord (asked by {author})"
        return text[:512]

    async def get_member(uid):
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            raise ValueError("user_id must be a numeric id")
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound:
                raise ValueError("that user is not in this server")
        return member

    def protect(member):
        me = guild.me
        if member.id == me.id:
            raise ValueError("you can't target yourself")
        if member.id == guild.owner_id:
            raise ValueError("you can't touch the server owner")
        if member.top_role >= me.top_role:
            raise ValueError("their top role is as high as or higher than yours")

    def webhook_channel():
        # Webhooks live on the parent channel, not on threads
        base = channel.parent if isinstance(channel, discord.Thread) else channel
        if not isinstance(base, (discord.TextChannel, discord.VoiceChannel, discord.ForumChannel)):
            raise ValueError("this channel type can't have webhooks")
        return base

    async def act(name, args):
        reason = reason_for(args)

        # ---- owner-only server management (defense in depth; tool visibility is not authorization)
        if name in {"whitelist_server", "server_ban"}:
            from Moderation import OWNER_ID
            if int(author.id) != int(OWNER_ID):
                raise ValueError("owner-only tool")
            try:
                target_guild_id = int(args.get("guild_id"))
                if target_guild_id <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("guild_id must be a positive numeric Discord server ID")
            import Whitelist
            if name == "whitelist_server":
                target_guild = next(
                    (g for g in getattr(message, "client", getattr(message, "bot", None)).guilds
                     if g.id == target_guild_id), None
                ) if getattr(message, "client", getattr(message, "bot", None)) else None
                Whitelist.whitelist_guild(
                    target_guild_id,
                    name=target_guild.name if target_guild else None,
                    approved_by=author.id,
                )
                return {"ok": True, "whitelisted_guild_id": target_guild_id}
            entry = Whitelist.ban_server(
                target_guild_id,
                (args.get("reason") or "No reason provided").strip()[:500],
                by=author.id,
                name=None,
            )
            return {"ok": True, "server_banned_guild_id": target_guild_id}

        # ---- read only
        if name == "react":
            target = ref if args.get("target") == "replied" else message
            if target is None:
                raise ValueError("there is no replied message to react to")
            perms = target.channel.permissions_for(guild.me)
            if not (perms.add_reactions and perms.read_message_history):
                raise ValueError("i can't add reactions in this channel")
            raw = (args.get("emoji") or "").strip()
            if not raw:
                raise ValueError("emoji can't be empty")
            emoji = discord.PartialEmoji.from_str(raw) if raw.startswith("<") else raw
            try:
                await target.add_reaction(emoji)
            except discord.NotFound:
                raise ValueError("unknown emoji or the message is gone")
            except discord.HTTPException as e:
                if e.code == 10014:
                    raise ValueError("unknown emoji, use a real unicode emoji or one from list_emojis")
                raise
            reacted.append(raw)
            return {"ok": True}

        if name == "list_emojis":
            query = (args.get("query") or "").strip().lower()
            found = [
                e for e in guild.emojis
                if e.available and e.is_usable() and (not query or query in e.name.lower())
            ]
            return {
                "ok": True,
                "total": len(found),
                "emojis": [
                    {"name": e.name, "animated": e.animated, "use": str(e)}
                    for e in found[:MAX_EMOJIS]
                ],
            }

        # ---- normal
        if name == "nick":
            nick = (args.get("nickname") or "").strip()[:32] or None
            await guild.me.edit(nick=nick, reason=reason)
            return {"ok": True, "nickname": nick}

        # ---- annoying
        if name == "set_channel_name":
            if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)):
                raise ValueError("this channel type can't be renamed")
            new = (args.get("name") or "").strip()[:100]
            if not new:
                raise ValueError("name can't be empty")
            await channel.edit(name=new, reason=reason)
            return {"ok": True, "name": new}

        if name == "set_channel_topic":
            if not isinstance(channel, discord.TextChannel):
                raise ValueError("only normal text channels have a topic")
            topic = (args.get("topic") or "").strip()[:1024]
            await channel.edit(topic=topic or None, reason=reason)
            return {"ok": True, "topic": topic}

        if name == "mute_member":
            member = await get_member(args.get("user_id"))
            protect(member)
            seconds = max(1, min(int(args.get("seconds", 60)), MAX_MUTE_SECONDS))
            await member.timeout(datetime.timedelta(seconds=seconds), reason=reason)
            return {"ok": True, "muted": member.display_name, "seconds": seconds}

        if name == "nick_member":
            member = await get_member(args.get("user_id"))
            protect(member)
            nick = (args.get("nickname") or "").strip()[:32] or None
            await member.edit(nick=nick, reason=reason)
            return {"ok": True, "member": member.display_name, "nickname": nick}

        if name == "add_webhook":
            base = webhook_channel()
            wh_name = (args.get("name") or "").strip()[:80]
            if not wh_name:
                raise ValueError("webhook name can't be empty")
            if len(await base.webhooks()) >= MAX_WEBHOOKS:
                raise ValueError("channel already has the max number of webhooks")
            hook = await base.create_webhook(name=wh_name, reason=reason)
            return {"ok": True, "webhook_id": str(hook.id)}

        if name == "send_webhook":
            base = webhook_channel()
            content = (args.get("content") or "").strip()[:2000]
            if not content:
                raise ValueError("content can't be empty")
            wid = args.get("webhook_id")
            # only webhooks with a token are usable (i.e. ones this bot created)
            hooks = [h for h in await base.webhooks() if h.token]
            hook = next((h for h in hooks if not wid or str(h.id) == str(wid)), None)
            if hook is None:
                raise ValueError("no usable webhook found, use add_webhook first")
            kwargs = {}
            username = (args.get("username") or "").strip()[:80]
            if username:
                kwargs["username"] = username
            avatar = (args.get("avatar_url") or "").strip()
            if avatar:
                kwargs["avatar_url"] = avatar
            if isinstance(channel, discord.Thread):
                kwargs["thread"] = channel
            await hook.send(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
                **kwargs,
            )
            return {"ok": True}

        # ---- dangerous
        if name == "timeout_member":
            member = await get_member(args.get("user_id"))
            protect(member)
            minutes = int(args.get("duration_minutes", 0))
            delta = MAX_TIMEOUT if minutes <= 0 else min(datetime.timedelta(minutes=minutes), MAX_TIMEOUT)
            await member.timeout(delta, reason=reason)
            return {"ok": True, "timed_out": member.display_name, "minutes": int(delta.total_seconds() // 60)}

        if name == "kick_member":
            member = await get_member(args.get("user_id"))
            protect(member)
            await guild.kick(member, reason=reason)
            return {"ok": True, "kicked": member.display_name}

        if name == "ban_member":
            try:
                uid = int(args.get("user_id"))
            except (TypeError, ValueError):
                raise ValueError("user_id must be a numeric id")
            if uid == guild.owner_id or uid == guild.me.id:
                raise ValueError("you can't ban the owner or yourself")
            member = guild.get_member(uid)
            if member is not None:
                protect(member)
            await guild.ban(discord.Object(id=uid), reason=reason, delete_message_seconds=0)
            return {"ok": True, "banned": uid}

        if name == "unban_member":
            try:
                uid = int(args.get("user_id"))
            except (TypeError, ValueError):
                raise ValueError("user_id must be a numeric id")
            await guild.unban(discord.Object(id=uid), reason=reason)
            return {"ok": True, "unbanned": uid}

        if name == "set_server_name":
            new = (args.get("name") or "").strip()[:100]
            if len(new) < 2:
                raise ValueError("server name must be at least 2 characters")
            await guild.edit(name=new, reason=reason)
            return {"ok": True, "name": new}

        if name == "set_server_icon":
            usable = [data for mime, data in images if mime in ICON_TYPES]
            if not usable:
                raise ValueError("no PNG/JPEG image attached to this message or the one it replies to")
            await guild.edit(icon=usable[0], reason=reason)
            return {"ok": True}

        if name == "set_channel_permission":
            if isinstance(channel, discord.Thread):
                raise ValueError("threads don't have their own permission overwrites")
            perm = args.get("permission")
            value = args.get("value")
            if perm not in PERMISSION_NAMES:
                raise ValueError(f"permission must be one of {PERMISSION_NAMES}")
            if value not in ("allow", "deny", "reset"):
                raise ValueError("value must be allow, deny or reset")
            try:
                tid = int(args.get("target_id"))
            except (TypeError, ValueError):
                raise ValueError("target_id must be a numeric role or user id")
            if tid == guild.me.id:
                raise ValueError("you can't change your own permissions")
            target = guild.get_role(tid)
            if target is None:
                target = await get_member(tid)
            overwrite = channel.overwrites_for(target)
            setattr(overwrite, perm, {"allow": True, "deny": False, "reset": None}[value])
            await channel.set_permissions(
                target, overwrite=None if overwrite.is_empty() else overwrite, reason=reason
            )
            return {"ok": True, "target": getattr(target, "name", str(tid)), perm: value}

        return {"ok": False, "error": f"unknown action {name}"}

    async def safe(name, args):
        try:
            return await act(name, args or {})
        except discord.Forbidden:
            return {"ok": False, "error": "discord says I lack the permission or my role is too low"}
        except discord.HTTPException as e:
            return {"ok": False, "error": f"discord error: {e.text or e.status}"}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def execute(name, args):
        fut = asyncio.run_coroutine_threadsafe(safe(name, args), loop)
        try:
            return fut.result(timeout=DISCORD_WAIT_SECONDS)
        except TimeoutError:
            fut.cancel()  # don't leave a rate-limited edit hanging in the background
            return {"ok": False, "error": "discord took too long (probably rate limited), try later"}

    execute.reacted = reacted  # lets the caller see whether a reaction went out
    return execute