#!/usr/bin/env python3
"""
KOYEB-STYLE APPLICATION HOSTING PLATFORM
=========================================
Complete hosting platform for Python applications in a single file.
Deploy bots, APIs, web apps, workers - 24/7 operation.

Features:
✅ Deploy ANY Python application
✅ Live logs with real-time streaming
✅ Terminal access for pip install and commands
✅ Start, Stop, Restart, Redeploy applications
✅ Edit application code and environment variables
✅ Auto-restart on crash
✅ Professional Koyeb-style UI
✅ MongoDB for persistent storage
✅ Process isolation and monitoring

Deployment:
1. For Render: python app.py
2. For Railway: python app.py  
3. For VPS: python app.py

Environment Variables:
- MONGO_URI: MongoDB connection string
- SECRET_KEY: Flask secret key
- ADMIN_USERNAME: Admin username (default: admin)
- ADMIN_PASSWORD: Admin password (default: admin123)
- PORT: Server port (default: 5000)

Author: Application Hosting Platform
Version: 1.0.0
License: MIT
"""

import os
import sys
import json
import time
import uuid
import atexit
import signal
import hashlib
import threading
import subprocess
import tempfile
import shutil
import queue
import select
import shlex
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict, deque
from functools import wraps
from typing import Dict, List, Optional, Any, Union

# Third-party imports
try:
    from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for, Response, stream_with_context, send_file, make_response
    from flask_cors import CORS
    from pymongo import MongoClient, ASCENDING, DESCENDING
    from pymongo.errors import ConnectionFailure, PyMongoError
    from bson import ObjectId, json_util
    import psutil
    import eventlet
    eventlet.monkey_patch()
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Please install requirements: pip install Flask pymongo psutil eventlet")
    sys.exit(1)

# ============================================================================
# CONFIGURATION & CONSTANTS
# ============================================================================

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Configuration
CONFIG = {
    'SECRET_KEY': os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production'),
    'ADMIN_USERNAME': os.getenv('ADMIN_USERNAME', 'admin'),
    'ADMIN_PASSWORD': os.getenv('ADMIN_PASSWORD', 'admin123'),
    'MONGO_URI': os.getenv('MONGO_URI', 'mongodb://localhost:27017/'),
    'MONGO_DB_NAME': os.getenv('MONGO_DB_NAME', 'app_hosting'),
    'PORT': int(os.getenv('PORT', 5000)),
    'DEBUG': os.getenv('FLASK_DEBUG', 'false').lower() == 'true',
    'WORKSPACE_BASE': Path(tempfile.gettempdir()) / "app_hosting_platform",
    'MAX_LOG_LINES': 1000,
    'MONITOR_INTERVAL': 5,  # seconds
    'COMMAND_TIMEOUT': 60,  # seconds
    'GRACEFUL_SHUTDOWN_TIMEOUT': 10,  # seconds
}

# Application templates
TEMPLATES = {
    'telegram-bot': {
        'name': 'Telegram Bot',
        'script': '''from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Bot is running on hosting platform!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Available commands: /start, /help")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    print("Bot started successfully!")
    app.run_polling()

if __name__ == "__main__":
    main()''',
        'requirements': ['python-telegram-bot==20.3'],
        'env_vars': {'BOT_TOKEN': ''}
    },
    'flask-api': {
        'name': 'Flask API',
        'script': '''from flask import Flask, jsonify
import os

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"status": "running", "message": "API is live!"})

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port)''',
        'requirements': ['Flask==3.0.0'],
        'env_vars': {'PORT': '5000'}
    },
    'fastapi': {
        'name': 'FastAPI',
        'script': '''from fastapi import FastAPI
import os

app = FastAPI()

@app.get("/")
async def root():
    return {"status": "running", "message": "FastAPI is live!"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)''',
        'requirements': ['fastapi==0.104.1', 'uvicorn==0.24.0'],
        'env_vars': {'PORT': '8000'}
    },
    'discord-bot': {
        'name': 'Discord Bot',
        'script': '''import discord
import os
from discord.ext import commands

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')

@bot.command(name='hello')
async def hello(ctx):
    await ctx.send(f'Hello {ctx.author.name}!')

def main():
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()''',
        'requirements': ['discord.py==2.3.2'],
        'env_vars': {'DISCORD_TOKEN': ''}
    },
    'background-worker': {
        'name': 'Background Worker',
        'script': '''import time
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    logger.info("Background worker started")
    
    counter = 0
    while True:
        counter += 1
        logger.info(f"Worker iteration {counter}")
        time.sleep(60)  # Run every minute

if __name__ == "__main__":
    main()''',
        'requirements': [],
        'env_vars': {}
    }
}

# ============================================================================
# DATABASE CLASS
# ============================================================================

class Database:
    """MongoDB database operations"""
    
    def __init__(self):
        self.client = None
        self.db = None
        self.connected = False
        
    def connect(self):
        """Connect to MongoDB"""
        try:
            self.client = MongoClient(
                CONFIG['MONGO_URI'],
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=10000,
                socketTimeoutMS=10000
            )
            
            # Test connection
            self.client.admin.command('ping')
            
            self.db = self.client[CONFIG['MONGO_DB_NAME']]
            
            # Create indexes
            self._create_indexes()
            
            self.connected = True
            print(f"✅ Connected to MongoDB: {CONFIG['MONGO_DB_NAME']}")
            return True
            
        except Exception as e:
            print(f"❌ Failed to connect to MongoDB: {e}")
            self.connected = False
            return False
    
    def _create_indexes(self):
        """Create database indexes"""
        try:
            # Applications collection
            self.db.applications.create_index([('app_id', ASCENDING)], unique=True)
            self.db.applications.create_index([('status', ASCENDING)])
            self.db.applications.create_index([('created_at', DESCENDING)])
            self.db.applications.create_index([('auto_start', ASCENDING)])
            
            # Logs collection
            self.db.logs.create_index([('app_id', ASCENDING), ('timestamp', DESCENDING)])
            self.db.logs.create_index([('level', ASCENDING)])
            self.db.logs.create_index([('timestamp', DESCENDING)])
            
            print("✅ Database indexes created")
        except Exception as e:
            print(f"⚠️ Error creating indexes: {e}")
    
    def check_connection(self):
        """Check database connection"""
        try:
            if self.client:
                self.client.admin.command('ping')
                return True
            return False
        except:
            return False
    
    def close(self):
        """Close database connection"""
        try:
            if self.client:
                self.client.close()
                self.connected = False
                print("✅ Database connection closed")
        except Exception as e:
            print(f"⚠️ Error closing database: {e}")
    
    # Application operations
    def create_application(self, **kwargs):
        """Create new application"""
        app_id = str(ObjectId())
        
        application = {
            '_id': ObjectId(),
            'app_id': app_id,
            'name': kwargs.get('name', 'Unnamed App'),
            'type': kwargs.get('type', 'custom'),
            'script': kwargs.get('script', ''),
            'requirements': kwargs.get('requirements', []),
            'env_vars': kwargs.get('env_vars', {}),
            'auto_restart': kwargs.get('auto_restart', True),
            'auto_start': kwargs.get('auto_start', True),
            'status': 'stopped',
            'created_at': datetime.now(),
            'updated_at': datetime.now(),
            'last_started': None,
            'uptime_seconds': 0,
            'restart_count': 0,
            'pid': None,
            'last_error': None
        }
        
        try:
            self.db.applications.insert_one(application)
            print(f"✅ Created application: {application['name']} ({app_id})")
            return app_id
        except Exception as e:
            print(f"❌ Error creating application: {e}")
            raise
    
    def get_application(self, app_id):
        """Get application by ID"""
        try:
            app = self.db.applications.find_one({'app_id': app_id})
            if app:
                app['_id'] = str(app['_id'])
                return app
            return None
        except Exception as e:
            print(f"⚠️ Error getting application: {e}")
            return None
    
    def get_all_applications(self):
        """Get all applications"""
        try:
            apps = list(self.db.applications.find().sort('created_at', DESCENDING))
            for app in apps:
                app['_id'] = str(app['_id'])
                
                # Calculate uptime if running
                if app.get('status') == 'running' and app.get('last_started'):
                    if isinstance(app['last_started'], datetime):
                        uptime = (datetime.now() - app['last_started']).total_seconds()
                        app['uptime_seconds'] = uptime
            return apps
        except Exception as e:
            print(f"⚠️ Error getting applications: {e}")
            return []
    
    def update_application(self, app_id, updates):
        """Update application"""
        try:
            updates['updated_at'] = datetime.now()
            result = self.db.applications.update_one(
                {'app_id': app_id},
                {'$set': updates}
            )
            return result.modified_count > 0
        except Exception as e:
            print(f"⚠️ Error updating application: {e}")
            return False
    
    def delete_application(self, app_id):
        """Delete application and all related data"""
        try:
            # Delete application
            result = self.db.applications.delete_one({'app_id': app_id})
            
            if result.deleted_count > 0:
                # Delete related logs
                self.db.logs.delete_many({'app_id': app_id})
                print(f"✅ Deleted application: {app_id}")
                return True
            return False
        except Exception as e:
            print(f"❌ Error deleting application: {e}")
            return False
    
    def get_running_applications(self):
        """Get all running applications"""
        try:
            apps = list(self.db.applications.find({'status': 'running'}))
            for app in apps:
                app['_id'] = str(app['_id'])
            return apps
        except Exception as e:
            print(f"⚠️ Error getting running applications: {e}")
            return []
    
    def get_auto_start_applications(self):
        """Get applications with auto_start enabled"""
        try:
            apps = list(self.db.applications.find({
                'auto_start': True,
                'status': {'$ne': 'running'}
            }))
            for app in apps:
                app['_id'] = str(app['_id'])
            return apps
        except Exception as e:
            print(f"⚠️ Error getting auto-start applications: {e}")
            return []
    
    # Log operations
    def add_log(self, app_id, level, message):
        """Add log entry"""
        try:
            log_entry = {
                'app_id': app_id,
                'timestamp': datetime.now(),
                'level': level.upper(),
                'message': message[:1000]
            }
            self.db.logs.insert_one(log_entry)
            return True
        except Exception as e:
            print(f"⚠️ Error adding log: {e}")
            return False
    
    def get_logs(self, app_id, limit=100, offset=0, level=None):
        """Get logs for application"""
        try:
            query = {'app_id': app_id}
            if level:
                query['level'] = level.upper()
            
            logs = list(self.db.logs.find(query)
                       .sort('timestamp', DESCENDING)
                       .skip(offset)
                       .limit(limit))
            
            for log in logs:
                log['_id'] = str(log['_id'])
                log['timestamp'] = log['timestamp'].isoformat()
            return logs
        except Exception as e:
            print(f"⚠️ Error getting logs: {e}")
            return []
    
    def get_log_count(self, app_id, level=None):
        """Get total log count"""
        try:
            query = {'app_id': app_id}
            if level:
                query['level'] = level.upper()
            return self.db.logs.count_documents(query)
        except Exception as e:
            print(f"⚠️ Error getting log count: {e}")
            return 0
    
    def clear_logs(self, app_id):
        """Clear all logs for application"""
        try:
            result = self.db.logs.delete_many({'app_id': app_id})
            return result.deleted_count > 0
        except Exception as e:
            print(f"⚠️ Error clearing logs: {e}")
            return False
    
    # Statistics
    def get_platform_stats(self):
        """Get platform statistics"""
        try:
            stats = {
                'total_applications': self.db.applications.count_documents({}),
                'running_applications': self.db.applications.count_documents({'status': 'running'}),
                'stopped_applications': self.db.applications.count_documents({'status': 'stopped'}),
                'crashed_applications': self.db.applications.count_documents({'status': 'crashed'}),
                'total_logs': self.db.logs.count_documents({}),
                'timestamp': datetime.now().isoformat()
            }
            return stats
        except Exception as e:
            print(f"⚠️ Error getting stats: {e}")
            return {}

# ============================================================================
# PROCESS MANAGER
# ============================================================================

