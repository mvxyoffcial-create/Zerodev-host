#!/usr/bin/env python3
"""
Production-Ready Application Hosting Platform
Similar to Koyeb/Railway/Render - Hosts ANY type of application 24/7

Features:
- Multi-application support (Telegram bots, APIs, Discord bots, workers)
- Real-time log streaming with SSE
- Process management with auto-restart
- Web terminal for package management
- MongoDB persistence
- Modern admin dashboard
- REST API
- Auto-start on server boot

Author: Application Hosting Platform
Version: 1.0.0
"""

import os
import sys
import json
import time
import signal
import threading
import subprocess
import traceback
import logging
from datetime import datetime, timedelta
from queue import Queue, Empty
from functools import wraps
from typing import Dict, List, Optional, Any

# Flask imports
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for, Response, stream_with_context
from flask_cors import CORS

# MongoDB imports
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import ConnectionFailure, OperationFailure

# System monitoring
import psutil

# Environment variables
from dotenv import load_dotenv

# ═══════════════════════════════════════════════════════════════════════
# CONFIGURATION & CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

load_dotenv()

# Admin credentials
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "venuboy")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "venuboy")
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production-2024")

# MongoDB configuration
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://Zerobothost:zerobothost@cluster0.bl0tf2.mongodb.net/?appName=Cluster0")
DB_NAME = "hosting_platform"

# Application settings
MAX_LOG_LINES = 1000
LOG_CLEANUP_DAYS = 7
HEALTH_CHECK_INTERVAL = 30
MAX_RESTART_ATTEMPTS = 5
PROCESS_TIMEOUT = 10

# Port configuration
PORT = int(os.getenv("PORT", 10000))

# ═══════════════════════════════════════════════════════════════════════
# FLASK APP SETUP
# ═══════════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.secret_key = SECRET_KEY
CORS(app)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# MONGODB SETUP
# ═══════════════════════════════════════════════════════════════════════

try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping')
    db = mongo_client[DB_NAME]
    
    # Collections
    applications_collection = db.applications
    logs_collection = db.logs
    storage_collection = db.storage
    
    # Create indexes
    logs_collection.create_index([("app_id", ASCENDING), ("timestamp", DESCENDING)])
    applications_collection.create_index([("app_id", ASCENDING)], unique=True)
    
    logger.info("✅ MongoDB connected successfully")
except Exception as e:
    logger.error(f"❌ MongoDB connection failed: {e}")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════════
# GLOBAL STATE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════

# Store running processes: {app_id: subprocess.Popen}
running_processes: Dict[str, subprocess.Popen] = {}

# Store log queues for real-time streaming: {app_id: Queue}
log_queues: Dict[str, Queue] = {}

# Store log capture threads: {app_id: Thread}
log_threads: Dict[str, threading.Thread] = {}

# Store virtual environments: {app_id: venv_path}
venv_paths: Dict[str, str] = {}

# Platform is running flag
platform_running = True

# ═══════════════════════════════════════════════════════════════════════
# APPLICATION TEMPLATES
# ═══════════════════════════════════════════════════════════════════════

TEMPLATES = {
    "telegram_bot_ptb": {
        "name": "Telegram Bot (python-telegram-bot)",
        "requirements": ["python-telegram-bot>=20.0"],
        "code": '''from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Bot is running on hosting platform!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Available commands:\\n/start - Start bot\\n/help - Show this message")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    
    print("Bot started successfully!")
    app.run_polling()

if __name__ == "__main__":
    main()
'''
    },
    
    "flask_api": {
        "name": "Flask API",
        "requirements": ["Flask>=3.0.0", "flask-cors>=4.0.0"],
        "code": '''from flask import Flask, jsonify, request
from flask_cors import CORS
import os

app = Flask(__name__)
CORS(app)

@app.route('/')
def home():
    return jsonify({
        "status": "running",
        "message": "API is live!",
        "endpoints": ["/", "/health", "/api/data"]
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

@app.route('/api/data', methods=['GET', 'POST'])
def data():
    if request.method == 'POST':
        return jsonify({"message": "Data received", "data": request.json})
    return jsonify({"message": "Send POST request with data"})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"API started on port {port}")
    app.run(host='0.0.0.0', port=port)
'''
    },
    
    "fastapi_app": {
        "name": "FastAPI Application",
        "requirements": ["fastapi>=0.104.0", "uvicorn>=0.24.0"],
        "code": '''from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import os

app = FastAPI(title="FastAPI App", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {
        "status": "running",
        "message": "FastAPI is live!",
        "docs": "/docs"
    }

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.post("/api/data")
async def create_data(data: dict):
    return {"message": "Data received", "data": data}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print(f"FastAPI started on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
'''
    },
    
    "discord_bot": {
        "name": "Discord Bot",
        "requirements": ["discord.py>=2.3.0"],
        "code": '''import discord
from discord.ext import commands
import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_DISCORD_BOT_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')

@bot.command(name='hello')
async def hello(ctx):
    await ctx.send(f'Hello {ctx.author.name}! Bot is running on hosting platform!')

@bot.command(name='ping')
async def ping(ctx):
    await ctx.send(f'Pong! Latency: {round(bot.latency * 1000)}ms')

print("Starting Discord bot...")
bot.run(BOT_TOKEN)
'''
    },
    
    "worker": {
        "name": "Background Worker",
        "requirements": ["requests>=2.31.0"],
        "code": '''import time
import requests
from datetime import datetime

def main():
    print("Worker started successfully!")
    counter = 0
    
    while True:
        counter += 1
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{current_time}] Worker tick #{counter}")
        
        # Your background task logic here
        # Example: API call, database cleanup, scheduled job, etc.
        
        time.sleep(60)  # Run every 60 seconds

if __name__ == "__main__":
    main()
'''
    }
}

# ═══════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════

def generate_app_id() -> str:
    """Generate unique application ID"""
    return f"app_{int(time.time())}_{os.urandom(4).hex()}"

