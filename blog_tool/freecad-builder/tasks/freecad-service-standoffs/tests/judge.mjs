import { mkdir, readFile as readFile8, writeFile as writeFile6 } from "node:fs/promises";
import { resolve as resolve20 } from "node:path";
import { resolve as resolve9 } from "node:path";
import { writeFile as writeFile3, unlink as unlink3 } from "node:fs/promises";
import { isDeepStrictEqual } from "node:util";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import { mkdirSync, realpathSync, existsSync, statSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
function workspace() {
  const root = resolve(process.env.REEF_WORKSPACE ?? process.cwd());
  mkdirSync(root, { recursive: true });
  return realpathSync(root);
}
var exec = promisify(execFile);
var NativeEngine = class {
  constructor(application) {
    this.application = application;
  }
  application;
  pending = Promise.resolve();
  run(statements, params) {
    const operation = this.pending.then(
      () => this.execute(statements, params)
    );
    this.pending = operation.catch(() => void 0);
    return operation;
  }
  async execute(statements, params) {
    const temporary = await mkdtemp(join(tmpdir(), "reef-native-"));
    const resultPath = join(temporary, "result.json");
    const setup = this.application === "freecad" ? [
      "sys.path.extend([os.environ.get('FREECAD_LIB',''),'/usr/lib/freecad-python3/lib','/usr/lib/freecad/lib','/usr/local/lib/freecad/lib'])",
      "import FreeCAD as App, Part"
    ] : ["import bpy", "from mathutils import Vector"];
    const body = [...setup, ...statements, "reply={'ok':True,'result':result}"];
    const expression = [
      "import sys,json,os,math",
      "p=json.loads(sys.argv[-2])",
      "try:",
      ...body.map((line) => "    " + line),
      "except Exception as error:",
      "    reply={'ok':False,'error':type(error).__name__+': '+str(error)}",
      "with open(sys.argv[-1],'w') as stream: json.dump(reply,stream,allow_nan=False)"
    ].join("\n");
    const args = this.application === "blender" ? [
      "--background",
      "--threads",
      "2",
      "--python-expr",
      expression,
      "--",
      JSON.stringify(params),
      resultPath
    ] : ["-c", expression, JSON.stringify(params), resultPath];
    const binary = this.application === "blender" ? process.env.BLENDER_BIN ?? "blender" : process.env.FREECAD_PYTHON ?? "/usr/bin/python3";
    try {
      await exec(binary, args, {
        cwd: workspace(),
        timeout: Number(process.env.REEF_ENGINE_TIMEOUT ?? 180) * 1e3,
        maxBuffer: 4 * 1024 * 1024,
        env: {
          ...process.env,
          OMP_NUM_THREADS: "2",
          OPENBLAS_NUM_THREADS: "1",
          MKL_NUM_THREADS: "1"
        }
      });
      const reply = JSON.parse(await readFile(resultPath, "utf8"));
      if (!reply.ok) throw new Error(reply.error);
      return reply.result;
    } catch (error) {
      if (error instanceof Error && "killed" in error && error.killed) {
        throw new Error(
          "Engine deadline exceeded; inspect the project before retrying"
        );
      }
      throw error;
    } finally {
      await rm(temporary, { recursive: true, force: true });
    }
  }
};
import { readFile as readFile2, writeFile } from "node:fs/promises";
import { execFile as execFile2 } from "node:child_process";
import { promisify as promisify2 } from "node:util";
import { unzipSync, strFromU8 } from "fflate";
import { XMLParser } from "fast-xml-parser";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { dirname as dirname2, resolve as resolve2 } from "node:path";
import { fileURLToPath } from "node:url";
var repoRoot = process.env.REEF_REPO_ROOT ?? resolve2(dirname2(fileURLToPath(import.meta.url)), "../..");
function stringEnvironment(extra = {}) {
  return Object.fromEntries(
    Object.entries({ ...process.env, ...extra }).filter(
      (entry) => typeof entry[1] === "string"
    )
  );
}
async function connectApplication(name, env = {}) {
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [resolve2(repoRoot, "dist/cli.js"), "serve", name],
    env: stringEnvironment(env),
    stderr: "inherit"
  });
  const client = new Client({
    name: "reef-integration-client",
    version: "0.1.0"
  });
  try {
    await client.connect(transport);
  } catch (error) {
    await transport.close();
    throw error;
  }
  return client;
}
async function call(client, name, args = {}) {
  const reply = await client.callTool({ name, arguments: args }, void 0, {
    timeout: 18e4
  });
  const blocks = reply.content;
  const text = blocks.filter((b) => b.type === "text").map((b) => b.text).join("\n");
  if (reply.isError) throw new Error(`${name}: ${text}`);
  if (reply.structuredContent !== void 0) return reply.structuredContent;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}
