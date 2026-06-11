import ezdxf
from shapely.geometry import Polygon, box, Point
from shapely.ops import unary_union
from z3 import *
import json
import math

# --- 1. BELLEVUE ZONE LOOKUP TABLE ---
ZONE_RULES = {
    "R1": {"front": 35, "rear": 25, "side_min": 5, "side_combined": 20, "max_coverage": 0.35, "max_height": 30,
           "max_far": 0.5},
    "R1.8": {"front": 30, "rear": 25, "side_min": 5, "side_combined": 15, "max_coverage": 0.35, "max_height": 30,
             "max_far": 0.5},
    "R2.5": {"front": 20, "rear": 25, "side_min": 5, "side_combined": 15, "max_coverage": 0.35, "max_height": 30,
             "max_far": 0.5},
    "R3.5": {"front": 20, "rear": 25, "side_min": 5, "side_combined": 15, "max_coverage": 0.35, "max_height": 30,
             "max_far": 0.5},
    "R4": {"front": 20, "rear": 20, "side_min": 5, "side_combined": 15, "max_coverage": 0.35, "max_height": 30,
           "max_far": 0.5},
    "R5": {"front": 20, "rear": 20, "side_min": 5, "side_combined": 15, "max_coverage": 0.40, "max_height": 30,
           "max_far": 0.5},
    "R7.5": {"front": 20, "rear": 20, "side_min": 5, "side_combined": 10, "max_coverage": 0.40, "max_height": 30,
             "max_far": 0.5},
}

REQUIRED_ROOMS = ["Living", "Kitchen", "Bedroom", "Bathroom", "Corridor"]


def extract_dynamic_environment(dxf_path, zone_rules, is_corner_lot=False):
    """
    Dynamically extracts the Plot Boundary, Trees, Easements, and Critical Areas.
    Calculates the strict Buildable Envelope based on Bellevue zoning rules.
    """
    try:
        doc = ezdxf.readfile(dxf_path)
        msp = doc.modelspace()
    except Exception as e:
        return None, {}, f"FILE ERROR: Could not read DXF. {str(e)}"

    env_data = {}

    # 1. Extract Plot Boundary
    lot_polys = msp.query('LWPOLYLINE[layer=="LOT_BOUNDARY"]')
    if not lot_polys:
        return None, {}, "ENVIRONMENT VIOLATION: Missing 'LOT_BOUNDARY' polyline."

    lot_points = list(lot_polys[0].get_points('xy'))
    plot_polygon = Polygon(lot_points)
    env_data['plot_polygon'] = plot_polygon
    env_data['lot_area'] = plot_polygon.area

    # Calculate exact bounding box of the lot to apply directional setbacks
    minx, miny, maxx, maxy = plot_polygon.bounds
    env_data['bounds'] = (minx, miny, maxx, maxy)

    # 2. Extract Trees Dynamically
    tree_zones = []
    for t in msp.query('CIRCLE[layer=="TREES"]'):
        # Using the specified radius as the protection zone per city code
        tree_zones.append(Point(t.dxf.center.x, t.dxf.center.y).buffer(t.dxf.radius))
    env_data['trees'] = tree_zones

    # 3. Extract Access Easements (If any)
    easements = []
    for e in msp.query('LWPOLYLINE[layer=="ACCESS_EASEMENT"]'):
        pts = list(e.get_points('xy'))
        if len(pts) > 2:
            easements.append(Polygon(pts))
    env_data['easements'] = easements

    # 4. Extract Critical Areas (If any)
    critical_areas = []
    for c in msp.query('LWPOLYLINE[layer=="CRITICAL_AREA"]'):
        pts = list(c.get_points('xy'))
        if len(pts) > 2:
            critical_areas.append(Polygon(pts))
    env_data['critical_areas'] = critical_areas

    return env_data, None


def extract_rooms(dxf_path):
    """Extracts rectangular rooms, enforces orthogonal topology, and basic proportions."""
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
        # Remove closing duplicate vertex if present
        if len(points) > 1 and points[0] == points[-1]:
            points = points[:-1]

        if len(points) != 4:
            return None, None, f"TOPOLOGY VIOLATION: A room has {len(points)} vertices instead of 4."

        for j in range(4):
            p1 = points[j]
            p2 = points[(j + 1) % 4]
            if not (abs(p1[1] - p2[1]) < 1e-5 or abs(p1[0] - p2[0]) < 1e-5):
                return None, None, "TOPOLOGY VIOLATION: All rooms must be perfectly orthogonal rectangles."

        x_coords = [p[0] for p in points]
        y_coords = [p[1] for p in points]
        xmin, xmax, ymin, ymax = min(x_coords), max(x_coords), min(y_coords), max(y_coords)

        # Room Proportion Check
        width = xmax - xmin
        height = ymax - ymin

        room_name = f"Unknown_Room_{i}"
        for t in texts:
            tx, ty = t.dxf.insert.x, t.dxf.insert.y
            if xmin <= tx <= xmax and ymin <= ty <= ymax:
                # Sanitize name to prevent Z3 variable unpacking bugs
                room_name = t.dxf.text.strip().replace(" ", "_")
                break

        # Enforce human-scale livability rules (skip Corridor)
        if "Corridor" not in room_name:
            if width < 6.0 or height < 6.0:
                return None, None, f"PROPORTION VIOLATION: '{room_name}' is too narrow. Min dimension is 6ft."
            if max(width, height) / min(width, height) > 3.0:
                return None, None, f"PROPORTION VIOLATION: '{room_name}' aspect ratio exceeds 3:1."

        rooms_data[room_name] = {"xmin": xmin, "xmax": xmax, "ymin": ymin, "ymax": ymax, "area": width * height}
        room_shapes.append(box(xmin, ymin, xmax, ymax))

    return rooms_data, room_shapes, None


