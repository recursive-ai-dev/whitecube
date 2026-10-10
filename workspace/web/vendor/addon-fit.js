/* Small offline fit adapter for the bundled xterm public API. MIT. */
(function (global) {
  class FitAddon {
    activate(terminal) { this.terminal = terminal; }
    dispose() { this.terminal = null; }
    proposeDimensions() {
      const t = this.terminal, parent = t?.element?.parentElement;
      if (!parent || !parent.clientWidth || !parent.clientHeight) return;
      const style = getComputedStyle(parent);
      const screen = t.element.querySelector('.xterm-screen');
      const rect = screen?.getBoundingClientRect();
      const font = t.options.fontSize || 14;
      const charWidth = rect?.width > 0 ? rect.width / t.cols : font * 0.61;
      const charHeight = rect?.height > 0 ? rect.height / t.rows : font * (t.options.lineHeight || 1.2);
      const width = parent.clientWidth - parseFloat(style.paddingLeft || 0) - parseFloat(style.paddingRight || 0) - 18;
      const height = parent.clientHeight - parseFloat(style.paddingTop || 0) - parseFloat(style.paddingBottom || 0);
      return {cols: Math.min(500, Math.max(2, Math.floor(width / charWidth))), rows: Math.min(200, Math.max(1, Math.floor(height / charHeight)))};
    }
    fit() { const size = this.proposeDimensions(); if (size) this.terminal.resize(size.cols, size.rows); }
  }
  global.FitAddon = {FitAddon};
})(window);
