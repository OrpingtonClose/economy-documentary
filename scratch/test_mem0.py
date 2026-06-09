import os
from mem0 import Memory

# Ensure OpenAI API key or some dummy key is set, since mem0 uses OpenAI embedding models by default,
# or we can configure it to use a local embedding provider or pass a key.
# Wait, let's check what API keys we have. We saw that /Users/orpington/api_keys/openai_api_key.txt or gemini_api_key.txt was checked.
# Let's read the OpenAI API key if available.
openai_key = os.environ.get("OPENAI_API_KEY")
if not openai_key:
    for path in ["/Users/orpington/api_keys/openai_api_key.txt", "/Users/orpington/api_keys/LLMS/openai_api_key.txt"]:
        if os.path.exists(path):
            with open(path) as f:
                openai_key = f.read().strip()
                os.environ["OPENAI_API_KEY"] = openai_key
                break

config = {
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "path": "/tmp/documentary-pipeline/qdrant",
            "collection_name": "memories",
        }
    },
    "db": {
        "provider": "sqlite",
        "config": {
            "path": "/tmp/documentary-pipeline/mem0.db",
        }
    }
}

try:
    print("Initializing Memory...")
    m = Memory.from_config(config)
    print("Memory initialized successfully.")
    
    print("Adding memory...")
    res = m.add("The Czechia Vast.ai VM is using RTX 4090", user_id="scenario")
    print("Add response:", res)
    
    print("Searching memory...")
    search_res = m.search("Vast.ai VM", user_id="scenario")
    print("Search response:", search_res)
except Exception as e:
    import traceback
    traceback.print_exc()
