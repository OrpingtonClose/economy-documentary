import os
import httpx

key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
if not os.path.exists(key_path):
    print("DeepSeek key path does not exist")
    exit(1)

with open(key_path) as f:
    key = f.read().strip()

print("Key starts with:", key[:10] if key else "None")

headers = {
    "Authorization": f"Bearer {key}",
    "Content-Type": "application/json"
}

payload = {
    "model": "deepseek-chat",
    "messages": [
        {"role": "user", "content": "Say 'hello' and nothing else."}
    ],
    "max_tokens": 10
}

try:
    print("Sending request to DeepSeek API...")
    resp = httpx.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=payload, timeout=10.0)
    print("Status code:", resp.status_code)
    print("Response:", resp.text)
except Exception as e:
    print("Error:", e)
