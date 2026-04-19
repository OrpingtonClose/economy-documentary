import "@testing-library/jest-dom";

// jsdom does not implement ResizeObserver, which cmdk (used by
// shadcn's <Command /> primitive — see the Intent Bar, DESIGN-06)
// requires at mount time. Provide a no-op polyfill so tests can
// render components that use cmdk without crashing.
if (typeof globalThis.ResizeObserver === "undefined") {
  class ResizeObserverPolyfill {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  (globalThis as unknown as { ResizeObserver: typeof ResizeObserverPolyfill }).ResizeObserver =
    ResizeObserverPolyfill;
}

// jsdom doesn't implement these either; Radix's Dialog/Sheet primitives
// (used by the Scene Drilldown, DESIGN-05) call them on mount.
if (typeof Element !== "undefined") {
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = (): boolean => false;
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = (): void => {};
  }
}
