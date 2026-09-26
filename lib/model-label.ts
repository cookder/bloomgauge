// One display name per model everywhere: gemma-4-26b-qat-4bit → Gemma 4 26B,
// gpt-oss-20b → GPT-OSS 20B. Quantization suffixes are dropped.
const UPPER = new Set(['gpt', 'oss', 'vl', 'mtp', 'moe', 'it']);
export function modelLabel(id: string) {
  const parts = id
    .replace(/-(qat-)?\d+bit$|-mx?fp\d+$|-(bf|fp)16$/i, '')
    .split('-')
    .filter(Boolean);
  const words = parts.map((p) =>
    UPPER.has(p.toLowerCase())
      ? p.toUpperCase()
      : /^\d+(\.\d+)?[bm]$|^a\d+b$|^e\d+b$/i.test(p)
        ? p.toUpperCase()
        : p.charAt(0).toUpperCase() + p.slice(1),
  );
  // Joined brand words keep a hyphen: GPT-OSS.
  return words.join(' ').replace(/^GPT OSS\b/, 'GPT-OSS');
}

/** The quantization a model id names: -qat-4bit → QAT 4-bit, -8bit → 8-bit, -mxfp8 → MXFP8. */
export function quantLabel(id: string) {
  const m = id.match(/-(qat-)?(\d+)bit$|-(mx?fp\d+)$|-((?:bf|fp)16)$/i);
  if (!m) return '';
  if (m[2]) return (m[1] ? 'QAT ' : '') + m[2] + '-bit';
  return (m[3] || m[4]).toUpperCase();
}

/** Labels for a list: the short name, plus the quantization only where two ids share it. */
export function distinctLabels(
  ids: string[],
  label: (id: string) => string = modelLabel,
) {
  const names = new Map(ids.map((id) => [id, label(id)]));
  const counts = new Map<string, number>();
  for (const name of names.values())
    counts.set(name, (counts.get(name) ?? 0) + 1);
  return (id: string) => {
    const name = names.get(id) ?? label(id);
    const quant = quantLabel(id);
    return (counts.get(name) ?? 0) > 1 && quant ? `${name} ${quant}` : name;
  };
}
