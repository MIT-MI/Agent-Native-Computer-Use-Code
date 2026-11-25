import { mkdir as mkdir5, readFile as readFile22, writeFile as writeFile15 } from "node:fs/promises";
import { resolve as resolve44 } from "node:path";
import { copyFile as copyFile2, mkdtemp as mkdtemp7, readFile as readFile10, rm as rm7 } from "node:fs/promises";
import { resolve as resolve15 } from "node:path";
import { isDeepStrictEqual as isDeepStrictEqual6 } from "node:util";
import { readFile, writeFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { unzipSync, strFromU8 } from "fflate";
import { XMLParser } from "fast-xml-parser";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
var repoRoot = process.env.REEF_REPO_ROOT ?? resolve(dirname(fileURLToPath(import.meta.url)), "../..");
function stringEnvironment(extra = {}) {
  return Object.fromEntries(
    Object.entries({ ...process.env, ...extra }).filter(
      (entry) => typeof entry[1] === "string"
    )
  );
}
async function connectApplication(name, env = {}, connect) {
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [
      resolve(repoRoot, "dist/cli.js"),
      "serve",
      name,
      ...connect ? ["--connect", connect] : []
    ],
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
async function withApplication(name, work, action, extra = {}, connect) {
  const client = await connectApplication(
    name,
    {
      ...extra,
      REEF_WORKSPACE: work,
      RAYON_NUM_THREADS: "2"
    },
    connect
  );
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
  return Object.entries(object).filter(([key3]) => !key3.startsWith("@")).map(([, value2]) => xmlText(value2)).join("");
}
async function docx(path) {
  const zip = unzipSync(await readFile(path));
  const raw = strFromU8(zip["word/document.xml"]);
  const xml = new XMLParser({
    ignoreAttributes: false,
    attributeNamePrefix: "@"
  }).parse(raw);
  const paragraphs2 = array(xml["w:document"]["w:body"]["w:p"]);
  const text = paragraphs2.map(
    (p) => array(p["w:r"]).map((r) => xmlText(r["w:t"])).join("")
  );
  const numbering = zip["word/numbering.xml"] ? new XMLParser({
    ignoreAttributes: false,
    attributeNamePrefix: "@"
  }).parse(strFromU8(zip["word/numbering.xml"]))["w:numbering"] : {};
  const counters = /* @__PURE__ */ new Map();
  const numberedText = paragraphs2.map((p, index) => {
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
  return { paragraphs: paragraphs2, text, numberedText, raw };
}
var execute = promisify(execFile);
async function pdfText(path) {
  return (await execute("pdftotext", [path, "-"], { timeout: 3e4 })).stdout;
}
function scopedClient(client, application) {
  return new Proxy(client, {
    get(target3, key3) {
      if (key3 === "callTool")
        return (...args) => {
          args[0] = { ...args[0], name: application + "__" + args[0].name };
          return target3.callTool(...args);
        };
      const value = Reflect.get(target3, key3, target3);
      return typeof value === "function" ? value.bind(target3) : value;
    }
  });
}
import { createHash } from "node:crypto";
import { readFile as readFile2 } from "node:fs/promises";
import { resolve as resolve2 } from "node:path";
import { isDeepStrictEqual } from "node:util";
var source = "review-notes.docx";
var receipt = "delivery.docx";
var title = "Reef release delivery register";
var active = "Current delivery";
var history = "Historical archive";
var footer = "Internal - retain the signed historical record.";
var digest = (bytes) => createHash("sha256").update(bytes).digest("hex");
async function unwrappedPdfText(path) {
  const text = (await execute("pdftotext", ["-raw", path, "-"], { timeout: 15e3 })).stdout;
  return text.replace(/(\S)-[ \t]*\r?\n[ \t]*(?=\S)/g, "$1-");
}
var factKey = (text) => canonicalText(text).toLowerCase().replace(/\s*([:;,()])\s*/g, "$1").replace(/\s*x\s*/g, "x").replace(/\.$/, "");
function sameRegisterFact(actual, expected) {
  if (factKey(actual) === factKey(expected)) return true;
  if (!expected.startsWith("Gate:")) return false;
  const subjects = expected.includes("engineering") ? ["engineering sign-off"] : ["Leah's campaign sign-off", "Leah campaign sign-off"];
  const equivalent = subjects.flatMap((approval) => [
    "Gate: Publish only after " + approval,
    "Gate: Publish after " + approval,
    "Gate: Publication requires " + approval,
    "Gate: Await " + approval + " before publication",
    "Gate: Do not publish until " + approval
  ]);
  return equivalent.some((value) => factKey(actual) === factKey(value));
}
async function create(c, path, lines) {
  await call(c, "new_document", { template: "blank" });
  await call(c, "type_text", { text: lines.join("\n"), paragraphs: true });
  await call(c, "select_text", { text: lines[0] });
  await call(c, "execute", {
    command: "format.set",
    params: { props: { bold: true, size: 18, color: "1F4E78" } }
  });
  await call(c, "save_document", { path });
}
async function prepareRegister(work, notes3, draft3, final) {
  const archive2 = [
    "Signed R1 / 22 October 2026",
    "Owner: Alex",
    "Historical status: approved preview; retain without amendments."
  ];
  await withApplication("wordcraft", work, async (c) => {
    await create(c, resolve2(work, source), notes3);
    await create(c, resolve2(work, receipt), [
      title,
      active,
      ...draft3,
      history,
      ...archive2,
      footer
    ]);
  });
  return {
    source_sha256: digest(await readFile2(resolve2(work, source))),
    draft: draft3,
    final,
    archive: archive2
  };
}
async function registerFields(work) {
  const lines = (await docx(resolve2(work, receipt))).numberedText.map(canonicalText).filter(Boolean);
  return lines.slice(lines.indexOf(active) + 1, lines.indexOf(history));
}
async function verifyRegister(ctx, m) {
  const lines = () => docx(resolve2(ctx.work, receipt)).then(
    (d) => d.numberedText.map(canonicalText).filter(Boolean)
  );
  return Promise.all([
    criterion(
      "the later approval source is preserved byte for byte",
      async () => digest(await readFile2(resolve2(ctx.work, source))) === m.source_sha256
    ),
    criterion(
      "the existing current register fields contain the approved facts exactly once in their original order",
      async () => {
        const t = await lines(), fields = await registerFields(ctx.work);
        return t.indexOf(active) === 1 && t.indexOf(history) > 1 && fields.length === m.final.length && fields.every((v, i) => sameRegisterFact(v, m.final[i]));
      }
    ),
    criterion(
      "the register retains its original title, signed historical record and footer",
      async () => {
        const t = await lines();
        return t[0] === title && t.at(-1) === footer && isDeepStrictEqual(t.slice(t.indexOf(history) + 1, -1), m.archive);
      }
    ),
    criterion(
      "the register PDF contains the final current fields and the unchanged historical record",
      async () => {
        const text = canonicalText(
          await unwrappedPdfText(resolve2(ctx.work, "delivery.pdf"))
        ), t = await lines();
        const start = text.indexOf(active), end = text.indexOf(history), current2 = text.slice(start + active.length, end);
        return start >= 0 && end > start && t.every((line) => text.includes(line)) && (await registerFields(ctx.work)).every(
          (line) => current2.includes(line)
        );
      }
    )
  ]);
}
import { readFile as readFile3 } from "node:fs/promises";
import { resolve as resolve3 } from "node:path";
import { unzipSync as unzipSync2, strFromU8 as strFromU82 } from "fflate";
async function manifest(path) {
  const archive2 = unzipSync2(await readFile3(path));
  return JSON.parse(strFromU82(archive2["manifest.json"]));
}
async function dimensions(path) {
  const probe2 = await execute(
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
  return JSON.parse(probe2.stdout).streams[0];
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
        path: resolve3(work, "pilot-banner.pcraft")
      });
      await call(client, "doc_export", {
        path: resolve3(work, "reference.png")
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
    const read2 = () => manifest(resolve3(context.work, "pilot-banner.pcraft"));
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
        const d = await dimensions(resolve3(context.work, "pilot-banner.png"));
        return d.width === 1200 && d.height === 675;
      }),
      criterion("JPEG has the requested size", async () => {
        const d = await dimensions(resolve3(context.work, "pilot-banner.jpg"));
        return d.width === 1200 && d.height === 675;
      }),
      criterion("export keeps the original dark-blue background", async () => {
        const { stdout } = await execute(
          "ffmpeg",
          [
            "-loglevel",
            "error",
            "-i",
            resolve3(context.work, "pilot-banner.png"),
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
import { copyFile, mkdtemp as mkdtemp6, readFile as readFile9, rm as rm6, unlink as unlink2 } from "node:fs/promises";
import { resolve as resolve14 } from "node:path";
import { isDeepStrictEqual as isDeepStrictEqual5 } from "node:util";
import { resolve as resolve5 } from "node:path";
import { execFile as execFile2 } from "node:child_process";
import { mkdtemp, readFile as readFile4, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify as promisify2 } from "node:util";
import { mkdirSync, realpathSync, existsSync, statSync } from "node:fs";
import { dirname as dirname2, isAbsolute, relative, resolve as resolve4, sep } from "node:path";
function workspace() {
  const root = resolve4(process.env.REEF_WORKSPACE ?? process.cwd());
  mkdirSync(root, { recursive: true });
  return realpathSync(root);
}
var exec = promisify2(execFile2);
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
      const reply = JSON.parse(await readFile4(resultPath, "utf8"));
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
      project: resolve5(work, "mounting-plate.FCStd"),
      step: resolve5(work, "mounting-plate.step"),
      stl: resolve5(work, "mounting-plate.stl")
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
      project: resolve5(work, options.projectFile ?? "product-hero.blend"),
      target: options.target ?? [0, 0, 1],
      geometry: options.geometry ?? false
    }
  );
}
var closeTo = (actual, expected, tolerance = 1e-3) => Number.isFinite(actual) && Math.abs(actual - expected) <= tolerance;
var vectorClose = (actual, expected, tolerance = 1e-3) => actual?.length === expected.length && actual.every((value, index) => closeTo(value, expected[index], tolerance));
import { isDeepStrictEqual as isDeepStrictEqual2 } from "node:util";
import { resolve as resolve6 } from "node:path";
var read = async (work) => (await manifest(resolve6(work, "pilot-banner.pcraft"))).document;
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
      resolve6(work, "pilot-banner.png"),
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
      await call(c, "doc_open", { path: resolve6(work, "pilot-banner.pcraft") });
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
      await call(c, "doc_save", { path: resolve6(work, "pilot-banner.pcraft") });
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
        const p = await dimensions(resolve6(context.work, "pilot-banner.png"));
        return p.width === 960 && p.height === 720;
      }),
      criterion("the JPEG is 960x720", async () => {
        const p = await dimensions(resolve6(context.work, "pilot-banner.jpg"));
        return p.width === 960 && p.height === 720;
      }),
      criterion(
        "the added bottom canvas has the original dark-blue background",
        () => backgroundBand(context.work, "crop=800:30:60:680")
      )
    ]);
  }
};
import { resolve as resolve7 } from "node:path";
import { readFile as readFile5, unlink } from "node:fs/promises";
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
    const path = resolve7(work, "product-hero.blend");
    await withApplication("blender", work, async (c) => {
      await call(c, "create_scene", { path, overwrite: true });
      for (const [name, kind, location, scale3] of [
        ["Body", "cube", [0, 0, 1], [1.5, 1, 0.6]],
        ["Floor", "plane", [0, 0, 0], [6, 6, 1]],
        ["ArchiveSample", "cube", [4, 3, 0.5], [0.5, 0.5, 0.5]]
      ]) {
        await call(c, "add_mesh", { path, kind, name, location });
        await call(c, "set_transform", {
          path,
          object_name: name,
          location,
          scale: scale3
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
    await unlink(resolve7(work, "product-hero.blend1")).catch(
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
        const path = resolve7(context.work, "product-hero.png");
        const probe2 = JSON.parse(
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
        return probe2.width === width && probe2.height === height && values.length === 576 && values.reduce((a, b) => a + (b - mean) ** 2, 0) / values.length > 25;
      }
    ),
    criterion("OBJ contains only the two product meshes", async () => {
      const obj = await readFile5(
        resolve7(context.work, "product-variants.obj"),
        "utf8"
      );
      const names3 = [...obj.matchAll(/^o (.+)$/gm)].map((m) => m[1]);
      return names3.length === 2 && names3.some((n) => /^Body(?:_|$)/.test(n)) && names3.some((n) => /^Body_Coral(?:_|$)/.test(n)) && !/Floor|ArchiveSample/.test(obj) && [...obj.matchAll(/^v /gm)].length === 16;
    })
  ]);
}
import { resolve as resolve8 } from "node:path";
var linear = (v) => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
var colors = { Body: [22, 139, 131], Body_Coral: [230, 97, 76] };
async function solveHero(c, work) {
  const path = resolve8(work, "product-hero.blend");
  await call(c, "inspect_scene", { path });
  await call(c, "duplicate_object", {
    path,
    source_name: "Body",
    name: "Body_Coral"
  });
  await finishHero(c, work);
}
async function finishHero(c, work) {
  const path = resolve8(work, "product-hero.blend");
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
    output: resolve8(work, "product-hero.png"),
    width: 640,
    height: 360
  });
  await call(c, "export_meshes", {
    path,
    output: resolve8(work, "product-variants.obj"),
    object_names: ["Body", "Body_Coral"]
  });
}
import { readFile as readFile6, mkdtemp as mkdtemp3, rm as rm3 } from "node:fs/promises";
import { resolve as resolve10 } from "node:path";
import { isDeepStrictEqual as isDeepStrictEqual3 } from "node:util";
import { resolve as resolve9, join as join2 } from "node:path";
import { mkdtemp as mkdtemp2, rm as rm2 } from "node:fs/promises";
import { tmpdir as tmpdir2 } from "node:os";
async function pngEvidence(path, width, height) {
  const probe2 = JSON.parse(
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
  if (probe2?.codec_name !== "png" || probe2.width !== width || probe2.height !== height)
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
  const pixels4 = [...stdout], mean = pixels4.reduce((a, b) => a + b, 0) / pixels4.length;
  return pixels4.length === 576 && pixels4.reduce((a, b) => a + (b - mean) ** 2, 0) / pixels4.length > 25;
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
async function pngMatchesSavedScene(work, project2, output, state) {
  if (state.render_engine !== "CYCLES" || state.device !== "CPU" || state.samples !== 16 || state.resolution.join(",") !== "640,360,100")
    return false;
  if (!await pngEvidence(resolve9(work, output), 640, 360)) return false;
  const temporary = await mkdtemp2(join2(tmpdir2(), "reef-render-judge-")), reference = join2(temporary, "scene.png");
  try {
    await new NativeEngine("blender").run(
      [
        "bpy.ops.wm.open_mainfile(filepath=p['project'])",
        "scene=bpy.context.scene; scene.render.threads_mode='FIXED'; scene.render.threads=2",
        "scene.render.image_settings.file_format='PNG'; scene.render.filepath=p['output']",
        "bpy.ops.render.render(write_still=True); result={'rendered':True}"
      ],
      { project: resolve9(work, project2), output: reference }
    );
    const actual = await pixels(resolve9(work, output)), expected = await pixels(reference);
    return actual.length === 6912 && expected.length === actual.length && [...actual].reduce((sum, v, i) => sum + Math.abs(v - expected[i]), 0) / actual.length <= 2;
  } finally {
    await rm2(temporary, { recursive: true, force: true });
  }
}
var project = "studio-campaign.vectorcraft";
var finals = [
  project,
  "studio-web.svg",
  "studio-web.png",
  "studio-proof.pdf"
];
var command = (c, id, params = {}) => call(c, "command_run", { id, params });
function vectorNodes(document) {
  const visit = (nodes) => nodes.flatMap((n) => [n, ...visit(n.kind.children ?? [])]);
  return visit(document.document.layers);
}
var vectorText = (node) => node.kind.runs?.map((r) => r.text).join("") ?? "";
function vectorBounds(node) {
  const points = node.kind.path?.subpaths.flatMap((p) => p.anchors.map((a) => a.p)) ?? [];
  if (!points.length) return void 0;
  return [
    Math.min(...points.map((p) => p[0])),
    Math.min(...points.map((p) => p[1])),
    Math.max(...points.map((p) => p[0])),
    Math.max(...points.map((p) => p[1]))
  ];
}
function vectorDeliveryBoundsMatch(actual, expected) {
  return actual?.length === expected.length && actual.every(
    (value, index) => Number.isFinite(value) && Math.abs(value - expected[index]) <= 1e-6
  );
}
var review = {
  copy: "APPROVED / R2: Change the CURRENT WEB headline to Reef Open Studio. The event is 23 October 2026. Change the call to action to Reserve your studio slot in 22 pt cyan #67E8F9. Keep positions, existing type objects, brand, sponsors, print and archive unchanged. R1's 22 October date is superseded.",
  sponsors: "APPROVED / R2: Sponsors are Reef Labs first, then Northbridge. Retire Pending Co. completely from CURRENT WEB. Put Reef Labs' existing badge/text at x=40/54 and Northbridge at x=240/254; keep their y positions, sizes, colors and IDs. Do not edit event copy, brand, approved print or archive. R1's three-sponsor roster is superseded.",
  formats: "APPROVED / R2: CURRENT WEB needs an 800 x 400 canvas. Extend its existing dark background to cover it; keep all other artwork at original size and position. Change only its headline to Reef Open Studio and date to 23 October 2026. APPROVED PRINT is locked: deliver that existing artboard as the PDF proof without modifying it. Web SVG/PNG must use CURRENT WEB. Preserve the archive and existing editable objects. Do not scale the whole design.",
  campaign: "R2 / superseded by the signed R3 approval in review-notes.docx: retain the old date, three-sponsor roster and waitlist action until final approval. APPROVED PRINT / R1 and ARCHIVE are locked. Read the later approval before revising CURRENT WEB; do not apply this superseded instruction."
};
async function prepareVector(profile, work) {
  return withApplication("vectorcraft", work, async (c) => {
    await command(c, "file.new", {
      width: 640,
      height: 360,
      title: "Reef studio campaign"
    });
    await command(c, "artboard.setProps", {
      index: 0,
      name: "CURRENT WEB / R1"
    });
    const ids = {};
    const shape = async (key3, x, y, width, height, fill) => {
      const r = await call(c, "draw_shape", {
        shape: "rectangle",
        x,
        y,
        width,
        height,
        fill,
        stroke: "none",
        name: key3
      });
      ids[key3] = r.id;
    };
    const text = async (key3, value, x, y, size, color2 = "#FFFFFF") => {
      const r = await call(c, "add_text", {
        text: value,
        x,
        y,
        size,
        color: color2,
        font: "Source Sans 3"
      });
      ids[key3] = r.id;
      await command(c, "object.setProps", { id: r.id, name: key3 });
    };
    await shape("Web background", 0, 0, 640, 360, "#123047");
    await text("Web brand", "REEF", 40, 52, 24, "#67E8F9");
    await text("Web headline", "Launch preview", 40, 140, 40);
    await text("Web date", "22 October 2026", 40, 205, 24, "#67E8F9");
    await text("Web action", "Join the waitlist", 40, 248, 20);
    for (const [index, name] of [
      "Northbridge",
      "Reef Labs",
      "Pending Co."
    ].entries()) {
      await shape(name + " badge", 40 + index * 200, 285, 160, 44, "#1F4D68");
      await text(name + " label", name, 54 + index * 200, 313, 17);
    }
    await command(c, "artboard.new", {
      x: 900,
      y: 0,
      width: 480,
      height: 640,
      name: "APPROVED PRINT / R1"
    });
    await shape("Print background", 900, 0, 480, 640, "#123047");
    await text("Print brand", "REEF", 940, 70, 24, "#67E8F9");
    await text("Print headline", "Launch preview", 940, 250, 38);
    await text("Print date", "22 October 2026", 940, 330, 24, "#67E8F9");
    await command(c, "artboard.new", {
      x: 1460,
      y: 0,
      width: 640,
      height: 360,
      name: "ARCHIVE / 2025"
    });
    await shape("Archive background", 1460, 0, 640, 360, "#123047");
    await text("Archive headline", "Launch preview", 1500, 140, 40);
    await text("Archive date", "22 October 2025", 1500, 205, 24, "#67E8F9");
    const reviewLines = review[profile].split(" ").reduce((lines, word) => {
      const last = lines.at(-1);
      if (last && last.length + word.length + 1 <= 76)
        lines[lines.length - 1] += " " + word;
      else lines.push(word);
      return lines;
    }, []);
    await text("Review notes", reviewLines.join("\n"), 20, 460, 14, "#111827");
    await call(c, "save_file", { path: resolve10(work, project) });
    await call(c, "export", {
      path: resolve10(work, "review-r1.png"),
      artboard: 0
    });
    return {
      profile,
      ids,
      original: JSON.parse(await readFile6(resolve10(work, project), "utf8"))
    };
  });
}
function target(profile, approvedDate) {
  return {
    width: ["formats", "campaign"].includes(profile) ? 800 : 640,
    height: ["formats", "campaign"].includes(profile) ? 400 : 360,
    headline: profile === "sponsors" ? "Launch preview" : "Reef Open Studio",
    date: approvedDate ?? (profile === "sponsors" ? "22 October 2026" : "23 October 2026")
  };
}
function deletions(m) {
  return ["sponsors", "campaign"].includes(m.profile) ? [m.ids["Pending Co. badge"], m.ids["Pending Co. label"]] : [];
}
function editable(m) {
  if (m.profile === "campaign")
    return new Set(
      [
        "Web headline",
        "Web date",
        "Web action",
        "Web background",
        "Reef Labs badge",
        "Reef Labs label",
        "Northbridge badge",
        "Northbridge label",
        "Pending Co. badge",
        "Pending Co. label"
      ].map((key3) => m.ids[key3])
    );
  return new Set(
    m.profile === "sponsors" ? [
      "Reef Labs badge",
      "Reef Labs label",
      "Northbridge badge",
      "Northbridge label",
      "Pending Co. badge",
      "Pending Co. label"
    ].map((k) => m.ids[k]) : [
      m.ids["Web headline"],
      m.ids["Web date"],
      m.ids[m.profile === "copy" ? "Web action" : "Web background"]
    ]
  );
}
async function pngMatches(work, expectedWidth, expectedHeight) {
  if (!await pngEvidence(
    resolve10(work, "studio-web.png"),
    expectedWidth,
    expectedHeight
  ))
    return false;
  const temp = await mkdtemp3(resolve10(work, ".vector-verify-"));
  try {
    const reference = resolve10(temp, "web.png");
    await withApplication("vectorcraft", work, async (c) => {
      await call(c, "open_file", { path: resolve10(work, project) });
      await call(c, "export", { path: reference, artboard: 0 });
    });
    const pixels4 = async (path) => (await execute(
      "ffmpeg",
      [
        "-v",
        "error",
        "-threads",
        "1",
        "-i",
        path,
        "-vf",
        "scale=160:90",
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
      { encoding: "buffer", timeout: 15e3, maxBuffer: 65536 }
    )).stdout;
    const a = await pixels4(resolve10(work, "studio-web.png")), b = await pixels4(reference);
    return a.length === 43200 && a.length === b.length && a.every((v, i) => Math.abs(v - b[i]) <= 5);
  } finally {
    await rm3(temp, { recursive: true, force: true });
  }
}
async function verifyVector(ctx) {
  const m = ctx.metadata, expected = target(m.profile, m.approved_date), originalNodes = vectorNodes(m.original);
  const read2 = async () => JSON.parse(await readFile6(resolve10(ctx.work, project), "utf8"));
  const node = async (key3) => {
    const found = vectorNodes(await read2()).find((n) => n.id === m.ids[key3]);
    if (!found) throw Error("Missing original object: " + key3);
    return found;
  };
  const original3 = (key3) => originalNodes.find((n) => n.id === m.ids[key3]);
  const sameStyle = (actual, before, size = before.kind.runs[0].style.size, fill = before.kind.runs[0].style.fill) => actual.kind.type === "text" && actual.kind.runs.every(
    (r) => isDeepStrictEqual3(r.style, { ...before.kind.runs[0].style, size, fill })
  ) && isDeepStrictEqual3(actual.kind.xf, before.kind.xf);
  return Promise.all([
    criterion(
      "the original editable object identities remain, with only the retired sponsor removed",
      async () => {
        const expectedIds = originalNodes.map((n) => n.id).filter((id) => !deletions(m).includes(id)).sort((a, b) => a - b);
        return isDeepStrictEqual3(
          vectorNodes(await read2()).map((n) => n.id).sort((a, b) => a - b),
          expectedIds
        );
      }
    ),
    criterion(
      "all protected artwork, print, archive and review notes remain unchanged",
      async () => {
        const nodes = vectorNodes(await read2()), allowed = editable(m);
        const own = (n) => n && {
          ...n,
          kind: {
            ...n.kind,
            children: n.kind.children?.map((c) => c.id).filter((id) => !deletions(m).includes(id))
          }
        };
        return originalNodes.filter((n) => !allowed.has(n.id)).every(
          (before) => isDeepStrictEqual3(
            own(nodes.find((n) => n.id === before.id)),
            own(before)
          )
        );
      }
    ),
    criterion(
      "the web headline and date reflect the approved review rather than the superseded request",
      async () => vectorText(await node("Web headline")) === expected.headline && vectorText(await node("Web date")) === expected.date
    ),
    criterion(
      "the headline and date retain their original editable type, styling and transforms",
      async () => sameStyle(await node("Web headline"), original3("Web headline")) && sameStyle(await node("Web date"), original3("Web date"))
    ),
    criterion(
      ["sponsors", "campaign"].includes(m.profile) ? "approved sponsors retain their objects, styling and required order" : m.profile === "copy" ? "the approved action retains editable type with the requested styling" : "the newly exposed web canvas contains the approved solid background",
      async () => {
        if (m.profile === "copy") {
          const actual = await node("Web action");
          return vectorText(actual) === "Reserve your studio slot" && sameStyle(
            actual,
            original3("Web action"),
            22,
            original3("Web date").kind.runs[0].style.fill
          );
        }
        if (m.profile === "formats") {
          for (const [x, y] of [
            [780, 200],
            [500, 380],
            [780, 380]
          ]) {
            const { stdout } = await execute(
              "ffmpeg",
              [
                "-v",
                "error",
                "-threads",
                "1",
                "-i",
                resolve10(ctx.work, "studio-web.png"),
                "-vf",
                `crop=8:8:${x}:${y},scale=1:1`,
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
              { encoding: "buffer", timeout: 15e3, maxBuffer: 32 }
            );
            if (stdout.length !== 3 || ![18, 48, 71].every((v, i) => Math.abs(v - stdout[i]) <= 1))
              return false;
          }
          return true;
        }
        for (const [name, dx] of [
          ["Reef Labs", -200],
          ["Northbridge", 200]
        ]) {
          const label = await node(name + " label"), beforeLabel = original3(name + " label");
          const badge = await node(name + " badge"), beforeBounds = vectorBounds(original3(name + " badge"));
          if (vectorText(label) !== name || !isDeepStrictEqual3(label.kind.runs, beforeLabel.kind.runs) || !isDeepStrictEqual3(
            label.kind.xf,
            beforeLabel.kind.xf.map((v, i) => i === 4 ? v + dx : v)
          ) || !isDeepStrictEqual3(
            badge.appearance,
            original3(name + " badge").appearance
          ) || !isDeepStrictEqual3(
            vectorBounds(badge),
            beforeBounds.map((v, i) => i % 2 === 0 ? v + dx : v)
          ))
            return false;
        }
        return true;
      }
    ),
    criterion(
      "artboard geometry is correct and protected artboards are unchanged",
      async () => {
        const boards = (await read2()).document.artboards, before = m.original.document.artboards;
        return boards.length === 3 && isDeepStrictEqual3(boards.slice(1), before.slice(1)) && isDeepStrictEqual3(boards[0], {
          ...before[0],
          rect: {
            ...before[0].rect,
            x1: expected.width,
            y1: expected.height
          }
        });
      }
    ),
    criterion(
      "the original dark web background fills the delivery canvas",
      async () => vectorDeliveryBoundsMatch(vectorBounds(await node("Web background")), [
        0,
        0,
        expected.width,
        expected.height
      ]) && isDeepStrictEqual3(
        (await node("Web background")).appearance,
        original3("Web background").appearance
      )
    ),
    criterion(
      "the web SVG contains editable approved text and the requested viewBox",
      async () => {
        const svg = await readFile6(resolve10(ctx.work, "studio-web.svg"), "utf8");
        return svg.includes(expected.headline) && svg.includes(expected.date) && new RegExp(`viewBox="0 0 ${expected.width} ${expected.height}"`).test(
          svg
        ) && /<text\b/.test(svg);
      }
    ),
    criterion(
      "the delivered web PNG matches an independent render of the saved native artwork",
      () => pngMatches(ctx.work, expected.width, expected.height)
    ),
    criterion(
      "the PDF proof uses the requested artboard and native text",
      async () => {
        const path = resolve10(ctx.work, "studio-proof.pdf"), text = await pdfText(path), print = ["formats", "campaign"].includes(m.profile);
        const info = (await execute("pdfinfo", [path], { timeout: 15e3 })).stdout;
        return /Pages:\s+1\b/.test(info) && new RegExp(
          `Page size:\\s+${print ? "480 x 640" : `${expected.width} x ${expected.height}`} pts`
        ).test(info) && text.includes(print ? "Launch preview" : expected.headline) && text.includes(print ? "22 October 2026" : expected.date);
      }
    ),
    ...m.profile === "campaign" ? [
      criterion(
        "the campaign action remains editable with the approved copy, size and cyan style",
        async () => vectorText(await node("Web action")) === "Reserve your studio slot" && sameStyle(
          await node("Web action"),
          original3("Web action"),
          22,
          original3("Web date").kind.runs[0].style.fill
        )
      )
    ] : []
  ]);
}
function workflow(profile, id, title4) {
  return {
    id,
    title: title4,
    application: "vectorcraft",
    family: "vectorcraft-existing-design",
    split: profile === "formats" ? "held-out" : "development",
    inputFile: project,
    outputs: finals,
    brief: `The existing studio-campaign.vectorcraft contains CURRENT WEB, APPROVED PRINT and ARCHIVE artboards. Revise the existing design using the APPROVED / R2 Review notes beneath the web artwork; earlier R1 copy is superseded. Preserve the existing editable objects and all unrequested artwork. Do not rebuild or flatten the design. Save the editable master in place. Deliver studio-web.svg with editable text and studio-web.png at one pixel per point, both from CURRENT WEB. Deliver studio-proof.pdf as one page from ${profile === "formats" ? "APPROVED PRINT" : "CURRENT WEB"}. Check the actual deliverables before finishing.`,
    mcpGuidance: "The existing VectorCraft desktop is open. Inspect native artboards and objects, read the review notes, and discover native edit commands. Edit objects by stable identity and export the explicitly requested artboards. Preserve editable type and protected regions.",
    prepare: (work) => prepareVector(profile, work),
    verify: verifyVector
  };
}
var vectorReview = workflow(
  "copy",
  "vectorcraft-approved-copy",
  "Revise an existing campaign from approved review notes"
);
var vectorSponsors = workflow(
  "sponsors",
  "vectorcraft-sponsor-repair",
  "Repair an existing sponsor layout without rebuilding the artwork"
);
var vectorFormats = workflow(
  "formats",
  "vectorcraft-web-print-delivery",
  "Revise web artwork while preserving an approved print proof"
);
var vectorCampaign = workflow(
  "campaign",
  "vectorcraft-campaign-component",
  "Apply a signed campaign approval across existing web artwork"
);
import { mkdtemp as mkdtemp4, rm as rm4 } from "node:fs/promises";
import { resolve as resolve11 } from "node:path";
async function svgMatchesNativeDelivery(ctx) {
  const temp = await mkdtemp4(resolve11(ctx.work, ".svg-verify-")), raster = resolve11(temp, "delivered-svg.png");
  try {
    await withApplication("vectorcraft", ctx.work, async (c) => {
      await call(c, "open_file", { path: resolve11(ctx.work, "studio-web.svg") });
      await call(c, "export", { path: raster, artboard: 0 });
    });
    const pixels4 = async (path) => (await execute(
      "ffmpeg",
      [
        "-v",
        "error",
        "-threads",
        "1",
        "-i",
        path,
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-threads",
        "1",
        "pipe:1"
      ],
      { encoding: "buffer", timeout: 15e3, maxBuffer: 2 * 1024 * 1024 }
    )).stdout;
    const a = await pixels4(raster), b = await pixels4(resolve11(ctx.work, "studio-web.png"));
    if (a.length !== 800 * 400 * 3 || b.length !== a.length) return false;
    for (let y = 0; y < 400; y += 16)
      for (let x = 0; x < 800; x += 16) {
        let difference = 0;
        for (let dy = 0; dy < 16; dy++)
          for (let dx = 0; dx < 16; dx++)
            for (let channel = 0; channel < 3; channel++) {
              const index = ((y + dy) * 800 + x + dx) * 3 + channel;
              difference += Math.abs(a[index] - b[index]);
            }
        if (difference / (16 * 16 * 3) > 2) return false;
      }
    return true;
  } finally {
    await rm4(temp, { recursive: true, force: true });
  }
}
import { mkdtemp as mkdtemp5, readFile as readFile8, rm as rm5, writeFile as writeFile2 } from "node:fs/promises";
import { posix as posix2, resolve as resolve13 } from "node:path";
import { isDeepStrictEqual as isDeepStrictEqual4 } from "node:util";
import { unzipSync as unzipSync4, strFromU8 as strFromU84 } from "fflate";
import { XMLParser as XMLParser3 } from "fast-xml-parser";
import { readFile as readFile7 } from "node:fs/promises";
import { resolve as resolve12, posix } from "node:path";
import { unzipSync as unzipSync3, strFromU8 as strFromU83 } from "fflate";
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
  const zip = unzipSync3(await readFile7(path));
  const parser = new XMLParser2({ ignoreAttributes: false });
  const model = parser.parse(strFromU83(zip["ppt/presentation.xml"]))["p:presentation"];
  const rels = array(
    parser.parse(strFromU83(zip["ppt/_rels/presentation.xml.rels"]))["Relationships"]["Relationship"]
  );
  const ids = array(model["p:sldIdLst"]?.["p:sldId"]);
  return ids.map((id) => {
    const target3 = rels.find((r) => r["@_Id"] === id["@_r:id"])?.["@_Target"];
    if (!target3) throw new Error("Missing slide relationship: " + id["@_r:id"]);
    const name = posix.normalize(
      target3.startsWith("/") ? target3.slice(1) : posix.join("ppt", target3)
    );
    if (!name.startsWith("ppt/slides/") || !zip[name])
      throw new Error("Invalid slide target: " + target3);
    const raw = strFromU83(zip[name]);
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
      for (const [title4, body] of contents) {
        await call(client, "add_slide", { layout: "blank" });
        await call(client, "add_text_box", {
          x: 60,
          y: 40,
          w: 840,
          h: 90,
          text: title4,
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
        path: resolve12(work, "partner-update.pptx")
      });
    });
    return { footer: footer2, finalText };
  },
  async verify(context) {
    const read2 = () => slides(resolve12(context.work, "partner-update.pptx"));
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
          return finalText.slice(2, 5).every((s2) => t.includes(s2)) && !d.some((s2) => texts(s2.raw).some((v) => v.includes("87%")));
        }
      ),
      criterion(
        "risk description retained with owner and deadline",
        async () => {
          const d = await read2(), t = texts(d[2].raw);
          return ["Camera calibration", finalText[5], finalText[6]].every(
            (s2) => t.includes(s2)
          );
        }
      ),
      criterion("new final slide contains both handoff actions", async () => {
        const d = await read2(), t = texts(d[3].raw);
        return finalText.slice(7).every((s2) => t.includes(s2)) && !t.includes("Owner: TBC");
      }),
      criterion(
        "all slides preserve the branded footer",
        async () => (await read2()).every((s2) => texts(s2.raw).includes(footer2))
      ),
      criterion(
        "dark-blue 36 pt title styling retained",
        async () => (await read2()).every(
          (s2) => /sz="3600"/.test(s2.raw) && /(?:srgbClr|color)[^>]*val="1[fF]4[eE]78"/.test(s2.raw)
        )
      ),
      criterion(
        "PDF has four pages",
        async () => /Pages:\s+4\b/.test(
          (await execute(
            "pdfinfo",
            [resolve12(context.work, "partner-update.pdf")],
            { timeout: 3e4 }
          )).stdout
        )
      ),
      criterion("PDF contains all final briefing content", async () => {
        const t = (await pdfText(resolve12(context.work, "partner-update.pdf"))).replace(/\s+/g, " ");
        return finalText.every((s2) => t.includes(s2)) && !t.includes("87%");
      })
    ]);
  }
};
function proportionalTypeMatches(before, after, scale3) {
  const a = before.content, b = after.content, ma = a.transform?.m, mb = b.transform?.m;
  if (a.kind !== "text" || b.kind !== "text" || !ma || !mb || ma.length !== 6 || mb.length !== 6 || !a.size_pt || !b.size_pt || a.font_family !== b.font_family || !isDeepStrictEqual4(a.color, b.color))
    return false;
  return [0, 1, 2, 3].every(
    (i) => Math.abs(ma[i] * a.size_pt * scale3 - mb[i] * b.size_pt) <= 2e-3
  ) && [4, 5].every((i) => Math.abs(ma[i] * scale3 - mb[i]) <= 2e-3);
}
var photoDocument = async (work) => (await manifest(resolve13(work, "pilot-banner.pcraft"))).document;
var factText = (text) => canonicalText(text).toLowerCase().replace(/[×]/g, "x").replace(/(\d+)\.0+(?=\D|$)/g, "$1").replace(/\s*([:;,()x])\s*/g, "$1").replace(/[’]/g, "'");
function publicationGateMatches(text, owner, kind, implicitOwner = false) {
  const t = factText(text).replace(/sign[ -]?off/g, "signoff"), person = factText(owner), discipline = factText(kind);
  if (!new RegExp(`\\b${discipline}\\s+signoff\\b`).test(t) || !/\bpending\b|\bawait(?:ing|s)?\b/.test(t))
    return false;
  if (/\b(?:not|never|no longer|isn't)(?:\s+(?:currently|still|actually|really|presently|yet|genuinely)){0,3}\s+(?:pending|awaiting|awaits)\b|\b(?:completed|complete|granted|approved)\b|publish (?:now|immediately)|publication (?:now|immediately)/.test(
    t
  ))
    return false;
  if (/\b(?:no|without)\s+(?:(?:creative|sales|language|campaign)\s+)?(?:signoff|approval)\b|\b(?:does not|doesn't|not|never)\s+require(?:s|d)?[^;.!?]*(?:signoff|approval)|(?:signoff|approval)\s+(?:is\s+)?(?:not|never)\s+required\b/.test(
    t
  ))
    return false;
  const named = new RegExp(`\\b${person}(?:'s)?\\b`).test(t), before = t.match(
    new RegExp(`(?:^|[:;])([^:;]*?)\\b${discipline}\\s+signoff\\b`)
  )?.[1]?.trim() ?? "", prefix = before.replace(new RegExp(`\\b${person}(?:'s)?\\b`, "g"), "").replace(
    /\b(?:gate|publication|publish|requires|awaits|await|awaiting|only|after|until|before|do|not|no|the|pending|and)\b/g,
    ""
  ).replace(/[^a-z]/g, "");
  if (prefix || !named && !implicitOwner) return false;
  for (const m of t.matchAll(
    /\b([a-z]+)'s\s+(?:(?:creative|sales|language|campaign)\s+)?(?:signoff|approval)\b/g
  ))
    if (m[1] !== person) return false;
  for (const m of t.matchAll(
    /\b(?:requires|awaits|after|before|until|by|from)\s+([a-z]+)(?:'s)?\s+(?:(?:creative|sales|language|campaign)\s+)?(?:signoff|approval)\b/g
  ))
    if (![person, "a", "an", "the", discipline].includes(m[1])) return false;
  for (const m of t.matchAll(
    /(?:signoff|approval)\s+(?:by|from)\s+([a-z]+)(?:'s)?\b/g
  ))
    if (m[1] !== person) return false;
  for (const m of t.matchAll(
    /\b(creative|sales|language|campaign)\s+(?:signoff|approval)\b/g
  ))
    if (m[1] !== discipline) return false;
  let dependency = false;
  for (const clause of t.split(/[;,.!?]+|\bbut\b|\bhowever\b|\band(?=\s+(?:publication|publish))/).map((x) => x.trim()).filter(Boolean)) {
    const prohibited = /\bno publication\b|\bdo not publish\b|\bdon't publish\b/.test(clause);
    if (/signoff.*\bafter\b.*(?:publish|publication)/.test(clause))
      return false;
    if (/(?:publish|publication).*\bbefore\b.*(?:signoff|\bit\b|approval)/.test(
      clause
    ) && !prohibited)
      return false;
    const relation = prohibited && /(?:before|until)\s+(?:it|(?:the\s+)?(?:[a-z]+(?:'s)?\s+)?(?:creative |sales |language |campaign )?signoff|approval)\b/.test(
      clause
    ) || /signoff.*(?:\bbefore\b|\bprecede(?:s)?\b).*\b(?:publication|publishing|publish)\b/.test(
      clause
    ) || /(?:publication|publish).*(?:\bafter\b|\brequires\b|\bawaits\b).*signoff/.test(
      clause
    );
    if (/\b(?:publish|publication|publishing)\b/.test(clause) && !relation && !/^(?:publication|publishing)\s+(?:is\s+)?(?:pending|awaiting approval)$/.test(
      clause
    ))
      return false;
    if (/\b(?:ignore|waive|bypass)\b.*\b(?:gate|signoff|approval)\b/.test(
      clause
    ) && !/\b(?:do not|never|no)\s+(?:ignore|waive|bypass)\b/.test(clause))
      return false;
    dependency ||= relation;
  }
  return dependency;
}
function proofFactMatches(text, expected) {
  const retention = "(?:retain(?:ed|s|ing)?|keep(?:s|ing)?|kept|preserv(?:e[ds]?|ing)|remains?)", destructive = "(?:delet(?:e[ds]?|ing)|remov(?:e[ds]?|ing)|flatten(?:ed|s|ing)?|rasteriz(?:e[ds]?|ing))";
  const identity2 = expected.replace(/^proof:\s*/i, "").split(new RegExp(`\\s+${retention}\\b`, "i"))[0], t = factText(text).replace(/\b(not|never)-(?=[a-z])/g, "$1 ");
  if (!t.includes(factText(identity2))) return false;
  const safe = t.replace(new RegExp(`\\b(?:not|never) ${destructive}\\b`, "g"), "").replace(/\bnot (?:only|merely)\b/g, "").replace(/\bnot visible\b/g, "hidden");
  return new RegExp(`\\b${retention}\\b`).test(safe) && /\beditable\b|\beditability (?:retained|preserved)\b/.test(safe) && /\b(?:hidden|invisible)\b/.test(safe) && !new RegExp(
    `\\b(?:not|never)(?:\\s+[a-z]+){0,3}\\s+(?:${retention}|editable|hidden|invisible)\\b|\\b(?:non[- ]?editable|uneditable|ineditable|${destructive}|visible)\\b`
  ).test(safe);
}
function matchesFact(text, fact2) {
  if (fact2.final.startsWith("Gate:")) {
    const expected = fact2.final.match(
      /Gate: (\w+)'s (creative|sales|language|campaign) sign-off/
    );
    if (expected) return publicationGateMatches(text, expected[1], expected[2]);
  }
  if (fact2.final.startsWith("Proof:"))
    return proofFactMatches(text, fact2.final);
  return new RegExp(fact2.pattern, "i").test(factText(text));
}
var roleNames = {
  revision: "revision",
  approval: "revision",
  campaign: "campaign",
  date: "date",
  event: "date",
  "event date": "date",
  "web date": "date",
  canvas: "canvas",
  size: "canvas",
  master: "master",
  masters: "master",
  native: "master",
  exports: "exports",
  files: "exports",
  proof: "proof",
  owner: "owner",
  responsible: "owner",
  deadline: "deadline",
  due: "deadline",
  gate: "gate",
  publication: "gate",
  composition: "composition",
  products: "composition",
  camera: "camera",
  image: "image",
  render: "render",
  geometry: "geometry",
  mesh: "geometry",
  archive: "archive",
  history: "archive",
  headline: "headline",
  title: "headline",
  action: "action",
  cta: "action",
  "call to action": "action",
  subtitle: "subtitle",
  web: "web",
  vector: "web",
  newsletter: "newsletter",
  photo: "newsletter",
  sponsors: "sponsors",
  print: "print",
  "print exception": "print",
  support: "support",
  soporte: "support"
};
function labeledFacts(lines) {
  const result = [];
  for (const line of lines)
    for (const raw of line.split(/\n|;\s*(?=[A-Za-z][A-Za-z ]{1,30}:)/).map((x) => x.trim()).filter(Boolean)) {
      const match = raw.match(/^([A-Za-z][A-Za-z ]{0,30}):\s*(.*)$/), label = match?.[1].toLowerCase(), role = label ? roleNames[label] : /^publication\s+(?:requires|awaits)\b|^(?:publish|do not publish|don't publish|await)\b/i.test(
        raw
      ) ? "gate" : void 0;
      if (!role) return void 0;
      result.push({
        role,
        label: label ?? "gate",
        value: match?.[2] ?? raw,
        raw
      });
    }
  return result;
}
var roleOf = (f) => roleNames[f.final.slice(0, f.final.indexOf(":")).toLowerCase()] ?? "";
var valueOf = (f) => f.final.slice(f.final.indexOf(":") + 1).trim();
var fileNames = (value) => [
  ...factText(value).matchAll(
    /\b[\w-]+\.(?:pcraft|blend|obj|png|jpe?g|svg|pdf)\b/g
  )
].map((m) => m[0]).sort();
var dimensionValues = (value) => [
  ...factText(value).matchAll(
    /\b(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)\s*(px|pixels|pt|points)\b/g
  )
].map(
  (m) => [
    Number(m[1]),
    Number(m[2]),
    /^p(?:t|oints)$/.test(m[3]) ? "pt" : "px"
  ].join("/")
);
var monthNames = {
  january: "01",
  enero: "01",
  february: "02",
  febrero: "02",
  march: "03",
  marzo: "03",
  april: "04",
  abril: "04",
  may: "05",
  mayo: "05",
  june: "06",
  junio: "06",
  july: "07",
  julio: "07",
  august: "08",
  agosto: "08",
  september: "09",
  septiembre: "09",
  october: "10",
  octubre: "10",
  november: "11",
  noviembre: "11",
  december: "12",
  diciembre: "12"
};
var dateValues = (value) => [
  ...factText(value).matchAll(
    /\b(\d{1,2})\s+(?:de\s+)?([a-z]+)\s+(?:de\s+)?(\d{4})\b/g
  )
].flatMap(
  (m) => monthNames[m[2]] ? [`${m[3]}-${monthNames[m[2]]}-${m[1].padStart(2, "0")}`] : []
);
var sameDates = (a, b) => {
  const expected = dateValues(b), actual = dateValues(a);
  return expected.length === 1 && actual.length === 1 && actual[0] === expected[0];
};
var sameFiles = (a, b) => isDeepStrictEqual4(fileNames(a), fileNames(b));
var sameDimensions = (a, b) => {
  const expected = dimensionValues(b);
  return expected.length > 0 && isDeepStrictEqual4(dimensionValues(a), expected);
};
var tupleValues = (value) => [
  ...factText(value).matchAll(
    /\((-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)\)/g
  )
].map((m) => m.slice(1).map(Number));
function cameraFactMatches(actual, expected) {
  const a = factText(actual), e = factText(expected), tuples = tupleValues(expected), location = a.match(/(?:location|position|at)\s*:?\s*(\([^)]*\))/)?.[1], target3 = a.match(/target\s*:?\s*(\([^)]*\))/)?.[1];
  const all = tupleValues(actual), actualLocation = location ? tupleValues(location)[0] : all[0], actualTarget = target3 ? tupleValues(target3)[0] : all[1], scale3 = a.match(/\bscale\s*:?\s*(\d+(?:\.\d+)?)/)?.[1], expectedScale = e.match(/\bscale\s*:?\s*(\d+(?:\.\d+)?)/)?.[1];
  return /\bortho(?:graphic)?\b/.test(a) && !/\bperspective\b/.test(a) && Number(scale3) === Number(expectedScale) && isDeepStrictEqual4(actualLocation, tuples[0]) && isDeepStrictEqual4(actualTarget, tuples[1]) && all.length === 2;
}
function currentVisualFactsMatch(lines, facts3, options = {}) {
  const parsed = labeledFacts(lines);
  if (!parsed) return false;
  const actual = /* @__PURE__ */ new Map(), expected = new Map(facts3.map((f) => [roleOf(f), f]));
  for (const field of parsed) {
    if (actual.has(field.role)) return false;
    actual.set(field.role, field);
  }
  const optional = {
    headline: options.headline,
    subtitle: options.subtitle,
    action: options.action
  };
  for (const field of parsed)
    if (!expected.has(field.role)) {
      const approved2 = optional[field.role];
      if (!approved2 || factText(field.value) !== factText(approved2))
        return false;
    }
  for (const [role, fact2] of expected) {
    const field = actual.get(role), e = valueOf(fact2), a = field?.value ?? "", n = factText(a);
    if (!field) {
      if (role === "date" && options.subtitle && actual.has("subtitle") && sameDates(actual.get("subtitle").value, e))
        continue;
      return false;
    }
    if (role === "gate") {
      const target3 = e.match(
        /^(\w+)'s (creative|sales|language|campaign) sign-off/
      );
      if (!target3 || !publicationGateMatches(
        field.raw,
        target3[1],
        target3[2],
        actual.get("owner")?.value.toLowerCase() === target3[1].toLowerCase()
      ))
        return false;
    } else if (role === "proof") {
      if (!proofFactMatches(field.raw, fact2.final)) return false;
    } else if (role === "owner") {
      if (n !== factText(e)) return false;
    } else if (role === "date") {
      if (!sameDates(a, e)) return false;
    } else if (role === "revision") {
      const versions = [...n.matchAll(/\br\d+\b/g)].map((m) => m[0]), required2 = factText(e).match(/\br\d+\b/)?.[0];
      if (!required2 || versions.length !== 1 || versions[0] !== required2 || !/\b(?:signed|approved)\b/.test(n) || /\b(?:unsigned|unapproved|draft)\b/.test(n) || /\bportrait\b/i.test(e) && /\blandscape\b/.test(n) || /\bspanish\b/i.test(e) && /\benglish\b/.test(n))
        return false;
    } else if (role === "camera") {
      if (!cameraFactMatches(a, e)) return false;
    } else if (role === "composition") {
      const group = factText(a + ";" + (actual.get("geometry")?.value ?? "")), staging = /\bstaging\b/.test(n) || !!options.stagingNames?.length && options.stagingNames.every(
        (name) => n.includes(name.toLowerCase())
      );
      if (!staging || !/\bapproved\b/.test(n) || !/(?:retained|unchanged|preserved|locked)/.test(n) || !group.includes("transforms") || !group.includes("materials") || /\b(?:changed|altered|replaced|unlocked)\b/.test(group))
        return false;
    } else if (role === "render") {
      const group = factText(a + ";" + (actual.get("image")?.value ?? "")), expectedSamples = factText(e).match(/\b(\d+) samples\b/)?.[1], samples = [...group.matchAll(/\b(\d+) samples\b/g)].map((m) => m[1]), percent = [...group.matchAll(/\b(\d+)%\s*resolution/g)].map(
        (m) => Number(m[1])
      );
      if (!/\bcpu\b/.test(n) || !/\bcycles\b/.test(n) || /\bgpu\b/.test(n) || samples.length !== 1 || samples[0] !== expectedSamples || !percent.length || percent.some((v) => v !== 100))
        return false;
    } else if (["canvas", "image", "web", "newsletter"].includes(role)) {
      if (!sameDimensions(a, e) || fileNames(e).length && !sameFiles(a, e))
        return false;
    } else if (role === "print") {
      if (!/approved print\s*\/\s*r1/.test(n) || !sameDates(a, e) || !sameDimensions(a, e) || !sameFiles(a, e))
        return false;
    } else if (["master", "exports", "geometry", "archive"].includes(role)) {
      if (!sameFiles(a, e)) return false;
      if (role === "master" && (!/\beditable\b/.test(n) || /\b(?:flattened|not editable)\b/.test(n)))
        return false;
      if (["geometry", "archive"].includes(role) && (!/\b(?:retained|unchanged|preserved|locked)\b/.test(n) || /\b(?:changed|altered|replaced)\b/.test(n)))
        return false;
    } else if (!matchesFact(field.raw, fact2)) return false;
  }
  return true;
}
function approvedVisualFactOptions(ctx, facts3) {
  const m = ctx.metadata, photo = m.photo, targets = photo?.expectedText, roles2 = new Map(facts3.map((f) => [roleOf(f), f])), options = {};
  if (Array.isArray(targets) && targets.length === 3 && targets.every((t) => typeof t === "string") && roles2.has("campaign") && roles2.has("date") && factText(targets[0]) === factText(valueOf(roles2.get("campaign"))) && sameDates(targets[1], valueOf(roles2.get("date")))) {
    [options.headline, options.subtitle, options.action] = targets;
  }
  const scene = m.approved_scene;
  if (scene?.objects)
    options.stagingNames = Object.entries(scene.objects).filter(
      ([name, obj]) => obj.type === "LIGHT" || obj.hidden || /^floor$/i.test(name)
    ).map(([name]) => name);
  return options;
}
function decisionFactLines(raw) {
  const parsed = new XMLParser3({
    preserveOrder: true,
    ignoreAttributes: true,
    trimValues: false
  }).parse(raw), shapes = [];
  const text = (v) => {
    if (Array.isArray(v)) return v.map(text).join("");
    if (!v || typeof v !== "object") return "";
    return Object.entries(v).map(
      ([k, x]) => k === "a:t" ? String(x[0]?.["#text"] ?? "") : k === "a:br" ? "\n" : text(x)
    ).join("");
  };
  const visit = (v) => {
    if (Array.isArray(v)) {
      v.forEach(visit);
      return;
    }
    if (!v || typeof v !== "object") return;
    for (const [k, x] of Object.entries(v)) {
      if (k === "p:sp") {
        const paragraphs2 = [];
        const collect = (o) => {
          if (Array.isArray(o)) {
            o.forEach(collect);
            return;
          }
          if (!o || typeof o !== "object") return;
          for (const [key3, value] of Object.entries(o))
            if (key3 === "a:p") paragraphs2.push(text(value));
            else collect(value);
        };
        collect(x);
        shapes.push(paragraphs2);
      } else visit(x);
    }
  };
  visit(parsed);
  return shapes.slice(1).flat().map((x) => x.trim()).filter((x) => x && x !== deckFooter);
}
async function prepareVisualRegister(work, notes3, facts3) {
  return {
    ...await prepareRegister(
      work,
      notes3,
      facts3.map((f) => f.draft),
      facts3.map((f) => f.final)
    ),
    facts: facts3
  };
}
async function verifyVisualRegister(ctx, m) {
  const lines = async () => (await docx(resolve13(ctx.work, "delivery.docx"))).numberedText.map(canonicalText).filter(Boolean);
  return Promise.all([
    criterion(
      "the signed approval source is preserved byte for byte",
      async () => digest(await readFile8(resolve13(ctx.work, "review-notes.docx"))) === m.source_sha256
    ),
    criterion(
      "all current register fields reconcile the approved facts without obsolete or duplicate entries",
      async () => {
        const fields = await registerFields(ctx.work);
        return currentVisualFactsMatch(
          fields,
          m.facts,
          approvedVisualFactOptions(ctx, m.facts)
        );
      }
    ),
    criterion(
      "the existing register title, historical record and footer are protected",
      async () => {
        const t = await lines();
        return t[0] === "Reef release delivery register" && t[1] === "Current delivery" && t.at(-1) === "Internal - retain the signed historical record." && isDeepStrictEqual4(
          t.slice(t.indexOf("Historical archive") + 1, -1),
          m.archive
        );
      }
    ),
    criterion(
      "the delivery PDF agrees with the saved register including its signed historical exception",
      async () => {
        const t = await lines(), pdf = canonicalText(
          await unwrappedPdfText(resolve13(ctx.work, "delivery.pdf"))
        );
        return t.every((line) => pdf.includes(line)) && (await registerFields(ctx.work)).every(
          (line) => pdf.slice(
            pdf.indexOf("Current delivery"),
            pdf.indexOf("Historical archive")
          ).includes(line)
        );
      }
    )
  ]);
}
async function pixels2(path, filter) {
  return (await execute(
    "ffmpeg",
    [
      "-v",
      "error",
      "-threads",
      "1",
      "-i",
      path,
      ...filter ? ["-vf", filter] : [],
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
    { encoding: "buffer", timeout: 2e4, maxBuffer: 5 * 1024 * 1024 }
  )).stdout;
}
function tiledPixelsMatch(a, b, width, height, tolerance = 3, tile = 16) {
  if (a.length !== width * height * 3 || b.length !== a.length) return false;
  for (let y = 0; y < height; y += tile)
    for (let x = 0; x < width; x += tile) {
      let sum = 0, n = 0;
      for (let dy = 0; dy < Math.min(tile, height - y); dy++)
        for (let dx = 0; dx < Math.min(tile, width - x); dx++)
          for (let ch = 0; ch < 3; ch++) {
            const i = ((y + dy) * width + x + dx) * 3 + ch;
            sum += Math.abs(a[i] - b[i]);
            n++;
          }
      if (sum / n > tolerance) return false;
    }
  return true;
}
async function imageMatches(a, b, tolerance = 3) {
  const da = await dimensions(a), db = await dimensions(b);
  return da.width === db.width && da.height === db.height && tiledPixelsMatch(
    await pixels2(a),
    await pixels2(b),
    da.width,
    da.height,
    tolerance
  );
}
async function photoExportsMatch(ctx) {
  const temp = await mkdtemp5(resolve13(ctx.work, ".visual-photo-check-"));
  try {
    const current2 = resolve13(temp, "current.png");
    await withApplication(
      "photocraft",
      ctx.work,
      async (c) => {
        await call(c, "doc_open", {
          path: resolve13(ctx.work, "pilot-banner.pcraft")
        });
        await call(c, "doc_export", { path: current2 });
      },
      ctx.env
    );
    return await imageMatches(current2, resolve13(ctx.work, "pilot-banner.png"), 1) && await imageMatches(current2, resolve13(ctx.work, "pilot-banner.jpg"), 15);
  } finally {
    await rm5(temp, { recursive: true, force: true });
  }
}
async function singlePagePdfMatches(a, b, work) {
  const temp = await mkdtemp5(resolve13(work, ".visual-pdf-pixels-"));
  try {
    for (const [source4, name] of [
      [a, "a"],
      [b, "b"]
    ]) {
      const info = await execute("pdfinfo", [source4], { timeout: 1e4 });
      if (!/^Pages:\s+1$/m.test(info.stdout)) return false;
      await execute(
        "pdftoppm",
        ["-r", "72", "-singlefile", "-png", source4, resolve13(temp, name)],
        { timeout: 2e4 }
      );
    }
    return await imageMatches(
      resolve13(temp, "a.png"),
      resolve13(temp, "b.png"),
      3
    );
  } finally {
    await rm5(temp, { recursive: true, force: true });
  }
}
var deckFile = "sales-briefing.pptx";
var deckFooter = "Reef creative operations | Internal";
async function prepareVisualDeck(work, imageFile, imageBox, facts3, theme) {
  const contents = [
    [
      theme + " / creative review",
      "Reef 2026 delivery controls; retain this introduction."
    ],
    [
      "Current visual",
      "Draft artwork: replace this preview with the final exported image."
    ],
    ["Publication decision", facts3.map((f) => f.draft).join("\n")],
    [
      "Signed R1 archive",
      "22 October 2026 | Alex | historical preview approved; retain without amendments."
    ]
  ];
  await withApplication("deckcraft", work, async (c) => {
    await call(c, "new_presentation", { blank: true });
    for (const [title4, body] of contents) {
      await call(c, "add_slide", { layout: "blank" });
      await call(c, "add_text_box", {
        x: 40,
        y: 28,
        w: 870,
        h: 60,
        text: title4,
        size: 26,
        color: "#1F4E78"
      });
      await call(c, "add_text_box", {
        x: 60,
        y: 110,
        w: 830,
        h: 340,
        text: body,
        size: 18,
        color: "#333333"
      });
      await call(c, "add_text_box", {
        x: 40,
        y: 510,
        w: 850,
        h: 20,
        text: deckFooter,
        size: 11,
        color: "#777777"
      });
    }
    await call(c, "save_presentation", { path: resolve13(work, deckFile) });
  });
  const s2 = await slides(resolve13(work, deckFile));
  if (s2.length !== 4 || !texts(s2[3].raw).includes("Signed R1 archive"))
    throw Error("Visual deck seed has unexpected slide ordering");
  return {
    protected_slides: [
      { index: 0, raw: s2[0].raw },
      { index: 3, raw: s2[3].raw }
    ],
    image_slide: 1,
    image_box: imageBox,
    image_file: imageFile,
    current_facts: facts3
  };
}
var xmlFlag = (v) => v === true || v === 1 || v === "1" || v === "true";
var boxFromTransform = (t) => {
  if (!t) return;
  const b = {
    x: Number(t["a:off"]?.["@_x"]) / 12700,
    y: Number(t["a:off"]?.["@_y"]) / 12700,
    width: Number(t["a:ext"]?.["@_cx"]) / 12700,
    height: Number(t["a:ext"]?.["@_cy"]) / 12700
  };
  return Object.values(b).every(Number.isFinite) ? b : void 0;
};
function nativeSlideSize(zip) {
  if (!zip["ppt/presentation.xml"]) return;
  const size = new XMLParser3({ ignoreAttributes: false }).parse(
    strFromU84(zip["ppt/presentation.xml"])
  )?.["p:presentation"]?.["p:sldSz"], width = Number(size?.["@_cx"]) / 12700, height = Number(size?.["@_cy"]) / 12700;
  return Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0 ? { width, height } : void 0;
}
function pictureEffectsUnchanged(blip) {
  const identity2 = {
    "a:alpha": 1e5,
    "a:alphaModFix": 1e5,
    "a:alphaRepl": 1e5,
    "a:alphaOff": 0,
    "a:lumMod": 1e5,
    "a:lumOff": 0,
    "a:satMod": 1e5,
    "a:satOff": 0,
    "a:hueMod": 1e5,
    "a:hueOff": 0,
    "a:redMod": 1e5,
    "a:redOff": 0,
    "a:greenMod": 1e5,
    "a:greenOff": 0,
    "a:blueMod": 1e5,
    "a:blueOff": 0,
    "a:shade": 1e5,
    "a:tint": 0
  };
  for (const [key3, value] of Object.entries(blip ?? {})) {
    if (key3.startsWith("@_")) continue;
    if (!(key3 in identity2)) return false;
    for (const node of array(
      value
    ))
      if (node && typeof node === "object" && Object.keys(node).some((k) => !k.startsWith("@_")) || Number(node?.["@_amt"] ?? node?.["@_val"] ?? identity2[key3]) !== identity2[key3])
        return false;
  }
  return true;
}
function embeddedPictures(zip, raw) {
  const parser = new XMLParser3({ ignoreAttributes: false }), slideName = Object.keys(zip).find(
    (k) => /^ppt\/slides\/slide\d+\.xml$/.test(k) && strFromU84(zip[k]) === raw
  );
  if (!slideName) return [];
  const relName = posix2.join(
    posix2.dirname(slideName),
    "_rels",
    posix2.basename(slideName) + ".rels"
  ), rels = array(
    parser.parse(strFromU84(zip[relName] ?? new Uint8Array()))?.Relationships?.Relationship
  ), pics = array(
    parser.parse(raw)["p:sld"]?.["p:cSld"]?.["p:spTree"]?.["p:pic"]
  );
  return pics.flatMap((pic) => {
    const fill = pic["p:blipFill"], blip = fill?.["a:blip"], rid = blip?.["@_r:embed"], rel = rels.find((r) => r["@_Id"] === rid);
    if (!rel || rel["@_TargetMode"] === "External") return [];
    const target3 = posix2.normalize(
      posix2.join(posix2.dirname(slideName), rel["@_Target"])
    ), bytes = zip[target3], t = pic["p:spPr"]?.["a:xfrm"], box2 = boxFromTransform(t);
    if (!target3.startsWith("ppt/media/") || !bytes || !box2) return [];
    const crop = fill?.["a:srcRect"], rot = Number(t["@_rot"] ?? 0), turn = 360 * 6e4;
    return [
      {
        ...box2,
        bytes,
        visible: !xmlFlag(pic["p:nvPicPr"]?.["p:cNvPr"]?.["@_hidden"]) && pictureEffectsUnchanged(blip),
        cropped: ["l", "t", "r", "b"].some(
          (k) => Number(crop?.["@_" + k] ?? 0) !== 0
        ),
        flipped: xmlFlag(t["@_flipH"]) || xmlFlag(t["@_flipV"]),
        rotated: !Number.isFinite(rot) || Math.abs((rot % turn + turn) % turn) > 1e-3
      }
    ];
  });
}
function occupiedTextFrames(raw) {
  const tree = new XMLParser3({ ignoreAttributes: false }).parse(raw)["p:sld"]?.["p:cSld"]?.["p:spTree"], shapes = array(tree?.["p:sp"]);
  return shapes.flatMap((shape) => {
    if (xmlFlag(shape["p:nvSpPr"]?.["p:cNvPr"]?.["@_hidden"])) return [];
    const content = shape["p:txBody"], containsText = (v) => {
      if (Array.isArray(v)) return v.some(containsText);
      if (!v || typeof v !== "object") return false;
      return Object.entries(v).some(
        ([k, a]) => k === "a:t" ? String(
          typeof a === "object" ? a["#text"] ?? "" : a ?? ""
        ).trim().length > 0 : containsText(a)
      );
    };
    const box2 = boxFromTransform(shape["p:spPr"]?.["a:xfrm"]);
    return content && containsText(content) && box2 ? [box2] : [];
  });
}
function displayedPictureMatches(pic, size, frames, image) {
  const epsilon = 1e-3;
  if (!pic.visible || pic.cropped || pic.flipped || pic.rotated || !Object.values(size).every((v) => Number.isFinite(v) && v > 0) || !Object.values(image).every((v) => Number.isFinite(v) && v > 0) || ![pic.x, pic.y, pic.width, pic.height].every(Number.isFinite))
    return false;
  if (pic.width < 160 || pic.height < 160 || pic.x < -epsilon || pic.y < -epsilon || pic.x + pic.width > size.width + epsilon || pic.y + pic.height > size.height + epsilon || Math.abs(pic.width / pic.height - image.width / image.height) > 0.01)
    return false;
  return frames.every(
    (f) => Math.min(pic.x + pic.width, f.x + f.width) - Math.max(pic.x, f.x) <= epsilon || Math.min(pic.y + pic.height, f.y + f.height) - Math.max(pic.y, f.y) <= epsilon
  );
}
async function deckPdfMatches(ctx) {
  const temp = await mkdtemp5(resolve13(ctx.work, ".visual-deck-check-"));
  try {
    const current2 = resolve13(temp, "current.pdf");
    await withApplication(
      "deckcraft",
      ctx.work,
      async (c) => {
        await call(c, "open_presentation", {
          path: resolve13(ctx.work, deckFile)
        });
        await call(c, "run_command", {
          command: "file.export",
          params: { path: current2, format: "pdf", range: "all" }
        });
      },
      ctx.env
    );
    const delivered = canonicalText(
      await unwrappedPdfText(resolve13(ctx.work, "sales-briefing.pdf"))
    ), actual = canonicalText(await unwrappedPdfText(current2));
    if (delivered !== actual) return false;
    const info = await execute(
      "pdfinfo",
      [resolve13(ctx.work, "sales-briefing.pdf")],
      { timeout: 1e4 }
    );
    if (!/^Pages:\s+4$/m.test(info.stdout)) return false;
    for (const [source4, prefix] of [
      [current2, "native"],
      [resolve13(ctx.work, "sales-briefing.pdf"), "delivered"]
    ])
      await execute(
        "pdftoppm",
        [
          "-r",
          "72",
          "-f",
          "2",
          "-l",
          "2",
          "-singlefile",
          "-png",
          source4,
          resolve13(temp, prefix)
        ],
        { timeout: 2e4 }
      );
    return await imageMatches(
      resolve13(temp, "native.png"),
      resolve13(temp, "delivered.png"),
      3
    );
  } finally {
    await rm5(temp, { recursive: true, force: true });
  }
}
async function verifyVisualDeck(ctx, m) {
  const read2 = () => slides(resolve13(ctx.work, deckFile));
  return Promise.all([
    criterion(
      "the original introduction, signed archive and all four slide identities/order remain",
      async () => {
        const s2 = await read2();
        return s2.length === 4 && m.protected_slides.every((old) => s2[old.index].raw === old.raw) && s2.every((slide) => texts(slide.raw).includes(deckFooter));
      }
    ),
    criterion(
      "the decision slide agrees with the approved current facts and pending publication gate",
      async () => {
        return currentVisualFactsMatch(
          decisionFactLines((await read2())[2].raw),
          m.current_facts,
          approvedVisualFactOptions(ctx, m.current_facts)
        );
      }
    ),
    criterion(
      "the current slide visibly embeds the actual delivered image at an undistorted readable placement",
      async () => {
        const s2 = await read2(), zip = unzipSync4(await readFile8(resolve13(ctx.work, deckFile))), pics = embeddedPictures(zip, s2[m.image_slide].raw), temp = await mkdtemp5(resolve13(ctx.work, ".visual-embed-check-"));
        try {
          if (pics.length !== 1) return false;
          const pic = pics[0], d = await dimensions(resolve13(ctx.work, m.image_file));
          const size = nativeSlideSize(zip);
          if (!size || !displayedPictureMatches(
            pic,
            size,
            occupiedTextFrames(s2[m.image_slide].raw),
            d
          ))
            return false;
          const file = resolve13(temp, "embedded.png");
          await writeFile2(file, pic.bytes);
          return await imageMatches(file, resolve13(ctx.work, m.image_file), 2);
        } finally {
          await rm5(temp, { recursive: true, force: true });
        }
      }
    ),
    criterion(
      "all four PDF pages match the saved briefing text and its displayed current artwork",
      () => deckPdfMatches(ctx)
    )
  ]);
}
import { strFromU8 as strFromU85, strToU8, unzipSync as unzipSync5, zipSync } from "fflate";
var fact = (draft3, final, pattern) => ({
  draft: draft3,
  final,
  pattern
});
var commonApproval = [
  "R1 / 22 October / Alex: preview approved for internal review only. The historical signed R1 must remain unchanged.",
  "R2 / 23 October 09:00 / forwarded proposal: use 24 October, flatten artwork and publish immediately. This proposal was not signed and is superseded."
];
var publicationFacts = [
  fact(
    "Revision: draft R2",
    "Revision: signed R3",
    "(?:revision|approval).*signed.*r3"
  ),
  fact(
    "Campaign: Launch preview",
    "Campaign: Partner pilot opens",
    "(?:campaign|headline).*partner pilot opens"
  ),
  fact(
    "Date: 24 October 2026",
    "Date: 23 October 2026",
    "(?:date|event).*23 october 2026"
  ),
  fact(
    "Canvas: 960 x 540 px",
    "Canvas: 1200 x 675 px",
    "(?:canvas|size).*1200x675\\s*(?:px|pixels)"
  ),
  fact(
    "Masters: flattened preview",
    "Master: pilot-banner.pcraft; separate editable type layers",
    "(?:master|native).*pilot-banner\\.pcraft.*editable"
  ),
  fact(
    "Exports: reference.png",
    "Exports: pilot-banner.png; pilot-banner.jpg",
    "(?:exports|files).*pilot-banner\\.png.*pilot-banner\\.jpg"
  ),
  fact(
    "Proof: delete INTERNAL PROOF",
    "Proof: INTERNAL PROOF retained as a hidden editable layer",
    "(?:proof).*internal proof.*(?:hidden|invisible).*editable"
  ),
  fact("Owner: Alex", "Owner: Maya", "(?:owner|responsible).*maya"),
  fact(
    "Deadline: 24 October 2026, 09:00 UTC",
    "Deadline: 23 October 2026, 18:00 UTC",
    "(?:deadline|due).*23 october 2026.*18:00\\s*utc"
  ),
  fact(
    "Gate: publish immediately",
    "Gate: Maya's creative sign-off before publication; pending",
    "(?:gate|publication).*maya.*creative sign-off.*before publication.*pending"
  )
];
var portraitFacts = [
  fact(
    "Revision: landscape R1",
    "Revision: approved portrait R3",
    "(?:revision|approval).*portrait.*r3"
  ),
  fact(
    "Composition: rebuild products",
    "Composition: approved product transforms, materials and staging retained",
    "(?:composition|products).*transforms.*materials.*(?:staging|retained)"
  ),
  fact(
    "Camera: perspective, 52 mm",
    "Camera: orthographic; scale 12; (0,-10,6); target (0,0,1)",
    "camera.*orthographic.*scale\\s*12.*\\(0,-10,6\\).*\\(0,0,1\\)"
  ),
  fact(
    "Image: landscape 640 x 360",
    "Image: product-hero.png; 360 x 640 px",
    "(?:image|render).*product-hero\\.png.*360x640\\s*(?:px|pixels)"
  ),
  fact(
    "Render: unverified preview",
    "Render: CPU Cycles; 16 samples; 100% resolution",
    "(?:render).*cpu.*cycles.*16 samples.*100%"
  ),
  fact(
    "Geometry: replace the OBJ",
    "Geometry: product-variants.obj retained unchanged",
    "(?:geometry|mesh).*product-variants\\.obj.*(?:unchanged|retained)"
  ),
  fact(
    "Archive: replace landscape proof",
    "Archive: approved-landscape.png retained unchanged",
    "(?:archive|history).*approved-landscape\\.png.*(?:unchanged|retained)"
  ),
  fact("Owner: Alex", "Owner: Imani", "(?:owner|responsible).*imani"),
  fact(
    "Gate: publish draft",
    "Gate: Imani's sales sign-off before publication; pending",
    "(?:gate|publication).*imani.*sales sign-off.*before publication.*pending"
  )
];
var localizationFacts = [
  fact(
    "Revision: unsigned English R2",
    "Revision: signed Spanish R3",
    "(?:revision|approval).*spanish.*r3"
  ),
  fact(
    "Headline: Launch preview",
    "Headline: Estudio abierto Reef",
    "(?:headline|title).*estudio abierto reef"
  ),
  fact(
    "Web date: 24 October 2026",
    "Web date: 23 de octubre de 2026",
    "(?:web date|event).*23 de octubre de 2026"
  ),
  fact(
    "Action: Join the waitlist",
    "Action: Reserva tu plaza",
    "(?:action|cta).*reserva tu plaza"
  ),
  fact(
    "Web: 640 x 360 preview",
    "Web: 800 x 400 px; studio-web.svg; studio-web.png",
    "(?:web|vector).*800x400\\s*(?:px|pixels).*studio-web\\.svg.*studio-web\\.png"
  ),
  fact(
    "Newsletter: stretched 1200 x 675",
    "Newsletter: 960 x 720 px; pilot-banner.pcraft; pilot-banner.png; pilot-banner.jpg",
    "(?:newsletter|photo).*960x720\\s*(?:px|pixels).*pilot-banner\\.pcraft.*pilot-banner\\.png.*pilot-banner\\.jpg"
  ),
  fact(
    "Sponsors: Pending Co.; Northbridge; Reef Labs",
    "Sponsors: Reef Labs first; Northbridge second; Pending Co. retired",
    "sponsors.*reef labs.*(?:first|1).*northbridge.*(?:second|2).*pending co.*retired"
  ),
  fact(
    "Print: translate and update date",
    "Print exception: APPROVED PRINT / R1; 22 October 2026; 480 x 640 pt; studio-proof.pdf",
    "print.*approved print\\s*/\\s*r1.*22 october 2026.*480x640\\s*(?:pt|points).*studio-proof\\.pdf"
  ),
  fact(
    "Support: none",
    "Support: Soporte: support@reef.example",
    "(?:support|soporte).*support@reef\\.example"
  ),
  fact("Owner: Alex", "Owner: Sofia", "(?:owner|responsible).*sofia"),
  fact(
    "Gate: publish immediately",
    "Gate: Sofia's language sign-off before publication; pending",
    "(?:gate|publication).*sofia.*language sign-off.*before publication.*pending"
  )
];
var registerBrief = "Update the existing Current delivery fields in delivery.docx from the signed R3 source. Keep its title, signed historical archive and footer. Record every requested field exactly once; wording, field order and decimal precision may vary if the facts and units remain unambiguous. Save in place and export delivery.pdf. Preserve review-notes.docx byte for byte.";
var deckBrief = "In the existing four-slide sales-briefing.pptx retain the introduction and signed R1 archive without amendment. Replace the Current visual placeholder with the actual final PNG as one visible, undistorted embedded picture inside the slide. Update the Publication decision slide from R3, including all current register facts and the still-pending sign-off; do not claim approval was granted. Keep all four slides in their existing order and the original footer. Save in place and export all four pages to sales-briefing.pdf. Inspect the resulting image and PDFs.";
var guidance = "Use the assigned native application tools and existing projects. Inspect IDs before editing. Office and Craft tools act on the connected native desktop; Blender tools save the persisted scene. Read approval notes and preserve protected history. Use native save/export and picture insertion; do not fabricate files or replace editable documents.";
var publication = {
  id: "photocraft-publication-approval-pack",
  title: "Resolve a proof approval and deliver a faithful campaign publication pack",
  application: "photocraft",
  applications: ["photocraft", "wordcraft", "deckcraft"],
  family: "daily-long-review",
  split: "development",
  deadlineSeconds: 2700,
  mcpMode: "live",
  inputFile: "pilot-banner.pcraft",
  views: [
    { application: "photocraft", inputFile: "pilot-banner.pcraft" },
    { application: "wordcraft", inputFile: "review-notes.docx" },
    { application: "deckcraft", inputFile: "sales-briefing.pptx" }
  ],
  outputs: [
    ...proofVisibility.outputs,
    "delivery.docx",
    "delivery.pdf",
    "sales-briefing.pptx",
    "sales-briefing.pdf"
  ],
  brief: [
    "Finish the existing editable newsletter artwork and its office handoff using the signed R3 in review-notes.docx; the forwarded R2 is obsolete.",
    proofVisibility.brief,
    "R3 assigns Maya; due 23 October 2026 at 18:00 UTC. Creative sign-off from Maya remains pending and is required before publication. The delivery must retain that gate.",
    registerBrief,
    deckBrief
  ].join("\n"),
  mcpGuidance: guidance,
  async prepare(work, env) {
    const photo = await proofVisibility.prepare(work, env), original3 = await photoDocument(work);
    const register = await prepareVisualRegister(
      work,
      [
        "Reef newsletter approval log",
        ...commonApproval,
        "SIGNED R3 / 23 October 11:00 / Maya: Partner pilot opens. Subtitle Reef 5.4 - 23 October 2026. Reserve your pilot slot. Resize proportionally to 1200 x 675 px. Retain Brand, background and all editable layer identities. INTERNAL PROOF must remain editable but hidden. Deliver pilot-banner.pcraft, PNG and JPEG. Maya owns the pack, deadline 23 October 2026 at 18:00 UTC. Maya's creative sign-off remains pending; no publication before it.",
        "The briefing must display the actual exported current PNG; a filename or old preview is insufficient."
      ],
      publicationFacts
    );
    const deck = await prepareVisualDeck(
      work,
      "pilot-banner.png",
      [80, 130, 640, 360],
      publicationFacts,
      "Reef newsletter"
    );
    return {
      visual_kind: "publication",
      photo,
      photo_original: original3,
      register,
      deck
    };
  },
  async verify(ctx) {
    const m = ctx.metadata;
    return [
      ...await proofVisibility.verify({ ...ctx, metadata: m.photo }),
      await criterion(
        "the original type composition and brand styling scale proportionally with the canvas",
        async () => {
          const d = await photoDocument(ctx.work);
          return m.photo_original.layers.filter((l) => l.content.kind === "text").every((old) => {
            const now = d.layers.find((l) => l.id === old.id);
            return now && proportionalTypeMatches(old, now, 1.25);
          });
        }
      ),
      await criterion(
        "both public raster exports reconcile the saved layered master, including proof visibility",
        () => photoExportsMatch(ctx)
      ),
      ...await verifyVisualRegister(ctx, m.register),
      ...await verifyVisualDeck(ctx, m.deck)
    ];
  }
};
var portrait = {
  id: "blender-portrait-sales-pack",
  title: "Reshoot an approved product scene for a portrait sales catalog and gated briefing",
  application: "blender",
  applications: ["blender", "wordcraft", "deckcraft"],
  family: "daily-long-review",
  split: "held-out",
  deadlineSeconds: 2700,
  mcpMode: "mixed",
  inputFile: "product-hero.blend",
  views: [
    { application: "blender", inputFile: "product-hero.blend" },
    { application: "wordcraft", inputFile: "review-notes.docx" },
    { application: "deckcraft", inputFile: "sales-briefing.pptx" }
  ],
  outputs: [
    "product-hero.blend",
    "product-hero.png",
    "product-variants.obj",
    "approved-landscape.png",
    "delivery.docx",
    "delivery.pdf",
    "sales-briefing.pptx",
    "sales-briefing.pdf"
  ],
  brief: [
    "Prepare the approved two-product scene for a portrait catalog slot. Read the signed R3 in review-notes.docx; the products have already passed product approval. Preserve both product meshes, their transforms, independent materials and all staging (Floor, ArchiveSample, Key and Fill). Keep the archive excluded from rendering. Do not add objects or recut approved geometry.",
    "Reframe using the active orthographic camera at (0,-10,6), target (0,0,1), orthographic scale 12. Render product-hero.png at 360 x 640, 100% resolution, CPU Cycles, 16 samples. Save those settings in product-hero.blend. The existing product-variants.obj and approved-landscape.png are signed prior deliveries: preserve them byte for byte.",
    "R3 assigns Imani. Sales sign-off is still pending and required before publication. The archive records R1 and must keep its historical date/owner.",
    registerBrief,
    deckBrief
  ].join("\n"),
  mcpGuidance: guidance,
  async prepare(work, env) {
    await productHero.prepare(work, env);
    await withApplication("blender", work, (c) => solveHero(c, work));
    await unlink2(resolve14(work, "product-hero.blend1")).catch(
      (error) => {
        if (error.code !== "ENOENT") throw error;
      }
    );
    await copyFile(
      resolve14(work, "product-hero.png"),
      resolve14(work, "approved-landscape.png")
    );
    const approved_scene = await sceneState(work, { geometry: true });
    const retained_files = {};
    for (const file of ["product-variants.obj", "approved-landscape.png"])
      retained_files[file] = digest(await readFile9(resolve14(work, file)));
    const register = await prepareVisualRegister(
      work,
      [
        "Reef portrait catalog approval log",
        ...commonApproval,
        "SIGNED R3 / 23 October 11:30 / Imani: products, transforms, independent teal/coral materials and approved staging are locked. Only reframe the active camera: orthographic, scale 12, location (0,-10,6), target (0,0,1). Portrait product-hero.png is 360 x 640 px, 100% resolution, CPU Cycles, 16 samples. Save the native scene. Preserve signed product-variants.obj and approved-landscape.png byte for byte. Imani's sales sign-off is pending and required before publication.",
        "The sales deck must embed the new portrait PNG, not stretch the landscape proof. Record the unchanged signed mesh and landscape exception in the delivery register."
      ],
      portraitFacts
    );
    const deck = await prepareVisualDeck(
      work,
      "product-hero.png",
      [380, 100, 216, 384],
      portraitFacts,
      "Reef portrait catalog"
    );
    return {
      visual_kind: "portrait",
      approved_scene,
      retained_files,
      register,
      deck
    };
  },
  async verify(ctx) {
    const m = ctx.metadata;
    return [
      ...await verifyPortraitScene(ctx, m.approved_scene, m.retained_files),
      ...await verifyVisualRegister(ctx, m.register),
      ...await verifyVisualDeck(ctx, m.deck)
    ];
  }
};
async function verifyPortraitScene(ctx, approvedScene, retainedFiles) {
  const state = sceneState(ctx.work, { geometry: true });
  return Promise.all([
    criterion(
      "all approved product geometry, transforms, independent materials and staging are unchanged",
      async () => {
        const now = await state, old = approvedScene;
        return Object.keys(now.objects).sort().join(",") === Object.keys(old.objects).sort().join(",") && Object.keys(old.objects).filter((name) => name !== "Camera").every(
          (name) => isDeepStrictEqual5(now.objects[name], old.objects[name])
        );
      }
    ),
    criterion(
      "the active camera has the approved orthographic portrait framing",
      async () => {
        const c = (await state).camera;
        return c.projection === "ORTHO" && closeTo(c.ortho_scale, 12) && vectorClose(c.location, [0, -10, 6]) && c.aim > 0.9999;
      }
    ),
    criterion(
      "the saved scene uses the portrait resolution and bounded CPU render settings",
      async () => {
        const d = await state;
        return d.render_engine === "CYCLES" && d.device === "CPU" && d.samples === 16 && vectorClose(d.resolution, [360, 640, 100]);
      }
    ),
    criterion(
      "the signed landscape proof and exported product geometry are preserved byte for byte",
      async () => {
        for (const [file, sha2] of Object.entries(retainedFiles))
          if (digest(await readFile9(resolve14(ctx.work, file))) !== sha2)
            return false;
        return true;
      }
    ),
    criterion(
      "the portrait PNG reconciles an independent render of the actual saved scene",
      () => portraitRenderMatches(ctx)
    )
  ]);
}
async function portraitRenderMatches(ctx) {
  const d = await dimensions(resolve14(ctx.work, "product-hero.png"));
  if (d.width !== 360 || d.height !== 640) return false;
  const temp = await mkdtemp6(resolve14(ctx.work, ".visual-portrait-check-")), output = resolve14(temp, "current.png");
  try {
    await new NativeEngine("blender").run(
      [
        "bpy.ops.wm.open_mainfile(filepath=p['path']); s=bpy.context.scene",
        "s.render.threads_mode='FIXED'; s.render.threads=2; s.render.filepath=p['output']; s.render.image_settings.file_format='PNG'",
        "bpy.ops.render.render(write_still=True); result={'rendered':True}"
      ],
      { path: resolve14(ctx.work, "product-hero.blend"), output }
    );
    return await imageMatches(output, resolve14(ctx.work, "product-hero.png"), 4);
  } finally {
    await rm6(temp, { recursive: true, force: true });
  }
}
var localized = {
  id: "localized-artwork-print-handoff",
  title: "Localize linked web and newsletter artwork while preserving a signed print exception",
  application: "photocraft",
  applications: ["photocraft", "vectorcraft", "wordcraft"],
  family: "daily-long-review",
  split: "development",
  deadlineSeconds: 2700,
  mcpMode: "live",
  inputFile: "pilot-banner.pcraft",
  views: [
    { application: "photocraft", inputFile: "pilot-banner.pcraft" },
    { application: "vectorcraft", inputFile: "studio-campaign.vectorcraft" },
    { application: "wordcraft", inputFile: "review-notes.docx" }
  ],
  outputs: [
    "pilot-banner.pcraft",
    "pilot-banner.png",
    "pilot-banner.jpg",
    ...vectorCampaign.outputs,
    "delivery.docx",
    "delivery.pdf"
  ],
  brief: [
    "Use the signed Spanish R3 source in review-notes.docx to localize both existing newsletter and web masters. R2's English publication proposal and 24 October date are obsolete. Preserve all source layer/object identities except the explicitly retired Pending Co. badge and label.",
    "Newsletter: extend pilot-banner.pcraft from 960 x 540 to 960 x 720 by adding canvas at the bottom, anchored top-left, without rescaling or repositioning existing artwork. Preserve the Brand, background and original text styles/positions. Replace Headline with Estudio abierto Reef, Subtitle with 23 de octubre de 2026 and Call to action with Reserva tu plaza. Retain INTERNAL PROOF as a hidden editable layer. Add one separate editable Support caption: Soporte: support@reef.example, 24 pt cyan #67E8F9 with baseline (60,650) and no scale/skew. Save in place; export pilot-banner.png and pilot-banner.jpg.",
    "Web: update only CURRENT WEB / R1 in studio-campaign.vectorcraft to 800 x 400 without scaling existing text. Expand its background. Use Estudio abierto Reef, 23 de octubre de 2026 and Reserva tu plaza; CTA 22 pt cyan #67E8F9. Place Reef Labs first at badge/text x=40/54 and Northbridge second at x=240/254, keep y positions, and remove Pending Co. badge/text. Export this artboard as studio-web.svg and studio-web.png. Keep APPROVED PRINT / R1 at 480 x 640 pt with the historical English 22 October 2026 artwork; export that artboard as studio-proof.pdf. Preserve ARCHIVE / 2025, Review notes and every other protected object/style.",
    "Owner Sofia; language sign-off is pending and must precede publication. The print exception remains English and dated 22 October even though both current Spanish deliveries say 23 de octubre de 2026.",
    registerBrief,
    "Review all native masters and delivered image/PDF files; filenames alone are insufficient evidence."
  ].join("\n"),
  mcpGuidance: guidance,
  async prepare(work, env) {
    const photo = await proofVisibility.prepare(work, env), original3 = await photoDocument(work), vector = await vectorCampaign.prepare(work, env);
    const register = await prepareVisualRegister(
      work,
      [
        "Reef Spanish localization approval log",
        ...commonApproval,
        "SIGNED SPANISH R3 / 23 October 12:00 / Sofia: Headline Estudio abierto Reef; event 23 de octubre de 2026; action Reserva tu plaza. Both current web and newsletter must use this approved translation. Keep REEF unchanged. Newsletter canvas extends down to 960 x 720 px without rescaling/moving existing type. Hide but retain editable INTERNAL PROOF. Add Soporte: support@reef.example at baseline (60,650), 24 pt cyan #67E8F9.",
        "Web is 800 x 400 px; sponsor order Reef Labs, Northbridge; retire Pending Co. CTA 22 pt cyan. The signed English APPROVED PRINT / R1 stays 480 x 640 pt, dated 22 October 2026, unchanged. Preserve ARCHIVE / 2025 and original in-artwork Review notes as historical evidence. Deliver native masters, newsletter PNG/JPEG, studio-web.svg/PNG and studio-proof.pdf.",
        "Sofia owns localization. Sofia's language sign-off remains pending and is required before publication. Record the print exception explicitly."
      ],
      localizationFacts
    );
    return {
      visual_kind: "localized",
      photo,
      photo_original: original3,
      vector,
      register
    };
  },
  async verify(ctx) {
    const m = ctx.metadata;
    return [
      ...await verifyLocalizedPhoto(ctx, m),
      ...await verifyLocalizedVector(ctx, m),
      await criterion(
        "both newsletter raster exports reconcile the saved Spanish layered master",
        () => photoExportsMatch(ctx)
      ),
      await criterion(
        "the editable SVG reconciles the actual delivered Spanish web PNG",
        () => svgMatchesNativeDelivery(ctx)
      ),
      ...await verifyVisualRegister(ctx, m.register)
    ];
  }
};
var translated = {
  Headline: "Estudio abierto Reef",
  Subtitle: "23 de octubre de 2026",
  "Call to action": "Reserva tu plaza"
};
async function verifyLocalizedPhoto(ctx, m) {
  const read2 = () => photoDocument(ctx.work);
  return Promise.all([
    criterion(
      "the newsletter has an extended 960x720 canvas without resizing its source type",
      async () => {
        const d = await read2();
        return d.size.width === 960 && d.size.height === 720 && m.photo_original.layers.filter((l) => l.content.kind === "text").every((old) => {
          const now = d.layers.find((l) => l.id === old.id);
          if (!now) return false;
          return isDeepStrictEqual5(
            now.content.transform,
            old.content.transform
          ) && now.content.size_pt === old.content.size_pt && isDeepStrictEqual5(now.content.color, old.content.color);
        });
      }
    ),
    criterion(
      "all three Spanish newsletter messages remain in the original editable visible layers",
      async () => {
        const d = await read2();
        return Object.entries(translated).every(([name, text]) => {
          const old = m.photo_original.layers.find((l) => l.name === name), now = d.layers.find((l) => l.id === old?.id);
          return now?.visible && now.content.kind === "text" && now.content.text === text;
        });
      }
    ),
    criterion(
      "the brand and proof identities are retained, with proof hidden and original text unchanged",
      async () => {
        const d = await read2();
        return ["Brand", "Proof"].every((name) => {
          const old = m.photo_original.layers.find((l) => l.name === name), now = d.layers.find((l) => l.id === old.id);
          return now && now.content.text === old.content.text && now.content.kind === old.content.kind && (name === "Proof" ? !now.visible : now.visible);
        });
      }
    ),
    criterion(
      "one editable support caption has the approved Spanish content, cyan style and baseline",
      async () => {
        const d = await read2(), support = d.layers.filter((l) => l.name === "Support caption"), s2 = support[0], c = s2?.content.color;
        return support.length === 1 && s2.visible && s2.content.text === "Soporte: support@reef.example" && s2.content.kind === "text" && closeTo(s2.content.size_pt, 24) && vectorClose(s2.content.transform?.m, [1, 0, 0, 1, 60, 650]) && c?.alpha === 1 && vectorClose(c.c?.slice(0, 3), [103 / 255, 232 / 255, 249 / 255]);
      }
    ),
    criterion(
      "all newsletter source layers remain and only the support caption is added",
      async () => {
        const d = await read2(), old = m.photo_original.layers;
        return d.layers.length === old.length + 1 && old.every((l) => d.layers.some((n) => n.id === l.id));
      }
    )
  ]);
}
async function verifyLocalizedVector(ctx, m) {
  const read2 = async () => JSON.parse(
    await readFile9(
      resolve14(ctx.work, "studio-campaign.vectorcraft"),
      "utf8"
    )
  ), meta = m.vector, originalNodes = vectorNodes(meta.original), nodes = async () => vectorNodes(await read2()), byKey = async (key3) => (await nodes()).find((n) => n.id === meta.ids[key3]);
  const editable2 = new Set(
    [
      "Web headline",
      "Web date",
      "Web action",
      "Web background",
      "Reef Labs badge",
      "Reef Labs label",
      "Northbridge badge",
      "Northbridge label",
      "Pending Co. badge",
      "Pending Co. label"
    ].map((k) => meta.ids[k])
  );
  return Promise.all([
    criterion(
      "the web canvas is 800x400 while signed print and archive artboards remain unchanged",
      async () => {
        const d = await read2();
        return d.document.artboards[0].rect.x1 - d.document.artboards[0].rect.x0 === 800 && d.document.artboards[0].rect.y1 - d.document.artboards[0].rect.y0 === 400 && isDeepStrictEqual5(
          d.document.artboards.slice(1),
          meta.original.document.artboards.slice(1)
        );
      }
    ),
    criterion(
      "the three Spanish web messages use their original editable text objects and positions",
      async () => {
        return (await Promise.all(
          [
            ["Web headline", "Estudio abierto Reef"],
            ["Web date", "23 de octubre de 2026"],
            ["Web action", "Reserva tu plaza"]
          ].map(async ([key3, text]) => {
            const n = await byKey(key3), old = originalNodes.find((n2) => n2.id === meta.ids[key3]);
            return n && old && vectorText(n) === text && n.kind.type === "text" && n.kind.runs?.every(
              (r) => isDeepStrictEqual5(r.style, {
                ...old.kind.runs[0].style,
                ...key3 === "Web action" ? {
                  size: 22,
                  fill: originalNodes.find(
                    (n2) => n2.id === meta.ids["Web date"]
                  ).kind.runs[0].style.fill
                } : {}
              })
            ) && isDeepStrictEqual5(n.kind.xf, old.kind.xf);
          })
        )).every(Boolean);
      }
    ),
    criterion(
      "the web CTA retains the approved 22pt cyan style and the expanded background fills the artboard",
      async () => {
        const action = await byKey("Web action"), bg = await byKey("Web background");
        return action?.kind.runs?.every(
          (r) => r.style.size === 22 && isDeepStrictEqual5(
            r.style.fill,
            originalNodes.find((n) => n.id === meta.ids["Web date"]).kind.runs[0].style.fill
          )
        ) && vectorClose(vectorBounds(bg), [0, 0, 800, 400]);
      }
    ),
    criterion(
      "the two retained sponsor badges and labels have the approved order and the pending sponsor is retired",
      async () => {
        const n = await nodes();
        return !n.some(
          (n2) => [
            meta.ids["Pending Co. badge"],
            meta.ids["Pending Co. label"]
          ].includes(n2.id)
        ) && (await Promise.all(
          [
            ["Reef Labs", 40, 54],
            ["Northbridge", 240, 254]
          ].map(async ([name, badgeX, labelX]) => {
            const b = await byKey(name + " badge"), l = await byKey(name + " label"), oldB = originalNodes.find(
              (n2) => n2.id === meta.ids[name + " badge"]
            ), oldL = originalNodes.find(
              (n2) => n2.id === meta.ids[name + " label"]
            );
            return b && l && oldB && oldL && closeTo(vectorBounds(b)[0], Number(badgeX)) && closeTo(l.kind.xf?.[4], Number(labelX)) && closeTo(vectorBounds(b)[1], vectorBounds(oldB)[1]) && closeTo(l.kind.xf?.[5], oldL.kind.xf?.[5]) && vectorText(l) === name && isDeepStrictEqual5(l.kind.runs, oldL.kind.runs) && isDeepStrictEqual5(b.appearance, oldB.appearance) && closeTo(
              vectorBounds(b)[2] - vectorBounds(b)[0],
              vectorBounds(oldB)[2] - vectorBounds(oldB)[0]
            ) && closeTo(
              vectorBounds(b)[3] - vectorBounds(b)[1],
              vectorBounds(oldB)[3] - vectorBounds(oldB)[1]
            );
          })
        )).every(Boolean);
      }
    ),
    criterion(
      "signed print, 2025 archive, brand and historical artwork notes remain untouched with no replacement objects",
      async () => {
        const n = await nodes(), removed = [
          meta.ids["Pending Co. badge"],
          meta.ids["Pending Co. label"]
        ];
        return n.length === originalNodes.length - 2 && originalNodes.filter((old) => !removed.includes(old.id)).every((old) => {
          const now = n.find((n2) => n2.id === old.id);
          return now && (editable2.has(old.id) || isDeepStrictEqual5(
            {
              ...now,
              kind: {
                ...now.kind,
                children: now.kind.children?.map((n2) => n2.id).filter((id) => !removed.includes(id))
              }
            },
            {
              ...old,
              kind: {
                ...old.kind,
                children: old.kind.children?.map((n2) => n2.id).filter((id) => !removed.includes(id))
              }
            }
          ));
        });
      }
    ),
    criterion(
      "the web PNG reconciles a fresh native export of the saved localized artboard",
      async () => {
        const temp = await mkdtemp6(resolve14(ctx.work, ".visual-vector-check-"));
        try {
          const file = resolve14(temp, "current.png");
          await withApplication("vectorcraft", ctx.work, async (c) => {
            await call(c, "open_file", {
              path: resolve14(ctx.work, "studio-campaign.vectorcraft")
            });
            await call(c, "export", { path: file, artboard: 0 });
          });
          return await imageMatches(
            file,
            resolve14(ctx.work, "studio-web.png"),
            2
          );
        } finally {
          await rm6(temp, { recursive: true, force: true });
        }
      }
    ),
    criterion(
      "the print PDF is the unchanged signed English 480x640 proof, including the historical date",
      async () => {
        const text = await unwrappedPdfText(
          resolve14(ctx.work, "studio-proof.pdf")
        );
        if (!text.includes("22 October 2026") || !text.includes("Launch preview") || /Estudio abierto|23 de octubre/.test(text))
          return false;
        const temp = await mkdtemp6(resolve14(ctx.work, ".visual-print-check-"));
        try {
          const pdf = resolve14(temp, "native.pdf");
          await withApplication("vectorcraft", ctx.work, async (c) => {
            await call(c, "open_file", {
              path: resolve14(ctx.work, "studio-campaign.vectorcraft")
            });
            await call(c, "export", { path: pdf, artboard: 1 });
          });
          return (await unwrappedPdfText(pdf)).trim() === text.trim() && await singlePagePdfMatches(
            pdf,
            resolve14(ctx.work, "studio-proof.pdf"),
            ctx.work
          );
        } finally {
          await rm6(temp, { recursive: true, force: true });
        }
      }
    )
  ]);
}
var visualReviewFlows = [
  publication,
  portrait,
  localized
];
var campaignFacts = [
  ["Revision: draft R2", "Revision: approved portrait R3"],
  [
    "Camera: perspective, 52 mm",
    "Camera: orthographic; scale 12; (0,-10,6); target (0,0,1)"
  ],
  [
    "Image: approved-landscape.png; 640 x 360 px",
    "Image: product-hero.png; 360 x 640 px"
  ],
  ["Render: preview", "Render: CPU Cycles; 16 samples; 100% resolution"],
  ["Canvas: 960 x 720 px", "Canvas: 960 x 720 px"],
  ["Master: flattened preview", "Master: campaign-card.pcraft; editable"],
  ["Exports: campaign-r2.png", "Exports: campaign-card.png; campaign-card.jpg"],
  [
    "Archive: approved-landscape.png; product-variants.obj; replace",
    "Archive: approved-landscape.png; product-variants.obj; unchanged"
  ],
  ["Owner: Alex", "Owner: Imani"],
  [
    "Gate: publish immediately",
    "Gate: Imani's sales sign-off before publication; pending"
  ]
].map(([draft3, final]) => ({ draft: draft3, final, pattern: "" }));
var screeningFacts = [
  ["Revision: draft R2", "Revision: signed R3"],
  [
    "Image: review-r1.png; 320 x 180 px",
    "Image: studio-final.png; 320 x 180 px"
  ],
  [
    "Exports: review-r1.mp4",
    "Exports: master.wav; master.flac; studio-final.mp4; studio-final.srt; studio-final.png"
  ],
  ["Owner: Alex", "Owner: Leah"],
  [
    "Gate: publish immediately",
    "Gate: Leah's campaign sign-off before publication; pending"
  ]
].map(([draft3, final]) => ({ draft: draft3, final, pattern: "" }));
var artwork = {
  master: "campaign-card.pcraft",
  width: 960,
  height: 720,
  renderBox: [520, 40, 360, 640],
  text: {
    Brand: "REEF",
    Headline: "Portrait collection",
    Subtitle: "23 October 2026",
    "Call to action": "Request the sales pack"
  }
};
var drafts = {
  Brand: "REEF",
  Headline: "Landscape preview",
  Subtitle: "22 October 2026",
  "Call to action": "Request the draft"
};
var campaign = {
  id: "render-to-campaign-approval-pack",
  title: "Carry an approved product reshoot through editable campaign artwork and a gated sales handoff",
  application: "blender",
  applications: ["blender", "photocraft", "deckcraft", "wordcraft"],
  family: "dependent-handoff",
  split: "development",
  deadlineSeconds: 3600,
  mcpMode: "mixed",
  inputFile: "product-hero.blend",
  views: [
    { application: "blender", inputFile: "product-hero.blend" },
    { application: "photocraft", inputFile: artwork.master },
    { application: "wordcraft", inputFile: "review-notes.docx" },
    { application: "deckcraft", inputFile: "sales-briefing.pptx" }
  ],
  outputs: [
    "product-hero.blend",
    "product-hero.png",
    "product-variants.obj",
    "approved-landscape.png",
    artwork.master,
    "campaign-card.png",
    "campaign-card.jpg",
    "delivery.docx",
    "delivery.pdf",
    "sales-briefing.pptx",
    "sales-briefing.pdf"
  ],
  brief: [
    "Complete the product-campaign handoff from signed R3 in review-notes.docx. R2 is superseded. This authored scenario models a sales creative handoff; it is not a collected production trace.",
    "The existing two-product Blender scene is product-approved. Preserve every non-camera object, geometry, transform, independent material and staging; keep ArchiveSample excluded. Reframe only the active camera: orthographic scale 12, (0,-10,6), target (0,0,1). Save product-hero.blend with 360 x 640 px at 100%, CPU Cycles, 16 samples and export product-hero.png. Preserve approved-landscape.png, product-variants.obj and review-r2-render.png byte for byte.",
    "In the existing 960 x 720 campaign-card.pcraft, replace the contents of the existing Product render Smart Object with the ACTUAL new product-hero.png. Keep a separately editable Smart Object at 100% scale and placement: top-left (520,40), 360 x 640 px. Keep it visible, opaque, undistorted, unmasked and separately editable. Native replacement, Place Embedded, or copying the opened image and converting its pasted layer to a Smart Object are equivalent; remove the obsolete render layer. Do not flatten or rebuild the master. Preserve the Background and Brand layers exactly. Update the separate existing type layers: Headline = Portrait collection; Subtitle = 23 October 2026; Call to action = Request the sales pack. Preserve their identities, placement and styles. Save the editable master and export campaign-card.png and campaign-card.jpg from it.",
    "The four-slide sales-briefing.pptx must embed the actual final campaign-card.png on Current visual as one unobscured, undistorted picture inside the slide, replacing the text placeholder. Update Publication decision with every R3 current delivery field. Keep all four slides in order, the original introduction, signed R1 archive and footer. Save in place and export all four pages to sales-briefing.pdf.",
    "Reconcile the existing Current delivery fields in delivery.docx against R3; retain title, signed historical archive and footer. Save in place and export delivery.pdf. Preserve review-notes.docx. Imani's sales sign-off is still pending and required BEFORE publication; do not treat completion of the assets as approval."
  ].join("\n"),
  mcpGuidance: "Use only assigned native application tools. Blender saves persisted files; the three Craft tools connect to their running desktops. Finish the real render before opening it in PhotoCraft with doc_open, select.all and edit.copy; select the campaign document, edit.paste at the required center and layer.smartObjects.convertToSmartObject; remove only the old render layer; then export the actual card before inserting it in DeckCraft. Inspect layer/shape IDs and native command parameters. Save each editable master before export; a preview or filename alone does not establish the handoff.",
  async prepare(work, env) {
    const base = await portrait.prepare(work, env);
    await withApplication("blender", work, async (c) => {
      await call(c, "render_preview", {
        path: resolve15(work, "product-hero.blend"),
        output: resolve15(work, "product-hero.png"),
        width: 360,
        height: 640
      });
    });
    await copyFile2(
      resolve15(work, "product-hero.png"),
      resolve15(work, "review-r2-render.png")
    );
    let renderLayer = -1;
    await withApplication("photocraft", work, async (c) => {
      await call(c, "doc_new", {
        name: "Reef product campaign",
        width: artwork.width,
        height: artwork.height,
        background: "#123047"
      });
      for (const [name, y, size] of [
        ["Brand", 70, 28],
        ["Headline", 240, 32],
        ["Subtitle", 320, 22],
        ["Call to action", 440, 22]
      ])
        await call(c, "command_run", {
          id: "type.create",
          params: {
            name,
            text: drafts[name],
            x: 40,
            y,
            size,
            color: name === "Headline" ? "#FFFFFF" : "#67E8F9"
          }
        });
      const placed = await call(c, "doc_place", {
        path: resolve15(work, "review-r2-render.png"),
        scale: 100,
        center: [700, 360]
      });
      renderLayer = placed.layer;
      await call(c, "command_run", {
        id: "layer.setProps",
        params: { layer: renderLayer, name: "Product render" }
      });
      await call(c, "doc_save", { path: resolve15(work, artwork.master) });
      for (const ext of ["png", "jpg"])
        await call(c, "doc_export", {
          path: resolve15(work, "campaign-card." + ext)
        });
    });
    const register = await prepareVisualRegister(
      work,
      [
        "Reef product campaign / signed handoff review",
        "R1 / 22 October / Alex: approved landscape and OBJ are signed historical deliveries; never replace them.",
        "R2 / 23 October 09:00 / unsigned proposal: use the perspective draft and publish immediately. Superseded; not an approval.",
        "SIGNED R3 / 23 October 11:30 / Imani: retain all approved products and staging; reshoot the camera in portrait, then transfer the ACTUAL new image into the existing Product render Smart Object. Preserve its placement and the editable typography. Transfer the exported campaign card, not the raw product image, into the existing sales deck.",
        "Editable campaign type: Portrait collection / 23 October 2026 / Request the sales pack. Background and REEF brand are locked.",
        ...campaignFacts.map((f) => f.final),
        "Keep signed history in the register and sales deck. Completing exports does not grant Imani's pending sales sign-off."
      ],
      campaignFacts
    );
    const deck = await prepareVisualDeck(
      work,
      "campaign-card.png",
      [220, 110, 400, 300],
      campaignFacts,
      "Reef product campaign"
    );
    return {
      portrait: base,
      card: (await manifest(resolve15(work, artwork.master))).document,
      render_layer: renderLayer,
      draft_sha256: digest(
        await readFile10(resolve15(work, "review-r2-render.png"))
      ),
      register,
      deck
    };
  },
  async verify(ctx) {
    const m = ctx.metadata;
    const scene = m.portrait.approved_scene;
    const card = await manifest(resolve15(ctx.work, artwork.master)).then((value) => value.document).catch(() => void 0);
    return [
      ...await verifyPortraitScene(
        ctx,
        scene,
        m.portrait.retained_files
      ),
      await criterion(
        "the superseded review render remains byte-identical",
        async () => digest(await readFile10(resolve15(ctx.work, "review-r2-render.png"))) === m.draft_sha256
      ),
      await criterion(
        "the existing campaign master retains its canvas, locked background and brand",
        () => !!card && card.size.width === artwork.width && card.size.height === artwork.height && card.layers.length === m.card.layers.length && ["Background", "Brand"].every(
          (name) => isDeepStrictEqual6(
            card.layers.find((l) => l.name === name),
            m.card.layers.find((l) => l.name === name)
          )
        )
      ),
      await criterion(
        "the original editable campaign type identities, styling and approved copy remain",
        () => !!card && Object.entries(artwork.text).filter(([name]) => name !== "Brand").every(([name, text]) => {
          const old = m.card.layers.find((l) => l.name === name), now = card.layers.find((l) => l.id === old?.id);
          return campaignTypeMatches(old, now, text);
        })
      ),
      await criterion(
        "the retained Product render Smart Object is visible, opaque and at the approved undistorted placement",
        () => renderLayerMatches(
          campaignRenderLayer(card?.layers, m.card.layers, m.render_layer)
        )
      ),
      await criterion(
        "the saved native campaign master actually displays the new saved-scene render and both delivered raster exports agree",
        () => cardPixelsMatch(ctx)
      ),
      ...await verifyVisualRegister(ctx, m.register),
      ...await verifyVisualDeck(ctx, m.deck)
    ];
  }
};
function campaignRenderLayer(layers, original3, renderId) {
  if (!layers || new Set(layers.map((layer) => layer.id)).size !== layers.length)
    return void 0;
  const protectedIds = new Set(
    original3.filter((layer) => layer.id !== renderId).map((layer) => layer.id)
  );
  const candidates = layers.filter((layer) => !protectedIds.has(layer.id));
  return candidates.length === 1 ? candidates[0] : void 0;
}
function renderLayerMatches(layer) {
  return !!layer && layer.content.kind === "smart" && layer.visible && layer.opacity === 1 && layer.fill_opacity === 1 && layer.blend === "Normal" && !layer.mask && !layer.vector_mask && !layer.clipped && !layer.effects?.items?.length && !layer.content.warp && !layer.content.smart_filters?.length && isDeepStrictEqual6(layer.content.transform?.m, [1, 0, 0, 1, 520, 40]);
}
async function cardPixelsMatch(ctx) {
  const temp = await mkdtemp7(resolve15(ctx.work, ".handoff-card-check-"));
  try {
    const current2 = resolve15(temp, "current.png");
    await withApplication(
      "photocraft",
      ctx.work,
      async (c) => {
        await call(c, "doc_open", { path: resolve15(ctx.work, artwork.master) });
        await call(c, "doc_export", { path: current2 });
      },
      ctx.env
    );
    return tiledPixelsMatch(
      await pixels2(current2, "crop=360:640:520:40"),
      await pixels2(resolve15(ctx.work, "product-hero.png")),
      360,
      640,
      1
    ) && await imageMatches(
      current2,
      resolve15(ctx.work, "campaign-card.png"),
      1
    ) && await imageMatches(current2, resolve15(ctx.work, "campaign-card.jpg"), 15);
  } finally {
    await rm7(temp, { recursive: true, force: true });
  }
}
function campaignTypeMatches(before, after, text) {
  if (!before || !after || after.content.kind !== "text" || after.content.text !== text)
    return false;
  const properties = (layer) => {
    const content = structuredClone(layer.content);
    for (const key3 of ["text", "cache", "psd_raw", "runs", "paragraphs"])
      delete content[key3];
    return { ...layer, content };
  };
  const uniform = (old, now) => {
    if (!Array.isArray(old) || old.length !== 1 || !Array.isArray(now) || !now.length)
      return false;
    const style = (value) => {
      const v = { ...value };
      delete v.len;
      return v;
    };
    return now.every(
      (v) => v && Number.isInteger(v.len) && v.len >= 0 && isDeepStrictEqual6(style(v), style(old[0]))
    ) && now.reduce((n, v) => n + v.len, 0) === text.length;
  };
  return isDeepStrictEqual6(properties(before), properties(after)) && uniform(before.content.runs, after.content.runs) && uniform(before.content.paragraphs, after.content.paragraphs);
}
import {
  copyFile as copyFile5,
  mkdtemp as mkdtemp11,
  readFile as readFile14,
  rename as rename2,
  rm as rm11,
  writeFile as writeFile6
} from "node:fs/promises";
import { dirname as dirname5, resolve as resolve19 } from "node:path";
import { isDeepStrictEqual as isDeepStrictEqual9 } from "node:util";
import { copyFile as copyFile3, mkdir as mkdir2, mkdtemp as mkdtemp9, rm as rm9, writeFile as writeFile4 } from "node:fs/promises";
import { tmpdir as tmpdir4 } from "node:os";
import { resolve as resolve17 } from "node:path";
import { isDeepStrictEqual as isDeepStrictEqual7 } from "node:util";
import { execFile as execFile3, spawn } from "node:child_process";
import { closeSync as closeSync2, constants as constants2, openSync as openSync2 } from "node:fs";
import {
  access,
  cp,
  mkdir,
  mkdtemp as mkdtemp8,
  readFile as readFile11,
  readdir,
  readlink,
  rename,
  rm as rm8,
  stat,
  unlink as unlink3,
  writeFile as writeFile3
} from "node:fs/promises";
import { tmpdir as tmpdir3 } from "node:os";
import { dirname as dirname3, resolve as resolve16 } from "node:path";
import { fileURLToPath as fileURLToPath2 } from "node:url";
import { promisify as promisify3 } from "node:util";
import {
  constants,
  openSync,
  writeSync,
  closeSync,
  readSync,
  statSync as statSync2,
  fstatSync
} from "node:fs";
import { StringDecoder } from "node:string_decoder";
var AudacityPipe = class {
  constructor(to = process.env.AUDACITY_PIPE_TO, from = process.env.AUDACITY_PIPE_FROM, timeoutMs = 45e3) {
    this.to = to;
    this.from = from;
    this.timeoutMs = timeoutMs;
    if (!to || !from) throw new Error("Audacity pipe paths are required");
    this.writer = openSync(to, "r+");
    try {
      this.reader = openSync(from, constants.O_RDWR | constants.O_NONBLOCK);
    } catch (e) {
      closeSync(this.writer);
      throw e;
    }
    this.polling = setInterval(() => {
      const bytes = Buffer.alloc(65536);
      let count = 0;
      try {
        count = readSync(this.reader, bytes, 0, bytes.length, null);
      } catch (e) {
        if (e.code !== "EAGAIN")
          this.poison(e);
      }
      if (!count) {
        if (this.pending && this.buffer === "\n" && Date.now() - this.lastByte >= 100) {
          this.buffer = "";
          const p2 = this.pending;
          this.pending = void 0;
          clearTimeout(p2.timer);
          p2.reject(
            new Error("No active Audacity project (empty startup response)")
          );
        }
        return;
      }
      this.lastByte = Date.now();
      this.buffer += this.decoder.write(bytes.subarray(0, count));
      if (this.buffer.length > 4 * 1024 * 1024) {
        this.poison(new Error("Audacity response exceeded4MiB"));
        return;
      }
      const end = this.buffer.indexOf("\n\n");
      if (end < 0 || !this.pending) return;
      const reply = this.buffer.slice(0, end).trim();
      this.buffer = this.buffer.slice(end + 2);
      const p = this.pending;
      this.pending = void 0;
      clearTimeout(p.timer);
      if (!/BatchCommand finished: OK/.test(reply)) p.reject(new Error(reply));
      else p.resolve(reply.replace(/BatchCommand finished: OK\s*$/, "").trim());
    }, 15);
  }
  to;
  from;
  timeoutMs;
  writer;
  reader;
  closed = false;
  broken;
  polling;
  buffer = "";
  lastByte = 0;
  decoder = new StringDecoder("utf8");
  pending;
  queue = Promise.resolve();
  poison(error) {
    if (this.broken) return;
    this.broken = error;
    clearInterval(this.polling);
    if (this.pending) {
      clearTimeout(this.pending.timer);
      this.pending.reject(error);
      this.pending = void 0;
    }
  }
  command(command2) {
    if (this.closed)
      return Promise.reject(new Error("Audacity pipe is closed"));
    if (/[\r\n]/.test(command2) || Buffer.byteLength(command2) > 1e3)
      return Promise.reject(new Error("Command must be one bounded line"));
    const task = this.queue.then(() => {
      if (this.broken)
        throw new Error(
          "Audacity stream unusable after an earlier failure; start a fresh session: " + this.broken.message
        );
      if (this.closed) throw new Error("Audacity pipe is closed");
      return new Promise((resolve46, reject) => {
        const timer = setTimeout(
          () => this.poison(
            new Error("Audacity command timed out: " + command2.slice(0, 140))
          ),
          this.timeoutMs
        );
        this.pending = { resolve: resolve46, reject, timer };
        try {
          writeSync(this.writer, command2 + "\n");
        } catch (e) {
          this.poison(e);
        }
      });
    });
    this.queue = task.catch(() => void 0);
    return task;
  }
  async json(command2) {
    return JSON.parse(await this.command(command2));
  }
  diagnostics() {
    const info = (path, fd) => {
      try {
        const h = fstatSync(fd);
        let p;
        try {
          const v = statSync2(path);
          p = { dev: v.dev, ino: v.ino };
        } catch (e) {
          p = { error: String(e) };
        }
        return {
          path,
          fd,
          handle: { dev: h.dev, ino: h.ino },
          current_path: p
        };
      } catch (e) {
        return { path, fd, error: String(e) };
      }
    };
    return {
      closed: this.closed,
      broken: this.broken?.message,
      raw_response: this.buffer.slice(0, 4096),
      writer: info(this.to, this.writer),
      reader: info(this.from, this.reader)
    };
  }
  close() {
    if (this.closed) return;
    this.closed = true;
    if (this.pending) {
      clearTimeout(this.pending.timer);
      this.pending.reject(new Error("Pipe closed"));
      this.pending = void 0;
    }
    clearInterval(this.polling);
    closeSync(this.reader);
    closeSync(this.writer);
  }
};
function quoted(value) {
  if (/[\r\n\0]/.test(value))
    throw new Error("Multiline command values are forbidden");
  return '"' + value.replaceAll("\\", "\\\\").replaceAll('"', '\\"') + '"';
}
var execute2 = promisify3(execFile3);
var pause = (ms) => new Promise((r) => setTimeout(r, ms));
function isLiveAudacityProcess(status) {
  const state = /^State:[ \t]+(\S+)/m.exec(status)?.[1];
  return !["Z", "X", "x"].includes(state ?? "");
}
async function captureAudacityFailure(session, pipe, failure) {
  const dir = await mkdtemp8(resolve16(tmpdir3(), "reef-audacity-failure-")), notes3 = {
    error: String(failure),
    pid: session.pid,
    pipe: pipe?.diagnostics(),
    display: session.env.DISPLAY
  };
  try {
    await cp(session.env.AUDACITY_CONFIG_DIR, resolve16(dir, "profile"), {
      recursive: true
    });
  } catch (e) {
    notes3.profile_error = String(e);
  }
  if (session.pid) {
    const fds = [];
    try {
      for (const fd of await readdir(`/proc/${session.pid}/fd`)) {
        const path = `/proc/${session.pid}/fd/${fd}`;
        try {
          const link = await readlink(path);
          if (link.includes("audacity_script_pipe")) {
            const h = await stat(path);
            fds.push({ fd, link, dev: h.dev, ino: h.ino });
          }
        } catch {
        }
      }
    } catch (e) {
      notes3.native_fd_error = String(e);
    }
    notes3.native_pipe_fds = fds;
  }
  if (session.env.DISPLAY) {
    const env = { ...process.env, DISPLAY: session.env.DISPLAY };
    try {
      notes3.windows = (await execute2(
        "xdotool",
        ["search", "--name", ".", "getwindowname", "%@"],
        { env, timeout: 5e3 }
      )).stdout;
    } catch (e) {
      notes3.windows_error = String(e);
    }
    try {
      const dimensions2 = (await execute2("xdpyinfo", ["-display", session.env.DISPLAY], {
        timeout: 5e3
      })).stdout.match(/dimensions:\s*(\d+x\d+)/)?.[1];
      if (dimensions2)
        await execute2(
          "ffmpeg",
          [
            "-y",
            "-v",
            "error",
            "-f",
            "x11grab",
            "-video_size",
            dimensions2,
            "-i",
            session.env.DISPLAY,
            "-frames:v",
            "1",
            resolve16(dir, "desktop.png")
          ],
          { timeout: 1e4, maxBuffer: 1024 * 1024 }
        );
    } catch (e) {
      notes3.screenshot_error = String(e);
    }
  }
  await writeFile3(
    resolve16(dir, "failure.json"),
    JSON.stringify(notes3, null, 2) + "\n"
  );
  return dir;
}
async function exited(child) {
  if (!child?.pid || child.exitCode !== null || child.signalCode !== null)
    return;
  await new Promise((r) => {
    child.once("exit", () => r());
    child.once("error", () => r());
  });
}
async function startAudacity(work, options = {}) {
  const extra = options.environment ?? {};
  if (extra.AUDACITY_PIPE_TO && extra.AUDACITY_PIPE_FROM)
    return {
      env: Object.fromEntries(
        Object.entries(extra).filter(
          ([k]) => k.startsWith("AUDACITY_") || k === "DISPLAY"
        )
      ),
      async stop() {
      }
    };
  const binary = resolve16(
    extra.AUDACITY_BIN ?? process.env.AUDACITY_BIN ?? resolve16(
      dirname3(fileURLToPath2(import.meta.url)),
      "../../.local/software/audacity-3.7.9/squashfs-root/AppRun"
    )
  );
  if (!(await stat(binary)).isFile())
    throw new Error("Audacity executable is not a file");
  await access(binary, constants2.X_OK);
  const module = resolve16(
    dirname3(binary),
    "lib/audacity/modules/mod-script-pipe.so"
  ), moduleDate = (await stat(module)).mtime.toISOString().slice(0, 19), uid = process.getuid?.() ?? 0;
  for (const entry of await readdir("/proc")) {
    if (!/^\d+$/.test(entry)) continue;
    try {
      const proc = resolve16("/proc", entry);
      if ((await stat(proc)).uid === uid && (await readFile11(resolve16(proc, "comm"), "utf8")).trim() === "audacity" && isLiveAudacityProcess(await readFile11(resolve16(proc, "status"), "utf8")))
        throw new Error(
          "A native Audacity process is already running; attach explicit pipe paths instead"
        );
    } catch (e) {
      if (!["ENOENT", "EACCES"].includes(e.code ?? ""))
        throw e;
    }
  }
  const lock = resolve16(tmpdir3(), `reef-audacity-${uid}.lock`);
  try {
    await mkdir(lock);
  } catch {
    const pid = Number(
      await readFile11(resolve16(lock, "pid"), "utf8").catch(() => "0")
    );
    if (!pid) throw new Error("Audacity ownership lock exists without a PID");
    try {
      process.kill(pid, 0);
      throw new Error("Another owned Audacity session is active");
    } catch (e) {
      if (e.code !== "ESRCH") throw e;
    }
    await rm8(lock, { recursive: true });
    await mkdir(lock);
  }
  let config = "";
  async function writeStartupPreferences(scriptEnabled) {
    const cfg = resolve16(config, "config/audacity/audacity.cfg"), pending = cfg + ".startup";
    await writeFile3(
      pending,
      `PrefsVersion=1.1.1r1
SendAnonymousUsageInfo=0
[Version]
Major=3
Minor=7
Micro=9
[Module]
mod-script-pipe=${+scriptEnabled}
[ModulePath]
mod-script-pipe=${module}
[ModuleDateTime]
mod-script-pipe=${moduleDate}
[GUI]
ShowSplashScreen=0
[Update]
DefaultUpdatesChecking=0
UpdateNoticeShown=1
[Directories]
TempDir=${resolve16(config, "temp")}
` + (scriptEnabled ? "[Effects]\nSkipEffectsScanAtStartup=1\n" : "")
    );
    await rename(pending, cfg);
  }
  try {
    await writeFile3(resolve16(lock, "pid"), String(process.pid));
    config = await mkdtemp8(resolve16(tmpdir3(), "reef-audacity-"));
    await mkdir(resolve16(config, "config/audacity"), { recursive: true });
    await mkdir(resolve16(config, "temp"));
    await writeStartupPreferences(false);
  } catch (e) {
    if (config) await rm8(config, { recursive: true, force: true });
    await rm8(lock, { recursive: true, force: true });
    throw e;
  }
  const env = {
    AUDACITY_BIN: binary,
    AUDACITY_CONFIG_DIR: config,
    AUDACITY_PIPE_TO: `/tmp/audacity_script_pipe.to.${uid}`,
    AUDACITY_PIPE_FROM: `/tmp/audacity_script_pipe.from.${uid}`
  };
  let display = options.display ?? extra.DISPLAY, app, xvfb, stopPromise;
  const holders = [];
  let readinessPipe;
  function stop() {
    return stopPromise ??= (async () => {
      readinessPipe?.close();
      for (const c of [app, xvfb])
        if (c?.pid && c.exitCode === null && c.signalCode === null) {
          try {
            process.kill(-c.pid, "SIGKILL");
          } catch {
          }
        }
      await Promise.all([exited(app), exited(xvfb)]);
      for (const fd of holders) closeSync2(fd);
      await rm8(config, { recursive: true, force: true });
      await rm8(lock, { recursive: true, force: true });
    })();
  }
  try {
    let launch2 = function(name) {
      const log = openSync2(resolve16(config, name + ".log"), "w");
      const child = spawn(binary, [], {
        cwd: work,
        env: appEnv,
        detached: true,
        stdio: ["ignore", log, log]
      });
      closeSync2(log);
      child.on("error", () => {
      });
      return child;
    };
    var launch = launch2;
    if (!display) {
      display = `:${200 + Math.floor(Math.random() * 300)}`;
      xvfb = spawn(
        "Xvfb",
        [display, "-screen", "0", "1280x900x24", "-nolisten", "tcp", "-ac"],
        { detached: true, stdio: "ignore" }
      );
      xvfb.on("error", () => {
      });
      await pause(300);
      if (xvfb.exitCode !== null || !xvfb.pid)
        throw new Error("Audacity Xvfb failed");
    }
    env.DISPLAY = display;
    const appEnv = {
      ...process.env,
      ...extra,
      DISPLAY: display,
      TZ: "UTC",
      XDG_CONFIG_HOME: resolve16(config, "config"),
      XDG_CACHE_HOME: resolve16(config, "cache"),
      XDG_DATA_HOME: resolve16(config, "data"),
      XDG_STATE_HOME: resolve16(config, "state")
    };
    for (const path of [env.AUDACITY_PIPE_TO, env.AUDACITY_PIPE_FROM])
      try {
        const info = await stat(path);
        if (!info.isFIFO() || info.uid !== uid)
          throw new Error("Unexpected Audacity pipe object");
        await unlink3(path);
      } catch (e) {
        if (e.code !== "ENOENT") throw e;
      }
    app = launch2("bootstrap");
    let registered = false;
    for (let n = 0; n < 400; n++) {
      if (app.exitCode !== null || !app.pid)
        throw new Error("Audacity bootstrap exited");
      try {
        await access(resolve16(config, "config/audacity/pluginregistry.cfg"));
        if ((await readdir(resolve16(config, "temp"))).some(
          (n2) => n2.endsWith(".aup3unsaved")
        )) {
          registered = true;
          break;
        }
      } catch {
      }
      await pause(100);
    }
    if (!registered)
      throw new Error(
        "Audacity plugin registration did not reach a blank project"
      );
    process.kill(-app.pid, "SIGKILL");
    await exited(app);
    await rm8(resolve16(config, "temp"), { recursive: true, force: true });
    await mkdir(resolve16(config, "temp"));
    await writeStartupPreferences(true);
    app = launch2("application");
    let mapped = false;
    for (let n = 0; n < 400; n++) {
      if (app.exitCode !== null || !app.pid)
        throw new Error("Audacity application exited");
      try {
        await access(env.AUDACITY_PIPE_TO);
        await access(env.AUDACITY_PIPE_FROM);
        const blank = (await readdir(resolve16(config, "temp"))).some(
          (n2) => n2.endsWith(".aup3unsaved")
        );
        if (blank && (await execute2(
          "xdotool",
          [
            "search",
            "--onlyvisible",
            "--pid",
            String(app.pid),
            "--class",
            "[Aa]udacity"
          ],
          { env: appEnv, timeout: 1e3 }
        )).stdout.trim()) {
          mapped = true;
          break;
        }
      } catch {
      }
      await pause(100);
    }
    if (!mapped)
      throw new Error("Audacity did not map its active project window");
    holders.push(
      openSync2(env.AUDACITY_PIPE_TO, "r+"),
      openSync2(env.AUDACITY_PIPE_FROM, "r+")
    );
    readinessPipe = new AudacityPipe(
      env.AUDACITY_PIPE_TO,
      env.AUDACITY_PIPE_FROM,
      3e3
    );
    let responsive = false;
    for (let n = 0; n < 10; n++) {
      try {
        await readinessPipe.json("GetInfo: Type=Tracks");
        responsive = true;
        break;
      } catch (e) {
        if (!String(e).includes("No active Audacity project")) throw e;
        await pause(100);
      }
    }
    if (!responsive)
      throw new Error("Audacity active project did not answer GetInfo");
    if (options.projectFile)
      await readinessPipe.command(
        `OpenProject2: Filename=${quoted(resolve16(work, options.projectFile))}`
      );
    readinessPipe.close();
    readinessPipe = void 0;
    return {
      env: {
        ...env,
        ...options.projectFile ? { AUDACITY_PROJECT_FILE: resolve16(work, options.projectFile) } : {}
      },
      pid: app.pid,
      stop
    };
  } catch (e) {
    let proof = "";
    try {
      proof = await captureAudacityFailure(
        { env, pid: app?.pid },
        readinessPipe,
        e
      );
    } catch {
    }
    await stop();
    throw new Error(
      String(e) + (proof ? "; native diagnostics: " + proof : "")
    );
  }
}
import { z as z2 } from "zod";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}
function nativeLabels(labels2) {
  return labels2.flatMap((row2) => row2[1]);
}
function insertedLabelIndex(before, after) {
  const counts = /* @__PURE__ */ new Map();
  for (const label of before) {
    const key3 = JSON.stringify(label);
    counts.set(key3, (counts.get(key3) ?? 0) + 1);
  }
  const added = [];
  for (const [i, label] of after.entries()) {
    const key3 = JSON.stringify(label), count = counts.get(key3) ?? 0;
    if (count) counts.set(key3, count - 1);
    else added.push(i);
  }
  if (after.length !== before.length + 1 || added.length !== 1 || [...counts.values()].some((n) => n))
    throw new Error("Native label insertion did not add exactly one label");
  return added[0];
}
var AudacityNative = class {
  constructor(pipe, projectPath) {
    this.pipe = pipe;
    this.projectPath = projectPath;
  }
  pipe;
  projectPath;
  async inspect() {
    return {
      tracks: await this.pipe.json("GetInfo: Type=Tracks"),
      clips: await this.pipe.json("GetInfo: Type=Clips"),
      labels: await this.pipe.json("GetInfo: Type=Labels"),
      envelopes: await this.pipe.json("GetInfo: Type=Envelopes")
    };
  }
  async select(track, start = 0, end = 3600) {
    await this.pipe.command(
      `Select: Track=${track} TrackCount=1 Start=${start} End=${end}`
    );
  }
  async open(path) {
    if (this.projectPath !== path)
      await this.pipe.command(`OpenProject2: Filename=${quoted(path)}`);
    this.projectPath = path;
    return this.inspect();
  }
  async save(path) {
    await this.pipe.command(
      this.projectPath === path ? "Save:" : `SaveProject2: Filename=${quoted(path)}`
    );
    this.projectPath = path;
    return { path };
  }
  async import(path, name) {
    await this.pipe.command(`Import2: Filename=${quoted(path)}`);
    if (name) await this.pipe.command(`SetTrackStatus: Name=${quoted(name)}`);
    return this.inspect();
  }
  async mix(track, settings) {
    await this.select(track);
    const fields = [
      settings.gainDb === void 0 ? "" : `Volume=${settings.gainDb}`,
      settings.pan === void 0 ? "" : `Pan=${settings.pan * 100}`,
      settings.mute === void 0 ? "" : `Mute=${+settings.mute}`,
      settings.solo === void 0 ? "" : `Solo=${+settings.solo}`
    ].filter(Boolean);
    if (fields.length)
      await this.pipe.command("SetTrackAudio: " + fields.join(" "));
    if (settings.name !== void 0)
      await this.pipe.command(`SetTrackStatus: Name=${quoted(settings.name)}`);
    return this.inspect();
  }
  async move(track, at, start, name) {
    await this.select(track);
    await this.pipe.command(
      `SetClip: At=${at} Start=${start}` + (name === void 0 ? "" : ` Name=${quoted(name)}`)
    );
    return this.inspect();
  }
  async effect(track, start, end, effect, count = 1) {
    await this.select(track, start, end);
    const names3 = {
      "fade-in": "FadeIn",
      "fade-out": "FadeOut",
      delete: "Delete",
      "split-delete": "SplitDelete",
      trim: "Trim",
      repeat: "Repeat"
    };
    await this.pipe.command(
      names3[effect] + ":" + (effect === "repeat" ? ` Count=${count}` : "")
    );
    return this.inspect();
  }
  async tone(track, start, end, frequency, amplitude) {
    await this.select(track, start, end);
    await this.pipe.command(
      `Tone: Frequency=${frequency} Amplitude=${amplitude} Waveform=Sine`
    );
    return this.inspect();
  }
  async label(start, end, text) {
    const before = nativeLabels(
      await this.pipe.json("GetInfo: Type=Labels")
    );
    await this.pipe.command(`SelectTime: Start=${start} End=${end}`);
    await this.pipe.command("AddLabel:");
    const after = nativeLabels(
      await this.pipe.json("GetInfo: Type=Labels")
    ), index = insertedLabelIndex(before, after);
    await this.pipe.command(
      `SetLabel: Label=${index} Text=${quoted(text)} Start=${start} End=${end}`
    );
    return this.inspect();
  }
  async export(path, channels, start, end, tracks) {
    await this.pipe.command(`SelectAll:`);
    await this.pipe.command(`SelectTime: Start=${start} End=${end}`);
    if (tracks?.length) {
      await this.pipe.command(
        `SelectTracks: Track=${tracks[0]} TrackCount=1 Mode=Set`
      );
      for (const n of tracks.slice(1))
        await this.pipe.command(
          `SelectTracks: Track=${n} TrackCount=1 Mode=Add`
        );
    }
    await this.pipe.command(
      `Export2: Filename=${quoted(path)} NumChannels=${channels}`
    );
    return { path, channels, start, end };
  }
};
import { execFile as execFile4 } from "node:child_process";
import { promisify as promisify4 } from "node:util";
import { readFile as readFile12 } from "node:fs/promises";
var execute3 = promisify4(execFile4);
var audioRate = 48e3;
function encodeMono(samples, rate = audioRate) {
  const b = Buffer.alloc(44 + samples.length * 2);
  b.write("RIFF");
  b.writeUInt32LE(b.length - 8, 4);
  b.write("WAVEfmt ", 8);
  b.writeUInt32LE(16, 16);
  b.writeUInt16LE(1, 20);
  b.writeUInt16LE(1, 22);
  b.writeUInt32LE(rate, 24);
  b.writeUInt32LE(rate * 2, 28);
  b.writeUInt16LE(2, 32);
  b.writeUInt16LE(16, 34);
  b.write("data", 36);
  b.writeUInt32LE(samples.length * 2, 40);
  for (let i = 0; i < samples.length; i++)
    b.writeInt16LE(
      Math.max(-32768, Math.min(32767, Math.round(samples[i] * 32767))),
      44 + i * 2
    );
  return b;
}
function originalStem(kind, seconds2) {
  const a = new Float32Array(Math.round(seconds2 * audioRate));
  for (let i = 0; i < a.length; i++) {
    const t = i / audioRate, beat = t % 0.5;
    a[i] = kind === "rhythm" ? 0.13 * Math.sin(2 * Math.PI * 90 * beat) * Math.exp(-beat * 25) : kind === "bass" ? 0.1 * Math.sin(
      2 * Math.PI * (Math.floor(t / 2) % 2 ? 110 : 130.812783) * t
    ) : kind === "lead" ? 0.09 * Math.sin(
      2 * Math.PI * [261.625565, 329.627557, 391.995436, 523.251131][Math.floor(t / 0.5) % 4] * t
    ) : kind === "archive" ? 0.07 * Math.sin(2 * Math.PI * 174.614116 * t) : 0;
  }
  return a;
}
async function decodeAudio(path) {
  const info = JSON.parse(
    (await execute3(
      "ffprobe",
      [
        "-v",
        "error",
        "-show_entries",
        "stream=codec_name,sample_rate,channels,duration",
        "-of",
        "json",
        path
      ],
      { maxBuffer: 1024 * 1024, timeout: 15e3 }
    )).stdout
  ).streams[0];
  const { stdout } = await execute3(
    "ffmpeg",
    [
      "-v",
      "error",
      "-threads",
      "1",
      "-i",
      path,
      "-f",
      "f32le",
      "-acodec",
      "pcm_f32le",
      "-"
    ],
    { encoding: "buffer", maxBuffer: 32 * 1024 * 1024, timeout: 2e4 }
  );
  const samples = new Float32Array(stdout.length / 4);
  for (let i = 0; i < samples.length; i++)
    samples[i] = stdout.readFloatLE(i * 4);
  return {
    rate: Number(info.sample_rate),
    channels: info.channels,
    codec: info.codec_name,
    duration: samples.length / info.channels / Number(info.sample_rate),
    samples
  };
}
function signalDifference(actual, expected) {
  if (actual.length !== expected.length)
    return { rms: Infinity, peak: Infinity, lengthMatch: false };
  let sum = 0, peak = 0;
  for (let i = 0; i < actual.length; i++) {
    const d = Math.abs(actual[i] - expected[i]);
    sum += d * d;
    peak = Math.max(peak, d);
  }
  return {
    rms: Math.sqrt(sum / Math.max(1, actual.length)),
    peak,
    lengthMatch: true
  };
}
function signalMatches(a, b, tolerance = 8e-5) {
  const diff = signalDifference(a, b);
  return diff.rms < tolerance && diff.peak < tolerance * 8;
}
function quantized(samples) {
  return Float32Array.from(
    samples,
    (x) => Math.max(-32768, Math.min(32767, Math.round(x * 32767))) / 32768
  );
}
function fade(samples, start, end, direction) {
  const a = samples.slice(), s2 = Math.round(start * audioRate), e = Math.round(end * audioRate);
  for (let i = s2; i < e; i++) {
    const f = (i - s2) / Math.max(1, e - s2 - 1);
    a[i] = a[i] * (direction === "in" ? f : 1 - f);
  }
  return a;
}
function mixSignals(stems, seconds2) {
  const out = new Float32Array(Math.round(seconds2 * audioRate) * 2);
  for (const s2 of stems) {
    const g = 10 ** (s2.gainDb / 20), left = g * Math.min(1, 1 - s2.pan), right = g * Math.min(1, 1 + s2.pan);
    for (let i = 0; i < Math.min(s2.samples.length, out.length / 2); i++) {
      out[i * 2] = out[i * 2] + s2.samples[i] * left;
      out[i * 2 + 1] = out[i * 2 + 1] + s2.samples[i] * right;
    }
  }
  return out;
}
async function audioFileDigest(path) {
  const { createHash: createHash7 } = await import("node:crypto");
  return createHash7("sha256").update(await readFile12(path)).digest("hex");
}
async function savedAudioHashes(path) {
  const files = {};
  for (const suffix of ["", "-wal"]) {
    try {
      files[suffix || "main"] = await audioFileDigest(path + suffix);
    } catch (e) {
      if (e.code !== "ENOENT") throw e;
      files[suffix || "main"] = null;
    }
  }
  return files;
}
function independentExportsAgree(a, b) {
  return signalMatches(a, b, 2 * 8e-5);
}
var specs = [
  {
    id: "music-stem-mix-release",
    profile: "stems",
    title: "Mix a community music release from stems and reconcile its delivery register",
    sourceSeconds: 12,
    seconds: 12,
    split: "development",
    labels: [
      [0, 4, "Intro"],
      [4, 8, "Hook"],
      [8, 12, "Outro"]
    ],
    gain: [-3, -6, -9],
    pan: [0, -0.25, 0.25],
    outputs: [
      "music-session.aup3",
      "master.wav",
      "master.flac",
      "delivery.docx",
      "delivery.pdf"
    ]
  },
  {
    id: "music-jingle-arrangement",
    profile: "jingle",
    title: "Compose and arrange a short sonic logo from an approved motif",
    sourceSeconds: 8,
    seconds: 8,
    split: "held-out",
    labels: [
      [0, 2, "Intro"],
      [4, 6, "Sonic logo"],
      [7, 8, "Tag"]
    ],
    gain: [-6, -9, -3],
    pan: [0, -0.2, 0.2],
    outputs: [
      "music-session.aup3",
      "master.wav",
      "master.flac",
      "logo.wav",
      "delivery.docx",
      "delivery.pdf"
    ]
  },
  {
    id: "music-radio-edit-versioning",
    profile: "radio",
    title: "Create a radio edit and station sting while preserving the signed long version",
    sourceSeconds: 16,
    seconds: 12,
    split: "development",
    labels: [
      [0, 4, "Opening"],
      [4, 10, "Radio hook"],
      [10, 12, "Station tag"]
    ],
    gain: [-3, -9, -6],
    pan: [0, -0.3, 0.3],
    outputs: [
      "music-session.aup3",
      "master.wav",
      "master.flac",
      "sting.wav",
      "delivery.docx",
      "delivery.pdf"
    ]
  }
];
var names = [
  "Rhythm current",
  "Bass current",
  "Melody current",
  "Signed R1 archive"
];
var find = (id) => {
  const s2 = specs.find((s3) => s3.id === id);
  if (!s2) throw new Error("Unknown music workflow " + id);
  return s2;
};
function expectedMusicSignals(profile) {
  const s2 = specs.find((s3) => s3.profile === profile);
  const source4 = [
    quantized(
      originalStem("rhythm", profile === "jingle" ? 2 : s2.sourceSeconds)
    ),
    quantized(originalStem("bass", s2.sourceSeconds)),
    quantized(
      originalStem(profile === "jingle" ? "silence" : "lead", s2.sourceSeconds)
    )
  ];
  const signals = source4.map((a, index) => {
    if (profile === "radio") {
      const out = new Float32Array(12 * audioRate);
      out.set(a.slice(0, 4 * audioRate));
      out.set(a.slice(8 * audioRate), 4 * audioRate);
      return out;
    }
    if (profile === "jingle" && index === 0) {
      const out = new Float32Array(8 * audioRate);
      for (let i = 0; i < 4; i++) out.set(a, i * a.length);
      return out;
    }
    if (profile === "jingle" && index === 2) {
      const out = a.slice();
      for (const [j, f] of [
        261.625565,
        329.627557,
        391.995436,
        523.251131
      ].entries())
        for (let i = 0; i < audioRate / 2; i++)
          out[(4 + j * 0.5) * audioRate + i] = 0.16 * Math.sin(2 * Math.PI * f * i / audioRate);
      return out;
    }
    return a.slice();
  });
  return signals.map(
    (a) => fade(fade(a, 0, 0.25, "in"), s2.seconds - 1, s2.seconds, "out")
  );
}
function expectedMusicMix(profile) {
  const s2 = specs.find((s3) => s3.profile === profile);
  return mixSignals(
    expectedMusicSignals(profile).map((samples, i) => ({
      samples,
      gainDb: s2.gain[i],
      pan: s2.pan[i]
    })),
    s2.seconds
  );
}
async function native(work, env, action) {
  const session = await startAudacity(work, {
    environment: Object.fromEntries(
      Object.entries(env).filter(
        ([k]) => !k.startsWith("AUDACITY_PIPE_") && k !== "DISPLAY"
      )
    )
  });
  const p = new AudacityPipe(
    session.env.AUDACITY_PIPE_TO,
    session.env.AUDACITY_PIPE_FROM
  );
  try {
    return await action(new AudacityNative(p));
  } catch (e) {
    const proof = await captureAudacityFailure(session, p, e).catch(
      () => void 0
    );
    throw new Error(
      String(e) + (proof ? "; native diagnostics: " + proof : "")
    );
  } finally {
    p.close();
    await session.stop();
  }
}
function musicLabelsMatch(actual, expected) {
  const key3 = (a) => JSON.stringify(a);
  return isDeepStrictEqual7(actual.map(key3).sort(), expected.map(key3).sort());
}
async function prepare(s2, work, env) {
  await mkdir2(resolve17(work, "stems"), { recursive: true });
  const hashes = {};
  for (const [i, kind] of [
    "rhythm",
    "bass",
    s2.profile === "jingle" ? "silence" : "lead",
    "archive"
  ].entries()) {
    const path = resolve17(work, "stems", `${i + 1}-${kind}.wav`);
    await writeFile4(
      path,
      encodeMono(
        originalStem(
          kind,
          s2.profile === "jingle" && i === 0 ? 2 : s2.sourceSeconds
        )
      )
    );
    hashes[`stems/${i + 1}-${kind}.wav`] = await audioFileDigest(path);
  }
  await writeFile4(
    resolve17(work, "ATTRIBUTION.txt"),
    "Reef daily music fixtures: original synthesized rhythm, bass, melody and archive signals created for this benchmark. CC0-1.0. No third-party recording, samples, voice, cloud or generative service. WAV sources retain their original bytes.\n"
  );
  hashes["ATTRIBUTION.txt"] = await audioFileDigest(
    resolve17(work, "ATTRIBUTION.txt")
  );
  const original3 = await native(work, env, async (a) => {
    await a.pipe.command("SetProject: Rate=48000");
    for (const [i, path] of Object.keys(hashes).filter((p) => p.endsWith(".wav")).entries())
      await a.import(resolve17(work, path), names[i]);
    if (s2.profile === "stems") await a.move(2, 1, 1, "Melody current");
    await a.mix(3, { mute: true, gainDb: 0, pan: 0 });
    for (const [start, end, text] of [
      [0, 2, "DRAFT intro"],
      [2, 4, "DRAFT hook"],
      [4, 6, "DRAFT outro"]
    ])
      await a.label(start, end, text);
    await a.label(
      0,
      s2.sourceSeconds,
      "Signed R1 / 22 October 2026 - do not amend"
    );
    await a.save(resolve17(work, "music-session.aup3"));
    const state = await a.inspect();
    await a.export(resolve17(work, "master.wav"), 2, 0, s2.sourceSeconds);
    await a.export(resolve17(work, "master.flac"), 2, 0, s2.sourceSeconds);
    return state;
  });
  const final = [
    `Owner: Leah`,
    `Revision: R3 / 23 October 2026`,
    `Native project: music-session.aup3`,
    `Master duration: ${s2.seconds} seconds`,
    `Project and delivery rate: 48000 Hz`,
    `Master delivery: stereo WAV and FLAC`,
    `Current mix gains: ${s2.gain.join(", ")} dB`,
    `Current pan: ${s2.pan.join(", ")}`,
    `Sections: ${s2.labels.map(([a, b, t]) => `${t} ${a}-${b}s`).join("; ")}`,
    `Derived version: ${s2.profile === "jingle" ? "logo.wav, stereo, 4-6s (2 seconds)" : s2.profile === "radio" ? "sting.wav, stereo, 6-12s (6 seconds)" : "none"}`,
    `Archive: Signed R1 remains muted, ${s2.sourceSeconds} seconds, unchanged`,
    `Credit: original Reef synthesized stems; CC0-1.0; ATTRIBUTION.txt`,
    `Gate: Publish only after Leah's campaign sign-off`
  ];
  const draft3 = final.map(
    (v, i) => i === 0 ? "Owner: Alex" : i === 1 ? "Revision: R2 / 22 October 2026" : i === 3 ? "Master duration: 16 seconds" : i === 4 ? "Project and delivery rate: 44100 Hz" : i === 6 ? "Current mix gains: 0, 0, 0 dB" : i === 7 ? "Current pan: 0, 0, 0" : i === 8 ? "Sections: draft timing" : i === 9 ? "Derived version: pending" : i === 12 ? "Gate: Approved for immediate publication" : v
  );
  const edits = s2.profile === "stems" ? "Keep all three original current tracks editable. Move the melody clip from1s to0s; do not time-stretch. The mix is12s. Preserve the original rhythms and melody samples." : s2.profile === "jingle" ? "Arrange the2s Rhythm current loop four times to8s using editable native repetitions. Keep the8s bass. In the existing silent Melody current track create four native sine notes, each0.5s: C4 261.625565Hz at4s; E4 329.627557Hz at4.5s; G4 391.995436Hz at5s; C5 523.251131Hz at5.5s. Amplitude0.16; silence elsewhere. This motif replacesR2's A-minor sketch." : "From each of the three current16s tracks remove exactly source4-8s and close that gap within the track. The retained0-4s then8-16s make12s. Do not trim the last4s, stretch, or edit the archived16s version.";
  const notes3 = [
    "Reef music production approval record",
    "R1 / 22 October 2026 / Alex: signed preview; archive gain0dB, centre pan, muted; retain its entire original recording and signed range label.",
    "R2 / 22 October 2026 / Alex: draft ownerAlex; sixteen-second delivery; zero-dB mix; publish immediately.",
    "R3 / 23 October 2026 / Leah: supersedesR2 for current tracks and current register only. R1 archive and source WAVs remain untouched.",
    edits,
    `Current tracks in original order: ${names.slice(0, 3).join("; ")}. Gains ${s2.gain.join(", ")}dB and pans ${s2.pan.join(", ")} (-1left,+1right); no solo/mute on current tracks.`,
    `Apply linear fade-in0-.25s and linear fade-out${s2.seconds - 1}-${s2.seconds}s to each current track, after arranging/editing.`,
    ...s2.labels.map(
      ([a, b, t], i) => `Replace draft section label${i + 1}: ${t}, ${a}-${b}s.`
    ),
    ...final
  ];
  const register = await prepareRegister(work, notes3, draft3, final);
  return { profile: s2.profile, register, source_hashes: hashes, original: original3 };
}
async function inspectSaved(ctx, s2) {
  const dir = await mkdtemp9(resolve17(tmpdir4(), "reef-audio-judge-")), project2 = resolve17(dir, "saved.aup3"), source4 = resolve17(ctx.work, "music-session.aup3");
  const before = await savedAudioHashes(source4);
  await copyFile3(source4, project2);
  try {
    await copyFile3(source4 + "-wal", project2 + "-wal");
  } catch (e) {
    if (e.code !== "ENOENT") throw e;
  }
  let complete = false;
  try {
    const integrity = await execute(
      "sqlite3",
      [project2, "PRAGMA integrity_check; SELECT count(*) FROM project;"],
      { timeout: 15e3 }
    );
    if (integrity.stdout.trim() !== "ok\n1")
      throw new Error(
        "Invalid saved Audacity SQLite project: " + integrity.stdout
      );
    await execute("sqlite3", [project2, "DELETE FROM autosave;"], {
      timeout: 15e3
    });
    const snapshot = await native(dir, ctx.env, async (a) => {
      await a.open(project2);
      const state = await a.inspect();
      await a.export(resolve17(dir, "native-mix.wav"), 2, 0, s2.seconds);
      const tracks = [];
      for (let i = 0; i < 4; i++) {
        await a.mix(i, { mute: false, solo: false, gainDb: 0, pan: 0 });
        await a.export(
          resolve17(dir, `track-${i}.wav`),
          1,
          0,
          i === 3 ? s2.sourceSeconds : s2.seconds,
          [i]
        );
        tracks.push(await decodeAudio(resolve17(dir, `track-${i}.wav`)));
      }
      return {
        state,
        mix: await decodeAudio(resolve17(dir, "native-mix.wav")),
        tracks
      };
    });
    if (!isDeepStrictEqual7(before, await savedAudioHashes(source4)))
      throw new Error("Judge changed submitted native project");
    complete = true;
    return snapshot;
  } catch (e) {
    await writeFile4(resolve17(dir, "inspection-error.txt"), String(e) + "\n");
    throw new Error(String(e) + "; saved verifier copy retained: " + dir);
  } finally {
    if (complete) await rm9(dir, { recursive: true, force: true });
  }
}
async function verify(ctx, s2) {
  const m = ctx.metadata;
  let snapshot, error;
  try {
    snapshot = await inspectSaved(ctx, s2);
  } catch (e) {
    error = e;
  }
  const expected = expectedMusicSignals(s2.profile), mix = expectedMusicMix(s2.profile), state = snapshot?.state;
  const checks = await Promise.all([
    criterion(
      "native saved project reopens as editable tracks rather than a flattened substitute",
      () => {
        if (!snapshot) throw error;
        return state.tracks.filter((t) => t.kind === "wave").length === 4 && names.every((n, i) => state.tracks[i]?.name === n);
      }
    ),
    criterion(
      "current native gain, pan, mute and solo follow the later approval",
      () => names.slice(0, 3).every((_, i) => {
        const t = state?.tracks[i];
        return t && Math.abs((t.volume ?? 0) - 10 ** (s2.gain[i] / 20)) < 2e-6 && Math.abs((t.pan ?? 2) - s2.pan[i]) < 1e-5 && !t.mute && !t.solo;
      })
    ),
    criterion(
      "current native clip arrangement has the approved start and end without stretched replacements",
      () => [0, 1, 2].every((i) => {
        const c = state?.clips.filter((c2) => c2.track === i) ?? [];
        return c.length > 0 && Math.abs(Math.min(...c.map((c2) => c2.start))) < 1e-4 && Math.abs(Math.max(...c.map((c2) => c2.end)) - s2.seconds) < 1e-4;
      })
    ),
    ...[0, 1, 2].map(
      (i) => criterion(
        `native current track${i + 1} contains the approved retained/composed audio and fades`,
        () => !!snapshot && signalMatches(snapshot.tracks[i].samples, expected[i])
      )
    ),
    criterion(
      "signed archive native mix settings and full original samples remain unchanged",
      () => {
        const t = state?.tracks[3];
        return !!snapshot && !!t && t.mute === 1 && !t.solo && t.volume === 1 && t.pan === 0 && isDeepStrictEqual7(
          state.clips.filter((c) => c.track === 3),
          m.original.clips.filter((c) => c.track === 3)
        ) && signalMatches(
          snapshot.tracks[3].samples,
          quantized(originalStem("archive", s2.sourceSeconds))
        );
      }
    ),
    criterion(
      "existing current section labels follow R3 while the signed historical label is retained",
      () => !!state && musicLabelsMatch(nativeLabels(state.labels), [
        ...s2.labels,
        [0, s2.sourceSeconds, "Signed R1 / 22 October 2026 - do not amend"]
      ])
    ),
    criterion(
      "native project renders the independently specified stereo mix",
      () => !!snapshot && snapshot.mix.rate === audioRate && snapshot.mix.channels === 2 && signalMatches(snapshot.mix.samples, mix)
    ),
    ...["master.wav", "master.flac"].map(
      (file) => criterion(
        `${file} is independently decoded at48kHz stereo with approved duration and actual approved mix`,
        async () => {
          const a = await decodeAudio(resolve17(ctx.work, file));
          return a.rate === audioRate && a.channels === 2 && Math.abs(a.duration - s2.seconds) < 1 / audioRate && (file.endsWith("flac") ? a.codec === "flac" : a.codec.startsWith("pcm_")) && signalMatches(a.samples, mix);
        }
      )
    ),
    criterion(
      "WAV/FLAC deliveries agree with the mix reopened from saved native state",
      async () => {
        if (!snapshot) return false;
        return (await Promise.all(
          ["master.wav", "master.flac"].map(
            async (f) => independentExportsAgree(
              (await decodeAudio(resolve17(ctx.work, f))).samples,
              snapshot.mix.samples
            )
          )
        )).every(Boolean);
      }
    ),
    criterion(
      "original input stem bytes and CC0 provenance remain unchanged",
      async () => {
        for (const [p, h] of Object.entries(m.source_hashes))
          if (await audioFileDigest(resolve17(ctx.work, p)) !== h) return false;
        return true;
      }
    ),
    ...s2.profile === "stems" ? [] : [
      criterion(
        "derived logo/station-sting export contains the approved excerpt rather than the entire master",
        async () => {
          const start = s2.profile === "jingle" ? 4 : 6, end = s2.profile === "jingle" ? 6 : 12, a = await decodeAudio(
            resolve17(
              ctx.work,
              s2.profile === "jingle" ? "logo.wav" : "sting.wav"
            )
          );
          return a.rate === audioRate && a.channels === 2 && signalMatches(
            a.samples,
            mix.slice(start * audioRate * 2, end * audioRate * 2)
          );
        }
      )
    ]
  ]);
  return [...checks, ...await verifyRegister(ctx, m.register)];
}
var musicWorkflows = specs.map((s2) => ({
  id: s2.id,
  application: "audacity",
  applications: ["audacity", "wordcraft"],
  views: [
    { application: "audacity", inputFile: "music-session.aup3" },
    { application: "wordcraft", inputFile: "review-notes.docx" }
  ],
  title: s2.title,
  family: "daily-long-review",
  split: s2.split,
  deadlineSeconds: 2700,
  mcpMode: "live",
  inputFile: "music-session.aup3",
  outputs: s2.outputs,
  brief: `Complete the existing music production session and delivery register. Read review-notes.docx and reconcile the conflicting R1/R2/R3 approvals. Edit the existing native Audacity session: ${s2.profile === "jingle" ? "compose the four-note sonic logo and arrange the repeated loop" : s2.profile === "radio" ? "create the twelve-second radio edit plus station sting" : "realign and balance the multitrack music release"}. Preserve the signed R1 track, range label, source WAVs and original credit file. Keep the current mix editable. Apply the specified gain/pan and fades, replace the three draft section labels, save music-session.aup3 and export the requested actual audio deliveries at48000Hz stereo. Update only the current fields of delivery.docx and export delivery.pdf; preserve its signed historical record. Required outputs: ${s2.outputs.join(", ")}. R3 approval still requires Leah's campaign sign-off before publication.`,
  mcpGuidance: "Use the assigned audacity__ and wordcraft__ tools. Audacity live native project tools to inspect tracks/clips/labels, edit selections and mix, generate the requested musical notes, save and export. WordCraft opens the approval notes and updates the existing delivery register. Tool track indices are zero-based; gain uses dB and pan uses-1..1. Native edits affect the GUI; save_project persists the editable project.",
  prepare: (work, env) => prepare(s2, work, env),
  verify: (ctx) => verify(ctx, s2)
}));
function musicCalibrationCases(id) {
  const s2 = find(id);
  const mutate = (action) => async (ctx) => {
    await native(ctx.work, ctx.env, async (a) => {
      await a.open(resolve17(ctx.work, "music-session.aup3"));
      await action(a, ctx);
      await a.save(resolve17(ctx.work, "music-session.aup3"));
    });
  };
  return [
    {
      id: "equivalent-native-clip-colour",
      expected: true,
      apply: mutate(async (a) => {
        await a.select(0);
        await a.pipe.command("SetClip: At=1 Color=Color2");
      })
    },
    {
      id: "equivalent-unsaved-gain-is-not-saved-delivery",
      expected: true,
      apply: async (ctx) => {
        await native(ctx.work, ctx.env, async (a) => {
          await a.open(resolve17(ctx.work, "music-session.aup3"));
          await a.mix(1, { gainDb: 0 });
        });
      }
    },
    {
      id: "wrong-native-samples-with-valid-track-metadata",
      expected: false,
      apply: mutate((a) => a.tone(1, 2, 2.5, 440, 0.2))
    },
    {
      id: "wrong-native-mix-gain",
      expected: false,
      apply: mutate((a) => a.mix(1, { gainDb: 0 }))
    },
    {
      id: "wrong-native-melody-position",
      expected: false,
      apply: mutate((a) => a.move(2, 5, 0.5))
    },
    {
      id: "unmuted-signed-archive",
      expected: false,
      apply: mutate((a) => a.mix(3, { mute: false }))
    },
    {
      id: "stale-master-waveform",
      expected: false,
      apply: async (ctx) => {
        await copyFile3(
          resolve17(ctx.work, "stems/4-archive.wav"),
          resolve17(ctx.work, "master.wav")
        );
      }
    },
    {
      id: "wrong-delivery-sample-rate",
      expected: false,
      apply: async (ctx) => {
        const p = resolve17(ctx.work, "master.wav"), t = p + ".fault.wav";
        await execute(
          "ffmpeg",
          ["-y", "-v", "error", "-i", p, "-ar", "44100", t],
          { timeout: 15e3 }
        );
        await copyFile3(t, p);
        await rm9(t);
      }
    },
    ...s2.profile === "stems" ? [] : [
      {
        id: "wrong-derived-version-interval",
        expected: false,
        apply: async (ctx) => {
          await copyFile3(
            resolve17(ctx.work, "master.wav"),
            resolve17(
              ctx.work,
              s2.profile === "jingle" ? "logo.wav" : "sting.wav"
            )
          );
        }
      }
    ]
  ];
}
import { copyFile as copyFile4, readFile as readFile13, writeFile as writeFile5, mkdtemp as mkdtemp10, rm as rm10 } from "node:fs/promises";
import { basename, resolve as resolve18, dirname as dirname4 } from "node:path";
import { createHash as createHash2 } from "node:crypto";
import { isDeepStrictEqual as isDeepStrictEqual8 } from "node:util";
var FILM_TICKS = 254016e6;
var filmProject = "studio-cut.fcproj";
var mediaFiles = [
  "shot-arrival.mp4",
  "shot-journey.mp4",
  "shot-discovery.mp4",
  "narration.wav",
  "music-bed.wav"
];
var mediaRoot = resolve18(import.meta.dirname, "../../fixtures/filmcraft");
var filmCommand = (c, id, params = {}) => call(c, "command_run", { id, params });
function filmSequence(document, id) {
  const sequence = document.project.items[String(id)]?.kind.Sequence;
  if (!sequence) throw Error("Missing original sequence " + id);
  return sequence;
}
var digest2 = (bytes) => createHash2("sha256").update(bytes).digest("hex");
var s = (seconds2) => seconds2 * FILM_TICKS;
var approvedCaptions = [
  "Sintel returns to the open studio.",
  "Discover the story on Friday.",
  "23 October 2026"
];
var approvedTimes = (profile) => profile === "picture" ? [
  [0.25, 2],
  [2.75, 6.5],
  [6.75, 8.25]
] : [
  [0.25, 3.5],
  [3.75, 7],
  [7.25, 8.75]
];
var review2 = {
  picture: "APPROVED R2 / supersedes R1: Shorten Arrival by 6 frames at its OUT; keep its source IN. Play Arrival, Discovery, Journey on V1, gap-free. Discovery and Journey keep their full 3 seconds and source INs. Final duration 8.5 seconds. Trim existing narration and music OUTs to the same end, keeping their starts, source INs and approved gains. Keep caption text/styles/IDs; retime cues to 0.25-2.00, 2.75-6.50, 6.75-8.25 seconds. Archive sequence is locked.",
  captions: "APPROVED R2 / supersedes R1: Lock picture edit and all audio. Correct the existing three subtitle objects to: Sintel returns to the open studio. / Discover the story on Friday. / 23 October 2026. Cue times: 0.25-3.50, 3.75-7.00, 7.25-8.75 seconds. Preserve subtitle track, cue IDs, styles and archive. The Thursday/22 October R1 captions are wrong. Final duration remains 9 seconds.",
  audio: "APPROVED R2 / supersedes R1: Lock all picture, caption and timing edits. Existing A1 narration clip gain must be 0 dB; A2 music clip gain -18 dB; master volume 0 dB. Retain both clips, source INs and 9-second duration. Do not mute/delete music or alter track routing/effects. Archive is locked. Export with audible narration and quieter music, stereo 48 kHz audio.",
  campaign: "R2 / superseded: the old Thursday captions and preview mix are awaiting review. The signed R3 approval is in review-notes.docx. Keep the original nine-second picture, source ranges, clip identities and archive locked; apply only the later approved caption and audio corrections."
};
async function exportFilm(c, work, sequence, stem = "studio-final") {
  await filmCommand(c, "sequence.open", { item: sequence });
  await filmCommand(c, "file.exportMedia", {
    path: resolve18(work, stem + ".mp4"),
    sequence,
    format: "h264",
    width: 320,
    height: 180,
    fps: 12,
    audio: true,
    hardwareEncoding: "off",
    burnCaptions: true,
    captionSidecar: "srt",
    wait: true
  });
  await filmCommand(c, "file.exportFrame", {
    path: resolve18(work, stem + ".png"),
    format: "png",
    target: "program",
    sequence,
    time: s(2)
  });
}
async function prepareFilm(profile, work) {
  for (const file of [...mediaFiles, "ATTRIBUTION.md"])
    await copyFile4(resolve18(mediaRoot, file), resolve18(work, file));
  const metadata = await withApplication("filmcraft", work, async (c) => {
    await call(c, "media_import", {
      paths: mediaFiles.map((f) => resolve18(work, f))
    });
    const { sequence } = await filmCommand(c, "file.newSequence", {
      name: "CURRENT / Studio R1",
      width: 320,
      height: 180,
      fps: 12,
      fromItems: [1, 2, 3]
    });
    await filmCommand(c, "timeline.place", {
      item: 4,
      track: "A1",
      seconds: 0,
      insert: false,
      video: false,
      audio: true,
      duration: s(9)
    });
    await filmCommand(c, "timeline.place", {
      item: 5,
      track: "A2",
      seconds: 0,
      insert: false,
      video: false,
      audio: true,
      duration: s(9)
    });
    await filmCommand(c, "captions.newTrack", {
      format: "Subtitle",
      name: "Studio captions",
      language: "en"
    });
    await filmCommand(c, "captions.setStyle", {
      track: "C1",
      font: "Inter",
      size: 16,
      background: true,
      anchor: "bottom",
      margin: 0.08
    });
    for (const [i, text] of (["captions", "campaign"].includes(profile) ? [
      "Maya returns to the studio.",
      "Discover the story on Thursday.",
      "22 October 2026"
    ] : approvedCaptions).entries()) {
      const [start, end] = ["captions", "campaign"].includes(profile) ? [
        [0, 1],
        [3, 4],
        [6, 8]
      ][i] : approvedTimes("audio")[i];
      await filmCommand(c, "captions.add", {
        track: "C1",
        text,
        seconds: start,
        durationSeconds: end - start
      });
    }
    const timeline = await call(c, "sequence_inspect");
    await filmCommand(c, "clip.audioGain", {
      clips: [timeline.audio[0].items[0].clip],
      mode: "set",
      db: ["audio", "campaign"].includes(profile) ? -9 : 0
    });
    await filmCommand(c, "clip.audioGain", {
      clips: [timeline.audio[1].items[0].clip],
      mode: "set",
      db: ["audio", "campaign"].includes(profile) ? 0 : -18
    });
    if (["audio", "campaign"].includes(profile))
      await filmCommand(c, "mixer.setStrip", { strip: "Mix", volumeDb: -6 });
    await filmCommand(c, "markers.add", {
      target: "program",
      time: 0,
      name: "APPROVED / R2",
      comment: review2[profile]
    });
    const { sequence: archive2 } = await filmCommand(c, "file.newSequence", {
      name: "ARCHIVE / approved teaser",
      width: 320,
      height: 180,
      fps: 12,
      fromItems: [1]
    });
    await filmCommand(c, "sequence.open", { item: sequence });
    await exportFilm(c, work, sequence, "review-r1");
    await filmCommand(c, "file.saveAs", { path: resolve18(work, filmProject) });
    const original3 = JSON.parse(
      await readFile13(resolve18(work, filmProject), "utf8")
    );
    for (const item of Object.values(original3.project.items))
      if (item.kind.Media?.media.File)
        item.kind.Media.media.File.path = basename(
          item.kind.Media.media.File.path
        );
    await writeFile5(resolve18(work, filmProject), JSON.stringify(original3));
    return {
      profile,
      sequence,
      archive: archive2,
      original: original3,
      media_hashes: Object.fromEntries(
        await Promise.all(
          mediaFiles.map(async (f) => [
            f,
            digest2(await readFile13(resolve18(work, f)))
          ])
        )
      )
    };
  });
  await rm10(resolve18(work, "FilmCraft Previews"), {
    recursive: true,
    force: true
  });
  return metadata;
}
function expectedSequence(m) {
  const expected = structuredClone(filmSequence(m.original, m.sequence));
  if (m.profile === "picture") {
    const [arrival, journey, discovery] = expected.video_tracks[0].items;
    arrival.duration = s(2.5);
    discovery.start = s(2.5);
    journey.start = s(5.5);
    for (const track of expected.audio_tracks)
      for (const clip of track.items) clip.duration = s(8.5);
  }
  if (["audio", "campaign"].includes(m.profile)) {
    expected.audio_tracks[0].items[0].gain_db = 0;
    expected.audio_tracks[1].items[0].gain_db = -18;
    expected.master_volume_db = 0;
  }
  const times = approvedTimes(m.profile);
  for (const [i, cue] of expected.caption_tracks[0].captions.entries()) {
    cue.text = approvedCaptions[i];
    cue.start = s(times[i][0]);
    cue.duration = s(times[i][1] - times[i][0]);
  }
  return expected;
}
var orderedTracks = (tracks) => tracks.map((t) => ({
  ...t,
  items: [...t.items].sort((a, b) => a.id - b.id)
}));
async function safeMedia(document, ctx, m) {
  const media = Object.values(document.project.items).filter(
    (i) => i.kind.Media
  );
  if (media.length !== mediaFiles.length) return false;
  for (const original3 of Object.values(m.original.project.items).filter(
    (i) => i.kind.Media
  )) {
    const actual = document.project.items[String(original3.id)]?.kind.Media?.media.File.path;
    const name = basename(original3.kind.Media.media.File.path);
    if (!actual || resolve18(ctx.work, actual) !== resolve18(ctx.work, name) || digest2(await readFile13(resolve18(ctx.work, name))) !== m.media_hashes[name])
      return false;
  }
  return Object.values(document.project.items).filter((i) => i.kind.Sequence).every((i) => {
    const q = i.kind.Sequence, rate = q.settings.frame_rate;
    return q.settings.width === 320 && q.settings.height === 180 && rate.num / rate.den === 12 && [...q.video_tracks, ...q.audio_tracks].every(
      (t) => t.items.length < 10 && t.items.every(
        (clip) => Number.isFinite(clip.start) && clip.start >= 0 && clip.duration > 0 && clip.start + clip.duration <= s(10) && clip.source_in >= 0 && clip.source_in + clip.duration <= s(10)
      )
    );
  });
}
async function decoded(path, kind) {
  return (await execute(
    "ffmpeg",
    [
      "-v",
      "error",
      "-threads",
      "1",
      "-i",
      path,
      ...kind === "video" ? [
        "-an",
        "-vf",
        "scale=160:90",
        "-frames:v",
        "120",
        "-threads",
        "1",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo"
      ] : ["-vn", "-t", "10", "-ar", "8000", "-ac", "1", "-f", "s16le"],
      "pipe:1"
    ],
    { encoding: "buffer", timeout: 3e4, maxBuffer: 6e6 }
  )).stdout;
}
function videoEqual(a, b) {
  if (a.length === 0 || a.length !== b.length) return false;
  if (a.length % (160 * 90 * 3)) return false;
  for (let frame = 0; frame < a.length / (160 * 90 * 3); frame++)
    for (let ty = 0; ty < 9; ty++)
      for (let tx = 0; tx < 16; tx++) {
        let error = 0;
        for (let y = ty * 10; y < (ty + 1) * 10; y++)
          for (let x = tx * 10; x < (tx + 1) * 10; x++)
            for (let c = 0; c < 3; c++) {
              const i = ((frame * 90 + y) * 160 + x) * 3 + c;
              error += Math.abs(a[i] - b[i]);
            }
        if (error / 300 > 4) return false;
      }
  return true;
}
function audioEqual(a, b) {
  if (a.length < 8e3 || a.length !== b.length) return false;
  let error = 0, energy = 0;
  for (let i = 0; i < a.length; i += 2) {
    const x = a.readInt16LE(i) / 32768, y = b.readInt16LE(i) / 32768;
    error += (x - y) ** 2;
    energy += y ** 2;
  }
  return energy > 1e-3 && error / energy <= 25e-4;
}
function srtCues(text) {
  return text.trim().split(/\r?\n\s*\r?\n/).map((block) => {
    const match = /\d+\s*\n(\d\d):(\d\d):(\d\d),(\d{3}) --> (\d\d):(\d\d):(\d\d),(\d{3})\s*\n([\s\S]*)/.exec(
      block.replaceAll("\r", "")
    );
    if (!match) throw Error("Invalid SRT cue");
    const time = (offset) => +match[offset] * 3600 + +match[offset + 1] * 60 + +match[offset + 2] + +match[offset + 3] / 1e3;
    return { start: time(1), end: time(5), text: match[9].trim() };
  });
}
function filmSequenceIdentitiesMatch(document, original3, sequence, archive2) {
  return isDeepStrictEqual8(
    Object.keys(document.project.items).sort(),
    Object.keys(original3.project.items).sort()
  ) && isDeepStrictEqual8(
    document.project.items[String(archive2)],
    original3.project.items[String(archive2)]
  ) && document.project.items[String(sequence)]?.kind.Sequence !== void 0;
}
async function verifyFilm(ctx) {
  const m = ctx.metadata, expected = expectedSequence(m), duration = m.profile === "picture" ? 8.5 : 9;
  const document = () => readFile13(resolve18(ctx.work, filmProject), "utf8").then(
    (t) => JSON.parse(t)
  );
  const sequence = async () => filmSequence(await document(), m.sequence);
  let independent;
  let temp;
  const rendered = () => independent ??= (async () => {
    const d = await document();
    if (!await safeMedia(d, ctx, m))
      throw Error("Saved project violates bounded local-media contract");
    temp = await mkdtemp10(resolve18(ctx.work, ".film-verify-"));
    await withApplication("filmcraft", ctx.work, async (c) => {
      await filmCommand(c, "file.open", {
        path: resolve18(ctx.work, filmProject)
      });
      await exportFilm(c, temp, m.sequence, "independent");
    });
    return resolve18(temp, "independent.mp4");
  })();
  try {
    return await Promise.all([
      criterion(
        "original source media remain intact and the saved project uses only bounded local assets",
        async () => safeMedia(await document(), ctx, m)
      ),
      criterion(
        "the original current and archive sequences retain their identities without replacements",
        async () => {
          const d = await document();
          return filmSequenceIdentitiesMatch(
            d,
            m.original,
            m.sequence,
            m.archive
          );
        }
      ),
      criterion(
        "existing picture clips retain source ranges, effects and identities with the approved order and duration",
        async () => isDeepStrictEqual8(
          orderedTracks((await sequence()).video_tracks),
          orderedTracks(expected.video_tracks)
        )
      ),
      criterion(
        "existing narration and music retain source ranges, routing and effects with the approved gains and duration",
        async () => isDeepStrictEqual8(
          orderedTracks((await sequence()).audio_tracks),
          orderedTracks(expected.audio_tracks)
        )
      ),
      criterion(
        "the existing caption track and cue IDs have approved text, timing and unchanged styles",
        async () => isDeepStrictEqual8(
          (await sequence()).caption_tracks,
          expected.caption_tracks
        )
      ),
      criterion(
        "sequence settings, review markers, master mix and all other protected sequence state remain correct",
        async () => {
          const rest = (q) => {
            const {
              video_tracks: _v,
              audio_tracks: _a,
              caption_tracks: _c,
              ...other
            } = q;
            return other;
          };
          return isDeepStrictEqual8(rest(await sequence()), rest(expected));
        }
      ),
      criterion(
        "the delivered H.264 has the exact video duration, 320x180 at 12 fps and stereo 48 kHz audio",
        async () => {
          const path = resolve18(ctx.work, "studio-final.mp4");
          const data = JSON.parse(
            (await execute(
              "ffprobe",
              [
                "-v",
                "error",
                "-count_frames",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                path
              ],
              { timeout: 3e4 }
            )).stdout
          );
          const v = data.streams.find(
            (s2) => s2.codec_type === "video"
          ), a = data.streams.find(
            (s2) => s2.codec_type === "audio"
          );
          return v?.codec_name === "h264" && v.width === 320 && v.height === 180 && v.avg_frame_rate === "12/1" && +v.nb_read_frames === duration * 12 && a?.channels === 2 && +a.sample_rate === 48e3 && Math.abs(+data.format.duration - duration) <= 0.1;
        }
      ),
      criterion(
        "every delivered video frame matches an independent render of the saved edit including burned captions",
        async () => videoEqual(
          await decoded(resolve18(ctx.work, "studio-final.mp4"), "video"),
          await decoded(await rendered(), "video")
        )
      ),
      criterion(
        "the delivered audio matches the saved narration/music mix rather than silence or a stale soundtrack",
        async () => audioEqual(
          await decoded(resolve18(ctx.work, "studio-final.mp4"), "audio"),
          await decoded(await rendered(), "audio")
        )
      ),
      criterion(
        "the SRT sidecar contains the same approved caption text and cue times",
        async () => {
          const cues = srtCues(
            await readFile13(resolve18(ctx.work, "studio-final.srt"), "utf8")
          );
          return cues.length === 3 && cues.every(
            (cue, i) => cue.text === approvedCaptions[i] && Math.abs(cue.start - approvedTimes(m.profile)[i][0]) <= 1e-3 && Math.abs(cue.end - approvedTimes(m.profile)[i][1]) <= 1e-3
          );
        }
      ),
      criterion(
        "the delivered poster is the program frame at 2 seconds from the saved project",
        async () => {
          const path = resolve18(ctx.work, "studio-final.png");
          if (!await pngEvidence(path, 320, 180)) return false;
          const reference = resolve18(
            dirname4(await rendered()),
            "independent.png"
          );
          return videoEqual(
            await decoded(path, "video"),
            await decoded(reference, "video")
          );
        }
      )
    ]);
  } finally {
    if (temp) await rm10(temp, { recursive: true, force: true });
  }
}
function workflow2(profile, id, title4) {
  return {
    id,
    title: title4,
    application: "filmcraft",
    family: "filmcraft-existing-edit",
    split: profile === "audio" ? "held-out" : "development",
    inputFile: filmProject,
    outputs: [
      filmProject,
      "studio-final.mp4",
      "studio-final.srt",
      "studio-final.png"
    ],
    brief: `Revise the existing studio-cut.fcproj. Read the CURRENT / Studio R1 sequence's APPROVED / R2 review marker and apply the approved revision; earlier R1 content is superseded. Preserve existing sequence, clip, subtitle identities, source media and the locked ARCHIVE sequence. Do not recreate the project. Save the native master in place. Deliver studio-final.mp4: the full current sequence, H.264, 320 x 180, 12 fps, stereo 48 kHz audio, software encoding, with burned subtitles. Deliver studio-final.srt with matching subtitle text/times, and studio-final.png as the program frame at exactly 2 seconds. Check all deliveries. Source credits remain in ATTRIBUTION.md.`,
    mcpGuidance: "The existing FilmCraft desktop is open. Inspect the current sequence, review markers, source media and existing clips/captions; discover native editing commands. Export the current sequence explicitly, with software encoding, burned captions and an SRT sidecar. Preserve protected picture/audio edits and archive.",
    prepareTrial: async (work) => {
      const path = resolve18(work, filmProject), document = JSON.parse(await readFile13(path, "utf8"));
      for (const item of Object.values(document.project.items))
        if (item.kind.Media?.media.File)
          item.kind.Media.media.File.path = basename(
            item.kind.Media.media.File.path
          );
      await writeFile5(path, JSON.stringify(document));
      return {};
    },
    prepare: (work) => prepareFilm(profile, work),
    verify: verifyFilm
  };
}
var filmPicture = workflow2(
  "picture",
  "filmcraft-picture-revision",
  "Reorder and trim an existing multi-clip edit with dependent audio and captions"
);
var filmCaptions = workflow2(
  "captions",
  "filmcraft-caption-revision",
  "Correct existing subtitle timing and copy while preserving locked picture and sound"
);
var filmAudio = workflow2(
  "audio",
  "filmcraft-audio-delivery",
  "Repair an existing narration/music mix and deliver consistent video, subtitles and poster"
);
var filmCampaign = workflow2(
  "campaign",
  "filmcraft-campaign-component",
  "Apply approved captions and mixing to a locked existing picture edit"
);
import { unzipSync as unzipSync6, zipSync as zipSync2, strFromU8 as strFromU86, strToU8 as strToU82 } from "fflate";
import { XMLParser as XMLParser4 } from "fast-xml-parser";
var currentHeading = "Current delivery";
var archiveHeading = "Historical archive";
var sharedCurrentCriterion = "the existing current register fields contain the approved facts exactly once in their original order";
function key(text) {
  return canonicalText(text).toLowerCase().replace(/[\u2018\u2019]/g, "'").replace(/\bh\.?\s*264\b/g, "h264").replace(
    /\b(\d+(?:\.\d+)?)\s*(khz|hz)\b/g,
    (_, n, unit) => `${Number(n) * (unit === "khz" ? 1e3 : 1)}hz`
  ).replace(/(\d+)\s*[x×]\s*(\d+)/g, "$1x$2").replace(/\s*([,:()])\s*/g, "$1").replace(/\.$/, "");
}
function row(text) {
  const colon = text.indexOf(":");
  if (colon < 1) return void 0;
  return { label: key(text.slice(0, colon)), value: text.slice(colon + 1) };
}
function mediaRegisterFieldsMatch(actual, expected) {
  if (actual.length !== expected.length) return false;
  return actual.every((text, i) => {
    const a = row(text), e = row(expected[i]);
    if (!a || !e || a.label !== e.label) return false;
    if (a.label === "gate") {
      if (key(e.value) !== "publish only after leah's campaign sign-off")
        return false;
      return [
        "Publish only after Leah's campaign sign-off",
        "Publish after Leah's campaign sign-off",
        "Publication requires Leah's campaign sign-off",
        "Await Leah's campaign sign-off before publication",
        "Do not publish until Leah's campaign sign-off",
        "Leah's campaign sign-off before publication is still pending"
      ].some((value) => key(a.value) === key(value));
    }
    const clauses2 = (value) => value.split(";").map(key).sort();
    const left = clauses2(a.value), right = clauses2(e.value);
    return left.length === right.length && left.every((v, j) => v === right[j]);
  });
}
function paragraphs(raw) {
  const parsed = new XMLParser4({
    ignoreAttributes: false,
    attributeNamePrefix: "@"
  }).parse(raw);
  return array(parsed?.["w:document"]?.["w:body"]?.["w:p"]);
}
function paragraphText(p) {
  return canonicalText(
    array(p["w:r"]).map((r) => xmlText(r["w:t"])).join("")
  );
}
function hasHiddenText(value) {
  if (!value || typeof value !== "object") return false;
  if (Array.isArray(value)) return value.some(hasHiddenText);
  const node = value;
  if (node["w:r"] && array(node["w:r"]).some((run) => {
    const props = run["w:rPr"];
    return canonicalText(xmlText(run["w:t"])).length > 0 && enabledProperty(props?.["w:vanish"]);
  }))
    return true;
  return Object.values(node).some(hasHiddenText);
}
function mediaRegisterCurrentRowsVisible(raw) {
  const p = paragraphs(raw), text = p.map(paragraphText);
  const starts = text.flatMap((t, i) => t === currentHeading ? [i] : []);
  const ends = text.flatMap((t, i) => t === archiveHeading ? [i] : []);
  if (starts.length !== 1 || ends.length !== 1 || ends[0] <= starts[0] + 1)
    return false;
  return !p.slice(starts[0], ends[0]).some(hasHiddenText);
}
function localizeMediaRegisterChecks(checks, semantic, visibility) {
  if (checks.filter((c) => c.name === sharedCurrentCriterion).length !== 1)
    throw Error(
      "Shared register current-field seam changed; refusing to mask other criteria"
    );
  return [
    ...checks.map((c) => c.name === sharedCurrentCriterion ? semantic : c),
    visibility
  ];
}
function mediaRegisterEquivalentRows(expected) {
  return expected.map((line) => {
    if (line.startsWith("Audio delivery:"))
      return line.replace(
        "stereo 48000 Hz; WAV PCM16",
        "WAV PCM16; stereo 48 kHz"
      );
    if (line.startsWith("Video delivery:"))
      return line.replace(
        "H.264 320 x 180; 12 fps; stereo 48000 Hz",
        "H264 320 \xD7 180; stereo 48 kHz; 12 fps"
      );
    if (line.startsWith("Music gains:"))
      return line.replace(
        "pan 0, -0.25, 0.25; melody aligned to 0s",
        "melody aligned to 0s; pan 0, -0.25, 0.25"
      );
    if (line.startsWith("Gate:")) return line.replace("Leah's", "Leah\u2019s");
    return line;
  });
}
function hideMediaRegisterCurrentRows(raw) {
  let current2 = false, found = 0;
  const edited = raw.replace(
    /<w:p(?:\s[^>]*)?>[\s\S]*?<\/w:p>/g,
    (paragraph) => {
      const parsed = paragraphs(
        `<w:document><w:body>${paragraph}</w:body></w:document>`
      )[0];
      const text = parsed ? paragraphText(parsed) : "";
      if (text === currentHeading) {
        current2 = true;
        return paragraph;
      }
      if (text === archiveHeading) {
        current2 = false;
        return paragraph;
      }
      if (!current2 || !text) return paragraph;
      found++;
      return paragraph.replace(/<w:r(?:\s[^>]*)?>[\s\S]*?<\/w:r>/g, (run) => {
        if (!/<w:t(?:\s[^>]*)?>/.test(run)) return run;
        let next2 = run.replace(
          /<w:vanish\b[^>]*(?:\/>|>[\s\S]*?<\/w:vanish>)/g,
          ""
        );
        if (/<w:rPr\s*\/>/.test(next2))
          return next2.replace(/<w:rPr\s*\/>/, "<w:rPr><w:vanish/></w:rPr>");
        if (/<w:rPr(?:\s[^>]*)?>/.test(next2))
          return next2.replace(/<w:rPr(?:\s[^>]*)?>/, "$&<w:vanish/>");
        next2 = next2.replace(
          /<w:r(?:\s[^>]*)?>/,
          "$&<w:rPr><w:vanish/></w:rPr>"
        );
        return next2;
      });
    }
  );
  if (!found) throw Error("No existing current register paragraphs to hide");
  return edited;
}
var music = musicWorkflows.find((w) => w.id === "music-stem-mix-release");
var seconds = (n) => n * FILM_TICKS;
var captions = [
  { text: "Sintel returns to the open studio.", start: 0.25, end: 3.5 },
  { text: "Discover the story on Friday.", start: 3.75, end: 7 },
  { text: "23 October 2026", start: 7.25, end: 8.75 }
];
var musicContext = (ctx) => ({
  ...ctx,
  metadata: {
    ...ctx.metadata.music,
    register: ctx.metadata.register
  }
});
var current = (d, m) => filmSequence(d, m.film.sequence);
var sortedTracks = (tracks) => tracks.map((t) => ({
  ...t,
  items: [...t.items].sort((a, b) => a.id - b.id)
}));
var defaultEmptyVideoTrack = (id, name) => ({
  id,
  kind: "Video",
  name,
  items: [],
  transitions: [],
  locked: false,
  sync_lock: true,
  enabled: true,
  muted: false,
  solo: false,
  channels: "Stereo",
  volume_db: 0,
  pan: 0,
  effects: [],
  mixer: {
    mode: "Read",
    output: null,
    record_arm: false,
    solo_safe: false,
    input_map: "Stereo"
  }
});
function uniqueUnreferencedTrackId(d, id) {
  let definitions = 0, referenced = false;
  const referenceKey = /^(?:item|Item|link|group|from|to|target|output|track|track_id|clip)$/;
  const visit = (value) => {
    if (!value || typeof value !== "object") return;
    for (const [key3, entry] of Object.entries(value)) {
      if (key3 === "id" && entry === id) definitions++;
      if (referenceKey.test(key3) && entry === id) referenced = true;
      if (entry && typeof entry === "object") visit(entry);
    }
  };
  visit(d);
  return definitions === 1 && !referenced;
}
function filmStateContract(d, m, work) {
  const original3 = m.film.original;
  const q = current(d, m), old = current(original3, m);
  const originalKeys = Object.keys(original3.project.items);
  const added = Object.keys(d.project.items).filter(
    (k) => !originalKeys.includes(k)
  );
  const extra = added.length === 1 ? d.project.items[added[0]] : void 0;
  const asset = extra?.kind.Media?.media.File.path;
  const normalizedItem = (item) => {
    const copy = structuredClone(item);
    if (copy?.kind.Media?.media.File)
      copy.kind.Media.media.File.path = resolve19(
        work,
        copy.kind.Media.media.File.path
      );
    return copy;
  };
  const assets = !!asset && resolve19(work, asset) === resolve19(work, "master.wav") && originalKeys.every(
    (key3) => key3 === String(m.film.sequence) || isDeepStrictEqual9(
      normalizedItem(d.project.items[key3]),
      normalizedItem(original3.project.items[key3])
    )
  );
  const oldNarration = structuredClone(old.audio_tracks[0]);
  oldNarration.items[0].gain_db = 0;
  const oldBedTrack = old.audio_tracks[1], bedTrack = q.audio_tracks[1];
  const bed = bedTrack?.items[0];
  const oldBed = oldBedTrack.items[0];
  const expectedBed = {
    ...oldBed,
    id: bed?.id,
    item: extra?.id,
    name: bed?.name,
    gain_db: -18
  };
  const audio = q.audio_tracks.length === old.audio_tracks.length && isDeepStrictEqual9(q.audio_tracks.slice(2), old.audio_tracks.slice(2)) && isDeepStrictEqual9(q.audio_tracks[0], oldNarration) && !!bed && typeof bed.name === "string" && bedTrack.items.length === 1 && isDeepStrictEqual9(
    { ...bedTrack, items: [] },
    { ...oldBedTrack, items: [] }
  ) && isDeepStrictEqual9(bed, expectedBed) && bed.start === 0 && bed.source_in === 0 && bed.duration === seconds(9) && Number.isSafeInteger(bed.id) && bed.id !== oldNarration.items[0].id;
  const expectedCaptions = structuredClone(old.caption_tracks);
  for (const [i, cue] of expectedCaptions[0].captions.entries()) {
    const approved2 = captions[i];
    if (!approved2)
      return {
        assets: false,
        picture: false,
        audio: false,
        captions: false,
        rest: false
      };
    cue.text = approved2.text;
    cue.start = seconds(approved2.start);
    cue.duration = seconds(approved2.end - approved2.start);
  }
  const rest = (s2) => {
    const {
      video_tracks: _v,
      audio_tracks: _a,
      caption_tracks: _c,
      master_volume_db: _m,
      ...r
    } = s2;
    return r;
  };
  const originalNext = original3.project.next_id;
  const next2 = d.project.next_id;
  const appended = q.video_tracks.slice(old.video_tracks.length);
  const empty = appended[0];
  const appendedVideoValid = appended.length === 0 || appended.length === 1 && !!empty && typeof empty.name === "string" && empty.name.length <= 256 && !/[\u0000-\u001f\u007f]/.test(empty.name) && Number.isSafeInteger(empty.id) && typeof originalNext === "number" && Number.isSafeInteger(originalNext) && empty.id >= originalNext && typeof next2 === "number" && Number.isSafeInteger(next2) && next2 > empty.id && isDeepStrictEqual9(empty, defaultEmptyVideoTrack(empty.id, empty.name)) && uniqueUnreferencedTrackId(d, empty.id);
  const originalRoot = original3.project.root;
  const allocatorValid = typeof originalNext === "number" && Number.isSafeInteger(originalNext) && typeof next2 === "number" && Number.isSafeInteger(next2) && next2 >= originalNext && !!extra && Number.isSafeInteger(extra.id) && extra.id >= originalNext && next2 > extra.id && !!bed && Number.isSafeInteger(bed.id) && (bed.id === oldBed.id || bed.id >= originalNext) && bed.id !== extra.id && next2 > bed.id && added[0] === String(extra.id);
  const expectedOuter = structuredClone(original3);
  expectedOuter.project.items = {};
  if (originalRoot && Array.isArray(originalRoot.children))
    expectedOuter.project.root = {
      ...originalRoot,
      children: [...originalRoot.children, { Item: extra?.id }]
    };
  expectedOuter.project.next_id = next2;
  const { view: _savedView, ...savedState } = d;
  const { view: _originalView, ...expectedState } = expectedOuter;
  return {
    assets,
    picture: appendedVideoValid && isDeepStrictEqual9(
      sortedTracks(q.video_tracks.slice(0, old.video_tracks.length)),
      sortedTracks(old.video_tracks)
    ),
    audio,
    captions: isDeepStrictEqual9(q.caption_tracks, expectedCaptions),
    rest: q.master_volume_db === 0 && isDeepStrictEqual9(rest(q), rest(old)) && allocatorValid && !!originalRoot && Array.isArray(originalRoot.children) && isDeepStrictEqual9(
      { ...savedState, project: { ...savedState.project, items: {} } },
      expectedState
    )
  };
}
async function prepare2(work, env) {
  const musicMetadata = await music.prepare(work, env);
  const film = await filmCampaign.prepare(work, env);
  film.media_hashes["ATTRIBUTION.md"] = await audioFileDigest(
    resolve19(work, "ATTRIBUTION.md")
  );
  const final = [
    "Owner: Leah",
    "Revision: signed R3 / 23 October 2026",
    "Music master: music-session.aup3; 12 seconds; editable current stems",
    "Audio delivery: master.wav and master.flac; stereo 48000 Hz; WAV PCM16",
    "Music gains: -3, -6, -9 dB; pan 0, -0.25, 0.25; melody aligned to 0s",
    "Music fades: linear 0-0.25s in; 11-12s out on each current stem",
    "Music sections: Intro 0-4s; Hook 4-8s; Outro 8-12s",
    "Film master: studio-cut.fcproj; existing current sequence; 9 seconds",
    "Film soundtrack: first 9s of actual R3 master.wav on A2 at -18 dB; original A1 narration 0 dB; master 0 dB",
    "Captions: Sintel returns to the open studio. 0.25-3.50s; Discover the story on Friday. 3.75-7.00s; 23 October 2026 7.25-8.75s",
    "Video delivery: studio-final.mp4; H.264 320 x 180; 12 fps; stereo 48000 Hz; burned captions",
    "Companions: studio-final.srt; studio-final.png program frame at 2 seconds",
    "Protected history: signed muted 12s R1 music and label; Film archive, picture and original narration; source media and both credit files unchanged",
    "Gate: Publish only after Leah's campaign sign-off"
  ];
  const draft3 = final.map(
    (line, i) => ({
      0: "Owner: Alex",
      1: "Revision: R2 / 22 October 2026",
      3: "Audio delivery: preview mix; stereo 44100 Hz",
      4: "Music gains: 0, 0, 0 dB; pan 0, 0, 0; melody offset 1s",
      5: "Music fades: none",
      6: "Music sections: draft timing",
      8: "Film soundtrack: old music-bed.wav at 0 dB; narration -9 dB; master -6 dB",
      9: "Captions: R2 Thursday / 22 October draft",
      13: "Gate: Approved for immediate publication"
    })[i] ?? line
  );
  const notes3 = [
    "Reef music-to-video release approval record \u2014 authored production scenario",
    "R1 / 22 October / Alex: signed music preview and locked Film archive. Retain their complete native history and original source media.",
    "R2 / 22 October / Alex: zero-dB preview stems; use music-bed.wav, Thursday/22 October captions; publish immediately. OBSOLETE.",
    "SIGNED R3 / 23 October 2026 / Leah: supersedes R2 current production and delivery fields. Campaign publication approval is still pending Leah's sign-off.",
    "Music: use existing Rhythm current, Bass current, Melody current tracks in original order. Move the existing melody clip from1s to0s without stretching. Gains -3,-6,-9dB, pan0,-0.25,+0.25; current tracks audible, no solo. Apply native linear fades0-.25s and11-12s to all three. Replace DRAFT labels with Intro0-4, Hook4-8, Outro8-12. Preserve signed R1 muted/centre/unity recording and its signed label.",
    "Save the editable Audacity project; export actual12s master.wav (PCM16) and FLAC, both stereo48kHz. The original raw stems and credits remain intact.",
    "Film: import the ACTUAL exported R3 master.wav as a new native media item. Overwrite only the existing A2 music bed with its first9s, source IN0, timeline0-9s, gain-18dB. New A2 clip identity is permitted by this source replacement. Preserve the original A2 track routing/effects. Original narration A1 gain0dB and master0dB. Preserve all picture clips/source ranges, narration identity/source/effects, review markers, sequence and archive. Retain old music-bed.wav as a source asset; it must no longer be the current A2 bed.",
    "Correct existing caption objects in place to the R3 text/times below, retaining cue IDs and styles. Render through FilmCraft with software encoding; save its editable project. Export native H.264, burned subtitles, matching SRT and 2s poster. Check that final movie uses the corrected master, not a stale preview with the right path.",
    ...final
  ];
  const register = await prepareRegister(work, notes3, draft3, final);
  return { music: musicMetadata, film, register };
}
async function decodeNarrationAtOutputRate(path) {
  const source4 = await decodeAudio(path);
  if (![1, 2].includes(source4.channels) || !Number.isInteger(source4.rate) || source4.rate <= 0 || source4.duration < 9)
    throw Error("Unsupported original narration layout or source duration");
  if (source4.rate === audioRate) return { ...source4, sourceRate: source4.rate };
  const { stdout } = await execute(
    "ffmpeg",
    [
      "-v",
      "error",
      "-threads",
      "1",
      "-i",
      path,
      "-map",
      "0:a:0",
      "-t",
      "9",
      "-ar",
      String(audioRate),
      "-ac",
      String(source4.channels),
      "-f",
      "f32le",
      "pipe:1"
    ],
    { encoding: "buffer", timeout: 3e4, maxBuffer: 4e6 }
  );
  const samples = Float32Array.from(
    { length: stdout.length / 4 },
    (_, i) => stdout.readFloatLE(i * 4)
  );
  if (samples.length !== 9 * audioRate * source4.channels)
    throw Error(
      "Independent narration resampling produced an incomplete interval"
    );
  return {
    ...source4,
    sourceRate: source4.rate,
    rate: audioRate,
    samples,
    duration: 9
  };
}
function expectedMovieAudio(narration) {
  if (narration.rate !== audioRate || ![1, 2].includes(narration.channels) || narration.samples.length < 9 * audioRate * narration.channels)
    throw Error(
      "Narration must be independently resampled to the48kHz output clock before comparison"
    );
  const mix = expectedMusicMix("stems"), out = new Float32Array(9 * audioRate * 2);
  const gain = 10 ** (-18 / 20);
  for (let i = 0; i < 9 * audioRate; i++)
    for (let channel = 0; channel < 2; channel++)
      out[i * 2 + channel] = narration.samples[i * narration.channels + Math.min(channel, narration.channels - 1)] + mix[i * 2 + channel] * gain;
  return out;
}
function encodedAudioMatches(actual, expected) {
  if (actual.length !== expected.length || !actual.length) return false;
  let error = 0, energy = 0;
  for (let i = 0; i < actual.length; i++) {
    error += (actual[i] - expected[i]) ** 2;
    energy += expected[i] ** 2;
  }
  return energy > 1e-3 && error / energy <= 25e-4;
}
function compositeAudioMatches(actual, narration) {
  return compositeAudioEvidence(actual, narration).passed;
}
var componentBounds = {
  voiceGainError: 0.03,
  musicGainError: 0.1,
  minimumNormalizedDeterminant: 1e-6,
  zeroVoiceEnergy: 1e-12,
  nativeValidated: false
};
function compositeAudioEvidence(actual, narration) {
  const expected = expectedMovieAudio(narration);
  if (actual.length !== expected.length || !actual.length)
    return { passed: false, reason: "sample-count" };
  const master2 = expectedMusicMix("stems"), musicScale = 10 ** (-18 / 20);
  let voiceEnergy = 0, musicEnergy = 0, cross = 0, actualVoice = 0, actualMusic = 0;
  for (let i = 0; i < actual.length; i++) {
    const voice2 = narration.samples[Math.floor(i / 2) * narration.channels + Math.min(i % 2, narration.channels - 1)];
    const bed = master2[i] * musicScale, sample = actual[i];
    if (![voice2, bed, sample].every(Number.isFinite))
      return { passed: false, reason: "non-finite-sample" };
    voiceEnergy += voice2 * voice2;
    musicEnergy += bed * bed;
    cross += voice2 * bed;
    actualVoice += sample * voice2;
    actualMusic += sample * bed;
  }
  if (musicEnergy <= 1e-12)
    return { passed: false, reason: "unidentifiable-music" };
  let voiceGain = null, musicGain, determinant = 1;
  if (voiceEnergy <= componentBounds.zeroVoiceEnergy) {
    musicGain = actualMusic / musicEnergy;
  } else {
    const voiceNorm = Math.sqrt(voiceEnergy), musicNorm = Math.sqrt(musicEnergy);
    const correlation = cross / (voiceNorm * musicNorm);
    determinant = 1 - correlation * correlation;
    if (determinant < componentBounds.minimumNormalizedDeterminant)
      return {
        passed: false,
        reason: "unidentifiable-collinear-components",
        determinant
      };
    const v = actualVoice / voiceNorm, b = actualMusic / musicNorm;
    voiceGain = (v - correlation * b) / (determinant * voiceNorm);
    musicGain = (b - correlation * v) / (determinant * musicNorm);
  }
  const globalMatch = encodedAudioMatches(actual, expected);
  const componentMatch = (voiceGain === null || Math.abs(voiceGain - 1) <= componentBounds.voiceGainError) && Math.abs(musicGain - 1) <= componentBounds.musicGainError;
  return {
    passed: globalMatch && componentMatch,
    reason: !globalMatch ? "global-codec-error" : !componentMatch ? "component-gain" : "matched",
    voiceGain,
    musicGain,
    determinant,
    globalMatch
  };
}
async function movieAudio(path) {
  const { stdout } = await execute(
    "ffmpeg",
    [
      "-v",
      "error",
      "-threads",
      "1",
      "-i",
      path,
      "-map",
      "0:a:0",
      "-t",
      "9",
      "-ar",
      "48000",
      "-ac",
      "2",
      "-f",
      "f32le",
      "pipe:1"
    ],
    { encoding: "buffer", timeout: 3e4, maxBuffer: 4e6 }
  );
  return Float32Array.from(
    { length: stdout.length / 4 },
    (_, i) => stdout.readFloatLE(i * 4)
  );
}
async function videoBytes(path) {
  return (await execute(
    "ffmpeg",
    [
      "-v",
      "error",
      "-threads",
      "1",
      "-i",
      path,
      "-an",
      "-vf",
      "scale=160:90",
      "-frames:v",
      "120",
      "-threads",
      "1",
      "-pix_fmt",
      "rgb24",
      "-f",
      "rawvideo",
      "pipe:1"
    ],
    { encoding: "buffer", timeout: 3e4, maxBuffer: 6e6 }
  )).stdout;
}
function sameVideo(a, b) {
  if (!a.length || a.length !== b.length || a.length % (160 * 90 * 3))
    return false;
  for (let frame = 0; frame < a.length / (160 * 90 * 3); frame++)
    for (let ty = 0; ty < 9; ty++)
      for (let tx = 0; tx < 16; tx++) {
        let difference = 0;
        for (let y = ty * 10; y < (ty + 1) * 10; y++)
          for (let x = tx * 10; x < (tx + 1) * 10; x++)
            for (let c = 0; c < 3; c++) {
              const i = ((frame * 90 + y) * 160 + x) * 3 + c;
              difference += Math.abs(a[i] - b[i]);
            }
        if (difference / 300 > 4) return false;
      }
  return true;
}
async function verify2(ctx) {
  const m = ctx.metadata;
  const inheritedChecks = await music.verify(musicContext(ctx));
  const musicChecks = localizeMediaRegisterChecks(
    inheritedChecks,
    await criterion(
      "media current register retains all approved facts in original rows with equivalent units and typography",
      async () => mediaRegisterFieldsMatch(
        await registerFields(ctx.work),
        m.register.final
      )
    ),
    await criterion(
      "media current register has no explicitly hidden current text runs",
      async () => mediaRegisterCurrentRowsVisible(
        (await docx(resolve19(ctx.work, "delivery.docx"))).raw
      )
    )
  );
  const d = JSON.parse(
    await readFile14(resolve19(ctx.work, filmProject), "utf8")
  );
  const contract = filmStateContract(d, m, ctx.work);
  const originalAssetsSafe = (async () => {
    for (const [name, hash] of Object.entries(m.film.media_hashes))
      if (await audioFileDigest(resolve19(ctx.work, name)) !== hash)
        return false;
    return true;
  })();
  const masterSafe = (async () => {
    const clip = current(d, m).audio_tracks[1].items[0];
    const path = d.project.items[String(clip.item)]?.kind.Media?.media.File.path;
    if (!path || resolve19(ctx.work, path) !== resolve19(ctx.work, "master.wav"))
      return false;
    const a = await decodeAudio(resolve19(ctx.work, path));
    return a.rate === audioRate && a.channels === 2 && a.codec === "pcm_s16le" && a.duration === 12 && signalMatches(a.samples, expectedMusicMix("stems"));
  })();
  let temp, render2;
  const rendered = () => render2 ??= (async () => {
    if (!Object.values(contract).every(Boolean) || !await originalAssetsSafe || !await masterSafe)
      throw Error(
        "Saved Film state violates the bounded native project contract"
      );
    temp = await mkdtemp11(resolve19(ctx.work, ".media-verify-"));
    await withApplication(
      "filmcraft",
      ctx.work,
      async (c) => {
        await filmCommand(c, "file.open", {
          path: resolve19(ctx.work, filmProject)
        });
        await exportFilm(c, temp, m.film.sequence, "independent");
      },
      ctx.env
    );
    return resolve19(temp, "independent.mp4");
  })();
  try {
    const checks = await Promise.all([
      ...Object.entries(contract).map(
        ([key3, passed]) => criterion(
          `saved Film ${key3} satisfies R3 and protected native history`,
          () => passed
        )
      ),
      criterion(
        "original Film media and attribution remain byte-identical",
        () => originalAssetsSafe
      ),
      criterion(
        "Film current A2 references the actual approved 12s master signal, not a stale file with an approved name",
        () => masterSafe
      ),
      criterion(
        "native Film render independently contains original narration plus first9s of the approved music at -18dB",
        async () => {
          const narration = await decodeNarrationAtOutputRate(
            resolve19(ctx.work, "narration.wav")
          );
          return compositeAudioMatches(
            await movieAudio(await rendered()),
            narration
          );
        }
      ),
      criterion(
        "delivered movie independently contains the approved narration/music composite",
        async () => {
          const narration = await decodeNarrationAtOutputRate(
            resolve19(ctx.work, "narration.wav")
          );
          return compositeAudioMatches(
            await movieAudio(resolve19(ctx.work, "studio-final.mp4")),
            narration
          );
        }
      ),
      criterion(
        "delivered movie format is nine-second H264 320x180 12fps stereo48kHz",
        async () => {
          const info = JSON.parse(
            (await execute(
              "ffprobe",
              [
                "-v",
                "error",
                "-count_frames",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                resolve19(ctx.work, "studio-final.mp4")
              ],
              { timeout: 3e4 }
            )).stdout
          );
          const v = info.streams.find(
            (s2) => s2.codec_type === "video"
          ), a = info.streams.find(
            (s2) => s2.codec_type === "audio"
          );
          return v?.codec_name === "h264" && v.width === 320 && v.height === 180 && v.avg_frame_rate === "12/1" && +v.nb_read_frames === 108 && a?.channels === 2 && +a.sample_rate === audioRate && Math.abs(+info.format.duration - 9) < 0.1;
        }
      ),
      criterion(
        "all delivered movie frames match the saved native picture and burned captions",
        async () => sameVideo(
          await videoBytes(resolve19(ctx.work, "studio-final.mp4")),
          await videoBytes(await rendered())
        )
      ),
      criterion(
        "delivered soundtrack agrees with an independent saved-native render",
        async () => encodedAudioMatches(
          await movieAudio(resolve19(ctx.work, "studio-final.mp4")),
          await movieAudio(await rendered())
        )
      ),
      criterion(
        "SRT sidecar has precisely the three R3 caption texts and timings",
        async () => {
          const text = (await readFile14(resolve19(ctx.work, "studio-final.srt"), "utf8")).replaceAll("\r", "").trim();
          const format = (n) => `00:00:${String(Math.floor(n)).padStart(2, "0")},${String(Math.round(n % 1 * 1e3)).padStart(3, "0")}`;
          return text.split(/\n\s*\n/).length === 3 && captions.every(
            (cue, i) => text.split(/\n\s*\n/)[i] === `${i + 1}
${format(cue.start)} --> ${format(cue.end)}
${cue.text}`
          );
        }
      ),
      criterion("poster is the saved native program frame at2s", async () => {
        const path = resolve19(ctx.work, "studio-final.png");
        return await pngEvidence(path, 320, 180) && sameVideo(
          await videoBytes(path),
          await videoBytes(
            resolve19(dirname5(await rendered()), "independent.png")
          )
        );
      })
    ]);
    return [...musicChecks, ...checks];
  } finally {
    if (temp) await rm11(temp, { recursive: true, force: true });
  }
}
var workflow3 = {
  id: "music-to-video-release-pack",
  application: "audacity",
  applications: ["audacity", "filmcraft", "wordcraft"],
  views: [
    { application: "audacity", inputFile: "music-session.aup3" },
    { application: "filmcraft", inputFile: filmProject },
    { application: "wordcraft", inputFile: "review-notes.docx" }
  ],
  title: "Finish a multitrack music release, use its real master in a locked film and reconcile delivery approval",
  family: "production-long",
  split: "development",
  deadlineSeconds: 3600,
  mcpMode: "live",
  inputFile: "music-session.aup3",
  outputs: [
    "music-session.aup3",
    "master.wav",
    "master.flac",
    filmProject,
    "studio-final.mp4",
    "studio-final.srt",
    "studio-final.png",
    "delivery.docx",
    "delivery.pdf"
  ],
  brief: "Complete the existing music-to-video production using signed R3 in review-notes.docx, superseding obsolete R2. Correct the editable12s Audacity stems, gains/pans, alignment, fades and labels; preserve muted signed R1 and raw sources. Save the native project and48kHz stereo master.wav PCM16/master.flac. Import that actual mix into the existing9s FilmCraft current sequence: first9s on A2 at-18dB, original A1 narration0dB/master0dB. Retain locked picture, voice identity/source/effects, archive, old source assets and credits. Correct the existing three R3 captions in place. Save studio-cut.fcproj; export studio-final.mp4 H264320x18012fps stereo48kHz burned captions, matching SRT and2s PNG poster. Reconcile only current delivery.docx fields and export delivery.pdf; keep signed history. Publication remains pending Leah's campaign sign-off. This is an authored realistic scenario, not a real user trace.",
  mcpGuidance: "Use assigned audacity__, filmcraft__ and wordcraft__ native tools. Finish and save/export Audacity before importing its real master through FilmCraft media_import; inspect the new asset identity and use native timeline.place overwrite on A2 with9s duration. Preserve all other edit identities. No shell-generated replacement project or movie is a native edit.",
  prepare: prepare2,
  prepareTrial: async (work, env, metadata) => filmCampaign.prepareTrial?.(work, env, metadata.film) ?? {},
  verify: verify2
};
var changeFilm = (fn) => async (ctx) => {
  const path = resolve19(ctx.work, filmProject), d = JSON.parse(await readFile14(path, "utf8"));
  fn(d, ctx.metadata);
  await writeFile6(path, JSON.stringify(d));
};
function appendEmptyVideo(d, m) {
  const id = d.project.next_id;
  if (typeof id !== "number" || !Number.isSafeInteger(id))
    throw Error("Missing native allocator");
  const track = defaultEmptyVideoTrack(id, "Video 4");
  current(d, m).video_tracks.push(track);
  d.project.next_id = id + 1;
  return track;
}
var cases = [
  {
    id: "equivalent-gui-playhead-and-track-layout",
    expected: true,
    apply: changeFilm((d, m) => {
      d.view = {
        sequences: {
          [m.film.sequence]: {
            pps: 51.756404079861106,
            scroll: 0,
            v_scroll: 0,
            a_scroll: 80,
            video_track_h: 60,
            audio_track_h: 56
          }
        },
        playheads: { [m.film.sequence]: seconds(2) }
      };
    })
  },
  {
    id: "changed-project-identity-with-gui-view",
    expected: false,
    apply: changeFilm((d, m) => {
      d.view = { playheads: { [m.film.sequence]: seconds(2) } };
      d.project.name = String(d.project.name) + " / amended";
    })
  },
  {
    id: "equivalent-appended-default-empty-video-track",
    expected: true,
    apply: changeFilm((d, m) => {
      appendEmptyVideo(d, m);
    })
  },
  {
    id: "appended-video-track-with-content",
    expected: false,
    apply: changeFilm((d, m) => {
      const track = appendEmptyVideo(d, m);
      const source4 = current(d, m).video_tracks.flatMap((t) => t.items).at(-1);
      if (!source4) throw Error("Missing protected picture source");
      const id = d.project.next_id;
      track.items.push({ ...structuredClone(source4), id, start: 0 });
      d.project.next_id = id + 1;
    })
  },
  {
    id: "appended-empty-video-track-with-effects",
    expected: false,
    apply: changeFilm((d, m) => {
      appendEmptyVideo(d, m).effects = [
        {
          effect: "volume",
          enabled: true,
          params: { level: { value: { Float: -6 } } }
        }
      ];
    })
  },
  {
    id: "appended-empty-video-track-with-routing",
    expected: false,
    apply: changeFilm((d, m) => {
      const track = appendEmptyVideo(d, m);
      track.mixer.output = current(
        d,
        m
      ).audio_tracks[0].id;
    })
  },
  {
    id: "appended-empty-video-track-with-duplicate-id",
    expected: false,
    apply: changeFilm((d, m) => {
      appendEmptyVideo(d, m).id = current(d, m).video_tracks[0].id;
    })
  },
  {
    id: "equivalent-native-register-units-and-clause-order",
    expected: true,
    apply: async (ctx) => {
      const original3 = ctx.metadata.register.final;
      const equivalent = mediaRegisterEquivalentRows(original3);
      await withApplication(
        "wordcraft",
        ctx.work,
        async (c) => {
          await call(c, "open_document", {
            path: resolve19(ctx.work, "delivery.docx")
          });
          for (const [i, before] of original3.entries())
            if (before !== equivalent[i]) {
              await call(c, "select_text", { text: before, occurrence: 1 });
              await call(c, "type_text", {
                text: equivalent[i],
                paragraphs: true
              });
            }
          for (const name of ["delivery.docx", "delivery.pdf"])
            await call(c, "save_document", { path: resolve19(ctx.work, name) });
        },
        ctx.env
      );
    }
  },
  {
    id: "direct-hidden-current-register-rows",
    expected: false,
    apply: async (ctx) => {
      const path = resolve19(ctx.work, "delivery.docx"), zip = unzipSync6(await readFile14(path));
      zip["word/document.xml"] = strToU82(
        hideMediaRegisterCurrentRows(strFromU86(zip["word/document.xml"]))
      );
      await writeFile6(path, zipSync2(zip));
    }
  },
  ...musicCalibrationCases(music.id).filter(
    (c) => [
      "equivalent-native-clip-colour",
      "equivalent-unsaved-gain-is-not-saved-delivery",
      "wrong-native-mix-gain",
      "unmuted-signed-archive",
      "stale-master-waveform"
    ].includes(c.id)
  ).map((c) => ({
    ...c,
    apply: (ctx, fixture) => c.apply(musicContext(ctx), fixture)
  })),
  {
    id: "stale-film-wav-despite-correct-master",
    expected: false,
    apply: changeFilm((d, m) => {
      current(d, m).audio_tracks[1].items[0].item = current(
        m.film.original,
        m
      ).audio_tracks[1].items[0].item;
    })
  },
  {
    id: "unsaved-correct-mix-with-stale-aup3",
    expected: false,
    apply: async (ctx, fixture) => {
      await copyFile5(
        resolve19(fixture, "music-session.aup3"),
        resolve19(ctx.work, "music-session.aup3")
      );
      await rm11(resolve19(ctx.work, "music-session.aup3-wal"), { force: true });
    }
  },
  {
    id: "wrong-film-bed-gain",
    expected: false,
    apply: changeFilm((d, m) => {
      current(d, m).audio_tracks[1].items[0].gain_db = -9;
    })
  },
  {
    id: "caption-timing-changed",
    expected: false,
    apply: changeFilm((d, m) => {
      current(d, m).caption_tracks[0].captions[0].duration -= seconds(0.5);
    })
  },
  {
    id: "protected-film-archive-changed",
    expected: false,
    apply: changeFilm((d, m) => {
      filmSequence(d, m.film.archive).video_tracks[0].items[0].source_in += seconds(1 / 12);
    })
  },
  {
    id: "stale-movie-despite-correct-native-edits",
    expected: false,
    apply: async (ctx) => {
      await copyFile5(
        resolve19(ctx.work, "review-r1.mp4"),
        resolve19(ctx.work, "studio-final.mp4")
      );
    }
  },
  {
    id: "delivered-movie-missing-bed-with-correct-native-master",
    expected: false,
    apply: async (ctx) => {
      const movie = resolve19(ctx.work, "studio-final.mp4");
      const temp = await mkdtemp11(resolve19(ctx.work, ".missing-bed-control-"));
      try {
        const info = JSON.parse(
          (await execute(
            "ffprobe",
            ["-v", "error", "-show_streams", "-of", "json", movie],
            { timeout: 15e3 }
          )).stdout
        );
        const audio = info.streams.find(
          (s2) => s2.codec_type === "audio"
        );
        if (audio?.codec_name !== "aac" || +audio.sample_rate !== audioRate || audio.channels !== 2)
          throw Error(
            "Missing-bed control requires the correct stereo48kHz AAC delivery baseline"
          );
        const bitrate = Number(audio.bit_rate) > 0 ? String(audio.bit_rate) : "192000";
        const voiceInfo = JSON.parse(
          (await execute(
            "ffprobe",
            [
              "-v",
              "error",
              "-select_streams",
              "a:0",
              "-show_entries",
              "stream=channels",
              "-of",
              "json",
              resolve19(ctx.work, "narration.wav")
            ],
            { timeout: 15e3 }
          )).stdout
        );
        const voiceChannels = voiceInfo.streams[0]?.channels;
        if (![1, 2].includes(voiceChannels))
          throw Error("Unsupported preserved narration channel layout");
        const replacement = resolve19(temp, "narration-only.mp4");
        await execute(
          "ffmpeg",
          [
            "-y",
            "-v",
            "error",
            "-threads",
            "1",
            "-i",
            movie,
            "-i",
            resolve19(ctx.work, "narration.wav"),
            "-map",
            "0",
            "-map",
            "-0:a",
            "-map",
            "1:a:0",
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
            "-c",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            bitrate,
            ...voiceChannels === 1 ? ["-af", "pan=stereo|c0=c0|c1=c0"] : [],
            "-ar",
            String(audioRate),
            "-ac",
            "2",
            "-t",
            "9",
            "-movflags",
            "+faststart",
            replacement
          ],
          { timeout: 3e4, maxBuffer: 1e6 }
        );
        await rename2(replacement, movie);
      } finally {
        await rm11(temp, { recursive: true, force: true });
      }
    }
  },
  {
    id: "missing-register-pdf",
    expected: false,
    apply: async (ctx) => {
      await rm11(resolve19(ctx.work, "delivery.pdf"));
    }
  },
  {
    id: "premature-publication-gate",
    expected: false,
    apply: async (ctx) => {
      await withApplication(
        "wordcraft",
        ctx.work,
        async (c) => {
          await call(c, "open_document", {
            path: resolve19(ctx.work, "delivery.docx")
          });
          await call(c, "select_text", {
            text: "Gate: Publish only after Leah's campaign sign-off",
            occurrence: 1
          });
          await call(c, "type_text", {
            text: "Gate: Approved for immediate publication",
            paragraphs: true
          });
          await call(c, "save_document", {
            path: resolve19(ctx.work, "delivery.docx")
          });
        },
        ctx.env
      );
    }
  }
];
var screeningContext = (ctx) => ({
  ...ctx,
  metadata: ctx.metadata.media
});
var screening = {
  ...workflow3,
  id: "soundtrack-to-screening-approval-pack",
  title: "Carry an editable soundtrack through a locked film, screening briefing and approval register",
  family: "dependent-handoff",
  split: "held-out",
  applications: ["audacity", "filmcraft", "deckcraft", "wordcraft"],
  views: [
    ...workflow3.views,
    { application: "deckcraft", inputFile: "sales-briefing.pptx" }
  ],
  outputs: [...workflow3.outputs, "sales-briefing.pptx", "sales-briefing.pdf"],
  brief: workflow3.brief + "\n" + [
    "The screening producer also needs the EXISTING four-slide sales-briefing.pptx completed. This is one dependent handoff: editable music -> actual WAV -> mixed movie -> actual 2-second poster -> screening deck -> delivery register. Complete the soundtrack and film before inserting the ACTUAL studio-final.png in Current visual; do not use the old review image or a different movie frame.",
    "Replace the Current visual placeholder with one visible, undistorted embedded poster inside the slide. Keep all four slides in order, the introduction, signed R1 archive and footer unchanged. Reconcile Publication decision: signed R3; studio-final.png at 320 x 180 px; exports master.wav, master.flac, studio-final.mp4, studio-final.srt and studio-final.png; owner Leah; Leah's campaign sign-off before publication is still pending. Save the editable briefing and export all four pages to sales-briefing.pdf.",
    "The delivery register must describe the actual audio settings, not the desired settings if the movie was exported incorrectly. Preparing the screening briefing does not authorize publication. Keep every source and historical exception required by R3."
  ].join("\n"),
  mcpGuidance: workflow3.mcpGuidance + " Then use deckcraft__ tools to insert the actual native movie poster, update the existing decision slide and export the full briefing. Use only the four assigned applications.",
  async prepare(work, env) {
    const base = await workflow3.prepare(work, env);
    const deck = await prepareVisualDeck(
      work,
      "studio-final.png",
      [180, 150, 560, 315],
      screeningFacts,
      "Reef screening review"
    );
    return { media: base, deck };
  },
  prepareTrial: (work, env, metadata) => workflow3.prepareTrial?.(work, env, metadata.media) ?? Promise.resolve({}),
  async verify(ctx) {
    return [
      ...await workflow3.verify(screeningContext(ctx)),
      ...await verifyVisualDeck(ctx, ctx.metadata.deck)
    ];
  }
};
import { copyFile as copyFile7, readFile as readFile17, unlink as unlink7, writeFile as writeFile10 } from "node:fs/promises";
import { resolve as resolve29 } from "node:path";
import { unzipSync as unzipSync8, zipSync as zipSync4, strFromU8 as strFromU88, strToU8 as strToU84 } from "fflate";
import { copyFile as copyFile6, readFile as readFile16, unlink as unlink6, writeFile as writeFile9 } from "node:fs/promises";
import { resolve as resolve28 } from "node:path";
import { isDeepStrictEqual as isDeepStrictEqual11 } from "node:util";
import { strFromU8 as strFromU87, strToU8 as strToU83, unzipSync as unzipSync7, zipSync as zipSync3 } from "fflate";
import { XMLParser as XMLParser5 } from "fast-xml-parser";
import { resolve as resolve25 } from "node:path";
import { writeFile as writeFile8, unlink as unlink5 } from "node:fs/promises";
import { isDeepStrictEqual as isDeepStrictEqual10 } from "node:util";
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
  const covers = (source4, target3) => {
    const grid = /* @__PURE__ */ new Map();
    for (const p of target3) {
      const key3 = p.map((v) => Math.floor(v / tolerance)).join(",");
      const bucket = grid.get(key3) ?? [];
      bucket.push(p);
      grid.set(key3, bucket);
    }
    return source4.every((p) => {
      const [x, y, z3] = p.map((v) => Math.floor(v / tolerance));
      for (let dx = -1; dx <= 1; dx++)
        for (let dy = -1; dy <= 1; dy++)
          for (let dz = -1; dz <= 1; dz++) {
            if (grid.get([x + dx, y + dy, z3 + dz].join(","))?.some(
              (q) => q.every((v, i) => Math.abs(v - p[i]) <= tolerance)
            ))
              return true;
          }
      return false;
    });
  };
  return covers(a, b) && covers(b, a);
}
import { resolve as resolve22 } from "node:path";
import { resolve as resolve20 } from "node:path";
import { unlink as unlink4 } from "node:fs/promises";
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
    const path = resolve20(work, "mounting-plate.FCStd");
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
    await unlink4(resolve20(work, "mounting-plate.FCStd1")).catch(
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
          const s2 = (await state).step;
          return s2?.valid && s2.solids === 1 && closeTo(s2.volume, volume, 0.1) && vectorClose(s2.bounds, [0, 0, 0, 100, 60, 5], 0.01);
        }
      ),
      criterion(
        "STL reimport is closed and matches the final plate volume",
        async () => {
          const s2 = (await state).stl;
          return s2?.solid && s2.facets > 0 && closeTo(s2.volume, volume, volume * 2e-3);
        }
      )
    ]);
  }
};
import { resolve as resolve21 } from "node:path";
async function solvePlate(c, work) {
  const path = resolve21(work, "mounting-plate.FCStd");
  await call(c, "inspect_document", { path });
  await call(c, "set_dimensions", {
    path,
    object_id: "PlateStock",
    dimensions: { Length: 100, Width: 60, Height: 5 }
  });
  for (const [object_id, x] of [
    ["LeftHole", 12],
    ["RightHole", 88]
  ]) {
    await call(c, "set_dimensions", {
      path,
      object_id,
      dimensions: { Radius: 3 }
    });
    await call(c, "set_placement", { path, object_id, x, y: 30, z: -2 });
  }
  for (const ext of ["step", "stl"])
    await call(c, "export_objects", {
      path,
      object_ids: ["MountingPlate"],
      output: resolve21(work, `mounting-plate.${ext}`)
    });
}
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
function intact(s2) {
  const o = s2.objects;
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
function linked(s2) {
  const o = s2.objects;
  return o.LeftClearance.type === "Part::Cut" && o.LeftClearance.base === "PlateStock" && o.LeftClearance.tool === "LeftHole" && o.MountingPlate.type === "Part::Cut" && o.MountingPlate.base === "LeftClearance" && o.MountingPlate.tool === "RightHole";
}
function names2(kind) {
  return kind === "slot" ? ["CableSlot", "CablePlate"] : posts.flatMap((_, i) => ["Standoff" + (i + 1), "Raised" + (i + 1)]);
}
function featureRoles(state, labels2) {
  const byLabel = /* @__PURE__ */ new Map(), byId = /* @__PURE__ */ new Map();
  for (const label of labels2) {
    const matches2 = Object.entries(state.objects).filter(
      ([id, o]) => (o.label ?? id) === label
    );
    if (matches2.length > 1) throw Error("Ambiguous feature label: " + label);
    if (matches2.length === 1) {
      byLabel.set(label, matches2[0][0]);
      byId.set(matches2[0][0], label);
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
async function solvePlateVariant(kind, c, work) {
  await solvePlate(c, work);
  const path = resolve22(work, "mounting-plate.FCStd");
  if (kind === "slot") {
    await call(c, "add_primitive", {
      path,
      kind: "box",
      name: "CableSlot",
      length: 20,
      width: 8,
      height: 9
    });
    await call(c, "set_placement", {
      path,
      object_id: "CableSlot",
      x: 40,
      y: 26,
      z: -2
    });
    await call(c, "boolean_operation", {
      path,
      operation: "cut",
      base_id: "MountingPlate",
      tool_id: "CableSlot",
      name: "CablePlate"
    });
  } else
    for (const [i, [x, y, z3]] of posts.entries()) {
      await call(c, "add_primitive", {
        path,
        kind: "cylinder",
        name: "Standoff" + (i + 1),
        radius: 4,
        height: 10
      });
      await call(c, "set_placement", {
        path,
        object_id: "Standoff" + (i + 1),
        x,
        y,
        z: z3
      });
      await call(c, "boolean_operation", {
        path,
        operation: "fuse",
        base_id: i ? "Raised" + i : "MountingPlate",
        tool_id: "Standoff" + (i + 1),
        name: "Raised" + (i + 1)
      });
    }
  for (const ext of ["step", "stl"])
    await call(c, "export_objects", {
      path,
      object_ids: [kind === "slot" ? "CablePlate" : "Raised4"],
      output: resolve22(work, "mounting-plate." + ext)
    });
  await call(c, "inspect_document", { path });
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
        const o = featureRoles(await state, names2(kind)).objects;
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
        const o = featureRoles(await state, names2(kind)).objects;
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
        const raw = await state, roles2 = featureRoles(raw, names2(kind));
        return Object.keys(raw.objects).length === original.length + names2(kind).length && original.every((id) => raw.objects[id]) && roles2.ids.length === names2(kind).length && new Set(roles2.ids).size === roles2.ids.length && roles2.ids.every((id) => !original.includes(id));
      }
    ),
    criterion(
      "the final feature is one valid solid with the expected bounds and volume",
      async () => {
        const o = featureRoles(await state, names2(kind)).objects[kind === "slot" ? "CablePlate" : "Raised4"];
        return o.valid && o.solids === 1 && closeTo(o.volume, volume2, 0.05) && vectorClose(o.bounds, bounds);
      }
    ),
    criterion(
      "STEP reimport contains only the final modified solid",
      async () => {
        const s2 = (await state).step;
        return s2?.valid && s2.solids === 1 && closeTo(s2.volume, volume2, 0.1) && vectorClose(s2.bounds, bounds, 0.01);
      }
    ),
    criterion(
      "STL reimport is closed and has the expected modified volume",
      async () => {
        const s2 = (await state).stl;
        return s2?.solid && s2.facets > 0 && closeTo(s2.volume, volume2, volume2 * 2e-3);
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
import { createHash as createHash3 } from "node:crypto";
import { writeFile as writeFile7 } from "node:fs/promises";
import { resolve as resolve24 } from "node:path";
import { readFile as readFile15 } from "node:fs/promises";
import { resolve as resolve23 } from "node:path";
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
  const target3 = new URL(env.MATTERMOST_URL);
  if (!["127.0.0.1", "localhost"].includes(target3.hostname) && !(env.REEF_HARBOR_SERVICE_FIXTURE === "1" && target3.hostname === "mattermost"))
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
      new Blob([await readFile15(resolve23(work, "QA-summary.txt"))]),
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
    await writeFile7(resolve24(work, "QA-summary.txt"), attachment);
    await writeFile7(
      resolve24(work, "seed-discussion.json"),
      JSON.stringify(seed, null, 2) + "\n"
    );
    return {
      dataset: "release-54-corrected-handoff",
      attachment_sha256: createHash3("sha256").update(attachment).digest("hex")
    };
  },
  prepareTrial: (work, env) => seedReleaseDiscussion(work, env),
  async verify(context) {
    const meta = context.metadata;
    const posts3 = async () => {
      const result = await api(
        context.env,
        "GET",
        `/channels/${meta.channel_id}/posts?per_page=200`
      );
      return Object.values(result.posts);
    };
    const replies = async () => (await posts3()).filter(
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
            if (response.ok && createHash3("sha256").update(Buffer.from(await response.arrayBuffer())).digest("hex") === meta.attachment_sha256)
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
          const current2 = await posts3();
          return meta.originals.every(
            (p) => current2.some((q) => q.id === p.id && q.message === p.message)
          ) && !current2.some(
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
  const stat2 = (id, title4, calc, x) => ({
    id,
    title: title4,
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
    stat2(4, "Latest p95 latency", "lastNotNull", 0),
    stat2(5, "Peak p95 latency", "max", 12)
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
  const posts3 = Object.values(r.posts).filter(
    (p) => p.user_id === m.actor_id && !p.type
  );
  return {
    posts: posts3,
    body: posts3.filter((p) => p.root_id === m.root_id).map((p) => p.message).join(" ")
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
    await writeFile8(
      resolve25(work, "monitoring-request.txt"),
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
    const number2 = (body, label, unit) => Number(
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
            return isDeepStrictEqual10(await latency, samples.latency) && isDeepStrictEqual10(await errors, samples.errors);
          }
        ),
        criterion(
          "handoff latest and peak p95 match actual source data",
          async () => {
            const body = (await reply).body, values = await latency;
            return closeTo(number2(body, "Latest p95", "ms"), values.at(-1)) && closeTo(number2(body, "Peak p95", "ms"), Math.max(...values));
          }
        ),
        criterion(
          "handoff error percentage and threshold assessment match the source",
          async () => {
            const body = (await reply).body, latest = (await errors).at(-1), d = await dashboard;
            const threshold = (id) => d.panels.find((p) => p.id === id)?.fieldConfig?.defaults.thresholds?.steps.find(
              (s2) => s2.color === "red"
            )?.value;
            const limit = threshold(4), errorLimit = threshold(2);
            if (typeof limit !== "number" || typeof errorLimit !== "number")
              return false;
            const status = Math.max(...await latency) < limit && latest < errorLimit ? "within thresholds" : "breached";
            return closeTo(number2(body, "Error rate", "%"), latest) && body.toLowerCase().includes("monitoring: " + status);
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
          async () => isDeepStrictEqual10(await dashboard, g.protected_dashboard)
        ),
        criterion(
          "the archive dashboard is unchanged",
          async () => isDeepStrictEqual10(
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
    const m = await cableSlot.prepare(work, env), path = resolve25(work, "cad-hero.blend");
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
    await unlink5(resolve25(work, "cad-hero.blend1")).catch(
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
            const s2 = (await cad).stl, o = (await scene).objects.CadPlate;
            if (!s2?.vertices || !o.world_vertices || !o.triangles) return false;
            const normalized = o.world_vertices.map((p) => [
              (p[0] + 1) / 0.02,
              (p[1] + 0.6) / 0.02,
              (p[2] - 0.15) / 0.02
            ]);
            return samePointCloud(s2.vertices, normalized) && closeTo(
              triangleVolume(o.world_vertices, o.triangles),
              s2.volume * 0.02 ** 3,
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
            const s2 = await scene;
            return s2.render_engine === "CYCLES" && s2.device === "CPU" && s2.samples === 16 && vectorClose(s2.resolution, [640, 360, 100]);
          }
        ),
        criterion(
          "the delivered PNG has the required nonblank content and size",
          () => pngEvidence(resolve25(context.work, "cad-hero.png"), 640, 360)
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
import { lstat } from "node:fs/promises";
import { resolve as resolve26 } from "node:path";
var CAD_EXPORT_LIMITS = {
  fileBytes: 16 * 1024 * 1024,
  masterFaces: 256,
  stepFaces: 1e3,
  vertices: 1e4,
  facets: 2e4,
  surfaceSamples: 9e4
};
var CAD_EXPORT_TOLERANCES = {
  stepVolumeMm3: 0.1,
  stlSurfaceMm: 0.1,
  stlVolumeFraction: 2e-3
};
var finite = (...values) => values.every((v) => Number.isFinite(v) && v >= 0);
var integer = (n, max) => Number.isInteger(n) && n > 0 && n <= max;
function stepMatchesCadMaster(state) {
  const m = state.master, s2 = state.step, t = CAD_EXPORT_TOLERANCES.stepVolumeMm3;
  return !!s2 && m.valid && m.solids === 1 && integer(m.faces, CAD_EXPORT_LIMITS.masterFaces) && s2.valid && s2.solids === 1 && integer(s2.faces, CAD_EXPORT_LIMITS.stepFaces) && finite(
    m.volume_mm3,
    s2.volume_mm3,
    s2.common_volume_mm3,
    s2.master_only_volume_mm3,
    s2.export_only_volume_mm3
  ) && m.volume_mm3 > 0 && Math.abs(m.volume_mm3 - s2.volume_mm3) <= t && s2.common_volume_mm3 <= Math.min(m.volume_mm3, s2.volume_mm3) + t && Math.abs(s2.common_volume_mm3 - m.volume_mm3) <= t && s2.master_only_volume_mm3 + s2.export_only_volume_mm3 <= t;
}
function stlMatchesCadMaster(state) {
  const m = state.master, s2 = state.stl;
  return !!s2 && m.valid && m.solids === 1 && integer(m.faces, CAD_EXPORT_LIMITS.masterFaces) && s2.closed && s2.surface_coverage_complete && integer(s2.vertices, CAD_EXPORT_LIMITS.vertices) && integer(s2.facets, CAD_EXPORT_LIMITS.facets) && s2.vertices_checked === s2.vertices && s2.facet_samples_checked === s2.facets * 4 && s2.vertices_checked + s2.facet_samples_checked <= CAD_EXPORT_LIMITS.surfaceSamples && finite(
    m.volume_mm3,
    s2.volume_mm3,
    s2.max_vertex_distance_mm,
    s2.max_facet_distance_mm
  ) && m.volume_mm3 > 0 && Math.abs(s2.volume_mm3 - m.volume_mm3) <= m.volume_mm3 * CAD_EXPORT_TOLERANCES.stlVolumeFraction && s2.max_vertex_distance_mm <= CAD_EXPORT_TOLERANCES.stlSurfaceMm && s2.max_facet_distance_mm <= CAD_EXPORT_TOLERANCES.stlSurfaceMm;
}
async function readCadExportState(work) {
  const paths = {
    project: resolve26(work, "mounting-plate.FCStd"),
    step: resolve26(work, "mounting-plate.step"),
    stl: resolve26(work, "mounting-plate.stl")
  };
  const exists = {};
  for (const [name, path] of Object.entries(paths)) {
    try {
      const info = await lstat(path);
      if (!info.isFile() || info.size <= 0 || info.size > CAD_EXPORT_LIMITS.fileBytes)
        throw Error("CAD inspection requires a bounded regular file: " + name);
      exists[name] = true;
    } catch (error) {
      if (error.code !== "ENOENT" || name === "project")
        throw error;
      exists[name] = false;
    }
  }
  return new NativeEngine("freecad").run(
    [
      "import math",
      "doc=App.openDocument(p['project']); doc.recompute()",
      "roles=[o for o in doc.Objects if o.Label=='Raised4']; assert len(roles)==1, 'Ambiguous final CAD role'",
      "master=roles[0].Shape; assert not master.isNull(), 'Final CAD shape is empty'",
      "assert len(master.Faces)<=p['limits']['masterFaces'], 'Master face budget exceeded'",
      "result={'master':{'valid':master.isValid(),'solids':len(master.Solids),'volume_mm3':master.Volume,'faces':len(master.Faces)},'limits':p['limits'],'tolerances':p['tolerances']}",
      "if p['has_step']:",
      "    exported=Part.read(p['step']); assert len(exported.Faces)<=p['limits']['stepFaces'], 'STEP face budget exceeded'",
      "    common=master.common(exported); missing=master.cut(exported); additional=exported.cut(master)",
      "    result['step']={'valid':exported.isValid(),'solids':len(exported.Solids),'faces':len(exported.Faces),'volume_mm3':exported.Volume,'common_volume_mm3':common.Volume,'master_only_volume_mm3':missing.Volume,'export_only_volume_mm3':additional.Volume}",
      "if p['has_stl']:",
      "    import Mesh",
      "    mesh=Mesh.Mesh(p['stl']); points,facets=mesh.Topology",
      "    assert 0<len(points)<=p['limits']['vertices'] and 0<len(facets)<=p['limits']['facets'], 'STL geometry budget exceeded'",
      "    assert len(points)+len(facets)*4<=p['limits']['surfaceSamples'], 'STL sample budget exceeded'",
      "    assert all(all(math.isfinite(float(x)) for x in v) for v in points), 'Nonfinite STL point'",
      "    surface=Part.makeCompound(master.Faces)",
      "    def distance(v):",
      "        d=float(Part.Vertex(App.Vector(float(v[0]),float(v[1]),float(v[2]))).distToShape(surface)[0]); assert math.isfinite(d) and d>=0, 'Invalid surface distance'; return d",
      "    maximum_vertex=0.0; checked_vertices=0",
      "    for v in points:",
      "        maximum_vertex=max(maximum_vertex,distance(v)); checked_vertices+=1",
      "        if maximum_vertex>p['tolerances']['stlSurfaceMm']: break",
      "    maximum_facet=0.0; checked_facets=0",
      "    for triangle in facets:",
      "        assert len(triangle)==3 and all(0<=i<len(points) for i in triangle), 'Invalid STL topology'",
      "        a,b,c=[points[i] for i in triangle]",
      "        samples=[[(a[k]+b[k]+c[k])/3 for k in range(3)]]+[[ (u[k]+v[k])/2 for k in range(3)] for u,v in ((a,b),(b,c),(c,a))]",
      "        for sample in samples: maximum_facet=max(maximum_facet,distance(sample)); checked_facets+=1",
      "        if maximum_facet>p['tolerances']['stlSurfaceMm']: break",
      "    result['stl']={'closed':mesh.isSolid(),'volume_mm3':abs(mesh.Volume),'vertices':len(points),'facets':len(facets),'vertices_checked':checked_vertices,'facet_samples_checked':checked_facets,'max_vertex_distance_mm':maximum_vertex,'max_facet_distance_mm':maximum_facet,'surface_coverage_complete':checked_vertices==len(points) and checked_facets==len(facets)*4}"
    ],
    {
      ...paths,
      has_step: exists.step,
      has_stl: exists.stl,
      limits: CAD_EXPORT_LIMITS,
      tolerances: CAD_EXPORT_TOLERANCES
    }
  );
}
import { Client as Client2 } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport as StdioClientTransport2 } from "@modelcontextprotocol/sdk/client/stdio.js";
import { resolve as resolve27 } from "node:path";
async function withWorkflowApplications(workflow6, work, action, env = {}) {
  const applications = workflow6.applications ?? [workflow6.application];
  const connects = JSON.parse(env.REEF_APPLICATION_CONNECTS ?? "{}");
  if (applications.length === 1)
    return withApplication(
      workflow6.application,
      work,
      action,
      env,
      connects[workflow6.application]
    );
  const transport = new StdioClientTransport2({
    command: process.execPath,
    args: [resolve27(repoRoot, "dist/evaluation/application-server.js")],
    env: stringEnvironment({
      ...env,
      REEF_WORKSPACE: work,
      REEF_APPLICATIONS: JSON.stringify(applications)
    }),
    stderr: "inherit"
  });
  const client = new Client2({
    name: "reef-workflow-reference",
    version: "0.5.0"
  });
  try {
    await client.connect(transport);
    return await action(client);
  } finally {
    await client.close().catch(() => transport.close());
  }
}
var number = "[-+]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)(?:e[-+]?\\d+)?";
var negativeClaim = /\b(?:not|no|without|missing|invalid|incorrect|instead)\b/i;
function clauses(text) {
  if (text.length > 4096) return;
  const parts = [];
  let depth = 0, start = 0;
  for (let i = 0; i < text.length; i++) {
    if (text[i] === "(" && ++depth > 1) return;
    if (text[i] === ")" && --depth < 0) return;
    const commaBoundary = text[i] === "," && /^\s*(?:[a-z]|\()/i.test(text.slice(i + 1));
    if (!depth && (commaBoundary || text[i] === ";")) {
      parts.push(text.slice(start, i).trim());
      start = i + 1;
    }
    if (parts.length > 64) return;
  }
  if (depth) return;
  parts.push(text.slice(start).trim());
  const grouped = [];
  for (const part of parts.filter(Boolean)) {
    if (/^(?:and\s*)?\(/i.test(part) && grouped.length)
      grouped[grouped.length - 1] += ", " + part;
    else grouped.push(part);
  }
  return grouped;
}
function positions(text, expected) {
  if (!expected.length || expected.length > 16 || expected.some((p) => p.length !== 3 || !p.every(Number.isFinite)))
    return false;
  const tuple = new RegExp(
    "\\(\\s*(" + number + ")\\s*,\\s*(" + number + ")\\s*,\\s*(" + number + ")\\s*\\)",
    "gi"
  );
  const found = [...text.matchAll(tuple)].map((m) => m.slice(1).map(Number));
  const rest = text.replace(tuple, "").replace(/\band\b|[\s,]/gi, "");
  if (rest || found.length !== expected.length || found.some((p) => !p.every(Number.isFinite)))
    return false;
  const remaining = [...expected];
  return found.every((point) => {
    const i = remaining.findIndex((other) => vectorClose(point, other, 1e-6));
    if (i < 0) return false;
    remaining.splice(i, 1);
    return true;
  });
}
function plateFactParts(text, evidence) {
  const parts = clauses(text);
  if (!parts) return { base: "", valid: false };
  const [base = "", ...extra] = parts, seen = /* @__PURE__ */ new Set();
  let valid = !negativeClaim.test(text);
  for (const clause of extra) {
    const hole = new RegExp(
      "^radius(?:-(?=\\d)|\\s*[:=]?\\s*)(" + number + ")\\s*(?:mm\\s+)?through\\s+holes\\s+at\\s+(.+)$",
      "i"
    ).exec(clause);
    const height = new RegExp(
      "^cutter\\s+height\\s*[:=]?\\s*(" + number + ")\\s*mm$",
      "i"
    ).exec(clause);
    const board = new RegExp(
      "^board\\s*reference\\s*[:=]?\\s*(" + number + ")\\s*[x\xD7]\\s*(" + number + ")\\s*[x\xD7]\\s*(" + number + ")\\s*mm\\s+at\\s+(.+)$",
      "i"
    ).exec(clause);
    if (hole) {
      valid &&= !seen.has("holes") && !!evidence.holes?.length && evidence.holes.every((h) => closeTo(Number(hole[1]), h.radius, 1e-6)) && positions(
        hole[2],
        evidence.holes.map((h) => h.position)
      );
      seen.add("holes");
    } else if (height) {
      valid &&= !seen.has("height") && !!evidence.holes?.length && evidence.holes.every((h) => closeTo(Number(height[1]), h.height, 1e-6));
      seen.add("height");
    } else if (board) {
      const native3 = evidence.boardReference;
      valid &&= !seen.has("board") && !!native3 && native3.dimensions.length === 3 && native3.dimensions.every(Number.isFinite) && vectorClose(board.slice(1, 4).map(Number), native3.dimensions, 1e-6) && positions(board[4], [native3.position]);
      seen.add("board");
    } else valid = false;
  }
  return { base, valid };
}
function standoffFactParts(text, evidence) {
  const parts = clauses(text);
  if (!parts) return { base: "", valid: false };
  const base = [];
  let seen = false, valid = !negativeClaim.test(text);
  for (const clause of parts) {
    const located = /^(.*?)(?:\bat\b|\bpositions?\s*[:=]?)\s*(\(.+)$/i.exec(
      clause
    );
    if (!located) base.push(clause);
    else {
      if (located[1].trim()) base.push(located[1].trim());
      valid &&= !seen && !!evidence.standoffPositions?.length && positions(located[2], evidence.standoffPositions);
      seen = true;
    }
  }
  return { base: base.join("; "), valid };
}
function stackFactParts(text, evidence) {
  const [base = "", ...extra] = text.split(";").map((s2) => s2.trim()).filter(Boolean);
  if (!extra.length) return { base, valid: !negativeClaim.test(text) };
  if (extra.length !== 1 || negativeClaim.test(text))
    return { base, valid: false };
  const volume2 = "(?:\\d{1,3}(?:,\\d{3})+(?:\\.\\d+)?|(?:\\d+(?:\\.\\d*)?|\\.\\d+)(?:e[-+]?\\d+)?)";
  const match = new RegExp(
    "^raised\\s*4\\s+(?:is\\s+)?(?:one\\s+)?valid\\s+solid(?:\\s*,?\\s*(?:volume\\s*[:=]?\\s*)?(" + volume2 + ")\\s*mm(?:3|\\^3))?$",
    "i"
  ).exec(extra[0]);
  const final = evidence.finalSolid;
  if (!match || !final?.valid || final.solids !== 1 || !Number.isFinite(final.volume))
    return { base, valid: false };
  if (!match[1]) return { base, valid: true };
  const literal = match[1].replaceAll(",", ""), [mantissa, exponent = "0"] = literal.toLowerCase().split("e");
  const precision = mantissa.split(".")[1]?.length ?? 0;
  const tolerance = Math.min(
    CAD_EXPORT_TOLERANCES.stepVolumeMm3,
    0.5 * 10 ** (Number(exponent) - precision)
  );
  return {
    base,
    valid: closeTo(Number(literal), final.volume, tolerance + 1e-8)
  };
}
var scale = 1e-3;
var translation = [-0.05, -0.03, 0];
var target2 = [0, 0, 75e-4];
var camera = [0.16, -0.24, 0.2];
var labels = Array.from({ length: 4 }, (_, i) => [
  "Standoff" + (i + 1),
  "Raised" + (i + 1)
]).flat();
var installationFacts = [
  "Revision: signed R3",
  "Plate: 100 x 60 x 5 mm",
  "Standoffs: 4 cylinders; radius 4 mm; height 10 mm",
  "Stack: 15 mm total including the 5 mm plate",
  "Exports: mounting-plate.step; mounting-plate.stl; Raised4 only",
  "Transfer: 0.001 m/mm; bounds (-0.05,-0.03,0) to (0.05,0.03,0.015) m",
  "Render: cad-hero.png; 640 x 360; CPU Cycles; 16 samples; 100% resolution",
  "Owner: Nora",
  "Gate: Nora's engineering sign-off before release; pending"
];
var draft = [
  "Revision: draft R2",
  "Plate: 100 x 60 x 5 mm",
  "Standoffs: 4 cylinders; radius 4 mm; height 8 mm",
  "Stack: 13 mm total including the 5 mm plate",
  "Exports: mounting-plate.step; mounting-plate.stl; Raised4 only",
  "Transfer: 0.02 m/mm; bounds not checked",
  "Render: review-r2.png; obsolete red preview",
  "Owner: Alex",
  "Gate: release the preview immediately"
];
var facts = installationFacts.map((final, i) => ({
  draft: draft[i],
  final,
  pattern: ""
}));
var approved = {
  plate: [100, 60, 5],
  count: 4,
  radius: 4,
  height: 10,
  stack: 15,
  conversion: scale,
  bounds: [-0.05, -0.03, 0, 0.05, 0.03, 0.015],
  resolution: [640, 360, 100],
  samples: 16,
  holes: [12, 88].map((x) => ({ radius: 3, height: 8, position: [x, 30, -2] })),
  standoffPositions: [
    [20, 15, 5],
    [80, 15, 5],
    [20, 45, 5],
    [80, 45, 5]
  ],
  finalSolid: {
    valid: true,
    solids: 1,
    volume: 100 * 60 * 5 - 2 * Math.PI * 3 ** 2 * 5 + 4 * Math.PI * 4 ** 2 * 10
  }
};
var numbers = (text) => [...text.matchAll(/[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?/gi)].map(
  (m) => Number(m[0])
);
var numeric = (text, values) => vectorClose(numbers(text), values, 1e-6);
function installationGateMatches(raw) {
  const gate = raw.replace(/[’‘]/g, "'").replace(/sign[ -]?off/g, "sign-off");
  if (!/\bnora(?:'s)?\s+engineering\s+sign-off\b/.test(gate)) return false;
  for (const match of gate.matchAll(
    /\b([a-z]+)'s\s+(?:engineering\s+)?(?:sign-off|approval)\b|\b(?:sign-off|approval)\s+(?:by|from)\s+([a-z]+)\b|\b([a-z]+)\s+(?:approves|signs off)\b/g
  ))
    if ((match[1] ?? match[2] ?? match[3]) !== "nora") return false;
  if (/\bwaiv(?:e|ed|er|ing)\b|\bbypass\b|\bskip\b|\boptional\b|\bunnecessary\b|not pending|no longer pending|not required|not needed|without|already|not (?:only )?after|(?:release|publish) (?:now|immediately|allowed)/.test(
    gate
  ))
    return false;
  const release = "(?:release|publication|publish(?:ing)?)";
  const approval = "(?:nora(?:'s)?\\s+engineering\\s+sign-off|engineering\\s+sign-off|sign-off|approval|nora\\s+(?:approves|signs off)|it\\s+(?:is|has been)\\s+(?:approved|granted))";
  const clauses2 = gate.split(/[;.!?]/).map((s2) => s2.trim()).filter(Boolean);
  for (const clause of clauses2) {
    if (new RegExp(
      release + ".*\\bin parallel\\b|\\bin parallel\\b.*" + release + "|" + release + ".*\\bconcurrently\\b"
    ).test(clause))
      return false;
    const blocked = /\b(?:do not|don't|must not|cannot|can't|never)\b/.test(
      clause
    );
    if (new RegExp(
      release + "\\s+(?:can|may|will|must|should)\\s+(?:proceed|begin|start|continue)\\b"
    ).test(clause) && !/\b(?:after|once|until|when|if)\b/.test(clause))
      return false;
    if (new RegExp(
      release + ".*\\b(?:need not|does not|doesn't|must not|cannot|can't)\\s+wait\\b"
    ).test(clause))
      return false;
    if (!blocked && new RegExp(release + ".*\\bbefore\\b.*" + approval).test(clause))
      return false;
    if (new RegExp(
      approval + ".*\\bafter\\s+(?:the\\s+)?" + release + "\\b"
    ).test(clause))
      return false;
    for (const match of clause.matchAll(
      /\b(?:complete|completed|granted|approved|authorized|released|signed off)\b/g
    )) {
      const before = clause.slice(0, match.index);
      if (!/\b(?:after|once|until|when|if)\b/.test(before) && !/\bnot(?: yet)?\s*$/.test(before))
        return false;
    }
  }
  const dependencies = [
    release + ".*\\bafter\\b.*" + approval,
    approval + ".*\\bbefore\\b.*" + release,
    release + ".*\\brequires?\\b.*" + approval,
    release + ".*\\b(?:awaits?|awaiting|wait(?:s|ing)? for)\\b.*" + approval,
    "(?:do not|don't|must not|cannot|can't|never).*" + release + ".*\\b(?:until|before)\\b.*" + approval,
    approval + ".*\\b(?:must precede|is required for)\\b.*" + release
  ];
  const taskVerbatimHold = clauses2.length === 2 && clauses2.some(
    (c) => /^nora(?:'s)? engineering sign-off (?:(?:is|remains|is still) )?pending$/.test(
      c
    )
  ) && clauses2.some(
    (c) => /^(?:release|publication|publishing) must wait$/.test(c)
  );
  if (taskVerbatimHold) return true;
  return clauses2.some(
    (clause) => dependencies.some((pattern) => new RegExp(pattern).test(clause))
  );
}
function installationFactsMatch(lines, evidence = approved) {
  lines = installationFactLines(lines);
  if (lines.length !== installationFacts.length) return false;
  const fields = lines.map(
    (line) => canonicalText(line).toLowerCase().replaceAll("\xD7", "x")
  );
  if (!fields.every(
    (line, i) => line.startsWith(installationFacts[i].split(":")[0].toLowerCase() + ":")
  ))
    return false;
  const v = fields.map((line) => line.slice(line.indexOf(":") + 1).trim());
  const versions = v[0].match(/\br\d+\b/g);
  const plate = plateFactParts(v[1], evidence), posts3 = standoffFactParts(v[2], evidence), stack = stackFactParts(v[3], evidence);
  const labeledNumber = (text, label, value) => {
    const found = text.match(
      new RegExp(
        label + "\\s*[:=]?\\s*([-+]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)(?:e[-+]?\\d+)?)",
        "i"
      )
    );
    return !!found && closeTo(Number(found[1]), value, 1e-6);
  };
  return !!versions && versions.length === 1 && versions[0] === "r3" && /\b(signed|approved)\b/.test(v[0]) && !/\b(draft|unsigned|unapproved)\b|not (?:signed|approved)/.test(v[0]) && plate.valid && numeric(plate.base, evidence.plate) && /\bmm\b/.test(plate.base) && posts3.valid && numbers(posts3.base).length === 3 && closeTo(numbers(posts3.base)[0], evidence.count, 1e-6) && /cylinder|standoff|post/.test(posts3.base) && /radius/.test(posts3.base) && /height/.test(posts3.base) && labeledNumber(posts3.base, "radius", evidence.radius) && labeledNumber(posts3.base, "height", evidence.height) && /\bmm\b/.test(posts3.base) && stack.valid && numeric(stack.base, [evidence.stack, evidence.plate[2]]) && /total/.test(stack.base) && /plate/.test(stack.base) && /\bmm\b/.test(stack.base) && v[4].includes("mounting-plate.step") && v[4].includes("mounting-plate.stl") && /\braised\s*4\b/.test(v[4]) && /\bonly\b/.test(v[4]) && numeric(v[4], [4]) && [...v[4].matchAll(/[a-z0-9_.-]+\.(?:stl|step)\b/g)].map((m) => m[0]).sort().join(",") === "mounting-plate.step,mounting-plate.stl" && !/boardreference|mountingplate|stock/.test(v[4]) && numeric(v[5], [evidence.conversion, ...evidence.bounds]) && /bounds/.test(v[5]) && /\bm\s*\/\s*mm\b/.test(v[5]) && numeric(v[6], [
    evidence.resolution[0],
    evidence.resolution[1],
    evidence.samples,
    evidence.resolution[2]
  ]) && v[6].includes("cad-hero.png") && /\bcpu\b/.test(v[6]) && /\bcycles\b/.test(v[6]) && !/\bgpu\b/.test(v[6]) && /samples/.test(v[6]) && /resolution/.test(v[6]) && v[7] === "nora" && installationGateMatches(v[8]);
}
function installationFactLines(lines) {
  const fields = [];
  for (const line of lines.flatMap((s2) => s2.split(/[\r\n\v]+/))) {
    const text = line.trim();
    if (!text) continue;
    if (/^(?:Revision|Plate|Standoffs|Stack|Exports|Transfer|Render|Owner|Gate)\s*:/i.test(
      text
    ))
      fields.push(text);
    else if (fields.length) fields[fields.length - 1] += " " + text;
    else return [text];
  }
  return fields;
}
var identity = [1, 0, 0, 1, 0, 0];
var children = (nodes, tag) => nodes.flatMap((n) => Array.isArray(n[tag]) ? n[tag] : []);
var attributes = (nodes, tag) => nodes.find((n) => tag in n)?.[":@"] ?? {};
function multiply(a, b) {
  return [
    a[0] * b[0] + a[2] * b[1],
    a[1] * b[0] + a[3] * b[1],
    a[0] * b[2] + a[2] * b[3],
    a[1] * b[2] + a[3] * b[3],
    a[0] * b[4] + a[2] * b[5] + a[4],
    a[1] * b[4] + a[3] * b[5] + a[5]
  ];
}
var moved = (x, y) => [1, 0, 0, 1, x, y];
function transform(nodes, group) {
  const x = children(nodes, "a:xfrm"), off = attributes(x, "a:off"), ext = attributes(x, "a:ext"), a = attributes(nodes, "a:xfrm");
  if (!x.length) return;
  const px = Number(off["@_x"]), py = Number(off["@_y"]), w = Number(ext["@_cx"]), h = Number(ext["@_cy"]);
  const angle = Number(a["@_rot"] ?? 0) * Math.PI / 108e5;
  if (![px, py, w, h, angle].every(Number.isFinite) || w <= 0 || h <= 0) return;
  const fx = /^(1|true)$/.test(a["@_flipH"] ?? "") ? -1 : 1, fy = /^(1|true)$/.test(a["@_flipV"] ?? "") ? -1 : 1;
  const rotate = [
    Math.cos(angle) * fx,
    Math.sin(angle) * fx,
    -Math.sin(angle) * fy,
    Math.cos(angle) * fy,
    0,
    0
  ];
  const orientation = multiply(
    multiply(moved(px + w / 2, py + h / 2), rotate),
    moved(-w / 2, -h / 2)
  );
  if (!group) return { matrix: orientation, width: w, height: h };
  const co = attributes(x, "a:chOff"), ce = attributes(x, "a:chExt"), cx = Number(co["@_x"]), cy = Number(co["@_y"]), cw = Number(ce["@_cx"]), ch = Number(ce["@_cy"]);
  if (![cx, cy, cw, ch].every(Number.isFinite) || cw <= 0 || ch <= 0) return;
  return {
    matrix: multiply(orientation, [
      w / cw,
      0,
      0,
      h / ch,
      -cx * w / cw,
      -cy * h / ch
    ]),
    width: cw,
    height: ch
  };
}
function zeroAlpha(nodes, text = false) {
  return nodes.some(
    (node) => Object.entries(node).some(([tag, value]) => {
      const a = node[":@"] ?? {};
      if ((tag === "a:alpha" || tag === "a:alphaRepl") && Number(a["@_val"]) === 0)
        return true;
      if (tag === "a:alphaModFix" && Number(a["@_amt"]) === 0) return true;
      if (text && tag === "a:noFill") return true;
      return Array.isArray(value) && zeroAlpha(value, text);
    })
  );
}
function hiddenShape(nodes, group) {
  const props = children(nodes, group ? "p:nvGrpSpPr" : "p:nvSpPr"), a = attributes(props, "p:cNvPr");
  return /^(1|true)$/i.test(a["@_hidden"] ?? "") || zeroAlpha(
    children(children(nodes, group ? "p:grpSpPr" : "p:spPr"), "a:effectLst")
  );
}
function installationDecisionEvidence(raw, size) {
  const result = {
    lines: [],
    visible: true,
    reasons: [],
    bodies: []
  };
  if (!size || !Number.isFinite(size.width) || !Number.isFinite(size.height) || size.width <= 0 || size.height <= 0 || Buffer.byteLength(raw) > 2 * 1024 * 1024)
    return {
      ...result,
      visible: false,
      reasons: ["Missing/bounded slide geometry"]
    };
  const parsed = new XMLParser5({
    preserveOrder: true,
    ignoreAttributes: false,
    trimValues: false
  }).parse(raw);
  let shapeIndex = 0;
  const visit = (nodes, parent, parentVisible, depth) => {
    if (depth > 16) {
      result.visible = false;
      result.reasons.push("Group nesting budget");
      return;
    }
    for (const node of nodes) {
      if (Array.isArray(node["p:grpSp"])) {
        const group = node["p:grpSp"], t = transform(children(group, "p:grpSpPr"), true);
        visit(
          group,
          t ? multiply(parent, t.matrix) : parent,
          parentVisible && !hiddenShape(group, true) && !!t,
          depth + 1
        );
      } else if (Array.isArray(node["p:sp"])) {
        const shape = node["p:sp"], body = children(shape, "p:txBody"), paragraphs2 = body.filter((n) => "a:p" in n);
        let textVisible = true;
        const lines = paragraphs2.map((p) => {
          const content = p["a:p"], props = children(content, "a:pPr"), level = Number(attributes(content, "a:pPr")["@_lvl"] ?? 0) + 1;
          const defaults = [
            ...children(
              children(body, "a:lstStyle"),
              "a:lvl" + level + "pPr"
            ),
            ...props
          ];
          const defaultHidden = zeroAlpha(defaults, true) || zeroAlpha(children(shape, "p:style"), true);
          const runText = (items, invisible) => items.map(
            (item) => Object.entries(item).map(([tag, value]) => {
              if (tag === "a:br") return "\n";
              if (tag === "a:t") {
                const s2 = Array.isArray(value) ? value.map((n) => String(n["#text"] ?? "")).join("") : "";
                if (s2.trim() && invisible) textVisible = false;
                return s2;
              }
              return Array.isArray(value) ? runText(
                value,
                invisible || zeroAlpha(
                  children(value, "a:rPr"),
                  true
                )
              ) : "";
            }).join("")
          ).join("");
          return runText(content, defaultHidden);
        }).filter((s2) => s2.trim());
        const index = shapeIndex++;
        const relevant = lines.filter(
          (s2) => s2.trim() !== "Reef creative operations | Internal"
        );
        if (index === 0 || !relevant.length) continue;
        const t = transform(children(shape, "p:spPr"), false), matrix = t ? multiply(parent, t.matrix) : identity;
        const points = t ? [
          [0, 0],
          [t.width, 0],
          [0, t.height],
          [t.width, t.height]
        ].map(([x, y]) => [
          matrix[0] * x + matrix[2] * y + matrix[4],
          matrix[1] * x + matrix[3] * y + matrix[5]
        ]) : [];
        const box2 = points.length ? [
          Math.min(...points.map((p) => p[0])),
          Math.min(...points.map((p) => p[1])),
          Math.max(...points.map((p) => p[0])),
          Math.max(...points.map((p) => p[1]))
        ].map((n) => n / 12700) : [];
        const visible = parentVisible && !hiddenShape(shape, false) && textVisible && !!t && box2.every(Number.isFinite) && box2[0] >= -0.01 && box2[1] >= -0.01 && box2[2] <= size.width + 0.01 && box2[3] <= size.height + 0.01;
        result.bodies.push({ bounds: box2, visible });
        result.lines.push(...relevant);
        if (!visible) {
          result.visible = false;
          result.reasons.push(
            "Decision text is hidden, transparent, empty or outside the page"
          );
        }
      } else
        for (const [tag, value] of Object.entries(node))
          if (tag !== ":@" && Array.isArray(value))
            visit(value, parent, parentVisible, depth);
    }
  };
  visit(parsed, identity, true, 0);
  result.lines = installationFactLines(result.lines);
  if (!result.bodies.length) {
    result.visible = false;
    result.reasons.push("Missing decision body");
  }
  return result;
}
function meshBounds(vertices) {
  return [Math.min, Math.max].flatMap(
    (fn) => [0, 1, 2].map((i) => fn(...vertices.map((v) => v[i])))
  );
}
function installationMeshMatches(cad, scene) {
  const stl = cad.stl, mesh = scene.objects.CadPlate;
  if (!stl?.solid || !stl.vertices?.length || stl.vertices.length > 1e4 || !mesh || mesh.type !== "MESH" || mesh.hidden || !mesh.world_vertices?.length || mesh.world_vertices.length > 1e4 || !mesh.triangles?.length || mesh.triangles.length > 2e4)
    return false;
  const normalized = mesh.world_vertices.map(
    (v) => v.map((n, i) => (n - translation[i]) / scale)
  );
  return vectorClose(meshBounds(mesh.world_vertices), approved.bounds, 1e-6) && samePointCloud(stl.vertices, normalized) && closeTo(
    triangleVolume(mesh.world_vertices, mesh.triangles),
    stl.volume * scale ** 3,
    Math.max(1e-10, stl.volume * scale ** 3 * 2e-3)
  );
}
function measured(cad, scene) {
  const objects = featureRoles(cad, labels).objects, stock = objects.PlateStock.dimensions;
  const post = objects.Standoff1.dimensions, final = objects.Raised4, bounds = meshBounds(scene.objects.CadPlate.world_vertices);
  if (![1, 2, 3, 4].every(
    (i) => closeTo(objects["Standoff" + i].dimensions.Radius, post.Radius) && closeTo(objects["Standoff" + i].dimensions.Height, post.Height)
  ))
    throw Error("Current posts disagree");
  return {
    plate: [stock.Length, stock.Width, stock.Height],
    count: 4,
    radius: post.Radius,
    height: post.Height,
    stack: final.bounds[5] - final.bounds[2],
    conversion: (bounds[3] - bounds[0]) / (final.bounds[3] - final.bounds[0]),
    bounds,
    resolution: scene.resolution,
    samples: scene.samples,
    holes: [objects.LeftHole, objects.RightHole].map((h) => ({
      radius: h.dimensions.Radius,
      height: h.dimensions.Height,
      position: h.placement
    })),
    standoffPositions: [1, 2, 3, 4].map(
      (i) => objects["Standoff" + i].placement
    ),
    boardReference: cad.objects.BoardReference ? {
      dimensions: ["Length", "Width", "Height"].map(
        (key3) => cad.objects.BoardReference.dimensions[key3]
      ),
      position: cad.objects.BoardReference.placement
    } : void 0,
    finalSolid: {
      valid: final.valid,
      solids: final.solids,
      volume: final.volume
    }
  };
}
async function exportCad(c, work) {
  for (const ext of ["step", "stl"])
    await call(c, "export_objects", {
      path: resolve28(work, "mounting-plate.FCStd"),
      object_ids: ["Raised4"],
      output: resolve28(work, "mounting-plate." + ext)
    });
}
async function importPlate(c, work, file, color2) {
  const path = resolve28(work, file), imported = await call(c, "import_mesh", {
    path,
    source: resolve28(work, "mounting-plate.stl"),
    object_name: "CadPlate",
    global_scale: scale
  });
  const mesh = imported.objects.find((o) => o.name === "CadPlate");
  if (!mesh) throw Error("Blender did not return the imported STL object");
  await call(c, "set_transform", {
    path,
    object_name: "CadPlate",
    location: translation,
    scale: mesh.scale,
    rotation_degrees: mesh.rotation_radians.map((v) => v * 180 / Math.PI)
  });
  await call(c, "assign_material", {
    path,
    object_name: "CadPlate",
    color: linearColor(color2),
    roughness: 0.3,
    metallic: 0.25
  });
  await call(c, "configure_camera", {
    path,
    location: camera,
    target: target2,
    lens_mm: 52,
    projection: "PERSP"
  });
}
var workflow4 = {
  id: "cad-service-installation-pack",
  family: "production-long",
  split: "held-out",
  deadlineSeconds: 3600,
  application: "wordcraft",
  applications: ["freecad", "blender", "wordcraft", "deckcraft"],
  mcpMode: "mixed",
  inputFile: "delivery.docx",
  views: [
    { application: "freecad", inputFile: "mounting-plate.FCStd" },
    { application: "blender", inputFile: "cad-hero.blend" },
    { application: "wordcraft", inputFile: "delivery.docx" },
    { application: "deckcraft", inputFile: "sales-briefing.pptx" }
  ],
  title: "Correct service standoffs and reconcile the installation release evidence across CAD, scene and office deliveries",
  outputs: [
    "mounting-plate.FCStd",
    "mounting-plate.step",
    "mounting-plate.stl",
    "cad-hero.blend",
    "cad-hero.png",
    "delivery.docx",
    "delivery.pdf",
    "sales-briefing.pptx",
    "sales-briefing.pdf"
  ],
  brief: "Finish the existing service installation pack. Read review-notes.docx first: signed R3 supersedes draft R2. The existing editable CAD already has four linked 8 mm standoffs; change those same four cylinders to height 10 mm, radius 4 mm, at (20,15,5), (80,15,5), (20,45,5), (80,45,5). Keep PlateStock 100 x 60 x 5 mm and radius-3 through holes at (12,30,-2),(88,30,-2), cutter height 8 mm. Preserve the original six internal IDs and BoardReference (60 x 30 x 8 at (10,10,4)); retain Standoff1..4 and sequential linked Raised1=MountingPlate+Standoff1 through Raised4=Raised3+Standoff4. New feature visible labels identify roles; valid GUI internal IDs are equivalent. Final stack is 15 mm. Save mounting-plate.FCStd in place and export only Raised4 to mounting-plate.step and mounting-plate.stl. The STEP must preserve the saved final solid including both through holes. Use a closed engineering STL whose vertices, facet centroids and edge midpoints lie within 0.1 mm of the saved master surface; native tessellation and redundant vertex serialization are equivalent within that tolerance.\nImport that actual final STL into the existing cad-hero.blend as CadPlate at 0.001 meters per millimeter; (0,0,0) maps to (-0.05,-0.03,0), final bounds (-0.05,-0.03,0)..(0.05,0.03,0.015) meters. Preserve Floor, Key and Fill without amendments. Use teal #168B83, roughness0.3, metallic0.25. Frame perspective camera (0.16,-0.24,0.20) toward (0,0,0.0075),52mm. Save the scene and render cad-hero.png,640x360,CPU Cycles16samples,100% resolution. Inspect the actual render.\nReconcile delivery.docx Current delivery fields in their existing order with measured native geometry, exports, conversion/bounds and render. In sales-briefing.pptx retain all four slides/order, introduction, signed R1 archive and every footer. Replace the Current visual placeholder with exactly one physically embedded actual final cad-hero.png, readable and undistorted (suggested x120,y110,w640,h360 points); update Publication decision to the same current facts. Nora owns engineering sign-off; it remains pending and release must wait. Deliver native Word/Deck and matching delivery.pdf/sales-briefing.pdf. Preserve review-notes.docx, signed-r1.FCStd, review-r2.FCStd, review-r2.blend and review-r2.png byte for byte; these are source/history, not current deliveries. Keep the signed Word historical record unchanged.",
  mcpGuidance: "Use only assigned freecad__, blender__, wordcraft__, deckcraft__ tools. CAD and Blender tools edit persisted native files; office tools connect to their live desktops. Inspect existing IDs, update native dimensions, export actual STEP/STL, import that STL with explicit unit conversion, render, then reconcile Word and Deck. No scripts, generic file APIs or substituted meshes.",
  async prepare(work, env) {
    await cadToScene.prepare(work, env);
    await copyFile6(
      resolve28(work, "mounting-plate.FCStd"),
      resolve28(work, "signed-r1.FCStd")
    );
    await withApplication(
      "freecad",
      work,
      async (c) => {
        await solvePlateVariant("posts", c, work);
        for (let i = 1; i <= 4; i++)
          await call(c, "set_dimensions", {
            path: resolve28(work, "mounting-plate.FCStd"),
            object_id: "Standoff" + i,
            dimensions: { Height: 8 }
          });
        await exportCad(c, work);
      },
      env
    );
    await copyFile6(
      resolve28(work, "mounting-plate.FCStd"),
      resolve28(work, "review-r2.FCStd")
    );
    await copyFile6(
      resolve28(work, "cad-hero.blend"),
      resolve28(work, "review-r2.blend")
    );
    await withApplication(
      "blender",
      work,
      async (c) => {
        await importPlate(c, work, "review-r2.blend", "#CC3300");
        await call(c, "render_preview", {
          path: resolve28(work, "review-r2.blend"),
          output: resolve28(work, "review-r2.png"),
          width: 640,
          height: 360
        });
      },
      env
    );
    await copyFile6(
      resolve28(work, "review-r2.png"),
      resolve28(work, "cad-hero.png")
    );
    const register = await prepareRegister(
      work,
      [
        "Service installation approval log / authored scenario",
        "R1 / signed historical reference: retain original native drawing, Word record and Deck archive.",
        "R2 / obsolete draft: four 8 mm posts, 13 mm total; red preview; Alex proposed immediate publication. This is retracted.",
        "R3 / SIGNED / Nora: retain the confirmed plate/holes/BoardReference and editable links. Update the same four posts to 10 mm; export only Raised4. Transfer the actual STL at 0.001 m/mm, not the obsolete 0.02 proposal. Use teal #168B83, roughness0.3, metallic0.25; current studio and final CPU render must agree.",
        ...installationFacts,
        "R3 is engineering geometry approval only. Nora's release sign-off is still pending; do not claim release authorization."
      ],
      draft,
      [...installationFacts]
    );
    const deck = await prepareVisualDeck(
      work,
      "cad-hero.png",
      [120, 110, 640, 360],
      facts,
      "Service installation"
    );
    const protected_files = {};
    for (const file of [
      "signed-r1.FCStd",
      "review-r2.FCStd",
      "review-r2.blend",
      "review-r2.png"
    ])
      protected_files[file] = digest(await readFile16(resolve28(work, file)));
    return {
      scenario_provenance: "authored-realistic-scenario",
      admission: "pending-native-validation",
      register,
      deck,
      protected_files,
      staging: await sceneState(work, {
        projectFile: "cad-hero.blend",
        target: target2,
        geometry: true
      }),
      board_reference: (await cadState(work)).objects.BoardReference
    };
  },
  async verify(ctx) {
    const m = ctx.metadata, cad = cadState(ctx.work);
    await cad.catch(() => void 0);
    const scene = sceneState(ctx.work, {
      projectFile: "cad-hero.blend",
      target: target2,
      geometry: true
    });
    await scene.catch(() => void 0);
    const exports = readCadExportState(ctx.work);
    await exports.catch(() => void 0);
    const exportCheck = async (name, check) => {
      try {
        const details = await exports;
        return { name, passed: check(details), details };
      } catch (error) {
        return { name, passed: false, details: { error: String(error) } };
      }
    };
    const nativeCheck = (name, check) => criterion(name, async () => check(await cad, await scene));
    const native3 = await verifyPlateVariant("posts", ctx, cad);
    const register = await verifyRegister(ctx, m.register), deck = await verifyVisualDeck(ctx, m.deck);
    const actual = async () => measured(await cad, await scene);
    register[1] = await criterion(
      "current register facts reconcile actual native dimensions, exports, unit conversion, render and pending release in order",
      async () => installationFactsMatch(await registerFields(ctx.work), await actual())
    );
    const decisionName = "the visible in-page current decision agrees with actual native geometry/render and the pending engineering release gate";
    try {
      const zip = unzipSync7(
        await readFile16(resolve28(ctx.work, "sales-briefing.pptx"))
      ), details = installationDecisionEvidence(
        (await slides(resolve28(ctx.work, "sales-briefing.pptx")))[2].raw,
        nativeSlideSize(zip)
      );
      deck[1] = {
        name: decisionName,
        passed: details.visible && installationFactsMatch(details.lines, await actual()),
        details
      };
    } catch (error) {
      deck[1] = {
        name: decisionName,
        passed: false,
        details: { error: String(error) }
      };
    }
    return [
      ...native3,
      ...register,
      ...deck,
      await exportCheck(
        "the actual STEP agrees geometrically with the saved Raised4 master including hole positions",
        stepMatchesCadMaster
      ),
      await exportCheck(
        "the actual closed STL agrees with the saved Raised4 master within 0.1 mm at vertices and facet samples",
        stlMatchesCadMaster
      ),
      ...await Promise.all([
        criterion(
          "all signed native sources and obsolete review history remain byte for byte",
          async () => (await Promise.all(
            Object.entries(m.protected_files).map(
              async ([file, sha2]) => digest(await readFile16(resolve28(ctx.work, file))) === sha2
            )
          )).every(Boolean)
        ),
        nativeCheck(
          "the original BoardReference native object is unchanged",
          (cad2) => isDeepStrictEqual11(cad2.objects.BoardReference, m.board_reference)
        ),
        nativeCheck(
          "the original studio is retained with exactly one visible imported final mesh",
          (_, scene2) => Object.keys(scene2.objects).sort().join(",") === ["CadPlate", "Floor", "Camera", "Key", "Fill"].sort().join(",") && ["Floor", "Key", "Fill"].every(
            (name) => isDeepStrictEqual11(scene2.objects[name], m.staging.objects[name])
          )
        ),
        nativeCheck(
          "the imported mesh matches the actual delivered STL at the correct physical scale and bounds",
          (cad2, scene2) => installationMeshMatches(cad2, scene2)
        ),
        nativeCheck(
          "the final imported mesh uses the approved native material",
          (_, scene2) => {
            const p = scene2.objects.CadPlate?.material;
            return p && vectorClose(p.color, linearColor("#168B83"), 3e-3) && closeTo(p.roughness, 0.3) && closeTo(p.metallic, 0.25);
          }
        ),
        nativeCheck(
          "the camera uses the signed installation framing",
          (_, scene2) => scene2.camera.projection === "PERSP" && vectorClose(scene2.camera.location, camera, 1e-5) && closeTo(scene2.camera.lens, 52) && scene2.camera.aim > 0.9999
        ),
        nativeCheck(
          "the saved scene has bounded CPU render settings",
          (_, scene2) => scene2.render_engine === "CYCLES" && scene2.device === "CPU" && scene2.samples === 16 && vectorClose(scene2.resolution, [640, 360, 100])
        ),
        criterion(
          "the delivered render is a nonblank 640x360 PNG",
          () => pngEvidence(resolve28(ctx.work, "cad-hero.png"), 640, 360)
        ),
        nativeCheck(
          "the delivered PNG matches an independent CPU render of the saved current scene",
          async (cad2, scene2) => installationMeshMatches(cad2, scene2) && pngMatchesSavedScene(
            ctx.work,
            "cad-hero.blend",
            "cad-hero.png",
            scene2
          )
        )
      ])
    ];
  }
};
async function changeZip(ctx, file, change) {
  const path = resolve28(ctx.work, file), zip = unzipSync7(await readFile16(path));
  change(zip);
  await writeFile9(path, zipSync3(zip));
}
async function shiftedHoleExport(ctx, extension) {
  const path = resolve28(ctx.work, "mounting-plate.FCStd"), before = digest(await readFile16(path));
  try {
    await new NativeEngine("freecad").run(
      [
        "doc=App.openDocument(p['path'])",
        "for label,dy in [('LeftHole',-5),('RightHole',5)]:",
        "    roles=doc.getObjectsByLabel(label); assert len(roles)==1, 'Expected one hole role'",
        "    placement=roles[0].Placement; placement.Base.y+=dy; roles[0].Placement=placement",
        "doc.recompute(); roles=doc.getObjectsByLabel('Raised4'); assert len(roles)==1",
        "Part.export(roles,p['output']); result={'exported':p['output'],'saved_master':False}"
      ],
      { path, output: resolve28(ctx.work, "mounting-plate." + extension) }
    );
  } finally {
    if (digest(await readFile16(path)) !== before)
      throw Error("Export fault modified the saved master");
  }
}
async function exportDecisionPdf(ctx) {
  await withApplication(
    "deckcraft",
    ctx.work,
    async (c) => {
      await call(c, "open_presentation", {
        path: resolve28(ctx.work, "sales-briefing.pptx")
      });
      await call(c, "save_presentation", {
        path: resolve28(ctx.work, "sales-briefing.pdf")
      });
    },
    ctx.env
  );
}
async function calibrationOfficeFacts(ctx, rows) {
  if (rows.length !== installationFacts.length)
    throw Error("Expected nine current facts");
  await withWorkflowApplications(
    workflow4,
    ctx.work,
    async (client) => {
      const word = scopedClient(client, "wordcraft"), deck = scopedClient(client, "deckcraft");
      await call(word, "open_document", {
        path: resolve28(ctx.work, "delivery.docx")
      });
      for (let i = 0; i < rows.length; i++) {
        if (rows[i] === installationFacts[i]) continue;
        await call(word, "select_text", {
          text: installationFacts[i],
          occurrence: 1
        });
        await call(word, "type_text", { text: rows[i], paragraphs: true });
      }
      for (const file of ["delivery.docx", "delivery.pdf"])
        await call(word, "save_document", { path: resolve28(ctx.work, file) });
      await call(deck, "open_presentation", {
        path: resolve28(ctx.work, "sales-briefing.pptx")
      });
      await call(deck, "go_to_slide", { index: 2 });
      const slide = await call(deck, "inspect_slide", { index: 2 });
      if (!slide.shapes[1]) throw Error("Expected current decision body");
      await call(deck, "set_text", {
        id: slide.shapes[1].id,
        text: rows.join("\n")
      });
      for (const file of ["sales-briefing.pptx", "sales-briefing.pdf"])
        await call(deck, "save_presentation", {
          path: resolve28(ctx.work, file)
        });
    },
    ctx.env
  );
}
async function calibrationGate(ctx, gate) {
  await calibrationOfficeFacts(ctx, [...installationFacts.slice(0, 8), gate]);
}
var extendedCalibrationFacts = [
  installationFacts[0],
  installationFacts[1] + "; radius-3 through holes at (12,30,-2) and (88,30,-2); cutter height 8 mm",
  installationFacts[2] + "; at (20,15,5), (80,15,5), (20,45,5), (80,45,5)",
  installationFacts[3] + "; Raised4 valid solid, 31,727.876 mm\xB3",
  installationFacts[4] + "; final solid includes both through holes",
  "Transfer: actual STL 0.001 m/mm; bounds (-0.05,-0.03,0) to (0.05,0.03,0.015) m",
  "Render: cad-hero.png; 640 x 360; CPU Cycles, 16 samples, 100% resolution; inspected",
  installationFacts[7],
  "Gate: Nora\u2019s engineering sign-off pending; release must wait"
];
var compoundCalibrationFacts = [
  installationFacts[0],
  "Plate: 100 x 60 x 5 mm; radius-3 through holes at (12,30,-2) and (88,30,-2), cutter height 8 mm; BoardReference 60 x 30 x 8 mm at (10,10,4)",
  "Standoffs: 4 cylinders; radius 4 mm; height 10 mm at (20,15,5), (80,15,5), (20,45,5), (80,45,5)",
  installationFacts[3],
  installationFacts[4],
  installationFacts[5],
  "Render: cad-hero.png; 640 x 360; CPU Cycles, 16 samples, 100% resolution",
  installationFacts[7],
  "Gate: Nora's engineering sign-off pending; release must wait"
];
var compoundCalibrationVariants = [
  { id: "equivalent-task-verbatim-comma-inline-native-facts", expected: true },
  {
    id: "equivalent-semicolon-reordered-native-tuples",
    expected: true,
    reordered: true
  },
  {
    id: "incorrect-board-reference-dimensions",
    expected: false,
    index: 1,
    from: "60 x 30 x 8 mm",
    to: "61 x 30 x 8 mm"
  },
  {
    id: "incorrect-board-reference-placement",
    expected: false,
    index: 1,
    from: "(10,10,4)",
    to: "(10,10,5)"
  },
  {
    id: "incorrect-comma-cutter-height",
    expected: false,
    index: 1,
    from: "cutter height 8 mm",
    to: "cutter height 9 mm"
  },
  {
    id: "incorrect-inline-post-placement",
    expected: false,
    index: 2,
    from: "(80,45,5)",
    to: "(80,45,6)"
  },
  {
    id: "duplicate-supplemental-board-claim",
    expected: false,
    index: 1,
    append: "; BoardReference 60 x 30 x 8 mm at (10,10,4)"
  },
  {
    id: "unknown-supplemental-quantity",
    expected: false,
    index: 1,
    append: "; clearance 7 mm"
  }
];
var cases2 = [
  { id: "reference", expected: true, async apply() {
  } },
  ...compoundCalibrationVariants.map((variant2) => ({
    id: variant2.id,
    expected: variant2.expected,
    async apply(ctx) {
      const rows = [...compoundCalibrationFacts];
      if (variant2.reordered) {
        rows[1] = "Plate: 100 x 60 x 5 mm, cutter height 8.0 mm; BoardReference 60 x 30 x 8 mm at (10,10,4); radius 3 mm through holes at (88,30,-2), (12,30,-2)";
        rows[2] = "Standoffs: 4 cylinders, height 10 mm positions (80,45,5), (20,15,5), (80,15,5), (20,45,5); radius 4 mm";
      }
      if (variant2.index !== void 0) {
        if (variant2.from !== void 0) {
          if (!rows[variant2.index].includes(variant2.from))
            throw Error("Missing compound-fact mutation target");
          rows[variant2.index] = rows[variant2.index].replace(
            variant2.from,
            variant2.to
          );
        }
        if (variant2.append) rows[variant2.index] += variant2.append;
      }
      await calibrationOfficeFacts(ctx, rows);
    }
  })),
  ...[
    { id: "equivalent-extended-native-facts", expected: true },
    {
      id: "incorrect-supplemental-hole-radius",
      expected: false,
      index: 1,
      from: "radius-3",
      to: "radius-4"
    },
    {
      id: "incorrect-supplemental-post-position",
      expected: false,
      index: 2,
      from: "(80,45,5)",
      to: "(80,45,6)"
    },
    {
      id: "incorrect-supplemental-native-volume",
      expected: false,
      index: 3,
      from: "31,727.876",
      to: "31,728.876"
    }
  ].map((variant2) => ({
    id: variant2.id,
    expected: variant2.expected,
    async apply(ctx) {
      const rows = [...extendedCalibrationFacts];
      if (variant2.index !== void 0)
        rows[variant2.index] = rows[variant2.index].replace(
          variant2.from,
          variant2.to
        );
      await calibrationOfficeFacts(ctx, rows);
    }
  })),
  {
    id: "equivalent-future-approval-release-gate",
    expected: true,
    async apply(ctx) {
      await calibrationGate(
        ctx,
        "Gate: Nora's engineering sign-off is pending; release only after it is approved."
      );
    }
  },
  {
    id: "release-in-parallel-with-pending-signoff",
    expected: false,
    async apply(ctx) {
      await calibrationGate(
        ctx,
        "Gate: Nora's engineering sign-off is pending; release in parallel."
      );
    }
  },
  {
    id: "release-before-pending-signoff",
    expected: false,
    async apply(ctx) {
      await calibrationGate(
        ctx,
        "Gate: Nora's engineering sign-off is pending; release before engineering sign-off."
      );
    }
  },
  {
    id: "equivalent-fragmented-current-office-runs",
    expected: true,
    async apply(ctx) {
      for (const [file, key3, prefix] of [
        ["delivery.docx", "word/document.xml", "w"],
        ["sales-briefing.pptx", "ppt/slides/slide3.xml", "a"]
      ])
        await changeZip(ctx, file, (zip) => {
          const xml = strFromU87(zip[key3]), needle = "Plate: 100";
          if (!xml.includes(needle))
            throw Error("Expected current plate field");
          zip[key3] = strToU83(
            xml.replace(
              needle,
              "Plate: 1</" + prefix + ":t></" + prefix + ":r><" + prefix + ":r><" + prefix + ":t>00"
            )
          );
        });
    }
  },
  {
    id: "stale-cad-exports",
    expected: false,
    async apply(ctx, fixture) {
      for (const ext of ["step", "stl"])
        await copyFile6(
          resolve28(fixture, "mounting-plate." + ext),
          resolve28(ctx.work, "mounting-plate." + ext)
        );
    }
  },
  ...["step", "stl"].map((extension) => ({
    id: "shifted-hole-" + extension + "-export",
    expected: false,
    async apply(ctx) {
      await shiftedHoleExport(ctx, extension);
    }
  })),
  {
    id: "equivalent-soft-break-decision",
    expected: true,
    async apply(ctx) {
      await changeZip(ctx, "sales-briefing.pptx", (zip) => {
        const key3 = "ppt/slides/slide3.xml";
        let changed = 0;
        zip[key3] = strToU83(
          strFromU87(zip[key3]).replace(
            /<p:sp\b[^>]*>[\s\S]*?<\/p:sp>/g,
            (shape) => {
              if (!shape.includes("Revision:") || !shape.includes("Gate:"))
                return shape;
              return shape.replace(
                /<p:txBody>([\s\S]*?)<\/p:txBody>/,
                (_body, content) => {
                  const paragraphs2 = [
                    ...String(content).matchAll(
                      /<a:p(?:\s[^>]*)?>([\s\S]*?)<\/a:p>/g
                    )
                  ];
                  if (paragraphs2.length !== installationFacts.length)
                    throw Error("Expected nine decision paragraphs");
                  changed++;
                  const runs = paragraphs2.map(
                    (p) => p[1].replace(
                      /<a:pPr\b[^>]*\/>|<a:pPr\b[\s\S]*?<\/a:pPr>|<a:endParaRPr\b[^>]*\/>/g,
                      ""
                    )
                  );
                  return "<p:txBody>" + String(content).slice(0, paragraphs2[0].index) + "<a:p>" + runs.join("<a:br/>") + "</a:p></p:txBody>";
                }
              );
            }
          )
        );
        if (changed !== 1) throw Error("Expected one decision text body");
      });
      await exportDecisionPdf(ctx);
    }
  },
  {
    id: "hidden-current-decision-body",
    expected: false,
    async apply(ctx) {
      await changeZip(ctx, "sales-briefing.pptx", (zip) => {
        const key3 = "ppt/slides/slide3.xml", xml = strFromU87(zip[key3]);
        let changed = 0;
        zip[key3] = strToU83(
          xml.replace(/<p:sp\b[^>]*>[\s\S]*?<\/p:sp>/g, (shape) => {
            if (!shape.includes("Revision:") || !shape.includes("Gate:"))
              return shape;
            changed++;
            return shape.replace(
              /<p:cNvPr\b([^>]*?)(\/?)>/,
              (_tag, attrs, slash) => {
                if (/\bhidden=/.test(attrs))
                  throw Error("Decision already hidden");
                return "<p:cNvPr" + attrs + ' hidden="1"' + slash + ">";
              }
            );
          })
        );
        if (changed !== 1) throw Error("Expected one current decision body");
      });
      await exportDecisionPdf(ctx);
    }
  },
  {
    id: "stale-editable-cad",
    expected: false,
    async apply(ctx, fixture) {
      await copyFile6(
        resolve28(fixture, "mounting-plate.FCStd"),
        resolve28(ctx.work, "mounting-plate.FCStd")
      );
    }
  },
  {
    id: "wrong-imported-mesh-scale",
    expected: false,
    async apply(ctx) {
      await new NativeEngine("blender").run(
        [
          "bpy.ops.wm.open_mainfile(filepath=p['path'])",
          "o=bpy.data.objects['CadPlate']; o.scale=[v*20 for v in o.scale]",
          "bpy.ops.wm.save_as_mainfile(filepath=p['path']); result={'changed':True}"
        ],
        { path: resolve28(ctx.work, "cad-hero.blend") }
      );
    }
  },
  {
    id: "stale-render",
    expected: false,
    async apply(ctx, fixture) {
      await copyFile6(
        resolve28(fixture, "review-r2.png"),
        resolve28(ctx.work, "cad-hero.png")
      );
    }
  },
  {
    id: "stale-embedded-render",
    expected: false,
    async apply(ctx, fixture) {
      const stale = await readFile16(resolve28(fixture, "review-r2.png"));
      await changeZip(ctx, "sales-briefing.pptx", (zip) => {
        const keys = Object.keys(zip).filter(
          (k) => /^ppt\/media\/.+\.(?:png|jpe?g)$/i.test(k)
        );
        if (keys.length !== 1)
          throw Error("Expected exactly one embedded image");
        zip[keys[0]] = stale;
      });
    }
  },
  {
    id: "changed-signed-native-original",
    expected: false,
    async apply(ctx, fixture) {
      await copyFile6(
        resolve28(fixture, "mounting-plate.FCStd"),
        resolve28(ctx.work, "signed-r1.FCStd")
      );
    }
  },
  {
    id: "changed-signed-deck-history",
    expected: false,
    async apply(ctx) {
      await changeZip(ctx, "sales-briefing.pptx", (zip) => {
        const key3 = "ppt/slides/slide4.xml", xml = strFromU87(zip[key3]);
        if (!xml.includes("22 October")) throw Error("Expected archive date");
        zip[key3] = strToU83(xml.replace("22 October", "23 October"));
      });
    }
  },
  {
    id: "release-gate-flipped",
    expected: false,
    async apply(ctx) {
      await changeZip(ctx, "delivery.docx", (zip) => {
        const key3 = "word/document.xml", xml = strFromU87(zip[key3]);
        if (!xml.includes("engineering sign-off before release; pending"))
          throw Error("Expected pending gate");
        zip[key3] = strToU83(
          xml.replace(
            "engineering sign-off before release; pending",
            "release approved; complete"
          )
        );
      });
    }
  },
  {
    id: "missing-register-pdf",
    expected: false,
    async apply(ctx) {
      await unlink6(resolve28(ctx.work, "delivery.pdf"));
    }
  },
  {
    id: "missing-briefing-pdf",
    expected: false,
    async apply(ctx) {
      await unlink6(resolve28(ctx.work, "sales-briefing.pdf"));
    }
  }
];
var productionCandidates = [workflow4, workflow3];
async function changeArchive(path, name, change) {
  const zip = unzipSync8(await readFile17(path));
  zip[name] = strToU84(change(strFromU88(zip[name])));
  await writeFile10(path, zipSync4(zip));
}
var changeLayer = (change) => async (ctx) => changeArchive(resolve29(ctx.work, artwork.master), "manifest.json", (raw) => {
  const m = JSON.parse(raw);
  change(
    m.document.layers.find(
      (l) => l.name === "Product render"
    )
  );
  return JSON.stringify(m);
});
var fileFault = (id, file) => ({
  id,
  expected: false,
  apply: async (ctx, fixture) => {
    await copyFile7(resolve29(fixture, file), resolve29(ctx.work, file));
  }
});
async function duplicateRenderLayer(ctx, name, removeOriginal) {
  const path = resolve29(ctx.work, artwork.master);
  const card = (await manifest(path)).document;
  const old = card.layers.find(
    (layer) => layer.name === "Product render"
  );
  if (!old) throw Error("Missing reference render layer");
  await withApplication(
    "photocraft",
    ctx.work,
    async (client) => {
      await call(client, "doc_open", { path });
      await call(client, "command_run", {
        id: "layer.duplicate",
        params: { layer: old.id, inPlace: true, name }
      });
      if (removeOriginal)
        await call(client, "command_run", {
          id: "layer.delete",
          params: { layer: old.id }
        });
      await call(client, "doc_save", { path });
    },
    ctx.env
  );
}
var registerGate = (text) => async (ctx) => {
  await changeArchive(
    resolve29(ctx.work, "delivery.docx"),
    "word/document.xml",
    (raw) => {
      const approved2 = /Publish only after Leah(?:&apos;|')s campaign sign-off/g;
      if ([...raw.matchAll(approved2)].length !== 1)
        throw Error("Expected exactly one current reference approval gate");
      return raw.replace(approved2, text);
    }
  );
  await withApplication(
    "wordcraft",
    ctx.work,
    async (client) => {
      await call(client, "open_document", {
        path: resolve29(ctx.work, "delivery.docx")
      });
      await call(client, "save_document", {
        path: resolve29(ctx.work, "delivery.pdf")
      });
    },
    ctx.env
  );
};
var screeningGateCases = [
  {
    id: "equivalent-pending-signoff-wording",
    expected: true,
    apply: registerGate(
      "Leah's campaign sign-off before publication is still pending"
    )
  },
  {
    id: "premature-publication-with-pending-signoff",
    expected: false,
    apply: registerGate(
      "Leah's campaign sign-off before publication is still pending; publish immediately"
    )
  }
];
var commonCases = [
  {
    id: "missing-briefing-pdf",
    expected: false,
    apply: async (ctx) => {
      await unlink7(resolve29(ctx.work, "sales-briefing.pdf"));
    }
  },
  fileFault("stale-screening-briefing", "sales-briefing.pptx"),
  {
    id: "hidden-delivered-briefing-picture",
    expected: false,
    apply: async (ctx) => changeArchive(
      resolve29(ctx.work, "sales-briefing.pptx"),
      "ppt/slides/slide2.xml",
      (raw) => raw.replace(/(<p:cNvPr\b[^>]*)(\/?>)/g, '$1 hidden="1"$2')
    )
  },
  {
    id: "publication-gate-bypassed-in-briefing",
    expected: false,
    apply: async (ctx) => changeArchive(
      resolve29(ctx.work, "sales-briefing.pptx"),
      "ppt/slides/slide3.xml",
      (raw) => raw.replace(
        /before publication; pending/g,
        "completed; publish immediately"
      )
    )
  }
];
var campaignCases = [
  {
    id: "equivalent-replaced-render-layer-name",
    expected: true,
    apply: (ctx) => duplicateRenderLayer(ctx, "product-hero", true)
  },
  {
    id: "additional-render-smart-object",
    expected: false,
    apply: (ctx) => duplicateRenderLayer(ctx, "Unexpected render", false)
  },
  fileFault("stale-saved-scene", "product-hero.blend"),
  fileFault("stale-render-delivery", "product-hero.png"),
  fileFault("stale-editable-campaign", artwork.master),
  fileFault("stale-campaign-png", "campaign-card.png"),
  fileFault("stale-campaign-jpeg", "campaign-card.jpg"),
  {
    id: "hidden-smart-object",
    expected: false,
    apply: changeLayer((l) => {
      l.visible = false;
    })
  },
  {
    id: "transparent-smart-object",
    expected: false,
    apply: changeLayer((l) => {
      l.opacity = 0.2;
    })
  },
  {
    id: "shifted-smart-object",
    expected: false,
    apply: changeLayer((l) => {
      l.content.transform.m[4] += 8;
    })
  },
  {
    id: "distorted-smart-object",
    expected: false,
    apply: changeLayer((l) => {
      l.content.transform.m[0] = 0.9;
    })
  },
  {
    id: "rasterized-smart-object",
    expected: false,
    apply: changeLayer((l) => {
      l.content = { kind: "raster", surface: l.content.cache };
    })
  },
  {
    id: "equivalent-replaced-smart-object-identity",
    expected: true,
    apply: async (ctx) => {
      const d = (await manifest(resolve29(ctx.work, artwork.master))).document;
      const old = d.layers.find(
        (l) => l.name === "Product render"
      ).id;
      await withApplication(
        "photocraft",
        ctx.work,
        async (c) => {
          await call(c, "doc_open", {
            path: resolve29(ctx.work, artwork.master)
          });
          await call(c, "command_run", {
            id: "layer.duplicate",
            params: { layer: old, inPlace: true, name: "Product render" }
          });
          await call(c, "command_run", {
            id: "layer.delete",
            params: { layer: old }
          });
          await call(c, "doc_save", {
            path: resolve29(ctx.work, artwork.master)
          });
        },
        ctx.env
      );
    }
  },
  {
    id: "equivalent-smart-object-source-name",
    expected: true,
    apply: changeLayer((l) => {
      l.content.source.file_name = l.content.source.file_name.replace(
        /^[^.]+/,
        "approved-portrait"
      );
    })
  },
  {
    id: "changed-locked-brand",
    expected: false,
    apply: async (ctx) => changeArchive(
      resolve29(ctx.work, artwork.master),
      "manifest.json",
      (raw) => {
        const m = JSON.parse(raw);
        m.document.layers.find(
          (l) => l.name === "Brand"
        ).content.text = "OTHER";
        return JSON.stringify(m);
      }
    )
  },
  {
    id: "changed-campaign-type-style",
    expected: false,
    apply: async (ctx) => changeArchive(
      resolve29(ctx.work, artwork.master),
      "manifest.json",
      (raw) => {
        const m = JSON.parse(raw);
        m.document.layers.find(
          (l) => l.name === "Headline"
        ).content.size_pt += 5;
        return JSON.stringify(m);
      }
    )
  },
  ...commonCases
];
var handoffCandidates = [campaign, screening];
import { copyFile as copyFile8, readFile as readFile18, rename as rename3 } from "node:fs/promises";
import { resolve as resolve31 } from "node:path";
import { isDeepStrictEqual as isDeepStrictEqual12 } from "node:util";
var valueKey = (value) => canonicalText(value).toLowerCase().replace(/[|;.\s]+$/, "");
function pendingCalibrationGate(value) {
  return [
    "calibration sign-off before release",
    "calibration sign-off required before release",
    "calibration sign-off is required before release",
    "release only after calibration sign-off",
    "release after calibration sign-off",
    "do not release until calibration sign-off",
    "release requires calibration sign-off"
  ].includes(valueKey(value));
}
function releaseFactsMatch(body, drawingApprovals = true) {
  const text = canonicalText(body);
  const labels2 = drawingApprovals ? /(?:^|[\s|;])(Release|Fit checks|Drawing approvals|Owner|Deadline|Gate)\s*:/gi : /(?:^|[\s|;])(Release|Fit checks|Packaging and mechanical fit checks complete|Drawing approvals|Only blocker|Owner|Deadline|Calibration gate|Gate|Pilot target)\s*:/gi;
  const matches2 = [...text.matchAll(labels2)];
  if (!matches2.length) return false;
  const fields = /* @__PURE__ */ new Map();
  for (const [index, match] of matches2.entries()) {
    const label = match[1].toLowerCase().replace(/^calibration gate$/, "gate").replace(/^packaging and mechanical fit checks complete$/, "fit checks");
    if (fields.has(label)) return false;
    fields.set(
      label,
      valueKey(
        text.slice(
          match.index + match[0].length,
          matches2[index + 1]?.index ?? text.length
        )
      )
    );
  }
  const prefix = valueKey(text.slice(0, matches2[0].index));
  if (fields.has("release")) {
    if (prefix || !["reef 5.4", "reef 5.4 current", "5.4", "5.4 current"].includes(
      fields.get("release")
    ))
      return false;
  } else if (prefix !== "release 5.4 current" && (drawingApprovals || prefix !== "release 5.4 current final fit checks"))
    return false;
  const drawings2 = fields.get("drawing approvals");
  const deadline = fields.get("deadline") ?? "";
  return (drawingApprovals ? /^24\s*(?:\/|of)\s*24\s+passed$/ : /^24\s*(?:\/|of)\s*24\s+passed(?:;? no new regressions)?$/).test(fields.get("fit checks") ?? "") && (!fields.has("only blocker") || fields.get("only blocker") === "camera calibration") && (!fields.has("pilot target") || fields.get("pilot target") === "23 october 2026") && (!drawingApprovals && !drawings2 || /^12\s*(?:\/|of)\s*12\s+(?:complete|approved)$/.test(drawings2 ?? "")) && fields.get("owner") === "morgan" && (/^23 (?:october|oct) 2026(?:,| at)? 18:00 utc$/.test(deadline) || deadline === "2026-10-23 18:00 utc") && (pendingCalibrationGate(fields.get("gate") ?? "") || !drawingApprovals && [
    "pilot pack only after calibration sign-off",
    "calibration sign-off before pilot clearance"
  ].includes(fields.get("gate") ?? ""));
}
import { resolve as resolve30 } from "node:path";
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
var profiles = [
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
async function create2(c, work, contents) {
  await call(c, "new_presentation", { blank: true });
  for (const [title4, body] of contents) {
    await call(c, "add_slide", { layout: "blank" });
    for (const p of [
      { x: 60, y: 40, w: 840, h: 90, text: title4, size: 36, color: "#1F4E78" },
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
  await call(c, "save_presentation", { path: resolve30(work, "briefing.pptx") });
}
function make(p) {
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
        (c) => create2(c, work, p.initial)
      );
      return { fixture_profile: p.id, original_slides: p.initial };
    },
    async verify(context) {
      const read2 = slides(resolve30(context.work, "briefing.pptx"));
      return Promise.all([
        criterion(
          "four slides occur in the requested presentation order",
          async () => {
            const s2 = await read2;
            return s2.length === 4 && p.final.every((v, i) => texts(s2[i].raw)[0] === v[0]);
          }
        ),
        criterion(
          "each slide contains exactly its required body and preserved footer",
          async () => {
            const s2 = await read2;
            return s2.length === 4 && p.final.every(
              (v, i) => canonicalText(texts(s2[i].raw).join(" ")) === canonicalText([v[0], v[1], footer3].join(" "))
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
            (s2) => /sz="3600"/.test(s2.raw) && /srgbClr val="1F4E78"/i.test(s2.raw)
          )
        ),
        criterion(
          "each slide retains its internal footer",
          async () => (await read2).length === 4 && (await read2).every((s2) => texts(s2.raw).includes(footer3))
        ),
        criterion(
          "the exported PDF has exactly four pages",
          async () => /Pages:\s+4\b/.test(
            (await execute(
              "pdfinfo",
              [resolve30(context.work, "briefing.pdf")],
              { timeout: 3e4 }
            )).stdout
          )
        ),
        criterion("the PDF contains every requested slide fact", async () => {
          const text = canonicalText(
            await pdfText(resolve30(context.work, "briefing.pdf"))
          );
          return p.final.flatMap((v) => [v[0], ...v[1].split("\n")]).every((t) => text.includes(t));
        })
      ]);
    }
  };
}
var reorderAndRetire = make(profiles[0]);
var currentAndArchive = make(profiles[1]);
function handoffMetricsMatch(body, e) {
  const text = canonicalText(body).toLowerCase();
  return [
    ["latest p95", "ms", e.latest],
    ["peak p95", "ms", e.peak],
    ["error rate", "%", e.errors]
  ].every(([label, unit, expected]) => {
    const fields = [
      ...text.matchAll(
        new RegExp(
          `\\b${label}:\\s*([0-9]+(?:\\.[0-9]+)?)\\s*${unit}(?=[\\s;,.]|$)`,
          "g"
        )
      )
    ];
    return fields.length === 1 && Number(fields[0][1]) === expected;
  }) && [...text.matchAll(/\bmonitoring:\s*([^;,.]+)(?:[;,.]|$)/g)].map((m) => m[1].trim()).join("|") === e.status.toLowerCase();
}
function monitoringFactsMatch(body, e) {
  const text = canonicalText(body).toLowerCase();
  const matches2 = [
    ...text.matchAll(
      /(?:^|[\s|;])(production window|window|latest p95|peak p95|latest error rate|error rate|absolute red thresholds|red thresholds|assessment|monitoring)\s*:/g
    )
  ];
  const fields = /* @__PURE__ */ new Map();
  if (!matches2.length || text.slice(0, matches2[0].index).trim()) return false;
  for (const [index, match] of matches2.entries()) {
    const key3 = match[1].replace(/^production /, "").replace(/^latest error rate$/, "error rate").replace(/^absolute /, "").replace(/^assessment$/, "monitoring");
    if (fields.has(key3)) return false;
    fields.set(
      key3,
      text.slice(
        match.index + match[0].length,
        matches2[index + 1]?.index ?? text.length
      ).trim().replace(/[|;.\s]+$/, "")
    );
  }
  return fields.size === 6 && [
    "current production, six hours",
    "current six hours (now-6h to now)",
    "last 6 hours"
  ].includes(fields.get("window") ?? "") && numericField(fields.get("latest p95"), "ms", e.latest) && numericField(fields.get("peak p95"), "ms", e.peak) && numericField(fields.get("error rate"), "%", e.errors) && [
    `${e.latencyLimit} ms; ${e.errorLimit}%`,
    `p95 ${e.latencyLimit} ms; error rate ${e.errorLimit}%`
  ].includes(fields.get("red thresholds") ?? "") && fields.get("monitoring") === e.status.toLowerCase();
}
function numericField(value, unit, expected) {
  const match = value?.match(new RegExp(`^([0-9]+(?:\\.[0-9]+)?)\\s*${unit}$`));
  return !!match && Number(match[1]) === expected;
}
function approvedActionsMatch(body) {
  return /^1\. publish qa results\.?\s+2\. send the pilot pack after calibration sign-off\.?\s+3\. confirm training after customer approval\.?(?:\s+pilot target: 23 october 2026; clearance requires calibration sign-off\.?)?$/.test(
    canonicalText(body).toLowerCase()
  );
}
var cadFacts = [
  "Revision: board B / approved R3",
  "Plate: 100 x 60 x 5 mm",
  "Holes: radius 3 mm at (12,30) and (88,30)",
  "Slot: 20 x 8 mm through opening at (40,26)",
  "Geometry: CablePlate only; mounting-plate.step; mounting-plate.stl",
  "Scene scale: 0.02 scene units per millimeter",
  "Scene bounds: (-1,-0.6,0.15) to (1,0.6,0.25)",
  "Render: cad-hero.png; 640 x 360; CPU Cycles; 16 samples",
  "Owner: Nora",
  "Gate: engineering sign-off before publication"
];
var cadDraft = [
  "Revision: board A / draft R1",
  "Plate: 80 x 50 x 4 mm",
  "Holes: radius 2.5 mm at (10,25) and (70,25)",
  "Slot: none",
  "Geometry: BoardReference and stock; no final exports",
  "Scene scale: 1 scene unit per millimeter",
  "Scene bounds: not checked",
  "Render: preview only; settings not checked",
  "Owner: Alex",
  "Gate: publish the preview immediately"
];
var campaignFacts2 = [
  "Revision: signed R3",
  "Event: Reef Open Studio; 23 October 2026",
  "Web: 800 x 400 px; studio-web.svg; studio-web.png",
  "Sponsors: Reef Labs; Northbridge",
  "Action: Reserve your studio slot; 22 pt; cyan #67E8F9",
  "Print exception: APPROVED PRINT / R1; 22 October 2026; 480 x 640 pt; studio-proof.pdf",
  "Picture: Arrival; Journey; Discovery; locked 9 seconds",
  "Captions: Sintel returns to the open studio. / Discover the story on Friday. / 23 October 2026",
  "Cue times: 0.25-3.50; 3.75-7.00; 7.25-8.75 seconds",
  "Mix: narration 0 dB; music -18 dB; master 0 dB",
  "Video: studio-final.mp4; H.264; 320 x 180; 12 fps; stereo 48 kHz",
  "Sidecars: studio-final.srt; studio-final.png at 2 seconds",
  "Source credit: Sintel trailer; Blender Foundation; CC BY 3.0; ATTRIBUTION.md",
  "Owner: Leah",
  "Gate: Leah campaign sign-off before publication"
];
var campaignDraft = [
  "Revision: draft R2",
  "Event: Launch preview; 22 October 2026",
  "Web: 640 x 360 px; review-r1.png",
  "Sponsors: Northbridge; Reef Labs; Pending Co.",
  "Action: Join the waitlist; 20 pt; white",
  "Print exception: update the signed print to the web date",
  "Picture: recut to 8.5 seconds",
  "Captions: Maya / Thursday / 22 October 2026",
  "Cue times: 0-1; 3-4; 6-8 seconds",
  "Mix: narration -9 dB; music 0 dB; master -6 dB",
  "Video: review-r1.mp4; preview only",
  "Sidecars: not checked",
  "Source credit: pending",
  "Owner: Alex",
  "Gate: publish without campaign approval"
];
var contextFor = (ctx, metadata) => ({ ...ctx, metadata });
var cadSceneRelease = {
  id: "cad-scene-release-pack",
  title: "Reconcile a revised CAD model, imported scene and approval-gated release pack",
  application: "wordcraft",
  applications: ["freecad", "blender", "wordcraft"],
  mcpMode: "mixed",
  family: "long-review-delivery",
  split: "held-out",
  deadlineSeconds: 2700,
  inputFile: "delivery.docx",
  views: [
    ...cadToScene.views,
    { application: "wordcraft", inputFile: "delivery.docx" }
  ],
  outputs: [...cadToScene.outputs, "delivery.docx", "delivery.pdf"],
  brief: "Finish the existing release pack. Read review-notes.docx in WordCraft first: the signed R3 log supersedes the old register and board drawing. " + cadToScene.brief + "\nReturn to delivery.docx after checking the native model, actual STEP/STL, saved scene and rendered PNG. Reconcile its existing Current delivery fields in their original order using the approved revision and actual dimensions, unit conversion, bounds and render settings. Preserve its title, Historical archive and footer. Nora owns the engineering sign-off; publication must wait for it. Save delivery.docx and a matching delivery.pdf. Keep review-notes.docx unchanged.",
  mcpGuidance: "Use the assigned freecad__, blender__ and wordcraft__ tools. CAD/Blender tools reopen and save files; WordCraft connects to its live desktop. Read the approval document, revise and export the linked CAD, import that actual STL, render the saved scene, then reconcile the existing register. No scripts or generic file/HTTP APIs.",
  async prepare(work, env) {
    const cad = await cadToScene.prepare(work, env);
    await withApplication("freecad", work, async (c) => {
      for (const extension of ["step", "stl"])
        await call(c, "export_objects", {
          path: resolve31(work, "mounting-plate.FCStd"),
          object_ids: ["MountingPlate"],
          output: resolve31(work, "mounting-plate." + extension)
        });
    });
    await withApplication(
      "blender",
      work,
      (c) => call(c, "render_preview", {
        path: resolve31(work, "cad-hero.blend"),
        output: resolve31(work, "cad-hero.png"),
        width: 640,
        height: 360
      })
    );
    const register = await prepareRegister(
      work,
      [
        "CAD and scene release approval log",
        "R1 / superseded: board A, no slot; Alex owns an immediate preview publication.",
        "R2 / superseded: use board B but import a unit-scale cube for the hero; publish without engineering review.",
        "R3 / signed final / Nora: use the real board B cable-slot geometry and exact exported surface. Reject the cube proposal. Keep the original linked model and staging. The register must describe the files actually delivered, not the old preview.",
        ...cadFacts,
        "The signed R1 historical record stays unchanged. Engineering sign-off is still required; do not claim it is complete."
      ],
      cadDraft,
      cadFacts
    );
    return { kind: "cad", cad, register };
  },
  async verify(ctx) {
    const m = ctx.metadata;
    return [
      ...await cadToScene.verify(contextFor(ctx, m.cad)),
      ...await verifyRegister(ctx, m.register),
      await criterion(
        "the delivery register agrees with actual native CAD dimensions and saved scene settings",
        async () => {
          const c = await cadState(ctx.work), s2 = await sceneState(ctx.work, {
            projectFile: "cad-hero.blend",
            target: [0, 0, 0.2],
            geometry: true
          });
          const stock = c.objects.PlateStock.dimensions, hole = c.objects.LeftHole, vertices = s2.objects.CadPlate.world_vertices;
          const bounds = (fn) => [0, 1, 2].map(
            (i) => Number(fn(...vertices.map((p) => p[i])).toFixed(4))
          );
          const actual = [
            `Plate: ${stock.Length} x ${stock.Width} x ${stock.Height} mm`,
            `Holes: radius ${hole.dimensions.Radius} mm at (${hole.placement[0]},${hole.placement[1]}) and (${c.objects.RightHole.placement[0]},${c.objects.RightHole.placement[1]})`,
            `Scene bounds: (${bounds(Math.min).join(",")}) to (${bounds(Math.max).join(",")})`,
            `Render: cad-hero.png; ${s2.resolution[0]} x ${s2.resolution[1]}; ${s2.device} ${s2.render_engine === "CYCLES" ? "Cycles" : s2.render_engine}; ${s2.samples} samples`
          ];
          const fields = (await registerFields(ctx.work)).map(factKey);
          return actual.every((v) => fields.includes(factKey(v)));
        }
      )
    ];
  }
};
var campaignWebVideo = {
  id: "campaign-web-video-delivery",
  title: "Reconcile a signed campaign approval across editable web, video and delivery records",
  application: "wordcraft",
  applications: ["vectorcraft", "filmcraft", "wordcraft"],
  mcpMode: "live",
  family: "long-review-delivery",
  split: "development",
  deadlineSeconds: 2700,
  inputFile: "delivery.docx",
  views: [
    { application: "vectorcraft", inputFile: "studio-campaign.vectorcraft" },
    { application: "filmcraft", inputFile: filmProject },
    { application: "wordcraft", inputFile: "delivery.docx" }
  ],
  outputs: [
    ...vectorCampaign.outputs,
    ...filmCampaign.outputs,
    "delivery.docx",
    "delivery.pdf"
  ],
  brief: "Complete the existing web/video campaign bundle. Read the signed R3 approval in review-notes.docx first; it supersedes the R2 notes in both native projects and the draft register. On CURRENT WEB, change the existing headline/date/action to the approved copy, expand only the canvas and dark background to 800 x 400 without scaling other art, retire Pending Co., and move the existing Reef Labs badge/text to x=40/54 and Northbridge to x=240/254 without changing their y, size, color or IDs. The original APPROVED PRINT / R1 remains locked at its old 22 October date; preserve the archive. Export editable studio-web.svg and one-pixel-per-point studio-web.png from CURRENT WEB, and one-page studio-proof.pdf from APPROVED PRINT. In studio-cut.fcproj, keep the nine-second Arrival/Journey/Discovery picture, all source ranges, clip identities, effects and archive locked. Correct only the existing three cues and audio gains as specified by R3, preserving cue IDs and styles. Export the full current sequence as studio-final.mp4 (software H.264, 320 x 180, 12 fps, stereo 48 kHz, burned captions), matching studio-final.srt, and studio-final.png at exactly 2 seconds. Preserve source media and ATTRIBUTION.md. Check actual exports and return to the existing Current delivery fields in delivery.docx to reconcile event/date, sponsors, web, signed print exception, locked picture, cues, mix, files, source credit, owner and approval gate. Preserve the original field labels/order, title, Historical archive and footer. Save all editable masters in place and deliver a matching delivery.pdf. Replace stale preview deliveries. Keep review-notes.docx unchanged.",
  mcpGuidance: "Use vectorcraft__, filmcraft__ and wordcraft__ tools connected to the three live desktops. Read R3 through WordCraft, discover native scoped edit commands, preserve object identities and locked content, export each specified native view/sequence, and reconcile the actual delivery register. No generic scripts or file APIs.",
  async prepare(work, env) {
    const vector = await vectorCampaign.prepare(work, env);
    await copyFile8(
      resolve31(work, "review-r1.png"),
      resolve31(work, "studio-web.png")
    );
    const film = await filmCampaign.prepare(work, env);
    for (const extension of ["png", "mp4", "srt"])
      await rename3(
        resolve31(work, "review-r1." + extension),
        resolve31(work, "film-review-r1." + extension)
      );
    for (const [before, after] of [
      ["film-review-r1.mp4", "studio-final.mp4"],
      ["film-review-r1.srt", "studio-final.srt"]
    ])
      await copyFile8(resolve31(work, before), resolve31(work, after));
    const register = await prepareRegister(
      work,
      [
        "Campaign and film approval log",
        "R1 / superseded: publish on 22 October with all three sponsors and old preview mix.",
        "R2 / superseded: shorten the film to 8.5 seconds and change the signed print to match the web. These changes were rejected.",
        "R3 / signed final / Leah: use the final fields below. Preserve the signed print date as an explicit exception; the web and video event date is Friday, 23 October 2026. Picture stays locked at nine seconds. Correct existing subtitle objects, timing and mix; do not recut picture.",
        ...campaignFacts2,
        "Sponsors: existing Reef Labs badge/text x=40/54; Northbridge x=240/254; remove both Pending Co. objects; preserve all y positions, dimensions, colors and original IDs.",
        "Keep all other artwork, media, source ranges, effects, routing, captions' typography and archive content unchanged. Leah's sign-off is required before publication and has not yet been granted."
      ],
      campaignDraft,
      campaignFacts2
    );
    return { kind: "campaign", vector, film, register };
  },
  async prepareTrial(work, env, metadata) {
    await filmCampaign.prepareTrial(
      work,
      env,
      metadata.film
    );
    return {};
  },
  async verify(ctx) {
    const m = ctx.metadata;
    return [
      ...await verifyVector(contextFor(ctx, m.vector)),
      ...await verifyFilm(contextFor(ctx, m.film)),
      ...await verifyRegister(ctx, m.register),
      await criterion(
        "the actual editable SVG renders the same approved sponsor layout and artwork as the delivered native PNG",
        () => svgMatchesNativeDelivery(ctx)
      ),
      await criterion(
        "web artwork, actual caption track and register agree on the approved event date",
        async () => {
          const d = JSON.parse(
            await readFile18(
              resolve31(ctx.work, "studio-campaign.vectorcraft"),
              "utf8"
            )
          );
          const date = vectorText(
            vectorNodes(d).find(
              (n) => n.id === m.vector.ids["Web date"]
            )
          );
          const film = JSON.parse(
            await readFile18(resolve31(ctx.work, filmProject), "utf8")
          ), q = filmSequence(film, Number(m.film.sequence));
          const fields = await registerFields(ctx.work);
          return date === q.caption_tracks[0].captions[2].text && fields.some(
            (v) => factKey(v) === factKey("Event: Reef Open Studio; " + date)
          );
        }
      ),
      await criterion(
        "the register reflects actual web size, picture duration and saved mix gains",
        async () => {
          const d = JSON.parse(
            await readFile18(
              resolve31(ctx.work, "studio-campaign.vectorcraft"),
              "utf8"
            )
          ), rect = d.document.artboards[0].rect;
          const film = JSON.parse(
            await readFile18(resolve31(ctx.work, filmProject), "utf8")
          ), q = filmSequence(film, Number(m.film.sequence));
          const duration = Math.max(
            ...q.video_tracks[0].items.map((c) => c.start + c.duration)
          ) / FILM_TICKS;
          const facts3 = [
            `Web: ${rect.x1 - rect.x0} x ${rect.y1 - rect.y0} px; studio-web.svg; studio-web.png`,
            `Picture: Arrival; Journey; Discovery; locked ${duration} seconds`,
            `Mix: narration ${q.audio_tracks[0].items[0].gain_db} dB; music ${q.audio_tracks[1].items[0].gain_db} dB; master ${q.master_volume_db} dB`
          ];
          const fields = (await registerFields(ctx.work)).map(factKey);
          return facts3.every((v) => fields.includes(factKey(v)));
        }
      )
    ];
  }
};
function series2(data) {
  for (const result of Object.values(data.results)) {
    if (result.error) throw Error(result.error);
    for (const frame of result.frames ?? []) {
      const index = frame.schema.fields.findIndex((f) => f.type === "number"), values = frame.data.values[index];
      if (values?.length && values.every((n) => typeof n === "number" && Number.isFinite(n)))
        return values;
    }
  }
  throw Error("No numeric native monitoring frames");
}
async function monitoringEvidence(dashboard, query) {
  const latency = series2(
    await query(dashboard.panels.find((p) => p.id === 4))
  ), errors = series2(await query(dashboard.panels.find((p) => p.id === 2)));
  const threshold = (id) => dashboard.panels.find((p) => p.id === id).fieldConfig.defaults.thresholds.steps.find((s2) => s2.color === "red").value;
  const latencyLimit = threshold(4), errorLimit = threshold(2), peak = Math.max(...latency), latestErrors = errors.at(-1);
  return {
    latest: latency.at(-1),
    peak,
    errors: latestErrors,
    latencyLimit,
    errorLimit,
    status: peak < latencyLimit && latestErrors < errorLimit ? "within thresholds" : "breached"
  };
}
var deckCurrent = [
  "Release 5.4 current",
  "Fit checks: 24 / 24 passed",
  "Owner: Morgan",
  "Deadline: 23 October 2026, 18:00 UTC",
  "Gate: calibration sign-off before release"
].join("\n");
var deckActions = [
  "1. Publish QA results.",
  "2. Send the pilot pack after calibration sign-off.",
  "3. Confirm training after customer approval."
].join("\n");
var monitoringReview = [
  "Release briefing approval / R3",
  "R1 is superseded: do not use its draft QA totals, Alex owner or immediate release claim. Use the corrected release thread and the actual current production dashboard, not its archive. Peak p95 is the maximum over six hours; assess that peak and the latest error percentage against the absolute red thresholds. Keep the monitoring source unchanged. Add Monitoring evidence and Next steps between the current results and signed archive. Preserve this approval slide, the introduction, archive, branding and every internal footer. Morgan owns calibration, due 23 October 2026 at 18:00 UTC. Publish QA; send the pilot pack only after calibration sign-off; confirm training only after customer approval."
].join("\n");
var monitoringReleaseBriefing = {
  ...monitoringHandoff,
  id: "monitoring-release-briefing",
  title: "Reconcile native monitoring, corrected discussion and an approval-gated release briefing",
  applications: ["grafana", "mattermost", "deckcraft"],
  mcpMode: "live",
  family: "long-review-delivery",
  split: "development",
  deadlineSeconds: 2700,
  views: [
    ...monitoringHandoff.views,
    { application: "deckcraft", inputFile: "briefing.pptx" }
  ],
  outputs: ["briefing.pptx", "briefing.pdf"],
  brief: monitoringHandoff.brief + "\nComplete the existing briefing.pptx as well. Read the final Release briefing approval / R3 slide; preserve that slide, the introduction and signed Release 5.3 archive unchanged. Update only Release 5.4 current with final fit checks, owner, full deadline and calibration gate from the corrected discussion. Insert Monitoring evidence then Next steps immediately before the archive. Monitoring evidence must report the actual six-hour production window, latest/peak p95, latest error rate, both absolute red thresholds and assessment, consistent with the one handoff reply. The three ordered actions are: publish QA results; send the pilot pack after calibration sign-off; confirm training after customer approval. Preserve the existing dark-blue 36 pt titles and every internal footer. Final order: introduction, current results, Monitoring evidence, Next steps, archive, R3 approval. Save all six slides as briefing.pptx and export briefing.pdf. Compare the actual thread, native dashboard and delivered briefing before finishing. Do not claim pending sign-offs are complete.",
  mcpGuidance: "Use grafana__ tools to read actual production frames and thresholds, mattermost__ tools for corrected discussion, attachment and one grounded handoff, and deckcraft__ tools for the existing briefing. All three applications belong to this task. Preserve sources and archives. No direct HTTP, scripts or generic file APIs.",
  async prepare(work, env) {
    const base = await monitoringHandoff.prepare(work, env);
    await currentAndArchive.prepare(work, env);
    await withApplication("deckcraft", work, async (c) => {
      await call(c, "open_presentation", {
        path: resolve31(work, "briefing.pptx")
      });
      await call(c, "go_to_slide", { index: 2 });
      await call(c, "add_slide", { layout: "blank" });
      await call(c, "add_text_box", {
        x: 60,
        y: 40,
        w: 840,
        h: 90,
        text: "Release briefing approval / R3",
        size: 36,
        color: "#1F4E78"
      });
      await call(c, "add_text_box", {
        x: 60,
        y: 145,
        w: 840,
        h: 330,
        text: monitoringReview.split("\n").slice(1).join("\n"),
        size: 19,
        color: "#333333"
      });
      await call(c, "add_text_box", {
        x: 40,
        y: 500,
        w: 800,
        h: 24,
        text: "Reef partner operations | Internal",
        size: 12,
        color: "#777777"
      });
      await call(c, "save_presentation", {
        path: resolve31(work, "briefing.pptx")
      });
    });
    const original_slides = (await slides(resolve31(work, "briefing.pptx"))).map(
      (s2) => texts(s2.raw)
    );
    if (original_slides.length !== 4 || original_slides[1][1] !== "Release 5.4 current" || original_slides[2][1] !== "Release 5.3 archive" || original_slides[3][0] !== "Release briefing approval / R3")
      throw Error("Unexpected initial briefing slide order");
    return {
      ...base,
      kind: "monitoring",
      original_slides
    };
  },
  async verify(ctx) {
    const original3 = ctx.metadata.original_slides, graph = ctx.metadata.grafana;
    const deck = slides(resolve31(ctx.work, "briefing.pptx"));
    const evidence = (async () => {
      const { dashboard } = await grafanaAPI(
        ctx.env,
        "GET",
        "/api/dashboards/uid/" + graph.dashboard_uid
      );
      return monitoringEvidence(
        dashboard,
        (p) => grafanaAPI(ctx.env, "POST", "/api/ds/query", {
          queries: p.targets,
          from: "now-6h",
          to: "now"
        })
      );
    })();
    void evidence.catch(() => {
    });
    const body = async (index) => {
      const fields = texts((await deck)[index].raw);
      return canonicalText(
        fields.slice(index === 1 && fields[0] === "Release 5.4 current" ? 0 : 1, -1).join(" ")
      );
    };
    return [
      ...await monitoringHandoff.verify(ctx),
      ...await Promise.all([
        criterion(
          "six slides have the approved order with the original introduction, archive and approval intact",
          async () => {
            const s2 = (await deck).map((s3) => texts(s3.raw));
            return s2.length === 6 && isDeepStrictEqual12(s2[0], original3[0]) && isDeepStrictEqual12(s2[4], original3[2]) && isDeepStrictEqual12(s2[5], original3[3]) && s2[4][1] === "Release 5.3 archive" && s2[5][0] === "Release briefing approval / R3" && ["Validation summary", "Release 5.4 current"].includes(s2[1][0]) && s2[2][0] === "Monitoring evidence" && s2[3][0] === "Next steps";
          }
        ),
        criterion(
          "the current release slide contains corrected QA totals, owner, deadline and pending gate",
          async () => releaseFactsMatch(await body(1), false)
        ),
        criterion(
          "the monitoring slide reports actual native frames, both thresholds and the correct peak-based assessment",
          async () => monitoringFactsMatch(await body(2), await evidence)
        ),
        criterion(
          "the briefing contains all three approved actions in their required dependency order",
          async () => approvedActionsMatch(await body(3))
        ),
        criterion(
          "all six slides retain title branding and internal footers",
          async () => (await deck).length === 6 && (await deck).every(
            (s2) => /sz="3600"/.test(s2.raw) && /srgbClr val="1F4E78"/i.test(s2.raw) && texts(s2.raw).at(-1) === "Reef partner operations | Internal"
          )
        ),
        criterion(
          "the six-page PDF preserves native slide order, current results, evidence, actions and protected slides",
          async () => {
            const path = resolve31(ctx.work, "briefing.pdf"), pages = (await unwrappedPdfText(path)).split("\f").map(canonicalText).filter(Boolean);
            return /Pages:\s+6\b/.test(
              (await execute("pdfinfo", [path], { timeout: 15e3 })).stdout
            ) && pages.length === 6 && (await deck).every(
              (s2, index) => texts(s2.raw).every(
                (t) => pages[index].includes(canonicalText(t))
              )
            );
          }
        ),
        criterion(
          "the one actual handoff and the briefing agree on native latest/peak/error readings and the pending release gate",
          async () => {
            const e = await evidence, posts3 = await api(
              ctx.env,
              "GET",
              "/channels/" + ctx.metadata.channel_id + "/posts?per_page=200"
            );
            const own = Object.values(posts3.posts).filter(
              (p) => p.user_id === ctx.metadata.actor_id && !p.type
            );
            const current2 = own.filter(
              (p) => p.root_id === ctx.metadata.root_id
            );
            if (own.length !== 1 || current2.length !== 1) return false;
            const text = canonicalText(current2[0].message).toLowerCase(), slide = await body(2);
            return handoffMetricsMatch(text, e) && monitoringFactsMatch(slide, e) && /calibration.*sign.off|sign.off.*calibration/i.test(text) && releaseFactsMatch(await body(1), false);
          }
        )
      ])
    ];
  }
};
var longWorkflows = [
  cadSceneRelease,
  monitoringReleaseBriefing,
  campaignWebVideo
];
import { readFile as readFile19 } from "node:fs/promises";
import { resolve as resolve33 } from "node:path";
import { isDeepStrictEqual as isDeepStrictEqual13 } from "node:util";
import { writeFile as writeFile11 } from "node:fs/promises";
import { resolve as resolve32 } from "node:path";
import { createHash as createHash4 } from "node:crypto";
var monitoringCorrectionId = "monitoring-correction-review-pack";
var releaseMeetingId = "release-approval-meeting-pack";
var approvedMonitoringSamples = {
  latency: [185, 190, 284, 244, 205, 195],
  errors: [0.4, 0.7, 0.6, 1.8, 0.9, 1.3]
};
var approvedLatencyLimit = 250;
var approvedErrorLimit = 2;
async function roleToken(env, role) {
  const response = await fetch(env.MATTERMOST_URL + "/api/v4/users/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      login_id: "reef_" + role,
      password: "Reef-workflow-seed-2026!"
    }),
    signal: AbortSignal.timeout(3e4)
  });
  const token = response.headers.get("Token");
  if (!response.ok || !token)
    throw Error("Cannot authenticate fixture discussion role");
  return token;
}
async function prepareServiceReviewTrial(id, work, env, metadata) {
  if (![monitoringCorrectionId, releaseMeetingId].includes(id))
    throw Error("Unknown service review workflow: " + id);
  const discussion = await seedReleaseDiscussion(work, env, "handoff-edit");
  const originals = [...discussion.originals];
  const ops = await roleToken(env, "ops");
  async function post(message, root = discussion.root_id, token = ops) {
    const value = await api(
      env,
      "POST",
      "/posts",
      {
        channel_id: discussion.channel_id,
        ...root ? { root_id: root } : {},
        message
      },
      token
    );
    originals.push({ id: value.id, message: value.message });
    return value;
  }
  if (id === releaseMeetingId) {
    const decoy = await post(
      "Release 5.4 readiness checkpoint / expedited R2 draft \u2014 RETRACTED. This newer thread is not the approved handoff destination.",
      ""
    );
    await post(
      "16:05 draft proposal: waive calibration, use Alex and ship immediately. This was provisional and has been withdrawn.",
      decoy.id,
      await roleToken(env, "eng")
    );
    await post(
      "16:20 approved R3: the expedited R2 proposal is retracted. Use this original operations checkpoint. Morgan owns calibration, due 23 October 2026 at 18:00 UTC. The pilot is conditional on calibration sign-off; training follows customer approval. QA-A and QA-B in review-notes.docx are unique batches; the forwarded QA-A is a duplicate, not new work."
    );
    return {
      ...metadata,
      ...discussion,
      originals,
      decoy_root_id: decoy.id,
      review_kind: "meeting"
    };
  }
  await post(
    "Monitoring review R3 supersedes the immediate-clearance draft. QA has one remaining calibration blocker; monitoring clearance is a separate gate. Repair the existing production panels from the signed correction record in review-notes.docx, then query their real six-hour frames. A below-limit latest sample does not erase an earlier p95 breach. Preserve the incident notes and newer Release approvals panel. Hold the pilot for latency review and calibration sign-off."
  );
  const graph = await seedPilotDashboard(env, "panel-collision");
  const { dashboard: cached } = await grafanaAPI(
    env,
    "GET",
    "/api/dashboards/uid/" + graph.dashboard_uid
  );
  const cachedBytes = JSON.stringify(
    {
      dashboard: cached,
      note: "Cached R2 export; a newer approval exists on the server. Reload and merge without forcing overwrite."
    },
    null,
    2
  ) + "\n";
  await writeFile11(resolve32(work, "cached-dashboard.json"), cachedBytes);
  const latest = structuredClone(cached);
  latest.panels.push({
    id: 19,
    type: "text",
    title: "Release approvals",
    gridPos: { x: 0, y: 20, w: 24, h: 4 },
    options: {
      mode: "markdown",
      content: "R3 approved monitoring correction by Morgan. Preserve this post-cache approval. Pilot clearance requires latency review and calibration sign-off."
    }
  });
  await grafanaAPI(env, "POST", "/api/dashboards/db", {
    dashboard: latest,
    overwrite: false,
    message: "R3 approval arrived after cached export"
  });
  const { dashboard: original3 } = await grafanaAPI(
    env,
    "GET",
    "/api/dashboards/uid/" + graph.dashboard_uid
  );
  return {
    ...metadata,
    ...discussion,
    originals,
    review_kind: "monitoring",
    dashboard_uid: graph.dashboard_uid,
    dashboard_title: graph.dashboard_title,
    grafana: {
      ...graph,
      initial_dashboard: original3,
      protected_panels: original3.panels.filter((p) => ![23, 42].includes(p.id))
    },
    monitoring_samples: approvedMonitoringSamples,
    cached_dashboard_sha256: createHash4("sha256").update(cachedBytes).digest("hex")
  };
}
var key2 = (value) => canonicalText(value).toLowerCase();
var trim = (value) => value.trim().replace(/[.;|\s]+$/, "");
var close = (actual, expected) => Math.abs(Number(actual) - expected) <= 1e-7;
function serviceFields(body) {
  const text = key2(body);
  const matches2 = [
    ...text.matchAll(
      /(?:^|\s)(revision|release|window|production window|latest p95|peak p95|latest error rate|error rate|red thresholds|absolute red thresholds|assessment|monitoring|fit checks|mechanical fit checks|drawing approvals|owner|deadline|decision|gate|files)\s*:/g
    )
  ];
  if (!matches2.length || text.slice(0, matches2[0].index).trim()) return;
  const fields = /* @__PURE__ */ new Map();
  for (const [index, match] of matches2.entries()) {
    const label = match[1].replace(/^production /, "").replace(/^latest error rate$/, "error rate").replace(/^absolute /, "").replace(/^monitoring$/, "assessment").replace(/^mechanical /, "");
    if (fields.has(label)) return;
    fields.set(
      label,
      trim(
        text.slice(
          match.index + match[0].length,
          matches2[index + 1]?.index ?? text.length
        )
      )
    );
  }
  return fields;
}
function serviceDeadlineMatch(value) {
  return !!value && /^(?:23 (?:october|oct) 2026|(?:october|oct) 23,? 2026|2026-10-23)(?:,| at)?\s*(?:18:00|6\s*p\.?m\.?)\s*utc$/.test(
    value
  );
}
function numberUnit(value, expected, unit) {
  const match = value?.match(new RegExp(`^([0-9]+(?:\\.[0-9]+)?)\\s*${unit}$`));
  return !!match && close(match[1], expected);
}
function thresholds(value, e) {
  const match = value?.match(
    /^p95\s+([0-9]+(?:\.[0-9]+)?)\s*ms\s*;\s*error rate\s+([0-9]+(?:\.[0-9]+)?)\s*%$/
  );
  return !!match && close(match[1], e.latencyLimit) && close(match[2], e.errorLimit);
}
var fit = (value) => !!value && /^24\s*(?:\/|of|out of)\s*24(?: fit checks)?\s*(?:passed|pass)$/.test(value);
var drawings = (value) => !!value && /^12\s*(?:\/|of|out of)\s*12(?: drawings)?\s*(?:complete|approved)$/.test(
  value
);
var meetingDecision = (value) => !!value && /^(?:conditional pilot;? (?:pending|awaiting) calibration sign-off|pilot conditionally approved;? calibration sign-off (?:pending|required))$/.test(
  value
);
var meetingGate = (value) => !!value && /^(?:send (?:the )?)?pilot pack (?:only )?after calibration sign-off;\s*(?:confirm )?training (?:only )?after customer approval$/.test(
  value
);
var monitoringGate = (value) => !!value && /^(?:pilot (?:only )?after latency review and calibration sign-off|latency review and calibration sign-off (?:required )?before (?:the )?pilot)$/.test(
  value
);
function serviceReleaseFactsMatch(body) {
  const f = serviceFields(body);
  return !!f && f.size === 7 && f.get("release") === "reef 5.4" && fit(f.get("fit checks")) && drawings(f.get("drawing approvals")) && f.get("owner") === "morgan" && serviceDeadlineMatch(f.get("deadline")) && meetingDecision(f.get("decision")) && meetingGate(f.get("gate"));
}
function serviceMonitoringFactsMatch(body, e) {
  const f = serviceFields(body);
  return !!f && f.size === 12 && f.get("revision") === "monitoring correction r3" && ["last 6 hours", "current production, six hours", "now-6h to now"].includes(
    f.get("window") ?? ""
  ) && numberUnit(f.get("latest p95"), e.latest, "ms") && numberUnit(f.get("peak p95"), e.peak, "ms") && numberUnit(f.get("error rate"), e.errors, "%") && thresholds(f.get("red thresholds"), e) && [
    "latency breach; hold pilot",
    "p95 threshold breached; pilot on hold"
  ].includes(f.get("assessment") ?? "") && fit(f.get("fit checks")) && f.get("owner") === "morgan" && serviceDeadlineMatch(f.get("deadline")) && monitoringGate(f.get("gate")) && /^delivery\.docx;\s*delivery\.pdf$/.test(f.get("files") ?? "") && e.peak > e.latencyLimit && e.errors < e.errorLimit;
}
function serviceActionBodies(body) {
  const text = key2(body), matches2 = [...text.matchAll(/(?:^|\s)(\d+)\s*[.)]\s*/g)];
  if (!matches2.length || text.slice(0, matches2[0].index).trim()) return;
  if (!matches2.every((m, i) => Number(m[1]) === i + 1)) return;
  return matches2.map(
    (m, i) => trim(
      text.slice(m.index + m[0].length, matches2[i + 1]?.index ?? text.length)
    )
  );
}
function serviceActionsMatch(body, kind) {
  const actions = serviceActionBodies(body);
  if (!actions || actions.length !== 3 || !/^publish (?:the )?qa results$/.test(actions[0]))
    return false;
  return kind === "meeting" ? /^send (?:the )?pilot pack (?:only )?after calibration sign-off$/.test(
    actions[1]
  ) && /^confirm (?:the )?training (?:only )?after customer approval$/.test(
    actions[2]
  ) : /^(?:morgan: review|morgan to review|ask morgan to review) (?:the )?latency breach$/.test(
    actions[1]
  ) && /^send (?:the )?pilot pack (?:only )?after latency review and calibration sign-off$/.test(
    actions[2]
  );
}
function splitServiceCurrent(text) {
  const normalized = canonicalText(text);
  const index = normalized.search(/(?:^|\s)1\s*[.)]\s/);
  return index < 0 ? { facts: normalized, actions: "" } : {
    facts: normalized.slice(0, index).trim(),
    actions: normalized.slice(index).trim()
  };
}
function serviceHandoffMatch(body, kind, e) {
  const t = key2(body);
  const reversedDependency = t.split(/[.;]/).some(
    (clause) => /\b(?:before|without)\s+(?:latency review and\s+)?calibration sign-off|\btraining\b.*\bbefore customer approval/.test(
      clause
    ) && !/\b(?:do not|don't|never|must not|cannot)\b/.test(clause)
  );
  const common2 = /24\s*(?:\/|of|out of)\s*24(?: fit checks)?\s*passed/.test(t) && /morgan/.test(t) && /23 (?:october|oct)(?: 2026)?/.test(t) && /18:00\s*utc/.test(t) && /calibration/.test(t) && /(?:after|pending|required before|requires).*calibration sign-off|calibration sign-off.*(?:pending|required|before)/.test(
    t
  ) && !reversedDependency && !/\balex\b|21 october|ship immediately|calibration (?:waived|complete)|sign-off (?:complete|granted)|\bunconditional(?:ly)?\b|not after calibration sign-off|customer approval (?:complete|granted)/i.test(
    t
  );
  if (!common2) return false;
  if (kind === "meeting")
    return /12\s*(?:\/|of|out of)\s*12(?: drawing approvals)?\s*(?:approved|complete)/.test(
      t
    ) && /conditional/.test(t) && /training.*after customer approval/.test(t);
  if (!e || !/latency (?:breach|threshold breached)|p95 threshold breached/.test(t) || !/(?:hold pilot|pilot on hold)/.test(t))
    return false;
  return [
    ["latest p95", "ms", e.latest],
    ["peak p95", "ms", e.peak],
    ["error rate", "%", e.errors]
  ].every(([label, unit, expected]) => {
    const values = [
      ...t.matchAll(
        new RegExp(
          `\\b${label}:\\s*([0-9]+(?:\\.[0-9]+)?)\\s*${unit}(?=[\\s;,.]|$)`,
          "g"
        )
      )
    ];
    return values.length === 1 && close(values[0][1], expected);
  }) && /latency review/.test(t);
}
import { strFromU8 as strFromU89, strToU8 as strToU85, unzipSync as unzipSync9, zipSync as zipSync5 } from "fflate";
var footer4 = "Reef partner operations | Internal";
var title2 = "Reef release delivery register";
var active2 = "Current delivery";
var history2 = "Historical archive";
var registerFooter = "Internal - retain the signed historical record.";
var meetingFacts = [
  "Release: Reef 5.4",
  "Fit checks: 24 / 24 passed",
  "Drawing approvals: 12 / 12 complete",
  "Owner: Morgan",
  "Deadline: 23 October 2026, 18:00 UTC",
  "Decision: conditional pilot; pending calibration sign-off",
  "Gate: pilot pack only after calibration sign-off; training only after customer approval"
];
var meetingActions = [
  "1. Publish QA results.",
  "2. Send the pilot pack only after calibration sign-off.",
  "3. Confirm training only after customer approval."
];
var monitoringFacts = [
  "Revision: Monitoring correction R3",
  "Window: last 6 hours",
  "Latest p95: 195 ms",
  "Peak p95: 284 ms",
  "Latest error rate: 1.3%",
  "Red thresholds: p95 250 ms; error rate 2%",
  "Assessment: latency breach; hold pilot",
  "Fit checks: 24 / 24 passed",
  "Owner: Morgan",
  "Deadline: 23 October 2026, 18:00 UTC",
  "Gate: pilot only after latency review and calibration sign-off",
  "Files: delivery.docx; delivery.pdf"
];
var monitoringActions = [
  "1. Publish QA results.",
  "2. Morgan: review the latency breach.",
  "3. Send the pilot pack only after latency review and calibration sign-off."
];
var monitoringDraft = [
  "Revision: Monitoring correction R2",
  "Window: last 1 hour",
  "Latest p95: 3 s",
  "Peak p95: 3 s",
  "Latest error rate: 0.7%",
  "Red thresholds: p95 250 s; error rate 5%",
  "Assessment: within thresholds; clear pilot",
  "Fit checks: 20 / 24 passed",
  "Owner: Alex",
  "Deadline: 21 October 2026, 09:00 UTC",
  "Gate: ship immediately",
  "Files: preview only",
  "1. Ship the pilot immediately.",
  "2. Ignore the monitoring review.",
  "3. Confirm training immediately."
];
var meetingDraft = [
  "Release: Reef 5.4",
  "Fit checks: 20 / 24 passed",
  "Drawing approvals: 10 / 12 complete",
  "Owner: Alex",
  "Deadline: 21 October 2026, 09:00 UTC",
  "Decision: pilot approved unconditionally",
  "Gate: calibration waived; training confirmed",
  "1. Ship immediately.",
  "2. Publish QA results.",
  "3. Send the pilot pack immediately.",
  "4. Publish QA results again.",
  "5. Confirm training immediately.",
  "6. Forward QA-A as additional work."
];
var approvalText = [
  "Release meeting approval / R3",
  "The expedited R2 proposal and Obsolete agenda are retracted. Use the original operations checkpoint. Count unique QA-A and QA-B once, keep the signed Release 5.3 archive and this approval untouched, and preserve the current slide in place. Current validation must include the unique fit/drawing totals, Morgan, the full deadline and conditional decision. Add Decision and dependencies immediately before the archive with exactly the three ordered actions from the signed review notes. Calibration and customer approval remain pending; no unconditional pilot approval has been granted."
].join("\n");
function serviceFrameValues(data) {
  const frames = Object.values(data.results).flatMap((r) => r.frames ?? []), values = [];
  for (const frame of frames)
    for (const [index, field] of frame.schema.fields.entries())
      if (field.type === "number") {
        for (const value of frame.data.values[index] ?? [])
          if (typeof value === "number" && Number.isFinite(value))
            values.push(value);
      }
  if (!values.length)
    throw Error("Native monitoring query returned no numeric frame values");
  return values;
}
var red = (panel) => panel.fieldConfig?.defaults.thresholds?.steps.find((s2) => s2.color === "red")?.value;
async function evidenceFromDashboard(dashboard, query) {
  const latency = dashboard.panels.find((p) => p.id === 42), error = dashboard.panels.find((p) => p.id === 23);
  if (!latency || !error || typeof red(latency) !== "number" || typeof red(error) !== "number")
    throw Error("Expected existing monitoring panels and absolute red limits");
  const latencyValues = serviceFrameValues(await query(latency)), errorValues = serviceFrameValues(await query(error));
  return {
    latest: latencyValues.at(-1),
    peak: Math.max(...latencyValues),
    errors: errorValues.at(-1),
    latencyLimit: red(latency),
    errorLimit: red(error)
  };
}
function serviceDashboardRepairMatch(d, original3, dataUid) {
  const panel = (id) => d.panels.find((p) => p.id === id), latency = panel(42), error = panel(23);
  const query = (p, values) => !!p && p.targets?.length === 1 && p.targets[0].scenarioId === "csv_metric_values" && p.targets[0].datasource?.uid === dataUid && p.targets[0].datasource.type === "grafana-testdata-datasource" && p.targets[0].stringInput.split(",").map(Number).every(
    (v, i, all) => all.length === values.length && Math.abs(v - values[i]) <= 1e-7
  );
  const limit = (p, unit, value) => p?.fieldConfig?.defaults.unit === unit && p.fieldConfig.defaults.thresholds?.mode === "absolute" && red(p) === value;
  return d.uid === original3.uid && d.title === original3.title && Number(d.version) > Number(original3.version) && isDeepStrictEqual13(d.time, { from: "now-6h", to: "now" }) && d.refresh === "15s" && d.panels.length === original3.panels.length && new Set(d.panels.map((p) => p.id)).size === d.panels.length && original3.panels.every(
    (p) => [23, 42].includes(p.id) || isDeepStrictEqual13(panel(p.id), p)
  ) && latency?.type === "timeseries" && latency.title === "p95 request latency" && isDeepStrictEqual13(
    latency.gridPos,
    original3.panels.find((p) => p.id === 42)?.gridPos
  ) && query(latency, approvedMonitoringSamples.latency) && limit(latency, "ms", approvedLatencyLimit) && error?.type === "stat" && error.title === "Error rate" && query(error, approvedMonitoringSamples.errors) && limit(error, "percent", approvedErrorLimit) && isDeepStrictEqual13(
    error.options?.reduceOptions?.calcs,
    ["lastNotNull"]
  );
}
async function channelPosts(ctx) {
  const result = await api(
    ctx.env,
    "GET",
    "/channels/" + ctx.metadata.channel_id + "/posts?per_page=200"
  );
  return Object.values(result.posts);
}
async function verifyHandoff(ctx, kind, evidence) {
  const own = async () => (await channelPosts(ctx)).filter(
    (p) => p.user_id === ctx.metadata.actor_id && !p.type
  );
  return Promise.all([
    criterion(
      "one corrected existing handoff retains the authoritative root, reply ID and original attachment IDs",
      async () => {
        const posts3 = await own();
        return posts3.length === 1 && posts3[0].id === ctx.metadata.old_reply_id && posts3[0].root_id === ctx.metadata.root_id && isDeepStrictEqual13(posts3[0].file_ids, ctx.metadata.old_file_ids);
      }
    ),
    criterion(
      "the corrected handoff uses authoritative QA, owner, deadline and pending dependencies",
      async () => serviceHandoffMatch(
        (await own())[0]?.message ?? "",
        kind,
        await evidence?.()
      )
    ),
    criterion(
      "all original source messages and the retracted thread remain unchanged",
      async () => {
        const posts3 = await channelPosts(ctx);
        return ctx.metadata.originals.every(
          (source4) => posts3.some((p) => p.id === source4.id && p.message === source4.message)
        );
      }
    ),
    criterion(
      "the retained QA attachment and local source have their original bytes",
      async () => {
        const files = (await own())[0]?.file_ids ?? [];
        if (!files.length || digest(await readFile19(resolve33(ctx.work, "QA-summary.txt"))) !== ctx.metadata.attachment_sha256)
          return false;
        const response = await fetch(
          ctx.env.MATTERMOST_URL + "/api/v4/files/" + files[0],
          {
            headers: { Authorization: "Bearer " + ctx.env.MATTERMOST_TOKEN },
            signal: AbortSignal.timeout(3e4)
          }
        );
        return response.ok && digest(Buffer.from(await response.arrayBuffer())) === ctx.metadata.attachment_sha256;
      }
    ),
    criterion(
      "the actor acknowledges the actual operations checkpoint with a check-mark reaction",
      async () => {
        const reactions = await api(
          ctx.env,
          "GET",
          "/posts/" + ctx.metadata.root_id + "/reactions"
        );
        return reactions.some(
          (r) => r.user_id === ctx.metadata.actor_id && r.emoji_name === "white_check_mark"
        );
      }
    )
  ]);
}
async function wordState(ctx) {
  const lines = (await docx(resolve33(ctx.work, "delivery.docx"))).numberedText.map(canonicalText).filter(Boolean), begin = lines.indexOf(active2), end = lines.indexOf(history2);
  if (begin !== 1 || end <= begin || lines.filter((v) => v === active2).length !== 1 || lines.filter((v) => v === history2).length !== 1)
    throw Error("The existing current/archive register structure was lost");
  return {
    lines,
    begin,
    end,
    current: splitServiceCurrent(lines.slice(begin + 1, end).join(" "))
  };
}
async function verifyWord(ctx, kind, evidence) {
  const m = ctx.metadata.service_register;
  return Promise.all([
    criterion(
      "the signed source approval and correction notes remain byte-for-byte unchanged",
      async () => digest(await readFile19(resolve33(ctx.work, "review-notes.docx"))) === m.source_sha256
    ),
    criterion(
      "the existing Word register contains the source-derived current facts with no stale approvals",
      async () => {
        const current2 = (await wordState(ctx)).current;
        return kind === "meeting" ? serviceReleaseFactsMatch(current2.facts) : serviceMonitoringFactsMatch(current2.facts, await evidence());
      }
    ),
    criterion(
      "Word contains exactly three deduplicated actions in the required dependency order",
      async () => serviceActionsMatch((await wordState(ctx)).current.actions, kind)
    ),
    criterion(
      "the register retains its original title, signed R1 archive and internal footer",
      async () => {
        const { lines, end } = await wordState(ctx);
        return lines[0] === title2 && lines.at(-1) === registerFooter && isDeepStrictEqual13(lines.slice(end + 1, -1), m.archive);
      }
    ),
    criterion(
      "the exported Word PDF has the same current facts, ordered actions and protected historical record",
      async () => {
        const state = await wordState(ctx), text = canonicalText(
          await unwrappedPdfText(resolve33(ctx.work, "delivery.pdf"))
        ), begin = text.indexOf(active2), end = text.indexOf(history2);
        if (begin < 0 || end <= begin || !state.lines.every((line) => text.includes(line)))
          return false;
        const current2 = splitServiceCurrent(
          text.slice(begin + active2.length, end)
        );
        return serviceActionsMatch(current2.actions, kind) && (kind === "meeting" ? serviceReleaseFactsMatch(current2.facts) : serviceMonitoringFactsMatch(current2.facts, await evidence()));
      }
    )
  ]);
}
var shapeIds = (raw) => [...raw.matchAll(/<p:cNvPr\b[^>]*\bid="([^"]+)"/g)].map((m) => m[1]);
async function setDeckText(c, index, heading, body) {
  await call(c, "go_to_slide", { index });
  const slide = await call(c, "inspect_slide", { index });
  await call(c, "set_text", { id: slide.shapes[0].id, text: heading });
  await call(c, "set_text", { id: slide.shapes[1].id, text: body });
}
async function addDeckSlide(c, heading, body) {
  await call(c, "add_slide", { layout: "blank" });
  for (const p of [
    { x: 60, y: 40, w: 840, h: 90, text: heading, size: 36, color: "#1F4E78" },
    { x: 60, y: 145, w: 840, h: 335, text: body, size: 19, color: "#333333" },
    { x: 40, y: 500, w: 800, h: 24, text: footer4, size: 12, color: "#777777" }
  ])
    await call(c, "add_text_box", p);
}
async function prepareReview(kind, work, env) {
  const base = await releaseHandoff.prepare(work, env);
  const notes3 = kind === "monitoring" ? [
    "Monitoring correction / signed R3",
    "R1 cached estimate is superseded: latency values 1,2,3 seconds, 5% red error limit and immediate clearance were placeholders. Repair the same production panel IDs42/23 without replacing their identities. A newer Release approvals panel is authoritative; preserve it and all other panels. Keep the archive dashboard and cached-dashboard.json unchanged. Approved data source remains the existing metrics datasource; use csv_metric_values queries with the following reviewed samples, read actual returned frames after repair, and assess the six-hour PEAK rather than latest p95.",
    "Approved latency samples in milliseconds: " + approvedMonitoringSamples.latency.join(", "),
    "Approved error samples in percent: " + approvedMonitoringSamples.errors.join(", "),
    "Approved absolute red thresholds: p95 250 ms; error rate 2%. Production range now-6h to now; refresh 15s. The error stat displays lastNotNull. Do not force overwrite from a cached dashboard version.",
    "R2 immediate pilot clearance is retracted. Calibration is the only outstanding mechanical QA blocker, but monitoring review is a separate pending gate. Morgan owns calibration and reviews the latency breach, due 23 October 2026 at 18:00 UTC. Release only after latency review AND calibration sign-off. Keep the original QA-summary.txt attached to the existing handoff, with its post/file IDs.",
    "Required register evidence and ordered actions:",
    ...monitoringFacts,
    ...monitoringActions
  ] : [
    "Release meeting / signed R3 approval log",
    "R1 / superseded: fit20/24; drawings10/12; owner Alex; ship immediately.",
    "QA-A / final: 10 of 10 fit checks passed; 5 of 5 drawings approved.",
    "QA-B / final: 14 of 14 fit checks passed; 7 of 7 drawings approved.",
    "Forwarded duplicate QA-A: 10 of 10 fit checks passed; 5 of 5 drawings approved. Same record, count once.",
    "16:05 expedited R2 / provisional: calibration waived, Alex, unconditional ship. RETRACTED at 16:20; never an approval.",
    "R3 / Operations approval: Morgan, 23 October 2026 at 18:00 UTC. Pilot approval remains conditional on calibration sign-off. Training follows customer approval. The operations checkpoint is the destination even though a newer expedited draft thread exists.",
    "Action notes A: Publish QA results. Send the pilot pack after calibration sign-off.",
    "Action notes B repeats A: Publish QA results. Confirm training after customer approval. Merge each approved action exactly once in dependency order.",
    "Keep the signed R1 Word archive, introduction, Release 5.3 archive and final R3 Deck approval unchanged. Retire Obsolete agenda. Keep and update the existing current slide, duplicate it as Decision and dependencies immediately before the archive, and export all five final slides."
  ];
  const final = kind === "monitoring" ? [...monitoringFacts, ...monitoringActions] : [...meetingFacts, ...meetingActions];
  const register = await prepareRegister(
    work,
    notes3,
    kind === "monitoring" ? monitoringDraft : meetingDraft,
    final
  );
  const metadata = {
    ...base,
    review_kind: kind,
    service_register: register
  };
  if (kind === "meeting") {
    await currentAndArchive.prepare(work, env);
    await withApplication("deckcraft", work, async (c) => {
      await call(c, "open_presentation", {
        path: resolve33(work, "briefing.pptx")
      });
      await setDeckText(
        c,
        1,
        "Validation summary",
        meetingDraft.slice(0, 7).join("\n")
      );
      await call(c, "go_to_slide", { index: 1 });
      await addDeckSlide(
        c,
        "Obsolete agenda",
        "R2 provisional: waive calibration and ship immediately. Retracted; retire this agenda."
      );
      await call(c, "go_to_slide", { index: 3 });
      await addDeckSlide(
        c,
        "Release meeting approval / R3",
        approvalText.split("\n").slice(1).join("\n")
      );
      await call(c, "save_presentation", {
        path: resolve33(work, "briefing.pptx")
      });
    });
    const initial = await slides(resolve33(work, "briefing.pptx"));
    metadata.initial_slides = initial.map((s2) => texts(s2.raw));
    metadata.initial_shape_ids = initial.map((s2) => shapeIds(s2.raw));
    if (initial.length !== 5 || !metadata.initial_slides[3].includes("Release 5.3 archive") || metadata.initial_slides[4][0] !== "Release meeting approval / R3")
      throw Error("Unexpected meeting source slide order");
  }
  return metadata;
}
async function verifyDeck(ctx) {
  const m = ctx.metadata, deck = slides(resolve33(ctx.work, "briefing.pptx"));
  const body = async (i) => texts((await deck)[i].raw).slice(1, -1).join(" ");
  return Promise.all([
    criterion(
      "the five slides retain introduction/current/decision/archive/R3 order and retire the obsolete agenda",
      async () => {
        const s2 = (await deck).map((s3) => texts(s3.raw));
        return s2.length === 5 && s2[1][0] === "Validation summary" && s2[2][0] === "Decision and dependencies" && !s2.some((v) => v[0] === "Obsolete agenda") && [0, 3, 4].every((i) => isDeepStrictEqual13(s2[i], m.initial_slides[i]));
      }
    ),
    criterion(
      "the original current slide retains its shape identities and approved source-derived QA and pending decision",
      async () => isDeepStrictEqual13(
        shapeIds((await deck)[1].raw),
        m.initial_shape_ids[1]
      ) && serviceReleaseFactsMatch(await body(1))
    ),
    criterion(
      "the decision slide contains exactly the three deduplicated actions with both pending dependencies",
      async () => serviceActionsMatch(await body(2), "meeting")
    ),
    criterion(
      "the introduction, signed archive and R3 approval are preserved without amendments",
      async () => {
        const s2 = await deck;
        return [0, 3, 4].every(
          (i) => isDeepStrictEqual13(texts(s2[i].raw), m.initial_slides[i])
        );
      }
    ),
    criterion(
      "every slide retains its internal footer and dark-blue 36 pt title branding",
      async () => (await deck).length === 5 && (await deck).every(
        (s2) => /sz="3600"/.test(s2.raw) && /srgbClr val="1F4E78"/i.test(s2.raw) && texts(s2.raw).at(-1) === footer4
      )
    ),
    criterion(
      "the five-page Deck PDF follows the actual native page order with every native fact",
      async () => {
        const pages = (await unwrappedPdfText(resolve33(ctx.work, "briefing.pdf"))).split("\f").map(canonicalText).filter(Boolean);
        return pages.length === 5 && (await deck).every(
          (s2, i) => texts(s2.raw).every((v) => pages[i].includes(canonicalText(v)))
        );
      }
    ),
    criterion(
      "Word, Deck and handoff agree on final QA totals, owner and pending approval dependencies",
      async () => {
        const current2 = (await wordState(ctx)).current, posts3 = (await channelPosts(ctx)).filter(
          (p) => p.user_id === ctx.metadata.actor_id && !p.type
        );
        return serviceReleaseFactsMatch(current2.facts) && serviceReleaseFactsMatch(await body(1)) && serviceActionsMatch(current2.actions, "meeting") && serviceActionsMatch(await body(2), "meeting") && posts3.length === 1 && serviceHandoffMatch(posts3[0].message, "meeting");
      }
    )
  ]);
}
var sharedGuidance = "Use only the assigned native application MCP tools. Read source notes in WordCraft, inspect actual service records and IDs, edit existing native documents and the existing own handoff, preserve all source/history exceptions, and export through the applications. No direct HTTP, generic file APIs, scripts or fabricated delivery files.";
var monitoringCorrection = {
  id: monitoringCorrectionId,
  title: "Repair a production monitoring correction and reconcile the approval-gated evidence pack",
  application: "mattermost",
  applications: ["grafana", "mattermost", "wordcraft"],
  views: [
    { application: "grafana", contextKey: "grafana" },
    { application: "mattermost" },
    { application: "wordcraft", inputFile: "delivery.docx" }
  ],
  inputFile: void 0,
  family: "daily-long-review",
  split: "development",
  mcpMode: "live",
  deadlineSeconds: 2700,
  outputs: ["delivery.docx", "delivery.pdf"],
  brief: "A monitoring review was rejected because a cached dashboard and an immediate-clearance handoff used obsolete data. Read signed R3 in review-notes.docx and the actual operations discussion in #${CHANNEL_NAME}. Repair the LIVE production dashboard __DASHBOARD_TITLE__ from its current server version. Keep its existing latency panel ID 42 and error panel ID 23; preserve all other panels, including Incident notes ID 4 and the newer Release approvals ID 19, and the archive dashboard. Apply the approved csv_metric_values samples and absolute red thresholds from the source, preserve the existing datasource UID, set milliseconds/percent, use a six-hour window and 15s refresh, and display the error stat with lastNotNull. Do not force a stale cached-dashboard.json over newer approvals, and leave that cached source file unchanged. After saving, query the actual repaired latency and error frames; report latest p95, six-hour PEAK p95, latest error rate, both limits and the correct peak-based assessment. The pilot is held for latency review AND calibration sign-off, even if the latest latency is below its limit. Edit your one existing handoff in the original operations checkpoint in place, retain its reply ID and QA-summary.txt file IDs/bytes, and acknowledge the root with white_check_mark. Distinguish the only mechanical QA blocker from the separate monitoring clearance gate. Correct all existing Current delivery fields in delivery.docx from the source and actual frames; keep the title, signed R1 archive, source notes and footer unchanged. Use exactly three deduplicated actions in this order: publish QA results; Morgan reviews the latency breach; send the pilot pack only after latency review and calibration sign-off. Include the DOCX/PDF filenames in Files. Save the native register and export delivery.pdf. Compare the actual dashboard, same handoff, native register and final PDF before finishing; no pending review is complete.",
  mcpGuidance: sharedGuidance,
  prepare: (work, env) => prepareReview("monitoring", work, env),
  prepareTrial: (work, env, metadata) => prepareServiceReviewTrial(monitoringCorrectionId, work, env, metadata),
  async verify(ctx) {
    const graph = ctx.metadata.grafana, read2 = () => grafanaAPI(
      ctx.env,
      "GET",
      "/api/dashboards/uid/" + graph.dashboard_uid
    ), evidence = async () => evidenceFromDashboard(
      (await read2()).dashboard,
      (panel) => grafanaAPI(ctx.env, "POST", "/api/ds/query", {
        queries: panel.targets,
        from: "now-6h",
        to: "now"
      })
    );
    return [
      ...await Promise.all([
        criterion(
          "the actual production dashboard repairs existing query/panel identities and merges the newer approved version",
          async () => serviceDashboardRepairMatch(
            (await read2()).dashboard,
            graph.initial_dashboard,
            String(graph.data_uid)
          )
        ),
        criterion(
          "the native repaired queries return the approved samples and a real peak breach despite a lower latest sample",
          async () => {
            const d = (await read2()).dashboard;
            const values = await Promise.all(
              [42, 23].map(
                async (id) => serviceFrameValues(
                  await grafanaAPI(ctx.env, "POST", "/api/ds/query", {
                    queries: d.panels.find((p) => p.id === id).targets,
                    from: "now-6h",
                    to: "now"
                  })
                )
              )
            );
            const e = await evidence();
            return values.every(
              (v, i) => v.length === [
                approvedMonitoringSamples.latency,
                approvedMonitoringSamples.errors
              ][i].length && v.every(
                (n, j) => Math.abs(
                  n - [
                    approvedMonitoringSamples.latency,
                    approvedMonitoringSamples.errors
                  ][i][j]
                ) <= 1e-7
              )
            ) && e.peak > e.latencyLimit && e.latest < e.latencyLimit && e.errors < e.errorLimit;
          }
        ),
        criterion(
          "the original archive dashboard and cached pre-approval snapshot remain unchanged",
          async () => {
            const archived = await grafanaAPI(
              ctx.env,
              "GET",
              "/api/dashboards/uid/" + graph.archive_uid
            );
            return isDeepStrictEqual13(archived.dashboard, graph.archive) && digest(
              await readFile19(resolve33(ctx.work, "cached-dashboard.json"))
            ) === ctx.metadata.cached_dashboard_sha256;
          }
        ),
        criterion(
          "the handoff and Word register use the same actual frames and keep both pilot gates pending",
          async () => {
            const e = await evidence(), posts3 = (await channelPosts(ctx)).filter(
              (p) => p.user_id === ctx.metadata.actor_id && !p.type
            );
            return posts3.length === 1 && serviceHandoffMatch(posts3[0].message, "monitoring", e) && serviceMonitoringFactsMatch(
              (await wordState(ctx)).current.facts,
              e
            );
          }
        )
      ]),
      ...await verifyHandoff(ctx, "monitoring", evidence),
      ...await verifyWord(ctx, "monitoring", evidence)
    ];
  }
};
var releaseMeeting = {
  id: releaseMeetingId,
  title: "Reconcile a retracted release proposal into an existing handoff, action register and meeting deck",
  application: "mattermost",
  applications: ["mattermost", "wordcraft", "deckcraft"],
  views: [
    { application: "mattermost" },
    { application: "wordcraft", inputFile: "delivery.docx" },
    { application: "deckcraft", inputFile: "briefing.pptx" }
  ],
  family: "daily-long-review",
  split: "held-out",
  mcpMode: "live",
  deadlineSeconds: 2700,
  outputs: ["delivery.docx", "delivery.pdf", "briefing.pptx", "briefing.pdf"],
  brief: "Prepare the actual Reef 5.4 release approval meeting pack. Read #${CHANNEL_NAME}, including the newer expedited draft thread and its retraction, the confirmed engineering correction in the original operations checkpoint, review-notes.docx and the final R3 approval slide in briefing.pptx. The newer expedited R2 proposal was withdrawn; it is not authority to waive calibration. Edit your EXISTING attached handoff in the original operations checkpoint in place: preserve its post ID and original QA-summary.txt attachment IDs/bytes, publish no extra posts or attachments, and acknowledge the true root with white_check_mark. State the corrected passing QA total, drawing approvals, Morgan's full confirmed deadline, conditional pilot decision and both pending dependencies. Derive unique fit/drawing totals from QA-A and QA-B in the source; the forwarded QA-A is a duplicate, and tentative/retracted estimates contribute nothing. Update the existing Current delivery in delivery.docx with the final facts, then merge the repeated action notes into exactly three ordered actions: publish QA results; send the pilot pack only after calibration sign-off; confirm training only after customer approval. Preserve the original title, signed R1 historical archive and footer; do not change review-notes.docx. In briefing.pptx keep and update only the current Release 5.4 Validation summary, including fit/drawing totals, owner, full deadline, conditional decision and pending gates. Retire Obsolete agenda. Duplicate the existing current slide as Decision and dependencies immediately before the signed archive, with the same three ordered actions. Keep the original introduction, Release 5.3 archive (87%, Alex) and final R3 approval unchanged. Final five-slide order: introduction, current Validation summary, Decision and dependencies, archive, R3 approval. Preserve 36 pt dark-blue titles and every internal footer. Save both existing native files and export delivery.pdf and all five slides to briefing.pdf. Compare all four deliveries with the one actual handoff; do not convert pending calibration/customer approval into completed clearance.",
  mcpGuidance: sharedGuidance,
  prepare: (work, env) => prepareReview("meeting", work, env),
  prepareTrial: (work, env, metadata) => prepareServiceReviewTrial(releaseMeetingId, work, env, metadata),
  async verify(ctx) {
    return [
      ...await verifyHandoff(ctx, "meeting"),
      ...await verifyWord(ctx, "meeting"),
      ...await verifyDeck(ctx)
    ];
  }
};
var serviceReviewFlows = [
  monitoringCorrection,
  releaseMeeting
];
var reviewWorkflows = [
  ...visualReviewFlows,
  ...serviceReviewFlows,
  ...musicWorkflows
];
import { resolve as resolve34 } from "node:path";
var footer5 = "Internal use only - Reef partner operations.";
var profiles2 = [
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
function make2(p) {
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
        footer5
      ];
      await withApplication("wordcraft", work, async (c) => {
        await call(c, "new_document", { template: "blank" });
        await call(c, "type_text", {
          text: original3.join("\n"),
          paragraphs: true
        });
        await call(c, "save_document", {
          path: resolve34(work, "amendment.docx")
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
      const read2 = docx(resolve34(context.work, "amendment.docx")).then((d) => ({
        ...d,
        text: d.numberedText.map(canonicalText)
      }));
      const active4 = async () => {
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
          async () => JSON.stringify(await active4()) === JSON.stringify(p.final)
        ),
        criterion(
          "the historical archive remains unchanged and in order",
          async () => {
            const d = await read2, a = d.text.indexOf("Historical archive"), b = d.text.indexOf(footer5);
            return a >= 0 && b > a && JSON.stringify(d.text.slice(a + 1, b).filter(Boolean)) === JSON.stringify(p.archive);
          }
        ),
        criterion(
          "section order and the final internal-use footer are preserved",
          async () => {
            const d = (await read2).text.filter(Boolean);
            return d[0] === p.title && d.indexOf(p.heading) > 0 && d.indexOf("Historical archive") > d.indexOf(p.heading) && d.at(-1) === footer5;
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
              await pdfText(resolve34(context.work, "amendment.pdf"))
            );
            return [
              p.title,
              p.heading,
              ...p.final,
              "Historical archive",
              ...p.archive,
              footer5
            ].every((t) => text.includes(t));
          }
        )
      ]);
    }
  };
}
var scopedAmendment = make2(profiles2[0]);
var actionRegister = make2(profiles2[1]);
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
          const posts3 = await actorPosts(context);
          return posts3.length === 1 && posts3[0].id === context.metadata.old_reply_id && JSON.stringify(posts3[0].file_ids) === JSON.stringify(context.metadata.old_file_ids);
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
import { resolve as resolve35 } from "node:path";
import { unlink as unlink8 } from "node:fs/promises";
async function removeBackup(work) {
  await unlink8(resolve35(work, "product-hero.blend1")).catch(
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
    const metadata = await productHero.prepare(work, env), path = resolve35(work, "product-hero.blend");
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
import { writeFile as writeFile13 } from "node:fs/promises";
import { resolve as resolve37 } from "node:path";
import { writeFile as writeFile12 } from "node:fs/promises";
import { resolve as resolve36 } from "node:path";
import { isDeepStrictEqual as isDeepStrictEqual14 } from "node:util";
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
  for (const [key3, fallback] of Object.entries(defaults))
    result[key3] = withDefaults(result[key3], fallback);
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
    await writeFile12(
      resolve36(work, "pilot-monitoring-brief.txt"),
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
            (s2) => s2.value === 2 && s2.color === "red"
          ) && !f.thresholds.steps.some((s2) => s2.value === 5);
        }
      ),
      criterion("the original error-rate query is preserved", async () => {
        const p = (await read2).panels.find(
          (p2) => p2.id === (m.error_panel_id ?? 2)
        ), old = m.original_panels.find(
          (p2) => p2.id === (m.error_panel_id ?? 2)
        );
        return !!p && !!old && isDeepStrictEqual14(panelQueries(p), panelQueries(old));
      }),
      criterion(
        "request-rate settings and runbook content are unchanged",
        async () => {
          const d = await read2;
          return m.original_panels.filter(
            (p) => p.id !== (m.error_panel_id ?? 2) && p.id !== m.latency_panel_id
          ).every((old) => {
            const p = d.panels.find((p2) => p2.id === old.id);
            return p && isDeepStrictEqual14(semanticPanel(p), semanticPanel(old));
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
            (s2) => s2.value === 250 && s2.color === "red"
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
          const notes3 = await grafanaAPI(
            env,
            "GET",
            "/api/annotations?tags=" + m.dashboard_uid + "&from=" + (Number(m.deploy_time_ms) - 1e3) + "&to=" + (Number(m.deploy_time_ms) + 1e3) + "&limit=100"
          );
          const found = notes3.filter(
            (n) => n.text === "Reef 5.4 pilot deployed" && n.tags.includes("reef-5.4")
          );
          return found.length === 1 && sameSecond(found[0].time, Number(m.deploy_time_ms)) && found[0].tags.includes("deployment") && (found[0].dashboardUID === m.dashboard_uid || found[0].dashboardId === (await read2).id);
        }
      ),
      criterion(
        "the similarly named archive dashboard remains unchanged",
        async () => isDeepStrictEqual14(
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
    await writeFile13(
      resolve37(work, "snapshot-warning.txt"),
      "The cached dashboard predates Morgan's release approval. Refresh before saving; preserve the newer Release approvals panel.\n"
    );
    return { ...meta, fixture_profile: "stale-snapshot" };
  },
  prepareTrial: async (_work, env) => seedPilotDashboard(env, "stale-snapshot")
};
import { resolve as resolve38 } from "node:path";
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
        path: resolve38(work, "weekly-report.docx")
      });
    });
    return { original: original2, required };
  },
  async verify(context) {
    const path = resolve38(context.work, "weekly-report.docx");
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
          const positions2 = required.slice(5).map((text) => d.text.indexOf(text));
          return positions2.every(
            (p, i) => p >= 0 && (i === 0 || p > positions2[i - 1])
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
          await pdfText(resolve38(context.work, "weekly-report.pdf"))
        );
        return [original2[0], ...required, original2.at(-1)].every(
          (s2) => text.includes(s2)
        );
      })
    ]);
  }
};
import { copyFile as copyFile9, readFile as readFile20 } from "node:fs/promises";
import { resolve as resolve39 } from "node:path";
import { createHash as createHash5 } from "node:crypto";
import { isDeepStrictEqual as isDeepStrictEqual15 } from "node:util";
var receipt2 = "delivery.docx";
var source2 = "review-notes.docx";
var title3 = "Reef delivery register";
var active3 = "Current delivery";
var archiveHeading2 = "Historical archive";
var footer6 = "Internal - retain the signed historical record.";
var facts2 = {
  briefing: [
    "Release: Reef 5.4",
    "Fit checks: 24 / 24 passed",
    "Drawing approvals: 12 / 12 complete",
    "Owner: Morgan",
    "Deadline: 23 October 2026, 18:00 UTC",
    "Gate: calibration sign-off before release",
    "1. Publish QA results.",
    "2. Send the pilot pack after calibration sign-off.",
    "3. Confirm training after customer approval."
  ],
  campaign: [
    "Revision: approved R3",
    "Web date: 24 October 2026",
    "Web size: 800 x 400 px",
    "Web files: studio-web.svg; studio-web.png",
    "Print: studio-proof.pdf; 480 x 640 pt; approved R1 unchanged",
    "Owner: Leah",
    "Deadline: 23 October 2026, 18:00 UTC",
    "Gate: Leah checks the revised web date before publishing"
  ],
  video: [
    "Revision: approved R2 picture edit",
    "Picture order: Arrival; Discovery; Journey",
    "Duration: 8.5 seconds",
    "Format: H.264; 320 x 180; 12 fps; stereo 48 kHz",
    "Captions: 3 cues; burned in; studio-final.srt",
    "Poster: studio-final.png; program frame at 2 seconds",
    "Source: Sintel; Blender Foundation; CC BY 3.0; ATTRIBUTION.md",
    "Owner: Sam",
    "Deadline: 24 October 2026, 10:00 UTC",
    "Gate: Sam checks picture, audio, subtitles and poster before publishing"
  ]
};
var notes = {
  briefing: [
    "Reef 5.4 evidence and approval log",
    "R1 / 09:00 / superseded: 20 of 24 fit checks, 10 of 12 drawing approvals; owner Alex; ship 22 October without waiting.",
    "QA record QA-A / 14:00 / final: 10 of 10 fit checks passed; 5 of 5 drawings approved.",
    "QA record QA-B / 14:10 / final: 14 of 14 fit checks passed; 7 of 7 drawings approved.",
    "Forwarded duplicate of QA-A: 10 of 10 fit checks passed; 5 of 5 drawings approved. This is the same record, not an additional batch.",
    "R2 / 15:00 / APPROVED: use the final QA records, count each record once. Morgan owns Reef 5.4. Deadline 23 October 2026, 18:00 UTC. Calibration sign-off is required before release.",
    "Approved actions, in order: publish QA results; send the pilot pack after calibration sign-off; confirm training after customer approval.",
    "Delivery: reconcile delivery.docx and the CURRENT slide in briefing.pptx. Insert a Next steps slide before the archive, with the three approved actions. Preserve the introduction, historical slide and all internal footers. PDF versions must contain the same final facts."
  ],
  campaign: [
    "Studio campaign review log",
    "R2 / superseded date: 23 October 2026. The VectorCraft review note still records this earlier approval.",
    "R3 / 16:30 / APPROVED by Leah: the web event date is now 24 October 2026. This date overrides R2 in the artwork. The web canvas remains 800 x 400; extend only the existing dark background and preserve positions and editable type. Headline: Reef Open Studio.",
    "APPROVED PRINT / R1 is signed and locked: keep its 22 October date and 480 x 640 geometry. The print PDF must use that artboard, not the revised web artboard. Keep archive and sponsor artwork unchanged.",
    "Leah owns delivery. Deadline 23 October 2026, 18:00 UTC. Leah must check the revised web date before publishing.",
    "Reconcile delivery.docx with the actual final web SVG/PNG and print PDF. Retain the historical delivery record exactly. Save the editable artwork and replace stale exports."
  ],
  video: [
    "Studio video review and delivery log",
    "R1 / superseded delivery: 9 seconds; Arrival, Journey, Discovery; publishing cleared without a media check.",
    "R2 / APPROVED: apply the CURRENT sequence's review marker in studio-cut.fcproj. Keep the source media and locked archive. The order is Arrival, Discovery, Journey; the first shot is shortened by 6 frames and total duration is 8.5 seconds. Existing narration, music and captions must follow the revised edit without gaps or overlaps.",
    "Delivery must include H.264 320 x 180 at 12 fps, stereo 48 kHz audio, three burned captions, matching SRT, and the program frame at 2 seconds as PNG. Use software encoding. Credits: Sintel, Blender Foundation, CC BY 3.0; retain ATTRIBUTION.md. Original movie audio is excluded; the supplied synthetic narration/music is the only audio source.",
    "Sam owns delivery. Deadline 24 October 2026, 10:00 UTC. Sam checks picture, audio, subtitles and poster before publishing. Reconcile the current receipt with the actual native timeline and deliveries, preserving the historical record."
  ]
};
var draft2 = {
  briefing: [
    "Release: Reef 5.4",
    "Fit checks: 20 / 24 passed",
    "Drawing approvals: 10 / 12 complete",
    "Owner: Alex",
    "Deadline: 22 October 2026, 09:00 UTC",
    "Gate: release before calibration",
    "1. Ship now.",
    "2. Send duplicate QA totals.",
    "3. Confirm training immediately."
  ],
  campaign: [
    "Revision: draft R2",
    "Web date: 23 October 2026",
    "Web size: 640 x 360 px",
    "Web files: review-r1.png",
    "Print: web artwork, not checked",
    "Owner: Alex",
    "Deadline: 22 October 2026, 09:00 UTC",
    "Gate: publish immediately"
  ],
  video: [
    "Revision: draft R1",
    "Picture order: Arrival; Journey; Discovery",
    "Duration: 9 seconds",
    "Format: unchecked",
    "Captions: old draft; not checked",
    "Poster: old draft",
    "Source: credit pending",
    "Owner: Alex",
    "Deadline: 22 October 2026, 09:00 UTC",
    "Gate: publish immediately"
  ]
};
var sha = (b) => createHash5("sha256").update(b).digest("hex");
var compactFact = (value) => canonicalText(value).toLowerCase().replaceAll("\xD7", "x").replace(/\d+(?:\.\d+)?/g, (n) => String(Number(n))).replace(/(?<=\d)\.(?=\d)/g, "decimal").replace(/[^a-z0-9]/g, "");
function sameFact(kind, index, actual) {
  const expected = facts2[kind][index];
  if (compactFact(actual) === compactFact(expected)) return true;
  const label = expected.includes(":") ? expected.split(":")[0] + ":" : expected.slice(0, 2);
  if (!actual.toLowerCase().startsWith(label.toLowerCase())) return false;
  const body = actual.slice(label.length).toLowerCase();
  if (kind === "campaign" && label === "Print:") {
    const sizes = [
      ...body.matchAll(
        /(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(pt|points|px|mm)?\b/g
      )
    ];
    return body.includes("studio-proof.pdf") && sizes.length === 1 && Number(sizes[0][1]) === 480 && Number(sizes[0][2]) === 640 && ["pt", "points"].includes(sizes[0][3]) && /approved\s+(?:print\s*\/?\s*)?r1\b/.test(body) && !/\b(?:changed|modified|updated|revised|not|r2|r3)\b/.test(body);
  }
  if (label === "Revision:")
    return body.includes(kind === "campaign" ? "r3" : "r2") && /approv/.test(body) && !/draft|pending|not approved/.test(body);
  if (label === "Gate:") {
    if (kind === "briefing") return pendingCalibrationGate(body);
    const owner = kind === "campaign" ? "leah" : "sam";
    const check = new RegExp(
      owner + ".*(?:check|review|verify).*(?:before.*publish|prior to.*publish)"
    ).test(body) || new RegExp(
      "publish.*(?:after|once).*" + owner + ".*(?:check|review|verif)"
    ).test(body);
    return check && (kind === "campaign" ? /web.*date/.test(body) : /picture|video/.test(body) && /audio|sound/.test(body) && /subtitle|caption/.test(body) && /poster/.test(body));
  }
  if (kind === "briefing" && index >= 6) {
    const expressions = [
      /publish.*qa.*results/,
      /send.*pilot.*pack.*after.*calibration.*sign.?off/,
      /confirm.*training.*after.*customer.*approval/
    ];
    return expressions[index - 6].test(body);
  }
  return false;
}
async function createWord(c, path, paragraphs2) {
  await call(c, "new_document", { template: "blank" });
  await call(c, "type_text", { text: paragraphs2.join("\n"), paragraphs: true });
  await call(c, "select_text", { text: paragraphs2[0] });
  await call(c, "execute", {
    command: "format.set",
    params: { props: { bold: true, size: 18, color: "1F4E78" } }
  });
  await call(c, "save_document", { path });
}
async function prepare3(kind, work) {
  const application_metadata = await (kind === "briefing" ? currentAndArchive : kind === "campaign" ? vectorFormats : filmPicture).prepare(work, {});
  if (kind === "campaign") {
    application_metadata.approved_date = "24 October 2026";
    await copyFile9(
      resolve39(work, "review-r1.png"),
      resolve39(work, "studio-web.png")
    );
  }
  const archive2 = [
    "Signed R1 / 22 October 2026",
    "Owner: Alex",
    "Historical status: 9 seconds; date 22 October; no further amendments."
  ];
  await withApplication("wordcraft", work, async (c) => {
    await createWord(c, resolve39(work, source2), notes[kind]);
    await createWord(c, resolve39(work, receipt2), [
      title3,
      active3,
      ...draft2[kind],
      archiveHeading2,
      ...archive2,
      footer6
    ]);
  });
  const extra = kind === "briefing" ? {
    original_slides: (await slides(resolve39(work, "briefing.pptx"))).map(
      (s2) => texts(s2.raw)
    )
  } : {};
  return {
    kind,
    application_metadata,
    source_sha256: sha(await readFile20(resolve39(work, source2))),
    draft: draft2[kind],
    final: facts2[kind],
    archive: archive2,
    ...extra
  };
}
function applicationContext(ctx) {
  return {
    ...ctx,
    metadata: ctx.metadata.application_metadata
  };
}
async function verifyReceipt(ctx) {
  const m = ctx.metadata;
  const read2 = () => docx(resolve39(ctx.work, receipt2));
  return Promise.all([
    criterion(
      "the source approval log is preserved byte for byte",
      async () => sha(await readFile20(resolve39(ctx.work, source2))) === m.source_sha256
    ),
    criterion(
      "the current receipt contains every reconciled fact once and in the existing field order",
      async () => {
        const t = (await read2()).numberedText.map(canonicalText).filter(Boolean), start = t.indexOf(active3), end = t.indexOf(archiveHeading2);
        const fields = t.slice(start + 1, end);
        return start === 1 && end > start && fields.length === m.final.length && fields.every((text, i) => sameFact(m.kind, i, text));
      }
    ),
    criterion(
      "the original receipt title, historical record and final footer are preserved",
      async () => {
        const t = (await read2()).numberedText.map(canonicalText).filter(Boolean);
        return t[0] === title3 && t.at(-1) === footer6 && isDeepStrictEqual15(
          t.slice(t.indexOf(archiveHeading2) + 1, -1),
          m.archive
        );
      }
    ),
    criterion(
      "the saved receipt PDF contains the final fields and the preserved historical record",
      async () => {
        const t = canonicalText(
          await pdfText(resolve39(ctx.work, "delivery.pdf"))
        );
        const paragraphs2 = (await read2()).numberedText.map(canonicalText).filter(Boolean);
        const fields = paragraphs2.slice(
          paragraphs2.indexOf(active3) + 1,
          paragraphs2.indexOf(archiveHeading2)
        );
        const pdfActive = t.slice(
          t.indexOf(active3) + active3.length,
          t.indexOf(archiveHeading2)
        );
        return paragraphs2.every((v) => t.includes(v)) && fields.every((v) => pdfActive.includes(v));
      }
    )
  ]);
}
var deckCurrent2 = ["Release 5.4 current", ...facts2.briefing.slice(1, 6)].join(
  "\n"
);
var deckActions2 = facts2.briefing.slice(6).join("\n");
function briefingFactsMatch(body) {
  return releaseFactsMatch(body);
}
async function verifyBriefing(ctx) {
  const original3 = ctx.metadata.original_slides;
  const read2 = () => slides(resolve39(ctx.work, "briefing.pptx"));
  return Promise.all([
    criterion(
      "four slides retain the introduction and archive with current results and next steps between them",
      async () => {
        const s2 = (await read2()).map((s3) => texts(s3.raw));
        return s2.length === 4 && isDeepStrictEqual15(s2[0], original3[0]) && isDeepStrictEqual15(s2[3], original3[2]) && s2[1][0] === "Validation summary" && s2[2][0] === "Next steps";
      }
    ),
    criterion(
      "the presentation reports deduplicated final QA totals, owner, deadline and release gate",
      async () => briefingFactsMatch(
        texts((await read2())[1].raw).slice(1, -1).join(" ")
      )
    ),
    criterion(
      "the three approved actions appear in order on the next-steps slide",
      async () => canonicalText(
        texts((await read2())[2].raw).slice(1, -1).join(" ")
      ) === canonicalText(deckActions2)
    ),
    criterion(
      "every presentation slide preserves its original title branding and internal footer",
      async () => (await read2()).every(
        (s2) => /sz="3600"/.test(s2.raw) && /srgbClr val="1F4E78"/i.test(s2.raw) && texts(s2.raw).at(-1) === "Reef partner operations | Internal"
      )
    ),
    criterion(
      "the presentation PDF has four pages and the same final current results and actions",
      async () => {
        const path = resolve39(ctx.work, "briefing.pdf"), pages = (await execute("pdftotext", ["-raw", path, "-"], { timeout: 15e3 })).stdout.replace(/(\S)-[ \t]*\r?\n[ \t]*(?=\S)/g, "$1-").split("\f").map(canonicalText).filter(Boolean);
        return /Pages:\s+4\b/.test(
          (await execute("pdfinfo", [path], { timeout: 15e3 })).stdout
        ) && pages.length === 4 && briefingFactsMatch(
          texts((await read2())[1].raw).slice(1, -1).join(" ")
        ) && (await read2()).every(
          (s2, index) => texts(s2.raw).every((t) => pages[index].includes(canonicalText(t)))
        );
      }
    ),
    criterion(
      "the receipt and presentation agree on the actual current QA totals, owner, deadline and gate",
      async () => {
        const deck = texts((await read2())[1].raw).slice(1, -1).join(" "), doc = (await docx(resolve39(ctx.work, receipt2))).numberedText.map(
          canonicalText
        );
        return briefingFactsMatch(deck) && facts2.briefing.slice(1, 6).every((_, i) => doc.some((p) => sameFact("briefing", i + 1, p)));
      }
    )
  ]);
}
function workflow5(kind, id, title4) {
  const app = kind === "briefing" ? "deckcraft" : kind === "campaign" ? "vectorcraft" : "filmcraft";
  const inputFile = kind === "briefing" ? "briefing.pptx" : kind === "campaign" ? "studio-campaign.vectorcraft" : filmProject;
  const applicationOutputs = kind === "briefing" ? [inputFile, "briefing.pdf"] : kind === "campaign" ? vectorFormats.outputs : filmPicture.outputs;
  return {
    id,
    title: title4,
    application: "wordcraft",
    applications: [app, "wordcraft"],
    family: "evidence-review-delivery",
    split: kind === "video" ? "held-out" : "development",
    deadlineSeconds: 2100,
    views: [
      { application: app, inputFile },
      { application: "wordcraft", inputFile: receipt2 }
    ],
    inputFile: receipt2,
    outputs: [...applicationOutputs, receipt2, "delivery.pdf"],
    brief: `Complete the existing ${kind} delivery bundle. Read review-notes.docx in WordCraft to identify final evidence, superseded instructions and approval conditions. Reconcile the existing Current delivery fields in delivery.docx using that evidence; retain the original field labels/order, Historical archive and footer. ${kind === "briefing" ? "Deduplicate the QA records before deriving totals. Revise the existing CURRENT results slide in briefing.pptx; preserve the introduction and historical archive, and insert a Next steps slide immediately before the archive with the three approved numbered actions. Preserve title branding and every internal footer. Deliver the four-slide briefing.pptx and briefing.pdf." : kind === "campaign" ? "Revise the existing studio-campaign.vectorcraft using both its review note and the newer R3 approval in WordCraft. R3 supersedes the web date in R2. Preserve all original editable objects and protected artboards. Deliver studio-web.svg with editable text and studio-web.png at one pixel per point from CURRENT WEB, plus studio-proof.pdf from the locked APPROVED PRINT artboard. Replace stale deliveries." : "Revise the existing studio-cut.fcproj using its APPROVED / R2 marker. Preserve existing sequence/clip/caption identities, source media and archive. Deliver the full current sequence as studio-final.mp4 (H.264, 320 x 180, 12 fps, stereo 48 kHz, software encoding, burned captions), studio-final.srt and studio-final.png (program frame at exactly 2 seconds). Preserve ATTRIBUTION.md."} Save all editable masters in place and deliver delivery.pdf with the same final receipt. Check the actual exported files against their native masters and reconcile facts across the bundle before finishing. Preserve review-notes.docx unchanged.`,
    mcpGuidance: `Use the assigned ${app}__ and wordcraft__ tools connected to their existing desktops. Read the approval log through WordCraft, inspect the existing native project and make scoped edits. Open the receipt again to reconcile its fields. Preserve locked/archive content, save native masters and export the required deliveries.`,
    prepare: (work) => prepare3(kind, work),
    prepareTrial: kind === "video" ? async (work, env, metadata) => {
      await filmPicture.prepareTrial(
        work,
        env,
        metadata.application_metadata
      );
      return {};
    } : void 0,
    async verify(ctx) {
      const own = await verifyReceipt(ctx);
      if (kind === "briefing") return [...own, ...await verifyBriefing(ctx)];
      const native3 = await (kind === "campaign" ? verifyVector : verifyFilm)(
        applicationContext(ctx)
      );
      const consistency = await criterion(
        "the receipt agrees with the saved editable project's actual approved delivery state",
        async () => {
          const lines = (await docx(resolve39(ctx.work, receipt2))).numberedText.map(canonicalText);
          const m = ctx.metadata.application_metadata;
          if (kind === "campaign") {
            const document2 = JSON.parse(
              await readFile20(resolve39(ctx.work, inputFile), "utf8")
            );
            const date = vectorText(
              vectorNodes(document2).find(
                (n) => n.id === m.ids["Web date"]
              )
            );
            const rect = document2.document.artboards[0].rect;
            const values = [
              "Web date: " + date,
              `Web size: ${rect.x1 - rect.x0} x ${rect.y1 - rect.y0} px`
            ];
            return values.every(
              (value) => lines.some((line) => compactFact(line) === compactFact(value))
            );
          }
          const document = JSON.parse(
            await readFile20(resolve39(ctx.work, filmProject), "utf8")
          ), q = filmSequence(document, Number(m.sequence));
          const clips = q.video_tracks[0].items.slice().sort((a, b) => a.start - b.start);
          const names3 = clips.map(
            (c) => document.project.items[String(c.item)].name.replace(/^shot-/, "").replace(/\.mp4$/, "")
          ).map((s2) => s2[0].toUpperCase() + s2.slice(1));
          const seconds2 = Math.max(...clips.map((c) => c.start + c.duration)) / FILM_TICKS;
          return [
            `Picture order: ${names3.join("; ")}`,
            `Duration: ${seconds2} seconds`
          ].every(
            (value) => lines.some((line) => compactFact(line) === compactFact(value))
          );
        }
      );
      return [...own, ...native3, consistency];
    }
  };
}
var evidenceBriefing = workflow5(
  "briefing",
  "evidence-to-briefing",
  "Reconcile duplicated QA evidence into a report and briefing with an approval gate"
);
var campaignDelivery = workflow5(
  "campaign",
  "campaign-review-delivery",
  "Apply a superseding campaign review and reconcile web, signed print and handoff receipt"
);
var videoDelivery = workflow5(
  "video",
  "video-review-delivery",
  "Revise a multi-clip edit and reconcile video, captions, poster, attribution and handoff receipt"
);
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
  cadToScene,
  vectorReview,
  vectorSponsors,
  vectorFormats,
  filmPicture,
  filmCaptions,
  filmAudio,
  evidenceBriefing,
  campaignDelivery,
  videoDelivery,
  ...longWorkflows,
  ...reviewWorkflows
];
import { resolve as resolve40 } from "node:path";
import { unlink as unlink9 } from "node:fs/promises";
async function probe(work, project2, exports) {
  return new NativeEngine("freecad").run(
    [
      "doc=App.openDocument(p['project']); doc.recompute()",
      "def shape_state(s):",
      "    if s.isNull(): return {'valid':False,'solids':0,'volume':0.0,'bounds':[0.0]*6,'shape_error':'NULL shape'}",
      "    b=s.BoundBox; return {'valid':s.isValid(),'solids':len(s.Solids),'volume':s.Volume,'bounds':[b.XMin,b.YMin,b.ZMin,b.XMax,b.YMax,b.ZMax]}",
      "result={'objects':{},'exports':{}}",
      "for o in doc.Objects:",
      "    entry={'type':o.TypeId,'label':o.Label,'dimensions':{k:float(getattr(o,k).Value) for k in ('Length','Width','Height','Radius') if hasattr(o,k)},'placement':list(o.Placement.Base),'rotation':list(o.Placement.Rotation.toEuler())}",
      "    if hasattr(o,'Shape') and not o.Shape.isNull(): entry.update(shape_state(o.Shape))",
      "    if hasattr(o,'Base') and o.Base: entry['base']=o.Base.Name",
      "    if hasattr(o,'Tool') and o.Tool: entry['tool']=o.Tool.Name",
      "    if o.TypeId=='Part::MultiFuse': entry['shapes']=[s.Name for s in o.Shapes]",
      "    result['objects'][o.Name]=entry",
      "import Mesh",
      "for name in p['exports']:",
      "    path=os.path.join(p['work'],name)",
      "    if not os.path.exists(path): result['exports'][name]=None",
      "    elif name.endswith('.stl'):",
      "        m=Mesh.Mesh(path); result['exports'][name]={'kind':'stl','solid':m.isSolid(),'volume':abs(m.Volume),'facets':m.CountFacets}",
      "    else: result['exports'][name]=dict(shape_state(Part.read(path)),kind='step')"
    ],
    { project: resolve40(work, project2), work, exports }
  );
}
function roles(state, labels2) {
  const byLabel = /* @__PURE__ */ new Map(), byId = /* @__PURE__ */ new Map();
  for (const label of labels2) {
    const matches2 = Object.entries(state.objects).filter(
      ([id, o]) => (o.label ?? id) === label
    );
    if (matches2.length > 1) throw Error("Ambiguous feature label: " + label);
    if (matches2.length === 1) {
      byLabel.set(label, matches2[0][0]);
      byId.set(matches2[0][0], label);
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
var dims = (o) => o.type === "Part::Box" ? [o.dimensions.Length, o.dimensions.Width, o.dimensions.Height] : [o.dimensions.Radius, o.dimensions.Height];
function matches(o, p) {
  return !!o && o.type === (p.kind === "box" ? "Part::Box" : "Part::Cylinder") && vectorClose(dims(o), p.size) && vectorClose(o.placement, p.at) && closeTo(o.rotation[0] ?? NaN, p.rotation ?? 0, 0.01) && closeTo(o.rotation[1] ?? NaN, 0, 0.01) && closeTo(o.rotation[2] ?? NaN, 0, 0.01);
}
function linked2(o, l) {
  return !!o && (l.operation === "fuse" && o.type === "Part::MultiFuse" ? o.shapes?.length === 2 && o.shapes[0] === l.base && o.shapes[1] === l.tool : o.type === (l.operation === "cut" ? "Part::Cut" : "Part::Fuse") && o.base === l.base && o.tool === l.tool);
}
function solidMatches(s2, volume2, bounds, tolerance = 0.05) {
  return !!s2?.valid && s2.solids === 1 && closeTo(s2.volume, volume2, tolerance) && vectorClose(s2.bounds, bounds, 0.01);
}
var isLink = (s2) => "operation" in s2;
async function build(c, path, steps) {
  for (const s2 of steps) {
    if (isLink(s2)) {
      await call(c, "boolean_operation", {
        path,
        operation: s2.operation,
        base_id: s2.base,
        tool_id: s2.tool,
        name: s2.name
      });
      continue;
    }
    const [a, b, h] = s2.size;
    await call(
      c,
      "add_primitive",
      s2.kind === "box" ? { path, kind: "box", name: s2.name, length: a, width: b, height: h } : { path, kind: "cylinder", name: s2.name, radius: a, height: b }
    );
    await call(c, "set_placement", {
      path,
      object_id: s2.name,
      x: s2.at[0],
      y: s2.at[1],
      z: s2.at[2],
      rotation_z: s2.rotation ?? 0
    });
  }
}
async function prepare4(s2, work) {
  const path = resolve40(work, s2.project);
  await withApplication("freecad", work, async (c) => {
    await call(c, "create_document", { path, label: s2.label, overwrite: true });
    await build(c, path, s2.seed);
  });
  await unlink9(path + "1").catch((error) => {
    if (error.code !== "ENOENT") throw error;
  });
  return {
    parts: s2.parts.map((p) => ({
      name: p.name,
      final: p.final,
      expected_volume_mm3: p.volume
    }))
  };
}
async function verify3(s2, ctx) {
  const files = s2.parts.flatMap((p) => p.files), state = probe(ctx.work, s2.project, files), labels2 = [...s2.added, ...s2.addedLinks].map((o) => o.name), original3 = [
    ...s2.seed.map((o) => o.name),
    ...s2.protectedObjects.map((o) => o.name)
  ].filter((n, i, all) => all.indexOf(n) === i);
  const named = async () => roles(await state, labels2).objects;
  const groups = /* @__PURE__ */ new Map();
  for (const p of s2.added) {
    const group = p.group ?? p.name.replace(/\d+$/, "");
    groups.set(group, [...groups.get(group) ?? [], p]);
  }
  return Promise.all([
    ...s2.revised.map(
      (r) => criterion(
        `${r.name} has the approved revision dimensions and placement`,
        async () => matches((await state).objects[r.name], r)
      )
    ),
    criterion("protected reference geometry is unchanged", async () => {
      const o = (await state).objects;
      return s2.protectedObjects.every((p) => matches(o[p.name], p));
    }),
    criterion(
      "the original parametric links and their operands are retained",
      async () => {
        const o = (await state).objects;
        return s2.originalLinks.every((l) => linked2(o[l.name], l));
      }
    ),
    ...[...groups].map(
      ([group, items]) => criterion(
        `new ${group} features have the specified geometry and placement`,
        async () => {
          const o = await named();
          return items.every((p) => matches(o[p.name], p));
        }
      )
    ),
    criterion(
      "new features retain the specified editable dependency chain",
      async () => {
        const o = await named();
        return s2.addedLinks.every((l) => linked2(o[l.name], l));
      }
    ),
    criterion(
      "there are no duplicate, flattened or unexpected feature objects",
      async () => {
        const raw = await state, r = roles(raw, labels2);
        return Object.keys(raw.objects).length === original3.length + labels2.length && original3.every((id) => raw.objects[id]) && r.ids.length === labels2.length && r.ids.every((id) => !original3.includes(id));
      }
    ),
    ...s2.parts.flatMap((part) => [
      criterion(
        `the final ${part.name} feature is one valid solid with the expected volume and bounds`,
        async () => solidMatches((await named())[part.final], part.volume, part.bounds)
      ),
      criterion(
        `${part.files[0]} reimports as only the final ${part.name}`,
        async () => {
          const e = (await state).exports[part.files[0]];
          return e?.kind === "step" && solidMatches(e, part.volume, part.bounds, 0.1);
        }
      ),
      criterion(
        `${part.files[1]} is closed and has the final ${part.name} volume`,
        async () => {
          const e = (await state).exports[part.files[1]];
          return e?.kind === "stl" && e.solid && e.facets > 0 && closeTo(e.volume, part.volume, part.volume * 2e-3);
        }
      )
    ])
  ]);
}
var box = (name, size, at, rotation) => ({ name, kind: "box", size, at, rotation });
var cylinder = (name, size, at) => ({
  name,
  kind: "cylinder",
  size,
  at
});
var chain = (operation, start, tools, names3) => tools.map((tool2, i) => ({
  name: names3[i],
  operation,
  base: i ? names3[i - 1] : start,
  tool: tool2
}));
var corners = (x0, x1, y0, y1) => [
  [x0, y0],
  [x1, y0],
  [x0, y1],
  [x1, y1]
];
var numbered = (prefix, count, last) => Array.from(
  { length: count },
  (_, i) => i === count - 1 && last ? last : prefix + (i + 1)
);
var lidHoles = corners(6, 94, 6, 54).map(
  ([x, y], i) => cylinder("ScrewHole" + (i + 1), [1.5, 7], [x, y, -2])
);
var lidGasket = box("GasketReference", [96, 56, 1], [2, 2, 3]);
var vents = [26, 32, 38, 44, 50].map(
  (y, i) => box("Vent" + (i + 1), [40, 3, 7], [40, y, -2])
);
var lidSpec = {
  project: "sensor-lid.FCStd",
  label: "Reef sensor lid",
  seed: [
    box("LidStock", [100, 60, 3], [0, 0, 0]),
    ...lidHoles,
    ...chain(
      "cut",
      "LidStock",
      lidHoles.map((h) => h.name),
      ["Drilled1", "Drilled2", "Drilled3", "Lid"]
    ),
    lidGasket
  ],
  revised: [
    box("LidStock", [120, 80, 3], [0, 0, 0]),
    ...corners(7, 113, 7, 73).map(
      ([x, y], i) => cylinder("ScrewHole" + (i + 1), [1.75, 7], [x, y, -2])
    )
  ],
  protectedObjects: [lidGasket],
  originalLinks: chain(
    "cut",
    "LidStock",
    lidHoles.map((h) => h.name),
    ["Drilled1", "Drilled2", "Drilled3", "Lid"]
  ),
  added: vents,
  addedLinks: chain(
    "cut",
    "Lid",
    vents.map((v) => v.name),
    numbered("Vented", 5)
  ),
  parts: [
    {
      name: "lid",
      final: "Vented5",
      volume: 120 * 80 * 3 - 4 * Math.PI * 1.75 ** 2 * 3 - 5 * 40 * 3 * 3,
      bounds: [0, 0, 0, 120, 80, 3],
      files: ["sensor-lid.step", "sensor-lid.stl"]
    }
  ]
};
var pcb = box("PCBReference", [80, 45, 1.6], [10, 7.5, 8]);
var lidHolesA = corners(5, 95, 5, 55).map(
  ([x, y], i) => cylinder("LidHole" + (i + 1), [1.5, 7], [x, y, 23])
);
var lidCuts = ["LidCut1", "LidCut2", "LidCut3", "Lid"];
var posts2 = corners(20, 120, 15, 75).map(
  ([x, y], i) => cylinder("Post" + (i + 1), [3, 6], [x, y, 2])
);
var ports = [
  box("UsbCut", [6, 14, 8], [-2, 38, 12]),
  box("EthCut", [6, 18, 15], [136, 36, 10])
].map((p) => ({ ...p, group: "wall cutout" }));
var lidVents = [30, 36, 42, 48, 54, 60].map(
  (y, i) => box("Vent" + (i + 1), [50, 3, 7], [45, y, 33])
);
var labelPlate = box("LabelPlate", [30, 12, 1], [100, 10, 38], 15);
var enclosureSpec = {
  project: "enclosure.FCStd",
  label: "Reef field enclosure",
  seed: [
    box("BaseStock", [100, 60, 25], [0, 0, 0]),
    box("Cavity", [96, 56, 25], [2, 2, 2]),
    { name: "Shell", operation: "cut", base: "BaseStock", tool: "Cavity" },
    box("LidStock", [100, 60, 3], [0, 0, 25]),
    ...lidHolesA,
    ...chain(
      "cut",
      "LidStock",
      lidHolesA.map((h) => h.name),
      lidCuts
    ),
    pcb
  ],
  revised: [
    box("BaseStock", [140, 90, 35], [0, 0, 0]),
    box("Cavity", [136, 86, 35], [2, 2, 2]),
    box("LidStock", [140, 90, 3], [0, 0, 35]),
    ...corners(7, 133, 7, 83).map(
      ([x, y], i) => cylinder("LidHole" + (i + 1), [1.75, 7], [x, y, 33])
    )
  ],
  protectedObjects: [pcb],
  originalLinks: [
    { name: "Shell", operation: "cut", base: "BaseStock", tool: "Cavity" },
    ...chain(
      "cut",
      "LidStock",
      lidHolesA.map((h) => h.name),
      lidCuts
    )
  ],
  added: [...posts2, ...ports, ...lidVents, labelPlate],
  addedLinks: [
    ...chain(
      "fuse",
      "Shell",
      posts2.map((p) => p.name),
      numbered("Mount", 4)
    ),
    ...chain(
      "cut",
      "Mount4",
      ports.map((p) => p.name),
      ["Ported1", "Ported2"]
    ),
    ...chain(
      "cut",
      "Lid",
      lidVents.map((v) => v.name),
      numbered("Vented", 6)
    ),
    {
      name: "LidFinal",
      operation: "fuse",
      base: "Vented6",
      tool: "LabelPlate"
    }
  ],
  parts: [
    {
      name: "base",
      final: "Ported2",
      volume: 140 * 90 * 35 - 136 * 86 * 33 + 4 * Math.PI * 9 * 6 - 2 * 14 * 8 - 2 * 18 * 15,
      bounds: [0, 0, 0, 140, 90, 35],
      files: ["enclosure-base.step", "enclosure-base.stl"]
    },
    {
      name: "lid",
      final: "LidFinal",
      volume: 140 * 90 * 3 - 4 * Math.PI * 1.75 ** 2 * 3 - 6 * 50 * 3 * 3 + 30 * 12 * 1,
      bounds: [
        0,
        0,
        35,
        140,
        90,
        // The rotated label plate is entirely inside the lid footprint.
        39
      ],
      files: ["enclosure-lid.step", "enclosure-lid.stl"]
    }
  ]
};
var rule = "The new feature names refer to visible FreeCAD labels; GUI-generated internal IDs for new features are acceptable. Keep the existing internal object IDs unchanged. Do not replace the feature tree with an uneditable imported solid, and do not create duplicate final parts.";
var guidance2 = "Use FreeCAD's typed file-based MCP tools on the existing project. Inspect IDs and dimensions, update existing primitives, add the new primitives and linked booleans, and export only the requested final features. These tools reopen and save native files; they do not control the open GUI session. No generic scripts or file APIs.";
var placement = (p) => `(${p.at.join(",")})` + (p.rotation ? ` rotated ${p.rotation} degrees about Z` : "");
var describe = (p) => p.kind === "box" ? `${p.name}, a ${p.size.join(" x ")} box at ${placement(p)}` : `${p.name}, a cylinder with radius ${p.size[0]} and height ${p.size[1]} at ${placement(p)}`;
var lidVentPattern = {
  id: "freecad-lid-vent-pattern",
  application: "freecad",
  title: "Revise a sensor lid and cut a linked five-slot vent pattern",
  family: "showcase-freecad",
  split: "development",
  deadlineSeconds: 2400,
  inputFile: lidSpec.project,
  outputs: [lidSpec.project, ...lidSpec.parts[0].files],
  mcpMode: "file",
  brief: [
    "Revise the existing sensor-lid.FCStd for lid revision B. Work in millimeters; every placement is the object's minimum corner (boxes) or base-circle center (cylinders).",
    "Revision A was a 100 x 60 x 3 lid with radius-1.5 screw holes 6 mm from each edge. It is superseded.",
    "Set LidStock to 120 x 80 x 3 at (0,0,0). Set ScrewHole1..ScrewHole4 to radius 1.75, keeping height 7 and Z=-2, at (7,7,-2), (113,7,-2), (7,73,-2) and (113,73,-2) respectively.",
    "Keep the existing linked cut chain Drilled1, Drilled2, Drilled3 and Lid with their original operands. GasketReference is a protected 96 x 56 x 1 reference at (2,2,3); do not change it.",
    "Add five vent slots, each a 40 x 3 x 7 box: " + vents.map((v) => `${v.name} at (${v.at.join(",")})`).join(", ") + ". Cut them sequentially as Vented1=Lid-Vent1, Vented2=Vented1-Vent2, Vented3=Vented2-Vent3, Vented4=Vented3-Vent4 and Vented5=Vented4-Vent5, so each slot passes through the lid.",
    rule,
    "Save the native project in place. Export only Vented5 to sensor-lid.step and sensor-lid.stl in the same folder, excluding GasketReference, cutters and intermediate features. Inspect the final lid before finishing."
  ].join("\n"),
  mcpGuidance: guidance2,
  prepare: (work) => prepare4(lidSpec, work),
  verify: (ctx) => verify3(lidSpec, ctx)
};
var enclosureRelease = {
  id: "freecad-enclosure-release",
  application: "freecad",
  title: "Release a two-part field enclosure revision with mounts, ports, vents and a label plate",
  family: "showcase-freecad",
  split: "development",
  deadlineSeconds: 5400,
  inputFile: enclosureSpec.project,
  outputs: [
    enclosureSpec.project,
    ...enclosureSpec.parts.flatMap((p) => p.files)
  ],
  mcpMode: "file",
  brief: [
    "Release revision B of the existing two-part enclosure.FCStd. Work in millimeters; every placement is the object's minimum corner (boxes) or base-circle center (cylinders). Revision A (100 x 60 x 25 base, 100 x 60 x 3 lid, radius-1.5 lid holes) is superseded.",
    "BASE. Set BaseStock to 140 x 90 x 35 at (0,0,0) and Cavity to 136 x 86 x 35 at (2,2,2), keeping the existing linked cut Shell=BaseStock-Cavity.",
    "Add four mounting posts, each with radius 3 and height 6: " + posts2.map((p) => `${p.name} at (${p.at.join(",")})`).join(", ") + ". Fuse them sequentially as Mount1=Shell+Post1, Mount2=Mount1+Post2, Mount3=Mount2+Post3, Mount4=Mount3+Post4.",
    "Add two wall cutouts: " + ports.map(describe).join("; ") + ". Cut them sequentially as Ported1=Mount4-UsbCut and Ported2=Ported1-EthCut. Ported2 is the final base.",
    "LID. Set LidStock to 140 x 90 x 3 at (0,0,35). Set LidHole1..LidHole4 to radius 1.75, keeping height 7, at (7,7,33), (133,7,33), (7,83,33) and (133,83,33). Keep the existing linked cut chain LidCut1, LidCut2, LidCut3 and Lid with their original operands.",
    "Add six vent slots, each a 50 x 3 x 7 box: " + lidVents.map((v) => `${v.name} at (${v.at.join(",")})`).join(", ") + ". Cut them sequentially as Vented1=Lid-Vent1 through Vented6=Vented5-Vent6.",
    `Add ${describe(labelPlate)}, then fuse LidFinal=Vented6+LabelPlate. LidFinal is the final lid.`,
    "PCBReference is a protected 80 x 45 x 1.6 reference at (10,7.5,8); do not change it.",
    rule,
    "Save the native project in place. Export only Ported2 to enclosure-base.step and enclosure-base.stl, and only LidFinal to enclosure-lid.step and enclosure-lid.stl, all in the same folder. Exclude PCBReference, cutters and intermediate features. Inspect both final parts before finishing."
  ].join("\n"),
  mcpGuidance: guidance2,
  prepare: (work) => prepare4(enclosureSpec, work),
  verify: (ctx) => verify3(enclosureSpec, ctx)
};
var specs2 = /* @__PURE__ */ new Map([
  [lidVentPattern.id, lidSpec],
  [enclosureRelease.id, enclosureSpec]
]);
var freecadShowcase = [lidVentPattern, enclosureRelease];
import { createHash as createHash6 } from "node:crypto";
import { join as join3, resolve as resolve41 } from "node:path";
import { mkdtemp as mkdtemp12, readFile as readFile21, readdir as readdir2, rm as rm12, unlink as unlink10 } from "node:fs/promises";
import { tmpdir as tmpdir5 } from "node:os";
var scale2 = [1.2, 0.8, 0.65];
var floorColor = [0.13, 0.15, 0.18, 1];
var lights = [
  { name: "Key", location: [3, -4, 6], energy: 1e3 },
  { name: "Fill", location: [-4, -2, 3], energy: 400 }
];
var product = (name, location, rotationZ, color2, roughness, metallic) => ({
  name,
  location,
  rotationZ,
  scale: scale2,
  color: color2,
  roughness,
  metallic
});
var round = (v) => v.map((x) => Math.round(x * 1e3) / 1e3);
var teal = linearColor("#168B83");
var coral = linearColor("#E6614C");
function cameraEuler(c) {
  const [dx, dy, dz] = c.target.map((t, i) => t - c.location[i]);
  const deg = (r) => Math.round(r * 180 / Math.PI * 100) / 100;
  return [
    deg(Math.atan2(Math.hypot(dx, dy), -dz)),
    0,
    deg(Math.atan2(dy, dx) - Math.PI / 2)
  ];
}
var cameraText = (c) => `at (${c.location.join(",")}) looking at (${c.target.join(",")}) (equivalent to an XYZ Euler rotation of (${cameraEuler(c).join(", ")}) degrees), ` + (c.ortho ? `orthographic with orthographic scale ${c.ortho}` : `perspective with a ${c.lens} mm lens`);
var cameraArgs = (path, c) => ({
  path,
  location: c.location,
  target: c.target,
  lens_mm: c.lens ?? 50,
  projection: c.ortho ? "ORTHO" : "PERSP",
  ortho_scale: c.ortho ?? 6
});
async function seed2(s2, work) {
  const path = resolve41(work, s2.project);
  await withApplication("blender", work, async (c) => {
    await call(c, "create_scene", { path, overwrite: true });
    const objects = [
      ["Floor", "plane", [0, 0, 0], s2.floorScale, floorColor],
      ["ArchiveSample", "cube", s2.archive, [0.5, 0.5, 0.5], [0.5, 0.5, 0.5, 1]]
    ];
    for (const p of s2.seed) {
      await call(c, "add_mesh", {
        path,
        kind: "cube",
        name: p.name,
        location: p.location
      });
      await call(c, "set_transform", {
        path,
        object_name: p.name,
        location: p.location,
        rotation_degrees: [0, 0, p.rotationZ],
        scale: p.scale
      });
      await call(c, "assign_material", {
        path,
        object_name: p.name,
        name: p.name + "Material",
        color: p.color,
        roughness: p.roughness,
        metallic: p.metallic
      });
    }
    for (const [name, kind, location, size, color2] of objects) {
      await call(c, "add_mesh", { path, kind, name, location });
      await call(c, "set_transform", {
        path,
        object_name: name,
        location,
        scale: size
      });
      await call(c, "assign_material", {
        path,
        object_name: name,
        name: name + "Material",
        color: color2
      });
    }
    await call(c, "configure_camera", cameraArgs(path, s2.camera));
    for (const l of lights)
      await call(c, "add_light", {
        path,
        name: l.name,
        location: l.location,
        energy: l.energy
      });
    for (const file of [...s2.protectedFiles ?? [], ...s2.staleFiles ?? []])
      await call(c, "render_preview", {
        path,
        output: resolve41(work, file),
        width: 640,
        height: 360
      });
  });
  await new NativeEngine("blender").run(
    [
      "bpy.ops.wm.open_mainfile(filepath=p['path']); s=bpy.context.scene",
      "bpy.data.objects['ArchiveSample'].hide_render=True",
      "s.render.threads_mode='FIXED'; s.render.threads=2",
      "s.cycles.use_denoising=False; bpy.context.view_layer.cycles.use_denoising=False",
      "s.render.resolution_x=p['w']; s.render.resolution_y=p['h']; s.cycles.samples=p['samples']",
      "bpy.ops.wm.save_as_mainfile(filepath=p['path']); result={'seeded':True}"
    ],
    {
      path,
      w: draftSettings.width,
      h: draftSettings.height,
      samples: draftSettings.samples
    }
  );
  for (const name of await readdir2(work))
    if (name.endsWith(".blend1")) await unlink10(resolve41(work, name));
  const digests = async (files = []) => {
    const hashes = {};
    for (const file of files)
      hashes[file] = sha256(await readFile21(resolve41(work, file)));
    return hashes;
  };
  return {
    protected_files: await digests(s2.protectedFiles),
    stale_files: await digests(s2.staleFiles)
  };
}
var sha256 = (data) => createHash6("sha256").update(data).digest("hex");
async function pixels3(path, [w, h]) {
  return (await execute(
    "ffmpeg",
    [
      "-loglevel",
      "error",
      "-i",
      path,
      "-vf",
      `scale=${w}:${h}`,
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
    { encoding: "buffer", timeout: 15e3, maxBuffer: w * h * 3 + 1024 }
  )).stdout;
}
async function renderMatches(ctx, s2, still, override) {
  const [w, h] = still.size, delivered = resolve41(ctx.work, still.file);
  if (!await pngEvidence(delivered, w, h)) return false;
  const temporary = await mkdtemp12(join3(tmpdir5(), "reef-showcase-render-")), reference = join3(temporary, "reference.png");
  try {
    await new NativeEngine("blender").run(
      [
        "bpy.ops.wm.open_mainfile(filepath=p['project']); s=bpy.context.scene",
        "s.render.threads_mode='FIXED'; s.render.threads=2",
        "s.render.resolution_x=p['w']; s.render.resolution_y=p['h']; s.render.resolution_percentage=100",
        "if p['camera']:",
        "    c=s.camera; k=p['camera']; c.location=k['location']",
        "    c.rotation_euler=(Vector(k['target'])-c.location).to_track_quat('-Z','Y').to_euler()",
        "    c.data.type='ORTHO' if k.get('ortho') else 'PERSP'",
        "    if k.get('ortho'): c.data.ortho_scale=k['ortho']",
        "    else: c.data.lens=k['lens']",
        "s.render.image_settings.file_format='PNG'; s.render.filepath=p['output']",
        "bpy.ops.render.render(write_still=True); result={'rendered':True}"
      ],
      {
        project: resolve41(ctx.work, s2.project),
        output: reference,
        w,
        h,
        camera: override ? still.camera : null
      }
    );
    const size = [
      Math.max(16, Math.round(w / 12)),
      Math.max(16, Math.round(h / 12))
    ], actual = await pixels3(delivered, size), expected = await pixels3(reference, size);
    return actual.length === size[0] * size[1] * 3 && expected.length === actual.length && [...actual].reduce((sum, v, i) => sum + Math.abs(v - expected[i]), 0) / actual.length <= 2;
  } finally {
    await rm12(temporary, { recursive: true, force: true });
  }
}
var productNames = (s2) => s2.products.map((p) => p.name);
function materialMatches(d, p) {
  const m = d.objects[p.name]?.material;
  return !!m && vectorClose(m.color, p.color, 3e-3) && closeTo(m.roughness, p.roughness) && closeTo(m.metallic, p.metallic);
}
var sameCamera = (a, b) => vectorClose(a.location, b.location) && vectorClose(a.target, b.target) && a.lens === b.lens && a.ortho === b.ortho;
var draftSettings = { width: 1280, height: 720, samples: 64 };
async function verify4(s2, ctx) {
  const final = s2.stills[s2.stills.length - 1], state = sceneState(ctx.work, {
    projectFile: s2.project,
    target: final.camera.target
  }), names3 = productNames(s2), unchanged = (p) => {
    const before = s2.seed.find((q) => q.name === p.name);
    return !!before && vectorClose(before.location, p.location) && before.rotationZ === p.rotationZ && vectorClose(before.scale, p.scale);
  }, arranged = !s2.products.every(unchanged), recolored = s2.products.filter((p) => {
    const before = s2.seed.find((q) => q.name === p.name);
    return !before || !vectorClose(before.color, p.color);
  }), kept = s2.products.filter((p) => !recolored.includes(p)), cameraMoved = !sameCamera(s2.camera, final.camera);
  const m = ctx.metadata;
  const independent = (d) => names3.every(
    (n) => d.objects[n]?.type === "MESH" && d.objects[n].vertices === 8
  ) && new Set(names3.map((n) => d.objects[n]?.mesh_data)).size === names3.length, placed = (d) => s2.products.every(
    (p) => vectorClose(d.objects[p.name]?.location, p.location) && vectorClose(d.objects[p.name]?.scale, p.scale)
  ), rotated = (d) => s2.products.every(
    (p) => vectorClose(d.objects[p.name]?.rotation, [0, 0, p.rotationZ], 0.01)
  ), cameraMatches = (d) => {
    const c = d.camera, k = final.camera;
    return !!c && vectorClose(c.location, k.location) && c.aim > 0.9999 && (k.ortho ? c.projection === "ORTHO" && closeTo(c.ortho_scale, k.ortho) : c.projection === "PERSP" && closeTo(c.lens, k.lens));
  };
  return Promise.all([
    ...arranged ? [
      criterion(
        "every product is an independent eight-vertex mesh with its own data",
        async () => independent(await state)
      ),
      criterion(
        "products have the approved locations and scale",
        async () => placed(await state)
      ),
      criterion(
        "products have the approved rotations",
        async () => rotated(await state)
      )
    ] : [],
    ...recolored.map(
      (p) => criterion(
        `${p.name} has its approved Principled color, roughness and metallic values`,
        async () => materialMatches(await state, p)
      )
    ),
    ...s2.rim ? [
      criterion("RimLight is the approved new point light", async () => {
        const rim = (await state).objects.RimLight;
        return rim?.light_kind === "POINT" && closeTo(rim.energy, s2.rim.energy) && vectorClose(rim.location, s2.rim.location);
      })
    ] : [],
    ...cameraMoved ? [
      criterion(
        "the saved active camera has the final approved viewpoint and projection",
        async () => cameraMatches(await state)
      )
    ] : [],
    criterion(
      "saved render settings use CPU Cycles, 16 samples and the final delivery resolution",
      async () => {
        const d = await state;
        return d.render_engine === "CYCLES" && d.device === "CPU" && d.samples === 16 && vectorClose(d.resolution, [...final.size, 100]);
      }
    ),
    ...s2.stills.map(
      (still, i) => criterion(
        `${still.file} matches an independent render of the saved scene from its approved viewpoint`,
        async () => {
          await state;
          const stale = m.stale_files?.[still.file];
          if (stale && sha256(await readFile21(resolve41(ctx.work, still.file))) === stale)
            return false;
          return renderMatches(ctx, s2, still, i < s2.stills.length - 1);
        }
      )
    ),
    ...s2.obj ? [
      criterion(`${s2.obj} contains only the product meshes`, async () => {
        const obj = await readFile21(resolve41(ctx.work, s2.obj), "utf8"), objects = [...obj.matchAll(/^o (.+)$/gm)].map((m2) => m2[1]);
        return objects.length === names3.length && names3.every(
          (n) => objects.some((o) => o === n || o.startsWith(n + "_"))
        ) && !/Floor|ArchiveSample/.test(obj) && [...obj.matchAll(/^v /gm)].length === 8 * names3.length;
      })
    ] : [],
    // One criterion for everything the brief protects, so doing nothing earns little.
    criterion(
      "everything outside the approved changes is preserved",
      async () => {
        const d = await state, f = d.objects.Floor, a = d.objects.ArchiveSample, allowed = /* @__PURE__ */ new Set([
          ...names3,
          "Floor",
          "ArchiveSample",
          "Camera",
          "Key",
          "Fill",
          ...s2.rim ? ["RimLight"] : []
        ]);
        for (const [file, sha2] of Object.entries(m.protected_files ?? {}))
          if (sha256(await readFile21(resolve41(ctx.work, file))) !== sha2)
            return false;
        return (arranged || independent(d) && placed(d) && rotated(d)) && (cameraMoved || cameraMatches(d)) && kept.every((p) => materialMatches(d, p)) && f?.vertices === 4 && vectorClose(f.location, [0, 0, 0]) && vectorClose(f.scale, s2.floorScale) && vectorClose(f.rotation, [0, 0, 0]) && vectorClose(f.material?.color, floorColor) && a?.vertices === 8 && vectorClose(a.location, s2.archive) && vectorClose(a.scale, [0.5, 0.5, 0.5]) && vectorClose(a.rotation, [0, 0, 0]) && a.hidden && Object.keys(d.objects).every((n) => allowed.has(n)) && lights.every(
          (l) => d.objects[l.name]?.light_kind === "AREA" && closeTo(d.objects[l.name].energy, l.energy) && vectorClose(d.objects[l.name].location, l.location)
        );
      }
    )
  ]);
}
var heroCamera = {
  location: [7, -10, 7],
  target: [0, 0, 1],
  lens: 52
};
var pair = [
  product("Body", [-1.7, 0, 1], 15, teal, 0.3, 0.25),
  product("Body_Coral", [1.7, 0, 1], -15, coral, 0.3, 0.25)
];
var navy = [0.01, 0.06, 0.18, 1];
var amber = [0.9, 0.42, 0.03, 1];
var sand = [0.68, 0.52, 0.33, 1];
var refreshSpec = {
  project: "product-hero.blend",
  floorScale: [6, 6, 1],
  archive: [4, 3, 0.5],
  seed: pair,
  products: [
    { ...pair[0], color: navy, roughness: 0.45, metallic: 0.1 },
    { ...pair[1], color: amber, roughness: 0.35, metallic: 0 }
  ],
  camera: heroCamera,
  stills: [{ file: "product-hero.png", size: [640, 360], camera: heroCamera }],
  staleFiles: ["product-hero.png"]
};
var draftCamera = {
  location: [7, -9, 6],
  target: [0, 0, 1],
  lens: 50
};
var stillsSpec = {
  project: "product-hero.blend",
  floorScale: [6, 6, 1],
  archive: [4, 3, 0.5],
  seed: pair,
  products: pair,
  camera: draftCamera,
  stills: [
    {
      file: "still-front.png",
      size: [480, 480],
      camera: { location: [0, -11, 3], target: [0, 0, 1], lens: 50 }
    },
    {
      file: "still-side.png",
      size: [480, 480],
      camera: { location: [11, 0, 3], target: [0, 0, 1], lens: 50 }
    },
    { file: "still-hero.png", size: [480, 480], camera: heroCamera }
  ],
  protectedFiles: ["product-hero.png"]
};
var master = product("Body", [0, 0, 1], 0, teal, 0.3, 0.25);
var lineupCamera = {
  location: [0, -14, 4],
  target: [0, 0, 1],
  ortho: 15
};
var lineupSpec = {
  project: "colorway-lineup.blend",
  floorScale: [9, 6, 1],
  archive: [7.5, 4.5, 0.5],
  seed: [master],
  source: "Body",
  products: [
    { ...master, location: [-5.6, 0, 1], rotationZ: -20 },
    product("Body_Navy", [-2.8, 0, 1], -10, navy, 0.45, 0.1),
    product("Body_Sand", [0, 0, 1], 0, sand, 0.6, 0),
    product("Body_Amber", [2.8, 0, 1], 10, amber, 0.35, 0),
    product(
      "Body_Coral",
      [5.6, 0, 1],
      20,
      round(coral.slice(0, 3)).concat(1),
      0.3,
      0.25
    )
  ],
  camera: draftCamera,
  rim: { location: [0, 6, 5], energy: 800 },
  stills: [
    {
      file: "detail-amber.png",
      size: [400, 400],
      camera: { location: [4.6, -4.4, 2.4], target: [2.8, 0, 1], lens: 60 }
    },
    { file: "lineup.png", size: [960, 400], camera: lineupCamera }
  ],
  obj: "colorway-lineup.obj"
};
var color = (p) => `scene-linear RGB (${p.color.slice(0, 3).join(", ")}), roughness ${p.roughness}, metallic ${p.metallic}`;
var colorRule = "Color values are scene-linear RGB, exactly as shown in the RGB fields of Blender's color picker; do not enter them in the gamma-corrected Hex field.";
var renderDetails = (still) => `a nonblank ${still.size.join(" x ")} PNG rendered with Cycles on CPU, 16 samples, 100% resolution, from the camera ${cameraText(still.camera)}`;
var render = (still) => `${still.file}: ${renderDetails(still)}`;
var guidance3 = "Use Blender's typed file-based tools to inspect, duplicate, transform, assign independent materials, add lights, configure the camera, render and export. Material RGBA values are scene-linear. Tools reopen/save the native file rather than controlling the visible GUI. No scripts or generic file APIs.";
var preserve = "Preserve Floor's geometry, transform and material, the Key and Fill lights, and ArchiveSample's geometry and transform; ArchiveSample must remain excluded from rendering. Keep the single existing camera object; do not add cameras, empties or any objects this brief does not request.";
var materialRefresh = {
  id: "blender-material-refresh",
  application: "blender",
  title: "Apply an approved colorway refresh and re-render the product hero",
  family: "showcase-blender",
  split: "development",
  deadlineSeconds: 1800,
  inputFile: refreshSpec.project,
  outputs: [refreshSpec.project, "product-hero.png"],
  mcpMode: "file",
  brief: [
    "The R4 colorway replaces the teal and coral products in the existing product-hero.blend. Body becomes Harbor navy: " + color(refreshSpec.products[0]) + ". Body_Coral becomes Signal amber: " + color(refreshSpec.products[1]) + ". The two products must keep independent materials.",
    colorRule,
    "Do not move, rotate, rescale or rebuild either product, and keep the current camera framing.",
    preserve,
    "Save the scene in place, then replace the superseded product-hero.png with " + renderDetails(refreshSpec.stills[0]) + ". Review the delivered image before finishing."
  ].join("\n"),
  mcpGuidance: guidance3,
  prepare: (work) => seed2(refreshSpec, work),
  verify: (ctx) => verify4(refreshSpec, ctx)
};
var turntableStills = {
  id: "blender-turntable-stills",
  application: "blender",
  title: "Render front, side and hero catalog stills from one saved scene",
  family: "showcase-blender",
  split: "development",
  deadlineSeconds: 2700,
  inputFile: stillsSpec.project,
  outputs: [stillsSpec.project, ...stillsSpec.stills.map((s2) => s2.file)],
  mcpMode: "file",
  brief: [
    "Produce three square catalog stills of the two existing products in product-hero.blend without changing the products, their materials or the lights.",
    "Deliver, in this order:",
    ...stillsSpec.stills.map((s2, i) => `${i + 1}. ${render(s2)}.`),
    "The scene's current camera framing is an earlier draft and is superseded. The saved scene must end on the hero viewpoint with the 480 x 480 delivery settings. Reuse the single existing camera for every viewpoint.",
    preserve,
    "The existing product-hero.png is a signed earlier delivery; keep it byte-for-byte unchanged. Save the scene in place and review all three stills before finishing."
  ].join("\n"),
  mcpGuidance: guidance3,
  prepare: (work) => seed2(stillsSpec, work),
  verify: (ctx) => verify4(stillsSpec, ctx)
};
var colorwayLineup = {
  id: "blender-colorway-lineup",
  application: "blender",
  title: "Build a five-colorway product lineup with a detail shot and catalog render",
  family: "showcase-blender",
  split: "development",
  deadlineSeconds: 5400,
  inputFile: lineupSpec.project,
  outputs: [
    lineupSpec.project,
    ...lineupSpec.stills.map((s2) => s2.file),
    lineupSpec.obj
  ],
  mcpMode: "file",
  brief: [
    "Build the approved five-colorway lineup in colorway-lineup.blend from the existing master product Body. Every product keeps scale (1.2,0.8,0.65), zero X/Y rotation and location Z=1, and must be an independent mesh with its own material slot; changing one product's material must not recolor another.",
    "Products, left to right (location; Z rotation; material):",
    ...lineupSpec.products.map(
      (p, i) => `${i + 1}. ${p.name} at (${p.location.join(",")}); ${p.rotationZ} degrees; ${i === 0 ? "keep its existing teal material unchanged" : color(p)}.`
    ),
    "Create the four new products by duplicating Body, not by adding new primitives.",
    colorRule,
    `Add one point light named RimLight at (${lineupSpec.rim.location.join(",")}) with a power of 800 W. Keep the Key and Fill area lights unchanged.`,
    "Deliver two renders, in this order:",
    ...lineupSpec.stills.map((s2, i) => `${i + 1}. ${render(s2)}.`),
    "The saved scene must end on the lineup viewpoint with the 960 x 400 delivery settings. Reuse the single existing camera for both viewpoints.",
    preserve,
    "Save the scene in place. Export only the five products to colorway-lineup.obj, excluding Floor, ArchiveSample, the camera and lights. Review both renders before finishing."
  ].join("\n"),
  mcpGuidance: guidance3,
  prepare: (work) => seed2(lineupSpec, work),
  verify: (ctx) => verify4(lineupSpec, ctx)
};
var specs3 = /* @__PURE__ */ new Map([
  [materialRefresh.id, refreshSpec],
  [turntableStills.id, stillsSpec],
  [colorwayLineup.id, lineupSpec]
]);
var blenderShowcase = [
  materialRefresh,
  turntableStills,
  colorwayLineup
];
import { copyFile as copyFile10, mkdir as mkdir3, mkdtemp as mkdtemp13, rm as rm13, writeFile as writeFile14 } from "node:fs/promises";
import { tmpdir as tmpdir6 } from "node:os";
import { resolve as resolve42 } from "node:path";
import { isDeepStrictEqual as isDeepStrictEqual16 } from "node:util";
var signed = "Signed R1 / 22 October 2026 - do not amend";
var archiveName = "Signed R1 archive";
function voice(kind, seconds2) {
  const a = new Float32Array(Math.round(seconds2 * audioRate)), base = kind === "host" ? 180 : 240;
  for (let i = 0; i < a.length; i++) {
    const t = i / audioRate, f = base + 17 * (Math.floor(t) * 7 % 11), env = 0.55 + 0.45 * Math.sin(2 * Math.PI * (kind === "host" ? 3.1 : 2.7) * t);
    a[i] = env * (0.07 * Math.sin(2 * Math.PI * f * t) + 0.025 * Math.sin(2 * Math.PI * 2 * f * t));
  }
  return a;
}
function source3(kind, seconds2) {
  if (kind === "host" || kind === "guest") return voice(kind, seconds2);
  return originalStem(kind === "bed" ? "lead" : kind, seconds2);
}
var fileName = (t, i) => `sources/${i + 1}-${t.source}.wav`;
function expectedTrack(t, seconds2) {
  let a = quantized(source3(t.source, t.sourceSeconds));
  for (const [s2, e] of [...t.cuts ?? []].sort((x, y) => y[0] - x[0])) {
    const out2 = new Float32Array(a.length - Math.round((e - s2) * audioRate));
    out2.set(a.slice(0, Math.round(s2 * audioRate)));
    out2.set(a.slice(Math.round(e * audioRate)), Math.round(s2 * audioRate));
    a = out2;
  }
  if (t.repeat) {
    const out2 = new Float32Array(a.length * (t.repeat + 1));
    for (let i = 0; i <= t.repeat; i++) out2.set(a, i * a.length);
    a = out2;
  }
  if (t.endAt !== void 0) a = a.slice(0, Math.round(t.endAt * audioRate));
  if (t.start) {
    const out2 = new Float32Array(a.length + Math.round(t.start * audioRate));
    out2.set(a, Math.round(t.start * audioRate));
    a = out2;
  }
  for (const [start, end, frequency] of t.tones ?? [])
    for (let i = 0; i < Math.round((end - start) * audioRate); i++)
      a[Math.round(start * audioRate) + i] = 0.2 * Math.sin(2 * Math.PI * frequency * i / audioRate);
  for (const [start, end, direction] of t.fades)
    a = fade(a, start, end, direction);
  const out = new Float32Array(Math.round(seconds2 * audioRate));
  out.set(a.slice(0, out.length));
  return out;
}
var expectedMix = (s2) => mixSignals(
  s2.tracks.map((t) => ({
    samples: expectedTrack(t, s2.seconds),
    gainDb: t.gainDb,
    pan: t.pan
  })),
  s2.seconds
);
var finalLength = (t) => {
  let length = t.sourceSeconds - (t.cuts ?? []).reduce((sum, [s2, e]) => sum + e - s2, 0);
  length *= (t.repeat ?? 0) + 1;
  if (t.endAt !== void 0) length = Math.min(length, t.endAt);
  return length + (t.start ?? 0);
};
async function native2(work, env, action) {
  const session = await startAudacity(work, {
    environment: Object.fromEntries(
      Object.entries(env).filter(
        ([k]) => !k.startsWith("AUDACITY_PIPE_") && k !== "DISPLAY"
      )
    )
  });
  const p = new AudacityPipe(
    session.env.AUDACITY_PIPE_TO,
    session.env.AUDACITY_PIPE_FROM
  );
  try {
    return await action(new AudacityNative(p));
  } catch (e) {
    const proof = await captureAudacityFailure(session, p, e).catch(
      () => void 0
    );
    throw new Error(
      String(e) + (proof ? "; native diagnostics: " + proof : "")
    );
  } finally {
    p.close();
    await session.stop();
  }
}
async function prepare5(s2, work, env) {
  await mkdir3(resolve42(work, "sources"), { recursive: true });
  const hashes = {}, all = [
    ...s2.tracks,
    {
      name: archiveName,
      source: "archive",
      sourceSeconds: s2.archiveSeconds
    }
  ];
  for (const [i, t] of all.entries()) {
    const path = resolve42(work, fileName(t, i));
    await writeFile14(path, encodeMono(source3(t.source, t.sourceSeconds)));
    hashes[fileName(t, i)] = await audioFileDigest(path);
  }
  await writeFile14(
    resolve42(work, "ATTRIBUTION.txt"),
    "Reef showcase audio fixtures: original synthesized voice, music-bed and archive signals created for this benchmark. CC0-1.0. No third-party recording, samples, voice, cloud or generative service.\n"
  );
  hashes["ATTRIBUTION.txt"] = await audioFileDigest(
    resolve42(work, "ATTRIBUTION.txt")
  );
  const original3 = await native2(work, env, async (a) => {
    await a.pipe.command("SetProject: Rate=48000");
    for (const [i, t] of all.entries())
      await a.import(resolve42(work, fileName(t, i)), t.name);
    await a.mix(all.length - 1, { mute: true, gainDb: 0, pan: 0 });
    for (const [i, [draft3]] of s2.drafts.entries())
      await a.label(i * 2, i * 2 + 2, draft3);
    await a.label(0, s2.archiveSeconds, signed);
    await a.save(resolve42(work, s2.project));
    return a.inspect();
  });
  return { source_hashes: hashes, original: original3 };
}
async function inspectSaved2(ctx, s2) {
  const dir = await mkdtemp13(resolve42(tmpdir6(), "reef-showcase-audio-")), project2 = resolve42(dir, "saved.aup3"), submitted = resolve42(ctx.work, s2.project);
  const before = await savedAudioHashes(submitted);
  await copyFile10(submitted, project2);
  try {
    await copyFile10(submitted + "-wal", project2 + "-wal");
  } catch (e) {
    if (e.code !== "ENOENT") throw e;
  }
  let complete = false;
  try {
    const integrity = await execute(
      "sqlite3",
      [project2, "PRAGMA integrity_check; SELECT count(*) FROM project;"],
      { timeout: 15e3 }
    );
    if (integrity.stdout.trim() !== "ok\n1")
      throw new Error(
        "Invalid saved Audacity SQLite project: " + integrity.stdout
      );
    await execute("sqlite3", [project2, "DELETE FROM autosave;"], {
      timeout: 15e3
    });
    const snapshot = await native2(dir, ctx.env, async (a) => {
      await a.open(project2);
      const state = await a.inspect();
      await a.export(resolve42(dir, "native-mix.wav"), 2, 0, s2.seconds);
      const tracks = [];
      for (let i = 0; i <= s2.tracks.length; i++) {
        await a.mix(i, { mute: false, solo: false, gainDb: 0, pan: 0 });
        await a.export(
          resolve42(dir, `track-${i}.wav`),
          1,
          0,
          i === s2.tracks.length ? s2.archiveSeconds : s2.seconds,
          [i]
        );
        tracks.push(await decodeAudio(resolve42(dir, `track-${i}.wav`)));
      }
      return {
        state,
        mix: await decodeAudio(resolve42(dir, "native-mix.wav")),
        tracks
      };
    });
    if (!isDeepStrictEqual16(before, await savedAudioHashes(submitted)))
      throw new Error("Judge changed submitted native project");
    complete = true;
    return snapshot;
  } catch (e) {
    await writeFile14(resolve42(dir, "inspection-error.txt"), String(e) + "\n");
    throw new Error(String(e) + "; saved verifier copy retained: " + dir);
  } finally {
    if (complete) await rm13(dir, { recursive: true, force: true });
  }
}
var labelKey = (l) => JSON.stringify(l);
async function verify5(s2, ctx) {
  const m = ctx.metadata, archive2 = s2.tracks.length;
  let snapshot, error;
  try {
    snapshot = await inspectSaved2(ctx, s2);
  } catch (e) {
    error = e;
  }
  const state = snapshot?.state, mix = expectedMix(s2), names3 = [...s2.tracks.map((t) => t.name), archiveName];
  return Promise.all([
    criterion(
      "native saved project reopens as editable tracks rather than a flattened substitute",
      () => {
        if (!snapshot) throw error;
        return state.tracks.filter((t) => t.kind === "wave").length === names3.length && names3.every((n, i) => state.tracks[i]?.name === n);
      }
    ),
    criterion(
      "current tracks have the approved native gain and pan without mute or solo",
      () => s2.tracks.every((t, i) => {
        const n = state?.tracks[i];
        return !!n && Math.abs((n.volume ?? 0) - 10 ** (t.gainDb / 20)) < 2e-6 && Math.abs((n.pan ?? 2) - t.pan) < 1e-5 && !n.mute && !n.solo;
      })
    ),
    criterion(
      "current clips start and end at the approved times without stretched replacements",
      () => s2.tracks.every((t, i) => {
        const c = state?.clips.filter((c2) => c2.track === i) ?? [];
        return c.length > 0 && Math.abs(Math.min(...c.map((c2) => c2.start)) - (t.start ?? 0)) < 1e-4 && Math.abs(Math.max(...c.map((c2) => c2.end)) - finalLength(t)) < 1e-4;
      })
    ),
    ...s2.tracks.map(
      (t, i) => criterion(
        `native ${t.name} track contains the approved edited audio and fades`,
        () => !!snapshot && signalMatches(
          snapshot.tracks[i].samples,
          expectedTrack(t, s2.seconds)
        )
      )
    ),
    // One criterion for everything the brief protects, so doing nothing earns little.
    criterion(
      "the signed archive, its recording and the source files are preserved",
      async () => {
        for (const [p, h] of Object.entries(m.source_hashes))
          if (await audioFileDigest(resolve42(ctx.work, p)) !== h) return false;
        const t = state?.tracks[archive2];
        return !!snapshot && !!t && t.mute === 1 && !t.solo && t.volume === 1 && t.pan === 0 && isDeepStrictEqual16(
          state.clips.filter((c) => c.track === archive2),
          m.original.clips.filter((c) => c.track === archive2)
        ) && signalMatches(
          snapshot.tracks[archive2].samples,
          quantized(originalStem("archive", s2.archiveSeconds))
        );
      }
    ),
    criterion(
      "draft section labels are replaced in place and the signed label is retained",
      () => !!state && isDeepStrictEqual16(
        nativeLabels(state.labels).map(labelKey).sort(),
        [
          ...s2.drafts.map(([, l]) => l),
          [0, s2.archiveSeconds, signed]
        ].map(labelKey).sort()
      )
    ),
    criterion(
      "the saved native project renders the independently specified stereo mix",
      () => !!snapshot && snapshot.mix.rate === audioRate && snapshot.mix.channels === 2 && signalMatches(snapshot.mix.samples, mix)
    ),
    ...s2.exports.map(
      (x) => criterion(
        `${x.file} decodes as 48 kHz stereo audio of the approved ${x.start}-${x.end}s mix`,
        async () => {
          const a = await decodeAudio(resolve42(ctx.work, x.file));
          return a.rate === audioRate && a.channels === 2 && Math.abs(a.duration - (x.end - x.start)) < 1 / audioRate && (x.file.endsWith(".flac") ? a.codec === "flac" : a.codec.startsWith("pcm_")) && signalMatches(
            a.samples,
            mix.slice(x.start * audioRate * 2, x.end * audioRate * 2)
          );
        }
      )
    ),
    criterion(
      "full-length deliveries agree with the mix reopened from saved native state",
      async () => {
        if (!snapshot) return false;
        const full = s2.exports.filter(
          (x) => x.start === 0 && x.end === s2.seconds
        );
        return full.length > 0 && (await Promise.all(
          full.map(
            async (x) => independentExportsAgree(
              (await decodeAudio(resolve42(ctx.work, x.file))).samples,
              snapshot.mix.samples
            )
          )
        )).every(Boolean);
      }
    )
  ]);
}
var interviewSpec = {
  project: "interview-session.aup3",
  seconds: 17,
  archiveSeconds: 20,
  tracks: [
    {
      name: "Host",
      source: "host",
      sourceSeconds: 20,
      cuts: [
        [3, 5],
        [12, 13]
      ],
      fades: [],
      gainDb: -2,
      pan: 0
    },
    {
      name: "Guest",
      source: "guest",
      sourceSeconds: 20,
      cuts: [
        [3, 5],
        [12, 13]
      ],
      fades: [],
      gainDb: -4,
      pan: 0.2
    },
    {
      name: "Music bed",
      source: "bed",
      sourceSeconds: 20,
      endAt: 17,
      fades: [
        [0, 1, "in"],
        [15, 17, "out"]
      ],
      gainDb: -14,
      pan: 0
    }
  ],
  drafts: [
    ["DRAFT welcome", [0, 3, "Welcome"]],
    ["DRAFT interview", [3, 17, "Interview"]]
  ],
  exports: [{ file: "episode.wav", start: 0, end: 17 }]
};
var notes2 = [
  [0, 0.5, 523.251131],
  [0.5, 1, 659.255114],
  [1, 1.5, 783.990872],
  [1.5, 2, 1046.502261],
  [2, 2.5, 783.990872],
  [2.5, 3, 1046.502261]
];
var voiceCuts = [
  [4, 6],
  [14, 15],
  [22, 24]
];
var episodeSpec = {
  project: "episode-session.aup3",
  seconds: 28,
  archiveSeconds: 30,
  tracks: [
    {
      name: "Intro sting",
      source: "silence",
      sourceSeconds: 28,
      tones: notes2,
      fades: [[0, 0.25, "in"]],
      gainDb: -3,
      pan: 0
    },
    {
      name: "Host",
      source: "host",
      sourceSeconds: 30,
      cuts: voiceCuts,
      start: 3,
      fades: [[27, 28, "out"]],
      gainDb: -2,
      pan: -0.1
    },
    {
      name: "Guest",
      source: "guest",
      sourceSeconds: 30,
      cuts: voiceCuts,
      start: 3,
      fades: [[27, 28, "out"]],
      gainDb: -4,
      pan: 0.15
    },
    {
      name: "Music bed",
      source: "bed",
      sourceSeconds: 6,
      repeat: 4,
      endAt: 28,
      fades: [
        [0, 2, "in"],
        [26, 28, "out"]
      ],
      gainDb: -15,
      pan: 0
    }
  ],
  drafts: [
    ["DRAFT sting", [0, 3, "Sting"]],
    ["DRAFT part 1", [3, 15, "Part 1"]],
    ["DRAFT part 2", [15, 22, "Part 2"]],
    ["DRAFT part 3", [22, 28, "Part 3"]]
  ],
  exports: [
    { file: "episode.wav", start: 0, end: 28 },
    { file: "episode.flac", start: 0, end: 28 },
    { file: "sting.wav", start: 0, end: 3 },
    { file: "part-1.wav", start: 3, end: 15 },
    { file: "part-2.wav", start: 15, end: 22 },
    { file: "part-3.wav", start: 22, end: 28 }
  ]
};
var common = "All times are in seconds. A ripple delete removes the interval and closes the gap within that track only. Cut intervals are given in the original recording's timeline; account for earlier deletions if you remove them from left to right. Apply fades after the timeline edits, at the final times given. Gains are in dB and pans run from -1 (left) to +1 (right); current tracks must not be muted or soloed.";
var keep = `Preserve the muted ${archiveName} track, its full recording and the "${signed}" range label exactly. Keep the source WAV files and ATTRIBUTION.txt unchanged. Keep every current track as an editable native track; do not flatten, mix down or replace tracks with imports.`;
var exportRule = (deliveries) => `Save the native project in place, then export ${deliveries}. Review the project and ${deliveries.includes("deliveries") ? "deliveries" : "delivery"} before finishing.`;
var guidance4 = "Use the assigned Audacity live native project tools to inspect tracks/clips/labels, edit selections, generate tones, set gain and pan, update labels, save and export. Tool track indices are zero-based; gain uses dB and pan uses -1..1. Native edits affect the visible GUI; save_project persists the editable project.";
var interviewCleanup = {
  id: "audacity-interview-cleanup",
  application: "audacity",
  title: "Clean up a two-voice interview, duck its music bed and deliver it",
  family: "showcase-audacity",
  split: "development",
  deadlineSeconds: 2400,
  mcpMode: "live",
  inputFile: interviewSpec.project,
  outputs: [interviewSpec.project, "episode.wav"],
  brief: [
    "Finish the approved edit of the existing interview-session.aup3.",
    common,
    "1. Host and Guest: remove the false start at 3-5 s and the cough at 12-13 s of the original recording from both tracks, so they stay in sync and end at 17 s.",
    "2. Music bed: delete everything after 17 s, then fade in over 0-1 s and fade out over 15-17 s.",
    "3. Mix: Host -2 dB, centered; Guest -4 dB, pan +0.2; Music bed -14 dB, centered.",
    '4. Replace the draft labels in place: "DRAFT welcome" becomes "Welcome" at 0-3 s and "DRAFT interview" becomes "Interview" at 3-17 s. Do not add other labels.',
    keep,
    exportRule(
      "episode.wav from the saved mix as 48000 Hz stereo audio covering exactly 0-17 s"
    )
  ].join("\n"),
  mcpGuidance: guidance4,
  prepare: (work, env) => prepare5(interviewSpec, work, env),
  verify: (ctx) => verify5(interviewSpec, ctx)
};
var episodeAssembly = {
  id: "audacity-episode-assembly",
  application: "audacity",
  title: "Assemble a podcast episode with a composed sting, chapter edits, a looped bed and six deliveries",
  family: "showcase-audacity",
  split: "development",
  deadlineSeconds: 5400,
  mcpMode: "live",
  inputFile: episodeSpec.project,
  outputs: [episodeSpec.project, ...episodeSpec.exports.map((x) => x.file)],
  brief: [
    "Assemble the approved 28-second episode in the existing episode-session.aup3.",
    common,
    "1. Host and Guest: remove 4-6 s, 14-15 s and 22-24 s of the original recordings from both tracks. Then move each edited clip so it starts at 3 s (move the clip; do not pad it with generated silence); both end at 28 s.",
    "2. Intro sting: in the existing silent Intro sting track, generate six 0.5 s sine tones at amplitude 0.2, back to back from 0 s: C5 523.251131 Hz, E5 659.255114 Hz, G5 783.990872 Hz, C6 1046.502261 Hz, G5 783.990872 Hz, C6 1046.502261 Hz. Then fade in over 0-0.25 s.",
    "3. Music bed: repeat the 6 s loop four more times (to 30 s), delete everything after 28 s, then fade in over 0-2 s and fade out over 26-28 s.",
    "4. Fade out Host and Guest over 27-28 s.",
    "5. Mix: Intro sting -3 dB, centered; Host -2 dB, pan -0.1; Guest -4 dB, pan +0.15; Music bed -15 dB, centered.",
    '6. Replace the four draft labels in place: "DRAFT sting" becomes "Sting" at 0-3 s; "DRAFT part 1" becomes "Part 1" at 3-15 s; "DRAFT part 2" becomes "Part 2" at 15-22 s; "DRAFT part 3" becomes "Part 3" at 22-28 s. Do not add other labels.',
    keep,
    exportRule(
      "these six deliveries from the saved mix as 48000 Hz stereo audio, each covering exactly its stated time range: episode.wav and episode.flac (0-28 s), sting.wav (0-3 s), part-1.wav (3-15 s), part-2.wav (15-22 s) and part-3.wav (22-28 s)"
    )
  ].join("\n"),
  mcpGuidance: guidance4,
  prepare: (work, env) => prepare5(episodeSpec, work, env),
  verify: (ctx) => verify5(episodeSpec, ctx)
};
var specs4 = /* @__PURE__ */ new Map([
  [interviewCleanup.id, interviewSpec],
  [episodeAssembly.id, episodeSpec]
]);
var audacityShowcase = [interviewCleanup, episodeAssembly];
var showcaseWorkflows = [
  ...freecadShowcase,
  ...blenderShowcase,
  ...audacityShowcase
];
function workflowCohort(name = "admitted") {
  if (name === "admitted")
    return { name, admission: "admitted", workflows: dailyWorkflows };
  if (name === "production")
    return {
      name,
      admission: "draft",
      workflows: productionCandidates
    };
  if (name === "handoff")
    return { name, admission: "draft", workflows: handoffCandidates };
  if (name === "showcase")
    return { name, admission: "draft", workflows: showcaseWorkflows };
  throw Error("Unknown workflow cohort: " + name);
}
function cohortWorkflow(id, name = "admitted") {
  const cohort = workflowCohort(name);
  const workflow6 = cohort.workflows.find((w) => w.id === id);
  if (!workflow6) throw Error(`Unknown ${cohort.name} workflow: ${id}`);
  return workflow6;
}
import { chmod, mkdir as mkdir4, mkdtemp as mkdtemp14, rm as rm14 } from "node:fs/promises";
import { join as join4, resolve as resolve43 } from "node:path";
var DESKTOP_PROFILE_ENV_KEYS = [
  "XDG_CONFIG_HOME",
  "XDG_DATA_HOME",
  "XDG_CACHE_HOME",
  "XDG_STATE_HOME",
  "XDG_RUNTIME_DIR",
  "PHOTOCRAFT_CONFIG_DIR"
];
async function createPrivateDesktopProfile(logsRoot) {
  const logs = resolve43(logsRoot);
  await mkdir4(logs, { recursive: true, mode: 448 });
  const root = await mkdtemp14(join4(logs, "profile-"));
  try {
    await chmod(root, 448);
    const directories = {
      config: join4(root, "config"),
      data: join4(root, "data"),
      cache: join4(root, "cache"),
      state: join4(root, "state"),
      runtime: join4(root, "runtime"),
      photocraftConfig: join4(root, "config", "photocraft")
    };
    for (const directory of [
      directories.config,
      directories.data,
      directories.cache,
      directories.state,
      directories.runtime,
      directories.photocraftConfig
    ]) {
      await mkdir4(directory, { mode: 448 });
      await chmod(directory, 448);
    }
    return {
      root,
      directories,
      env: {
        XDG_CONFIG_HOME: directories.config,
        XDG_DATA_HOME: directories.data,
        XDG_CACHE_HOME: directories.cache,
        XDG_STATE_HOME: directories.state,
        XDG_RUNTIME_DIR: directories.runtime,
        PHOTOCRAFT_CONFIG_DIR: directories.photocraftConfig
      }
    };
  } catch (error) {
    await rm14(root, { recursive: true, force: true });
    throw error;
  }
}
async function grade(task, work, logs, context, env = process.env, cohort = "admitted") {
  await mkdir5(logs, { recursive: true });
  await writeFile15(resolve44(logs, "reward.txt"), "0\n");
  const previousProfileEnv = new Map(
    DESKTOP_PROFILE_ENV_KEYS.map((key3) => [key3, process.env[key3]])
  );
  try {
    const profile = await createPrivateDesktopProfile(
      resolve44(logs, "native-session")
    );
    await writeFile15(
      resolve44(logs, "native-profile.json"),
      JSON.stringify(profile, null, 2) + "\n",
      { mode: 384 }
    );
    Object.assign(process.env, profile.env);
    const metadata = JSON.parse(await readFile22(context, "utf8"));
    const criteria = await cohortWorkflow(task, cohort).verify({
      work,
      metadata,
      env: {
        ...Object.fromEntries(
          Object.entries(env).filter(
            (e) => typeof e[1] === "string"
          )
        ),
        ...profile.env
      }
    });
    const passed = criteria.filter((c) => c.passed).length;
    const reward = Number(criteria.length > 0 && passed === criteria.length);
    await writeFile15(
      resolve44(logs, "criteria.json"),
      JSON.stringify(
        {
          task,
          cohort,
          admission_status: workflowCohort(cohort).admission,
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
    await writeFile15(resolve44(logs, "reward.txt"), `${reward}
`);
    return reward;
  } catch (error) {
    await writeFile15(
      resolve44(logs, "criteria.json"),
      JSON.stringify(
        { task, cohort, reward: 0, infrastructure_error: errorMessage(error) },
        null,
        2
      ) + "\n"
    );
    throw error;
  } finally {
    for (const [key3, value] of previousProfileEnv) {
      if (value === void 0) delete process.env[key3];
      else process.env[key3] = value;
    }
  }
}
import { readFile as readFile24 } from "node:fs/promises";
import { mkdir as mkdir6, readFile as readFile23, writeFile as writeFile16 } from "node:fs/promises";
import { resolve as resolve45 } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
async function requestVerification(root = "/app", timeout = 3e4) {
  const session = JSON.parse(
    await readFile23(resolve45(root, "session.json"), "utf8").catch(() => "{}")
  );
  if (!session.verification_shutdown) return;
  const directory = resolve45(root, "verification");
  await mkdir6(directory, { recursive: true, mode: 448 });
  await writeFile16(resolve45(directory, "request"), "stop without saving\n");
  const started = Date.now();
  while (Date.now() - started < timeout) {
    const bytes = await readFile23(resolve45(directory, "ready"), "utf8").catch(
      () => void 0
    );
    if (bytes) {
      const result = JSON.parse(bytes);
      if (!result.ok) throw Error(result.error);
      return;
    }
    await delay(100);
  }
  throw Error("Owned music engine did not stop for saved-state verification");
}
await requestVerification();
Object.assign(
  process.env,
  JSON.parse(await readFile24("/app/service-env.json", "utf8").catch(() => "{}"))
);
await grade(
  process.argv[2],
  process.env.REEF_WORKSPACE ?? "/app/work",
  process.env.REEF_VERIFIER_LOGS ?? "/logs/verifier",
  process.env.REEF_TASK_CONTEXT ?? "/app/context.json",
  process.env,
  process.argv[3] ?? "admitted"
);
