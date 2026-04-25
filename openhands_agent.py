#!/usr/bin/env python3
"""Full-stack coding agent using OpenHands.

Usage:
  python3 openhands_agent.py start [--port PORT] [--local]
  python3 openhands_agent.py task "description" [--workspace PATH] [--timeout SEC]
  python3 openhands_agent.py status [--task-id ID]
  python3 openhands_agent.py workspace list|show|pull|clean [ID] [--dest PATH]
  python3 openhands_agent.py review TASK_ID [--diff] [--apply PATH]
  python3 openhands_agent.py config [--model M] [--api-key K] [--sandbox S]
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

G = "\033[92m"
R = "\033[91m"
Y = "\033[93m"
C = "\033[96m"
W = "\033[0m"
BOLD = "\033[1m"

SKILL_DIR = Path(__file__).parent
STATE_FILE = SKILL_DIR / "state.json"
WORKSPACE_DIR = SKILL_DIR / "workspaces"
CONFIG_FILE = SKILL_DIR / "config.json"

DEFAULT_PORT = 3001


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"tasks": [], "server_pid": None, "status": "stopped"}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {
        "model": os.environ.get("OPENHANDS_MODEL", "claude-sonnet-4-20250514"),
        "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "sandbox": "docker",
        "port": DEFAULT_PORT,
    }


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


async def run_cmd(cmd, cwd=None, timeout=60):
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=cwd
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode(errors="replace").strip(), stderr.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "timeout"


async def check_openhands_installed():
    code, out, _ = await run_cmd("python3 -c \"import openhands; print(openhands.__version__)\"")
    if code == 0:
        return True, out
    code, out, _ = await run_cmd("pip show openhands")
    if code == 0:
        return True, "installed"
    return False, ""


async def cmd_start():
    cfg = load_config()
    state = load_state()
    port = cfg.get("port", DEFAULT_PORT)

    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
        cfg["port"] = port
        save_config(cfg)

    local_mode = "--local" in sys.argv

    if state.get("status") == "running":
        pid = state.get("server_pid")
        if pid:
            code, _, _ = await run_cmd(f"kill -0 {pid} 2>/dev/null")
            if code == 0:
                print(f"{G}OpenHands server already running (PID {pid}) on port {port}.{W}")
                return

    installed, version = await check_openhands_installed()
    if not installed:
        print(f"{Y}OpenHands not installed. Installing...{W}")
        code, out, err = await run_cmd("pip install openhands-ai", timeout=300)
        if code != 0:
            print(f"{R}Failed to install openhands:{W}\n{err}")
            print(f"\n{Y}Alternative: run via Docker:{W}")
            print(f"  docker run -d -p {port}:3000 --name openhands ghcr.io/all-hands-ai/openhands:latest")
            return
        print(f"{G}OpenHands installed.{W}")

    # Try Docker first unless --local
    if not local_mode:
        code, out, _ = await run_cmd("docker ps -a --filter name=openhands --format '{{.Names}}'")
        if "openhands" in out:
            code, _, _ = await run_cmd("docker start openhands")
            if code == 0:
                state["status"] = "running"
                state["server_pid"] = "docker:openhands"
                save_state(state)
                print(f"{G}OpenHands Docker container started on port {port}.{W}")
                print(f"{C}UI: http://localhost:{port}{W}")
                return

        # Try running new docker container
        api_key = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        env_flags = ""
        if api_key:
            env_flags = f"-e ANTHROPIC_API_KEY={api_key}"

        code, out, err = await run_cmd(
            f"docker run -d -p {port}:3000 --name openhands {env_flags} "
            f"ghcr.io/all-hands-ai/openhands:latest",
            timeout=120
        )
        if code == 0:
            state["status"] = "running"
            state["server_pid"] = "docker:openhands"
            save_state(state)
            print(f"{G}OpenHands started via Docker on port {port}.{W}")
            print(f"{C}UI: http://localhost:{port}{W}")
            return
        else:
            print(f"{Y}Docker not available or failed. Trying local mode...{W}")

    # Local mode
    model = cfg.get("model", "claude-sonnet-4-20250514")
    api_key = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")

    env = os.environ.copy()
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key

    print(f"{C}Starting OpenHands locally (model: {model})...{W}")
    proc = await asyncio.create_subprocess_exec(
        "python3", "-m", "openhands.server",
        "--port", str(port),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Wait briefly for startup
    await asyncio.sleep(3)
    if proc.returncode is not None:
        print(f"{R}OpenHands failed to start. Check logs.{W}")
        return

    state["status"] = "running"
    state["server_pid"] = proc.pid
    save_state(state)
    print(f"{G}OpenHands started locally (PID {proc.pid}) on port {port}.{W}")
    print(f"{C}UI: http://localhost:{port}{W}")


async def cmd_task():
    cfg = load_config()
    state = load_state()

    if len(sys.argv) < 3:
        print(f"{R}Usage: python3 openhands_agent.py task \"description\" [--workspace PATH] [--timeout SEC]{W}")
        return

    task_desc = sys.argv[2]
    task_id = f"task-{int(time.time())}"
    timeout = 300
    workspace = None

    if "--timeout" in sys.argv:
        timeout = int(sys.argv[sys.argv.index("--timeout") + 1])
    if "--workspace" in sys.argv:
        workspace = sys.argv[sys.argv.index("--workspace") + 1]

    # Create workspace
    task_ws = WORKSPACE_DIR / task_id
    task_ws.mkdir(parents=True, exist_ok=True)

    task_entry = {
        "id": task_id,
        "description": task_desc,
        "workspace": str(workspace or task_ws),
        "status": "submitted",
        "created": time.time(),
        "timeout": timeout,
        "result": None,
    }

    model = cfg.get("model", "claude-sonnet-4-20250514")

    print(f"{BOLD}Submitting task to OpenHands:{W}")
    print(f"  {C}Task:{W} {task_desc}")
    print(f"  {C}ID:{W} {task_id}")
    print(f"  {C}Model:{W} {model}")
    print(f"  {C}Workspace:{W} {task_entry['workspace']}")

    # Try to submit via API
    api_url = f"http://localhost:{cfg.get('port', DEFAULT_PORT)}"
    code, out, err = await run_cmd(
        f'curl -s -X POST {api_url}/api/tasks '
        f'-H "Content-Type: application/json" '
        f'-d \'{json.dumps({"task": task_desc, "workspace": task_entry["workspace"]})}\'',
        timeout=30
    )

    if code == 0 and out:
        try:
            resp = json.loads(out)
            if resp.get("task_id"):
                task_entry["remote_id"] = resp["task_id"]
                task_entry["status"] = "running"
                print(f"\n{G}Task submitted to OpenHands server.{W}")
            else:
                print(f"\n{Y}Server response did not include task_id. Running locally.{W}")
        except json.JSONDecodeError:
            pass

    # If server not available, generate code locally as fallback
    if task_entry["status"] == "submitted":
        print(f"\n{Y}OpenHands server not reachable. Generating task file for manual execution.{W}")
        task_file = task_ws / "task.json"
        task_file.write_text(json.dumps(task_entry, indent=2))

        readme = task_ws / "TASK.md"
        readme.write_text(f"# Task: {task_id}\n\n{task_desc}\n\n"
                          f"Model: {model}\nWorkspace: {workspace or task_ws}\n"
                          f"Timeout: {timeout}s\nCreated: {time.ctime()}\n")
        task_entry["status"] = "pending"

    state["tasks"].append(task_entry)
    save_state(state)

    print(f"\n{G}Task {task_id} created.{W}")
    print(f"{C}Check status: python3 openhands_agent.py status --task-id {task_id}{W}")


async def cmd_status():
    state = load_state()
    task_id = None

    if "--task-id" in sys.argv:
        task_id = sys.argv[sys.argv.index("--task-id") + 1]

    print(f"{BOLD}OpenHands Status:{W}\n")
    print(f"  Server: {G if state['status'] == 'running' else R}{state['status']}{W}")

    if task_id:
        for t in state.get("tasks", []):
            if t["id"] == task_id:
                status_color = G if t["status"] == "completed" else Y if t["status"] == "running" else C
                print(f"\n  {BOLD}Task: {t['id']}{W}")
                print(f"    Description: {t['description']}")
                print(f"    Status: {status_color}{t['status']}{W}")
                print(f"    Workspace: {t['workspace']}")
                if t.get("result"):
                    print(f"    Result: {t['result']}")
                break
        else:
            print(f"\n  {R}Task {task_id} not found.{W}")
    else:
        tasks = state.get("tasks", [])
        if not tasks:
            print(f"\n  {Y}No tasks yet.{W}")
        else:
            print(f"\n  {'ID':25s} {'STATUS':12s} {'DESCRIPTION':50s}")
            print(f"  {'-'*25} {'-'*12} {'-'*50}")
            for t in tasks[-10:]:
                status_color = G if t["status"] == "completed" else Y if t["status"] == "running" else C
                desc = t["description"][:50]
                print(f"  {t['id']:25s} {status_color}{t['status']:12s}{W} {desc}")


async def cmd_workspace():
    if len(sys.argv) < 3:
        print(f"{R}Usage: python3 openhands_agent.py workspace list|show|pull|clean [ID] [--dest PATH]{W}")
        return

    action = sys.argv[2]
    state = load_state()

    if action == "list":
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        dirs = sorted(WORKSPACE_DIR.iterdir()) if WORKSPACE_DIR.exists() else []
        print(f"{BOLD}Workspaces:{W}\n")
        if not dirs:
            print(f"  {Y}No workspaces.{W}")
            return
        for d in dirs:
            if d.is_dir():
                files = list(d.iterdir())
                print(f"  {C}{d.name}{W} ({len(files)} files)")
                for f in files[:5]:
                    print(f"    - {f.name}")
                if len(files) > 5:
                    print(f"    ... and {len(files) - 5} more")

    elif action == "show":
        if len(sys.argv) < 4:
            print(f"{R}Usage: workspace show <task-id>{W}")
            return
        task_ws = WORKSPACE_DIR / sys.argv[3]
        if not task_ws.exists():
            print(f"{R}Workspace not found: {sys.argv[3]}{W}")
            return
        for f in sorted(task_ws.rglob("*")):
            if f.is_file():
                print(f"  {C}{f.relative_to(task_ws)}{W}")
                if f.suffix in (".py", ".js", ".ts", ".md", ".txt", ".json", ".yaml", ".yml"):
                    content = f.read_text()[:500]
                    print(f"    {content}")

    elif action == "pull":
        if len(sys.argv) < 4:
            print(f"{R}Usage: workspace pull <task-id> [--dest PATH]{W}")
            return
        task_ws = WORKSPACE_DIR / sys.argv[3]
        dest = Path.cwd()
        if "--dest" in sys.argv:
            dest = Path(sys.argv[sys.argv.index("--dest") + 1])
        if not task_ws.exists():
            print(f"{R}Workspace not found.{W}")
            return
        code, out, err = await run_cmd(f"cp -r {task_ws}/* {dest}/")
        if code == 0:
            print(f"{G}Pulled workspace to {dest}{W}")
        else:
            print(f"{R}Failed: {err}{W}")

    elif action == "clean":
        code, out, err = await run_cmd(f"rm -rf {WORKSPACE_DIR}/*")
        state["tasks"] = []
        save_state(state)
        print(f"{G}All workspaces cleaned.{W}")


async def cmd_review():
    if len(sys.argv) < 3:
        print(f"{R}Usage: python3 openhands_agent.py review TASK_ID [--diff] [--apply PATH]{W}")
        return

    task_id = sys.argv[2]
    task_ws = WORKSPACE_DIR / task_id

    if not task_ws.exists():
        print(f"{R}Workspace not found for task {task_id}.{W}")
        return

    show_diff = "--diff" in sys.argv
    apply_path = None
    if "--apply" in sys.argv:
        apply_path = sys.argv[sys.argv.index("--apply") + 1]

    print(f"{BOLD}Reviewing task: {task_id}{W}\n")

    # List generated files
    files = list(task_ws.rglob("*")) if task_ws.exists() else []
    py_files = [f for f in files if f.is_file() and f.suffix in (".py", ".js", ".ts", ".go", ".rs", ".java")]

    if not py_files:
        print(f"{Y}No code files found in workspace.{W}")
        # Show all files
        all_files = [f for f in files if f.is_file()]
        if all_files:
            print(f"\nFiles in workspace:")
            for f in all_files:
                print(f"  - {f.relative_to(task_ws)}")
        return

    for f in py_files:
        print(f"\n{C}--- {f.relative_to(task_ws)} ---{W}")
        content = f.read_text()
        print(content[:2000])
        if len(content) > 2000:
            print(f"\n{Y}... ({len(content) - 2000} more bytes){W}")

    if show_diff:
        # Create a temp git repo to show diff
        print(f"\n{BOLD}Diff view:{W}")
        for f in py_files:
            print(f"\n{C}--- {f.relative_to(task_ws)} ---{W}")
            content = f.read_text()
            for i, line in enumerate(content.split("\n"), 1):
                if line.strip():
                    print(f"{G}+{line}{W}")

    if apply_path:
        target = Path(apply_path)
        target.mkdir(parents=True, exist_ok=True)
        code, out, err = await run_cmd(f"cp -r {task_ws}/* {target}/")
        if code == 0:
            print(f"\n{G}Code applied to {target}{W}")
        else:
            print(f"\n{R}Failed to apply: {err}{W}")


async def cmd_config():
    cfg = load_config()

    if "--model" in sys.argv:
        cfg["model"] = sys.argv[sys.argv.index("--model") + 1]
    if "--api-key" in sys.argv:
        cfg["api_key"] = sys.argv[sys.argv.index("--api-key") + 1]
    if "--sandbox" in sys.argv:
        cfg["sandbox"] = sys.argv[sys.argv.index("--sandbox") + 1]

    if "--model" in sys.argv or "--api-key" in sys.argv or "--sandbox" in sys.argv:
        save_config(cfg)
        print(f"{G}Config updated.{W}")

    print(f"{BOLD}OpenHands Configuration:{W}\n")
    print(f"  Model:    {C}{cfg['model']}{W}")
    key_display = cfg['api_key'][:8] + "..." if cfg.get("api_key") else f"{Y}not set{W}"
    print(f"  API Key:  {key_display}")
    print(f"  Sandbox:  {C}{cfg['sandbox']}{W}")
    print(f"  Port:     {C}{cfg['port']}{W}")
    print(f"\n{Y}Set values: python3 openhands_agent.py config --model M --api-key K --sandbox S{W}")


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    dispatch = {
        "start": cmd_start,
        "task": cmd_task,
        "status": cmd_status,
        "workspace": cmd_workspace,
        "review": cmd_review,
        "config": cmd_config,
    }

    handler = dispatch.get(cmd)
    if handler:
        await handler()
    else:
        print(__doc__)


if __name__ == "__main__":
    asyncio.run(main())
