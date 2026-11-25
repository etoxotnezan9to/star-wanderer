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
  // How the arrow keys map to steering: 'ship' — relative to the
  // satellite's nose; 'screen' — fixed screen directions.
  let controlMode = 'ship';
  const keysDown = new Set();
  const particles = [];

  const PARTICLE_LIFE = 30;
  const PARTICLE_COUNT_PER_FRAME = 3;
  // Trail look: opacity next to the satellite, distance (world units)
  // over which it fades to nothing, and how many opacity bands make up the
  // fade (more bands — smoother fade, a little more drawing work).
  const TRAIL_ALPHA = 0.7;
  const TRAIL_FADE_DISTANCE = 3000;
  const TRAIL_FADE_STEPS = 64;

  // The camera is locked to a fixed world scale (no zoom) and simply
  // follows the ship: the view center is the satellite, so the visible
  // region is whatever world is around the ship at this scale.
  const SCALE = 0.5; // screen pixels per world unit, fixed

  // The camera target is the satellite in flying mode and the orbit planet
  // while orbiting. The camera smoothly lerps towards the target each frame
  // so switching between the two feels like a gentle re-focus rather than a
  // jump.
  const CAMERA_LERP = 0.03; // fraction of the remaining distance per frame
  let camTargetX = world.width / 2;
  let camTargetY = world.height / 2;

  function updateCamera() {
    // Pick the target
    if (SIM.isOrbiting()) {
      const op = SIM.getOrbitPlanet();
      if (op) {
        camTargetX = op.x;
        camTargetY = op.y;
      }
    } else {
      camTargetX = sat.x;
      camTargetY = sat.y;
    }
    // Lerp the camera towards the target
    camera.x += (camTargetX - camera.x) * CAMERA_LERP;
    camera.y += (camTargetY - camera.y) * CAMERA_LERP;
    camera.zoom = SCALE;
  }

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

  // A simplified astronaut icon for off-screen astronauts: a helmet circle
  // with a visor and a small torso, drawn at a fixed screen size.
  function drawAstronautIcon(x, y, r) {
    ctx.save();
    // Torso
    ctx.fillStyle = '#e8edf5';
    ctx.strokeStyle = 'rgba(130, 150, 180, 0.8)';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.ellipse(x, y + r * 0.3, r * 0.4, r * 0.6, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    // Helmet
    ctx.beginPath();
    ctx.arc(x, y - r * 0.4, r * 0.45, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    // Visor
    ctx.fillStyle = 'rgba(30, 50, 80, 0.92)';
    ctx.beginPath();
    ctx.ellipse(x, y - r * 0.4, r * 0.3, r * 0.22, 0, 0, Math.PI * 2);
    ctx.fill();
    // Glow ring to make it stand out at the screen edge
    ctx.strokeStyle = 'rgba(120, 220, 255, 0.5)';
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.arc(x, y, r * 1.3, 0, Math.PI * 2);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();
  }

  function drawAstronauts() {
    const astronauts = SIM.getAstronauts();
    const dpr = window.devicePixelRatio || 1;
    const screenW = canvas.width / dpr;
    const screenH = canvas.height / dpr;
    for (const a of astronauts) {
      const c = toScreen(a.x, a.y);
      const r = a.r * camera.zoom;
      const margin = r * 2 + 20;
      const onScreen = c.x >= -margin && c.x <= screenW + margin &&
                       c.y >= -margin && c.y <= screenH + margin;
      if (!onScreen) {
        // Draw a small astronaut icon at the screen edge, on the ray from
        // the satellite to this astronaut.
        const sc = toScreen(sat.x, sat.y);
        const dx = c.x - sc.x;
        const dy = c.y - sc.y;
        // Find where this ray hits the screen border (with a small inset)
        const inset = 12;
        let t = Infinity;
        if (dx > 1e-9) t = Math.min(t, (screenW - inset - sc.x) / dx);
        if (dx < -1e-9) t = Math.min(t, (inset - sc.x) / dx);
        if (dy > 1e-9) t = Math.min(t, (screenH - inset - sc.y) / dy);
        if (dy < -1e-9) t = Math.min(t, (inset - sc.y) / dy);
        if (!isFinite(t) || t < 0) continue;
        const ex = sc.x + dx * t;
        const ey = sc.y + dy * t;
        drawAstronautIcon(ex, ey, 10);
        continue;
      }

      const suitColor = '#e8edf5';
      const suitOutline = 'rgba(130, 150, 180, 0.8)';
      const visorColor = 'rgba(30, 50, 80, 0.92)';
      const visorHighlight = 'rgba(160, 210, 255, 0.45)';
      const packColor = 'rgba(170, 180, 200, 0.75)';
      const jointColor = 'rgba(200, 210, 225, 0.9)';

      // Legs (drawn first, behind the torso)
      const legW = r * 0.38;
      const legLen = r * 0.75;
      // Left leg — slightly angled outward
      ctx.strokeStyle = suitOutline;
      ctx.lineWidth = legW;
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.moveTo(c.x - r * 0.15, c.y + r * 0.3);
      ctx.lineTo(c.x - r * 0.4, c.y + r * 0.3 + legLen);
      ctx.stroke();
      // Right leg — slightly angled outward
      ctx.beginPath();
      ctx.moveTo(c.x + r * 0.15, c.y + r * 0.3);
      ctx.lineTo(c.x + r * 0.4, c.y + r * 0.3 + legLen);
      ctx.stroke();
      // Boots
      ctx.fillStyle = suitColor;
      ctx.strokeStyle = suitOutline;
      ctx.lineWidth = 1;
      for (const side of [-1, 1]) {
        const bx = c.x + side * r * 0.4;
        const by = c.y + r * 0.3 + legLen;
        ctx.beginPath();
        ctx.arc(bx, by, legW * 0.75, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      }

      // Arms (behind torso)
      const armW = r * 0.3;
      const armLen = r * 0.65;
      // Left arm — angled outward and slightly down
      ctx.strokeStyle = suitOutline;
      ctx.lineWidth = armW;
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.moveTo(c.x - r * 0.3, c.y - r * 0.1);
      ctx.lineTo(c.x - r * 0.3 - armLen * 0.7, c.y - r * 0.1 + armLen * 0.7);
      ctx.stroke();
      // Right arm — angled outward and slightly up (waving)
      ctx.beginPath();
      ctx.moveTo(c.x + r * 0.3, c.y - r * 0.1);
      ctx.lineTo(c.x + r * 0.3 + armLen * 0.7, c.y - r * 0.1 - armLen * 0.5);
      ctx.stroke();
      // Gloves
      ctx.fillStyle = suitColor;
      ctx.strokeStyle = suitOutline;
      ctx.lineWidth = 1;
      // Left glove
      ctx.beginPath();
      ctx.arc(c.x - r * 0.3 - armLen * 0.7, c.y - r * 0.1 + armLen * 0.7, armW * 0.65, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      // Right glove
      ctx.beginPath();
      ctx.arc(c.x + r * 0.3 + armLen * 0.7, c.y - r * 0.1 - armLen * 0.5, armW * 0.65, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();

      // Torso (suit body) — slim
      ctx.fillStyle = suitColor;
      ctx.strokeStyle = suitOutline;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.ellipse(c.x, c.y, r * 0.38, r * 0.65, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();

      // Life support backpack (behind torso, visible on top)
      ctx.fillStyle = packColor;
      ctx.strokeStyle = suitOutline;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect(c.x - r * 0.25, c.y - r * 0.6, r * 0.5, r * 0.35, r * 0.06);
      ctx.fill();
      ctx.stroke();

      // Chest control panel
      ctx.fillStyle = 'rgba(100, 120, 150, 0.6)';
      ctx.fillRect(c.x - r * 0.12, c.y - r * 0.05, r * 0.24, r * 0.2);
      ctx.fillStyle = 'rgba(80, 200, 120, 0.8)';
      ctx.fillRect(c.x - r * 0.08, c.y, r * 0.07, r * 0.05);
      ctx.fillStyle = 'rgba(255, 180, 60, 0.8)';
      ctx.fillRect(c.x + r * 0.01, c.y, r * 0.07, r * 0.05);

      // Helmet
      ctx.fillStyle = suitColor;
      ctx.strokeStyle = suitOutline;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(c.x, c.y - r * 0.55, r * 0.4, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();

      // Visor
      ctx.fillStyle = visorColor;
      ctx.beginPath();
      ctx.ellipse(c.x, c.y - r * 0.55, r * 0.28, r * 0.22, 0, 0, Math.PI * 2);
      ctx.fill();

      // Visor highlight
      ctx.strokeStyle = visorHighlight;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(c.x - r * 0.08, c.y - r * 0.62, r * 0.12, Math.PI * 1.1, Math.PI * 1.8);
      ctx.stroke();

      // Joint connectors (shoulders, hips)
      ctx.fillStyle = jointColor;
      for (const jx of [-r * 0.28, r * 0.28]) {
        ctx.beginPath();
        ctx.arc(c.x + jx, c.y - r * 0.1, r * 0.14, 0, Math.PI * 2);
        ctx.fill();
      }
      for (const jx of [-r * 0.15, r * 0.15]) {
        ctx.beginPath();
        ctx.arc(c.x + jx, c.y + r * 0.3, r * 0.12, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }

  // Parse a hex color like '#e8b04b' into {r, g, b} (0–255).
  function hexToRgb(hex) {
    const n = parseInt(hex.slice(1), 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }

  // Light source direction (world coords, normalized). Fixed to upper-left.
  const LIGHT_X = -0.7;
  const LIGHT_Y = -0.7;

  function drawPlanets() {
    const dpr = window.devicePixelRatio || 1;
    const screenW = canvas.width / dpr;
    const screenH = canvas.height / dpr;
    for (const p of world.planets) {
      const c = toScreen(p.x, p.y);
      const r = p.r * camera.zoom;
      if (c.x < -r - 40 || c.x > screenW + r + 40 ||
          c.y < -r - 40 || c.y > screenH + r + 40) continue;

      const rgb = hexToRgb(p.color);

      // 1. Base sphere: radial gradient with the lit side brighter.
      //    Offset the gradient center toward the light source so the
      //    illuminated hemisphere is lighter and the far side falls off.
      const gx = c.x + LIGHT_X * r * 0.45;
      const gy = c.y + LIGHT_Y * r * 0.45;
      const grad = ctx.createRadialGradient(gx, gy, r * 0.1, c.x, c.y, r);
      const lr = Math.min(255, rgb.r + 70);
      const lg = Math.min(255, rgb.g + 70);
      const lb = Math.min(255, rgb.b + 70);
      grad.addColorStop(0, 'rgb(' + lr + ',' + lg + ',' + lb + ')');
      grad.addColorStop(0.55, 'rgb(' + rgb.r + ',' + rgb.g + ',' + rgb.b + ')');
      const dr = Math.max(0, rgb.r - 90);
      const dg = Math.max(0, rgb.g - 90);
      const db = Math.max(0, rgb.b - 90);
      grad.addColorStop(1, 'rgb(' + dr + ',' + dg + ',' + db + ')');
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(c.x, c.y, r, 0, Math.PI * 2);
      ctx.fill();

      // 2. Specular highlight: a small bright spot on the lit side.
      const hx = c.x + LIGHT_X * r * 0.55;
      const hy = c.y + LIGHT_Y * r * 0.55;
      const hs = r * 0.3;
      const hgrad = ctx.createRadialGradient(hx, hy, 0, hx, hy, hs);
      hgrad.addColorStop(0, 'rgba(255,255,255,0.55)');
      hgrad.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.fillStyle = hgrad;
      ctx.beginPath();
      ctx.arc(hx, hy, hs, 0, Math.PI * 2);
      ctx.fill();

      // 3. Atmosphere rim: a soft glow around the planet edge, stronger on
      //    the lit side.
      if (r > 20) {
        const atmo = ctx.createRadialGradient(c.x, c.y, r * 0.92, c.x, c.y, r * 1.15);
        atmo.addColorStop(0, 'rgba(' + lr + ',' + lg + ',' + lb + ',0)');
        atmo.addColorStop(0.5, 'rgba(' + lr + ',' + lg + ',' + lb + ',0.12)');
        atmo.addColorStop(1, 'rgba(' + lr + ',' + lg + ',' + lb + ',0)');
        ctx.fillStyle = atmo;
        ctx.beginPath();
        ctx.arc(c.x, c.y, r * 1.15, 0, Math.PI * 2);
        ctx.fill();
      }

      // 3.5. Attraction circle: a dashed circle showing the radius within
      //      which the satellite gets captured into orbit.
      const ar = SIM.getAttractionRadius(p);
      const arScreen = ar * camera.zoom;
      if (arScreen > 5) {
        ctx.save();
        ctx.strokeStyle = 'rgba(100, 180, 255, 0.35)';
        ctx.lineWidth = 1.5;
        ctx.setLineDash([6, 4]);
        ctx.beginPath();
        ctx.arc(c.x, c.y, arScreen, 0, Math.PI * 2);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.restore();
      }

      // 4. Terminator shadow: darken the side away from the light with a
      //    linear gradient perpendicular to the light direction.
      const lx = -LIGHT_X;
      const ly = -LIGHT_Y;
      const llen = Math.hypot(lx, ly);
      const nx = lx / llen;
      const ny = ly / llen;
      const shStartX = c.x + nx * r;
      const shStartY = c.y + ny * r;
      const shEndX = c.x - nx * r * 0.3;
      const shEndY = c.y - ny * r * 0.3;
      const shadow = ctx.createLinearGradient(shStartX, shStartY, shEndX, shEndY);
      shadow.addColorStop(0, 'rgba(0,0,0,0)');
      shadow.addColorStop(0.4, 'rgba(0,0,0,0.15)');
      shadow.addColorStop(1, 'rgba(0,0,0,0.45)');
      ctx.fillStyle = shadow;
      ctx.beginPath();
      ctx.arc(c.x, c.y, r, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  // The trail fades with the distance flown since that point: opaque
  // (TRAIL_ALPHA) next to the satellite, fully transparent once the
  // satellite has flown TRAIL_FADE_DISTANCE world units further along it.
  //
  // The trail is split into TRAIL_FADE_STEPS bands of equal opacity, one
  // stroke per band, laid end to end. The bands are drawn additively
  // ('lighter'): with ordinary blending the anti-aliased ends of two
  // neighbouring strokes overlap at the joint and the line visibly dims
  // there, a dot at every band boundary; added together, the two partial
  // ends give exactly the coverage of an unbroken line, so the trail stays
  // even. On the dark sky the additive look matches ordinary blending.
  function drawTrail() {
    const trail = sat.trail;
    if (!showTrail || trail.length < 2) return;
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'butt';
    let dist = 0;
    let band = -1;
    let prev = toScreen(trail[trail.length - 1].x, trail[trail.length - 1].y);
    for (let i = trail.length - 2; i >= 0; i--) {
      const a = trail[i + 1];
      const b = trail[i];
      dist += Math.hypot(a.x - b.x, a.y - b.y);
      const fade = 1 - dist / TRAIL_FADE_DISTANCE;
      if (fade <= 0) break;
      const p = toScreen(b.x, b.y);
      const newBand = Math.ceil(fade * TRAIL_FADE_STEPS);
      if (newBand !== band) {
        if (band >= 0) ctx.stroke();
        band = newBand;
        ctx.strokeStyle = 'rgba(120, 220, 255, ' +
          (TRAIL_ALPHA * band / TRAIL_FADE_STEPS).toFixed(4) + ')';
        ctx.beginPath();
        ctx.moveTo(prev.x, prev.y);
      }
      ctx.lineTo(p.x, p.y);
      prev = p;
    }
    if (band >= 0) ctx.stroke();
    ctx.restore();
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

  // While orbiting, draw a dashed line from the satellite in the direction
  // of its current velocity (tangential), extending to the screen border.
  function drawOrbitVelocityHint() {
    const c = toScreen(sat.x, sat.y);
    const sp = Math.hypot(sat.vx, sat.vy);
    if (sp < 1e-9) return;
    const ux = sat.vx / sp;
    const uy = sat.vy / sp;
    const dpr = window.devicePixelRatio || 1;
    const sw = canvas.width / dpr;
    const sh = canvas.height / dpr;
    let t = Infinity;
    if (ux > 1e-9) t = Math.min(t, (sw - c.x) / ux);
    if (ux < -1e-9) t = Math.min(t, -c.x / ux);
    if (uy > 1e-9) t = Math.min(t, (sh - c.y) / uy);
    if (uy < -1e-9) t = Math.min(t, -c.y / uy);
    if (!isFinite(t) || t < 0) return;
    const ex = c.x + ux * t;
    const ey = c.y + uy * t;
    ctx.save();
    ctx.strokeStyle = 'rgba(120, 220, 255, 0.25)';
    ctx.lineWidth = 2;
    ctx.setLineDash([8, 6]);
    ctx.beginPath();
    ctx.moveTo(c.x, c.y);
    ctx.lineTo(ex, ey);
    ctx.stroke();
    // Arrowhead at the end
    ctx.setLineDash([]);
    const headLen = 10;
    const headAngle = 0.4;
    const a = Math.atan2(uy, ux);
    ctx.beginPath();
    ctx.moveTo(ex, ey);
    ctx.lineTo(ex - headLen * Math.cos(a - headAngle), ey - headLen * Math.sin(a - headAngle));
    ctx.moveTo(ex, ey);
    ctx.lineTo(ex - headLen * Math.cos(a + headAngle), ey - headLen * Math.sin(a + headAngle));
    ctx.stroke();
    ctx.restore();
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
    // While orbiting, steering is disabled — only Space (break orbit) works.
    if (SIM.isOrbiting()) return null;
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
    if (controlMode === 'screen') {
      // Screen axes: "up" is towards the top of the screen, "right" towards
      // its right edge, whatever the satellite's heading. The camera does
      // not rotate or flip, so screen axes are world axes with y down.
      return { x: nly, y: -nlx };
    }
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

  function drawAstronautCounter() {
    const collected = SIM.getCollected();
    const total = SIM.getTotalAstronauts();
    const dpr = window.devicePixelRatio || 1;
    const x = canvas.width / dpr - 16;
    const y = 20;
    // Mini astronaut icon with limbs
    const s = 10;
    const ix = x - 60;
    const iy = y + 8;
    ctx.lineCap = 'round';
    // Legs
    ctx.strokeStyle = 'rgba(130, 150, 180, 0.9)';
    ctx.lineWidth = 2.5;
    ctx.beginPath(); ctx.moveTo(ix - 2, iy + 3); ctx.lineTo(ix - 5, iy + 10); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(ix + 2, iy + 3); ctx.lineTo(ix + 5, iy + 10); ctx.stroke();
    // Arms
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(ix - 5, iy - 2); ctx.lineTo(ix - 9, iy + 2); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(ix + 5, iy - 2); ctx.lineTo(ix + 9, iy - 5); ctx.stroke();
    // Torso
    ctx.fillStyle = '#e8edf5';
    ctx.strokeStyle = 'rgba(130, 150, 180, 0.8)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.ellipse(ix, iy, 4, 5, 0, 0, Math.PI * 2);
    ctx.fill(); ctx.stroke();
    // Helmet
    ctx.beginPath();
    ctx.arc(ix, iy - 5, 3.5, 0, Math.PI * 2);
    ctx.fill(); ctx.stroke();
    // Visor
    ctx.fillStyle = 'rgba(30, 50, 80, 0.9)';
    ctx.beginPath();
    ctx.ellipse(ix, iy - 5, 2.2, 1.8, 0, 0, Math.PI * 2);
    ctx.fill();
    // Counter text
    ctx.fillStyle = 'rgba(220, 235, 255, 0.95)';
    ctx.font = 'bold 16px system-ui, sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(collected + ' / ' + total, x, y + 14);
    ctx.textAlign = 'left';
  }

  // No black hole in the world — there is nothing to draw here. Kept as a
  // no-op so the frame loop stays simple.
  function drawEventHorizon() {}

  function drawGameOver() {
    if (!sat.crashed && !SIM.isEscaped() && !SIM.isWon()) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.width / dpr;
    const h = canvas.height / dpr;
    const isWon = SIM.isWon();
    const isEscaped = SIM.isEscaped();
    // Dark overlay
    ctx.fillStyle = 'rgba(0, 0, 0, 0.6)';
    ctx.fillRect(0, 0, w, h);
    // Panel
    const pw = Math.min(420, w - 40);
    const ph = 200;
    const px = (w - pw) / 2;
    const py = (h - ph) / 2;
    ctx.fillStyle = 'rgba(15, 20, 40, 0.95)';
    ctx.strokeStyle = isWon ? 'rgba(80, 220, 120, 0.7)'
      : isEscaped ? 'rgba(255, 200, 80, 0.6)'
        : 'rgba(255, 120, 80, 0.6)';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.roundRect(px, py, pw, ph, 12);
    ctx.fill();
    ctx.stroke();
    // Title
    ctx.fillStyle = 'rgba(220, 200, 255, 0.95)';
    ctx.font = 'bold 24px system-ui, sans-serif';
    ctx.textAlign = 'center';
    if (isWon) {
      ctx.fillText('Успешное прохождение!', w / 2, py + 40);
    } else if (isEscaped) {
      ctx.fillText('Полёт завершён', w / 2, py + 40);
    } else {
      ctx.fillText('Полёт завершён', w / 2, py + 40);
    }
    // Stats
    const collected = SIM.getCollected();
    const total = SIM.getTotalAstronauts();
    const elapsed = SIM.getElapsed();
    const mins = Math.floor(elapsed / 60);
    const secs = Math.floor(elapsed % 60);
    ctx.font = '18px system-ui, sans-serif';
    ctx.fillStyle = 'rgba(180, 200, 230, 0.9)';
    if (isWon) {
      ctx.fillText('Все космонавты спасены!', w / 2, py + 80);
    } else if (isEscaped) {
      ctx.fillText('Спутник улетел за пределы мира', w / 2, py + 80);
    } else {
      ctx.fillText('Спутник упал на планету', w / 2, py + 80);
    }
    ctx.fillText('Космонавты: ' + collected + ' / ' + total +
      '   Время: ' + mins + ':' + String(secs).padStart(2, '0'), w / 2, py + 110);
    // Prompt
    ctx.fillStyle = 'rgba(255, 220, 100, 0.9)';
    ctx.font = '14px system-ui, sans-serif';
    ctx.fillText('Нажмите любую клавишу для нового полёта', w / 2, py + ph - 25);
    ctx.textAlign = 'left';
  }

  function drawHud() {
    const speed = SIM.speed();
    ctx.font = '13px system-ui, sans-serif';
    if (sat.crashed) {
      ctx.fillStyle = 'rgba(255, 120, 80, 0.95)';
      ctx.fillText('Спутник упал на планету — полёт завершён. Нажмите любую клавишу для сброса.', 16, window.innerHeight - 16);
    } else if (SIM.isEscaped()) {
      ctx.fillStyle = 'rgba(255, 200, 80, 0.95)';
      ctx.fillText('Спутник улетел за пределы мира — полёт завершён. Нажмите любую клавишу для сброса.', 16, window.innerHeight - 16);
    } else if (SIM.isWon()) {
      ctx.fillStyle = 'rgba(80, 220, 120, 0.95)';
      ctx.fillText('Все космонавты спасены! Нажмите любую клавишу для нового полёта.', 16, window.innerHeight - 16);
    } else {
      ctx.fillStyle = 'rgba(180, 200, 225, 0.85)';
      const elapsed = SIM.getElapsed();
      const mins = Math.floor(elapsed / 60);
      const secs = Math.floor(elapsed % 60);
      let line = 'Скорость: ' + speed.toFixed(1) + ' ед/с   Время: ' + mins + ':' + String(secs).padStart(2, '0');
      if (SIM.isOrbiting()) {
        line += '   [Орбита: r=' + SIM.getOrbitRadius().toFixed(0) + ', ПРОБЕЛ — оторваться]';
      }
      ctx.fillText(line, 16, window.innerHeight - 16);
    }
    drawAstronautCounter();
  }

  // One fixed simulation tick (SIM.TICK seconds of game time): thrust,
  // physics, fuel and exhaust particles all advance by the same amount.
  function tick() {
    if (!sat.crashed && !SIM.isEscaped() && !SIM.isWon()) {
      if (keysDown.has(' ') && SIM.isOrbiting()) {
        SIM.breakOrbit();
      }
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
  }

  // Game speed follows real time, not the display refresh rate: the real
  // time elapsed since the previous frame is accumulated and spent in fixed
  // ticks, so 60, 144 or 240 Hz screens run the flight at the same speed.
  // A long pause (hidden tab, debugger) is clamped so the game does not
  // try to catch up with hundreds of ticks at once.
  const MAX_FRAME_TIME = 0.25;
  let lastTime = null;
  let accumulator = 0;

  function frame(now) {
    if (lastTime === null) lastTime = now;
    const frameTime = Math.min(MAX_FRAME_TIME, Math.max(0, (now - lastTime) / 1000));
    lastTime = now;
    accumulator += frameTime;
    while (accumulator >= SIM.TICK) {
      tick();
      accumulator -= SIM.TICK;
    }
    updateCamera();
    ctx.fillStyle = '#04060d';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    drawStars();
    drawEventHorizon();
    drawTrail();
    drawAstronauts();
    drawPlanets();
    if (sat.crashed) {
      drawCrashMarker();
    } else {
      drawSatellite();
      if (SIM.isOrbiting()) drawOrbitVelocityHint();
    }
    drawParticles();
    drawHud();
    drawGameOver();
    requestAnimationFrame(frame);
  }

  const panel = document.getElementById('panel');

  document.getElementById('btnReset').addEventListener('click', () => {
    SIM.resetSatellite();
    camera.x = sat.x;
    camera.y = sat.y;
    camTargetX = sat.x;
    camTargetY = sat.y;
  });

  document.getElementById('btnPlanets').addEventListener('click', () => {
    SIM.randomizePlanets();
    camera.x = sat.x;
    camera.y = sat.y;
    camTargetX = sat.x;
    camTargetY = sat.y;
  });

  window.addEventListener('resize', resize);

  window.addEventListener('keydown', (e) => {
    if (sat.crashed || SIM.isEscaped() || SIM.isWon()) {
      SIM.resetSatellite();
      camera.x = sat.x;
      camera.y = sat.y;
      camTargetX = sat.x;
      camTargetY = sat.y;
      keysDown.clear();
      return;
    }
    if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', ' '].includes(e.key)) {
      e.preventDefault();
      keysDown.add(e.key);
    }
  });

  window.addEventListener('keyup', (e) => {
    if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', ' '].includes(e.key)) {
      keysDown.delete(e.key);
    }
  });

  window.addEventListener('blur', () => {
    keysDown.clear();
  });

  resize();
  SIM.init();
  // Snap the camera to the satellite on first frame so there is no lerp
  // from the initial world-center position.
  camera.x = sat.x;
  camera.y = sat.y;
  camTargetX = sat.x;
  camTargetY = sat.y;
  updateCamera();
  requestAnimationFrame(frame);
})();
