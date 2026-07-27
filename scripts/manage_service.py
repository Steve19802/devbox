import json
import os
import shutil
import sys

from scaffold import (
    create_nvim_dirs,
    generate_architecture,
    generate_dockerfile,
    get_available_runtimes,
    load_manifest,
)

BUILD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


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


def build_service_block_yaml(name, port):
    return f"""  {name}:
    build:
      context: ./src/{name}
    ports:
      - "{port}:{port}"
"""


def build_service_dev_block_yaml(name, rt, inject_ide):
    user_dir = rt["home_dir"]
    lines = [
        f"  {name}:",
        "    build:",
        "      target: dev",
        "      args:",
        "        - NEOVIM_VERSION=${NEOVIM_VERSION:-stable}",
        "        - LAZYGIT_VERSION=${LAZYGIT_VERSION:-stable}",
        '    user: "${UID}:${GID}"',
        "    volumes:",
        f"      - ./src/{name}:/app",
    ]
    if inject_ide:
        lines.extend(
            [
                f"      - ~/.config/nvim:{user_dir}/.config/nvim",
                f"      - ./src/{name}/.nvim/share/nvim:{user_dir}/.local/share/nvim",
                f"      - ./src/{name}/.nvim/state/nvim:{user_dir}/.local/state/nvim",
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
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        if skip:
            if indent <= target_indent and stripped and not stripped.startswith("#"):
                skip = False
            else:
                continue

        if indent == 2 and stripped == f"{service_name}:":
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


def regenerate_architecture(project_root, service_names):
    path = os.path.join(project_root, "ARCHITECTURE.md")
    if os.path.exists(path):
        os.remove(path)
    services = [{"name": n} for n in service_names]
    generate_architecture(services, project_root)


def cmd_add(args, project_root, manifest):
    existing = get_existing_services(project_root)
    all_services = get_all_services(project_root)
    runtimes = get_available_runtimes(manifest)

    name = args.get("--name") or ""
    runtime_key = args.get("--runtime") or ""
    port_str = args.get("--port") or ""
    inject_ide = "--inject-ide" in args

    if not name:
        print(
            "Existing services:",
            ", ".join(existing) if existing else "(none)",
        )
        name = input("  Service name: ").strip()
        while not name:
            name = input("  Service name (required): ").strip()
        if name in existing:
            print(f"Error: Service '{name}' already exists.")
            sys.exit(1)

        print(f"  Runtime ({'/'.join(runtimes)}): ", end="")
        runtime_key = input().strip()
        while runtime_key not in manifest["runtimes"]:
            print(f"  Available: {', '.join(runtimes)}")
            runtime_key = input("  Runtime: ").strip()

        default_port = manifest["runtimes"][runtime_key]["default_port"]
        port_input = input(f"  Port [{default_port}]: ").strip()
        port_str = port_input if port_input else str(default_port)

        inject_input = (
            input("  Inject Containerized IDE (Neovim/Lazygit)? [y/N]: ")
            .strip()
            .lower()
        )
        inject_ide = inject_input in ("y", "yes")

    if name in all_services:
        print(f"Error: Service '{name}' already exists in docker-compose.yml or docker-compose.dev.yml.")
        sys.exit(1)

    if runtime_key not in manifest["runtimes"]:
        print(f"Unknown runtime '{runtime_key}'.")
        sys.exit(1)

    port = int(port_str)
    rt = manifest["runtimes"][runtime_key]

    svc = {"name": name, "runtime": runtime_key, "port": port}

    block = build_service_block_yaml(name, port)
    append_to_yaml(os.path.join(project_root, "docker-compose.yml"), block)
    print(f"  Added to docker-compose.yml")

    dev_block = build_service_dev_block_yaml(name, rt, inject_ide)
    append_to_yaml(os.path.join(project_root, "docker-compose.dev.yml"), dev_block)
    print(f"  Added to docker-compose.dev.yml")

    generate_dockerfile(svc, manifest, BUILD_DIR, inject_ide, project_root)

    if inject_ide:
        create_nvim_dirs([svc], project_root)

    if runtime_key == "python":
        req_file = os.path.join(project_root, "src", name, "requirements.txt")
        if not os.path.exists(req_file):
            with open(req_file, "w") as f:
                f.write("")
            print(f"  Created {req_file}")

    regenerate_architecture(project_root, existing + [name])

    print(f"\nService '{name}' added successfully!")
    print("Run 'devbox-build' and 'devbox-up' to start it.")


def cmd_remove(service_name, project_root):
    existing = get_existing_services(project_root)
    all_services = get_all_services(project_root)

    if not service_name:
        if not all_services:
            print("No services found in docker-compose.yml or docker-compose.dev.yml.")
            return
        print("Existing services:")
        for i, s in enumerate(sorted(all_services), 1):
            src = "yml" if s in get_services_from_file(os.path.join(project_root, "docker-compose.yml")) else "dev.yml"
            print(f"  {i}. {s} ({src})")
        while True:
            try:
                choice = int(input("Select service to remove: "))
                if 1 <= choice <= len(all_services):
                    service_name = sorted(all_services)[choice - 1]
                    break
            except ValueError:
                pass
            print("Invalid selection.")

    if service_name not in all_services:
        print(f"Error: Service '{service_name}' not found in docker-compose.yml or docker-compose.dev.yml.")
        sys.exit(1)

    removed = remove_from_yaml(
        os.path.join(project_root, "docker-compose.yml"), service_name
    )
    remove_from_yaml(os.path.join(project_root, "docker-compose.dev.yml"), service_name)

    dockerfile_path = os.path.join(project_root, "src", service_name, "Dockerfile")
    if os.path.exists(dockerfile_path):
        os.remove(dockerfile_path)
        print(f"  Deleted Dockerfile")

    src_dir = os.path.join(project_root, "src", service_name)
    if os.path.isdir(src_dir) and os.listdir(src_dir):
        resp = (
            input(f"  Delete entire src/{service_name}/ directory? [y/N]: ")
            .strip()
            .lower()
        )
        if resp in ("y", "yes"):
            shutil.rmtree(src_dir)
            print(f"  Deleted src/{service_name}/")

    remaining = get_existing_services(project_root)
    regenerate_architecture(project_root, remaining)

    print(f"\nService '{service_name}' removed!")
    if remaining:
        print("Run 'devbox-build' to rebuild containers.")
    else:
        print("No services remain. Run 'devbox-init' to rebuild from scratch.")


def cmd_list(project_root):
    yml_services = set(get_services_from_file(os.path.join(project_root, "docker-compose.yml")))
    dev_services = set(get_services_from_file(os.path.join(project_root, "docker-compose.dev.yml")))
    all_services = yml_services | dev_services

    if not all_services:
        print("No services defined in docker-compose.yml or docker-compose.dev.yml.")
    else:
        print("Services:")
        for s in sorted(all_services):
            labels = []
            if s in yml_services:
                labels.append("docker-compose.yml")
            if s in dev_services:
                labels.append("docker-compose.dev.yml")
            print(f"  - {s} ({', '.join(labels)})")


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
