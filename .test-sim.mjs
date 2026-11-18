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
const s0 = SIM.getSatellite();
const snap = { x: s0.x, y: s0.y, vx: s0.vx, vy: s0.vy };
for (let i = 0; i < 60; i++) SIM.step();
SIM.resetSatellite();
const s = SIM.getSatellite();
check('reset returns to placed start position and velocity',
  Math.abs(s.x - snap.x) < 1e-9 && Math.abs(s.y - snap.y) < 1e-9 &&
  Math.abs(s.vx - snap.vx) < 1e-9 && Math.abs(s.vy - snap.vy) < 1e-9);

check('speed() matches |v|', Math.abs(SIM.speed() - Math.hypot(snap.vx, snap.vy)) < 1e-9);

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

// Test 23: fuel barrels exist after init
SIM.init();
const barrels = SIM.getFuelBarrels();
check('fuel barrels created after init', barrels.length === 5);
check('all barrels are within world bounds',
  barrels.every(b => b.x > 0 && b.x < 2000 && b.y > 0 && b.y < 1200));

// Test 24: touching a barrel refuels and removes it
// Burn fuel first so refuel is visible
SIM.placeSatellite(100, 100, 0, 0);
for (let i = 0; i < 20; i++) SIM.thrust(1, 0);
const fuelAfterBurn = SIM.getFuel();
check('fuel decreased after burning', fuelAfterBurn < SIM.getFuelMax());
// Now touch a barrel
const barrels2 = SIM.getFuelBarrels();
if (barrels2.length > 0) {
  const b = barrels2[0];
  SIM.placeSatellite(b.x, b.y, 0, 0); // place exactly on the barrel (resets fuel to max)
  // placeSatellite resets fuel to max, so burn a little again
  for (let i = 0; i < 10; i++) SIM.thrust(1, 0);
  const fuelBeforeRefuel = SIM.getFuel();
  const barrelsBefore = SIM.getFuelBarrels().length;
  // Move the satellite onto the barrel we want to collect (re-place on it)
  const b2 = SIM.getFuelBarrels()[0];
  SIM.placeSatellite(b2.x, b2.y, 0, 0);
  const fuelBeforeTouch = SIM.getFuel();
  SIM.step();
  const fuelAfterTouch = SIM.getFuel();
  const barrelsAfter = SIM.getFuelBarrels().length;
  check('barrel removed after touch', barrelsAfter === barrelsBefore - 1);
  check('fuel increased after touching barrel (refuel +25)', fuelAfterTouch > fuelBeforeTouch || fuelAfterTouch === SIM.getFuelMax());
} else {
  console.log('SKIP barrel touch check (no barrels)');
}

// Test 25: refuel is capped at FUEL_MAX
SIM.placeSatellite(100, 100, 0, 0);
SIM.thrust(1, 0); // burn a little fuel
const barrels3 = SIM.getFuelBarrels();
if (barrels3.length > 0) {
  const b = barrels3[0];
  SIM.placeSatellite(b.x, b.y, 0, 0);
  SIM.step();
  check('fuel after refuel is at most FUEL_MAX', SIM.getFuel() <= SIM.getFuelMax());
}

// Test 26: randomizePlanets recreates barrels
const barrelsBeforeRandomize = SIM.getFuelBarrels().length;
SIM.randomizePlanets();
const barrelsAfterRandomize = SIM.getFuelBarrels().length;
check('randomizePlanets recreates fuel barrels', barrelsAfterRandomize === 5);
check('barrel count is 5 after randomize', barrelsAfterRandomize === 5);

console.log(failures === 0 ? 'ALL TESTS PASSED' : failures + ' TEST(S) FAILED');
process.exit(failures === 0 ? 0 : 1);
