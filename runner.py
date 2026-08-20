#!/usr/bin/env python3
"""
runner.py — Executes a job's active script, auto-installing missing Python
dependencies. Called either manually, from the dashboard's "Run Now" button,
or from a cron/launchd entry.

Usage:
    runner.py --job <slug>                 (new format — reads config for active file)
    runner.py /path/to/script.py           (legacy format — still supported)

Cross-platform: Linux (incl. Raspberry Pi), macOS, Windows.
"""
import sys, os, re, subprocess, runpy, importlib, ast, json, time, platform, shutil
from pathlib import Path
from datetime import datetime

BASE_DIR   = Path(__file__).resolve().parent
CONFIG_DIR = (BASE_DIR / "config").resolve()
JOBS_DIR   = (BASE_DIR / "jobs").resolve()
LOGS_DIR   = (BASE_DIR / "logs").resolve()

# Make project root importable so jobs can `import email_notifier` etc.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Load .env from project root if present (simple KEY=VALUE parser, no dotenv dep)
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

ALLOW_AUTO_INSTALL = True
PYTHON   = sys.executable
IS_ROOT  = (os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0)
IS_MAC   = (platform.system() == "Darwin")
IS_LINUX = (platform.system() == "Linux")
MAX_PASSES = int(os.getenv("RUNNER_MAX_PASSES", "50"))

def log(msg):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - - [runner] {msg}", flush=True)

# ── pip / apt / brew install helpers ─────────────────────────────────────────

def ensure_pip():
    try:
        import pip; return True
    except Exception:
        pass
    try:
        import ensurepip
        log("bootstrapping pip via ensurepip …")
        subprocess.check_call([PYTHON, "-m", "ensurepip", "--upgrade"])
        return True
    except Exception as e:
        log(f"ensurepip failed: {e}"); return False

def in_venv():
    return getattr(sys, "base_prefix", sys.prefix) != sys.prefix

def is_externally_managed():
    candidates = [
        "/usr/lib/python3/dist-packages/EXTERNALLY-MANAGED",
        f"/usr/local/lib/python{sys.version_info.major}.{sys.version_info.minor}/dist-packages/EXTERNALLY-MANAGED",
        f"/opt/homebrew/lib/python{sys.version_info.major}.{sys.version_info.minor}/EXTERNALLY-MANAGED",
        f"/usr/local/lib/python{sys.version_info.major}.{sys.version_info.minor}/EXTERNALLY-MANAGED",
    ]
    if any(os.path.exists(c) for c in candidates):
        return True
    for p in sys.path:
        try:
            if "EXTERNALLY-MANAGED" in os.listdir(p):
                return True
        except Exception:
            pass
    return False

def pip_install(pkg: str) -> bool:
    if not ALLOW_AUTO_INSTALL:
        log(f"auto-install disabled; missing: {pkg}"); return False
    if not ensure_pip():
        log("pip unavailable; cannot auto-install."); return False
    base = [PYTHON, "-m", "pip", "install",
            "--disable-pip-version-check", "--no-input", "--no-cache-dir"]
    if is_externally_managed() and not in_venv():
        base.append("--break-system-packages")
    if not IS_ROOT and not in_venv() and "--break-system-packages" not in base:
        base.append("--user")
    top = pkg.split(".")[0]
    candidates = [top] + ([top.replace("_", "-")] if "_" in top else [])
    for c in candidates:
        args = base + [c]
        log(f"pip cmd: {' '.join(args)}")
        res = subprocess.run(args, capture_output=True, text=True)
        if res.stdout: log("pip stdout:\n" + res.stdout.strip())
        if res.stderr: log("pip stderr:\n"  + res.stderr.strip())
        if res.returncode == 0:
            importlib.invalidate_caches()
            try:
                importlib.import_module(top); return True
            except Exception:
                pass
    return False

APT_MAP = {
    "requests": "python3-requests", "bs4": "python3-bs4",
    "beautifulsoup4": "python3-bs4", "lxml": "python3-lxml",
    "yaml": "python3-yaml", "PyYAML": "python3-yaml",
    "dateutil": "python3-dateutil", "ujson": "python3-ujson",
}
BREW_MAP = {"chromedriver": "chromedriver"}

def apt_install_for(modname: str) -> bool:
    if not IS_LINUX or not IS_ROOT or not shutil.which("apt-get"):
        return False
    name = modname.split(".")[0]
    pkg = APT_MAP.get(name, f"python3-{name.replace('_','-')}")
    log(f"apt-get install {pkg}")
    rc = subprocess.call(["apt-get", "install", "-y", pkg])
    if rc != 0:
        subprocess.call(["apt-get", "update"])
        rc = subprocess.call(["apt-get", "install", "-y", pkg])
    if rc == 0:
        importlib.invalidate_caches()
        try:
            importlib.import_module(name); return True
        except Exception:
            pass
    return False

def brew_install_for(modname: str) -> bool:
    if not IS_MAC or not shutil.which("brew"):
        return False
    name = modname.split(".")[0]
    pkg = BREW_MAP.get(name)
    if not pkg:
        return False
    log(f"brew install {pkg}")
    rc = subprocess.call(["brew", "install", pkg])
    return rc == 0

