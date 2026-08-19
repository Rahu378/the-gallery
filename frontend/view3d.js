/* Three-dimensional circuit view.
 *
 * A second way of looking at the same race, not a replacement. Top-down 2D is
 * the better instrument for reading gaps and order, which is why every timing
 * screen uses it — this exists to show the thing 2D cannot: elevation. Spa
 * climbs 102 m through Eau Rouge and Raidillon; Monza moves 12 m across the
 * whole lap. Both numbers come from the Z channel in position telemetry, so
 * the shape is measured rather than styled.
 *
 * Bloom is done with additive sprites rather than a post-processing pass. It
 * gets most of the look for none of the pipeline, and keeps the vendored
 * dependency to three core.
 */
import * as THREE from "/static/vendor/three.module.js";

const COL = {
  track: 0x2a3040,
  edge: 0x00e5ff,
  onair: 0xff5722,
  sel: 0xffffff,
  drs: 0x00e676,
  ground: 0x0f1117,
};

// Circuits are normalised into a unit box; this scales that to world units.
const SPAN = 100;
// Elevation arrives on the same scale as X and Y. Real proportions make Spa
// look like a gentle ramp on a 100-unit circuit, so it is exaggerated enough
// to read as terrain while staying honest about which circuit is hillier.
const VERT = 7.0;

let renderer, scene, camera, clock;
let trackMesh, drsMeshes = [], carGroup, glowGroup, lineGroup;
let cars = new Map();
let outline = [], drsZones = [], bounds = [0, 0, 1, 1];
let selected = null, onAir = null;
let raf = 0, mounted = false;
let orbit = 0;

function glowTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d").createRadialGradient(32, 32, 0, 32, 32, 32);
  g.addColorStop(0, "rgba(255,255,255,1)");
  g.addColorStop(0.35, "rgba(255,255,255,0.5)");
  g.addColorStop(1, "rgba(255,255,255,0)");
  const ctx = c.getContext("2d");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
}
let GLOW = null;

function toWorld(p) {
  const cx = (bounds[0] + bounds[2]) / 2, cy = (bounds[1] + bounds[3]) / 2;
  return new THREE.Vector3((p[0] - cx) * SPAN, (p[2] || 0) * SPAN * VERT, (p[1] - cy) * SPAN);
}

function buildTrack() {
  if (trackMesh) { scene.remove(trackMesh); trackMesh.geometry.dispose(); }
  drsMeshes.forEach(m => { scene.remove(m); m.geometry.dispose(); });
  drsMeshes = [];
  if (!outline.length) return;

  const pts = outline.map(toWorld);
  const curve = new THREE.CatmullRomCurve3(pts, true, "centripetal");

  // A flat ribbon rather than a tube: it reads as a road surface from above
  // and does not hide cars behind its own geometry at low camera angles.
  const N = 900, HALF = 2.1;
  const pos = [], idx = [], col = [];
  const up = new THREE.Vector3(0, 1, 0);
  for (let i = 0; i < N; i++) {
    const t = i / N;
    const p = curve.getPointAt(t);
    const tan = curve.getTangentAt(t);
    const side = new THREE.Vector3().crossVectors(tan, up).normalize().multiplyScalar(HALF);
    pos.push(p.x - side.x, p.y, p.z - side.z, p.x + side.x, p.y, p.z + side.z);
    // Tint by gradient so climbs read even in a still frame.
    const grade = Math.max(-1, Math.min(1, tan.y * 6));
    const c = new THREE.Color(COL.track).lerp(new THREE.Color(0x4a5570), Math.abs(grade));
    col.push(c.r, c.g, c.b, c.r, c.g, c.b);
  }
  for (let i = 0; i < N; i++) {
    const a = i * 2, b = ((i + 1) % N) * 2;
    idx.push(a, a + 1, b, b, a + 1, b + 1);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  geo.setAttribute("color", new THREE.Float32BufferAttribute(col, 3));
  geo.setIndex(idx);
  geo.computeVertexNormals();
  trackMesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
    vertexColors: true, side: THREE.DoubleSide,
  }));
  scene.add(trackMesh);

  // DRS zones as a raised emissive strip over the surface.
  drsZones.forEach(z => {
    const a = Math.floor(z[0] * N), b = Math.floor(z[1] * N);
    const zp = [];
    for (let i = a; i <= b; i++) zp.push(curve.getPointAt((i % N) / N).add(new THREE.Vector3(0, 0.35, 0)));
    if (zp.length < 2) return;
    const g = new THREE.BufferGeometry().setFromPoints(zp);
    const m = new THREE.Line(g, new THREE.LineBasicMaterial({
      color: COL.drs, transparent: true, opacity: 0.75,
    }));
    scene.add(m); drsMeshes.push(m);
  });
}

export function init(container) {
  if (mounted) return;
  GLOW = GLOW || glowTexture();
  scene = new THREE.Scene();
  scene.background = new THREE.Color(COL.ground);
  scene.fog = new THREE.Fog(COL.ground, SPAN * 0.7, SPAN * 2.8);

  camera = new THREE.PerspectiveCamera(46, 1, 0.1, 2000);
  camera.position.set(0, SPAN * 0.3, SPAN * 0.92);

  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  container.appendChild(renderer.domElement);
  renderer.domElement.className = "gl";

  carGroup = new THREE.Group(); glowGroup = new THREE.Group(); lineGroup = new THREE.Group();
  scene.add(carGroup, glowGroup, lineGroup);

  clock = new THREE.Clock();
  mounted = true;
  resize(container);
  buildTrack();
  loop();
}

