"""
Professional Application Hosting Platform (Koyeb Clone)
Deploy and manage Python applications 24/7

Author: AI Assistant
Version: 2.0.0 (Fixed)
License: MIT
"""

import os
import sys
import json
import time
import signal
import subprocess
import threading
import queue
import logging
import traceback
from datetime import datetime, timedelta
from functools import wraps
from typing import Dict, List, Optional, Any
import secrets
import hashlib

from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for, Response
from flask_cors import CORS
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import PyMongoError
import psutil

# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION & CONSTANTS
# ═══════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', secrets.token_hex(32))
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
CORS(app)

# Admin credentials
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')

# MongoDB connection
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017/')
DB_NAME = 'hosting_platform'

# Platform configuration
MAX_APPS = 50
LOG_RETENTION_DAYS = 7
MAX_LOGS_PER_APP = 10000
HEALTH_CHECK_INTERVAL = 30
MAX_RESTART_ATTEMPTS = 3

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# MONGODB SETUP
# ═══════════════════════════════════════════════════════════════════

try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.server_info()
    db = mongo_client[DB_NAME]
    
    applications_col = db['applications']
    logs_col = db['logs']
    storage_col = db['storage']
    
    applications_col.create_index([('app_id', ASCENDING)], unique=True)
    logs_col.create_index([('app_id', ASCENDING), ('timestamp', DESCENDING)])
    storage_col.create_index([('app_id', ASCENDING), ('key', ASCENDING)])
    
    logger.info("MongoDB connected successfully")
except Exception as e:
    logger.error(f"MongoDB connection failed: {e}")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════
# GLOBAL VARIABLES
# ═══════════════════════════════════════════════════════════════════

running_apps: Dict[str, subprocess.Popen] = {}
log_queues: Dict[str, queue.Queue] = {}
log_threads: Dict[str, threading.Thread] = {}
app_start_times: Dict[str, datetime] = {}
shutdown_event = threading.Event()
installation_lock = threading.Lock()

# ═══════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def generate_app_id():
    return f"app_{secrets.token_hex(8)}"

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def api_auth_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return jsonify({'error': 'Authentication required'}), 401
        return f(*args, **kwargs)
    return decorated_function

def get_app_status(app_id: str) -> str:
    if app_id in running_apps:
        proc = running_apps[app_id]
        if proc.poll() is None:
            return 'running'
        else:
            return 'crashed'
    return 'stopped'

def get_app_uptime(app_id: str) -> int:
    if app_id in app_start_times and get_app_status(app_id) == 'running':
        return int((datetime.utcnow() - app_start_times[app_id]).total_seconds())
    return 0

def save_log(app_id: str, level: str, message: str):
    try:
        log_entry = {
            'app_id': app_id,
            'timestamp': datetime.utcnow(),
            'level': level,
            'message': message
        }
        logs_col.insert_one(log_entry)
        
        if app_id in log_queues:
            try:
                log_queues[app_id].put(log_entry, block=False)
            except queue.Full:
                pass
        
        log_count = logs_col.count_documents({'app_id': app_id})
        if log_count > MAX_LOGS_PER_APP:
            oldest_logs = logs_col.find({'app_id': app_id}).sort('timestamp', ASCENDING).limit(log_count - MAX_LOGS_PER_APP)
            for log in oldest_logs:
                logs_col.delete_one({'_id': log['_id']})
    except Exception as e:
        logger.error(f"Failed to save log: {e}")

# ═══════════════════════════════════════════════════════════════════
# PROCESS MANAGEMENT FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

class LogCaptureThread(threading.Thread):
    def __init__(self, app_id: str, process: subprocess.Popen):
        super().__init__(daemon=True)
        self.app_id = app_id
        self.process = process
        self.running = True
    
    def run(self):
        while self.running and self.process.poll() is None:
            try:
                if self.process.stdout:
                    line = self.process.stdout.readline()
                    if line:
                        message = line.decode('utf-8', errors='ignore').strip()
                        if message:
                            save_log(self.app_id, 'INFO', message)
                
                if self.process.stderr:
                    line = self.process.stderr.readline()
                    if line:
                        message = line.decode('utf-8', errors='ignore').strip()
                        if message:
                            save_log(self.app_id, 'ERROR', message)
            except Exception as e:
                logger.error(f"Log capture error for {self.app_id}: {e}")
                break
    
    def stop(self):
        self.running = False

def install_requirements(app_id: str, requirements: List[str]) -> bool:
    if not requirements:
        return True
    
    save_log(app_id, 'INFO', f"Installing {len(requirements)} packages...")
    
    try:
        # Use lock to prevent concurrent pip installations
        with installation_lock:
            for package in requirements:
                package = package.strip()
                if not package or package.startswith('#'):
                    continue
                
                save_log(app_id, 'INFO', f"Installing {package}...")
                
                result = subprocess.run(
                    [sys.executable, '-m', 'pip', 'install', '--upgrade', package],
                    capture_output=True,
                    text=True,
                    timeout=300
                )
                
                if result.returncode != 0:
                    save_log(app_id, 'ERROR', f"Failed to install {package}")
                    save_log(app_id, 'ERROR', result.stderr[:500])
                    return False
                
                save_log(app_id, 'INFO', f"✓ Installed {package}")
        
        return True
    except subprocess.TimeoutExpired:
        save_log(app_id, 'ERROR', "Installation timeout (5 minutes)")
        return False
    except Exception as e:
        save_log(app_id, 'ERROR', f"Installation error: {str(e)}")
        return False