def missing_from_exc(exc):
    n = getattr(exc, "name", None)
    if n: return n
    m = re.search(r"No module named '([^']+)'", str(exc))
    return m.group(1) if m else None

def parse_requirements_header(path: str):
    reqs = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            head = "".join([next(f) for _ in range(40)])
        m = re.search(r"requirements\s*:\s*([^\n]+)", head, re.IGNORECASE)
        if m:
            reqs = [x.strip() for x in re.split(r"[,\s]+", m.group(1)) if x.strip()]
    except Exception:
        pass
    return reqs

def parse_imports(path: str):
    mods = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    mods.add(n.name.split(".")[0].strip())
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0].strip())
    except Exception as e:
        log(f"import parse skipped: {e}")
    blacklist = {
        "os","sys","re","json","subprocess","pathlib","time","datetime",
        "math","logging","typing","itertools","functools","collections"
    }
    return [m for m in mods if m and m not in blacklist]

def ensure_importables(mods):
    installed_any = False
    for m in mods:
        try:
            importlib.import_module(m); continue
        except Exception:
            pass
        log(f"pre-install missing: {m}")
        if pip_install(m) or apt_install_for(m) or brew_install_for(m):
            installed_any = True
        else:
            log(f"pre-install failed for: {m}")
    return installed_any

def clear_module(modname: str):
    top = modname.split(".")[0]
    if top in sys.modules:
        del sys.modules[top]
    importlib.invalidate_caches()

# ── Python in-process execution (auto-retry on missing deps) ─────────────────

def run_until_stable(path: str) -> int:
    passes = 0
    while passes < MAX_PASSES:
        passes += 1
        log(f"pass {passes}")
        try:
            runpy.run_path(path, run_name="__main__")
            return 0
        except (ModuleNotFoundError, ImportError) as e:
            missing = missing_from_exc(e)
            if not missing:
                log(f"could not parse missing module from: {e}"); raise
            log(f"missing module detected: {missing}")
            installed = pip_install(missing) or apt_install_for(missing) or brew_install_for(missing)
            if installed:
                clear_module(missing); continue
            log(f"install failed for: {missing}"); raise
        except SystemExit as se:
            return int(se.code or 0)
        except Exception as e:
            log(f"script crashed: {e}"); return 1
    log("max passes reached; still failing due to cascading imports")
    return 1

# ── Subprocess execution (non-Python or custom exec_command) ─────────────────

def run_subprocess(cmd: list, cwd: str, extra_env: dict = None) -> int:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=cwd, env=env
    )
    for line in proc.stdout:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
    proc.wait()
    return proc.returncode

# ── Auth token helpers ────────────────────────────────────────────────────────

def _decrypt_token(encrypted: str) -> str:
    key_path = CONFIG_DIR / ".key"
    try:
        from cryptography.fernet import Fernet
        key = key_path.read_bytes()
        return Fernet(key).decrypt(encrypted.encode()).decode()
    except ImportError:
        return encrypted
    except Exception as e:
        log(f"token decrypt failed: {e}"); return ""

def _get_auth_env(cfg: dict) -> dict:
    enc   = cfg.get("auth_token_enc")
    var   = cfg.get("auth_token_env")
    if enc and var:
        token = _decrypt_token(enc)
        if token:
            return {var: token}
    return {}

# ── Config helpers ────────────────────────────────────────────────────────────

def load_cfg(slug: str) -> dict:
    p = CONFIG_DIR / f"{slug}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}

def read_run_until_success(slug: str) -> bool:
    return bool(load_cfg(slug).get("run_until_success", False))

def log_event(kind, name=None, **fields):
    try:
        entry = {"ts": int(time.time()), "kind": kind, "name": name, **fields}
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOGS_DIR / "events.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

def disable_cron_by_comment(comment: str):
    if platform.system() == "Windows":
        task_name = f"PiDashboard\\{comment}"
        try:
            subprocess.run(["schtasks", "/Delete", "/F", "/TN", task_name],
                           capture_output=True, text=True, check=True)
            log(f"deleted Windows task {task_name}")
        except Exception as e:
            log(f"failed to delete Windows task {task_name}: {e}")
        bat = CONFIG_DIR / f"{comment}_cron.bat"
        try:
            if bat.exists(): bat.unlink()
        except Exception:
            pass
        cfg_path = CONFIG_DIR / f"{comment}.json"
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg["win_schedule"] = None
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False)
        except Exception:
            pass
        return
    try:
        cur = subprocess.check_output(["crontab", "-l"], text=True)
    except subprocess.CalledProcessError:
        cur = ""
    pat = re.compile(rf".*#\s*{re.escape(comment)}\s*$")
    kept = [ln for ln in cur.splitlines() if not pat.match(ln)]
    new  = ("\n".join(kept) + "\n") if kept else ""
    subprocess.run(["crontab", "-"], input=new, text=True, check=True)
    log(f"disabled schedule for {comment}")

