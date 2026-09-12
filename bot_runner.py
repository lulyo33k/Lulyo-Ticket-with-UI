import os
import re
import json
import uuid
import asyncio
import discord
from discord import app_commands
from discord.ext import commands


# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------

def sanitize_channel_name(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return name[:90] if name else "utilisateur"


def has_manage_perms(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.guild_permissions.manage_guild


# ---------------------------------------------------------------------------
# Logique métier (chaque bot accède à SA config via interaction.client.data)
# ---------------------------------------------------------------------------

async def create_ticket(interaction: discord.Interaction, panel_id: str):
    bot = interaction.client
    guild = interaction.guild
    panel = bot.data["panels"].get(panel_id)

    if panel is None:
        await interaction.response.send_message(
            "❌ Ce système de tickets n'existe plus.", ephemeral=True
        )
        return

    category = guild.get_channel(panel["category_id"])
    if category is None or not isinstance(category, discord.CategoryChannel):
        await interaction.response.send_message(
            "❌ La catégorie configurée est introuvable. Contactez un administrateur.",
            ephemeral=True,
        )
        return

    staff_role = guild.get_role(panel["staff_role_id"])
    if staff_role is None:
        await interaction.response.send_message(
            "❌ Le rôle staff configuré est introuvable. Contactez un administrateur.",
            ephemeral=True,
        )
        return

    stale_ids = []
    for ch_id, info in bot.data["tickets"].items():
        if info["panel_id"] == panel_id and info["user_id"] == interaction.user.id:
            existing_channel = guild.get_channel(int(ch_id))
            if existing_channel:
                await interaction.response.send_message(
                    f"⚠️ Vous avez déjà un ticket ouvert : {existing_channel.mention}",
                    ephemeral=True,
                )
                return
            stale_ids.append(ch_id)

    if stale_ids:
        for ch_id in stale_ids:
            del bot.data["tickets"][ch_id]
        bot.save_config()

    bot_perms = category.permissions_for(guild.me)
    if not bot_perms.manage_channels or not bot_perms.manage_roles:
        await interaction.response.send_message(
            "❌ Je n'ai pas les permissions nécessaires pour créer un ticket ici.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        staff_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True, read_message_history=True
        ),
    }

    channel_name = f"ticket-{sanitize_channel_name(interaction.user.name)}"

    try:
        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"Ticket ouvert par {interaction.user} ({interaction.user.id})",
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Je n'ai pas la permission de créer un salon dans cette catégorie.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as e:
        await interaction.followup.send(
            f"❌ Erreur lors de la création du ticket : {e}", ephemeral=True
        )
        return

    bot.data["tickets"][str(channel.id)] = {
        "panel_id": panel_id,
        "user_id": interaction.user.id,
        "staff_role_id": staff_role.id,
    }
    bot.save_config()

    embed = discord.Embed(
        title="🎫 Ticket ouvert",
        description=(
            "Bienvenue dans votre ticket.\n"
            "Expliquez votre problème et un membre du staff vous répondra."
        ),
        color=discord.Color.green(),
    )

    try:
        await channel.send(
            content=f"{interaction.user.mention} {staff_role.mention}",
            embed=embed,
            view=TicketCloseView(),
        )
    except discord.HTTPException:
        pass

    await interaction.followup.send(f"✅ Votre ticket a été créé : {channel.mention}", ephemeral=True)


async def close_ticket(interaction: discord.Interaction):
    bot = interaction.client
    channel = interaction.channel
    info = bot.data["tickets"].get(str(channel.id))

    if info is None:
        await interaction.response.send_message(
            "❌ Ce salon n'est pas reconnu comme un ticket actif.", ephemeral=True
        )
        return

    staff_role = interaction.guild.get_role(info["staff_role_id"])
    is_owner = interaction.user.id == info["user_id"]
    is_staff = staff_role in interaction.user.roles if staff_role else False
    is_admin = has_manage_perms(interaction.user)

    if not (is_owner or is_staff or is_admin):
        await interaction.response.send_message(
            "❌ Vous n'avez pas la permission de fermer ce ticket.", ephemeral=True
        )
        return

    await interaction.response.send_message("🔒 Ce ticket va être fermé dans 5 secondes...")

    del bot.data["tickets"][str(channel.id)]
    bot.save_config()

    await asyncio.sleep(5)

    try:
        await channel.delete(reason=f"Ticket fermé par {interaction.user}")
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------------------
# Vues persistantes
# ---------------------------------------------------------------------------

class TicketPanelView(discord.ui.View):
    def __init__(self, panel_id: str, button_label: str = "Ouvrir un ticket"):
        super().__init__(timeout=None)
        self.panel_id = panel_id

        button = discord.ui.Button(
            label=button_label,
            style=discord.ButtonStyle.primary,
            emoji="🎫",
            custom_id=f"ticket_open_{panel_id}",
        )
        button.callback = self.open_ticket
        self.add_item(button)

    async def open_ticket(self, interaction: discord.Interaction):
        await create_ticket(interaction, self.panel_id)


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Fermer le ticket",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="ticket_close_button",
    )
    async def close_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await close_ticket(interaction)


