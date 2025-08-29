from flask import Flask, render_template, request, redirect, url_for, flash, Response, abort, jsonify
from collections import deque
from crontab import CronTab
from datetime import datetime, timezone
import os, re, sys, subprocess, time
from pathlib import Path

app = Flask(__name__)
app.secret_key = "pi-dashboard"
user_cron = CronTab(user=True)

BASE_DIR    = Path(__file__).resolve().parent
SCRIPTS_DIR = (BASE_DIR / "scripts").resolve()
LOGS_DIR    = (BASE_DIR / "logs").resolve()
RUNNER      = f"/usr/bin/python3 {BASE_DIR}/runner.py"
BOOT_SYNC_DONE = False

# Service Log vars
SAFE_UNIT = re.compile(r"^[\w\-.@]+$")  # e.g., flask-dashboard
SERVICE_CACHE = {"expires": 0.0, "items": []}
SERVICE_CACHE_TTL = 30.0  # seconds

# how long to wait (max) for the log mtime to change after launch
RUN_WAIT_MAX  = 8.0   # seconds
RUN_WAIT_STEP = 0.25  # polling interval

# Make directory structures needed
SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Sample App
SAMPLE_NAME = "helloworld"
SAMPLE_SCRIPT="""
from datetime import datetime
print(f"{datetime.now()} Hello from the Raspberry Pi Dashboard!")
"""

print(f" * Will write new scripts to {SCRIPTS_DIR} and logs to {LOGS_DIR}")

@app.before_request
def _boot_sync_once():
    global BOOT_SYNC_DONE
    if not BOOT_SYNC_DONE:
        rebuild_jobs()      # ensure jobs list reflects scripts + crontab
        BOOT_SYNC_DONE = True

@app.route("/")
def index():
    jobs = rebuild_jobs()
    has_scheduled = any(j.get("has_cron") for j in jobs)
    return render_template("index.html", jobs=jobs, has_scheduled=has_scheduled)

@app.route("/view/<name>")
def view(name):
    log_file = os.path.join(LOGS_DIR, f"{name}.log")
    content = open(log_file).read() if os.path.exists(log_file) else "No logs found."
    return render_template("view.html", name=name, content=content)

@app.route("/purge/<name>")
def purge_log(name):
    log_file = os.path.join(LOGS_DIR, f"{name}.log")

    if os.path.exists(log_file): 
        with open(log_file, 'w') as file:
            pass
    return redirect(url_for("index"))

def _log_mtime_epoch(name: str) -> int:
    log_path = os.path.join(LOGS_DIR, f"{name}.log")
    try:
        return int(os.path.getmtime(log_path))
    except FileNotFoundError:
        return 0

def _launch_job(name: str):
    """Start the script via runner.py, append output to its log, return (ok, msg)."""
    script_path = os.path.join(SCRIPTS_DIR, f"{name}.py")
    log_path    = os.path.join(LOGS_DIR,    f"{name}.log")

    if not os.path.exists(script_path):
        return False, f"{name}.py not found"

    os.makedirs(LOGS_DIR, exist_ok=True)
                                                                     
    # make runner write immediately
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    # non-blocking; child writes to log file
    with open(log_path, "a") as lf:
        subprocess.Popen(
            [sys.executable, str(BASE_DIR / "runner.py"), script_path],
            stdout=lf, stderr=subprocess.STDOUT,
            cwd=os.path.dirname(script_path) or ".",
            close_fds=True, env=env,
        )
    return True, f"{name}.py launched"

@app.route("/run/<name>", methods=["POST", "GET"])
def run_now(name):
    """Trigger → wait for log to advance (or timeout) → flash → redirect to index."""
    prev = _log_mtime_epoch(name)
    ok, msg = _launch_job(name)
    if not ok:
        flash(msg, "danger")
        return redirect(url_for("index"))

    # wait briefly for the log mtime to change
    deadline = time.time() + RUN_WAIT_MAX
    while time.time() < deadline:
        if _log_mtime_epoch(name) > prev:
            break
        time.sleep(RUN_WAIT_STEP)

    flash(msg, "info")
    return redirect(url_for("index"))