def login_required(f):
    """Decorator for routes requiring authentication"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def api_response(success: bool, message: str, data: Any = None, status: int = 200):
    """Standardized API response format"""
    response = {
        "success": success,
        "message": message,
        "timestamp": datetime.utcnow().isoformat()
    }
    if data is not None:
        response["data"] = data
    return jsonify(response), status

def log_message(app_id: str, level: str, message: str):
    """Log message to MongoDB and queue for streaming"""
    try:
        log_entry = {
            "app_id": app_id,
            "timestamp": datetime.utcnow(),
            "level": level,
            "message": message
        }
        
        # Save to MongoDB
        logs_collection.insert_one(log_entry)
        
        # Add to queue for live streaming
        if app_id in log_queues:
            try:
                log_queues[app_id].put_nowait(log_entry)
            except:
                pass
                
    except Exception as e:
        logger.error(f"Failed to log message for {app_id}: {e}")

def get_venv_path(app_id: str) -> str:
    """Get or create virtual environment path for application"""
    venv_dir = os.path.join(os.getcwd(), "venvs", app_id)
    return venv_dir

def create_virtualenv(app_id: str) -> str:
    """Create virtual environment for application"""
    try:
        venv_path = get_venv_path(app_id)
        
        if os.path.exists(venv_path):
            logger.info(f"Virtual environment already exists for {app_id}")
            return venv_path
            
        os.makedirs(os.path.dirname(venv_path), exist_ok=True)
        
        log_message(app_id, "INFO", "Creating virtual environment...")
        subprocess.run([sys.executable, "-m", "venv", venv_path], check=True)
        log_message(app_id, "INFO", "Virtual environment created successfully")
        
        return venv_path
        
    except Exception as e:
        log_message(app_id, "ERROR", f"Failed to create virtual environment: {e}")
        raise

def get_python_executable(app_id: str) -> str:
    """Get Python executable path for application's venv"""
    venv_path = get_venv_path(app_id)
    
    if sys.platform == "win32":
        return os.path.join(venv_path, "Scripts", "python.exe")
    else:
        return os.path.join(venv_path, "bin", "python")

def install_requirements(app_id: str, requirements: List[str]) -> bool:
    """Install pip requirements for application"""
    try:
        if not requirements:
            log_message(app_id, "INFO", "No requirements to install")
            return True
            
        venv_path = create_virtualenv(app_id)
        python_exe = get_python_executable(app_id)
        
        # Upgrade pip first
        log_message(app_id, "INFO", "Upgrading pip...")
        subprocess.run(
            [python_exe, "-m", "pip", "install", "--upgrade", "pip"],
            capture_output=True,
            timeout=120
        )
        
        # Install requirements
        for req in requirements:
            req = req.strip()
            if not req or req.startswith("#"):
                continue
                
            log_message(app_id, "INFO", f"Installing {req}...")
            
            result = subprocess.run(
                [python_exe, "-m", "pip", "install", req],
                capture_output=True,
                text=True,
                timeout=300
            )
            
            if result.returncode != 0:
                log_message(app_id, "ERROR", f"Failed to install {req}: {result.stderr}")
                return False
            else:
                log_message(app_id, "INFO", f"✅ Installed {req}")
        
        log_message(app_id, "INFO", "All requirements installed successfully")
        return True
        
    except subprocess.TimeoutExpired:
        log_message(app_id, "ERROR", "Package installation timed out")
        return False
    except Exception as e:
        log_message(app_id, "ERROR", f"Requirements installation failed: {e}")
        return False

def save_app_script(app_id: str, script: str) -> str:
    """Save application script to file"""
    try:
        scripts_dir = os.path.join(os.getcwd(), "scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        
        script_path = os.path.join(scripts_dir, f"{app_id}.py")
        
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)
        
        return script_path
        
    except Exception as e:
        logger.error(f"Failed to save script for {app_id}: {e}")
        raise

# ═══════════════════════════════════════════════════════════════════════
# LOG CAPTURE THREAD CLASS
# ═══════════════════════════════════════════════════════════════════════

class LogCaptureThread(threading.Thread):
    """Thread to capture stdout/stderr from subprocess"""
    
    def __init__(self, app_id: str, stream, stream_name: str):
        super().__init__(daemon=True)
        self.app_id = app_id
        self.stream = stream
        self.stream_name = stream_name
        self.running = True
    
    def run(self):
        """Capture and log output from stream"""
        try:
            for line in iter(self.stream.readline, ''):
                if not self.running:
                    break
                    
                if line:
                    line = line.strip()
                    level = "ERROR" if self.stream_name == "stderr" else "INFO"
                    log_message(self.app_id, level, line)
                    
        except Exception as e:
            logger.error(f"Log capture error for {self.app_id}: {e}")
        finally:
            self.stream.close()
    
    def stop(self):
        """Stop capturing logs"""
        self.running = False

# ═══════════════════════════════════════════════════════════════════════
# PROCESS MANAGEMENT FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════

