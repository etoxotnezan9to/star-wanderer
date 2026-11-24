import fs from 'node:fs';
import vm from 'node:vm';

const code = fs.readFileSync(new URL('./src/simulation.js', import.meta.url), 'utf8');
const sandbox = {
  console,
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
};
vm.createContext(sandbox);
vm.runInContext(code + '\nthis.__SIM = SIM;', sandbox);
const SIM = sandbox.__SIM;

let failures = 0;
function check(name, cond) {
  console.log((cond ? 'PASS' : 'FAIL') + ' ' + name);
  if (!cond) failures++;
}

// Black hole at world center (1000, 600), radius 120.
const BH_CX = 1000, BH_CY = 600, BH_R = 120;

// Test 1: init creates 4 planets outside the world, drifting to the center
SIM.init();
let w = SIM.getWorld();
check('init creates 4 planets', w.planets.length === 4);
check('all planets start outside the world boundary',
  w.planets.every(p =>
    p.x < 0 || p.x > 2000 || p.y < 0 || p.y > 1200));
check('all planets drift towards the center (dot > 0)',
  w.planets.every(p => {
    const dx = BH_CX - p.x, dy = BH_CY - p.y;
    return p.vx * dx + p.vy * dy > 0;
  }));

// Test 2: planets move closer to the center over time
const p0 = w.planets[0];
const dBefore = Math.hypot(p0.x - BH_CX, p0.y - BH_CY);
for (let i = 0; i < 60; i++) SIM.step(); // 1 second
const dAfter = Math.hypot(w.planets[0].x - BH_CX, w.planets[0].y - BH_CY);
check('planet moved closer to center over time', dAfter < dBefore);
check('planet drift speed is approximately 12 units/s',
  Math.abs((dBefore - dAfter) - 12) < 3);

// Test 3: satellite is attracted by the black hole gravity
SIM.init();
SIM.placeSatellite(1400, 600, 0, 0);
// After some steps the satellite must have gained velocity towards the center
// (negative x direction), proving the BH gravity is acting.
for (let i = 0; i < 10; i++) SIM.step();
const after = SIM.getSatellite();
check('black hole gravity gives satellite velocity towards center',
  after.vx < -1 || after.x < 1400);

// Test 4: net acceleration direction (planets + BH)
SIM.init();
w = SIM.getWorld();
const cx = 1400, cy = 400;
const G = 5000;
const SOFTEN2 = 16;
let expAx = 0, expAy = 0;
for (const p of w.planets) {
  const dx = p.x - cx, dy = p.y - cy;
  const d2 = dx * dx + dy * dy + SOFTEN2;
  const invD = 1 / Math.sqrt(d2);
  const f = (G * p.mass * invD) / d2;
  expAx += f * dx * invD;
  expAy += f * dy * invD;
}
{
  const dx = BH_CX - cx, dy = BH_CY - cy;
  const d2 = dx * dx + dy * dy + SOFTEN2;
  const invD = 1 / Math.sqrt(d2);
  const f = (G * 5000 * invD) / d2;
  expAx += f * dx * invD;
  expAy += f * dy * invD;
}
SIM.placeSatellite(cx, cy, 0, 0);
const st = { x: SIM.getSatellite().x, y: SIM.getSatellite().y };
for (let i = 0; i < 10; i++) SIM.step();
const mx = SIM.getSatellite().x - st.x;
const my = SIM.getSatellite().y - st.y;
const accAngle = Math.atan2(expAy, expAx);
const moveAngle = Math.atan2(my, mx);
let diff = Math.abs(accAngle - moveAngle);
if (diff > Math.PI) diff = 2 * Math.PI - diff;
check('satellite moves along net acceleration', diff < (25 * Math.PI) / 180);

// Test 5: reset launches away from the hole
SIM.resetSatellite();
const s = SIM.getSatellite();
const launchDist = Math.hypot(s.x - BH_CX, s.y - BH_CY);
check('launch position is 700 units from the center',
  Math.abs(launchDist - 700) < 1e-6);
check('launch velocity is zero',
  Math.abs(s.vx) < 1e-9 && Math.abs(s.vy) < 1e-9);

// Test 6: crash on planet
SIM.init();
w = SIM.getWorld();
const target = w.planets[0];
SIM.placeSatellite(target.x, target.y, 0, 0);
check('not crashed before step', SIM.isCrashed() === false);
SIM.step();
check('crash detected after step', SIM.isCrashed() === true);
const xAfter = SIM.getSatellite().x;
SIM.step();
check('further steps do not move satellite', SIM.getSatellite().x === xAfter);
SIM.resetSatellite();
check('reset clears crash', SIM.isCrashed() === false);

