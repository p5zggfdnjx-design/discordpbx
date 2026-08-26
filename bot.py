from __future__ import annotations

import asyncio
import logging
import os
import signal
import tempfile
import urllib.request
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from appdb import AppDatabase, CAPABILITIES, DEFAULT_ADMIN_CAPS, DEFAULT_OPERATOR_CAPS
from backup_manager import BackupManager
from audiosocket import AudioSocketServer, DISCORD_FRAME_BYTES
from bridge import BridgeManager
from config import Config
from pbx import AsteriskAMI
from secrets_store import SecretStore
from workspace_service import WorkspaceService
from webui import WebControlServer

load_dotenv()
config = Config.from_env()
logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("discord-pbx")


class BridgeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        # A backup restore is staged from the UI and applied only during process
        # startup, before SQLite connections exist. This makes rollback atomic from
        # the application's perspective and avoids replacing live database files.
        try:
            restored = BackupManager(config.data_dir).apply_pending_restore()
            if restored:
                log.warning("Applied queued PBX backup restore: %s", restored.get("name"))
        except Exception:
            log.exception("Queued PBX backup restore failed; continuing with current data")
        self.appdb = AppDatabase(str(Path(config.data_dir) / "pbx_app.sqlite3"))
        self.secrets = SecretStore(config.data_dir)
        self._migrate_legacy_bootstrap()
        self.bridge = BridgeManager(self, config)
        self.audio_server = AudioSocketServer(self.bridge, config.audiosocket_bind, config.audiosocket_port)
        ami_secret = self.secrets.get("asterisk_ami_secret", config.ami_secret)
        self.ami = AsteriskAMI(config.ami_host, config.ami_port, config.ami_user, ami_secret, config.ami_timeout)
        self.workspaces = WorkspaceService(self, self.appdb, config)
        self.web = WebControlServer(self, config, self.appdb, self.secrets, self.workspaces)
        # v2.x contacts had no workspace ownership. Attach them to the first/default
        # workspace once rather than exposing them globally or making them vanish.
        default_workspace = self.workspaces.default_workspace()
        if default_workspace:
            workspace_id = default_workspace["id"]
            migrated = self.web.contacts.migrate_legacy_workspace(workspace_id)
            if migrated:
                log.info("Migrated %d legacy contact(s) into workspace %s", migrated, workspace_id)
            migrated = self.web.scheduler.migrate_legacy_workspace(workspace_id)
            if migrated:
                log.info("Migrated %d legacy schedule(s) into workspace %s", migrated, workspace_id)
            migrated = self.web.call_history.migrate_legacy_workspace(workspace_id)
            if migrated:
                log.info("Migrated %d legacy call-history row(s) into workspace %s", migrated, workspace_id)
        self.services_started = False

    def _migrate_legacy_bootstrap(self) -> None:
        legacy = {
            "discord_bot_token": config.discord_token,
            "discord_oauth_client_secret": config.discord_client_secret,
            "asterisk_ami_secret": config.ami_secret,
            "pbx_ingress_token": config.pbx_ingress_token,
            "legacy_web_password": config.web_password,
        }
        for key, value in legacy.items():
            if value and not self.secrets.has(key):
                self.secrets.set(key, value)
        for key, value in {
            "discord_client_id": config.discord_client_id,
            "public_base_url": config.public_base_url,
            "asterisk_host": config.ami_host,
            "asterisk_port": config.ami_port,
            "asterisk_user": config.ami_user,
            "audiosocket_advertise_host": config.audiosocket_advertise_host,
            "asterisk_dial_context": config.ami_dial_context,
            "max_simultaneous_calls": config.max_simultaneous_calls,
        }.items():
            if value not in (None, "") and self.appdb.get_setting(key, None) is None:
                self.appdb.set_setting(key, value)
        # A copied v2.x Basic-auth .env remains usable immediately after upgrade.
        # Where possible, migrate it into the stronger local break-glass account.
        # Short legacy passwords remain available through HTTP Basic fallback until
        # the owner replaces them from Settings; we do not weaken the new 12-char rule.
        if not self.appdb.get_setting("system_initialized", False) and config.web_auth_mode in {"basic", "hybrid"} and config.web_password:
            if not self.appdb.local_admin_configured() and len(config.web_password) >= 12:
                try:
                    self.appdb.set_local_admin(config.web_username or "pbx", config.web_password)
                except Exception:
                    log.exception("Could not migrate legacy web credentials into local admin")
            self.appdb.set_setting("system_initialized", True)

    async def start_services(self):
        if self.services_started:
            return
        await self.audio_server.start()
        await self.web.start()
        self.services_started = True

    async def close(self):
        if self.services_started:
            await self.web.close()
            await self.audio_server.close()
            self.services_started = False
        await super().close()


