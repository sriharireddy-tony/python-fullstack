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

  function run(source, out) {
    var lines = [];
    var fakeConsole = {
      log:   function () { lines.push(joinArgs(arguments)); },
      info:  function () { lines.push(joinArgs(arguments)); },
      warn:  function () { lines.push(joinArgs(arguments)); },
      error: function () { lines.push(joinArgs(arguments)); },
      table: function () { lines.push(joinArgs(arguments)); }
    };

    out.textContent = '';
    out.classList.add('shown');

    try {
      // Function() gives the snippet its own scope, so two blocks on the same
      // page can both declare `const nums` without colliding.
      var fn = new Function('console', '"use strict";\n' + source);
      fn(fakeConsole);
      out.textContent = lines.length ? lines.join('\n') : '(ran with no output)';
    } catch (err) {
      out.textContent = lines.join('\n') + (lines.length ? '\n' : '');
      var span = document.createElement('span');
      span.className = 'err';
      span.textContent = (err && err.name ? err.name : 'Error') + ': ' +
                         (err && err.message ? err.message : String(err));
      out.appendChild(span);
    }
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
        btn.textContent = 'Running…';
        // Let the browser paint the label before a slow snippet blocks the thread.
        setTimeout(function () {
          run(pre.textContent, out);
          btn.textContent = 'Run again';
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
