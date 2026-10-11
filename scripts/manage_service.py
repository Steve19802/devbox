import os
import shutil
import sys

from devbox_config import (
    ConfigError,
    get_service,
    load_config,
    make_service,
    new_config,
    normalize_rel_path,
    remove_service,
    sanitize_service_name,
    save_config,
    upsert_service,
)
from scaffold import generate_architecture, get_available_runtimes, load_manifest

BUILD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


# --------------------------------------------------------------------------- #
# Compose-file parsing helpers
# --------------------------------------------------------------------------- #
def parse_service_names(yaml_text):
    names = []
    in_services = False
    for line in yaml_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if not in_services:
            if stripped == "services:" and indent == 0:
                in_services = True
            continue
        if indent == 0:
            break
        if indent == 2 and stripped.endswith(":"):
            names.append(stripped.rstrip(":"))
    return names


def get_existing_services(project_root):
    return get_services_from_file(os.path.join(project_root, "docker-compose.yml"))


def get_services_from_file(filepath):
    if not os.path.exists(filepath):
        return []
    with open(filepath) as f:
        return parse_service_names(f.read())


def get_all_services(project_root):
    yml = set(get_services_from_file(os.path.join(project_root, "docker-compose.yml")))
    dev = set(get_services_from_file(os.path.join(project_root, "docker-compose.dev.yml")))
    return yml | dev  # union, deduplicated


# --------------------------------------------------------------------------- #
# Compose block rendering (layout-aware: uses the chosen host path)
# --------------------------------------------------------------------------- #
def build_service_block_yaml(name, port, path):
    return f"""  {name}:
    build:
      context: ./{path}
    ports:
      - "{port}:{port}"
"""


def build_service_dev_block_yaml(name, rt, inject_ide, path):
    user_dir = rt["home_dir"]
    lines = [
        f"  {name}:",
        "    build:",
        "      target: dev",
        "      args:",
        "        - NEOVIM_VERSION=${NEOVIM_VERSION:-stable}",
        "        - LAZYGIT_VERSION=${LAZYGIT_VERSION:-stable}",
        '    user: "${DEVBOX_UID:-1000}:${DEVBOX_GID:-1000}"',
        "    volumes:",
        f"      - ./{path}:/app",
    ]
    if inject_ide:
        lines.extend(
            [
                f"      - ~/.config/nvim:{user_dir}/.config/nvim",
                f"      - ./{path}/.nvim/share/nvim:{user_dir}/.local/share/nvim",
                f"      - ./{path}/.nvim/state/nvim:{user_dir}/.local/state/nvim",
            ]
        )
    lines.extend(
        [
            "      - ./.opencode/skills:/var/devbox/skills:ro",
            "    environment:",
            f"      - HOME={user_dir}",
            f"      - DEVBOX_SERVICE={name}",
        ]
    )
    return "\n".join(lines) + "\n\n"


def append_to_yaml(filepath, block):
    if os.path.exists(filepath):
        with open(filepath) as f:
            content = f.read().rstrip() + "\n"
    else:
        content = "services:\n"
    content += block
    with open(filepath, "w") as f:
        f.write(content)


def remove_from_yaml(filepath, service_name):
    if not os.path.exists(filepath):
        return False
    with open(filepath) as f:
        content = f.read()

    lines = content.splitlines(keepends=True)
    result = []
    skip = False
    target_indent = None

    for line in lines:
        lstripped = line.lstrip()
        indent = len(line) - len(lstripped)
        token = lstripped.strip()

        if skip:
            if indent <= target_indent and token and not token.startswith("#"):
                skip = False
            else:
                continue

        if indent == 2 and token == f"{service_name}:":
            skip = True
            target_indent = indent
            continue

        result.append(line)

    new_content = "".join(result)
    if new_content != content:
        with open(filepath, "w") as f:
            f.write(new_content)
        return True
    return False


