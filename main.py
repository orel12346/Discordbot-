import discord
from discord import app_commands
from discord.ext import commands
import os
import requests
import logging
from dotenv import load_dotenv
import threading
from flask import Flask, jsonify
from datetime import datetime
from models import db, HWIDReset
from sqlalchemy import create_engine

# Load environment variables from .env file
load_dotenv()

# Create a simple Flask app - but we'll use the one from the Start application workflow
app = Flask(__name__)

# Configure the SQLAlchemy part of the app
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///hwid_resets.db")
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
    'pool_timeout': 30,
    'pool_size': 5,
    'max_overflow': 2
}
db.init_app(app)

# Create tables if they don't exist
with app.app_context():
    retries = 3
    while retries > 0:
        try:
            db.create_all()
            break
        except Exception as e:
            retries -= 1
            if retries == 0:
                logger.error(f"Failed to initialize database: {e}")
            else:
                logger.warning(f"Database connection attempt failed, retrying... ({retries} attempts left)")
                time.sleep(1)

@app.route('/')
def home():
    # Check if Discord bot workflow is running, if not try to restart it
    from subprocess import run, PIPE, STDOUT
    import os
    
    # When UptimeRobot pings this endpoint, check if the bot is alive
    discord_bot_running = True
    try:
        # Simple check if our Discord bot module is imported and running
        if 'bot' not in globals() or not bot.is_ready():
            discord_bot_running = False
    except:
        discord_bot_running = False
        
    # Return status with bot info
    return jsonify({
        "status": "online", 
        "bot": "running" if discord_bot_running else "restarting",
        "server_time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    })

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('hwid_reset_bot')

# Bot setup
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "MTMxODUxODI2ODYxMjMxMzE0MA.GDUiCt.hfJuMRFEzNlEuPsC05qWWR5NwrGsLQj4s0Ew_I")
SELLER_KEY = os.getenv("KEYAUTH_SELLER_KEY", "eab456d8c9d5e1a508249ae97a73ce4b")
GUILD_ID = int(os.getenv("GUILD_ID", "760572176939221022"))  # Your Discord server ID

# Ticket category IDs - these are the categories where the command can be used
TICKET_CATEGORY_IDS = [988391011380252712, 988391012210728980]

# Maximum number of HWID resets allowed per month
MAX_RESETS = 2

# Setting up bot intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Database utility functions for HWID resets
def get_reset_info(key):
    """Get reset information for a key from the database."""
    with app.app_context():
        # Look for the key in the database or create a new entry
        reset_info = HWIDReset.query.filter_by(key=key).first()
        if not reset_info:
            reset_info = HWIDReset(key=key, reset_count=0, is_invalid=False)
            db.session.add(reset_info)
            db.session.commit()
            
        # Collect the data we need while in session
        data = {
            'key': reset_info.key,
            'reset_count': reset_info.reset_count,
            'is_invalid': reset_info.is_invalid,
            'last_reset': reset_info.last_reset,
            'id': reset_info.id
        }
        return data

def update_reset_count(key, increment=True):
    """Update the reset count for a key and mark the reset time."""
    with app.app_context():
        # Get the reset info as a database object
        reset_info = HWIDReset.query.filter_by(key=key).first()
        if not reset_info:
            reset_info = HWIDReset(key=key, reset_count=0, is_invalid=False)
            db.session.add(reset_info)
        
        if increment:
            reset_info.reset_count += 1
            reset_info.last_reset = datetime.utcnow()
        
        db.session.commit()
        
        # Return the data as a dictionary to avoid session issues
        return {
            'key': reset_info.key,
            'reset_count': reset_info.reset_count,
            'is_invalid': reset_info.is_invalid,
            'last_reset': reset_info.last_reset,
            'id': reset_info.id
        }

def mark_key_invalid(key):
    """Mark a key as invalid."""
    with app.app_context():
        reset_info = HWIDReset.query.filter_by(key=key).first()
        if not reset_info:
            reset_info = HWIDReset(key=key, reset_count=0, is_invalid=True)
            db.session.add(reset_info)
        else:
            reset_info.is_invalid = True
        
        db.session.commit()
        
        # Return the data as a dictionary to avoid session issues
        return {
            'key': reset_info.key,
            'reset_count': reset_info.reset_count,
            'is_invalid': reset_info.is_invalid,
            'last_reset': reset_info.last_reset,
            'id': reset_info.id
        }

def is_key_invalid(key):
    """Check if a key is marked as invalid."""
    with app.app_context():
        reset_info = HWIDReset.query.filter_by(key=key).first()
        if reset_info:
            return reset_info.is_invalid
        return False

# KeyAuth API integration
def reset_user_hwid(seller_key: str, user_key: str):
    """Reset a user's HWID using the KeyAuth API."""
    url = f"https://keyauth.win/api/seller/?sellerkey={seller_key}&type=resetuser&user={user_key}&format=json"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"API request error: {e}")
        return {"success": False, "message": f"API request failed: {str(e)}"}