def start_application(app_id: str) -> bool:
    """Start an application process"""
    try:
        # Get application from database
        app = applications_collection.find_one({"app_id": app_id})
        if not app:
            logger.error(f"Application {app_id} not found")
            return False
        
        # Check if already running
        if app_id in running_processes:
            proc = running_processes[app_id]
            if proc.poll() is None:
                log_message(app_id, "WARNING", "Application is already running")
                return True
        
        log_message(app_id, "INFO", f"Starting {app['app_name']}...")
        
        # Install requirements
        if app.get("requirements"):
            if not install_requirements(app_id, app["requirements"]):
                log_message(app_id, "ERROR", "Failed to install requirements")
                applications_collection.update_one(
                    {"app_id": app_id},
                    {"$set": {"status": "crashed", "updated_at": datetime.utcnow()}}
                )
                return False
        
        # Save script to file
        script_path = save_app_script(app_id, app["script"])
        
        # Prepare environment variables
        env = os.environ.copy()
        if app.get("env_vars"):
            env.update(app["env_vars"])
        
        # Get Python executable from venv
        python_exe = get_python_executable(app_id)
        
        # Start process
        process = subprocess.Popen(
            [python_exe, script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env
        )
        
        # Store process
        running_processes[app_id] = process
        
        # Create log queue
        log_queues[app_id] = Queue()
        
        # Start log capture threads
        stdout_thread = LogCaptureThread(app_id, process.stdout, "stdout")
        stderr_thread = LogCaptureThread(app_id, process.stderr, "stderr")
        
        stdout_thread.start()
        stderr_thread.start()
        
        log_threads[app_id] = [stdout_thread, stderr_thread]
        
        # Update database
        applications_collection.update_one(
            {"app_id": app_id},
            {
                "$set": {
                    "status": "running",
                    "pid": process.pid,
                    "last_started": datetime.utcnow(),
                    "updated_at": datetime.utcnow()
                }
            }
        )
        
        log_message(app_id, "INFO", f"✅ Application started successfully (PID: {process.pid})")
        return True
        
    except Exception as e:
        log_message(app_id, "ERROR", f"Failed to start application: {e}")
        logger.error(f"Start application error: {traceback.format_exc()}")
        
        applications_collection.update_one(
            {"app_id": app_id},
            {"$set": {"status": "crashed", "updated_at": datetime.utcnow()}}
        )
        return False

def stop_application(app_id: str, force: bool = False) -> bool:
    """Stop an application process"""
    try:
        if app_id not in running_processes:
            log_message(app_id, "WARNING", "Application is not running")
            return True
        
        process = running_processes[app_id]
        
        log_message(app_id, "INFO", "Stopping application...")
        
        # Stop log capture threads
        if app_id in log_threads:
            for thread in log_threads[app_id]:
                thread.stop()
        
        # Graceful shutdown
        if not force and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=PROCESS_TIMEOUT)
            except subprocess.TimeoutExpired:
                log_message(app_id, "WARNING", "Graceful shutdown timed out, forcing kill...")
                process.kill()
                process.wait()
        else:
            process.kill()
            process.wait()
        
        # Clean up
        del running_processes[app_id]
        if app_id in log_queues:
            del log_queues[app_id]
        if app_id in log_threads:
            del log_threads[app_id]
        
        # Update database
        applications_collection.update_one(
            {"app_id": app_id},
            {
                "$set": {
                    "status": "stopped",
                    "pid": None,
                    "updated_at": datetime.utcnow()
                }
            }
        )
        
        log_message(app_id, "INFO", "✅ Application stopped successfully")
        return True
        
    except Exception as e:
        log_message(app_id, "ERROR", f"Failed to stop application: {e}")
        logger.error(f"Stop application error: {traceback.format_exc()}")
        return False

def restart_application(app_id: str) -> bool:
    """Restart an application"""
    log_message(app_id, "INFO", "Restarting application...")
    
    # Increment restart count
    applications_collection.update_one(
        {"app_id": app_id},
        {"$inc": {"restart_count": 1}}
    )
    
    stop_application(app_id)
    time.sleep(2)
    return start_application(app_id)

def delete_application(app_id: str) -> bool:
    """Delete an application completely"""
    try:
        # Stop if running
        if app_id in running_processes:
            stop_application(app_id, force=True)
        
        # Delete from database
        applications_collection.delete_one({"app_id": app_id})
        logs_collection.delete_many({"app_id": app_id})
        storage_collection.delete_many({"app_id": app_id})
        
        # Delete files
        script_path = os.path.join(os.getcwd(), "scripts", f"{app_id}.py")
        if os.path.exists(script_path):
            os.remove(script_path)
        
        # Delete venv
        venv_path = get_venv_path(app_id)
        if os.path.exists(venv_path):
            import shutil
            shutil.rmtree(venv_path, ignore_errors=True)
        
        logger.info(f"Application {app_id} deleted successfully")
        return True
        
    except Exception as e:
        logger.error(f"Failed to delete application {app_id}: {e}")
        return False

# ═══════════════════════════════════════════════════════════════════════
# MONITORING THREAD
# ═══════════════════════════════════════════════════════════════════════

def monitoring_loop():
    """Background thread to monitor application health"""
    logger.info("Monitoring thread started")
    
    while platform_running:
        try:
            # Check all applications
            apps = list(applications_collection.find({"status": "running"}))
            
            for app in apps:
                app_id = app["app_id"]
                
                # Check if process is still running
                if app_id in running_processes:
                    process = running_processes[app_id]
                    
                    if process.poll() is not None:
                        # Process crashed
                        log_message(app_id, "ERROR", f"Application crashed with code {process.returncode}")
                        
                        # Clean up
                        del running_processes[app_id]
                        
                        # Update database
                        applications_collection.update_one(
                            {"app_id": app_id},
                            {
                                "$set": {
                                    "status": "crashed",
                                    "pid": None,
                                    "updated_at": datetime.utcnow()
                                }
                            }
                        )
                        
                        # Auto-restart if enabled
                        if app.get("auto_restart", True):
                            restart_count = app.get("restart_count", 0)
                            
                            if restart_count < MAX_RESTART_ATTEMPTS:
                                log_message(app_id, "INFO", f"Auto-restarting (attempt {restart_count + 1}/{MAX_RESTART_ATTEMPTS})...")
                                restart_application(app_id)
                            else:
                                log_message(app_id, "ERROR", f"Max restart attempts ({MAX_RESTART_ATTEMPTS}) reached")
                    else:
                        # Update uptime
                        if app.get("last_started"):
                            uptime = (datetime.utcnow() - app["last_started"]).total_seconds()
                            applications_collection.update_one(
                                {"app_id": app_id},
                                {"$set": {"uptime_seconds": int(uptime)}}
                            )
                else:
                    # Process not found but marked as running
                    applications_collection.update_one(
                        {"app_id": app_id},
                        {
                            "$set": {
                                "status": "stopped",
                                "pid": None,
                                "updated_at": datetime.utcnow()
                            }
                        }
                    )
            
            time.sleep(HEALTH_CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"Monitoring error: {e}")
            time.sleep(HEALTH_CHECK_INTERVAL)
    
    logger.info("Monitoring thread stopped")

# ═══════════════════════════════════════════════════════════════════════
# HTML TEMPLATES
# ═══════════════════════════════════════════════════════════════════════

