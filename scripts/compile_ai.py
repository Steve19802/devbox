import json
import os
import shutil

base_dir = os.environ.get("BASE_DIR")
proj_dir = os.environ.get("PROJ_DIR")
out_dir = os.environ.get("OUT_DIR")

if not all([base_dir, proj_dir, out_dir]):
    print("❌ Missing required environment variables: BASE_DIR, PROJ_DIR, OUT_DIR")
    exit(1)


def merge_dict(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and k in a and isinstance(a[k], dict):
            merge_dict(a[k], v)
        else:
            a[k] = v
    return a


def overlay_dirs(src, dst):
    if not os.path.exists(src):
        return
    os.makedirs(dst, exist_ok=True)
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if item.endswith(".json"):
            continue
        if os.path.isdir(s):
            overlay_dirs(s, d)
        else:
            shutil.copy2(s, d)


# A. Wipe the old compiled target directory to prevent ghost files
if os.path.exists(out_dir):
    shutil.rmtree(out_dir)
os.makedirs(out_dir, exist_ok=True)

# B. Overlay standard folders (Agents, Skills, etc.)
overlay_dirs(base_dir, out_dir)
overlay_dirs(proj_dir, out_dir)

# C. Dynamically find and merge ANY .json configuration files
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

print(f"✨ Successfully compiled {out_dir}")
