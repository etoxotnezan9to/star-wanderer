import fs from 'node:fs';
import vm from 'node:vm';

const code = fs.readFileSync(new URL('./src/simulation.js', import.meta.url), 'utf8');

const store = {};
const sandbox = {
  console,
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  },
};
vm.createContext(sandbox);
vm.runInContext(code + '\nthis.__SIM = SIM;', sandbox);
const SIM = sandbox.__SIM;

let failures = 0;
function check(name, cond) {
  console.log((cond ? 'PASS' : 'FAIL') + ' ' + name);
  if (!cond) failures++;
}

SIM.init();
let w = SIM.getWorld();
check('init creates 3 planets', w.planets.length === 3);
check('init saves planets to localStorage', 'satellite-sim.planets.v1' in store);
const saved1 = JSON.parse(store['satellite-sim.planets.v1']);
check('saved planets count is 3', saved1.length === 3);

SIM.init();
w = SIM.getWorld();
check('re-init restores saved positions',
  w.planets.every((p, i) => Math.abs(p.x - saved1[i].x) < 1e-9 && Math.abs(p.y - saved1[i].y) < 1e-9));

SIM.randomizePlanets();
w = SIM.getWorld();
const saved2 = JSON.parse(store['satellite-sim.planets.v1']);
check('randomize changes at least one position',
  w.planets.some((p, i) => Math.abs(p.x - saved1[i].x) > 1 || Math.abs(p.y - saved1[i].y) > 1));
check('storage updated to match new planets',
  w.planets.every((p, i) => Math.abs(p.x - saved2[i].x) < 1e-9));
check('randomize keeps 3 planets', w.planets.length === 3);

const cx = 1000, cy = 600;
let nearest = w.planets[0];
let best = Infinity;
for (const p of w.planets) {
  const d = Math.hypot(p.x - cx, p.y - cy);
  if (d < best) { best = d; nearest = p; }
}
// Compute expected net acceleration direction (sum of gravity from all planets)
const G = 5000;
const SOFTEN = 4;
const SOFTEN2 = SOFTEN * SOFTEN;
let expAx = 0, expAy = 0;
for (const p of w.planets) {
  const dx = p.x - cx;
  const dy = p.y - cy;
  const d2 = dx * dx + dy * dy + SOFTEN2;
  const invD = 1 / Math.sqrt(d2);
  const f = (G * p.mass * invD) / d2;
  expAx += f * dx * invD;
  expAy += f * dy * invD;
}
SIM.placeSatellite(cx, cy, 0, 0);
const sat = SIM.getSatellite();
const before = { x: sat.x, y: sat.y };
// Short run: 10 frames so the displacement direction stays close to the initial
// net gravitational acceleration direction (multi-body direction changes over
// longer runs, which is physically correct).
for (let i = 0; i < 10; i++) SIM.step();
const movedX = sat.x - before.x;
const movedY = sat.y - before.y;
// Compare angles: displacement should be within ~20 degrees of net accel
const accAngle = Math.atan2(expAy, expAx);
const moveAngle = Math.atan2(movedY, movedX);
let angleDiff = Math.abs(accAngle - moveAngle);
if (angleDiff > Math.PI) angleDiff = 2 * Math.PI - angleDiff;
check('satellite moves in direction of net gravity (angle within 20 deg)',
  angleDiff < (20 * Math.PI) / 180);
check('satellite actually moved (non-zero)', Math.abs(movedX) + Math.abs(movedY) > 1e-9);

SIM.placeSatellite(500, 400, 10, -5);
for (let i = 0; i < 60; i++) SIM.step();
SIM.resetSatellite();
const s = SIM.getSatellite();
// resetSatellite now always launches from the fixed position (1000, 1100, 0, -80)
check('reset launches from fixed position (1000, 1100)',
  Math.abs(s.x - 1000) < 1e-9 && Math.abs(s.y - 1100) < 1e-9);
check('reset launches with fixed velocity (0, -80)',
  Math.abs(s.vx - 0) < 1e-9 && Math.abs(s.vy - (-80)) < 1e-9);

check('speed() matches |v|', Math.abs(SIM.speed() - Math.hypot(0, 80)) < 1e-9);

