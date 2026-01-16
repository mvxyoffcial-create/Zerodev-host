#!/usr/bin/env python3
"""
Professional Application Hosting Platform (Koyeb Clone)
Deploy and manage Python applications 24/7
Deploy on Render with MongoDB Atlas
"""

import os
import sys
import json
import time
import signal
import psutil
import subprocess
import threading
import queue
from datetime import datetime, timedelta
from functools import wraps
from collections import defaultdict

from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for, Response
from flask_cors import CORS
from pymongo import MongoClient
from bson import ObjectId
import secrets

# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION & CONSTANTS
# ═══════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))
CORS(app)

# MongoDB Configuration
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017/')
DATABASE_NAME = 'hosting_platform'

# Admin Credentials
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')

# Platform Configuration
MAX_LOGS_PER_APP = 10000
LOG_CLEANUP_INTERVAL = 3600  # 1 hour
HEALTH_CHECK_INTERVAL = 30  # 30 seconds
MAX_RESTART_ATTEMPTS = 5
COMMAND_TIMEOUT = 60

# ═══════════════════════════════════════════════════════════════════
# MONGODB SETUP
# ═══════════════════════════════════════════════════════════════════

try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.server_info()  # Test connection
    db = mongo_client[DATABASE_NAME]
    apps_collection = db.applications
    logs_collection = db.logs
    storage_collection = db.storage
    
    # Create indexes
    logs_collection.create_index([('app_id', 1), ('timestamp', -1)])
    apps_collection.create_index('app_id', unique=True)
    
    print("✅ MongoDB connected successfully")
except Exception as e:
    print(f"❌ MongoDB connection failed: {e}")
    print("⚠️  Platform will start but database operations will fail")
    db = None

# ═══════════════════════════════════════════════════════════════════
# GLOBAL VARIABLES
# ═══════════════════════════════════════════════════════════════════

running_processes = {}  # {app_id: subprocess.Popen}
log_queues = {}  # {app_id: queue.Queue}
log_threads = {}  # {app_id: threading.Thread}
app_start_times = {}  # {app_id: datetime}
shutdown_flag = threading.Event()

# ═══════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def generate_app_id():
    return secrets.token_hex(8)

def save_log(app_id, level, message):
    """Save log to MongoDB and queue"""
    timestamp = datetime.utcnow()
    log_entry = {
        'app_id': app_id,
        'timestamp': timestamp,
        'level': level,
        'message': str(message)
    }
    
    try:
        if db:
            logs_collection.insert_one(log_entry)
            # Limit logs per app
            count = logs_collection.count_documents({'app_id': app_id})
            if count > MAX_LOGS_PER_APP:
                oldest_logs = logs_collection.find({'app_id': app_id}).sort('timestamp', 1).limit(count - MAX_LOGS_PER_APP)
                for log in oldest_logs:
                    logs_collection.delete_one({'_id': log['_id']})
    except Exception as e:
        print(f"Error saving log: {e}")
    
    # Add to queue for real-time streaming
    if app_id in log_queues:
        try:
            log_queues[app_id].put({
                'timestamp': timestamp.isoformat(),
                'level': level,
                'message': message
            }, block=False)
        except queue.Full:
            pass

def get_app_uptime(app_id):
    """Calculate application uptime"""
    if app_id in app_start_times:
        delta = datetime.utcnow() - app_start_times[app_id]
        return int(delta.total_seconds())
    return 0

def is_process_running(pid):
    """Check if process is running"""
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

# ═══════════════════════════════════════════════════════════════════
# PROCESS MANAGEMENT
# ═══════════════════════════════════════════════════════════════════

class LogCaptureThread(threading.Thread):
    """Thread to capture and stream application logs"""
    def __init__(self, app_id, process):
        super().__init__(daemon=True)
        self.app_id = app_id
        self.process = process
        self.running = True
    
    def run(self):
        """Capture stdout and stderr"""
        while self.running and self.process.poll() is None:
            try:
                # Read stdout
                if self.process.stdout:
                    line = self.process.stdout.readline()
                    if line:
                        save_log(self.app_id, 'INFO', line.strip())
                
                # Read stderr
                if self.process.stderr:
                    line = self.process.stderr.readline()
                    if line:
                        save_log(self.app_id, 'ERROR', line.strip())
                
                time.sleep(0.01)
            except Exception as e:
                save_log(self.app_id, 'ERROR', f"Log capture error: {e}")
                break
        
        # Capture any remaining output
        if self.process.stdout:
            remaining = self.process.stdout.read()
            if remaining:
                save_log(self.app_id, 'INFO', remaining.strip())
    
    def stop(self):
        self.running = False

