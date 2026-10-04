#!/usr/bin/env python3
"""
devbox-adopt: Brownfield adoption wizard.

Scans an existing project (Dockerfile, compose, .env, package.json)
and generates only the missing pieces needed by devbox:
  - devbox.json          (project config)
  - docker-compose.dev.yml (dev overrides: volumes, ports, IDE)
  - .env.devbox          (devbox-managed vars: UID, GID)
  - ai/                  (empty, for project AI overrides)

No source files are moved or modified.
"""

import json
import os
import pwd
import sys


def find_existing_dockerfile(project_root):
    candidates = [
        os.path.join(project_root, "Dockerfile"),
        os.path.join(project_root, "src", "Dockerfile"),
        os.path.join(project_root, "src", "app", "Dockerfile"),
        os.path.join(project_root, "src", "web", "Dockerfile"),
        os.path.join(project_root, "src", "api", "Dockerfile"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def find_existing_compose(project_root):
    candidates = [
        os.path.join(project_root, "docker-compose.yml"),
        os.path.join(project_root, "docker-compose.yaml"),
        os.path.join(project_root, "compose.yml"),
        os.path.join(project_root, "compose.yaml"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def find_existing_env(project_root):
    candidates = [
        os.path.join(project_root, ".env"),
        os.path.join(project_root, ".env.local"),
        os.path.join(project_root, ".env.example"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def detect_service_name(project_root, compose_path=None):
    """Try to determine a reasonable service name from the project."""
    # First, read from existing docker-compose.yml (most reliable)
    if compose_path:
        try:
            import re
            with open(compose_path) as f:
                content = f.read()
            # Match first service name under services:
            match = re.search(r'^\s+(\w+):', content, re.MULTILINE)
            if match:
                return match.group(1)
        except (IOError, re.error):
            pass
    # Fallback: package.json name
    pkg_path = os.path.join(project_root, "package.json")
    if os.path.isfile(pkg_path):
        try:
            with open(pkg_path) as f:
                pkg = json.load(f)
            name = pkg.get("name", "")
            if name and name != "react-example":
                return name.replace("@", "").replace("/", "-")
        except (json.JSONDecodeError, IOError):
            pass
    # Fallback: directory name
    return os.path.basename(os.path.abspath(project_root))


def detect_dockerfile_stages(dockerfile_path):
    """Scan a Dockerfile and return a dict of stage names (FROM ... AS ...)."""
    stages = {}
    if not dockerfile_path:
        return stages
    try:
        with open(dockerfile_path) as f:
            for line in f:
                line = line.strip()
                if line.upper().startswith("FROM ") and " AS " in line.upper():
                    parts = line.split()
                    as_idx = [i for i, p in enumerate(parts) if p.upper() == "AS"]
                    if as_idx and as_idx[0] + 1 < len(parts):
                        stage_name = parts[as_idx[0] + 1]
                        stages[stage_name] = stage_name
    except IOError:
        pass
    return stages


def detect_port(project_root, dockerfile_path):
    """Try to detect the main service port."""
    # Check Dockerfile EXPOSE
    if dockerfile_path:
        try:
            with open(dockerfile_path) as f:
                for line in f:
                    if line.strip().upper().startswith("EXPOSE"):
                        parts = line.split()
                        for p in parts[1:]:
                            try:
                                return int(p)
                            except ValueError:
                                pass
        except IOError:
            pass
    # Check package.json scripts for common dev ports
    pkg_path = os.path.join(project_root, "package.json")
    if os.path.isfile(pkg_path):
        try:
            with open(pkg_path) as f:
                pkg = json.load(f)
            scripts = pkg.get("scripts", {})
            for name, cmd in scripts.items():
                if "5173" in cmd:
                    return 5173
                if "3000" in cmd:
                    return 3000
                if "8080" in cmd:
                    return 8080
                if "8000" in cmd:
                    return 8000
        except (json.JSONDecodeError, IOError):
            pass
    return 5173  # Vite default


def read_env_keys(env_path):
    """Read variable names from a .env file without parsing values."""
    keys = []
    if not env_path:
        return keys
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    keys.append(line.split("=", 1)[0].strip())
    except IOError:
        pass
    return keys


def prompt(msg, default=None):
    """Simple interactive prompt with optional default."""
    if default is not None:
        result = input(f"{msg} [{default}]: ").strip()
        return result if result else default
    return input(f"{msg}: ").strip()


def confirm(msg, default=True):
    """Yes/no prompt."""
    suffix = " [Y/n]" if default else " [y/N]"
    result = input(f"{msg}{suffix}: ").strip().lower()
    if not result:
        return default
    return result in ("y", "yes")


def generate_devbox_json(project_root, cfg):
    """Generate devbox.json at project root."""
    path = os.path.join(project_root, "devbox.json")
    if os.path.exists(path):
        overwrite = confirm(f"  {path} already exists. Overwrite?", default=False)
        if not overwrite:
            print("  Skipped.")
            return False

    content = json.dumps(cfg, indent=2) + "\n"
    with open(path, "w") as f:
        f.write(content)
    print(f"  Created {path}")
    return True


def generate_compose_dev(project_root, cfg):
    """Generate docker-compose.dev.yml with dev overrides."""
    path = os.path.join(project_root, "docker-compose.dev.yml")
    if os.path.exists(path):
        overwrite = confirm(f"  {path} already exists. Overwrite?", default=False)
        if not overwrite:
            print("  Skipped.")
            return False

    svc = cfg["service"]
    context = cfg.get("dockerfile_context", ".")
    dockerfile = cfg.get("dockerfile", "Dockerfile")

    buf = [
        "services:",
        f"  {svc}:",
        "    build:",
        f"      context: {context}",
        f"      dockerfile: {dockerfile}",
    ]

    # Only add target if the Dockerfile has a dev stage or IDE is injected
    has_dev_stage = "dev" in cfg.get("stages", {})
    if cfg.get("inject_ide") or has_dev_stage:
        buf.append("      target: dev")

    buf.append("    ports:")

    # Add app port
    app_port = cfg.get("port", 5173)
    buf.append(f'      - "{app_port}:{app_port}"')

    # Add Firebase emulator ports if detected
    if cfg.get("has_firebase"):
        emulator_ports = [
            "9099:9099",   # Auth
            "8080:8080",   # Firestore
            "5001:5001",   # Functions
            "4001:4001",   # Emulator UI
            "6001:6001",   # Hosting
        ]
        for p in emulator_ports:
            buf.append(f'      - "{p}"')

    buf.append("    volumes:")
    buf.append("      - .:/app")
    buf.append("      - /app/node_modules")

    fn_dir = cfg.get("functions_dir")
    if fn_dir:
        buf.append(f"      - /app/{fn_dir}/node_modules")

    if cfg.get("inject_ide"):
        user_dir = cfg.get("user_home_dir", "/home/node")
        buf.append(f"      - ~/.config/nvim:{user_dir}/.config/nvim")
        buf.append(f"      - ./.nvim/share/nvim:{user_dir}/.local/share/nvim")
        buf.append(f"      - ./.nvim/state/nvim:{user_dir}/.local/state/nvim")

    buf.append("      - ./.opencode/skills:/var/devbox/skills:ro")

    buf.append("    env_file:")
    buf.append("      - .env")
    buf.append("      - .env.devbox")

    buf.append("")
    if cfg.get("has_firebase"):
        buf.append("  # Firebase emulator UI (standalone service)")
        buf.append("  # Runs alongside the app for local Firebase development")
        buf.append("")

    content = "\n".join(buf)
    with open(path, "w") as f:
        f.write(content)
    print(f"  Created {path}")
    return True


def generate_env_devbox(project_root):
    """Generate .env.devbox with UID/GID."""
    path = os.path.join(project_root, ".env.devbox")
    if os.path.exists(path):
        print(f"  Skipped {path} (already exists)")
        return False

    uid = os.environ.get("LOCAL_UID", str(os.getuid() if hasattr(os, "getuid") else 1000))
    gid = os.environ.get("LOCAL_GID", str(os.getgid() if hasattr(os, "getgid") else 1000))

    content = f"""# DevBox-managed environment variables
# Auto-generated by devbox-adopt. Do not edit manually unless you know what you're doing.

UID={uid}
GID={gid}

# Can be 'stable', 'nightly', or a specific tag like 'v0.10.0'
# It is recommended to match with the host version
NEOVIM_VERSION=v0.11.6
LAZYGIT_VERSION=0.60.0
"""
    with open(path, "w") as f:
        f.write(content)
    print(f"  Created {path}")
    return True


def create_ai_dir(project_root):
    """Create ai/ directory if it doesn't exist."""
    path = os.path.join(project_root, "ai")
    os.makedirs(path, exist_ok=True)
    print(f"  Ensured {path}/ exists")


def scan_adopt(project_root):
    """Main adoption logic."""
    print("\n🔍 Scanning project...\n")

    dockerfile_path = find_existing_dockerfile(project_root)
    compose_path = find_existing_compose(project_root)
    env_path = find_existing_env(project_root)
    existing_env_keys = read_env_keys(env_path)

    print(f"  Project root:      {project_root}")
    print(f"  Dockerfile:         {dockerfile_path or 'NOT FOUND'}")
    print(f"  docker-compose.yml: {compose_path or 'NOT FOUND'}")
    print(f"  .env file:          {env_path or 'NOT FOUND'}")
    if existing_env_keys:
        print(f"  Existing env vars:  {', '.join(existing_env_keys)}")

    # Detect service info
    detected_name = detect_service_name(project_root, compose_path)
    detected_stages = detect_dockerfile_stages(dockerfile_path)
    detected_port = detect_port(project_root, dockerfile_path)

    # Determine Dockerfile context path
    if dockerfile_path:
        df_rel = os.path.relpath(dockerfile_path, project_root)
        df_dir = os.path.dirname(df_rel)
        dockerfile_context = df_dir if df_dir else "."
        dockerfile_name = os.path.basename(dockerfile_path)
    else:
        dockerfile_context = "."
        dockerfile_name = "Dockerfile"

    # Detect Firebase
    has_firebase = os.path.isfile(os.path.join(project_root, "firebase.json"))
    has_functions = os.path.isdir(os.path.join(project_root, "functions"))
    functions_dir = "functions" if has_functions else None

    # Detect project type from package.json
    project_type = "node"
    pkg_path = os.path.join(project_root, "package.json")
    if os.path.isfile(pkg_path):
        try:
            with open(pkg_path) as f:
                pkg = json.load(f)
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "firebase" in deps:
                project_type = "firebase"
        except (json.JSONDecodeError, IOError):
            pass

    print()
    print(f"  Detected service name:  {detected_name}")
    print(f"  Detected Dockerfile:    {dockerfile_path or 'will create'}")
    print(f"  Detected port:          {detected_port}")
    print(f"  Has Firebase:           {'yes' if has_firebase else 'no'}")
    print(f"  Has Functions:          {'yes' if has_functions else 'no'}")
    print(f"  Project type:           {project_type}")
    print()

    # Interactive configuration
    if not confirm("Continue with adoption?"):
        print("Aborted.")
        return

    service_name = prompt("Service name", default=detected_name)
    app_port = int(prompt("App port", default=str(detected_port)))
    inject_ide = confirm("Inject containerized IDE (Neovim/Lazygit)?", default=False)
    has_fb = confirm("Does this project use Firebase?", default=has_firebase)

    # Preserved env vars: detect from existing .env + common vars
    default_preserved = list(existing_env_keys)
    if has_fb:
        for v in ["GEMINI_API_KEY", "FIREBASE_PROJECT_ID"]:
            if v not in default_preserved:
                default_preserved.append(v)
    preserved = prompt(
        "Env vars to preserve (comma-separated)",
        default=", ".join(default_preserved) if default_preserved else "none"
    )
    preserved_list = [v.strip() for v in preserved.split(",") if v.strip() and v.strip() != "none"]

    print("\n⚙️  Generating devbox configuration...\n")

    # Build config
    cfg = {
        "version": 1,
        "service": service_name,
        "dockerfile": dockerfile_name,
        "dockerfile_context": dockerfile_context,
        "port": app_port,
        "stages": detected_stages,
        "inject_ide": inject_ide,
        "has_firebase": has_fb,
        "preserve_env": preserved_list,
        "compose_files": [
            "docker-compose.yml",
            "docker-compose.dev.yml",
            "build/docker-compose.mcp.yml"
        ],
    }
    if functions_dir:
        cfg["functions_dir"] = functions_dir

    generate_devbox_json(project_root, cfg)
    generate_compose_dev(project_root, cfg)
    generate_env_devbox(project_root)
    create_ai_dir(project_root)

    print()
    print("✅ Adoption complete!")
    print()
    print("Next steps:")
    print(f"  1. Source the devbox environment:  source build/envsetup.sh")
    print(f"  2. Review generated files:")
    print(f"     - devbox.json           (project config)")
    print(f"     - docker-compose.dev.yml (dev overrides)")
    print(f"     - .env.devbox           (UID/GID)")
    print(f"  3. Start the environment:   devbox-up")
    print(f"  4. (Optional) Setup AI:     devbox-ai-setup")
    print()


def main():
    project_root = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    project_root = os.path.abspath(project_root)

    if not os.path.isdir(project_root):
        print(f"Error: {project_root} is not a directory")
        sys.exit(1)

    scan_adopt(project_root)


if __name__ == "__main__":
    main()
