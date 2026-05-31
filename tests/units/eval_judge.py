import os
import json
import litellm
import sys
from pathlib import Path

# Add project root to python path to read configurations
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

import config

# Setup DeepSeek API key for litellm
if config.DEEPSEEK_API:
    os.environ["DEEPSEEK_API_KEY"] = config.DEEPSEEK_API

MODEL_NAME = "deepseek/deepseek-chat"

def call_llm_eval_judge(prompt: str) -> tuple[float, str]:
    """Runs a semantic evaluation query against the configured DeepSeek LLM.
    
    Returns:
        tuple[float, str]: (score between 0.0 and 1.0, feedback explanation text)
    """
    try:
        response = litellm.completion(
            model=MODEL_NAME,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a precise, objective semantic evaluation judge. "
                        "Evaluate the user query carefully and output ONLY a valid "
                        "JSON object with two keys: "
                        "1. 'score': a float value between 0.0 (fail/incoherent) and 1.0 (pass/fully aligned). "
                        "2. 'feedback': a short string explaining the score and any discrepancies found."
                    )
                },
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )
        content = response.choices[0].message.content
        result = json.loads(content)
        return float(result.get("score", 0.0)), result.get("feedback", "No feedback provided")
    except Exception as e:
        return 0.0, f"LLM evaluation failed due to error: {str(e)}"