bot = BridgeBot()


def workspace_for_interaction(interaction: discord.Interaction):
    return bot.workspaces.workspace_for_guild(interaction.guild_id or 0) if interaction.guild_id else None


async def allowed(interaction: discord.Interaction, capability: str = "panel_access") -> bool:
    if interaction.user.id in config.bot_owner_ids:
        return True
    ws = workspace_for_interaction(interaction)
    if not ws:
        return False
    return await bot.workspaces.has_capability(interaction.user.id, ws["id"], capability)


def require_capability(capability: str):
    async def predicate(interaction: discord.Interaction) -> bool:
        if await allowed(interaction, capability):
            return True
        message = f"You need the PBX `{capability}` permission in this Discord workspace."
        if interaction.response.is_done(): await interaction.followup.send(message, ephemeral=True)
        else: await interaction.response.send_message(message, ephemeral=True)
        return False
    return app_commands.check(predicate)


async def _sync_guild_commands(guild: discord.Guild) -> None:
    try:
        target = discord.Object(id=guild.id)
        bot.tree.copy_global_to(guild=target)
        synced = await bot.tree.sync(guild=target)
        log.info("Synced %d PBX commands to %s", len(synced), guild.name)
    except Exception:
        log.exception("Command sync failed for guild %s", guild.id)


def _guild_setup_channel(guild: discord.Guild):
    me = guild.me or (guild.get_member(bot.user.id) if bot.user else None)
    candidates = []
    if guild.system_channel:
        candidates.append(guild.system_channel)
    candidates.extend(c for c in guild.text_channels if c not in candidates)
    for channel in candidates:
        try:
            perms = channel.permissions_for(me) if me else None
            if perms and perms.view_channel and perms.send_messages:
                return channel
        except Exception:
            continue
    return None


async def _send_guild_setup_prompt(guild: discord.Guild) -> None:
    if bot.workspaces.workspace_for_guild(guild.id):
        return
    key = f"discord_onboarding_prompted:{guild.id}"
    if bot.appdb.get_setting(key, False):
        return
    channel = _guild_setup_channel(guild)
    if not channel:
        return
    try:
        await channel.send(
            "👋 **Discord PBX needs one-time setup for this server.**\n"
            "A server owner or member with **Manage Server** should run `/pbx setup` (or `/pbx-setup`). "
            "For a new server, a configured PBX system-admin Discord account must also be a member here. "
            "Discord will ask for the PBX voice channel, notification text channel, PBX user role, and PBX admin role.\n"
            "The bot stays out of voice until this server actually has a PBX call."
        )
        bot.appdb.set_setting(key, True)
    except Exception:
        log.exception("Could not send PBX onboarding prompt in guild %s", guild.id)


@bot.event
async def on_ready():
    log.info("Discord connected as %s (%s), %d guild(s)", bot.user, bot.user.id if bot.user else "?", len(bot.guilds))
    for guild in bot.guilds:
        await _sync_guild_commands(guild)
        ws = bot.workspaces.workspace_for_guild(guild.id)
        if ws:
            # v3.2+ voice is intentionally on-demand. The bot remains installed
            # for OAuth/RBAC/presence, but does not idle in a voice channel.
            if config.auto_join_on_start:
                log.warning("AUTO_JOIN_ON_START is ignored in on-demand voice mode for %s", ws["alias"])
        else:
            await _send_guild_setup_prompt(guild)
    if bot.web:
        await bot.web._publish("discord.ready", {"guilds": len(bot.guilds)})