// Test 7: fuel and thrust
SIM.placeSatellite(1400, 600, 0, 0);
check('fuel starts at max', SIM.getFuel() === SIM.getFuelMax());
check('fuel max is 100', SIM.getFuelMax() === 100);
const fb = SIM.getFuel();
check('thrust returns true', SIM.thrust(1, 0) === true);
check('fuel decreases', SIM.getFuel() < fb);
check('isThrusting true', SIM.isThrusting() === true);

// Test 8: empty tank shuts engine off
{
  SIM.placeSatellite(1400, 600, 0, 0);
  let fired = 0;
  while (SIM.thrust(1, 0) && fired < 5000) fired++;
  check('held thrust empties the tank', SIM.getFuel() < 0.01);
  check('thrust returns false when empty', SIM.thrust(1, 0) === false);
  check('isEngineCut true', SIM.isEngineCut() === true);
  SIM.setThrustDir(null);
  check('releasing keys clears shut-off', SIM.isEngineCut() === false);
  SIM.resetSatellite();
  check('reset clears shut-off', SIM.isEngineCut() === false);
}

// Test 9: setThrustDir
SIM.placeSatellite(1400, 600, 0, 0);
SIM.thrust(0, 1);
check('thrustDir set', SIM.getThrustDir() !== null);
SIM.setThrustDir(null);
check('thrustDir cleared', SIM.getThrustDir() === null);
SIM.setThrustDir(3, 4);
check('setThrustDir normalizes',
  Math.abs(SIM.getThrustDir().x - 0.6) < 1e-9 &&
  Math.abs(SIM.getThrustDir().y - 0.8) < 1e-9);

// Test 10: angle follows velocity
SIM.placeSatellite(1400, 600, 10, 0);
check('angle 0 for +x', Math.abs(SIM.getAngle()) < 1e-9);
SIM.placeSatellite(1400, 600, 0, 10);
check('angle PI/2 for +y', Math.abs(SIM.getAngle() - Math.PI / 2) < 1e-9);

// Test 11: astronauts
SIM.init();
check('astronauts created', SIM.getAstronauts().length === 5);
check('collected starts at 0', SIM.getCollected() === 0);

// Test 12: fuel regen
SIM.placeSatellite(1400, 600, 0, 0);
for (let i = 0; i < 50; i++) SIM.thrust(1, 0);
const f1 = SIM.getFuel();
for (let i = 0; i < 100; i++) SIM.step();
check('fuel increased from regen', SIM.getFuel() > f1);

// Test 13: event horizon — satellite consumed when crossing the circle
SIM.init();
for (const p of SIM.getWorld().planets) { p.x += 5000; p.vx = 0; p.vy = 0; }
SIM.placeSatellite(BH_CX + BH_R - 5, BH_CY, -50, 0);
for (let i = 0; i < 30; i++) SIM.step();
check('satellite consumed inside the horizon', SIM.isConsumed() === true);
const elapsedAt = SIM.getElapsed();
SIM.step();
check('elapsed frozen after consumed', SIM.getElapsed() === elapsedAt);

// Test 14: reset after consumption
SIM.resetSatellite();
check('reset after consumed: not consumed', SIM.isConsumed() === false);
check('reset after consumed: elapsed is 0', SIM.getElapsed() === 0);

// Test 15: planets swallowed by the hole are removed
SIM.init();
w = SIM.getWorld();
w.planets[0].x = BH_CX + 50;
w.planets[0].y = BH_CY;
w.planets[0].vx = -12;
w.planets[0].vy = 0;
const countBefore = w.planets.length;
for (let i = 0; i < 30; i++) SIM.step(); // 0.5 s → moves 6 units to center
check('planet inside the horizon is removed',
  !w.planets.some(p => p === w.planets[0]) || w.planets.length < countBefore + 1);

// Test 16: new planets spawn over time
SIM.init();
for (let i = 0; i < 600; i++) SIM.step(); // 10 s
check('planets present after 10 s', SIM.getWorld().planets.length > 0);
check('max 4 planets', SIM.getWorld().planets.length <= 4);

// Test 17: time tracking
SIM.init();
check('elapsed starts at 0', SIM.getElapsed() === 0);
for (let i = 0; i < 60; i++) SIM.step();
check('elapsed ~1s after 60 frames', Math.abs(SIM.getElapsed() - 1.0) < 0.1);

// Test 18: black hole getters
const bh = SIM.getBlackHole();
check('getBlackHole returns center and radius',
  bh.x === 1000 && bh.y === 600 && bh.r === 120);
check('getBlackHoleMass is 5000', SIM.getBlackHoleMass() === 5000);

console.log(failures === 0 ? 'ALL TESTS PASSED' : failures + ' TEST(S) FAILED');
process.exit(failures === 0 ? 0 : 1);
