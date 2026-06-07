import ezdxf
from shapely.geometry import Polygon, box, Point
from shapely.ops import unary_union
from z3 import *
import json
import math

# The required rooms for the interior program (Corridor added per new constraints)
REQUIRED_ROOMS = ["Living", "Kitchen", "Bedroom", "Bathroom", "Corridor"]


def extract_dynamic_environment(dxf_path):
    """
    Dynamically extracts the Plot Boundary, Setbacks, and Protected Trees
    directly from the CAD file layers so the solver is strictly universal.
    """
    try:
        doc = ezdxf.readfile(dxf_path)
        msp = doc.modelspace()
    except Exception as e:
        return None, None, [], f"FILE ERROR: Could not read DXF. {str(e)}"

    # 1. Extract Plot Boundary
    lot_polys = msp.query('LWPOLYLINE[layer=="LOT_BOUNDARY"]')
    if not lot_polys:
        return None, None, [], "ENVIRONMENT VIOLATION: Missing 'LOT_BOUNDARY' polyline. Cannot calculate setbacks."

    lot_points = list(lot_polys[0].get_points('xy'))
    plot_polygon = Polygon(lot_points)

    # Calculate Buildable Envelope (Seattle LR zone: 5ft side, 7ft front/rear)
    # Using a generalized -5.0 ft buffer for this universal heuristic script.
    buildable_polygon = plot_polygon.buffer(-5.0)

    # 2. Extract Trees Dynamically
    tree_zones = []
    trees = msp.query('CIRCLE[layer=="TREES"]')
    for t in trees:
        tx, ty = t.dxf.center.x, t.dxf.center.y
        tr = t.dxf.radius
        tree_zones.append(Point(tx, ty).buffer(tr))

    return plot_polygon, buildable_polygon, tree_zones, None


def extract_rooms(dxf_path):
    """Extracts all rectangular rooms from the DXF and enforces orthogonal topology."""
    try:
        doc = ezdxf.readfile(dxf_path)
        msp = doc.modelspace()
    except Exception:
        return None, None, "FILE ERROR: Could not read DXF."

    room_polys = msp.query('LWPOLYLINE[layer=="ROOMS"]')
    texts = list(msp.query('TEXT[layer=="ANNOTATIONS"]'))

    rooms_data = {}
    room_shapes = []

    for i, poly in enumerate(room_polys):
        points = list(poly.get_points('xy'))
        if len(points) > 1 and points[0] == points[-1]:
            points = points[:-1]

        # TOPOLOGY CHECK: Must be exactly 4 vertices
        if len(points) != 4:
            return None, None, f"TOPOLOGY VIOLATION: A room has {len(points)} vertices instead of 4."

        # TOPOLOGY CHECK: Must be strictly orthogonal
        for j in range(4):
            p1 = points[j]
            p2 = points[(j + 1) % 4]
            if not (abs(p1[1] - p2[1]) < 1e-5 or abs(p1[0] - p2[0]) < 1e-5):
                return None, None, "TOPOLOGY VIOLATION: All rooms must be perfectly orthogonal rectangles."

        x_coords = [p[0] for p in points]
        y_coords = [p[1] for p in points]
        xmin, xmax, ymin, ymax = min(x_coords), max(x_coords), min(y_coords), max(y_coords)

        # Match label to room (check if text is inside the bounds)
        room_name = f"Unknown_Room_{i}"
        for t in texts:
            tx, ty = t.dxf.insert.x, t.dxf.insert.y
            if xmin <= tx <= xmax and ymin <= ty <= ymax:
                room_name = t.dxf.text.strip()
                break

        rooms_data[room_name] = {"xmin": xmin, "xmax": xmax, "ymin": ymin, "ymax": ymax}
        room_shapes.append(box(xmin, ymin, xmax, ymax))

    return rooms_data, room_shapes, None


