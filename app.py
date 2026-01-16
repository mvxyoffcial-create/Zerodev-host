"""
PROFESSIONAL APPLICATION HOSTING PLATFORM - KOYEB CLONE
Complete Python-based application hosting platform
Deploy ANY Python application 24/7
"""

import os
import sys
import json
import time
import uuid
import signal
import atexit
import hashlib
import logging
import threading
import subprocess
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Generator
from functools import wraps
from queue import Queue
from collections import defaultdict

# Third-party imports
from flask import Flask, render_template_string, request, jsonify, Response, session, redirect, url_for, send_file, stream_with_context
from flask_cors import CORS
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import PyMongoError
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
import psutil
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ============================================================================
# CONFIGURATION & CONSTANTS
# ============================================================================

class Config:
    """Application configuration"""
    
    # Security
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
    ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
    ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')
    
    # MongoDB
    MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017/')
    MONGO_DB = os.getenv('MONGO_DB', 'app_hosting')
    
    # Platform settings
    MAX_APPS = 50
    MAX_LOG_LINES = 1000
    MAX_TERMINAL_HISTORY = 100
    AUTO_RESTART_DELAY = 5  # seconds
    MONITOR_INTERVAL = 30  # seconds
    COMMAND_TIMEOUT = 60  # seconds
    
    # Paths
    APP_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'apps')
    LOG_BUFFER_SIZE = 10000
    
    # Security settings
    SESSION_TIMEOUT = timedelta(hours=8)
    RATE_LIMITS = {
        'api': 100,  # requests per minute
        'login': 5,  # attempts per minute
        'terminal': 10  # commands per minute
    }
    
    # Allowed terminal commands (whitelist for security)
    ALLOWED_COMMANDS = [
        'pwd', 'ls', 'll', 'cd', 'cat', 'head', 'tail', 'grep',
        'find', 'pip', 'python', 'python3', 'echo', 'env',
        'ps', 'top', 'htop', 'df', 'du', 'free', 'whoami',
        'uname', 'date', 'w', 'curl', 'wget', 'git'
    ]
    
    # Dangerous commands (blacklist)
    DANGEROUS_PATTERNS = [
        'rm -rf', 'rm /*', 'rm -rf /', 'dd if=', 'mkfs',
        ':(){:|:&};:', '> /dev/sda', 'chmod -R 777 /',
        'wget -O-', 'curl | sh', 'bash <('
    ]

# Ensure app data directory exists
os.makedirs(Config.APP_DATA_DIR, exist_ok=True)

# ============================================================================
# MONGODB SETUP
# ============================================================================

try:
    client = MongoClient(Config.MONGO_URI, connectTimeoutMS=5000, serverSelectionTimeoutMS=5000)
    db = client[Config.MONGO_DB]
    
    # Collections
    apps_collection = db.applications
    logs_collection = db.logs
    storage_collection = db.storage
    sessions_collection = db.sessions
    metrics_collection = db.metrics
    
    # Create indexes
    apps_collection.create_index([('app_id', ASCENDING)], unique=True)
    apps_collection.create_index([('status', ASCENDING)])
    logs_collection.create_index([('app_id', ASCENDING), ('timestamp', DESCENDING)])
    logs_collection.create_index([('timestamp', DESCENDING)], expireAfterSeconds=2592000)  # 30 days TTL
    storage_collection.create_index([('app_id', ASCENDING), ('key', ASCENDING)], unique=True)
    
    # Test connection
    client.admin.command('ping')
    print("✓ MongoDB connection successful")
    mongo_available = True
    
except Exception as e:
    print(f"✗ MongoDB connection failed: {e}")
    print("Using in-memory storage (for demo only)")
    # Fallback to in-memory storage
    apps_collection = None
    logs_collection = None
    storage_collection = None
    mongo_available = False

# ============================================================================
# GLOBAL VARIABLES
# ============================================================================

# Store running applications {app_id: subprocess}
running_apps: Dict[str, subprocess.Popen] = {}

# Store application metadata {app_id: app_data}
app_metadata: Dict[str, Dict] = {}

# Log queues for real-time streaming {app_id: Queue}
log_queues: Dict[str, Queue] = {}

# Terminal sessions {app_id: terminal_session}
terminal_sessions: Dict[str, Dict] = {}

# Monitoring thread control
monitor_running = True
monitor_thread: Optional[threading.Thread] = None

