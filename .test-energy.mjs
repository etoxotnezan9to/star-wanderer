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

// Isolate a two-body problem: one frozen planet + satellite. All other
// planets are pushed far away (mass 0) so the only field is the kept one.
const keep = w.planets[0];
for (let i = 1; i < w.planets.length; i++) {
  w.planets[i].mass = 0;
  w.planets[i].x = 19000;
  w.planets[i].y = 100;
  w.planets[i].vx = 0;
  w.planets[i].vy = 0;
}
while (w.planets.length < 4) {
  w.planets.push({ x: 19000, y: 100, r: 16, vx: 0, vy: 0, mass: 0, color: '#fff' });
}

const G = 5000;
const M = 50000;
keep.mass = M;
keep.x = 1700;
keep.y = 900;
keep.vx = 0;
keep.vy = 0;
keep.r = 10;

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
for (let i = 0; i < 500; i++) {
  SIM.step();
  if (SIM.isCrashed()) break;
  const drift = Math.abs(energy() - e0) / Math.abs(e0);
  if (drift > maxDrift) maxDrift = drift;
}
const s = SIM.getSatellite();
console.log('max energy drift over 500 frames (RK4, SUBSTEPS=4):', maxDrift.toFixed(8));
const ok = maxDrift < 0.002;
console.log(ok ? 'PASS: RK4 stable, energy conserved' : 'FAIL');
process.exit(ok ? 0 : 1);
