"""
═══════════════════════════════════════════════════════════════════
PROFESSIONAL APPLICATION HOSTING PLATFORM
Deploy ANY Python Application - 24/7 Operation
Similar to Koyeb/Railway/Render
═══════════════════════════════════════════════════════════════════
"""

import os
import sys
import json
import time
import signal
import subprocess
import threading
import queue
import psutil
import hashlib
import secrets
from datetime import datetime, timedelta
from functools import wraps
from typing import Dict, List, Optional
import re

from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for, Response
from flask_cors import CORS
from pymongo import MongoClient, ASCENDING, DESCENDING
from bson import ObjectId
import logging

# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION & CONSTANTS
# ═══════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))
CORS(app)

# MongoDB Configuration
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017/')
DB_NAME = 'hosting_platform'

# Admin Credentials
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')

# Platform Configuration
LOGS_RETENTION_DAYS = 7
MAX_LOGS_PER_APP = 10000
HEALTH_CHECK_INTERVAL = 30  # seconds
MAX_RESTART_ATTEMPTS = 5
COMMAND_TIMEOUT = 60  # seconds

# Safe terminal commands whitelist
SAFE_COMMANDS = ['pip', 'python', 'ls', 'pwd', 'cat', 'echo', 'env', 'which', 'whoami']

# ═══════════════════════════════════════════════════════════════════
# MONGODB SETUP
# ═══════════════════════════════════════════════════════════════════

try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = mongo_client[DB_NAME]
    
    # Collections
    applications_col = db['applications']
    logs_col = db['logs']
    storage_col = db['storage']
    
    # Create indexes
    applications_col.create_index([('app_id', ASCENDING)], unique=True)
    logs_col.create_index([('app_id', ASCENDING), ('timestamp', DESCENDING)])
    storage_col.create_index([('app_id', ASCENDING), ('key', ASCENDING)])
    
    print("✅ MongoDB connected successfully")
except Exception as e:
    print(f"❌ MongoDB connection failed: {e}")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════
# GLOBAL VARIABLES
# ═══════════════════════════════════════════════════════════════════

running_apps: Dict[str, subprocess.Popen] = {}
log_queues: Dict[str, queue.Queue] = {}
log_threads: Dict[str, threading.Thread] = {}
monitoring_active = True

# ═══════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def generate_app_id():
    """Generate unique application ID"""
    return secrets.token_hex(8)

def hash_password(password: str) -> str:
    """Hash password using SHA256"""
    return hashlib.sha256(password.encode()).hexdigest()

def login_required(f):
    """Decorator for protected routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def api_auth_required(f):
    """Decorator for API authentication"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated_function

def validate_python_code(code: str) -> tuple:
    """Validate Python syntax"""
    try:
        compile(code, '<string>', 'exec')
        return True, "Valid Python code"
    except SyntaxError as e:
        return False, f"Syntax error: {str(e)}"

def is_safe_command(command: str) -> bool:
    """Check if command is safe to execute"""
    cmd_parts = command.strip().split()
    if not cmd_parts:
        return False
    
    base_cmd = cmd_parts[0]
    
    # Check whitelist
    if base_cmd not in SAFE_COMMANDS:
        return False
    
    # Block dangerous patterns
    dangerous_patterns = ['rm', 'del', 'format', '>', '>>', '|', ';', '&&', '||', 'sudo']
    for pattern in dangerous_patterns:
        if pattern in command:
            return False
    
    return True

def get_uptime(app_id: str) -> int:
    """Calculate application uptime in seconds"""
    app = applications_col.find_one({'app_id': app_id})
    if app and app.get('last_started'):
        if app.get('status') == 'running':
            delta = datetime.utcnow() - app['last_started']
            return int(delta.total_seconds())
    return 0

def add_log(app_id: str, level: str, message: str):
    """Add log entry to database"""
    try:
        log_entry = {
            'app_id': app_id,
            'timestamp': datetime.utcnow(),
            'level': level,
            'message': message
        }
        logs_col.insert_one(log_entry)
        
        # Add to queue for live streaming
        if app_id in log_queues:
            log_queues[app_id].put(log_entry)
        
        # Cleanup old logs
        count = logs_col.count_documents({'app_id': app_id})
        if count > MAX_LOGS_PER_APP:
            oldest = logs_col.find({'app_id': app_id}).sort('timestamp', ASCENDING).limit(count - MAX_LOGS_PER_APP)
            for log in oldest:
                logs_col.delete_one({'_id': log['_id']})
    except Exception as e:
        print(f"Error adding log: {e}")

# ═══════════════════════════════════════════════════════════════════
# PROCESS MANAGEMENT FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

