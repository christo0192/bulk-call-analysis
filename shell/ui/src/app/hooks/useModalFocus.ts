import { useEffect, useRef } from 'preact/hooks';
import type { RefObject } from 'preact';

/**
 * Keyboard rules for a modal dialog: focus moves into it when it opens, Tab and Shift+Tab stay
 * inside it, Escape closes it, and focus goes back to the button that opened it.
 *
 * `opener` is where focus returns; without it, the element that had focus when the dialog
 * opened. (A click does not focus a button in every web view, so pass it when you can.)
 */
export function useModalFocus(
  dialog: RefObject<HTMLElement>,
  onClose: () => void,
  opener?: RefObject<HTMLElement | null>,
): void {
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    const returnTo = opener?.current ?? document.activeElement;
    const focusFirst = requestAnimationFrame(() =>
      dialog.current?.querySelector<HTMLElement>('input, select, button')?.focus(),
    );
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab' || !dialog.current) return;
      const items = [
        ...dialog.current.querySelectorAll<HTMLElement>('button, input, select, textarea'),
      ].filter(
        (item) => !(item as HTMLButtonElement | HTMLInputElement | HTMLSelectElement).disabled,
      );
      const first = items[0];
      const last = items[items.length - 1];
      const outside = !dialog.current.contains(document.activeElement);
      if (
        first &&
        last &&
        (outside ||
          (event.shiftKey && document.activeElement === first) ||
          (!event.shiftKey && document.activeElement === last))
      ) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      }
    };
    document.addEventListener('keydown', handler);
    return () => {
      cancelAnimationFrame(focusFirst);
      document.removeEventListener('keydown', handler);
      if (returnTo instanceof HTMLElement) returnTo.focus();
    };
  }, []);
}
