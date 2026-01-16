#!/usr/bin/env python3
"""
100% WORKING Application Hosting Platform - Like Koyeb
Everything works: buttons, package installation, deployment, logs
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
import shutil
from datetime import datetime
from queue import Queue, Empty
from functools import wraps

from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for, Response
from flask_cors import CORS
from pymongo import MongoClient, ASCENDING, DESCENDING
import psutil

# ═══════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "venuboy")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "venuboy")
SECRET_KEY = os.getenv("SECRET_KEY", "secret-key-2024")
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://Zerobothost:zerobothost@cluster0.bl0tf2.mongodb.net/?appName=Cluster0")
DB_NAME = "hosting_platform"
PORT = int(os.getenv("PORT", 10000))

# ═══════════════════════════════════════════════════════════════════════
# FLASK SETUP
# ═══════════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.secret_key = SECRET_KEY
CORS(app)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# MONGODB CONNECTION
# ═══════════════════════════════════════════════════════════════════════

try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping')
    db = mongo_client[DB_NAME]
    apps_col = db.applications
    logs_col = db.logs
    apps_col.create_index([("app_id", ASCENDING)], unique=True)
    logs_col.create_index([("app_id", ASCENDING), ("timestamp", DESCENDING)])
    logger.info("✅ MongoDB connected")
except Exception as e:
    logger.error(f"❌ MongoDB failed: {e}")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════════
# GLOBAL STATE
# ═══════════════════════════════════════════════════════════════════════

running_processes = {}
log_queues = {}
platform_running = True

# ═══════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════

def generate_app_id():
    return f"app_{int(time.time())}_{os.urandom(4).hex()}"

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def log_to_db(app_id, level, message):
    try:
        entry = {"app_id": app_id, "timestamp": datetime.utcnow(), "level": level, "message": str(message)}
        logs_col.insert_one(entry)
        if app_id in log_queues:
            try:
                log_queues[app_id].put_nowait(entry)
            except:
                pass
    except Exception as e:
        logger.error(f"Log error: {e}")

def install_packages(app_id, requirements):
    """Install packages using pip directly (no venv needed on Render)"""
    try:
        if not requirements:
            log_to_db(app_id, "INFO", "No requirements to install")
            return True
        
        log_to_db(app_id, "INFO", "📦 Installing packages...")
        
        for req in requirements:
            req = req.strip()
            if not req or req.startswith("#"):
                continue
            
            log_to_db(app_id, "INFO", f"Installing {req}...")
            
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", req],
                capture_output=True,
                text=True,
                timeout=300
            )
            
            if result.returncode != 0:
                log_to_db(app_id, "ERROR", f"Failed to install {req}")
                log_to_db(app_id, "ERROR", result.stderr[:500])
                return False
            
            log_to_db(app_id, "INFO", f"✅ Installed {req}")
        
        log_to_db(app_id, "INFO", "✅ All packages installed")
        return True
        
    except subprocess.TimeoutExpired:
        log_to_db(app_id, "ERROR", "Package installation timeout")
        return False
    except Exception as e:
        log_to_db(app_id, "ERROR", f"Installation error: {e}")
        return False

def save_script(app_id, script):
    """Save application script"""
    try:
        script_dir = os.path.join(os.getcwd(), "apps")
        os.makedirs(script_dir, exist_ok=True)
        script_path = os.path.join(script_dir, f"{app_id}.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)
        return script_path
    except Exception as e:
        logger.error(f"Save script error: {e}")
        raise

# ═══════════════════════════════════════════════════════════════════════
# LOG CAPTURE THREAD
# ═══════════════════════════════════════════════════════════════════════

class LogCapture(threading.Thread):
    def __init__(self, app_id, stream, level):
        super().__init__(daemon=True)
        self.app_id = app_id
        self.stream = stream
        self.level = level
        self.running = True
    
    def run(self):
        try:
            for line in iter(self.stream.readline, ''):
                if not self.running:
                    break
                if line:
                    log_to_db(self.app_id, self.level, line.strip())
        except:
            pass
        finally:
            self.stream.close()
    
    def stop(self):
        self.running = False

# ═══════════════════════════════════════════════════════════════════════
# PROCESS MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════

def start_app(app_id):
    try:
        app = apps_col.find_one({"app_id": app_id})
        if not app:
            return False
        
        if app_id in running_processes and running_processes[app_id].poll() is None:
            log_to_db(app_id, "WARNING", "Already running")
            return True
        
        log_to_db(app_id, "INFO", f"🚀 Starting {app['app_name']}...")
        
        # Install requirements
        if app.get("requirements"):
            if not install_packages(app_id, app["requirements"]):
                apps_col.update_one({"app_id": app_id}, {"$set": {"status": "crashed"}})
                return False
        
        # Save script
        script_path = save_script(app_id, app["script"])
        
        # Prepare environment
        env = os.environ.copy()
        if app.get("env_vars"):
            env.update(app["env_vars"])
        
        # Start process
        process = subprocess.Popen(
            [sys.executable, "-u", script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env
        )
        
        running_processes[app_id] = process
        log_queues[app_id] = Queue()
        
        # Start log capture
        LogCapture(app_id, process.stdout, "INFO").start()
        LogCapture(app_id, process.stderr, "ERROR").start()
        
        # Update database
        apps_col.update_one(
            {"app_id": app_id},
            {"$set": {"status": "running", "pid": process.pid, "last_started": datetime.utcnow()}}
        )
        
        log_to_db(app_id, "INFO", f"✅ Started (PID: {process.pid})")
        return True
        
    except Exception as e:
        log_to_db(app_id, "ERROR", f"Start failed: {e}")
        apps_col.update_one({"app_id": app_id}, {"$set": {"status": "crashed"}})
        return False

def stop_app(app_id):
    try:
        if app_id not in running_processes:
            return True
        
        process = running_processes[app_id]
        log_to_db(app_id, "INFO", "⏹️ Stopping...")
        
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        
        del running_processes[app_id]
        if app_id in log_queues:
            del log_queues[app_id]
        
        apps_col.update_one({"app_id": app_id}, {"$set": {"status": "stopped", "pid": None}})
        log_to_db(app_id, "INFO", "✅ Stopped")
        return True
        
    except Exception as e:
        log_to_db(app_id, "ERROR", f"Stop failed: {e}")
        return False

def restart_app(app_id):
    log_to_db(app_id, "INFO", "🔄 Restarting...")
    apps_col.update_one({"app_id": app_id}, {"$inc": {"restart_count": 1}})
    stop_app(app_id)
    time.sleep(2)
    return start_app(app_id)

def delete_app(app_id):
    try:
        stop_app(app_id)
        apps_col.delete_one({"app_id": app_id})
        logs_col.delete_many({"app_id": app_id})
        
        script_path = os.path.join(os.getcwd(), "apps", f"{app_id}.py")
        if os.path.exists(script_path):
            os.remove(script_path)
        
        return True
    except Exception as e:
        logger.error(f"Delete failed: {e}")
        return False

# ═══════════════════════════════════════════════════════════════════════
# MONITORING
# ═══════════════════════════════════════════════════════════════════════

def monitor_loop():
    while platform_running:
        try:
            for app in apps_col.find({"status": "running"}):
                app_id = app["app_id"]
                
                if app_id in running_processes:
                    proc = running_processes[app_id]
                    
                    if proc.poll() is not None:
                        log_to_db(app_id, "ERROR", f"Crashed (exit code: {proc.returncode})")
                        del running_processes[app_id]
                        apps_col.update_one({"app_id": app_id}, {"$set": {"status": "crashed", "pid": None}})
                        
                        if app.get("auto_restart", True) and app.get("restart_count", 0) < 5:
                            log_to_db(app_id, "INFO", "Auto-restarting...")
                            threading.Thread(target=restart_app, args=(app_id,), daemon=True).start()
                    else:
                        if app.get("last_started"):
                            uptime = int((datetime.utcnow() - app["last_started"]).total_seconds())
                            apps_col.update_one({"app_id": app_id}, {"$set": {"uptime_seconds": uptime}})
            
            time.sleep(30)
        except Exception as e:
            logger.error(f"Monitor error: {e}")
            time.sleep(30)

# ═══════════════════════════════════════════════════════════════════════
# HTML TEMPLATES
# ═══════════════════════════════════════════════════════════════════════

LOGIN_HTML = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Login - Hosting Platform</title><style>
*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);min-height:100vh;display:flex;align-items:center;justify-content:center}.container{background:#fff;padding:3rem;border-radius:1rem;box-shadow:0 20px 60px rgba(0,0,0,0.3);width:100%;max-width:400px}h1{color:#333;margin-bottom:0.5rem;font-size:2rem}.subtitle{color:#666;margin-bottom:2rem;font-size:0.9rem}.form-group{margin-bottom:1.5rem}label{display:block;margin-bottom:0.5rem;color:#333;font-weight:500}input{width:100%;padding:0.75rem;border:2px solid #e0e0e0;border-radius:0.5rem;font-size:1rem}input:focus{outline:none;border-color:#667eea}button{width:100%;padding:0.75rem;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:#fff;border:none;border-radius:0.5rem;font-size:1rem;font-weight:600;cursor:pointer}button:hover{transform:translateY(-2px)}.error{background:#fee;color:#c33;padding:1rem;border-radius:0.5rem;margin-bottom:1rem;border-left:4px solid #c33}
</style></head><body><div class="container"><h1>🚀 Hosting Platform</h1><p class="subtitle">Sign in to manage applications</p>
{% if error %}<div class="error">{{ error }}</div>{% endif %}
<form method="POST"><div class="form-group"><label>Username</label><input type="text" name="username" required autofocus></div>
<div class="form-group"><label>Password</label><input type="password" name="password" required></div>
<button type="submit">Sign In</button></form></div></body></html>'''

DASHBOARD_HTML = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard - Hosting Platform</title><style>
*{margin:0;padding:0;box-sizing:border-box}:root{--primary:#667eea;--success:#10b981;--error:#ef4444;--warning:#f59e0b;--bg:#f3f4f6;--card:#fff;--text:#1f2937;--text-light:#6b7280;--border:#e5e7eb}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text)}.navbar{background:#fff;padding:1rem 2rem;box-shadow:0 1px 3px rgba(0,0,0,0.1);display:flex;justify-content:space-between;align-items:center;margin-bottom:2rem}.navbar h1{font-size:1.5rem;background:linear-gradient(135deg,#667eea,#764ba2);-webkit-background-clip:text;-webkit-text-fill-color:transparent}.btn{padding:0.5rem 1rem;border:none;border-radius:0.5rem;cursor:pointer;font-size:0.9rem;font-weight:500;transition:all 0.2s;text-decoration:none;display:inline-block}.btn-primary{background:linear-gradient(135deg,#667eea,#764ba2);color:#fff}.btn-danger{background:var(--error);color:#fff}.btn-success{background:var(--success);color:#fff}.btn-warning{background:var(--warning);color:#fff}.btn-sm{padding:0.35rem 0.75rem;font-size:0.85rem}.btn:hover{transform:translateY(-2px);opacity:0.9}.container{max-width:1400px;margin:0 auto;padding:2rem}.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:1.5rem;margin-bottom:2rem}.stat-card{background:var(--card);padding:1.5rem;border-radius:1rem;box-shadow:0 1px 3px rgba(0,0,0,0.1)}.stat-card h3{color:var(--text-light);font-size:0.875rem;font-weight:500;margin-bottom:0.5rem}.stat-card .value{font-size:2rem;font-weight:700}.stat-card .icon{font-size:2rem;margin-bottom:0.5rem}.card{background:var(--card);border-radius:1rem;box-shadow:0 1px 3px rgba(0,0,0,0.1);padding:1.5rem;margin-bottom:1.5rem}.card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:1.5rem;flex-wrap:wrap;gap:1rem}.card-header h2{font-size:1.25rem}table{width:100%;border-collapse:collapse}th,td{padding:1rem;text-align:left;border-bottom:1px solid var(--border)}th{background:var(--bg);font-weight:600;color:var(--text-light);font-size:0.875rem}.status-badge{padding:0.25rem 0.75rem;border-radius:1rem;font-size:0.75rem;font-weight:600;text-transform:uppercase}.status-running{background:#d1fae5;color:#065f46}.status-stopped{background:#fee2e2;color:#991b1b}.status-crashed{background:#fef3c7;color:#92400e}.modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:1000;overflow-y:auto;padding:2rem}.modal.show{display:block}.modal-content{background:#fff;margin:0 auto;padding:2rem;border-radius:1rem;max-width:700px;width:100%;max-height:90vh;overflow-y:auto}.modal-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:1.5rem}.form-group{margin-bottom:1.5rem}.form-group label{display:block;margin-bottom:0.5rem;font-weight:500}.form-group input,.form-group select,.form-group textarea{width:100%;padding:0.75rem;border:2px solid var(--border);border-radius:0.5rem;font-size:1rem;font-family:inherit}.form-group textarea{font-family:'Courier New',monospace;min-height:200px}.action-btns{display:flex;gap:0.5rem;flex-wrap:wrap}.logs-viewer{background:#1e1e1e;color:#d4d4d4;padding:1rem;border-radius:0.5rem;font-family:'Courier New',monospace;font-size:0.875rem;max-height:500px;overflow-y:auto;white-space:pre-wrap;word-wrap:break-word}.log-line{margin-bottom:0.25rem}.log-info{color:#4fc3f7}.log-error{color:#f44336}.log-warning{color:#ff9800}.toast{position:fixed;bottom:2rem;right:2rem;background:#fff;padding:1rem 1.5rem;border-radius:0.5rem;box-shadow:0 4px 12px rgba(0,0,0,0.15);display:none;z-index:2000;min-width:250px}.toast.show{display:block;animation:slideIn 0.3s}@keyframes slideIn{from{transform:translateX(400px)}to{transform:translateX(0)}}@media(max-width:768px){.container{padding:1rem}.stats-grid{grid-template-columns:1fr 1fr}.card-header{flex-direction:column;align-items:stretch}.action-btns{flex-direction:column}}
</style></head><body>
<div class="navbar"><h1>🚀 Hosting Platform</h1><a href="/logout" class="btn btn-danger btn-sm">Logout</a></div>
<div class="container">
<div class="stats-grid">
<div class="stat-card"><div class="icon">📦</div><h3>Total Applications</h3><div class="value" id="stat-total">0</div></div>
<div class="stat-card"><div class="icon">✅</div><h3>Running</h3><div class="value" id="stat-running" style="color:var(--success)">0</div></div>
<div class="stat-card"><div class="icon">⏸️</div><h3>Stopped</h3><div class="value" id="stat-stopped" style="color:var(--error)">0</div></div>
<div class="stat-card"><div class="icon">💥</div><h3>Crashed</h3><div class="value" id="stat-crashed" style="color:var(--warning)">0</div></div>
</div>
<div class="card"><div class="card-header"><h2>Applications</h2>
<button class="btn btn-primary" id="create-btn">+ Create Application</button></div>
<table><thead><tr><th>Name</th><th>Type</th><th>Status</th><th>Uptime</th><th>Actions</th></tr></thead>
<tbody id="apps-tbody"><tr><td colspan="5" style="text-align:center;padding:2rem">Loading...</td></tr></tbody></table></div></div>

<div class="modal" id="create-modal"><div class="modal-content">
<div class="modal-header"><h2>Create Application</h2><button class="btn btn-sm" id="close-create">Close</button></div>
<form id="create-form">
<div class="form-group"><label>Application Name *</label><input type="text" id="app-name" required placeholder="My Telegram Bot"></div>
<div class="form-group"><label>Template</label><select id="app-type">
<option value="">Select template...</option>
<option value="telegram">Telegram Bot</option>
<option value="flask">Flask API</option>
<option value="fastapi">FastAPI</option>
<option value="discord">Discord Bot</option>
<option value="worker">Background Worker</option>
<option value="custom">Custom (Blank)</option>
</select></div>
<div class="form-group"><label>Python Script *</label><textarea id="app-script" required placeholder="# Your Python code"></textarea></div>
<div class="form-group"><label>Requirements (one per line)</label><textarea id="app-reqs" rows="4" placeholder="python-telegram-bot>=20.0"></textarea></div>
<div class="form-group"><label>Environment Variables (JSON)</label><textarea id="app-env" rows="3">{"BOT_TOKEN": "your_token_here"}</textarea></div>
<div class="form-group"><label><input type="checkbox" id="auto-restart" checked> Auto-restart on crash</label>
<label><input type="checkbox" id="auto-start" checked> Auto-start on boot</label></div>
<button type="submit" class="btn btn-primary" style="width:100%">Create Application</button>
</form></div></div>

<div class="modal" id="logs-modal"><div class="modal-content">
<div class="modal-header"><h2 id="logs-title">Logs</h2><button class="btn btn-sm" id="close-logs">Close</button></div>
<div class="logs-viewer" id="logs-content">Loading logs...</div></div></div>

<div class="toast" id="toast"></div>

<script>
const templates={
telegram:{code:`from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TOKEN")

async def start(update, context):
    await update.message.reply_text("Hello! Bot is running!")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    print("Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()`,reqs:['python-telegram-bot>=20.0']},
flask:{code:`from flask import Flask, jsonify
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"status": "running", "message": "API is live!"})

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)`,reqs:['Flask>=3.0.0']},
fastapi:{code:`from fastapi import FastAPI
import uvicorn
app = FastAPI()

@app.get("/")
def root():
    return {"status": "running", "message": "FastAPI is live!"}

@app.get("/health")
def health():
    return {"status": "healthy"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)`,reqs:['fastapi>=0.104.0','uvicorn>=0.24.0']},
discord:{code:`import discord
import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TOKEN")
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'{client.user} connected!')

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if message.content.startswith('!hello'):
        await message.channel.send('Hello!')

client.run(BOT_TOKEN)`,reqs:['discord.py>=2.3.0']},
worker:{code:`import time
from datetime import datetime

print("Worker started!")
counter = 0

while True:
    counter += 1
    print(f"[{datetime.now()}] Worker tick #{counter}")
    time.sleep(60)`,reqs:[]},
custom:{code:`# Your custom Python code
print("Application started!")`,reqs:[]}
};

let logsSource=null;

document.getElementById('create-btn').onclick=()=>{document.getElementById('create-modal').classList.add('show')};
document.getElementById('close-create').onclick=()=>{document.getElementById('create-modal').classList.remove('show')};
document.getElementById('close-logs').onclick=()=>{
document.getElementById('logs-modal').classList.remove('show');
if(logsSource){logsSource.close();logsSource=null}
};

document.getElementById('app-type').onchange=function(){
const t=this.value;
if(t&&t!=='custom'&&templates[t]){
document.getElementById('app-script').value=templates[t].code;
document.getElementById('app-reqs').value=templates[t].reqs.join('\\n');
toast('Template loaded!','success');
}else if(t==='custom'){
document.getElementById('app-script').value='# Your code\\nprint("Started!")';
document.getElementById('app-reqs').value='';
}
};

document.getElementById('create-form').onsubmit=async(e)=>{
e.preventDefault();
const btn=e.target.querySelector('button');
btn.disabled=true;
btn.textContent='Creating...';

try{
const data={
app_name:document.getElementById('app-name').value.trim(),
app_type:document.getElementById('app-type').value||'custom',
script:document.getElementById('app-script').value,
requirements:document.getElementById('app-reqs').value.split('\\n').map(r=>r.trim()).filter(r=>r&&!r.startsWith('#')),
env_vars:JSON.parse(document.getElementById('app-env').value||'{}'),
auto_restart:document.getElementById('auto-restart').checked,
auto_start:document.getElementById('auto-start').checked
};

const res=await fetch('/api/apps/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
const result=await res.json();

if(result.success){
toast('✅ Application created!','success');
document.getElementById('create-modal').classList.remove('show');
document.getElementById('create-form').reset();
loadApps();
}else{
toast('❌ '+result.message,'error');
}
}catch(err){
toast('❌ Error: '+err.message,'error');
}finally{
btn.disabled=false;
btn.textContent='Create Application';
}
};

async function loadApps(){
try{
const res=await fetch('/api/apps');
const data=await res.json();
if(data.success){
renderApps(data.data);
updateStats(data.data);
}
}catch(err){
console.error(err);
}
}

function renderApps(apps){
const tbody=document.getElementById('apps-tbody');
if(!apps||apps.length===0){
tbody.innerHTML='<tr><td colspan="5" style="text-align:center;padding:2rem;color:#6b7280">No applications. Create one!</td></tr>';
return;
}

tbody.innerHTML=apps.map(a=>`<tr>
<td><strong>${esc(a.app_name)}</strong></td>
<td>${esc(a.app_type||'Custom')}</td>
<td><span class="status-badge status-${a.status}">${a.status.toUpperCase()}</span></td>
<td>${formatTime(a.uptime_seconds||0)}</td>
<td><div class="action-btns">
${a.status==='running'?`<button class="btn btn-warning btn-sm" onclick="doAction('${a.app_id}','stop')">Stop</button>`:`<button class="btn btn-success btn-sm" onclick="doAction('${a.app_id}','start')">Start</button>`}
<button class="btn btn-primary btn-sm" onclick="viewLogs('${a.app_id}','${esc(a.app_name)}')">Logs</button>
<button class="btn btn-sm" style="background:#667eea;color:#fff" onclick="doAction('${a.app_id}','restart')">Restart</button>
<button class="btn btn-danger btn-sm" onclick="doDelete('${a.app_id}','${esc(a.app_name)}')">Delete</button>
</div></td></tr>`).join('');
}

function updateStats(apps){
document.getElementById('stat-total').textContent=apps.length;
document.getElementById('stat-running').textContent=apps.filter(a=>a.status==='running').length;
document.getElementById('stat-stopped').textContent=apps.filter(a=>a.status==='stopped').length;
document.getElementById('stat-crashed').textContent=apps.filter(a=>a.status==='crashed').length;
}

function esc(str){const d=document.createElement('div');d.textContent=str;return d.innerHTML}

function formatTime(sec){
if(!sec)return'-';
const h=Math.floor(sec/3600);
const m=Math.floor((sec%3600)/60);
return h>0?`${h}h ${m}m`:`${m}m`;
}

async function doAction(id,action){
try{
const res=await fetch(`/api/apps/${id}/${action}`,{method:'POST'});
const data=await res.json();
toast(data.success?'✅ '+data.message:'❌ '+data.message,data.success?'success':'error');
if(data.success)loadApps();
}catch(err){
toast('❌ Error: '+err.message,'error');
}
}

async function doDelete(id,name){
if(!confirm(`Delete "${name}"? This cannot be undone!`))return;
try{
const res=await fetch(`/api/apps/${id}`,{method:'DELETE'});
const data=await res.json();
toast(data.success?'✅ Deleted':'❌ '+data.message,data.success?'success':'error');
if(data.success)loadApps();
}catch(err){
toast('❌ Error: '+err.message,'error');
}
}

function viewLogs(id,name){
document.getElementById('logs-title').textContent=`Logs: ${name}`;
document.getElementById('logs-modal').classList.add('show');
document.getElementById('logs-content').innerHTML='Loading...';

if(logsSource)logsSource.close();

logsSource=new EventSource(`/api/apps/${id}/logs/stream`);
const content=document.getElementById('logs-content');
content.innerHTML='';

logsSource.onmessage=(e)=>{
const log=JSON.parse(e.data);
const line=document.createElement('div');
line.className=`log-line log-${log.level.toLowerCase()}`;
const time=new Date(log.timestamp).toLocaleTimeString();
line.textContent=`[${time}] [${log.level}] ${log.message}`;
content.appendChild(line);
content.scrollTop=content.scrollHeight;
};

logsSource.onerror=()=>{
toast('Log stream disconnected','error');
};
}

function toast(msg,type='info'){
const t=document.getElementById('toast');
t.textContent=msg;
t.style.background=type==='success'?'#10b981':type==='error'?'#ef4444':'#fff';
t.style.color=type==='info'?'#1f2937':'#fff';
t.classList.add('show');
setTimeout(()=>t.classList.remove('show'),3000);
}

loadApps();
setInterval(loadApps,10000);
</script>
</body></html>'''

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
        return render_template_string(LOGIN_HTML, error="Invalid credentials")
    return render_template_string(LOGIN_HTML)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

# ═══════════════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════════════

@app.route('/api/apps/create', methods=['POST'])
@login_required
def api_create():
    try:
        data = request.json
        app_id = generate_app_id()
        
        doc = {
            "app_id": app_id,
            "app_name": data.get('app_name', 'Unnamed'),
            "app_type": data.get('app_type', 'custom'),
            "script": data.get('script', ''),
            "requirements": data.get('requirements', []),
            "env_vars": data.get('env_vars', {}),
            "auto_restart": data.get('auto_restart', True),
            "auto_start": data.get('auto_start', False),
            "status": "stopped",
            "created_at": datetime.utcnow(),
            "last_started": None,
            "uptime_seconds": 0,
            "restart_count": 0,
            "pid": None
        }
        
        apps_col.insert_one(doc)
        log_to_db(app_id, "INFO", f"Application '{doc['app_name']}' created")
        
        return jsonify({"success": True, "message": "Application created", "data": {"app_id": app_id}})
    except Exception as e:
        logger.error(f"Create error: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/apps', methods=['GET'])
@login_required
def api_list():
    try:
        apps = list(apps_col.find({}, {'_id': 0}).sort('created_at', DESCENDING))
        return jsonify({"success": True, "data": apps})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/apps/<app_id>', methods=['DELETE'])
@login_required
def api_delete(app_id):
    try:
        if delete_app(app_id):
            return jsonify({"success": True, "message": "Application deleted"})
        return jsonify({"success": False, "message": "Delete failed"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/apps/<app_id>/start', methods=['POST'])
@login_required
def api_start(app_id):
    try:
        if start_app(app_id):
            return jsonify({"success": True, "message": "Application started"})
        return jsonify({"success": False, "message": "Start failed"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/apps/<app_id>/stop', methods=['POST'])
@login_required
def api_stop(app_id):
    try:
        if stop_app(app_id):
            return jsonify({"success": True, "message": "Application stopped"})
        return jsonify({"success": False, "message": "Stop failed"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/apps/<app_id>/restart', methods=['POST'])
@login_required
def api_restart(app_id):
    try:
        if restart_app(app_id):
            return jsonify({"success": True, "message": "Application restarted"})
        return jsonify({"success": False, "message": "Restart failed"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/apps/<app_id>/logs/stream')
@login_required
def api_logs_stream(app_id):
    def generate():
        if app_id not in log_queues:
            log_queues[app_id] = Queue()
        
        queue = log_queues[app_id]
        
        # Send existing logs
        try:
            logs = list(logs_col.find({"app_id": app_id}, {'_id': 0}).sort('timestamp', DESCENDING).limit(100))
            logs.reverse()
            for log in logs:
                log['timestamp'] = log['timestamp'].isoformat()
                yield f"data: {json.dumps(log)}\n\n"
        except:
            pass
        
        # Stream new logs
        while True:
            try:
                log = queue.get(timeout=30)
                log['timestamp'] = log['timestamp'].isoformat()
                yield f"data: {json.dumps(log)}\n\n"
            except Empty:
                yield f": keepalive\n\n"
            except:
                break
    
    return Response(generate(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

# ═══════════════════════════════════════════════════════════════════════
# STARTUP & SHUTDOWN
# ═══════════════════════════════════════════════════════════════════════

def startup():
    logger.info("="*70)
    logger.info("🚀 HOSTING PLATFORM STARTING")
    logger.info("="*70)
    
    # Start monitor
    threading.Thread(target=monitor_loop, daemon=True).start()
    logger.info("✅ Monitor started")
    
    # Auto-start apps
    auto_apps = list(apps_col.find({"auto_start": True}))
    logger.info(f"Auto-starting {len(auto_apps)} applications")
    
    for app in auto_apps:
        threading.Thread(target=start_app, args=(app["app_id"],), daemon=True).start()
    
    logger.info("="*70)
    logger.info(f"✅ PLATFORM READY - http://localhost:{PORT}")
    logger.info(f"👤 Username: {ADMIN_USERNAME}")
    logger.info("="*70)

def shutdown(sig=None, frame=None):
    global platform_running
    logger.info("🛑 Shutting down...")
    platform_running = False
    
    for app_id in list(running_processes.keys()):
        try:
            stop_app(app_id)
        except:
            pass
    
    mongo_client.close()
    logger.info("✅ Shutdown complete")
    if sig:
        sys.exit(0)

signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    startup()
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