@bot.event
async def on_guild_join(guild: discord.Guild):
    # New servers receive commands and a one-time guided onboarding prompt even
    # though they do not have a PBX workspace yet.
    await _sync_guild_commands(guild)
    await _send_guild_setup_prompt(guild)
    if bot.web:
        await bot.web._publish("discord.guild_joined", {"guild_id": str(guild.id), "name": guild.name})


@bot.event
async def on_voice_state_update(member, before, after):
    ws = bot.workspaces.workspace_for_guild(member.guild.id)
    if not ws:
        return
    bot.workspaces._member_caps_cache.pop((str(member.id), ws["id"]), None)
    try:
        presence = await bot.workspaces.eligible_presence(ws["id"])
        await bot.web._publish("presence.changed", {"workspace_id": ws["id"], "presence": presence})
    except Exception:
        pass


@bot.event
async def on_member_update(before, after):
    ws = bot.workspaces.workspace_for_guild(after.guild.id)
    if ws:
        bot.workspaces._member_caps_cache.pop((str(after.id), ws["id"]), None)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    root = error.original if isinstance(error, app_commands.CommandInvokeError) else error
    command_name = getattr(getattr(interaction, "command", None), "qualified_name", "unknown")
    log.error(
        "Discord application command failed: command=%s guild=%s user=%s error=%r",
        command_name, interaction.guild_id, getattr(interaction.user, "id", None), root,
        exc_info=(type(root), root, root.__traceback__),
    )
    message = "The PBX command failed on the bridge. The error has been logged; try again or check the container logs."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        log.exception("Could not return Discord application-command error to the user")


@bot.tree.command(name="ping", description="Check whether the PBX bot is alive.")
@require_capability("panel_access")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"PBX v3.2.9 · {round(bot.latency * 1000)} ms", ephemeral=True)


pbx = app_commands.Group(name="pbx", description="Control this Discord workspace's PBX bridge")


def _interaction_can_setup_guild(interaction: discord.Interaction) -> bool:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        return False
    return member.id == guild.owner_id or member.guild_permissions.manage_guild


async def _guild_authorized_for_pbx_setup(guild: discord.Guild) -> bool:
    # Do not let an arbitrary Discord server that discovers the application invite
    # itself onto the owner's phone trunk. A fresh guild may self-onboard only when
    # at least one configured PBX system-admin Discord account is also a member.
    # Existing PBX workspaces can always be reconfigured by that guild's admins.
    if bot.workspaces.workspace_for_guild(guild.id):
        return True

    admin_ids = sorted(bot.workspaces._system_admin_ids())
    if not admin_ids:
        log.warning("PBX setup authorization denied for guild %s (%s): no system-admin Discord IDs are configured", guild.id, guild.name)
        return False

    for user_id in admin_ids:
        uid = int(user_id)
        member = guild.get_member(uid)
        if member is not None:
            return True

        # Member cache misses are possible shortly after a bot joins a guild. Do a
        # bounded REST lookup, but never let Discord onboarding hang indefinitely.
        try:
            member = await asyncio.wait_for(guild.fetch_member(uid), timeout=4.0)
        except asyncio.TimeoutError:
            log.warning("PBX setup member lookup timed out for guild=%s admin_user=%s", guild.id, uid)
            member = None
        except discord.NotFound:
            member = None
        except discord.Forbidden:
            log.error(
                "PBX setup member lookup forbidden for guild=%s admin_user=%s. "
                "Verify Server Members Intent is enabled for the Discord application.",
                guild.id, uid,
            )
            member = None
        except discord.HTTPException as exc:
            log.warning("PBX setup member lookup failed for guild=%s admin_user=%s: %s", guild.id, uid, exc)
            member = None
        except Exception:
            log.exception("Unexpected PBX setup member lookup failure for guild=%s admin_user=%s", guild.id, uid)
            member = None
        if member is not None:
            return True

    log.warning(
        "PBX setup authorization denied for guild %s (%s): none of the configured PBX system-admin Discord accounts are members",
        guild.id, guild.name,
    )
    return False


