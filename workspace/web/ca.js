(function (root) {
  'use strict';
  const RULES = {
    life: {name: 'CONWAY’S LIFE', label: 'B3 / S23', birth: [3], survive: [2, 3]},
    highlife: {name: 'HIGHLIFE', label: 'B36 / S23', birth: [3, 6], survive: [2, 3]},
    seeds: {name: 'SEEDS', label: 'B2 / S∅', birth: [2], survive: []},
    volume: {name: '3D CRYSTAL LIFE', label: 'B5 / S456 · 26 neighbors', birth: [5], survive: [4, 5, 6]}
  };
  const PATTERNS = {
    glider: [[0,1,0],[1,2,0],[2,0,0],[2,1,0],[2,2,0]],
    blinker: [[0,0,0],[1,0,0],[2,0,0]],
    pulsar: [[2,0,0],[3,0,0],[4,0,0],[8,0,0],[9,0,0],[10,0,0],[0,2,0],[5,2,0],[7,2,0],[12,2,0],[0,3,0],[5,3,0],[7,3,0],[12,3,0],[0,4,0],[5,4,0],[7,4,0],[12,4,0],[2,5,0],[3,5,0],[4,5,0],[8,5,0],[9,5,0],[10,5,0],[2,7,0],[3,7,0],[4,7,0],[8,7,0],[9,7,0],[10,7,0],[0,8,0],[5,8,0],[7,8,0],[12,8,0],[0,9,0],[5,9,0],[7,9,0],[12,9,0],[0,10,0],[5,10,0],[7,10,0],[12,10,0],[2,12,0],[3,12,0],[4,12,0],[8,12,0],[9,12,0],[10,12,0]],
    crystal: [[0,0,0],[1,0,0],[0,1,0],[0,0,1],[1,1,1],[1,0,1]]
  };
  class Automaton {
    constructor(width = 64, height = 40, rule = 'life', depth = 1) {
      if (![width, height, depth].every(n => Number.isInteger(n) && n >= 1 && n <= 128) || width * height * depth > 131072) throw new Error('Invalid field dimensions.');
      if (!Object.hasOwn(RULES, rule) || (depth > 1) !== (rule === 'volume')) throw new Error('Rule does not match the field dimension.');
      this.width = width; this.height = height; this.depth = depth;
      this.cells = new Uint8Array(width * height * depth);
      this.ages = new Uint16Array(this.cells.length);
      this.generation = 0; this.rule = rule;
    }
    index(x, y, z = 0) { return (((z % this.depth + this.depth) % this.depth) * this.height + (y % this.height + this.height) % this.height) * this.width + (x % this.width + this.width) % this.width; }
    set(x, y, z = 0, alive = true) { const i = this.index(x, y, z); this.cells[i] = alive ? 1 : 0; this.ages[i] = this.cells[i]; }
    stamp(pattern, x, y, z = 0) { if (!Object.hasOwn(PATTERNS, pattern)) throw new Error('Unknown pattern.'); for (const [dx, dy, dz] of PATTERNS[pattern]) this.set(x + dx, y + dy, z + dz); }
    clear() { this.cells.fill(0); this.ages.fill(0); this.generation = 0; }
    seed(random = Math.random) {
      const density = this.rule === 'seeds' ? .085 : this.depth > 1 ? .17 : .24;
      for (let i = 0; i < this.cells.length; i++) { this.cells[i] = random() < density ? 1 : 0; this.ages[i] = this.cells[i]; }
      this.generation = 0;
    }
    step() {
      const {width: w, height: h, depth: d, cells, ages} = this;
      const next = new Uint8Array(cells.length), nextAges = new Uint16Array(cells.length), rule = RULES[this.rule];
      for (let z = 0; z < d; z++) for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
        let neighbors = 0;
        for (let dz = d > 1 ? -1 : 0; dz <= (d > 1 ? 1 : 0); dz++) for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
          if (dx || dy || dz) neighbors += cells[((((z + dz + d) % d) * h) + (y + dy + h) % h) * w + (x + dx + w) % w];
        }
        const i = (z * h + y) * w + x;
        next[i] = (cells[i] ? rule.survive : rule.birth).includes(neighbors) ? 1 : 0;
        nextAges[i] = next[i] ? Math.min(65535, ages[i] + 1) : 0;
      }
      this.cells = next; this.ages = nextAges; this.generation++;
      return this.population();
    }
    population() { let n = 0; for (const cell of this.cells) n += cell; return n; }
    hash() { let h = 2166136261; for (const cell of this.cells) h = Math.imul(h ^ cell, 16777619); return h >>> 0; }
    snapshot() { return {version: 1, width: this.width, height: this.height, depth: this.depth, rule: this.rule, generation: this.generation, cells: Array.from(this.cells), ages: Array.from(this.ages)}; }
    static restore(seed) {
      if (!seed || seed.version !== 1 || !Number.isSafeInteger(seed.generation) || seed.generation < 0) throw new Error('Invalid saved seed.');
      const a = new Automaton(seed.width, seed.height, seed.rule, seed.depth);
      if (!Array.isArray(seed.cells) || seed.cells.length !== a.cells.length || !seed.cells.every(c => c === 0 || c === 1)) throw new Error('Invalid saved cells.');
      if (!Array.isArray(seed.ages) || seed.ages.length !== a.cells.length || !seed.ages.every(c => Number.isInteger(c) && c >= 0 && c <= 65535)) throw new Error('Invalid cell ages.');
      a.cells.set(seed.cells); a.ages.set(seed.ages); a.generation = seed.generation; return a;
    }
  }
  class VolumeAutomaton extends Automaton { constructor(width = 16, height = 16, depth = 12) { super(width, height, 'volume', depth); } }
  function performanceBudget({hidden = false, reducedMotion = false, fullScreen = false, temperature = null, load = 0} = {}) {
    if (hidden) return {fps: 0, steps: 0, reason: 'Tab sleeping'};
    if (reducedMotion) return {fps: 8, steps: 4, reason: 'Reduced motion'};
    if ((Number.isFinite(temperature) && temperature >= 80) || load >= 1.2) return {fps: 10, steps: 4, reason: 'System cooling'};
    if (fullScreen) return {fps: 8, steps: 3, reason: 'Focus mode'};
    return {fps: 30, steps: 16, reason: 'Balanced · 30 FPS'};
  }
  root.WhitecubeCA = {Automaton, VolumeAutomaton, RULES, PATTERNS, performanceBudget};
  if (typeof module !== 'undefined') module.exports = root.WhitecubeCA;
})(typeof window !== 'undefined' ? window : globalThis);