LOGIN_PAGE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - Hosting Platform</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .login-container {
            background: white;
            padding: 3rem;
            border-radius: 1rem;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            width: 100%;
            max-width: 400px;
        }
        h1 {
            color: #333;
            margin-bottom: 0.5rem;
            font-size: 2rem;
        }
        .subtitle {
            color: #666;
            margin-bottom: 2rem;
            font-size: 0.9rem;
        }
        .form-group {
            margin-bottom: 1.5rem;
        }
        label {
            display: block;
            margin-bottom: 0.5rem;
            color: #333;
            font-weight: 500;
        }
        input {
            width: 100%;
            padding: 0.75rem;
            border: 2px solid #e0e0e0;
            border-radius: 0.5rem;
            font-size: 1rem;
            transition: all 0.3s;
        }
        input:focus {
            outline: none;
            border-color: #667eea;
        }
        button {
            width: 100%;
            padding: 0.75rem;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 0.5rem;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.2s;
        }
        button:hover {
            transform: translateY(-2px);
        }
        .error {
            background: #fee;
            color: #c33;
            padding: 1rem;
            border-radius: 0.5rem;
            margin-bottom: 1rem;
            border-left: 4px solid #c33;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <h1>🚀 Hosting Platform</h1>
        <p class="subtitle">Sign in to manage your applications</p>
        
        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}
        
        <form method="POST">
            <div class="form-group">
                <label for="username">Username</label>
                <input type="text" id="username" name="username" required autofocus>
            </div>
            
            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password" required>
            </div>
            
            <button type="submit">Sign In</button>
        </form>
    </div>
