'use strict';

const SIM = (() => {
  const G = 5000;
  const DT = 1 / 240;
  const SUBSTEPS = 4;
  const TICK = DT * SUBSTEPS;
  const SOFTEN = 4;
  const SOFTEN2 = SOFTEN * SOFTEN;
  const PLANET_MIN_R = 16;
  const PLANET_MAX_R = 42;
  const PLANET_COLORS = ['#e8b04b', '#6fb3e8', '#d97b6c', '#8fd6a4', '#c79be0'];
  const MAX_TRAIL = 4000;
  // Planets are static: PLANET_COUNT of them, placed at random points
  // inside the (large) world at init, never moving.
  const FUEL_MAX = 100;
  const FUEL_BURN_PER_SECOND = 15;
  const FUEL_REGEN_PER_SECOND = 0.15;
  const FUEL_BURN_PER_TICK = FUEL_BURN_PER_SECOND * TICK;
  const FUEL_REGEN_PER_TICK = FUEL_REGEN_PER_SECOND * TICK;
  const THRUST_ACCEL = 120;

  const ASTRONAUT_COUNT = 5;
  const ASTRONAUT_RADIUS = 18;
  const ASTRONAUT_ORBIT_R = 60;
  const ASTRONAUT_ORBIT_SPEED = 1.5;

  // Чёрной дыры нет: планеты стоят статично, спутник притягивают только
  // они. Завершение полёта — только столкновение с планетой.
  const BH_X = 1000;
  const BH_Y = 600;

  // Старт — в стороне от центра (на 700 ед. от точки (1000,600), вправо).
  const LAUNCH_X = 1700;
  const LAUNCH_Y = 600;
  const LAUNCH_VX = 0;
  const LAUNCH_VY = 0;

  const world = {
    width: 6000,
    height: 3600,
    planets: [],
    stars: [],
    astronauts: [],
  };

  // How many planets fill the world. More than what fits the initial
  // screen, so the player finds new planets as the ship travels.
  const PLANET_COUNT = 12;

  const satellite = {
    x: 0,
    y: 0,
    vx: 0,
    vy: 0,
    start: null,
    trail: [],
    crashed: false,
    crashedPlanet: null,
    fuel: FUEL_MAX,
    thrustDir: null,
    engineCut: false,
    angle: 0,
    consumed: false,
  };

  let collected = 0;
  let elapsed = 0;
  let gravityOff = false;
  let fuelUnlimited = false;

  function rand(min, max) {
    return min + Math.random() * (max - min);
  }

  // The minimum distance between two planets: 0.1 of the sum of the world
  // width and height (the world stands in for "the screen", so this keeps
  // the planets well spread out across the whole space, not clustered in
  // the initial view).
  const PLANET_MIN_DIST = 0.1 * (world.width + world.height); // 960

  // Create a new planet at a random point inside the (large) world, at
  // least PLANET_MIN_DIST from every other planet and away from the launch
  // position. It is static (no drift).
  function spawnPlanet() {
    const r = rand(PLANET_MIN_R, PLANET_MAX_R);
    const margin = r + 10;
    let x = 0;
    let y = 0;
    for (let tries = 0; tries < 2000; tries++) {
      x = rand(margin, world.width - margin);
      y = rand(margin, world.height - margin);
      // Keep away from the launch position (satellite starts at 1700, 600).
      if (Math.hypot(x - LAUNCH_X, y - LAUNCH_Y) < 250 + r) continue;
      // Keep at least PLANET_MIN_DIST from the other planets.
      let ok = true;
      for (const p of world.planets) {
        if (Math.hypot(x - p.x, y - p.y) < PLANET_MIN_DIST) { ok = false; break; }
      }
      if (ok) break;
    }
    return {
      x, y, r,
      vx: 0,
      vy: 0,
      mass: r * r * 2,
      color: PLANET_COLORS[Math.floor(Math.random() * PLANET_COLORS.length)],
    };
  }

  function makePlanets() {
    // Build into world.planets directly so spawnPlanet() sees each planet
    // as it is placed (the min-distance check references world.planets).
    world.planets = [];
    for (let i = 0; i < PLANET_COUNT; i++) {
      world.planets.push(spawnPlanet());
    }
    return world.planets;
  }

  // Planets are static — they do not drift and are never removed by the
  // simulation. Nothing to advance.
  function stepPlanets() {}

  function maybeSpawnPlanet() {
    // Planets are static and all created up front; nothing to spawn.
  }

  function makeStars() {
    const stars = [];
    const count = Math.round((world.width * world.height) / 4000);
    for (let i = 0; i < count; i++) {
      stars.push({
        x: rand(0, world.width),
        y: rand(0, world.height),
        r: rand(0.4, 1.6),
        a: rand(0.25, 0.9),
      });
    }
    return stars;
  }

  function makeAstronauts() {
    const astronauts = [];
    if (world.planets.length === 0) return astronauts;
    for (let i = 0; i < ASTRONAUT_COUNT; i++) {
      const planetIdx = i % world.planets.length;
      const planet = world.planets[planetIdx];
      const phase = rand(0, Math.PI * 2);
      astronauts.push({
        x: planet.x + Math.cos(phase) * ASTRONAUT_ORBIT_R,
        y: planet.y + Math.sin(phase) * ASTRONAUT_ORBIT_R,
        orbitPlanet: planetIdx,
        orbitR: ASTRONAUT_ORBIT_R,
        orbitPhase: phase,
        r: ASTRONAUT_RADIUS,
      });
    }
    return astronauts;
  }

  function updateAngle() {
    const sp = Math.hypot(satellite.vx, satellite.vy);
    if (sp > 1e-9) {
      satellite.angle = Math.atan2(satellite.vy, satellite.vx);
    }
  }

  function launch() {
    satellite.x = LAUNCH_X;
    satellite.y = LAUNCH_Y;
    satellite.vx = LAUNCH_VX;
    satellite.vy = LAUNCH_VY;
    satellite.start = { x: LAUNCH_X, y: LAUNCH_Y, vx: LAUNCH_VX, vy: LAUNCH_VY };
    satellite.trail = [];
    satellite.crashed = false;
    satellite.crashedPlanet = null;
    satellite.consumed = false;
    satellite.fuel = FUEL_MAX;
    satellite.thrustDir = null;
    satellite.engineCut = false;
    gravityOff = false;
    fuelUnlimited = false;
    elapsed = 0;
    updateAngle();
  }

  function init() {
    world.planets = makePlanets();
    world.stars = makeStars();
    world.astronauts = makeAstronauts();
    collected = 0;
    launch();
  }

  function randomizePlanets() {
    world.planets = makePlanets();
    world.astronauts = makeAstronauts();
    collected = 0;
    launch();
  }

  function resetSatellite() {
    world.astronauts = makeAstronauts();
    collected = 0;
    launch();
  }

  function placeSatellite(x, y, vx, vy) {
    satellite.start = { x, y, vx, vy };
    satellite.x = x;
    satellite.y = y;
    satellite.vx = vx;
    satellite.vy = vy;
    satellite.trail = [];
    satellite.crashed = false;
    satellite.crashedPlanet = null;
    satellite.consumed = false;
    satellite.fuel = FUEL_MAX;
    satellite.thrustDir = null;
    satellite.engineCut = false;
    updateAngle();
  }

  function thrust(dirX, dirY) {
    if (satellite.crashed) return false;
    // With unlimited fuel the tank never empties and the engine never cuts:
    // thrust is always available and nothing is burned.
    if (!fuelUnlimited) {
      if (satellite.engineCut || satellite.fuel <= 0) {
        satellite.engineCut = true;
        satellite.thrustDir = null;
        return false;
      }
    }
    const len = Math.hypot(dirX, dirY);
    if (len < 1e-9) return false;
    const ux = dirX / len;
    const uy = dirY / len;
    if (!fuelUnlimited) {
      const burn = Math.min(FUEL_BURN_PER_TICK, satellite.fuel);
      satellite.fuel -= burn;
      if (satellite.fuel <= 0) {
        satellite.fuel = 0;
        satellite.engineCut = true;
      }
    }
    satellite.vx += ux * THRUST_ACCEL * DT * SUBSTEPS;
    satellite.vy += uy * THRUST_ACCEL * DT * SUBSTEPS;
    satellite.thrustDir = { x: -ux, y: -uy };
    return true;
  }

  function setThrustDir(dirX, dirY) {
    if (dirX === null) {
      satellite.thrustDir = null;
      satellite.engineCut = false;
      return;
    }
    const len = Math.hypot(dirX, dirY);
    if (len < 1e-9) {
      satellite.thrustDir = null;
      return;
    }
    satellite.thrustDir = { x: dirX / len, y: dirY / len };
  }

  function checkCrash() {
    if (satellite.crashed) return;
    for (const p of world.planets) {
      const dx = satellite.x - p.x;
      const dy = satellite.y - p.y;
      if (dx * dx + dy * dy <= p.r * p.r) {
        satellite.crashed = true;
        satellite.crashedPlanet = p;
        return;
      }
    }
  }

  function accelAt(x, y) {
    let ax = 0;
    let ay = 0;
    for (const p of world.planets) {
      const dx = p.x - x;
      const dy = p.y - y;
      const d2 = dx * dx + dy * dy + SOFTEN2;
      const invD3 = 1 / (d2 * Math.sqrt(d2));
      const f = G * p.mass * invD3;
      ax += f * dx;
      ay += f * dy;
    }
    return { ax, ay };
  }

  function rk4Step() {
    // With gravity off (coasting mode) the satellite moves in a straight
    // line at constant velocity — a simple Euler step.
    if (gravityOff) {
      satellite.x += satellite.vx * DT;
      satellite.y += satellite.vy * DT;
      return;
    }
    const { x, y, vx, vy } = satellite;
    const h = DT;
    const a1 = accelAt(x, y);
    const k1x = vx, k1y = vy, k1vx = a1.ax, k1vy = a1.ay;
    const a2 = accelAt(x + k1x * h / 2, y + k1y * h / 2);
    const k2x = vx + k1vx * h / 2, k2y = vy + k1vy * h / 2, k2vx = a2.ax, k2vy = a2.ay;
    const a3 = accelAt(x + k2x * h / 2, y + k2y * h / 2);
    const k3x = vx + k2vx * h / 2, k3y = vy + k2vy * h / 2, k3vx = a3.ax, k3vy = a3.ay;
    const a4 = accelAt(x + k3x * h, y + k3y * h);
    const k4x = vx + k3vx * h, k4y = vy + k3vy * h, k4vx = a4.ax, k4vy = a4.ay;
    satellite.x = x + (h / 6) * (k1x + 2 * k2x + 2 * k3x + k4x);
    satellite.y = y + (h / 6) * (k1y + 2 * k2y + 2 * k3y + k4y);
    satellite.vx = vx + (h / 6) * (k1vx + 2 * k2vx + 2 * k3vx + k4vx);
    satellite.vy = vy + (h / 6) * (k1vy + 2 * k2vy + 2 * k3vy + k4vy);
  }

  function checkAstronauts() {
    if (satellite.crashed) return;
    for (let i = world.astronauts.length - 1; i >= 0; i--) {
      const a = world.astronauts[i];
      const dx = satellite.x - a.x;
      const dy = satellite.y - a.y;
      if (dx * dx + dy * dy <= (a.r + 10) * (a.r + 10)) {
        world.astronauts.splice(i, 1);
        collected++;
      }
    }
  }

  function regenFuel() {
    if (satellite.crashed) return;
    if (fuelUnlimited) {
      satellite.fuel = FUEL_MAX;
      return;
    }
    satellite.fuel = Math.min(FUEL_MAX, satellite.fuel + FUEL_REGEN_PER_TICK);
  }

  function stepAstronauts() {
    for (const a of world.astronauts) {
      a.orbitPhase += ASTRONAUT_ORBIT_SPEED * DT * SUBSTEPS;
      const p = world.planets[a.orbitPlanet];
      if (!p) { a.x = -1000; a.y = -1000; continue; }
      a.x = p.x + Math.cos(a.orbitPhase) * a.orbitR;
      a.y = p.y + Math.sin(a.orbitPhase) * a.orbitR;
    }
  }

  function step() {
    if (satellite.crashed) return;
    elapsed += DT * SUBSTEPS;
    stepPlanets();
    maybeSpawnPlanet();
    for (let i = 0; i < SUBSTEPS; i++) {
      rk4Step();
      checkAstronauts();
      checkCrash();
      if (satellite.crashed) return;
    }
    regenFuel();
    stepAstronauts();
    updateAngle();
    if (satellite.trail.length > 0) {
      const last = satellite.trail[satellite.trail.length - 1];
      const dx = satellite.x - last.x;
      const dy = satellite.y - last.y;
      if (dx * dx + dy * dy > 9) {
        satellite.trail.push({ x: satellite.x, y: satellite.y });
        if (satellite.trail.length > MAX_TRAIL) satellite.trail.shift();
      }
    } else {
      satellite.trail.push({ x: satellite.x, y: satellite.y });
    }
  }

  function speed() { return Math.hypot(satellite.vx, satellite.vy); }
  function isCrashed() { return satellite.crashed; }
  function isConsumed() { return satellite.consumed; }
  function getFuel() { return satellite.fuel; }
  function getFuelMax() { return FUEL_MAX; }
  function isThrusting() {
    return satellite.thrustDir !== null && satellite.fuel > 0 && !satellite.crashed;
  }
  function isEngineCut() { return satellite.engineCut; }
  function getThrustDir() { return satellite.thrustDir; }
  function getAngle() { return satellite.angle; }
  function getAstronauts() { return world.astronauts; }
  function getCollected() { return collected; }
  function getTotalAstronauts() { return ASTRONAUT_COUNT; }
  function getElapsed() { return elapsed; }
  function getWorld() { return world; }
  function getSatellite() { return satellite; }
  function getBlackHole() { return null; }
  function getBlackHoleMass() { return 0; }
  function setGravityOff(on) { gravityOff = !!on; }
  function isGravityOff() { return gravityOff; }
  function setFuelUnlimited(on) { fuelUnlimited = !!on; }
  function isFuelUnlimited() { return fuelUnlimited; }

  return {
    DT, TICK,
    init, randomizePlanets, placeSatellite, resetSatellite,
    thrust, setThrustDir, step,
    speed, isCrashed, isConsumed,
    getFuel, getFuelMax, isThrusting, isEngineCut,
    getThrustDir, getAngle,
    getAstronauts, getCollected, getTotalAstronauts, getElapsed,
    getWorld, getSatellite, getBlackHole, getBlackHoleMass,
    setGravityOff, isGravityOff,
    setFuelUnlimited, isFuelUnlimited,
  };
})();
