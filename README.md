# NSVIF_Proto_Ag: Neuro-Symbolic Verification & Iterative Feedback

NSVIF_Proto_Ag is a multi-agent AI prototype designed to automatically generate, formally verify, and incrementally optimize architectural CAD drawings (.dxf) against municipal zoning regulations (specifically, the Seattle Building Code). 

By combining the creative generation capabilities of Large Language Models (LLMs) with the rigorous mathematical proofs of Satisfiability Modulo Theories (SMT), this project ensures that AI-generated architectural designs are 100% compliant with hard constraints before approval.

## Architecture & How It Works

The system operates on an iterative, closed-loop pipeline utilizing two AI agents and a formal constraint solver.

```text
User Input (Plot Sketch + SBC Constraints)
            │
            ▼
      ┌──────────────┐
      │   Agent 1    │  ──► Generates Python script (ezdxf + shapely)
      │ (Generator)  │      to output a proposed house footprint (.dxf)
      └──────────────┘
            │
            ▼ (Executes script -> outputs generated_plot.dxf)
      ┌──────────────┐
      │   Verifier   │  ──► Evaluates constraints using Z3 SMT Solver
      │ (Z3 Solver)  │      Outputs Pass/Fail + JSON Violation Report
      └──────────────┘
            │
      ┌─────┴─────┐
      │           │
    PASS        FAIL
      │           │
      ▼           ▼
  Final DXF    ┌──────────────┐
  Approved!    │   Agent 2    │ ──► Translates Z3 JSON violations into 
               │   (Critic)   │     actionable natural language feedback
               └──────────────┘
                      │
                      └───► Back to Agent 1 (Loop continues)

```

### The Components:

1. **Agent 1 (Generator) - Google Gemini 2.5 Flash:** Takes a reference sketch image and the Seattle Building Code (SBC) context to write a Python script. This script uses the `ezdxf` library to draw a proposed house footprint specifically on the `SBC_HOUSE_FOOTPRINT` layer.
2. **The Verifier - Z3 Solver:** A deterministic Python script (`Z3_Verifier.py`) that uses Microsoft's Z3 theorem prover. It checks the generated `.dxf` against strict spatial rules (e.g., SMC 23.45.518: 5ft side setbacks, 7ft front/rear setbacks, tree root zone protection, and total area maximization).
3. **Agent 2 (Critic) - DeepSeek V4 Pro (via NVIDIA API):** If the Z3 solver rejects the design, it outputs a highly technical JSON report. Agent 2 ingests this JSON and summarizes it into concise, architectural instructions (e.g., *"Your western boundary violates the 5ft setback. Adjust xmin from 3 to 5."*) and passes it back to Agent 1.

## Setup and Installation

### 1. Prerequisites

* Python 3.9+
* API Keys for Google Gemini and NVIDIA (DeepSeek).

### 2. Install Dependencies

Clone the repository and install the required packages using `requirements.txt`:

```bash
pip install -r requirements.txt

```

*(Core libraries: `openai`, `google-genai`, `z3-solver`, `ezdxf`, `shapely`, `Pillow`)*

### 3. Environment Variables

Create a `.env` file in the root directory (you can copy `.env.example`) and add your API keys:

```ini
DEEPSEEK_API_KEY=nvapi-your-nvidia-deepseek-key-here
GEMINI_API_KEY=your-google-gemini-key-here

```

## Usage

To run the multi-agent loop, provide a reference image of the plot (e.g., `image_21bab4.png`) and execute the orchestrator:

```bash
python main.py

```

**What to expect during execution:**

1. **Iteration 1:** Gemini generates an initial CAD script. The system runs it.
2. **Z3 Check:** The verifier checks the generated bounds. It will likely catch early setback violations or inefficient space usage.
3. **Feedback:** DeepSeek translates the failure into a prompt.
4. **Subsequent Iterations:** The agents will converse and refine the coordinates until the Z3 solver outputs `APPROVED`, meaning the design mathematically maximizes the legal footprint without violating any Seattle code constraints.

## 📂 Project Structure

* **`main.py`**: The central orchestrator that manages the LLM API calls, executes the generated Python code, and handles the multi-agent feedback loop.
* **`prompts.py`**: Contains the system prompts, behavioral guidelines, and dynamic context templates for both Gemini and DeepSeek.
* **`Z3_Verifier.py`**: The formal verification engine. It extracts geometries from the DXF and asserts them against SMT logic variables reflecting Seattle Municipal Code constraints.
* **`Gen_DXF.py`**: A standalone utility script to generate the initial baseline lot boundary and setback polygons based on the provided sketch dimensions.
* **`seattle_lot_plan.dxf`**: Base CAD file of the plot, including property lines, trees, and utilities.

## License

This project is licensed under the GNU Lesser General Public License v2.1. See the `LICENSE` file for details.
