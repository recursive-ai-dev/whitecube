const test = require('node:test');
const assert = require('node:assert');
const { render } = require('../renderer/markdown');

test('plain text becomes escaped paragraphs with line breaks', () => {
  assert.strictEqual(render('a < b & c\nsecond line'), '<p>a &lt; b &amp; c<br>second line</p>');
  assert.strictEqual(render('one\n\ntwo'), '<p>one</p><p>two</p>');
});

test('hostile HTML is inert everywhere', () => {
  const evil = [
    '<script>alert(1)</script>',
    '<img src=x onerror=alert(1)>',
    '`<b onmouseover=x>`',
    '```html\n<script>alert(1)</script>\n```',
    '**<i onclick=x>**',
    '# <svg onload=x>',
    '- <a href="javascript:x">',
    '```"><img src=x onerror=y>\ncode\n```'
  ];
  for (const e of evil) {
    const html = render(e);
    // Strip the tags this renderer is allowed to produce; no raw "<" from the input may remain.
    const stripped = html.replace(/<\/?(p|br|strong|em|code|pre|ul|ol|li|h[345]|div|span|button)( class="[a-z-]+"| type="button")*>/g, '');
    assert.ok(!/[<>]/.test(stripped), `${e} -> ${html}`);
    assert.ok(!/<(script|img|svg|a)\b/i.test(html), `${e} -> ${html}`);
  }
});

test('links and images are not rendered, only escaped text', () => {
  const html = render('[x](javascript:alert(1)) ![y](http://e/x.png)');
  assert.ok(!html.includes('<a'));
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('[x](javascript:alert(1))'));
});

test('fenced code keeps its content verbatim and gets a Copy button', () => {
  const html = render('Here:\n```js\nconst a = "<b>" && 1;\n  indented\n```\nDone');
  assert.ok(html.includes('<span>js</span>'));
  assert.ok(html.includes('class="copy-btn"'));
  assert.ok(html.includes('<pre><code>const a = &quot;&lt;b&gt;&quot; &amp;&amp; 1;\n  indented</code></pre>'));
  assert.ok(html.startsWith('<p>Here:</p>'));
  assert.ok(html.endsWith('<p>Done</p>'));
});

test('an unclosed fence (streaming or cut-off reply) renders as code to the end', () => {
  const html = render('```py\nprint(1)');
  assert.ok(html.includes('<pre><code>print(1)</code></pre>'));
});

test('inline code protects its contents from emphasis', () => {
  assert.strictEqual(render('use `a**b**c` now'), '<p>use <code>a**b**c</code> now</p>');
  assert.strictEqual(render('**bold** and *it*'), '<p><strong>bold</strong> and <em>it</em></p>');
  assert.strictEqual(render('2 * 3 * 4'), '<p>2 * 3 * 4</p>');
});

test('headings and lists', () => {
  assert.strictEqual(render('# T\n## U'), '<h3>T</h3><h4>U</h4>');
  assert.strictEqual(render('- a\n- b\n\ntext'), '<ul><li>a</li><li>b</li></ul><p>text</p>');
  assert.strictEqual(render('1. a\n2) b'), '<ol><li>a</li><li>b</li></ol>');
  assert.strictEqual(render('- a\n1. b'), '<ul><li>a</li></ul><ol><li>b</li></ol>');
});

test('pathological input finishes quickly (no catastrophic backtracking)', () => {
  const inputs = ['*'.repeat(50000), '`'.repeat(50000), '**a'.repeat(20000), '* '.repeat(30000), ('`a'.repeat(30000)), '-'.repeat(100000)];
  for (const input of inputs) {
    const t = process.hrtime.bigint();
    render(input);
    const ms = Number(process.hrtime.bigint() - t) / 1e6;
    assert.ok(ms < 1500, `${input.slice(0, 6)}… took ${ms.toFixed(0)} ms`);
  }
});

test('empty and non-string input do not throw', () => {
  assert.strictEqual(render(''), '');
  assert.strictEqual(render(undefined), '<p>undefined</p>');
});

test('Markdown tables escape content and format inline code', () => {
  const html = render('| Name | Value |\n| --- | :---: |\n| `<b>` | **safe** |\n| <img onerror=x> | a \\| b |');
  assert.ok(html.includes('<th>Name</th><th>Value</th>'));
  assert.ok(html.includes('<td><code>&lt;b&gt;</code></td><td><strong>safe</strong></td>'));
  assert.ok(html.includes('<td>&lt;img onerror=x&gt;</td><td>a | b</td>'));
  assert.ok(!html.includes('<img'));
  assert.ok(render('a | b\nwords | words').startsWith('<p>'));
});

test('blockquotes keep hostile markup inert and do not recurse on nested markers', () => {
  const html = render('> **quoted**\n> <script>alert(1)</script>\n\nAfter');
  assert.ok(html.includes('<blockquote><p><strong>quoted</strong><br>&lt;script&gt;alert(1)&lt;/script&gt;</p></blockquote>'));
  assert.ok(html.endsWith('<p>After</p>'));
  assert.doesNotThrow(() => render('>'.repeat(50000)));
});
