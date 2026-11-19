'use strict';

const SIM = (() => {
  const STORAGE_KEY = 'satellite-sim.planets.v1';

  const G = 5000;
  const DT = 1 / 240;
  const SUBSTEPS = 4;
  const SOFTEN = 4;
  const SOFTEN2 = SOFTEN * SOFTEN;
  const PLANET_MIN_R = 16;
  const PLANET_MAX_R = 42;
  const PLANET_COLORS = ['#e8b04b', '#6fb3e8', '#d97b6c', '#8fd6a4', '#c79be0'];
  const MAX_TRAIL = 4000;
  const FUEL_MAX = 100;
  const FUEL_BURN_PER_FRAME = 0.15;
  const FUEL_REGEN_PER_FRAME = 0.01;
  const THRUST_ACCEL = 120;
  const ASTRONAUT_COUNT = 5;
  const ASTRONAUT_RADIUS = 18;

  const PORTAL_X = 1880;
  const PORTAL_Y = 120;
  const PORTAL_RADIUS = 45;

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
    angle: 0,
  };

  let collected = 0;
  let elapsed = 0;
  let gameWon = false;

  function rand(min, max) {
    return min + Math.random() * (max - min);
  }

  function makePlanets() {
    const planets = [];
    for (let i = 0; i < 3; i++) {
      const r = rand(PLANET_MIN_R, PLANET_MAX_R);
      planets.push({
        x: rand(r + 60, world.width - r - 60),
        y: rand(r + 60, world.height - r - 60),
        r,
        mass: r * r * 2,
        color: PLANET_COLORS[i % PLANET_COLORS.length],
      });
    }
    return planets;
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

  const ASTRONAUT_ORBIT_R = 60;
  const ASTRONAUT_ORBIT_SPEED = 1.5;

  function makeAstronauts() {
    const astronauts = [];
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

  function savePlanets() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(world.planets));
    } catch {
      /* хранилище недоступно — игнорируем */
    }
  }

  function loadPlanets() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return false;
      const list = JSON.parse(raw);
      if (!Array.isArray(list) || list.length !== 3) return false;
      for (const p of list) {
        if (typeof p.x !== 'number' || typeof p.y !== 'number' ||
            typeof p.r !== 'number' || typeof p.mass !== 'number' ||
            typeof p.color !== 'string') return false;
        if (!(p.x > 0 && p.x < world.width && p.y > 0 && p.y < world.height)) {
          return false;
        }
      }
      world.planets = list;
      return true;
    } catch {
      return false;
    }
  }

  const LAUNCH_X = 1000;
  const LAUNCH_Y = 1100;
  const LAUNCH_VX = 0;
  const LAUNCH_VY = -80;

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
    satellite.fuel = FUEL_MAX;
    satellite.thrustDir = null;
    elapsed = 0;
    gameWon = false;
    updateAngle();
  }

  function init() {
    if (!loadPlanets()) {
      world.planets = makePlanets();
      savePlanets();
    }
    world.stars = makeStars();
    world.astronauts = makeAstronauts();
    collected = 0;
    launch();
  }

  function randomizePlanets() {
    world.planets = makePlanets();
    world.astronauts = makeAstronauts();
    collected = 0;
    savePlanets();
    launch();
  }

  function resetSatellite() {
    world.astronauts = makeAstronauts();
    collected = 0;
    launch();
  }

  // For testing: place the satellite at an arbitrary position with a
  // given velocity. Resets fuel and crash state. Not used in the UI.
  function placeSatellite(x, y, vx, vy) {
    satellite.start = { x, y, vx, vy };
    satellite.x = x;
    satellite.y = y;
    satellite.vx = vx;
    satellite.vy = vy;
    satellite.trail = [];
    satellite.crashed = false;
    satellite.crashedPlanet = null;
    satellite.fuel = FUEL_MAX;
    satellite.thrustDir = null;
    updateAngle();
  }

  // dirX, dirY — direction of acceleration in world coordinates (where the
  // satellite's nose points after thrust). Exhaust comes out opposite.
  function thrust(dirX, dirY) {
    if (satellite.crashed || satellite.fuel <= 0) return false;
    const len = Math.hypot(dirX, dirY);
    if (len < 1e-9) return false;
    const ux = dirX / len;
    const uy = dirY / len;
    const burn = Math.min(FUEL_BURN_PER_FRAME, satellite.fuel);
    satellite.fuel -= burn;
    satellite.vx += ux * THRUST_ACCEL * DT * SUBSTEPS;
    satellite.vy += uy * THRUST_ACCEL * DT * SUBSTEPS;
    satellite.thrustDir = { x: -ux, y: -uy };
    return true;
  }

  function setThrustDir(dirX, dirY) {
    if (dirX === null) {
      satellite.thrustDir = null;
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
    satellite.fuel = Math.min(FUEL_MAX, satellite.fuel + FUEL_REGEN_PER_FRAME);
  }

  // Astronauts orbit their assigned planet on a small fixed circle.
  function stepAstronauts() {
    for (const a of world.astronauts) {
      a.orbitPhase += ASTRONAUT_ORBIT_SPEED * DT * SUBSTEPS;
      const p = world.planets[a.orbitPlanet];
      a.x = p.x + Math.cos(a.orbitPhase) * a.orbitR;
      a.y = p.y + Math.sin(a.orbitPhase) * a.orbitR;
    }
  }

  function checkPortal() {
    if (gameWon) return;
    const dx = satellite.x - PORTAL_X;
    const dy = satellite.y - PORTAL_Y;
    if (dx * dx + dy * dy <= PORTAL_RADIUS * PORTAL_RADIUS) {
      gameWon = true;
    }
  }

  function step() {
    if (satellite.crashed || gameWon) return;
    elapsed += DT * SUBSTEPS;
    for (let i = 0; i < SUBSTEPS; i++) {
      rk4Step();
      checkAstronauts();
      checkCrash();
      if (satellite.crashed) return;
      checkPortal();
      if (gameWon) return;
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

  function isGameWon() {
    return gameWon;
  }

  function getPortal() {
    return { x: PORTAL_X, y: PORTAL_Y, r: PORTAL_RADIUS };
  }

  function getWorld() {
    return world;
  }

  function getSatellite() {
    return satellite;
  }

  return {
    DT,
    init,
    randomizePlanets,
    placeSatellite,
    resetSatellite,
    thrust,
    setThrustDir,
    step,
    speed,
    isCrashed,
    isGameWon,
    getFuel,
    getFuelMax,
    isThrusting,
    getThrustDir,
    getAngle,
    getAstronauts,
    getCollected,
    getTotalAstronauts,
    getElapsed,
    getPortal,
    getWorld,
    getSatellite,
    STORAGE_KEY,
  };
})();