</body>
</html>
'''

DASHBOARD_PAGE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard - Hosting Platform</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        :root {
            --primary: #667eea;
            --primary-dark: #5568d3;
            --success: #10b981;
            --error: #ef4444;
            --warning: #f59e0b;
            --bg: #f3f4f6;
            --card: #ffffff;
            --text: #1f2937;
            --text-light: #6b7280;
            --border: #e5e7eb;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.6;
        }
        
        .navbar {
            background: white;
            padding: 1rem 2rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .navbar h1 {
            font-size: 1.5rem;
            background: linear-gradient(135deg, #667eea, #764ba2);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        
        .btn {
            padding: 0.5rem 1rem;
            border: none;
            border-radius: 0.5rem;
            cursor: pointer;
            font-size: 0.9rem;
            font-weight: 500;
            transition: all 0.2s;
            text-decoration: none;
            display: inline-block;
        }
        
        .btn-primary {
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
        }
        
        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
        }
        
        .btn-danger {
            background: var(--error);
            color: white;
        }
        
        .btn-success {
            background: var(--success);
            color: white;
        }
        
        .btn-sm {
            padding: 0.35rem 0.75rem;
            font-size: 0.85rem;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 2rem;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 1.5rem;
            margin-bottom: 2rem;
        }
        
        .stat-card {
            background: var(--card);
            padding: 1.5rem;
            border-radius: 1rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        
        .stat-card h3 {
            color: var(--text-light);
            font-size: 0.875rem;
            font-weight: 500;
            margin-bottom: 0.5rem;
        }
        
        .stat-card .value {
            font-size: 2rem;
            font-weight: 700;
            color: var(--text);
        }
        
        .stat-card .icon {
            font-size: 2rem;
            margin-bottom: 0.5rem;
        }
        
        .card {
            background: var(--card);
            border-radius: 1rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            padding: 1.5rem;
            margin-bottom: 1.5rem;
        }
        
        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.5rem;
        }
        
        .card-header h2 {
            font-size: 1.25rem;
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
        }
        
        th, td {
            padding: 1rem;
            text-align: left;
            border-bottom: 1px solid var(--border);
        }
        
        th {
            background: var(--bg);
            font-weight: 600;
            color: var(--text-light);
            font-size: 0.875rem;
        }
        
        .status-badge {
            padding: 0.25rem 0.75rem;
            border-radius: 1rem;
            font-size: 0.75rem;
            font-weight: 600;
        }
        
        .status-running {
            background: #d1fae5;
            color: #065f46;
        }
        
        .status-stopped {
            background: #fee2e2;
            color: #991b1b;
        }
        
        .status-crashed {
            background: #fef3c7;
            color: #92400e;
        }
        
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.5);
            z-index: 1000;
            align-items: center;
            justify-content: center;
            padding: 1rem;
        }
        
        .modal.active {
            display: flex !important;
        }
        
        .modal-content {
            background: white;
            padding: 2rem;
            border-radius: 1rem;
            max-width: 600px;
            width: 90%;
            max-height: 90vh;
            overflow-y: auto;
        }
        
        .form-group {
            margin-bottom: 1.5rem;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 0.5rem;
            font-weight: 500;
        }
        
        .form-group input,
        .form-group select,
        .form-group textarea {
            width: 100%;
            padding: 0.75rem;
            border: 2px solid var(--border);
            border-radius: 0.5rem;
            font-size: 1rem;
        }
        
        .form-group textarea {
            font-family: 'Courier New', monospace;
            min-height: 300px;
        }
        
        .toast {
            position: fixed;
            bottom: 2rem;
            right: 2rem;
            background: white;
            padding: 1rem 1.5rem;
            border-radius: 0.5rem;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            display: none;
            z-index: 2000;
        }
        
        .toast.show {
            display: block;
            animation: slideIn 0.3s ease;
        }
        
        @keyframes slideIn {
            from { transform: translateX(400px); }
            to { transform: translateX(0); }
        }
        
        .logs-viewer {
            background: #1e1e1e;
            color: #d4d4d4;
            padding: 1rem;
            border-radius: 0.5rem;
            font-family: 'Courier New', monospace;
            font-size: 0.875rem;
            max-height: 500px;
            overflow-y: auto;
        }
        
        .log-line {
            margin-bottom: 0.25rem;
        }
        
        .log-info { color: #4fc3f7; }
        .log-error { color: #f44336; }
        .log-warning { color: #ff9800; }
        
        .action-buttons {
            display: flex;
            gap: 0.5rem;
        }
    </style>
</head>
<body>
    <div class="navbar">
        <h1>🚀 Hosting Platform</h1>
        <div>
            <a href="/logout" class="btn btn-danger btn-sm">Logout</a>
        </div>
    </div>
    
    <div class="container">
        <div class="stats-grid">
            <div class="stat-card">
                <div class="icon">📦</div>
                <h3>Total Applications</h3>
                <div class="value" id="stat-total">0</div>
            </div>
            
            <div class="stat-card">
                <div class="icon">✅</div>
                <h3>Running</h3>
                <div class="value" id="stat-running" style="color: var(--success);">0</div>
            </div>
            
            <div class="stat-card">
                <div class="icon">⏸️</div>
                <h3>Stopped</h3>
                <div class="value" id="stat-stopped" style="color: var(--error);">0</div>
            </div>
            
            <div class="stat-card">
                <div class="icon">💥</div>
                <h3>Crashed</h3>
                <div class="value" id="stat-crashed" style="color: var(--warning);">0</div>
            </div>
        </div>
        
        <div class="card">
            <div class="card-header">
                <h2>Applications</h2>
                <button class="btn btn-primary" onclick="showCreateModal(); return false;">+ Create Application</button>
            </div>
            
            <table id="apps-table">
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Type</th>
                        <th>Status</th>
                        <th>Uptime</th>
                        <th>Created</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody id="apps-tbody">
                    <tr>
                        <td colspan="6" style="text-align: center; padding: 2rem;">
                            Loading applications...
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>
    </div>
    
    <!-- Create Application Modal -->
    <div class="modal" id="create-modal">
        <div class="modal-content">
            <h2 style="margin-bottom: 1.5rem;">Create New Application</h2>
            
            <form id="create-form">
                <div class="form-group">
                    <label>Application Name</label>
                    <input type="text" id="app-name" required placeholder="My Telegram Bot">
                </div>
                
                <div class="form-group">
                    <label>Application Type</label>
                    <select id="app-type" onchange="loadTemplate()">
                        <option value="">Select Template...</option>
                        <option value="telegram_bot_ptb">Telegram Bot (python-telegram-bot)</option>
                        <option value="flask_api">Flask API</option>
                        <option value="fastapi_app">FastAPI Application</option>
                        <option value="discord_bot">Discord Bot</option>
                        <option value="worker">Background Worker</option>
                        <option value="custom">Custom (Blank)</option>
                    </select>
                </div>
                
                <div class="form-group">
                    <label>Python Script</label>
                    <textarea id="app-script" required placeholder="# Your Python code here"></textarea>
                </div>
                
                <div class="form-group">
                    <label>Requirements (one per line)</label>
                    <textarea id="app-requirements" rows="5" placeholder="flask>=3.0.0
python-telegram-bot>=20.0"></textarea>
                </div>
                
                <div class="form-group">
                    <label>Environment Variables (JSON format)</label>
                    <textarea id="app-env" rows="3" placeholder='{"BOT_TOKEN": "your_token_here", "API_KEY": "your_key"}'>{}</textarea>
                </div>
                
                <div class="form-group">
                    <label>
                        <input type="checkbox" id="auto-restart" checked> Auto-restart on crash
                    </label>
                    <label>
                        <input type="checkbox" id="auto-start" checked> Auto-start on server boot
                    </label>
                </div>
                
                <div style="display: flex; gap: 1rem;">
                    <button type="submit" class="btn btn-primary" style="flex: 1;">Create</button>
                    <button type="button" class="btn" onclick="closeCreateModal()" style="flex: 1; background: #ccc;">Cancel</button>
                </div>
            </form>
        </div>
    </div>
    
    <!-- Logs Modal -->
    <div class="modal" id="logs-modal">
        <div class="modal-content" style="max-width: 900px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
                <h2 id="logs-title">Application Logs</h2>
                <button class="btn btn-sm" onclick="closeLogsModal()">Close</button>
            </div>
            
            <div class="logs-viewer" id="logs-content">
                <div class="log-line">Loading logs...</div>
            </div>
        </div>
    </div>
    
    <!-- Toast Notification -->
    <div class="toast" id="toast"></div>
    
    <script>
        let currentLogsAppId = null;
        let logsEventSource = null;
        
        // Templates data
        const templates = {{ templates|tojson }};
        
        // Prevent default form submission and ensure buttons work
        document.addEventListener('DOMContentLoaded', function() {
            console.log('Dashboard loaded successfully');
            loadApplications();
            
            // Ensure modal events are properly bound
            const createBtn = document.querySelector('[onclick="showCreateModal()"]');
            if (createBtn) {
                createBtn.addEventListener('click', function(e) {
                    e.preventDefault();
                    showCreateModal();
                });
            }
        });
        
        // Load applications
        async function loadApplications() {
            try {
                const res = await fetch('/api/apps');
                const data = await res.json();
                
                if (data.success) {
                    renderApplications(data.data);
                    updateStats(data.data);
                } else {
                    console.error('Failed to load apps:', data.message);
                    // Show empty state
                    document.getElementById('apps-tbody').innerHTML = `
                        <tr>
                            <td colspan="6" style="text-align: center; padding: 2rem; color: var(--text-light);">
                                No applications yet. Create your first application!
                            </td>
                        </tr>
                    `;
                }
            } catch (error) {
                console.error('Error loading applications:', error);
                showToast('Failed to load applications', 'error');
                document.getElementById('apps-tbody').innerHTML = `
                    <tr>
                        <td colspan="6" style="text-align: center; padding: 2rem; color: var(--text-light);">
                            No applications yet. Create your first application!
                        </td>
                    </tr>
                `;
            }
        }
        
        function renderApplications(apps) {
            const tbody = document.getElementById('apps-tbody');
            
            if (!apps || apps.length === 0) {
                tbody.innerHTML = `
                    <tr>
                        <td colspan="6" style="text-align: center; padding: 2rem; color: var(--text-light);">
                            No applications yet. Create your first application!
                        </td>
                    </tr>
                `;
                return;
            }
            
            tbody.innerHTML = apps.map(app => `
                <tr>
                    <td><strong>${escapeHtml(app.app_name)}</strong></td>
                    <td>${escapeHtml(app.app_type || 'Custom')}</td>
                    <td>
                        <span class="status-badge status-${app.status}">
                            ${app.status.toUpperCase()}
                        </span>
                    </td>
                    <td>${formatUptime(app.uptime_seconds || 0)}</td>
                    <td>${new Date(app.created_at).toLocaleDateString()}</td>
                    <td>
                        <div class="action-buttons">
                            ${app.status === 'running' 
                                ? `<button class="btn btn-sm" style="background: var(--warning);" onclick="stopApp('${app.app_id}')">Stop</button>`
                                : `<button class="btn btn-success btn-sm" onclick="startApp('${app.app_id}')">Start</button>`
                            }
                            <button class="btn btn-primary btn-sm" onclick="viewLogs('${app.app_id}', '${escapeHtml(app.app_name)}')">Logs</button>
                            <button class="btn btn-sm" style="background: var(--primary);" onclick="restartApp('${app.app_id}')">Restart</button>
                            <button class="btn btn-danger btn-sm" onclick="deleteApp('${app.app_id}', '${escapeHtml(app.app_name)}')">Delete</button>
                        </div>
                    </td>
                </tr>
            `).join('');
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        function updateStats(apps) {
            document.getElementById('stat-total').textContent = apps.length;
            document.getElementById('stat-running').textContent = apps.filter(a => a.status === 'running').length;
            document.getElementById('stat-stopped').textContent = apps.filter(a => a.status === 'stopped').length;
            document.getElementById('stat-crashed').textContent = apps.filter(a => a.status === 'crashed').length;
        }
        
        function formatUptime(seconds) {
            if (seconds === 0) return '-';
            const hours = Math.floor(seconds / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);
            return `${hours}h ${minutes}m`;
        }
        
        // Modal functions
        function showCreateModal() {
            console.log('Opening create modal...');
            const modal = document.getElementById('create-modal');
            if (modal) {
                modal.classList.add('active');
                modal.style.display = 'flex';
                // Focus on first input
                setTimeout(() => {
                    const firstInput = document.getElementById('app-name');
                    if (firstInput) firstInput.focus();
                }, 100);
            } else {
                console.error('Modal element not found');
            }
        }
        
        function closeCreateModal() {
            const modal = document.getElementById('create-modal');
            if (modal) {
                modal.classList.remove('active');
                modal.style.display = 'none';
                document.getElementById('create-form').reset();
                document.getElementById('app-env').value = '{}';
            }
        }
        
        function loadTemplate() {
            const type = document.getElementById('app-type').value;
            console.log('Loading template:', type);
            
            if (type && type !== 'custom' && templates[type]) {
                document.getElementById('app-script').value = templates[type].code;
                document.getElementById('app-requirements').value = templates[type].requirements.join('\n');
                showToast('Template loaded successfully!', 'success');
            } else if (type === 'custom') {
                document.getElementById('app-script').value = '# Your custom Python code here\n\nif __name__ == "__main__":\n    print("Application started!")\n';
                document.getElementById('app-requirements').value = '';
                showToast('Blank template loaded', 'success');
            }
        }
        
        // Create application
        document.getElementById('create-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            e.stopPropagation();
            
            console.log('Form submitted');
            
            const submitBtn = e.target.querySelector('button[type="submit"]');
            const originalText = submitBtn.textContent;
            submitBtn.disabled = true;
            submitBtn.textContent = 'Creating...';
            
            try {
                const appName = document.getElementById('app-name').value.trim();
                const appType = document.getElementById('app-type').value;
                const script = document.getElementById('app-script').value;
                const requirements = document.getElementById('app-requirements').value
                    .split('\n')
                    .map(r => r.trim())
                    .filter(r => r && !r.startsWith('#'));
                
                if (!appName) {
                    showToast('Please enter an application name', 'error');
                    submitBtn.disabled = false;
                    submitBtn.textContent = originalText;
                    return;
                }
                
                if (!script) {
                    showToast('Please enter your Python script', 'error');
                    submitBtn.disabled = false;
                    submitBtn.textContent = originalText;
                    return;
                }
                
                let envVars = {};
                try {
                    const envText = document.getElementById('app-env').value.trim();
                    if (envText) {
                        envVars = JSON.parse(envText);
                    }
                } catch (error) {
                    showToast('Invalid JSON in environment variables', 'error');
                    submitBtn.disabled = false;
                    submitBtn.textContent = originalText;
                    return;
                }
                
                const autoRestart = document.getElementById('auto-restart').checked;
                const autoStart = document.getElementById('auto-start').checked;
                
                const payload = {
                    app_name: appName,
                    app_type: appType || 'custom',
                    script: script,
                    requirements: requirements,
                    env_vars: envVars,
                    auto_restart: autoRestart,
                    auto_start: autoStart
                };
                
                console.log('Creating app with payload:', payload);
                
                const res = await fetch('/api/apps/create', {
                    method: 'POST',
                    headers: { 
                        'Content-Type': 'application/json',
                        'Accept': 'application/json'
                    },
                    body: JSON.stringify(payload)
                });
                
                const data = await res.json();
                console.log('Response:', data);
                
                if (data.success) {
                    showToast('✅ Application created successfully!', 'success');
                    closeCreateModal();
                    setTimeout(() => {
                        loadApplications();
                    }, 500);
                } else {
                    showToast('❌ ' + (data.message || 'Failed to create application'), 'error');
                }
            } catch (error) {
                console.error('Create error:', error);
                showToast('❌ Failed to create application: ' + error.message, 'error');
            } finally {
                submitBtn.disabled = false;
                submitBtn.textContent = originalText;
            }
        });
        
        // Application actions
        async function startApp(appId) {
            try {
                const res = await fetch(`/api/apps/${appId}/start`, { method: 'POST' });
                const data = await res.json();
                showToast(data.message, data.success ? 'success' : 'error');
                if (data.success) loadApplications();
            } catch (error) {
                showToast('Failed to start application', 'error');
            }
        }
        
        async function stopApp(appId) {
            try {
                const res = await fetch(`/api/apps/${appId}/stop`, { method: 'POST' });
                const data = await res.json();
                showToast(data.message, data.success ? 'success' : 'error');
                if (data.success) loadApplications();
            } catch (error) {
                showToast('Failed to stop application', 'error');
            }
        }
        
        async function restartApp(appId) {
            try {
                const res = await fetch(`/api/apps/${appId}/restart`, { method: 'POST' });
                const data = await res.json();
                showToast(data.message, data.success ? 'success' : 'error');
                if (data.success) loadApplications();
            } catch (error) {
                showToast('Failed to restart application', 'error');
            }
        }
        
        async function deleteApp(appId, appName) {
            if (!confirm(`Are you sure you want to delete "${appName}"? This cannot be undone.`)) {
                return;
            }
            
            try {
                const res = await fetch(`/api/apps/${appId}`, { method: 'DELETE' });
                const data = await res.json();
                showToast(data.message, data.success ? 'success' : 'error');
                if (data.success) loadApplications();
            } catch (error) {
                showToast('Failed to delete application', 'error');
            }
        }
        
        // Logs viewer
        function viewLogs(appId, appName) {
            currentLogsAppId = appId;
            document.getElementById('logs-title').textContent = `Logs: ${appName}`;
            document.getElementById('logs-modal').classList.add('active');
            
            // Close existing connection
            if (logsEventSource) {
                logsEventSource.close();
            }
            
            // Start SSE connection
            logsEventSource = new EventSource(`/api/apps/${appId}/logs/stream`);
            
            const logsContent = document.getElementById('logs-content');
            logsContent.innerHTML = '';
            
            logsEventSource.onmessage = (event) => {
                const log = JSON.parse(event.data);
                const logLine = document.createElement('div');
                logLine.className = `log-line log-${log.level.toLowerCase()}`;
                
                const timestamp = new Date(log.timestamp).toLocaleTimeString();
                logLine.textContent = `[${timestamp}] [${log.level}] ${log.message}`;
                
                logsContent.appendChild(logLine);
                logsContent.scrollTop = logsContent.scrollHeight;
            };
            
            logsEventSource.onerror = () => {
                showToast('Lost connection to logs stream', 'error');
            };
        }
        
        function closeLogsModal() {
            document.getElementById('logs-modal').classList.remove('active');
            if (logsEventSource) {
                logsEventSource.close();
                logsEventSource = null;
            }
        }
        
        // Toast notification
        function showToast(message, type = 'info') {
            const toast = document.getElementById('toast');
            if (!toast) return;
            
            toast.textContent = message;
            
            // Set colors based on type
            if (type === 'success') {
                toast.style.background = '#10b981';
                toast.style.color = 'white';
            } else if (type === 'error') {
                toast.style.background = '#ef4444';
                toast.style.color = 'white';
            } else {
                toast.style.background = 'white';
                toast.style.color = '#1f2937';
            }
            
            toast.classList.add('show');
            
            setTimeout(() => {
                toast.classList.remove('show');
            }, 4000);
        }
        
        // Auto-refresh applications every 10 seconds
        setInterval(loadApplications, 10000);
        
        // Close modal when clicking outside
        document.addEventListener('click', function(e) {
            const createModal = document.getElementById('create-modal');
            const logsModal = document.getElementById('logs-modal');
            
            if (e.target === createModal) {
                closeCreateModal();
            }
            if (e.target === logsModal) {
                closeLogsModal();
            }
        });
    </script>
</body>
</html>
'''

