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

// Isolate a two-body problem
const keep = w.planets[0];
for (let i = 1; i < w.planets.length; i++) {
  w.planets[i].mass = 0;
  w.planets[i].x = 1e9;
  w.planets[i].y = 1e9;
}
const G = 5000;
const M = 50000;
keep.mass = M;
keep.x = 1000;
keep.y = 600;

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
const ok = maxDrift < 0.001 && Math.abs(dist - 300) < 5;
console.log(ok ? 'PASS: RK4 stable, orbit preserved' : 'FAIL');
process.exit(ok ? 0 : 1);