# --------------------------------------------------------------------------- #
# Dockerfile / IDE helpers
#
# ``scaffold.generate_dockerfile`` and ``scaffold.create_nvim_dirs`` hardcode
# ``src/<name>``. Rather than editing scaffold.py, these local helpers mirror
# the exact same template + IDE-injection-marker logic but write into an
# arbitrary host directory so layout-aware services land in their real folder.
# --------------------------------------------------------------------------- #
def render_dockerfile_content(svc, manifest, build_dir, inject_ide):
    rt = manifest["runtimes"][svc["runtime"]]
    dockerfile_name = rt["dockerfile"]
    src_file = os.path.join(build_dir, "templates", "dockerfiles", dockerfile_name)

    with open(src_file) as f:
        content = f.read()

    marker = "@DEVBOX_IDE_INJECTION_POINT@"

    if inject_ide:
        if f"# {marker}" in content:
            ide_path = os.path.join(build_dir, "templates", "ide_injection.dockerfile")
            if os.path.exists(ide_path):
                with open(ide_path) as f:
                    ide_snippet = f.read()
                content = content.replace(f"# {marker}", ide_snippet, 1)
        else:
            print(
                f"  Note: {svc['name']} Dockerfile has no IDE injection marker. "
                "IDE tools are pre-installed or not needed. Skipping."
            )
    else:
        content = "\n".join(
            line for line in content.split("\n") if marker not in line
        )

    return content


def write_dockerfile(svc, manifest, build_dir, inject_ide, dst_dir):
    os.makedirs(dst_dir, exist_ok=True)
    dst_file = os.path.join(dst_dir, "Dockerfile")

    if os.path.exists(dst_file):
        print(f"  Skipped {dst_file} (already exists)")
        return

    content = render_dockerfile_content(svc, manifest, build_dir, inject_ide)
    with open(dst_file, "w") as f:
        f.write(content)

    if svc["runtime"] == "python":
        req_file = os.path.join(dst_dir, "requirements.txt")
        if not os.path.exists(req_file):
            with open(req_file, "w") as f:
                f.write("")
            print(f"  Created {req_file}")

    print(f"  Created {dst_file}")


def create_nvim_dirs_at(base_dir):
    base = os.path.join(base_dir, ".nvim")
    os.makedirs(os.path.join(base, "share", "nvim"), exist_ok=True)
    os.makedirs(os.path.join(base, "state", "nvim"), exist_ok=True)


def regenerate_architecture(project_root, service_names):
    """Regenerate the marker-scoped architecture block (never deletes files)."""
    generate_architecture(service_names, project_root)


# --------------------------------------------------------------------------- #
# Interactive helpers
# --------------------------------------------------------------------------- #
def _ask_yes_no(prompt):
    """Read a yes/no answer. EOF (non-interactive stdin) counts as 'no'."""
    try:
        resp = input(prompt)
    except EOFError:
        print()
        return False
    return resp.strip().lower() in ("y", "yes")


def _confirm_continue_nonempty(path_display, cli_mode):
    print(f"  Warning: '{path_display}' already exists and is not empty.")
    if cli_mode:
        print(
            "  Continuing (non-interactive). Existing files are never overwritten."
        )
        return True
    return _ask_yes_no("  Continue anyway? [y/N]: ")