# ═══════════════════════════════════════════════════════════════════════
# FLASK ROUTES - ADMIN PANEL
# ═══════════════════════════════════════════════════════════════════════

@app.route('/')
@login_required
def dashboard():
    """Main dashboard page"""
    return render_template_string(DASHBOARD_PAGE, templates=TEMPLATES)

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        else:
            return render_template_string(LOGIN_PAGE, error="Invalid credentials")
    
    return render_template_string(LOGIN_PAGE)

@app.route('/logout')
def logout():
    """Logout"""
    session.pop('logged_in', None)
    return redirect(url_for('login'))

# ═══════════════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════════════

@app.route('/api/apps/create', methods=['POST'])
@login_required
def api_create_app():
    """Create new application"""
    try:
        data = request.json
        
        # Validate required fields
        required = ['app_name', 'script']
        for field in required:
            if not data.get(field):
                return api_response(False, f"Missing required field: {field}", status=400)
        
        # Generate app ID
        app_id = generate_app_id()
        
        # Create application document
        app_doc = {
            "app_id": app_id,
            "app_name": data['app_name'],
            "app_type": data.get('app_type', 'custom'),
            "script": data['script'],
            "requirements": data.get('requirements', []),
            "env_vars": data.get('env_vars', {}),
            "auto_restart": data.get('auto_restart', True),
            "auto_start": data.get('auto_start', False),
            "status": "stopped",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "last_started": None,
            "uptime_seconds": 0,
            "restart_count": 0,
            "pid": None
        }
        
        # Save to database
        applications_collection.insert_one(app_doc)
        
        log_message(app_id, "INFO", f"Application '{data['app_name']}' created")
        
        return api_response(True, "Application created successfully", {"app_id": app_id})
        
    except Exception as e:
        logger.error(f"Create app error: {e}")
        return api_response(False, f"Failed to create application: {str(e)}", status=500)