// Test 7: crash on planet — place satellite inside a planet, step, expect crashed
SIM.init();
w = SIM.getWorld();
const target = w.planets[0];
// Place satellite right on top of the planet center (inside it)
SIM.placeSatellite(target.x, target.y, 0, 0);
check('not crashed before step when placed on planet (checked at step time)',
  SIM.isCrashed() === false);
SIM.step();
check('crash detected after step when satellite is inside a planet',
  SIM.isCrashed() === true);
check('crashedPlanet is the target planet',
  SIM.getSatellite().crashedPlanet === target);
// After crash, further steps do nothing
const xAfterCrash = SIM.getSatellite().x;
SIM.step();
check('further steps after crash do not move satellite',
  SIM.getSatellite().x === xAfterCrash);

// Test 8: reset clears crash
SIM.resetSatellite();
check('reset clears crash flag', SIM.isCrashed() === false);
check('reset clears crashedPlanet', SIM.getSatellite().crashedPlanet === null);

// Test 9: placing a new satellite clears crash
SIM.placeSatellite(target.x, target.y, 0, 0);
SIM.step();
check('crash again after re-placing on planet', SIM.isCrashed() === true);
SIM.placeSatellite(100, 100, 10, 10);
check('re-placing away from planet clears crash', SIM.isCrashed() === false);

// Test 10: randomize clears crash
SIM.placeSatellite(target.x, target.y, 0, 0);
SIM.step();
check('crash before randomize', SIM.isCrashed() === true);
SIM.randomizePlanets();
check('randomize clears crash', SIM.isCrashed() === false);

// Test 11: fuel starts full
SIM.placeSatellite(200, 200, 0, 0);
check('fuel starts at max after place', SIM.getFuel() === SIM.getFuelMax());
check('fuel max is 100', SIM.getFuelMax() === 100);

// Test 12: thrust consumes fuel and accelerates in the given direction
// thrust(dirX, dirY) — dirX,dirY is the direction of acceleration (nose).
const fuelBefore = SIM.getFuel();
const velBefore = { x: SIM.getSatellite().vx, y: SIM.getSatellite().vy };
const thrusted = SIM.thrust(1, 0); // accelerate in +x (nose points +x)
check('thrust returns true when fuel available', thrusted === true);
check('fuel decreases after thrust', SIM.getFuel() < fuelBefore);
check('thrustDir is set after thrust', SIM.getThrustDir() !== null);
// thrustDir stores exhaust direction (opposite to acceleration)
check('thrustDir points -x (exhaust, opposite to accel +x)',
  Math.abs(SIM.getThrustDir().x - (-1)) < 1e-9 && Math.abs(SIM.getThrustDir().y) < 1e-9);
const velAfter = { x: SIM.getSatellite().vx, y: SIM.getSatellite().vy };
check('velocity changed in +x direction (acceleration direction)', velAfter.x > velBefore.x);
check('isThrusting true while thrustDir set and fuel > 0', SIM.isThrusting() === true);

// Test 13: thrust with no fuel returns false
SIM.placeSatellite(200, 200, 0, 0);
// drain fuel by thrusting many times
for (let i = 0; i < 500; i++) SIM.thrust(1, 0);
check('fuel reaches 0 after many thrusts', SIM.getFuel() <= 0);
const noFuel = SIM.thrust(1, 0);
check('thrust returns false when fuel is 0', noFuel === false);
check('isThrusting false when fuel is 0', SIM.isThrusting() === false);

// Test 14: setThrustDir(null) clears thrust
SIM.placeSatellite(200, 200, 0, 0);
SIM.thrust(0, 1);
check('thrustDir set before clear', SIM.getThrustDir() !== null);
SIM.setThrustDir(null);
check('thrustDir cleared after setThrustDir(null)', SIM.getThrustDir() === null);
check('isThrusting false after clear', SIM.isThrusting() === false);

// Test 15: setThrustDir normalizes
SIM.placeSatellite(200, 200, 0, 0);
SIM.setThrustDir(3, 4);
check('setThrustDir normalizes to unit vector',
  Math.abs(SIM.getThrustDir().x - 0.6) < 1e-9 && Math.abs(SIM.getThrustDir().y - 0.8) < 1e-9);

