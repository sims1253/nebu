// The server counts offsets in Unicode code points (DESIGN.md, "Provenance that survives
// re-parsing"); JavaScript strings index UTF-16 code units. Convert explicitly at the edge.

/** Maps code-point offsets into `text` to UTF-16 indexes. Offset `length` maps to the end. */
export const codePointIndex = (text: string): ((offset: number) => number) => {
  let astral = false;
  for (let i = 0; i < text.length; i++) {
    const unit = text.charCodeAt(i);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      astral = true;
      break;
    }
  }
  if (!astral) return (offset) => offset;
  const units: number[] = [];
  let unit = 0;
  for (const char of text) {
    units.push(unit);
    unit += char.length;
  }
  units.push(unit);
  return (offset) => units[Math.min(offset, units.length - 1)] ?? unit;
};

/** The inverse: UTF-16 indexes to code-point offsets. */
export const utf16ToCodePoint = (text: string, index: number): number => {
  let points = 0;
  for (let i = 0; i < index && i < text.length; i++) {
    const unit = text.charCodeAt(i);
    if (!(unit >= 0xdc00 && unit <= 0xdfff)) points++;
  }
  return points;
};

/** `text.slice` over code-point offsets. */
export const sliceCodePoints = (text: string, start: number, end: number): string => {
  const at = codePointIndex(text);
  return text.slice(at(start), at(end));
};