class ProcessManager:
    """Manages application processes"""
    
    def __init__(self, db):
        self.db = db
        self.processes = {}
        self.log_queues = defaultdict(queue.Queue)
        self.app_workspaces = {}
        self.monitoring = False
        self.monitor_thread = None
        
        # Create workspace directory
        CONFIG['WORKSPACE_BASE'].mkdir(exist_ok=True)
    
    def start_application(self, app_id):
        """Start an application"""
        try:
            app_data = self.db.get_application(app_id)
            if not app_data:
                print(f"❌ Application {app_id} not found")
                return False
            
            # Check if already running
            if app_id in self.processes:
                print(f"⚠️ Application {app_id} is already running")
                return False
            
            # Create workspace
            workspace = CONFIG['WORKSPACE_BASE'] / app_id
            workspace.mkdir(exist_ok=True)
            self.app_workspaces[app_id] = workspace
            
            # Install requirements
            requirements = app_data.get('requirements', [])
            if requirements:
                self._install_requirements(app_id, requirements)
            
            # Write application script
            script_content = app_data.get('script', '')
            script_file = workspace / 'app.py'
            script_file.write_text(script_content)
            
            # Prepare environment
            env = os.environ.copy()
            env.update(app_data.get('env_vars', {}))
            env['PYTHONUNBUFFERED'] = '1'
            env['APP_ID'] = app_id
            env['WORKSPACE'] = str(workspace)
            
            # Start process
            cmd = [sys.executable, str(script_file)]
            process = subprocess.Popen(
                cmd,
                cwd=str(workspace),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            # Store process info
            self.processes[app_id] = {
                'process': process,
                'pid': process.pid,
                'start_time': datetime.now(),
                'workspace': workspace,
                'cmd': cmd
            }
            
            # Start log capture
            self._start_log_capture(app_id, process)
            
            # Update database
            self.db.update_application(app_id, {
                'status': 'running',
                'pid': process.pid,
                'last_started': datetime.now(),
                'restart_count': app_data.get('restart_count', 0) + 1,
                'last_error': None
            })
            
            self.db.add_log(app_id, 'INFO', f"Application started with PID {process.pid}")
            print(f"✅ Started application {app_id} (PID: {process.pid})")
            return True
            
        except Exception as e:
            error_msg = f"Failed to start: {str(e)}"
            print(f"❌ Error starting application {app_id}: {e}")
            self.db.add_log(app_id, 'ERROR', error_msg)
            self.db.update_application(app_id, {'status': 'crashed', 'last_error': error_msg})
            return False
    
    def stop_application(self, app_id):
        """Stop an application"""
        try:
            if app_id not in self.processes:
                print(f"⚠️ Application {app_id} not running")
                return False
            
            process_info = self.processes[app_id]
            process = process_info['process']
            
            print(f"🛑 Stopping application {app_id} (PID: {process.pid})...")
            
            # Try graceful termination
            try:
                process.terminate()
                
                # Wait for process to terminate
                for _ in range(CONFIG['GRACEFUL_SHUTDOWN_TIMEOUT']):
                    if process.poll() is not None:
                        break
                    time.sleep(1)
                
                # Force kill if still running
                if process.poll() is None:
                    print(f"⚠️ Force killing application {app_id}")
                    process.kill()
                    process.wait()
                    
            except Exception as e:
                print(f"⚠️ Error during termination: {e}")
                try:
                    process.kill()
                except:
                    pass
            
            # Clean up
            if app_id in self.processes:
                del self.processes[app_id]
            
            # Update database
            self.db.update_application(app_id, {
                'status': 'stopped',
                'pid': None
            })
            
            self.db.add_log(app_id, 'INFO', "Application stopped by user")
            print(f"✅ Stopped application {app_id}")
            return True
            
        except Exception as e:
            print(f"❌ Error stopping application {app_id}: {e}")
            return False
    
    def restart_application(self, app_id):
        """Restart an application"""
        try:
            # Stop if running
            if app_id in self.processes:
                self.stop_application(app_id)
            
            time.sleep(1)
            
            # Start again
            return self.start_application(app_id)
            
        except Exception as e:
            print(f"❌ Error restarting application {app_id}: {e}")
            return False
    
    def _install_requirements(self, app_id, requirements):
        """Install Python packages"""
        try:
            workspace = self.app_workspaces[app_id]
            requirements_file = workspace / 'requirements.txt'
            requirements_file.write_text('\n'.join(requirements))
            
            cmd = [sys.executable, '-m', 'pip', 'install', '-r', str(requirements_file)]
            result = subprocess.run(
                cmd,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=300
            )
            
            if result.returncode == 0:
                self.db.add_log(app_id, 'INFO', f"Installed {len(requirements)} packages")
            else:
                error_msg = f"Failed to install packages: {result.stderr}"
                self.db.add_log(app_id, 'ERROR', error_msg)
                raise Exception(error_msg)
                
        except subprocess.TimeoutExpired:
            error_msg = "Package installation timeout (5 minutes)"
            self.db.add_log(app_id, 'ERROR', error_msg)
            raise Exception(error_msg)
        except Exception as e:
            self.db.add_log(app_id, 'ERROR', f"Installation error: {str(e)}")
            raise
    
    def _start_log_capture(self, app_id, process):
        """Start threads to capture stdout and stderr"""
        
        def capture_output(stream, level):
            try:
                for line in iter(stream.readline, ''):
                    if line:
                        line = line.rstrip('\n')
                        
                        # Add to log queue for SSE
                        self.log_queues[app_id].put({
                            'timestamp': datetime.now().isoformat(),
                            'level': level,
                            'message': line
                        })
                        
                        # Save to database
                        self.db.add_log(app_id, level, line)
                        
            except Exception as e:
                print(f"⚠️ Log capture error for {app_id}: {e}")
            finally:
                stream.close()
        
        # Start capture threads
        threading.Thread(
            target=capture_output,
            args=(process.stdout, 'INFO'),
            daemon=True
        ).start()
        
        threading.Thread(
            target=capture_output,
            args=(process.stderr, 'ERROR'),
            daemon=True
        ).start()
    
    def get_log_queue(self, app_id):
        """Get log queue for application"""
        return self.log_queues.get(app_id)
    
    def execute_command(self, app_id, command):
        """Execute command in application workspace"""
        try:
            if app_id not in self.app_workspaces:
                return {'error': 'Application workspace not found'}
            
            workspace = self.app_workspaces[app_id]
            
            # Security check
            dangerous_commands = ['rm -rf', 'sudo', 'chmod 777', '> /dev/', 'mkfs', 'dd']
            for dangerous in dangerous_commands:
                if dangerous in command:
                    return {'error': f'Command not allowed: {dangerous}'}
            
            # Execute command
            args = shlex.split(command)
            result = subprocess.run(
                args,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=CONFIG['COMMAND_TIMEOUT'],
                shell=False
            )
            
            return {
                'success': True,
                'command': command,
                'stdout': result.stdout,
                'stderr': result.stderr,
                'returncode': result.returncode,
                'timestamp': datetime.now().isoformat()
            }
            
        except subprocess.TimeoutExpired:
            return {'error': 'Command timed out after 60 seconds', 'command': command}
        except Exception as e:
            return {'error': str(e), 'command': command}
    
    def start_monitoring(self):
        """Start monitoring thread"""
        if self.monitoring:
            return
        
        self.monitoring = True
        self.monitor_thread = threading.Thread(
            target=self._monitor_applications,
            daemon=True
        )
        self.monitor_thread.start()
        print("✅ Started application monitoring")
    
    def stop_monitoring(self):
        """Stop monitoring thread"""
        self.monitoring = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
        print("✅ Stopped application monitoring")
    
    def _monitor_applications(self):
        """Monitor all running applications"""
        while self.monitoring:
            try:
                apps_to_restart = []
                
                for app_id, process_info in list(self.processes.items()):
                    process = process_info['process']
                    
                    # Check if process terminated
                    if process.poll() is not None:
                        returncode = process.returncode
                        
                        # Get app data
                        app_data = self.db.get_application(app_id)
                        auto_restart = app_data.get('auto_restart', True) if app_data else True
                        
                        # Log the crash
                        if returncode != 0:
                            self.db.add_log(app_id, 'ERROR', f"Process crashed with exit code {returncode}")
                        
                        # Clean up
                        if app_id in self.processes:
                            del self.processes[app_id]
                        
                        # Update database
                        self.db.update_application(app_id, {
                            'status': 'crashed' if returncode != 0 else 'stopped',
                            'pid': None
                        })
                        
                        # Auto-restart if configured
                        if auto_restart and returncode != 0:
                            print(f"🔄 Auto-restarting crashed application: {app_id}")
                            apps_to_restart.append(app_id)
                
                # Restart crashed apps
                for app_id in apps_to_restart:
                    time.sleep(2)
                    self.start_application(app_id)
                
                time.sleep(CONFIG['MONITOR_INTERVAL'])
                
            except Exception as e:
                print(f"⚠️ Monitoring error: {e}")
                time.sleep(10)
    
    def is_alive(self):
        """Check if process manager is running"""
        return self.monitoring
    
    def cleanup_workspaces(self):
        """Clean up temporary workspaces"""
        try:
            for app_id, workspace in list(self.app_workspaces.items()):
                if app_id not in self.processes:
                    try:
                        shutil.rmtree(workspace, ignore_errors=True)
                        del self.app_workspaces[app_id]
                    except:
                        pass
        except Exception as e:
            print(f"⚠️ Error cleaning workspaces: {e}")

# ============================================================================
# FLASK APPLICATION
# ============================================================================

# Initialize Flask app
app = Flask(__name__)
app.secret_key = CONFIG['SECRET_KEY']
CORS(app)

# Initialize database and process manager
db = Database()
pm = ProcessManager(db)

# Authentication middleware
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Calculate password hash
ADMIN_PASSWORD_HASH = hashlib.sha256(CONFIG['ADMIN_PASSWORD'].encode()).hexdigest()

# ============================================================================
# HTML TEMPLATES
# ============================================================================

BASE_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}App Hosting Platform{% endblock %}</title>
    
    <!-- Bootstrap 5 -->
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    
    <!-- Font Awesome -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <!-- Custom CSS -->
    <style>
        :root {
            --primary: #667eea;
            --primary-dark: #764ba2;
            --success: #10b981;
            --danger: #ef4444;
            --warning: #f59e0b;
            --info: #3b82f6;
            --light: #f3f4f6;
            --dark: #111827;
        }
        
        body {
            background-color: var(--light);
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
        }
        
        .navbar {
            background: linear-gradient(135deg, var(--primary), var(--primary-dark));
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        
        .stat-card {
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            transition: transform 0.3s, box-shadow 0.3s;
            border: none;
            background: white;
        }
        
        .stat-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 8px 15px rgba(0,0,0,0.1);
        }
        
        .stat-card.running { border-left: 5px solid var(--success); }
        .stat-card.stopped { border-left: 5px solid #6b7280; }
        .stat-card.crashed { border-left: 5px solid var(--danger); }
        .stat-card.total { border-left: 5px solid var(--primary); }
        
        .stat-icon {
            font-size: 2.5rem;
            margin-bottom: 10px;
            opacity: 0.8;
        }
        
        .btn-primary {
            background: linear-gradient(135deg, var(--primary), var(--primary-dark));
            border: none;
            padding: 10px 20px;
            border-radius: 8px;
            transition: all 0.3s;
        }
        
        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
        }
        
        .table-hover tbody tr:hover {
            background-color: rgba(102, 126, 234, 0.05);
        }
        
        .badge {
            padding: 6px 12px;
            border-radius: 20px;
            font-weight: 500;
        }
        
        .log-info { color: var(--success); }
        .log-error { color: var(--danger); }
        .log-warning { color: var(--warning); }
        .log-debug { color: #6b7280; }
        
        .terminal {
            background-color: #1a202c;
            color: #e2e8f0;
            font-family: 'Courier New', monospace;
            padding: 15px;
            border-radius: 8px;
            height: 500px;
            overflow-y: auto;
        }
        
        .terminal-line {
            margin-bottom: 5px;
            white-space: pre-wrap;
            word-break: break-all;
        }
        
        .terminal-prompt {
            color: #68d391;
        }
        
        .terminal-command {
            color: #e2e8f0;
        }
        
        .terminal-output {
            color: #a0aec0;
        }
        
        .toast-container {
            z-index: 1055;
        }
        
        .form-control:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 0.2rem rgba(102, 126, 234, 0.25);
        }
        
        .custom-select {
            border-radius: 8px;
            padding: 10px;
            border: 1px solid #d1d5db;
            width: 100%;
        }
        
        .code-editor {
            font-family: 'Courier New', monospace;
            min-height: 300px;
            border-radius: 8px;
            border: 1px solid #d1d5db;
            padding: 15px;
        }
        
        .env-var-item {
            background: #f8fafc;
            border-radius: 6px;
            padding: 10px;
            margin-bottom: 10px;
            border: 1px solid #e2e8f0;
        }
        
        .action-btn {
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 0.875rem;
            transition: all 0.2s;
        }
        
        .action-btn:hover {
            transform: translateY(-1px);
        }
    </style>
    
    {% block extra_css %}{% endblock %}
