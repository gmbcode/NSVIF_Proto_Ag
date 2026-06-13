import os
import re
import time
import json
import subprocess
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
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

# Safe Mistral Import (Handles both v1.x/v2.x and v3.x versions of the SDK)

from mistralai.client import Mistral

IS_MISTRAL_V3 = True


# Local imports
import prompts
from Z3_Verifier import verify_bellevue_layout

# ==========================================
# PAGE CONFIGURATION & SETUP
# ==========================================
st.set_page_config(
    page_title="SBC Multi-Agent Architecture Verifier",
    page_icon="Z",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Load Environment Variables
config = dotenv_values(".env")
DEFAULT_GEMINI_KEY = config.get('GEMINI_API_KEY', "")
DEFAULT_DEEPSEEK_KEY = config.get('DEEPSEEK_API_KEY', "")
DEFAULT_MISTRAL_KEY = config.get('MISTRAL_API_KEY', "")

# New Prompt for Text/Coordinate generation

COORDS_INITIAL_PROMPT = """
Please generate a complete, executable Python script using the `ezdxf` and `shapely` libraries to create '<output_dxf>'.
You are given the exact bounding coordinates of the lot polygon (in feet):
{coords}

Ensure the house footprint is maximized and legally placed on the 'SBC_HOUSE_FOOTPRINT' layer, respecting the setbacks inferred from the SBC.

### CRITICAL `ezdxf` SYNTAX CHEAT SHEET ###
LLMs frequently hallucinate ezdxf methods. You MUST strictly use ONLY the following syntax.

1. SETUP & LAYERS:
- Initialize document: `doc = ezdxf.new('R2010')`
- Access modelspace: `msp = doc.modelspace()`
- Create a layer: `doc.layers.add('SBC_HOUSE_FOOTPRINT', color=3)` 
- ERROR PREVENTION: Layer colors MUST be integer ACI codes (1-255). NEVER use RGB strings like '#FF0000' or 'red'.

2. DRAWING SHAPES:
- Draw a closed polygon: `msp.add_lwpolyline([(x1, y1), (x2, y2), ...], close=True, dxfattribs={{'layer': 'SBC_HOUSE_FOOTPRINT'}})`
- ERROR PREVENTION: DO NOT use `add_polygon()`, `add_rectangle()`, or `add_rect()`. They do not exist. Always use `add_lwpolyline()`.

3. ADDING TEXT / LABELS (CRITICAL):
- Create and align text: `msp.add_text("Room Name", dxfattribs={{'height': 2.0, 'layer': 'ANNOTATIONS'}}).set_placement((x, y), align='MIDDLE_CENTER')`
- ERROR PREVENTION 1: NEVER use `.set_pos()`. It will throw an AttributeError. You MUST use `.set_placement()`.
- ERROR PREVENTION 2: NEVER add `'alignment'` inside `dxfattribs={{}}`. It will throw a DXFAttributeError. Alignment is strictly passed as the `align=` parameter in `set_placement()`.

4. SAVING:
- Save the document: `doc.saveas('<output_dxf>')`
- ERROR PREVENTION: NEVER use `doc.save()`.

### CRITICAL `shapely` SYNTAX ###
- Create polygon: `from shapely.geometry import Polygon; lot = Polygon(coords)`
- Apply setbacks (shrink polygon): `buildable_area = lot.buffer(-setback_distance)`
- Extract coordinates to draw: `list(buildable_area.exterior.coords)`
"""


# ==========================================
# HELPER FUNCTIONS
# ==========================================
def extract_python_code(text: str) -> str:
    """Robust extraction of code from markdown code blocks."""
    # Try exact python match
    match = re.search(r'```python\n(.*?)\n```', text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Try generic markdown block match
    match = re.search(r'```\n(.*?)\n```', text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Fallback parsing
    if '```' in text:
        parts = text.split('```')
        if len(parts) >= 3:
            code = parts[1]
            if code.startswith('python\n'):
                code = code[7:]
            return code.strip()

    # If all else fails, return the raw text
    return text.strip()


def render_dxf_to_figure(dxf_path: str):
    """Reads a DXF file and renders it to a matplotlib figure for Streamlit."""
    try:
        doc = ezdxf.readfile(dxf_path)
        msp = doc.modelspace()
        fig = plt.figure(figsize=(10, 10), facecolor='white')
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_facecolor('white')
        ctx = RenderContext(doc)
        ctx.set_current_layout(msp)
        out = MatplotlibBackend(ax)
        Frontend(ctx, out).draw_layout(msp, finalize=True)
        return fig
    except Exception as e:
        return None


def calculate_polygon_area(x_coords, y_coords):
    """Calculates polygon area using the Shoelace formula."""
    x, y = list(x_coords), list(y_coords)
    return 0.5 * abs(sum(x[i] * y[i - 1] - x[i - 1] * y[i] for i in range(len(x))))


# ==========================================
# AGENT FUNCTIONS
# ==========================================
def summarize_violations(nim_client, z3_json_report: str) -> str:
    """Agent 2 interprets the Z3 JSON and creates a feedback prompt."""
    response = nim_client.chat.completions.create(
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


def run_agent_loop_ui(input_data, input_type, agent1_model, gemini_client, nim_client, mistral_client, max_iterations,
                      bellevue_config):
    """Executes the multi-agent loop, updating Streamlit UI components in real-time."""

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

    # Configure Initial Message History
    if "gemini" in agent1_model:
        if input_type == "image":
            messages = [
                {"role": "user", "parts": [prompts.GEMINI_SYSTEM_PROMPT, input_data, prompts.GEMINI_INITIAL_PROMPT]}]
        else:
            messages = [{"role": "user", "parts": [prompts.GEMINI_SYSTEM_PROMPT, input_data]}]
    else:
        messages = [
            {"role": "system", "content": prompts.GEMINI_SYSTEM_PROMPT},
            {"role": "user", "content": input_data}
        ]

    for iteration in range(1, max_iterations + 1):
        progress_fraction = iteration / max_iterations
        progress_bar.progress(progress_fraction)
        status_text.markdown(f"**Iteration {iteration} of {max_iterations}** - Running agents...")

        with log_container:
            with st.expander(f"Iteration {iteration} Details", expanded=True):

                # 1. GENERATE
                response_text = ""
                if "gemini" in agent1_model:
                    with st.spinner("Agent 1 (Gemini) is generating architecture code..."):
                        try:
                            contents = []
                            for msg in messages:
                                contents.extend(msg["parts"])
                            gemini_response = gemini_client.models.generate_content(
                                model=agent1_model,
                                contents=contents
                            )
                            response_text = gemini_response.text
                            messages.append({"role": "model", "parts": [response_text]})
                        except Exception as e:
                            st.error(f"Gemini API Error: {e}")
                            break

                    script_code = extract_python_code(response_text)
                    st.markdown("**Agent 1 Generated Code:**")
                    st.code(script_code, language="python", line_numbers=True)

                else:
                    st.markdown(f"**Agent 1 ({agent1_model}) is Generating Code...**")
                    stream_container = st.empty()
                    max_retries = 3

                    for attempt in range(max_retries):
                        try:
                            full_content = ""

                            # Mistral Version Agnostic Execution
                            if IS_MISTRAL_V3:
                                mistral_response = mistral_client.chat.stream(
                                    model=agent1_model,
                                    messages=messages,
                                    temperature=0.7
                                )
                                for chunk in mistral_response:
                                    content = chunk.data.choices[0].delta.content
                                    if content is not None:
                                        full_content += content
                                        stream_container.markdown(full_content + "▌")
                            else:
                                chat_msgs = [ChatMessage(role=m["role"], content=m["content"]) for m in messages]
                                mistral_response = mistral_client.chat_stream(
                                    model=agent1_model,
                                    messages=chat_msgs,
                                    temperature=0.7
                                )
                                for chunk in mistral_response:
                                    content = chunk.choices[0].delta.content
                                    if content is not None:
                                        full_content += content
                                        stream_container.markdown(full_content + "▌")

                            stream_container.markdown(full_content)
                            response_text = full_content
                            messages.append({"role": "assistant", "content": response_text})
                            break

                        except Exception as api_err:
                            err_str = str(api_err).lower()
                            if "429" in err_str or "rate limit" in err_str or "timeout" in err_str:
                                if attempt < max_retries - 1:
                                    wait_time = 5 * (attempt + 1)
                                    st.warning(f"⚠️ API busy. Retrying in {wait_time}s...")
                                    time.sleep(wait_time)
                                    continue
                            st.error(f"Mistral API Error: {api_err}")
                            break

                    script_code = extract_python_code(response_text)
                    with st.expander("View Extracted Python Code"):
                        st.code(script_code, language="python", line_numbers=True)

                # 2. EXTRACT & EXECUTE
                script_filename = "temp_generated_dxf_script.py"
                output_dxf = "output_dxf.dxf"

                with open(script_filename, "w", encoding="utf-8") as f:
                    f.write(script_code)

                with st.spinner("Executing script & rendering DXF..."):
                    result = subprocess.run(["python", script_filename], capture_output=True, text=True)

                if result.returncode != 0:
                    st.error("Python execution failed! Sending error stack back to Agent 1.")
                    st.code(result.stderr, language="bash")
                    error_feedback = f"Your Python code threw an error during execution:\n{result.stderr}\nPlease fix the code."

                    if "gemini" in agent1_model:
                        messages.append({"role": "user", "parts": [error_feedback]})
                    else:
                        messages.append({"role": "user", "content": error_feedback})
                    continue

                if not os.path.exists(output_dxf):
                    st.error(f"Execution finished but '{output_dxf}' was not found.")
                    error_feedback = f"The script executed but failed to save the file as '{output_dxf}'."
                    if "gemini" in agent1_model:
                        messages.append({"role": "user", "parts": [error_feedback]})
                    else:
                        messages.append({"role": "user", "content": error_feedback})
                    continue

                # UPDATE VIZUALIZATION
                fig = render_dxf_to_figure(output_dxf)
                if fig:
                    viz_container.pyplot(fig)
                    plt.close(fig)

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
                        deepseek_summary = summarize_violations(nim_client, z3_json_str)
                        st.markdown("**Agent 2 Feedback Summary:**")
                        st.info(deepseek_summary)

                    next_prompt = prompts.GEMINI_FEEDBACK_PROMPT.format(feedback=deepseek_summary)
                    if "gemini" in agent1_model:
                        messages.append({"role": "user", "parts": [next_prompt]})
                    else:
                        messages.append({"role": "user", "content": next_prompt})
    else:
        status_text.error(f"Reached maximum iterations ({max_iterations}) without Z3 approval.")


# ==========================================
# MAIN APP UI
# ==========================================
def main():
    st.title("SBC Multi-Agent Architecture Verifier")
    st.markdown("Automated layout generation using multi-agent LLM systems and the Z3 Theorem Prover.")

    # --- SIDEBAR CONFIGURATION ---
    with st.sidebar:
        st.header("Configuration")

        st.subheader("1. API Keys")
        gemini_api_key = st.text_input("Gemini API Key", value=DEFAULT_GEMINI_KEY, type="password")
        nim_api_key = st.text_input("NVIDIA NIM API Key", value=DEFAULT_DEEPSEEK_KEY, type="password")
        mistral_api_key = st.text_input("Mistral Direct API Key", value=DEFAULT_MISTRAL_KEY, type="password")

        st.subheader("2. Agent Selection")
        input_method = st.radio("Input Method", ["Interactive Plot Generator", "Image Upload"])

        if input_method == "Image Upload":
            agent1_model = st.selectbox("Agent 1 (Generator)", ["gemini-3.5-flash"])
            st.info("Mistral is text-only and disabled for image inputs.")
        else:
            agent1_model = st.selectbox("Agent 1 (Generator)",
                                        ["codestral-latest", "gemini-3.5-flash","mistral-large-latest"])

        st.subheader("3. Simulation Parameters")
        max_iters = st.slider("Max Iterations", min_value=1, max_value=10, value=5)

        st.subheader("4. Bellevue Zoning Config")
        zone_select = st.selectbox("Zone", ["R1", "R1.8", "R2.5", "R3.5", "R4", "R5", "R7.5"], index=5)
        corner_lot = st.checkbox("Corner Lot", value=False)
        garage_type = st.radio("Garage Type", ["attached", "detached"])

        bellevue_config = {"zone": zone_select, "corner_lot": corner_lot, "garage_type": garage_type}

    # --- MODULAR INPUT SECTION ---
    st.header("Input Coordinates & Environment")
    loaded_image = None
    text_prompt_data = None

    if input_method == "Interactive Plot Generator":
        st.write(
            "Modify the table coordinates below. The plot will update live to reflect the exact plot boundaries passed to Agent 1.")

        if 'plot_points' not in st.session_state:
            st.session_state.plot_points = pd.DataFrame({
                'X': [0, 75, 75, 80, 80, 75, 75, 35, 35, 15, 15],
                'Y': [80, 80, 84, 84, 4, 4, -4, -4, 4, 4, 54]
            })

        col_table, col_canvas = st.columns([1, 2])

        with col_table:
            st.subheader("Coordinate Data")
            edited_df = st.data_editor(st.session_state.plot_points, num_rows="dynamic", use_container_width=True)
            st.session_state.plot_points = edited_df

            area = calculate_polygon_area(edited_df['X'], edited_df['Y'])
            st.metric("Total Usable Area", f"{area:,.1f} sq ft")

            coords_list = list(zip(edited_df['X'], edited_df['Y']))
            text_prompt_data = COORDS_INITIAL_PROMPT.format(coords=coords_list)

        with col_canvas:
            st.subheader("Live Polygon Canvas")
            fig = go.Figure()

            plot_x = list(edited_df['X']) + [edited_df['X'].iloc[0]] if not edited_df.empty else []
            plot_y = list(edited_df['Y']) + [edited_df['Y'].iloc[0]] if not edited_df.empty else []

            fig.add_trace(go.Scatter(
                x=plot_x, y=plot_y, fill='toself', mode='lines+markers',
                marker=dict(size=10, color='red'),
                line=dict(color='darkred')
            ))
            fig.update_layout(margin=dict(l=20, r=20, t=30, b=20), xaxis_title="X (ft)", yaxis_title="Y (ft)")
            st.plotly_chart(fig, use_container_width=True)

    else:
        uploaded_file = st.file_uploader("Upload a hand-drawn sketch or plot image", type=["jpg", "png", "jpeg"])
        if uploaded_file is not None:
            loaded_image = Image.open(uploaded_file)
            st.image(loaded_image, caption="Reference Plot Sketch", width=400)

    # --- EXECUTION ---
    st.divider()
    start_btn = st.button("Start Multi-Agent Verification Loop", type="primary", use_container_width=True)

    if start_btn:
        if input_method == "Image Upload" and not loaded_image:
            st.error("Please upload an image first.")
            return
        if not gemini_api_key or not nim_api_key or (("mistral" in agent1_model.lower() or "codestral" in agent1_model.lower()) and not mistral_api_key):
            st.error("Please provide the required API keys in the sidebar.")
            return

        gemini_client = genai.Client(api_key=gemini_api_key)
        nim_client = OpenAI(api_key=nim_api_key, base_url="https://integrate.api.nvidia.com/v1")

        # Init Mistral using version agnostic fallback check
        # Init Mistral using version agnostic fallback check
        if "mistral" in agent1_model.lower() or "codestral" in agent1_model.lower():
            mistral_client = Mistral(api_key=mistral_api_key)

        else:
            mistral_client = None

        input_data = loaded_image if input_method == "Image Upload" else text_prompt_data
        input_type = "image" if input_method == "Image Upload" else "text"

        run_agent_loop_ui(input_data, input_type, agent1_model, gemini_client, nim_client, mistral_client, max_iters,
                          bellevue_config)


if __name__ == "__main__":
    main()