def validate_python_syntax(script: str) -> tuple[bool, str]:
    """Validate Python syntax before execution"""
    try:
        compile(script, '<string>', 'exec')
        return True, ""
    except SyntaxError as e:
        error_msg = f"Syntax Error at line {e.lineno}: {e.msg}"
        if e.text:
            error_msg += f"\n  {e.text.strip()}\n  {' ' * (e.offset - 1)}^"
        return False, error_msg
    except Exception as e:
        return False, f"Validation error: {str(e)}"

def start_application(app_id: str) -> bool:
    try:
        app_doc = applications_col.find_one({'app_id': app_id})
        if not app_doc:
            logger.error(f"Application {app_id} not found")
            return False
        
        # Stop if already running
        if app_id in running_apps and running_apps[app_id].poll() is None:
            logger.warning(f"Application {app_id} is already running")
            return True
        
        # Validate Python syntax
        is_valid, error_msg = validate_python_syntax(app_doc['script'])
        if not is_valid:
            save_log(app_id, 'ERROR', "Python syntax validation failed:")
            save_log(app_id, 'ERROR', error_msg)
            applications_col.update_one(
                {'app_id': app_id},
                {'$set': {'status': 'crashed'}}
            )
            return False
        
        # Install requirements
        if app_doc.get('requirements'):
            if not install_requirements(app_id, app_doc['requirements']):
                save_log(app_id, 'ERROR', "Failed to install requirements")
                applications_col.update_one(
                    {'app_id': app_id},
                    {'$set': {'status': 'crashed'}}
                )
                return False
        
        # Create script file
        script_path = f"/tmp/{app_id}.py"
        script_content = app_doc['script']
        
        # Replace environment variable placeholders
        for key, value in app_doc.get('env_vars', {}).items():
            script_content = script_content.replace(f"{{{{{key}}}}}", value)
        
        with open(script_path, 'w') as f:
            f.write(script_content)
        
        # Prepare environment
        env = os.environ.copy()
        env.update(app_doc.get('env_vars', {}))
        
        save_log(app_id, 'INFO', f"🚀 Starting {app_doc['app_name']}...")
        
        # Start process
        process = subprocess.Popen(
            [sys.executable, '-u', script_path],  # -u for unbuffered output
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=1,
            universal_newlines=False
        )
        
        running_apps[app_id] = process
        app_start_times[app_id] = datetime.utcnow()
        
        # Start log capture
        log_thread = LogCaptureThread(app_id, process)
        log_thread.start()
        log_threads[app_id] = log_thread
        
        # Create log queue
        if app_id not in log_queues:
            log_queues[app_id] = queue.Queue(maxsize=1000)
        
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
        
        save_log(app_id, 'INFO', f"✓ Application started (PID: {process.pid})")
        logger.info(f"Started application {app_id} (PID: {process.pid})")
        
        return True
        
    except Exception as e:
        error_trace = traceback.format_exc()
        logger.error(f"Failed to start application {app_id}: {e}\n{error_trace}")
        save_log(app_id, 'ERROR', f"Startup failed: {str(e)}")
        save_log(app_id, 'ERROR', error_trace[:1000])
        applications_col.update_one(
            {'app_id': app_id},
            {'$set': {'status': 'crashed'}}
        )
        return False

def stop_application(app_id: str, force: bool = False) -> bool:
    try:
        if app_id not in running_apps:
            logger.warning(f"Application {app_id} is not running")
            applications_col.update_one(
                {'app_id': app_id},
                {'$set': {'status': 'stopped', 'pid': None}}
            )
            return True
        
        process = running_apps[app_id]
        
        # Stop log capture
        if app_id in log_threads:
            log_threads[app_id].stop()
            del log_threads[app_id]
        
        save_log(app_id, 'INFO', "⏹ Stopping application...")
        
        if force or process.poll() is not None:
            try:
                process.kill()
                save_log(app_id, 'INFO', "Application force killed")
            except:
                pass
        else:
            try:
                process.terminate()
                process.wait(timeout=10)
                save_log(app_id, 'INFO', "Application stopped gracefully")
            except subprocess.TimeoutExpired:
                process.kill()
                save_log(app_id, 'WARNING', "Application force killed after timeout")
        
        del running_apps[app_id]
        if app_id in app_start_times:
            del app_start_times[app_id]
        
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
        
        logger.info(f"Stopped application {app_id}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to stop application {app_id}: {e}")
        return False

def restart_application(app_id: str) -> bool:
    save_log(app_id, 'INFO', "🔄 Restarting application...")
    
    if stop_application(app_id):
        time.sleep(2)
        return start_application(app_id)
    
    return False

# ═══════════════════════════════════════════════════════════════════
# MONITORING THREAD
# ═══════════════════════════════════════════════════════════════════