def _resolve_service_path(raw_path, project_root, name, cfg, cli_mode):
    """Validate/normalize the host path for a new service.

    Must stay inside the project, must not be the project root ("."), and must
    not already be the source folder of another devbox.json service.
    """
    candidate = raw_path if raw_path else f"src/{name}"
    while True:
        try:
            rel = normalize_rel_path(candidate, project_root)
        except ConfigError as exc:
            if cli_mode:
                print(f"Error: {exc}")
                sys.exit(1)
            print(f"  Invalid location: {exc}")
            candidate = input("  Location: ").strip()
            continue

        if rel == ".":
            msg = "a new service needs its own folder ('.' is the project root)"
            if cli_mode:
                print(f"Error: {msg}")
                sys.exit(1)
            print(f"  {msg}")
            candidate = input("  Location: ").strip()
            continue

        if cfg is not None:
            for svc in cfg.get("services", []):
                if svc.get("source") == rel:
                    print(
                        f"Error: '{rel}' is already the source of service "
                        f"'{svc.get('name')}'."
                    )
                    sys.exit(1)

        return rel


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_add(args, project_root, manifest):
    runtimes = get_available_runtimes(manifest)
    cli_mode = args.get("--name") is not None

    name_arg = args.get("--name")
    runtime_arg = args.get("--runtime")
    port_arg = args.get("--port")
    path_arg = args.get("--path")
    inject_ide = "--inject-ide" in args

    existing = get_existing_services(project_root)
    all_services = get_all_services(project_root)
    cfg = load_config(project_root)
    cfg_names = {s["name"] for s in cfg.get("services", [])} if cfg else set()
    known = all_services | cfg_names

    # ---- Service name -----------------------------------------------------
    if cli_mode:
        try:
            name = sanitize_service_name(name_arg)
        except ConfigError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
    else:
        print("Existing services:", ", ".join(sorted(known)) if known else "(none)")
        while True:
            raw = input("  Service name: ").strip()
            try:
                name = sanitize_service_name(raw)
            except ConfigError:
                print("  Invalid service name. Use letters, digits, '-' or '_'.")
                continue
            break

    if name in known:
        print(f"Error: Service '{name}' already exists.")
        sys.exit(1)

    # ---- Runtime ----------------------------------------------------------
    if cli_mode:
        runtime_key = runtime_arg or ""
        if runtime_key not in manifest["runtimes"]:
            print(
                f"Error: Unknown runtime '{runtime_key}'. "
                f"Available: {', '.join(runtimes)}"
            )
            sys.exit(1)
    else:
        print(f"  Runtime ({'/'.join(runtimes)}): ", end="")
        runtime_key = input().strip()
        while runtime_key not in manifest["runtimes"]:
            print(f"  Available: {', '.join(runtimes)}")
            runtime_key = input("  Runtime: ").strip()

    # ---- Port -------------------------------------------------------------
    if cli_mode:
        if port_arg is None or port_arg == "":
            port = manifest["runtimes"][runtime_key]["default_port"]
        else:
            try:
                port = int(port_arg)
            except (TypeError, ValueError):
                print(f"Error: Invalid port '{port_arg}'. Expected an integer.")
                sys.exit(1)
    else:
        default_port = manifest["runtimes"][runtime_key]["default_port"]
        while True:
            port_input = input(f"  Port [{default_port}]: ").strip()
            if not port_input:
                port = default_port
                break
            try:
                port = int(port_input)
                break
            except ValueError:
                print("  Invalid port: expected an integer.")

    # ---- Location ---------------------------------------------------------
    if cli_mode:
        loc_input = path_arg or ""
    else:
        loc_input = input(f"  Location [src/{name}]: ").strip()
    path = _resolve_service_path(loc_input, project_root, name, cfg, cli_mode)

    abs_path = os.path.join(project_root, path)

    # ---- Warn about a pre-existing, non-empty folder ----------------------
    if os.path.isdir(abs_path) and os.listdir(abs_path):
        if not _confirm_continue_nonempty(path, cli_mode):
            print("Aborted.")
            return

    # ---- Interactive-only IDE prompt --------------------------------------
    if not cli_mode:
        inject_input = (
            input("  Inject Containerized IDE (Neovim/Lazygit)? [y/N]: ")
            .strip()
            .lower()
        )
        inject_ide = inject_input in ("y", "yes")

    rt = manifest["runtimes"][runtime_key]
    svc_record = make_service(
        name,
        source=path,
        workdir="/app",
        context=path,
        dockerfile="Dockerfile",
        target="dev",
        port=port,
        runtime=runtime_key,
        managed=True,
    )

    # ---- Compose blocks (layout-aware) ------------------------------------
    block = build_service_block_yaml(name, port, path)
    append_to_yaml(os.path.join(project_root, "docker-compose.yml"), block)
    print("  Added to docker-compose.yml")

    dev_block = build_service_dev_block_yaml(name, rt, inject_ide, path)
    append_to_yaml(os.path.join(project_root, "docker-compose.dev.yml"), dev_block)
    print("  Added to docker-compose.dev.yml")

    # ---- Dockerfile + IDE dirs in the chosen folder -----------------------
    write_dockerfile(svc_record, manifest, BUILD_DIR, inject_ide, abs_path)

    if inject_ide:
        create_nvim_dirs_at(abs_path)

    # ---- Record in devbox.json -------------------------------------------
    if cfg is None:
        print("  Note: no devbox.json found; creating one in 'scaffold' mode.")
        cfg = new_config("scaffold", [])
    upsert_service(cfg, svc_record)
    save_config(project_root, cfg)
    print(f"  Recorded '{name}' in devbox.json ({path} -> /app, managed)")

    regenerate_architecture(project_root, existing + [name])

    print(f"\nService '{name}' added successfully!")
    print("Run 'devbox-build' and 'devbox-up' to start it.")