def verify_bellevue_layout(dxf_path, config):
    """
    Main Verification Engine.
    Config expects: {"zone": "R5", "corner_lot": False, "garage_type": "attached"}
    """
    zone = config.get("zone", "R5")
    if zone not in ZONE_RULES:
        return json.dumps({"status": "REJECTED", "violations": [f"INVALID ZONE: {zone} not recognized."]})

    rules = ZONE_RULES[zone]
    is_corner = config.get("corner_lot", False)
    garage_type = config.get("garage_type", "attached")

    # 1. Dynamically load the plot environment
    env_data, env_error = extract_dynamic_environment(dxf_path, rules, is_corner)
    if env_error:
        return json.dumps({"status": "REJECTED", "violations": [env_error]}, indent=2)

    plot_poly = env_data['plot_polygon']
    lot_minx, lot_miny, lot_maxx, lot_maxy = env_data['bounds']
    lot_area = env_data['lot_area']

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

    if not rooms_data:
        return json.dumps({"status": "REJECTED", "violations": ["PROGRAMMING VIOLATION: No valid rooms found."]},
                          indent=2)

    # --- B. Z3 LOGICAL CONSTRAINTS (INTERIOR TOPOLOGY) ---
    z3_rooms = {}
    for name, b in rooms_data.items():
        xmin, xmax, ymin, ymax = Reals(f'{name}_xmin {name}_xmax {name}_ymin {name}_ymax')
        v_solver.add(xmin == b['xmin'], xmax == b['xmax'])
        v_solver.add(ymin == b['ymin'], ymax == b['ymax'])
        z3_rooms[name] = (xmin, xmax, ymin, ymax)

    # 1. Exact Tiling & Logical Adjacency
    for i in range(len(found_rooms)):
        for j in range(i + 1, len(found_rooms)):
            n1, n2 = found_rooms[i], found_rooms[j]
            x1_min, x1_max, y1_min, y1_max = z3_rooms[n1]
            x2_min, x2_max, y2_min, y2_max = z3_rooms[n2]

            # Bathroom must be inside Bedroom
            if ("Bathroom" in n1 and "Bedroom" in n2) or ("Bathroom" in n2 and "Bedroom" in n1):
                bath_name = n1 if "Bathroom" in n1 else n2
                bed_name = n2 if bath_name == n1 else n1
                bx_min, bx_max, by_min, by_max = z3_rooms[bath_name]
                bd_min, bd_max, bdy_min, bdy_max = z3_rooms[bed_name]

                is_inside = And(bx_min >= bd_min, bx_max <= bd_max, by_min >= bdy_min, by_max <= bdy_max)
                v_solver.push()
                v_solver.add(Not(is_inside))
                if v_solver.check() == sat:
                    violations.append(f"LAYOUT VIOLATION: '{bath_name}' must be fully contained within '{bed_name}'.")
                v_solver.pop()
                continue

            # Garage must be detached by >5ft if specified
            if garage_type == "detached" and ("Garage" in n1 or "Garage" in n2):
                detached_logic = Or(x1_max <= x2_min - 5.0, x2_max <= x1_min - 5.0,
                                    y1_max <= y2_min - 5.0, y2_max <= y1_min - 5.0)
                v_solver.push()
                v_solver.add(Not(detached_logic))
                if v_solver.check() == sat:
                    violations.append(
                        f"GARAGE VIOLATION: Detached Garage must be >= 5ft away from '{n1 if 'Garage' in n2 else n2}'.")
                v_solver.pop()
                continue

            # Default: No Overlap between separate rooms
            no_overlap = Or(x1_max <= x2_min, x2_max <= x1_min, y1_max <= y2_min, y2_max <= y1_min)
            v_solver.push()
            v_solver.add(Not(no_overlap))
            if v_solver.check() == sat:
                violations.append(f"TILING VIOLATION: '{n1}' and '{n2}' are overlapping. Rooms must sit flush.")
            v_solver.pop()

    # 2. Door & Fire Safety Logic
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
                And(dx == lx_min, dy >= ly_min, dy <= ly_max),
                And(dx == lx_max, dy >= ly_min, dy <= ly_max),
                And(dy == ly_min, dx >= lx_min, dx <= lx_max),
                And(dy == ly_max, dx >= lx_min, dx <= lx_max)
            )
            v_solver.push()
            v_solver.add(Not(door_on_living))
            if v_solver.check() == sat:
                violations.append("FIRE CODE VIOLATION: Main entry door does not touch the Living Room.")
            v_solver.pop()

    # --- C. BELLEVUE EXTERNAL SETBACKS (Calculated via Exact Concrete Math) ---
    actual_house_xmin = min([b['xmin'] for b in rooms_data.values()])
    actual_house_xmax = max([b['xmax'] for b in rooms_data.values()])
    actual_house_ymin = min([b['ymin'] for b in rooms_data.values()])
    actual_house_ymax = max([b['ymax'] for b in rooms_data.values()])

    front_setback_val = rules['front']
    rear_setback_val = rules['rear']
    side_setback_val = rules['side_min']
    combined_side_val = rules['side_combined']

    # Front Setback (Assumes Y-min is front edge of the lot)
    if actual_house_ymin < lot_miny + front_setback_val:
        violations.append(
            f"SETBACK VIOLATION: Front setback must be >= {front_setback_val}ft. Currently only {actual_house_ymin - lot_miny:.1f}ft.")

    # Rear Setback (Assumes Y-max is rear edge of the lot)
    if actual_house_ymax > lot_maxy - rear_setback_val:
        violations.append(
            f"SETBACK VIOLATION: Rear setback must be >= {rear_setback_val}ft. Currently only {lot_maxy - actual_house_ymax:.1f}ft.")

    # Minimum Side Setbacks
    if actual_house_xmin < lot_minx + side_setback_val or actual_house_xmax > lot_maxx - side_setback_val:
        violations.append(f"SETBACK VIOLATION: Minimum Side setback on both sides must be >= {side_setback_val}ft.")

    # Combined Side Setback Math
    left_yard = actual_house_xmin - lot_minx
    right_yard = lot_maxx - actual_house_xmax
    if (left_yard + right_yard) < combined_side_val:
        violations.append(
            f"SETBACK VIOLATION: Combined side yards must be >= {combined_side_val}ft. Currently {left_yard + right_yard:.1f}ft.")

    # Corner Lot Frontage Rule
    if is_corner:
        if actual_house_xmin < lot_minx + front_setback_val:
            violations.append(
                f"SETBACK VIOLATION (CORNER LOT): Secondary street frontage (Left edge) requires {front_setback_val}ft setback.")

    # --- D. LOT COVERAGE & AREA MAXIMIZATION ---
    actual_footprint_area = sum([b['area'] for b in rooms_data.values()])
    max_legal_area = lot_area * rules['max_coverage']

    if actual_footprint_area > max_legal_area:
        violations.append(
            f"COVERAGE VIOLATION: Footprint ({actual_footprint_area:.1f} sqft) exceeds max lot coverage ({max_legal_area:.1f} sqft).")

    # Optimization Heuristic: The design must utilize at least 95% of the legal maximum coverage
    optimization_target = max_legal_area * 0.95
    if actual_footprint_area < optimization_target:
        violations.append(
            f"OPTIMIZATION FAILURE: Maximize area! Current footprint ({actual_footprint_area:.1f} sqft) is below the target maximum ({optimization_target:.1f} sqft). Expand the rooms.")

    # --- E. SHAPELY GEOMETRIC CONSTRAINTS (Easements & Trees) ---
    if room_shapes:
        composite_footprint = unary_union(room_shapes)

        # 1. Protected Trees
        for i, tree_crz in enumerate(env_data.get('trees', [])):
            if composite_footprint.intersects(tree_crz):
                violations.append(f"ENVIRONMENTAL VIOLATION: Footprint breaches Protected Tree #{i + 1} CRZ.")

        # 2. Access Easements (10ft clearance required)
        for i, easement in enumerate(env_data.get('easements', [])):
            if composite_footprint.distance(easement) < 10.0:
                violations.append(
                    f"EASEMENT VIOLATION: Footprint must maintain 10ft clearance from Access Easement #{i + 1}.")

        # 3. Critical Areas
        for i, critical in enumerate(env_data.get('critical_areas', [])):
            if composite_footprint.intersects(critical):
                violations.append(f"ENVIRONMENTAL VIOLATION: Footprint intersects Critical Area #{i + 1}.")

    return json.dumps({
        "status": "REJECTED" if violations else "APPROVED",
        "violations": violations,
        "metrics": {
            "lot_area": lot_area,
            "max_allowed_coverage": max_legal_area,
            "actual_coverage": actual_footprint_area
        },
        "rooms": rooms_data
    }, indent=2)


if __name__ == "__main__":
    # Example Config passed by the master orchestration script
    test_config = {
        "zone": "R5",
        "corner_lot": False,
        "garage_type": "attached"
    }
    print(verify_bellevue_layout("generated_plot.dxf", test_config))