'use strict';

const SIM = (() => {
  const DT = 1 / 240;
  const SUBSTEPS = 4;
  const TICK = DT * SUBSTEPS;
  const PLANET_MIN_R = 16;
  const PLANET_MAX_R = 42;
  const PLANET_COLORS = ['#e8b04b', '#6fb3e8', '#d97b6c', '#8fd6a4', '#c79be0'];
  const MAX_TRAIL = 4000;

  // The satellite's speed magnitude is ALWAYS constant — it never
  // accelerates or decelerates. Arrows change direction, not speed.
  const SPEED = 400; // units per second
  // Maximum angular velocity in orbit: 1 revolution per 3 seconds.
  // If the orbit radius is so small that SPEED would give a higher angular
  // velocity, the linear speed is reduced to orbitR * MAX_OMEGA.
  const MAX_OMEGA = 2 * Math.PI / 3; // rad/s (period = 3 s)

  // Each planet's attraction circle radius is a multiple of its visible
  // radius. When the satellite passes close enough (the closest-approach
  // point is inside this circle) it is captured into orbit.
  const ATTRACTION_MULT = 16; // attractionR = ATTRACTION_MULT * planet.r

  const FUEL_MAX = 100;
  const FUEL_BURN_PER_SECOND = 15;
  const FUEL_REGEN_PER_SECOND = 0.15;
  const FUEL_BURN_PER_TICK = FUEL_BURN_PER_SECOND * TICK;
  const FUEL_REGEN_PER_TICK = FUEL_REGEN_PER_SECOND * TICK;

  const ASTRONAUT_COUNT = 5;
  const ASTRONAUT_RADIUS = 18;
  const ASTRONAUT_ORBIT_R = 180;
  const ASTRONAUT_ORBIT_SPEED = 1.5;

  // Старт — справа от центра мира, летит вправо на крейсерской скорости.
  const LAUNCH_X = 1700;
  const LAUNCH_Y = 600;
  const LAUNCH_VX = SPEED;
  const LAUNCH_VY = 0;

  const world = {
    width: 6000,
    height: 3600,
    planets: [],
    stars: [],
    astronauts: [],
  };

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
    escaped: false,
    won: false,
    fuel: FUEL_MAX,
    thrustDir: null,
    engineCut: false,
    angle: 0,
    consumed: false,
    // New state-machine fields:
    state: 'flying',      // 'flying' | 'orbiting'
    orbitPlanet: -1,      // index into world.planets, or -1
    orbitR: 0,            // orbit radius (distance from planet center)
    orbitAngle: 0,        // current angle in the orbit (radians)
    orbitDir: 1,          // +1 = counter-clockwise, -1 = clockwise
  };

  let collected = 0;
  let elapsed = 0;
  let fuelUnlimited = false;
  // Planet index to ignore for re-capture after breakOrbit. Reset when the
  // satellite leaves that planet's attraction circle.
  let ignoredPlanet = -1;

  function rand(min, max) {
    return min + Math.random() * (max - min);
  }

  const PLANET_MIN_DIST = 0.02 * (world.width + world.height); // 192

  function spawnPlanet() {
    const r = rand(PLANET_MIN_R, PLANET_MAX_R);
    const margin = r + 10;
    let x = 0;
    let y = 0;
    for (let tries = 0; tries < 2000; tries++) {
      x = rand(margin, world.width - margin);
      y = rand(margin, world.height - margin);
      if (Math.hypot(x - LAUNCH_X, y - LAUNCH_Y) < 250 + r) continue;
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
    world.planets = [];
    for (let i = 0; i < PLANET_COUNT; i++) {
      world.planets.push(spawnPlanet());
    }
    return world.planets;
  }

  function stepPlanets() {}
  function maybeSpawnPlanet() {}

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
    satellite.angle = Math.atan2(satellite.vy, satellite.vx);
  }

  // Set the satellite's velocity to a given direction at constant speed.
  function setVelDir(dirX, dirY) {
    const len = Math.hypot(dirX, dirY);
    if (len < 1e-9) return;
    satellite.vx = (dirX / len) * SPEED;
    satellite.vy = (dirY / len) * SPEED;
    updateAngle();
  }

  // Compute the attraction radius of a planet.
  function attractionRadius(p) {
    return ATTRACTION_MULT * p.r;
  }

  // Capture the satellite into orbit around planet p. The orbit radius is
  // the current distance from the planet center. The orbit direction is
  // determined by the cross product of (radial) x (velocity).
  function captureOrbit(p) {
    const dx = satellite.x - p.x;
    const dy = satellite.y - p.y;
    const dist = Math.hypot(dx, dy);
    satellite.orbitPlanet = world.planets.indexOf(p);
    satellite.orbitR = Math.max(dist, p.r + 1); // don't orbit inside the planet
    satellite.orbitAngle = Math.atan2(dy, dx);
    // Cross product z-component: (dx,dy) x (vx,vy) = dx*vy - dy*vx
    const cross = dx * satellite.vy - dy * satellite.vx;
    satellite.orbitDir = cross >= 0 ? 1 : -1;
    satellite.state = 'orbiting';
  }

  // In FLYING mode, check if the satellite is inside any planet's
  // attraction circle AND is at or past the closest-approach point
  // (radial velocity >= 0). If so, capture into orbit. The planet just
  // broken from (ignoredPlanet) is skipped until the satellite leaves its
  // attraction circle. Returns true if captured.
  function checkFlyingCapture() {
    // Clear the ignore flag once the satellite has left the ignored
    // planet's attraction circle.
    if (ignoredPlanet >= 0) {
      const ip = world.planets[ignoredPlanet];
      if (ip) {
        const d = Math.hypot(satellite.x - ip.x, satellite.y - ip.y);
        if (d >= attractionRadius(ip)) {
          ignoredPlanet = -1;
        }
      } else {
        ignoredPlanet = -1;
      }
    }

    let bestPlanet = null;
    let bestRatio = Infinity;

    for (let i = 0; i < world.planets.length; i++) {
      if (i === ignoredPlanet) continue;
      const p = world.planets[i];
      const ar = attractionRadius(p);
      const dx = satellite.x - p.x;
      const dy = satellite.y - p.y;
      const dist = Math.hypot(dx, dy);
      if (dist >= ar) continue; // outside the attraction circle
      if (dist < p.r) continue; // would crash, not capture
      // Radial velocity: d(dist)/dt = (r · v) / |r|. Negative = approaching,
      // zero = at closest approach, positive = receding. Capture when the
      // satellite is at or past the closest approach (vr >= 0) and still
      // inside the attraction circle.
      const invDist = 1 / dist;
      const vr = (satellite.vx * dx + satellite.vy * dy) * invDist;
      if (vr < 0) continue; // still approaching, not captured yet
      const ratio = dist / ar;
      if (ratio < bestRatio) {
        bestRatio = ratio;
        bestPlanet = p;
      }
    }

    if (bestPlanet) {
      captureOrbit(bestPlanet);
      return true;
    }
    return false;
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
    satellite.escaped = false;
    satellite.won = false;
    satellite.consumed = false;
    satellite.fuel = FUEL_MAX;
    satellite.thrustDir = null;
    satellite.engineCut = false;
    fuelUnlimited = false;
    elapsed = 0;
    satellite.state = 'flying';
    satellite.orbitPlanet = -1;
    satellite.orbitR = 0;
    satellite.orbitAngle = 0;
    satellite.orbitDir = 1;
    ignoredPlanet = -1;
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
    // Normalize to constant speed
    const len = Math.hypot(vx, vy);
    if (len > 1e-9) {
      vx = (vx / len) * SPEED;
      vy = (vy / len) * SPEED;
    } else {
      vx = SPEED;
      vy = 0;
    }
    satellite.start = { x, y, vx, vy };
    satellite.x = x;
    satellite.y = y;
    satellite.vx = vx;
    satellite.vy = vy;
    satellite.trail = [];
    satellite.crashed = false;
    satellite.crashedPlanet = null;
    satellite.escaped = false;
    satellite.won = false;
    satellite.consumed = false;
    satellite.fuel = FUEL_MAX;
    satellite.thrustDir = null;
    satellite.engineCut = false;
    satellite.state = 'flying';
    satellite.orbitPlanet = -1;
    satellite.orbitR = 0;
    satellite.orbitAngle = 0;
    satellite.orbitDir = 1;
    ignoredPlanet = -1;
    updateAngle();
  }

  // Thrust now changes DIRECTION only (speed is always constant).
  // dirX/dirY is the desired new direction.
  function thrust(dirX, dirY) {
    if (satellite.crashed) return false;
    if (satellite.state === 'orbiting') return false; // no steering in orbit
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
    // Change direction only — speed stays constant
    setVelDir(ux, uy);
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

  // Break out of orbit: satellite goes to FLYING mode, moving tangentially
  // in its current velocity direction (which is already tangential). The
  // planet just broken from is ignored (no re-capture) until the satellite
  // leaves its attraction circle.
  // Break out of orbit: satellite goes to FLYING mode, moving tangentially
  // at full SPEED. If the orbit was slow (small radius, angular-speed cap),
  // the satellite accelerates back to SPEED in the tangential direction.
  // The planet just broken from is ignored (no re-capture) until the
  // satellite leaves its attraction circle.
  function breakOrbit() {
    if (satellite.state !== 'orbiting') return false;
    // Direction of the current tangential velocity
    const sp = Math.hypot(satellite.vx, satellite.vy);
    let dx = satellite.vx;
    let dy = satellite.vy;
    if (sp < 1e-9) {
      // Fallback: use the orbit tangent
      dx = -satellite.orbitDir * Math.sin(satellite.orbitAngle);
      dy = satellite.orbitDir * Math.cos(satellite.orbitAngle);
    } else {
      dx /= sp;
      dy /= sp;
    }
    // Accelerate back to full SPEED in the tangential direction
    satellite.vx = dx * SPEED;
    satellite.vy = dy * SPEED;
    satellite.state = 'flying';
    ignoredPlanet = satellite.orbitPlanet;
    satellite.orbitPlanet = -1;
    updateAngle();
    return true;
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

  // --- New physics step ---

  function physicsStep() {
    if (satellite.state === 'flying') {
      // Move straight at constant speed
      satellite.x += satellite.vx * DT;
      satellite.y += satellite.vy * DT;
      // Check for capture by any planet's attraction circle
      checkFlyingCapture();
    } else if (satellite.state === 'orbiting') {
      const p = world.planets[satellite.orbitPlanet];
      if (!p) {
        // Planet gone (shouldn't happen, but safety)
        satellite.state = 'flying';
        satellite.orbitPlanet = -1;
        return;
      }
      // Effective orbital speed: capped so the angular velocity never
      // exceeds MAX_OMEGA. For small orbits this is lower than SPEED.
      const orbitSpeed = Math.min(SPEED, satellite.orbitR * MAX_OMEGA);
      // Advance the orbit angle: dθ/dt = orbitSpeed / orbitR
      const dTheta = (orbitSpeed / satellite.orbitR) * DT * satellite.orbitDir;
      satellite.orbitAngle += dTheta;
      // Update position on the circle
      satellite.x = p.x + satellite.orbitR * Math.cos(satellite.orbitAngle);
      satellite.y = p.y + satellite.orbitR * Math.sin(satellite.orbitAngle);
      // Tangential velocity (magnitude = orbitSpeed)
      satellite.vx = -satellite.orbitDir * orbitSpeed * Math.sin(satellite.orbitAngle);
      satellite.vy = satellite.orbitDir * orbitSpeed * Math.cos(satellite.orbitAngle);
      updateAngle();
    }
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

  function checkEndConditions() {
    // Won: all astronauts collected
    if (!satellite.won && !satellite.crashed && !satellite.escaped) {
      if (collected >= ASTRONAUT_COUNT) {
        satellite.won = true;
        return;
      }
    }
    // Escaped: satellite in flying mode, outside the world bounds (with a
    // margin) and not near any planet's attraction circle.
    if (!satellite.escaped && !satellite.crashed && !satellite.won &&
        satellite.state === 'flying') {
      const margin = 200;
      if (satellite.x < -margin || satellite.x > world.width + margin ||
          satellite.y < -margin || satellite.y > world.height + margin) {
        // Check it's not inside any attraction circle
        let nearAny = false;
        for (const p of world.planets) {
          const ar = attractionRadius(p);
          const dx = satellite.x - p.x;
          const dy = satellite.y - p.y;
          if (dx * dx + dy * dy <= ar * ar) {
            nearAny = true;
            break;
          }
        }
        if (!nearAny) {
          satellite.escaped = true;
        }
      }
    }
  }

  function step() {
    if (satellite.crashed || satellite.escaped || satellite.won) return;
    elapsed += DT * SUBSTEPS;
    stepPlanets();
    maybeSpawnPlanet();
    for (let i = 0; i < SUBSTEPS; i++) {
      physicsStep();
      checkAstronauts();
      checkCrash();
      if (satellite.crashed) return;
    }
    checkEndConditions();
    if (satellite.escaped || satellite.won) return;
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

  // --- Getters / Setters ---

  function speed() { return Math.hypot(satellite.vx, satellite.vy); }
  function isCrashed() { return satellite.crashed; }
  function isEscaped() { return satellite.escaped; }
  function isWon() { return satellite.won; }
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
  function setFuelUnlimited(on) { fuelUnlimited = !!on; }
  function isFuelUnlimited() { return fuelUnlimited; }

  // New state getters
  function getState() { return satellite.state; }
  function isOrbiting() { return satellite.state === 'orbiting'; }
  function getOrbitPlanet() {
    return satellite.orbitPlanet >= 0 ? world.planets[satellite.orbitPlanet] : null;
  }
  function getOrbitRadius() { return satellite.orbitR; }
  function getSpeedConstant() { return SPEED; }
  function getAttractionMultiplier() { return ATTRACTION_MULT; }
  function getAttractionRadius(p) { return attractionRadius(p); }

  return {
    DT, TICK,
    init, randomizePlanets, placeSatellite, resetSatellite,
    thrust, setThrustDir, step, breakOrbit,
    speed, isCrashed, isEscaped, isWon, isConsumed,
    getFuel, getFuelMax, isThrusting, isEngineCut,
    getThrustDir, getAngle,
    getAstronauts, getCollected, getTotalAstronauts, getElapsed,
    getWorld, getSatellite, getBlackHole, getBlackHoleMass,
    setFuelUnlimited, isFuelUnlimited,
    getState, isOrbiting, getOrbitPlanet, getOrbitRadius,
    getSpeedConstant, getAttractionMultiplier, getAttractionRadius,
  };
})();