</head>
<body>
    <!-- Navigation -->
    <nav class="navbar navbar-expand-lg navbar-dark">
        <div class="container-fluid">
            <a class="navbar-brand fw-bold" href="{{ url_for('dashboard') }}">
                <i class="fas fa-cloud me-2"></i>App Hosting Platform
            </a>
            
            <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
                <span class="navbar-toggler-icon"></span>
            </button>
            
            <div class="collapse navbar-collapse" id="navbarNav">
                <ul class="navbar-nav me-auto">
                    <li class="nav-item">
                        <a class="nav-link {% if request.endpoint == 'dashboard' %}active{% endif %}" 
                           href="{{ url_for('dashboard') }}">
                            <i class="fas fa-tachometer-alt me-1"></i> Dashboard
                        </a>
                    </li>
                    <li class="nav-item">
                        <a class="nav-link {% if request.endpoint == 'apps_page' %}active{% endif %}" 
                           href="{{ url_for('apps_page') }}">
                            <i class="fas fa-box me-1"></i> Applications
                        </a>
                    </li>
                    <li class="nav-item">
                        <a class="nav-link {% if request.endpoint == 'create_app_page' %}active{% endif %}" 
                           href="{{ url_for('create_app_page') }}">
                            <i class="fas fa-plus-circle me-1"></i> Create App
                        </a>
                    </li>
                </ul>
                
                <ul class="navbar-nav">
                    <li class="nav-item dropdown">
                        <a class="nav-link dropdown-toggle" href="#" id="userDropdown" 
                           role="button" data-bs-toggle="dropdown">
                            <i class="fas fa-user me-1"></i> {{ session.get('username', 'Admin') }}
                        </a>
                        <ul class="dropdown-menu dropdown-menu-end">
                            <li><a class="dropdown-item" href="#"><i class="fas fa-cog me-2"></i>Settings</a></li>
                            <li><hr class="dropdown-divider"></li>
                            <li><a class="dropdown-item text-danger" href="{{ url_for('logout') }}">
                                <i class="fas fa-sign-out-alt me-2"></i>Logout
                            </a></li>
                        </ul>
                    </li>
                </ul>
            </div>
        </div>
    </nav>

    <!-- Main Content -->
    <div class="container-fluid mt-4">
        {% block content %}{% endblock %}
    </div>

    <!-- Toast Container -->
    <div id="toast-container" class="toast-container position-fixed bottom-0 end-0 p-3"></div>

    <!-- Bootstrap JS -->
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    
    <!-- jQuery -->
    <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
    
    <!-- Custom JS -->
    <script>
        // Toast notification system
        function showToast(message, type = 'info', duration = 3000) {
            const types = {
                'success': {bg: 'bg-success', icon: 'check-circle'},
                'error': {bg: 'bg-danger', icon: 'exclamation-circle'},
                'warning': {bg: 'bg-warning', icon: 'exclamation-triangle'},
                'info': {bg: 'bg-info', icon: 'info-circle'}
            };
            
            const toastType = types[type] || types.info;
            const toastId = 'toast-' + Date.now();
            
            const toastHtml = `
                <div id="${toastId}" class="toast ${toastType.bg} text-white" role="alert">
                    <div class="d-flex">
                        <div class="toast-body">
                            <i class="fas fa-${toastType.icon} me-2"></i>${message}
                        </div>
                        <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
                    </div>
                </div>
            `;
            
            // Add to container
            let container = $('#toast-container');
            if (container.length === 0) {
                $('body').append('<div id="toast-container" class="toast-container position-fixed bottom-0 end-0 p-3"></div>');
                container = $('#toast-container');
            }
            
            container.append(toastHtml);
            
            // Show toast
            const toastEl = document.getElementById(toastId);
            const toast = new bootstrap.Toast(toastEl, {delay: duration});
            toast.show();
            
            // Remove after hide
            toastEl.addEventListener('hidden.bs.toast', function () {
                $(this).remove();
            });
        }
        
        // API helper functions
        function apiCall(url, method = 'GET', data = null) {
            const headers = {
                'Content-Type': 'application/json',
            };
            
            const options = {
                method: method,
                headers: headers,
            };
            
            if (data && (method === 'POST' || method === 'PUT')) {
                options.body = JSON.stringify(data);
            }
            
            return fetch(url, options)
                .then(response => {
                    if (!response.ok) {
                        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
                    }
                    return response.json();
                });
        }
        
        // Application control functions
        function startApplication(appId) {
            if (!confirm('Start this application?')) return;
            
            apiCall(`/api/apps/${appId}/start`, 'POST')
                .then(data => {
                    if (data.success) {
                        showToast('Application started successfully', 'success');
                        setTimeout(() => location.reload(), 1000);
                    } else {
                        showToast(data.error || 'Failed to start application', 'error');
                    }
                })
                .catch(error => {
                    showToast('Network error: ' + error.message, 'error');
                });
        }
        
        function stopApplication(appId) {
            if (!confirm('Stop this application?')) return;
            
            apiCall(`/api/apps/${appId}/stop`, 'POST')
                .then(data => {
                    if (data.success) {
                        showToast('Application stopped successfully', 'success');
                        setTimeout(() => location.reload(), 1000);
                    } else {
                        showToast(data.error || 'Failed to stop application', 'error');
                    }
                })
                .catch(error => {
                    showToast('Network error: ' + error.message, 'error');
                });
        }
        
        function restartApplication(appId) {
            if (!confirm('Restart this application?')) return;
            
            apiCall(`/api/apps/${appId}/restart`, 'POST')
                .then(data => {
                    if (data.success) {
                        showToast('Application restarted successfully', 'success');
                        setTimeout(() => location.reload(), 1000);
                    } else {
                        showToast(data.error || 'Failed to restart application', 'error');
                    }
                })
                .catch(error => {
                    showToast('Network error: ' + error.message, 'error');
                });
        }
        
        function deleteApplication(appId, appName) {
            if (!confirm(`Delete application "${appName}"? This action cannot be undone.`)) return;
            
            apiCall(`/api/apps/${appId}`, 'DELETE')
                .then(data => {
                    if (data.success) {
                        showToast('Application deleted successfully', 'success');
                        setTimeout(() => location.reload(), 1000);
                    } else {
                        showToast(data.error || 'Failed to delete application', 'error');
                    }
                })
                .catch(error => {
                    showToast('Network error: ' + error.message, 'error');
                });
        }
        
        // Load system health
        function loadSystemHealth() {
            apiCall('/api/system/health')
                .then(data => {
                    // Update UI with system health
                    console.log('System health:', data);
                })
                .catch(error => {
                    console.error('Error loading system health:', error);
                });
        }
        
        // Document ready
        $(document).ready(function() {
            // Auto-hide alerts after 5 seconds
            setTimeout(() => {
                $('.alert').fadeOut('slow');
            }, 5000);
            
            // Initialize tooltips
            $('[data-bs-toggle="tooltip"]').tooltip();
        });
    </script>
    
    {% block extra_js %}{% endblock %}
</body>
</html>
'''

LOGIN_HTML = '''
{% extends "base.html" %}

{% block title %}Login - App Hosting Platform{% endblock %}

{% block content %}
<div class="row justify-content-center">
    <div class="col-md-6 col-lg-4">
        <div class="card border-0 shadow-lg mt-5">
            <div class="card-body p-5">
                <div class="text-center mb-4">
                    <i class="fas fa-cloud fa-3x text-primary mb-3"></i>
                    <h2 class="h4 text-gray-900 mb-1">App Hosting Platform</h2>
                    <p class="text-muted">Sign in to your account</p>
                </div>
                
                {% if error %}
                <div class="alert alert-danger alert-dismissible fade show" role="alert">
                    <i class="fas fa-exclamation-circle me-2"></i>{{ error }}
                    <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
                </div>
                {% endif %}
                
                <form method="POST" action="{{ url_for('login') }}">
                    <div class="mb-3">
                        <label for="username" class="form-label">Username</label>
                        <div class="input-group">
                            <span class="input-group-text"><i class="fas fa-user"></i></span>
                            <input type="text" class="form-control" id="username" name="username" 
                                   required autofocus>
                        </div>
                    </div>
                    
                    <div class="mb-4">
                        <label for="password" class="form-label">Password</label>
                        <div class="input-group">
                            <span class="input-group-text"><i class="fas fa-lock"></i></span>
                            <input type="password" class="form-control" id="password" name="password" required>
                        </div>
                    </div>
                    
                    <button type="submit" class="btn btn-primary btn-block w-100 py-2">
                        <i class="fas fa-sign-in-alt me-2"></i>Sign In
                    </button>
                </form>
                
                <hr class="my-4">
                
                <div class="text-center">
                    <small class="text-muted">
                        <i class="fas fa-info-circle me-1"></i>
                        Default credentials: admin / admin123
                    </small>
                </div>
            </div>
        </div>
    </div>
</div>
{% endblock %}
'''

DASHBOARD_HTML = '''
{% extends "base.html" %}

{% block title %}Dashboard - App Hosting Platform{% endblock %}

