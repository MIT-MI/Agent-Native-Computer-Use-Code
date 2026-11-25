import { readFile } from "node:fs/promises";
import { performance } from "node:perf_hooks";

export const layout = JSON.parse(
  await readFile(new URL("./re-layout.json", import.meta.url), "utf8"),
);
const address = (value) => Number(value);

const G = layout.globals;
const GLOBALS_BASE = address(G.block_base_address);
const globalOffset = (addr) => address(addr) - GLOBALS_BASE;
const OFF_PAUSED = globalOffset(G.paused_u8_address);
const OFF_MODE = globalOffset(G.mode_i32_address);
const OFF_ENDED = globalOffset(G.round_ended_u8_address);
const OFF_SCORE = globalOffset(G.score_i32_address);
const OFF_CAMERA = globalOffset(G.camera_pointer_address);
const OFF_CONFIG = globalOffset(G.config_pointer_address);
const OFF_GAME_OVER = globalOffset(G.game_over_screen_pointer_address);
const SIM_TICKS_ADDR = address(G.simulation_ticks_u32_address);
const REMAINING_OFFSET = G.remaining_time.offset;
const CAM = layout.camera;
const CAM_FX = CAM.effects;
const MOTION = layout.motion;
const ENT = layout.entities;
const COMMON = layout.common;

/** Read the active native lists recovered with REA; never writes target memory. */
export class NativeState {
  constructor(helper) {
    this.helper = helper;
    this.started = false;
    this.clockHistory = [];
    this.observationQueue = Promise.resolve();
  }

  async read(pointer, length) {
    const result = await this.helper.request(`read ${pointer} ${length}`);
    if (!result.ok)
      throw new Error(
        `ReadProcessMemory ${pointer.toString(16)}: ${result.error}`,
      );
    return Buffer.from(result.hex, "hex");
  }

