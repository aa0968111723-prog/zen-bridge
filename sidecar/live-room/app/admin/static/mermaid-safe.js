/* Optional diagram renderer. Only local, pinned Mermaid may initialize this. */
(() => {
  'use strict';
  if (!window.mermaid) return;
  window.mermaid.initialize({
    securityLevel: 'strict',
    startOnLoad: false,
    maxTextSize: 10000,
    maxEdges: 500,
    suppressErrorRendering: true,
    secure: ['securityLevel', 'startOnLoad', 'maxTextSize', 'maxEdges', 'secure'],
  });
})();