class GuildSetupView(discord.ui.View):
    """Discord-native onboarding for one PBX workspace.

    A server administrator chooses the routing channels and the two RBAC roles.
    The bridge stores only Discord IDs, so later renames do not break the setup.
    """

    def __init__(self, guild: discord.Guild, actor_id: int):
        super().__init__(timeout=600)
        self.guild_id = int(guild.id)
        self.actor_id = int(actor_id)
        self.voice_channel_id = ""
        self.text_channel_id = ""
        self.user_role_id = ""
        self.admin_role_id = ""

        self.voice_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.voice], min_values=1, max_values=1,
            placeholder="1. PBX voice channel"
        )
        self.text_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text], min_values=1, max_values=1,
            placeholder="2. Notification text channel"
        )
        self.user_role_select = discord.ui.RoleSelect(
            min_values=1, max_values=1, placeholder="3. Role allowed to use the PBX"
        )
        self.admin_role_select = discord.ui.RoleSelect(
            min_values=1, max_values=1, placeholder="4. Role allowed to administer this workspace"
        )
        self.save_button = discord.ui.Button(
            label="Save PBX setup", style=discord.ButtonStyle.success, disabled=True, row=4
        )
        self.voice_select.row = 0
        self.text_select.row = 1
        self.user_role_select.row = 2
        self.admin_role_select.row = 3
        self.voice_select.callback = self._voice_changed
        self.text_select.callback = self._text_changed
        self.user_role_select.callback = self._user_role_changed
        self.admin_role_select.callback = self._admin_role_changed
        self.save_button.callback = self._save
        self.add_item(self.voice_select)
        self.add_item(self.text_select)
        self.add_item(self.user_role_select)
        self.add_item(self.admin_role_select)
        self.add_item(self.save_button)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.error(
            "Discord PBX setup component failed: guild=%s user=%s item=%s error=%r",
            self.guild_id, getattr(interaction.user, "id", None), type(item).__name__, error,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "PBX setup hit an error. The bridge logged the details; try the action again or check the container logs."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            log.exception("Could not return Discord PBX setup component error to the user")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Only the administrator who opened this setup can change it.", ephemeral=True)
            return False
        if interaction.guild_id != self.guild_id or not _interaction_can_setup_guild(interaction):
            await interaction.response.send_message("Manage Server permission is required to configure this PBX workspace.", ephemeral=True)
            return False
        # Authorization was verified before this ephemeral setup view was created.
        # Do not perform another network member lookup for every select interaction;
        # doing so can make Discord time out component acknowledgements. The view is
        # actor-bound, guild-bound, Manage-Server-only, and expires after 10 minutes.
        return True

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self.save_button.disabled = not all((self.voice_channel_id, self.text_channel_id, self.user_role_id, self.admin_role_id))
        await interaction.response.edit_message(view=self)

    async def _voice_changed(self, interaction: discord.Interaction) -> None:
        self.voice_channel_id = str(self.voice_select.values[0].id)
        await self._refresh(interaction)

    async def _text_changed(self, interaction: discord.Interaction) -> None:
        self.text_channel_id = str(self.text_select.values[0].id)
        await self._refresh(interaction)

    async def _user_role_changed(self, interaction: discord.Interaction) -> None:
        self.user_role_id = str(self.user_role_select.values[0].id)
        await self._refresh(interaction)

    async def _admin_role_changed(self, interaction: discord.Interaction) -> None:
        self.admin_role_id = str(self.admin_role_select.values[0].id)
        await self._refresh(interaction)

    async def _save(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("This setup must be run inside a Discord server.", ephemeral=True)
        voice = guild.get_channel(int(self.voice_channel_id))
        text = guild.get_channel(int(self.text_channel_id))
        user_role = guild.get_role(int(self.user_role_id))
        admin_role = guild.get_role(int(self.admin_role_id))
        if not isinstance(voice, discord.VoiceChannel):
            return await interaction.response.send_message("The selected PBX voice channel is no longer available.", ephemeral=True)
        if not isinstance(text, discord.TextChannel):
            return await interaction.response.send_message("The selected notification channel is no longer available.", ephemeral=True)
        if not user_role or not admin_role or user_role.is_default() or admin_role.is_default():
            return await interaction.response.send_message("Choose explicit server roles rather than @everyone.", ephemeral=True)
        if user_role.managed or admin_role.managed:
            return await interaction.response.send_message("Integration-managed roles cannot be used for PBX permissions.", ephemeral=True)
        me = guild.me
        if me:
            vp = voice.permissions_for(me)
            tp = text.permissions_for(me)
            if not (vp.view_channel and vp.connect and vp.speak):
                return await interaction.response.send_message("I need View Channel, Connect, and Speak in the selected voice channel.", ephemeral=True)
            if not (tp.view_channel and tp.send_messages):
                return await interaction.response.send_message("I need View Channel and Send Messages in the selected text channel.", ephemeral=True)

        # Saving touches persistence, event delivery, and Discord's REST API. Ack the
        # component first so a slow notification send cannot make the Save button
        # appear to fail even though the workspace was written successfully.
        await interaction.response.defer()

        existing = bot.workspaces.workspace_for_guild(guild.id)
        workspace_id = existing["id"] if existing else f"ws_{guild.id}"
        data = {
            "id": workspace_id,
            "guild_id": str(guild.id),
            "alias": existing.get("alias", guild.name) if existing else guild.name,
            "voice_channel_id": str(voice.id),
            "text_channel_id": str(text.id),
            "enabled": existing.get("enabled", True) if existing else True,
            "accept_inbound": existing.get("accept_inbound", True) if existing else True,
            "allow_outbound": existing.get("allow_outbound", True) if existing else True,
            "auto_route": existing.get("auto_route", True) if existing else True,
            "priority": existing.get("priority", 100) if existing else 100,
            "max_calls": existing.get("max_calls", config.max_simultaneous_calls) if existing else config.max_simultaneous_calls,
            "presence_grace_seconds": existing.get("presence_grace_seconds", 4) if existing else 4,
            "ring_mode": existing.get("ring_mode", "auto") if existing else "auto",
        }
        ws = bot.appdb.upsert_workspace(data)
        bot.appdb.replace_role_capabilities(ws["id"], str(user_role.id), user_role.name, DEFAULT_OPERATOR_CAPS)
        bot.appdb.replace_role_capabilities(ws["id"], str(admin_role.id), admin_role.name, DEFAULT_ADMIN_CAPS)
        bot.workspaces._member_caps_cache.clear()
        if not bot.appdb.get_setting("default_workspace_id", ""):
            bot.appdb.set_setting("default_workspace_id", ws["id"])
        bot.appdb.set_setting(f"discord_onboarding_prompted:{guild.id}", True)
        bot.appdb.audit(
            "workspace.discord_setup", actor_user_id=str(interaction.user.id), actor_name=interaction.user.display_name,
            auth_type="discord", workspace_id=ws["id"], entity_type="workspace", entity_id=ws["id"],
            detail={"guild_id": str(guild.id), "voice_channel_id": str(voice.id), "text_channel_id": str(text.id),
                    "user_role_id": str(user_role.id), "admin_role_id": str(admin_role.id)},
        )
        await bot.web._publish("workspace.changed", {"workspace_id": ws["id"], "action": "discord_setup"})
        panel = bot.web.auth.public_base_url(None)
        try:
            await text.send(
                f"✅ **Discord PBX configured for {guild.name}.**\n"
                f"Voice: {voice.mention}\nNotifications: {text.mention}\n"
                f"PBX users: {user_role.mention}\nPBX workspace admins: {admin_role.mention}"
                + (f"\nPanel: {panel}/login" if panel else "")
            )
        except Exception:
            log.exception("Could not send PBX setup confirmation to guild %s", guild.id)
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(
            content=(
                f"✅ **PBX workspace configured.**\n"
                f"Voice channel: {voice.mention}\nNotification channel: {text.mention}\n"
                f"PBX user role: {user_role.mention}\nPBX admin role: {admin_role.mention}\n\n"
                "The bot will join voice only when this workspace has an active call."
            ),
            view=self,
        )


async def _run_pbx_setup(interaction: discord.Interaction) -> None:
    if not interaction.guild or not _interaction_can_setup_guild(interaction):
        await interaction.response.send_message("You need **Manage Server** permission to run PBX setup.", ephemeral=True)
        return

    # Acknowledge the slash command before any Discord REST/member lookup. Discord
    # otherwise marks the command as failed if authorization takes longer than the
    # initial interaction response window.
    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        authorized = await _guild_authorized_for_pbx_setup(interaction.guild)
    except Exception:
        log.exception(
            "PBX setup authorization crashed for guild=%s actor=%s",
            interaction.guild.id, interaction.user.id,
        )
        await interaction.edit_original_response(
            content=(
                "PBX setup could not verify this server right now. The error was logged by the bridge. "
                "Check the container logs and try again."
            ),
            view=None,
        )
        return

    if not authorized:
        await interaction.edit_original_response(
            content=(
                "This server is not authorized for this PBX yet. For first-time setup, at least one Discord account "
                "configured as a **PBX system administrator** must also be a member of this server. This prevents "
                "unrelated servers from attaching themselves to the phone trunk."
            ),
            view=None,
        )
        return

    existing = bot.workspaces.workspace_for_guild(interaction.guild.id)
    current = ""
    if existing:
        voice = interaction.guild.get_channel(int(existing.get("voice_channel_id") or 0))
        text = interaction.guild.get_channel(int(existing.get("text_channel_id") or 0))
        current = f"\nCurrent: voice **{getattr(voice, 'name', 'not set')}**, notifications **{getattr(text, 'name', 'not set')}**."

    await interaction.edit_original_response(
        content=(
            "**Configure this Discord server as a PBX workspace.**\n"
            "Choose the voice channel used during calls, the text channel for call notifications, the role allowed "
            "to use the PBX, and the role allowed to administer this workspace."
            + current
        ),
        view=GuildSetupView(interaction.guild, interaction.user.id),
    )


@pbx.command(name="setup", description="Set up or reconfigure PBX channels and roles for this Discord server.")
async def pbx_setup(interaction: discord.Interaction):
    await _run_pbx_setup(interaction)


@bot.tree.command(name="pbx-setup", description="Open the Discord PBX server setup wizard.")
async def pbx_setup_standalone(interaction: discord.Interaction):
    # Standalone fallback for Discord clients that cache an older /pbx subgroup.
    await _run_pbx_setup(interaction)


@pbx.command(name="status", description="Show this workspace's PBX status.")
@require_capability("panel_access")
async def pbx_status(interaction: discord.Interaction):
    ws = workspace_for_interaction(interaction)
    active = [x for x in bot.bridge.status_dict().get("calls", []) if ws and ws["id"] in x.get("workspace_ids", [])]
    presence = await bot.workspaces.eligible_presence(ws["id"]) if ws else {}
    await interaction.response.send_message(
        f"**{ws['alias'] if ws else 'PBX'}** · {len(active)} active call(s) · "
        f"{presence.get('eligible_count',0)} eligible operator(s) in voice · AMI {'configured' if bot.ami.configured else 'not configured'}",
        ephemeral=True,
    )


@pbx.command(name="config", description="Show this server's configured PBX channels and role permissions.")
@require_capability("panel_access")
async def pbx_config(interaction: discord.Interaction):
    ws = workspace_for_interaction(interaction)
    guild = interaction.guild
    if not ws or not guild:
        return await interaction.response.send_message("This server is not configured as a PBX workspace.", ephemeral=True)
    voice = guild.get_channel(int(ws.get("voice_channel_id") or 0))
    text = guild.get_channel(int(ws.get("text_channel_id") or 0))
    rows = bot.appdb.list_workspace_roles(ws["id"])
    role_lines = []
    for row in rows:
        role = guild.get_role(int(row["role_id"])) if str(row.get("role_id", "")).isdigit() else None
        label = role.mention if role else f"`{row.get('role_name') or row.get('role_id')}`"
        role_lines.append(f"{label}: {', '.join(row.get('capabilities', [])) or 'no capabilities'}")
    await interaction.response.send_message(
        f"**{ws['alias']} PBX configuration**\n"
        f"Voice: {voice.mention if voice else '`not configured`'}\n"
        f"Notifications: {text.mention if text else '`not configured`'}\n"
        f"Roles:\n" + ("\n".join(role_lines) if role_lines else "`none configured`") +
        "\n\nA server administrator can change this with `/pbx setup`.",
        ephemeral=True,
    )


@pbx.command(name="join", description="Connect PBX audio to this workspace's configured voice channel.")
@require_capability("panel_access")
async def pbx_join(interaction: discord.Interaction):
    ws = workspace_for_interaction(interaction); await interaction.response.defer(ephemeral=True)
    try:
        vc = await bot.bridge.ensure_voice(ws["id"]); bot.bridge.schedule_voice_idle_disconnect(ws["id"]); await interaction.followup.send(f"Connected to **{vc.channel.name}** for testing; it will leave automatically when idle.", ephemeral=True)
    except Exception as exc: await interaction.followup.send(f"Join failed: `{exc}`", ephemeral=True)


@pbx.command(name="leave", description="Disconnect this workspace from PBX voice.")
@require_capability("workspace_admin")
async def pbx_leave(interaction: discord.Interaction):
    ws=workspace_for_interaction(interaction);await bot.bridge.disconnect_voice(ws["id"]);await interaction.response.send_message("Disconnected.",ephemeral=True)


@pbx.command(name="dial", description="Dial a phone number or PBX extension through this workspace.")
@app_commands.describe(number="Phone number or PBX extension", caller_id="Optional permitted outbound caller ID")
@require_capability("dial")
async def pbx_dial(interaction: discord.Interaction, number: str, caller_id: str = ""):
    ws=workspace_for_interaction(interaction);await interaction.response.defer(ephemeral=True)
    try:
        n,c,name,uid=bot.web._queue_web_outbound(number,caller_id,"",False,"discord",workspace_ids=[ws["id"]],operator_user_id=str(interaction.user.id),operator_name=interaction.user.display_name)
        await interaction.followup.send(f"Queued **{name or n}** (`{uid[:8]}`) using `{c or 'PBX default'}`.",ephemeral=True)
    except Exception as exc:await interaction.followup.send(f"Dial failed: `{exc}`",ephemeral=True)


@pbx.command(name="hangup", description="Hang up one call in this workspace.")
@app_commands.describe(call_uuid="Call UUID; omit when there is exactly one call in this workspace")
@require_capability("bridge")
async def pbx_hangup(interaction: discord.Interaction, call_uuid: str = ""):
    ws=workspace_for_interaction(interaction);calls=[s for s in bot.bridge.get_sessions() if ws["id"] in getattr(s,"workspace_ids",[])]
    uid=call_uuid.strip() or (calls[0].call_uuid if len(calls)==1 else "")
    ok=bool(uid and await bot.bridge.hangup(uid));await interaction.response.send_message("Call disconnected." if ok else "Specify a valid call UUID.",ephemeral=True)


@pbx.command(name="hangup-all", description="Hang up every active call belonging to this Discord workspace.")
@require_capability("bridge")
async def pbx_hangup_all(interaction: discord.Interaction):
    ws = workspace_for_interaction(interaction)
    sessions = [s for s in bot.bridge.get_sessions() if ws and ws["id"] in list(getattr(s, "workspace_ids", []) or [])]
    for uid in list(bot.web._auto_redial):
        row = bot.web.call_history.get_by_uuid(uid) or {}
        if ws["id"] in list(row.get("workspace_ids", []) or []):
            bot.web._cancel_auto_redial(uid)
    disconnected = 0
    for session in sessions:
        uid = str(getattr(session, "call_uuid", "") or "")
        if uid and await bot.bridge.hangup(uid):
            disconnected += 1
            bot.web.call_history.event(uid, "hangup-all", actor_user_id=str(interaction.user.id), actor_name=interaction.user.display_name, workspace_id=ws["id"])
    await interaction.response.send_message(f"Disconnected **{disconnected}** call(s) in **{ws['alias']}**.", ephemeral=True)


@pbx.command(name="recent", description="Show recent calls for this Discord workspace.")
@require_capability("history")
async def pbx_recent(interaction: discord.Interaction):
    ws=workspace_for_interaction(interaction);rows=bot.web.call_history.list_calls(limit=8,workspace_id=ws["id"])["calls"]
    if not rows:return await interaction.response.send_message("No calls yet.",ephemeral=True)
    lines=[f"**{r.get('direction','')}** `{r.get('contact_name') or r.get('number')}` · {r.get('outcome') or r.get('state')} · {int(r.get('duration') or 0)}s" for r in rows]
    await interaction.response.send_message("\n".join(lines),ephemeral=True)


bot.tree.add_command(pbx)


async def _forward_soundboard_to_pbx(workspace_id: str, sound_id: int, volume: float):
    if not bot.bridge.get_sessions(): return
    url=f"https://cdn.discordapp.com/soundboard-sounds/{sound_id}";volume=max(0.0,min(float(volume),2.0));temp_path=None
    def download():
        req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"*/*"})
        with urllib.request.urlopen(req,timeout=15) as r:data=r.read()
        f=tempfile.NamedTemporaryFile(prefix="discord-soundboard-",suffix=".ogg",delete=False);f.write(data);f.close();return f.name
    try:
        temp_path=await asyncio.to_thread(download)
        proc=await asyncio.create_subprocess_exec("ffmpeg","-hide_banner","-loglevel","error","-i",temp_path,"-filter:a",f"volume={volume:.3f}","-f","s16le","-acodec","pcm_s16le","-ar","48000","-ac","2","pipe:1",stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        pcm,_=await proc.communicate();pseudo=-abs(hash((workspace_id,sound_id,asyncio.get_running_loop().time())));loop=asyncio.get_running_loop();tick=loop.time()
        for pos in range(0,len(pcm),DISCORD_FRAME_BYTES):
            frame=pcm[pos:pos+DISCORD_FRAME_BYTES].ljust(DISCORD_FRAME_BYTES,b"\x00");bot.bridge.push_discord_pcm(workspace_id,pseudo,frame);tick+=.02;await asyncio.sleep(max(0,tick-loop.time()))
    except Exception:log.exception("Soundboard forwarding failed")
    finally:
        if temp_path:
            try:os.unlink(temp_path)
            except OSError:pass


@bot.event
async def on_voice_channel_effect(effect: discord.VoiceChannelEffect):
    if effect.sound is None:return
    ws=bot.workspaces.workspace_for_guild(effect.channel.guild.id)
    if not ws or str(effect.channel.id)!=str(ws.get("voice_channel_id","")):return
    asyncio.create_task(_forward_soundboard_to_pbx(ws["id"],int(effect.sound.id),float(effect.sound.volume)))


async def main():
    await bot.start_services()
    token=bot.secrets.get("discord_bot_token",config.discord_token)
    if not token:
        log.warning("Discord token is not configured. Web/setup remains online; configure it and restart the container.")
        stop=asyncio.Event()
        loop=asyncio.get_running_loop()
        for sig in (signal.SIGINT,signal.SIGTERM):
            try:loop.add_signal_handler(sig,stop.set)
            except NotImplementedError:pass
        await stop.wait();await bot.close();return
    try:
        await bot.start(token)
    finally:
        if not bot.is_closed(): await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
