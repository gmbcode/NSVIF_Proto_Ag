# main.py
import os
import re
import json
import subprocess
from dotenv import dotenv_values
from PIL import Image

# LLM Libraries
from google import genai
from openai import OpenAI

# Local imports
import prompts
from Z3_Verifier import verify_and_optimize_footprint

# Load Environment Variables
config = dotenv_values(".env")
gemini_api_key = config.get('GEMINI_API_KEY')
deepseek_api_key = config.get('DEEPSEEK_API_KEY')

# Initialize Clients
gemini_client = genai.Client(api_key=gemini_api_key)

# Initialize DeepSeek via NVIDIA API Endpoint
deepseek_client = OpenAI(
    api_key=deepseek_api_key,
    base_url="https://integrate.api.nvidia.com/v1"
)


def extract_python_code(text: str) -> str:
    """Extracts code from markdown code blocks."""
    match = re.search(r'```python\n(.*?)\n```', text, re.DOTALL)
    if match:
        return match.group(1)
    return text.strip()


def summarize_violations(z3_json_report: str) -> str:
    """Agent 2 (DeepSeek via NVIDIA) interprets the Z3 JSON and creates a feedback prompt."""
    print("\n[Agent 2 - DeepSeek] Analyzing Z3 Verifier report...")
    response = deepseek_client.chat.completions.create(
        model="deepseek-ai/deepseek-v4-pro",
        messages=[
            {"role": "system", "content": prompts.DEEPSEEK_SYSTEM_PROMPT},
            {"role": "user", "content": z3_json_report}
        ],
        temperature=1,
        top_p=0.95,
        max_tokens=16384,
        extra_body={"chat_template_kwargs": {"thinking": False}},
        stream=False
    )
    return response.choices[0].message.content


def run_agent_loop(image_path: str, max_iterations: int = 5):
    print(f"Starting Multi-Agent SBC Verification Loop (Max {max_iterations} iterations)\n")

    try:
        reference_image = Image.open(image_path)
    except Exception as e:
        print(f"Error loading image: {e}")
        return

    gemini_messages = [
        {"role": "user", "parts": [
            prompts.GEMINI_SYSTEM_PROMPT,
            reference_image,
            prompts.GEMINI_INITIAL_PROMPT
        ]}
    ]

    for iteration in range(1, max_iterations + 1):
        print(f"--- Iteration {iteration} ---")

        # 1. GENERATE: Ask Gemini to write/fix the DXF code
        print("[Agent 1 - Gemini Flash] Generating Python code...")

        contents = []
        for msg in gemini_messages:
            contents.extend(msg["parts"])

        gemini_response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=contents
        )

        response_text = gemini_response.text
        gemini_messages.append({"role": "model", "parts": [response_text]})

        # 2. EXTRACT & EXECUTE: Run the generated code
        script_code = extract_python_code(response_text)
        script_filename = "temp_generated_dxf_script.py"
        output_dxf = "generated_plot.dxf"

        with open(script_filename, "w") as f:
            f.write(script_code)

        print("[System] Executing generated Python script...")
        result = subprocess.run(["python", script_filename], capture_output=True, text=True)

        if result.returncode != 0:
            print("[System] Python execution failed! Sending error stack back to Gemini.")
            error_feedback = f"Your Python code threw an error during execution:\n{result.stderr}\nPlease fix the code."
            gemini_messages.append({"role": "user", "parts": [error_feedback]})
            continue

        # 3. VERIFY: Run the Z3 Solver on the resulting DXF
        if not os.path.exists(output_dxf):
            error_feedback = f"The script executed but failed to save the file as '{output_dxf}'."
            gemini_messages.append({"role": "user", "parts": [error_feedback]})
            continue

        print("[Z3 Solver] Evaluating constraints...")
        z3_json_str = verify_and_optimize_footprint(output_dxf)

        try:
            z3_report = json.loads(z3_json_str)
        except json.JSONDecodeError:
            print("[Error] Z3 Verifier did not output valid JSON.")
            break

        # 4. EVALUATE: Check if it passed
        if z3_report.get("status") == "APPROVED":
            print("\nSUCCESS! The design is approved and optimized by Z3.")
            print(f"Final Area: {z3_report['proposed_footprint']['calculated_area_sqft']} sq ft.")
            print(f"Final DXF File ready at: {output_dxf}")
            break
        else:
            print("REJECTED. Design contains violations.")

            # 5. CRITIQUE: DeepSeek summarizes the Z3 output
            deepseek_summary = summarize_violations(z3_json_str)
            print(f"\n[DeepSeek Summary]:\n{deepseek_summary}\n")

            next_prompt = prompts.GEMINI_FEEDBACK_PROMPT.format(feedback=deepseek_summary)
            gemini_messages.append({"role": "user", "parts": [next_prompt]})

    else:
        print(f"\nReached maximum iterations ({max_iterations}) without Z3 approval.")


if __name__ == "__main__":
    run_agent_loop("image_21bab4.png")