def _ownership(svc, cfg):
    """Return True when DevBox may delete the service's Dockerfile/source."""
    if svc is not None:
        return bool(svc.get("managed"))
    if cfg is None or cfg.get("mode") == "adopt":
        return False
    # Legacy scaffold project that predates the devbox.json service entry.
    return True


def cmd_remove(service_name, project_root):
    cfg = load_config(project_root)
    yml_path = os.path.join(project_root, "docker-compose.yml")
    dev_path = os.path.join(project_root, "docker-compose.dev.yml")

    yml_services = set(get_services_from_file(yml_path))
    dev_services = set(get_services_from_file(dev_path))
    cfg_names = {s["name"] for s in cfg.get("services", [])} if cfg else set()
    known = yml_services | dev_services | cfg_names

    if not service_name:
        if not known:
            print(
                "No services found in docker-compose.yml, docker-compose.dev.yml "
                "or devbox.json."
            )
            return
        print("Existing services:")
        for i, s in enumerate(sorted(known), 1):
            labels = []
            if s in yml_services:
                labels.append("docker-compose.yml")
            if s in dev_services:
                labels.append("docker-compose.dev.yml")
            if s in cfg_names:
                labels.append("devbox.json")
            print(f"  {i}. {s} ({', '.join(labels) or 'devbox.json'})")
        ordered = sorted(known)
        while True:
            try:
                choice = int(input("Select service to remove: "))
                if 1 <= choice <= len(ordered):
                    service_name = ordered[choice - 1]
                    break
            except ValueError:
                pass
            print("Invalid selection.")

    if service_name not in known:
        print(
            f"Error: Service '{service_name}' not found in docker-compose.yml, "
            "docker-compose.dev.yml or devbox.json."
        )
        sys.exit(1)

    svc = get_service(cfg, service_name) if cfg else None
    managed = _ownership(svc, cfg)

    if managed:
        if remove_from_yaml(yml_path, service_name):
            print("  Removed from docker-compose.yml")
        remove_from_yaml(dev_path, service_name)
        print("  Removed from docker-compose.dev.yml")

        source = (svc or {}).get("source") or f"src/{service_name}"
        context = (svc or {}).get("context") or source
        dockerfile_name = (svc or {}).get("dockerfile") or "Dockerfile"
        dockerfile_path = os.path.join(project_root, context, dockerfile_name)

        rel_dockerfile = os.path.relpath(dockerfile_path, project_root)
        if os.path.exists(dockerfile_path):
            if _ask_yes_no(f"  Delete Dockerfile at {rel_dockerfile}? [y/N]: "):
                os.remove(dockerfile_path)
                print("  Deleted Dockerfile")

        src_dir = os.path.join(project_root, source)
        if source == ".":
            # Never offer to delete the project root itself.
            print("  Source is the project root; leaving files in place.")
        elif os.path.isdir(src_dir) and os.listdir(src_dir):
            if _ask_yes_no(f"  Delete entire {source}/ directory? [y/N]: "):
                shutil.rmtree(src_dir)
                print(f"  Deleted {source}/")
    else:
        # User-owned (adopt / legacy without devbox.json): only the DevBox-owned
        # dev override may be edited. Everything else is left untouched.
        remove_from_yaml(dev_path, service_name)
        print("  Removed from docker-compose.dev.yml")

        source = (svc or {}).get("source")
        print("  Left in place (user-owned):")
        print(
            f"    - docker-compose.yml entry for '{service_name}' "
            "(remove it manually if you no longer want it)"
        )
        if source and source != ".":
            print(f"    - source folder '{source}/'")
        else:
            print("    - source files")
        print("    - Dockerfile / build files")

    # ---- devbox.json entry (both managed and unmanaged) -------------------
    if cfg is not None:
        if remove_service(cfg, service_name):
            save_config(project_root, cfg)
            print("  Removed from devbox.json")

    remaining = sorted(known - {service_name})
    regenerate_architecture(project_root, remaining)

    print(f"\nService '{service_name}' removed!")
    if remaining:
        print("Run 'devbox-build' to rebuild containers.")
    else:
        print("No services remain. Run 'devbox-init' to rebuild from scratch.")