def monitoring_loop():
    logger.info("Monitoring thread started")
    
    while not shutdown_event.is_set():
        try:
            apps = list(applications_col.find())
            
            for app_doc in apps:
                app_id = app_doc['app_id']
                
                if app_id in running_apps:
                    process = running_apps[app_id]
                    
                    if process.poll() is not None:
                        # Process crashed
                        save_log(app_id, 'ERROR', f"💥 Application crashed (exit code: {process.returncode})")
                        
                        applications_col.update_one(
                            {'app_id': app_id},
                            {
                                '$set': {'status': 'crashed'},
                                '$inc': {'restart_count': 1}
                            }
                        )
                        
                        del running_apps[app_id]
                        if app_id in app_start_times:
                            del app_start_times[app_id]
                        
                        # Auto-restart
                        if app_doc.get('auto_restart', False):
                            restart_count = app_doc.get('restart_count', 0)
                            if restart_count < MAX_RESTART_ATTEMPTS:
                                save_log(app_id, 'INFO', f"🔄 Auto-restarting (attempt {restart_count + 1}/{MAX_RESTART_ATTEMPTS})")
                                time.sleep(5)  # Wait before restart
                                threading.Thread(target=start_application, args=(app_id,), daemon=True).start()
                            else:
                                save_log(app_id, 'ERROR', f"❌ Max restart attempts ({MAX_RESTART_ATTEMPTS}) reached")
                
                # Update uptime
                if app_doc.get('status') == 'running':
                    uptime = get_app_uptime(app_id)
                    applications_col.update_one(
                        {'app_id': app_id},
                        {'$set': {'uptime_seconds': uptime}}
                    )
            
            for _ in range(HEALTH_CHECK_INTERVAL):
                if shutdown_event.is_set():
                    break
                time.sleep(1)
                
        except Exception as e:
            logger.error(f"Monitoring error: {e}")
            time.sleep(5)
    
    logger.info("Monitoring thread stopped")

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
            color: #1f2937;
            margin-bottom: 30px;
            text-align: center;
            font-size: 28px;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            margin-bottom: 8px;
            color: #4b5563;
            font-weight: 500;
        }
        input {
            width: 100%;
            padding: 12px;
            border: 2px solid #e5e7eb;
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
            color: #c00;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 20px;
            text-align: center;
        }
        .logo {
            text-align: center;
            margin-bottom: 30px;
            font-size: 48px;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <div class="logo">🚀</div>
        <h1>Hosting Platform</h1>
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
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        .navbar h1 {
            font-size: 24px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .navbar button {
            background: rgba(255,255,255,0.2);
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            color: white;
            cursor: pointer;
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
            color: #6b7280;
            font-size: 14px;
            margin-bottom: 8px;
            text-transform: uppercase;
        }
        .stat-card .value {
            font-size: 36px;
            font-weight: bold;
            color: #667eea;
        }
        .apps-section {
            background: white;
            border-radius: 12px;
            padding: 24px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .btn {
            padding: 10px 20px;
            border-radius: 8px;
            border: none;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
        }
        .btn-primary {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
        }
        .btn-success { background: #10b981; color: white; }
        .btn-danger { background: #ef4444; color: white; }
        .btn-warning { background: #f59e0b; color: white; }
        .btn-small {
            padding: 6px 12px;
            font-size: 12px;
            margin: 0 4px;
        }
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
            color: #4b5563;
        }
        .status {
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
            display: inline-block;
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
            border-radius: 12px;
            width: 90%;
            max-width: 800px;
            max-height: 90vh;
            overflow-y: auto;
            padding: 30px;
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
            border-radius: 6px;
            font-size: 14px;
        }
        .form-group textarea {
            font-family: 'Courier New', monospace;
            min-height: 200px;
        }
        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: #9ca3af;
        }
        .toast {
            position: fixed;
            top: 20px;
            right: 20px;
            background: #10b981;
            color: white;
            padding: 16px 24px;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            display: none;
            z-index: 2000;
        }
        .toast.show { display: block; }
        .toast.error { background: #ef4444; }
    </style>
</head>
<body>
    <div class="navbar">
        <h1>🚀 Hosting Platform</h1>
        <button onclick="logout()">Logout</button>
    </div>
    
    <div class="container">
        <div class="stats-grid">
            <div class="stat-card">
                <h3>Total Apps</h3>
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
                <h3>Crashed</h3>
                <div class="value" style="color: #f59e0b;" id="crashed-apps">0</div>
            </div>
        </div>
        
        <div class="apps-section">
            <div class="section-header">
                <h2>Applications</h2>
                <button class="btn btn-primary" onclick="showCreateModal()">+ New Application</button>
            </div>
            <div id="apps-container"></div>
        </div>
    </div>
    
    <!-- Create Modal -->
    <div class="modal" id="create-modal">
        <div class="modal-content">
            <h2>Create New Application</h2>
            <form onsubmit="createApp(event)">
                <div class="form-group">
                    <label>Environment Variables (JSON format)</label>
                    <textarea name="env_vars" rows="4" placeholder='{"BOT_TOKEN": "your_token_here"}'>{}</textarea>
                </div>
                <div class="form-group">
                    <label><input type="checkbox" name="auto_restart" checked> Auto-restart on crash</label>
                </div>
                <div class="form-group">
                    <label><input type="checkbox" name="auto_start" checked> Auto-start on server boot</label>
                </div>
                <div style="display: flex; gap: 10px; justify-content: flex-end;">
                    <button type="button" class="btn" onclick="closeModal('create-modal')">Cancel</button>
                    <button type="submit" class="btn btn-primary">Create Application</button>
                </div>
            </form>
        </div>
    </div>
    
    <!-- Edit Modal -->
    <div class="modal" id="edit-modal">
        <div class="modal-content">
            <h2>Edit Application</h2>
            <form onsubmit="updateApp(event)">
                <input type="hidden" name="app_id" id="edit-app-id">
                <div class="form-group">
                    <label>Application Name</label>
                    <input type="text" name="app_name" id="edit-app-name" required>
                </div>
                <div class="form-group">
                    <label>Python Script</label>
                    <textarea name="script" id="edit-script" required></textarea>
                </div>
                <div class="form-group">
                    <label>Requirements (one per line)</label>
                    <textarea name="requirements" id="edit-requirements" rows="4"></textarea>
                </div>
                <div class="form-group">
                    <label>Environment Variables (JSON format)</label>
                    <textarea name="env_vars" id="edit-env-vars" rows="4"></textarea>
                </div>
                <div class="form-group">
                    <label><input type="checkbox" name="auto_restart" id="edit-auto-restart"> Auto-restart on crash</label>
                </div>
                <div class="form-group">
                    <label><input type="checkbox" name="auto_start" id="edit-auto-start"> Auto-start on server boot</label>
                </div>
                <div style="display: flex; gap: 10px; justify-content: flex-end;">
                    <button type="button" class="btn" onclick="closeModal('edit-modal')">Cancel</button>
                    <button type="submit" class="btn btn-primary">Update Application</button>
                </div>
            </form>
        </div>
    </div>
    
    <!-- Logs Modal -->
    <div class="modal" id="logs-modal">
        <div class="modal-content" style="max-width: 1200px;">
            <h2 id="logs-title">Application Logs</h2>
            <div style="background: #1f2937; color: #f3f4f6; padding: 20px; border-radius: 8px; height: 500px; overflow-y: auto; font-family: 'Courier New', monospace; font-size: 13px;" id="logs-content">
                Loading logs...
            </div>
            <div style="margin-top: 20px; display: flex; gap: 10px; justify-content: flex-end;">
                <button class="btn btn-danger btn-small" onclick="clearLogs()">Clear Logs</button>
                <button class="btn" onclick="closeModal('logs-modal')">Close</button>
            </div>
        </div>
    </div>
    
    <div class="toast" id="toast"></div>
    
    <script>
        const APP_TEMPLATES = {
            telegram_bot: {
                script: `from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import os

BOT_TOKEN = os.getenv('BOT_TOKEN', '{{BOT_TOKEN}}')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Hello! I am running on the hosting platform! 🚀')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """Available commands:
/start - Start the bot
/help - Show this help message
/status - Check bot status"""
    await update.message.reply_text(help_text)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('✅ Bot is running perfectly!')

def main():
    print("Starting Telegram Bot...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('status', status))
    
    print("Bot started successfully! Ready to receive messages.")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()`,
                requirements: 'python-telegram-bot==20.7',
                env_vars: '{"BOT_TOKEN": "paste_your_bot_token_here"}'
            },
            flask_api: {
                script: `from flask import Flask, jsonify, request
import os

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        'status': 'running',
        'message': 'API is live on hosting platform!',
        'version': '1.0.0'
    })

@app.route('/health')
def health():
    return jsonify({'status': 'healthy', 'uptime': 'running'})

@app.route('/api/data')
def get_data():
    return jsonify({
        'data': [1, 2, 3, 4, 5],
        'count': 5,
        'timestamp': str(__import__('datetime').datetime.now())
    })

@app.route('/api/echo', methods=['POST'])
def echo():
    data = request.get_json()
    return jsonify({'echo': data, 'received': True})

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    print(f"Starting Flask API on port {port}...")
    app.run(host='0.0.0.0', port=port, debug=False)`,
                requirements: 'flask==3.0.0',
                env_vars: '{"PORT": "5000"}'
            },
            fastapi: {
                script: `from fastapi import FastAPI
import uvicorn
import os

app = FastAPI(title="My API", version="1.0.0")

@app.get("/")
def read_root():
    return {"status": "running", "message": "FastAPI is live!"}

@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.get("/api/items/{item_id}")
def read_item(item_id: int, q: str = None):
    return {"item_id": item_id, "name": f"Item {item_id}", "query": q}

@app.post("/api/create")
def create_item(item: dict):
    return {"created": True, "item": item}

if __name__ == "__main__":
    port = int(os.getenv('PORT', 8000))
    print(f"Starting FastAPI on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)`,
                requirements: 'fastapi==0.104.1\\nuvicorn==0.24.0',
                env_vars: '{"PORT": "8000"}'
            },
            discord_bot: {
                script: `import discord
from discord.ext import commands
import os

BOT_TOKEN = os.getenv('BOT_TOKEN', '{{BOT_TOKEN}}')

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'✅ Bot logged in as {bot.user}')
    print(f'Connected to {len(bot.guilds)} servers')

@bot.command()
async def hello(ctx):
    await ctx.send('👋 Hello! I am running on the hosting platform!')

@bot.command()
async def ping(ctx):
    latency = round(bot.latency * 1000)
    await ctx.send(f'🏓 Pong! Latency: {latency}ms')

@bot.command()
async def info(ctx):
    await ctx.send(f'Bot: {bot.user.name}\\nServers: {len(bot.guilds)}\\nUsers: {len(bot.users)}')

print("Starting Discord Bot...")
bot.run(BOT_TOKEN)`,
                requirements: 'discord.py==2.3.2',
                env_vars: '{"BOT_TOKEN": "paste_your_discord_token_here"}'
            },
            worker: {
                script: `import time
import os
from datetime import datetime

def main():
    print("✅ Worker started successfully!")
    counter = 0
    
    while True:
        counter += 1
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] Worker iteration {counter}")
        
        # Your background task here
        # Examples:
        # - Process queue items
        # - Check database for pending tasks
        # - Send scheduled notifications
        # - Clean up old data
        # - Generate reports
        
        time.sleep(60)  # Sleep for 60 seconds

if __name__ == '__main__':
    main()`,
                requirements: '',
                env_vars: '{}'
            },
            custom: {
                script: `# Write your custom Python code here
import time
from datetime import datetime

print("Application started successfully!")
print(f"Started at: {datetime.now()}")

# Your code here
while True:
    print(f"Running... {datetime.now()}")
    time.sleep(60)`,
                requirements: '',
                env_vars: '{}'
            }
        };
        
        let currentLogAppId = null;
        let logEventSource = null;
        
        function loadTemplate(type) {
            if (!type || !APP_TEMPLATES[type]) return;
            
            const template = APP_TEMPLATES[type];
            document.querySelector('[name="script"]').value = template.script;
            document.querySelector('[name="requirements"]').value = template.requirements;
            document.querySelector('[name="env_vars"]').value = template.env_vars;
        }
        
        function showToast(message, isError = false) {
            const toast = document.getElementById('toast');
            toast.textContent = message;
            toast.className = 'toast show' + (isError ? ' error' : '');
            setTimeout(() => toast.className = 'toast', 3000);
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
        
        async function createApp(e) {
            e.preventDefault();
            const formData = new FormData(e.target);
            
            try {
                const envVars = formData.get('env_vars') || '{}';
                JSON.parse(envVars); // Validate JSON
                
                const data = {
                    app_name: formData.get('app_name'),
                    app_type: formData.get('app_type'),
                    script: formData.get('script'),
                    requirements: formData.get('requirements').split('\\n').filter(r => r.trim()),
                    env_vars: JSON.parse(envVars),
                    auto_restart: formData.get('auto_restart') === 'on',
                    auto_start: formData.get('auto_start') === 'on'
                };
                
                const response = await fetch('/api/apps/create', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                });
                
                const result = await response.json();
                
                if (response.ok) {
                    showToast('✓ Application created successfully!');
                    closeModal('create-modal');
                    e.target.reset();
                    loadApps();
                } else {
                    showToast(result.error || 'Failed to create application', true);
                }
            } catch (error) {
                showToast('Error: ' + error.message, true);
            }
        }
        
        async function loadApps() {
            try {
                const response = await fetch('/api/apps');
                const apps = await response.json();
                
                document.getElementById('total-apps').textContent = apps.length;
                document.getElementById('running-apps').textContent = apps.filter(a => a.status === 'running').length;
                document.getElementById('stopped-apps').textContent = apps.filter(a => a.status === 'stopped').length;
                document.getElementById('crashed-apps').textContent = apps.filter(a => a.status === 'crashed').length;
                
                const container = document.getElementById('apps-container');
                
                if (apps.length === 0) {
                    container.innerHTML = '<div class="empty-state"><div style="font-size:64px;margin-bottom:20px;">📦</div><h3>No applications yet</h3><p>Create your first application to get started</p></div>';
                    return;
                }
                
                container.innerHTML = `
                    <table>
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
                        <tbody>
                            ${apps.map(app => `
                                <tr>
                                    <td><strong>${app.app_name}</strong></td>
                                    <td>${app.app_type}</td>
                                    <td><span class="status status-${app.status}">${app.status.toUpperCase()}</span></td>
                                    <td>${formatUptime(app.uptime_seconds || 0)}</td>
                                    <td>${new Date(app.created_at).toLocaleDateString()}</td>
                                    <td>
                                        ${app.status === 'running' ? 
                                            `<button class="btn btn-warning btn-small" onclick="stopApp('${app.app_id}')">Stop</button>` :
                                            `<button class="btn btn-success btn-small" onclick="startApp('${app.app_id}')">Start</button>`
                                        }
                                        <button class="btn btn-small" onclick="restartApp('${app.app_id}')">Restart</button>
                                        <button class="btn btn-small" onclick="showLogs('${app.app_id}', '${app.app_name}')">Logs</button>
                                        <button class="btn btn-small" onclick="showEditModal('${app.app_id}')">Edit</button>
                                        <button class="btn btn-danger btn-small" onclick="deleteApp('${app.app_id}', '${app.app_name}')">Delete</button>
                                    </td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                `;
            } catch (error) {
                showToast('Failed to load applications', true);
            }
        }
        
        function formatUptime(seconds) {
            if (seconds === 0) return '-';
            const hours = Math.floor(seconds / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);
            return `${hours}h ${minutes}m`;
        }
        
        async function startApp(appId) {
            try {
                showToast('Starting application...');
                const response = await fetch(`/api/apps/${appId}/start`, {method: 'POST'});
                const result = await response.json();
                showToast(result.message || '✓ Application started');
                loadApps();
            } catch (error) {
                showToast('Failed to start application', true);
            }
        }
        
        async function stopApp(appId) {
            try {
                showToast('Stopping application...');
                const response = await fetch(`/api/apps/${appId}/stop`, {method: 'POST'});
                const result = await response.json();
                showToast(result.message || '✓ Application stopped');
                loadApps();
            } catch (error) {
                showToast('Failed to stop application', true);
            }
        }
        
        async function restartApp(appId) {
            try {
                showToast('Restarting application...');
                const response = await fetch(`/api/apps/${appId}/restart`, {method: 'POST'});
                const result = await response.json();
                showToast(result.message || '✓ Application restarting');
                setTimeout(loadApps, 2000);
            } catch (error) {
                showToast('Failed to restart application', true);
            }
        }
        
        async function deleteApp(appId, appName) {
            if (!confirm(`Are you sure you want to delete "${appName}"? This action cannot be undone.`)) {
                return;
            }
            
            try {
                const response = await fetch(`/api/apps/${appId}`, {method: 'DELETE'});
                const result = await response.json();
                showToast(result.message || '✓ Application deleted');
                loadApps();
            } catch (error) {
                showToast('Failed to delete application', true);
            }
        }
        
        async function showEditModal(appId) {
            try {
                const response = await fetch(`/api/apps/${appId}`);
                const app = await response.json();
                
                document.getElementById('edit-app-id').value = app.app_id;
                document.getElementById('edit-app-name').value = app.app_name;
                document.getElementById('edit-script').value = app.script;
                document.getElementById('edit-requirements').value = app.requirements.join('\\n');
                document.getElementById('edit-env-vars').value = JSON.stringify(app.env_vars, null, 2);
                document.getElementById('edit-auto-restart').checked = app.auto_restart;
                document.getElementById('edit-auto-start').checked = app.auto_start;
                
                document.getElementById('edit-modal').classList.add('active');
            } catch (error) {
                showToast('Failed to load application', true);
            }
        }
        
        async function updateApp(e) {
            e.preventDefault();
            const formData = new FormData(e.target);
            const appId = formData.get('app_id');
            
            try {
                const data = {
                    app_name: formData.get('app_name'),
                    script: formData.get('script'),
                    requirements: formData.get('requirements').split('\\n').filter(r => r.trim()),
                    env_vars: JSON.parse(formData.get('env_vars') || '{}'),
                    auto_restart: formData.get('auto_restart') === 'on',
                    auto_start: formData.get('auto_start') === 'on'
                };
                
                const response = await fetch(`/api/apps/${appId}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                });
                
                const result = await response.json();
                
                if (response.ok) {
                    showToast('✓ Application updated successfully!');
                    closeModal('edit-modal');
                    loadApps();
                } else {
                    showToast(result.error || 'Failed to update application', true);
                }
            } catch (error) {
                showToast('Error: ' + error.message, true);
            }
        }
        
        function showLogs(appId, appName) {
            currentLogAppId = appId;
            document.getElementById('logs-title').textContent = `Logs - ${appName}`;
            document.getElementById('logs-modal').classList.add('active');
            
            const logsContent = document.getElementById('logs-content');
            logsContent.innerHTML = 'Connecting to live logs...';
            
            if (logEventSource) {
                logEventSource.close();
            }
            
            logEventSource = new EventSource(`/api/apps/${appId}/logs/stream`);
            
            logEventSource.onmessage = function(event) {
                const log = JSON.parse(event.data);
                const logLine = document.createElement('div');
                logLine.style.marginBottom = '4px';
                
                const time = new Date(log.timestamp).toLocaleTimeString();
                const color = log.level === 'ERROR' ? '#ef4444' : 
                             log.level === 'WARNING' ? '#f59e0b' : 
                             log.level === 'INFO' ? '#10b981' : '#9ca3af';
                
                logLine.innerHTML = `<span style="color: #9ca3af;">${time}</span> <span style="color: ${color};">[${log.level}]</span> ${escapeHtml(log.message)}`;
                logsContent.appendChild(logLine);
                logsContent.scrollTop = logsContent.scrollHeight;
                
                while (logsContent.children.length > 1000) {
                    logsContent.removeChild(logsContent.firstChild);
                }
            };
            
            logEventSource.onerror = function() {
                const errorLine = document.createElement('div');
                errorLine.style.color = '#ef4444';
                errorLine.textContent = 'Connection lost. Reconnecting...';
                logsContent.appendChild(errorLine);
            };
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        async function clearLogs() {
            if (!currentLogAppId) return;
            
            if (!confirm('Are you sure you want to clear all logs for this application?')) {
                return;
            }
            
            try {
                const response = await fetch(`/api/apps/${currentLogAppId}/logs`, {method: 'DELETE'});
                const result = await response.json();
                showToast(result.message || '✓ Logs cleared');
                document.getElementById('logs-content').innerHTML = 'Logs cleared.';
            } catch (error) {
                showToast('Failed to clear logs', true);
            }
        }
        
        function logout() {
            if (confirm('Are you sure you want to logout?')) {
                window.location.href = '/logout';
            }
        }
        
        setInterval(loadApps, 5000);
        loadApps();
    </script>
</body>
</html>
"""

# ═══════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════════════

@app.route('/')
@login_required
def dashboard():
    return render_template_string(DASHBOARD_TEMPLATE)

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
    session.clear()
    return redirect(url_for('login'))

# ═══════════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/apps/create', methods=['POST'])
@api_auth_required
def api_create_app():
    try:
        data = request.json
        
        required_fields = ['app_name', 'app_type', 'script']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'Missing required field: {field}'}), 400
        
        app_count = applications_col.count_documents({})
        if app_count >= MAX_APPS:
            return jsonify({'error': f'Maximum {MAX_APPS} applications allowed'}), 400
        
        # Validate syntax
        is_valid, error_msg = validate_python_syntax(data['script'])
        if not is_valid:
            return jsonify({'error': f'Python syntax error: {error_msg}'}), 400
        
        app_id = generate_app_id()
        
        app_doc = {
            'app_id': app_id,
            'app_name': data['app_name'],
            'app_type': data['app_type'],
            'script': data['script'],
            'requirements': data.get('requirements', []),
            'env_vars': data.get('env_vars', {}),
            'auto_restart': data.get('auto_restart', True),
            'auto_start': data.get('auto_start', True),
            'status': 'stopped',
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow(),
            'last_started': None,
            'uptime_seconds': 0,
            'restart_count': 0,
            'pid': None
        }
        
        applications_col.insert_one(app_doc)
        save_log(app_id, 'INFO', f"✨ Application '{data['app_name']}' created")
        
        if app_doc['auto_start']:
            threading.Thread(target=start_application, args=(app_id,), daemon=True).start()
        
        return jsonify({
            'message': 'Application created successfully',
            'app_id': app_id
        }), 201
        
    except Exception as e:
        logger.error(f"Failed to create application: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps', methods=['GET'])
@api_auth_required
def api_list_apps():
    try:
        apps = list(applications_col.find({}, {'_id': 0}).sort('created_at', DESCENDING))
        
        for app in apps:
            app['status'] = get_app_status(app['app_id'])
            app['uptime_seconds'] = get_app_uptime(app['app_id'])
        
        return jsonify(apps), 200
    except Exception as e:
        logger.error(f"Failed to list applications: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>', methods=['GET'])
@api_auth_required
def api_get_app(app_id):
    try:
        app = applications_col.find_one({'app_id': app_id}, {'_id': 0})
        if not app:
            return jsonify({'error': 'Application not found'}), 404
        
        app['status'] = get_app_status(app_id)
        app['uptime_seconds'] = get_app_uptime(app_id)
        
        return jsonify(app), 200
    except Exception as e:
        logger.error(f"Failed to get application: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>', methods=['PUT'])
@api_auth_required
def api_update_app(app_id):
    try:
        data = request.json
        app = applications_col.find_one({'app_id': app_id})
        if not app:
            return jsonify({'error': 'Application not found'}), 404
        
        # Validate syntax if script changed
        if 'script' in data:
            is_valid, error_msg = validate_python_syntax(data['script'])
            if not is_valid:
                return jsonify({'error': f'Python syntax error: {error_msg}'}), 400
        
        update_data = {
            'updated_at': datetime.utcnow()
        }
        
        if 'app_name' in data:
            update_data['app_name'] = data['app_name']
        if 'script' in data:
            update_data['script'] = data['script']
        if 'requirements' in data:
            update_data['requirements'] = data['requirements']
        if 'env_vars' in data:
            update_data['env_vars'] = data['env_vars']
        if 'auto_restart' in data:
            update_data['auto_restart'] = data['auto_restart']
        if 'auto_start' in data:
            update_data['auto_start'] = data['auto_start']
        
        applications_col.update_one(
            {'app_id': app_id},
            {'$set': update_data}
        )
        
        save_log(app_id, 'INFO', "✏️ Application configuration updated")
        
        return jsonify({'message': 'Application updated successfully'}), 200
        
    except Exception as e:
        logger.error(f"Failed to update application: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>', methods=['DELETE'])
@api_auth_required
def api_delete_app(app_id):
    try:
        app = applications_col.find_one({'app_id': app_id})
        if not app:
            return jsonify({'error': 'Application not found'}), 404
        
        if get_app_status(app_id) == 'running':
            stop_application(app_id, force=True)
        
        applications_col.delete_one({'app_id': app_id})
        logs_col.delete_many({'app_id': app_id})
        storage_col.delete_many({'app_id': app_id})
        
        if app_id in log_queues:
            del log_queues[app_id]
        
        logger.info(f"Deleted application {app_id}")
        
        return jsonify({'message': 'Application deleted successfully'}), 200
    except Exception as e:
        logger.error(f"Failed to delete application: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/start', methods=['POST'])
@api_auth_required
def api_start_app(app_id):
    try:
        app = applications_col.find_one({'app_id': app_id})
        if not app:
            return jsonify({'error': 'Application not found'}), 404
        
        # Reset restart count on manual start
        applications_col.update_one(
            {'app_id': app_id},
            {'$set': {'restart_count': 0}}
        )
        
        success = start_application(app_id)
        
        if success:
            return jsonify({'message': 'Application started successfully'}), 200
        else:
            return jsonify({'error': 'Failed to start application'}), 500
    except Exception as e:
        logger.error(f"Failed to start application: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/stop', methods=['POST'])
@api_auth_required
def api_stop_app(app_id):
    try:
        success = stop_application(app_id)
        
        if success:
            return jsonify({'message': 'Application stopped successfully'}), 200
        else:
            return jsonify({'error': 'Failed to stop application'}), 500
    except Exception as e:
        logger.error(f"Failed to stop application: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/restart', methods=['POST'])
@api_auth_required
def api_restart_app(app_id):
    try:
        # Reset restart count on manual restart
        applications_col.update_one(
            {'app_id': app_id},
            {'$set': {'restart_count': 0}}
        )
        
        success = restart_application(app_id)
        
        if success:
            return jsonify({'message': 'Application restarted successfully'}), 200
        else:
            return jsonify({'error': 'Failed to restart application'}), 500
    except Exception as e:
        logger.error(f"Failed to restart application: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/logs', methods=['GET'])
@api_auth_required
def api_get_logs(app_id):
    try:
        limit = int(request.args.get('limit', 1000))
        
        logs = list(logs_col.find(
            {'app_id': app_id},
            {'_id': 0}
        ).sort('timestamp', DESCENDING).limit(limit))
        
        logs.reverse()
        
        return jsonify(logs), 200
    except Exception as e:
        logger.error(f"Failed to get logs: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/apps/<app_id>/logs/stream')
@api_auth_required
def api_stream_logs(app_id):
    def generate():
        if app_id not in log_queues:
            log_queues[app_id] = queue.Queue(maxsize=1000)
        
        log_queue = log_queues[app_id]
        
        try:
            logs = list(logs_col.find(
                {'app_id': app_id},
                {'_id': 0}
            ).sort('timestamp', DESCENDING).limit(100))
            
            for log in reversed(logs):
                log['timestamp'] = log['timestamp'].isoformat()
                yield f"data: {json.dumps(log)}\n\n"
        except:
            pass
        
        while True:
            try:
                log = log_queue.get(timeout=30)
                log['timestamp'] = log['timestamp'].isoformat()
                yield f"data: {json.dumps(log)}\n\n"
            except queue.Empty:
                yield f": heartbeat\n\n"
            except:
                break
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/apps/<app_id>/logs', methods=['DELETE'])
@api_auth_required
def api_clear_logs(app_id):
    try:
        result = logs_col.delete_many({'app_id': app_id})
        save_log(app_id, 'INFO', '🗑️ Logs cleared by admin')
        return jsonify({
            'message': f'Deleted {result.deleted_count} log entries'
        }), 200
    except Exception as e:
        logger.error(f"Failed to clear logs: {e}")
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════════
# STARTUP & SHUTDOWN HANDLERS
# ═══════════════════════════════════════════════════════════════════

def startup_platform():
    logger.info("=" * 70)
    logger.info("STARTING HOSTING PLATFORM")
    logger.info("=" * 70)
    
    monitoring_thread = threading.Thread(target=monitoring_loop, daemon=True)
    monitoring_thread.start()
    logger.info("✓ Monitoring thread started")
    
    try:
        apps = list(applications_col.find({'auto_start': True}))
        logger.info(f"Found {len(apps)} applications with auto-start enabled")
        
        for app in apps:
            app_id = app['app_id']
            logger.info(f"Auto-starting {app['app_name']} ({app_id})")
            threading.Thread(target=start_application, args=(app_id,), daemon=True).start()
            time.sleep(2)
    except Exception as e:
        logger.error(f"Error during auto-start: {e}")
    
    logger.info("=" * 70)
    logger.info("✓ PLATFORM STARTED SUCCESSFULLY")
    logger.info(f"Admin URL: http://localhost:{PORT}")
    logger.info(f"Username: {ADMIN_USERNAME}")
    logger.info("=" * 70)

def shutdown_platform():
    logger.info("=" * 70)
    logger.info("SHUTTING DOWN PLATFORM")
    logger.info("=" * 70)
    
    shutdown_event.set()
    
    app_ids = list(running_apps.keys())
    for app_id in app_ids:
        logger.info(f"Stopping application {app_id}")
        stop_application(app_id, force=False)
    
    mongo_client.close()
    
    logger.info("✓ PLATFORM SHUTDOWN COMPLETE")
    logger.info("=" * 70)

def signal_handler(sig, frame):
    shutdown_platform()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ═══════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    PORT = int(os.getenv('PORT', 5000))
    
    startup_platform()
    
    app.run(
        host='0.0.0.0',
        port=PORT,
        debug=False,
        threaded=True
    ) Application Name</label>
                    <input type="text" name="app_name" required placeholder="My Awesome Bot">
                </div>
                <div class="form-group">
                    <label>Application Type</label>
                    <select name="app_type" onchange="loadTemplate(this.value)">
                        <option value="">Select type...</option>
                        <option value="telegram_bot">Telegram Bot</option>
                        <option value="flask_api">Flask API</option>
                        <option value="fastapi">FastAPI</option>
                        <option value="discord_bot">Discord Bot</option>
                        <option value="worker">Background Worker</option>
                        <option value="custom">Custom</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Python Script</label>
                    <textarea name="script" id="script-textarea" required></textarea>
                </div>
                <div class="form-group">
                    <label>Requirements (one per line)</label>
                    <textarea name="requirements" rows="4" placeholder="flask==3.0.0&#10;requests"></textarea>
                </div>
                <div class="form-group">
                    <label>
