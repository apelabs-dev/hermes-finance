/* One rule for every popover that hangs under a trigger (the category picker, the Time window picker, the help panels):
 * when the page moves under it, it goes away, because it is placed once and would otherwise stay put while its row
 * scrolls off. Scrolling the page, any scroller outside the popover (a capture-phase listener on window sees them all),
 * a resize that changes the width (a rotation, a resized window) or the height on a mouse-driven screen, closes it.
 * Scrolling inside the popover's own list does not. A phone's on-screen keyboard changes only the height of a touch
 * screen, so that never closes a docked picker. Focus goes back to the trigger without moving the page.
 *
 * HFPopClose.watch({root, trigger, close}) -> stop(). `root` is the popover's element (or a function giving it),
 * `trigger` the element or function that opened it, `close(restoreFocus)` the popover's own close. Call stop() when
 * the popover closes by any other route. HFPopClose.focus(trigger) is the focus return the closers share. */
(function () {
  'use strict';
  const el = (v) => (typeof v === 'function' ? v() : v);

  // focus(): to the trigger, never scrolling the page; a no-op once the trigger is gone from the page
  function focus(trigger) {
    const t = el(trigger);
    if (t && t.isConnected && typeof t.focus === 'function') t.focus({preventScroll: true});
  }

  function watch({root, trigger, close}) {
    const width = window.innerWidth;
    const height = window.innerHeight;
    let live = false;
    let stopped = false;
    let timer = 0;
    let frame = 0;
    const dismiss = () => {
      if (stopped) return;
      stop();
      // focus goes back to the trigger when it was inside the popover, on nothing, or already on the trigger itself;
      // a pointer or key that moved it to some other control keeps it there
      const here = document.activeElement;
      const r = el(root);
      const t = el(trigger);
      close(!here || here === document.body || here === t || Boolean(r && r.contains(here)));
    };
    const onScroll = (e) => {
      const r = el(root);
      if (r && e.target instanceof Node && r.contains(e.target)) return;   // the popover's own list
      dismiss();
    };
    const onResize = () => {
      const touch = matchMedia('(pointer: coarse)').matches;
      if (window.innerWidth !== width || (!touch && window.innerHeight !== height)) dismiss();
    };
    // Start listening after the next frame: a scroll that was queued just before the popover opened (a row scrolled
    // into view, then tapped) fires on the next frame, and must not close what that tap just opened.
    const start = () => {
      if (live || stopped) return;
      live = true;
      cancelAnimationFrame(frame);
      clearTimeout(timer);
      window.addEventListener('scroll', onScroll, true);
      window.addEventListener('resize', onResize);
      window.addEventListener('orientationchange', dismiss);
    };
    frame = requestAnimationFrame(start);
    timer = setTimeout(start, 120);
    function stop() {
      stopped = true;
      cancelAnimationFrame(frame);
      clearTimeout(timer);
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', onResize);
      window.removeEventListener('orientationchange', dismiss);
    }
    return stop;
  }

  window.HFPopClose = {watch, focus};
})();