class LogCaptureThread(threading.Thread):
    """Thread to capture stdout/stderr from subprocess"""
    
    def __init__(self, app_id: str, process: subprocess.Popen, stream_type: str):
        super().__init__(daemon=True)
        self.app_id = app_id
        self.process = process
        self.stream_type = stream_type
        self.stream = process.stdout if stream_type == 'stdout' else process.stderr
        
    def run(self):
        """Capture and log output"""
        try:
            for line in iter(self.stream.readline, b''):
                if line:
                    message = line.decode('utf-8', errors='replace').strip()
                    level = 'ERROR' if self.stream_type == 'stderr' else 'INFO'
                    add_log(self.app_id, level, message)
        except Exception as e:
            add_log(self.app_id, 'ERROR', f"Log capture error: {str(e)}")

def install_requirements(app_id: str, requirements: List[str]) -> bool:
    """Install pip requirements for application"""
    if not requirements:
        return True
    
    add_log(app_id, 'INFO', '📦 Installing dependencies...')
    
    try:
        # Create requirements file
        req_file = f'/tmp/requirements_{app_id}.txt'
        with open(req_file, 'w') as f:
            f.write('\n'.join(requirements))
        
        # Install packages
        process = subprocess.Popen(
            [sys.executable, '-m', 'pip', 'install', '-r', req_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        stdout, stderr = process.communicate(timeout=300)
        
        if process.returncode == 0:
            add_log(app_id, 'INFO', '✅ Dependencies installed successfully')
            return True
        else:
            add_log(app_id, 'ERROR', f'❌ Dependency installation failed: {stderr}')
            return False
    except Exception as e:
        add_log(app_id, 'ERROR', f'❌ Installation error: {str(e)}')
        return False
    finally:
        if os.path.exists(req_file):
            os.remove(req_file)

def start_application(app_id: str) -> tuple:
    """Start an application"""
    try:
        # Get application from database
        app = applications_col.find_one({'app_id': app_id})
        if not app:
            return False, "Application not found"
        
        # Check if already running
        if app_id in running_apps and running_apps[app_id].poll() is None:
            return False, "Application already running"
        
        add_log(app_id, 'INFO', '🚀 Starting application...')
        
        # Install requirements
        if app.get('requirements'):
            if not install_requirements(app_id, app['requirements']):
                applications_col.update_one(
                    {'app_id': app_id},
                    {'$set': {'status': 'crashed', 'updated_at': datetime.utcnow()}}
                )
                return False, "Failed to install dependencies"
        
        # Create script file
        script_file = f'/tmp/app_{app_id}.py'
        script_content = app['script']
        
        # Replace environment variable placeholders
        env_vars = app.get('env_vars', {})
        for key, value in env_vars.items():
            script_content = script_content.replace(f'{{{{{key}}}}}', str(value))
        
        with open(script_file, 'w') as f:
            f.write(script_content)
        
        # Prepare environment
        env = os.environ.copy()
        env.update(env_vars)
        
        # Start process
        process = subprocess.Popen(
            [sys.executable, '-u', script_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=1
        )
        
        # Store process
        running_apps[app_id] = process
        
        # Create log queue
        if app_id not in log_queues:
            log_queues[app_id] = queue.Queue()
        
        # Start log capture threads
        stdout_thread = LogCaptureThread(app_id, process, 'stdout')
        stderr_thread = LogCaptureThread(app_id, process, 'stderr')
        stdout_thread.start()
        stderr_thread.start()
        
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
        
        add_log(app_id, 'INFO', f'✅ Application started (PID: {process.pid})')
        return True, "Application started successfully"
        
    except Exception as e:
        add_log(app_id, 'ERROR', f'❌ Start failed: {str(e)}')
        applications_col.update_one(
            {'app_id': app_id},
            {'$set': {'status': 'crashed', 'updated_at': datetime.utcnow()}}
        )
        return False, str(e)

def stop_application(app_id: str, force: bool = False) -> tuple:
    """Stop an application"""
    try:
        if app_id not in running_apps:
            return False, "Application not running"
        
        process = running_apps[app_id]
        
        add_log(app_id, 'INFO', '🛑 Stopping application...')
        
        if force:
            process.kill()
        else:
            # Graceful shutdown
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        
        # Remove from running apps
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
        
        add_log(app_id, 'INFO', '✅ Application stopped')
        return True, "Application stopped successfully"
        
    except Exception as e:
        add_log(app_id, 'ERROR', f'❌ Stop failed: {str(e)}')
        return False, str(e)

def restart_application(app_id: str) -> tuple:
    """Restart an application"""
    add_log(app_id, 'INFO', '🔄 Restarting application...')
    
    # Stop if running
    if app_id in running_apps:
        stop_application(app_id)
        time.sleep(2)
    
    # Start
    return start_application(app_id)

# ═══════════════════════════════════════════════════════════════════
# MONITORING THREAD
# ═══════════════════════════════════════════════════════════════════

def monitoring_thread():
    """Monitor application health and auto-restart"""
    global monitoring_active
    
    print("🔍 Monitoring thread started")
    
    while monitoring_active:
        try:
            # Check all applications
            apps = applications_col.find({'status': 'running'})
            
            for app in apps:
                app_id = app['app_id']
                
                # Check if process is alive
                if app_id in running_apps:
                    process = running_apps[app_id]
                    
                    if process.poll() is not None:
                        # Process died
                        add_log(app_id, 'ERROR', '💀 Application crashed')
                        
                        # Update status
                        restart_count = app.get('restart_count', 0)
                        applications_col.update_one(
                            {'app_id': app_id},
                            {
                                '$set': {
                                    'status': 'crashed',
                                    'pid': None,
                                    'restart_count': restart_count + 1,
                                    'last_crash': datetime.utcnow(),
                                    'updated_at': datetime.utcnow()
                                }
                            }
                        )
                        
                        # Remove from running apps
                        del running_apps[app_id]
                        
                        # Auto-restart if enabled
                        if app.get('auto_restart', True) and restart_count < MAX_RESTART_ATTEMPTS:
                            add_log(app_id, 'INFO', f'🔄 Auto-restarting (attempt {restart_count + 1}/{MAX_RESTART_ATTEMPTS})...')
                            time.sleep(5)
                            start_application(app_id)
                        else:
                            add_log(app_id, 'ERROR', '❌ Max restart attempts reached. Manual intervention required.')
                else:
                    # Process not in running_apps but marked as running
                    applications_col.update_one(
                        {'app_id': app_id},
                        {'$set': {'status': 'crashed', 'pid': None, 'updated_at': datetime.utcnow()}}
                    )
            
            time.sleep(HEALTH_CHECK_INTERVAL)
            
        except Exception as e:
            print(f"Monitoring error: {e}")
            time.sleep(HEALTH_CHECK_INTERVAL)
    
    print("🔍 Monitoring thread stopped")

# ═══════════════════════════════════════════════════════════════════
# APPLICATION TEMPLATES
# ═══════════════════════════════════════════════════════════════════

TEMPLATES = {
    'telegram_bot_ptb': {
        'name': 'Telegram Bot (python-telegram-bot)',
        'requirements': ['python-telegram-bot'],
        'env_vars': {'BOT_TOKEN': 'your_bot_token_here'},
        'script': '''from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import os

BOT_TOKEN = os.getenv('BOT_TOKEN', '{{BOT_TOKEN}}')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Bot is running on hosting platform!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Available commands:\\n/start - Start bot\\n/help - Show this message")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    print("✅ Bot started successfully!")
    app.run_polling()

if __name__ == "__main__":
    main()
'''
    },
    'flask_api': {
        'name': 'Flask API',
        'requirements': ['flask'],
        'env_vars': {'PORT': '5000'},
        'script': '''from flask import Flask, jsonify
import os

app = Flask(__name__)
PORT = int(os.getenv('PORT', 5000))

@app.route('/')
def home():
    return jsonify({
        "status": "running",
        "message": "Flask API is live!",
        "version": "1.0.0"
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

@app.route('/api/data')
def get_data():
    return jsonify({
        "data": ["item1", "item2", "item3"],
        "count": 3
    })

if __name__ == "__main__":
    print(f"✅ Flask API starting on port {PORT}...")
    app.run(host='0.0.0.0', port=PORT, debug=False)
'''
    },
    'fastapi': {
        'name': 'FastAPI Application',
        'requirements': ['fastapi', 'uvicorn'],
        'env_vars': {'PORT': '8000'},
        'script': '''from fastapi import FastAPI
import uvicorn
import os

app = FastAPI(title="Hosted API", version="1.0.0")
PORT = int(os.getenv('PORT', 8000))

@app.get("/")
async def root():
    return {"status": "running", "message": "FastAPI is live!"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/api/users")
async def get_users():
    return {"users": ["Alice", "Bob", "Charlie"]}

if __name__ == "__main__":
    print(f"✅ FastAPI starting on port {PORT}...")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
'''
    },
    'discord_bot': {
        'name': 'Discord Bot',
        'requirements': ['discord.py'],
        'env_vars': {'DISCORD_TOKEN': 'your_discord_token_here'},
        'script': '''import discord
from discord.ext import commands
import os

TOKEN = os.getenv('DISCORD_TOKEN', '{{DISCORD_TOKEN}}')
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'✅ {bot.user} is now running!')

@bot.command()
async def ping(ctx):
    await ctx.send('Pong! 🏓')

@bot.command()
async def hello(ctx):
    await ctx.send(f'Hello {ctx.author.mention}!')

if __name__ == "__main__":
    print("Starting Discord bot...")
    bot.run(TOKEN)
'''
    },
    'worker': {
        'name': 'Background Worker',
        'requirements': [],
        'env_vars': {},
        'script': '''import time
from datetime import datetime

def main():
    print("✅ Worker started successfully!")
    counter = 0
    
    while True:
        counter += 1
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{current_time}] Worker tick #{counter}")
        
        # Your background task logic here
        # Example: process queue, check database, send notifications, etc.
        
        time.sleep(10)  # Wait 10 seconds between tasks

if __name__ == "__main__":
    main()
'''
    }
}

# ═══════════════════════════════════════════════════════════════════
# HTML TEMPLATES
# ═══════════════════════════════════════════════════════════════════

LOGIN_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - Hosting Platform</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
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
        h1 {
            color: #667eea;
            margin-bottom: 10px;
            font-size: 28px;
        }
        .subtitle {
            color: #666;
            margin-bottom: 30px;
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
            transition: border-color 0.3s;
        }
        input:focus {
            outline: none;
            border-color: #667eea;
        }
        button {
            width: 100%;
            padding: 14px;
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
            color: #c00;
            padding: 10px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-size: 14px;
        }
        .logo {
            font-size: 48px;
            text-align: center;
            margin-bottom: 20px;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <div class="logo">🚀</div>
        <h1>Hosting Platform</h1>
        <p class="subtitle">Deploy and manage your applications</p>
        
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

DASHBOARD_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard - Hosting Platform</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: #f3f4f6;
            color: #333;
        }
        .navbar {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 16px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        .navbar h1 {
            font-size: 24px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .navbar button {
            background: rgba(255,255,255,0.2);
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 24px;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: white;
            padding: 24px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        .stat-card h3 {
            font-size: 14px;
            color: #666;
            margin-bottom: 8px;
        }
        .stat-card .value {
            font-size: 32px;
            font-weight: bold;
            color: #667eea;
        }
        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .section-header h2 {
            font-size: 24px;
        }
        .btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 12px 24px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        .btn:hover {
            opacity: 0.9;
        }
        .apps-table {
            background: white;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            overflow: hidden;
        }
        table {
            width: 100%;
            border-collapse: collapse;
        }
        th {
            background: #f9fafb;
            padding: 16px;
            text-align: left;
            font-weight: 600;
            color: #666;
            font-size: 13px;
            text-transform: uppercase;
        }
        td {
            padding: 16px;
            border-top: 1px solid #e5e7eb;
        }
        .status {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
        }
        .status.running { background: #d1fae5; color: #065f46; }
        .status.stopped { background: #fee; color: #991b1b; }
        .status.crashed { background: #fef3c7; color: #92400e; }
        .action-btn {
            background: none;
            border: 1px solid #e5e7eb;
            padding: 6px 12px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 12px;
            margin-right: 4px;
        }
        .action-btn:hover {
            background: #f9fafb;
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
            padding: 30px;
            border-radius: 16px;
            width: 90%;
            max-width: 800px;
            max-height: 90vh;
            overflow-y: auto;
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .modal-header h2 {
            font-size: 24px;
        }
        .close-btn {
            background: none;
            border: none;
            font-size: 28px;
            cursor: pointer;
            color: #666;
        }
        .form-group {
            margin-bottom: 20px;
        }
        .form-group label {
            display: block;
            margin-bottom: 8px;
            font-weight: 600;
            color: #333;
        }
        .form-group input,
        .form-group select,
        .form-group textarea {
            width: 100%;
            padding: 12px;
            border: 2px solid #e5e7eb;
            border-radius: 8px;
            font-size: 14px;
        }
        .form-group textarea {
            font-family: 'Courier New', monospace;
            min-height: 200px;
        }
        .toast {
            position: fixed;
            top: 20px;
            right: 20px;
            background: white;
            padding: 16px 24px;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            z-index: 2000;
            display: none;
        }
        .toast.success { border-left: 4px solid #10b981; }
        .toast.error { border-left: 4px solid #ef4444; }
        .logs-viewer {
            background: #1e1e1e;
            color: #d4d4d4;
            padding: 16px;
            border-radius: 8px;
            font-family: 'Courier New', monospace;
            font-size: 13px;
            max-height: 500px;
            overflow-y: auto;
        }
        .log-line {
            margin-bottom: 4px;
            line-height: 1.5;
        }
        .log-line.info { color: #4ade80; }
        .log-line.error { color: #f87171; }
        .log-line.warning { color: #fb923c; }
        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: #666;
        }
        .empty-state-icon {
            font-size: 64px;
            margin-bottom: 16px;
        }
    </style>
</head>
<body>
    <div class="navbar">
        <h1><span>🚀</span> Hosting Platform</h1>
        <button onclick="logout()">Logout</button>
    </div>
    
    <div class="container">
        <div class="stats-grid" id="stats">
            <div class="stat-card">
                <h3>Total Applications</h3>
                <div class="value" id="total-apps">0</div>
            </div>
            <div class="stat-card">
                <h3>Running</h3>
                <div class="value" style="color: #10b981;" id="running-apps">0</div>
            </div>
            <div class="stat-card">
                <h3>Stopped</h3>
                <div class="value" style="color: #ef4444;" id="stopped-apps">0</div>
            </div>
            <div class="stat-card">
                <h3>Total Logs</h3>
                <div class="value" style="color: #f59e0b;" id="total-logs">0</div>
            </div>
        </div>
        
        <div class="section-header">
            <h2>Applications</h2>
            <button class="btn" onclick="showCreateModal()">
                <span>+</span> New Application
            </button>
        </div>
        
        <div class="apps-table">
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
                        <td colspan="6" class="empty-state">
                            <div class="empty-state-icon">📦</div>
                            <div>No applications yet. Create your first one!</div>
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>
    </div>
    
    <!-- Create App Modal -->
    <div id="createModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>Create New Application</h2>
                <button class="close-btn" onclick="closeModal('createModal')">&times;</button>
            </div>
            <form id="createForm" onsubmit="createApp(event)">
                <div class="form-group">
                    <label>Application Name</label>
                    <input type="text" name="app_name" required placeholder="my-awesome-app">
                </div>
                <div class="form-group">
                    <label>Application Type</label>
                    <select name="app_type" onchange="loadTemplate(this.value)">
                        <option value="">Select a template...</option>
                        <option value="telegram_bot_ptb">Telegram Bot (python-telegram-bot)</option>
                        <option value="flask_api">Flask API</option>
                        <option value="fastapi">FastAPI Application</option>
                        <option value="discord_bot">Discord Bot</option>
                        <option value="worker">Background Worker</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Python Code</label>
                    <textarea name="script" required placeholder="# Your Python code here"></textarea>
                </div>
                <div class="form-group">
                    <label>Requirements (one per line)</label>
                    <textarea name="requirements" placeholder="flask==3.0.0&#10;requests==2.31.0" rows="4"></textarea>
                </div>
                <div class="form-group">
                    <label>Environment Variables (JSON)</label>
                    <textarea name="env_vars" placeholder='{"BOT_TOKEN": "your_token", "API_KEY": "your_key"}' rows="4"></textarea>
                </div>
                <div class="form-group">
                    <label>
                        <input type="checkbox" name="auto_restart" checked> Auto-restart on crash
                    </label>
                    <label>
                        <input type="checkbox" name="auto_start" checked> Auto-start on server boot
                    </label>
                </div>
                <button type="submit" class="btn">Create Application</button>
            </form>
        </div>
    </div>
    
    <!-- Logs Modal -->
    <div id="logsModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>Application Logs - <span id="logs-app-name"></span></h2>
                <button class="close-btn" onclick="closeModal('logsModal')">&times;</button>
            </div>
            <div class="logs-viewer" id="logs-viewer"></div>
        </div>
    </div>
    
    <!-- Toast Notification -->
    <div id="toast" class="toast"></div>
    
    <script>
        let currentLogsAppId = null;
        let logsEventSource = null;
        
        const templates = {{ templates | tojson }};
        
        function showToast(message, type = 'success') {
            const toast = document.getElementById('toast');
            toast.textContent = message;
            toast.className = `toast ${type}`;
            toast.style.display = 'block';
            setTimeout(() => toast.style.display = 'none', 3000);
        }
        
        function showCreateModal() {
            document.getElementById('createModal').classList.add('active');
        }
        
        function closeModal(modalId) {
            document.getElementById(modalId).classList.remove('active');
            if (modalId === 'logsModal' && logsEventSource) {
                logsEventSource.close();
                logsEventSource = null;
            }
        }
        
        function loadTemplate(templateId) {
            if (!templateId || !templates[templateId]) return;
            
            const template = templates[templateId];
            const form = document.getElementById('createForm');
            
            form.script.value = template.script;
            form.requirements.value = template.requirements.join('\\n');
            form.env_vars.value = JSON.stringify(template.env_vars, null, 2);
        }
        
        async function createApp(event) {
            event.preventDefault();
            
            const form = event.target;
            const formData = new FormData(form);
            
            const requirements = formData.get('requirements')
                .split('\\n')
                .map(r => r.trim())
                .filter(r => r);
            
            let env_vars = {};
            try {
                const envText = formData.get('env_vars').trim();
                if (envText) {
                    env_vars = JSON.parse(envText);
                }
            } catch (e) {
                showToast('Invalid JSON in environment variables', 'error');
                return;
            }
            
            const data = {
                app_name: formData.get('app_name'),
                app_type: formData.get('app_type') || 'custom',
                script: formData.get('script'),
                requirements: requirements,
                env_vars: env_vars,
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
                
                if (response.ok) {
                    showToast('Application created successfully!', 'success');
                    closeModal('createModal');
                    form.reset();
                    loadApps();
                } else {
                    showToast(result.error || 'Failed to create application', 'error');
                }
            } catch (error) {
                showToast('Network error: ' + error.message, 'error');
            }
        }
        
        async function loadApps() {
            try {
                const response = await fetch('/api/apps');
                const apps = await response.json();
                
                const tbody = document.getElementById('apps-tbody');
                
                if (apps.length === 0) {
                    tbody.innerHTML = `
                        <tr>
                            <td colspan="6" class="empty-state">
                                <div class="empty-state-icon">📦</div>
                                <div>No applications yet. Create your first one!</div>
                            </td>
                        </tr>
                    `;
                } else {
                    tbody.innerHTML = apps.map(app => `
                        <tr>
                            <td><strong>${app.app_name}</strong></td>
                            <td>${app.app_type}</td>
                            <td><span class="status ${app.status}">${app.status.toUpperCase()}</span></td>
                            <td>${formatUptime(app.uptime_seconds || 0)}</td>
                            <td>${new Date(app.created_at).toLocaleDateString()}</td>
                            <td>
                                ${app.status !== 'running' ? 
                                    `<button class="action-btn" onclick="startApp('${app.app_id}')">▶ Start</button>` :
                                    `<button class="action-btn" onclick="stopApp('${app.app_id}')">⏸ Stop</button>`
                                }
                                <button class="action-btn" onclick="restartApp('${app.app_id}')">🔄 Restart</button>
                                <button class="action-btn" onclick="showLogs('${app.app_id}', '${app.app_name}')">📄 Logs</button>
                                <button class="action-btn" onclick="deleteApp('${app.app_id}')">🗑 Delete</button>
                            </td>
                        </tr>
                    `).join('');
                }
                
                updateStats(apps);
            } catch (error) {
                console.error('Failed to load apps:', error);
            }
        }
        
        function updateStats(apps) {
            document.getElementById('total-apps').textContent = apps.length;
            document.getElementById('running-apps').textContent = apps.filter(a => a.status === 'running').length;
            document.getElementById('stopped-apps').textContent = apps.filter(a => a.status === 'stopped').length;
            
            fetch('/api/stats/logs').then(r => r.json()).then(data => {
                document.getElementById('total-logs').textContent = data.total_logs || 0;
            });
        }
        
        function formatUptime(seconds) {
            if (seconds === 0) return '-';
            const hours = Math.floor(seconds / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);
            return `${hours}h ${minutes}m`;
        }
        
        async function startApp(appId) {
            try {
                const response = await fetch(`/api/apps/${appId}/start`, {method: 'POST'});
                const result = await response.json();
                showToast(result.message, response.ok ? 'success' : 'error');
                loadApps();
            } catch (error) {
                showToast('Failed to start application', 'error');
            }
        }
        
        async function stopApp(appId) {
            try {
                const response = await fetch(`/api/apps/${appId}/stop`, {method: 'POST'});
                const result = await response.json();
                showToast(result.message, response.ok ? 'success' : 'error');
                loadApps();
            } catch (error) {
                showToast('Failed to stop application', 'error');
            }
        }
        
        async function restartApp(appId) {
            try {
                const response = await fetch(`/api/apps/${appId}/restart`, {method: 'POST'});
                const result = await response.json();
                showToast(result.message, response.ok ? 'success' : 'error');
                loadApps();
            } catch (error) {
                showToast('Failed to restart application', 'error');
            }
        }
        
        async function deleteApp(appId) {
            if (!confirm('Are you sure you want to delete this application?')) return;
            
            try {
                const response = await fetch(`/api/apps/${appId}`, {method: 'DELETE'});
                const result = await response.json();
                showToast(result.message, response.ok ? 'success' : 'error');
                loadApps();
            } catch (error) {
                showToast('Failed to delete application', 'error');
            }
        }
        
        function showLogs(appId, appName) {
            currentLogsAppId = appId;
            document.getElementById('logs-app-name').textContent = appName;
            document.getElementById('logsModal').classList.add('active');
            
            // Close existing connection
            if (logsEventSource) {
                logsEventSource.close();
            }
            
            // Start SSE connection
            logsEventSource = new EventSource(`/api/apps/${appId}/logs/stream`);
            const logsViewer = document.getElementById('logs-viewer');
            logsViewer.innerHTML = '';
            
            logsEventSource.onmessage = (event) => {
                const log = JSON.parse(event.data);
                const logLine = document.createElement('div');
                logLine.className = `log-line ${log.level.toLowerCase()}`;
                const timestamp = new Date(log.timestamp).toLocaleTimeString();
                logLine.textContent = `[${timestamp}] [${log.level}] ${log.message}`;
                logsViewer.appendChild(logLine);
                logsViewer.scrollTop = logsViewer.scrollHeight;
            };
            
            logsEventSource.onerror = () => {
                console.error('SSE connection error');
            };
        }
        
        function logout() {
            window.location.href = '/logout';
        }
        
        // Load apps on page load
        loadApps();
        
        // Refresh apps every 5 seconds
        setInterval(loadApps, 5000);
    </script>
</body>
</html>
'''

# ═══════════════════════════════════════════════════════════════════
# FLASK ROUTES - ADMIN PANEL
# ═══════════════════════════════════════════════════════════════════

@app.route('/')
@login_required
def dashboard():
    """Dashboard page"""
    return render_template_string(DASHBOARD_HTML, templates=TEMPLATES)

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            session['username'] = username
            return redirect(url_for('dashboard'))
        else:
            return render_template_string(LOGIN_HTML, error='Invalid credentials')
    
    return render_template_string(LOGIN_HTML)

@app.route('/logout')
def logout():
    """Logout"""
    session.clear()
    return redirect(url_for('login'))

# ═══════════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/apps/create', methods=['POST'])
@api_auth_required
def api_create_app():
    """Create new application"""
    try:
        data = request.json
        
        # Validate required fields
        required = ['app_name', 'script']
        for field in required:
            if field not in data:
                return jsonify({'error': f'Missing field: {field}'}), 400
        
        # Validate Python code
        valid, message = validate_python_code(data['script'])
        if not valid:
            return jsonify({'error': message}), 400
        
        # Generate app ID
        app_id = generate_app_id()
        
        # Create application document
        app_doc = {
            'app_id': app_id,
            'app_name': data['app_name'],
            'app_type': data.get('app_type', 'custom'),
            'script': data['script'],
            'requirements': data.get('requirements', []),
            'env_vars': data.get('env_vars', {}),
            'auto_restart': data.get('auto_restart', True),
            'auto_start': data.get('auto_start', False),
            'status': 'stopped',
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow(),
            'pid': None,
            'restart_count': 0,
            'uptime_seconds': 0
        }
        
        # Insert into database
        applications_col.insert_one(app_doc)
        
        # Create log queue
        log_queues[app_id] = queue.Queue()
        
        add_log(app_id, 'INFO', f'Application "{data["app_name"]}" created')
        
        return jsonify({
            'success': True,
            'app_id': app_id,
            'message': 'Application created successfully'
        }), 201
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps', methods=['GET'])
@api_auth_required
def api_list_apps():
    """List all applications"""
    try:
        apps = list(applications_col.find({}, {'_id': 0}))
        
        # Calculate uptime for running apps
        for app in apps:
            if app['status'] == 'running':
                app['uptime_seconds'] = get_uptime(app['app_id'])
            else:
                app['uptime_seconds'] = 0
            
            # Convert datetime to string
            if 'created_at' in app:
                app['created_at'] = app['created_at'].isoformat()
            if 'updated_at' in app:
                app['updated_at'] = app['updated_at'].isoformat()
            if 'last_started' in app and app['last_started']:
                app['last_started'] = app['last_started'].isoformat()
        
        return jsonify(apps), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>', methods=['GET'])
@api_auth_required
def api_get_app(app_id):
    """Get application details"""
    try:
        app = applications_col.find_one({'app_id': app_id}, {'_id': 0})
        
        if not app:
            return jsonify({'error': 'Application not found'}), 404
        
        # Convert datetime to string
        if 'created_at' in app:
            app['created_at'] = app['created_at'].isoformat()
        if 'updated_at' in app:
            app['updated_at'] = app['updated_at'].isoformat()
        
        app['uptime_seconds'] = get_uptime(app_id)
        
        return jsonify(app), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>', methods=['DELETE'])
@api_auth_required
def api_delete_app(app_id):
    """Delete application"""
    try:
        app = applications_col.find_one({'app_id': app_id})
        
        if not app:
            return jsonify({'error': 'Application not found'}), 404
        
        # Stop if running
        if app_id in running_apps:
            stop_application(app_id, force=True)
        
        # Delete from database
        applications_col.delete_one({'app_id': app_id})
        logs_col.delete_many({'app_id': app_id})
        storage_col.delete_many({'app_id': app_id})
        
        # Remove log queue
        if app_id in log_queues:
            del log_queues[app_id]
        
        # Remove script file
        script_file = f'/tmp/app_{app_id}.py'
        if os.path.exists(script_file):
            os.remove(script_file)
        
        return jsonify({'success': True, 'message': 'Application deleted successfully'}), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/start', methods=['POST'])
@api_auth_required
def api_start_app(app_id):
    """Start application"""
    success, message = start_application(app_id)
    
    if success:
        return jsonify({'success': True, 'message': message}), 200
    else:
        return jsonify({'error': message}), 400

@app.route('/api/apps/<app_id>/stop', methods=['POST'])
@api_auth_required
def api_stop_app(app_id):
    """Stop application"""
    success, message = stop_application(app_id)
    
    if success:
        return jsonify({'success': True, 'message': message}), 200
    else:
        return jsonify({'error': message}), 400

@app.route('/api/apps/<app_id>/restart', methods=['POST'])
@api_auth_required
def api_restart_app(app_id):
    """Restart application"""
    success, message = restart_application(app_id)
    
    if success:
        return jsonify({'success': True, 'message': message}), 200
    else:
        return jsonify({'error': message}), 400

@app.route('/api/apps/<app_id>/logs', methods=['GET'])
@api_auth_required
def api_get_logs(app_id):
    """Get application logs"""
    try:
        limit = int(request.args.get('limit', 100))
        logs = list(logs_col.find(
            {'app_id': app_id},
            {'_id': 0}
        ).sort('timestamp', DESCENDING).limit(limit))
        
        # Convert datetime to string
        for log in logs:
            log['timestamp'] = log['timestamp'].isoformat()
        
        return jsonify(logs), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/logs/stream')
@api_auth_required
def api_stream_logs(app_id):
    """Stream logs using Server-Sent Events"""
    
    def generate():
        # Send existing logs first
        logs = list(logs_col.find(
            {'app_id': app_id},
            {'_id': 0}
        ).sort('timestamp', ASCENDING).limit(100))
        
        for log in logs:
            log['timestamp'] = log['timestamp'].isoformat()
            yield f"data: {json.dumps(log)}\n\n"
        
        # Stream new logs
        if app_id not in log_queues:
            log_queues[app_id] = queue.Queue()
        
        log_queue = log_queues[app_id]
        
        while True:
            try:
                log = log_queue.get(timeout=30)
                log['timestamp'] = log['timestamp'].isoformat()
                yield f"data: {json.dumps(log)}\n\n"
            except queue.Empty:
                # Send heartbeat
                yield ": heartbeat\n\n"
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/apps/<app_id>/status', methods=['GET'])
@api_auth_required
def api_get_status(app_id):
    """Get application status"""
    try:
        app = applications_col.find_one({'app_id': app_id}, {'_id': 0})
        
        if not app:
            return jsonify({'error': 'Application not found'}), 404
        
        status = {
            'app_id': app_id,
            'status': app['status'],
            'pid': app.get('pid'),
            'uptime_seconds': get_uptime(app_id),
            'restart_count': app.get('restart_count', 0)
        }
        
        return jsonify(status), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/stats/logs', methods=['GET'])
@api_auth_required
def api_stats_logs():
    """Get log statistics"""
    try:
        total_logs = logs_col.count_documents({})
        return jsonify({'total_logs': total_logs}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════════
# STARTUP & SHUTDOWN HANDLERS
# ═══════════════════════════════════════════════════════════════════

def startup():
    """Initialize platform on startup"""
    print("═══════════════════════════════════════════════════════════════════")
    print("🚀 HOSTING PLATFORM STARTING...")
    print("═══════════════════════════════════════════════════════════════════")
    
    # Start monitoring thread
    monitor_thread = threading.Thread(target=monitoring_thread, daemon=True)
    monitor_thread.start()
    
    # Auto-start applications
    apps = applications_col.find({'auto_start': True})
    auto_start_count = 0
    
    for app in apps:
        app_id = app['app_id']
        print(f"📦 Auto-starting: {app['app_name']}")
        success, message = start_application(app_id)
        if success:
            auto_start_count += 1
        else:
            print(f"   ❌ Failed: {message}")
    
    print(f"✅ Auto-started {auto_start_count} applications")
    print("═══════════════════════════════════════════════════════════════════")
    print("🎉 Platform started successfully!")
    print("📊 Admin panel: http://localhost:5000")
    print(f"🔐 Login: {ADMIN_USERNAME} / {ADMIN_PASSWORD}")
    print("═══════════════════════════════════════════════════════════════════")

def shutdown():
    """Graceful shutdown"""
    global monitoring_active
    
    print("\n═══════════════════════════════════════════════════════════════════")
    print("🛑 SHUTTING DOWN PLATFORM...")
    print("═══════════════════════════════════════════════════════════════════")
    
    # Stop monitoring
    monitoring_active = False
    
    # Stop all applications
    for app_id in list(running_apps.keys()):
        app = applications_col.find_one({'app_id': app_id})
        if app:
            print(f"🛑 Stopping: {app['app_name']}")
            stop_application(app_id)
    
    # Close MongoDB connection
    mongo_client.close()
    
    print("✅ Platform shutdown complete")
    print("═══════════════════════════════════════════════════════════════════")

def signal_handler(sig, frame):
    """Handle shutdown signals"""
    shutdown()
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ═══════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    startup()
    
    # Run Flask app
    port = int(os.getenv('PORT', 5000))
    app.run(
        host='0.0.0.0',
        port=port,
        debug=False,
        threaded=True
    )
