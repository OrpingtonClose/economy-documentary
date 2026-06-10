import os
import shutil

src_dir = "/Users/orpington/Documents/economy-documentary-work/tests/units"
dst_dir = "/Users/orpington/Documents/economy-documentary-work/scratch/tests_copy"

if os.path.exists(dst_dir):
    shutil.rmtree(dst_dir)
os.makedirs(dst_dir, exist_ok=True)

for name in os.listdir(src_dir):
    src_path = os.path.join(src_dir, name)
    if os.path.isfile(src_path):
        shutil.copy2(src_path, os.path.join(dst_dir, name))
        print(f"Copied {name}")
