import ezdxf
import json
from z3 import *
from shapely.geometry import Polygon, box, Point


def extract_house_bounds(dxf_path, layer_name="SBC_HOUSE_FOOTPRINT"):
    """
    Extracts the bounding box, but FIRST enforces architectural topology:
    The footprint must be a strictly 4-sided orthogonal rectangle.
    """
    try:
        doc = ezdxf.readfile(dxf_path)
    except IOError:
        return None, f"Error: Cannot read DXF file at {dxf_path}"

    msp = doc.modelspace()
    polylines = msp.query(f'LWPOLYLINE[layer=="{layer_name}"]')

    if not polylines:
        return None, f"Error: No geometries found on layer '{layer_name}'"

    polyline = polylines[0]
    points = list(polyline.get_points('xy'))

    # Remove the last point if ezdxf duplicated it to close the loop
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]

    # TOPOLOGY CHECK 1: Must be exactly 4 vertices
    if len(points) != 4:
        return None, f"ARCHITECTURAL VIOLATION: House footprint has {len(points)} vertices. It must be a simple 4-sided rectangle, not a complex polygon."

    # TOPOLOGY CHECK 2: Edges must be orthogonal (90 degrees / axis-aligned)
    for i in range(4):
        p1 = points[i]
        p2 = points[(i + 1) % 4]
        is_horizontal = abs(p1[1] - p2[1]) < 1e-5
        is_vertical = abs(p1[0] - p2[0]) < 1e-5

        if not (is_horizontal or is_vertical):
            return None, "ARCHITECTURAL VIOLATION: House footprint contains diagonal lines. All walls must be strictly horizontal or vertical."

    x_coords = [p[0] for p in points]
    y_coords = [p[1] for p in points]

    return (min(x_coords), max(x_coords), min(y_coords), max(y_coords)), None


def get_heuristic_max_rectangle(poly, p_box):
    """
    Dynamically finds a local maximum valid footprint by incrementally
    expanding the proposed valid box until it hits the setback boundaries.
    """
    if poly.contains(p_box):
        cx, cy, cx2, cy2 = p_box.bounds
    else:
        # Fallback to a tiny box at the centroid if current design is invalid
        rp = poly.representative_point()
        cx, cy, cx2, cy2 = rp.x - 0.5, rp.y - 0.5, rp.x + 0.5, rp.y + 0.5
        if not poly.contains(box(cx, cy, cx2, cy2)):
            return poly.bounds  # Absolute worst-case fallback

    step = 2.0
    while step >= 0.1:
        expanded_this_step = False

        # Expand Right
        if poly.contains(box(cx, cy, cx2 + step, cy2)):
            cx2 += step
            expanded_this_step = True
        # Expand Left
        if poly.contains(box(cx - step, cy, cx2, cy2)):
            cx -= step
            expanded_this_step = True
        # Expand Top
        if poly.contains(box(cx, cy, cx2, cy2 + step)):
            cy2 += step
            expanded_this_step = True
        # Expand Bottom
        if poly.contains(box(cx, cy - step, cx2, cy2)):
            cy -= step
            expanded_this_step = True

        # Halve the step size for tighter precision if we get stuck
        if not expanded_this_step:
            step /= 2.0

    return cx, cy, cx2, cy2


