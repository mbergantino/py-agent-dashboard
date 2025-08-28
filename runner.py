#!/usr/bin/env python3
import sys, os, re, subprocess, runpy, importlib, ast
from pathlib import Path

ALLOW_AUTO_INSTALL = True
PYTHON = sys.executable
IS_ROOT = (os.geteuid() == 0)
MAX_PASSES = int(os.getenv("RUNNER_MAX_PASSES", "50"))  # safety cap

def log(msg): print(f"[runner] {msg}", flush=True)

def ensure_pip():
    try:
        import pip  # noqa
        return True
    except Exception:
        pass
    try:
        import ensurepip  # noqa
        log("bootstrapping pip via ensurepip ...")
        subprocess.check_call([PYTHON, "-m", "ensurepip", "--upgrade"])
        return True
    except Exception as e:
        log(f"ensurepip failed: {e}")
        return False

def in_venv():
    return getattr(sys, "base_prefix", sys.prefix) != sys.prefix

def is_externally_managed():
    candidates = [
        "/usr/lib/python3/dist-packages/EXTERNALLY-MANAGED",
        "/usr/local/lib/python3.11/dist-packages/EXTERNALLY-MANAGED",
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
        log(f"auto-install disabled; missing: {pkg}")
        return False
    if not ensure_pip():
        log("pip unavailable; cannot auto-install.")
        return False

    base = [PYTHON, "-m", "pip", "install",
            "--disable-pip-version-check", "--no-input", "--no-cache-dir"]
    if is_externally_managed() and not in_venv():
        base.append("--break-system-packages")
    if not IS_ROOT and "--break-system-packages" not in base:
        base.append("--user")

    top = pkg.split(".")[0]
    candidates = [top] + ([top.replace("_", "-")] if "_" in top else [])

    for c in candidates:
        args = base + [c]
        log(f"pip cmd: {' '.join(args)}")
        res = subprocess.run(args, capture_output=True, text=True)
        if res.stdout: log("pip stdout:\n" + res.stdout.strip())
        if res.stderr: log("pip stderr:\n" + res.stderr.strip())
        if res.returncode == 0:
            importlib.invalidate_caches()
            try:
                importlib.import_module(top)
                return True
            except Exception:
                pass
    return False

APT_MAP = {
    "requests": "python3-requests",
    "bs4": "python3-bs4",
    "beautifulsoup4": "python3-bs4",
    "lxml": "python3-lxml",
    "yaml": "python3-yaml",
    "PyYAML": "python3-yaml",
    "dateutil": "python3-dateutil",
    "ujson": "python3-ujson",
}

def apt_install_for(modname: str) -> bool:
    if not IS_ROOT: return False
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
            importlib.import_module(name)
            return True
        except Exception:
            pass
    return False

def missing_from_exc(exc: BaseException):
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
    blacklist = {"os","sys","re","json","subprocess","pathlib","time","datetime","math","logging","typing","itertools","functools","collections"}
    return [m for m in mods if m and m not in blacklist]

def ensure_importables(mods):
    installed_any = False
    for m in mods:
        try:
            importlib.import_module(m)
            continue
        except Exception:
            pass
        log(f"pre-install missing: {m}")
        if pip_install(m) or apt_install_for(m):
            installed_any = True
        else:
            log(f"pre-install failed for: {m}")
    return installed_any

def clear_module(modname: str):
    top = modname.split(".")[0]
    if top in sys.modules:
        del sys.modules[top]
    importlib.invalidate_caches()

def run_until_stable(path: str) -> int:
    """Keep retrying as long as the last attempt installed a missing dependency."""
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
                log(f"could not parse missing module from: {e}")
                raise
            log(f"missing module detected: {missing}")
            installed = False
            if pip_install(missing):
                installed = True
            elif apt_install_for(missing):
                installed = True

            if installed:
                clear_module(missing)
                continue  # try again since we installed something
            log(f"install failed for: {missing}")
            raise
    log("max passes reached; still failing due to cascading imports")
    return 1

def main():
    if len(sys.argv) < 2:
        print("usage: runner.py /path/to/script.py", file=sys.stderr)
        sys.exit(2)
    script_path = os.path.abspath(sys.argv[1])
    os.chdir(os.path.dirname(script_path) or ".")
    log(f"running script: {script_path}")

    # Preflight: attempt to install declared/parsed imports (may speed things up)
    pre = parse_requirements_header(script_path)
    if pre:
        log(f"requirements header: {', '.join(pre)}")
    ensure_importables(pre + parse_imports(script_path))

    sys.exit(run_until_stable(script_path))

if __name__ == "__main__":
    main()