// Test 16: reset restores fuel
SIM.placeSatellite(200, 200, 0, 0);
for (let i = 0; i < 50; i++) SIM.thrust(1, 0);
check('fuel decreased before reset', SIM.getFuel() < SIM.getFuelMax());
SIM.resetSatellite();
check('reset restores fuel to max', SIM.getFuel() === SIM.getFuelMax());

// Test 17: placeSatellite restores fuel
for (let i = 0; i < 50; i++) SIM.thrust(1, 0);
check('fuel decreased before place', SIM.getFuel() < SIM.getFuelMax());
SIM.placeSatellite(300, 300, 5, 5);
check('place restores fuel to max', SIM.getFuel() === SIM.getFuelMax());

// Test 18: randomize restores fuel
for (let i = 0; i < 50; i++) SIM.thrust(1, 0);
check('fuel decreased before randomize', SIM.getFuel() < SIM.getFuelMax());
SIM.randomizePlanets();
check('randomize restores fuel to max', SIM.getFuel() === SIM.getFuelMax());

// Test 19: thrust while crashed returns false
// Use the current planets (after randomize in test 18)
const currentPlanets = SIM.getWorld().planets;
const crashTarget = currentPlanets[0];
SIM.placeSatellite(crashTarget.x, crashTarget.y, 0, 0);
SIM.step(); // crash
check('crashed before thrust', SIM.isCrashed() === true);
const crashedThrust = SIM.thrust(1, 0);
check('thrust returns false while crashed', crashedThrust === false);

// Test 20: orientation follows velocity
SIM.placeSatellite(200, 200, 10, 0); // moving in +x
check('angle is 0 when moving in +x', Math.abs(SIM.getAngle() - 0) < 1e-9);
SIM.placeSatellite(200, 200, 0, 10); // moving in +y
check('angle is PI/2 when moving in +y', Math.abs(SIM.getAngle() - Math.PI / 2) < 1e-9);
SIM.placeSatellite(200, 200, -10, 0); // moving in -x
check('angle is PI when moving in -x', Math.abs(SIM.getAngle() - Math.PI) < 1e-9);
SIM.placeSatellite(200, 200, 0, -10); // moving in -y
check('angle is -PI/2 when moving in -y', Math.abs(SIM.getAngle() - (-Math.PI / 2)) < 1e-9);

// Test 21: angle is preserved when velocity is zero (does not snap to 0)
SIM.placeSatellite(200, 200, 10, 0); // set angle to 0
check('angle is 0 before zero-velocity place', Math.abs(SIM.getAngle()) < 1e-9);
SIM.placeSatellite(200, 200, 0, 0); // zero velocity — angle should stay 0
check('angle preserved (stays 0) when velocity set to zero', Math.abs(SIM.getAngle()) < 1e-9);
SIM.placeSatellite(200, 200, 0, 10); // set angle to PI/2
SIM.placeSatellite(200, 200, 0, 0); // zero velocity — angle should stay PI/2
check('angle preserved (stays PI/2) when velocity set to zero',
  Math.abs(SIM.getAngle() - Math.PI / 2) < 1e-9);

// Test 22: angle updates after step (satellite curves under gravity)
SIM.placeSatellite(200, 200, 5, 0);
const angleBefore = SIM.getAngle();
for (let i = 0; i < 240; i++) SIM.step();
// After stepping under gravity, the angle should have changed (curved path)
// unless the satellite crashed; if it did, skip this check
if (!SIM.isCrashed()) {
  const angleAfter = SIM.getAngle();
  check('angle changed after gravity step (curved trajectory)',
    Math.abs(angleAfter - angleBefore) > 1e-6);
} else {
  console.log('SKIP angle change check (satellite crashed during step)');
}

// Test 23: astronauts exist after init
SIM.init();
const astronauts = SIM.getAstronauts();
check('astronauts created after init', astronauts.length === 5);
check('all astronauts are within world bounds',
  astronauts.every(a => a.x > 0 && a.x < 2000 && a.y > 0 && a.y < 1200));
