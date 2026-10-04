"""
Startup file for the Zero Hour Discord bot. Point the host's "main file" / startup file at this one.

The bot token can come from either:
  1. an environment variable named DISCORD_TOKEN (set it on the host's Startup/Variables page), or
  2. a file named .env in this folder containing one line:  DISCORD_TOKEN=your-bot-token
"""
import os
import sys

# Always run from this file's folder so the bot finds zerohour_stats.db and its other files.
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)


def load_env_file(path: str = ".env"):
    """Reads KEY=VALUE lines from a .env file (if there is one) into the environment."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()

token = os.environ.get("DISCORD_TOKEN")
if not token:
    raise SystemExit("No bot token found. Set DISCORD_TOKEN or create a .env file with DISCORD_TOKEN=your-token")

import zerohour_bot  # noqa: E402  (imported after the folder is set up)

zerohour_bot.client.run(token)
