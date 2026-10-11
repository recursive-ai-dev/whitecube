import { useEffect, useRef } from 'react';

// Shared modal behavior, including nested dialogs inside monitoring panels.
export function useDialog(open: boolean, onClose: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!open || !ref.current) return;
    const dialog = ref.current;
    const previous = document.activeElement as HTMLElement | null;
    const siblings: { node: HTMLElement; inert: boolean }[] = [];
    let branch: HTMLElement = dialog;
    while (branch.parentElement && branch !== document.body) {
      for (const child of branch.parentElement.children) {
        if (child !== branch && child instanceof HTMLElement) {
          siblings.push({ node: child, inert: child.inert });
          child.inert = true;
        }
      }
      branch = branch.parentElement;
    }
    const focusable = () =>
      [
        ...dialog.querySelectorAll<HTMLElement>(
          'button, input, select, textarea, a[href], [tabindex]'
        ),
      ].filter(
        (el) => !el.hasAttribute('disabled') && el.tabIndex >= 0 && el.getClientRects().length
      );
    (dialog.querySelector<HTMLElement>('[data-autofocus]') || focusable()[0] || dialog).focus();
    const keydown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        close.current();
      }
      if (e.key === 'Tab') {
        const items = focusable(),
          first = items[0],
          last = items.at(-1);
        if (!first) {
          e.preventDefault();
          dialog.focus();
        } else if (
          e.shiftKey &&
          (document.activeElement === first || !dialog.contains(document.activeElement))
        ) {
          e.preventDefault();
          last?.focus();
        } else if (
          !e.shiftKey &&
          (document.activeElement === last || !dialog.contains(document.activeElement))
        ) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', keydown, true);
    return () => {
      document.removeEventListener('keydown', keydown, true);
      siblings.forEach(({ node, inert }) => {
        node.inert = inert;
      });
      if (previous?.isConnected) previous.focus();
    };
  }, [open]);
  return ref;
}