def verify_layout(dxf_path):
    # 1. Dynamically load the plot and trees
    plot_poly, buildable_poly, tree_zones, env_error = extract_dynamic_environment(dxf_path)
    if env_error:
        return json.dumps({"status": "REJECTED", "violations": [env_error]}, indent=2)

    # 2. Extract the generated rooms
    rooms_data, room_shapes, error = extract_rooms(dxf_path)
    if error:
        return json.dumps({"status": "REJECTED", "violations": [error]}, indent=2)

    violations = []
    v_solver = Solver()

    # --- A. CHECK MISSING ROOMS ---
    found_rooms = list(rooms_data.keys())
    for req in REQUIRED_ROOMS:
        if not any(req in r for r in found_rooms):
            violations.append(f"PROGRAMMING VIOLATION: Missing required room '{req}'.")

    # --- B. Z3 LOGICAL CONSTRAINTS (TILING & INTERIOR) ---
        # --- B. Z3 LOGICAL CONSTRAINTS (TILING & INTERIOR) ---
    z3_rooms = {}
    for name, b in rooms_data.items():
        # FIX: Replace spaces with underscores so Z3 doesn't split the names
        safe_name = name.replace(" ", "_")

        xmin, xmax, ymin, ymax = Reals(f'{safe_name}_xmin {safe_name}_xmax {safe_name}_ymin {safe_name}_ymax')
        v_solver.add(xmin == b['xmin'], xmax == b['xmax'])
        v_solver.add(ymin == b['ymin'], ymax == b['ymax'])

        # Keep the original name for the dictionary key so the rest of the logic works
        z3_rooms[name] = (xmin, xmax, ymin, ymax)

    # 1. Exact Tiling (No Overlaps except Bathrooms)
    for i in range(len(found_rooms)):
        for j in range(i + 1, len(found_rooms)):
            n1, n2 = found_rooms[i], found_rooms[j]
            x1_min, x1_max, y1_min, y1_max = z3_rooms[n1]
            x2_min, x2_max, y2_min, y2_max = z3_rooms[n2]

            # Bathroom is allowed to be INSIDE the Bedroom
            if ("Bathroom" in n1 and "Bedroom" in n2) or ("Bathroom" in n2 and "Bedroom" in n1):
                continue

            no_overlap = Or(x1_max <= x2_min, x2_max <= x1_min, y1_max <= y2_min, y2_max <= y1_min)
            v_solver.push()
            v_solver.add(Not(no_overlap))
            if v_solver.check() == sat:
                violations.append(
                    f"TILING VIOLATION: '{n1}' and '{n2}' are overlapping. Rooms must sit flush against each other.")
            v_solver.pop()

    # 2. Bathroom Inside Bedroom Logic
    bed_keys = [k for k in found_rooms if "Bedroom" in k]
    bath_keys = [k for k in found_rooms if "Bathroom" in k]
    if bed_keys and bath_keys:
        bed, bath = z3_rooms[bed_keys[0]], z3_rooms[bath_keys[0]]
        is_inside = And(bath[0] >= bed[0], bath[1] <= bed[1], bath[2] >= bed[2], bath[3] <= bed[3])
        v_solver.push()
        v_solver.add(Not(is_inside))
        if v_solver.check() == sat:
            violations.append(
                "LAYOUT VIOLATION: The Bathroom must be fully contained within the bounds of the Bedroom.")
        v_solver.pop()

    # 3. Door & Fire Safety Logic
    doc = ezdxf.readfile(dxf_path)
    door_lines = doc.modelspace().query('LINE[layer=="SBC_DOOR"]')
    if not door_lines:
        violations.append("FIRE CODE VIOLATION: Missing Main Door line on 'SBC_DOOR' layer.")
    else:
        door = door_lines[0]
        dx, dy = door.dxf.start.x, door.dxf.start.y

        # Door must touch Living Room
        living_keys = [k for k in found_rooms if "Living" in k]
        if living_keys:
            lx_min, lx_max, ly_min, ly_max = z3_rooms[living_keys[0]]
            door_on_living = Or(
                And(dx == lx_min, dy >= ly_min, dy <= ly_max),  # Touching Left wall
                And(dx == lx_max, dy >= ly_min, dy <= ly_max),  # Touching Right wall
                And(dy == ly_min, dx >= lx_min, dx <= lx_max),  # Touching Bottom wall
                And(dy == ly_max, dx >= lx_min, dx <= lx_max)  # Touching Top wall
            )
            v_solver.push()
            v_solver.add(Not(door_on_living))
            if v_solver.check() == sat:
                violations.append("FIRE CODE VIOLATION: Main entry door does not touch the Living Room.")
            v_solver.pop()

    # --- C. SHAPELY EXTERNAL CONSTRAINTS (Setbacks & Trees) ---
    if room_shapes:
        composite_footprint = unary_union(room_shapes)

        # Floating point tolerance (+0.1 buffer) so perfectly aligned walls don't trigger a false UNSAT
        buffered_buildable = buildable_poly.buffer(0.1)

        # Setback Check
        if not buffered_buildable.contains(composite_footprint):
            violations.append("SETBACK VIOLATION: The composite footprint crosses the allowed green setback lines.")

        # Tree CRZ Check (Negative buffer shrinks the footprint slightly to avoid false touches)
        for i, tree_crz in enumerate(tree_zones):
            if composite_footprint.buffer(-0.1).intersects(tree_crz):
                violations.append(
                    f"ENVIRONMENTAL VIOLATION: A room breaches Protected Tree #{i + 1} Critical Root Zone.")

    return json.dumps({
        "status": "REJECTED" if violations else "APPROVED",
        "violations": violations,
        "rooms": rooms_data
    }, indent=2)


if __name__ == "__main__":
    print(verify_layout("generated_plot.dxf"))