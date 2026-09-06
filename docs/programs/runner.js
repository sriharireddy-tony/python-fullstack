/* ==========================================================================
   Two small things every page needs:
     1. a light/dark toggle that remembers your choice
     2. a Run button on every code block, so you can see the output
        without leaving the page

   Nothing here is part of the lessons. It is just the page furniture.
   ========================================================================== */

(function () {
  'use strict';

  /* ------------------------------------------------------------ theme --- */

  var root = document.documentElement;

  function readStoredTheme() {
    try { return localStorage.getItem('programs-theme'); } catch (e) { return null; }
  }

  function storeTheme(value) {
    try { localStorage.setItem('programs-theme', value); } catch (e) { /* private mode */ }
  }

  var stored = readStoredTheme();
  if (stored === 'dark' || stored === 'light') root.setAttribute('data-theme', stored);

  function currentTheme() {
    var explicit = root.getAttribute('data-theme');
    if (explicit) return explicit;
    return window.matchMedia &&
           window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function setupThemeButton() {
    var btn = document.querySelector('.themebtn');
    if (!btn) return;

    function label() { btn.textContent = currentTheme() === 'dark' ? 'Light' : 'Dark'; }

    btn.addEventListener('click', function () {
      var next = currentTheme() === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      storeTheme(next);
      label();
    });

    label();
  }

  /* ------------------------------------------------------------- run --- */

  // Turn whatever console.log was given into readable text.
  function render(value, depth) {
    depth = depth || 0;

    if (typeof value === 'string') return depth === 0 ? value : '"' + value + '"';
    if (value === null) return 'null';
    if (value === undefined) return 'undefined';
    if (typeof value === 'number' || typeof value === 'boolean') return String(value);
    if (typeof value === 'function') return 'function ' + (value.name || '(anonymous)');

    if (Array.isArray(value)) {
      if (depth > 3) return '[...]';
      return '[' + value.map(function (v) { return render(v, depth + 1); }).join(', ') + ']';
    }

    if (value instanceof Map) {
      var pairs = [];
      value.forEach(function (v, k) { pairs.push(render(k, depth + 1) + ' => ' + render(v, depth + 1)); });
      return 'Map(' + value.size + ') {' + pairs.join(', ') + '}';
    }

    if (value instanceof Set) {
      var items = [];
      value.forEach(function (v) { items.push(render(v, depth + 1)); });
      return 'Set(' + value.size + ') {' + items.join(', ') + '}';
    }

    if (value instanceof Error) return value.name + ': ' + value.message;

    if (depth > 3) return '{...}';
    try {
      var keys = Object.keys(value);
      if (keys.length === 0) return '{}';
      return '{ ' + keys.map(function (k) {
        return k + ': ' + render(value[k], depth + 1);
      }).join(', ') + ' }';
    } catch (e) {
      return String(value);
    }
  }

  function joinArgs(args) {
    return Array.prototype.slice.call(args).map(function (a) { return render(a, 0); }).join(' ');
  }

  // Blocks are wrapped in an async function, so a snippet may use `await`
  // at what looks like top level, and anything it schedules with setTimeout
  // or a promise still gets its output captured. Purely synchronous blocks
  // behave exactly as before.
  var AsyncFunction = Function;
  try {
    AsyncFunction = Object.getPrototypeOf(
      new Function('return async function () {}')()
    ).constructor;
  } catch (e) { /* very old engine: fall back to sync Function */ }

  // How long to keep listening after the block's promise settles, so output
  // from a trailing setTimeout(..., 0) still lands.
  var SETTLE_MS = 400;

  function run(source, out, onDone) {
    var lines = [];
    var errText = null;
    var pending = false;

    function paint() {
      pending = false;
      out.textContent = lines.length ? lines.join('\n')
                                     : (errText ? '' : '(ran with no output)');
      if (errText) {
        if (lines.length) out.appendChild(document.createTextNode('\n'));
        var span = document.createElement('span');
        span.className = 'err';
        span.textContent = errText;
        out.appendChild(span);
      }
    }

    // Repaint at most once a frame -- a loop that logs thousands of lines
    // should not force thousands of layouts.
    function schedulePaint() {
      if (pending) return;
      pending = true;
      (window.requestAnimationFrame || function (f) { setTimeout(f, 16); })(paint);
    }

    function record() { lines.push(joinArgs(arguments)); schedulePaint(); }

    var fakeConsole = {
      log: record, info: record, warn: record, error: record, table: record
    };

    out.textContent = '';
    out.classList.add('shown');

    function finish() {
      paint();
      if (onDone) onDone();
    }

    function fail(err) {
      errText = (err && err.name ? err.name : 'Error') + ': ' +
                (err && err.message ? err.message : String(err));
      paint();
    }

    var result;
    try {
      // AsyncFunction() gives the snippet its own scope, so two blocks on the
      // same page can both declare `const nums` without colliding.
      var fn = new AsyncFunction('console', '"use strict";\n' + source);
      result = fn(fakeConsole);
    } catch (err) {
      // a syntax error in the snippet, or a throw from a synchronous block
      fail(err);
      if (onDone) onDone();
      return;
    }

    if (!result || typeof result.then !== 'function') {
      finish();
      return;
    }

    result.then(null, fail).then(function () {
      // give trailing timers and unawaited promises a moment to report
      setTimeout(finish, SETTLE_MS);
    });
  }

  function setupRunButtons() {
    var blocks = document.querySelectorAll('.codeblock[data-run]');

    Array.prototype.forEach.call(blocks, function (block) {
      var pre  = block.querySelector('pre');
      var head = block.querySelector('.codehead');
      if (!pre || !head) return;

      var out = document.createElement('div');
      out.className = 'output';
      block.appendChild(out);

      var btn = document.createElement('button');
      btn.className = 'runbtn';
      btn.type = 'button';
      btn.textContent = 'Run';
      head.appendChild(btn);

      btn.addEventListener('click', function () {
        if (btn.disabled) return;
        btn.disabled = true;
        btn.textContent = 'Running…';
        // Let the browser paint the label before a slow snippet blocks the thread.
        setTimeout(function () {
          run(pre.textContent, out, function () {
            btn.disabled = false;
            btn.textContent = 'Run again';
          });
        }, 15);
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      setupThemeButton();
      setupRunButtons();
    });
  } else {
    setupThemeButton();
    setupRunButtons();
  }
})();
