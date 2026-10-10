// Small, safe Markdown renderer for chat replies: fenced code (with a Copy button), inline
// code, bold, italics, headings, lists and paragraphs. Every piece of model text is HTML-escaped
// before any tag is added, and links/images/raw HTML are deliberately not supported.
(function (root) {
  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function inline(s) {
    return String(s)
      .split(/(`[^`\n]+`)/g)
      .map((part, i) => {
        if (i % 2) return `<code>${esc(part.slice(1, -1))}</code>`;
        return esc(part)
          .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
          .replace(/(^|[^*])\*([^*\s][^*\n]*)\*(?!\*)/g, '$1<em>$2</em>');
      })
      .join('');
  }

  function render(text) {
    const lines = String(text).replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let para = [];
    let list = null;

    const flushPara = () => {
      if (para.length) out.push(`<p>${para.map(inline).join('<br>')}</p>`);
      para = [];
    };
    const flushList = () => {
      if (list) out.push(`<${list.tag}>${list.items.map((x) => `<li>${inline(x)}</li>`).join('')}</${list.tag}>`);
      list = null;
    };
    const addItem = (tag, item) => {
      flushPara();
      if (list && list.tag !== tag) flushList();
      if (!list) list = { tag, items: [] };
      list.items.push(item);
    };

    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      const fence = /^\s*```\s*([\w+#.-]*)\s*$/.exec(line);
      if (fence) {
        flushPara();
        flushList();
        const code = [];
        i++;
        // An unclosed fence (a reply still streaming, or cut off) renders as code to the end.
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) code.push(lines[i++]);
        i++;
        out.push(
          `<div class="code-block"><div class="code-head"><span>${esc(fence[1] || 'text')}</span>` +
            `<button class="copy-btn" type="button">Copy</button></div><pre><code>${esc(code.join('\n'))}</code></pre></div>`
        );
        continue;
      }
      const heading = /^(#{1,3})\s+(.+)$/.exec(line);
      const bullet = /^\s*[-*]\s+(.+)$/.exec(line);
      const numbered = /^\s*\d+[.)]\s+(.+)$/.exec(line);
      if (heading) {
        flushPara();
        flushList();
        out.push(`<h${heading[1].length + 2}>${inline(heading[2])}</h${heading[1].length + 2}>`);
      } else if (bullet) {
        addItem('ul', bullet[1]);
      } else if (numbered) {
        addItem('ol', numbered[1]);
      } else if (line.trim() === '') {
        flushPara();
        flushList();
      } else {
        flushList();
        para.push(line);
      }
      i++;
    }
    flushPara();
    flushList();
    return out.join('');
  }

  root.renderMarkdown = render;
  if (typeof module !== 'undefined' && module.exports) module.exports = { render, esc };
})(typeof window !== 'undefined' ? window : globalThis);
