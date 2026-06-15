import os
import sys

from compile_ai import compile_ai


def discover_tools(build_dir):
    ai_dir = os.path.join(build_dir, "ai")
    if not os.path.isdir(ai_dir):
        return []
    return sorted(
        d for d in os.listdir(ai_dir) if os.path.isdir(os.path.join(ai_dir, d))
    )


def interactive_menu(tools):
    tools = tools + ["none"]
    print("DevBox AI Toolchain - Select your IDE/Agent:")
    for i, opt in enumerate(tools, 1):
        print(f"  {i}. {opt}")
    while True:
        try:
            choice = input(f"Enter choice (1-{len(tools)}): ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(tools):
                return tools[idx]
        except ValueError:
            pass
        print("Invalid selection.")


def main():
    build_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    project_root = os.path.abspath(os.path.join(build_dir, ".."))

    available = discover_tools(build_dir)

    target_tool = sys.argv[1] if len(sys.argv) > 1 else None

    if not target_tool:
        if not available:
            print("No AI tools found in build/ai/. Nothing to do.")
            return
        target_tool = interactive_menu(available)

    if target_tool == "none":
        print("AI toolchain compilation skipped.")
        return

    base_dir = os.path.join(build_dir, "ai", target_tool)
    if not os.path.isdir(base_dir):
        print(f"Unknown AI tool: {target_tool}")
        sys.exit(1)

    proj_dir = os.path.join(project_root, "ai", target_tool)
    out_dir = os.path.join(project_root, f".{target_tool}")

    print(f"Compiling AI workspace for: {target_tool}")
    compile_ai(base_dir, proj_dir, out_dir)
    print(f"Activated {target_tool}! You can now launch your IDE.")


if __name__ == "__main__":
    main()