{% block content %}
<div class="row">
    <!-- Page Header -->
    <div class="col-12 mb-4">
        <h1 class="h2 mb-0"><i class="fas fa-tachometer-alt me-2"></i>Dashboard</h1>
        <p class="text-muted">Monitor and manage your applications</p>
    </div>
    
    <!-- Statistics Cards -->
    <div class="col-md-3 col-sm-6">
        <div class="stat-card total">
            <div class="stat-icon text-primary">
                <i class="fas fa-box"></i>
            </div>
            <h3 class="mb-1">{{ total_apps }}</h3>
            <p class="text-muted mb-0">Total Applications</p>
        </div>
    </div>
    
    <div class="col-md-3 col-sm-6">
        <div class="stat-card running">
            <div class="stat-icon text-success">
                <i class="fas fa-play-circle"></i>
            </div>
            <h3 class="mb-1">{{ running_apps }}</h3>
            <p class="text-muted mb-0">Running</p>
        </div>
    </div>
    
    <div class="col-md-3 col-sm-6">
        <div class="stat-card stopped">
            <div class="stat-icon text-secondary">
                <i class="fas fa-stop-circle"></i>
            </div>
            <h3 class="mb-1">{{ stopped_apps }}</h3>
            <p class="text-muted mb-0">Stopped</p>
        </div>
    </div>
    
    <div class="col-md-3 col-sm-6">
        <div class="stat-card crashed">
            <div class="stat-icon text-danger">
                <i class="fas fa-exclamation-triangle"></i>
            </div>
            <h3 class="mb-1">{{ crashed_apps }}</h3>
            <p class="text-muted mb-0">Crashed</p>
        </div>
    </div>
    
    <!-- Quick Actions -->
    <div class="col-12">
        <div class="card border-0 shadow-sm mb-4">
            <div class="card-header bg-white py-3">
                <h5 class="mb-0"><i class="fas fa-bolt me-2"></i>Quick Actions</h5>
            </div>
            <div class="card-body">
                <div class="d-flex flex-wrap gap-2">
                    <a href="{{ url_for('create_app_page') }}" class="btn btn-primary">
                        <i class="fas fa-plus me-2"></i>Create New App
                    </a>
                    <a href="{{ url_for('apps_page') }}" class="btn btn-success">
                        <i class="fas fa-box me-2"></i>View All Apps
                    </a>
                    <button class="btn btn-info" onclick="loadSystemHealth()">
                        <i class="fas fa-heartbeat me-2"></i>Check System Health
                    </button>
                    <button class="btn btn-warning" onclick="refreshStats()">
                        <i class="fas fa-sync-alt me-2"></i>Refresh Stats
                    </button>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Recent Applications -->
    <div class="col-12">
        <div class="card border-0 shadow-sm">
            <div class="card-header bg-white py-3 d-flex justify-content-between align-items-center">
                <h5 class="mb-0"><i class="fas fa-history me-2"></i>Recent Applications</h5>
                <a href="{{ url_for('apps_page') }}" class="btn btn-sm btn-outline-primary">
                    View All <i class="fas fa-arrow-right ms-1"></i>
                </a>
            </div>
            <div class="card-body">
                {% if apps %}
                <div class="table-responsive">
                    <table class="table table-hover">
                        <thead class="table-light">
                            <tr>
                                <th>Name</th>
                                <th>Type</th>
                                <th>Status</th>
                                <th>Created</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for app in apps %}
                            <tr>
                                <td>
                                    <div class="d-flex align-items-center">
                                        <div class="me-3">
                                            {% if app.type == 'telegram-bot' %}
                                                <i class="fab fa-telegram fa-lg text-info"></i>
                                            {% elif app.type == 'flask-api' %}
                                                <i class="fas fa-server fa-lg text-primary"></i>
                                            {% elif app.type == 'discord-bot' %}
                                                <i class="fab fa-discord fa-lg text-purple"></i>
                                            {% else %}
                                                <i class="fas fa-code fa-lg text-secondary"></i>
                                            {% endif %}
                                        </div>
                                        <div>
                                            <strong>{{ app.name }}</strong><br>
                                            <small class="text-muted">{{ app.app_id[:8] }}...</small>
                                        </div>
                                    </div>
                                </td>
                                <td>
                                    {% if app.type == 'telegram-bot' %}
                                        <span class="badge bg-info">Telegram Bot</span>
                                    {% elif app.type == 'flask-api' %}
                                        <span class="badge bg-primary">Flask API</span>
                                    {% elif app.type == 'discord-bot' %}
                                        <span class="badge bg-purple">Discord Bot</span>
                                    {% else %}
                                        <span class="badge bg-secondary">{{ app.type }}</span>
                                    {% endif %}
                                </td>
                                <td>
                                    {% if app.status == 'running' %}
                                        <span class="badge bg-success">
                                            <i class="fas fa-play-circle me-1"></i>Running
                                        </span>
                                        <br>
                                        <small class="text-muted">Uptime: {{ (app.uptime_seconds or 0)|int }}s</small>
                                    {% elif app.status == 'stopped' %}
                                        <span class="badge bg-secondary">
                                            <i class="fas fa-stop-circle me-1"></i>Stopped
                                        </span>
                                    {% elif app.status == 'crashed' %}
                                        <span class="badge bg-danger">
                                            <i class="fas fa-exclamation-triangle me-1"></i>Crashed
                                        </span>
                                    {% endif %}
                                </td>
                                <td>
                                    <small>{{ app.created_at.strftime('%Y-%m-%d') if app.created_at else 'N/A' }}</small>
                                </td>
                                <td>
                                    <div class="btn-group btn-group-sm">
                                        <a href="{{ url_for('app_detail', app_id=app.app_id) }}" 
                                           class="btn btn-outline-primary" 
                                           data-bs-toggle="tooltip" title="View Details">
                                            <i class="fas fa-eye"></i>
                                        </a>
                                        {% if app.status == 'running' %}
                                            <button class="btn btn-outline-danger stop-app" 
                                                    data-app-id="{{ app.app_id }}"
                                                    data-bs-toggle="tooltip" title="Stop">
                                                <i class="fas fa-stop"></i>
                                            </button>
                                            <button class="btn btn-outline-warning restart-app" 
                                                    data-app-id="{{ app.app_id }}"
                                                    data-bs-toggle="tooltip" title="Restart">
                                                <i class="fas fa-redo"></i>
                                            </button>
                                        {% else %}
                                            <button class="btn btn-outline-success start-app" 
                                                    data-app-id="{{ app.app_id }}"
                                                    data-bs-toggle="tooltip" title="Start">
                                                <i class="fas fa-play"></i>
                                            </button>
                                        {% endif %}
                                    </div>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                {% else %}
                <div class="text-center py-5">
                    <i class="fas fa-box-open fa-4x text-muted mb-3"></i>
                    <h5 class="text-muted">No applications yet</h5>
                    <p class="text-muted mb-4">Create your first application to get started</p>
                    <a href="{{ url_for('create_app_page') }}" class="btn btn-primary">
                        <i class="fas fa-plus me-2"></i>Create Application
                    </a>
                </div>
                {% endif %}
            </div>
        </div>
    </div>
    
    <!-- System Info -->
    <div class="col-md-6 mt-4">
        <div class="card border-0 shadow-sm">
            <div class="card-header bg-white py-3">
                <h5 class="mb-0"><i class="fas fa-info-circle me-2"></i>System Information</h5>
            </div>
            <div class="card-body">
                <div id="system-info">
                    <p class="text-center mb-0">
                        <i class="fas fa-spinner fa-spin me-2"></i>Loading system information...
                    </p>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Platform Stats -->
    <div class="col-md-6 mt-4">
        <div class="card border-0 shadow-sm">
            <div class="card-header bg-white py-3">
                <h5 class="mb-0"><i class="fas fa-chart-bar me-2"></i>Platform Statistics</h5>
            </div>
            <div class="card-body">
                <div class="list-group list-group-flush">
                    <div class="list-group-item d-flex justify-content-between align-items-center border-0 px-0">
                        Total Applications
                        <span class="badge bg-primary rounded-pill">{{ stats.total_applications or 0 }}</span>
                    </div>
                    <div class="list-group-item d-flex justify-content-between align-items-center border-0 px-0">
                        Total Logs
                        <span class="badge bg-info rounded-pill">{{ stats.total_logs or 0 }}</span>
                    </div>
                    <div class="list-group-item d-flex justify-content-between align-items-center border-0 px-0">
                        Last Updated
                        <small class="text-muted">{{ stats.timestamp or 'N/A' }}</small>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>
{% endblock %}

{% block extra_js %}
<script>
    // Load system information
    function loadSystemHealth() {
        $('#system-info').html('<p class="text-center mb-0"><i class="fas fa-spinner fa-spin me-2"></i>Loading...</p>');
        
        apiCall('/api/system/health')
            .then(data => {
                let html = `
                    <div class="mb-3">
                        <strong>Status:</strong>
                        <span class="badge ${data.status === 'healthy' ? 'bg-success' : 'bg-warning'}">
                            ${data.status.toUpperCase()}
                        </span>
                    </div>
                    <div class="mb-2"><strong>Database:</strong> ${data.database}</div>
                    <div class="mb-2"><strong>Process Manager:</strong> ${data.process_manager}</div>
                    <div class="mb-2"><strong>Platform:</strong> ${data.system.platform}</div>
                    <div class="mb-2"><strong>Python:</strong> ${data.system.python_version.split(' ')[0]}</div>
                    <div class="mb-2"><strong>CPU Cores:</strong> ${data.system.cpu_count}</div>
                    <div class="mb-2">
                        <strong>Memory:</strong> 
                        ${data.system.memory_available_gb.toFixed(1)} / 
                        ${data.system.memory_total_gb.toFixed(1)} GB available
                    </div>
                    <div class="text-muted"><small>Last checked: ${new Date().toLocaleTimeString()}</small></div>
                `;
                $('#system-info').html(html);
                showToast('System health loaded successfully', 'success');
            })
            .catch(error => {
                $('#system-info').html('<p class="text-danger mb-0">Error loading system health</p>');
                showToast('Error loading system health', 'error');
            });
    }
    
    // Refresh statistics
    function refreshStats() {
        apiCall('/api/stats')
            .then(data => {
                showToast('Statistics refreshed successfully', 'success');
                // In a real app, you would update the UI with new stats
                location.reload();
            })
            .catch(error => {
                showToast('Error refreshing stats', 'error');
            });
    }
    
    $(document).ready(function() {
        // Load system health on page load
        loadSystemHealth();
        
        // Start application
        $('.start-app').click(function() {
            const appId = $(this).data('app-id');
            startApplication(appId);
        });
        
        // Stop application
        $('.stop-app').click(function() {
            const appId = $(this).data('app-id');
            stopApplication(appId);
        });
        
        // Restart application
        $('.restart-app').click(function() {
            const appId = $(this).data('app-id');
            restartApplication(appId);
        });
        
        // Auto-refresh every 30 seconds
        setInterval(() => {
            // You can add auto-refresh logic here
        }, 30000);
    });
</script>
{% endblock %}
'''

APPS_HTML = '''
{% extends "base.html" %}

{% block title %}Applications - App Hosting Platform{% endblock %}

{% block extra_css %}
<style>
    .app-card {
        border-radius: 12px;
        border: 1px solid #e5e7eb;
        background: white;
        transition: all 0.3s;
        margin-bottom: 20px;
    }
    
    .app-card:hover {
        transform: translateY(-5px);
        box-shadow: 0 10px 25px rgba(0,0,0,0.1);
        border-color: #667eea;
    }
    
    .app-card-header {
        border-bottom: 1px solid #e5e7eb;
        padding: 15px 20px;
        background: #f9fafb;
        border-radius: 12px 12px 0 0;
    }
    
    .app-card-body {
        padding: 20px;
    }
    
    .app-status-badge {
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 0.75rem;
        font-weight: 600;
    }
    
    .search-box {
        max-width: 400px;
    }
</style>
{% endblock %}

