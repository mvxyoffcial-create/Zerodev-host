"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    PROFESSIONAL APPLICATION HOSTING PLATFORM                  ║
║                         Similar to Koyeb/Railway/Render                       ║
║                                                                                ║
║  Host ANY application 24/7: Telegram bots, APIs, Discord bots, Workers, etc. ║
╚══════════════════════════════════════════════════════════════════════════════╝

Author: Production-Ready Hosting Platform
Version: 1.0.0
Python: 3.10+

DEPLOYMENT INSTRUCTIONS:
------------------------
Render.com:
1. Create new Web Service
2. Build Command: pip install -r requirements.txt
3. Start Command: gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:$PORT app:app
4. Environment Variables: MONGO_URI, SECRET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD

Railway:
1. Create new project from GitHub
2. Add environment variables
3. Railway auto-detects Python and runs with Procfile

VPS/Server:
1. Install Python 3.10+
2. pip install -r requirements.txt
3. Run with: gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:8080 app:app
4. Setup systemd service for auto-start
5. Configure nginx reverse proxy

"""

# ═══════════════════════════════════════════════════════════════════════════════
# IMPORTS
# ═══════════════════════════════════════════════════════════════════════════════

import os
import sys
import json
import time
import signal
import subprocess
import threading
import queue
import uuid
import hashlib
import re
import shutil
from datetime import datetime, timedelta
from functools import wraps
from collections import defaultdict

import psutil
from flask import (
    Flask, render_template_string, request, jsonify, 
    session, redirect, url_for, Response, stream_with_context
)
from flask_cors import CORS
from pymongo import MongoClient
from bson import ObjectId
import logging

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION & CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

# Admin credentials
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "venuboy")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "venuboy")

# MongoDB connection
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://Zerobothost:zerobothost@cluster0.bl0tf2.mongodb.net/?appName=Cluster0")

# Flask secret key
SECRET_KEY = os.getenv("SECRET_KEY", "your-super-secret-key-change-in-production-" + str(uuid.uuid4()))

# Platform settings
PLATFORM_NAME = "CloudHost Pro"
MAX_LOG_LINES = 1000
LOG_RETENTION_DAYS = 7
HEALTH_CHECK_INTERVAL = 30  # seconds
MAX_RESTART_ATTEMPTS = 5
COMMAND_TIMEOUT = 60  # seconds

# Safe terminal commands whitelist
SAFE_COMMANDS = ['pip', 'ls', 'pwd', 'cat', 'echo', 'python', 'which', 'whoami', 'date', 'env']

# ═══════════════════════════════════════════════════════════════════════════════
# FLASK APP INITIALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
CORS(app)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# MONGODB SETUP
# ═══════════════════════════════════════════════════════════════════════════════

try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.server_info()  # Test connection
    db = mongo_client['hosting_platform']
    
    # Collections
    applications_col = db['applications']
    logs_col = db['logs']
    storage_col = db['storage']
    
    # Create indexes
    applications_col.create_index('app_id', unique=True)
    logs_col.create_index([('app_id', 1), ('timestamp', -1)])
    storage_col.create_index([('app_id', 1), ('key', 1)])
    
    logger.info("✓ MongoDB connected successfully")
except Exception as e:
    logger.error(f"✗ MongoDB connection failed: {e}")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL VARIABLES
# ═══════════════════════════════════════════════════════════════════════════════

# Running applications: {app_id: subprocess.Popen object}
running_apps = {}

# Log queues for real-time streaming: {app_id: queue.Queue()}
log_queues = defaultdict(queue.Queue)

# Log capture threads: {app_id: threading.Thread}
log_threads = {}

# Application locks for thread-safe operations
app_locks = defaultdict(threading.Lock)

# Monitoring thread
monitoring_thread = None
shutdown_event = threading.Event()

# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def generate_app_id():
    """Generate unique application ID"""
    return f"app_{uuid.uuid4().hex[:12]}"

def hash_password(password):
    """Hash password for secure storage"""
    return hashlib.sha256(password.encode()).hexdigest()

def require_auth(f):
    """Decorator to require authentication"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def validate_python_code(code):
    """Validate Python syntax"""
    try:
        compile(code, '<string>', 'exec')
        return True, "Valid Python code"
    except SyntaxError as e:
        return False, f"Syntax Error: {str(e)}"

def is_safe_command(command):
    """Check if terminal command is safe to execute"""
    cmd_parts = command.strip().split()
    if not cmd_parts:
        return False
    
    base_cmd = cmd_parts[0]
    
    # Check if command is in whitelist
    if base_cmd not in SAFE_COMMANDS:
        return False
    
    # Block dangerous patterns
    dangerous_patterns = ['rm', 'del', 'format', '>', '>>', '|', '&', ';', '$(', '`']
    for pattern in dangerous_patterns:
        if pattern in command:
            return False
    
    return True

