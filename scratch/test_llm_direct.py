import os
import asyncio
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel

async def test_direct():
    print("Testing DeepSeek via OpenAIChatModel...")
    api_key = ""
    _deepseek_key_path = "/Users/orpington/api_keys/LLMS/deepseek_api.txt"
    if os.path.exists(_deepseek_key_path):
        with open(_deepseek_key_path) as f:
            api_key = f.read().strip()
    
    # We try both settings
    print(f"Loaded key: {api_key[:5]}...")
    os.environ["DEEPSEEK_API_KEY"] = api_key
    
    # Let's try OpenAIChatModel with provider="deepseek"
    try:
        model = OpenAIChatModel(
            "deepseek-chat",
            provider="deepseek",
        )
        agent = Agent(model, system_prompt="You are a helpful assistant.")
        print("Running agent with provider='deepseek'...")
        result = await agent.run("Hello, answer in one word.")
        print(f"Result: {result.output}")
    except Exception as e:
        print(f"Error with provider='deepseek': {e}")

    # Let's try OpenAIChatModel with custom base_url
    try:
        model = OpenAIChatModel(
            "deepseek-chat",
            base_url="https://api.deepseek.com",
            api_key=api_key
        )
        agent = Agent(model, system_prompt="You are a helpful assistant.")
        print("Running agent with custom base_url...")
        result = await agent.run("Hello, answer in one word.")
        print(f"Result: {result.output}")
    except Exception as e:
        print(f"Error with custom base_url: {e}")

if __name__ == "__main__":
    asyncio.run(test_direct())