@app.route('/api/apps', methods=['GET'])
@login_required
def api_list_apps():
    """List all applications"""
    try:
        apps = list(applications_collection.find({}, {'_id': 0}).sort('created_at', DESCENDING))
        return api_response(True, "Applications retrieved", apps)
    except Exception as e:
        return api_response(False, str(e), status=500)

@app.route('/api/apps/<app_id>', methods=['GET'])
@login_required
def api_get_app(app_id):
    """Get application details"""
    try:
        app = applications_collection.find_one({"app_id": app_id}, {'_id': 0})
        if not app:
            return api_response(False, "Application not found", status=404)
        return api_response(True, "Application retrieved", app)
    except Exception as e:
        return api_response(False, str(e), status=500)

@app.route('/api/apps/<app_id>', methods=['DELETE'])
@login_required
def api_delete_app(app_id):
    """Delete application"""
    try:
        if delete_application(app_id):
            return api_response(True, "Application deleted successfully")
        else:
            return api_response(False, "Failed to delete application", status=500)
    except Exception as e:
        return api_response(False, str(e), status=500)

@app.route('/api/apps/<app_id>/start', methods=['POST'])
@login_required
def api_start_app(app_id):
    """Start application"""
    try:
        if start_application(app_id):
            return api_response(True, "Application started successfully")
        else:
            return api_response(False, "Failed to start application", status=500)
    except Exception as e:
        return api_response(False, str(e), status=500)

@app.route('/api/apps/<app_id>/stop', methods=['POST'])
@login_required
def api_stop_app(app_id):
    """Stop application"""
    try:
        if stop_application(app_id):
            return api_response(True, "Application stopped successfully")
        else:
            return api_response(False, "Failed to stop application", status=500)
    except Exception as e:
        return api_response(False, str(e), status=500)

@app.route('/api/apps/<app_id>/restart', methods=['POST'])
@login_required
def api_restart_app(app_id):
    """Restart application"""
    try:
        if restart_application(app_id):
            return api_response(True, "Application restarted successfully")
        else:
            return api_response(False, "Failed to restart application", status=500)
    except Exception as e:
        return api_response(False, str(e), status=500)