# Application templates
APPLICATION_TEMPLATES = {
    'telegram_bot_ptb': {
        'name': 'Telegram Bot (python-telegram-bot)',
        'requirements': ['python-telegram-bot==20.3'],
        'script': '''from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 Hello! I'm running on the hosting platform!")
    print(f"User {update.effective_user.id} started the bot")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is running smoothly!")
    
async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ' '.join(context.args) if context.args else "Say something!"
    await update.message.reply_text(f"📝 You said: {text}")

def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN environment variable is not set!")
        return
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("echo", echo))
    
    print("🤖 Telegram Bot started successfully!")
    print(f"✅ Bot token loaded: {BOT_TOKEN[:10]}...")
    
    app.run_polling()

if __name__ == "__main__":
    main()'''
    },
    
    'flask_api': {
        'name': 'Flask API',
        'requirements': ['flask==3.0.0', 'flask-cors==4.0.0'],
        'script': '''from flask import Flask, jsonify, request
from flask_cors import CORS
import os
import time

app = Flask(__name__)
CORS(app)
start_time = time.time()

@app.route('/')
def home():
    return jsonify({
        "status": "running",
        "service": "Flask API",
        "timestamp": time.time()
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy", "uptime": time.time() - start_time})

@app.route('/api/data', methods=['GET'])
def get_data():
    return jsonify({"data": [1, 2, 3, 4, 5], "count": 5})

@app.route('/api/echo', methods=['POST'])
def echo():
    data = request.get_json()
    return jsonify({"echo": data, "received": True})

@app.route('/api/env')
def show_env():
    # Show non-sensitive environment variables
    env_vars = {k: v if 'KEY' not in k and 'TOKEN' not in k and 'SECRET' not in k else '***' 
                for k, v in os.environ.items()}
    return jsonify({"environment": env_vars})

if __name__ == '__main__':
    # Use PORT environment variable for cloud platforms
    port = int(os.getenv('PORT', 5000))
    host = os.getenv('HOST', '0.0.0.0')
    print(f"🌐 Flask API starting on http://{host}:{port}")
    app.run(host=host, port=port, debug=False)'''
    },
    
    'fastapi': {
        'name': 'FastAPI',
        'requirements': ['fastapi==0.104.1', 'uvicorn==0.24.0'],
        'script': '''from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import uvicorn
from typing import Optional

app = FastAPI(title="FastAPI Service", version="1.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Item(BaseModel):
    name: str
    description: Optional[str] = None
    price: float
    tax: Optional[float] = None

@app.get("/")
async def root():
    return {
        "service": "FastAPI",
        "status": "running",
        "docs": "/docs",
        "redoc": "/redoc"
    }

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/items/{item_id}")
async def read_item(item_id: int, q: Optional[str] = None):
    return {"item_id": item_id, "q": q}

@app.post("/items/")
async def create_item(item: Item):
    return {"item": item, "created": True}

@app.get("/env")
async def show_env():
    env = {k: '***' if any(secret in k.lower() for secret in ['key', 'token', 'secret', 'pass']) else v
           for k, v in os.environ.items()}
    return {"environment": env}

if __name__ == "__main__":
    # Use PORT environment variable for cloud platforms
    port = int(os.getenv('PORT', 8000))
    host = os.getenv('HOST', '0.0.0.0')
    print("🚀 FastAPI starting on http://{}:{}".format(host, port))
    print("📚 Documentation: http://{}:{}/docs".format(host, port))
    uvicorn.run(app, host=host, port=port, log_level="info")'''
    },
    
    'discord_bot': {
        'name': 'Discord Bot',
        'requirements': ['discord.py==2.3.2'],
        'script': '''import discord
from discord.ext import commands
import os
import asyncio

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'✅ Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')
    
    # Set status
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="the hosting platform"
        )
    )

@bot.command()
async def ping(ctx):
    """Check bot latency"""
    latency = round(bot.latency * 1000)
    await ctx.send(f'🏓 Pong! {latency}ms')

@bot.command()
async def info(ctx):
    """Show bot information"""
    embed = discord.Embed(
        title="Bot Information",
        description="Running on Application Hosting Platform",
        color=discord.Color.blue()
    )
    embed.add_field(name="Creator", value="Hosting Platform", inline=True)
    embed.add_field(name="Server Count", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="User Count", value=str(len(bot.users)), inline=True)
    embed.add_field(name="Uptime", value="24/7", inline=True)
    await ctx.send(embed=embed)

@bot.command()
async def echo(ctx, *, message: str):
    """Repeat a message"""
    await ctx.send(f"📢 {message}")

@bot.command()
async def status(ctx):
    """Check bot status"""
    await ctx.send("✅ Bot is online and running smoothly!")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    print("❌ DISCORD_TOKEN environment variable is not set!")
    exit(1)

print("🤖 Discord Bot starting...")
bot.run(DISCORD_TOKEN)'''
    },
    
    'background_worker': {
        'name': 'Background Worker',
        'requirements': ['schedule==1.2.0'],
        'script': '''import time
import random
import os
from datetime import datetime
import schedule

def task1():
    print(f"[{datetime.now()}] Task 1 executed - Processing data...")
    # Simulate work
    time.sleep(random.uniform(0.1, 0.5))
    print(f"[{datetime.now()}] Task 1 completed")

def task2():
    print(f"[{datetime.now()}] Task 2 executed - Sending notifications...")
    # Simulate API call
    time.sleep(random.uniform(0.2, 0.8))
    print(f"[{datetime.now()}] Task 2 completed")

def task3():
    print(f"[{datetime.now()}] Task 3 executed - Cleaning up...")
    # Simulate cleanup
    time.sleep(random.uniform(0.05, 0.3))
    print(f"[{datetime.now()}] Task 3 completed")

def health_check():
    print(f"[{datetime.now()}] Health check - Worker is running")
    print(f"Environment keys: {list(os.environ.keys())}")

def main():
    print("👷 Background Worker starting...")
    print(f"Worker ID: {os.getenv('WORKER_ID', 'default')}")
    print(f"Start time: {datetime.now()}")
    
    # Schedule tasks
    schedule.every(30).seconds.do(task1)
    schedule.every(1).minutes.do(task2)
    schedule.every(5).minutes.do(task3)
    schedule.every(2).minutes.do(health_check)
    
    print("📅 Scheduler started. Running tasks:")
    print("  • Task 1: Every 30 seconds")
    print("  • Task 2: Every 1 minute")
    print("  • Task 3: Every 5 minutes")
    print("  • Health check: Every 2 minutes")
    
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("👋 Worker stopped by user")
    except Exception as e:
        print(f"❌ Worker error: {e}")

if __name__ == "__main__":
    main()'''
    },
    
    'custom': {
        'name': 'Custom Application',
        'requirements': [],
        'script': '''# Custom Python Application
# Write your code here

import os
import time
from datetime import datetime

def main():
    print("🚀 Custom application started!")
    print(f"Start time: {datetime.now()}")
    print(f"App name: {os.getenv('APP_NAME', 'Unknown')}")
    
    try:
        counter = 0
        while True:
            print(f"[{datetime.now()}] Application running... Iteration: {counter}")
            counter += 1
            time.sleep(10)  # Run every 10 seconds
    except KeyboardInterrupt:
        print("👋 Application stopped gracefully")
    except Exception as e:
        print(f"❌ Application error: {e}")

if __name__ == "__main__":
    main()'''
    }
}

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def log_system(message: str, level: str = "INFO"):
    """Log system messages"""
    timestamp = datetime.now().isoformat()
    print(f"[{timestamp}] [{level}] {message}")