def cmd_list(project_root):
    yml_services = set(
        get_services_from_file(os.path.join(project_root, "docker-compose.yml"))
    )
    dev_services = set(
        get_services_from_file(os.path.join(project_root, "docker-compose.dev.yml"))
    )
    cfg = load_config(project_root)
    cfg_map = {s["name"]: s for s in cfg.get("services", [])} if cfg else {}
    all_services = yml_services | dev_services | set(cfg_map)

    if not all_services:
        print(
            "No services defined in docker-compose.yml, docker-compose.dev.yml "
            "or devbox.json."
        )
        return

    print("Services:")
    for s in sorted(all_services):
        labels = []
        if s in yml_services:
            labels.append("docker-compose.yml")
        if s in dev_services:
            labels.append("docker-compose.dev.yml")
        line = f"  - {s} ({', '.join(labels)})" if labels else f"  - {s}"
        svc = cfg_map.get(s)
        if svc is not None:
            source = svc.get("source")
            workdir = svc.get("workdir")
            owner = "managed" if svc.get("managed") else "user"
            mapping = (
                f"{source if source is not None else '(image only)'} -> "
                f"{workdir if workdir is not None else '(n/a)'}"
            )
            line += f"  {mapping}  ({owner})"
        print(line)


def main():
    manifest = load_manifest(BUILD_DIR)
    project_root = os.path.abspath(".")

    if len(sys.argv) < 2:
        print("Usage: manage_service.py <add|remove|list> [args...]")
        sys.exit(1)

    command = sys.argv[1]
    args = sys.argv[2:]

    if command == "add":
        cli_args = {}
        i = 0
        while i < len(args):
            if args[i] == "--name" and i + 1 < len(args):
                cli_args["--name"] = args[i + 1]
                i += 2
            elif args[i] == "--runtime" and i + 1 < len(args):
                cli_args["--runtime"] = args[i + 1]
                i += 2
            elif args[i] == "--port" and i + 1 < len(args):
                cli_args["--port"] = args[i + 1]
                i += 2
            elif args[i] == "--path" and i + 1 < len(args):
                cli_args["--path"] = args[i + 1]
                i += 2
            elif args[i] == "--inject-ide":
                cli_args["--inject-ide"] = True
                i += 1
            else:
                i += 1
        cmd_add(cli_args, project_root, manifest)
    elif command == "remove":
        service_name = args[0] if args else ""
        cmd_remove(service_name, project_root)
    elif command == "list":
        cmd_list(project_root)
    else:
        print(f"Unknown command: {command}")
        print("Usage: manage_service.py <add|remove|list> [args...]")
        sys.exit(1)


if __name__ == "__main__":
    main()
