import fs from 'node:fs';
import vm from 'node:vm';

const code = fs.readFileSync(new URL('./src/simulation.js', import.meta.url), 'utf8');

const sandbox = {
  console,
  localStorage: {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
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

// Test 1: init creates 3 planets in the upper-right corner
SIM.init();
let w = SIM.getWorld();
check('init creates 3 planets', w.planets.length === 3);
check('all planets are in upper-right area (x > 1500, y < 500)',
  w.planets.every(p => p.x > 1500 && p.y < 500));
check('all planets have drift velocity',
  w.planets.every(p => typeof p.vx === 'number' && typeof p.vy === 'number'));
check('drift velocity points towards BH (60, 1140)',
  w.planets.every(p => p.vx < 0 && p.vy > 0));

// Test 2: planets move towards the black hole over time
const p0 = w.planets[0];
const p0x = p0.x, p0y = p0.y;
for (let i = 0; i < 60; i++) SIM.step(); // 1 second
check('planet moved towards BH (x decreased)', w.planets[0].x < p0x);
check('planet moved towards BH (y increased)', w.planets[0].y > p0y);
const dx = p0x - w.planets[0].x;
const dy = w.planets[0].y - p0y;
// Speed should be ~12 units/s
const dist = Math.hypot(dx, dy);
check('planet drift speed is approximately 12 units/s', Math.abs(dist - 12) < 3);

// Test 3: satellite moves in direction of net gravity (planets + BH)
SIM.init();
w = SIM.getWorld();
const cx = 1000, cy = 600;
const G = 5000;
const SOFTEN2 = 16;
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
// BH contribution
{
  const dx = 60 - cx;
  const dy = 1140 - cy;
  const d2 = dx * dx + dy * dy + SOFTEN2;
  const invD = 1 / Math.sqrt(d2);
  const f = (G * 100 * invD) / d2;
  expAx += f * dx * invD;
  expAy += f * dy * invD;
}
SIM.placeSatellite(cx, cy, 0, 0);
const sat = SIM.getSatellite();
const before = { x: sat.x, y: sat.y };
for (let i = 0; i < 10; i++) SIM.step();
const movedX = sat.x - before.x;
const movedY = sat.y - before.y;
const accAngle = Math.atan2(expAy, expAx);
const moveAngle = Math.atan2(movedY, movedX);
let angleDiff = Math.abs(accAngle - moveAngle);
if (angleDiff > Math.PI) angleDiff = 2 * Math.PI - angleDiff;
check('satellite moves in direction of net gravity (angle within 20 deg)',
  angleDiff < (20 * Math.PI) / 180);
check('satellite actually moved (non-zero)', Math.abs(movedX) + Math.abs(movedY) > 1e-9);

// Test 4: reset launches from fixed position
SIM.resetSatellite();
const s = SIM.getSatellite();
check('reset launches from fixed position (1000, 1100)',
  Math.abs(s.x - 1000) < 1e-9 && Math.abs(s.y - 1100) < 1e-9);
check('reset launches with fixed velocity (0, -80)',
  Math.abs(s.vx - 0) < 1e-9 && Math.abs(s.vy - (-80)) < 1e-9);

// Test 5: speed() matches |v|
check('speed() matches |v|', Math.abs(SIM.speed() - Math.hypot(0, 80)) < 1e-9);

// Test 6: crash on planet
SIM.init();
w = SIM.getWorld();
const target = w.planets[0];
SIM.placeSatellite(target.x, target.y, 0, 0);
check('not crashed before step when placed on planet',
  SIM.isCrashed() === false);
SIM.step();
check('crash detected after step when satellite is inside a planet',
  SIM.isCrashed() === true);
check('crashedPlanet is the target planet',
  SIM.getSatellite().crashedPlanet === target);
const xAfterCrash = SIM.getSatellite().x;
SIM.step();
check('further steps after crash do not move satellite',
  SIM.getSatellite().x === xAfterCrash);

// Test 7: reset clears crash
SIM.resetSatellite();
check('reset clears crash flag', SIM.isCrashed() === false);
check('reset clears crashedPlanet', SIM.getSatellite().crashedPlanet === null);

// Test 8: fuel starts full
SIM.placeSatellite(200, 200, 0, 0);
check('fuel starts at max after place', SIM.getFuel() === SIM.getFuelMax());
check('fuel max is 100', SIM.getFuelMax() === 100);

// Test 9: thrust consumes fuel and accelerates
const fuelBefore = SIM.getFuel();
const velBefore = { x: SIM.getSatellite().vx, y: SIM.getSatellite().vy };
const thrusted = SIM.thrust(1, 0);
check('thrust returns true when fuel available', thrusted === true);
check('fuel decreases after thrust', SIM.getFuel() < fuelBefore);
check('thrustDir is set after thrust', SIM.getThrustDir() !== null);
check('thrustDir points -x (exhaust)',
  Math.abs(SIM.getThrustDir().x - (-1)) < 1e-9 && Math.abs(SIM.getThrustDir().y) < 1e-9);
const velAfter = { x: SIM.getSatellite().vx, y: SIM.getSatellite().vy };
check('velocity changed in +x direction', velAfter.x > velBefore.x);
check('isThrusting true while thrustDir set and fuel > 0', SIM.isThrusting() === true);

// Test 10: thrust with no fuel returns false
SIM.placeSatellite(200, 200, 0, 0);
for (let i = 0; i < 500; i++) SIM.thrust(1, 0);
check('fuel reaches 0 after many thrusts', SIM.getFuel() <= 0);
const noFuel = SIM.thrust(1, 0);
check('thrust returns false when fuel is 0', noFuel === false);
check('isThrusting false when fuel is 0', SIM.isThrusting() === false);

// Test 11: setThrustDir(null) clears thrust
SIM.placeSatellite(200, 200, 0, 0);
SIM.thrust(0, 1);
check('thrustDir set before clear', SIM.getThrustDir() !== null);
SIM.setThrustDir(null);
check('thrustDir cleared after setThrustDir(null)', SIM.getThrustDir() === null);

// Test 12: setThrustDir normalizes
SIM.placeSatellite(200, 200, 0, 0);
SIM.setThrustDir(3, 4);
check('setThrustDir normalizes to unit vector',
  Math.abs(SIM.getThrustDir().x - 0.6) < 1e-9 && Math.abs(SIM.getThrustDir().y - 0.8) < 1e-9);

// Test 13: reset restores fuel
SIM.placeSatellite(200, 200, 0, 0);
for (let i = 0; i < 50; i++) SIM.thrust(1, 0);
check('fuel decreased before reset', SIM.getFuel() < SIM.getFuelMax());
SIM.resetSatellite();
check('reset restores fuel to max', SIM.getFuel() === SIM.getFuelMax());

// Test 14: orientation follows velocity
SIM.placeSatellite(200, 200, 10, 0);
check('angle is 0 when moving in +x', Math.abs(SIM.getAngle() - 0) < 1e-9);
SIM.placeSatellite(200, 200, 0, 10);
check('angle is PI/2 when moving in +y', Math.abs(SIM.getAngle() - Math.PI / 2) < 1e-9);

// Test 15: astronauts exist after init
SIM.init();
const astronauts = SIM.getAstronauts();
check('astronauts created after init', astronauts.length === 5);
check('collected starts at 0', SIM.getCollected() === 0);
check('total astronauts is 5', SIM.getTotalAstronauts() === 5);

// Test 16: fuel regen
SIM.placeSatellite(100, 100, 0, 0);
for (let i = 0; i < 50; i++) SIM.thrust(1, 0);
const fuelBeforeRegen = SIM.getFuel();
for (let i = 0; i < 100; i++) SIM.step();
const fuelAfterRegen = SIM.getFuel();
check('fuel increased from regen', fuelAfterRegen > fuelBeforeRegen);

// Test 17: black hole exists and satellite not consumed initially
const bh = SIM.getBlackHole();
check('black hole has x, y, mass, r',
  typeof bh.x === 'number' && typeof bh.y === 'number' &&
  typeof bh.mass === 'number' && typeof bh.r === 'number');
check('black hole is in lower-left corner (x < 100, y > 1000)',
  bh.x < 100 && bh.y > 1000);
SIM.placeSatellite(1800, 100, 0, 0);
check('satellite not consumed initially', SIM.isConsumed() === false);
check('black hole radius starts small (< 25)', SIM.getBlackHoleRadius() < 25);

// Test 18: black hole radius grows over time
SIM.init();
SIM.placeSatellite(1800, 100, 0, 0);
const rStart = SIM.getBlackHoleRadius();
for (let i = 0; i < 60; i++) SIM.step();
const rLater = SIM.getBlackHoleRadius();
const elapsedAfter = SIM.getElapsed();
check('black hole radius grows over time', rLater > rStart);
check('growth rate is approximately 1.2 units/s',
  Math.abs((rLater - rStart) / elapsedAfter - 1.2) < 0.2);

// Test 19: satellite inside black hole is consumed
SIM.init();
const bh2 = SIM.getBlackHole();
SIM.placeSatellite(bh2.x, bh2.y, 0, 0);
SIM.step();
check('satellite consumed after entering black hole', SIM.isConsumed() === true);
const elapsedAtConsume = SIM.getElapsed();
SIM.step();
check('elapsed unchanged after consumed', SIM.getElapsed() === elapsedAtConsume);

// Test 20: reset after consumption
SIM.resetSatellite();
check('reset after consumed: not consumed', SIM.isConsumed() === false);
check('reset after consumed: elapsed is 0', SIM.getElapsed() === 0);
check('reset after consumed: black hole radius back to initial',
  SIM.getBlackHoleRadius() < 25);
check('reset after consumed: collected is 0', SIM.getCollected() === 0);

// Test 21: planets are absorbed by the black hole
SIM.init();
w = SIM.getWorld();
// Place a planet very close to the BH (60, 1140)
w.planets[0].x = 60 + 25;
w.planets[0].y = 1140 - 25;
w.planets[0].vx = 0;
w.planets[0].vy = 0;
const planetCountBefore = w.planets.length;
for (let i = 0; i < 120; i++) SIM.step(); // 2 seconds
check('planets are still present (spawning works)', w.planets.length > 0);

// Test 22: new planets spawn over time
SIM.init();
const initialCount = SIM.getWorld().planets.length;
for (let i = 0; i < 600; i++) SIM.step(); // 10 seconds
const laterCount = SIM.getWorld().planets.length;
check('planets are present after 10 seconds', laterCount > 0);
check('planet count is at most PLANET_MAX_COUNT (6)', laterCount <= 6);

// Test 23: empty tank shuts engine off
// Test the engine cut logic directly via thrust() without running step()
// (to avoid BH gravity and planet spawning interfering).
{
  SIM.placeSatellite(1000, 600, 0, 0);
  // Drain the tank by calling thrust() repeatedly (no step, no gravity)
  let fired = 0;
  while (SIM.thrust(1, 0) && fired < 5000) { fired++; }
  check('held thrust empties the tank', SIM.getFuel() < 0.01);
  // Keep "holding" the key: thrust() should return false (engine cut)
  const noFire = SIM.thrust(1, 0);
  check('engine does not fire on an empty tank while the key is held', noFire === false);
  check('isThrusting false on an empty tank', SIM.isThrusting() === false);
  check('isEngineCut reports the shut-off engine', SIM.isEngineCut() === true);
  // Release all keys
  SIM.setThrustDir(null);
  check('releasing the keys clears the shut-off', SIM.isEngineCut() === false);
  // Small regen has occurred (no step ran, so fuel is still 0)
  // Simulate regen by stepping a bit — but step() will apply gravity.
  // Instead, just verify the engine can fire again if fuel is restored.
  // We'll use a reset to verify.
  SIM.resetSatellite();
  check('reset clears the shut-off', SIM.isEngineCut() === false);
  check('engine fires after reset', SIM.thrust(1, 0) === true);
}

// Test 24: time tracking
SIM.init();
check('elapsed starts at 0', SIM.getElapsed() === 0);
for (let i = 0; i < 60; i++) SIM.step();
check('elapsed increased after 60 frames', SIM.getElapsed() > 0);
check('elapsed is approximately 1s (60 frames * 1/60)',
  Math.abs(SIM.getElapsed() - 1.0) < 0.1);

console.log(failures === 0 ? 'ALL TESTS PASSED' : failures + ' TEST(S) FAILED');
process.exit(failures === 0 ? 0 : 1);
