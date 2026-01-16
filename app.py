#!/usr/bin/env python3
"""
Professional Application Hosting Platform
Similar to Koyeb/Railway/Render
Supports: Telegram Bots, APIs, Discord Bots, Workers, and any Python application
"""

import os
import sys
import subprocess
import threading
import time
import signal
import uuid
import json
import logging
import re
from datetime import datetime, timedelta
from queue import Queue, Empty
from functools import wraps

from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for, Response
from flask_cors import CORS
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
import psutil

# ═══════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "venuboy")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "venuboy")
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://Zerobothost:zerobothost@cluster0.bl0tf2.mongodb.net/?appName=Cluster0")
SECRET_KEY = os.environ.get("SECRET_KEY", "your-secret-key-change-in-production")

MAX_LOGS_PER_APP = 5000
LOG_RETENTION_DAYS = 7
HEALTH_CHECK_INTERVAL = 30
MAX_RESTART_ATTEMPTS = 5
COMMAND_TIMEOUT = 60

# ═══════════════════════════════════════════════════════════════════════
# FLASK APP SETUP
# ═══════════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.secret_key = SECRET_KEY
CORS(app)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# MONGODB SETUP
# ═══════════════════════════════════════════════════════════════════════

try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping')
    db = mongo_client.hosting_platform
    
    applications_col = db.applications
    logs_col = db.logs
    storage_col = db.storage
    
    logs_col.create_index([("app_id", 1), ("timestamp", -1)])
    applications_col.create_index("app_id", unique=True)
    
    logger.info("✓ MongoDB connected successfully")
except ConnectionFailure as e:
    logger.error(f"✗ MongoDB connection failed: {e}")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════════
# GLOBAL STATE
# ═══════════════════════════════════════════════════════════════════════

running_processes = {}
log_queues = {}
log_threads = {}
app_start_times = {}

# ═══════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def validate_python_code(code):
    try:
        compile(code, '<string>', 'exec')
        return True, "Valid Python code"
    except SyntaxError as e:
        return False, f"Syntax Error: {e.msg} at line {e.lineno}"
    except Exception as e:
        return False, f"Error: {str(e)}"

def get_app_directory(app_id):
    app_dir = os.path.join(os.getcwd(), "apps", app_id)
    os.makedirs(app_dir, exist_ok=True)
    return app_dir