check('collected starts at 0', SIM.getCollected() === 0);
check('total astronauts is 5', SIM.getTotalAstronauts() === 5);

// Test 24: touching an astronaut collects it (no fuel refuel)
SIM.placeSatellite(100, 100, 0, 0);
for (let i = 0; i < 20; i++) SIM.thrust(1, 0);
const fuelAfterBurn = SIM.getFuel();
check('fuel decreased after burning', fuelAfterBurn < SIM.getFuelMax());
const astronauts2 = SIM.getAstronauts();
if (astronauts2.length > 0) {
  const a = astronauts2[0];
  const planet = SIM.getWorld().planets[a.orbitPlanet];
  // Place satellite at the astronaut's position, velocity zero.
  // In step(): RK4 moves the satellite slightly, then checkAstronauts
  // tests distance <= (a.r + 10). The astronaut stays at its orbit
  // position during checkAstronauts (stepAstronauts runs after).
  SIM.placeSatellite(a.x, a.y, 0, 0);
  const fuelBeforeTouch = SIM.getFuel();
  const astronautsBefore = SIM.getAstronauts().length;
  const collectedBefore = SIM.getCollected();
  SIM.step();
  const fuelAfterTouch = SIM.getFuel();
  const astronautsAfter = SIM.getAstronauts().length;
  const collectedAfter = SIM.getCollected();
  // The satellite moved slightly due to gravity during RK4, so it may
  // not be exactly on the astronaut. Check that the astronaut was
  // collected OR that the distance is still small (within tolerance).
  check('astronaut removed after touch', astronautsAfter === astronautsBefore - 1);
  check('collected incremented after touch', collectedAfter === collectedBefore + 1);
  check('fuel did not jump by refuel amount (no barrel refuel)',
    fuelAfterTouch - fuelBeforeTouch < 5);
} else {
  console.log('SKIP astronaut touch check (no astronauts)');
}

// Test 25: fuel regen — fuel increases over time when not thrusting
SIM.placeSatellite(100, 100, 0, 0);
for (let i = 0; i < 50; i++) SIM.thrust(1, 0); // burn fuel
const fuelBeforeRegen = SIM.getFuel();
for (let i = 0; i < 100; i++) SIM.step(); // 100 frames of regen
const fuelAfterRegen = SIM.getFuel();
check('fuel increased from regen', fuelAfterRegen > fuelBeforeRegen);
check('fuel regen is slow (less than burn rate)',
  (fuelAfterRegen - fuelBeforeRegen) / 100 < 0.35);

// Test 26: randomizePlanets recreates astronauts and resets counter
SIM.randomizePlanets();
check('randomizePlanets recreates astronauts', SIM.getAstronauts().length === 5);
check('randomizePlanets resets collected to 0', SIM.getCollected() === 0);

// Test 27: astronauts orbit their assigned planet
SIM.init();
const astroList = SIM.getAstronauts();
check('astronauts have orbitPlanet and orbitR properties',
  astroList.every(a => typeof a.orbitPlanet === 'number' && typeof a.orbitR === 'number'));
const aBefore = { x: astroList[0].x, y: astroList[0].y };
for (let i = 0; i < 60; i++) SIM.step();
const aAfter = { x: astroList[0].x, y: astroList[0].y };
const aMoved = Math.hypot(aAfter.x - aBefore.x, aAfter.y - aBefore.y);
check('astronaut moved after 60 frames (orbiting)', aMoved > 1);
const planet = astroList[0];
const distToPlanet = Math.hypot(
  aAfter.x - SIM.getWorld().planets[planet.orbitPlanet].x,
  aAfter.y - SIM.getWorld().planets[planet.orbitPlanet].y
);
check('astronaut is at orbit radius from its planet',
  Math.abs(distToPlanet - planet.orbitR) < 5);

// Test 28: resetSatellite recreates astronauts and resets counter
SIM.init();
// Collect one astronaut
const aToCollect = SIM.getAstronauts()[0];
SIM.placeSatellite(aToCollect.x, aToCollect.y, 0, 0);
SIM.step();
check('astronaut was collected', SIM.getCollected() >= 1);
check('astronaut count decreased', SIM.getAstronauts().length < 5);
// Reset
SIM.resetSatellite();
check('resetSatellite recreates all astronauts', SIM.getAstronauts().length === 5);
check('resetSatellite resets collected to 0', SIM.getCollected() === 0);

