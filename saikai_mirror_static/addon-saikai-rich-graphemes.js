(function (root, factory) {
  if (typeof exports === "object" && typeof module === "object") {
    module.exports = factory();
  } else if (typeof define === "function" && define.amd) {
    define([], factory);
  } else {
    root.SaikaiRichGraphemesAddon = factory();
  }
})(globalThis, function () {
  "use strict";

  const WIDTH_AND_JOIN = 0x7;
  const GCB_SHIFT = 3;
  const GCB_MASK = 0x1f;
  const RI_ODD = 1 << 8;
  const EP_EXTEND = 1 << 9;
  const EP_ZWJ = 1 << 10;
  const INCB_ACTIVE = 1 << 11;
  const INCB_LINKED = 1 << 12;
  const LAST_NARROW = 1 << 13;
  const RICH_SKIP_NEXT = 1 << 14;

  const GCB_OTHER = 0;
  const GCB_CONTROL = 1;
  const GCB_LF = 2;
  const GCB_CR = 3;
  const GCB_EXTEND = 4;
  const GCB_PREPEND = 5;
  const GCB_SPACING_MARK = 6;
  const GCB_L = 7;
  const GCB_V = 8;
  const GCB_T = 9;
  const GCB_ZWJ = 10;
  const GCB_LV = 11;
  const GCB_LVT = 12;
  const GCB_RI = 13;

  const INCB_NONE = 0;
  const INCB_EXTEND = 1;
  const INCB_CONSONANT = 2;
  const INCB_LINKER = 3;

  const ZWJ = 0x200d;
  const VS16 = 0xfe0f;

  function makeWidthLookup(ranges) {
    const lastEnd = ranges.length ? ranges[ranges.length - 1][1] : -1;
    return function richWidth(codepoint) {
      if ((codepoint && codepoint < 32) ||
          (codepoint >= 0x7f && codepoint < 0xa0)) {
        return 0;
      }
      if (codepoint > lastEnd) {
        return 1;
      }
      let low = 0;
      let high = ranges.length - 1;
      while (low <= high) {
        const middle = (low + high) >>> 1;
        const range = ranges[middle];
        if (codepoint < range[0]) {
          high = middle - 1;
        } else if (codepoint > range[1]) {
          low = middle + 1;
        } else {
          return range[2];
        }
      }
      return 1;
    };
  }

  function makeRangeContains(ranges) {
    return function contains(codepoint) {
      let low = 0;
      let high = ranges.length - 1;
      while (low <= high) {
        const middle = (low + high) >>> 1;
        const range = ranges[middle];
        if (codepoint < range[0]) {
          high = middle - 1;
        } else if (codepoint > range[1]) {
          low = middle + 1;
        } else {
          return true;
        }
      }
      return false;
    };
  }

  function makeRangeValueLookup(ranges) {
    return function valueAt(codepoint) {
      let low = 0;
      let high = ranges.length - 1;
      while (low <= high) {
        const middle = (low + high) >>> 1;
        const range = ranges[middle];
        if (codepoint < range[0]) {
          high = middle - 1;
        } else if (codepoint > range[1]) {
          low = middle + 1;
        } else {
          return range[2];
        }
      }
      return 0;
    };
  }

  function isControl(gcb) {
    return gcb === GCB_CONTROL || gcb === GCB_CR || gcb === GCB_LF;
  }

  function semanticJoin(preceding, currentGcb, currentIncb, currentEp) {
    if (!preceding) {
      return false;
    }
    const previousGcb = (preceding >>> GCB_SHIFT) & GCB_MASK;
    if (previousGcb === GCB_CR && currentGcb === GCB_LF) {
      return true;
    }
    if (isControl(previousGcb) || isControl(currentGcb)) {
      return false;
    }
    if (previousGcb === GCB_L &&
        (currentGcb === GCB_L || currentGcb === GCB_V ||
         currentGcb === GCB_LV || currentGcb === GCB_LVT)) {
      return true;
    }
    if ((previousGcb === GCB_LV || previousGcb === GCB_V) &&
        (currentGcb === GCB_V || currentGcb === GCB_T)) {
      return true;
    }
    if ((previousGcb === GCB_LVT || previousGcb === GCB_T) &&
        currentGcb === GCB_T) {
      return true;
    }
    if (currentGcb === GCB_EXTEND || currentGcb === GCB_ZWJ ||
        currentGcb === GCB_SPACING_MARK || previousGcb === GCB_PREPEND) {
      return true;
    }
    if ((preceding & INCB_ACTIVE) && (preceding & INCB_LINKED) &&
        currentIncb === INCB_CONSONANT) {
      return true;
    }
    if ((preceding & EP_ZWJ) && currentEp) {
      return true;
    }
    return (
      previousGcb === GCB_RI && currentGcb === GCB_RI &&
      !!(preceding & RI_ODD)
    );
  }

  function stateBits(currentGcb, currentIncb, currentEp, joined, preceding) {
    const previousGcb = (preceding >>> GCB_SHIFT) & GCB_MASK;
    let bits = currentGcb << GCB_SHIFT;

    const riOdd = currentGcb === GCB_RI && (
      !joined || previousGcb !== GCB_RI || !(preceding & RI_ODD)
    );
    if (riOdd) {
      bits |= RI_ODD;
    }

    if (currentGcb === GCB_EXTEND && joined) {
      if (preceding & EP_EXTEND) {
        bits |= EP_EXTEND;
      }
    } else if (currentGcb === GCB_ZWJ && joined) {
      if (preceding & EP_EXTEND) {
        bits |= EP_ZWJ;
      }
    } else if (currentEp) {
      bits |= EP_EXTEND;
    }

    if (currentIncb === INCB_CONSONANT) {
      bits |= INCB_ACTIVE;
    } else if (joined &&
               (currentIncb === INCB_EXTEND ||
                currentIncb === INCB_LINKER) &&
               (preceding & INCB_ACTIVE)) {
      bits |= INCB_ACTIVE;
      if (currentIncb === INCB_LINKER || (preceding & INCB_LINKED)) {
        bits |= INCB_LINKED;
      }
    }
    return bits;
  }

  function properties(state, width, joined, lastNarrow) {
    let value = (
      state |
      ((Math.max(0, Math.min(2, width)) & 3) << 1) |
      (joined ? 1 : 0)
    );
    if (lastNarrow) {
      value |= LAST_NARROW;
    }
    return value;
  }

  class SaikaiRichGraphemesAddon {
    constructor(widthData) {
      if (!widthData || !Array.isArray(widthData.widths) ||
          !Array.isArray(widthData.narrowToWide) ||
          !Array.isArray(widthData.extendedPictographic) ||
          !Array.isArray(widthData.graphemeBreak) ||
          !Array.isArray(widthData.indicConjunctBreak)) {
        throw new TypeError("Rich Unicode width and grapheme data are required");
      }
      this._version = "saikai-rich-" + String(widthData.version || "unknown");
      this._richWidth = makeWidthLookup(widthData.widths);
      this._narrowToWide = new Set(widthData.narrowToWide);
      this._isExtendedPictographic = makeRangeContains(
        widthData.extendedPictographic
      );
      this._graphemeBreak = makeRangeValueLookup(widthData.graphemeBreak);
      this._indicConjunctBreak = makeRangeValueLookup(
        widthData.indicConjunctBreak
      );
      this._unicode = null;
      this._oldVersion = "";
      this._provider = null;
    }

    activate(terminal) {
      const richWidth = this._richWidth;
      const narrowToWide = this._narrowToWide;
      const isExtendedPictographic = this._isExtendedPictographic;
      const graphemeBreak = this._graphemeBreak;
      const indicConjunctBreak = this._indicConjunctBreak;
      const provider = {
        version: this._version,
        wcwidth(codepoint) {
          return richWidth(codepoint);
        },
        charProperties(codepoint, preceding) {
          const currentGcb = graphemeBreak(codepoint);
          const currentIncb = indicConjunctBreak(codepoint);
          const currentEp = isExtendedPictographic(codepoint);
          const joined = semanticJoin(
            preceding, currentGcb, currentIncb, currentEp
          );
          let state = stateBits(
            currentGcb, currentIncb, currentEp, joined, preceding
          );
          const codepointWidth = richWidth(codepoint);
          const previousWidth = (preceding >>> 1) & 3;
          const previousNarrow = !!(preceding & LAST_NARROW);
          const skipCurrent = !!(preceding & RICH_SKIP_NEXT);
          let combinedWidth = codepointWidth;
          let lastNarrow = (
            codepointWidth > 0 && narrowToWide.has(codepoint)
          );

          if (!joined) {
            // Rich still treats a leading ZWJ as consuming exactly the next
            // codepoint. Do not inherit an older cluster's skip bit, but arm a
            // fresh one for this cluster.
            if (codepoint === ZWJ) {
              state |= RICH_SKIP_NEXT;
            }
            return properties(
              state, codepointWidth, false, lastNarrow
            );
          }
          if (skipCurrent) {
            combinedWidth = previousWidth;
            lastNarrow = previousNarrow;
          } else if (codepoint === ZWJ) {
            combinedWidth = previousWidth;
            lastNarrow = previousNarrow;
            // Rich's width loop consumes the ZWJ and skips exactly the next
            // codepoint. A skipped second ZWJ must not recursively arm another
            // skip (otherwise a following VS16 never widens its narrow base).
            state |= RICH_SKIP_NEXT;
          } else if (codepoint === VS16) {
            combinedWidth = previousWidth + (previousNarrow ? 1 : 0);
            lastNarrow = false;
          } else if (codepointWidth === 0) {
            combinedWidth = previousWidth;
            lastNarrow = previousNarrow;
          } else {
            combinedWidth = previousWidth + codepointWidth;
          }

          if (combinedWidth <= 2) {
            return properties(state, combinedWidth, true, lastNarrow);
          }
          // xterm stores at most two columns in one cell. Preserve the UAX #29
          // state above but start another storage cell so a 3+ column Rich EGC
          // retains the same total text and cursor position.
          return properties(
            state,
            codepoint === VS16 ? 1 : codepointWidth,
            false,
            lastNarrow
          );
        }
      };

      this._unicode = terminal.unicode;
      this._oldVersion = this._unicode.activeVersion;
      this._provider = provider;
      this._unicode.register(provider);
      this._unicode.activeVersion = provider.version;
    }

    dispose() {
      if (this._unicode && this._oldVersion &&
          this._unicode.activeVersion === this._version) {
        this._unicode.activeVersion = this._oldVersion;
      }
      this._unicode = null;
      this._provider = null;
    }
  }

  return { SaikaiRichGraphemesAddon };
});
