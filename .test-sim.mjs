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

const WORLD_W = 6000, WORLD_H = 3600;
const PLANET_N = 12;
const PLANET_MIN_DIST = 0.02 * (WORLD_W + WORLD_H);
const SPEED = 400;
const ATTRACTION_MULT = 16;

// Test 1: init creates PLANET_N static planets
SIM.init();
let w = SIM.getWorld();
check('init creates ' + PLANET_N + ' planets', w.planets.length === PLANET_N);
check('all planets are static (zero velocity)',
  w.planets.every(p => p.vx === 0 && p.vy === 0));
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

// Test 2: satellite speed is always constant
{
  SIM.init();
  const s = SIM.getSatellite();
  check('launch speed equals SPEED constant',
    Math.abs(Math.hypot(s.vx, s.vy) - SPEED) < 1e-9);
  check('speed constant is 400', SIM.getSpeedConstant() === 400);
  // Step for 5 seconds
  for (let i = 0; i < 300; i++) SIM.step();
  const s2 = SIM.getSatellite();
  check('speed still constant after 5s of flight',
    Math.abs(Math.hypot(s2.vx, s2.vy) - SPEED) < 1e-6);
}

// Test 3: straight-line flight (no planet nearby)
{
  SIM.init();
  // Move all planets far away
  for (const p of w.planets) { p.x += 20000; p.y += 20000; }
  SIM.placeSatellite(1000, 500, SPEED, 0);
  check('state is flying', SIM.getState() === 'flying');
  const x0 = SIM.getSatellite().x;
  const y0 = SIM.getSatellite().y;
  for (let i = 0; i < 60; i++) SIM.step(); // 1 second
  const s = SIM.getSatellite();
  check('moved in a straight line (x advanced by SPEED * 1s)',
    Math.abs((s.x - x0) - SPEED * 1.0) < 0.5);
  check('y unchanged (moving along +x)', Math.abs(s.y - y0) < 1e-6);
  check('still flying (no capture)', SIM.getState() === 'flying');
}

// Test 4: capture into orbit when passing close to a planet
{
  SIM.init();
  for (const p of w.planets) { p.x = -5000; p.y = -5000; }
  const p0 = w.planets[0];
  p0.r = 20;
  const ar = ATTRACTION_MULT * p0.r; // 80
  // Place the planet offset from the satellite's path so the closest
  // approach is at distance 50 (between planet.r=20 and ar=80).
  // Satellite flies along y=500, +x. Planet at (1500, 550): closest
  // approach at x=1500, distance = 50.
  p0.x = 1500;
  p0.y = 550;
  SIM.placeSatellite(1000, 500, SPEED, 0);
  check('state is flying before capture', SIM.getState() === 'flying');
  let captured = false;
  for (let i = 0; i < 1000; i++) {
    SIM.step();
    if (SIM.isCrashed()) break;
    if (SIM.isOrbiting()) { captured = true; break; }
  }
  check('captured into orbit', captured);
  if (captured) {
    check('orbit planet is p0', SIM.getOrbitPlanet() === p0);
    check('orbit radius ~50 (closest approach)',
      Math.abs(SIM.getOrbitRadius() - 50) < 5);
  }
}

// Test 5: break orbit with breakOrbit()
{
  SIM.init();
  for (const p of w.planets) { p.x = -5000; p.y = -5000; }
  const p0 = w.planets[0];
  p0.r = 20;
  p0.x = 1500; p0.y = 550; // closest approach at dist=50
  SIM.placeSatellite(1000, 500, SPEED, 0);
  let captured = false;
  for (let i = 0; i < 1000; i++) {
    SIM.step();
    if (SIM.isCrashed()) break;
    if (SIM.isOrbiting()) { captured = true; break; }
  }
  check('captured for break-orbit test', captured);
  if (captured) {
    const ox = SIM.getSatellite().x;
    const oy = SIM.getSatellite().y;
    SIM.breakOrbit();
    check('state is flying after breakOrbit', SIM.getState() === 'flying');
    check('speed still constant after break',
      Math.abs(Math.hypot(SIM.getSatellite().vx, SIM.getSatellite().vy) - SPEED) < 1e-6);
    for (let i = 0; i < 60; i++) SIM.step();
    const distAfter = Math.hypot(SIM.getSatellite().x - p0.x, SIM.getSatellite().y - p0.y);
    check('moved away from planet after break',
      distAfter > Math.hypot(ox - p0.x, oy - p0.y));
    // Key fix: after breaking orbit, the satellite must NOT be re-captured
    // by the same planet while still inside its attraction circle. Step for
    // a full second and verify the state stays 'flying'.
    for (let i = 0; i < 60; i++) SIM.step();
    check('not re-captured by the same planet after break (1s)',
      SIM.getState() === 'flying');
  }
}

