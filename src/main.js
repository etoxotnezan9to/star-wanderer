'use strict';

(() => {
  const canvas = document.getElementById('scene');
  const ctx = canvas.getContext('2d');

  const world = SIM.getWorld();
  const sat = SIM.getSatellite();

  const camera = {
    x: world.width / 2,
    y: world.height / 2,
    zoom: 1,
  };

  let showTrail = true;
  let placing = null;
  const keysDown = new Set();
  const particles = [];

  const MIN_ZOOM = 0.05;
  const MAX_ZOOM = 20;
  const PARTICLE_LIFE = 30;
  const PARTICLE_COUNT_PER_FRAME = 3;

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    const w = window.innerWidth;
    const h = window.innerHeight;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function fitCamera() {
    const dpr = 1;
    const w = canvas.width / dpr;
    const h = canvas.height / dpr;
    if (w <= 0 || h <= 0) return;
    camera.zoom = Math.min(w / world.width, h / world.height);
    camera.x = world.width / 2;
    camera.y = world.height / 2;
  }

  function toScreen(px, py) {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.width / dpr;
    const h = canvas.height / dpr;
    return {
      x: (px - camera.x) * camera.zoom + w / 2,
      y: (py - camera.y) * camera.zoom + h / 2,
    };
  }

  function toWorld(sx, sy) {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.width / dpr;
    const h = canvas.height / dpr;
    return {
      x: (sx - w / 2) / camera.zoom + camera.x,
      y: (sy - h / 2) / camera.zoom + camera.y,
    };
  }

  // Star field generated procedurally on the visible screen range.
  // Density is constant in world units (1 star per ~55 world-units), so the
  // background covers the whole screen at any zoom. When zoomed in so far that
  // the grid places fewer than MIN_STARS, the remaining visible area is filled
  // with extra stars so the sky never looks empty.
  const STAR_SPACING = 55;
  const MIN_STARS = 60;
  function hash2(ix, iy) {
    let h = Math.imul(ix, 374761393) + Math.imul(iy, 668265263);
    h = Math.imul(h ^ (h >>> 13), 1274126177);
    return ((h ^ (h >>> 16)) >>> 0) / 4294967295;
  }

  function drawStars() {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.width / dpr;
    const h = canvas.height / dpr;
    const topLeft = toWorld(0, 0);
    const bottomRight = toWorld(w, h);
    const pad = STAR_SPACING;
    const minX = topLeft.x - pad;
    const maxX = bottomRight.x + pad;
    const minY = topLeft.y - pad;
    const maxY = bottomRight.y + pad;
    const step = STAR_SPACING;
    const stars = [];
    for (let ix = Math.floor(minX / step); ix * step <= maxX; ix++) {
      for (let iy = Math.floor(minY / step); iy * step <= maxY; iy++) {
        const n = hash2(ix, iy);
        if (n > 0.72) continue;
        const ox = (hash2(ix * 3 + 1, iy * 7 + 2) - 0.5) * step;
        const oy = (hash2(ix * 5 + 3, iy * 11 + 4) - 0.5) * step;
        const wx = ix * step + ox;
        const wy = iy * step + oy;
        const p = toScreen(wx, wy);
        if (p.x < -2 || p.x > w + 2 || p.y < -2 || p.y > h + 2) continue;
        const radius = 0.5 + hash2(ix * 13 + 5, iy * 17 + 6) * 1.2;
        const alpha = 0.25 + hash2(ix * 19 + 7, iy * 23 + 8) * 0.65;
        stars.push({ x: p.x, y: p.y, r: radius, a: alpha });
      }
    }
    // Fill with extra stars if the grid produced too few (deep zoom-in)
    if (stars.length < MIN_STARS) {
      const need = MIN_STARS - stars.length;
      for (let i = 0; i < need; i++) {
        const n1 = hash2(1000 + i, 7);
        const n2 = hash2(2000 + i, 13);
        const n3 = hash2(3000 + i, 19);
        const n4 = hash2(4000 + i, 23);
        const n5 = hash2(5000 + i, 29);
        stars.push({
          x: n1 * w,
          y: n2 * h,
          r: 0.5 + n3 * 1.2,
          a: 0.25 + n4 * 0.65,
        });
      }
    }
    ctx.fillStyle = '#ffffff';
    for (const s of stars) {
      ctx.globalAlpha = s.a;
      ctx.beginPath();
      ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  function drawFuelBarrels() {
    const barrels = SIM.getFuelBarrels();
    for (const b of barrels) {
      const c = toScreen(b.x, b.y);
      const r = b.r * camera.zoom;
      if (c.x < -r - 20 || c.x > canvas.width / (window.devicePixelRatio || 1) + r + 20 ||
          c.y < -r - 20 || c.y > canvas.height / (window.devicePixelRatio || 1) + r + 20) {
        continue;
      }
      // Barrel body: circle with a lightning bolt icon inside
      ctx.fillStyle = 'rgba(30, 60, 120, 0.6)';
      ctx.beginPath();
      ctx.arc(c.x, c.y, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = 'rgba(120, 180, 255, 0.8)';
      ctx.lineWidth = 2;
      ctx.stroke();
      // Lightning bolt: a zigzag path inside the barrel
      const s = r * 0.6;
      ctx.fillStyle = '#ffe060';
      ctx.beginPath();
      ctx.moveTo(c.x + s * 0.1, c.y - s * 0.7);
      ctx.lineTo(c.x - s * 0.3, c.y + s * 0.1);
      ctx.lineTo(c.x + s * 0.1, c.y + s * 0.1);
      ctx.lineTo(c.x - s * 0.1, c.y + s * 0.7);
      ctx.lineTo(c.x + s * 0.4, c.y - s * 0.1);
      ctx.lineTo(c.x + s * 0.0, c.y - s * 0.1);
      ctx.closePath();
      ctx.fill();
    }
  }

  function drawPlanets() {
    for (const p of world.planets) {
      const c = toScreen(p.x, p.y);
      const r = p.r * camera.zoom;
      ctx.fillStyle = p.color;
      ctx.beginPath();
      ctx.arc(c.x, c.y, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = 'rgba(255,255,255,0.25)';
      ctx.lineWidth = 1;
      ctx.stroke();
    }
  }

  function drawTrail() {
    if (!showTrail || sat.trail.length < 2) return;
    ctx.strokeStyle = 'rgba(120, 220, 255, 0.7)';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    const first = toScreen(sat.trail[0].x, sat.trail[0].y);
    ctx.moveTo(first.x, first.y);
    for (let i = 1; i < sat.trail.length; i++) {
      const p = toScreen(sat.trail[i].x, sat.trail[i].y);
      ctx.lineTo(p.x, p.y);
    }
    ctx.stroke();
  }

  function drawSatellite() {
    const c = toScreen(sat.x, sat.y);
    const a = SIM.getAngle();
    const size = 10;
    // Nose triangle pointing in the heading direction
    const noseX = c.x + Math.cos(a) * size;
    const noseY = c.y + Math.sin(a) * size;
    const leftX = c.x + Math.cos(a + Math.PI * 0.8) * size * 0.7;
    const leftY = c.y + Math.sin(a + Math.PI * 0.8) * size * 0.7;
    const rightX = c.x + Math.cos(a - Math.PI * 0.8) * size * 0.7;
    const rightY = c.y + Math.sin(a - Math.PI * 0.8) * size * 0.7;
    ctx.fillStyle = '#ffffff';
    ctx.beginPath();
    ctx.moveTo(noseX, noseY);
    ctx.lineTo(leftX, leftY);
    ctx.lineTo(rightX, rightY);
    ctx.closePath();
    ctx.fill();
    ctx.strokeStyle = 'rgba(255,255,255,0.4)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(c.x, c.y, size + 3, 0, Math.PI * 2);
    ctx.stroke();
  }

  function drawPlacementArrow() {
    if (!placing) return;
    const s = toScreen(placing.x, placing.y);
    const e = toScreen(placing.ex, placing.ey);
    ctx.strokeStyle = 'rgba(255, 210, 90, 0.9)';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(s.x, s.y);
    ctx.lineTo(e.x, e.y);
    ctx.stroke();
    const dx = e.x - s.x;
    const dy = e.y - s.y;
    const len = Math.hypot(dx, dy);
    if (len > 4) {
      const ux = dx / len;
      const uy = dy / len;
      ctx.fillStyle = 'rgba(255, 210, 90, 0.9)';
      ctx.beginPath();
      ctx.moveTo(e.x, e.y);
      ctx.lineTo(e.x - ux * 10 - uy * 5, e.y - uy * 10 + ux * 5);
      ctx.lineTo(e.x - ux * 10 + uy * 5, e.y - uy * 10 - ux * 5);
      ctx.closePath();
      ctx.fill();
    }
    ctx.fillStyle = 'rgba(255, 210, 90, 0.9)';
    ctx.beginPath();
    ctx.arc(s.x, s.y, 5, 0, Math.PI * 2);
    ctx.fill();
  }

  function drawCrashMarker() {
    if (!sat.crashed || !sat.crashedPlanet) return;
    const p = sat.crashedPlanet;
    const c = toScreen(p.x, p.y);
    ctx.fillStyle = 'rgba(255, 90, 60, 0.35)';
    ctx.beginPath();
    ctx.arc(c.x, c.y, p.r * camera.zoom + 6, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = 'rgba(255, 120, 80, 0.9)';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(c.x, c.y, p.r * camera.zoom + 6, 0, Math.PI * 2);
    ctx.stroke();
  }

  // Compute the combined thrust direction from held arrow keys in the
  // satellite's local coordinate frame, rotated into world coordinates by
  // the current heading (nose angle).
  // Local frame: +x = forward (nose direction), +y = right (starboard).
  // Screen y grows downward, so the rotation from local to world is:
  //   world_x = lx * cos a - ly * sin a
  //   world_y = lx * sin a + ly * cos a
  //   (a = heading angle, nose points along world (cos a, sin a))
  // Arrow keys map to local frame:
  //   ArrowUp    = forward  = local (+1, 0)
  //   ArrowDown  = backward = local (-1, 0)
  //   ArrowLeft  = left     = local (0, -1)
  //   ArrowRight = right    = local (0, +1)
  // Check: ArrowUp at a=0 (nose along +x): world = (1, 0) = +x  ✓ (forward)
  //        ArrowUp at a=PI/2 (nose along +y): world = (0, 1) = +y  ✓ (forward)
  //        ArrowRight at a=0: local (0,1) → world = (-1, 0)?? No:
  //          world_x = 0*cos0 - 1*sin0 = 0, world_y = 0*sin0 + 1*cos0 = 1 → (0,1)
  //          But nose is +x, right of nose should be +y (down). ✓
  function currentThrustDir() {
    let lx = 0;
    let ly = 0;
    if (keysDown.has('ArrowUp')) lx += 1;
    if (keysDown.has('ArrowDown')) lx -= 1;
    if (keysDown.has('ArrowLeft')) ly -= 1;
    if (keysDown.has('ArrowRight')) ly += 1;
    const len = Math.hypot(lx, ly);
    if (len < 1e-9) return null;
    const nlx = lx / len;
    const nly = ly / len;
    const a = SIM.getAngle();
    const cos = Math.cos(a);
    const sin = Math.sin(a);
    const wx = nlx * cos - nly * sin;
    const wy = nlx * sin + nly * cos;
    return { x: wx, y: wy };
  }

  function emitThrustParticles() {
    if (!SIM.isThrusting()) {
      particles.length = 0;
      return;
    }
    // thrustDir stores the exhaust direction (opposite to acceleration)
    const dir = SIM.getThrustDir();
    const c = toScreen(sat.x, sat.y);
    const ex = dir.x;
    const ey = dir.y;
    for (let i = 0; i < PARTICLE_COUNT_PER_FRAME; i++) {
      const spread = (Math.random() - 0.5) * 0.6;
      const cos = Math.cos(spread);
      const sin = Math.sin(spread);
      const px = ex * cos - ey * sin;
      const py = ex * sin + ey * cos;
      const speed = (1.5 + Math.random() * 2.5) * camera.zoom;
      particles.push({
        x: c.x + ex * 12,
        y: c.y + ey * 12,
        vx: px * speed,
        vy: py * speed,
        life: PARTICLE_LIFE,
      });
    }
  }

  function updateParticles() {
    for (let i = particles.length - 1; i >= 0; i--) {
      const p = particles[i];
      p.x += p.vx;
      p.y += p.vy;
      p.vx *= 0.96;
      p.vy *= 0.96;
      p.life--;
      if (p.life <= 0) {
        particles.splice(i, 1);
      }
    }
  }

  function drawParticles() {
    if (particles.length === 0) return;
    for (const p of particles) {
      const t = p.life / PARTICLE_LIFE;
      const r = 2 + (1 - t) * 3;
      const alpha = t * 0.9;
      const hue = 20 + t * 30;
      ctx.globalAlpha = alpha;
      ctx.fillStyle = 'hsl(' + hue + ', 100%, ' + (50 + t * 20) + '%)';
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  function drawFuelBar() {
    const fuel = SIM.getFuel();
    const max = SIM.getFuelMax();
    const frac = max > 0 ? fuel / max : 0;
    const barW = 160;
    const barH = 14;
    const x = 16;
    const y = 56;
    ctx.fillStyle = 'rgba(10, 18, 35, 0.85)';
    ctx.fillRect(x - 2, y - 2, barW + 4, barH + 4);
    ctx.strokeStyle = 'rgba(90, 130, 200, 0.7)';
    ctx.lineWidth = 1;
    ctx.strokeRect(x - 2, y - 2, barW + 4, barH + 4);
    const fillColor = frac > 0.5 ? '#5fd68a' : frac > 0.2 ? '#e8c84b' : '#e0604a';
    ctx.fillStyle = fillColor;
    ctx.fillRect(x, y, barW * frac, barH);
    ctx.fillStyle = 'rgba(220, 235, 255, 0.9)';
    ctx.font = '11px system-ui, sans-serif';
    ctx.fillText('Топливо: ' + Math.round(fuel) + '%', x, y - 6);
  }

  function drawHud() {
    const speed = SIM.speed();
    ctx.font = '13px system-ui, sans-serif';
    if (sat.crashed) {
      ctx.fillStyle = 'rgba(255, 120, 80, 0.95)';
      ctx.fillText('Спутник упал на планету — полёт завершён. Кнопка «Сброс полёта» или новая установка мышью.', 16, window.innerHeight - 16);
    } else {
      ctx.fillStyle = 'rgba(180, 200, 225, 0.85)';
      ctx.fillText('Скорость: ' + speed.toFixed(1) + ' ед/с', 16, window.innerHeight - 16);
    }
    drawFuelBar();
  }

  function frame() {
    if (!sat.crashed) {
      const dir = currentThrustDir();
      if (dir) {
        SIM.thrust(dir.x, dir.y);
      } else {
        SIM.setThrustDir(null);
      }
      SIM.step();
    } else {
      SIM.setThrustDir(null);
    }
    emitThrustParticles();
    updateParticles();
    ctx.fillStyle = '#04060d';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    drawStars();
    drawTrail();
    drawFuelBarrels();
    drawPlanets();
    if (sat.crashed) {
      drawCrashMarker();
    } else {
      drawSatellite();
    }
    drawParticles();
    drawPlacementArrow();
    drawHud();
    requestAnimationFrame(frame);
  }

  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const before = toWorld(e.offsetX, e.offsetY);
    const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
    camera.zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, camera.zoom * factor));
    const after = toWorld(e.offsetX, e.offsetY);
    camera.x += before.x - after.x;
    camera.y += before.y - after.y;
  }, { passive: false });

  canvas.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    const w = toWorld(e.offsetX, e.offsetY);
    placing = { x: w.x, y: w.y, ex: w.x, ey: w.y };
    canvas.setPointerCapture(e.pointerId);
  });

  canvas.addEventListener('pointermove', (e) => {
    if (!placing) return;
    const w = toWorld(e.offsetX, e.offsetY);
    placing.ex = w.x;
    placing.ey = w.y;
  });

  canvas.addEventListener('pointerup', (e) => {
    if (!placing || e.button !== 0) return;
    const vx = (placing.ex - placing.x) * 2;
    const vy = (placing.ey - placing.y) * 2;
    SIM.placeSatellite(placing.x, placing.y, vx, vy);
    placing = null;
  });

  const btnTrail = document.getElementById('btnTrail');
  btnTrail.addEventListener('click', () => {
    showTrail = !showTrail;
    btnTrail.classList.toggle('active', showTrail);
    btnTrail.textContent = showTrail ? 'След: да' : 'След: нет';
  });

  document.getElementById('btnReset').addEventListener('click', () => {
    SIM.resetSatellite();
  });

  document.getElementById('btnPlanets').addEventListener('click', () => {
    SIM.randomizePlanets();
  });

  document.getElementById('btnZoomIn').addEventListener('click', () => {
    camera.zoom = Math.min(MAX_ZOOM, camera.zoom * 1.4);
  });

  document.getElementById('btnZoomOut').addEventListener('click', () => {
    camera.zoom = Math.max(MIN_ZOOM, camera.zoom / 1.4);
  });

  window.addEventListener('resize', resize);

  window.addEventListener('keydown', (e) => {
    if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) {
      e.preventDefault();
      keysDown.add(e.key);
    }
  });

  window.addEventListener('keyup', (e) => {
    if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) {
      keysDown.delete(e.key);
    }
  });

  window.addEventListener('blur', () => {
    keysDown.clear();
  });

  resize();
  fitCamera();
  SIM.init();
  requestAnimationFrame(frame);
})();