# ── Execution command logic ───────────────────────────────────────────────────

_FULL_CMD_PREFIXES = ("python", "python3", "bash", "sh", "node", "ruby", "perl", "/")

def _is_full_command(exec_command: str) -> bool:
    first_token = exec_command.strip().split()[0] if exec_command.strip() else ""
    return any(first_token.startswith(p) for p in _FULL_CMD_PREFIXES)

def run_job(slug: str) -> int:
    cfg = load_cfg(slug)
    active_file  = cfg.get("active_file", "")
    exec_command = (cfg.get("exec_command") or "").strip()

    job_dir = JOBS_DIR / slug
    if not job_dir.exists():
        log(f"Job folder not found: {job_dir}"); return 1

    if not active_file:
        py_files = sorted(job_dir.glob("*.py"))
        if py_files:
            active_file = py_files[0].name
        else:
            log("No active_file set and no .py files found in job folder."); return 1

    active_path = job_dir / active_file
    if not active_path.exists():
        log(f"Active file not found: {active_path}"); return 1

    auth_env = _get_auth_env(cfg)
    ext = active_path.suffix.lower()

    log(f"slug={slug} active_file={active_file} exec_command={exec_command!r}")

    if exec_command and _is_full_command(exec_command):
        import shlex
        cmd = shlex.split(exec_command)
        log(f"running full command: {cmd}")
        return run_subprocess(cmd, str(job_dir), auth_env)

    if exec_command:
        import shlex
        extra_args = shlex.split(exec_command)
        if ext == ".py":
            cmd = [PYTHON, "-u", str(active_path)] + extra_args
        elif ext in (".sh", ""):
            cmd = ["bash", str(active_path)] + extra_args
        else:
            cmd = [str(active_path)] + extra_args
        log(f"running with params: {cmd}")
        return run_subprocess(cmd, str(job_dir), auth_env)

    # No exec_command: infer from extension
    if ext == ".py":
        log(f"running Python script in-process: {active_path}")
        pre = parse_requirements_header(str(active_path))
        if pre:
            log(f"requirements header: {', '.join(pre)}")
        ensure_importables(pre + parse_imports(str(active_path)))
        os.chdir(str(job_dir))
        if auth_env:
            os.environ.update(auth_env)
        return run_until_stable(str(active_path))
    elif ext in (".sh", ""):
        cmd = ["bash", str(active_path)]
        log(f"running shell script: {cmd}")
        return run_subprocess(cmd, str(job_dir), auth_env)
    elif ext == ".md":
        log(f"Markdown/AI file detected but no exec_command set. Set Execution Command in the editor.")
        return 1
    else:
        log(f"Unknown file extension '{ext}'. Set Execution Command in the editor.")
        return 1

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    # New format: runner.py --job <slug>
    if args and args[0] == "--job":
        if len(args) < 2:
            log("--job requires a slug argument"); sys.exit(2)
        slug = args[1]
        log(f"job mode: slug={slug}")
        rc = run_job(slug)
        if os.environ.get("RUN_CONTEXT") == "cron" and rc == 0 and read_run_until_success(slug):
            try:
                disable_cron_by_comment(slug)
                log_event("schedule_disabled_after_success", name=slug)
            except Exception as e:
                log(f"failed to disable cron for {slug}: {e}")
        sys.exit(rc)

    # Legacy format: runner.py /path/to/script.py
    if not args:
        print("usage: runner.py --job <slug>  OR  runner.py /path/to/script.py", file=sys.stderr)
        sys.exit(2)

    script_path = os.path.abspath(args[0])

    # If the path no longer exists (e.g. old scripts/ dir renamed to jobs/),
    # try to find the job by slug in the new jobs/ layout and run it that way.
    if not os.path.exists(script_path):
        stem = Path(script_path).stem
        candidate = JOBS_DIR / stem / (stem + ".py")
        if not candidate.exists():
            # Also try any .py file in jobs/<stem>/
            job_dir = JOBS_DIR / stem
            py_files = list(job_dir.glob("*.py")) if job_dir.exists() else []
            candidate = py_files[0] if py_files else None
        if candidate and candidate.exists():
            log(f"legacy path not found; redirecting to job slug '{stem}'")
            sys.exit(run_job(stem))
        log(f"script not found: {script_path}")
        sys.exit(1)

    os.chdir(os.path.dirname(script_path) or ".")
    log(f"running script (legacy mode): {script_path}")

    pre = parse_requirements_header(script_path)
    if pre:
        log(f"requirements header: {', '.join(pre)}")
    ensure_importables(pre + parse_imports(script_path))

    name = Path(script_path).stem
    rc = run_until_stable(script_path)

    if os.environ.get("RUN_CONTEXT") == "cron" and rc == 0 and read_run_until_success(name):
        try:
            disable_cron_by_comment(name)
            log_event("schedule_disabled_after_success", name=name)
        except Exception as e:
            log(f"failed to disable cron for {name}: {e}")

    sys.exit(rc)

if __name__ == "__main__":
    main()
