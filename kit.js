// Kit layout + note map shared by index.html (overlay) and stats.html (history).
// Alesis Nitro Max defaults; GM-compatible.
const PIECES = [
  // id,      label,     x,   y,   r,  color,     cymbal
  ["crash",  "Crash",   205, 105, 78, "#ffcf5a", true],
  ["ride",   "Ride",    655, 150, 92, "#ffcf5a", true],
  ["hihat",  "Hi-hat",  105, 255, 64, "#ffe08a", true],
  ["tom1",   "Tom 1",   325, 195, 50, "#5ad1ff"],
  ["tom2",   "Tom 2",   480, 195, 54, "#7a8cff"],
  ["tom3",   "Floor",   600, 330, 66, "#b77aff"],
  ["snare",  "Snare",   240, 335, 60, "#ff5a7a"],
  ["kick",   "Kick",    415, 370, 84, "#ff8a3d"],
  ["hhpedal","HH Pedal",105, 410, 26, "#ffe08a"],
];

// note -> [piece, part]   part: "head" | "rim" | "bell" | "half" | "open"
const NOTES = {
  35: ["kick", "head"], 36: ["kick", "head"],
  38: ["snare", "head"], 37: ["snare", "rim"], 40: ["snare", "rim"],
  48: ["tom1", "head"], 50: ["tom1", "rim"],
  45: ["tom2", "head"], 47: ["tom2", "rim"],
  43: ["tom3", "head"], 58: ["tom3", "rim"], 41: ["tom3", "head"],
  42: ["hihat", "head"], 22: ["hihat", "rim"], 46: ["hihat", "open"], 26: ["hihat", "open"],
  44: ["hhpedal", "head"], 21: ["hhpedal", "head"],          // 21 = foot splash
  23: ["hihat", "half"],                                    // half-open
  49: ["crash", "head"], 55: ["crash", "rim"], 57: ["crash", "head"], 52: ["crash", "head"],
  51: ["ride", "head"], 59: ["ride", "rim"], 53: ["ride", "bell"],
};

// Display order for per-piece counts.
const PIECE_ORDER = ["kick", "snare", "hihat", "hhpedal", "tom1", "tom2", "tom3", "crash", "ride"];

// {note: count} (as served by /api/stats) -> {pieceId: count}; unmapped notes are skipped.
function pieceCounts(noteCounts) {
  const out = {};
  for (const note in noteCounts) {
    const m = NOTES[note];
    if (m) out[m[0]] = (out[m[0]] || 0) + noteCounts[note];
  }
  return out;
}