def generate_app_id(name: str) -> str:
    """Generate unique app ID from name"""
    name_slug = ''.join(c for c in name.lower() if c.isalnum() or c == '-')
    unique_hash = hashlib.md5(f"{name_slug}{time.time()}".encode()).hexdigest()[:8]
    return f"{name_slug}-{unique_hash}"

def get_app_directory(app_id: str) -> str:
    """Get directory path for application"""
    return os.path.join(Config.APP_DATA_DIR, app_id)

def create_app_directory(app_id: str) -> str:
    """Create directory for application files"""
    app_dir = get_app_directory(app_id)
    os.makedirs(app_dir, exist_ok=True)
    return app_dir

def save_app_script(app_id: str, script: str) -> str:
    """Save application script to file"""
    app_dir = create_app_directory(app_id)
    script_path = os.path.join(app_dir, 'main.py')
    
    with open(script_path, 'w', encoding='utf-8') as f:
        f.write(script)
    
    return script_path

def save_requirements(app_id: str, requirements: List[str]) -> str:
    """Save requirements to file"""
    app_dir = create_app_directory(app_id)
    req_path = os.path.join(app_dir, 'requirements.txt')
    
    with open(req_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(requirements))
    
    return req_path

def install_requirements(app_id: str, requirements: List[str]) -> bool:
    """Install pip requirements for application"""
    if not requirements:
        return True
    
    app_dir = get_app_directory(app_id)
    req_file = save_requirements(app_id, requirements)
    
    try:
        log_system(f"Installing requirements for {app_id}", "INFO")
        result = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '-r', req_file],
            cwd=app_dir,
            capture_output=True,
            text=True,
            timeout=300  # 5 minutes timeout
        )
        
        if result.returncode == 0:
            log_system(f"Requirements installed for {app_id}", "INFO")
            return True
        else:
            log_system(f"Failed to install requirements for {app_id}: {result.stderr}", "ERROR")
            return False
            
    except subprocess.TimeoutExpired:
        log_system(f"Requirements installation timeout for {app_id}", "ERROR")
        return False
    except Exception as e:
        log_system(f"Error installing requirements for {app_id}: {e}", "ERROR")
        return False

def validate_python_syntax(script: str) -> tuple[bool, str]:
    """Validate Python syntax"""
    try:
        compile(script, '<string>', 'exec')
        return True, "Syntax is valid"
    except SyntaxError as e:
        return False, f"Syntax error: {e}"
    except Exception as e:
        return False, f"Validation error: {e}"

def get_system_stats() -> Dict[str, Any]:
    """Get system resource statistics"""
    try:
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        # Count running processes
        running_count = sum(1 for app in app_metadata.values() 
                          if app.get('status') == 'running')
        
        return {
            'cpu_percent': cpu_percent,
            'memory_percent': memory.percent,
            'memory_used_gb': memory.used / (1024**3),
            'memory_total_gb': memory.total / (1024**3),
            'disk_percent': disk.percent,
            'disk_used_gb': disk.used / (1024**3),
            'disk_total_gb': disk.total / (1024**3),
            'running_apps': running_count,
            'total_apps': len(app_metadata),
            'platform_uptime': int(time.time() - platform_start_time)
        }
    except Exception as e:
        log_system(f"Error getting system stats: {e}", "ERROR")
        return {}