// Test 6: no capture when passing far from planet (outside attraction circle)
{
  SIM.init();
  for (const p of w.planets) { p.x = -5000; p.y = -5000; }
  const p0 = w.planets[0];
  p0.r = 20;
  const ar = ATTRACTION_MULT * p0.r; // 80
  // Place planet far off the path
  p0.x = 1500;
  p0.y = 500 + ar + 50; // 150 units below the path, well outside ar=80
  SIM.placeSatellite(1000, 500, SPEED, 0);
  for (let i = 0; i < 300; i++) SIM.step(); // 5 seconds
  check('no capture when passing far from planet', SIM.getState() === 'flying');
}

// Test 7: crash on planet contact
{
  SIM.init();
  for (const p of w.planets) { p.x = -5000; p.y = -5000; }
  const p0 = w.planets[0];
  p0.x = 1500; p0.y = 500; p0.r = 20;
  // Place satellite right on the planet
  SIM.placeSatellite(p0.x, p0.y, SPEED, 0);
  SIM.step();
  check('crash on planet contact', SIM.isCrashed() === true);
}

// Test 8: fuel and thrust (direction change)
{
  SIM.init();
  for (const p of w.planets) { p.x += 20000; p.y += 20000; }
  SIM.placeSatellite(1000, 500, SPEED, 0);
  check('fuel starts at max', SIM.getFuel() === SIM.getFuelMax());
  const angleBefore = SIM.getAngle();
  SIM.thrust(0, -1);
  const angleAfter = SIM.getAngle();
  check('thrust changes direction', Math.abs(angleAfter - angleBefore) > 0.1);
  check('speed unchanged after thrust',
    Math.abs(Math.hypot(SIM.getSatellite().vx, SIM.getSatellite().vy) - SPEED) < 1e-6);
}

// Test 9: unlimited fuel
{
  SIM.init();
  SIM.placeSatellite(1000, 500, SPEED, 0);
  SIM.setFuelUnlimited(true);
  check('isFuelUnlimited true', SIM.isFuelUnlimited() === true);
  let fired = 0;
  while (SIM.thrust(1, 0) && fired < 5000) fired++;
  check('unlimited: tank stays full', SIM.getFuel() === SIM.getFuelMax());
  check('unlimited: engine never cuts', SIM.isEngineCut() === false);
  SIM.setFuelUnlimited(false);
  check('isFuelUnlimited false', SIM.isFuelUnlimited() === false);
}

// Test 10: launch position
{
  SIM.resetSatellite();
  const s = SIM.getSatellite();
  check('launch position is (1700, 600)',
    Math.abs(s.x - 1700) < 1e-6 && Math.abs(s.y - 600) < 1e-6);
  check('launch velocity is +x at SPEED',
    Math.abs(s.vx - SPEED) < 1e-6 && Math.abs(s.vy) < 1e-6);
  check('state is flying at launch', SIM.getState() === 'flying');
}

// Test 11: astronauts
{
  SIM.init();
  check('astronauts created', SIM.getAstronauts().length === 5);
  check('collected starts at 0', SIM.getCollected() === 0);
}

// Test 12: time tracking
{
  SIM.init();
  check('elapsed starts at 0', SIM.getElapsed() === 0);
  for (let i = 0; i < 60; i++) SIM.step();
  check('elapsed ~1s after 60 frames', Math.abs(SIM.getElapsed() - 1.0) < 0.1);
}

