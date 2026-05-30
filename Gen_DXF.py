import ezdxf
from shapely.geometry import Polygon


def generate_dxf_from_sketch():
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()

    # 1. Define Standardized Layers for Multi-Agent Pipeline
    doc.layers.add("LOT_BOUNDARY", color=1)  # Red
    doc.layers.add("BUILDING_SETBACK", color=3)  # Green
    doc.layers.add("TREES", color=3)  # Green
    doc.layers.add("UTILITIES", color=5)  # Blue
    doc.layers.add("SBC_HOUSE_FOOTPRINT", color=6)  # Magenta

    # 2. Trace the Lot Boundary (Clockwise from Top-Left)
    # Using the exact lengths from the hand-drawn sketch
    lot_points = [
        (0, 80),  # Top-Left Start
        (75, 80),  # Top Edge: Right 75ft
        (75, 84),  # Top Notch: Up 4ft
        (80, 84),  # Top-Right Edge: Right 5ft
        (80, 4),  # Right Edge: Down 80ft
        (75, 4),  # Bottom-Right Edge: Left 5ft
        (75, -4),  # Bottom Right Notch: Down 8ft
        (35, -4),  # Bottom Middle Edge: Left 40ft
        (35, 4),  # Bottom Left Notch: Up 8ft
        (15, 4),  # Bottom-Left Edge: Left 20ft
        (15, 54),  # Left Edge: Up 50ft
        # ezdxf close=True will automatically draw the final diagonal line
        # from (15, 54) back to (0, 80) to close the 15x26ft gap.
    ]

    msp.add_lwpolyline(lot_points, dxfattribs={'layer': 'LOT_BOUNDARY'}, close=True)

    # 3. Calculate Building Setbacks (Green Inner Ring)
    # Using a 5ft uniform setback as a baseline for the SBC offsets
    UNIFORM_SETBACK = 5.0
    lot_polygon = Polygon(lot_points)

    # buffer(-distance) shrinks the polygon inward. join_style=2 keeps corners sharp.
    setback_polygon = lot_polygon.buffer(-UNIFORM_SETBACK, join_style=2)

    if not setback_polygon.is_empty:
        setback_coords = list(setback_polygon.exterior.coords)
        msp.add_lwpolyline(setback_coords, dxfattribs={'layer': 'BUILDING_SETBACK'})
    else:
        print("Warning: Offset distance collapsed the polygon entirely.")

    # 4. Add the Protected Tree
    # Placed in the 5x4 top-right notch as indicated in the sketch
    tree_center = (77.5, 82)
    tree_radius = 1.5
    msp.add_circle(tree_center, tree_radius, dxfattribs={'layer': 'TREES'})

    # 5. Add Utility Lines (Blue)
    # Placed in the bottom notches as sketched
    msp.add_line((25, 4), (25, 15), dxfattribs={'layer': 'UTILITIES'})  # Left utility
    msp.add_line((45, -4), (45, 10), dxfattribs={'layer': 'UTILITIES'})  # Right utility

    # 6. Generate the Proposed House Footprint
    # Creating a safe 40x40 rectangular footprint in the center of the buildable area
    house_points = [
        (25, 20),
        (65, 20),
        (65, 60),
        (25, 60)
    ]
    msp.add_lwpolyline(house_points, dxfattribs={'layer': 'SBC_HOUSE_FOOTPRINT'}, close=True)

    # 7. Save output
    filename = 'seattle_lot_plan_v2.dxf'
    doc.saveas(filename)
    print(f"File saved to {filename}. Ready for Z3 Verification Agent.")


if __name__ == '__main__':
    generate_dxf_from_sketch()