def verify_and_optimize_footprint(dxf_path):
    # 1. Extract Proposed Coordinates & Enforce Topology
    bounds, error = extract_house_bounds(dxf_path)
    if error:
        # Instantly fail if Agent 1 tried to draw an irregular blob
        return json.dumps({"status": "ERROR", "message": error}, indent=2)

    p_xmin, p_xmax, p_ymin, p_ymax = bounds
    p_area = (p_xmax - p_xmin) * (p_ymax - p_ymin)
    proposed_box = box(p_xmin, p_ymin, p_xmax, p_ymax)

    # 2. Extract DYNAMIC Constraints from the DXF
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    setback_polys = msp.query('LWPOLYLINE[layer=="BUILDING_SETBACK"]')
    if not setback_polys:
        return json.dumps({"status": "ERROR", "message": "Missing BUILDING_SETBACK layer in DXF."}, indent=2)

    sb_points = list(setback_polys[0].get_points('xy'))
    setback_polygon = Polygon(sb_points)
    b_minx, b_miny, b_maxx, b_maxy = setback_polygon.bounds

    # 3. Initialize Z3 Solver for Verification
    v_solver = Solver()
    xmin, xmax, ymin, ymax = Reals('xmin xmax ymin ymax')

    # Bind symbolic variables
    v_solver.add(xmin == p_xmin)
    v_solver.add(xmax == p_xmax)
    v_solver.add(ymin == p_ymin)
    v_solver.add(ymax == p_ymax)

    # -------------------------------------------------------------------------
    # DYNAMIC SEATTLE BUILDING CODE CONSTRAINTS
    # -------------------------------------------------------------------------

    # Base Box Constraints (Dynamic from the global limits of the setback layer)
    c_west = xmin >= b_minx
    c_east = xmax <= b_maxx
    c_south = ymin >= b_miny
    c_north = ymax <= b_maxy

    # Complex Geometry Constraint (Bridging Shapely intersection logic into Z3 Booleans)
    is_inside_complex = setback_polygon.contains(proposed_box)
    c_complex = BoolVal(is_inside_complex)

    rules = [
        (c_west, f"SMC Constraint: Global West boundary violation. Must be x >= {b_minx:.2f}."),
        (c_east, f"SMC Constraint: Global East boundary violation. Must be x <= {b_maxx:.2f}."),
        (c_south, f"SMC Constraint: Global South boundary violation. Must be y >= {b_miny:.2f}."),
        (c_north, f"SMC Constraint: Global North boundary violation. Must be y <= {b_maxy:.2f}."),
        (c_complex, "SMC Constraint: Footprint violates an irregular interior setback boundary (diagonals or notches).")
    ]

    # Dynamic Tree Constraints
    trees = msp.query('CIRCLE[layer=="TREES"]')
    for idx, t in enumerate(trees):
        tx, ty = t.dxf.center.x, t.dxf.center.y
        tr = t.dxf.radius
        # Check if the proposed house hits the tree buffer
        tree_circle = Point(tx, ty).buffer(tr)
        is_safe_from_tree = not proposed_box.intersects(tree_circle)
        rules.append((BoolVal(is_safe_from_tree),
                      f"SBC / DR 16-2008: Protected tree root zone invasion at ({tx:.1f}, {ty:.1f})."))

    # RUN VERIFICATION PHASE
    violations = []
    for condition, message in rules:
        v_solver.push()
        v_solver.add(Not(condition))  # Assert the negation
        if v_solver.check() == sat:
            violations.append(message)
        v_solver.pop()

    # -------------------------------------------------------------------------
    # DYNAMIC OPTIMIZATION PHASE (Finding the Maximum Legal Envelope)
    # -------------------------------------------------------------------------

    opt_xmin, opt_ymin, opt_xmax, opt_ymax = get_heuristic_max_rectangle(setback_polygon, proposed_box)
    max_legal_area = round((opt_xmax - opt_xmin) * (opt_ymax - opt_ymin), 2)

    optimal_bounds = {
        "xmin": round(opt_xmin, 2),
        "xmax": round(opt_xmax, 2),
        "ymin": round(opt_ymin, 2),
        "ymax": round(opt_ymax, 2)
    }

    is_optimized = True
    optimization_suggestions = []

    if len(violations) == 0:
        area_efficiency = (p_area / max_legal_area) * 100
        if area_efficiency < 99.0:
            is_optimized = False
            optimization_suggestions.append(
                f"The current valid design covers {p_area:.1f} sq ft, which is only {area_efficiency:.1f}% of the allowable space."
            )
            optimization_suggestions.append(
                f"To maximize footprint, expand your dimensions to target these limits: "
                f"x ranges from {optimal_bounds['xmin']} to {optimal_bounds['xmax']} feet, "
                f"y ranges from {optimal_bounds['ymin']} to {optimal_bounds['ymax']} feet to achieve ~{max_legal_area} sq ft."
            )
    else:
        is_optimized = False
        optimization_suggestions.append(
            "Cannot calculate expansion optimizations until the existing geometric violations are resolved."
        )

    # 4. CONSTRUCT STRUCTURED JSON RESPONSE FOR THE NEXT AGENT
    output_report = {
        # This will now correctly reject valid but un-optimized designs
        "status": "APPROVED" if (not violations and is_optimized) else "REJECTED",
        "proposed_footprint": {
            "xmin": round(p_xmin, 2),
            "xmax": round(p_xmax, 2),
            "ymin": round(p_ymin, 2),
            "ymax": round(p_ymax, 2),
            "calculated_area_sqft": round(p_area, 2)
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
# print(verify_and_optimize_footprint("generated_plot.dxf"))