def install_requirements(app_id, requirements):
    if not requirements:
        return True, "No requirements to install"
    
    app_dir = get_app_directory(app_id)
    req_file = os.path.join(app_dir, "requirements.txt")
    
    try:
        with open(req_file, 'w') as f:
            f.write('\n'.join(requirements))
        
        log_message(app_id, "INFO", f"Installing {len(requirements)} packages...")
        
        process = subprocess.Popen(
            [sys.executable, "-m", "pip", "install", "-r", req_file, "--upgrade"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=app_dir,
            text=True
        )
        
        for line in iter(process.stdout.readline, ''):
            if line:
                log_message(app_id, "INFO", line.strip())
        
        process.wait()
        
        if process.returncode == 0:
            log_message(app_id, "INFO", "✓ All packages installed successfully")
            return True, "Requirements installed successfully"
        else:
            log_message(app_id, "ERROR", "Package installation failed")
            return False, "Installation failed"
            
    except Exception as e:
        log_message(app_id, "ERROR", f"Installation error: {str(e)}")
        return False, f"Installation error: {str(e)}"

def log_message(app_id, level, message):
    log_entry = {
        "app_id": app_id,
        "timestamp": datetime.utcnow(),
        "level": level,
        "message": message
    }
    
    try:
        logs_col.insert_one(log_entry)
        cutoff = datetime.utcnow() - timedelta(days=LOG_RETENTION_DAYS)
        logs_col.delete_many({"app_id": app_id, "timestamp": {"$lt": cutoff}})
        
        total_logs = logs_col.count_documents({"app_id": app_id})
        if total_logs > MAX_LOGS_PER_APP:
            excess = total_logs - MAX_LOGS_PER_APP
            old_logs = logs_col.find({"app_id": app_id}).sort("timestamp", 1).limit(excess)
            logs_col.delete_many({"_id": {"$in": [log["_id"] for log in old_logs]}})
    except Exception as e:
        logger.error(f"Failed to save log: {e}")
    
    if app_id in log_queues:
        try:
            log_queues[app_id].put(log_entry, block=False)
        except:
            pass

# ═══════════════════════════════════════════════════════════════════════
# LOG CAPTURE THREAD
# ═══════════════════════════════════════════════════════════════════════

class LogCaptureThread(threading.Thread):
    def __init__(self, app_id, process):
        super().__init__(daemon=True)
        self.app_id = app_id
        self.process = process
        self.running = True
    
    def run(self):
        try:
            for line in self.process.stdout:
                if not self.running:
                    break
                line = line.strip()
                if line:
                    level = "INFO"
                    line_lower = line.lower()
                    if "error" in line_lower or "exception" in line_lower:
                        level = "ERROR"
                    elif "warning" in line_lower or "warn" in line_lower:
                        level = "WARNING"
                    elif "debug" in line_lower:
                        level = "DEBUG"
                    
                    log_message(self.app_id, level, line)
        except Exception as e:
            logger.error(f"Log capture error for {self.app_id}: {e}")
    
    def stop(self):
        self.running = False

# ═══════════════════════════════════════════════════════════════════════
# PROCESS MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════

def start_application(app_id):
    try:
        app = applications_col.find_one({"app_id": app_id})
        if not app:
            return False, "Application not found"
        
        if app_id in running_processes and running_processes[app_id].poll() is None:
            return False, "Application is already running"
        
        app_dir = get_app_directory(app_id)
        script_path = os.path.join(app_dir, "main.py")
        script_content = app['script']
        
        for key, value in app.get('env_vars', {}).items():
            script_content = script_content.replace(f"{{{{{key}}}}}", value)
        
        with open(script_path, 'w') as f:
            f.write(script_content)
        
        log_message(app_id, "INFO", f"Starting {app['app_name']}...")
        
        requirements = app.get('requirements', [])
        if requirements:
            success, msg = install_requirements(app_id, requirements)
            if not success:
                log_message(app_id, "ERROR", f"Failed to install requirements: {msg}")
                applications_col.update_one(
                    {"app_id": app_id},
                    {"$set": {"status": "crashed", "updated_at": datetime.utcnow()}}
                )
                return False, msg
        
        env = os.environ.copy()
        env.update(app.get('env_vars', {}))
        
        process = subprocess.Popen(
            [sys.executable, "-u", script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=app_dir,
            env=env,
            text=True,
            bufsize=1
        )
        
        running_processes[app_id] = process
        app_start_times[app_id] = datetime.utcnow()
        
        if app_id not in log_queues:
            log_queues[app_id] = Queue(maxsize=1000)
        
        log_thread = LogCaptureThread(app_id, process)
        log_thread.start()
        log_threads[app_id] = log_thread
        
        applications_col.update_one(
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
        
        log_message(app_id, "INFO", f"✓ Application started successfully (PID: {process.pid})")
        return True, "Application started successfully"
        
    except Exception as e:
        logger.error(f"Failed to start app {app_id}: {e}")
        log_message(app_id, "ERROR", f"Failed to start: {str(e)}")
        applications_col.update_one(
            {"app_id": app_id},
            {"$set": {"status": "crashed", "updated_at": datetime.utcnow()}}
        )
        return False, str(e)

def stop_application(app_id, force=False):
    try:
        if app_id not in running_processes:
            return False, "Application is not running"
        
        process = running_processes[app_id]
        log_message(app_id, "INFO", "Stopping application...")
        
        if app_id in log_threads:
            log_threads[app_id].stop()
        
        if not force:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log_message(app_id, "WARNING", "Graceful shutdown timeout, forcing kill...")
                process.kill()
                process.wait(timeout=5)
        else:
            process.kill()
            process.wait(timeout=5)
        
        del running_processes[app_id]
        if app_id in app_start_times:
            del app_start_times[app_id]
        
        applications_col.update_one(
            {"app_id": app_id},
            {"$set": {"status": "stopped", "pid": None, "updated_at": datetime.utcnow()}}
        )
        
        log_message(app_id, "INFO", "✓ Application stopped")
        return True, "Application stopped successfully"
        
    except Exception as e:
        logger.error(f"Failed to stop app {app_id}: {e}")
        return False, str(e)

def restart_application(app_id):
    log_message(app_id, "INFO", "Restarting application...")
    
    if app_id in running_processes:
        stop_application(app_id)
        time.sleep(2)
    
    applications_col.update_one({"app_id": app_id}, {"$inc": {"restart_count": 1}})
    return start_application(app_id)

def get_app_uptime(app_id):
    if app_id in app_start_times:
        delta = datetime.utcnow() - app_start_times[app_id]
        return int(delta.total_seconds())
    return 0

# ═══════════════════════════════════════════════════════════════════════
# MONITORING THREAD
# ═══════════════════════════════════════════════════════════════════════

def monitor_applications():
    while True:
        try:
            time.sleep(HEALTH_CHECK_INTERVAL)
            apps = applications_col.find({"status": "running"})
            
            for app in apps:
                app_id = app['app_id']
                
                if app_id in running_processes:
                    process = running_processes[app_id]
                    
                    if process.poll() is not None:
                        exit_code = process.returncode
                        log_message(app_id, "ERROR", f"Application crashed with exit code {exit_code}")
                        
                        applications_col.update_one(
                            {"app_id": app_id},
                            {"$set": {"status": "crashed", "updated_at": datetime.utcnow()}}
                        )
                        
                        if app.get('auto_restart', True):
                            restart_count = app.get('restart_count', 0)
                            
                            if restart_count < MAX_RESTART_ATTEMPTS:
                                log_message(app_id, "INFO", f"Auto-restarting... (attempt {restart_count + 1}/{MAX_RESTART_ATTEMPTS})")
                                time.sleep(5)
                                restart_application(app_id)
                            else:
                                log_message(app_id, "ERROR", f"Max restart attempts reached. Stopping auto-restart.")
                else:
                    applications_col.update_one(
                        {"app_id": app_id},
                        {"$set": {"status": "crashed", "updated_at": datetime.utcnow()}}
                    )
                    log_message(app_id, "ERROR", "Process not found. Marked as crashed.")
                    
        except Exception as e:
            logger.error(f"Monitor thread error: {e}")

monitor_thread = threading.Thread(target=monitor_applications, daemon=True)
monitor_thread.start()
logger.info("✓ Monitoring thread started")

# ═══════════════════════════════════════════════════════════════════════
# HTML TEMPLATES - Modern UI like Koyeb/Render
# ═══════════════════════════════════════════════════════════════════════

LOGIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sign In - CloudHost Platform</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .login-card {
            background: white;
            padding: 48px;
            border-radius: 20px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
            width: 100%;
            max-width: 440px;
        }
        .logo {
            text-align: center;
            margin-bottom: 32px;
        }
        .logo-icon {
            width: 64px;
            height: 64px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-radius: 16px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-size: 32px;
            margin-bottom: 16px;
        }
        h1 {
            font-size: 28px;
            font-weight: 700;
            color: #1a202c;
            margin-bottom: 8px;
            text-align: center;
        }
        .subtitle {
            color: #718096;
            text-align: center;
            margin-bottom: 32px;
            font-size: 15px;
        }
        .form-group {
            margin-bottom: 24px;
        }
        label {
            display: block;
            margin-bottom: 8px;
            color: #4a5568;
            font-weight: 500;
            font-size: 14px;
        }
        input {
            width: 100%;
            padding: 14px 16px;
            border: 2px solid #e2e8f0;
            border-radius: 10px;
            font-size: 15px;
            transition: all 0.2s;
            font-family: inherit;
        }
        input:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        .btn-primary {
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 10px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(102, 126, 234, 0.3);
        }
        .error {
            background: #fed7d7;
            color: #c53030;
            padding: 14px 16px;
            border-radius: 10px;
            margin-bottom: 24px;
            font-size: 14px;
            border-left: 4px solid #c53030;
        }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="logo">
            <div class="logo-icon">🚀</div>
            <h1>CloudHost Platform</h1>
            <p class="subtitle">Deploy and manage your applications with ease</p>
        </div>
        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}
        <form method="POST">
            <div class="form-group">
                <label>Username</label>
                <input type="text" name="username" required autofocus placeholder="Enter your username">
            </div>
            <div class="form-group">
                <label>Password</label>
                <input type="password" name="password" required placeholder="Enter your password">
            </div>
            <button type="submit" class="btn-primary">Sign In</button>
        </form>
    </div>
</body>
</html>
"""

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard - CloudHost Platform</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f7fafc;
            color: #2d3748;
        }
        
        /* Navbar */
        .navbar {
            background: white;
            border-bottom: 1px solid #e2e8f0;
            padding: 0 32px;
            height: 64px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            position: sticky;
            top: 0;
            z-index: 100;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        }
        
        .navbar-brand {
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 20px;
            font-weight: 700;
            color: #1a202c;
        }
        
        .logo-icon {
            width: 40px;
            height: 40px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 20px;
        }
        
        .nav-actions {
            display: flex;
            gap: 12px;
            align-items: center;
        }
        
        .btn {
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            font-size: 14px;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        
        .btn-primary {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
        }
        
        .btn-primary:hover {
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
        }
        
        .btn-secondary {
            background: #edf2f7;
            color: #4a5568;
        }
        
        .btn-secondary:hover {
            background: #e2e8f0;
        }
        
        /* Container */
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 32px;
        }
        
        /* Stats Grid */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 24px;
            margin-bottom: 32px;
        }
        
        .stat-card {
            background: white;
            padding: 24px;
            border-radius: 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
            border: 1px solid #e2e8f0;
            transition: all 0.2s;
        }
        
        .stat-card:hover {
            box-shadow: 0 4px 12px rgba(0,0,0,0.08);
            transform: translateY(-2px);
        }
        
        .stat-card .label {
            color: #718096;
            font-size: 13px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 8px;
        }
        
        .stat-card .value {
            font-size: 36px;
            font-weight: 700;
            color: #1a202c;
        }
        
        .stat-card .trend {
            font-size: 13px;
            color: #10b981;
            margin-top: 8px;
        }
        
        /* Applications Section */
        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
        }
        
        .section-title {
            font-size: 24px;
            font-weight: 700;
            color: #1a202c;
        }
        
        /* Apps Grid */
        .apps-grid {
            display: grid;
            gap: 20px;
        }
        
        .app-card {
            background: white;
            border-radius: 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
            border: 1px solid #e2e8f0;
            padding: 24px;
            transition: all 0.2s;
        }
        
        .app-card:hover {
            box-shadow: 0 4px 12px rgba(0,0,0,0.08);
        }
        
        .app-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 16px;
        }
        
        .app-info h3 {
            font-size: 18px;
            font-weight: 600;
            color: #1a202c;
            margin-bottom: 6px;
        }
        
        .app-type {
            font-size: 13px;
            color: #718096;
            font-weight: 500;
        }
        
        .status-badge {
            padding: 6px 14px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .status-badge.running {
            background: #d1fae5;
            color: #065f46;
        }
        
        .status-badge.stopped {
            background: #fee;
            color: #991b1b;
        }
        
        .status-badge.crashed {
            background: #fed7aa;
            color: #92400e;
        }
        
        .app-meta {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 16px;
            margin-bottom: 16px;
            padding-top: 16px;
            border-top: 1px solid #e2e8f0;
        }
        
        .meta-item {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }
        
        .meta-label {
            font-size: 12px;
            color: #718096;
            font-weight: 500;
        }
        
        .meta-value {
            font-size: 14px;
            color: #1a202c;
            font-weight: 600;
        }
        
        .app-actions {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }
        
        .btn-sm {
            padding: 8px 16px;
            font-size: 13px;
        }
        
        .btn-success { background: #10b981; color: white; }
        .btn-danger { background: #ef4444; color: white; }
        .btn-warning { background: #f59e0b; color: white; }
        .btn-info { background: #3b82f6; color: white; }
        .btn-edit { background: #8b5cf6; color: white; }
        
        .btn-success:hover { background: #059669; }
        .btn-danger:hover { background: #dc2626; }
        .btn-warning:hover { background: #d97706; }
        .btn-info:hover { background: #2563eb; }
        .btn-edit:hover { background: #7c3aed; }
        
        /* Modal */
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.6);
            z-index: 1000;
            align-items: center;
            justify-content: center;
            backdrop-filter: blur(4px);
        }
        
        .modal.active { display: flex; }
        
        .modal-content {
            background: white;
            border-radius: 20px;
            max-width: 900px;
            width: 90%;
            max-height: 90vh;
            overflow-y: auto;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
        }
        
        .modal-header {
            padding: 32px 32px 24px;
            border-bottom: 1px solid #e2e8f0;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .modal-title {
            font-size: 24px;
            font-weight: 700;
            color: #1a202c;
        }
        
        .close-btn {
            width: 40px;
            height: 40px;
            border-radius: 10px;
            border: none;
            background: #edf2f7;
            color: #4a5568;
            font-size: 20px;
            cursor: pointer;
            transition: all 0.2s;
        }
        
        .close-btn:hover {
            background: #e2e8f0;
        }
        
        .modal-body {
            padding: 32px;
        }
        
        /* Form */
        .form-group {
            margin-bottom: 24px;
        }
        
        .form-label {
            display: block;
            margin-bottom: 8px;
            color: #4a5568;
            font-weight: 600;
            font-size: 14px;
        }
        
        .form-input,
        .form-select,
        .form-textarea {
            width: 100%;
            padding: 12px 16px;
            border: 2px solid #e2e8f0;
            border-radius: 10px;
            font-size: 14px;
            font-family: inherit;
            transition: all 0.2s;
        }
        
        .form-textarea {
            min-height: 200px;
            font-family: 'Courier New', monospace;
            resize: vertical;
        }
        
        .form-input:focus,
        .form-select:focus,
        .form-textarea:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        .checkbox-group {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .checkbox-group input[type="checkbox"] {
            width: 20px;
            height: 20px;
            cursor: pointer;
        }
        
        /* Environment Variables */
        .env-vars {
            margin-top: 12px;
        }
        
        .env-var-row {
            display: grid;
            grid-template-columns: 1fr 1fr auto;
            gap: 12px;
            margin-bottom: 12px;
        }
        
        .btn-add {
            background: #10b981;
            color: white;
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
            margin-top: 8px;
        }
        
        .btn-remove {
            background: #ef4444;
            color: white;
            padding: 10px 16px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
        }
        
        /* Logs Viewer */
        .logs-container {
            background: #1a202c;
            border-radius: 12px;
            padding: 20px;
            max-height: 600px;
            overflow-y: auto;
            font-family: 'Courier New', monospace;
            font-size: 13px;
        }
        
        .log-line {
            margin-bottom: 6px;
            line-height: 1.6;
            white-space: pre-wrap;
            word-break: break-all;
        }
        
        .log-line .timestamp {
            color: #a0aec0;
            margin-right: 8px;
        }
        
        .log-line.info { color: #48bb78; }
        .log-line.error { color: #f56565; }
        .log-line.warning { color: #ed8936; }
        .log-line.debug { color: #cbd5e0; }
        
        /* Toast */
        .toast {
            position: fixed;
            bottom: 24px;
            right: 24px;
            background: white;
            padding: 16px 24px;
            border-radius: 12px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.2);
            z-index: 2000;
            display: none;
            min-width: 300px;
            border-left: 4px solid;
        }
        
        .toast.active {
            display: block;
            animation: slideIn 0.3s ease-out;
        }
        
        .toast.success {
            border-left-color: #10b981;
        }
        
        .toast.error {
            border-left-color: #ef4444;
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
        
        /* Empty State */
        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: #718096;
        }
        
        .empty-state-icon {
            font-size: 64px;
            margin-bottom: 16px;
        }
        
        .empty-state h3 {
            font-size: 20px;
            font-weight: 600;
            margin-bottom: 8px;
            color: #4a5568;
        }
        
        .empty-state p {
            font-size: 14px;
            margin-bottom: 24px;
        }
        
        /* Loading */
        .loading {
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 3px solid #e2e8f0;
            border-top-color: #667eea;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }
        
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        
        /* Responsive */
        @media (max-width: 768px) {
            .container { padding: 16px; }
            .stats-grid { grid-template-columns: 1fr; }
            .app-meta { grid-template-columns: 1fr; }
            .app-actions { flex-direction: column; }
            .btn { width: 100%; justify-content: center; }
        }
    </style>
</head>
<body>
    <div class="navbar">
        <div class="navbar-brand">
            <div class="logo-icon">🚀</div>
            <span>CloudHost</span>
        </div>
        <div class="nav-actions">
            <button class="btn btn-secondary" onclick="refreshApps()">
                🔄 Refresh
            </button>
            <button class="btn btn-secondary" onclick="logout()">
                Logout
            </button>
        </div>
    </div>
    
    <div class="container">
        <!-- Stats Grid -->
        <div class="stats-grid" id="statsGrid">
            <!-- Loaded via JS -->
        </div>
        
        <!-- Applications Section -->
        <div class="section-header">
            <h2 class="section-title">Applications</h2>
            <button class="btn btn-primary" onclick="openCreateModal()">
                + New Application
            </button>
        </div>
        
        <div class="apps-grid" id="appsGrid">
            <!-- Loaded via JS -->
        </div>
    </div>
    
    <!-- Create/Edit Modal -->
    <div class="modal" id="appModal">
        <div class="modal-content">
            <div class="modal-header">
                <h2 class="modal-title" id="modalTitle">Create New Application</h2>
                <button class="close-btn" onclick="closeModal('appModal')">×</button>
            </div>
            <div class="modal-body">
                <form id="appForm">
                    <input type="hidden" id="editAppId" value="">
                    
                    <div class="form-group">
                        <label class="form-label">Application Name</label>
                        <input type="text" class="form-input" id="appName" required placeholder="my-awesome-bot">
                    </div>
                    
                    <div class="form-group">
                        <label class="form-label">Application Type</label>
                        <select class="form-select" id="appType" onchange="loadTemplate()">
                            <option value="custom">Custom</option>
                            <option value="telegram_bot">Telegram Bot (python-telegram-bot)</option>
                            <option value="telegram_aiogram">Telegram Bot (aiogram)</option>
                            <option value="flask_api">Flask API</option>
                            <option value="fastapi">FastAPI</option>
                            <option value="discord_bot">Discord Bot</option>
                            <option value="worker">Background Worker</option>
                        </select>
                    </div>
                    
                    <div class="form-group">
                        <label class="form-label">Python Script</label>
                        <textarea class="form-textarea" id="appScript" required placeholder="Enter your Python code here..."></textarea>
                    </div>
                    
                    <div class="form-group">
                        <label class="form-label">Requirements (one per line)</label>
                        <textarea class="form-input" id="appRequirements" rows="6" placeholder="python-telegram-bot==20.7
requests==2.31.0
flask==3.0.0"></textarea>
                    </div>
                    
                    <div class="form-group">
                        <label class="form-label">Environment Variables</label>
                        <div id="envVars" class="env-vars">
                            <div class="env-var-row">
                                <input type="text" class="form-input env-key" placeholder="Key (e.g., BOT_TOKEN)">
                                <input type="text" class="form-input env-value" placeholder="Value">
                                <button type="button" class="btn-remove" onclick="removeEnvVar(this)">×</button>
                            </div>
                        </div>
                        <button type="button" class="btn-add" onclick="addEnvVar()">+ Add Variable</button>
                    </div>
                    
                    <div class="form-group">
                        <div class="checkbox-group">
                            <input type="checkbox" id="autoRestart" checked>
                            <label class="form-label" for="autoRestart" style="margin: 0;">Auto-restart on crash</label>
                        </div>
                    </div>
                    
                    <div class="form-group">
                        <div class="checkbox-group">
                            <input type="checkbox" id="autoStart" checked>
                            <label class="form-label" for="autoStart" style="margin: 0;">Auto-start on server boot</label>
                        </div>
                    </div>
                    
                    <button type="submit" class="btn btn-primary" style="width: 100%;">
                        <span id="submitBtnText">Create & Deploy</span>
                    </button>
                </form>
            </div>
        </div>
    </div>
    
    <!-- Logs Modal -->
    <div class="modal" id="logsModal">
        <div class="modal-content">
            <div class="modal-header">
                <h2 class="modal-title">Application Logs</h2>
                <button class="close-btn" onclick="closeLogsModal()">×</button>
            </div>
            <div class="modal-body">
                <div class="logs-container" id="logsContainer">
                    <div style="color: #a0aec0;">Loading logs...</div>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Toast Notification -->
    <div class="toast" id="toast"></div>
    
    <script>
        let logsEventSource = null;
        let currentEditingApp = null;
        
        // Application Templates
        const templates = {
            telegram_bot: {
                script: `from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = "{{BOT_TOKEN}}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! I'm running on CloudHost! 🚀")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Available commands:\\n/start - Start bot\\n/help - Show help")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    print("✓ Bot started successfully!")
    app.run_polling()

if __name__ == "__main__":
    main()`,
                requirements: `python-telegram-bot==20.7`
            },
            telegram_aiogram: {
                script: `from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import asyncio

BOT_TOKEN = "{{BOT_TOKEN}}"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start_command(message: types.Message):
    await message.answer("Hello! I'm running on CloudHost! 🚀")

@dp.message(Command("help"))
async def help_command(message: types.Message):
    await message.answer("Available commands:\\n/start - Start bot\\n/help - Show help")

async def main():
    print("✓ Bot started successfully!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())`,
                requirements: `aiogram==3.3.0`
            },
            flask_api: {
                script: `from flask import Flask, jsonify
import os

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        "status": "running",
        "message": "API is live on CloudHost! 🚀",
        "version": "1.0.0"
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

@app.route('/api/data')
def data():
    return jsonify({
        "data": [1, 2, 3, 4, 5],
        "message": "Sample data endpoint"
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"✓ Flask API started on port {port}")
    app.run(host='0.0.0.0', port=port)`,
                requirements: `flask==3.0.0
flask-cors==4.0.0`
            },
            fastapi: {
                script: `from fastapi import FastAPI
import uvicorn
import os

app = FastAPI(title="My API", version="1.0.0")

@app.get("/")
def home():
    return {"status": "running", "message": "FastAPI is live on CloudHost! 🚀"}

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.get("/api/data")
def data():
    return {"data": [1, 2, 3, 4, 5], "message": "Sample data"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"✓ FastAPI started on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)`,
                requirements: `fastapi==0.109.0
uvicorn==0.27.0`
            },
            discord_bot: {
                script: `import discord
from discord.ext import commands

BOT_TOKEN = "{{BOT_TOKEN}}"

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'✓ {bot.user} connected to Discord!')

@bot.command(name='hello')
async def hello(ctx):
    await ctx.send('Hello! I am running on CloudHost! 🚀')

@bot.command(name='ping')
async def ping(ctx):
    await ctx.send(f'Pong! Latency: {round(bot.latency * 1000)}ms')

bot.run(BOT_TOKEN)`,
                requirements: `discord.py==2.3.2`
            },
            worker: {
                script: `import time
from datetime import datetime

def worker_task():
    print("✓ Worker started successfully!")
    
    counter = 0
    while True:
        counter += 1
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Task #{counter} executed")
        
        # Your background task logic here
        # Example: process queue, send notifications, cleanup, etc.
        
        time.sleep(60)  # Run every 60 seconds

if __name__ == "__main__":
    worker_task()`,
                requirements: ``
            },
            custom: {
                script: `# Enter your custom Python code here
import time

print("✓ Application started!")

while True:
    print("Running...")
    time.sleep(60)`,
                requirements: ``
            }
        };
        
        function loadTemplate() {
            const type = document.getElementById('appType').value;
            const template = templates[type];
            if (template) {
                document.getElementById('appScript').value = template.script;
                document.getElementById('appRequirements').value = template.requirements;
            }
        }
        
        function addEnvVar() {
            const container = document.getElementById('envVars');
            const row = document.createElement('div');
            row.className = 'env-var-row';
            row.innerHTML = `
                <input type="text" class="form-input env-key" placeholder="Key">
                <input type="text" class="form-input env-value" placeholder="Value">
                <button type="button" class="btn-remove" onclick="removeEnvVar(this)">×</button>
            `;
            container.appendChild(row);
        }
        
        function removeEnvVar(btn) {
            btn.parentElement.remove();
        }
        
        function showToast(message, type = 'success') {
            const toast = document.getElementById('toast');
            toast.textContent = message;
            toast.className = `toast ${type} active`;
            setTimeout(() => toast.classList.remove('active'), 4000);
        }
        
        function formatUptime(seconds) {
            if (seconds < 60) return `${seconds}s`;
            if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
            if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
            return `${Math.floor(seconds / 86400)}d`;
        }
        
        function formatDate(dateStr) {
            const date = new Date(dateStr);
            const now = new Date();
            const diff = now - date;
            const days = Math.floor(diff / 86400000);
            
            if (days === 0) return 'Today';
            if (days === 1) return 'Yesterday';
            if (days < 7) return `${days} days ago`;
            return date.toLocaleDateString();
        }
        
        async function loadStats() {
            try {
                const response = await fetch('/api/stats');
                const stats = await response.json();
                
                document.getElementById('statsGrid').innerHTML = `
                    <div class="stat-card">
                        <div class="label">Total Applications</div>
                        <div class="value">${stats.total_apps}</div>
                    </div>
                    <div class="stat-card">
                        <div class="label">Running</div>
                        <div class="value" style="color: #10b981">${stats.running_apps}</div>
                        <div class="trend">● Active</div>
                    </div>
                    <div class="stat-card">
                        <div class="label">Stopped</div>
                        <div class="value" style="color: #ef4444">${stats.stopped_apps}</div>
                    </div>
                    <div class="stat-card">
                        <div class="label">Total Logs</div>
                        <div class="value" style="color: #3b82f6">${stats.total_logs.toLocaleString()}</div>
                    </div>
                `;
            } catch (error) {
                console.error('Failed to load stats:', error);
            }
        }
        
        async function loadApps() {
            try {
                const response = await fetch('/api/apps');
                const apps = await response.json();
                
                const grid = document.getElementById('appsGrid');
                
                if (apps.length === 0) {
                    grid.innerHTML = `
                        <div class="empty-state">
                            <div class="empty-state-icon">📦</div>
                            <h3>No applications yet</h3>
                            <p>Create your first application to get started</p>
                            <button class="btn btn-primary" onclick="openCreateModal()">+ New Application</button>
                        </div>
                    `;
                    return;
                }
                
                grid.innerHTML = apps.map(app => `
                    <div class="app-card">
                        <div class="app-header">
                            <div class="app-info">
                                <h3>${app.app_name}</h3>
                                <div class="app-type">${app.app_type.replace('_', ' ').toUpperCase()}</div>
                            </div>
                            <span class="status-badge ${app.status}">${app.status.toUpperCase()}</span>
                        </div>
                        
                        <div class="app-meta">
                            <div class="meta-item">
                                <div class="meta-label">Uptime</div>
                                <div class="meta-value">${formatUptime(app.uptime_seconds)}</div>
                            </div>
                            <div class="meta-item">
                                <div class="meta-label">Restarts</div>
                                <div class="meta-value">${app.restart_count || 0}</div>
                            </div>
                            <div class="meta-item">
                                <div class="meta-label">Created</div>
                                <div class="meta-value">${formatDate(app.created_at)}</div>
                            </div>
                        </div>
                        
                        <div class="app-actions">
                            ${app.status !== 'running' ? 
                                `<button class="btn btn-sm btn-success" onclick="startApp('${app.app_id}')">▶ Start</button>` : 
                                `<button class="btn btn-sm btn-danger" onclick="stopApp('${app.app_id}')">■ Stop</button>`
                            }
                            <button class="btn btn-sm btn-warning" onclick="restartApp('${app.app_id}')">🔄 Restart</button>
                            <button class="btn btn-sm btn-edit" onclick="editApp('${app.app_id}')">✏️ Edit</button>
                            <button class="btn btn-sm btn-info" onclick="viewLogs('${app.app_id}')">📄 Logs</button>
                            <button class="btn btn-sm btn-secondary" onclick="deleteApp('${app.app_id}')">🗑️ Delete</button>
                        </div>
                    </div>
                `).join('');
            } catch (error) {
                console.error('Failed to load apps:', error);
                showToast('Failed to load applications', 'error');
            }
        }
        
        async function refreshApps() {
            await loadStats();
            await loadApps();
            showToast('Refreshed successfully');
        }
        
        function openCreateModal() {
            currentEditingApp = null;
            document.getElementById('modalTitle').textContent = 'Create New Application';
            document.getElementById('submitBtnText').textContent = 'Create & Deploy';
            document.getElementById('editAppId').value = '';
            document.getElementById('appForm').reset();
            
            // Reset env vars to one row
            document.getElementById('envVars').innerHTML = `
                <div class="env-var-row">
                    <input type="text" class="form-input env-key" placeholder="Key (e.g., BOT_TOKEN)">
                    <input type="text" class="form-input env-value" placeholder="Value">
                    <button type="button" class="btn-remove" onclick="removeEnvVar(this)">×</button>
                </div>
            `;
            
            document.getElementById('appModal').classList.add('active');
        }
        
        async function editApp(appId) {
            try {
                const response = await fetch(`/api/apps/${appId}`);
                const app = await response.json();
                
                currentEditingApp = appId;
                document.getElementById('modalTitle').textContent = 'Edit Application';
                document.getElementById('submitBtnText').textContent = 'Update Application';
                document.getElementById('editAppId').value = appId;
                
                document.getElementById('appName').value = app.app_name;
                document.getElementById('appType').value = app.app_type;
                document.getElementById('appScript').value = app.script;
                document.getElementById('appRequirements').value = (app.requirements || []).join('\n');
                document.getElementById('autoRestart').checked = app.auto_restart;
                document.getElementById('autoStart').checked = app.auto_start;
                
                // Load env vars
                const envVarsContainer = document.getElementById('envVars');
                envVarsContainer.innerHTML = '';
                
                const envVars = app.env_vars || {};
                if (Object.keys(envVars).length === 0) {
                    envVarsContainer.innerHTML = `
                        <div class="env-var-row">
                            <input type="text" class="form-input env-key" placeholder="Key">
                            <input type="text" class="form-input env-value" placeholder="Value">
                            <button type="button" class="btn-remove" onclick="removeEnvVar(this)">×</button>
                        </div>
                    `;
                } else {
                    Object.entries(envVars).forEach(([key, value]) => {
                        const row = document.createElement('div');
                        row.className = 'env-var-row';
                        row.innerHTML = `
                            <input type="text" class="form-input env-key" placeholder="Key" value="${key}">
                            <input type="text" class="form-input env-value" placeholder="Value" value="${value}">
                            <button type="button" class="btn-remove" onclick="removeEnvVar(this)">×</button>
                        `;
                        envVarsContainer.appendChild(row);
                    });
                }
                
                document.getElementById('appModal').classList.add('active');
            } catch (error) {
                showToast('Failed to load application', 'error');
            }
        }
        
        function closeModal(id) {
            document.getElementById(id).classList.remove('active');
        }
        
        document.getElementById('appForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            
            const appId = document.getElementById('editAppId').value;
            const appName = document.getElementById('appName').value;
            const appType = document.getElementById('appType').value;
            const script = document.getElementById('appScript').value;
            const requirementsText = document.getElementById('appRequirements').value;
            const autoRestart = document.getElementById('autoRestart').checked;
            const autoStart = document.getElementById('autoStart').checked;
            
            // Collect env vars
            const envVars = {};
            document.querySelectorAll('.env-var-row').forEach(row => {
                const key = row.querySelector('.env-key').value.trim();
                const value = row.querySelector('.env-value').value.trim();
                if (key && value) {
                    envVars[key] = value;
                }
            });
            
            // Collect requirements
            const requirements = requirementsText
                .split('\n')
                .map(r => r.trim())
                .filter(r => r);
            
            const data = {
                app_name: appName,
                app_type: appType,
                script: script,
                requirements: requirements,
                env_vars: envVars,
                auto_restart: autoRestart,
                auto_start: autoStart
            };
            
            try {
                const url = appId ? `/api/apps/${appId}` : '/api/apps/create';
                const method = appId ? 'PUT' : 'POST';
                
                const response = await fetch(url, {
                    method: method,
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                });
                
                const result = await response.json();
                
                if (response.ok) {
                    showToast(appId ? 'Application updated!' : 'Application created!');
                    closeModal('appModal');
                    refreshApps();
                } else {
                    showToast(result.error || 'Operation failed', 'error');
                }
            } catch (error) {
                showToast('Error: ' + error.message, 'error');
            }
        });
        
        async function startApp(appId) {
            try {
                const response = await fetch(`/api/apps/${appId}/start`, {method: 'POST'});
                const result = await response.json();
                showToast(result.message || result.error, response.ok ? 'success' : 'error');
                setTimeout(refreshApps, 1000);
            } catch (error) {
                showToast('Error: ' + error.message, 'error');
            }
        }
        
        async function stopApp(appId) {
            try {
                const response = await fetch(`/api/apps/${appId}/stop`, {method: 'POST'});
                const result = await response.json();
                showToast(result.message || result.error, response.ok ? 'success' : 'error');
                setTimeout(refreshApps, 1000);
            } catch (error) {
                showToast('Error: ' + error.message, 'error');
            }
        }
        
        async function restartApp(appId) {
            try {
                showToast('Restarting application...');
                const response = await fetch(`/api/apps/${appId}/restart`, {method: 'POST'});
                const result = await response.json();
                showToast(result.message || result.error, response.ok ? 'success' : 'error');
                setTimeout(refreshApps, 1000);
            } catch (error) {
                showToast('Error: ' + error.message, 'error');
            }
        }
        
        async function deleteApp(appId) {
            if (!confirm('Are you sure you want to delete this application? This action cannot be undone.')) return;
            
            try {
                const response = await fetch(`/api/apps/${appId}`, {method: 'DELETE'});
                const result = await response.json();
                showToast(result.message || result.error, response.ok ? 'success' : 'error');
                refreshApps();
            } catch (error) {
                showToast('Error: ' + error.message, 'error');
            }
        }
        
        function viewLogs(appId) {
            document.getElementById('logsModal').classList.add('active');
            document.getElementById('logsContainer').innerHTML = '<div style="color: #a0aec0;">Loading logs...</div>';
            
            // Close existing connection
            if (logsEventSource) {
                logsEventSource.close();
            }
            
            // Open SSE connection
            logsEventSource = new EventSource(`/api/apps/${appId}/logs/stream`);
            
            logsEventSource.onmessage = (event) => {
                const log = JSON.parse(event.data);
                const logLine = document.createElement('div');
                logLine.className = `log-line ${log.level.toLowerCase()}`;
                
                const timestamp = new Date(log.timestamp).toLocaleTimeString();
                logLine.innerHTML = `<span class="timestamp">[${timestamp}]</span> [${log.level}] ${log.message}`;
                
                const container = document.getElementById('logsContainer');
                if (container.children[0]?.textContent === 'Loading logs...') {
                    container.innerHTML = '';
                }
                container.appendChild(logLine);
                container.scrollTop = container.scrollHeight;
            };
            
            logsEventSource.onerror = () => {
                console.error('SSE connection error');
            };
        }
        
        function closeLogsModal() {
            if (logsEventSource) {
                logsEventSource.close();
                logsEventSource = null;
            }
            closeModal('logsModal');
        }
        
        function logout() {
            if (confirm('Are you sure you want to logout?')) {
                window.location.href = '/logout';
            }
        }
        
        // Initialize
        loadStats();
        loadApps();
        
        // Auto-refresh every 30 seconds
        setInterval(refreshApps, 30000);
    </script>
</body>
</html>
"""

# ═══════════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════════════════

@app.route('/')
@login_required
def dashboard():
    return render_template_string(DASHBOARD_HTML)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        else:
            return render_template_string(LOGIN_HTML, error="Invalid credentials")
    
    return render_template_string(LOGIN_HTML)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

# ═══════════════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════════════

@app.route('/api/stats')
@login_required
def api_stats():
    total_apps = applications_col.count_documents({})
    running_apps = applications_col.count_documents({"status": "running"})
    stopped_apps = applications_col.count_documents({"status": {"$in": ["stopped", "crashed"]}})
    total_logs = logs_col.count_documents({})
    
    return jsonify({
        "total_apps": total_apps,
        "running_apps": running_apps,
        "stopped_apps": stopped_apps,
        "total_logs": total_logs
    })

@app.route('/api/apps')
@login_required
def api_list_apps():
    apps = list(applications_col.find({}, {"_id": 0}))
    
    for app in apps:
        app['uptime_seconds'] = get_app_uptime(app['app_id']) if app['status'] == 'running' else 0
        if 'created_at' in app:
            app['created_at'] = app['created_at'].isoformat()
        if 'updated_at' in app:
            app['updated_at'] = app['updated_at'].isoformat()
        if 'last_started' in app and app['last_started']:
            app['last_started'] = app['last_started'].isoformat()
    
    return jsonify(apps)

@app.route('/api/apps/create', methods=['POST'])
@login_required
def api_create_app():
    try:
        data = request.json
        
        if not data.get('app_name') or not data.get('script'):
            return jsonify({"error": "app_name and script are required"}), 400
        
        is_valid, msg = validate_python_code(data['script'])
        if not is_valid:
            return jsonify({"error": msg}), 400
        
        app_id = str(uuid.uuid4())[:8]
        
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
        
        applications_col.insert_one(app_doc)
        log_queues[app_id] = Queue(maxsize=1000)
        
        logger.info(f"Created application: {app_id} - {data['app_name']}")
        
        return jsonify({
            "message": "Application created successfully",
            "app_id": app_id
        })
        
    except Exception as e:
        logger.error(f"Failed to create app: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/apps/<app_id>', methods=['GET'])
@login_required
def api_get_app(app_id):
    app = applications_col.find_one({"app_id": app_id}, {"_id": 0})
    if not app:
        return jsonify({"error": "Application not found"}), 404
    
    if 'created_at' in app:
        app['created_at'] = app['created_at'].isoformat()
    if 'updated_at' in app:
        app['updated_at'] = app['updated_at'].isoformat()
    if 'last_started' in app and app['last_started']:
        app['last_started'] = app['last_started'].isoformat()
    
    app['uptime_seconds'] = get_app_uptime(app_id) if app['status'] == 'running' else 0
    
    return jsonify(app)

@app.route('/api/apps/<app_id>', methods=['PUT'])
@login_required
def api_update_app(app_id):
    try:
        data = request.json
        
        app = applications_col.find_one({"app_id": app_id})
        if not app:
            return jsonify({"error": "Application not found"}), 404
        
        # Validate script if provided
        if 'script' in data:
            is_valid, msg = validate_python_code(data['script'])
            if not is_valid:
                return jsonify({"error": msg}), 400
        
        # Prepare update data
        update_data = {
            "updated_at": datetime.utcnow()
        }
        
        if 'app_name' in data:
            update_data['app_name'] = data['app_name']
        if 'app_type' in data:
            update_data['app_type'] = data['app_type']
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
            {"app_id": app_id},
            {"$set": update_data}
        )
        
        logger.info(f"Updated application: {app_id}")
        
        # If app is running, suggest restart
        if app['status'] == 'running':
            return jsonify({
                "message": "Application updated successfully. Restart for changes to take effect.",
                "needs_restart": True
            })
        
        return jsonify({"message": "Application updated successfully"})
        
    except Exception as e:
        logger.error(f"Failed to update app {app_id}: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/apps/<app_id>', methods=['DELETE'])
@login_required
def api_delete_app(app_id):
    try:
        if app_id in running_processes:
            stop_application(app_id, force=True)
        
        result = applications_col.delete_one({"app_id": app_id})
        if result.deleted_count == 0:
            return jsonify({"error": "Application not found"}), 404
        
        logs_col.delete_many({"app_id": app_id})
        
        import shutil
        app_dir = get_app_directory(app_id)
        if os.path.exists(app_dir):
            shutil.rmtree(app_dir)
        
        if app_id in log_queues:
            del log_queues[app_id]
        
        logger.info(f"Deleted application: {app_id}")
        
        return jsonify({"message": "Application deleted successfully"})
        
    except Exception as e:
        logger.error(f"Failed to delete app {app_id}: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/apps/<app_id>/start', methods=['POST'])
@login_required
def api_start_app(app_id):
    success, message = start_application(app_id)
    if success:
        return jsonify({"message": message})
    else:
        return jsonify({"error": message}), 500

@app.route('/api/apps/<app_id>/stop', methods=['POST'])
@login_required
def api_stop_app(app_id):
    success, message = stop_application(app_id)
    if success:
        return jsonify({"message": message})
    else:
        return jsonify({"error": message}), 500

@app.route('/api/apps/<app_id>/restart', methods=['POST'])
@login_required
def api_restart_app(app_id):
    success, message = restart_application(app_id)
    if success:
        return jsonify({"message": message})
    else:
        return jsonify({"error": message}), 500

@app.route('/api/apps/<app_id>/logs')
@login_required
def api_get_logs(app_id):
    try:
        limit = int(request.args.get('limit', 100))
        logs = list(logs_col.find(
            {"app_id": app_id},
            {"_id": 0}
        ).sort("timestamp", -1).limit(limit))
        
        for log in logs:
            log['timestamp'] = log['timestamp'].isoformat()
        
        logs.reverse()
        return jsonify(logs)
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/apps/<app_id>/logs/stream')
def api_stream_logs(app_id):
    """Server-Sent Events for live log streaming"""
    
    def generate():
        # Send initial logs
        logs = list(logs_col.find(
            {"app_id": app_id},
            {"_id": 0}
        ).sort("timestamp", -1).limit(100))
        
        for log in reversed(logs):
            log['timestamp'] = log['timestamp'].isoformat()
            yield f"data: {json.dumps(log)}\n\n"
        
        # Stream new logs
        if app_id not in log_queues:
            log_queues[app_id] = Queue(maxsize=1000)
        
        queue = log_queues[app_id]
        
        while True:
            try:
                log = queue.get(timeout=30)
                log['timestamp'] = log['timestamp'].isoformat()
                yield f"data: {json.dumps(log)}\n\n"
            except Empty:
                yield f": heartbeat\n\n"
            except GeneratorExit:
                break
    
    return Response(generate(), mimetype='text/event-stream')

# ═══════════════════════════════════════════════════════════════════════
# STARTUP & SHUTDOWN
# ═══════════════════════════════════════════════════════════════════════

def startup():
    """Initialize platform on startup"""
    logger.info("=" * 70)
    logger.info("🚀 Starting CloudHost Platform")
    logger.info("=" * 70)
    
    apps = applications_col.find({})
    app_count = 0
    
    for app in apps:
        app_id = app['app_id']
        log_queues[app_id] = Queue(maxsize=1000)
        app_count += 1
        
        if app.get('auto_start', False) and app.get('status') != 'running':
            logger.info(f"Auto-starting: {app['app_name']}")
            start_application(app_id)
    
    logger.info(f"✓ Loaded {app_count} applications")
    logger.info("✓ Platform started successfully")
    logger.info("=" * 70)

def shutdown():
    """Gracefully shutdown platform"""
    logger.info("Shutting down platform...")
    
    for app_id in list(running_processes.keys()):
        stop_application(app_id)
    
    mongo_client.close()
    logger.info("✓ Platform shutdown complete")

def signal_handler(sig, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {sig}")
    shutdown()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ═══════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    startup()
    
    port = int(os.environ.get("PORT", 5000))
    
    app.run(
        host='0.0.0.0',
        port=port,
        debug=False,
        threaded=True
    )