  observe() {
    const operation = this.observationQueue.then(async () => {
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const state = await this.readSnapshot();
          if (attempt) state.warnings.push("snapshot-retried");
          return state;
        } catch (error) {
          if (
            !/read-failed|Native entity snapshot changed|Invalid native entity list/.test(
              error.message,
            ) ||
            attempt === 2
          )
            throw error;
          await new Promise((resolve) => setTimeout(resolve, 8));
        }
      }
    });
    this.observationQueue = operation.catch(() => {});
    return operation;
  }

  async readSnapshot() {
    const begun = performance.now();
    const info = await this.helper.request("info");
    if (!info.alive || !info.window)
      throw new Error("Game process/window is unavailable");
    const window = info.window;
    const aspect = window.width / window.height;
    const W =
      aspect > CAM.aspect_threshold
        ? CAM.wide_height * aspect
        : CAM.narrow_width;
    const H =
      aspect > CAM.aspect_threshold
        ? CAM.wide_height
        : CAM.narrow_width / aspect;
    const canvas = { ...window, W, H };
    delete canvas.hwnd;
    const globals = await this.read(GLOBALS_BASE, G.block_size);
    const cameraPointer = globals.readUInt32LE(OFF_CAMERA);
    let camera;
    if (cameraPointer) {
      const bytes = await this.read(cameraPointer, CAM.struct_size);
      const active = bytes.readFloatLE(CAM_FX.active_f32_offset) > 0;
      camera = {
        active,
        zoom: active ? bytes.readFloatLE(CAM_FX.zoom_f32_offset) : 1,
        angle: active
          ? ((bytes.readUInt16LE(CAM_FX.rotation_u16_offset) >>> 4) *
              2 *
              Math.PI) /
            4096
          : 0,
        x: active
          ? bytes.readFloatLE(CAM_FX.position_f32_offsets[0]) -
            bytes.readFloatLE(CAM_FX.shake_f32_offsets[0])
          : 0,
        y: active
          ? bytes.readFloatLE(CAM_FX.position_f32_offsets[1]) -
            bytes.readFloatLE(CAM_FX.shake_f32_offsets[1])
          : 0,
      };
      canvas.camera = camera;
    }
    const resultScreen = globals.readUInt32LE(OFF_GAME_OVER) !== 0;
    // 0x5b2b90 is cleared by the render callback. The integer tick counter is
    // cumulative; each update adds trunc(dt * 1000), so it has frame rounding.
    const simTime = (await this.read(SIM_TICKS_ADDR, 4)).readUInt32LE() / 1000;
    const clockAt = performance.now();
    this.clockHistory = this.clockHistory.filter(
      (sample) => clockAt - sample.wall < 1500 && sample.sim <= simTime,
    );
    this.clockHistory.push({ wall: clockAt, sim: simTime });
    const oldClock = this.clockHistory[0];
    const span = (clockAt - oldClock.wall) / 1000;
    const simulationRate =
      span >= 0.4
        ? Math.max(0.01, Math.min(1.2, (simTime - oldClock.sim) / span))
        : MOTION.default_simulation_rate;
    const config = globals.readUInt32LE(OFF_CONFIG);
    const remaining = config
      ? (await this.read(config + REMAINING_OFFSET, 4)).readFloatLE()
      : null;
    // The evaluator may initialize Arcade for either interface. GUI actors
    // still receive pixels only; the private referee recognizes native start.
    if (
      !resultScreen &&
      globals[OFF_ENDED] === 0 &&
      globals.readInt32LE(OFF_MODE) === G.arcade_mode_index &&
      remaining > 0 &&
      remaining <= G.arcade_duration_seconds
    )
      this.started = true;
    const root = (
      await this.read(address(ENT.list_descriptors_pointer_address), 4)
    ).readUInt32LE();
    if (root < 0x10000) throw new Error("Native entity manager is not ready");
    const descriptorBytes = ENT.descriptor_stride * 2;
    const descriptors = await this.read(root, descriptorBytes);
    const groups = { fruits: [], bombs: [] };
    const warnings = [];
    for (const [spec, name] of [
      [layout.fruit, "fruits"],
      [layout.bomb, "bombs"],
    ]) {
      const type = spec.type_index;
      const isBomb = name === "bombs";
      const sentinel = descriptors.readUInt32LE(
        type * ENT.descriptor_stride + ENT.descriptor_head_offset,
      );
      const count = descriptors.readUInt32LE(
        type * ENT.descriptor_stride + ENT.descriptor_count_offset,
      );
      if (count > 256 || sentinel < 0x10000)
        throw new Error("Invalid native entity list");
      let node = (await this.read(sentinel, 4)).readUInt32LE();
      const seen = new Set();
      for (let index = 0; index < count && node !== sentinel; index++) {
        if (seen.has(node) || node < 0x10000)
          throw new Error("Native entity snapshot changed");
        seen.add(node);
        const links = await this.read(node, ENT.node_size);
        const ptr = links.readUInt32LE(ENT.node_entity_offset);
        node = links.readUInt32LE(ENT.node_next_offset);
        if (ptr < 0x10000) throw new Error("Native entity snapshot changed");
        const entity = await this.read(ptr, spec.allocation_size);
        if (entity.readUInt32LE() !== address(spec.vtable_address)) continue;
        const flags = entity.readUInt32LE(COMMON.flags_offset);
        if (flags & COMMON.pending_removal_mask) continue;
        if (entity[isBomb ? spec.exploded_u8_offset : spec.sliced_u8_offset])
          continue;
        if (
          this.started &&
          !resultScreen &&
          !isBomb &&
          entity.readFloatLE(spec.slice_pending_f32_offset) > -1
        )
          continue;
        if (entity.readFloatLE(spec.spawn_delay_f32_offset) > 0) continue;
        const collider = entity.readUInt32LE(COMMON.collider_pointer_offset);
        if (!collider) continue;
        const radius = (
          await this.read(collider + COMMON.collider_radius_f32_offset, 4)
        ).readFloatLE();
        const [x, y, z] = COMMON.position_f32_offsets.map((offset) =>
          entity.readFloatLE(offset),
        );
        const [vx, vy] = COMMON.velocity_f32_offsets.map((offset) =>
          entity.readFloatLE(offset),
        );
        const scale = entity.readFloatLE(spec.time_scale_f32_offset);
        const moving =
          Boolean(entity[spec.physics_enabled_u8_offset]) &&
          !(!isBomb && entity[spec.frozen_u8_offset]);
        const ay = entity.readFloatLE(spec.acceleration_f32_offsets[1]);
        if (
          ![x, y, z, vx, vy, radius, scale, ay].every(Number.isFinite) ||
          radius <= 0 ||
          radius > 200
        )
          continue;
        groups[name].push({
          id: `0x${ptr.toString(16)}`,
          kind: !isBomb ? entity[spec.kind_u8_offset] : null,
          z,
          ...(!isBomb
            ? {
                wave: entity.readInt32LE(spec.combo_group_i32_offset),
                reward: 1,
                powerup: 0,
              }
            : {}),
          x: x + W / 2,
          y: H / 2 - y,
          r: radius,
          vx: moving ? vx * MOTION.position_factor * scale * simulationRate : 0,
          vy: moving
            ? -vy * MOTION.position_factor * scale * simulationRate
            : 0,
          gravity: moving
            ? -ay * MOTION.position_factor * (scale * simulationRate) ** 2
            : 0,
          age_ms: 0,
          moving,
          sampled_at: performance.now(),
        });
      }
    }
    // Reject a torn list rather than planning against missing bomb entries.
    if (!descriptors.equals(await this.read(root, descriptorBytes)))
      throw new Error("Native entity snapshot changed");
    const now = performance.now();
    for (const group of Object.values(groups))
      for (const entity of group) {
        entity.age_ms = now - entity.sampled_at;
        delete entity.sampled_at;
      }
    return {
      now,
      phase: resultScreen
        ? "result"
        : this.started
          ? globals[OFF_PAUSED]
            ? "paused"
            : "playing"
          : "menu",
      mode: G.modes[globals.readInt32LE(OFF_MODE)] ?? "unknown",
      score: globals.readInt32LE(OFF_SCORE),
      remaining: Number.isFinite(remaining) ? remaining : null,
      gravity: Math.abs(MOTION.default_gravity_world_per_second_squared),
      canvas,
      ...groups,
      observation_ms: now - begun,
      simulation_rate: simulationRate,
      warnings,
    };
  }
}