def requires_auth(f):
    """Decorator for authentication"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'error': 'Unauthorized'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def rate_limit(key: str, limit: int, period: int = 60):
    """Simple rate limiting decorator"""
    requests = defaultdict(list)
    
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            now = time.time()
            client_key = f"{key}:{request.remote_addr}"
            
            # Clean old requests
            requests[client_key] = [req_time for req_time in requests[client_key] 
                                   if now - req_time < period]
            
            if len(requests[client_key]) >= limit:
                return jsonify({'error': 'Rate limit exceeded'}), 429
            
            requests[client_key].append(now)
            return f(*args, **kwargs)
        return decorated
    return decorator

# ============================================================================
# LOG CAPTURE THREAD CLASS
# ============================================================================

class LogCaptureThread(threading.Thread):
    """Thread to capture stdout/stderr from subprocess"""
    
    def __init__(self, app_id: str, process: subprocess.Popen):
        super().__init__(daemon=True)
        self.app_id = app_id
        self.process = process
        self.running = True
        self.queue = log_queues.get(app_id, Queue(maxsize=Config.LOG_BUFFER_SIZE))
        
    def run(self):
        """Monitor process output and capture logs"""
        stdout = self.process.stdout
        stderr = self.process.stderr
        
        # Set file descriptors to non-blocking
        import fcntl
        for pipe in [stdout, stderr]:
            if pipe:
                fd = pipe.fileno()
                fl = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        
        while self.running and self.process.poll() is None:
            try:
                # Read stdout
                if stdout:
                    try:
                        line = stdout.readline()
                        if line:
                            self._process_line(line.strip(), 'INFO')
                    except:
                        pass
                
                # Read stderr
                if stderr:
                    try:
                        line = stderr.readline()
                        if line:
                            self._process_line(line.strip(), 'ERROR')
                    except:
                        pass
                
                time.sleep(0.1)
                
            except Exception as e:
                log_system(f"Log capture error for {self.app_id}: {e}", "ERROR")
                time.sleep(1)
        
        log_system(f"Log capture stopped for {self.app_id}", "INFO")
    
    def _process_line(self, line: str, level: str):
        """Process and store log line"""
        if not line:
            return
        
        timestamp = datetime.now()
        log_entry = {
            'app_id': self.app_id,
            'timestamp': timestamp,
            'level': level,
            'message': line
        }
        
        # Store in MongoDB
        if mongo_available and logs_collection is not None:
            try:
                logs_collection.insert_one(log_entry)
            except Exception as e:
                log_system(f"Failed to store log in MongoDB: {e}", "ERROR")
        
        # Add to real-time queue
        try:
            self.queue.put_nowait(log_entry)
        except:
            pass  # Queue is full
    
    def stop(self):
        """Stop the log capture thread"""
        self.running = False

# ============================================================================
# PROCESS MANAGEMENT FUNCTIONS
# ============================================================================

def start_application(app_id: str) -> tuple[bool, str]:
    """Start an application"""
    if app_id in running_apps:
        return False, "Application is already running"
    
    app_data = app_metadata.get(app_id)
    if not app_data:
        return False, "Application not found"
    
    app_dir = get_app_directory(app_id)
    script_path = os.path.join(app_dir, 'main.py')
    
    if not os.path.exists(script_path):
        return False, "Application script not found"
    
    try:
        # Create environment for the process
        env = os.environ.copy()
        env.update(app_data.get('env_vars', {}))
        env['APP_ID'] = app_id
        env['APP_NAME'] = app_data['name']
        env['PYTHONUNBUFFERED'] = '1'  # Ensure real-time output
        
        # Create log queue if not exists
        if app_id not in log_queues:
            log_queues[app_id] = Queue(maxsize=Config.LOG_BUFFER_SIZE)
        
        # Start the process
        process = subprocess.Popen(
            [sys.executable, script_path],
            cwd=app_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        # Store process
        running_apps[app_id] = process
        
        # Start log capture thread
        log_thread = LogCaptureThread(app_id, process)
        log_thread.start()
        
        # Update metadata
        app_data['status'] = 'running'
        app_data['pid'] = process.pid
        app_data['last_started'] = datetime.now()
        app_data['restart_count'] = app_data.get('restart_count', 0) + 1
        
        # Update in MongoDB
        if mongo_available and apps_collection is not None:
            apps_collection.update_one(
                {'app_id': app_id},
                {'$set': {
                    'status': 'running',
                    'pid': process.pid,
                    'last_started': datetime.now(),
                    '$inc': {'restart_count': 1}
                }}
            )
        
        log_system(f"Application {app_id} started (PID: {process.pid})", "INFO")
        return True, f"Application started successfully (PID: {process.pid})"
        
    except Exception as e:
        log_system(f"Failed to start application {app_id}: {e}", "ERROR")
        return False, f"Failed to start: {str(e)}"

def stop_application(app_id: str, force: bool = False) -> tuple[bool, str]:
    """Stop an application gracefully"""
    if app_id not in running_apps:
        return False, "Application is not running"
    
    process = running_apps[app_id]
    app_data = app_metadata.get(app_id, {})
    
    try:
        if process.poll() is None:  # Process is still running
            if force:
                # Force kill
                process.kill()
                log_system(f"Force killed application {app_id}", "WARNING")
            else:
                # Graceful shutdown
                process.terminate()
                
                # Wait for graceful shutdown
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    # Force kill if not responding
                    process.kill()
                    log_system(f"Force killed after timeout: {app_id}", "WARNING")
        
        # Remove from running apps
        del running_apps[app_id]
        
        # Update metadata
        if app_data:
            app_data['status'] = 'stopped'
            app_data['pid'] = None
            
            # Calculate uptime
            if app_data.get('last_started'):
                uptime = (datetime.now() - app_data['last_started']).total_seconds()
                app_data['uptime_seconds'] = app_data.get('uptime_seconds', 0) + uptime
        
        # Update in MongoDB
        if mongo_available and apps_collection is not None:
            apps_collection.update_one(
                {'app_id': app_id},
                {'$set': {'status': 'stopped', 'pid': None}}
            )
        
        log_system(f"Application {app_id} stopped", "INFO")
        return True, "Application stopped successfully"
        
    except Exception as e:
        log_system(f"Error stopping application {app_id}: {e}", "ERROR")
        return False, f"Error stopping: {str(e)}"

def restart_application(app_id: str) -> tuple[bool, str]:
    """Restart an application"""
    # First stop
    success, message = stop_application(app_id)
    if not success:
        return False, f"Failed to stop: {message}"
    
    # Wait a moment
    time.sleep(2)
    
    # Then start
    return start_application(app_id)

def check_application_health(app_id: str) -> Dict[str, Any]:
    """Check application health and status"""
    app_data = app_metadata.get(app_id, {})
    process = running_apps.get(app_id)
    
    status = {
        'app_id': app_id,
        'name': app_data.get('name', 'Unknown'),
        'status': 'unknown',
        'running': False,
        'pid': None,
        'exit_code': None,
        'memory_usage': 0,
        'cpu_percent': 0,
        'uptime': 0,
        'restart_count': app_data.get('restart_count', 0)
    }
    
    if process:
        exit_code = process.poll()
        status['running'] = exit_code is None
        status['exit_code'] = exit_code
        status['pid'] = process.pid
        
        if exit_code is None:
            status['status'] = 'running'
            # Get resource usage
            try:
                ps_process = psutil.Process(process.pid)
                status['memory_usage'] = ps_process.memory_info().rss / 1024 / 1024  # MB
                status['cpu_percent'] = ps_process.cpu_percent(interval=0.1)
                
                # Calculate uptime
                if app_data.get('last_started'):
                    uptime = (datetime.now() - app_data['last_started']).total_seconds()
                    status['uptime'] = uptime
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        else:
            status['status'] = 'stopped'
    else:
        status['status'] = app_data.get('status', 'stopped')
        status['running'] = False
    
    return status

# ============================================================================
# MONITORING THREAD
# ============================================================================

def monitoring_worker():
    """Background thread to monitor application health"""
    log_system("Monitoring thread started", "INFO")
    
    while monitor_running:
        try:
            # Check each running application
            for app_id, process in list(running_apps.items()):
                if process.poll() is not None:  # Process died
                    log_system(f"Application {app_id} crashed with exit code {process.returncode}", "ERROR")
                    
                    # Update status
                    app_data = app_metadata.get(app_id)
                    if app_data:
                        app_data['status'] = 'crashed'
                        app_data['pid'] = None
                        
                        # Auto-restart if enabled
                        if app_data.get('auto_restart', False):
                            log_system(f"Auto-restarting {app_id}...", "INFO")
                            time.sleep(Config.AUTO_RESTART_DELAY)
                            start_application(app_id)
                    
                    # Remove from running apps
                    del running_apps[app_id]
            
            # Update uptime for running apps
            for app_id, app_data in app_metadata.items():
                if app_data.get('status') == 'running' and app_data.get('last_started'):
                    app_data['uptime_seconds'] = app_data.get('uptime_seconds', 0) + 1
            
            # Save to MongoDB periodically
            if mongo_available and apps_collection is not None and int(time.time()) % 300 == 0:  # Every 5 minutes
                for app_id, app_data in app_metadata.items():
                    apps_collection.update_one(
                        {'app_id': app_id},
                        {'$set': {
                            'status': app_data.get('status'),
                            'uptime_seconds': app_data.get('uptime_seconds', 0),
                            'updated_at': datetime.now()
                        }}
                    )
            
            time.sleep(1)
            
        except Exception as e:
            log_system(f"Monitoring error: {e}", "ERROR")
            time.sleep(5)

# ============================================================================
# FLASK APPLICATION
# ============================================================================

app = Flask(__name__)
app.secret_key = Config.SECRET_KEY
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
CORS(app)

# Store platform start time
platform_start_time = time.time()

# ============================================================================
# HTML TEMPLATES (REMAINS THE SAME - TOO LONG TO REPEAT)
# ============================================================================

# ... [HTML templates remain exactly the same as before] ...

# ============================================================================
# FLASK ROUTES - ADMIN PANEL (REMAINS THE SAME)
# ============================================================================

@app.route('/')
@requires_auth
def index():
    """Dashboard home page"""
    stats = get_system_stats()
    
    # Prepare applications for display
    apps_list = []
    for app_id, app_data in app_metadata.items():
        app_display = app_data.copy()
        app_display['app_id'] = app_id
        app_display['type_name'] = APPLICATION_TEMPLATES.get(app_data.get('type', {}), {}).get('name', 'Custom')
        
        # Format dates
        if isinstance(app_display.get('created_at'), str):
            try:
                app_display['created_at'] = datetime.fromisoformat(app_display['created_at'])
            except:
                app_display['created_at'] = datetime.now()
        
        apps_list.append(app_display)
    
    # Sort by creation date (newest first)
    apps_list.sort(key=lambda x: x.get('created_at', datetime.now()), reverse=True)
    
    return render_template_string(
        DASHBOARD_TEMPLATE,
        stats=stats,
        applications=apps_list,
        templates=APPLICATION_TEMPLATES
    )

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if username == Config.ADMIN_USERNAME and password == Config.ADMIN_PASSWORD:
            session['authenticated'] = True
            session['username'] = username
            session['login_time'] = datetime.now().isoformat()
            return redirect(url_for('index'))
        else:
            return render_template_string(LOGIN_TEMPLATE, error='Invalid credentials')
    
    # Check if already logged in
    if session.get('authenticated'):
        return redirect(url_for('index'))
    
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    """Logout user"""
    session.clear()
    return redirect(url_for('login'))

@app.route('/apps/<app_id>/logs')
@requires_auth
def view_logs(app_id):
    """Logs viewer page"""
    app_data = app_metadata.get(app_id)
    if not app_data:
        return redirect(url_for('index'))
    
    app_display = app_data.copy()
    app_display['app_id'] = app_id
    app_display['type_name'] = APPLICATION_TEMPLATES.get(app_data.get('type', {}), {}).get('name', 'Custom')
    
    return render_template_string(LOGS_TEMPLATE, app=app_display)

# ============================================================================
# API ROUTES (REMAINS THE SAME)
# ============================================================================

@app.route('/api/apps/create', methods=['POST'])
@requires_auth
@rate_limit('api', Config.RATE_LIMITS['api'])
def api_create_app():
    """Create a new application"""
    try:
        data = request.get_json()
        
        # Validate required fields
        if not data.get('name'):
            return jsonify({'error': 'Application name is required'}), 400
        
        if not data.get('script'):
            return jsonify({'error': 'Application script is required'}), 400
        
        # Validate Python syntax
        is_valid, message = validate_python_syntax(data['script'])
        if not is_valid:
            return jsonify({'error': f'Invalid Python syntax: {message}'}), 400
        
        # Generate app ID
        app_id = generate_app_id(data['name'])
        
        # Check if app already exists
        if app_id in app_metadata:
            return jsonify({'error': 'Application with this name already exists'}), 409
        
        # Prepare application data
        app_data = {
            'name': data['name'],
            'type': data.get('type', 'custom'),
            'script': data['script'],
            'requirements': data.get('requirements', []),
            'env_vars': data.get('env_vars', {}),
            'auto_start': data.get('auto_start', True),
            'auto_restart': data.get('auto_restart', True),
            'status': 'stopped',
            'created_at': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat(),
            'pid': None,
            'restart_count': 0,
            'uptime_seconds': 0
        }
        
        # Save to metadata
        app_metadata[app_id] = app_data
        
        # Save to MongoDB
        if mongo_available and apps_collection is not None:
            app_data_mongo = app_data.copy()
            app_data_mongo['app_id'] = app_id
            apps_collection.insert_one(app_data_mongo)
        
        # Create app directory and save files
        save_app_script(app_id, data['script'])
        if data.get('requirements'):
            save_requirements(app_id, data['requirements'])
        
        # Install requirements
        if data.get('requirements'):
            threading.Thread(
                target=install_requirements,
                args=(app_id, data['requirements']),
                daemon=True
            ).start()
        
        # Auto-start if enabled
        if data.get('auto_start', True):
            threading.Thread(
                target=start_application,
                args=(app_id,),
                daemon=True
            ).start()
        
        log_system(f"Application created: {app_id}", "INFO")
        return jsonify({
            'success': True,
            'app_id': app_id,
            'message': 'Application created successfully'
        })
        
    except Exception as e:
        log_system(f"Error creating application: {e}", "ERROR")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps')
@requires_auth
def api_list_apps():
    """List all applications"""
    apps_list = []
    
    for app_id, app_data in app_metadata.items():
        app_info = app_data.copy()
        app_info['app_id'] = app_id
        app_info['health'] = check_application_health(app_id)
        apps_list.append(app_info)
    
    return jsonify({'applications': apps_list})

@app.route('/api/apps/<app_id>', methods=['GET'])
@requires_auth
def api_get_app(app_id):
    """Get application details"""
    if app_id not in app_metadata:
        return jsonify({'error': 'Application not found'}), 404
    
    app_data = app_metadata[app_id].copy()
    app_data['app_id'] = app_id
    app_data['health'] = check_application_health(app_id)
    
    return jsonify(app_data)

@app.route('/api/apps/<app_id>', methods=['PUT'])
@requires_auth
def api_update_app(app_id):
    """Update application"""
    if app_id not in app_metadata:
        return jsonify({'error': 'Application not found'}), 404
    
    try:
        data = request.get_json()
        app_data = app_metadata[app_id]
        
        # Update fields
        if 'script' in data:
            # Validate syntax
            is_valid, message = validate_python_syntax(data['script'])
            if not is_valid:
                return jsonify({'error': f'Invalid Python syntax: {message}'}), 400
            
            app_data['script'] = data['script']
            save_app_script(app_id, data['script'])
        
        if 'requirements' in data:
            app_data['requirements'] = data['requirements']
            save_requirements(app_id, data['requirements'])
            
            # Install new requirements
            threading.Thread(
                target=install_requirements,
                args=(app_id, data['requirements']),
                daemon=True
            ).start()
        
        if 'env_vars' in data:
            app_data['env_vars'] = data['env_vars']
        
        if 'auto_restart' in data:
            app_data['auto_restart'] = data['auto_restart']
        
        app_data['updated_at'] = datetime.now().isoformat()
        
        # Update MongoDB
        if mongo_available and apps_collection is not None:
            apps_collection.update_one(
                {'app_id': app_id},
                {'$set': app_data}
            )
        
        return jsonify({'success': True, 'message': 'Application updated'})
        
    except Exception as e:
        log_system(f"Error updating application {app_id}: {e}", "ERROR")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>', methods=['DELETE'])
@requires_auth
def api_delete_app(app_id):
    """Delete application"""
    if app_id not in app_metadata:
        return jsonify({'error': 'Application not found'}), 404
    
    try:
        # Stop if running
        if app_id in running_apps:
            stop_application(app_id, force=True)
        
        # Remove from metadata
        del app_metadata[app_id]
        
        # Remove from MongoDB
        if mongo_available and apps_collection is not None:
            apps_collection.delete_one({'app_id': app_id})
        
        # Remove logs
        if mongo_available and logs_collection is not None:
            logs_collection.delete_many({'app_id': app_id})
        
        # Remove storage
        if mongo_available and storage_collection is not None:
            storage_collection.delete_many({'app_id': app_id})
        
        # Remove app directory
        app_dir = get_app_directory(app_id)
        if os.path.exists(app_dir):
            import shutil
            shutil.rmtree(app_dir)
        
        log_system(f"Application deleted: {app_id}", "INFO")
        return jsonify({'success': True, 'message': 'Application deleted'})
        
    except Exception as e:
        log_system(f"Error deleting application {app_id}: {e}", "ERROR")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/start', methods=['POST'])
@requires_auth
def api_start_app(app_id):
    """Start application"""
    if app_id not in app_metadata:
        return jsonify({'error': 'Application not found'}), 404
    
    success, message = start_application(app_id)
    
    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'error': message}), 500

@app.route('/api/apps/<app_id>/stop', methods=['POST'])
@requires_auth
def api_stop_app(app_id):
    """Stop application"""
    if app_id not in app_metadata:
        return jsonify({'error': 'Application not found'}), 404
    
    force = request.args.get('force', 'false').lower() == 'true'
    success, message = stop_application(app_id, force)
    
    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'error': message}), 500

@app.route('/api/apps/<app_id>/restart', methods=['POST'])
@requires_auth
def api_restart_app(app_id):
    """Restart application"""
    if app_id not in app_metadata:
        return jsonify({'error': 'Application not found'}), 404
    
    success, message = restart_application(app_id)
    
    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'error': message}), 500

@app.route('/api/apps/<app_id>/logs')
@requires_auth
def api_get_logs(app_id):
    """Get application logs"""
    if app_id not in app_metadata:
        return jsonify({'error': 'Application not found'}), 404
    
    try:
        offset = int(request.args.get('offset', 0))
        limit = int(request.args.get('limit', 100))
        
        logs = []
        
        # Try MongoDB first
        if mongo_available and logs_collection is not None:
            cursor = logs_collection.find(
                {'app_id': app_id},
                sort=[('timestamp', DESCENDING)],
                skip=offset,
                limit=limit
            )
            
            for doc in cursor:
                doc['_id'] = str(doc['_id'])
                logs.append(doc)
        else:
            # Fallback to in-memory (limited)
            pass
        
        return jsonify({'logs': logs})
        
    except Exception as e:
        log_system(f"Error getting logs for {app_id}: {e}", "ERROR")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/logs/stream')
@requires_auth
def api_log_stream(app_id):
    """Server-Sent Events for live logs"""
    if app_id not in app_metadata:
        return '', 404
    
    def generate():
        """Generate SSE events"""
        queue = log_queues.get(app_id)
        if not queue:
            queue = Queue()
            log_queues[app_id] = queue
        
        try:
            while True:
                try:
                    # Get log from queue with timeout
                    log_entry = queue.get(timeout=30)
                    
                    # Convert datetime to string for JSON
                    if isinstance(log_entry.get('timestamp'), datetime):
                        log_entry['timestamp'] = log_entry['timestamp'].isoformat()
                    
                    yield f"data: {json.dumps(log_entry)}\n\n"
                    
                except:
                    # Send heartbeat to keep connection alive
                    yield ": heartbeat\n\n"
                    
        except GeneratorExit:
            log_system(f"Log stream closed for {app_id}", "INFO")
    
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'
        }
    )

@app.route('/api/apps/<app_id>/logs', methods=['DELETE'])
@requires_auth
def api_clear_logs(app_id):
    """Clear application logs"""
    if app_id not in app_metadata:
        return jsonify({'error': 'Application not found'}), 404
    
    try:
        if mongo_available and logs_collection is not None:
            logs_collection.delete_many({'app_id': app_id})
        
        # Clear queue
        if app_id in log_queues:
            while not log_queues[app_id].empty():
                try:
                    log_queues[app_id].get_nowait()
                except:
                    break
        
        return jsonify({'success': True, 'message': 'Logs cleared'})
        
    except Exception as e:
        log_system(f"Error clearing logs for {app_id}: {e}", "ERROR")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/logs/download')
@requires_auth
def api_download_logs(app_id):
    """Download logs as text file"""
    if app_id not in app_metadata:
        return jsonify({'error': 'Application not found'}), 404
    
    try:
        # Get logs
        logs = []
        if mongo_available and logs_collection is not None:
            cursor = logs_collection.find(
                {'app_id': app_id},
                sort=[('timestamp', ASCENDING)]
            ).limit(10000)
            
            for doc in cursor:
                timestamp = doc['timestamp']
                if isinstance(timestamp, datetime):
                    timestamp = timestamp.strftime('%Y-%m-%d %H:%M:%S')
                
                logs.append(f"[{timestamp}] [{doc['level']}] {doc['message']}")
        
        # Create text file
        log_text = '\n'.join(logs)
        
        # Create response
        response = Response(
            log_text,
            mimetype='text/plain',
            headers={
                'Content-Disposition': f'attachment; filename={app_id}-logs.txt'
            }
        )
        
        return response
        
    except Exception as e:
        log_system(f"Error downloading logs for {app_id}: {e}", "ERROR")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/terminal', methods=['POST'])
@requires_auth
@rate_limit('terminal', Config.RATE_LIMITS['terminal'])
def api_terminal_command(app_id):
    """Execute terminal command"""
    if app_id not in app_metadata:
        return jsonify({'error': 'Application not found'}), 404
    
    try:
        data = request.get_json()
        command = data.get('command', '').strip()
        
        if not command:
            return jsonify({'error': 'Command is required'}), 400
        
        # Security checks
        command_lower = command.lower()
        
        # Check for dangerous patterns
        for pattern in Config.DANGEROUS_PATTERNS:
            if pattern in command_lower:
                return jsonify({'error': 'Command contains dangerous pattern'}), 403
        
        # Check if command is allowed
        first_word = command.split()[0]
        if first_word not in Config.ALLOWED_COMMANDS:
            return jsonify({'error': f'Command "{first_word}" is not allowed'}), 403
        
        # Execute command
        app_dir = get_app_directory(app_id)
        
        result = subprocess.run(
            command,
            shell=True,
            cwd=app_dir,
            capture_output=True,
            text=True,
            timeout=Config.COMMAND_TIMEOUT
        )
        
        output = {
            'stdout': result.stdout,
            'stderr': result.stderr,
            'returncode': result.returncode,
            'success': result.returncode == 0
        }
        
        return jsonify(output)
        
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Command timeout'}), 408
    except Exception as e:
        log_system(f"Terminal error for {app_id}: {e}", "ERROR")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/status')
@requires_auth
def api_app_status(app_id):
    """Get application status"""
    if app_id not in app_metadata:
        return jsonify({'error': 'Application not found'}), 404
    
    status = check_application_health(app_id)
    return jsonify(status)

@app.route('/api/apps/<app_id>/env', methods=['POST'])
@requires_auth
def api_update_env(app_id):
    """Update environment variables"""
    if app_id not in app_metadata:
        return jsonify({'error': 'Application not found'}), 404
    
    try:
        data = request.get_json()
        env_vars = data.get('env_vars', {})
        
        # Update metadata
        app_metadata[app_id]['env_vars'] = env_vars
        app_metadata[app_id]['updated_at'] = datetime.now().isoformat()
        
        # Update MongoDB
        if mongo_available and apps_collection is not None:
            apps_collection.update_one(
                {'app_id': app_id},
                {'$set': {'env_vars': env_vars, 'updated_at': datetime.now()}}
            )
        
        # Restart if running
        if app_id in running_apps:
            threading.Thread(
                target=restart_application,
                args=(app_id,),
                daemon=True
            ).start()
        
        return jsonify({'success': True, 'message': 'Environment variables updated'})
        
    except Exception as e:
        log_system(f"Error updating env for {app_id}: {e}", "ERROR")
        return jsonify({'error': str(e)}), 500

@app.route('/api/system/stats')
@requires_auth
def api_system_stats():
    """Get system statistics"""
    stats = get_system_stats()
    
    # Add application stats
    stats['total_applications'] = len(app_metadata)
    stats['running_applications'] = len(running_apps)
    stats['total_processes'] = len(app_metadata)
    
    return jsonify(stats)

# ============================================================================
# STARTUP & SHUTDOWN HANDLERS
# ============================================================================

def startup():
    """Initialize platform on startup"""
    global monitor_thread, app_metadata
    
    log_system("Starting Application Hosting Platform", "INFO")
    log_system(f"Python version: {sys.version}", "INFO")
    log_system(f"Platform: {sys.platform}", "INFO")
    
    # Load applications from MongoDB
    if mongo_available and apps_collection is not None:
        try:
            cursor = apps_collection.find({})
            for doc in cursor:
                app_id = doc.pop('app_id', None)
                if app_id:
                    app_metadata[app_id] = doc
            
            log_system(f"Loaded {len(app_metadata)} applications from database", "INFO")
            
        except Exception as e:
            log_system(f"Error loading applications from MongoDB: {e}", "ERROR")
    
    # Auto-start applications
    auto_start_count = 0
    for app_id, app_data in app_metadata.items():
        if app_data.get('auto_start', False):
            threading.Thread(
                target=start_application,
                args=(app_id,),
                daemon=True
            ).start()
            auto_start_count += 1
    
    log_system(f"Auto-starting {auto_start_count} applications", "INFO")
    
    # Start monitoring thread
    monitor_thread = threading.Thread(target=monitoring_worker, daemon=True)
    monitor_thread.start()
    
    log_system("Platform started successfully", "INFO")
    log_system("=" * 50, "INFO")

def shutdown():
    """Clean shutdown of platform"""
    global monitor_running
    
    log_system("Shutting down platform...", "INFO")
    
    # Stop monitoring
    monitor_running = False
    if monitor_thread and monitor_thread.is_alive():
        monitor_thread.join(timeout=5)
    
    # Stop all running applications
    log_system(f"Stopping {len(running_apps)} running applications...", "INFO")
    for app_id in list(running_apps.keys()):
        stop_application(app_id, force=True)
    
    # Save state to MongoDB
    if mongo_available and apps_collection is not None:
        try:
            for app_id, app_data in app_metadata.items():
                apps_collection.update_one(
                    {'app_id': app_id},
                    {'$set': app_data},
                    upsert=True
                )
        except Exception as e:
            log_system(f"Error saving state to MongoDB: {e}", "ERROR")
    
    log_system("Platform shutdown complete", "INFO")

# Register shutdown handler
atexit.register(shutdown)

# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

if __name__ == '__main__':
    # Run startup
    startup()
    
    # Print startup banner
    print("\n" + "="*60)
    print("🚀 APP HOST PRO - Professional Application Hosting Platform")
    print("="*60)
    print(f"📊 Dashboard: http://localhost:{os.getenv('PORT', 5000)}")
    print(f"👤 Admin: {Config.ADMIN_USERNAME}")
    print(f"📁 Applications: {len(app_metadata)} loaded")
    print(f"⚙️  Templates: {len(APPLICATION_TEMPLATES)} available")
    print("="*60 + "\n")
    
    # Get port from environment variable (Render/Railway provide this)
    port = int(os.getenv('PORT', 5000))
    
    # Start Flask app
    app.run(
        host='0.0.0.0',
        port=port,
        debug=False,
        threaded=True
    )