def install_requirements(app_id, requirements):
    """Install pip requirements for application"""
    if not requirements:
        return True
    
    save_log(app_id, 'INFO', '📦 Installing requirements...')
    
    for req in requirements:
        if not req.strip():
            continue
        
        try:
            save_log(app_id, 'INFO', f'Installing {req}...')
            result = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', req],
                capture_output=True,
                text=True,
                timeout=300
            )
            
            if result.returncode == 0:
                save_log(app_id, 'INFO', f'✅ {req} installed successfully')
            else:
                save_log(app_id, 'ERROR', f'❌ Failed to install {req}: {result.stderr}')
                return False
        except Exception as e:
            save_log(app_id, 'ERROR', f'❌ Error installing {req}: {e}')
            return False
    
    return True

def start_application(app_id):
    """Start an application subprocess"""
    try:
        # Get app from database
        app_doc = apps_collection.find_one({'app_id': app_id})
        if not app_doc:
            return False, "Application not found"
        
        # Check if already running
        if app_id in running_processes and running_processes[app_id].poll() is None:
            return False, "Application already running"
        
        save_log(app_id, 'INFO', f'🚀 Starting {app_doc["app_name"]}...')
        
        # Install requirements
        if app_doc.get('requirements'):
            if not install_requirements(app_id, app_doc['requirements']):
                save_log(app_id, 'ERROR', '❌ Failed to install requirements')
                apps_collection.update_one(
                    {'app_id': app_id},
                    {'$set': {'status': 'stopped'}}
                )
                return False, "Requirements installation failed"
        
        # Create temporary script file
        script_path = f'/tmp/app_{app_id}.py'
        with open(script_path, 'w') as f:
            f.write(app_doc['script'])
        
        # Prepare environment variables
        env = os.environ.copy()
        if app_doc.get('env_vars'):
            env.update(app_doc['env_vars'])
        
        # Start process
        process = subprocess.Popen(
            [sys.executable, script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env
        )
        
        # Store process and start log capture
        running_processes[app_id] = process
        app_start_times[app_id] = datetime.utcnow()
        
        # Create log queue if not exists
        if app_id not in log_queues:
            log_queues[app_id] = queue.Queue(maxsize=1000)
        
        # Start log capture thread
        log_thread = LogCaptureThread(app_id, process)
        log_thread.start()
        log_threads[app_id] = log_thread
        
        # Update database
        apps_collection.update_one(
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
        
        save_log(app_id, 'INFO', f'✅ Application started successfully (PID: {process.pid})')
        return True, "Application started successfully"
        
    except Exception as e:
        save_log(app_id, 'ERROR', f'❌ Failed to start application: {e}')
        apps_collection.update_one(
            {'app_id': app_id},
            {'$set': {'status': 'crashed'}}
        )
        return False, str(e)

def stop_application(app_id, force=False):
    """Stop an application subprocess"""
    try:
        if app_id not in running_processes:
            return False, "Application not running"
        
        process = running_processes[app_id]
        
        if process.poll() is not None:
            # Process already stopped
            del running_processes[app_id]
            if app_id in app_start_times:
                del app_start_times[app_id]
            return True, "Application already stopped"
        
        save_log(app_id, 'INFO', '🛑 Stopping application...')
        
        # Stop log capture thread
        if app_id in log_threads:
            log_threads[app_id].stop()
        
        # Try graceful shutdown first
        if not force:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                save_log(app_id, 'WARNING', '⚠️ Graceful shutdown timeout, forcing kill...')
                process.kill()
                process.wait()
        else:
            process.kill()
            process.wait()
        
        # Cleanup
        del running_processes[app_id]
        if app_id in app_start_times:
            del app_start_times[app_id]
        if app_id in log_threads:
            del log_threads[app_id]
        
        # Update database
        apps_collection.update_one(
            {'app_id': app_id},
            {
                '$set': {
                    'status': 'stopped',
                    'pid': None,
                    'updated_at': datetime.utcnow()
                }
            }
        )
        
        save_log(app_id, 'INFO', '✅ Application stopped successfully')
        return True, "Application stopped successfully"
        
    except Exception as e:
        save_log(app_id, 'ERROR', f'❌ Error stopping application: {e}')
        return False, str(e)

def restart_application(app_id):
    """Restart an application"""
    save_log(app_id, 'INFO', '🔄 Restarting application...')
    
    # Increment restart count
    apps_collection.update_one(
        {'app_id': app_id},
        {'$inc': {'restart_count': 1}}
    )
    
    success, message = stop_application(app_id)
    if not success and "not running" not in message.lower():
        return False, f"Failed to stop: {message}"
    
    time.sleep(2)  # Brief pause
    
    return start_application(app_id)

# ═══════════════════════════════════════════════════════════════════
# MONITORING THREAD
# ═══════════════════════════════════════════════════════════════════

def monitoring_loop():
    """Background thread to monitor applications"""
    print("🔍 Monitoring thread started")
    
    while not shutdown_flag.is_set():
        try:
            if not db:
                time.sleep(HEALTH_CHECK_INTERVAL)
                continue
            
            # Check all applications
            apps = apps_collection.find({'status': 'running'})
            
            for app_doc in apps:
                app_id = app_doc['app_id']
                
                # Check if process is still running
                if app_id in running_processes:
                    process = running_processes[app_id]
                    
                    if process.poll() is not None:
                        # Process died
                        save_log(app_id, 'ERROR', '❌ Application crashed!')
                        
                        # Check auto-restart
                        if app_doc.get('auto_restart', True):
                            restart_count = app_doc.get('restart_count', 0)
                            
                            if restart_count < MAX_RESTART_ATTEMPTS:
                                save_log(app_id, 'INFO', f'🔄 Auto-restarting (attempt {restart_count + 1}/{MAX_RESTART_ATTEMPTS})...')
                                restart_application(app_id)
                            else:
                                save_log(app_id, 'ERROR', f'❌ Max restart attempts ({MAX_RESTART_ATTEMPTS}) reached')
                                apps_collection.update_one(
                                    {'app_id': app_id},
                                    {'$set': {'status': 'crashed', 'auto_restart': False}}
                                )
                        else:
                            apps_collection.update_one(
                                {'app_id': app_id},
                                {'$set': {'status': 'crashed'}}
                            )
                else:
                    # Process not in running_processes but marked as running
                    apps_collection.update_one(
                        {'app_id': app_id},
                        {'$set': {'status': 'stopped', 'pid': None}}
                    )
            
            time.sleep(HEALTH_CHECK_INTERVAL)
            
        except Exception as e:
            print(f"Monitoring error: {e}")
            time.sleep(HEALTH_CHECK_INTERVAL)

# ═══════════════════════════════════════════════════════════════════
# HTML TEMPLATES
# ═══════════════════════════════════════════════════════════════════

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - Hosting Platform</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
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
            text-align: center;
            color: #667eea;
            margin-bottom: 30px;
            font-size: 28px;
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
            border: 2px solid #e2e8f0;
            border-radius: 8px;
            font-size: 14px;
            transition: border 0.3s;
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
            color: #c33;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 20px;
            text-align: center;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <h1>🚀 Hosting Platform</h1>
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
"""

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard - Hosting Platform</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f3f4f6;
            color: #1f2937;
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
        .navbar h1 { font-size: 24px; }
        .navbar a {
            color: white;
            text-decoration: none;
            padding: 8px 16px;
            background: rgba(255,255,255,0.2);
            border-radius: 8px;
            transition: background 0.3s;
        }
        .navbar a:hover { background: rgba(255,255,255,0.3); }
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 24px;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 32px;
        }
        .stat-card {
            background: white;
            padding: 24px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            border-left: 4px solid #667eea;
        }
        .stat-value {
            font-size: 36px;
            font-weight: bold;
            color: #667eea;
            margin: 8px 0;
        }
        .stat-label {
            color: #6b7280;
            font-size: 14px;
        }
        .section {
            background: white;
            padding: 24px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            margin-bottom: 24px;
        }
        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .section-header h2 {
            font-size: 20px;
        }
        .btn {
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-weight: 600;
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
        .btn-success { background: #10b981; color: white; }
        .btn-danger { background: #ef4444; color: white; }
        .btn-warning { background: #f59e0b; color: white; }
        .btn-sm { padding: 6px 12px; font-size: 13px; }
        table {
            width: 100%;
            border-collapse: collapse;
        }
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #e5e7eb;
        }
        th {
            background: #f9fafb;
            font-weight: 600;
            color: #374151;
        }
        .status {
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
        }
        .status-running { background: #d1fae5; color: #065f46; }
        .status-stopped { background: #fee; color: #991b1b; }
        .status-crashed { background: #fef3c7; color: #92400e; }
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
        .modal.active { display: flex; }
        .modal-content {
            background: white;
            padding: 32px;
            border-radius: 16px;
            max-width: 800px;
            width: 90%;
            max-height: 90vh;
            overflow-y: auto;
        }
        .form-group {
            margin-bottom: 20px;
        }
        .form-group label {
            display: block;
            margin-bottom: 8px;
            font-weight: 600;
            color: #374151;
        }
        .form-group input,
        .form-group select,
        .form-group textarea {
            width: 100%;
            padding: 10px;
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
            padding: 16px 24px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            z-index: 2000;
            display: none;
        }
        .toast.success { border-left: 4px solid #10b981; }
        .toast.error { border-left: 4px solid #ef4444; }
        .logs-container {
            background: #1f2937;
            color: #f3f4f6;
            padding: 16px;
            border-radius: 8px;
            font-family: 'Courier New', monospace;
            font-size: 13px;
            max-height: 500px;
            overflow-y: auto;
        }
        .log-line {
            margin-bottom: 4px;
            word-wrap: break-word;
        }
        .log-info { color: #60a5fa; }
        .log-error { color: #f87171; }
        .log-warning { color: #fbbf24; }
    </style>
</head>
<body>
    <div class="navbar">
        <h1>🚀 Hosting Platform</h1>
        <a href="/logout">Logout</a>
    </div>
    
    <div class="container">
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Total Applications</div>
                <div class="stat-value" id="total-apps">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Running</div>
                <div class="stat-value" id="running-apps" style="color: #10b981;">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Stopped</div>
                <div class="stat-value" id="stopped-apps" style="color: #ef4444;">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Total Logs</div>
                <div class="stat-value" id="total-logs">0</div>
            </div>
        </div>
        
        <div class="section">
            <div class="section-header">
                <h2>Applications</h2>
                <button class="btn btn-primary" onclick="showCreateModal()">+ Create Application</button>
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
                        <td colspan="6" style="text-align: center; color: #9ca3af;">No applications yet. Create one to get started!</td>
                    </tr>
                </tbody>
            </table>
        </div>
    </div>
    
    <!-- Create Application Modal -->
    <div class="modal" id="create-modal">
        <div class="modal-content">
            <h2 style="margin-bottom: 24px;">Create New Application</h2>
            <form id="create-form">
                <div class="form-group">
                    <label>Application Name *</label>
                    <input type="text" name="app_name" required placeholder="my-awesome-bot">
                </div>
                
                <div class="form-group">
                    <label>Application Type *</label>
                    <select name="app_type" onchange="loadTemplate(this.value)" required>
                        <option value="">Select Type</option>
                        <option value="telegram-bot">Telegram Bot</option>
                        <option value="flask-api">Flask API</option>
                        <option value="fastapi">FastAPI</option>
                        <option value="discord-bot">Discord Bot</option>
                        <option value="worker">Background Worker</option>
                        <option value="custom">Custom</option>
                    </select>
                </div>
                
                <div class="form-group">
                    <label>Python Script *</label>
                    <textarea name="script" required placeholder="# Your Python code here"></textarea>
                </div>
                
                <div class="form-group">
                    <label>Requirements (one per line)</label>
                    <textarea name="requirements" placeholder="python-telegram-bot==20.7
flask==3.0.0"></textarea>
                </div>
                
                <div class="form-group">
                    <label>Environment Variables (JSON format)</label>
                    <textarea name="env_vars" placeholder='{"BOT_TOKEN": "your-token-here"}'></textarea>
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
                
                <div style="display: flex; gap: 12px;">
                    <button type="submit" class="btn btn-primary" style="flex: 1;">Create & Deploy</button>
                    <button type="button" class="btn btn-danger" onclick="closeModal('create-modal')">Cancel</button>
                </div>
            </form>
        </div>
    </div>
    
    <!-- Logs Modal -->
    <div class="modal" id="logs-modal">
        <div class="modal-content" style="max-width: 1000px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                <h2 id="logs-title">Application Logs</h2>
                <button class="btn btn-danger btn-sm" onclick="closeModal('logs-modal')">Close</button>
            </div>
            <div class="logs-container" id="logs-content">
                <div style="text-align: center; color: #9ca3af;">Loading logs...</div>
            </div>
        </div>
    </div>
    
    <!-- Toast Notification -->
    <div class="toast" id="toast"></div>
    
    <script>
        let currentLogAppId = null;
        let logEventSource = null;
        
        // Templates
        const templates = {
            'telegram-bot': {
                script: `from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import os

BOT_TOKEN = os.getenv('BOT_TOKEN', 'YOUR_TOKEN_HERE')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Hello! I am running on the hosting platform!')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Available commands:\\n/start - Start the bot\\n/help - Show this message')

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    
    print("Bot started successfully!")
    app.run_polling()

if __name__ == "__main__":
    main()`,
                requirements: 'python-telegram-bot==20.7',
                env_vars: '{"BOT_TOKEN": "your-bot-token-here"}'
            },
            'flask-api': {
                script: `from flask import Flask, jsonify
import os

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        "status": "running",
        "message": "Flask API is live!",
        "platform": "Hosting Platform"
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

@app.route('/api/data')
def get_data():
    return jsonify({
        "data": [1, 2, 3, 4, 5],
        "timestamp": "2024-01-01"
    })

if __name__ == "__main__":
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)`,
                requirements: 'flask==3.0.0',
                env_vars: '{"PORT": "5000"}'
            },
            'fastapi': {
                script: `from fastapi import FastAPI
import uvicorn
import os

app = FastAPI(title="My API")

@app.get("/")
def read_root():
    return {"status": "running", "message": "FastAPI is live!"}

@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.get("/items/{item_id}")
def read_item(item_id: int, q: str = None):
    return {"item_id": item_id, "q": q}

if __name__ == "__main__":
    port = int(os.getenv('PORT', 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)`,
                requirements: 'fastapi==0.109.0\nuvicorn==0.27.0',
                env_vars: '{"PORT": "8000"}'
            },
            'discord-bot': {
                script: `import discord
from discord.ext import commands
import os

TOKEN = os.getenv('DISCORD_TOKEN', 'YOUR_TOKEN_HERE')

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')

@bot.command(name='hello')
async def hello(ctx):
    await ctx.send('Hello! I am running on the hosting platform!')

@bot.command(name='ping')
async def ping(ctx):
    await ctx.send(f'Pong! Latency: {round(bot.latency * 1000)}ms')

if __name__ == "__main__":
    bot.run(TOKEN)`,
                requirements: 'discord.py==2.3.2',
                env_vars: '{"DISCORD_TOKEN": "your-discord-token-here"}'
            },
            'worker': {
                script: `import time
from datetime import datetime

def worker():
    print("Worker started!")
    counter = 0
    
    while True:
        counter += 1
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Worker iteration {counter}")
        
        # Your worker logic here
        # Example: process queue, send notifications, etc.
        
        time.sleep(60)  # Run every minute

if __name__ == "__main__":
    worker()`,
                requirements: '',
                env_vars: '{}'
            }
        };
        
        function loadTemplate(type) {
            if (templates[type]) {
                const form = document.getElementById('create-form');
                form.script.value = templates[type].script;
                form.requirements.value = templates[type].requirements;
                form.env_vars.value = templates[type].env_vars;
            }
        }
        
        function showToast(message, type = 'success') {
            const toast = document.getElementById('toast');
            toast.textContent = message;
            toast.className = `toast ${type}`;
            toast.style.display = 'block';
            setTimeout(() => {
                toast.style.display = 'none';
            }, 3000);
        }
        
        function showCreateModal() {
            document.getElementById('create-modal').classList.add('active');
        }
        
        function closeModal(modalId) {
            document.getElementById(modalId).classList.remove('active');
            if (modalId === 'logs-modal' && logEventSource) {
                logEventSource.close();
                logEventSource = null;
            }
        }
        
        async function loadDashboard() {
            try {
                const response = await fetch('/api/apps');
                const apps = await response.json();
                
                // Update stats
                document.getElementById('total-apps').textContent = apps.length;
                document.getElementById('running-apps').textContent = apps.filter(a => a.status === 'running').length;
                document.getElementById('stopped-apps').textContent = apps.filter(a => a.status !== 'running').length;
                
                // Load logs count
                const logsResponse = await fetch('/api/stats/logs');
                const logsData = await logsResponse.json();
                document.getElementById('total-logs').textContent = logsData.total_logs;
                
                // Update table
                const tbody = document.getElementById('apps-tbody');
                if (apps.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #9ca3af;">No applications yet. Create one to get started!</td></tr>';
                } else {
                    tbody.innerHTML = apps.map(app => `
                        <tr>
                            <td><strong>${app.app_name}</strong></td>
                            <td>${app.app_type}</td>
                            <td><span class="status status-${app.status}">${app.status.toUpperCase()}</span></td>
                            <td>${formatUptime(app.uptime)}</td>
                            <td>${formatDate(app.created_at)}</td>
                            <td>
                                ${app.status === 'running' ? 
                                    `<button class="btn btn-warning btn-sm" onclick="stopApp('${app.app_id}')">Stop</button>` :
                                    `<button class="btn btn-success btn-sm" onclick="startApp('${app.app_id}')">Start</button>`
                                }
                                <button class="btn btn-primary btn-sm" onclick="restartApp('${app.app_id}')">Restart</button>
                                <button class="btn btn-primary btn-sm" onclick="showLogs('${app.app_id}', '${app.app_name}')">Logs</button>
                                <button class="btn btn-danger btn-sm" onclick="deleteApp('${app.app_id}', '${app.app_name}')">Delete</button>
                            </td>
                        </tr>
                    `).join('');
                }
            } catch (error) {
                showToast('Failed to load dashboard', 'error');
            }
        }
        
        function formatUptime(seconds) {
            if (!seconds || seconds === 0) return 'N/A';
            const hours = Math.floor(seconds / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);
            if (hours > 0) return `${hours}h ${minutes}m`;
            return `${minutes}m`;
        }
        
        function formatDate(dateString) {
            const date = new Date(dateString);
            return date.toLocaleDateString() + ' ' + date.toLocaleTimeString();
        }
        
        document.getElementById('create-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            
            const formData = new FormData(e.target);
            const data = {
                app_name: formData.get('app_name'),
                app_type: formData.get('app_type'),
                script: formData.get('script'),
                requirements: formData.get('requirements').split('\n').filter(r => r.trim()),
                env_vars: formData.get('env_vars') ? JSON.parse(formData.get('env_vars')) : {},
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
                    showToast('Application created successfully!');
                    closeModal('create-modal');
                    e.target.reset();
                    loadDashboard();
                } else {
                    showToast(result.error || 'Failed to create application', 'error');
                }
            } catch (error) {
                showToast('Failed to create application', 'error');
            }
        });
        
        async function startApp(appId) {
            try {
                const response = await fetch(`/api/apps/${appId}/start`, {method: 'POST'});
                const result = await response.json();
                showToast(result.message);
                loadDashboard();
            } catch (error) {
                showToast('Failed to start application', 'error');
            }
        }
        
        async function stopApp(appId) {
            try {
                const response = await fetch(`/api/apps/${appId}/stop`, {method: 'POST'});
                const result = await response.json();
                showToast(result.message);
                loadDashboard();
            } catch (error) {
                showToast('Failed to stop application', 'error');
            }
        }
        
        async function restartApp(appId) {
            try {
                const response = await fetch(`/api/apps/${appId}/restart`, {method: 'POST'});
                const result = await response.json();
                showToast(result.message);
                loadDashboard();
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
                const result = await response.json();
                showToast(result.message);
                loadDashboard();
            } catch (error) {
                showToast('Failed to delete application', 'error');
            }
        }
        
        function showLogs(appId, appName) {
            currentLogAppId = appId;
            document.getElementById('logs-title').textContent = `Logs: ${appName}`;
            document.getElementById('logs-modal').classList.add('active');
            
            const logsContent = document.getElementById('logs-content');
            logsContent.innerHTML = '<div style="text-align: center; color: #9ca3af;">Connecting to live logs...</div>';
            
            // Close existing connection
            if (logEventSource) {
                logEventSource.close();
            }
            
            // Start SSE connection
            logEventSource = new EventSource(`/api/apps/${appId}/logs/stream`);
            
            logEventSource.onmessage = (event) => {
                const log = JSON.parse(event.data);
                
                if (logsContent.innerHTML.includes('Connecting to live logs')) {
                    logsContent.innerHTML = '';
                }
                
                const logLine = document.createElement('div');
                logLine.className = `log-line log-${log.level.toLowerCase()}`;
                logLine.textContent = `[${new Date(log.timestamp).toLocaleTimeString()}] [${log.level}] ${log.message}`;
                logsContent.appendChild(logLine);
                
                // Auto-scroll
                logsContent.scrollTop = logsContent.scrollHeight;
                
                // Limit displayed logs
                while (logsContent.children.length > 1000) {
                    logsContent.removeChild(logsContent.firstChild);
                }
            };
            
            logEventSource.onerror = () => {
                logsContent.innerHTML += '<div class="log-error">[ERROR] Connection to logs lost</div>';
            };
        }
        
        // Load dashboard on page load
        loadDashboard();
        
        // Refresh dashboard every 5 seconds
        setInterval(loadDashboard, 5000);
    </script>
</body>
</html>
"""

# ═══════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    if 'logged_in' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        else:
            return render_template_string(LOGIN_TEMPLATE, error='Invalid credentials')
    
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template_string(DASHBOARD_TEMPLATE)

# ═══════════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/apps', methods=['GET'])
@login_required
def get_apps():
    """Get all applications"""
    try:
        apps = list(apps_collection.find({}, {'_id': 0}))
        
        # Add uptime info
        for app in apps:
            app['uptime'] = get_app_uptime(app['app_id'])
        
        return jsonify(apps)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>', methods=['GET'])
@login_required
def get_app(app_id):
    """Get single application"""
    try:
        app = apps_collection.find_one({'app_id': app_id}, {'_id': 0})
        if not app:
            return jsonify({'error': 'Application not found'}), 404
        
        app['uptime'] = get_app_uptime(app_id)
        return jsonify(app)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/create', methods=['POST'])
@login_required
def create_app():
    """Create new application"""
    try:
        data = request.json
        
        # Validate required fields
        required = ['app_name', 'app_type', 'script']
        for field in required:
            if field not in data:
                return jsonify({'error': f'Missing required field: {field}'}), 400
        
        # Generate app ID
        app_id = generate_app_id()
        
        # Create application document
        app_doc = {
            'app_id': app_id,
            'app_name': data['app_name'],
            'app_type': data['app_type'],
            'script': data['script'],
            'requirements': data.get('requirements', []),
            'env_vars': data.get('env_vars', {}),
            'auto_restart': data.get('auto_restart', True),
            'auto_start': data.get('auto_start', False),
            'status': 'stopped',
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow(),
            'last_started': None,
            'uptime_seconds': 0,
            'restart_count': 0,
            'pid': None
        }
        
        # Save to database
        apps_collection.insert_one(app_doc)
        
        # Create log queue
        log_queues[app_id] = queue.Queue(maxsize=1000)
        
        save_log(app_id, 'INFO', f'Application "{data["app_name"]}" created successfully')
        
        return jsonify({
            'success': True,
            'app_id': app_id,
            'message': 'Application created successfully'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>', methods=['PUT'])
@login_required
def update_app(app_id):
    """Update application"""
    try:
        data = request.json
        
        # Check if app exists
        app = apps_collection.find_one({'app_id': app_id})
        if not app:
            return jsonify({'error': 'Application not found'}), 404
        
        # Update allowed fields
        update_fields = {}
        if 'app_name' in data:
            update_fields['app_name'] = data['app_name']
        if 'script' in data:
            update_fields['script'] = data['script']
        if 'requirements' in data:
            update_fields['requirements'] = data['requirements']
        if 'env_vars' in data:
            update_fields['env_vars'] = data['env_vars']
        if 'auto_restart' in data:
            update_fields['auto_restart'] = data['auto_restart']
        if 'auto_start' in data:
            update_fields['auto_start'] = data['auto_start']
        
        update_fields['updated_at'] = datetime.utcnow()
        
        apps_collection.update_one(
            {'app_id': app_id},
            {'$set': update_fields}
        )
        
        save_log(app_id, 'INFO', 'Application configuration updated')
        
        return jsonify({'success': True, 'message': 'Application updated successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>', methods=['DELETE'])
@login_required
def delete_app(app_id):
    """Delete application"""
    try:
        # Stop application if running
        if app_id in running_processes:
            stop_application(app_id, force=True)
        
        # Delete from database
        result = apps_collection.delete_one({'app_id': app_id})
        if result.deleted_count == 0:
            return jsonify({'error': 'Application not found'}), 404
        
        # Delete logs
        logs_collection.delete_many({'app_id': app_id})
        
        # Cleanup
        if app_id in log_queues:
            del log_queues[app_id]
        if app_id in app_start_times:
            del app_start_times[app_id]
        
        return jsonify({'success': True, 'message': 'Application deleted successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/start', methods=['POST'])
@login_required
def api_start_app(app_id):
    """Start application"""
    success, message = start_application(app_id)
    if success:
        return jsonify({'success': True, 'message': message})
    return jsonify({'error': message}), 500

@app.route('/api/apps/<app_id>/stop', methods=['POST'])
@login_required
def api_stop_app(app_id):
    """Stop application"""
    success, message = stop_application(app_id)
    if success:
        return jsonify({'success': True, 'message': message})
    return jsonify({'error': message}), 500

@app.route('/api/apps/<app_id>/restart', methods=['POST'])
@login_required
def api_restart_app(app_id):
    """Restart application"""
    success, message = restart_application(app_id)
    if success:
        return jsonify({'success': True, 'message': message})
    return jsonify({'error': message}), 500

@app.route('/api/apps/<app_id>/logs', methods=['GET'])
@login_required
def get_logs(app_id):
    """Get application logs"""
    try:
        limit = int(request.args.get('limit', 100))
        logs = list(logs_collection.find(
            {'app_id': app_id},
            {'_id': 0}
        ).sort('timestamp', -1).limit(limit))
        
        # Reverse to show oldest first
        logs.reverse()
        
        return jsonify(logs)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/logs/stream')
@login_required
def stream_logs(app_id):
    """Stream logs using Server-Sent Events"""
    def generate():
        # Send recent logs first
        try:
            recent_logs = list(logs_collection.find(
                {'app_id': app_id},
                {'_id': 0}
            ).sort('timestamp', -1).limit(50))
            
            recent_logs.reverse()
            
            for log in recent_logs:
                log['timestamp'] = log['timestamp'].isoformat()
                yield f"data: {json.dumps(log)}\n\n"
        except Exception as e:
            print(f"Error loading recent logs: {e}")
        
        # Stream new logs
        if app_id not in log_queues:
            log_queues[app_id] = queue.Queue(maxsize=1000)
        
        log_queue = log_queues[app_id]
        
        while True:
            try:
                log = log_queue.get(timeout=30)
                yield f"data: {json.dumps(log)}\n\n"
            except queue.Empty:
                # Send keepalive
                yield ": keepalive\n\n"
            except GeneratorExit:
                break
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/apps/<app_id>/logs', methods=['DELETE'])
@login_required
def clear_logs(app_id):
    """Clear application logs"""
    try:
        logs_collection.delete_many({'app_id': app_id})
        save_log(app_id, 'INFO', 'Logs cleared')
        return jsonify({'success': True, 'message': 'Logs cleared successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/status', methods=['GET'])
@login_required
def get_status(app_id):
    """Get application status"""
    try:
        app = apps_collection.find_one({'app_id': app_id}, {'_id': 0})
        if not app:
            return jsonify({'error': 'Application not found'}), 404
        
        status = {
            'app_id': app_id,
            'status': app['status'],
            'pid': app.get('pid'),
            'uptime': get_app_uptime(app_id),
            'restart_count': app.get('restart_count', 0)
        }
        
        return jsonify(status)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/stats/logs', methods=['GET'])
@login_required
def get_logs_stats():
    """Get logs statistics"""
    try:
        total_logs = logs_collection.count_documents({})
        return jsonify({'total_logs': total_logs})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════════
# STARTUP & SHUTDOWN HANDLERS
# ═══════════════════════════════════════════════════════════════════

def startup_handler():
    """Handle platform startup"""
    print("=" * 70)
    print("🚀 HOSTING PLATFORM STARTING")
    print("=" * 70)
    
    if not db:
        print("⚠️  Warning: MongoDB not connected. Platform will have limited functionality.")
        return
    
    # Auto-start applications
    try:
        apps = apps_collection.find({'auto_start': True})
        for app_doc in apps:
            app_id = app_doc['app_id']
            print(f"🔄 Auto-starting: {app_doc['app_name']}")
            start_application(app_id)
            time.sleep(1)  # Stagger starts
    except Exception as e:
        print(f"❌ Error during auto-start: {e}")
    
    # Start monitoring thread
    monitoring_thread = threading.Thread(target=monitoring_loop, daemon=True)
    monitoring_thread.start()
    
    print("=" * 70)
    print("✅ PLATFORM STARTED SUCCESSFULLY")
    print("=" * 70)

def shutdown_handler(signum=None, frame=None):
    """Handle platform shutdown"""
    print("\n" + "=" * 70)
    print("🛑 HOSTING PLATFORM SHUTTING DOWN")
    print("=" * 70)
    
    shutdown_flag.set()
    
    # Stop all applications
    for app_id in list(running_processes.keys()):
        app = apps_collection.find_one({'app_id': app_id})
        if app:
            print(f"🛑 Stopping: {app['app_name']}")
            stop_application(app_id, force=True)
    
    # Close MongoDB connection
    if mongo_client:
        mongo_client.close()
    
    print("=" * 70)
    print("✅ PLATFORM SHUTDOWN COMPLETE")
    print("=" * 70)
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)

# ═══════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    # Run startup handler
    startup_handler()
    
    # Get port from environment (for Render deployment)
    port = int(os.getenv('PORT', 5000))
    
    # Run Flask app
    print(f"\n🌐 Server running on port {port}")
    print(f"🔐 Login: {ADMIN_USERNAME} / {ADMIN_PASSWORD}")
    print(f"🔗 Access: http://localhost:{port}\n")
    
    # Use production server for deployment
    if os.getenv('RENDER'):
        # Running on Render
        app.run(host='0.0.0.0', port=port, debug=False)
    else:
        # Local development
        app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)