// Test 13: orbit period = 2π / min(SPEED/orbitR, MAX_OMEGA)
{
  SIM.init();
  for (const p of w.planets) { p.x = -5000; p.y = -5000; }
  const p0 = w.planets[0];
  p0.r = 20;
  p0.x = 1500; p0.y = 550;
  SIM.placeSatellite(1000, 500, SPEED, 0);
  let captured = false;
  for (let i = 0; i < 1000; i++) {
    SIM.step();
    if (SIM.isCrashed()) break;
    if (SIM.isOrbiting()) { captured = true; break; }
  }
  if (captured) {
    const orbitR = SIM.getOrbitRadius();
    const MAX_OMEGA = 2 * Math.PI / 3;
    const orbitSpeed = Math.min(SPEED, orbitR * MAX_OMEGA);
    const expectedPeriod = 2 * Math.PI * orbitR / orbitSpeed;
    const framesPerPeriod = expectedPeriod / SIM.TICK;
    const sx = SIM.getSatellite().x;
    const sy = SIM.getSatellite().y;
    for (let i = 0; i < Math.round(framesPerPeriod); i++) SIM.step();
    const dx = SIM.getSatellite().x - sx;
    const dy = SIM.getSatellite().y - sy;
    check('orbit period matches 2πr/v (within 5%)',
      Math.hypot(dx, dy) < 0.05 * orbitR);
  }
}

// Test 14: angular speed cap — for a small orbit the linear speed is
// reduced so the angular velocity never exceeds 2π/3 rad/s (period = 3s).
{
  SIM.init();
  for (const p of w.planets) { p.x = -5000; p.y = -5000; }
  const p0 = w.planets[0];
  p0.r = 20;
  // Place the planet so the closest approach is very small (orbitR ~ 25)
  p0.x = 1500; p0.y = 525; // 25 units below the path
  SIM.placeSatellite(1000, 500, SPEED, 0);
  let captured = false;
  for (let i = 0; i < 1000; i++) {
    SIM.step();
    if (SIM.isCrashed()) break;
    if (SIM.isOrbiting()) { captured = true; break; }
  }
  if (captured) {
    // Step one more tick so the orbit physics updates the velocity
    SIM.step();
    const orbitR = SIM.getOrbitRadius();
    const MAX_OMEGA = 2 * Math.PI / 3;
    const orbitSpeed = Math.min(SPEED, orbitR * MAX_OMEGA);
    const sp = Math.hypot(SIM.getSatellite().vx, SIM.getSatellite().vy);
    check('orbit speed is capped (sp == min(SPEED, orbitR*MAX_OMEGA))',
      Math.abs(sp - orbitSpeed) < 1e-6);
    // The angular velocity should be <= MAX_OMEGA
    const omega = sp / orbitR;
    check('angular velocity <= MAX_OMEGA', omega <= MAX_OMEGA + 1e-6);
  }
}

// Test 15: escape — satellite flies out of the world and is not near any
// planet's attraction circle.
{
  SIM.init();
  // Move all planets far away so the satellite is never captured
  for (const p of w.planets) { p.x = -10000; p.y = -10000; }
  // Launch the satellite moving right, out of the world
  SIM.placeSatellite(5500, 500, SPEED, 0);
  check('not escaped initially', SIM.isEscaped() === false);
  // Step until escaped or timeout (world width 6000, margin 200 → need to
  // travel 500+ units at 400 u/s ≈ 1.25 s ≈ 75 frames)
  let escaped = false;
  for (let i = 0; i < 300; i++) {
    SIM.step();
    if (SIM.isEscaped()) { escaped = true; break; }
  }
  check('satellite escaped after flying out of the world', escaped);
  if (escaped) {
    check('step stops after escape', SIM.getElapsed() > 0);
    const eAt = SIM.getElapsed();
    SIM.step();
    check('elapsed frozen after escape', SIM.getElapsed() === eAt);
  }
}

// Test 16: win — all astronauts collected.
{
  SIM.init();
  // Place the satellite on top of each astronaut one by one to collect
  // them all. Since astronauts orbit planets, we need to place the
  // satellite near each one.
  const astronauts = SIM.getAstronauts();
  check('has astronauts to collect', astronauts.length === 5);
  // Collect each astronaut by placing the satellite on top of them
  for (let i = astronauts.length - 1; i >= 0; i--) {
    const a = astronauts[i];
    SIM.placeSatellite(a.x, a.y, SPEED, 0);
    // Step a few ticks to trigger collection
    for (let j = 0; j < 10; j++) SIM.step();
  }
  check('all astronauts collected', SIM.getCollected() === 5);
  check('won after collecting all astronauts', SIM.isWon() === true);
  if (SIM.isWon()) {
    const eAt = SIM.getElapsed();
    SIM.step();
    check('elapsed frozen after win', SIM.getElapsed() === eAt);
  }
}

console.log(failures === 0 ? 'ALL TESTS PASSED' : failures + ' TEST(S) FAILED');
process.exit(failures === 0 ? 0 : 1);