@app.route("/edit/<name>", methods=["GET", "POST"])
def edit(name):
    script_path = os.path.join(SCRIPTS_DIR, f"{name}.py")
    job = next((j for j in user_cron if j.comment == name), None)

    if request.method == "POST":
        new_content = request.form["script"]
        schedule = request.form.get("schedule", "").strip()

        # Save script contents
        with open(script_path, "w") as f:
            f.write(new_content)

        # Handle "DISABLED" = remove cron job if present
        if schedule.upper() == "DISABLED":
            if job:
                user_cron.remove(job)
                user_cron.write()
            flash(f"{name}.py saved; schedule is DISABLED", "warning")
            return redirect(url_for("index"))

        # Otherwise, (re)create cron job with the provided expression
        try:
            if job:
                user_cron.remove(job)
            new_job = user_cron.new(
                command=f"{RUNNER} {script_path} >> {LOGS_DIR}/{name}.log 2>&1",
                #command=f"/usr/bin/python3 {script_path} >> {LOGS_DIR}/{name}.log 2>&1",
                comment=name
            )
            new_job.setall(schedule)  # raises if invalid
            user_cron.write()
            flash(f"{name}.py saved; schedule set to '{schedule}'", "success")
        except Exception as e:
            flash(f"Invalid cron schedule: '{schedule}' ({e})", "danger")
        return redirect(url_for("index"))

    # GET: render editor
    current_schedule = (job.slices if job else "DISABLED")
    content = open(script_path).read() if os.path.exists(script_path) else ""
    return render_template("edit.html", name=name, content=content, schedule=current_schedule)

@app.route("/add", methods=["GET", "POST"])
def add():
    if request.method == "POST":
        raw_name = request.form["name"].strip()
        name = re.sub(r"[^A-Za-z0-9_\-]", "_", raw_name)  # sanitize
        if name.endswith(".py"):  # avoid double .py
            name = name[:-3]

        script_path = os.path.join(SCRIPTS_DIR, f"{name}.py")

        if os.path.exists(script_path):
            # return the page with an error and preserve input
            error = f"A script named '{name}.py' already exists."
            return render_template("add.html", error=error, name=raw_name), 409

        # (optional) create the file now so the name is reserved
        open(script_path, "a").close()
        return redirect(url_for("edit", name=name))

    return render_template("add.html")

@app.route("/add-sample", methods=["POST"])
def add_sample():
    name = SAMPLE_NAME
    script_path = os.path.join(SCRIPTS_DIR, f"{name}.py")
    log_path = os.path.join(LOGS_DIR, f"{name}.log")

    # Create script if missing (do not overwrite existing)
    created = False
    if not os.path.exists(script_path):
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(SAMPLE_SCRIPT)
        os.chmod(script_path, 0o755)
        created = True
    
    if created:
        flash("Sample 'helloworld' script created.", "success")
    else:
        flash("Script 'helloworld' already existed; didn't touch it.", "info")

    return redirect(url_for("index"))

@app.route("/delete/<name>")
def delete(name):
    script_path = os.path.join(SCRIPTS_DIR, f"{name}.py")
    log_path = os.path.join(LOGS_DIR, f"{name}.log")
    
    if os.path.exists(script_path):
        os.remove(script_path)
    
    if os.path.exists(log_path):
        os.remove(log_path)

    for job in user_cron:
        if job.comment == name:
            user_cron.remove(job)
    user_cron.write()
    flash(f"{name}.py and its cron job deleted", "warning")
    return redirect(url_for("index"))

@app.get("/api/log/<name>")
def api_log(name):
    """Return last N lines of the log as plain text (no cache)."""
    log_path = os.path.join(LOGS_DIR, f"{name}.log")
    try:
        lines = int(request.args.get("lines", 500))
    except ValueError:
        lines = 500

    if not os.path.exists(log_path):
        return Response("", mimetype="text/plain", headers={"Cache-Control": "no-store"})

    # Efficient tail of last N lines
    with open(log_path, "rb") as f:
        tail_bytes = b"".join(deque(f, maxlen=lines))
    text = tail_bytes.decode("utf-8", errors="replace")
    return Response(text, mimetype="text/plain", headers={"Cache-Control": "no-store"})

@app.get("/api/service-log/<unit>")
def api_service_log(unit):
    if not SAFE_UNIT.match(unit):
        abort(400, "invalid unit name")
    try:
        lines = int(request.args.get("lines", 500))
    except ValueError:
        lines = 500

    # Tail last N lines from the journal (system scope)
    proc = subprocess.run(
        ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "--output=short-iso"],
        capture_output=True, text=True
    )
    text = proc.stdout if proc.returncode == 0 else proc.stderr
    return Response(text or "", mimetype="text/plain", headers={"Cache-Control": "no-store"})