export function resize(container) {
  if (!mounted) return;
  const r = container.getBoundingClientRect();
  if (!r.width || !r.height) return;
  renderer.setSize(r.width, r.height, false);
  camera.aspect = r.width / r.height;
  camera.updateProjectionMatrix();
}

export function setCircuit(o, zones, b) {
  outline = o || []; drsZones = zones || []; bounds = b || [0, 0, 1, 1];
  if (mounted) buildTrack();
}

export function setState(s, sel) {
  selected = sel;
  onAir = s.on_air ? s.on_air.num : null;
  if (!mounted) return;

  const seen = new Set();
  (s.cars || []).forEach(c => {
    seen.add(c.num);
    let e = cars.get(c.num);
    if (!e) {
      const dot = new THREE.Mesh(
        new THREE.SphereGeometry(0.85, 12, 12),
        new THREE.MeshBasicMaterial({ color: new THREE.Color(c.color) })
      );
      const halo = new THREE.Sprite(new THREE.SpriteMaterial({
        map: GLOW, color: new THREE.Color(c.color),
        blending: THREE.AdditiveBlending, transparent: true, depthWrite: false,
      }));
      halo.scale.set(2.6, 2.6, 1);
      carGroup.add(dot); glowGroup.add(halo);
      e = { dot, halo };
      cars.set(c.num, e);
    }
    const w = toWorld([c.x, c.y, c.z || 0]);
    w.y += 0.6;
    e.dot.position.copy(w);
    e.halo.position.copy(w);
    const isAir = c.num === onAir, isSel = c.num === selected;
    const dim = selected && !isSel;
    e.dot.scale.setScalar(isAir || isSel ? 1.5 : 1);
    // Halos are sized in world units, so at chase distance a large one fills
    // the frame. Scale with camera distance to keep the bloom proportionate.
    const d = camera.position.distanceTo(w);
    const k = Math.max(0.35, Math.min(1.6, d / 70));
    e.halo.scale.setScalar((isAir ? 6.5 : isSel ? 5.5 : dim ? 1.6 : 3) * k);
    e.halo.material.color.set(isAir ? COL.onair : isSel ? COL.sel : c.color);
    e.halo.material.opacity = dim ? 0.22 : isAir ? 0.95 : 0.75;
    e.dot.material.opacity = dim ? 0.3 : 1;
    e.dot.material.transparent = true;
  });
  cars.forEach((e, num) => {
    if (!seen.has(num)) {
      carGroup.remove(e.dot); glowGroup.remove(e.halo);
      e.dot.geometry.dispose(); cars.delete(num);
    }
  });

  // Tension beams between contesting cars.
  while (lineGroup.children.length) {
    const c = lineGroup.children.pop();
    c.geometry.dispose(); lineGroup.remove(c);
  }
  const byNum = {};
  (s.cars || []).forEach(c => { byNum[c.num] = c; });
  (s.battles || []).forEach(b => {
    const a = byNum[b.ahead_num], d = byNum[b.behind_num];
    if (!a || !d) return;
    const pa = toWorld([a.x, a.y, a.z || 0]), pb = toWorld([d.x, d.y, d.z || 0]);
    if (pa.distanceTo(pb) > SPAN * 0.5) return;
    pa.y += 0.8; pb.y += 0.8;
    const hot = b.ahead_num === onAir;
    const g = new THREE.BufferGeometry().setFromPoints([pa, pb]);
    lineGroup.add(new THREE.Line(g, new THREE.LineBasicMaterial({
      color: hot ? COL.onair : b.drs ? COL.drs : 0x8f9bba,
      transparent: true,
      opacity: hot ? 0.95 : Math.max(0.14, Math.min(0.7, b.score)),
    })));
  });
}

function loop() {
  raf = requestAnimationFrame(loop);
  if (!mounted) return;
  const dt = clock.getDelta();

  const target = new THREE.Vector3(0, 0, 0);
  if (selected && cars.has(selected)) {
    // Chase: drop toward track level and sit behind the car, so elevation is
    // seen from the side rather than flattened by a top-down view.
    const p = cars.get(selected).dot.position;
    target.copy(p);
    const want = new THREE.Vector3(p.x + 13, p.y + 7.5, p.z + 13);
    camera.position.lerp(want, Math.min(1, dt * 2.2));
  } else {
    orbit += dt * 0.05;
    // Low and close. A high orbit flattens the circuit into the 2D view it is
    // meant to complement — the elevation only reads from near track level.
    const r = SPAN * 0.92;
    const want = new THREE.Vector3(Math.cos(orbit) * r, SPAN * 0.3, Math.sin(orbit) * r);
    camera.position.lerp(want, Math.min(1, dt * 1.5));
  }
  camera.lookAt(target);
  renderer.render(scene, camera);
}

export function destroy() {
  cancelAnimationFrame(raf);
  mounted = false;
  if (renderer) { renderer.dispose(); renderer.domElement.remove(); }
  cars.clear();
}
