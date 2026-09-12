import os
from dotenv import load_dotenv
from bot_runner import TicketBot

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CONFIG_PATH = os.getenv("CONFIG_PATH", "config.json")

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN manquant dans le fichier .env")
    bot = TicketBot(CONFIG_PATH)
    bot.run(TOKEN)