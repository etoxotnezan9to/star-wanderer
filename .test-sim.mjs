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

// World is 6000 x 3600; PLANET_COUNT = 16 planets; min distance between
// planets = 0.1 * (6000 + 3600) = 960.
const WORLD_W = 6000, WORLD_H = 3600;
const PLANET_N = 12;
const PLANET_MIN_DIST = 0.1 * (WORLD_W + WORLD_H);

// Test 1: init creates PLANET_N static planets inside the world, none near
// launch, and pairwise distance >= PLANET_MIN_DIST.
SIM.init();
let w = SIM.getWorld();
check('init creates ' + PLANET_N + ' planets', w.planets.length === PLANET_N);
check('all planets start inside the world boundary',
  w.planets.every(p =>
    p.x >= 0 && p.x <= WORLD_W && p.y >= 0 && p.y <= WORLD_H));
check('all planets are static (zero velocity)',
  w.planets.every(p => p.vx === 0 && p.vy === 0));
check('no planet is too close to the launch position (1700,600)',
  w.planets.every(p => Math.hypot(p.x - 1700, p.y - 600) >= 250 + p.r));
check('pairwise planet distance >= ' + PLANET_MIN_DIST,
  (() => {
    for (let i = 0; i < w.planets.length; i++) {
      for (let j = i + 1; j < w.planets.length; j++) {
        const d = Math.hypot(w.planets[i].x - w.planets[j].x,
                             w.planets[i].y - w.planets[j].y);
        if (d < PLANET_MIN_DIST - 1e-6) return false;
      }
    }
    return true;
  })());

// Test 2: planets do not move over time
{
  const before = w.planets.map(p => ({ x: p.x, y: p.y }));
  for (let i = 0; i < 600; i++) SIM.step(); // 10 s
  const after = w.planets.map(p => ({ x: p.x, y: p.y }));
  check('planets did not move over 10 s',
    before.every((b, i) => b.x === after[i].x && b.y === after[i].y));
  check('still exactly ' + PLANET_N + ' planets after 10 s',
    w.planets.length === PLANET_N);
}

// Test 3: no black hole — the hole is removed
check('getBlackHole returns null', SIM.getBlackHole() === null);
check('getBlackHoleMass is 0', SIM.getBlackHoleMass() === 0);

// Test 4: net acceleration direction (planets only, no BH)
SIM.init();
w = SIM.getWorld();
const cx = 400, cy = 300;
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
SIM.placeSatellite(cx, cy, 0, 0);
const st = { x: SIM.getSatellite().x, y: SIM.getSatellite().y };
for (let i = 0; i < 10; i++) SIM.step();
const mx = SIM.getSatellite().x - st.x;
const my = SIM.getSatellite().y - st.y;
const accAngle = Math.atan2(expAy, expAx);
const moveAngle = Math.atan2(my, mx);
let diff = Math.abs(accAngle - moveAngle);
if (diff > Math.PI) diff = 2 * Math.PI - diff;
check('satellite moves along net planet acceleration',
  diff < (25 * Math.PI) / 180);

// Test 5: launch position and velocity
SIM.resetSatellite();
{
  const s = SIM.getSatellite();
  check('launch position is (1700, 600)',
    Math.abs(s.x - 1700) < 1e-6 && Math.abs(s.y - 600) < 1e-6);
  check('launch velocity is zero',
    Math.abs(s.vx) < 1e-9 && Math.abs(s.vy) < 1e-9);
}

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

// Test 13: time tracking
SIM.init();
check('elapsed starts at 0', SIM.getElapsed() === 0);
for (let i = 0; i < 60; i++) SIM.step();
check('elapsed ~1s after 60 frames', Math.abs(SIM.getElapsed() - 1.0) < 0.1);

// Test 14: gravity off (coast) — velocity stays constant while the flag is
// on, and planet gravity (acceleration) resumes when it is switched off.
{
  SIM.init();
  // Push every planet far away so only the satellite's own motion matters.
  for (const p of SIM.getWorld().planets) { p.x += 9000; p.y += 9000; }
  SIM.placeSatellite(1400, 600, 30, -20);
  SIM.setGravityOff(true);
  check('isGravityOff true after setGravityOff(true)', SIM.isGravityOff() === true);
  const s0 = SIM.getSatellite();
  const x0 = s0.x, y0 = s0.y, vx0 = s0.vx, vy0 = s0.vy;
  for (let i = 0; i < 60; i++) SIM.step(); // 1 second, key held
  const s1 = SIM.getSatellite();
  check('gravity off (held): velocity unchanged after 1s',
    Math.abs(s1.vx - vx0) < 1e-9 && Math.abs(s1.vy - vy0) < 1e-9);
  check('gravity off (held): position advanced by v * t',
    Math.abs((s1.x - x0) - vx0 * 1.0) < 0.5 &&
    Math.abs((s1.y - y0) - vy0 * 1.0) < 0.5);
  // Release: planet gravity comes back and changes the velocity.
  SIM.setGravityOff(false);
  check('isGravityOff false after setGravityOff(false)', SIM.isGravityOff() === false);
  const s2 = SIM.getSatellite();
  const vx2 = s2.vx, vy2 = s2.vy;
  for (let i = 0; i < 10; i++) SIM.step();
  const s3 = SIM.getSatellite();
  check('gravity resumed after release: velocity changes',
    Math.abs(s3.vx - vx2) > 1e-6 || Math.abs(s3.vy - vy2) > 1e-6);
  // Reset clears the flag.
  SIM.resetSatellite();
  check('reset clears gravityOff', SIM.isGravityOff() === false);
}

// Test 15: unlimited fuel — the tank never empties, the engine never cuts.
{
  SIM.init();
  SIM.placeSatellite(1400, 600, 0, 0);
  SIM.setFuelUnlimited(true);
  check('isFuelUnlimited true after setFuelUnlimited(true)', SIM.isFuelUnlimited() === true);
  let fired = 0;
  while (SIM.thrust(1, 0) && fired < 20000) fired++;
  check('unlimited: held thrust never stops firing', fired === 20000);
  check('unlimited: tank stays full', SIM.getFuel() === SIM.getFuelMax());
  check('unlimited: engine never cuts', SIM.isEngineCut() === false);
  check('unlimited: isThrusting stays true', SIM.isThrusting() === true);
  SIM.setFuelUnlimited(false);
  check('isFuelUnlimited false after setFuelUnlimited(false)', SIM.isFuelUnlimited() === false);
  let fired2 = 0;
  while (SIM.thrust(1, 0) && fired2 < 20000) fired2++;
  check('after switch off: held thrust empties the tank', SIM.getFuel() < 0.01);
  check('after switch off: engine cuts', SIM.isEngineCut() === true);
  SIM.resetSatellite();
  check('reset clears fuelUnlimited', SIM.isFuelUnlimited() === false);
}

console.log(failures === 0 ? 'ALL TESTS PASSED' : failures + ' TEST(S) FAILED');
process.exit(failures === 0 ? 0 : 1);
