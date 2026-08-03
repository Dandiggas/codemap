#!/usr/bin/env node
// codemap QA pass — optional tooling, NOT part of the zero-dependency core app.
// codemap.py / lib/ / template.html ship with no third-party dependencies;
// this script is dev/CI tooling only and requires `playwright` (see README's
// QA section: `npm i playwright` once, or point NODE_PATH at an existing
// install). Nothing in qa/ is imported by the core app.
//
// Usage: node qa/map-qa.mjs <path-to-codemap.html>
//
// Regenerate the file under test first, e.g.:
//   python3 codemap.py render tests/fixtures/toyrepo
//   node qa/map-qa.mjs tests/fixtures/toyrepo/.codemap/codemap.html
//
// What it checks (see report below for the live pass/fail per item):
//  1. No pageerror while loading the map.
//  2. Overview and System tabs render; screenshots saved.
//  3. Code tab, at root and at the two densest drillable levels found by
//     walking into the child with the most children (max depth 3): for
//     every box, hovering it must light exactly as many edge paths as that
//     box has in DATA (the pruning bug this script exists to catch: pruned
//     edges must still exist in the DOM, just hidden, so hover can reveal
//     them); zero duplicate (src,dst) paths per level; if a prune chip
//     exists, toggling it must drop hidden-edge count to zero and restore it.
//  4. Canvas usage: on any tested level with more than 6 boxes, the union
//     bounding box of the boxes must cover at least 40% of the viewport area
//     (the fixed 100x62 letterbox this replaced wasted most of the page), and
//     at most 250% of it (and no wider than 1.5x the viewport) for levels up to
//     15 boxes: sprawling across screens of empty canvas wastes the page just as
//     badly the other way. Bigger levels get the floor only; they do scroll.
//  5. Label collision: on the densest level, no visible edge label may overlap
//     any box by more than 4px on both axes (dagre reserves a rank for every
//     labeled edge; a regression here means labels stopped using it).
//  6. If lenses exist: activating each leaves every box in exactly one of
//     .lit / .dimmed (never neither, never both); every lens-lit arrow lands on
//     a box that is itself .lit at opacity >= 0.9 (no arrows into blackness);
//     no box falls below opacity 0.4 (the dim floor); one lens state screenshot.
//  7. Exclusive focus: with a lens active AND a box hovered, the visible lit
//     edges are exactly that box's edges, never the lens+hover union.

import path from 'node:path';
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

// Node's ESM resolver ignores NODE_PATH (unlike CJS require), so resolve
// playwright through createRequire — this is what lets `NODE_PATH=<an
// existing install> node qa/map-qa.mjs ...` work without a local install.
const { chromium } = createRequire(import.meta.url)('playwright');

const results = []; // {ok, label}
function check(ok, label) {
  results.push({ ok, label });
  console.log((ok ? 'PASS' : 'FAIL') + ' ' + label);
  return ok;
}

