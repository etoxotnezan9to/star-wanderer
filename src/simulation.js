'use strict';

const SIM = (() => {
  const G = 5000;
  const DT = 1 / 240;
  const SUBSTEPS = 4;
  // Simulated time advanced by one step(): 1/60 s of game time.
  const TICK = DT * SUBSTEPS;
  const SOFTEN = 4;
  const SOFTEN2 = SOFTEN * SOFTEN;
  const PLANET_MIN_R = 16;
  const PLANET_MAX_R = 42;
  const PLANET_COLORS = ['#e8b04b', '#6fb3e8', '#d97b6c', '#8fd6a4', '#c79be0'];
  const MAX_TRAIL = 4000;
  // Planets spawn in the upper-right corner and drift towards the black hole
  // at a constant velocity (uniform motion — only the satellite feels gravity).
  const PLANET_SPAWN_X = 1900;
  const PLANET_SPAWN_Y = 120;
  const PLANET_SPAWN_SPREAD = 250;
  const PLANET_DRIFT_SPEED = 12; // units per second of game time
  const PLANET_MAX_COUNT = 6;
  const PLANET_SPAWN_INTERVAL = 3; // seconds of game time between spawns

  // Fuel tuning.
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

  // Чёрная дыра: стоит в левом нижнем углу мира и постепенно растёт.
  // Планеты генерируются в правом верхнем углу и равномерно дрейфуют к ней.
  const BH_X = 60;
  const BH_Y = 1140;
  const BH_MASS = 100;
  const BH_INITIAL_R = 20;
  const BH_GROWTH_PER_SECOND = 1.2;

  const LAUNCH_X = 1000;
  const LAUNCH_Y = 1100;
  const LAUNCH_VX = 0;
  const LAUNCH_VY = -80;

  const world = {
    width: 2000,
    height: 1200,
    planets: [],
    stars: [],
    astronauts: [],
  };

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
  let blackHoleRadius = BH_INITIAL_R;

  function rand(min, max) {
    return min + Math.random() * (max - min);
  }

  // Create a new planet in the upper-right corner with a drift velocity
  // towards the black hole (constant speed).
  function spawnPlanet() {
    const r = rand(PLANET_MIN_R, PLANET_MAX_R);
    const x = PLANET_SPAWN_X - rand(0, PLANET_SPAWN_SPREAD);
    const y = PLANET_SPAWN_Y + rand(0, PLANET_SPAWN_SPREAD);
    const dx = BH_X - x;
    const dy = BH_Y - y;
    const d = Math.hypot(dx, dy);
    return {
      x, y, r,
      vx: (dx / d) * PLANET_DRIFT_SPEED,
      vy: (dy / d) * PLANET_DRIFT_SPEED,
      mass: r * r * 2,
      color: PLANET_COLORS[Math.floor(Math.random() * PLANET_COLORS.length)],
    };
  }

  function makePlanets() {
    const planets = [];
    for (let i = 0; i < 3; i++) {
      planets.push(spawnPlanet());
    }
    return planets;
  }

  // Advance planets by their drift velocity. Remove planets absorbed by the
  // black hole or that have left the world far outside.
  function stepPlanets() {
    const dt = DT * SUBSTEPS;
    for (let i = world.planets.length - 1; i >= 0; i--) {
      const p = world.planets[i];
      p.x += p.vx * dt;
      p.y += p.vy * dt;
      // Remove if swallowed by the black hole (center within BH radius + planet r)
      const dx = p.x - BH_X;
      const dy = p.y - BH_Y;
      if (dx * dx + dy * dy <= (blackHoleRadius + p.r * 0.5) * (blackHoleRadius + p.r * 0.5)) {
        world.planets.splice(i, 1);
        continue;
      }
      // Remove if far outside the world
      if (p.x < -200 || p.y > world.height + 200 ||
          p.x > world.width + 200 || p.y < -200) {
        world.planets.splice(i, 1);
      }
    }
  }

  // Spawn a new planet if the cap allows and the interval has elapsed.
  function maybeSpawnPlanet() {
    if (world.planets.length >= PLANET_MAX_COUNT) return;
    const expected = Math.floor(elapsed / PLANET_SPAWN_INTERVAL) + 3;
    if (world.planets.length < expected) {
      world.planets.push(spawnPlanet());
    }
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
    elapsed = 0;
    blackHoleRadius = BH_INITIAL_R;
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
    blackHoleRadius = BH_INITIAL_R;
    updateAngle();
  }

  function thrust(dirX, dirY) {
    if (satellite.crashed) return false;
    if (satellite.engineCut || satellite.fuel <= 0) {
      satellite.engineCut = true;
      satellite.thrustDir = null;
      return false;
    }
    const len = Math.hypot(dirX, dirY);
    if (len < 1e-9) return false;
    const ux = dirX / len;
    const uy = dirY / len;
    const burn = Math.min(FUEL_BURN_PER_TICK, satellite.fuel);
    satellite.fuel -= burn;
    if (satellite.fuel <= 0) {
      satellite.fuel = 0;
      satellite.engineCut = true;
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
    const bx = BH_X - x;
    const by = BH_Y - y;
    const bd2 = bx * bx + by * by + SOFTEN2;
    const binvD3 = 1 / (bd2 * Math.sqrt(bd2));
    const bf = G * BH_MASS * binvD3;
    ax += bf * bx;
    ay += bf * by;
    return { ax, ay };
  }

  function rk4Step() {
    const { x, y, vx, vy } = satellite;
    const h = DT;

    const a1 = accelAt(x, y);
    const k1x = vx;
    const k1y = vy;
    const k1vx = a1.ax;
    const k1vy = a1.ay;

    const a2 = accelAt(x + k1x * h / 2, y + k1y * h / 2);
    const k2x = vx + k1vx * h / 2;
    const k2y = vy + k1vy * h / 2;
    const k2vx = a2.ax;
    const k2vy = a2.ay;

    const a3 = accelAt(x + k2x * h / 2, y + k2y * h / 2);
    const k3x = vx + k2vx * h / 2;
    const k3y = vy + k2vy * h / 2;
    const k3vx = a3.ax;
    const k3vy = a3.ay;

    const a4 = accelAt(x + k3x * h, y + k3y * h);
    const k4x = vx + k3vx * h;
    const k4y = vy + k3vy * h;
    const k4vx = a4.ax;
    const k4vy = a4.ay;

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
    satellite.fuel = Math.min(FUEL_MAX, satellite.fuel + FUEL_REGEN_PER_TICK);
  }

  function stepAstronauts() {
    for (const a of world.astronauts) {
      a.orbitPhase += ASTRONAUT_ORBIT_SPEED * DT * SUBSTEPS;
      const p = world.planets[a.orbitPlanet];
      if (!p) {
        a.x = -1000;
        a.y = -1000;
        continue;
      }
      a.x = p.x + Math.cos(a.orbitPhase) * a.orbitR;
      a.y = p.y + Math.sin(a.orbitPhase) * a.orbitR;
    }
  }

  function checkBlackHole() {
    if (satellite.crashed || satellite.consumed) return;
    const dx = satellite.x - BH_X;
    const dy = satellite.y - BH_Y;
    if (dx * dx + dy * dy <= blackHoleRadius * blackHoleRadius) {
      satellite.consumed = true;
    }
  }

  function step() {
    if (satellite.crashed || satellite.consumed) return;
    elapsed += DT * SUBSTEPS;
    blackHoleRadius = BH_INITIAL_R + BH_GROWTH_PER_SECOND * elapsed;
    stepPlanets();
    maybeSpawnPlanet();
    for (let i = 0; i < SUBSTEPS; i++) {
      rk4Step();
      checkAstronauts();
      checkCrash();
      if (satellite.crashed) return;
      checkBlackHole();
      if (satellite.consumed) return;
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
        if (satellite.trail.length > MAX_TRAIL) {
          satellite.trail.shift();
        }
      }
    } else {
      satellite.trail.push({ x: satellite.x, y: satellite.y });
    }
  }

  function speed() {
    return Math.hypot(satellite.vx, satellite.vy);
  }

  function isCrashed() {
    return satellite.crashed;
  }

  function getFuel() {
    return satellite.fuel;
  }

  function getFuelMax() {
    return FUEL_MAX;
  }

  function isThrusting() {
    return satellite.thrustDir !== null && satellite.fuel > 0 && !satellite.crashed;
  }

  function isEngineCut() {
    return satellite.engineCut;
  }

  function getThrustDir() {
    return satellite.thrustDir;
  }

  function getAngle() {
    return satellite.angle;
  }

  function getAstronauts() {
    return world.astronauts;
  }

  function getCollected() {
    return collected;
  }

  function getTotalAstronauts() {
    return ASTRONAUT_COUNT;
  }

  function getElapsed() {
    return elapsed;
  }

  function isConsumed() {
    return satellite.consumed;
  }

  function getBlackHoleRadius() {
    return blackHoleRadius;
  }

  function getBlackHole() {
    return { x: BH_X, y: BH_Y, mass: BH_MASS, r: blackHoleRadius };
  }

  function getWorld() {
    return world;
  }

  function getSatellite() {
    return satellite;
  }

  return {
    DT,
    TICK,
    init,
    randomizePlanets,
    placeSatellite,
    resetSatellite,
    thrust,
    setThrustDir,
    step,
    speed,
    isCrashed,
    isConsumed,
    getFuel,
    getFuelMax,
    isThrusting,
    isEngineCut,
    getThrustDir,
    getAngle,
    getAstronauts,
    getCollected,
    getTotalAstronauts,
    getElapsed,
    getBlackHoleRadius,
    getBlackHole,
    getWorld,
    getSatellite,
  };
})();