// Test 29: time tracking
SIM.init();
check('elapsed starts at 0', SIM.getElapsed() === 0);
for (let i = 0; i < 60; i++) SIM.step();
check('elapsed increased after 60 frames', SIM.getElapsed() > 0);
check('elapsed is approximately 0.25s (60 frames * DT*SUBSTEPS)',
  Math.abs(SIM.getElapsed() - 60 * SIM.DT * 4) < 1);

// Test 30: portal exists and game not won initially
const portal = SIM.getPortal();
check('portal has x, y, r', typeof portal.x === 'number' && typeof portal.y === 'number' && typeof portal.r === 'number');
check('game not won initially', SIM.isGameWon() === false);

// Test 31: entering portal wins the game
SIM.placeSatellite(portal.x, portal.y, 0, 0);
SIM.step();
check('game won after entering portal', SIM.isGameWon() === true);
check('elapsed is frozen after win', SIM.getElapsed() > 0);
// Further steps should not change elapsed
const elapsedAtWin = SIM.getElapsed();
SIM.step();
check('elapsed unchanged after game won', SIM.getElapsed() === elapsedAtWin);

// Test 32: reset after game won
SIM.resetSatellite();
check('reset after win: game not won', SIM.isGameWon() === false);
check('reset after win: elapsed is 0', SIM.getElapsed() === 0);
check('reset after win: astronauts recreated', SIM.getAstronauts().length === 5);
check('reset after win: collected is 0', SIM.getCollected() === 0);

// Test 33: an empty tank shuts the engine off while the keys stay held.
// The UI calls thrust() every tick while an arrow is held and
// setThrustDir(null) once all arrows are released. Planets are moved far
// away and made massless for this test so gravity cannot crash the
// satellite (astronauts still orbit them, so the list stays the same size).
{
  const w33 = SIM.getWorld();
  const savedPlanets = w33.planets;
  w33.planets = savedPlanets.map((p) => Object.assign({}, p, { x: p.x + 1e7, mass: 0 }));
  SIM.placeSatellite(1000, 750, 0, 0);
  let ticks = 0;
  // hold the key until the engine stops firing (at most 60 s of game time)
  while (SIM.thrust(1, 0) && ticks < 3600) { SIM.step(); ticks++; }
  SIM.step();
  check('held thrust empties the tank', SIM.getFuel() < 0.01);
  // keep holding the key for 10 more seconds of game time
  const vxEmpty = SIM.getSatellite().vx;
  let firedWhileEmpty = 0;
  for (let i = 0; i < 600; i++) { if (SIM.thrust(1, 0)) firedWhileEmpty++; SIM.step(); }
  check('engine does not fire on an empty tank while the key is held', firedWhileEmpty === 0);
  check('no acceleration on an empty tank while the key is held',
    Math.abs(SIM.getSatellite().vx - vxEmpty) < 1e-9);
  check('isThrusting false on an empty tank while the key is held', SIM.isThrusting() === false);
  check('fuel regenerates while the engine is off', SIM.getFuel() > 1);
  check('isEngineCut reports the shut-off engine', SIM.isEngineCut() === true);
  // release all keys, then press again: the engine restarts on the regenerated fuel
  SIM.setThrustDir(null);
  check('releasing the keys clears the shut-off', SIM.isEngineCut() === false);
  check('engine fires again after the keys are released and pressed', SIM.thrust(1, 0) === true);
  // a reset also clears the shut-off
  for (let i = 0; i < 3600 && SIM.thrust(1, 0); i++) SIM.step();
  check('engine shut off again before reset', SIM.isEngineCut() === true);
  SIM.resetSatellite();
  check('reset clears the shut-off', SIM.isEngineCut() === false && SIM.thrust(1, 0) === true);
  w33.planets = savedPlanets;
}

console.log(failures === 0 ? 'ALL TESTS PASSED' : failures + ' TEST(S) FAILED');
process.exit(failures === 0 ? 0 : 1);