{% block content %}
<div class="row">
    <!-- Page Header -->
    <div class="col-12 mb-4">
        <div class="d-flex justify-content-between align-items-center">
            <div>
                <h1 class="h2 mb-0"><i class="fas fa-box me-2"></i>Applications</h1>
                <p class="text-muted">Manage all your applications in one place</p>
            </div>
            <a href="{{ url_for('create_app_page') }}" class="btn btn-primary">
                <i class="fas fa-plus me-2"></i>Create New App
            </a>
        </div>
    </div>
    
    <!-- Search and Filter -->
    <div class="col-12 mb-4">
        <div class="card border-0 shadow-sm">
            <div class="card-body">
                <div class="row">
                    <div class="col-md-6">
                        <div class="input-group search-box">
                            <span class="input-group-text"><i class="fas fa-search"></i></span>
                            <input type="text" id="searchInput" class="form-control" 
                                   placeholder="Search applications...">
                        </div>
                    </div>
                    <div class="col-md-6 text-end">
                        <div class="btn-group">
                            <button class="btn btn-outline-secondary dropdown-toggle" type="button" 
                                    data-bs-toggle="dropdown" aria-expanded="false">
                                <i class="fas fa-filter me-2"></i>Filter
                            </button>
                            <ul class="dropdown-menu dropdown-menu-end">
                                <li><a class="dropdown-item" href="#" onclick="filterApps('all')">All Apps</a></li>
                                <li><a class="dropdown-item" href="#" onclick="filterApps('running')">Running</a></li>
                                <li><a class="dropdown-item" href="#" onclick="filterApps('stopped')">Stopped</a></li>
                                <li><a class="dropdown-item" href="#" onclick="filterApps('crashed')">Crashed</a></li>
                            </ul>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Applications Grid -->
    <div class="col-12">
        <div class="row" id="appsContainer">
            {% for app in apps %}
            <div class="col-md-6 col-lg-4 app-item" 
                 data-status="{{ app.status }}" 
                 data-name="{{ app.name|lower }}" 
                 data-type="{{ app.type }}">
                <div class="app-card">
                    <div class="app-card-header d-flex justify-content-between align-items-center">
                        <div>
                            <h6 class="mb-0 fw-bold">{{ app.name }}</h6>
                            <small class="text-muted">{{ app.app_id[:8] }}...</small>
                        </div>
                        <div>
                            {% if app.status == 'running' %}
                                <span class="app-status-badge bg-success text-white">
                                    <i class="fas fa-play-circle me-1"></i>Running
                                </span>
                            {% elif app.status == 'stopped' %}
                                <span class="app-status-badge bg-secondary text-white">
                                    <i class="fas fa-stop-circle me-1"></i>Stopped
                                </span>
                            {% elif app.status == 'crashed' %}
                                <span class="app-status-badge bg-danger text-white">
                                    <i class="fas fa-exclamation-triangle me-1"></i>Crashed
                                </span>
                            {% endif %}
                        </div>
                    </div>
                    
                    <div class="app-card-body">
                        <div class="mb-3">
                            <small class="text-muted d-block">Type</small>
                            {% if app.type == 'telegram-bot' %}
                                <span class="badge bg-info">
                                    <i class="fab fa-telegram me-1"></i>Telegram Bot
                                </span>
                            {% elif app.type == 'flask-api' %}
                                <span class="badge bg-primary">
                                    <i class="fas fa-server me-1"></i>Flask API
                                </span>
                            {% elif app.type == 'discord-bot' %}
                                <span class="badge bg-purple">
                                    <i class="fab fa-discord me-1"></i>Discord Bot
                                </span>
                            {% else %}
                                <span class="badge bg-secondary">{{ app.type }}</span>
                            {% endif %}
                        </div>
                        
                        <div class="mb-3">
                            <small class="text-muted d-block">Created</small>
                            <span>{{ app.created_at.strftime('%Y-%m-%d %H:%M') if app.created_at else 'N/A' }}</span>
                        </div>
                        
                        <div class="mb-4">
                            <small class="text-muted d-block">Uptime</small>
                            <span>
                                {% if app.status == 'running' %}
                                    {{ (app.uptime_seconds or 0)|int }} seconds
                                {% else %}
                                    -
                                {% endif %}
                            </span>
                        </div>
                        
                        <div class="d-flex justify-content-between">
                            <div class="btn-group">
                                <a href="{{ url_for('app_detail', app_id=app.app_id) }}" 
                                   class="btn btn-sm btn-outline-primary" title="View">
                                    <i class="fas fa-eye"></i>
                                </a>
                                <a href="{{ url_for('logs_page', app_id=app.app_id) }}" 
                                   class="btn btn-sm btn-outline-info" title="Logs">
                                    <i class="fas fa-file-alt"></i>
                                </a>
                                <a href="{{ url_for('terminal_page', app_id=app.app_id) }}" 
                                   class="btn btn-sm btn-outline-secondary" title="Terminal">
                                    <i class="fas fa-terminal"></i>
                                </a>
                            </div>
                            
                            <div class="btn-group">
                                {% if app.status == 'running' %}
                                    <button class="btn btn-sm btn-outline-warning restart-app" 
                                            data-app-id="{{ app.app_id }}" title="Restart">
                                        <i class="fas fa-redo"></i>
                                    </button>
                                    <button class="btn btn-sm btn-outline-danger stop-app" 
                                            data-app-id="{{ app.app_id }}" title="Stop">
                                        <i class="fas fa-stop"></i>
                                    </button>
                                {% else %}
                                    <button class="btn btn-sm btn-outline-success start-app" 
                                            data-app-id="{{ app.app_id }}" title="Start">
                                        <i class="fas fa-play"></i>
                                    </button>
                                {% endif %}
                                <button class="btn btn-sm btn-outline-danger delete-app" 
                                        data-app-id="{{ app.app_id }}" 
                                        data-app-name="{{ app.name }}" title="Delete">
                                    <i class="fas fa-trash"></i>
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            {% endfor %}
            
            {% if not apps %}
            <div class="col-12">
                <div class="text-center py-5">
                    <i class="fas fa-box-open fa-4x text-muted mb-3"></i>
                    <h5 class="text-muted">No applications found</h5>
                    <p class="text-muted mb-4">Create your first application to get started</p>
                    <a href="{{ url_for('create_app_page') }}" class="btn btn-primary">
                        <i class="fas fa-plus me-2"></i>Create Application
                    </a>
                </div>
            </div>
            {% endif %}
        </div>
    </div>
</div>
{% endblock %}

{% block extra_js %}
<script>
    // Search and filter functionality
    function filterApps(status) {
        $('.app-item').show();
        
        if (status !== 'all') {
            $('.app-item').each(function() {
                if ($(this).data('status') !== status) {
                    $(this).hide();
                }
            });
        }
    }
    
    // Search functionality
    $('#searchInput').on('input', function() {
        const searchTerm = $(this).val().toLowerCase();
        
        $('.app-item').each(function() {
            const appName = $(this).data('name');
            const appType = $(this).data('type');
            
            if (appName.includes(searchTerm) || appType.includes(searchTerm)) {
                $(this).show();
            } else {
                $(this).hide();
            }
        });
    });
    
    $(document).ready(function() {
        // Start application
        $('.start-app').click(function() {
            const appId = $(this).data('app-id');
            startApplication(appId);
        });
        
        // Stop application
        $('.stop-app').click(function() {
            const appId = $(this).data('app-id');
            stopApplication(appId);
        });
        
        // Restart application
        $('.restart-app').click(function() {
            const appId = $(this).data('app-id');
            restartApplication(appId);
        });
        
        // Delete application
        $('.delete-app').click(function() {
            const appId = $(this).data('app-id');
            const appName = $(this).data('app-name');
            deleteApplication(appId, appName);
        });
    });
</script>
{% endblock %}
'''

CREATE_APP_HTML = '''
{% extends "base.html" %}

{% block title %}Create Application - App Hosting Platform{% endblock %}

{% block extra_css %}
<style>
    .step-indicator {
        display: flex;
        justify-content: space-between;
        margin-bottom: 30px;
        position: relative;
    }
    
    .step-indicator::before {
        content: '';
        position: absolute;
        top: 15px;
        left: 0;
        right: 0;
        height: 2px;
        background: #e5e7eb;
        z-index: 1;
    }
    
    .step {
        position: relative;
        z-index: 2;
        text-align: center;
        flex: 1;
    }
    
    .step-number {
        width: 32px;
        height: 32px;
        border-radius: 50%;
        background: white;
        border: 2px solid #e5e7eb;
        display: flex;
        align-items: center;
        justify-content: center;
        margin: 0 auto 8px;
        font-weight: 600;
        color: #6b7280;
    }
    
    .step.active .step-number {
        background: var(--primary);
        border-color: var(--primary);
        color: white;
    }
    
    .step.completed .step-number {
        background: var(--success);
        border-color: var(--success);
        color: white;
    }
    
    .step-label {
        font-size: 0.875rem;
        color: #6b7280;
    }
    
    .step.active .step-label {
        color: var(--primary);
        font-weight: 600;
    }
    
    .template-card {
        border: 2px solid #e5e7eb;
        border-radius: 10px;
        padding: 20px;
        text-align: center;
        cursor: pointer;
        transition: all 0.3s;
        height: 100%;
        background: white;
    }
    
    .template-card:hover {
        border-color: var(--primary);
        transform: translateY(-5px);
        box-shadow: 0 10px 25px rgba(102, 126, 234, 0.1);
    }
    
    .template-card.selected {
        border-color: var(--primary);
        background: rgba(102, 126, 234, 0.05);
    }
    
    .template-icon {
        font-size: 2.5rem;
        margin-bottom: 15px;
        color: var(--primary);
    }
    
    .template-badge {
        position: absolute;
        top: 10px;
        right: 10px;
    }
    
    .code-editor {
        font-family: 'Courier New', monospace;
        min-height: 300px;
        border-radius: 8px;
        border: 1px solid #d1d5db;
        padding: 15px;
        background: #f8fafc;
        white-space: pre-wrap;
        word-break: break-all;
    }
    
    .form-section {
        background: white;
        border-radius: 12px;
        padding: 25px;
        border: 1px solid #e5e7eb;
        margin-bottom: 25px;
    }
    
    .env-var-row {
        background: #f8fafc;
        border-radius: 8px;
        padding: 15px;
        margin-bottom: 15px;
        border: 1px solid #e2e8f0;
    }
    
    .form-step {
        display: none;
    }
    
    .form-step.active {
        display: block;
    }
</style>
{% endblock %}