def get_system_stats():
    """Get system CPU and memory usage"""
    try:
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        return {
            'cpu_percent': cpu_percent,
            'memory_percent': memory.percent,
            'memory_used_gb': memory.used / (1024**3),
            'memory_total_gb': memory.total / (1024**3)
        }
    except:
        return {
            'cpu_percent': 0,
            'memory_percent': 0,
            'memory_used_gb': 0,
            'memory_total_gb': 0
        }

def calculate_uptime(started_at):
    """Calculate uptime in human-readable format"""
    if not started_at:
        return "Not running"
    
    delta = datetime.utcnow() - started_at
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if seconds > 0 or not parts:
        parts.append(f"{seconds}s")
    
    return " ".join(parts)

# ═══════════════════════════════════════════════════════════════════════════════
# LOG MANAGEMENT FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def save_log(app_id, level, message):
    """Save log entry to MongoDB"""
    try:
        log_entry = {
            'app_id': app_id,
            'timestamp': datetime.utcnow(),
            'level': level,
            'message': message
        }
        logs_col.insert_one(log_entry)
        
        # Also add to queue for real-time streaming
        log_queues[app_id].put(log_entry)
        
    except Exception as e:
        logger.error(f"Failed to save log for {app_id}: {e}")

def get_logs(app_id, limit=MAX_LOG_LINES, skip=0):
    """Retrieve logs for an application"""
    try:
        logs = list(logs_col.find(
            {'app_id': app_id},
            {'_id': 0}
        ).sort('timestamp', -1).skip(skip).limit(limit))
        
        # Format timestamps
        for log in logs:
            log['timestamp'] = log['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
        
        return logs[::-1]  # Reverse to get chronological order
    except Exception as e:
        logger.error(f"Failed to get logs for {app_id}: {e}")
        return []

def clear_logs(app_id):
    """Clear all logs for an application"""
    try:
        logs_col.delete_many({'app_id': app_id})
        return True
    except Exception as e:
        logger.error(f"Failed to clear logs for {app_id}: {e}")
        return False

# ═══════════════════════════════════════════════════════════════════════════════
# PROCESS MANAGEMENT FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

class LogCaptureThread(threading.Thread):
    """Thread to capture stdout/stderr from subprocess in real-time"""
    
    def __init__(self, app_id, process, stream_type):
        super().__init__(daemon=True)
        self.app_id = app_id
        self.process = process
        self.stream_type = stream_type  # 'stdout' or 'stderr'
        self.stream = process.stdout if stream_type == 'stdout' else process.stderr
        
    def run(self):
        """Capture logs line by line"""
        try:
            for line in iter(self.stream.readline, b''):
                if not line:
                    break
                
                decoded_line = line.decode('utf-8', errors='replace').strip()
                if decoded_line:
                    # Determine log level based on content
                    level = 'INFO'
                    lower_line = decoded_line.lower()
                    
                    if 'error' in lower_line or 'exception' in lower_line or 'traceback' in lower_line:
                        level = 'ERROR'
                    elif 'warning' in lower_line or 'warn' in lower_line:
                        level = 'WARNING'
                    elif 'debug' in lower_line:
                        level = 'DEBUG'
                    
                    save_log(self.app_id, level, decoded_line)
                    
        except Exception as e:
            logger.error(f"Log capture error for {self.app_id}: {e}")
        finally:
            self.stream.close()

def install_requirements(app_id, requirements):
    """Install pip requirements for an application"""
    if not requirements:
        return True, "No requirements to install"
    
    save_log(app_id, 'INFO', '📦 Installing requirements...')
    
    try:
        # Create a temporary requirements file
        req_file = f"/tmp/requirements_{app_id}.txt"
        with open(req_file, 'w') as f:
            f.write('\n'.join(requirements))
        
        # Install requirements
        process = subprocess.Popen(
            [sys.executable, '-m', 'pip', 'install', '-r', req_file, '--quiet'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        stdout, stderr = process.communicate(timeout=300)  # 5 minute timeout
        
        # Clean up
        if os.path.exists(req_file):
            os.remove(req_file)
        
        if process.returncode == 0:
            save_log(app_id, 'INFO', '✓ Requirements installed successfully')
            return True, "Requirements installed"
        else:
            error_msg = stderr or stdout or "Unknown error"
            save_log(app_id, 'ERROR', f'✗ Failed to install requirements: {error_msg}')
            return False, error_msg
            
    except subprocess.TimeoutExpired:
        save_log(app_id, 'ERROR', '✗ Requirements installation timed out')
        return False, "Installation timed out"
    except Exception as e:
        save_log(app_id, 'ERROR', f'✗ Requirements installation error: {str(e)}')
        return False, str(e)

def start_application(app_id):
    """Start an application process"""
    with app_locks[app_id]:
        try:
            # Get application from database
            app_data = applications_col.find_one({'app_id': app_id})
            if not app_data:
                return False, "Application not found"
            
            # Check if already running
            if app_id in running_apps and running_apps[app_id].poll() is None:
                return False, "Application already running"
            
            save_log(app_id, 'INFO', '🚀 Starting application...')
            
            # Install requirements first
            if app_data.get('requirements'):
                success, msg = install_requirements(app_id, app_data['requirements'])
                if not success:
                    applications_col.update_one(
                        {'app_id': app_id},
                        {'$set': {'status': 'crashed'}}
                    )
                    return False, f"Requirements installation failed: {msg}"
            
            # Create temporary script file
            script_path = f"/tmp/{app_id}.py"
            
            # Replace environment variable placeholders
            script_content = app_data['script']
            for key, value in app_data.get('env_vars', {}).items():
                script_content = script_content.replace(f"{{{{{key}}}}}", value)
            
            with open(script_path, 'w') as f:
                f.write(script_content)
            
            # Prepare environment variables
            env = os.environ.copy()
            env.update(app_data.get('env_vars', {}))
            
            # Start process
            process = subprocess.Popen(
                [sys.executable, '-u', script_path],  # -u for unbuffered output
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                bufsize=1
            )
            
            # Store process
            running_apps[app_id] = process
            
            # Start log capture threads
            stdout_thread = LogCaptureThread(app_id, process, 'stdout')
            stderr_thread = LogCaptureThread(app_id, process, 'stderr')
            
            stdout_thread.start()
            stderr_thread.start()
            
            log_threads[app_id] = (stdout_thread, stderr_thread)
            
            # Update database
            applications_col.update_one(
                {'app_id': app_id},
                {
                    '$set': {
                        'status': 'running',
                        'pid': process.pid,
                        'last_started': datetime.utcnow(),
                        'updated_at': datetime.utcnow()
                    }
                }
            )
            
            save_log(app_id, 'INFO', f'✓ Application started successfully (PID: {process.pid})')
            return True, f"Application started with PID {process.pid}"
            
        except Exception as e:
            logger.error(f"Failed to start application {app_id}: {e}")
            save_log(app_id, 'ERROR', f'✗ Failed to start: {str(e)}')
            applications_col.update_one(
                {'app_id': app_id},
                {'$set': {'status': 'crashed'}}
            )
            return False, str(e)

def stop_application(app_id, force=False):
    """Stop an application process"""
    with app_locks[app_id]:
        try:
            if app_id not in running_apps:
                return False, "Application not running"
            
            process = running_apps[app_id]
            
            save_log(app_id, 'INFO', '🛑 Stopping application...')
            
            if force:
                # Force kill
                process.kill()
                save_log(app_id, 'WARNING', 'Application force killed')
            else:
                # Graceful shutdown
                process.terminate()
                try:
                    process.wait(timeout=10)
                    save_log(app_id, 'INFO', 'Application stopped gracefully')
                except subprocess.TimeoutExpired:
                    process.kill()
                    save_log(app_id, 'WARNING', 'Application killed after timeout')
            
            # Clean up
            del running_apps[app_id]
            
            # Update database
            applications_col.update_one(
                {'app_id': app_id},
                {
                    '$set': {
                        'status': 'stopped',
                        'pid': None,
                        'updated_at': datetime.utcnow()
                    }
                }
            )
            
            # Clean up script file
            script_path = f"/tmp/{app_id}.py"
            if os.path.exists(script_path):
                os.remove(script_path)
            
            return True, "Application stopped"
            
        except Exception as e:
            logger.error(f"Failed to stop application {app_id}: {e}")
            return False, str(e)

def restart_application(app_id):
    """Restart an application"""
    save_log(app_id, 'INFO', '🔄 Restarting application...')
    
    # Stop first
    stop_application(app_id)
    time.sleep(2)
    
    # Start again
    success, msg = start_application(app_id)
    
    if success:
        # Increment restart count
        applications_col.update_one(
            {'app_id': app_id},
            {'$inc': {'restart_count': 1}}
        )
    
    return success, msg

def is_process_running(app_id):
    """Check if application process is still running"""
    if app_id not in running_apps:
        return False
    
    process = running_apps[app_id]
    return process.poll() is None

# ═══════════════════════════════════════════════════════════════════════════════
# MONITORING THREAD
# ═══════════════════════════════════════════════════════════════════════════════

def monitoring_worker():
    """Background thread to monitor application health and auto-restart crashed apps"""
    logger.info("✓ Monitoring thread started")
    
    while not shutdown_event.is_set():
        try:
            # Get all applications that should be running
            apps = list(applications_col.find({'status': 'running'}))
            
            for app_data in apps:
                app_id = app_data['app_id']
                
                # Check if process is actually running
                if not is_process_running(app_id):
                    logger.warning(f"Application {app_id} crashed, attempting restart")
                    save_log(app_id, 'ERROR', '✗ Application crashed unexpectedly')
                    
                    # Update status
                    applications_col.update_one(
                        {'app_id': app_id},
                        {
                            '$set': {
                                'status': 'crashed',
                                'pid': None
                            }
                        }
                    )
                    
                    # Auto-restart if enabled
                    if app_data.get('auto_restart', True):
                        restart_count = app_data.get('restart_count', 0)
                        
                        if restart_count < MAX_RESTART_ATTEMPTS:
                            logger.info(f"Auto-restarting {app_id} (attempt {restart_count + 1}/{MAX_RESTART_ATTEMPTS})")
                            time.sleep(5)  # Wait before restart
                            restart_application(app_id)
                        else:
                            save_log(app_id, 'ERROR', f'✗ Max restart attempts ({MAX_RESTART_ATTEMPTS}) reached')
                            logger.error(f"Max restart attempts reached for {app_id}")
            
            # Clean up old logs
            cutoff_date = datetime.utcnow() - timedelta(days=LOG_RETENTION_DAYS)
            logs_col.delete_many({'timestamp': {'$lt': cutoff_date}})
            
        except Exception as e:
            logger.error(f"Monitoring thread error: {e}")
        
        # Wait before next check
        shutdown_event.wait(HEALTH_CHECK_INTERVAL)
    
    logger.info("✓ Monitoring thread stopped")

# ═══════════════════════════════════════════════════════════════════════════════
# APPLICATION TEMPLATES
# ═══════════════════════════════════════════════════════════════════════════════

TEMPLATES = {
    'telegram_bot_ptb': {
        'name': 'Telegram Bot (python-telegram-bot)',
        'type': 'Telegram Bot',
        'requirements': ['python-telegram-bot==20.7'],
        'env_vars': {'BOT_TOKEN': 'your-bot-token-here'},
        'script': '''from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "{{BOT_TOKEN}}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Bot is running on CloudHost Pro! 🚀")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Available commands:\\n/start - Start bot\\n/help - Show help")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    
    print("✓ Bot started successfully!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
'''
    },
    'flask_api': {
        'name': 'Flask API',
        'type': 'Web API',
        'requirements': ['flask==3.0.0'],
        'env_vars': {'PORT': '5000'},
        'script': '''from flask import Flask, jsonify
import os

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        "status": "running",
        "message": "API is live on CloudHost Pro!",
        "version": "1.0.0"
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

@app.route('/api/hello/<name>')
def hello(name):
    return jsonify({"message": f"Hello, {name}!"})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"✓ Flask API started on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
'''
    },
    'discord_bot': {
        'name': 'Discord Bot',
        'type': 'Discord Bot',
        'requirements': ['discord.py==2.3.2'],
        'env_vars': {'DISCORD_TOKEN': 'your-discord-token-here'},
        'script': '''import discord
from discord.ext import commands
import os

TOKEN = os.getenv("DISCORD_TOKEN", "{{DISCORD_TOKEN}}")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'✓ Bot logged in as {bot.user}')

@bot.command()
async def hello(ctx):
    await ctx.send('Hello! Bot is running on CloudHost Pro! 🚀')

@bot.command()
async def ping(ctx):
    await ctx.send(f'Pong! Latency: {round(bot.latency * 1000)}ms')

bot.run(TOKEN)
'''
    },
    'fastapi': {
        'name': 'FastAPI',
        'type': 'Web API',
        'requirements': ['fastapi==0.104.1', 'uvicorn==0.24.0'],
        'env_vars': {'PORT': '8000'},
        'script': '''from fastapi import FastAPI
import uvicorn
import os

app = FastAPI(title="CloudHost Pro API")

@app.get("/")
def read_root():
    return {
        "status": "running",
        "message": "FastAPI is live on CloudHost Pro!",
        "docs": "/docs"
    }

@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.get("/api/hello/{name}")
def hello(name: str):
    return {"message": f"Hello, {name}!"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print(f"✓ FastAPI started on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
'''
    },
    'worker': {
        'name': 'Background Worker',
        'type': 'Worker',
        'requirements': [],
        'env_vars': {},
        'script': '''import time
from datetime import datetime

def main():
    print("✓ Background worker started")
    
    counter = 0
    while True:
        counter += 1
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] Worker running... (iteration {counter})")
        
        # Your background task logic here
        # Example: Process queue, sync data, send notifications, etc.
        
        time.sleep(60)  # Run every 60 seconds

if __name__ == "__main__":
    main()
'''
    }
}

# ═══════════════════════════════════════════════════════════════════════════════
# HTML TEMPLATES
# ═══════════════════════════════════════════════════════════════════════════════

LOGIN_PAGE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - {{ platform_name }}</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
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
            padding: 40px;
            border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            width: 100%;
            max-width: 400px;
        }
        
        .logo {
            text-align: center;
            margin-bottom: 30px;
        }
        
        .logo h1 {
            font-size: 32px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 10px;
        }
        
        .logo p {
            color: #666;
            font-size: 14px;
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        label {
            display: block;
            margin-bottom: 8px;
            color: #333;
            font-weight: 500;
        }
        
        input {
            width: 100%;
            padding: 12px;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            font-size: 14px;
            transition: all 0.3s;
        }
        
        input:focus {
            outline: none;
            border-color: #667eea;
        }
        
        button {
            width: 100%;
            padding: 12px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 16px;
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
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 20px;
            border-left: 4px solid #c33;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <div class="logo">
            <h1>☁️ {{ platform_name }}</h1>
            <p>Application Hosting Platform</p>
        </div>
        
        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}
        
        <form method="POST">
            <div class="form-group">
                <label>Username</label>
                <input type="text" name="username" required autofocus>
            </div>
            <div class="form-group">
                <label>Password</label>
                <input type="password" name="password" required>
            </div>
            <button type="submit">Login</button>
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
    <title>Dashboard - {{ platform_name }}</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: #f3f4f6;
        }
        
        .navbar {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 16px 32px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
        }
        
        .navbar-brand {
            font-size: 24px;
            font-weight: bold;
        }
        
        .navbar-menu {
            display: flex;
            gap: 20px;
            align-items: center;
        }
        
        .navbar-menu a {
            color: white;
            text-decoration: none;
            padding: 8px 16px;
            border-radius: 6px;
            transition: background 0.3s;
        }
        
        .navbar-menu a:hover {
            background: rgba(255,255,255,0.1);
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 32px;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 24px;
            margin-bottom: 32px;
        }
        
        .stat-card {
            background: white;
            padding: 24px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            transition: transform 0.2s;
        }
        
        .stat-card:hover {
            transform: translateY(-4px);
        }
        
        .stat-value {
            font-size: 36px;
            font-weight: bold;
            margin: 12px 0;
        }
        
        .stat-label {
            color: #666;
            font-size: 14px;
        }
        
        .stat-icon {
            font-size: 48px;
        }
        
        .card {
            background: white;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            padding: 24px;
            margin-bottom: 24px;
        }
        
        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        
        .card-title {
            font-size: 20px;
            font-weight: 600;
        }
        
        .btn {
            padding: 10px 20px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.3s;
            text-decoration: none;
            display: inline-block;
        }
        
        .btn-primary {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
        }
        
        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
        }
        
        .btn-success {
            background: #10b981;
            color: white;
        }
        
        .btn-danger {
            background: #ef4444;
            color: white;
        }
        
        .btn-warning {
            background: #f59e0b;
            color: white;
        }
        
        .btn-sm {
            padding: 6px 12px;
            font-size: 12px;
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
        }
        
        thead {
            background: #f9fafb;
        }
        
        th {
            text-align: left;
            padding: 12px;
            font-weight: 600;
            color: #374151;
            border-bottom: 2px solid #e5e7eb;
        }
        
        td {
            padding: 12px;
            border-bottom: 1px solid #e5e7eb;
        }
        
        .status-badge {
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
            display: inline-block;
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
        }
        
        .modal.active {
            display: flex;
        }
        
        .modal-content {
            background: white;
            border-radius: 12px;
            max-width: 800px;
            width: 90%;
            max-height: 90vh;
            overflow-y: auto;
            padding: 32px;
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        label {
            display: block;
            margin-bottom: 8px;
            font-weight: 500;
            color: #374151;
        }
        
        input, select, textarea {
            width: 100%;
            padding: 10px;
            border: 2px solid #e5e7eb;
            border-radius: 6px;
            font-size: 14px;
            font-family: inherit;
        }
        
        textarea {
            font-family: 'Courier New', monospace;
            min-height: 200px;
        }
        
        input:focus, select:focus, textarea:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .toast {
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 16px 24px;
            border-radius: 8px;
            color: white;
            font-weight: 500;
            z-index: 2000;
            animation: slideIn 0.3s ease;
        }
        
        @keyframes slideIn {
            from {
                transform: translateX(400px);
                opacity: 0;
            }
            to {
                transform: translateX(0);
                opacity: 1;
            }
        }
        
        .toast-success {
            background: #10b981;
        }
        
        .toast-error {
            background: #ef4444;
        }
        
        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: #9ca3af;
        }
        
        .empty-state-icon {
            font-size: 64px;
            margin-bottom: 16px;
        }
        
        .action-buttons {
            display: flex;
            gap: 8px;
        }
        
        .search-box {
            padding: 10px;
            border: 2px solid #e5e7eb;
            border-radius: 6px;
            width: 300px;
        }
    </style>
</head>
<body>
    <div class="navbar">
        <div class="navbar-brand">☁️ {{ platform_name }}</div>
        <div class="navbar-menu">
            <a href="/dashboard">Dashboard</a>
            <a href="/logout">Logout</a>
        </div>
    </div>
    
    <div class="container">
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-icon">📦</div>
                <div class="stat-value" id="total-apps">0</div>
                <div class="stat-label">Total Applications</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">🟢</div>
                <div class="stat-value" id="running-apps">0</div>
                <div class="stat-label">Running</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">🔴</div>
                <div class="stat-value" id="stopped-apps">0</div>
                <div class="stat-label">Stopped</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">💾</div>
                <div class="stat-value" id="total-logs">0</div>
                <div class="stat-label">Total Logs</div>
            </div>
        </div>
        
        <div class="card">
            <div class="card-header">
                <h2 class="card-title">Applications</h2>
                <div style="display: flex; gap: 12px;">
                    <input type="text" class="search-box" id="search-input" placeholder="Search applications...">
                    <button class="btn btn-primary" onclick="openCreateModal()">+ New Application</button>
                </div>
            </div>
            
            <div id="apps-table-container">
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
                    </tbody>
                </table>
                <div id="empty-state" class="empty-state" style="display: none;">
                    <div class="empty-state-icon">📭</div>
                    <h3>No applications yet</h3>
                    <p>Create your first application to get started</p>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Create Application Modal -->
    <div class="modal" id="create-modal">
        <div class="modal-content">
            <h2>Create New Application</h2>
            <form id="create-form">
                <div class="form-group">
                    <label>Application Name *</label>
                    <input type="text" name="app_name" required placeholder="my-telegram-bot">
                </div>
                
                <div class="form-group">
                    <label>Application Type *</label>
                    <select name="app_type" id="app-type-select" onchange="loadTemplate()">
                        <option value="">-- Select Type --</option>
                        <option value="telegram_bot_ptb">Telegram Bot (python-telegram-bot)</option>
                        <option value="flask_api">Flask API</option>
                        <option value="fastapi">FastAPI</option>
                        <option value="discord_bot">Discord Bot</option>
                        <option value="worker">Background Worker</option>
                        <option value="custom">Custom</option>
                    </select>
                </div>
                
                <div class="form-group">
                    <label>Python Script *</label>
                    <textarea name="script" id="script-textarea" required placeholder="# Your Python code here"></textarea>
                </div>
                
                <div class="form-group">
                    <label>Requirements (one per line)</label>
                    <textarea name="requirements" id="requirements-textarea" rows="4" placeholder="flask==3.0.0&#10;requests==2.31.0"></textarea>
                </div>
                
                <div class="form-group">
                    <label>Environment Variables (JSON format)</label>
                    <textarea name="env_vars" id="env-vars-textarea" rows="4" placeholder='{"BOT_TOKEN": "your-token-here", "API_KEY": "your-key"}'></textarea>
                </div>
                
                <div class="form-group">
                    <label>
                        <input type="checkbox" name="auto_restart" checked> Auto-restart on crash
                    </label>
                </div>
                
                <div class="form-group">
                    <label>
                        <input type="checkbox" name="auto_start" checked> Auto-start on server boot
                    </label>
                </div>
                
                <div style="display: flex; gap: 12px; margin-top: 24px;">
                    <button type="submit" class="btn btn-primary">Create Application</button>
                    <button type="button" class="btn btn-danger" onclick="closeCreateModal()">Cancel</button>
                </div>
            </form>
        </div>
    </div>
    
    <!-- Logs Modal -->
    <div class="modal" id="logs-modal">
        <div class="modal-content" style="max-width: 1200px;">
            <div style="display: flex; justify-content: space-between; margin-bottom: 20px;">
                <h2>Application Logs: <span id="logs-app-name"></span></h2>
                <div>
                    <button class="btn btn-sm btn-primary" onclick="downloadLogs()">Download</button>
                    <button class="btn btn-sm btn-warning" onclick="clearLogs()">Clear</button>
                    <button class="btn btn-sm btn-danger" onclick="closeLogsModal()">Close</button>
                </div>
            </div>
            <div id="logs-container" style="background: #1e1e1e; color: #d4d4d4; padding: 16px; border-radius: 6px; font-family: 'Courier New', monospace; font-size: 12px; height: 500px; overflow-y: auto;">
            </div>
        </div>
    </div>
    
    <script>
        let currentAppId = null;
        let logsEventSource = null;
        const templates = {{ templates | tojson }};
        
        // Load applications on page load
        document.addEventListener('DOMContentLoaded', () => {
            loadApplications();
            setInterval(loadApplications, 5000); // Refresh every 5 seconds
        });
        
        // Search functionality
        document.getElementById('search-input').addEventListener('input', (e) => {
            const searchTerm = e.target.value.toLowerCase();
            const rows = document.querySelectorAll('#apps-tbody tr');
            rows.forEach(row => {
                const text = row.textContent.toLowerCase();
                row.style.display = text.includes(searchTerm) ? '' : 'none';
            });
        });
        
        async function loadApplications() {
            try {
                const response = await fetch('/api/apps');
                const data = await response.json();
                
                if (data.success) {
                    updateStats(data.stats);
                    renderApplications(data.applications);
                }
            } catch (error) {
                console.error('Failed to load applications:', error);
            }
        }
        
        function updateStats(stats) {
            document.getElementById('total-apps').textContent = stats.total;
            document.getElementById('running-apps').textContent = stats.running;
            document.getElementById('stopped-apps').textContent = stats.stopped;
            document.getElementById('total-logs').textContent = stats.total_logs.toLocaleString();
        }
        
        function renderApplications(apps) {
            const tbody = document.getElementById('apps-tbody');
            const emptyState = document.getElementById('empty-state');
            
            if (apps.length === 0) {
                tbody.innerHTML = '';
                emptyState.style.display = 'block';
                return;
            }
            
            emptyState.style.display = 'none';
            
            tbody.innerHTML = apps.map(app => `
                <tr>
                    <td><strong>${app.app_name}</strong></td>
                    <td>${app.app_type}</td>
                    <td><span class="status-badge status-${app.status}">${app.status.toUpperCase()}</span></td>
                    <td>${app.uptime}</td>
                    <td>${app.created_at}</td>
                    <td>
                        <div class="action-buttons">
                            ${app.status === 'running' 
                                ? `<button class="btn btn-sm btn-warning" onclick="stopApp('${app.app_id}')">Stop</button>`
                                : `<button class="btn btn-sm btn-success" onclick="startApp('${app.app_id}')">Start</button>`
                            }
                            <button class="btn btn-sm btn-primary" onclick="restartApp('${app.app_id}')">Restart</button>
                            <button class="btn btn-sm btn-primary" onclick="viewLogs('${app.app_id}', '${app.app_name}')">Logs</button>
                            <button class="btn btn-sm btn-danger" onclick="deleteApp('${app.app_id}', '${app.app_name}')">Delete</button>
                        </div>
                    </td>
                </tr>
            `).join('');
        }
        
        function openCreateModal() {
            document.getElementById('create-modal').classList.add('active');
        }
        
        function closeCreateModal() {
            document.getElementById('create-modal').classList.remove('active');
            document.getElementById('create-form').reset();
        }
        
        function loadTemplate() {
            const select = document.getElementById('app-type-select');
            const templateKey = select.value;
            
            if (!templateKey || templateKey === 'custom') {
                document.getElementById('script-textarea').value = '';
                document.getElementById('requirements-textarea').value = '';
                document.getElementById('env-vars-textarea').value = '{}';
                return;
            }
            
            const template = templates[templateKey];
            if (template) {
                document.getElementById('script-textarea').value = template.script;
                document.getElementById('requirements-textarea').value = template.requirements.join('\\n');
                document.getElementById('env-vars-textarea').value = JSON.stringify(template.env_vars, null, 2);
            }
        }
        
        document.getElementById('create-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            
            const formData = new FormData(e.target);
            const data = {
                app_name: formData.get('app_name'),
                app_type: formData.get('app_type'),
                script: formData.get('script'),
                requirements: formData.get('requirements').split('\\n').filter(r => r.trim()),
                env_vars: JSON.parse(formData.get('env_vars') || '{}'),
                auto_restart: formData.get('auto_restart') === 'on',
                auto_start: formData.get('auto_start') === 'on'
            };
            
            try {
                const response = await fetch('/api/apps/create', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                });
                
                const result = await response.json();
                
                if (result.success) {
                    showToast('Application created successfully!', 'success');
                    closeCreateModal();
                    loadApplications();
                } else {
                    showToast('Error: ' + result.message, 'error');
                }
            } catch (error) {
                showToast('Failed to create application', 'error');
            }
        });
        
        async function startApp(appId) {
            try {
                const response = await fetch(`/api/apps/${appId}/start`, {method: 'POST'});
                const data = await response.json();
                showToast(data.message, data.success ? 'success' : 'error');
                loadApplications();
            } catch (error) {
                showToast('Failed to start application', 'error');
            }
        }
        
        async function stopApp(appId) {
            try {
                const response = await fetch(`/api/apps/${appId}/stop`, {method: 'POST'});
                const data = await response.json();
                showToast(data.message, data.success ? 'success' : 'error');
                loadApplications();
            } catch (error) {
                showToast('Failed to stop application', 'error');
            }
        }
        
        async function restartApp(appId) {
            try {
                const response = await fetch(`/api/apps/${appId}/restart`, {method: 'POST'});
                const data = await response.json();
                showToast(data.message, data.success ? 'success' : 'error');
                loadApplications();
            } catch (error) {
                showToast('Failed to restart application', 'error');
            }
        }
        
        async function deleteApp(appId, appName) {
            if (!confirm(`Are you sure you want to delete "${appName}"? This cannot be undone.`)) {
                return;
            }
            
            try {
                const response = await fetch(`/api/apps/${appId}`, {method: 'DELETE'});
                const data = await response.json();
                showToast(data.message, data.success ? 'success' : 'error');
                loadApplications();
            } catch (error) {
                showToast('Failed to delete application', 'error');
            }
        }
        
        function viewLogs(appId, appName) {
            currentAppId = appId;
            document.getElementById('logs-app-name').textContent = appName;
            document.getElementById('logs-modal').classList.add('active');
            
            // Close existing connection
            if (logsEventSource) {
                logsEventSource.close();
            }
            
            // Load initial logs
            loadInitialLogs(appId);
            
            // Start SSE stream
            logsEventSource = new EventSource(`/api/apps/${appId}/logs/stream`);
            
            logsEventSource.onmessage = (event) => {
                const log = JSON.parse(event.data);
                appendLog(log);
            };
            
            logsEventSource.onerror = () => {
                console.error('SSE connection error');
            };
        }
        
        async function loadInitialLogs(appId) {
            try {
                const response = await fetch(`/api/apps/${appId}/logs`);
                const data = await response.json();
                
                const container = document.getElementById('logs-container');
                container.innerHTML = '';
                
                if (data.success && data.logs.length > 0) {
                    data.logs.forEach(log => appendLog(log));
                } else {
                    container.innerHTML = '<div style="color: #888;">No logs available</div>';
                }
            } catch (error) {
                console.error('Failed to load logs:', error);
            }
        }
        
        function appendLog(log) {
            const container = document.getElementById('logs-container');
            const logLine = document.createElement('div');
            logLine.style.marginBottom = '4px';
            
            let color = '#d4d4d4';
            if (log.level === 'ERROR') color = '#f48771';
            else if (log.level === 'WARNING') color = '#dcdcaa';
            else if (log.level === 'INFO') color = '#4ec9b0';
            else if (log.level === 'DEBUG') color = '#9cdcfe';
            
            logLine.innerHTML = `<span style="color: #888;">[${log.timestamp}]</span> <span style="color: ${color};">[${log.level}]</span> ${escapeHtml(log.message)}`;
            container.appendChild(logLine);
            container.scrollTop = container.scrollHeight;
        }
        
        function closeLogsModal() {
            document.getElementById('logs-modal').classList.remove('active');
            if (logsEventSource) {
                logsEventSource.close();
                logsEventSource = null;
            }
        }
        
        async function clearLogs() {
            if (!confirm('Are you sure you want to clear all logs?')) {
                return;
            }
            
            try {
                const response = await fetch(`/api/apps/${currentAppId}/logs`, {method: 'DELETE'});
                const data = await response.json();
                showToast(data.message, data.success ? 'success' : 'error');
                loadInitialLogs(currentAppId);
            } catch (error) {
                showToast('Failed to clear logs', 'error');
            }
        }
        
        async function downloadLogs() {
            try {
                const response = await fetch(`/api/apps/${currentAppId}/logs`);
                const data = await response.json();
                
                if (data.success) {
                    const content = data.logs.map(log => 
                        `[${log.timestamp}] [${log.level}] ${log.message}`
                    ).join('\\n');
                    
                    const blob = new Blob([content], {type: 'text/plain'});
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `logs_${currentAppId}_${Date.now()}.txt`;
                    a.click();
                }
            } catch (error) {
                showToast('Failed to download logs', 'error');
            }
        }
        
        function showToast(message, type) {
            const toast = document.createElement('div');
            toast.className = `toast toast-${type}`;
            toast.textContent = message;
            document.body.appendChild(toast);
            
            setTimeout(() => {
                toast.remove();
            }, 3000);
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
    </script>
</body>
</html>
'''
