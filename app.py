import os
import re
import json
import subprocess
import streamlit as st
from PIL import Image
from dotenv import dotenv_values

# Visualization libraries for DXF
import ezdxf
import matplotlib.pyplot as plt
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

# LLM Libraries
from google import genai
from openai import OpenAI

# Local imports
import prompts
from Z3_Verifier import verify_bellevue_layout

# ==========================================
# PAGE CONFIGURATION & SETUP
# ==========================================
st.set_page_config(
    page_title="SBC Multi-Agent Architecture Verifier",
    page_icon="🏗️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Load Environment Variables
config = dotenv_values(".env")
DEFAULT_GEMINI_KEY = config.get('GEMINI_API_KEY', "")
DEFAULT_DEEPSEEK_KEY = config.get('DEEPSEEK_API_KEY', "")


# ==========================================
# HELPER FUNCTIONS
# ==========================================
def extract_python_code(text: str) -> str:
    """Extracts code from markdown code blocks."""
    match = re.search(r'```python\n(.*?)\n```', text, re.DOTALL)
    if match:
        return match.group(1)
    return text.strip()


def render_dxf_to_figure(dxf_path: str):
    """Reads a DXF file and renders it to a matplotlib figure for Streamlit."""
    try:
        doc = ezdxf.readfile(dxf_path)
        msp = doc.modelspace()

        # Set dark theme or light theme based on preference; we'll use a clean white background
        fig = plt.figure(figsize=(10, 10), facecolor='white')
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_facecolor('white')

        ctx = RenderContext(doc)
        ctx.set_current_layout(msp)
        out = MatplotlibBackend(ax)

        Frontend(ctx, out).draw_layout(msp, finalize=True)
        return fig
    except Exception as e:
        st.error(f"Failed to render DXF: {str(e)}")
        return None


# ==========================================
# AGENT FUNCTIONS
# ==========================================
def summarize_violations(deepseek_client, z3_json_report: str) -> str:
    """Agent 2 (DeepSeek via NVIDIA) interprets the Z3 JSON and creates a feedback prompt."""
    response = deepseek_client.chat.completions.create(
        model="deepseek-ai/deepseek-v4-flash",
        messages=[
            {"role": "system", "content": prompts.DEEPSEEK_SYSTEM_PROMPT},
            {"role": "user", "content": z3_json_report}
        ],
        temperature=1,
        top_p=0.95,
        max_tokens=4096,
        stream=False
    )
    return response.choices[0].message.content


def run_agent_loop_ui(image, gemini_client, deepseek_client, max_iterations, bellevue_config):
    """Executes the multi-agent loop, updating Streamlit UI components in real-time."""

    # UI Containers for real-time updates
    progress_bar = st.progress(0)
    status_text = st.empty()

    col_viz, col_logs = st.columns([1.2, 1])

    with col_viz:
        st.subheader("Live Output Rendering")
        viz_container = st.empty()
        viz_container.info("Awaiting first DXF generation...")
        metrics_container = st.empty()

    with col_logs:
        st.subheader("Agent Communication Logs")
        log_container = st.container()

    gemini_messages = [
        {"role": "user", "parts": [
            prompts.GEMINI_SYSTEM_PROMPT,
            image,
            prompts.GEMINI_INITIAL_PROMPT
        ]}
    ]

    for iteration in range(1, max_iterations + 1):
        progress_fraction = iteration / max_iterations
        progress_bar.progress(progress_fraction)
        status_text.markdown(f"**Iteration {iteration} of {max_iterations}** - Running agents...")

        with log_container:
            with st.expander(f"Iteration {iteration} Details", expanded=True):

                # 1. GENERATE
                with st.spinner("Agent 1 (Gemini) is generating architecture code..."):
                    contents = []
                    for msg in gemini_messages:
                        contents.extend(msg["parts"])

                    try:
                        gemini_response = gemini_client.models.generate_content(
                            model='gemini-3.5-flash',
                            contents=contents
                        )
                        response_text = gemini_response.text
                        gemini_messages.append({"role": "model", "parts": [response_text]})
                    except Exception as e:
                        st.error(f"Gemini API Error: {e}")
                        break

                script_code = extract_python_code(response_text)
                st.markdown("**Agent 1 Generated Code:**")
                st.code(script_code, language="python", line_numbers=True)

                # 2. EXTRACT & EXECUTE
                script_filename = "temp_generated_dxf_script.py"
                output_dxf = "generated_plot.dxf"

                with open(script_filename, "w", encoding="utf-8") as f:
                    f.write(script_code)

                with st.spinner("Executing script & rendering DXF..."):
                    result = subprocess.run(["python", script_filename], capture_output=True, text=True)

                if result.returncode != 0:
                    st.error("Python execution failed! Sending error stack back to Gemini.")
                    st.code(result.stderr, language="bash")
                    error_feedback = f"Your Python code threw an error during execution:\n{result.stderr}\nPlease fix the code."
                    gemini_messages.append({"role": "user", "parts": [error_feedback]})
                    continue

                if not os.path.exists(output_dxf):
                    st.error(f"Execution finished but '{output_dxf}' was not found.")
                    error_feedback = f"The script executed but failed to save the file as '{output_dxf}'."
                    gemini_messages.append({"role": "user", "parts": [error_feedback]})
                    continue

                # UPDATE VIZUALIZATION
                fig = render_dxf_to_figure(output_dxf)
                if fig:
                    viz_container.pyplot(fig)
                    plt.close(fig)  # Free memory

                # 3. VERIFY WITH Z3
                with st.spinner("Z3 Solver evaluating constraints..."):
                    z3_json_str = verify_bellevue_layout(output_dxf, bellevue_config)

                    try:
                        z3_report = json.loads(z3_json_str)
                    except json.JSONDecodeError:
                        st.error("Z3 Verifier did not output valid JSON.")
                        break

                # 4. EVALUATE
                if z3_report.get("status") == "APPROVED":
                    st.success("✅ SUCCESS! The design is approved and optimized by Z3.")
                    status_text.success("Verification Complete. Layout Approved.")

                    total_area = z3_report.get("metrics", {}).get("actual_coverage", 0)
                    max_area = z3_report.get("metrics", {}).get("max_allowed_coverage", 0)

                    with metrics_container:
                        st.metric("Final Coverage", f"{total_area:.1f} sq ft", delta=f"Limit: {max_area:.1f} sq ft",
                                  delta_color="off")
                        with open(output_dxf, "rb") as file:
                            st.download_button(
                                label="Download Approved DXF",
                                data=file,
                                file_name="approved_layout.dxf",
                                mime="application/dxf"
                            )
                    break
                else:
                    st.warning(f"❌ REJECTED. Found {len(z3_report.get('violations', []))} violations.")
                    with st.expander("Raw Z3 JSON Report"):
                        st.json(z3_report)

                    # 5. CRITIQUE
                    with st.spinner("Agent 2 (DeepSeek) analyzing violations..."):
                        deepseek_summary = summarize_violations(deepseek_client, z3_json_str)
                        st.markdown("**Agent 2 Feedback Summary:**")
                        st.info(deepseek_summary)

                    next_prompt = prompts.GEMINI_FEEDBACK_PROMPT.format(feedback=deepseek_summary)
                    gemini_messages.append({"role": "user", "parts": [next_prompt]})
    else:
        status_text.error(f"Reached maximum iterations ({max_iterations}) without Z3 approval.")


# ==========================================
# MAIN APP UI
# ==========================================
def main():
    st.title("🏗️ SBC Multi-Agent Architecture Verifier")
    st.markdown("Automated layout generation using Gemini, DeepSeek, and Z3 Theorem Prover.")

    # --- SIDEBAR CONFIGURATION ---
    with st.sidebar:
        st.header("Configuration")

        st.subheader("API Keys")
        gemini_api_key = st.text_input("Gemini API Key", value=DEFAULT_GEMINI_KEY, type="password")
        deepseek_api_key = st.text_input("NVIDIA/DeepSeek API Key", value=DEFAULT_DEEPSEEK_KEY, type="password")

        st.subheader("Simulation Parameters")
        max_iters = st.slider("Max Iterations", min_value=1, max_value=10, value=5)

        st.subheader("Bellevue Zoning Config")
        zone_select = st.selectbox("Zone", ["R1", "R1.8", "R2.5", "R3.5", "R4", "R5", "R7.5"], index=5)
        corner_lot = st.checkbox("Corner Lot", value=False)
        garage_type = st.radio("Garage Type", ["attached", "detached"])

        bellevue_config = {
            "zone": zone_select,
            "corner_lot": corner_lot,
            "garage_type": garage_type
        }

    # --- MODULAR INPUT SECTION ---
    st.header("1. Input Parameters")
    input_method = st.radio("Select Input Method",
                            ["Image Upload", "Future: Text Description", "Future: JSON Param File"], horizontal=True)

    loaded_image = None

    if input_method == "Image Upload":
        uploaded_file = st.file_uploader("Upload a hand-drawn sketch or plot image", type=["jpg", "png", "jpeg"])
        if uploaded_file is not None:
            loaded_image = Image.open(uploaded_file)
            with st.expander("View Uploaded Image"):
                st.image(loaded_image, caption="Reference Plot Sketch", use_container_width=True)
    else:
        st.info("This input method is planned for a future update.")

    # --- EXECUTION SECTION ---
    st.header("2. Execution")
    start_btn = st.button("🚀 Start Multi-Agent Loop", type="primary", use_container_width=True)

    if start_btn:
        if not loaded_image:
            st.error("Please upload an image first to begin.")
            return
        if not gemini_api_key or not deepseek_api_key:
            st.error("Please provide both API keys in the sidebar.")
            return

        # Initialize clients dynamically based on UI inputs
        gemini_client = genai.Client(api_key=gemini_api_key)
        deepseek_client = OpenAI(
            api_key=deepseek_api_key,
            base_url="https://integrate.api.nvidia.com/v1"
        )

        st.divider()
        run_agent_loop_ui(
            image=loaded_image,
            gemini_client=gemini_client,
            deepseek_client=deepseek_client,
            max_iterations=max_iters,
            bellevue_config=bellevue_config
        )


if __name__ == "__main__":
    main()