# ---------------------------------------------------------------------------
# Slash command /set-ticket + gestion d'erreurs (par bot)
# ---------------------------------------------------------------------------

def register_commands(bot: "TicketBot"):

    @bot.tree.command(name="set-ticket", description="Crée un système de tickets dans ce salon")
    @app_commands.describe(
        titre="Titre du message",
        description="Description du message",
        bouton="Texte du bouton",
        categorie="Catégorie dans laquelle les tickets seront créés",
        staff="Rôle qui aura accès aux tickets",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def set_ticket(
        interaction: discord.Interaction,
        titre: str,
        description: str,
        bouton: str,
        categorie: discord.CategoryChannel,
        staff: discord.Role,
    ):
        if not has_manage_perms(interaction.user):
            await interaction.response.send_message(
                "❌ Vous devez être administrateur ou avoir « Gérer le serveur ».",
                ephemeral=True,
            )
            return

        bot_perms = categorie.permissions_for(interaction.guild.me)
        if not bot_perms.manage_channels or not bot_perms.manage_roles:
            await interaction.response.send_message(
                "❌ Je n'ai pas les permissions « Gérer les salons » / « Gérer les rôles » "
                "sur cette catégorie.",
                ephemeral=True,
            )
            return

        panel_id = uuid.uuid4().hex[:8]
        bot.data["panels"][panel_id] = {
            "title": titre,
            "description": description,
            "button_label": bouton,
            "category_id": categorie.id,
            "staff_role_id": staff.id,
            "guild_id": interaction.guild.id,
        }
        bot.save_config()

        embed = discord.Embed(title=titre, description=description, color=discord.Color.blurple())
        view = TicketPanelView(panel_id, bouton)

        try:
            await interaction.response.send_message(embed=embed, view=view)
            message = await interaction.original_response()
            bot.data["panels"][panel_id]["message_id"] = message.id
            bot.data["panels"][panel_id]["channel_id"] = message.channel.id
            bot.save_config()
        except discord.HTTPException as e:
            del bot.data["panels"][panel_id]
            bot.save_config()
            await interaction.followup.send(f"❌ Erreur lors de l'envoi : {e}", ephemeral=True)

    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
            message = "❌ Vous n'avez pas la permission d'utiliser cette commande."
        else:
            message = f"❌ Une erreur est survenue : {error}"

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    bot.tree.on_error = on_app_command_error


# ---------------------------------------------------------------------------
# Classe principale : une instance = un bot Discord avec sa propre config
# ---------------------------------------------------------------------------

class TicketBot(commands.Bot):
    def __init__(self, config_path: str):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.config_path = config_path
        self.data = self._load_config()
        register_commands(self)

    def _load_config(self) -> dict:
        if not os.path.exists(self.config_path) or os.path.getsize(self.config_path) == 0:
            data = {"panels": {}, "tickets": {}}
            self._write(data)
            return data
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            print(f"⚠️ {self.config_path} corrompu, réinitialisation.")
            data = {"panels": {}, "tickets": {}}
            self._write(data)
            return data
        data.setdefault("panels", {})
        data.setdefault("tickets", {})
        return data

    def _write(self, data: dict):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    def save_config(self):
        self._write(self.data)

    async def setup_hook(self):
        for panel_id, panel in self.data["panels"].items():
            self.add_view(TicketPanelView(panel_id, panel.get("button_label", "Ouvrir un ticket")))
        self.add_view(TicketCloseView())
        await self.tree.sync()

    async def on_ready(self):
        print(f"✅ Connecté en tant que {self.user} (ID: {self.user.id})")