import fs from 'node:fs';
import vm from 'node:vm';
const code = fs.readFileSync(new URL('./src/simulation.js', import.meta.url), 'utf8');
const store = {};
const sandbox = { console, localStorage: { getItem: k => k in store ? store[k] : null, setItem: (k,v)=>{store[k]=String(v)}, removeItem: k=>{delete store[k]} } };
vm.createContext(sandbox);
vm.runInContext(code + '\nthis.__SIM = SIM;', sandbox);
const SIM = sandbox.__SIM;

SIM.init();
const w = SIM.getWorld();

// Isolate a two-body problem: one frozen planet + satellite.
// Move all planets to the far corner of the world (inside bounds so
// stepPlanets doesn't remove them) with zero mass and zero drift.
const keep = w.planets[0];
for (let i = 1; i < w.planets.length; i++) {
  w.planets[i].mass = 0;
  w.planets[i].x = 50;
  w.planets[i].y = 50;
  w.planets[i].vx = 0;
  w.planets[i].vy = 0;
}
// Prevent new planets from spawning by filling the cap
while (w.planets.length < 3) {
  w.planets.push({ x: 50, y: 50, r: 16, vx: 0, vy: 0, mass: 0, color: '#fff' });
}

const G = 5000;
const M = 50000;
keep.mass = M;
keep.x = 1500;  // inside world, far from BH center (1000, 600): dist ~781
keep.y = 300;
keep.vx = 0;    // freeze
keep.vy = 0;

// circular orbit radius
const r = 300;
const v = Math.sqrt(G * M / r);
SIM.placeSatellite(keep.x + r, keep.y, 0, v);

function energy() {
  const s = SIM.getSatellite();
  const ke = 0.5 * (s.vx * s.vx + s.vy * s.vy);
  const d = Math.hypot(keep.x - s.x, keep.y - s.y);
  const pe = -G * M / d;
  return ke + pe;
}

const e0 = energy();
let maxDrift = 0;
for (let i = 0; i < 20000; i++) {
  SIM.step();
  const drift = Math.abs(energy() - e0) / Math.abs(e0);
  if (drift > maxDrift) maxDrift = drift;
}
const s = SIM.getSatellite();
const dist = Math.hypot(keep.x - s.x, keep.y - s.y);
console.log('max energy drift over 20000 frames (RK4, SUBSTEPS=4):', maxDrift.toFixed(8));
console.log('final orbit radius:', dist.toFixed(2), '(expected ~300)');
const ok = maxDrift < 0.005 && Math.abs(dist - 300) < 20;
console.log(ok ? 'PASS: RK4 stable, orbit preserved' : 'FAIL');
process.exit(ok ? 0 : 1);
