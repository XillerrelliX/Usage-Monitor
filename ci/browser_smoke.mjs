// CI-only actual rendering gates. No account, settings, or real rollout sources.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {chromium, webkit} from 'playwright';

const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
fs.mkdirSync(fixture.outputDirectory, {recursive: true});
const fields = ['inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningOutputTokens', 'totalTokens'];
const result = {status: 'running', engines: []};
const evidence = path.join(fixture.outputDirectory, 'browser-results.json');
const save = () => fs.writeFileSync(evidence, JSON.stringify(result, null, 2) + '\n');
save();
try {
  for (const [name, type] of Object.entries({chromium, webkit})) {
    const browser = await type.launch();
    const context = await browser.newContext({viewport: {width: 1440, height: 1050}, acceptDownloads: true});
    const page = await context.newPage();
    const errors = [], blocked = [], entry = {engine: name, version: browser.version(), status: 'running', checks: []};
    result.engines.push(entry); save();
    page.on('pageerror', error => errors.push(error.message));
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (!['file:', 'data:', 'blob:'].includes(url.protocol) && !(url.protocol === 'http:' && url.hostname === '127.0.0.1')) {
        blocked.push(url.origin); await route.abort(); return;
      }
      await route.continue();
    });
    const selection = {startDate: '2026-10-24', endDate: '2026-10-26', projects: [], chats: []};
    const vector = () => page.evaluate(keys => keys.map(key => window.codexUsageDashboard.data.totals[key]), fields);
    const apply = async selected => {
      assert.equal(await page.evaluate(item => window.codexUsageDashboard.applyFilters(item), selected), true);
    };
    try {
      // Exercise both transport modes, including WebKit's file:// restrictions.
      for (const url of [pathToFileURL(fixture.file).href, fixture.dashboardUrl]) {
        await page.goto(url, {waitUntil: 'load'});
        await page.waitForFunction(() => window.codexUsageDashboard?.applyFilters);
        await apply(selection);
        assert.deepEqual(await vector(), [220, 100, 62, 17, 282]);
        assert.equal(await page.locator('#total-tokens').innerText(), '282');
        entry.checks.push(url.startsWith('file:') ? 'file-dashboard-exact-counters' : 'loopback-dashboard-exact-counters');
      }
      // Real form controls retain drafts until Apply; keyboard closes picker.
      await page.locator('#filter-chat').click();
      await page.locator('#chat-options input').first().waitFor();
      await page.locator(`#chat-options input[value="${fixture.root}"]`).check();
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('#filter-chat').getAttribute('aria-expanded'), 'false');
      assert.deepEqual(await vector(), [220, 100, 62, 17, 282]);
      await page.locator('#filter-apply').click();
      assert.deepEqual(await vector(), [161, 76, 51, 14, 212]);
      entry.checks.push('keyboard-picker-and-draft-apply');
      await apply({...selection, chats: [fixture.root, fixture.fork], projects: fixture.projects});
      assert.deepEqual(await vector(), [161, 76, 51, 14, 212]);
      await page.locator('#trend-compare').click();
      const colors = await page.locator('#comparison-legend [data-series-id]').evaluateAll(nodes =>
        Object.fromEntries(nodes.map(node => [node.dataset.seriesId, getComputedStyle(node.querySelector('i')).backgroundColor])));
      assert.equal(Object.keys(colors).length, 2);
      assert.equal(new Set(Object.values(colors)).size, 2);
      await page.locator('#plot .day-bar').first().focus();
      await page.keyboard.press('ArrowRight');
      assert.equal(await page.locator('#comparison-values tbody tr').count(), 2);
      assert.match(await page.locator('#comparison-values').innerText(), /Total.*Input.*Cached input.*Output.*Reasoning/s);
      await apply({...selection, chats: [fixture.fork, fixture.root], projects: [...fixture.projects].reverse()});
      await page.locator('#trend-compare').click();
      const reordered = await page.locator('#comparison-legend [data-series-id]').evaluateAll(nodes =>
        Object.fromEntries(nodes.map(node => [node.dataset.seriesId, getComputedStyle(node.querySelector('i')).backgroundColor])));
      assert.deepEqual(reordered, colors);
      entry.checks.push('plural-intersection-distinct-stable-colors-and-keyboard-exact-values');
      const [download] = await Promise.all([page.waitForEvent('download'), page.locator('#export').click()]);
      const downloadPath = path.join(fixture.outputDirectory, name + '-selected.csv');
      await download.saveAs(downloadPath);
      const csv = fs.readFileSync(downloadPath, 'utf8');
      assert.match(csv, /input_tokens,cached_input_tokens,output_tokens,reasoning_tokens/);
      assert.equal(csv.trim().split(/\r?\n/).length, 2); // Empty selected fork has no record within these projects.
      assert.ok(csv.includes(fixture.root));
      assert.ok(!csv.includes(fixture.fork));
      entry.checks.push('actual-csv-download-selected-deduplicated-chat');
      await apply({...selection, startDate: '2026-10-25', endDate: '2026-10-25', projects: [], chats: []});
      assert.deepEqual(await vector(), [37, 12, 13, 3, 50]);
      assert.equal(await page.locator('#daily-trend').isVisible(), false);
      assert.equal(await page.locator('#export').isEnabled(), true);
      assert.equal(await page.locator('#chats-title').isVisible(), true);
      await page.setViewportSize({width: 390, height: 844});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true);
      await page.screenshot({path: path.join(fixture.outputDirectory, name + '-mobile.png'), fullPage: true});
      await page.setViewportSize({width: 1440, height: 1050});
      await apply({...selection, chats: [fixture.root, fixture.fork]});
      await page.screenshot({path: path.join(fixture.outputDirectory, name + '-desktop.png'), fullPage: true});
      await page.reload({waitUntil: 'load'});
      await page.waitForFunction(() => window.codexUsageDashboard?.getFilters);
      assert.deepEqual(await page.evaluate(() => window.codexUsageDashboard.getFilters().chats), [fixture.root, fixture.fork].sort());
      entry.checks.push('single-day-graph-threshold-mobile-layout-reload-selection');
      for (const state of fixture.queryStates) {
        // Exercise the ordinary sidecar loader against actual packaged output.
        // Only pending is explicitly a UI contract fixture (the producer emits
        // provisional immediately); origins are retained in browser evidence.
        fs.copyFileSync(state.file, fixture.queryLatest + '.tmp');
        fs.renameSync(fixture.queryLatest + '.tmp', fixture.queryLatest);
        await page.waitForFunction(expected => document.querySelector('#query-meter')?.dataset.status === expected,
                                   state.status, {timeout: 15000});
        assert.equal(await page.locator('#query-meter-value').innerText(), state.value);
        if (state.note) assert.ok((await page.evaluate(() =>
          window.UsageQueryMeter.describe(window.USAGE_QUERY_METER).note)).includes(state.note));
        const before = await vector();
        await apply({...selection, chats: [fixture.root], projects: []});
        assert.equal(await page.locator('#query-meter-value').innerText(), state.value);
        assert.deepEqual(await vector(), [161, 76, 51, 14, 212]);
        await apply({...selection, chats: [fixture.root, fixture.fork]});
        assert.deepEqual(await vector(), before);
        entry.checks.push('query-sidecar-' + state.status + ':' + state.origin);
      }
      const standalone = await context.newPage();
      await standalone.goto(pathToFileURL(fixture.queryView).href, {waitUntil: 'load'});
      await standalone.waitForFunction(() => document.querySelector('#total')?.textContent.includes('10,000,000,000,000,001'));
      assert.match(await standalone.locator('#status').innerText(), /Partial/);
      assert.match(await standalone.locator('#scope').innerText(), /request ownership unverified/);
      await standalone.screenshot({path: path.join(fixture.outputDirectory, name + '-partial-large-query.png'), fullPage: true});
      await standalone.close();
      entry.checks.push('standalone-query-partial-exact-large-integer');
      assert.deepEqual(blocked, [], 'Dashboard attempted an external request');
      assert.deepEqual(errors, [], 'Browser emitted script errors');
      entry.status = 'passed';
    } catch (error) {
      entry.status = 'failed'; entry.error = error.stack;
      await page.screenshot({path: path.join(fixture.outputDirectory, name + '-failure.png'), fullPage: true}).catch(() => {});
      throw error;
    } finally {
      entry.pageErrors = errors; entry.externalRequests = blocked; save();
      await browser.close();
    }
  }
  result.status = 'passed';
} catch (error) {
  result.status = 'failed'; result.error = error.stack; throw error;
} finally { save(); }
console.log('Chromium and WebKit public dashboard rendering gates passed.');
