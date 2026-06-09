import os
import sys
import torch
import psutil
from diffusers import LTXPipeline
from diffusers.utils import export_to_video

def print_memory():
    process = psutil.Process(os.getpid())
    print(f"Memory Usage: {process.memory_info().rss / (1024 ** 3):.2f} GB")

print("System Python Version:", sys.version)
print("PyTorch Version:", torch.__version__)
print("MPS Available:", torch.backends.mps.is_available())
print_memory()

model_id = "Lightricks/LTX-Video"

try:
    print(f"\nLoading LTXPipeline from local cache (repo: {model_id})...")
    # Load pipeline with local_files_only=True to ensure it uses the cached files
    pipe = LTXPipeline.from_pretrained(
        model_id,
        torch_dtype=torch.bfloat16,
        local_files_only=True
    )
    print("LTXPipeline loaded successfully!")
    print_memory()

    # Move to MPS or apply cpu offload
    if torch.backends.mps.is_available():
        print("\nEnabling CPU offload to MPS...")
        pipe.enable_model_cpu_offload(device=torch.device("mps"))
        print("CPU offload enabled.")
        print_memory()
    else:
        print("\nMPS is not available, using CPU...")
        pipe.to("cpu")
        print_memory()

    # Test generation with very low resolution and frame count to keep memory usage minimal
    # Default height=480, width=704, num_frames=161. Let's make it much smaller for testing.
    # LTX-Video supports dimensions divisible by 32. 
    # Let's use height=160, width=256, num_frames=17.
    prompt = "A simple red ball bouncing on a wooden floor, 3d render"
    print(f"\nGenerating test video with prompt: '{prompt}'...")
    print("Settings: height=160, width=256, num_frames=17, steps=5")
    
    video = pipe(
        prompt=prompt,
        height=160,
        width=256,
        num_frames=17,
        num_inference_steps=5,
    ).frames[0]
    
    print("\nGeneration completed! Exporting to output.mp4...")
    export_to_video(video, "scratch/output.mp4", fps=8)
    print("Video exported successfully to scratch/output.mp4!")
    
except Exception as e:
    print(f"\nAn error occurred: {e}")
    import traceback
    traceback.print_exc()
