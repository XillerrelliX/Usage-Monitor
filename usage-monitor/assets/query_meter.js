/* Query measurements have their own scope and never enter report totals. */
(function (root) {
  'use strict';
  const states = new Set(['pending', 'provisional', 'complete', 'partial', 'unavailable']);
  const format = new Intl.NumberFormat('en-US');
  function describe(value) {
    if (!value || value.schemaVersion !== 1 || !states.has(value.status)) return null;
    const counts = value.status === 'complete' ? value.counters : value.subtotal;
    const textCounts = value.status === 'complete' ? value.counterText : value.subtotalText;
    const total = counts && counts.totalTokens;
    const totalText = textCounts && textCounts.totalTokens;
    const exactText = typeof totalText === 'string' && /^(0|[1-9][0-9]*)$/.test(totalText);
    const validTotal = exactText || (Number.isSafeInteger(total) && total >= 0);
    const formatted = validTotal ? format.format(exactText ? BigInt(totalText) : total) : '';
    const final = value.status === 'complete' && value.final === true && validTotal;
    const turns = Number.isSafeInteger(value.registeredTurns) ? value.registeredTurns : 0;
    const scope = turns + (turns === 1 ? ' captured turn' : ' captured turns');
    const checkpoint = value.coverage === 'checkpoint-only' || value.coverage === 'since-checkpoint';
    if (final) return {state: 'complete', status: 'Final', value: formatted + ' tokens',
      note: (checkpoint ? 'Since the start checkpoint · ' : '') + scope + ' · includes completed responses.'};
    if (value.status === 'partial' || value.status === 'unavailable' || value.status === 'complete') {
      return {state: 'unavailable', status: 'Incomplete measurement', value: 'Not separately measurable',
        note: validTotal ? 'Captured-turn subtotal: ' + formatted + ' tokens. The full request is not covered.' : 'A verified total is unavailable for this request.'};
    }
    return {state: value.status, status: 'In progress', value: validTotal ? formatted + ' tokens so far' : 'Measuring…',
      note: scope + ' · provisional; the completed response is not yet fully included.'};
  }
  function render(value, document) {
    const section = document.getElementById('query-meter');
    if (!section) return;
    const view = describe(value);
    if (!view) { section.hidden = true; return; }
    section.hidden = false;
    section.dataset.status = view.state;
    for (const key of ['status', 'value', 'note']) {
      const element = document.getElementById('query-meter-' + key);
      if (element) element.textContent = view[key];
    }
  }
  root.UsageQueryMeter = {describe, render};
  if (typeof module === 'object' && module.exports) module.exports = root.UsageQueryMeter;
  if (!root.document) return;
  let scheduled;
  function load() {
    clearTimeout(scheduled);
    if (!root.document.getElementById('query-meter')) { scheduled = setTimeout(load, 2000); return; }
    if (root.document.hidden) { scheduled = setTimeout(load, 5000); return; }
    const script = root.document.createElement('script');
    script.src = 'query-usage/latest.js?updated=' + Date.now();
    script.onload = function () { render(root.USAGE_QUERY_METER, root.document); script.remove(); scheduled = setTimeout(load, 2000); };
    script.onerror = function () { script.remove(); scheduled = setTimeout(load, 10000); };
    root.document.head.appendChild(script);
  }
  // The report renderer replaces its inner DOM when filters change.
  new MutationObserver(function () { render(root.USAGE_QUERY_METER, root.document); }).observe(root.document.getElementById('report'), {childList: true});
  load();
})(typeof window === 'object' ? window : globalThis);