async function withApplication(name, work, action, extra = {}) {
  const client = await connectApplication(name, {
    ...extra,
    REEF_WORKSPACE: work,
    RAYON_NUM_THREADS: "2"
  });
  try {
    return await action(client);
  } finally {
    await client.close();
  }
}
async function criterion(name, predicate) {
  try {
    const result = await predicate();
    return { name, passed: !!result };
  } catch (error) {
    return {
      name,
      passed: false,
      details: error instanceof Error ? error.message : String(error)
    };
  }
}
function array(value) {
  return value === void 0 ? [] : Array.isArray(value) ? value : [value];
}
function canonicalText(value) {
  return value.normalize("NFKC").replace(/[\u2010-\u2015\u2212]/g, "-").replace(/\s+/g, " ").trim();
}
function enabledProperty(value) {
  return value !== void 0 && !(value && typeof value === "object" && ["0", "false", "off"].includes(
    String(value["@w:val"]).toLowerCase()
  ));
}
function xmlText(value) {
  if (typeof value === "string" || typeof value === "number")
    return String(value);
  if (!value || typeof value !== "object") return "";
  if (Array.isArray(value)) return value.map(xmlText).join("");
  const object = value;
  return Object.entries(object).filter(([key]) => !key.startsWith("@")).map(([, value2]) => xmlText(value2)).join("");
}
async function docx(path) {
  const zip = unzipSync(await readFile2(path));
  const raw = strFromU8(zip["word/document.xml"]);
  const xml = new XMLParser({
    ignoreAttributes: false,
    attributeNamePrefix: "@"
  }).parse(raw);
  const paragraphs = array(xml["w:document"]["w:body"]["w:p"]);
  const text = paragraphs.map(
    (p) => array(p["w:r"]).map((r) => xmlText(r["w:t"])).join("")
  );
  const numbering = zip["word/numbering.xml"] ? new XMLParser({
    ignoreAttributes: false,
    attributeNamePrefix: "@"
  }).parse(strFromU8(zip["word/numbering.xml"]))["w:numbering"] : {};
  const counters = /* @__PURE__ */ new Map();
  const numberedText = paragraphs.map((p, index) => {
    const props = p["w:pPr"]?.["w:numPr"];
    const id = String(props?.["w:numId"]?.["@w:val"] ?? "0");
    const level = String(props?.["w:ilvl"]?.["@w:val"] ?? "0");
    if (id === "0" || level !== "0") return text[index];
    const num = array(numbering["w:num"]).find(
      (n) => String(n["@w:numId"]) === id
    );
    const abstractId = num?.["w:abstractNumId"]?.["@w:val"];
    const abstract = array(numbering["w:abstractNum"]).find(
      (n) => n["@w:abstractNumId"] === abstractId
    );
    const format = array(abstract?.["w:lvl"]).find(
      (n) => String(n["@w:ilvl"]) === level
    );
    if (format?.["w:numFmt"]?.["@w:val"] !== "decimal")
      return text[index];
    const override = array(num?.["w:lvlOverride"]).find(
      (n) => String(n["@w:ilvl"]) === level
    );
    const start = Number(
      override?.["w:startOverride"]?.["@w:val"] ?? format?.["w:start"]?.["@w:val"] ?? 1
    );
    const value = counters.has(id) ? counters.get(id) + 1 : start;
    counters.set(id, value);
    const label = String(
      format?.["w:lvlText"]?.["@w:val"] ?? "%1."
    ).replace("%1", String(value));
    return `${label} ${text[index]}`;
  });
  return { paragraphs, text, numberedText, raw };
}
var execute = promisify2(execFile2);
async function pdfText(path) {
  return (await execute("pdftotext", [path, "-"], { timeout: 3e4 })).stdout;
}
import { resolve as resolve3 } from "node:path";
async function cadState(work) {
  return new NativeEngine("freecad").run(
    [
      "doc=App.openDocument(p['project']); doc.recompute()",
      "def shape_state(s):",
      "    if s.isNull(): return {'valid':False,'solids':0,'volume':0.0,'bounds':[0.0]*6,'shape_error':'NULL shape'}",
      "    b=s.BoundBox; return {'valid':s.isValid(),'solids':len(s.Solids),'volume':s.Volume,'bounds':[b.XMin,b.YMin,b.ZMin,b.XMax,b.YMax,b.ZMax]}",
      "result={'objects':{}}",
      "for o in doc.Objects:",
      "    entry={'type':o.TypeId,'label':o.Label,'dimensions':{k:float(getattr(o,k).Value) for k in ('Length','Width','Height','Radius') if hasattr(o,k)},'placement':list(o.Placement.Base)}",
      "    if hasattr(o,'Shape') and not o.Shape.isNull(): entry.update(shape_state(o.Shape))",
      "    if hasattr(o,'Base') and o.Base: entry['base']=o.Base.Name",
      "    if hasattr(o,'Tool') and o.Tool: entry['tool']=o.Tool.Name",
      "    if o.TypeId=='Part::MultiFuse': entry['shapes']=[s.Name for s in o.Shapes]",
      "    result['objects'][o.Name]=entry",
      "if os.path.exists(p['step']): result['step']=shape_state(Part.read(p['step']))",
      "if os.path.exists(p['stl']):",
      "    import Mesh",
      "    m=Mesh.Mesh(p['stl']); result['stl']={'solid':m.isSolid(),'volume':abs(m.Volume),'facets':m.CountFacets,'vertices':[list(v) for v in m.Topology[0]]}"
    ],
    {
      project: resolve3(work, "mounting-plate.FCStd"),
      step: resolve3(work, "mounting-plate.step"),
      stl: resolve3(work, "mounting-plate.stl")
    }
  );
}
async function sceneState(work, options = {}) {
  return new NativeEngine("blender").run(
    [
      "bpy.ops.wm.open_mainfile(filepath=p['project']); scene=bpy.context.scene; bpy.context.view_layer.update()",
      "result={'objects':{},'resolution':[scene.render.resolution_x,scene.render.resolution_y,scene.render.resolution_percentage],'render_engine':scene.render.engine,'samples':scene.cycles.samples,'device':scene.cycles.device}",
      "for o in scene.objects:",
      "    entry={'type':o.type,'location':list(o.location),'rotation':[math.degrees(v) for v in o.rotation_euler],'scale':list(o.scale),'dimensions':list(o.dimensions),'hidden':o.hide_render}",
      "    if o.type=='MESH':",
      "        entry['vertices']=len(o.data.vertices); entry['mesh_data']=o.data.name",
      "        if o.active_material and o.active_material.use_nodes:",
      "            n=o.active_material.node_tree.nodes.get('Principled BSDF')",
      "            if n: entry['material']={'color':list(n.inputs['Base Color'].default_value),'roughness':n.inputs['Roughness'].default_value,'metallic':n.inputs['Metallic'].default_value}",
      "    if o.type=='MESH' and p['geometry']:",
      "        o.data.calc_loop_triangles(); entry['world_vertices']=[list(o.matrix_world @ v.co) for v in o.data.vertices]; entry['triangles']=[list(t.vertices) for t in o.data.loop_triangles]",
      "    if o.type=='LIGHT': entry.update(energy=o.data.energy,light_kind=o.data.type)",
      "    result['objects'][o.name]=entry",
      "if scene.camera:",
      "    c=scene.camera; direction=(Vector(p['target'])-c.location).normalized(); forward=c.matrix_world.to_quaternion() @ Vector((0,0,-1))",
      "    result['camera']={'lens':c.data.lens,'location':list(c.location),'aim':direction.dot(forward),'projection':c.data.type,'ortho_scale':c.data.ortho_scale}"
    ],
    {
      project: resolve3(work, options.projectFile ?? "product-hero.blend"),
      target: options.target ?? [0, 0, 1],
      geometry: options.geometry ?? false
    }
  );
}
var closeTo = (actual, expected, tolerance = 1e-3) => Number.isFinite(actual) && Math.abs(actual - expected) <= tolerance;
var vectorClose = (actual, expected, tolerance = 1e-3) => actual?.length === expected.length && actual.every((value, index) => closeTo(value, expected[index], tolerance));
import { resolve as resolve4, join as join2 } from "node:path";
import { mkdtemp as mkdtemp2, rm as rm2 } from "node:fs/promises";
import { tmpdir as tmpdir2 } from "node:os";
async function pngEvidence(path, width, height) {
  const probe = JSON.parse(
    (await execute(
      "ffprobe",
      [
        "-v",
        "error",
        "-show_entries",
        "stream=codec_name,width,height",
        "-of",
        "json",
        path
      ],
      { timeout: 15e3 }
    )).stdout
  ).streams?.[0];
  if (probe?.codec_name !== "png" || probe.width !== width || probe.height !== height)
    return false;
  const { stdout } = await execute(
    "ffmpeg",
    [
      "-loglevel",
      "error",
      "-i",
      path,
      "-vf",
      "scale=32:18",
      "-frames:v",
      "1",
      "-threads",
      "1",
      "-pix_fmt",
      "gray",
      "-f",
      "rawvideo",
      "pipe:1"
    ],
    { encoding: "buffer", timeout: 15e3, maxBuffer: 4096 }
  );
  const pixels2 = [...stdout], mean = pixels2.reduce((a, b) => a + b, 0) / pixels2.length;
  return pixels2.length === 576 && pixels2.reduce((a, b) => a + (b - mean) ** 2, 0) / pixels2.length > 25;
}
async function pixels(path) {
  return (await execute(
    "ffmpeg",
    [
      "-loglevel",
      "error",
      "-i",
      path,
      "-vf",
      "scale=64:36",
      "-frames:v",
      "1",
      "-threads",
      "1",
      "-pix_fmt",
      "rgb24",
      "-f",
      "rawvideo",
      "pipe:1"
    ],
    { encoding: "buffer", timeout: 15e3, maxBuffer: 8192 }
  )).stdout;
}
async function pngMatchesSavedScene(work, project, output, state) {
  if (state.render_engine !== "CYCLES" || state.device !== "CPU" || state.samples !== 16 || state.resolution.join(",") !== "640,360,100")
    return false;
  if (!await pngEvidence(resolve4(work, output), 640, 360)) return false;
  const temporary = await mkdtemp2(join2(tmpdir2(), "reef-render-judge-")), reference = join2(temporary, "scene.png");
  try {
    await new NativeEngine("blender").run(
      [
        "bpy.ops.wm.open_mainfile(filepath=p['project'])",
        "scene=bpy.context.scene; scene.render.threads_mode='FIXED'; scene.render.threads=2",
        "scene.render.image_settings.file_format='PNG'; scene.render.filepath=p['output']",
        "bpy.ops.render.render(write_still=True); result={'rendered':True}"
      ],
      { project: resolve4(work, project), output: reference }
    );
    const actual = await pixels(resolve4(work, output)), expected = await pixels(reference);
    return actual.length === 6912 && expected.length === actual.length && [...actual].reduce((sum, v, i) => sum + Math.abs(v - expected[i]), 0) / actual.length <= 2;
  } finally {
    await rm2(temporary, { recursive: true, force: true });
  }
}
function triangleVolume(vertices, triangles) {
  let value = 0;
  for (const [i, j, k] of triangles) {
    const a = vertices[i], b = vertices[j], c = vertices[k];
    if (!a || !b || !c || [...a, ...b, ...c].some((v) => !Number.isFinite(v)))
      return NaN;
    value += (a[0] * (b[1] * c[2] - b[2] * c[1]) + a[1] * (b[2] * c[0] - b[0] * c[2]) + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6;
  }
  return Math.abs(value);
}
function samePointCloud(a, b, tolerance = 2e-3) {
  if (!a.length || !b.length || [...a, ...b].some(
    (p) => p.length !== 3 || p.some((v) => !Number.isFinite(v))
  ))
    return false;
  const covers = (source, target) => {
    const grid = /* @__PURE__ */ new Map();
    for (const p of target) {
      const key = p.map((v) => Math.floor(v / tolerance)).join(",");
      const bucket = grid.get(key) ?? [];
      bucket.push(p);
      grid.set(key, bucket);
    }
    return source.every((p) => {
      const [x, y, z2] = p.map((v) => Math.floor(v / tolerance));
      for (let dx = -1; dx <= 1; dx++)
        for (let dy = -1; dy <= 1; dy++)
          for (let dz = -1; dz <= 1; dz++) {
            if (grid.get([x + dx, y + dy, z2 + dz].join(","))?.some(
              (q) => q.every((v, i) => Math.abs(v - p[i]) <= tolerance)
            ))
              return true;
          }
      return false;
    });
  };
  return covers(a, b) && covers(b, a);
}
import { resolve as resolve5 } from "node:path";
import { unlink } from "node:fs/promises";
var volume = 100 * 60 * 5 - 2 * Math.PI * 3 ** 2 * 5;
var boardPlate = {
  id: "freecad-board-plate",
  application: "freecad",
  title: "Revise a linked mounting plate for the confirmed board revision",
  inputFile: "mounting-plate.FCStd",
  outputs: [
    "mounting-plate.FCStd",
    "mounting-plate.step",
    "mounting-plate.stl"
  ],
  mcpMode: "file",
  brief: `Revise the existing mounting-plate.FCStd for the confirmed board revision B. Work in millimeters.
The old revision A was 80 x 50 x 4, with radius-2.5 holes at (10,25) and (70,25). That drawing is superseded.
The confirmed revision B requires PlateStock to be 100 x 60 x 5. Set LeftHole and RightHole radii to 3 and their centers to (12,30) and (88,30), preserving each cutter's height 8 and Z=-2 so both holes go through the stock.
Retain the existing LeftClearance and MountingPlate linked cut features and their operands. Do not replace the feature tree with an uneditable imported solid or make a second final plate.
BoardReference is an unchanged reference block: preserve its 60 x 30 x 8 dimensions and placement (10,10,4).
Recompute and inspect the final MountingPlate: it must be one valid solid, 100 x 60 x 5, with both through holes.
Save the native project in place. Export only MountingPlate, excluding stock, cutters and BoardReference, to mounting-plate.step and mounting-plate.stl in the same folder. Inspect the model before finishing.`,
  mcpGuidance: "Use FreeCAD's typed file-based MCP tools on the existing project. Inspect IDs and dimensions, update stock/cutters, preserve the linked cuts, and export the final feature. These tools reopen and save native files; they do not control the open GUI session. No generic scripts or file APIs.",
  async prepare(work) {
    const path = resolve5(work, "mounting-plate.FCStd");
    await withApplication("freecad", work, async (c) => {
      await call(c, "create_document", {
        path,
        label: "Reef board mounting plate",
        overwrite: true
      });
      await call(c, "add_primitive", {
        path,
        kind: "box",
        name: "PlateStock",
        length: 80,
        width: 50,
        height: 4
      });
      for (const [name, x] of [
        ["LeftHole", 10],
        ["RightHole", 70]
      ]) {
        await call(c, "add_primitive", {
          path,
          kind: "cylinder",
          name,
          radius: 2.5,
          height: 8
        });
        await call(c, "set_placement", {
          path,
          object_id: name,
          x,
          y: 25,
          z: -2
        });
      }
      await call(c, "boolean_operation", {
        path,
        operation: "cut",
        base_id: "PlateStock",
        tool_id: "LeftHole",
        name: "LeftClearance"
      });
      await call(c, "boolean_operation", {
        path,
        operation: "cut",
        base_id: "LeftClearance",
        tool_id: "RightHole",
        name: "MountingPlate"
      });
      await call(c, "add_primitive", {
        path,
        kind: "box",
        name: "BoardReference",
        length: 60,
        width: 30,
        height: 8
      });
      await call(c, "set_placement", {
        path,
        object_id: "BoardReference",
        x: 10,
        y: 10,
        z: 4
      });
    });
    await unlink(resolve5(work, "mounting-plate.FCStd1")).catch(
      (error) => {
        if (error.code !== "ENOENT") throw error;
      }
    );
    return { revision: "B", expected_volume_mm3: volume };
  },
  async verify(context) {
    const state = cadState(context.work);
    return Promise.all([
      criterion("stock has the confirmed revision-B dimensions", async () => {
        const o = (await state).objects.PlateStock;
        return o.type === "Part::Box" && vectorClose(
          [o.dimensions.Length, o.dimensions.Width, o.dimensions.Height],
          [100, 60, 5]
        );
      }),
      criterion(
        "left hole is radius 3 at the confirmed through-cut placement",
        async () => {
          const o = (await state).objects.LeftHole;
          return o.type === "Part::Cylinder" && closeTo(o.dimensions.Radius, 3) && closeTo(o.dimensions.Height, 8) && vectorClose(o.placement, [12, 30, -2]);
        }
      ),
      criterion(
        "right hole is radius 3 at the confirmed through-cut placement",
        async () => {
          const o = (await state).objects.RightHole;
          return o.type === "Part::Cylinder" && closeTo(o.dimensions.Radius, 3) && closeTo(o.dimensions.Height, 8) && vectorClose(o.placement, [88, 30, -2]);
        }
      ),
      criterion(
        "both original parametric cut links are preserved",
        async () => {
          const o = (await state).objects;
          return o.LeftClearance.type === "Part::Cut" && o.LeftClearance.base === "PlateStock" && o.LeftClearance.tool === "LeftHole" && o.MountingPlate.type === "Part::Cut" && o.MountingPlate.base === "LeftClearance" && o.MountingPlate.tool === "RightHole";
        }
      ),
      criterion("the reference block is unchanged", async () => {
        const o = (await state).objects.BoardReference;
        return o.type === "Part::Box" && vectorClose(
          [o.dimensions.Length, o.dimensions.Width, o.dimensions.Height],
          [60, 30, 8]
        ) && vectorClose(o.placement, [10, 10, 4]);
      }),
      criterion(
        "the final model is one valid solid with two through holes",
        async () => {
          const o = (await state).objects.MountingPlate;
          return o.valid && o.solids === 1 && closeTo(o.volume, volume, 0.01) && vectorClose(o.bounds, [0, 0, 0, 100, 60, 5]);
        }
      ),
      criterion(
        "the original six-object feature tree is retained",
        async () => Object.keys((await state).objects).sort().join(",") === [
          "PlateStock",
          "LeftHole",
          "RightHole",
          "LeftClearance",
          "MountingPlate",
          "BoardReference"
        ].sort().join(",")
      ),
      criterion(
        "STEP reimport contains only the final valid plate",
        async () => {
          const s = (await state).step;
          return s?.valid && s.solids === 1 && closeTo(s.volume, volume, 0.1) && vectorClose(s.bounds, [0, 0, 0, 100, 60, 5], 0.01);
        }
      ),
      criterion(
        "STL reimport is closed and matches the final plate volume",
        async () => {
          const s = (await state).stl;
          return s?.solid && s.facets > 0 && closeTo(s.volume, volume, volume * 2e-3);
        }
      )
    ]);
  }
};
var baseVolume = 100 * 60 * 5 - 2 * Math.PI * 9 * 5;
var original = [
  "PlateStock",
  "LeftHole",
  "RightHole",
  "LeftClearance",
  "MountingPlate",
  "BoardReference"
];
var posts = [
  [20, 15, 5],
  [80, 15, 5],
  [20, 45, 5],
  [80, 45, 5]
];
function intact(s) {
  const o = s.objects;
  return vectorClose(
    [
      o.PlateStock.dimensions.Length,
      o.PlateStock.dimensions.Width,
      o.PlateStock.dimensions.Height
    ],
    [100, 60, 5]
  ) && ["LeftHole", "RightHole"].every(
    (name, i) => o[name].type === "Part::Cylinder" && closeTo(o[name].dimensions.Radius, 3) && closeTo(o[name].dimensions.Height, 8) && vectorClose(o[name].placement, [i ? 88 : 12, 30, -2])
  );
}
function linked(s) {
  const o = s.objects;
  return o.LeftClearance.type === "Part::Cut" && o.LeftClearance.base === "PlateStock" && o.LeftClearance.tool === "LeftHole" && o.MountingPlate.type === "Part::Cut" && o.MountingPlate.base === "LeftClearance" && o.MountingPlate.tool === "RightHole";
}
function names(kind) {
  return kind === "slot" ? ["CableSlot", "CablePlate"] : posts.flatMap((_, i) => ["Standoff" + (i + 1), "Raised" + (i + 1)]);
}
function featureRoles(state, labels) {
  const byLabel = /* @__PURE__ */ new Map(), byId = /* @__PURE__ */ new Map();
  for (const label of labels) {
    const matches = Object.entries(state.objects).filter(
      ([id, o]) => (o.label ?? id) === label
    );
    if (matches.length > 1) throw Error("Ambiguous feature label: " + label);
    if (matches.length === 1) {
      byLabel.set(label, matches[0][0]);
      byId.set(matches[0][0], label);
    }
  }
  const objects = { ...state.objects };
  for (const [label, id] of byLabel) {
    const o = state.objects[id];
    objects[label] = {
      ...o,
      base: byId.get(o.base ?? "") ?? o.base,
      tool: byId.get(o.tool ?? "") ?? o.tool,
      shapes: o.shapes?.map((name) => byId.get(name) ?? name)
    };
  }
  return { objects, ids: [...byLabel.values()] };
}
async function verifyPlateVariant(kind, context, sharedState) {
  const state = sharedState ?? cadState(context.work), volume2 = baseVolume + (kind === "slot" ? -800 : 4 * Math.PI * 16 * 10), bounds = [0, 0, 0, 100, 60, kind === "slot" ? 5 : 15];
  return Promise.all([
    criterion(
      "the confirmed stock and through-hole geometry is preserved",
      async () => intact(await state)
    ),
    criterion(
      "both original linked cut operands are preserved",
      async () => linked(await state)
    ),
    criterion("the reference block is unchanged", async () => {
      const o = (await state).objects.BoardReference;
      return o.type === "Part::Box" && vectorClose(
        [o.dimensions.Length, o.dimensions.Width, o.dimensions.Height],
        [60, 30, 8]
      ) && vectorClose(o.placement, [10, 10, 4]);
    }),
    criterion(
      "the underlying mounting plate remains one valid solid",
      async () => {
        const o = (await state).objects.MountingPlate;
        return o.valid && o.solids === 1 && closeTo(o.volume, baseVolume, 0.01) && vectorClose(o.bounds, [0, 0, 0, 100, 60, 5]);
      }
    ),
    criterion(
      "new parametric components have the specified geometry",
      async () => {
        const o = featureRoles(await state, names(kind)).objects;
        if (kind === "slot")
          return o.CableSlot.type === "Part::Box" && vectorClose(
            [
              o.CableSlot.dimensions.Length,
              o.CableSlot.dimensions.Width,
              o.CableSlot.dimensions.Height
            ],
            [20, 8, 9]
          ) && vectorClose(o.CableSlot.placement, [40, 26, -2]);
        return posts.every((p, i) => {
          const v = o["Standoff" + (i + 1)];
          return v.type === "Part::Cylinder" && closeTo(v.dimensions.Radius, 4) && closeTo(v.dimensions.Height, 10) && vectorClose(v.placement, p);
        });
      }
    ),
    criterion(
      "new features retain the specified dependency chain",
      async () => {
        const o = featureRoles(await state, names(kind)).objects;
        if (kind === "slot")
          return o.CablePlate.type === "Part::Cut" && o.CablePlate.base === "MountingPlate" && o.CablePlate.tool === "CableSlot";
        return posts.every((_, i) => {
          const v = o["Raised" + (i + 1)];
          return v.type === "Part::MultiFuse" ? v.shapes?.length === 2 && v.shapes[0] === (i ? "Raised" + i : "MountingPlate") && v.shapes[1] === "Standoff" + (i + 1) : v.type === "Part::Fuse" && v.base === (i ? "Raised" + i : "MountingPlate") && v.tool === "Standoff" + (i + 1);
        });
      }
    ),
    criterion(
      "there are no duplicate or unexpected feature objects",
      async () => {
        const raw = await state, roles = featureRoles(raw, names(kind));
        return Object.keys(raw.objects).length === original.length + names(kind).length && original.every((id) => raw.objects[id]) && roles.ids.length === names(kind).length && new Set(roles.ids).size === roles.ids.length && roles.ids.every((id) => !original.includes(id));
      }
    ),
    criterion(
      "the final feature is one valid solid with the expected bounds and volume",
      async () => {
        const o = featureRoles(await state, names(kind)).objects[kind === "slot" ? "CablePlate" : "Raised4"];
        return o.valid && o.solids === 1 && closeTo(o.volume, volume2, 0.05) && vectorClose(o.bounds, bounds);
      }
    ),
    criterion(
      "STEP reimport contains only the final modified solid",
      async () => {
        const s = (await state).step;
        return s?.valid && s.solids === 1 && closeTo(s.volume, volume2, 0.1) && vectorClose(s.bounds, bounds, 0.01);
      }
    ),
    criterion(
      "STL reimport is closed and has the expected modified volume",
      async () => {
        const s = (await state).stl;
        return s?.solid && s.facets > 0 && closeTo(s.volume, volume2, volume2 * 2e-3);
      }
    )
  ]);
}
function variant(kind) {
  return {
    ...boardPlate,
    id: kind === "slot" ? "freecad-cable-slot" : "freecad-service-standoffs",
    family: "freecad-board-plate",
    split: kind === "slot" ? "development" : "held-out",
    title: kind === "slot" ? "Add a linked cable opening to a revised mounting plate" : "Add four fused service standoffs without flattening the feature tree",
    brief: boardPlate.brief.split("Save the native project")[0] + (kind === "slot" ? "Add CableSlot as a parametric 20 x 8 x 9 box at (40,26,-2). Add CablePlate as a linked cut with Base=MountingPlate and Tool=CableSlot. The cable slot must pass through the plate. Retain all original objects and this new two-object dependency. Export only CablePlate." : "Add four cylinder standoffs Standoff1..Standoff4, each radius 4 and height 10, at (20,15,5), (80,15,5), (20,45,5), (80,45,5). Fuse them sequentially as Raised1=MountingPlate+Standoff1, Raised2=Raised1+Standoff2, Raised3=Raised2+Standoff3, Raised4=Raised3+Standoff4. Preserve this editable dependency chain and export only Raised4.") + " Save the native project in place and deliver mounting-plate.step and mounting-plate.stl. Exclude BoardReference and intermediate features from both exports. Inspect the final solid. The new feature names refer to visible FreeCAD labels; GUI-generated internal IDs for new features are acceptable. Keep the original six internal object IDs unchanged.",
    verify: (context) => verifyPlateVariant(kind, context)
  };
}
var cableSlot = variant("slot");
var serviceStandoffs = variant("posts");
import { resolve as resolve6 } from "node:path";
import { readFile as readFile3, unlink as unlink2 } from "node:fs/promises";
function linearColor(hex) {
  const rgb = [0, 2, 4].map(
    (offset) => parseInt(hex.replace(/^#/, "").slice(offset, offset + 2), 16) / 255
  ).map((v) => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
  return [rgb[0], rgb[1], rgb[2], 1];
}
var productHero = {
  id: "blender-product-variants",
  application: "blender",
  title: "Prepare two independently editable product variants and a delivery render",
  inputFile: "product-hero.blend",
  outputs: ["product-hero.blend", "product-hero.png", "product-variants.obj"],
  mcpMode: "file",
  brief: [
    "Update the existing product-hero.blend for the two-color Reef pilot catalog image. Preserve Body and duplicate it as Body_Coral with independent mesh/material slots.",
    "Set Body's location to (-1.7,0,1), scale to (1.2,0.8,0.65), and Z rotation to +15 degrees. Set Body_Coral to (1.7,0,1), the same scale, and Z rotation -15 degrees. Both X/Y rotations remain zero.",
    "Use sRGB swatches #168B83 for Body and #E6614C for Body_Coral, with Principled roughness 0.3 and metallic 0.25 on both. Changing one product's material must not recolor the other.",
    "Frame both products with the active camera at (7,-10,7), looking at (0,0,1), using a 52 mm lens.",
    "Preserve Floor's geometry, transform and material, both existing lights, and ArchiveSample's geometry and transform. ArchiveSample must remain excluded from rendering.",
    "Save the scene in place. Render a nonempty 640 x 360 PNG named product-hero.png using Cycles on CPU, 16 samples and 100% resolution.",
    "Export only Body and Body_Coral to product-variants.obj, excluding Floor, ArchiveSample, camera and lights. Review the delivered image before finishing."
  ].join("\n"),
  mcpGuidance: "Use Blender's typed file-based tools to inspect, duplicate, transform, assign independent materials, configure the camera, render and export. Material RGBA values are scene-linear: convert the brief's sRGB swatches accordingly. Tools reopen/save the native file rather than controlling the visible GUI. No scripts or generic file APIs.",
  async prepare(work) {
    const path = resolve6(work, "product-hero.blend");
    await withApplication("blender", work, async (c) => {
      await call(c, "create_scene", { path, overwrite: true });
      for (const [name, kind, location, scale] of [
        ["Body", "cube", [0, 0, 1], [1.5, 1, 0.6]],
        ["Floor", "plane", [0, 0, 0], [6, 6, 1]],
        ["ArchiveSample", "cube", [4, 3, 0.5], [0.5, 0.5, 0.5]]
      ]) {
        await call(c, "add_mesh", { path, kind, name, location });
        await call(c, "set_transform", {
          path,
          object_name: name,
          location,
          scale
        });
        await call(c, "assign_material", {
          path,
          object_name: name,
          name: name + "Material",
          color: name === "Floor" ? [0.13, 0.15, 0.18, 1] : [0.5, 0.5, 0.5, 1]
        });
      }
      await call(c, "configure_camera", {
        path,
        location: [7, -9, 6],
        target: [0, 0, 1],
        lens_mm: 50
      });
      await call(c, "add_light", {
        path,
        name: "Key",
        location: [3, -4, 6],
        energy: 1e3
      });
      await call(c, "add_light", {
        path,
        name: "Fill",
        location: [-4, -2, 3],
        energy: 400
      });
    });
    await new NativeEngine("blender").run(
      [
        "bpy.ops.wm.open_mainfile(filepath=p['path']); bpy.data.objects['ArchiveSample'].hide_render=True",
        "bpy.context.scene.render.threads_mode='FIXED'; bpy.context.scene.render.threads=2",
        "bpy.ops.wm.save_as_mainfile(filepath=p['path']); result={'seeded':True}"
      ],
      { path }
    );
    await unlink2(resolve6(work, "product-hero.blend1")).catch(
      (error) => {
        if (error.code !== "ENOENT") throw error;
      }
    );
    return { swatches: { Body: "#168B83", Body_Coral: "#E6614C" } };
  },
  verify: verifyProduct
};
async function verifyProduct(context, options = {}) {
  const [width, height] = options.resolution ?? [640, 360];
  const state = sceneState(context.work);
  return Promise.all([
    criterion(
      "both product meshes have the confirmed locations and scale",
      async () => {
        const d = await state;
        return ["Body", "Body_Coral"].every(
          (name, i) => d.objects[name]?.type === "MESH" && d.objects.Body.mesh_data !== d.objects.Body_Coral.mesh_data && d.objects[name].vertices === 8 && vectorClose(d.objects[name].location, [i ? 1.7 : -1.7, 0, 1]) && vectorClose(d.objects[name].scale, [1.2, 0.8, 0.65])
        );
      }
    ),
    criterion(
      "product rotations match the mirrored catalog composition",
      async () => {
        const d = await state;
        return vectorClose(d.objects.Body.rotation, [0, 0, 15], 0.01) && vectorClose(d.objects.Body_Coral.rotation, [0, 0, -15], 0.01);
      }
    ),
    criterion(
      "both swatches and Principled material properties are correct",
      async () => {
        const d = await state;
        return Object.entries({
          Body: "#168B83",
          Body_Coral: "#E6614C"
        }).every(([name, hex]) => {
          const m = d.objects[name]?.material;
          return m && vectorClose(m.color, linearColor(hex), 3e-3) && closeTo(m.roughness, 0.3) && closeTo(m.metallic, 0.25);
        });
      }
    ),
    criterion(
      "the camera uses the requested projection, framing and target",
      async () => {
        const c = (await state).camera;
        return (options.orthographic ? c.projection === "ORTHO" && closeTo(c.ortho_scale, 8) : c.projection === "PERSP" && closeTo(c.lens, 52)) && vectorClose(
          c.location,
          options.orthographic ? [0, -10, 6] : [7, -10, 7]
        ) && c.aim > 0.9999;
      }
    ),
    criterion(
      "floor and archived sample remain unchanged and the archive stays hidden",
      async () => {
        const d = await state, f = d.objects.Floor, a = d.objects.ArchiveSample;
        return f.vertices === 4 && vectorClose(f.location, [0, 0, 0]) && vectorClose(f.scale, [6, 6, 1]) && vectorClose(f.dimensions, [12, 12, 0]) && vectorClose(f.rotation, [0, 0, 0]) && vectorClose(f.material?.color, [0.13, 0.15, 0.18, 1]) && a.vertices === 8 && vectorClose(a.location, [4, 3, 0.5]) && vectorClose(a.scale, [0.5, 0.5, 0.5]) && vectorClose(a.dimensions, [1, 1, 1]) && vectorClose(a.rotation, [0, 0, 0]) && a.hidden;
      }
    ),
    criterion(
      "existing lights are preserved without extra scene objects",
      async () => {
        const d = await state;
        return Object.keys(d.objects).sort().join(",") === [
          "Body",
          "Body_Coral",
          "Floor",
          "ArchiveSample",
          "Camera",
          "Key",
          "Fill"
        ].sort().join(",") && d.objects.Key.type === "LIGHT" && d.objects.Fill.type === "LIGHT" && d.objects.Key.light_kind === "AREA" && d.objects.Fill.light_kind === "AREA" && closeTo(d.objects.Key.energy, 1e3) && closeTo(d.objects.Fill.energy, 400) && vectorClose(d.objects.Key.location, [3, -4, 6]) && vectorClose(d.objects.Fill.location, [-4, -2, 3]);
      }
    ),
    criterion(
      "saved render settings use CPU Cycles, 16 samples and the full requested resolution",
      async () => {
        const d = await state;
        return d.render_engine === "CYCLES" && d.device === "CPU" && d.samples === 16 && vectorClose(d.resolution, [width, height, 100]);
      }
    ),
    criterion(
      "delivered PNG has the requested dimensions and nonblank rendered content",
      async () => {
        const path = resolve6(context.work, "product-hero.png");
        const probe = JSON.parse(
          (await execute(
            "ffprobe",
            [
              "-v",
              "error",
              "-show_entries",
              "stream=width,height",
              "-of",
              "json",
              path
            ],
            { timeout: 3e4 }
          )).stdout
        ).streams[0];
        const { stdout } = await execute(
          "ffmpeg",
          [
            "-loglevel",
            "error",
            "-i",
            path,
            "-vf",
            "scale=32:18",
            "-frames:v",
            "1",
            "-threads",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1"
          ],
          { encoding: "buffer", timeout: 3e4, maxBuffer: 4096 }
        );
        const values = [...stdout], mean = values.reduce((a, b) => a + b, 0) / values.length;
        return probe.width === width && probe.height === height && values.length === 576 && values.reduce((a, b) => a + (b - mean) ** 2, 0) / values.length > 25;
      }
    ),
    criterion("OBJ contains only the two product meshes", async () => {
      const obj = await readFile3(
        resolve6(context.work, "product-variants.obj"),
        "utf8"
      );
      const names2 = [...obj.matchAll(/^o (.+)$/gm)].map((m) => m[1]);
      return names2.length === 2 && names2.some((n) => /^Body(?:_|$)/.test(n)) && names2.some((n) => /^Body_Coral(?:_|$)/.test(n)) && !/Floor|ArchiveSample/.test(obj) && [...obj.matchAll(/^v /gm)].length === 16;
    })
  ]);
}
import { createHash } from "node:crypto";
import { writeFile as writeFile2 } from "node:fs/promises";
import { resolve as resolve8 } from "node:path";
import { readFile as readFile4 } from "node:fs/promises";
import { resolve as resolve7 } from "node:path";
import { randomBytes } from "node:crypto";
var attachment = "Reef 5.4 QA summary\nMechanical fit checks: 24 / 24 passed.\nNo new regressions.\nPilot target: 23 October 2026, after calibration sign-off.\n";
var seed = [
  {
    role: "ops",
    message: "Release 5.3 archive: calibration owner Alex, target 16 October. This is the previous release."
  },
  {
    role: "qa",
    message: "Release 5.4 QA: mechanical fit checks are 24 / 24 passed. No new regressions. The pilot target is 23 October 2026 after the remaining blocker is cleared."
  },
  {
    role: "ops",
    message: "Release 5.4 readiness checkpoint. Please use this thread for the customer-pilot handoff.",
    readiness: true
  },
  {
    role: "eng",
    message: "Initial estimate: camera calibration owner Alex, target 21 October. This is tentative; a confirmed update will follow.",
    reply: true
  },
  {
    role: "qa",
    message: "QA update: packaging and fit checks are complete. Camera calibration is the only remaining blocker for the pilot.",
    reply: true
  },
  {
    role: "eng",
    message: "Confirmed correction: Morgan owns camera calibration. Deadline: 23 October, 18:00 UTC. Ignore the earlier tentative owner and date.",
    reply: true
  },
  {
    role: "ops",
    message: "Training-room booking is handled separately by Taylor. It is not a release blocker."
  }
];
async function api(env, method, path, json, token = env.MATTERMOST_TOKEN) {
  const response = await fetch(env.MATTERMOST_URL + "/api/v4" + path, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json"
    },
    body: json !== void 0 ? JSON.stringify(json) : void 0,
    signal: AbortSignal.timeout(3e4)
  });
  if (!response.ok)
    throw new Error(
      `Demo Mattermost ${method} ${path}: HTTP ${response.status}`
    );
  return response.json();
}
async function seedReleaseDiscussion(work, env, profile) {
  const target = new URL(env.MATTERMOST_URL);
  if (!["127.0.0.1", "localhost"].includes(target.hostname) && !(env.REEF_HARBOR_SERVICE_FIXTURE === "1" && target.hostname === "mattermost"))
    throw new Error(
      "Synthetic workflow seeding requires the local demo Mattermost"
    );
  const teamId = env.REEF_DEMO_TEAM_ID;
  const channelName = env.REEF_TASK_CHANNEL_NAME ?? "reef-pilot-" + randomBytes(4).toString("hex");
  const channel = await api(env, "POST", "/channels", {
    team_id: teamId,
    name: channelName,
    display_name: channelName,
    type: "O"
  });
  const main = await api(env, "GET", "/users/me");
  const identities = {};
  for (const role of ["ops", "qa", "eng"]) {
    const username = "reef_" + role;
    let found = await fetch(
      env.MATTERMOST_URL + `/api/v4/users/username/${username}`,
      { headers: { Authorization: `Bearer ${env.MATTERMOST_TOKEN}` } }
    );
    let user;
    if (found.status === 404)
      user = await api(env, "POST", "/users", {
        username,
        email: `${username}@example.test`,
        password: "Reef-workflow-seed-2026!"
      });
    else {
      if (!found.ok) throw new Error("Cannot discover demo role user");
      user = await found.json();
    }
    const id = user.id;
    const membership = await fetch(
      env.MATTERMOST_URL + `/api/v4/teams/${teamId}/members/${id}`,
      { headers: { Authorization: `Bearer ${env.MATTERMOST_TOKEN}` } }
    );
    if (membership.status === 404)
      await api(env, "POST", `/teams/${teamId}/members`, {
        team_id: teamId,
        user_id: id
      });
    await api(env, "POST", `/channels/${channel.id}/members`, {
      user_id: id
    });
    const login = await fetch(env.MATTERMOST_URL + "/api/v4/users/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        login_id: username,
        password: "Reef-workflow-seed-2026!"
      })
    });
    if (!login.ok || !login.headers.get("Token"))
      throw new Error("Cannot authenticate demo role");
    identities[role] = { user: id, token: login.headers.get("Token") };
  }
  let root = "";
  const originals = [];
  for (const item of seed) {
    const post = await api(
      env,
      "POST",
      "/posts",
      {
        channel_id: channel.id,
        message: item.message,
        ...item.reply ? { root_id: root } : {}
      },
      identities[item.role].token
    );
    if (item.readiness) root = post.id;
    originals.push({ id: post.id, message: item.message });
  }
  let oldReply, decoyRoot;
  if (profile === "handoff-edit") {
    const form = new FormData();
    form.set("channel_id", channel.id);
    form.set(
      "files",
      new Blob([await readFile4(resolve7(work, "QA-summary.txt"))]),
      "QA-summary.txt"
    );
    const upload = await fetch(env.MATTERMOST_URL + "/api/v4/files", {
      method: "POST",
      headers: { Authorization: "Bearer " + env.MATTERMOST_TOKEN },
      body: form,
      signal: AbortSignal.timeout(3e4)
    });
    if (!upload.ok)
      throw Error("Fixture attachment upload: HTTP " + upload.status);
    const files = await upload.json();
    oldReply = await api(env, "POST", "/posts", {
      channel_id: channel.id,
      root_id: root,
      message: "Draft handoff: 20 / 24 checks passed. Calibration owner Alex, deadline 21 October. Ship immediately.",
      file_ids: files.file_infos.map((f) => f.id)
    });
  }
  if (profile === "ambiguous-thread") {
    const decoy = await api(
      env,
      "POST",
      "/posts",
      {
        channel_id: channel.id,
        message: "Release 5.4 readiness checkpoint - superseded draft. Do not use this thread; the operations checkpoint remains authoritative."
      },
      identities.qa.token
    );
    const reply = await api(
      env,
      "POST",
      "/posts",
      {
        channel_id: channel.id,
        root_id: decoy.id,
        message: "Obsolete draft: Alex, 21 October. This is not the confirmed handoff destination."
      },
      identities.eng.token
    );
    decoyRoot = decoy.id;
    originals.push(
      { id: decoy.id, message: decoy.message },
      { id: reply.id, message: reply.message }
    );
  }
  const team = await api(env, "GET", `/teams/${teamId}`);
  return {
    channel_id: channel.id,
    channel_name: channelName,
    root_id: root,
    actor_id: main.id,
    old_reply_id: oldReply?.id,
    old_file_ids: oldReply?.file_ids,
    decoy_root_id: decoyRoot,
    fixture_profile: profile ?? "standard",
    originals,
    startUrl: `${env.MATTERMOST_URL}/${team.name}/channels/${channelName}`
  };
}
var releaseHandoff = {
  id: "mattermost-release-handoff",
  application: "mattermost",
  title: "Read corrected release discussion and publish an accurate handoff",
  outputs: [],
  brief: `You are in the local task channel #\${CHANNEL_NAME}. Prepare the Reef 5.4 customer-pilot handoff.
Read the QA update and the readiness checkpoint's replies, including the confirmed correction. Distinguish the current release from the old 5.3 archive and tentative estimates.
Post exactly one concise reply inside the Release 5.4 readiness checkpoint thread. State the QA fit-check result, the only remaining blocker, its confirmed owner and deadline, and the pilot target/clearance condition.
Attach the provided QA-summary.txt from the working folder to that same reply. Acknowledge the readiness checkpoint root with a white_check_mark reaction.
Do not edit or delete anyone's original messages, post the handoff as a new top-level message, or create duplicate handoff replies.`,
  mcpGuidance: "Use the Mattermost tools against the assigned local task channel. Discover its channel ID, search/read the current release messages and thread corrections, upload QA-summary.txt, reply with root_id set to the checkpoint, and add the reaction. Do not use direct HTTP or generic desktop tools.",
  async prepare(work) {
    await writeFile2(resolve8(work, "QA-summary.txt"), attachment);
    await writeFile2(
      resolve8(work, "seed-discussion.json"),
      JSON.stringify(seed, null, 2) + "\n"
    );
    return {
      dataset: "release-54-corrected-handoff",
      attachment_sha256: createHash("sha256").update(attachment).digest("hex")
    };
  },
  prepareTrial: (work, env) => seedReleaseDiscussion(work, env),
  async verify(context) {
    const meta = context.metadata;
    const posts2 = async () => {
      const result = await api(
        context.env,
        "GET",
        `/channels/${meta.channel_id}/posts?per_page=200`
      );
      return Object.values(result.posts);
    };
    const replies = async () => (await posts2()).filter(
      (p) => p.user_id === meta.actor_id && p.root_id === meta.root_id
    );
    const body = async () => canonicalText((await replies()).map((p) => p.message).join(" "));
    return Promise.all([
      criterion(
        "exactly one handoff reply is in the readiness thread",
        async () => (await replies()).length === 1
      ),
      criterion(
        "handoff states the passing QA fit-check result",
        async () => /24\s*(?:\/|of|out of)\s*24/.test(await body()) && /pass/i.test(await body())
      ),
      criterion(
        "handoff uses confirmed calibration owner and deadline",
        async () => {
          const t = await body();
          return /calibrat/i.test(t) && /Morgan/.test(t) && /23 October|October 23|23 Oct/.test(t) && /18:00|6\s*p\.?m/i.test(t) && /UTC/.test(t) && !/Alex/.test(t);
        }
      ),
      criterion(
        "handoff distinguishes the only blocker and conditional pilot target",
        async () => {
          const t = await body();
          return /only|sole|remaining/i.test(t) && /blocker|blocking/i.test(t) && /pilot/i.test(t) && /23/.test(t) && /clear|sign.off|pending|after|condition/i.test(t);
        }
      ),
      criterion(
        "the provided QA file is attached to the handoff with matching bytes",
        async () => {
          const reply = (await replies())[0];
          if (!reply?.file_ids?.length) return false;
          for (const id of reply.file_ids) {
            const response = await fetch(
              context.env.MATTERMOST_URL + `/api/v4/files/${id}`,
              {
                headers: {
                  Authorization: `Bearer ${context.env.MATTERMOST_TOKEN}`
                }
              }
            );
            if (response.ok && createHash("sha256").update(Buffer.from(await response.arrayBuffer())).digest("hex") === meta.attachment_sha256)
              return true;
          }
          return false;
        }
      ),
      criterion(
        "checkpoint root has the actor's check-mark reaction",
        async () => {
          const reactions = await api(
            context.env,
            "GET",
            `/posts/${meta.root_id}/reactions`
          );
          return reactions.some(
            (r) => r.user_id === meta.actor_id && r.emoji_name === "white_check_mark"
          );
        }
      ),
      criterion(
        "original discussion is preserved and no top-level handoff is posted",
        async () => {
          const current = await posts2();
          return meta.originals.every(
            (p) => current.some((q) => q.id === p.id && q.message === p.message)
          ) && !current.some(
            (p) => p.user_id === meta.actor_id && !p.root_id && !p.type
          );
        }
      )
    ]);
  }
};
import { randomBytes as randomBytes2 } from "node:crypto";
async function grafanaAPI(env, method, path, body) {
  const authorization = env.GRAFANA_TOKEN ? "Bearer " + env.GRAFANA_TOKEN : "Basic " + Buffer.from(env.GRAFANA_USER + ":" + env.GRAFANA_PASSWORD).toString(
    "base64"
  );
  const response = await fetch(env.GRAFANA_URL + path, {
    method,
    headers: {
      Authorization: authorization,
      "Content-Type": "application/json"
    },
    body: body === void 0 ? void 0 : JSON.stringify(body),
    signal: AbortSignal.timeout(3e4)
  });
  if (!response.ok)
    throw new Error(
      "Grafana fixture " + method + " " + path + ": HTTP " + response.status
    );
  return await response.json();
}
function latencyPanel(uid) {
  const datasource = { uid, type: "grafana-testdata-datasource" };
  return {
    id: 4,
    type: "timeseries",
    title: "p95 request latency",
    gridPos: { x: 0, y: 8, w: 12, h: 8 },
    datasource,
    targets: [
      {
        refId: "A",
        datasource,
        scenarioId: "csv_metric_values",
        stringInput: "185,190,180,210,205,195",
        alias: "p95 latency"
      }
    ],
    fieldConfig: {
      defaults: {
        unit: "ms",
        thresholds: {
          mode: "absolute",
          steps: [
            { color: "green", value: null },
            { color: "red", value: 250 }
          ]
        }
      },
      overrides: []
    }
  };
}
var deploymentTime = Date.parse("2026-10-21T22:30:00Z");
async function seedPilotDashboard(env, profile) {
  const host = new URL(env.GRAFANA_URL).hostname;
  if (!["localhost", "127.0.0.1"].includes(host) && !(env.REEF_HARBOR_SERVICE_FIXTURE === "1" && host === "grafana"))
    throw new Error(
      "Synthetic seeding requires the local or isolated task Grafana"
    );
  const suffix = env.REEF_TASK_NAMESPACE ?? randomBytes2(4).toString("hex"), uid = "pilot-" + suffix, archiveUid = "archive-" + suffix, dataUid = "metrics-" + suffix;
  const datasource = { uid: dataUid, type: "grafana-testdata-datasource" };
  await grafanaAPI(env, "POST", "/api/datasources", {
    uid: dataUid,
    name: "Reef pilot metrics " + suffix,
    type: datasource.type,
    access: "proxy"
  });
  const dashboard = {
    uid,
    title: "Reef pilot operations / " + suffix,
    schemaVersion: 41,
    tags: ["reef", "production"],
    time: { from: "now-1h", to: "now" },
    refresh: "30s",
    panels: [
      {
        id: 1,
        type: "timeseries",
        title: "Request rate",
        gridPos: { x: 0, y: 0, w: 12, h: 8 },
        datasource,
        targets: [
          {
            refId: "A",
            datasource,
            scenarioId: "csv_metric_values",
            stringInput: "110,120,125,118,130,128"
          }
        ],
        fieldConfig: { defaults: { unit: "reqps" }, overrides: [] }
      },
      {
        id: 2,
        type: "stat",
        title: "Error rate",
        gridPos: { x: 12, y: 0, w: 6, h: 8 },
        datasource,
        targets: [
          {
            refId: "A",
            datasource,
            scenarioId: "csv_metric_values",
            stringInput: "0.4,0.7,0.5,0.6,0.8,0.7"
          }
        ],
        fieldConfig: {
          defaults: {
            unit: "none",
            thresholds: {
              mode: "absolute",
              steps: [
                { color: "green", value: null },
                { color: "red", value: 5 }
              ]
            }
          },
          overrides: []
        }
      },
      {
        id: 3,
        type: "text",
        title: "Runbook",
        gridPos: { x: 18, y: 0, w: 6, h: 8 },
        options: {
          mode: "markdown",
          content: "Reef pilot operations\n\nEscalation: Morgan\n\nRunbook: https://example.test/reef/runbook"
        }
      }
    ]
  };
  if (profile === "panel-collision") {
    dashboard.panels[0].id = 17;
    dashboard.panels[1].id = 23;
    dashboard.panels[2].id = 31;
    const latency = latencyPanel(dataUid);
    latency.id = 42;
    latency.targets[0].stringInput = "1,2,3";
    latency.fieldConfig.defaults.unit = "s";
    dashboard.panels = [
      dashboard.panels[2],
      {
        id: 4,
        type: "text",
        title: "Incident notes",
        gridPos: { x: 0, y: 16, w: 12, h: 4 },
        options: {
          mode: "markdown",
          content: "Incident owner: Sam. Preserve the incident audit notes."
        }
      },
      dashboard.panels[0],
      latency,
      dashboard.panels[1]
    ];
  }
  await grafanaAPI(env, "POST", "/api/dashboards/db", {
    dashboard,
    overwrite: false
  });
  let staleSnapshot2;
  if (profile === "stale-snapshot") {
    staleSnapshot2 = (await grafanaAPI(
      env,
      "GET",
      "/api/dashboards/uid/" + uid
    )).dashboard;
    const latest = structuredClone(staleSnapshot2);
    latest.panels.push({
      id: 19,
      type: "text",
      title: "Release approvals",
      gridPos: { x: 0, y: 16, w: 12, h: 4 },
      options: {
        mode: "markdown",
        content: "Approved by Morgan after the cached export. Do not remove this newer approval."
      }
    });
    await grafanaAPI(env, "POST", "/api/dashboards/db", {
      dashboard: latest,
      overwrite: false
    });
  }
  const archived = {
    ...dashboard,
    id: null,
    uid: archiveUid,
    title: "Reef pilot operations / archive / " + suffix,
    tags: ["reef", "archive"]
  };
  await grafanaAPI(env, "POST", "/api/dashboards/db", {
    dashboard: archived,
    overwrite: false
  });
  const archive2 = (await grafanaAPI(
    env,
    "GET",
    "/api/dashboards/uid/" + archiveUid
  )).dashboard;
  const saved = (await grafanaAPI(
    env,
    "GET",
    "/api/dashboards/uid/" + uid
  )).dashboard;
  return {
    dashboard_uid: uid,
    dashboard_title: dashboard.title,
    archive_uid: archiveUid,
    archive: archive2,
    data_uid: dataUid,
    original_panels: saved.panels,
    error_panel_id: saved.panels.find((p) => p.title === "Error rate").id,
    latency_panel_id: saved.panels.find(
      (p) => p.title === "p95 request latency"
    )?.id,
    stale_snapshot: staleSnapshot2,
    fixture_profile: profile ?? "standard",
    startUrl: env.GRAFANA_URL + "/d/" + uid,
    deploy_time_ms: deploymentTime
  };
}
var monitoringSamples = {
  latency: [185, 190, 180, 244, 205, 195],
  errors: [0.4, 0.7, 0.6, 1.8, 0.9, 1.3]
};
async function configureMonitoringSource(env, graph, discussion, samples) {
  const { dashboard } = await grafanaAPI(
    env,
    "GET",
    "/api/dashboards/uid/" + graph.dashboard_uid
  );
  dashboard.time = { from: "now-6h", to: "now" };
  dashboard.refresh = "15s";
  const error = dashboard.panels.find((p) => p.id === 2);
  error.targets[0].stringInput = samples.errors.join(",");
  error.fieldConfig.defaults = {
    unit: "percent",
    thresholds: {
      mode: "absolute",
      steps: [
        { color: "green", value: null },
        { color: "red", value: 2 }
      ]
    }
  };
  error.options = {
    reduceOptions: { values: false, calcs: ["lastNotNull"], fields: "" }
  };
  const stat = (id, title, calc, x) => ({
    id,
    title,
    type: "stat",
    gridPos: { x, y: 8, w: 12, h: 4 },
    datasource: {
      uid: String(graph.data_uid),
      type: "grafana-testdata-datasource"
    },
    targets: [
      {
        refId: "A",
        datasource: {
          uid: String(graph.data_uid),
          type: "grafana-testdata-datasource"
        },
        scenarioId: "csv_metric_values",
        stringInput: samples.latency.join(","),
        alias: "p95 latency"
      }
    ],
    fieldConfig: {
      defaults: {
        unit: "ms",
        thresholds: {
          mode: "absolute",
          steps: [
            { color: "green", value: null },
            { color: "red", value: 250 }
          ]
        }
      },
      overrides: []
    },
    options: {
      reduceOptions: { values: false, calcs: [calc], fields: "" },
      graphMode: "none"
    }
  });
  dashboard.panels.find((p) => p.id === 3).gridPos = {
    x: 0,
    y: 12,
    w: 24,
    h: 4
  };
  dashboard.panels.push(
    stat(4, "Latest p95 latency", "lastNotNull", 0),
    stat(5, "Peak p95 latency", "max", 12)
  );
  await grafanaAPI(env, "POST", "/api/dashboards/db", {
    dashboard,
    overwrite: false
  });
  const saved = (await grafanaAPI(
    env,
    "GET",
    "/api/dashboards/uid/" + graph.dashboard_uid
  )).dashboard;
  return {
    ...discussion,
    dashboard_title: graph.dashboard_title,
    grafana: { ...graph, protected_dashboard: saved },
    monitoring_samples: samples
  };
}
async function seedMonitoringWorkflow(work, env, samples) {
  const graph = await seedPilotDashboard(env), discussion = await seedReleaseDiscussion(work, env, "ambiguous-thread");
  return configureMonitoringSource(env, graph, discussion, samples);
}
function series(data) {
  for (const result of Object.values(data.results)) {
    if (result.error) throw Error(result.error);
    for (const f of result.frames ?? []) {
      const i = f.schema.fields.findIndex((v) => v.type === "number");
      if (i >= 0) {
        const values = f.data.values[i];
        if (values.length && values.every((v) => typeof v === "number" && Number.isFinite(v)))
          return values;
      }
    }
  }
  throw Error("No numeric monitoring series in native data frames");
}
async function currentReply(context) {
  const m = context.metadata, r = await api(
    context.env,
    "GET",
    "/channels/" + m.channel_id + "/posts?per_page=200"
  );
  const posts2 = Object.values(r.posts).filter(
    (p) => p.user_id === m.actor_id && !p.type
  );
  return {
    posts: posts2,
    body: posts2.filter((p) => p.root_id === m.root_id).map((p) => p.message).join(" ")
  };
}
var monitoringHandoff = {
  id: "monitoring-to-handoff",
  application: "mattermost",
  applications: ["grafana", "mattermost"],
  family: "cross-application",
  split: "development",
  views: [
    { application: "grafana", contextKey: "grafana" },
    { application: "mattermost" }
  ],
  title: "Ground a release handoff in current native monitoring readings",
  outputs: [],
  brief: releaseHandoff.brief + "\nGrafana is also open in another browser tab. Inspect the current production dashboard __DASHBOARD_TITLE__, not its archive. Keep the dashboard, queries and archive unchanged. Read the latest p95 latency, maximum p95 over the six-hour window, latest error rate, and their configured absolute red thresholds. Add these exact labels to the same handoff reply: Latest p95: <number> ms; Peak p95: <number> ms; Error rate: <number>%; Monitoring: within thresholds or breached. Use the window maximum p95 and latest error rate for that assessment. Include a link to the actual production dashboard. Obtain readings from the application rather than assuming values from an earlier report.",
  mcpGuidance: "Use the assigned grafana__ tools to discover/read the production model and query its real data frames, then mattermost__ tools to read corrected release discussion, upload QA evidence and post one grounded reply. Both services belong to this task. No direct HTTP, file APIs, scripts or other servers.",
  async prepare(work, env) {
    const m = await releaseHandoff.prepare(work, env);
    await writeFile3(
      resolve9(work, "monitoring-request.txt"),
      "Use the current production dashboard readings. Preserve the source and report latest/peak latency, latest error percentage and threshold status in the handoff.\n"
    );
    return { ...m, monitoring_samples: monitoringSamples };
  },
  prepareTrial: (work, env, metadata) => seedMonitoringWorkflow(
    work,
    env,
    metadata.monitoring_samples
  ),
  async verify(context) {
    const m = context.metadata, g = m.grafana, env = context.env;
    const dashboard = grafanaAPI(
      env,
      "GET",
      "/api/dashboards/uid/" + g.dashboard_uid
    ).then((r) => r.dashboard);
    const query = async (id) => {
      const panel = (await dashboard).panels.find((p) => p.id === id);
      if (!panel?.targets) throw Error("Missing source metric panel");
      return series(
        await grafanaAPI(env, "POST", "/api/ds/query", {
          queries: panel.targets,
          from: "now-6h",
          to: "now"
        })
      );
    };
    const latency = query(4), errors = query(2), reply = currentReply(context);
    const number = (body, label, unit) => Number(
      body.match(
        new RegExp(label + ":\\s*([0-9]+(?:\\.[0-9]+)?)\\s*" + unit, "i")
      )?.[1] ?? NaN
    );
    return [
      ...await releaseHandoff.verify(context),
      ...await Promise.all([
        criterion(
          "native monitoring frames match the frozen fixture readings",
          async () => {
            const samples = m.monitoring_samples;
            return isDeepStrictEqual(await latency, samples.latency) && isDeepStrictEqual(await errors, samples.errors);
          }
        ),
        criterion(
          "handoff latest and peak p95 match actual source data",
          async () => {
            const body = (await reply).body, values = await latency;
            return closeTo(number(body, "Latest p95", "ms"), values.at(-1)) && closeTo(number(body, "Peak p95", "ms"), Math.max(...values));
          }
        ),
        criterion(
          "handoff error percentage and threshold assessment match the source",
          async () => {
            const body = (await reply).body, latest = (await errors).at(-1), d = await dashboard;
            const threshold = (id) => d.panels.find((p) => p.id === id)?.fieldConfig?.defaults.thresholds?.steps.find(
              (s) => s.color === "red"
            )?.value;
            const limit = threshold(4), errorLimit = threshold(2);
            if (typeof limit !== "number" || typeof errorLimit !== "number")
              return false;
            const status = Math.max(...await latency) < limit && latest < errorLimit ? "within thresholds" : "breached";
            return closeTo(number(body, "Error rate", "%"), latest) && body.toLowerCase().includes("monitoring: " + status);
          }
        ),
        criterion(
          "handoff links to the actual production dashboard",
          async () => {
            const body = (await reply).body;
            return body.includes("/d/" + g.dashboard_uid) && !body.includes("/d/" + g.archive_uid);
          }
        ),
        criterion(
          "the production dashboard is unchanged",
          async () => isDeepStrictEqual(await dashboard, g.protected_dashboard)
        ),
        criterion(
          "the archive dashboard is unchanged",
          async () => isDeepStrictEqual(
            (await grafanaAPI(
              env,
              "GET",
              "/api/dashboards/uid/" + g.archive_uid
            )).dashboard,
            g.archive
          )
        ),
        criterion(
          "there is only one actor handoff and none in the superseded thread",
          async () => (await reply).posts.length === 1
        )
      ])
    ];
  }
};
var cadToScene = {
  ...cableSlot,
  id: "cad-to-scene",
  application: "freecad",
  applications: ["freecad", "blender"],
  family: "cross-application",
  split: "held-out",
  views: [
    { application: "blender", inputFile: "cad-hero.blend" },
    { application: "freecad", inputFile: "mounting-plate.FCStd" }
  ],
  title: "Transfer revised CAD geometry into an editable and consistent product render",
  outputs: [...cableSlot.outputs, "cad-hero.blend", "cad-hero.png"],
  brief: cableSlot.brief + "\nBlender's cad-hero.blend is also open; switch between the two real applications as needed. Import the delivered mounting-plate.stl into that existing scene as one mesh named CadPlate, converting millimeters to scene units with factor 0.02. Position the imported geometry so CAD point (0,0,0) maps to (-1,-0.6,0.15); the plate's final bounds must be (-1,-0.6,0.15) to (1,0.6,0.25). Retain the actual exported slot and holes; do not substitute a cube or reconstruct a different plate. Assign CadPlate sRGB teal #168B83, roughness 0.3, metallic 0.25. Keep Floor and both lights unchanged. Frame it using a perspective camera at (4,-6,5), looking at (0,0,0.2), 52mm. Save cad-hero.blend and deliver a fresh nonblank 640x360 cad-hero.png, CPU Cycles, 16 samples, 100% resolution. The PNG must depict the saved scene; inspect the render after saving.",
  mcpGuidance: "Use freecad__ tools for the linked plate revision and real STEP/STL exports. Use blender__ tools to import that workspace STL with explicit scaling, position/material the existing imported mesh, configure the camera and render. Both tools operate saved native files; the open GUI does not automatically refresh. No scripts or generic file APIs.",
  async prepare(work, env) {
    const m = await cableSlot.prepare(work, env), path = resolve9(work, "cad-hero.blend");
    await withApplication("blender", work, async (c) => {
      await call(c, "create_scene", { path, overwrite: true });
      await call(c, "add_mesh", { path, kind: "plane", name: "Floor" });
      await call(c, "set_transform", {
        path,
        object_name: "Floor",
        scale: [3, 3, 1]
      });
      await call(c, "assign_material", {
        path,
        object_name: "Floor",
        name: "FloorMaterial",
        color: [0.13, 0.15, 0.18, 1]
      });
      await call(c, "configure_camera", {
        path,
        location: [4, -6, 5],
        target: [0, 0, 0.2],
        lens_mm: 50
      });
      for (const [name, location, energy] of [
        ["Key", [3, -4, 6], 1e3],
        ["Fill", [-4, -2, 3], 400]
      ])
        await call(c, "add_light", { path, name, location, energy });
    });
    await new NativeEngine("blender").run(
      [
        "bpy.ops.wm.open_mainfile(filepath=p['path']); scene=bpy.context.scene",
        "scene.render.threads_mode='FIXED'; scene.render.threads=2",
        "bpy.ops.wm.save_as_mainfile(filepath=p['path']); result={'bounded':True}"
      ],
      { path }
    );
    await unlink3(resolve9(work, "cad-hero.blend1")).catch(
      (e) => {
        if (e.code !== "ENOENT") throw e;
      }
    );
    return {
      ...m,
      transfer_scale: 0.02,
      transfer_translation: [-1, -0.6, 0.15]
    };
  },
  async verify(context) {
    const cad = cadState(context.work), scene = sceneState(context.work, {
      projectFile: "cad-hero.blend",
      target: [0, 0, 0.2],
      geometry: true
    });
    return [
      ...await verifyPlateVariant("slot", context, cad),
      ...await Promise.all([
        criterion(
          "the scene contains only the imported plate and original staging objects",
          async () => Object.keys((await scene).objects).sort().join(",") === ["CadPlate", "Floor", "Camera", "Key", "Fill"].sort().join(",")
        ),
        criterion(
          "the imported visible mesh has the correct CAD unit conversion and placement",
          async () => {
            const o = (await scene).objects.CadPlate;
            if (o.type !== "MESH" || o.hidden || !o.world_vertices?.length)
              return false;
            return vectorClose(
              [0, 1, 2].map(
                (i) => Math.min(...o.world_vertices.map((p) => p[i]))
              ),
              [-1, -0.6, 0.15]
            ) && vectorClose(
              [0, 1, 2].map(
                (i) => Math.max(...o.world_vertices.map((p) => p[i]))
              ),
              [1, 0.6, 0.25]
            );
          }
        ),
        criterion(
          "the actual imported surface matches the delivered STL rather than a replacement primitive",
          async () => {
            const s = (await cad).stl, o = (await scene).objects.CadPlate;
            if (!s?.vertices || !o.world_vertices || !o.triangles) return false;
            const normalized = o.world_vertices.map((p) => [
              (p[0] + 1) / 0.02,
              (p[1] + 0.6) / 0.02,
              (p[2] - 0.15) / 0.02
            ]);
            return samePointCloud(s.vertices, normalized) && closeTo(
              triangleVolume(o.world_vertices, o.triangles),
              s.volume * 0.02 ** 3,
              1e-5
            );
          }
        ),
        criterion(
          "the imported plate has the specified native material",
          async () => {
            const m = (await scene).objects.CadPlate.material;
            return m && vectorClose(m.color, linearColor("#168B83"), 3e-3) && closeTo(m.roughness, 0.3) && closeTo(m.metallic, 0.25);
          }
        ),
        criterion(
          "the existing floor and lights remain unchanged",
          async () => {
            const o = (await scene).objects, f = o.Floor;
            return f.type === "MESH" && !f.hidden && f.vertices === 4 && vectorClose(f.location, [0, 0, 0]) && vectorClose(f.scale, [3, 3, 1]) && vectorClose(f.rotation, [0, 0, 0]) && vectorClose(f.material?.color, [0.13, 0.15, 0.18, 1]) && o.Key.light_kind === "AREA" && o.Fill.light_kind === "AREA" && closeTo(o.Key.energy, 1e3) && closeTo(o.Fill.energy, 400) && vectorClose(o.Key.location, [3, -4, 6]) && vectorClose(o.Fill.location, [-4, -2, 3]);
          }
        ),
        criterion(
          "the perspective camera uses the requested framing",
          async () => {
            const c = (await scene).camera;
            return c.projection === "PERSP" && closeTo(c.lens, 52) && vectorClose(c.location, [4, -6, 5]) && c.aim > 0.9999;
          }
        ),
        criterion(
          "the saved scene uses the bounded CPU render settings",
          async () => {
            const s = await scene;
            return s.render_engine === "CYCLES" && s.device === "CPU" && s.samples === 16 && vectorClose(s.resolution, [640, 360, 100]);
          }
        ),
        criterion(
          "the delivered PNG has the required nonblank content and size",
          () => pngEvidence(resolve9(context.work, "cad-hero.png"), 640, 360)
        ),
        criterion(
          "the delivered PNG matches an independent render of the saved scene",
          async () => pngMatchesSavedScene(
            context.work,
            "cad-hero.blend",
            "cad-hero.png",
            await scene
          )
        )
      ])
    ];
  }
};
import { resolve as resolve10 } from "node:path";
var footer = "Internal use only - Reef partner operations.";
var profiles = [
  {
    id: "wordcraft-scoped-amendment",
    title: "Reef Decision Addendum",
    heading: "Current release",
    initialHeading: "Current release",
    draft: [
      "Reef 5.4: draft decision.",
      "Camera calibration - owner TBC",
      "Approval: pending"
    ],
    final: [
      "Reef 5.4: conditional pilot approved.",
      "Camera calibration - owner Morgan - due 23 October",
      "Approval: Morgan after calibration sign-off."
    ],
    archive: [
      "Reef 5.3: closed on 16 October.",
      "Camera calibration - owner TBC",
      "Approval: Alex on 16 October."
    ],
    changes: [
      ["Reef 5.4: draft decision.", "Reef 5.4: conditional pilot approved."],
      [
        "Camera calibration - owner TBC",
        "Camera calibration - owner Morgan - due 23 October"
      ],
      ["Approval: pending", "Approval: Morgan after calibration sign-off."]
    ],
    brief: "The current release and Historical archive contain a repeated calibration line. Amend only Current release: set the decision to Reef 5.4: conditional pilot approved.; the owner line to Camera calibration - owner Morgan - due 23 October; and the approval to Approval: Morgan after calibration sign-off. Keep the three archive paragraphs unchanged, including its older owner TBC line. Do not use a global replacement across both sections.",
    split: "development"
  },
  {
    id: "wordcraft-action-register",
    title: "Reef Action Register",
    heading: "Handoff actions",
    initialHeading: "Approved actions",
    draft: [
      "Draft slot A: send the pilot pack.",
      "Draft slot B: ship before calibration.",
      "Draft slot C: confirm the training slot.",
      "Duplicate draft: send the pilot pack.",
      "Withdrawn draft: ship before calibration."
    ],
    final: [
      "1. Publish QA results.",
      "2. Send the pilot pack after calibration sign-off.",
      "3. Confirm the training slot after customer approval."
    ],
    archive: [
      "Historical example: ship before calibration.",
      "Keep reference REEF-2026-17."
    ],
    changes: [
      ["Approved actions", "Handoff actions"],
      ["Draft slot A: send the pilot pack.", "1. Publish QA results."],
      [
        "Draft slot B: ship before calibration.",
        "2. Send the pilot pack after calibration sign-off."
      ],
      [
        "Draft slot C: confirm the training slot.",
        "3. Confirm the training slot after customer approval."
      ],
      ["Duplicate draft: send the pilot pack.", ""],
      ["Withdrawn draft: ship before calibration.", ""]
    ],
    brief: "Reconcile Approved actions into a Handoff actions section with exactly three separate numbered actions, in this order: 1. Publish QA results.; 2. Send the pilot pack after calibration sign-off.; 3. Confirm the training slot after customer approval. Remove the duplicate and withdrawn drafts. The Historical archive intentionally contains an old shipping example: preserve it and reference REEF-2026-17 unchanged. Do not retain draft labels in the active action list.",
    split: "held-out"
  }
];
function styled(p, size) {
  if (!p) return false;
  const runs = array(p["w:r"]).filter((r) => r["w:t"]);
  return runs.length > 0 && runs.every((r) => {
    const q = r["w:rPr"];
    return q && enabledProperty(q["w:b"]) && String(q["w:sz"]?.["@w:val"]) === String(size * 2) && String(q["w:color"]?.["@w:val"]).toUpperCase() === "1F4E78";
  });
}
function make(p) {
  return {
    id: p.id,
    application: "wordcraft",
    family: "wordcraft-weekly-report",
    split: p.split,
    title: p.title,
    inputFile: "amendment.docx",
    outputs: ["amendment.docx", "amendment.pdf"],
    brief: "The existing amendment.docx is open. " + p.brief + " Keep the original document title, formatted bold 18 pt, centered, dark blue #1F4E78. Make the active section heading bold 16 pt dark blue #1F4E78. Preserve Historical archive and the final internal-use footer. Save amendment.docx in place and deliver amendment.pdf with the same final content. Inspect the visible document.",
    mcpGuidance: "Use the live WordCraft document. Inspect text and choose occurrences by section; select_text accepts a 1-based occurrence. Preserve archive paragraphs, revise active paragraphs, format the title/heading, save DOCX and export PDF. No generic file or script APIs.",
    async prepare(work) {
      const original3 = [
        p.title,
        p.initialHeading,
        ...p.draft,
        "Historical archive",
        ...p.archive,
        footer
      ];
      await withApplication("wordcraft", work, async (c) => {
        await call(c, "new_document", { template: "blank" });
        await call(c, "type_text", {
          text: original3.join("\n"),
          paragraphs: true
        });
        await call(c, "save_document", {
          path: resolve10(work, "amendment.docx")
        });
      });
      return {
        fixture_profile: p.id,
        original: original3,
        required_active: p.final,
        protected_archive: p.archive
      };
    },
    async verify(context) {
      const read2 = docx(resolve10(context.work, "amendment.docx")).then((d) => ({
        ...d,
        text: d.numberedText.map(canonicalText)
      }));
      const active = async () => {
        const d = await read2, a = d.text.indexOf(p.heading), b = d.text.indexOf("Historical archive");
        return a >= 0 && b > a ? d.text.slice(a + 1, b).filter(Boolean) : [];
      };
      return Promise.all([
        criterion(
          "the original document title remains exactly once",
          async () => (await read2).text.filter((t) => t === p.title).length === 1
        ),
        criterion(
          "the active section contains exactly the approved paragraphs in order",
          async () => JSON.stringify(await active()) === JSON.stringify(p.final)
        ),
        criterion(
          "the historical archive remains unchanged and in order",
          async () => {
            const d = await read2, a = d.text.indexOf("Historical archive"), b = d.text.indexOf(footer);
            return a >= 0 && b > a && JSON.stringify(d.text.slice(a + 1, b).filter(Boolean)) === JSON.stringify(p.archive);
          }
        ),
        criterion(
          "section order and the final internal-use footer are preserved",
          async () => {
            const d = (await read2).text.filter(Boolean);
            return d[0] === p.title && d.indexOf(p.heading) > 0 && d.indexOf("Historical archive") > d.indexOf(p.heading) && d.at(-1) === footer;
          }
        ),
        criterion("the title is bold 18 pt dark blue", async () => {
          const d = await read2;
          return styled(d.paragraphs[d.text.indexOf(p.title)], 18);
        }),
        criterion("the title is centered", async () => {
          const d = await read2, paragraph = d.paragraphs[d.text.indexOf(p.title)];
          return paragraph?.["w:pPr"]?.["w:jc"]?.["@w:val"] === "center";
        }),
        criterion("the active heading is bold 16 pt dark blue", async () => {
          const d = await read2;
          return styled(d.paragraphs[d.text.indexOf(p.heading)], 16);
        }),
        criterion(
          "the PDF contains the approved active and preserved archive content",
          async () => {
            const text = canonicalText(
              await pdfText(resolve10(context.work, "amendment.pdf"))
            );
            return [
              p.title,
              p.heading,
              ...p.final,
              "Historical archive",
              ...p.archive,
              footer
            ].every((t) => text.includes(t));
          }
        )
      ]);
    }
  };
}
var scopedAmendment = make(profiles[0]);
var actionRegister = make(profiles[1]);
import { resolve as resolve12 } from "node:path";
import { readFile as readFile6 } from "node:fs/promises";
import { resolve as resolve11, posix } from "node:path";
import { unzipSync as unzipSync2, strFromU8 as strFromU82 } from "fflate";
import { XMLParser as XMLParser2 } from "fast-xml-parser";
var footer2 = "Reef partner operations | Internal";
var finalText = [
  "Reef 5.4 - Partner Pilot",
  "Pilot date: 23 October 2026",
  "Fit checks: 24 / 24 passed",
  "Drawing approvals: 12 / 12 complete",
  "Pilot readiness: 98%",
  "Owner: Morgan",
  "Deadline: 23 October",
  "Next steps",
  "Send the pilot pack to Customer Success.",
  "Confirm the partner training slot by Friday."
];
async function slides(path) {
  const zip = unzipSync2(await readFile6(path));
  const parser = new XMLParser2({ ignoreAttributes: false });
  const model = parser.parse(strFromU82(zip["ppt/presentation.xml"]))["p:presentation"];
  const rels = array(
    parser.parse(strFromU82(zip["ppt/_rels/presentation.xml.rels"]))["Relationships"]["Relationship"]
  );
  const ids = array(model["p:sldIdLst"]?.["p:sldId"]);
  return ids.map((id) => {
    const target = rels.find((r) => r["@_Id"] === id["@_r:id"])?.["@_Target"];
    if (!target) throw new Error("Missing slide relationship: " + id["@_r:id"]);
    const name = posix.normalize(
      target.startsWith("/") ? target.slice(1) : posix.join("ppt", target)
    );
    if (!name.startsWith("ppt/slides/") || !zip[name])
      throw new Error("Invalid slide target: " + target);
    const raw = strFromU82(zip[name]);
    return { raw, xml: parser.parse(raw) };
  });
}
function texts(raw) {
  const parsed = new XMLParser2({
    preserveOrder: true,
    ignoreAttributes: true
  }).parse(raw);
  const found = [];
  const walk = (v) => {
    if (Array.isArray(v)) {
      v.forEach(walk);
      return;
    }
    if (!v || typeof v !== "object") return;
    for (const [k, value] of Object.entries(v)) {
      if (k === "a:t")
        found.push(
          String(value[0]?.["#text"] ?? "")
        );
      else walk(value);
    }
  };
  walk(parsed);
  return found;
}
var partnerDeck = {
  id: "deckcraft-partner-update",
  application: "deckcraft",
  title: "Refresh a branded partner-pilot presentation",
  inputFile: "partner-update.pptx",
  outputs: ["partner-update.pptx", "partner-update.pdf"],
  brief: `The existing three-slide partner-update.pptx is open. Update it for the Reef 5.4 partner pilot.
On slide 1 change the title to Reef 5.4 - Partner Pilot and the date to Pilot date: 23 October 2026.
On slide 2 replace the three validation metrics with separate lines: Fit checks: 24 / 24 passed, Drawing approvals: 12 / 12 complete, and Pilot readiness: 98%.
On slide 3 keep its Open risk title and Camera calibration description, but set Owner: Morgan and Deadline: 23 October.
Duplicate that branded risk slide as the fourth slide. Rename its title Next steps and replace its body with two lines:
Send the pilot pack to Customer Success. and Confirm the partner training slot by Friday.
Preserve the dark-blue 36 pt title styling and the Reef partner operations | Internal footer on all four slides.
Keep all three original slides in their existing order. Save the PPTX in place and export all four slides to partner-update.pdf.
Review the slides so the text remains readable and no outdated 87% metric remains.`,
  mcpGuidance: "The presentation is already open in the real DeckCraft desktop. Inspect slides and their IDs before editing. Native list_commands, run_command, set_text, slide duplication and export tools are available. Preserve existing branding and slides rather than building a new deck.",
  async prepare(work) {
    await withApplication("deckcraft", work, async (client) => {
      await call(client, "new_presentation", { blank: true });
      const contents = [
        ["Reef 5.3 - Internal Update", "Pilot date: 16 October 2026"],
        [
          "Validation summary",
          "Fit checks: 20 / 24 passed\nDrawing approvals: 9 / 12 complete\nPilot readiness: 87%"
        ],
        ["Open risk", "Camera calibration\nOwner: TBC\nDeadline: to confirm"]
      ];
      for (const [title, body] of contents) {
        await call(client, "add_slide", { layout: "blank" });
        await call(client, "add_text_box", {
          x: 60,
          y: 40,
          w: 840,
          h: 90,
          text: title,
          size: 36,
          color: "#1F4E78"
        });
        await call(client, "add_text_box", {
          x: 80,
          y: 160,
          w: 800,
          h: 240,
          text: body,
          size: 24,
          color: "#333333"
        });
        await call(client, "add_text_box", {
          x: 40,
          y: 500,
          w: 800,
          h: 24,
          text: footer2,
          size: 12,
          color: "#777777"
        });
      }
      await call(client, "save_presentation", {
        path: resolve11(work, "partner-update.pptx")
      });
    });
    return { footer: footer2, finalText };
  },
  async verify(context) {
    const read2 = () => slides(resolve11(context.work, "partner-update.pptx"));
    return Promise.all([
      criterion(
        "four slides preserve original order and add Next steps last",
        async () => {
          const d = await read2();
          return d.length === 4 && texts(d[0].raw).includes(finalText[0]) && texts(d[1].raw).includes("Validation summary") && texts(d[2].raw).includes("Open risk") && texts(d[3].raw).includes("Next steps");
        }
      ),
      criterion("pilot title and date updated", async () => {
        const d = await read2();
        return [finalText[0], finalText[1]].every(
          (t) => texts(d[0].raw).includes(t)
        );
      }),
      criterion(
        "validation metrics all current and stale readiness removed",
        async () => {
          const d = await read2();
          const t = texts(d[1].raw);
          return finalText.slice(2, 5).every((s) => t.includes(s)) && !d.some((s) => texts(s.raw).some((v) => v.includes("87%")));
        }
      ),
      criterion(
        "risk description retained with owner and deadline",
        async () => {
          const d = await read2(), t = texts(d[2].raw);
          return ["Camera calibration", finalText[5], finalText[6]].every(
            (s) => t.includes(s)
          );
        }
      ),
      criterion("new final slide contains both handoff actions", async () => {
        const d = await read2(), t = texts(d[3].raw);
        return finalText.slice(7).every((s) => t.includes(s)) && !t.includes("Owner: TBC");
      }),
      criterion(
        "all slides preserve the branded footer",
        async () => (await read2()).every((s) => texts(s.raw).includes(footer2))
      ),
      criterion(
        "dark-blue 36 pt title styling retained",
        async () => (await read2()).every(
          (s) => /sz="3600"/.test(s.raw) && /(?:srgbClr|color)[^>]*val="1[fF]4[eE]78"/.test(s.raw)
        )
      ),
      criterion(
        "PDF has four pages",
        async () => /Pages:\s+4\b/.test(
          (await execute(
            "pdfinfo",
            [resolve11(context.work, "partner-update.pdf")],
            { timeout: 3e4 }
          )).stdout
        )
      ),
      criterion("PDF contains all final briefing content", async () => {
        const t = (await pdfText(resolve11(context.work, "partner-update.pdf"))).replace(/\s+/g, " ");
        return finalText.every((s) => t.includes(s)) && !t.includes("87%");
      })
    ]);
  }
};
var footer3 = "Reef partner operations | Internal";
var intro = [
  "Reef 5.4 pilot briefing",
  "Pilot target: 23 October 2026"
];
var validation = [
  "Validation summary",
  "Fit checks: 24 / 24 passed\nDrawing approvals: 12 / 12 complete"
];
var risk = [
  "Open risk",
  "Camera calibration\nOwner: Morgan\nDeadline: 23 October, 18:00 UTC"
];
var next = [
  "Next steps",
  "Publish QA results.\nRelease only after calibration sign-off."
];
var archive = [
  "Validation summary",
  "Release 5.3 archive\nPilot readiness: 87%\nOwner: Alex"
];
var agenda = ["Obsolete agenda", "Draft: ship before calibration."];
var profiles2 = [
  {
    id: "deckcraft-reorder-and-retire",
    split: "development",
    initial: [intro, agenda, validation, risk],
    final: [intro, risk, validation, next],
    brief: "Retire the Obsolete agenda slide. Move Open risk before Validation summary, preserving both slides' content. Duplicate the validation slide as the final Next steps slide and replace its body with Publish QA results. and Release only after calibration sign-off. Keep the introduction first. The final order must be introduction, Open risk, Validation summary, Next steps."
  },
  {
    id: "deckcraft-current-and-archive",
    split: "held-out",
    initial: [
      intro,
      [
        "Validation summary",
        "Release 5.4 current\nFit checks: 20 / 24 passed\nOwner: TBC"
      ],
      archive
    ],
    final: [
      intro,
      [
        "Validation summary",
        "Release 5.4 current\nFit checks: 24 / 24 passed\nOwner: Morgan"
      ],
      next,
      archive
    ],
    brief: "Two slides share the title Validation summary. Update only the Release 5.4 current slide: Fit checks: 24 / 24 passed and Owner: Morgan. Preserve its release label. Keep the Release 5.3 archive slide, its 87% metric and Alex owner unchanged. Duplicate the current slide as Next steps, place it immediately before the archive, and replace its body with Publish QA results. and Release only after calibration sign-off. The archive must remain last."
  }
];
async function create(c, work, contents) {
  await call(c, "new_presentation", { blank: true });
  for (const [title, body] of contents) {
    await call(c, "add_slide", { layout: "blank" });
    for (const p of [
      { x: 60, y: 40, w: 840, h: 90, text: title, size: 36, color: "#1F4E78" },
      { x: 80, y: 160, w: 800, h: 240, text: body, size: 24, color: "#333333" },
      {
        x: 40,
        y: 500,
        w: 800,
        h: 24,
        text: footer3,
        size: 12,
        color: "#777777"
      }
    ])
      await call(c, "add_text_box", p);
  }
  await call(c, "save_presentation", { path: resolve12(work, "briefing.pptx") });
}
function make2(p) {
  return {
    id: p.id,
    application: "deckcraft",
    family: "deckcraft-partner-update",
    split: p.split,
    title: p.brief.split(".")[0],
    inputFile: "briefing.pptx",
    outputs: ["briefing.pptx", "briefing.pdf"],
    brief: "The existing briefing.pptx is open. " + p.brief + " Do not replace unrelated slide content. Retain dark-blue #1F4E78 36 pt titles and the original internal footer on every slide. Save the native PPTX and export all four slides to briefing.pdf. Review ordering and readable layout.",
    mcpGuidance: "Inspect slide content and IDs in the live DeckCraft session. Discover slide.delete, slide.move and slide.duplicate parameter summaries, edit the selected slide's shape text, preserve branding, save PPTX and export all slides. No file APIs or scripts.",
    prepare: async (work) => {
      await withApplication(
        "deckcraft",
        work,
        (c) => create(c, work, p.initial)
      );
      return { fixture_profile: p.id, original_slides: p.initial };
    },
    async verify(context) {
      const read2 = slides(resolve12(context.work, "briefing.pptx"));
      return Promise.all([
        criterion(
          "four slides occur in the requested presentation order",
          async () => {
            const s = await read2;
            return s.length === 4 && p.final.every((v, i) => texts(s[i].raw)[0] === v[0]);
          }
        ),
        criterion(
          "each slide contains exactly its required body and preserved footer",
          async () => {
            const s = await read2;
            return s.length === 4 && p.final.every(
              (v, i) => canonicalText(texts(s[i].raw).join(" ")) === canonicalText([v[0], v[1], footer3].join(" "))
            );
          }
        ),
        criterion(
          "the unchanged introduction is first",
          async () => canonicalText(texts((await read2)[0].raw).join(" ")) === canonicalText([...intro, footer3].join(" "))
        ),
        criterion(
          "the historical archive or approved risk content is preserved in the right slot",
          async () => {
            const index = p.id === "deckcraft-current-and-archive" ? 3 : 1, expected = p.id === "deckcraft-current-and-archive" ? archive : risk;
            return canonicalText(texts((await read2)[index].raw).join(" ")) === canonicalText([...expected, footer3].join(" "));
          }
        ),
        criterion(
          "titles keep the original 36 pt dark-blue branding",
          async () => (await read2).every(
            (s) => /sz="3600"/.test(s.raw) && /srgbClr val="1F4E78"/i.test(s.raw)
          )
        ),
        criterion(
          "each slide retains its internal footer",
          async () => (await read2).length === 4 && (await read2).every((s) => texts(s.raw).includes(footer3))
        ),
        criterion(
          "the exported PDF has exactly four pages",
          async () => /Pages:\s+4\b/.test(
            (await execute(
              "pdfinfo",
              [resolve12(context.work, "briefing.pdf")],
              { timeout: 3e4 }
            )).stdout
          )
        ),
        criterion("the PDF contains every requested slide fact", async () => {
          const text = canonicalText(
            await pdfText(resolve12(context.work, "briefing.pdf"))
          );
          return p.final.flatMap((v) => [v[0], ...v[1].split("\n")]).every((t) => text.includes(t));
        })
      ]);
    }
  };
}
var reorderAndRetire = make2(profiles2[0]);
var currentAndArchive = make2(profiles2[1]);
import { isDeepStrictEqual as isDeepStrictEqual2 } from "node:util";
import { resolve as resolve14 } from "node:path";
import { readFile as readFile7 } from "node:fs/promises";
import { resolve as resolve13 } from "node:path";
import { unzipSync as unzipSync3, strFromU8 as strFromU83 } from "fflate";
async function manifest(path) {
  const archive2 = unzipSync3(await readFile7(path));
  return JSON.parse(strFromU83(archive2["manifest.json"]));
}
async function dimensions(path) {
  const probe = await execute(
    "ffprobe",
    [
      "-v",
      "error",
      "-select_streams",
      "v:0",
      "-show_entries",
      "stream=width,height",
      "-of",
      "json",
      path
    ],
    { timeout: 3e4 }
  );
  return JSON.parse(probe.stdout).streams[0];
}
var campaignBanner = {
  id: "photocraft-pilot-banner",
  application: "photocraft",
  title: "Adapt an editable campaign banner for a partner pilot",
  inputFile: "pilot-banner.pcraft",
  outputs: ["pilot-banner.pcraft", "pilot-banner.png", "pilot-banner.jpg"],
  brief: `The existing editable pilot-banner.pcraft is open. Prepare the newsletter banner for the partner pilot.
Resize the image from 960 x 540 to 1200 x 675 pixels, keeping the existing composition proportional.
Change the Headline type layer from Launch preview to Partner pilot opens.
Change the Subtitle type layer from Save the date to Reef 5.4 - 23 October 2026.
Change the Call to action type layer from Invite coming soon to Reserve your pilot slot.
Preserve the separate editable type layers, the REEF brand at the top and the dark-blue background.
Keep the text readable and within the canvas. Save the editable master in place and export both pilot-banner.png and pilot-banner.jpg in the same folder.
Inspect the final banner before finishing.`,
  mcpGuidance: "The editable banner is already open in the real PhotoCraft desktop. Inspect layers and discover native command parameters before editing. Preserve the existing layers and use native image/type commands, preview and export tools. Do not flatten the master or create a replacement document.",
  async prepare(work) {
    await withApplication("photocraft", work, async (client) => {
      await call(client, "doc_new", {
        name: "Reef pilot campaign",
        width: 960,
        height: 540,
        background: "#123047"
      });
      for (const params of [
        {
          name: "Brand",
          text: "REEF",
          x: 40,
          y: 70,
          size: 28,
          color: "#67E8F9"
        },
        {
          name: "Headline",
          text: "Launch preview",
          x: 60,
          y: 230,
          size: 54,
          color: "#FFFFFF"
        },
        {
          name: "Subtitle",
          text: "Save the date",
          x: 60,
          y: 330,
          size: 28,
          color: "#D6E4F0"
        },
        {
          name: "Call to action",
          text: "Invite coming soon",
          x: 60,
          y: 440,
          size: 24,
          color: "#67E8F9"
        }
      ])
        await call(client, "command_run", { id: "type.create", params });
      await call(client, "doc_save", {
        path: resolve13(work, "pilot-banner.pcraft")
      });
      await call(client, "doc_export", {
        path: resolve13(work, "reference.png")
      });
    });
    return {
      width: 1200,
      height: 675,
      expectedText: [
        "Partner pilot opens",
        "Reef 5.4 - 23 October 2026",
        "Reserve your pilot slot"
      ]
    };
  },
  async verify(context) {
    const read2 = () => manifest(resolve13(context.work, "pilot-banner.pcraft"));
    return Promise.all([
      criterion("editable master has the requested dimensions", async () => {
        const d = (await read2()).document;
        return d.size.width === 1200 && d.size.height === 675;
      }),
      criterion("headline is updated in its editable layer", async () => {
        const d = (await read2()).document;
        const layer = d.layers.find(
          (l) => l.name === "Headline"
        );
        return layer?.content?.kind === "text" && JSON.stringify(layer.content).includes("Partner pilot opens") && !JSON.stringify(layer.content).includes("Launch preview");
      }),
      criterion("subtitle is updated in its editable layer", async () => {
        const d = (await read2()).document;
        const layer = d.layers.find(
          (l) => l.name === "Subtitle"
        );
        return layer?.content?.kind === "text" && JSON.stringify(layer.content).includes("Reef 5.4 - 23 October 2026");
      }),
      criterion("call to action is updated in its editable layer", async () => {
        const d = (await read2()).document;
        const layer = d.layers.find(
          (l) => l.name === "Call to action"
        );
        return layer?.content?.kind === "text" && JSON.stringify(layer.content).includes("Reserve your pilot slot");
      }),
      criterion("brand and separate editable layers are retained", async () => {
        const d = (await read2()).document;
        const brand = d.layers.find(
          (l) => l.name === "Brand"
        );
        return d.layers.filter(
          (l) => l.content.kind === "text"
        ).length >= 4 && JSON.stringify(brand).includes("REEF");
      }),
      criterion("PNG has the requested size", async () => {
        const d = await dimensions(resolve13(context.work, "pilot-banner.png"));
        return d.width === 1200 && d.height === 675;
      }),
      criterion("JPEG has the requested size", async () => {
        const d = await dimensions(resolve13(context.work, "pilot-banner.jpg"));
        return d.width === 1200 && d.height === 675;
      }),
      criterion("export keeps the original dark-blue background", async () => {
        const { stdout } = await execute(
          "ffmpeg",
          [
            "-loglevel",
            "error",
            "-i",
            resolve13(context.work, "pilot-banner.png"),
            "-vf",
            "crop=1:1:10:10",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1"
          ],
          { encoding: "buffer", timeout: 3e4, maxBuffer: 1024 }
        );
        return [...stdout.subarray(0, 3)].every(
          (v, i) => Math.abs(v - [18, 48, 71][i]) <= 1
        );
      })
    ]);
  }
};
var read = async (work) => (await manifest(resolve14(work, "pilot-banner.pcraft"))).document;
var signature = (l) => ({
  id: l.id,
  name: l.name,
  visible: l.visible,
  kind: l.content.kind,
  text: l.content.text,
  size: l.content.size_pt,
  color: l.content.color,
  transform: l.content.transform
});
async function backgroundBand(work, crop) {
  const { stdout } = await execute(
    "ffmpeg",
    [
      "-loglevel",
      "error",
      "-i",
      resolve14(work, "pilot-banner.png"),
      "-vf",
      crop + ",scale=80:8",
      "-frames:v",
      "1",
      "-threads",
      "1",
      "-f",
      "rawvideo",
      "-pix_fmt",
      "rgb24",
      "pipe:1"
    ],
    { encoding: "buffer", maxBuffer: 4096, timeout: 3e4 }
  );
  return stdout.length === 1920 && [...stdout].every((v, i) => Math.abs(v - [18, 48, 71][i % 3]) <= 2);
}
var proofVisibility = {
  ...campaignBanner,
  id: "photocraft-proof-visibility",
  family: campaignBanner.id,
  split: "development",
  title: "Hide a retained proof layer before exporting the public campaign artwork",
  brief: campaignBanner.brief + "\nThe extra editable Proof layer contains INTERNAL PROOF. Keep that layer and its text, but hide it in the native master and both public exports. Do not delete it, merge layers or flatten the master.",
  async prepare(work, env) {
    const metadata = await campaignBanner.prepare(work, env);
    await withApplication("photocraft", work, async (c) => {
      await call(c, "doc_open", { path: resolve14(work, "pilot-banner.pcraft") });
      await call(c, "command_run", {
        id: "type.create",
        params: {
          name: "Proof",
          text: "INTERNAL PROOF",
          x: 60,
          y: 140,
          size: 40,
          color: "#FF0055"
        }
      });
      await call(c, "doc_save", { path: resolve14(work, "pilot-banner.pcraft") });
    });
    return {
      ...metadata,
      original_layer_ids: (await read(work)).layers.map((l) => l.id)
    };
  },
  async verify(context) {
    return [
      ...await campaignBanner.verify(context),
      await criterion(
        "the editable proof layer is retained but hidden",
        async () => {
          const l = (await read(context.work)).layers.find(
            (l2) => l2.name === "Proof"
          );
          return l?.content.kind === "text" && l.content.text === "INTERNAL PROOF" && !l.visible;
        }
      ),
      await criterion(
        "all original layer identities remain without extra copies",
        async () => JSON.stringify(
          (await read(context.work)).layers.map((l) => l.id).sort((a, b) => a - b)
        ) === JSON.stringify(
          context.metadata.original_layer_ids.slice().sort((a, b) => a - b)
        )
      ),
      await criterion(
        "the proof lettering is absent from the delivered PNG",
        () => backgroundBand(context.work, "crop=800:80:60:110")
      )
    ];
  }
};
var canvasExtension = {
  ...campaignBanner,
  id: "photocraft-canvas-extension",
  family: campaignBanner.id,
  split: "held-out",
  title: "Extend a canvas without resampling its existing editable artwork",
  brief: [
    "Extend the existing pilot-banner.pcraft from 960 x 540 to 960 x 720 pixels by adding canvas at the bottom. Anchor the original artwork at the top-left; do not stretch, rescale or reposition its existing text layers.",
    "Preserve the existing Brand, Headline, Subtitle and Call to action text, styles, transforms and visibility. Extend the dark-blue #123047 background into the new area.",
    "Add one separate editable text layer named Support caption: Pilot support: support@reef.example. Use 24 pt cyan #67E8F9 with its point-text baseline at (60,650), with no scale or skew.",
    "Save the editable master in place and deliver pilot-banner.png and pilot-banner.jpg at 960 x 720. Keep all existing layers and review the final composition."
  ].join("\n"),
  async prepare(work, env) {
    const m = await campaignBanner.prepare(work, env);
    return {
      ...m,
      protected_text_layers: (await read(work)).layers.filter((l) => l.content.kind === "text").map(signature),
      original_layer_ids: (await read(work)).layers.map((l) => l.id)
    };
  },
  async verify(context) {
    const d = read(context.work);
    return Promise.all([
      criterion("the canvas is extended to 960x720", async () => {
        const x = await d;
        return x.size.width === 960 && x.size.height === 720;
      }),
      criterion(
        "all original editable text, styling and transforms are preserved",
        async () => {
          const doc = await d;
          return context.metadata.protected_text_layers.every((old) => {
            const layer = doc.layers.find((l) => l.id === old.id);
            return layer && isDeepStrictEqual2(signature(layer), old);
          });
        }
      ),
      criterion(
        "exactly one support caption is editable with the required content",
        async () => {
          const ls = (await d).layers.filter(
            (l) => l.name === "Support caption"
          );
          return ls.length === 1 && ls[0].visible && ls[0].content.kind === "text" && ls[0].content.text === "Pilot support: support@reef.example";
        }
      ),
      criterion(
        "the caption keeps its 24pt baseline and unscaled transform",
        async () => {
          const c = (await d).layers.find(
            (l) => l.name === "Support caption"
          )?.content;
          return c && closeTo(c.size_pt, 24) && vectorClose(c.transform?.m, [1, 0, 0, 1, 60, 650]);
        }
      ),
      criterion("the caption uses the requested cyan color", async () => {
        const c = (await d).layers.find((l) => l.name === "Support caption")?.content.color;
        return c?.alpha === 1 && vectorClose(c.c?.slice(0, 3), [103 / 255, 232 / 255, 249 / 255]);
      }),
      criterion(
        "existing layer identities remain and only the caption is added",
        async () => {
          const ls = (await d).layers, ids = context.metadata.original_layer_ids;
          return ls.length === ids.length + 1 && ids.every((id) => ls.some((l) => l.id === id));
        }
      ),
      criterion("the PNG is 960x720", async () => {
        const p = await dimensions(resolve14(context.work, "pilot-banner.png"));
        return p.width === 960 && p.height === 720;
      }),
      criterion("the JPEG is 960x720", async () => {
        const p = await dimensions(resolve14(context.work, "pilot-banner.jpg"));
        return p.width === 960 && p.height === 720;
      }),
      criterion(
        "the added bottom canvas has the original dark-blue background",
        () => backgroundBand(context.work, "crop=800:30:60:680")
      )
    ]);
  }
};
import { resolve as resolve15 } from "node:path";
var linear = (v) => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
var colors = { Body: [22, 139, 131], Body_Coral: [230, 97, 76] };
async function solveHero(c, work) {
  const path = resolve15(work, "product-hero.blend");
  await call(c, "inspect_scene", { path });
  await call(c, "duplicate_object", {
    path,
    source_name: "Body",
    name: "Body_Coral"
  });
  await finishHero(c, work);
}
async function finishHero(c, work) {
  const path = resolve15(work, "product-hero.blend");
  for (const [index, object_name] of ["Body", "Body_Coral"].entries()) {
    await call(c, "set_transform", {
      path,
      object_name,
      location: [index ? 1.7 : -1.7, 0, 1],
      scale: [1.2, 0.8, 0.65],
      rotation_degrees: [0, 0, index ? -15 : 15]
    });
    await call(c, "assign_material", {
      path,
      object_name,
      name: object_name + "Pilot",
      color: [...colors[object_name].map((v) => linear(v / 255)), 1],
      roughness: 0.3,
      metallic: 0.25
    });
  }
  await call(c, "configure_camera", {
    path,
    location: [7, -10, 7],
    target: [0, 0, 1],
    lens_mm: 52
  });
  await call(c, "render_preview", {
    path,
    output: resolve15(work, "product-hero.png"),
    width: 640,
    height: 360
  });
  await call(c, "export_meshes", {
    path,
    output: resolve15(work, "product-variants.obj"),
    object_names: ["Body", "Body_Coral"]
  });
}
async function actorPosts(context) {
  const r = await api(
    context.env,
    "GET",
    "/channels/" + context.metadata.channel_id + "/posts?per_page=200"
  );
  return Object.values(r.posts).filter(
    (p) => p.user_id === context.metadata.actor_id && !p.type
  );
}
var editHandoff = {
  ...releaseHandoff,
  id: "mattermost-correct-existing-handoff",
  family: releaseHandoff.id,
  split: "development",
  serviceFixture: "handoff-edit",
  title: "Correct an existing handoff in place without duplicating the reply or attachment",
  brief: releaseHandoff.brief.replace(
    "Do not edit or delete anyone's original messages",
    "Do not edit or delete other participants' original messages"
  ).replace(
    "Post exactly one concise reply inside",
    "You already posted one inaccurate draft handoff. Edit that same reply in place inside"
  ).replace(
    "Attach the provided QA-summary.txt from the working folder to that same reply.",
    "Keep the existing QA-summary.txt attachment on your reply with its original file ID; its bytes are already correct. Do not upload or create another copy."
  ) + "\nPreserve your existing reply ID. Correct its obsolete facts and acknowledge the actual operations checkpoint; do not delete and repost.",
  prepareTrial: async (work, env) => seedReleaseDiscussion(work, env, "handoff-edit"),
  async verify(context) {
    return [
      ...await releaseHandoff.verify(context),
      await criterion(
        "the corrected handoff retains its original reply and file identities",
        async () => {
          const posts2 = await actorPosts(context);
          return posts2.length === 1 && posts2[0].id === context.metadata.old_reply_id && JSON.stringify(posts2[0].file_ids) === JSON.stringify(context.metadata.old_file_ids);
        }
      )
    ];
  }
};
var ambiguousThread = {
  ...releaseHandoff,
  id: "mattermost-ambiguous-thread",
  family: releaseHandoff.id,
  split: "held-out",
  serviceFixture: "ambiguous-thread",
  title: "Choose the authoritative checkpoint over a newer superseded draft thread",
  brief: releaseHandoff.brief + "\nA newer similarly titled QA checkpoint is explicitly superseded. Use the original operations checkpoint with the confirmed engineering correction, even though the draft thread is newer. Leave the superseded thread unchanged and publish no other handoff replies.",
  prepareTrial: async (work, env) => seedReleaseDiscussion(work, env, "ambiguous-thread"),
  async verify(context) {
    return [
      ...await releaseHandoff.verify(context),
      await criterion(
        "the actor publishes only the one authoritative-thread handoff",
        async () => {
          const p = await actorPosts(context);
          return p.length === 1 && p[0].root_id === context.metadata.root_id && p[0].root_id !== context.metadata.decoy_root_id;
        }
      )
    ];
  }
};
import { resolve as resolve16 } from "node:path";
import { unlink as unlink4 } from "node:fs/promises";
async function removeBackup(work) {
  await unlink4(resolve16(work, "product-hero.blend1")).catch(
    (e) => {
      if (e.code !== "ENOENT") throw e;
    }
  );
}
var linkedVariants = {
  ...productHero,
  id: "blender-repair-linked-variants",
  family: productHero.id,
  split: "development",
  mcpGuidance: "Inspect the existing objects and data sharing. Use typed tools to make their meshes independent, update materials/transforms, save, render and export. Keep both existing product objects. No scripts or generic file APIs.",
  title: "Repair shared mesh data before independently recoloring existing products",
  brief: productHero.brief.replace(
    "Preserve Body and duplicate it as Body_Coral with independent mesh/material slots.",
    "Body and Body_Coral already exist and share a mesh datablock. Keep both existing objects, make their meshes/material slots independent, and do not create a third product or replace an existing object."
  ),
  async prepare(work, env) {
    const metadata = await productHero.prepare(work, env), path = resolve16(work, "product-hero.blend");
    await withApplication("blender", work, async (c) => {
      await call(c, "duplicate_object", {
        path,
        source_name: "Body",
        name: "Body_Coral"
      });
      await call(c, "set_transform", {
        path,
        object_name: "Body_Coral",
        location: [3, 0, 1],
        scale: [1.5, 1, 0.6]
      });
    });
    await new NativeEngine("blender").run(
      [
        "bpy.ops.wm.open_mainfile(filepath=p['path'])",
        "bpy.data.objects['Body_Coral'].data=bpy.data.objects['Body'].data",
        "bpy.ops.wm.save_as_mainfile(filepath=p['path']); result={'linked':True}"
      ],
      { path }
    );
    await removeBackup(work);
    return { ...metadata, fixture_profile: "existing-linked-products" };
  }
};
var catalogReshoot = {
  ...productHero,
  id: "blender-catalog-reshoot",
  family: productHero.id,
  split: "held-out",
  mcpGuidance: "Inspect the approved scene, configure orthographic camera framing, render the portrait delivery and export the existing product meshes. Preserve the existing products and materials. No scripts or generic file APIs.",
  title: "Reframe existing product variants with an orthographic portrait delivery",
  brief: [
    "The existing product-hero.blend already contains the approved Body and Body_Coral variants. Produce a new portrait catalog image without duplicating or rebuilding any mesh.",
    "Preserve both product meshes, independent geometry/material slots, transforms and approved swatches, Floor, ArchiveSample's hidden state, and both lights.",
    "Change the active camera to orthographic, at (0,-10,6), looking at (0,0,1), with orthographic scale 8. Do not substitute a long-lens perspective camera.",
    "Save the scene and replace product-hero.png with a nonblank 384 x 512 portrait PNG, CPU Cycles, 16 samples, 100% resolution. The old landscape PNG is superseded.",
    "Deliver product-variants.obj containing only Body and Body_Coral. Preserve the native project and inspect the final render."
  ].join("\n"),
  async prepare(work, env) {
    const metadata = await productHero.prepare(work, env);
    await withApplication("blender", work, (c) => solveHero(c, work));
    await removeBackup(work);
    return { ...metadata, fixture_profile: "approved-products-landscape" };
  },
  verify: (context) => verifyProduct(context, { resolution: [384, 512], orthographic: true })
};
import { writeFile as writeFile5 } from "node:fs/promises";
import { resolve as resolve18 } from "node:path";
import { writeFile as writeFile4 } from "node:fs/promises";
import { resolve as resolve17 } from "node:path";
import { isDeepStrictEqual as isDeepStrictEqual3 } from "node:util";
var nativeDefaults = {
  timeseries: {
    fieldConfig: {
      defaults: {
        color: {
          mode: "palette-classic"
        },
        custom: {
          axisBorderShow: false,
          axisCenteredZero: false,
          axisColorMode: "text",
          axisLabel: "",
          axisPlacement: "auto",
          barAlignment: 0,
          barWidthFactor: 0.6,
          drawStyle: "line",
          fillOpacity: 0,
          gradientMode: "none",
          hideFrom: {
            legend: false,
            tooltip: false,
            viz: false
          },
          insertNulls: false,
          lineInterpolation: "linear",
          lineWidth: 1,
          pointSize: 5,
          scaleDistribution: {
            type: "linear"
          },
          showPoints: "auto",
          showValues: false,
          spanNulls: false,
          stacking: {
            group: "A",
            mode: "none"
          },
          thresholdsStyle: {
            mode: "off"
          }
        },
        mappings: [],
        thresholds: {
          mode: "absolute",
          steps: [
            {
              color: "green",
              value: 0
            },
            {
              color: "red",
              value: 80
            }
          ]
        }
      }
    },
    options: {
      legend: {
        calcs: [],
        displayMode: "list",
        placement: "bottom",
        showLegend: true
      },
      tooltip: {
        hideZeros: false,
        mode: "single",
        sort: "none"
      }
    }
  },
  text: {
    options: {
      code: {
        language: "plaintext",
        showLineNumbers: false,
        showMiniMap: false
      }
    }
  }
};
function withDefaults(value, defaults) {
  if (value === void 0) return structuredClone(defaults);
  if (!value || !defaults || typeof value !== "object" || typeof defaults !== "object" || Array.isArray(value) || Array.isArray(defaults))
    return value;
  const result = { ...value };
  for (const [key, fallback] of Object.entries(defaults))
    result[key] = withDefaults(result[key], fallback);
  return result;
}
function semanticPanel(panel) {
  const { gridPos: _grid, pluginVersion: _version, ...body } = panel;
  if (body.targets) body.targets = panelQueries(panel);
  return withDefaults(
    body,
    nativeDefaults[panel.type] ?? {}
  );
}
function panelQueries(panel) {
  return (panel.targets ?? []).map((query) => {
    const datasource = query.datasource ?? panel.datasource;
    if (!datasource?.uid || !datasource.type)
      throw Error("Panel query has no datasource");
    return { ...query, datasource };
  });
}
function sameSecond(actual, expected) {
  return Number.isFinite(actual) && Number.isFinite(expected) && Math.floor(actual / 1e3) === Math.floor(expected / 1e3);
}
var pilotDashboard = {
  id: "grafana-pilot-monitoring",
  application: "grafana",
  title: "Prepare the production pilot dashboard and scoped deployment marker",
  outputs: [],
  brief: [
    "Update the existing production dashboard __DASHBOARD_TITLE__ for the Reef 5.4 pilot. The similarly named archive dashboard is historical and must remain unchanged.",
    "Persist a default time range of the last 6 hours and an automatic refresh interval of 15 seconds. Preserve the dashboard UID, title and existing tags.",
    "Keep the Request rate panel's query and display settings and the Runbook panel's content unchanged. You may move panels to make space.",
    "Rename the existing Error rate stat panel to Pilot error rate. Display Percent (0-100) and use an absolute red threshold at 2 percent instead of the obsolete 5-percent threshold. Preserve its data query.",
    "Add one time-series panel titled p95 request latency using the same Reef pilot metrics TestData source. Use CSV Metric Values with the supplied p95 samples 185,190,180,210,205,195. Display milliseconds and an absolute red threshold at 250 ms.",
    "Create exactly one annotation on this production dashboard for 2026-10-21 22:30:00 UTC: Reef 5.4 pilot deployed, tagged deployment, reef-5.4 and correlation tag __DASHBOARD_UID__. Do not put the event on the archive or create a global annotation.",
    "Save the dashboard and verify that the new latency query returns real data. Inspect the visible dashboard before finishing."
  ].join("\n"),
  mcpGuidance: "Use the Grafana application tools to discover the production dashboard and source UIDs, read its full model, preserve unrelated settings, save changes, query the latency source and create the scoped annotation. No direct HTTP, file APIs or browser automation.",
  async prepare(work) {
    await writeFile4(
      resolve17(work, "pilot-monitoring-brief.txt"),
      "Reef 5.4 pilot monitoring: last 6h, refresh 15s, error threshold 2%, p95 threshold 250 ms.\n"
    );
    return { dataset: "pilot-monitoring-revision-b" };
  },
  prepareTrial: async (_work, env) => seedPilotDashboard(env),
  async verify(context) {
    const m = context.metadata, env = context.env;
    const read2 = grafanaAPI(
      env,
      "GET",
      "/api/dashboards/uid/" + m.dashboard_uid
    ).then((r) => r.dashboard);
    return Promise.all([
      criterion(
        "production dashboard identity and tags are preserved",
        async () => {
          const d = await read2;
          return d.uid === m.dashboard_uid && d.title === m.dashboard_title && ["reef", "production"].every((tag) => d.tags.includes(tag));
        }
      ),
      criterion(
        "the persisted default is six hours with a 15-second refresh",
        async () => {
          const d = await read2;
          return d.time.from === "now-6h" && d.time.to === "now" && d.refresh === "15s";
        }
      ),
      criterion(
        "the error stat displays percent with the corrected red threshold",
        async () => {
          const p = (await read2).panels.find(
            (p2) => p2.id === (m.error_panel_id ?? 2)
          );
          const f = p?.fieldConfig?.defaults;
          return p?.title === "Pilot error rate" && p.type === "stat" && f?.unit === "percent" && f.thresholds?.mode === "absolute" && f.thresholds.steps.some(
            (s) => s.value === 2 && s.color === "red"
          ) && !f.thresholds.steps.some((s) => s.value === 5);
        }
      ),
      criterion("the original error-rate query is preserved", async () => {
        const p = (await read2).panels.find(
          (p2) => p2.id === (m.error_panel_id ?? 2)
        ), old = m.original_panels.find(
          (p2) => p2.id === (m.error_panel_id ?? 2)
        );
        return !!p && !!old && isDeepStrictEqual3(panelQueries(p), panelQueries(old));
      }),
      criterion(
        "request-rate settings and runbook content are unchanged",
        async () => {
          const d = await read2;
          return m.original_panels.filter(
            (p) => p.id !== (m.error_panel_id ?? 2) && p.id !== m.latency_panel_id
          ).every((old) => {
            const p = d.panels.find((p2) => p2.id === old.id);
            return p && isDeepStrictEqual3(semanticPanel(p), semanticPanel(old));
          });
        }
      ),
      criterion(
        "exactly one latency panel has the requested source, units and threshold",
        async () => {
          const ps = (await read2).panels.filter(
            (p2) => p2.title === "p95 request latency"
          ), p = ps[0], f = p?.fieldConfig?.defaults;
          return ps.length === 1 && (await read2).panels.length === m.original_panels.length + (m.latency_panel_id ? 0 : 1) && (m.latency_panel_id === void 0 || p.id === m.latency_panel_id) && p.type === "timeseries" && f?.unit === "ms" && f.thresholds?.mode === "absolute" && f.thresholds.steps.some(
            (s) => s.value === 250 && s.color === "red"
          ) && p.targets?.length === 1 && panelQueries(p)[0].datasource.uid === m.data_uid && p.targets[0].scenarioId === "csv_metric_values" && p.targets[0].stringInput === "185,190,180,210,205,195";
        }
      ),
      criterion(
        "the delivered latency query returns populated native data frames",
        async () => {
          const p = (await read2).panels.find(
            (p2) => p2.title === "p95 request latency"
          );
          if (!p?.targets) return false;
          const data = await grafanaAPI(env, "POST", "/api/ds/query", {
            queries: panelQueries(p),
            from: "now-6h",
            to: "now"
          });
          return Object.values(data.results).some(
            (r) => !r.error && r.frames?.some(
              (f) => f.data.values.some((v) => v.some((x) => typeof x === "number"))
            )
          );
        }
      ),
      criterion(
        "one correctly timed deployment annotation is scoped to production",
        async () => {
          const notes = await grafanaAPI(
            env,
            "GET",
            "/api/annotations?tags=" + m.dashboard_uid + "&from=" + (Number(m.deploy_time_ms) - 1e3) + "&to=" + (Number(m.deploy_time_ms) + 1e3) + "&limit=100"
          );
          const found = notes.filter(
            (n) => n.text === "Reef 5.4 pilot deployed" && n.tags.includes("reef-5.4")
          );
          return found.length === 1 && sameSecond(found[0].time, Number(m.deploy_time_ms)) && found[0].tags.includes("deployment") && (found[0].dashboardUID === m.dashboard_uid || found[0].dashboardId === (await read2).id);
        }
      ),
      criterion(
        "the similarly named archive dashboard remains unchanged",
        async () => isDeepStrictEqual3(
          (await grafanaAPI(
            env,
            "GET",
            "/api/dashboards/uid/" + m.archive_uid
          )).dashboard,
          m.archive
        )
      )
    ]);
  }
};
var panelCollision = {
  ...pilotDashboard,
  id: "grafana-panel-collision",
  family: pilotDashboard.id,
  split: "development",
  serviceFixture: "panel-collision",
  title: "Repair an existing latency panel without overwriting a colliding panel ID",
  brief: pilotDashboard.brief.replace(
    "Add one time-series panel titled p95 request latency",
    "Update the existing p95 request latency panel in place as a time-series panel. Preserve its panel ID and do not create a duplicate. Another panel already uses ID 4; retain Incident notes unchanged. Use the title p95 request latency"
  ) + "\nPanel order and IDs differ from the earlier template. Discover panels by current content before updating them.",
  prepareTrial: async (_work, env) => seedPilotDashboard(env, "panel-collision")
};
var staleSnapshot = {
  ...pilotDashboard,
  id: "grafana-stale-snapshot",
  family: pilotDashboard.id,
  split: "held-out",
  serviceFixture: "stale-snapshot",
  title: "Merge monitoring changes with a newer approval after a stale export",
  brief: pilotDashboard.brief + "\nA cached dashboard snapshot predates a newer Release approvals panel. The current server is authoritative: preserve that panel and its approval exactly. If an update is rejected as stale, reload and merge the requested changes; do not force-overwrite or recreate the dashboard.",
  async prepare(work, env) {
    const meta = await pilotDashboard.prepare(work, env);
    await writeFile5(
      resolve18(work, "snapshot-warning.txt"),
      "The cached dashboard predates Morgan's release approval. Refresh before saving; preserve the newer Release approvals panel.\n"
    );
    return { ...meta, fixture_profile: "stale-snapshot" };
  },
  prepareTrial: async (_work, env) => seedPilotDashboard(env, "stale-snapshot")
};
import { resolve as resolve19 } from "node:path";
var original2 = [
  "Reef Weekly Release Report",
  "Reporting period: 12-16 October 2026",
  "Release status: At risk",
  "Completed work",
  "Draft placeholder",
  "Risks and owners",
  "Camera calibration - owner TBC",
  "Next steps",
  "Ship after final review.",
  "Internal use only - Reef partner operations."
];
var required = [
  "Reporting period: 19-23 October 2026",
  "Release status: Ready for customer pilot",
  "Mechanical fit checks: 24 of 24 passed.",
  "Packaging checklist: approved by Operations.",
  "Camera calibration - owner Morgan - due 23 October",
  "Customer pilot handoff",
  "1. Send the pilot pack to Customer Success.",
  "2. Confirm the partner training slot by Friday."
];
var weeklyReport = {
  id: "wordcraft-weekly-report",
  application: "wordcraft",
  title: "Revise a weekly release report for a customer handoff",
  inputFile: "weekly-report.docx",
  outputs: ["weekly-report.docx", "weekly-report.pdf"],
  brief: `The existing weekly-report.docx is open. Prepare it for the customer-pilot handoff.
Update the reporting period to 19-23 October 2026 and the release status to Ready for customer pilot.
Replace Draft placeholder with two paragraphs: Mechanical fit checks: 24 of 24 passed. and Packaging checklist: approved by Operations.
Update the risk line to Camera calibration - owner Morgan - due 23 October.
Format the existing report title in bold, 18 pt, centered, with dark-blue text (#1F4E78).
Before the final Internal use only - Reef partner operations. paragraph, add a bold section heading Customer pilot handoff,
followed by these two separate action paragraphs: 1. Send the pilot pack to Customer Success. and 2. Confirm the partner training slot by Friday.
Keep the existing Completed work, Risks and owners, Next steps headings and Ship after final review. paragraph.
Save the revised DOCX in place and export weekly-report.pdf in the same folder. Check the visible result and preserve readable layout.`,
  mcpGuidance: "The real desktop document is already open. Use WordCraft tools to inspect and revise it. Discover command parameters with list_commands. Save the DOCX and export a PDF using the normal document/file tools. Do not recreate the entire document or use a generic script/file API.",
  async prepare(work) {
    await withApplication("wordcraft", work, async (client) => {
      await call(client, "new_document", { template: "blank" });
      await call(client, "type_text", {
        text: original2.join("\n"),
        paragraphs: true
      });
      await call(client, "select_text", { text: original2[0] });
      await call(client, "execute", {
        command: "format.set",
        params: { props: { size: 16, bold: true, color: "555555" } }
      });
      await call(client, "save_document", {
        path: resolve19(work, "weekly-report.docx")
      });
    });
    return { original: original2, required };
  },
  async verify(context) {
    const path = resolve19(context.work, "weekly-report.docx");
    const read2 = async () => {
      const value = await docx(path);
      return { ...value, text: value.numberedText.map(canonicalText) };
    };
    return Promise.all([
      criterion("reporting period and release decision updated", async () => {
        const d = await read2();
        return required.slice(0, 2).every((t) => d.text.includes(t)) && !d.text.includes(original2[1]) && !d.text.includes(original2[2]);
      }),
      criterion("completed work contains both factual paragraphs", async () => {
        const d = await read2();
        return required.slice(2, 4).every((t) => d.text.includes(t)) && !d.text.includes("Draft placeholder");
      }),
      criterion("risk has the confirmed owner and deadline", async () => {
        const d = await read2();
        return d.text.includes(required[4]) && !d.text.includes(original2[6]);
      }),
      criterion(
        "report title remains and is bold 18 pt dark blue",
        async () => {
          const d = await read2(), p = d.paragraphs[d.text.indexOf(original2[0])];
          if (!p) return false;
          const runs = array(p["w:r"]).filter((r) => r["w:t"]);
          return runs.length > 0 && runs.every((r) => {
            const props = r["w:rPr"];
            return props && enabledProperty(props["w:b"]) && String(props["w:sz"]?.["@w:val"]) === "36" && String(props["w:color"]?.["@w:val"]).toUpperCase() === "1F4E78";
          });
        }
      ),
      criterion("report title is centered", async () => {
        const d = await read2(), p = d.paragraphs[d.text.indexOf(original2[0])];
        return p?.["w:pPr"]?.["w:jc"]?.["@w:val"] === "center";
      }),
      criterion("new handoff heading is bold", async () => {
        const d = await read2(), p = d.paragraphs[d.text.indexOf(required[5])];
        return !!p && array(p["w:r"]).some(
          (r) => enabledProperty((r["w:rPr"] ?? {})["w:b"])
        );
      }),
      criterion(
        "handoff actions precede the preserved final footer",
        async () => {
          const d = await read2();
          const positions = required.slice(5).map((text) => d.text.indexOf(text));
          return positions.every(
            (p, i) => p >= 0 && (i === 0 || p > positions[i - 1])
          ) && d.text.filter(Boolean).at(-1) === original2.at(-1);
        }
      ),
      criterion("unrequested report sections preserved", async () => {
        const d = await read2();
        return [original2[3], original2[5], original2[7], original2[8]].every(
          (text) => d.text.includes(text)
        );
      }),
      criterion("exported PDF contains every required update", async () => {
        const text = canonicalText(
          await pdfText(resolve19(context.work, "weekly-report.pdf"))
        );
        return [original2[0], ...required, original2.at(-1)].every(
          (s) => text.includes(s)
        );
      })
    ]);
  }
};
var dailyWorkflows = [
  weeklyReport,
  partnerDeck,
  campaignBanner,
  releaseHandoff,
  boardPlate,
  productHero,
  pilotDashboard,
  cableSlot,
  serviceStandoffs,
  linkedVariants,
  catalogReshoot,
  panelCollision,
  staleSnapshot,
  scopedAmendment,
  actionRegister,
  reorderAndRetire,
  currentAndArchive,
  proofVisibility,
  canvasExtension,
  editHandoff,
  ambiguousThread,
  monitoringHandoff,
  cadToScene
];
function dailyWorkflow(id) {
  const workflow = dailyWorkflows.find((w) => w.id === id);
  if (!workflow) throw new Error(`Unknown daily workflow: ${id}`);
  return workflow;
}
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}
async function grade(task, work, logs, context, env = process.env) {
  await mkdir(logs, { recursive: true });
  await writeFile6(resolve20(logs, "reward.txt"), "0\n");
  try {
    const metadata = JSON.parse(await readFile8(context, "utf8"));
    const criteria = await dailyWorkflow(task).verify({
      work,
      metadata,
      env: Object.fromEntries(
        Object.entries(env).filter(
          (e) => typeof e[1] === "string"
        )
      )
    });
    const passed = criteria.filter((c) => c.passed).length;
    const reward = Number(criteria.length > 0 && passed === criteria.length);
    await writeFile6(
      resolve20(logs, "criteria.json"),
      JSON.stringify(
        {
          task,
          reward,
          quality: criteria.length ? passed / criteria.length : 0,
          passed,
          total: criteria.length,
          criteria
        },
        null,
        2
      ) + "\n"
    );
    await writeFile6(resolve20(logs, "reward.txt"), `${reward}
`);
    return reward;
  } catch (error) {
    await writeFile6(
      resolve20(logs, "criteria.json"),
      JSON.stringify(
        { task, reward: 0, infrastructure_error: errorMessage(error) },
        null,
        2
      ) + "\n"
    );
    throw error;
  }
}
import { readFile as readFile9 } from "node:fs/promises";
Object.assign(
  process.env,
  JSON.parse(await readFile9("/app/service-env.json", "utf8").catch(() => "{}"))
);
await grade(
  process.argv[2],
  process.env.REEF_WORKSPACE ?? "/app/work",
  process.env.REEF_VERIFIER_LOGS ?? "/logs/verifier",
  process.env.REEF_TASK_CONTEXT ?? "/app/context.json"
);
