# Remotion showcase

Side-by-side comparison videos rendered from local experiment outputs:

- `Showcase` (`src/index.ts` → `src/Showcase.tsx`): 3-way comparison (Computer Use, MCP without RE,
  MCP with RE) with result screens, bar charts, tool lists, and 2× synchronized replay.
- `ShowcaseTwo` (`src/indexTwo.tsx` → `src/ShowcaseTwo.tsx`): 2-way comparison (Computer Use vs.
  RE-built MCP) including the animated build timeline.

No game footage, screenshots, or `node_modules` are committed here. Keep rendered game videos local
and do not publish them without permission.

## Data preparation and rendering

```sh
# 1. Populate data.json, build.json, and public/ from a local experiment directory
python3 showcase/prepare.py /path/to/experiment --round 1

# 2. Build the render image and render inside Docker
docker build -t fruit-ninja-builder/showcase-render:v1 -f showcase/Dockerfile.render showcase
docker run --rm -v "$PWD/showcase:/work" -w /work fruit-ninja-builder/showcase-render:v1 \
  bash -c "npm install && npm run render"
```