{% block content %}
<div class="row justify-content-center">
    <div class="col-12 col-lg-10 col-xl-8">
        <!-- Step Indicator -->
        <div class="step-indicator">
            <div class="step active" data-step="1">
                <div class="step-number">1</div>
                <div class="step-label">Template</div>
            </div>
            <div class="step" data-step="2">
                <div class="step-number">2</div>
                <div class="step-label">Configuration</div>
            </div>
            <div class="step" data-step="3">
                <div class="step-number">3</div>
                <div class="step-label">Review</div>
            </div>
        </div>
        
        <!-- Create App Form -->
        <form id="createAppForm" method="POST" action="{{ url_for('api_apps') }}">
            <!-- Step 1: Template Selection -->
            <div class="form-step active" id="step1">
                <div class="form-section">
                    <h4 class="mb-4"><i class="fas fa-layer-group me-2"></i>Choose a Template</h4>
                    <p class="text-muted mb-4">Select a template to get started quickly, or create a custom application.</p>
                    
                    <div class="row g-4">
                        {% for template_id, template in templates.items() %}
                        <div class="col-md-6 col-lg-4">
                            <div class="template-card" data-template="{{ template_id }}">
                                <div class="template-icon">
                                    {% if template_id == 'telegram-bot' %}
                                        <i class="fab fa-telegram"></i>
                                    {% elif template_id == 'flask-api' %}
                                        <i class="fas fa-server"></i>
                                    {% elif template_id == 'fastapi' %}
                                        <i class="fas fa-bolt"></i>
                                    {% elif template_id == 'discord-bot' %}
                                        <i class="fab fa-discord"></i>
                                    {% elif template_id == 'background-worker' %}
                                        <i class="fas fa-cogs"></i>
                                    {% else %}
                                        <i class="fas fa-code"></i>
                                    {% endif %}
                                </div>
                                <h5 class="mb-2">{{ template.name }}</h5>
                                <p class="text-muted small mb-3">
                                    {{ template.requirements|length }} package{% if template.requirements|length != 1 %}s{% endif %} required
                                </p>
                                <div class="badge bg-primary template-badge">Template</div>
                            </div>
                        </div>
                        {% endfor %}
                        
                        <div class="col-md-6 col-lg-4">
                            <div class="template-card" data-template="custom">
                                <div class="template-icon">
                                    <i class="fas fa-edit"></i>
                                </div>
                                <h5 class="mb-2">Custom Application</h5>
                                <p class="text-muted small mb-3">Write your own code from scratch</p>
                                <div class="badge bg-secondary template-badge">Custom</div>
                            </div>
                        </div>
                    </div>
                    
                    <input type="hidden" id="selectedTemplate" name="template">
                </div>
                
                <div class="d-flex justify-content-between mt-4">
                    <div></div> <!-- Empty div for spacing -->
                    <button type="button" class="btn btn-primary" onclick="nextStep()">
                        Next <i class="fas fa-arrow-right ms-2"></i>
                    </button>
                </div>
            </div>
            
            <!-- Step 2: Configuration -->
            <div class="form-step" id="step2">
                <div class="form-section">
                    <h4 class="mb-4"><i class="fas fa-cog me-2"></i>Application Configuration</h4>
                    
                    <div class="row mb-4">
                        <div class="col-md-6">
                            <label for="appName" class="form-label">Application Name *</label>
                            <input type="text" class="form-control" id="appName" name="name" 
                                   required placeholder="My Awesome App">
                            <small class="text-muted">Choose a descriptive name for your application</small>
                        </div>
                        <div class="col-md-6">
                            <label for="appType" class="form-label">Application Type *</label>
                            <select class="form-select" id="appType" name="type" required>
                                <option value="telegram-bot">Telegram Bot</option>
                                <option value="flask-api">Flask API</option>
                                <option value="fastapi">FastAPI</option>
                                <option value="discord-bot">Discord Bot</option>
                                <option value="background-worker">Background Worker</option>
                                <option value="custom">Custom</option>
                            </select>
                        </div>
                    </div>
                    
                    <div class="mb-4">
                        <label class="form-label">Application Code *</label>
                        <div class="code-editor" id="codeEditor" contenteditable="true"></div>
                        <textarea id="appScript" name="script" style="display: none;"></textarea>
                        <small class="text-muted">Write your Python application code here</small>
                    </div>
                    
                    <div class="mb-4">
                        <div class="d-flex justify-content-between align-items-center mb-3">
                            <label class="form-label mb-0">Requirements (pip packages)</label>
                            <button type="button" class="btn btn-sm btn-outline-primary" onclick="addRequirement()">
                                <i class="fas fa-plus me-1"></i>Add Package
                            </button>
                        </div>
                        <div id="requirementsContainer">
                            <div class="input-group mb-2">
                                <input type="text" class="form-control requirement-input" 
                                       placeholder="Package name and version (e.g., flask==3.0.0)">
                                <button type="button" class="btn btn-outline-danger" onclick="removeRequirement(this)">
                                    <i class="fas fa-times"></i>
                                </button>
                            </div>
                        </div>
                        <small class="text-muted">Add Python packages required by your application</small>
                    </div>
                    
                    <div class="mb-4">
                        <div class="d-flex justify-content-between align-items-center mb-3">
                            <label class="form-label mb-0">Environment Variables</label>
                            <button type="button" class="btn btn-sm btn-outline-primary" onclick="addEnvVar()">
                                <i class="fas fa-plus me-1"></i>Add Variable
                            </button>
                        </div>
                        <div id="envVarsContainer">
                            <div class="env-var-row">
                                <div class="row g-2">
                                    <div class="col-md-5">
                                        <input type="text" class="form-control env-key" placeholder="Key (e.g., API_KEY)">
                                    </div>
                                    <div class="col-md-6">
                                        <input type="text" class="form-control env-value" placeholder="Value">
                                    </div>
                                    <div class="col-md-1">
                                        <button type="button" class="btn btn-outline-danger w-100" 
                                                onclick="removeEnvVar(this)">
                                            <i class="fas fa-times"></i>
                                        </button>
                                    </div>
                                </div>
                            </div>
                        </div>
                        <small class="text-muted">Environment variables will be available to your application</small>
                    </div>
                    
                    <div class="row">
                        <div class="col-md-6">
                            <div class="form-check form-switch mb-3">
                                <input class="form-check-input" type="checkbox" id="autoRestart" name="auto_restart" checked>
                                <label class="form-check-label" for="autoRestart">
                                    Auto-restart on crash
                                </label>
                            </div>
                        </div>
                        <div class="col-md-6">
                            <div class="form-check form-switch mb-3">
                                <input class="form-check-input" type="checkbox" id="autoStart" name="auto_start" checked>
                                <label class="form-check-label" for="autoStart">
                                    Auto-start on server boot
                                </label>
                            </div>
                        </div>
                    </div>
                </div>
                
                <div class="d-flex justify-content-between mt-4">
                    <button type="button" class="btn btn-outline-secondary" onclick="prevStep()">
                        <i class="fas fa-arrow-left me-2"></i>Back
                    </button>
                    <button type="button" class="btn btn-primary" onclick="nextStep()">
                        Next <i class="fas fa-arrow-right ms-2"></i>
                    </button>
                </div>
            </div>
            
            <!-- Step 3: Review -->
            <div class="form-step" id="step3">
                <div class="form-section">
                    <h4 class="mb-4"><i class="fas fa-check-circle me-2"></i>Review & Create</h4>
                    
                    <div class="mb-4">
                        <h5 class="mb-3">Application Summary</h5>
                        <div class="row">
                            <div class="col-md-6">
                                <table class="table table-borderless">
                                    <tr>
                                        <th width="40%">Name:</th>
                                        <td id="reviewName">-</td>
                                    </tr>
                                    <tr>
                                        <th>Type:</th>
                                        <td id="reviewType">-</td>
                                    </tr>
                                    <tr>
                                        <th>Auto-restart:</th>
                                        <td id="reviewAutoRestart">-</td>
                                    </tr>
                                </table>
                            </div>
                            <div class="col-md-6">
                                <table class="table table-borderless">
                                    <tr>
                                        <th width="40%">Requirements:</th>
                                        <td id="reviewRequirements">-</td>
                                    </tr>
                                    <tr>
                                        <th>Env Variables:</th>
                                        <td id="reviewEnvVars">-</td>
                                    </tr>
                                    <tr>
                                        <th>Auto-start:</th>
                                        <td id="reviewAutoStart">-</td>
                                    </tr>
                                </table>
                            </div>
                        </div>
                    </div>
                    
                    <div class="mb-4">
                        <h5 class="mb-3">Application Code Preview</h5>
                        <div class="code-editor" id="reviewCode" style="max-height: 200px; overflow-y: auto;">
                            <!-- Code will be inserted here -->
                        </div>
                    </div>
                    
                    <div class="alert alert-info">
                        <i class="fas fa-info-circle me-2"></i>
                        Review your application configuration before creating. You can go back to make changes.
                    </div>
                </div>
                
                <div class="d-flex justify-content-between mt-4">
                    <button type="button" class="btn btn-outline-secondary" onclick="prevStep()">
                        <i class="fas fa-arrow-left me-2"></i>Back
                    </button>
                    <button type="submit" class="btn btn-success">
                        <i class="fas fa-plus-circle me-2"></i>Create Application
                    </button>
                </div>
            </div>
        </form>
    </div>
</div>
{% endblock %}

{% block extra_js %}
<script>
    let currentStep = 1;
    let selectedTemplate = null;
    
    // Template data from server
    let templates = {};
    
    // Initialize
    $(document).ready(function() {
        // Load templates from server
        fetch('/api/templates')
            .then(response => response.json())
            .then(data => {
                templates = data;
            })
            .catch(error => {
                console.error('Error loading templates:', error);
            });
        
        // Template selection
        $('.template-card').click(function() {
            $('.template-card').removeClass('selected');
            $(this).addClass('selected');
            selectedTemplate = $(this).data('template');
            $('#selectedTemplate').val(selectedTemplate);
            
            // Load template data if available
            if (selectedTemplate !== 'custom' && templates[selectedTemplate]) {
                const template = templates[selectedTemplate];
                
                // Update form fields
                $('#appType').val(selectedTemplate);
                $('#codeEditor').text(template.script);
                
                // Clear and set requirements
                $('#requirementsContainer').empty();
                template.requirements.forEach(req => {
                    addRequirement(req);
                });
                
                // Clear and set env vars
                $('#envVarsContainer').empty();
                Object.entries(template.env_vars).forEach(([key, value]) => {
                    addEnvVar(key, value);
                });
            }
        });
        
        // Form submission
        $('#createAppForm').submit(function(e) {
            e.preventDefault();
            createApplication();
        });
    });
    
    // Navigation functions
    function nextStep() {
        if (currentStep === 1 && !selectedTemplate) {
            showToast('Please select a template', 'warning');
            return;
        }
        
        if (currentStep === 2) {
            // Validate step 2
            if (!$('#appName').val().trim()) {
                showToast('Please enter an application name', 'warning');
                return;
            }
            
            if (!$('#codeEditor').text().trim()) {
                showToast('Please enter application code', 'warning');
                return;
            }
            
            // Update review section
            updateReview();
        }
        
        // Hide current step
        $(`#step${currentStep}`).removeClass('active');
        $(`.step[data-step="${currentStep}"]`).removeClass('active').addClass('completed');
        
        // Show next step
        currentStep++;
        $(`#step${currentStep}`).addClass('active');
        $(`.step[data-step="${currentStep}"]`).addClass('active');
    }
    
    function prevStep() {
        // Hide current step
        $(`#step${currentStep}`).removeClass('active');
        $(`.step[data-step="${currentStep}"]`).removeClass('active');
        
        // Show previous step
        currentStep--;
        $(`#step${currentStep}`).addClass('active');
        $(`.step[data-step="${currentStep}"]`).addClass('active').removeClass('completed');
    }
    
    // Requirement management
    function addRequirement(packageName = '') {
        const container = $('#requirementsContainer');
        const html = `
            <div class="input-group mb-2">
                <input type="text" class="form-control requirement-input" 
                       value="${packageName}" placeholder="Package name and version">
                <button type="button" class="btn btn-outline-danger" onclick="removeRequirement(this)">
                    <i class="fas fa-times"></i>
                </button>
            </div>
        `;
        container.append(html);
    }
    
    function removeRequirement(button) {
        $(button).closest('.input-group').remove();
    }
    
    // Environment variable management
    function addEnvVar(key = '', value = '') {
        const container = $('#envVarsContainer');
        const html = `
            <div class="env-var-row">
                <div class="row g-2">
                    <div class="col-md-5">
                        <input type="text" class="form-control env-key" value="${key}" placeholder="Key">
                    </div>
                    <div class="col-md-6">
                        <input type="text" class="form-control env-value" value="${value}" placeholder="Value">
                    </div>
                    <div class="col-md-1">
                        <button type="button" class="btn btn-outline-danger w-100" onclick="removeEnvVar(this)">
                            <i class="fas fa-times"></i>
                        </button>
                    </div>
                </div>
            </div>
        `;
        container.append(html);
    }
    
    function removeEnvVar(button) {
        $(button).closest('.env-var-row').remove();
    }
    
    // Update review section
    function updateReview() {
        // Basic info
        $('#reviewName').text($('#appName').val());
        $('#reviewType').text($('#appType option:selected').text());
        $('#reviewAutoRestart').text($('#autoRestart').is(':checked') ? 'Yes' : 'No');
        $('#reviewAutoStart').text($('#autoStart').is(':checked') ? 'Yes' : 'No');
        
        // Requirements
        const requirements = [];
        $('.requirement-input').each(function() {
            if ($(this).val().trim()) {
                requirements.push($(this).val().trim());
            }
        });
        $('#reviewRequirements').text(requirements.length > 0 ? requirements.join(', ') : 'None');
        
        // Environment variables
        const envVars = [];
        $('.env-var-row').each(function() {
            const key = $(this).find('.env-key').val().trim();
            const value = $(this).find('.env-value').val().trim();
            if (key) {
                envVars.push(`${key}=${value ? '***' : '(empty)'}`);
            }
        });
        $('#reviewEnvVars').text(envVars.length > 0 ? envVars.join(', ') : 'None');
        
        // Code preview
        $('#reviewCode').text($('#codeEditor').text().trim() || 'No code provided');
    }
    
    // Create application
    function createApplication() {
        // Collect form data
        const formData = {
            name: $('#appName').val(),
            type: $('#appType').val(),
            script: $('#codeEditor').text().trim(),
            requirements: [],
            env_vars: {},
            auto_restart: $('#autoRestart').is(':checked'),
            auto_start: $('#autoStart').is(':checked')
        };
        
        // Collect requirements
        $('.requirement-input').each(function() {
            const req = $(this).val().trim();
            if (req) {
                formData.requirements.push(req);
            }
        });
        
        // Collect environment variables
        $('.env-var-row').each(function() {
            const key = $(this).find('.env-key').val().trim();
            const value = $(this).find('.env-value').val().trim();
            if (key) {
                formData.env_vars[key] = value;
            }
        });
        
        // Show loading
        const submitBtn = $('#createAppForm button[type="submit"]');
        const originalText = submitBtn.html();
        submitBtn.prop('disabled', true).html('<i class="fas fa-spinner fa-spin me-2"></i>Creating...');
        
        // Send API request
        fetch('/api/apps', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(formData)
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showToast('Application created successfully!', 'success');
                setTimeout(() => {
                    window.location.href = '/apps/' + data.app_id;
                }, 1500);
            } else {
                showToast(data.error || 'Failed to create application', 'error');
                submitBtn.prop('disabled', false).html(originalText);
            }
        })
        .catch(error => {
            showToast('Network error: ' + error.message, 'error');
            submitBtn.prop('disabled', false).html(originalText);
        });
    }
