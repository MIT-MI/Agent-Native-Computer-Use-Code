Revise the existing mounting-plate.FCStd for the confirmed board revision B. Work in millimeters.
The old revision A was 80 x 50 x 4, with radius-2.5 holes at (10,25) and (70,25). That drawing is superseded.
The confirmed revision B requires PlateStock to be 100 x 60 x 5. Set LeftHole and RightHole radii to 3 and their centers to (12,30) and (88,30), preserving each cutter's height 8 and Z=-2 so both holes go through the stock.
Retain the existing LeftClearance and MountingPlate linked cut features and their operands. Do not replace the feature tree with an uneditable imported solid or make a second final plate.
BoardReference is an unchanged reference block: preserve its 60 x 30 x 8 dimensions and placement (10,10,4).
Recompute and inspect the final MountingPlate: it must be one valid solid, 100 x 60 x 5, with both through holes.
Save the native project in place. Export only MountingPlate, excluding stock, cutters and BoardReference, to mounting-plate.step and mounting-plate.stl in the same folder. Inspect the model before finishing.

The working folder is /app/work. Use the assigned reef interface only. In CUA mode, observe screenshots and use ordinary mouse/keyboard input. In MCP mode, use application tools. Do not use shell commands, file APIs, scripts, developer consoles, DOM, accessibility trees, direct HTTP, or other agents to perform the task.
