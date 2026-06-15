import json
import os
import shutil
import subprocess


def merge_dict(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and k in a and isinstance(a[k], dict):
            merge_dict(a[k], v)
        else:
            a[k] = v
    return a


def safe_rmtree(path):
    if os.path.exists(path):
        subprocess.run(["chmod", "-R", "u+w", path], check=False)
        shutil.rmtree(path)


def overlay_dirs(src, dst):
    if not os.path.exists(src):
        return
    os.makedirs(dst, exist_ok=True)
    os.chmod(dst, 0o755)
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if item.endswith(".json"):
            continue
        if os.path.isdir(s):
            overlay_dirs(s, d)
        else:
            shutil.copy2(s, d)


def compile_ai(base_dir, proj_dir, out_dir):
    safe_rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    overlay_dirs(base_dir, out_dir)
    overlay_dirs(proj_dir, out_dir)

    base_jsons = (
        set(f for f in os.listdir(base_dir) if f.endswith(".json"))
        if os.path.exists(base_dir)
        else set()
    )
    proj_jsons = (
        set(f for f in os.listdir(proj_dir) if f.endswith(".json"))
        if os.path.exists(proj_dir)
        else set()
    )

    for jf in base_jsons.union(proj_jsons):
        b_path = os.path.join(base_dir, jf)
        p_path = os.path.join(proj_dir, jf)
        o_path = os.path.join(out_dir, jf)

        b_data = {}
        if os.path.exists(b_path):
            with open(b_path) as f:
                b_data = json.load(f)

        p_data = {}
        if os.path.exists(p_path):
            with open(p_path) as f:
                p_data = json.load(f)

        merged = merge_dict(b_data, p_data)
        with open(o_path, "w") as f:
            json.dump(merged, f, indent=2)


def main():
    base_dir = os.environ.get("BASE_DIR")
    proj_dir = os.environ.get("PROJ_DIR")
    out_dir = os.environ.get("OUT_DIR")

    if not all([base_dir, proj_dir, out_dir]):
        print("Missing required environment variables: BASE_DIR, PROJ_DIR, OUT_DIR")
        exit(1)

    compile_ai(base_dir, proj_dir, out_dir)
    print(f"Successfully compiled {out_dir}")


if __name__ == "__main__":
    main()