</script>
{% endblock %}
'''

# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/')
def index():
    """Redirect to dashboard"""
    return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if (username == CONFIG['ADMIN_USERNAME'] and 
            hashlib.sha256(password.encode()).hexdigest() == ADMIN_PASSWORD_HASH):
            session['logged_in'] = True
            session['username'] = username
            return redirect(url_for('dashboard'))
        
        return render_template_string(LOGIN_HTML, error='Invalid credentials')
    
    return render_template_string(LOGIN_HTML)

@app.route('/logout')
def logout():
    """Logout user"""
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    """Dashboard page"""
    stats = db.get_platform_stats()
    apps = db.get_all_applications()
    
    running_apps = sum(1 for app in apps if app.get('status') == 'running')
    stopped_apps = sum(1 for app in apps if app.get('status') == 'stopped')
    crashed_apps = sum(1 for app in apps if app.get('status') == 'crashed')
    
    return render_template_string(
        DASHBOARD_HTML,
        stats=stats,
        total_apps=len(apps),
        running_apps=running_apps,
        stopped_apps=stopped_apps,
        crashed_apps=crashed_apps,
        apps=apps[:5]
    )

@app.route('/apps')
@login_required
def apps_page():
    """Applications page"""
    apps = db.get_all_applications()
    return render_template_string(APPS_HTML, apps=apps)

@app.route('/create-app')
@login_required
def create_app_page():
    """Create application page"""
    return render_template_string(CREATE_APP_HTML, templates=TEMPLATES)

@app.route('/apps/<app_id>')
@login_required
def app_detail(app_id):
    """Application detail page"""
    app_data = db.get_application(app_id)
    if not app_data:
        return "Application not found", 404
    
    # Render app detail HTML (simplified for brevity)
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>{app_data['name']} - App Hosting Platform</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    </head>
    <body>
        <nav class="navbar navbar-dark bg-dark">
            <div class="container-fluid">
                <a class="navbar-brand" href="/"><i class="fas fa-cloud"></i> App Hosting Platform</a>
                <a href="/dashboard" class="btn btn-outline-light">Back to Dashboard</a>
            </div>
        </nav>
        
        <div class="container mt-4">
            <h1><i class="fas fa-box"></i> {app_data['name']}</h1>
            <p class="text-muted">Application ID: {app_id}</p>
            
            <div class="row mt-4">
                <div class="col-md-8">
                    <div class="card">
                        <div class="card-header">
                            <h5 class="mb-0">Application Details</h5>
                        </div>
                        <div class="card-body">
                            <table class="table">
                                <tr>
                                    <th>Status:</th>
                                    <td>
                                        {% if app_data.status == 'running' %}
                                            <span class="badge bg-success">Running</span>
                                        {% elif app_data.status == 'stopped' %}
                                            <span class="badge bg-secondary">Stopped</span>
                                        {% else %}
                                            <span class="badge bg-danger">Crashed</span>
                                        {% endif %}
                                    </td>
                                </tr>
                                <tr>
                                    <th>Type:</th>
                                    <td>{app_data.get('type', 'custom')}</td>
                                </tr>
                                <tr>
                                    <th>Created:</th>
                                    <td>{app_data.get('created_at', 'N/A')}</td>
                                </tr>
                                <tr>
                                    <th>Uptime:</th>
                                    <td>{app_data.get('uptime_seconds', 0)} seconds</td>
                                </tr>
                            </table>
                            
                            <div class="btn-group mt-3">
                                <a href="/logs/{app_id}" class="btn btn-info">
                                    <i class="fas fa-file-alt"></i> View Logs
                                </a>
                                <a href="/terminal/{app_id}" class="btn btn-secondary">
                                    <i class="fas fa-terminal"></i> Terminal
                                </a>
                                <button onclick="startApp('{app_id}')" class="btn btn-success">
                                    <i class="fas fa-play"></i> Start
                                </button>
                                <button onclick="stopApp('{app_id}')" class="btn btn-danger">
                                    <i class="fas fa-stop"></i> Stop
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <script>
            function startApp(appId) {{
                fetch(`/api/apps/${{appId}}/start`, {{method: 'POST'}})
                    .then(response => response.json())
                    .then(data => {{
                        alert(data.message);
                        location.reload();
                    }});
            }}
            
            function stopApp(appId) {{
                fetch(`/api/apps/${{appId}}/stop`, {{method: 'POST'}})
                    .then(response => response.json())
                    .then(data => {{
                        alert(data.message);
                        location.reload();
                    }});
            }}
        </script>
    </body>
    </html>
    '''
    
    return render_template_string(html, app_data=app_data)