def _systemctl(*args):
    """Run systemctl and return (rc, stdout, stderr)."""
    proc = subprocess.run(["systemctl", *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr

def _unit_exists(unit: str) -> bool:
    rc, _, _ = _systemctl("status", unit, "--no-pager")
    return rc == 0

def _owns_unit(unit: str) -> bool:
    """
    Heuristic: show only services related to this app.
    We check systemd metadata and include if:
      - WorkingDirectory contains BASE_DIR, OR
      - ExecStart contains BASE_DIR
    """
    if not SAFE_UNIT.match(unit):
        return False
    rc, out, _ = _systemctl("show", unit, "-p", "WorkingDirectory", "-p", "ExecStart")
    if rc != 0:
        return False
    wd = ""
    es = ""
    for line in out.splitlines():
        if line.startswith("WorkingDirectory="):
            wd = line.split("=", 1)[1].strip()
        elif line.startswith("ExecStart="):
            es = line.split("=", 1)[1].strip()
    base = str(BASE_DIR)
    return (wd and base in wd) or (es and base in es)

def list_related_services():
    """
    List service units and filter to ones “owned” by this app directory.
    """
    rc, out, _ = _systemctl("list-units", "--type=service", "--all", "--no-legend", "--no-pager")
    if rc != 0:
        return []

    units = []
    for line in out.splitlines():
        # Format: "<unit> <load> <active> <sub> <description...>"
        # Split on whitespace for first field (unit name)
        parts = line.strip().split(None, 1)
        if not parts:
            continue
        unit = parts[0]
        if unit.endswith(".service") and _owns_unit(unit):
            units.append(unit)

    # Also include well-known units if present (in case they’re inactive at the moment)
    for extra in ("flask-dashboard.service", "crowdstrike-watch.service"):
        if extra not in units and _unit_exists(extra) and _owns_unit(extra):
            units.append(extra)

    return sorted(set(units))

# --- API: return JSON list of services we’ll allow in the dropdown ---
@app.get("/api/services")
def api_services():
    return jsonify(list_related_services())

# --- Page: service logs viewer with dynamic dropdown ---
@app.route("/services")
def services():
    # Page loads with no selection; JS will fetch /api/services to populate the dropdown dynamically.
    return render_template("services.html")

def _fmt_mtime(p: Path):
    if p.exists():
        return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return None

def last_modified_epoch(filepath: Path):
    try:
        return int(os.path.getmtime(filepath))
    except FileNotFoundError:
        return None

def _name_from_cron(job):
    # Prefer the comment as canonical name
    if job.comment:
        return job.comment
    # Otherwise try to extract from the script path in the command
    for tok in job.command.split():
        if tok.endswith(".py") and str(SCRIPTS_DIR) in tok:
            return Path(tok).stem
    return None

def rebuild_jobs():
    """
    Returns a list of job dicts by reconciling *.py files in scripts/
    with cron entries that invoke them.
    Each dict has: name, script_path, has_script, has_cron, schedule, last_run, log_path
    """
    jobs = {}

    # 1) Start with every script on disk
    for script in sorted(SCRIPTS_DIR.glob("*.py")):
        name = script.stem
        log_path = LOGS_DIR / f"{name}.log"
        jobs[name] = {
            "name": name,
            "script_path": str(script),
            "has_script": True,
            "has_cron": False,
            "schedule": None,
            "last_run": _fmt_mtime(log_path),
            "last_run_epoch": last_modified_epoch(log_path),
            "log_path": str(log_path)
        }

    # 2) Merge in cron entries that reference scripts under SCRIPTS_DIR
    for job in user_cron:
        if str(SCRIPTS_DIR) in job.command:
            name = _name_from_cron(job)
            if not name:
                continue
            if name not in jobs:
                # Cron points to a script that doesn't exist anymore (or lives elsewhere)
                script_in_cmd = None
                for tok in job.command.split():
                    if tok.endswith(".py"):
                        script_in_cmd = tok
                        break
                log_path = LOGS_DIR / f"{name}.log"
                jobs[name] = {
                    "name": name,
                    "script_path": script_in_cmd or "",
                    "has_script": False,
                    "has_cron": True,
                    "schedule": str(job.slices),
                    "last_run": _fmt_mtime(log_path),
                    "last_run_epoch": last_modified_epoch(log_path),
                    "log_path": str(log_path)
                }
            else:
                jobs[name]["has_cron"] = True
                jobs[name]["schedule"] = str(job.slices)

    # Stable order for table rendering
    return [jobs[k] for k in sorted(jobs.keys())]

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