function escAttr(s) {
  return String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

// Boxes fade in on a staggered "rise" animation, so anything that reads back an
// opacity or a geometry has to wait for the level to stop moving or it measures
// a mid-fade value and reports a styling bug that isn't there. Two passes: the
// first outlives closePeek's deferred re-render (a 210ms timer that rebuilds
// every box, restarting their animations), the second waits out whatever that
// re-render started.
async function settle(page) {
  const anims = () => page.evaluate(() =>
    Promise.all(document.getAnimations().map((a) => a.finished.catch(() => {}))));
  await page.waitForTimeout(260);
  await anims();
  await page.waitForTimeout(60);
  await anims();
}

async function main() {
  const mapPath = process.argv[2];
  if (!mapPath) {
    console.error('usage: node qa/map-qa.mjs <path-to-codemap.html>');
    process.exit(1);
  }
  const absMapPath = path.resolve(mapPath);
  if (!fs.existsSync(absMapPath)) {
    console.error('FAIL map file not found: ' + absMapPath);
    process.exit(1);
  }
  const mapDir = path.dirname(absMapPath); // <root>/.codemap
  const qaOutDir = path.join(mapDir, 'qa');
  fs.mkdirSync(qaOutDir, { recursive: true });
  const label = path.basename(path.dirname(mapDir)); // repo dir name, for screenshot naming

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });

  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(err.message || String(err)));
  page.on('console', (msg) => {
    if (msg.type() === 'error') pageErrors.push('console.error: ' + msg.text());
  });

  await page.goto(pathToFileURL(absMapPath).href);
  await page.waitForTimeout(150); // initial render (Overview tab, boxes rise-animate in)

  check(pageErrors.length === 0, `no pageerror on load${pageErrors.length ? ' (' + pageErrors.join(' | ') + ')' : ''}`);

  // ---- 2. Overview / System tabs render + screenshot ----
  await page.click('.tab[data-tab="overview"]');
  await page.waitForTimeout(150);
  const overviewBoxes = await page.locator('#canvas .box').count();
  check(overviewBoxes > 0, `overview tab renders boxes (${overviewBoxes})`);
  await page.screenshot({ path: path.join(qaOutDir, `${label}-overview.png`) });

  await page.click('.tab[data-tab="system"]');
  await page.waitForTimeout(150);
  const systemRendered = await page.locator('#canvas').isVisible();
  check(systemRendered, 'system tab renders');
  await page.screenshot({ path: path.join(qaOutDir, `${label}-system.png`) });

  // ---- 3. Code tab: root + two densest drillable levels ----
  await page.click('.tab[data-tab="code"]');
  await page.waitForTimeout(150);

  const tree = await page.evaluate(() => DATA.tree);
  const SYMBOL_KINDS = new Set(['fn', 'class', 'method']);
  function boxChildrenOf(node) {
    return node.kind === 'file' ? (node.symbols || []) : (node.children || []);
  }
  function isDrillableDir(node) {
    return node.kind !== 'file' && !SYMBOL_KINDS.has(node.kind) && (node.children || []).length > 0;
  }

  // Walk the tree up to depth 3 collecting every drillable dir/repo/group
  // node, then pick the two densest by edge count (falling back to child
  // count, then name, for determinism). Edge count — not raw child count —
  // is what actually triggers pruning (needsChip = edges.length > 10 in
  // renderCode), so it's what "densest" means for this defect: a wrapper
  // dir with one child (e.g. "src" containing only "pli") can have zero
  // children-count signal at its own level while its child is where the
  // real edge density (and therefore the pruning/dead-zone bug) lives.
  // A pure greedy "follow the child with the most children" walk misses
  // that case, so this does a bounded full search instead.
  const candidates = [];
  function collect(node, depth, names) {
    if (depth > 3) return;
    for (const c of node.children || []) {
      if (!isDrillableDir(c)) continue;
      const nextNames = names.concat(c.name);
      candidates.push({ node: c, names: nextNames });
      collect(c, depth + 1, nextNames);
    }
  }
  collect(tree, 1, []);
  candidates.sort((a, b) =>
    (b.node.edges || []).length - (a.node.edges || []).length ||
    (b.node.children || []).length - (a.node.children || []).length ||
    a.node.name.localeCompare(b.node.name)
  );
  const denseLevels = candidates.slice(0, 2);

  const levelsToTest = [{ label: 'root', names: [] }].concat(
    denseLevels.map((c, i) => ({
      label: `dense-${i + 1} (${c.names.join('/')}, ${(c.node.edges || []).length} edges)`,
      names: c.names,
      densest: i === 0,
    }))
  );

  if (denseLevels.length === 0) {
    console.log('note: no drillable dense levels found below root (shallow/flat repo) — testing root only');
  }

  for (const lvl of levelsToTest) {
    await testCodeLevel(page, lvl.label, lvl.names, !!lvl.densest);
  }

  // drill to a level by name (state-set, not simulated clicks — navigation
  // itself isn't the thing under test; hover/chip/lens interactions ARE).
  async function gotoLevel(page, names) {
    await page.evaluate(() => { path = [DATA.tree]; closePeek(); renderCode(); });
    for (const name of names) {
      const ok = await page.evaluate((n) => {
        const kids = (path[path.length - 1].children || []);
        const child = kids.find((k) => k.name === n);
        if (!child) return false;
        path.push(child);
        renderCode();
        return true;
      }, name);
      if (!ok) return false;
    }
    await page.waitForTimeout(120);
    return true;
  }

  // Every currently-displayed edge label vs every box, in page coordinates.
  // Overlap counts only when BOTH axes overlap by more than the tolerance, so a
  // label merely grazing a box corner by a pixel isn't reported as a collision.
  async function labelBoxCollisions(page, tol) {
    return page.evaluate((tolerance) => {
      const vis = (el) => {
        const s = getComputedStyle(el);
        return s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity || '1') > 0.05;
      };
      const labels = [...document.querySelectorAll('#canvas svg text.flow-label')].filter(vis);
      const boxes = [...document.querySelectorAll('#canvas .box')].filter(vis);
      const hits = [];
      for (const l of labels) {
        const lr = l.getBoundingClientRect();
        if (lr.width === 0 || lr.height === 0) continue;
        for (const b of boxes) {
          const br = b.getBoundingClientRect();
          const ow = Math.min(lr.right, br.right) - Math.max(lr.left, br.left);
          const oh = Math.min(lr.bottom, br.bottom) - Math.max(lr.top, br.top);
          if (ow > tolerance && oh > tolerance) {
            hits.push(`"${l.textContent}" x "${b.dataset.boxname || '?'}" (${Math.round(ow)}x${Math.round(oh)}px)`);
          }
        }
      }
      return { labels: labels.length, hits };
    }, tol);
  }

  async function testCodeLevel(page, levelLabel, names, densest) {
    if (!(await gotoLevel(page, names))) {
      check(false, `${levelLabel}: could not drill into ${names.join('/')}`);
      return;
    }

    const levelInfo = await page.evaluate(() => {
      const c = path[path.length - 1];
      const kidsRaw = c.kind === 'file' ? (c.symbols || []) : (c.children || []);
      return {
        curName: c.name,
        boxNames: kidsRaw.map((k) => k.name),
        edges: (c.edges || []).map((e) => ({ source: e.source, target: e.target })),
      };
    });

    console.log(`-- code level "${levelLabel}" = ${levelInfo.curName} (${levelInfo.boxNames.length} boxes, ${levelInfo.edges.length} edges) --`);

    // zero duplicate (src,dst) paths in DATA at this level
    const pairKeys = levelInfo.edges.map((e) => `${e.source} ${e.target}`);
    const seen = new Set();
    const dupes = pairKeys.filter((k) => (seen.has(k) ? true : (seen.add(k), false)));
    check(dupes.length === 0, `${levelLabel}: zero duplicate (src,dst) edges in DATA (found ${dupes.length})`);

    // zero duplicate (src,dst) SVG path elements actually drawn
    const domDupCheck = await page.evaluate(() => {
      const svg = document.querySelector('#canvas svg');
      if (!svg) return { total: 0, dupes: 0 };
      const seen = new Set();
      let dupes = 0;
      svg.querySelectorAll('path.flow-line[data-src]').forEach((el) => {
        const key = el.dataset.src + ' ' + el.dataset.dst;
        if (seen.has(key)) dupes++; else seen.add(key);
      });
      return { total: svg.querySelectorAll('path.flow-line[data-src]').length, dupes };
    });
    check(domDupCheck.dupes === 0, `${levelLabel}: zero duplicate (src,dst) DOM path elements (${domDupCheck.total} paths drawn, ${domDupCheck.dupes} dupes)`);
    // one path per DATA edge, exactly (guarantee-by-construction check)
    check(domDupCheck.total === levelInfo.edges.length,
      `${levelLabel}: DOM path count (${domDupCheck.total}) equals DATA edge count (${levelInfo.edges.length})`);

    // canvas usage: the union bounding box of this level's boxes, in layout
    // (not clipped-viewport) coordinates, must cover a real share of the page.
    // Guards the letterbox regression: the old fixed 100x62 unit canvas pinned
    // every level into a strip near the top no matter how much room there was.
    if (levelInfo.boxNames.length > 6) {
      await settle(page);
      const usage = await page.evaluate(() => {
        const boxes = [...document.querySelectorAll('#canvas .box')];
        if (!boxes.length) return null;
        let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
        for (const b of boxes) {
          x0 = Math.min(x0, b.offsetLeft); y0 = Math.min(y0, b.offsetTop);
          x1 = Math.max(x1, b.offsetLeft + b.offsetWidth);
          y1 = Math.max(y1, b.offsetTop + b.offsetHeight);
        }
        return { w: x1 - x0, h: y1 - y0, vw: window.innerWidth, vh: window.innerHeight };
      });
      // Band, not a floor. Too small and the level is letterboxed into a strip
      // (the pre-dagre defect); too large and it sprawls across screens of empty
      // canvas, which wastes the page just as badly in the other direction.
      // Above 15 boxes only the floor applies: those levels legitimately scroll.
      // Area alone is too loose to catch sprawl: the pre-tightening 2743px-wide
      // layout still scored 202%, under the ceiling, purely because it was short.
      // The width bound is what actually bites, and it is the thing the layout
      // targets (1.4x viewport width, with tolerance here for measurement drift).
      const banded = levelInfo.boxNames.length <= 15;
      const hi = banded ? 250 : Infinity;
      const maxW = banded ? usage.vw * 1.5 : Infinity;
      const pct = usage ? (usage.w * usage.h) / (usage.vw * usage.vh) * 100 : 0;
      const dims = usage ? `, ${Math.round(usage.w)}x${Math.round(usage.h)} in ${usage.vw}x${usage.vh}` : '';
      check(usage !== null && pct >= 40 && pct <= hi && usage.w <= maxW,
        `${levelLabel}: boxes cover ${banded ? '40-250% of viewport area and stay within 1.5x its width' : '>=40% of viewport area'} (${pct.toFixed(0)}%${dims})`);
    }

    // hover every box, assert lit-path count == that box's DATA edge count,
    // and (densest level only) that no visible label lands on a box
    let hoverFailures = 0;
    let labelHits = [], labelsSeen = 0;
    for (const name of levelInfo.boxNames) {
      const expected = levelInfo.edges.filter((e) => e.source === name || e.target === name).length;
      const locator = page.locator(`.box[data-boxname="${escAttr(name)}"]`);
      const count = await locator.count();
      if (count === 0) {
        // symbol-kind levels (functions) have no layout/edges at all — skip silently
        if (levelInfo.edges.length === 0 && levelInfo.boxNames.length && expected === 0) continue;
        hoverFailures++;
        console.log(`  FAIL ${levelLabel}: box "${name}" not found in DOM to hover`);
        continue;
      }
      await locator.hover();
      await page.waitForTimeout(30);
      const litCount = await page.evaluate(() => {
        const svg = document.querySelector('#canvas svg');
        if (!svg) return 0;
        return [...svg.querySelectorAll('path.flow-line.edge-lit')]
          .filter((el) => getComputedStyle(el).display !== 'none').length;
      });
      if (litCount !== expected) {
        hoverFailures++;
        console.log(`  FAIL ${levelLabel}: hover "${name}" expected ${expected} lit paths, got ${litCount}`);
      }
      if (densest) {
        const col = await labelBoxCollisions(page, 4);
        labelsSeen += col.labels;
        col.hits.forEach((h) => labelHits.push(`hover "${name}": ${h}`));
      }
      await page.mouse.move(0, 0);
      await page.waitForTimeout(15);
    }
    check(hoverFailures === 0, `${levelLabel}: every box's hover lit-count matches its DATA edge count (${levelInfo.boxNames.length} boxes checked, ${hoverFailures} mismatched)`);
    if (densest) {
      labelHits.slice(0, 6).forEach((h) => console.log('  ' + h));
      check(labelHits.length === 0,
        `${levelLabel}: no visible edge label overlaps a box by >4px (${labelsSeen} label sightings, ${labelHits.length} collisions)`);
    }

    // prune chip toggle, if present at this level
    const chipCount = await page.locator('#edgeChip').count();
    if (chipCount > 0) {
      const hiddenBefore = await page.evaluate(() =>
        document.querySelectorAll('#canvas svg path.flow-line.edge-hidden').length);
      await page.click('#edgeChip');
      await page.waitForTimeout(60);
      const hiddenAfterShowAll = await page.evaluate(() =>
        document.querySelectorAll('#canvas svg path.flow-line.edge-hidden').length);
      check(hiddenAfterShowAll === 0, `${levelLabel}: chip "show all" drops hidden-edge count to zero (was ${hiddenBefore}, now ${hiddenAfterShowAll})`);
      await page.click('#edgeChip');
      await page.waitForTimeout(60);
      const hiddenRestored = await page.evaluate(() =>
        document.querySelectorAll('#canvas svg path.flow-line.edge-hidden').length);
      check(hiddenRestored === hiddenBefore, `${levelLabel}: chip toggle back restores hidden-edge count (${hiddenBefore} -> 0 -> ${hiddenRestored})`);
    } else {
      console.log(`  (no prune chip at "${levelLabel}" — ${levelInfo.edges.length} edges, chip only appears above 10)`);
    }
  }

  // ---- 4. Lenses ----
  const lensNames = await page.evaluate(() => Object.keys((DATA.labels && DATA.labels.lenses) || {}));
  if (lensNames.length) {
    // Lens rendering at the densest level: a lit arrow must land on a box the
    // reader can actually see. The defect this catches is an arrow drawn lit
    // into apparent blackness because its endpoint lost .lit (or was dimmed to
    // the point of invisibility) in the layout path that drew the arrow.
    if (denseLevels.length && (await gotoLevel(page, denseLevels[0].names))) {
      let unlitEnds = 0, tooFaint = 0, litEdges = 0, boxesSeen = 0;
      for (const lensName of lensNames) {
        await page.evaluate((n) => {
          const el = [...document.querySelectorAll('#lensbar .lens')].find((l) => l.dataset.lens === n);
          if (el) el.click();
        }, lensName);
        await page.waitForTimeout(60);
        await settle(page);
        const r = await page.evaluate(() => {
          const boxes = {};
          for (const b of document.querySelectorAll('#canvas .box[data-boxname]')) {
            boxes[b.dataset.boxname] = {
              lit: b.classList.contains('lit'),
              op: parseFloat(getComputedStyle(b).opacity || '1'),
            };
          }
          const bad = [], faint = [];
          for (const name of Object.keys(boxes)) {
            if (boxes[name].op < 0.4) faint.push(`${name} @${boxes[name].op.toFixed(2)}`);
          }
          let lit = 0;
          for (const p of document.querySelectorAll('#canvas svg path.flow-line[data-lens-lit="1"]')) {
            lit++;
            for (const end of [p.dataset.src, p.dataset.dst]) {
              const b = boxes[end];
              if (!b || !b.lit || b.op < 0.9) {
                bad.push(`${p.dataset.src}->${p.dataset.dst} end "${end}" ` +
                  (b ? `lit=${b.lit} opacity=${b.op.toFixed(2)}` : 'box missing'));
              }
            }
          }
          return { bad, faint, lit, boxes: Object.keys(boxes).length };
        });
        litEdges += r.lit; boxesSeen += r.boxes;
        unlitEnds += r.bad.length; tooFaint += r.faint.length;
        r.bad.slice(0, 4).forEach((b) => console.log(`  lens "${lensName}": lit edge into a non-lit box: ${b}`));
        r.faint.slice(0, 4).forEach((f) => console.log(`  lens "${lensName}": box below the dim floor: ${f}`));
        await page.evaluate((n) => {
          const el = [...document.querySelectorAll('#lensbar .lens')].find((l) => l.dataset.lens === n);
          if (el) el.click();
        }, lensName);
        await page.waitForTimeout(50);
      }
      check(unlitEnds === 0,
        `lens-lit arrows land on lit boxes: both endpoints .lit and opacity >=0.9 (${litEdges} lit edges across ${lensNames.length} lenses, ${unlitEnds} bad endpoints)`);
      check(tooFaint === 0,
        `lens dim floor: every box stays at opacity >=0.4 (${boxesSeen} box readings, ${tooFaint} below floor)`);
    }

    // back to root for a clean, full-tree lens check
    await page.evaluate(() => { path = [DATA.tree]; closePeek(); renderCode(); });
    let firstLensShot = false;
    for (const lensName of lensNames) {
      const stateCheck = await page.evaluate((name) => {
        const lensbar = document.getElementById('lensbar');
        const el = [...lensbar.querySelectorAll('.lens')].find((l) => l.dataset.lens === name);
        if (!el) return { ok: false, reason: 'lens chip not found' };
        el.click();
        const boxes = [...document.querySelectorAll('#canvas .box[data-boxname]')];
        const bad = boxes.filter((b) => {
          const lit = b.classList.contains('lit');
          const dimmed = b.classList.contains('dimmed');
          return lit === dimmed; // both true or both false = third state
        });
        return { ok: bad.length === 0, total: boxes.length, bad: bad.length };
      }, lensName);
      check(stateCheck.ok, `lens "${lensName}": every box is .lit xor .dimmed (${stateCheck.total ?? 0} boxes, ${stateCheck.bad ?? '?'} bad${stateCheck.reason ? ' — ' + stateCheck.reason : ''})`);
      await page.waitForTimeout(120);
      if (!firstLensShot) {
        await page.screenshot({ path: path.join(qaOutDir, `${label}-lens-${lensName.replace(/[^a-z0-9-]+/gi, '_')}.png`) });
        firstLensShot = true;
      }
      // deactivate (lens click toggles) so the next lens starts clean
      await page.evaluate((name) => {
        const lensbar = document.getElementById('lensbar');
        const el = [...lensbar.querySelectorAll('.lens')].find((l) => l.dataset.lens === name);
        if (el) el.click();
      }, lensName);
      await page.waitForTimeout(60);
    }
  } else {
    console.log('note: no lenses defined in this map — skipping lens checks');
  }

  // ---- Exclusive focus: lens active AND a box hovered ----
  // The bug this catches: a lens lights its own flow persistently, so hovering
  // a box used to show the union (lens edges + hover edges) and the level read
  // as noise. Hover must win outright for its duration, and letting go must put
  // the lens resting state back exactly as it was.
  if (lensNames.length && denseLevels.length) {
    const dl = denseLevels[0];
    if (!(await gotoLevel(page, dl.names))) {
      check(false, `exclusive focus: could not drill into ${dl.names.join('/')}`);
    } else {
      const info = await page.evaluate(() => {
        const c = path[path.length - 1];
        const kidsRaw = c.kind === 'file' ? (c.symbols || []) : (c.children || []);
        return {
          boxNames: kidsRaw.map((k) => k.name),
          edges: (c.edges || []).map((e) => ({ source: e.source, target: e.target })),
        };
      });
      const clickLens = (name) => page.evaluate((n) => {
        const el = [...document.querySelectorAll('#lensbar .lens')].find((l) => l.dataset.lens === n);
        if (el) el.click();
      }, name);
      const visibleState = () => page.evaluate(() => {
        const svg = document.querySelector('#canvas svg');
        if (!svg) return { lit: 0, labels: [] };
        const shown = (el) => getComputedStyle(el).display !== 'none';
        return {
          lit: [...svg.querySelectorAll('path.flow-line.edge-lit')].filter(shown).length,
          labels: [...svg.querySelectorAll('text.flow-label')].filter(shown)
            .map((el) => el.dataset.src + '→' + el.dataset.dst),
        };
      });
      let exclusiveBad = 0, restingBad = 0, hovers = 0;
      for (const lensName of lensNames) {
        await clickLens(lensName);
        await page.waitForTimeout(90);
        const resting = await visibleState();
        for (const name of info.boxNames) {
          const expected = info.edges.filter((e) => e.source === name || e.target === name).length;
          const loc = page.locator(`.box[data-boxname="${escAttr(name)}"]`);
          if ((await loc.count()) === 0) continue;
          await loc.hover();
          await page.waitForTimeout(30);
          hovers++;
          const st = await visibleState();
          const stray = st.labels.filter((l) => !l.startsWith(name + '→') && !l.endsWith('→' + name));
          if (st.lit !== expected || stray.length) {
            exclusiveBad++;
            console.log(`  FAIL lens "${lensName}" + hover "${name}": expected ${expected} lit edges, got ${st.lit}` +
              (stray.length ? `; ${stray.length} stray lens label(s): ${stray.join(', ')}` : ''));
          }
          await page.mouse.move(0, 0);
          await page.waitForTimeout(25);
        }
        const after = await visibleState();
        if (after.lit !== resting.lit || after.labels.length !== resting.labels.length) {
          restingBad++;
          console.log(`  FAIL lens "${lensName}": resting state not restored (${resting.lit} lit/${resting.labels.length} labels -> ${after.lit}/${after.labels.length})`);
        }
        await clickLens(lensName);
        await page.waitForTimeout(60);
      }
      check(exclusiveBad === 0,
        `exclusive focus: lens + hover lights only the hovered box's edges (${hovers} lens/box hovers, ${exclusiveBad} showed the lens+hover union)`);
      check(restingBad === 0,
        `lens resting state returns exactly on mouseleave (${lensNames.length} lenses, ${restingBad} drifted)`);
    }
  }

  await browser.close();

  const failed = results.filter((r) => !r.ok);
  console.log('');
  console.log(`SUMMARY: ${results.length - failed.length}/${results.length} checks passed`);
  if (failed.length) {
    console.log('FAILED CHECKS:');
    failed.forEach((f) => console.log('  - ' + f.label));
    process.exit(1);
  }
  process.exit(0);
}

main().catch((err) => {
  console.error('FAIL map-qa crashed: ' + (err && err.stack || err));
  process.exit(1);
});
