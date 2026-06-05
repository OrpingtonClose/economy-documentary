import os
import sys
# Ensure server directory is in path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent_base import make_agent_app

if __name__ == "__main__":
    import uvicorn
    import importlib
    
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8003
    test_module_path = sys.argv[2] if len(sys.argv) > 2 else None
    
    extra_caps = []
    if test_module_path:
        test_module = importlib.import_module(test_module_path)
        for name, obj in vars(test_module).items():
            if isinstance(obj, type) and (name.endswith("Simulator") or name.endswith("Capability")):
                extra_caps.append(obj())
                
    app = make_agent_app("video", extra_capabilities=extra_caps)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
else:
    app = make_agent_app("video")