@bot.tree.command(name="resethwid", description="Reset your HWID with a valid key")
@app_commands.describe(
    key="Your license key",
    reason="Reason for the HWID reset"
)
async def reset_hwid(interaction: discord.Interaction, key: str, reason: str):
    """Command to reset a user's HWID."""
    await interaction.response.defer()
    
    # Check for required environment variables
    if not SELLER_KEY:
        logger.error("Missing KEYAUTH_SELLER_KEY environment variable")
        await interaction.followup.send("Bot configuration error: Missing API keys. Please contact an administrator.")
        return
    
    # Check if the command is used in a valid ticket channel
    if TICKET_CATEGORY_IDS and interaction.channel.category_id not in TICKET_CATEGORY_IDS:
        valid_categories = ", ".join([f"<#{id}>" for id in TICKET_CATEGORY_IDS])
        await interaction.followup.send(f"This command can only be used in a ticket channel in these categories: {valid_categories}")
        return

    # Check if the key is flagged as invalid in the database
    if is_key_invalid(key):
        await interaction.followup.send("This key is flagged as invalid and cannot be reset. Please contact an administrator if you believe this is an error.")
        return

    # Check if the reason is valid
    invalid_reasons = ["share", "friend", "sell", "gave", "give", "borrow", "duplicate", "transfer"]
    if any(word in reason.lower() for word in invalid_reasons):
        mark_key_invalid(key)
        await interaction.followup.send("Invalid reason provided. Sharing or selling licenses is against our terms. This key is now flagged.")
        logger.warning(f"Key {key} flagged due to invalid reason: {reason}")
        return

    # Get the reset info from the database
    reset_info = get_reset_info(key)
    
    # Check if the user has reached the reset limit
    if reset_info['reset_count'] >= MAX_RESETS:
        await interaction.followup.send(f"You have reached the reset limit for this month ({MAX_RESETS} resets). Please contact an administrator if you need additional resets.")
        return
    
    # Call the reset_user_hwid function to reset HWID
    success_response = reset_user_hwid(SELLER_KEY, key)

    if success_response.get("success"):
        # Update the reset count in the database
        reset_info = update_reset_count(key, increment=True)
        
        # Create a nice embed for successful reset
        embed = discord.Embed(
            title="HWID Reset Successful",
            description=f"Your hardware ID has been reset successfully.",
            color=discord.Color.green()
        )
        embed.add_field(name="Key", value=key, inline=True)
        embed.add_field(name="Resets Used", value=f"{reset_info['reset_count']}/{MAX_RESETS}", inline=True)
        embed.set_footer(text="Thank you for using our service!")
        
        await interaction.followup.send(embed=embed)
        logger.info(f"HWID reset successful for key: {key}")
    else:
        # Create an embed for failed reset
        embed = discord.Embed(
            title="HWID Reset Failed",
            description=f"Failed to reset your hardware ID.",
            color=discord.Color.red()
        )
        embed.add_field(name="Error", value=success_response.get('message', 'Unknown error'), inline=False)
        embed.add_field(name="Key", value=key, inline=True)
        embed.set_footer(text="Please contact an administrator if you need assistance.")
        
        await interaction.followup.send(embed=embed)
        logger.error(f"HWID reset failed for key: {key}. Error: {success_response.get('message', 'Unknown error')}")

@bot.event
async def on_ready():
    """Event triggered when the bot is ready."""
    try:
        # Sync commands with Discord
        if GUILD_ID > 0:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            logger.info(f"Commands synced to guild ID: {GUILD_ID}")
        else:
            await bot.tree.sync()
            logger.info("Commands synced globally")
        
        # Set custom status
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="/resethwid **IN TICKET**"))
        
        logger.info(f"Bot logged in as {bot.user}")
        print(f"Bot logged in as {bot.user} - Commands synced!")
    except Exception as e:
        logger.error(f"Error during startup: {e}")
        print(f"Error during startup: {e}")

# Command to check how many resets a user has remaining
@bot.tree.command(name="checkresets", description="Check how many HWID resets you have remaining")
@app_commands.describe(key="Your license key")
async def check_resets(interaction: discord.Interaction, key: str):
    """Command to check how many HWID resets a user has remaining."""
    await interaction.response.defer()
    
    # Check if the key is flagged as invalid
    if is_key_invalid(key):
        await interaction.followup.send("This key is flagged as invalid. Please contact an administrator if you believe this is an error.")
        return
    
    # Get reset info from the database
    reset_info = get_reset_info(key)
    resets_used = reset_info['reset_count']
    resets_remaining = MAX_RESETS - resets_used
    
    embed = discord.Embed(
        title="HWID Reset Status",
        description=f"Here's your current HWID reset status:",
        color=discord.Color.blue()
    )
    embed.add_field(name="Key", value=key, inline=True)
    embed.add_field(name="Resets Used", value=f"{resets_used}/{MAX_RESETS}", inline=True)
    embed.add_field(name="Resets Remaining", value=resets_remaining, inline=True)
    
    # Add last reset time if available
    if reset_info['last_reset']:
        last_reset_time = reset_info['last_reset'].strftime("%Y-%m-%d %H:%M:%S UTC")
        embed.add_field(name="Last Reset", value=last_reset_time, inline=False)
    
    embed.set_footer(text=f"You are allowed up to {MAX_RESETS} HWID resets per month.")
    
    await interaction.followup.send(embed=embed)

@bot.command(name="ping")
async def ping(ctx):
    """Check if the bot is responsive."""
    await ctx.send(f"Pong! Bot latency: {round(bot.latency * 1000)}ms")

# Configure Flask to bind to 0.0.0.0
def run_flask():
    app.run(host='0.0.0.0', port=5000)

# Run the bot
if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        logger.critical("Missing DISCORD_BOT_TOKEN environment variable!")
        print("ERROR: Missing DISCORD_BOT_TOKEN environment variable!")
        exit(1)
    
    while True:
        try:
            print("Starting Discord bot...")
            bot.run(DISCORD_BOT_TOKEN)
        except Exception as e:
            logger.error(f"Bot encountered an error: {e}")
            print(f"Error occurred: {e}")
            print("Restarting bot in 5 seconds...")
            time.sleep(5)
