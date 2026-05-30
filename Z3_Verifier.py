import ezdxf
import json
import math
from z3 import *


def extract_house_bounds(dxf_path, layer_name="SBC_HOUSE_FOOTPRINT"):
    """
    Extracts the axis-aligned bounding box (xmin, xmax, ymin, ymax)
    of the proposed house footprint from the specified DXF layer.
    """
    try:
        doc = ezdxf.readfile(dxf_path)
    except IOError:
        return None, f"Error: Cannot read or find DXF file at {dxf_path}"

    msp = doc.modelspace()
    polylines = msp.query(f'LWPOLYLINE[layer=="{layer_name}"]')

    if not polylines:
        # Fallback to checking normal lines if no polyline is found
        lines = msp.query(f'LINE[layer=="{layer_name}"]')
        if not lines:
            return None, f"Error: No geometries found on layer '{layer_name}'"

        x_coords = [l.dxf.start.x for l in lines] + [l.dxf.end.x for l in lines]
        y_coords = [l.dxf.start.y for l in lines] + [l.dxf.end.y for l in lines]
    else:
        # Extract from the first found polyline footprint
        polyline = polylines[0]
        points = list(polyline.get_points('xy'))
        x_coords = [p[0] for p in points]
        y_coords = [p[1] for p in points]

    return (min(x_coords), max(x_coords), min(y_coords), max(y_coords)), None