@app.route('/api/apps/<app_id>/logs', methods=['GET'])
@login_required
def api_get_logs(app_id):
    """Get application logs"""
    try:
        limit = int(request.args.get('limit', MAX_LOG_LINES))
        logs = list(
            logs_collection
            .find({"app_id": app_id}, {'_id': 0})
            .sort('timestamp', DESCENDING)
            .limit(limit)
        )
        logs.reverse()
        return api_response(True, "Logs retrieved", logs)
    except Exception as e:
        return api_response(False, str(e), status=500)

@app.route('/api/apps/<app_id>/logs/stream')
@login_required
def api_stream_logs(app_id):
    """Stream logs via Server-Sent Events"""
    def generate():
        # Create queue for this client if not exists
        if app_id not in log_queues:
            log_queues[app_id] = Queue()
        
        queue = log_queues[app_id]
        
        # Send existing logs first
        try:
            logs = list(
                logs_collection
                .find({"app_id": app_id}, {'_id': 0})
                .sort('timestamp', DESCENDING)
                .limit(100)
            )
            logs.reverse()
            
            for log in logs:
                log['timestamp'] = log['timestamp'].isoformat()
                yield f"data: {json.dumps(log)}\n\n"
        except Exception as e:
            logger.error(f"Error sending existing logs: {e}")
        
        # Stream new logs
        while True:
            try:
                log = queue.get(timeout=30)
                log['timestamp'] = log['timestamp'].isoformat()
                yield f"data: {json.dumps(log)}\n\n"
            except Empty:
                # Send keepalive
                yield f": keepalive\n\n"
            except Exception as e:
                logger.error(f"Log stream error: {e}")
                break
    
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )

@app.route('/api/apps/<app_id>/logs', methods=['DELETE'])
@login_required
def api_clear_logs(app_id):
    """Clear application logs"""
    try:
        result = logs_collection.delete_many({"app_id": app_id})
        return api_response(True, f"Cleared {result.deleted_count} log entries")
    except Exception as e:
        return api_response(False, str(e), status=500)

@app.route('/api/apps/<app_id>/status', methods=['GET'])
@login_required
def api_get_status(app_id):
    """Get application status"""
    try:
        app = applications_collection.find_one({"app_id": app_id}, {'_id': 0})
        if not app:
            return api_response(False, "Application not found", status=404)
        
        status = {
            "app_id": app_id,
            "status": app.get("status"),
            "pid": app.get("pid"),
            "uptime_seconds": app.get("uptime_seconds", 0),
            "restart_count": app.get("restart_count", 0),
            "is_running": app_id in running_processes and running_processes[app_id].poll() is None
        }
        
        return api_response(True, "Status retrieved", status)
    except Exception as e:
        return api_response(False, str(e), status=500)

@app.route('/api/stats', methods=['GET'])
@login_required
def api_get_stats():
    """Get platform statistics"""
    try:
        total = applications_collection.count_documents({})
        running = applications_collection.count_documents({"status": "running"})
        stopped = applications_collection.count_documents({"status": "stopped"})
        crashed = applications_collection.count_documents({"status": "crashed"})
        
        stats = {
            "total": total,
            "running": running,
            "stopped": stopped,
            "crashed": crashed,
            "cpu_percent": psutil.cpu_percent(interval=1),
            "memory_percent": psutil.virtual_memory().percent
        }
        
        return api_response(True, "Statistics retrieved", stats)
    except Exception as e:
        return api_response(False, str(e), status=500)

# ═══════════════════════════════════════════════════════════════════════
# STARTUP & SHUTDOWN HANDLERS
# ═══════════════════════════════════════════════════════════════════════

def startup_handler():
    """Handle platform startup"""
    logger.info("=" * 70)
    logger.info("🚀 HOSTING PLATFORM STARTING")
    logger.info("=" * 70)
    
    try:
        # Start monitoring thread
        monitor_thread = threading.Thread(target=monitoring_loop, daemon=True)
        monitor_thread.start()
        logger.info("✅ Monitoring thread started")
        
        # Auto-start applications
        apps = list(applications_collection.find({"auto_start": True}))
        logger.info(f"Found {len(apps)} applications with auto-start enabled")
        
        for app in apps:
            app_id = app["app_id"]
            app_name = app["app_name"]
            logger.info(f"Auto-starting: {app_name} ({app_id})")
            
            # Start in background to avoid blocking
            threading.Thread(
                target=start_application,
                args=(app_id,),
                daemon=True
            ).start()
        
        logger.info("=" * 70)
        logger.info("✅ HOSTING PLATFORM STARTED SUCCESSFULLY")
        logger.info(f"📊 Dashboard: http://localhost:{PORT}")
        logger.info(f"👤 Username: {ADMIN_USERNAME}")
        logger.info("=" * 70)
        
    except Exception as e:
        logger.error(f"Startup error: {e}")
        logger.error(traceback.format_exc())

def shutdown_handler(signum=None, frame=None):
    """Handle platform shutdown"""
    global platform_running
    
    logger.info("=" * 70)
    logger.info("🛑 HOSTING PLATFORM SHUTTING DOWN")
    logger.info("=" * 70)
    
    platform_running = False
    
    try:
        # Stop all running applications
        app_ids = list(running_processes.keys())
        logger.info(f"Stopping {len(app_ids)} running applications...")
        
        for app_id in app_ids:
            try:
                stop_application(app_id, force=True)
                logger.info(f"✅ Stopped {app_id}")
            except Exception as e:
                logger.error(f"Error stopping {app_id}: {e}")
        
        # Close MongoDB connection
        mongo_client.close()
        logger.info("✅ MongoDB connection closed")
        
        logger.info("=" * 70)
        logger.info("✅ PLATFORM SHUTDOWN COMPLETE")
        logger.info("=" * 70)
        
    except Exception as e:
        logger.error(f"Shutdown error: {e}")
    
    if signum is not None:
        sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)

# ═══════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    # Run startup handler
    startup_handler()
    
    # Start Flask app
    # Use 0.0.0.0 to allow external connections (required for Render)
    app.run(
        host='0.0.0.0',
        port=PORT,
        debug=False,  # Set to False in production
        threaded=True
    )