@app.route('/logs/<app_id>')
@login_required
def logs_page(app_id):
    """Logs viewer page"""
    app_data = db.get_application(app_id)
    if not app_data:
        return "Application not found", 404
    
    # Simplified logs page
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Logs - {app_data['name']}</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>
            body {{ background: #f8f9fa; }}
            .log-container {{ background: #1a1a1a; color: #fff; font-family: monospace; padding: 20px; border-radius: 8px; height: 70vh; overflow-y: auto; }}
            .log-entry {{ margin-bottom: 5px; }}
            .log-info {{ color: #4ade80; }}
            .log-error {{ color: #f87171; }}
            .log-warning {{ color: #fbbf24; }}
        </style>
    </head>
    <body>
        <nav class="navbar navbar-dark bg-dark">
            <div class="container-fluid">
                <a class="navbar-brand" href="/"><i class="fas fa-cloud"></i> App Hosting Platform</a>
                <a href="/apps/{app_id}" class="btn btn-outline-light">Back to App</a>
            </div>
        </nav>
        
        <div class="container mt-4">
            <h2><i class="fas fa-file-alt"></i> Logs: {app_data['name']}</h2>
            <div class="log-container" id="logContainer">
                <p>Loading logs...</p>
            </div>
            
            <div class="mt-3">
                <button class="btn btn-primary" onclick="startLogStream()">
                    <i class="fas fa-play"></i> Start Live Logs
                </button>
                <button class="btn btn-secondary" onclick="loadLogs()">
                    <i class="fas fa-sync"></i> Refresh
                </button>
                <button class="btn btn-danger" onclick="clearLogs()">
                    <i class="fas fa-trash"></i> Clear Logs
                </button>
            </div>
        </div>
        
        <script>
            let eventSource = null;
            
            function loadLogs() {{
                fetch(`/api/apps/{app_id}/logs`)
                    .then(response => response.json())
                    .then(data => {{
                        const container = document.getElementById('logContainer');
                        container.innerHTML = '';
                        data.logs.forEach(log => {{
                            const div = document.createElement('div');
                            div.className = 'log-entry log-' + log.level.toLowerCase();
                            div.textContent = `[${{log.timestamp}}] ${{log.level}}: ${{log.message}}`;
                            container.appendChild(div);
                        }});
                        container.scrollTop = container.scrollHeight;
                    }});
            }}
            
            function startLogStream() {{
                if (eventSource) eventSource.close();
                
                eventSource = new EventSource(`/api/apps/{app_id}/logs/stream`);
                const container = document.getElementById('logContainer');
                
                eventSource.onmessage = function(event) {{
                    try {{
                        const log = JSON.parse(event.data);
                        const div = document.createElement('div');
                        div.className = 'log-entry log-' + log.level.toLowerCase();
                        div.textContent = `[${{log.timestamp}}] ${{log.level}}: ${{log.message}}`;
                        container.appendChild(div);
                        container.scrollTop = container.scrollHeight;
                    }} catch (e) {{ /* Keep-alive message */ }}
                }};
                
                eventSource.onerror = function() {{
                    eventSource.close();
                }};
            }}
            
            function clearLogs() {{
                if (!confirm('Clear all logs for this application?')) return;
                
                fetch(`/api/apps/{app_id}/logs`, {{ method: 'DELETE' }})
                    .then(response => response.json())
                    .then(data => {{
                        alert(data.message);
                        loadLogs();
                    }});
            }}
            
            // Load initial logs
            loadLogs();
        </script>
    </body>
    </html>
    '''
    
    return render_template_string(html)

@app.route('/terminal/<app_id>')
@login_required
def terminal_page(app_id):
    """Terminal page"""
    app_data = db.get_application(app_id)
    if not app_data:
        return "Application not found", 404
    
    # Simplified terminal page
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Terminal - {app_data['name']}</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>
            body {{ background: #1a1a1a; color: #fff; }}
            .terminal {{ font-family: 'Courier New', monospace; padding: 20px; height: 80vh; overflow-y: auto; }}
            .prompt {{ color: #4ade80; }}
            .command {{ color: #fff; }}
            .output {{ color: #a1a1aa; }}
        </style>
    </head>
    <body>
        <nav class="navbar navbar-dark bg-dark">
            <div class="container-fluid">
                <a class="navbar-brand" href="/"><i class="fas fa-cloud"></i> App Hosting Platform</a>
                <a href="/apps/{app_id}" class="btn btn-outline-light">Back to App</a>
            </div>
        </nav>
        
        <div class="container-fluid mt-3">
            <h4 class="text-light"><i class="fas fa-terminal"></i> Terminal: {app_data['name']}</h4>
            <div class="terminal" id="terminal">
                <div class="mb-2"><span class="prompt">$</span> <span class="command">Welcome to application terminal</span></div>
                <div class="output">Type commands below. Available: pip install, ls, pwd, cat, etc.</div>
            </div>
            
            <div class="input-group mt-2">
                <span class="input-group-text bg-dark text-light border-dark">$</span>
                <input type="text" class="form-control bg-dark text-light border-dark" 
                       id="commandInput" placeholder="Enter command..." onkeypress="handleKeyPress(event)">
                <button class="btn btn-primary" onclick="executeCommand()">
                    <i class="fas fa-play"></i> Execute
                </button>
            </div>
            
            <div class="mt-2">
                <small class="text-muted">Note: Dangerous commands (rm -rf, sudo, etc.) are blocked for security.</small>
            </div>
        </div>
        
        <script>
            function handleKeyPress(event) {{
                if (event.key === 'Enter') {{
                    executeCommand();
                }}
            }}
            
            function executeCommand() {{
                const input = document.getElementById('commandInput');
                const command = input.value.trim();
                if (!command) return;
                
                const terminal = document.getElementById('terminal');
                
                // Add command to terminal
                const commandDiv = document.createElement('div');
                commandDiv.className = 'mb-2';
                commandDiv.innerHTML = `<span class="prompt">$</span> <span class="command">${{command}}</span>`;
                terminal.appendChild(commandDiv);
                
                // Clear input
                input.value = '';
                
                // Send command to server
                fetch(`/api/apps/{app_id}/terminal/execute`, {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ command: command }})
                }})
                .then(response => response.json())
                .then(data => {{
                    const outputDiv = document.createElement('div');
                    outputDiv.className = 'output mb-2';
                    
                    if (data.error) {{
                        outputDiv.innerHTML = `<span style="color: #f87171;">Error: ${{data.error}}</span>`;
                    }} else {{
                        if (data.stdout) {{
                            outputDiv.textContent = data.stdout;
                        }}
                        if (data.stderr) {{
                            const errorDiv = document.createElement('div');
                            errorDiv.style.color = '#f87171';
                            errorDiv.textContent = data.stderr;
                            terminal.appendChild(errorDiv);
                        }}
                    }}
                    
                    terminal.appendChild(outputDiv);
                    terminal.scrollTop = terminal.scrollHeight;
                }})
                .catch(error => {{
                    const errorDiv = document.createElement('div');
                    errorDiv.className = 'output mb-2';
                    errorDiv.style.color = '#f87171';
                    errorDiv.textContent = 'Network error: ' + error.message;
                    terminal.appendChild(errorDiv);
                    terminal.scrollTop = terminal.scrollHeight;
                }});
            }}
        </script>
    </body>
    </html>
    '''
    
    return render_template_string(html)

# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/api/stats')
@login_required
def api_stats():
    """Get platform statistics"""
    stats = db.get_platform_stats()
    return jsonify(stats)

@app.route('/api/apps', methods=['GET', 'POST'])
@login_required
def api_apps():
    """List all apps or create new app"""
    if request.method == 'GET':
        apps = db.get_all_applications()
        return jsonify(apps)
    
    elif request.method == 'POST':
        try:
            data = request.json
            
            # Validate required fields
            required_fields = ['name', 'type', 'script']
            for field in required_fields:
                if field not in data:
                    return jsonify({'error': f'Missing field: {field}'}), 400
            
            # Create application
            app_id = db.create_application(
                name=data['name'],
                type=data['type'],
                script=data['script'],
                requirements=data.get('requirements', []),
                env_vars=data.get('env_vars', {}),
                auto_restart=data.get('auto_restart', True),
                auto_start=data.get('auto_start', True)
            )
            
            return jsonify({
                'success': True,
                'app_id': app_id,
                'message': 'Application created successfully'
            })
            
        except Exception as e:
            return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>', methods=['GET', 'PUT', 'DELETE'])
@login_required
def api_app(app_id):
    """Get, update, or delete specific app"""
    if request.method == 'GET':
        app_data = db.get_application(app_id)
        if not app_data:
            return jsonify({'error': 'Application not found'}), 404
        return jsonify(app_data)
    
    elif request.method == 'PUT':
        try:
            data = request.json
            success = db.update_application(app_id, data)
            if success:
                return jsonify({'success': True, 'message': 'Application updated'})
            else:
                return jsonify({'error': 'Update failed'}), 500
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    elif request.method == 'DELETE':
        # Stop app first if running
        app_data = db.get_application(app_id)
        if app_data and app_data.get('status') == 'running':
            pm.stop_application(app_id)
        
        success = db.delete_application(app_id)
        if success:
            return jsonify({'success': True, 'message': 'Application deleted'})
        else:
            return jsonify({'error': 'Delete failed'}), 500

@app.route('/api/apps/<app_id>/start', methods=['POST'])
@login_required
def api_start_app(app_id):
    """Start an application"""
    try:
        success = pm.start_application(app_id)
        if success:
            return jsonify({'success': True, 'message': 'Application started'})
        else:
            return jsonify({'error': 'Failed to start application'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/stop', methods=['POST'])
@login_required
def api_stop_app(app_id):
    """Stop an application"""
    try:
        success = pm.stop_application(app_id)
        if success:
            return jsonify({'success': True, 'message': 'Application stopped'})
        else:
            return jsonify({'error': 'Failed to stop application'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/restart', methods=['POST'])
@login_required
def api_restart_app(app_id):
    """Restart an application"""
    try:
        success = pm.restart_application(app_id)
        if success:
            return jsonify({'success': True, 'message': 'Application restarted'})
        else:
            return jsonify({'error': 'Failed to restart application'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/logs')
@login_required
def api_get_logs(app_id):
    """Get application logs"""
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    level = request.args.get('level')
    
    logs = db.get_logs(app_id, limit=limit, offset=offset, level=level)
    total = db.get_log_count(app_id)
    
    return jsonify({
        'logs': logs,
        'total': total,
        'limit': limit,
        'offset': offset
    })

@app.route('/api/apps/<app_id>/logs', methods=['DELETE'])
@login_required
def api_clear_logs(app_id):
    """Clear application logs"""
    success = db.clear_logs(app_id)
    if success:
        return jsonify({'success': True, 'message': 'Logs cleared'})
    else:
        return jsonify({'error': 'Failed to clear logs'}), 500

@app.route('/api/apps/<app_id>/logs/stream')
@login_required
def api_log_stream(app_id):
    """SSE stream for live logs"""
    def generate():
        # Check if app exists
        app_data = db.get_application(app_id)
        if not app_data:
            yield f"data: {json.dumps({'error': 'App not found'})}\n\n"
            return
        
        # Subscribe to log updates
        log_queue = pm.get_log_queue(app_id)
        if not log_queue:
            yield f"data: {json.dumps({'error': 'No log queue available'})}\n\n"
            return
        
        try:
            while True:
                # Check if client disconnected
                if request.environ.get('wsgi.input').closed:
                    break
                
                # Get new logs from queue
                try:
                    log_entry = log_queue.get(timeout=1)
                    if log_entry:
                        yield f"data: {json.dumps(log_entry)}\n\n"
                except queue.Empty:
                    # Send keep-alive comment
                    yield ": keepalive\n\n"
                    
        except GeneratorExit:
            # Client disconnected
            pass
    
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )

@app.route('/api/apps/<app_id>/terminal/execute', methods=['POST'])
@login_required
def api_execute_terminal(app_id):
    """Execute terminal command"""
    try:
        data = request.json
        command = data.get('command')
        
        if not command:
            return jsonify({'error': 'No command provided'}), 400
        
        # Execute command
        result = pm.execute_command(app_id, command)
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/env', methods=['GET', 'POST'])
@login_required
def api_env_vars(app_id):
    """Get or update environment variables"""
    if request.method == 'GET':
        app_data = db.get_application(app_id)
        if not app_data:
            return jsonify({'error': 'Application not found'}), 404
        
        # Hide sensitive values
        env_vars = app_data.get('env_vars', {})
        masked_vars = {}
        for key, value in env_vars.items():
            if any(sensitive in key.lower() for sensitive in ['token', 'key', 'secret', 'password']):
                masked_vars[key] = '***HIDDEN***'
            else:
                masked_vars[key] = value
        
        return jsonify(masked_vars)
    
    elif request.method == 'POST':
        try:
            data = request.json
            success = db.update_application(app_id, {'env_vars': data})
            if success:
                return jsonify({'success': True, 'message': 'Environment variables updated'})
            else:
                return jsonify({'error': 'Update failed'}), 500
        except Exception as e:
            return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/status')
@login_required
def api_app_status(app_id):
    """Get application status"""
    app_data = db.get_application(app_id)
    if not app_data:
        return jsonify({'error': 'Application not found'}), 404
    
    # Get process info if running
    process_info = {}
    if app_data.get('status') == 'running' and 'pid' in app_data:
        try:
            process = psutil.Process(app_data['pid'])
            process_info = {
                'cpu_percent': process.cpu_percent(),
                'memory_mb': process.memory_info().rss / 1024 / 1024,
                'create_time': datetime.fromtimestamp(process.create_time()).isoformat(),
                'status': process.status()
            }
        except:
            process_info = {'error': 'Process not found'}
    
    return jsonify({
        'app_id': app_id,
        'status': app_data.get('status', 'stopped'),
        'pid': app_data.get('pid'),
        'uptime_seconds': app_data.get('uptime_seconds', 0),
        'restart_count': app_data.get('restart_count', 0),
        'process_info': process_info
    })

@app.route('/api/templates')
@login_required
def api_get_templates():
    """Get all application templates"""
    return jsonify(TEMPLATES)

@app.route('/api/templates/<template_type>')
@login_required
def api_get_template(template_type):
    """Get specific template"""
    if template_type in TEMPLATES:
        return jsonify(TEMPLATES[template_type])
    else:
        return jsonify({'error': 'Template not found'}), 404

@app.route('/api/system/health')
def api_system_health():
    """System health check"""
    try:
        # Check database connection
        db_ok = db.check_connection()
        
        # Check process manager
        pm_ok = pm.is_alive()
        
        # Get system info
        system_info = {
            'platform': sys.platform,
            'python_version': sys.version,
            'cpu_count': psutil.cpu_count(),
            'memory_total_gb': psutil.virtual_memory().total / 1024 / 1024 / 1024,
            'memory_available_gb': psutil.virtual_memory().available / 1024 / 1024 / 1024
        }
        
        return jsonify({
            'status': 'healthy' if db_ok and pm_ok else 'degraded',
            'timestamp': datetime.now().isoformat(),
            'database': 'connected' if db_ok else 'disconnected',
            'process_manager': 'running' if pm_ok else 'stopped',
            'system': system_info
        })
        
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 500

# ============================================================================
# STARTUP AND SHUTDOWN
# ============================================================================

def startup():
    """Initialize platform on startup"""
    print("=" * 60)
    print("🚀 STARTING APPLICATION HOSTING PLATFORM")
    print("=" * 60)
    
    # Connect to database
    if not db.connect():
        print("❌ Failed to connect to database. Exiting...")
        sys.exit(1)
    
    # Initialize process manager
    pm.start_monitoring()
    
    # Start auto-start applications
    auto_start_apps = db.get_auto_start_applications()
    print(f"📦 Found {len(auto_start_apps)} applications to auto-start")
    
    for app in auto_start_apps:
        app_id = app['app_id']
        app_name = app.get('name', app_id)
        print(f"  → Starting: {app_name}")
        try:
            pm.start_application(app_id)
        except Exception as e:
            print(f"  ❌ Failed to start {app_name}: {e}")
    
    print("✅ Platform started successfully!")
    print(f"🌐 Dashboard: http://localhost:{CONFIG['PORT']}")
    print("=" * 60)

def shutdown():
    """Graceful shutdown"""
    print("\n" + "=" * 60)
    print("🛑 SHUTTING DOWN PLATFORM")
    print("=" * 60)
    
    # Stop all running applications
    running_apps = db.get_running_applications()
    print(f"📦 Stopping {len(running_apps)} running applications")
    
    for app in running_apps:
        app_id = app['app_id']
        app_name = app.get('name', app_id)
        print(f"  → Stopping: {app_name}")
        try:
            pm.stop_application(app_id)
        except Exception as e:
            print(f"  ⚠️ Error stopping {app_name}: {e}")
    
    # Stop monitoring
    pm.stop_monitoring()
    
    # Clean up workspaces
    pm.cleanup_workspaces()
    
    # Close database connection
    db.close()
    
    print("✅ Platform shutdown complete")
    print("=" * 60)

# Register shutdown handler
atexit.register(shutdown)

# Signal handlers for graceful shutdown
def signal_handler(signum, frame):
    print(f"\n📶 Received signal {signum}, shutting down...")
    shutdown()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

if __name__ == '__main__':
    # Run startup
    startup()
    
    # Start Flask app
    if CONFIG['DEBUG']:
        app.run(host='0.0.0.0', port=CONFIG['PORT'], debug=True)
    else:
        # Use production server
        try:
            from gunicorn.app.base import BaseApplication
            
            class FlaskApplication(BaseApplication):
                def __init__(self, app, options=None):
                    self.options = options or {}
                    self.application = app
                    super().__init__()
                
                def load_config(self):
                    for key, value in self.options.items():
                        if key in self.cfg.settings and value is not None:
                            self.cfg.set(key, value)
                
                def load(self):
                    return self.application
            
            options = {
                'bind': f'0.0.0.0:{CONFIG["PORT"]}',
                'workers': 4,
                'worker_class': 'eventlet',
                'timeout': 120,
                'accesslog': '-',
                'errorlog': '-'
            }
            
            FlaskApplication(app, options).run()
            
        except ImportError:
            print("⚠️ Gunicorn not installed, using development server")
            print("⚠️ For production, install: pip install gunicorn eventlet")
            app.run(host='0.0.0.0', port=CONFIG['PORT'])