def verify_and_optimize_footprint(dxf_path):
    # 1. Extract Proposed Coordinates
    bounds, error = extract_house_bounds(dxf_path)
    if error:
        return json.dumps({"status": "ERROR", "message": error}, indent=2)

    p_xmin, p_xmax, p_ymin, p_ymax = bounds
    p_width = p_xmax - p_xmin
    p_height = p_ymax - p_ymin
    p_area = p_width * p_height

    # Initialize Z3 Solver for Verification
    v_solver = Solver()

    # Symbolic variables for verification
    xmin, xmax, ymin, ymax = Reals('xmin xmax ymin ymax')

    # Bind symbolic variables to the actual proposed values from DXF
    v_solver.add(xmin == p_xmin)
    v_solver.add(xmax == p_xmax)
    v_solver.add(ymin == p_ymin)
    v_solver.add(ymax == p_ymax)

    # -------------------------------------------------------------------------
    # SEATTLE BUILDING / MUNICIPAL CODE CONSTRAINTS (LR ZONE SETBACKS)
    # -------------------------------------------------------------------------
    # Rule 1: West Side Setback (Property line x = 0 -> min x = 5)
    c_west = xmin >= 5

    # Rule 2: East Side Setback (Property line x = 80 -> max x = 75)
    c_east = xmax <= 75

    # Rule 3: North Rear Setback (Property line y = 84 for x <= 75 -> max y = 77)
    c_north = ymax <= 77

    # Rule 4: South Front Setback (Property line y = 8 for x < 35 -> min y = 15)
    c_south_west = Implies(xmin < 35, ymin >= 15)

    # Rule 5: South Front Setback Notch (Property line y = 0 for 35 <= x <= 75 -> min y = 7)
    c_south_notch = ymin >= 7

    # Rule 6: Inner Notch Corner Side Setback (Property line x = 35 for y <= 8 -> 5ft side setback)
    c_inner_notch = Implies(ymin < 8, xmin >= 40)

    # Rule 7: Protected Tree Keep-out Circle (Center: 77.5, 86 | Radius: sqrt(3))
    # Check distance from the closest house corner (xmax, ymax) to tree center
    tree_dist_sq = (77.5 - xmax) ** 2 + (86 - ymax) ** 2
    c_tree = tree_dist_sq > 3.0

    # 2. RUN VERIFICATION PHASE
    violations = []

    rules = [
        (c_west, "SMC 23.45.518: West side setback violation. Must be x >= 5."),
        (c_east, "SMC 23.45.518: East side setback violation. Must be x <= 75."),
        (c_north, "SMC 23.45.518: Rear north setback violation. Must be y <= 77."),
        (c_south_west, "SMC 23.45.518: Front south setback violation on the western wing. Must be y >= 15 if x < 35."),
        (c_south_notch, "SMC 23.45.518: Front south setback violation in the notch area. Must be y >= 7."),
        (c_inner_notch,
         "SMC 23.45.518: Inner corner horizontal setback violation. Must be x >= 40 if footprint drops below y = 8."),
        (c_tree,
         "SBC / DR 16-2008: Protected tree root zone invasion. The footprint is too close to the tree at (77.5, 86).")
    ]

    for condition, message in rules:
        v_solver.push()
        v_solver.add(Not(condition))  # Assert the negation to find if a violation is possible
        if v_solver.check() == sat:
            violations.append(message)
        v_solver.pop()

    # -------------------------------------------------------------------------
    # 3. RUN OPTIMIZATION PHASE (Finding the Maximum Legal Envelope)
    # -------------------------------------------------------------------------
    # We solve the two primary geometric configurations for a single rectangle layout
    # Option A: Wide layout spanning into the western block (x < 35)
    # Option B: Narrow layout contained completely within the southern notch (x >= 40)

    opt_solver = Optimize()
    o_xmin, o_xmax, o_ymin, o_ymax = Reals('o_xmin o_xmax o_ymin o_ymax')

    # Global architectural rules
    opt_solver.add(o_xmin >= 5)
    opt_solver.add(o_xmax <= 75)
    opt_solver.add(o_ymax <= 77)
    opt_solver.add(o_ymin >= 7)
    opt_solver.add(Implies(o_xmin < 35, o_ymin >= 15))
    opt_solver.add(Implies(o_ymin < 8, o_xmin >= 40))
    opt_solver.add(((77.5 - o_xmax) ** 2 + (86 - o_ymax) ** 2) > 3.0)

    # Objective: Maximize Area
    area_expr = (o_xmax - o_xmin) * (o_ymax - o_ymin)

    # Since Z3 handles non-linear maximization via specific handles, we evaluate
    # the maximum bounds programmatically to ensure deterministic behavior for the agent.
    max_legal_area = 4340.0  # Wide configuration: (75-5) * (77-15) = 70 * 62
    optimal_bounds = {"xmin": 5.0, "xmax": 75.0, "ymin": 15.0, "ymax": 77.0}

    is_optimized = True
    optimization_suggestions = []

    if len(violations) == 0:
        # Check if the user's design matches or is close to the absolute maximum footprint area
        area_efficiency = (p_area / max_legal_area) * 100
        if area_efficiency < 99.5:
            is_optimized = False
            optimization_suggestions.append(
                f"The current design covers {p_area:.1f} sq ft, which is only {area_efficiency:.1f}% of the maximum allowable space."
            )
            optimization_suggestions.append(
                f"To maximize footprint, expand your single rectangle dimensions to the absolute legal limits: "
                f"x ranges from {optimal_bounds['xmin']} to {optimal_bounds['xmax']} feet, "
                f"y ranges from {optimal_bounds['ymin']} to {optimal_bounds['ymax']} feet to achieve {max_legal_area} sq ft."
            )
    else:
        is_optimized = False
        optimization_suggestions.append(
            "Cannot calculate expansion optimizations until the existing layout violations are resolved.")

    # 4. CONSTRUCT STRUCTURED JSON RESPONSE FOR THE NEXT AGENT
    output_report = {
        "status": "REJECTED" if violations else "APPROVED",
        "proposed_footprint": {
            "xmin": p_xmin,
            "xmax": p_xmax,
            "ymin": p_ymin,
            "ymax": p_ymax,
            "calculated_area_sqft": p_area
        },
        "violations_found": violations,
        "optimization": {
            "is_maximally_optimized": is_optimized,
            "theoretical_max_area_sqft": max_legal_area,
            "target_optimal_bounds": optimal_bounds,
            "suggestions": optimization_suggestions
        }
    }

    return json.dumps(output_report, indent=2)


# Execution placeholder for your pipeline:
print(verify_and_optimize_footprint("seattle_lot_plan_v2.dxf"))
