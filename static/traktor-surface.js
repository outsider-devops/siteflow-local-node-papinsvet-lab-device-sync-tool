'use strict';

// Physical inventory: NI S2 MK2 manual, sections 6–7 and Appendix C.
// MIDI identities are imported from Controller Editor; Learn can override them.
const SURFACE = [];
const TRAKTOR_ATLAS = '/assets/traktor/traktor-atlas-v1.png';
const TRAKTOR_SPRITES = {
  metal: [28, 68, 304, 304], jog: [364, 42, 360, 360],
  knob: [770, 85, 274, 274], encoder: [1124, 85, 278, 278],
  pad: [54, 435, 252, 250], key: [398, 474, 290, 172],
  cap: [760, 486, 292, 150], round: [1155, 452, 218, 218],
  decalA: [15, 704, 340, 340], decalB: [400, 675, 294, 365],
  wing: [738, 733, 362, 280], wild: [1105, 704, 324, 324]
};

function addSurface(id, label, type, x, y, deck = '', extra = {}) {
  SURFACE.push({ id, label, type, x, y, deck, action: 'turn', ...extra });
}
function addPress(parentId) {
  const parent = SURFACE.find(c => c.id === parentId);
  parent.pushable = true;
  addSurface(parentId + '-press', parent.label + ' · Push', 'button', parent.x, parent.y,
    parent.deck, { parentId, action: 'press', hidden: true });
}

for (const deck of ['A', 'B']) {
  const d = deck.toLowerCase(), right = deck === 'B';
  const fxX = right ? [840, 940, 1040, 1140] : [60, 160, 260, 360];
  ['Dry/Wet', '1', '2', '3'].forEach((name, i) => {
    addSurface(`deck-${d}-fx-${i}`, `${deck} · FX ${name}`, 'knob', fxX[i], 88, deck,
      { short: i ? name : 'DRY/WET', accent: 'orange' });
    addSurface(`deck-${d}-fx-button-${i}`, `${deck} · FX ${i ? name : 'On'}`, 'button',
      fxX[i], 137, deck, { round: true, action: 'press', short: i ? name : 'ON' });
  });
  const jogX = right ? 990 : 210;
  addSurface(`deck-${d}-jog-top`, `${deck} · Jog top plate`, 'jog', jogX, 321, deck);
  addSurface(`deck-${d}-jog-rim`, `${deck} · Jog outer rim`, 'jog-rim', jogX, 321, deck,
    { animationTarget: `deck-${d}-jog-top` });
  addPress(`deck-${d}-jog-top`);
  const tempoX = right ? 1156 : 44;
  addSurface(`deck-${d}-flux`, `${deck} · Flux`, 'button', tempoX, 501, deck,
    { round: true, action: 'press', short: '', topLabel: 'FLUX', accent: 'orange' });
  addSurface(`deck-${d}-tempo`, `${deck} · Tempo fader`, 'fader', tempoX, 624, deck,
    { travel: 148, short: 'TEMPO', topLabel: 'TEMPO' });
  // The right LOOP section is mirrored physically: SIZE/SET then IN/OUT then MOVE/ACTIVE.
  const loop = right ? { size: 850, in: 921, out: 985, move: 1056 }
    : { move: 144, in: 215, out: 279, size: 350 };
  for (const name of ['move', 'size']) {
    addSurface(`deck-${d}-loop-${name}`, `${deck} · Loop ${name === 'move' ? 'Move' : 'Size'} encoder`,
      'knob', loop[name], 554, deck, { short: name.toUpperCase(), encoder: true });
    addPress(`deck-${d}-loop-${name}`);
  }
  ['in', 'out'].forEach(name => addSurface(`deck-${d}-loop-${name}`, `${deck} · Loop ${name.toUpperCase()}`,
    'button', loop[name], 554, deck, { short: name.toUpperCase(), action: 'press' }));
  for (let i = 0; i < 4; i++) addSurface(`deck-${d}-remix-${i + 1}`, `${deck} · Remix slot ${i + 1}`,
    'button', (right ? 850 : 148) + i * 70, 634, deck,
    { short: String(i + 1), pad: true, action: 'press' });
  ['SHIFT', 'SYNC', 'CUE', 'PLAY'].forEach((name, i) => addSurface(`deck-${d}-${name.toLowerCase()}`,
    `${deck} · ${name}`, 'button', (right ? 850 : 148) + i * 70, 705, deck,
    { short: name, action: 'press', reserved: name === 'SHIFT' }));
}
for (const deck of ['A', 'B']) {
  const d = deck.toLowerCase(), x = deck === 'B' ? 733 : 467;
  addSurface(`mix-${d}-gain-filter`, `${deck} · Gain / Filter`, 'knob', x, 88, deck,
    { short: 'GAIN / FILTER', encoder: true });
  addPress(`mix-${d}-gain-filter`);
  ['FX 1', 'FX 2'].forEach((name, i) => addSurface(`mix-${d}-assign-${i + 1}`,
    `${deck} · Assign ${name}`, 'button', x - 35 + i * 70, 137, deck,
    { short: String(i + 1), round: true, action: 'press' }));
  ['HI', 'MID', 'LOW'].forEach((name, i) => addSurface(`mix-${d}-${name.toLowerCase()}`,
    `${deck} · ${name} EQ`, 'knob', x, 234 + i * 88, deck, { short: name }));
  addSurface(`mix-${d}-cue`, `${deck} · Cue monitor`, 'button', x, 494, deck,
    { short: 'CUE', action: 'press' });
  addSurface(`mix-${d}-channel-fader`, `${deck} · Channel fader`, 'fader', x, 603, deck,
    { travel: 140, short: deck });
}
addSurface('mixer-remix', 'Remix level', 'knob', 600, 234, '', { short: 'REMIX', accent: 'orange' });
addSurface('mixer-remix-a', 'Remix A', 'button', 570, 284, '', { short: 'A', round: true, action: 'press' });
addSurface('mixer-remix-b', 'Remix B', 'button', 630, 284, '', { short: 'B', round: true, action: 'press' });
addSurface('mixer-browser', 'Browse encoder', 'knob', 600, 410, '', { short: 'BROWSE', encoder: true, accent: 'cyan' });
addPress('mixer-browser');
addSurface('mixer-load-a', 'Load A', 'button', 570, 460, '', { short: 'A', round: true, action: 'press' });
addSurface('mixer-load-b', 'Load B', 'button', 630, 460, '', { short: 'B', round: true, action: 'press' });
addSurface('mixer-crossfader', 'Crossfader', 'crossfader', 600, 709, '', { short: '', travel: 130 });
addSurface('front-cue-mix', 'Front · Cue Mix', 'knob', 0, 0, '', { hidden: true });

function rasterSprite(name, x, y, width, height = width, extra = '') {
  return `<rect class="surface-raster ${extra}" x="${x}" y="${y}" width="${width}" height="${height}" fill="url(#traktor-sprite-${name})" aria-hidden="true"/>`;
}
function surfaceLabel(text, x, y, extra = '') {
  return `<text class="surface-print ${extra}" x="${x}" y="${y}" text-anchor="middle">${esc(text)}</text>`;
}
function physicalSurface(c) {
  return SURFACE.find(x => x.id === (c.parentId || c.id)) || c;
}
function surfaceActions(c) {
  const parent = physicalSurface(c);
  if (parent.type === 'jog' || parent.type === 'jog-rim') {
    const prefix = parent.id.replace(/-jog-(top|rim)$/, '-jog-');
    return [SURFACE.find(x => x.id === prefix + 'top'), SURFACE.find(x => x.id === prefix + 'rim'),
      SURFACE.find(x => x.id === prefix + 'top-press')].filter(Boolean);
  }
  return [parent, ...SURFACE.filter(x => x.parentId === parent.id)];
}
function surfaceActionLabel(c) {
  if (c.type === 'jog-rim') return 'Rim · turn';
  if (c.parentId?.includes('jog')) return 'Top plate · press';
  if (c.type === 'jog') return 'Top plate · turn';
  return c.action === 'press' ? 'Press / release' : c.type === 'fader' || c.type === 'crossfader' ? 'Slide' : 'Turn';
}
function jogGraphic(c) {
  const skin = surfaceSkin === 'papinsvet'
    ? rasterSprite(c.deck === 'A' ? 'decalA' : 'decalB', -73, -81, 146, 162)
    : surfaceLabel('TRAKTOR', 0, 5, 'factory-disc-label');
  return rasterSprite('jog', -164, -164, 328) + skin;
}
function renderSurfaceControl(c) {
  if (c.hidden) return '';
  const selected = selectedControl && physicalSurface(selectedControl).id === c.id;
  let face = '', highlight = '', label = '';
  if (c.type === 'knob') {
    face = `<g class="surface-knob-rotor">${rasterSprite(c.encoder ? 'encoder' : 'knob', -23, -23, 46)}${c.encoder ? '<path class="surface-encoder-position" d="M0 -16v-5"/>' : ''}</g>`;
    highlight = '<circle class="surface-highlight" r="28"/>';
    label = surfaceLabel(c.short, 0, c.id === 'mixer-remix' || c.id.includes('gain-filter') ? -35 : c.id.includes('-fx-') ? 32 : 38, c.accent || '');
  } else if (c.type === 'button') {
    face = c.round ? rasterSprite('round', -12, -12, 24) : c.pad
      ? rasterSprite('pad', -29, -24, 58, 48) : rasterSprite('key', -29, -19, 58, 38);
    if (c.short) face += surfaceLabel(c.short, 0, 4, c.round ? 'round-key-label' : 'key-label');
    highlight = c.round ? '<circle class="surface-highlight" r="16"/>'
      : `<rect class="surface-highlight" x="-32" y="${c.pad ? -27 : -22}" width="64" height="${c.pad ? 54 : 44}" rx="5"/>`;
    if (c.topLabel) label = surfaceLabel(c.topLabel, 0, -23, c.accent || '');
  } else if (c.type === 'jog') {
    face = jogGraphic(c);
    highlight = '<circle class="surface-highlight jog-highlight" r="166"/>';
  } else if (c.type === 'jog-rim') {
    face = '<circle class="surface-rim-hit" r="152"/>';
    highlight = '<circle class="surface-highlight jog-highlight" r="168"/>';
  } else {
    const cross = c.type === 'crossfader', travel = c.travel;
    const ticks = Array.from({ length: 15 }, (_, i) => {
      const p = -travel / 2 + travel * i / 14;
      return cross ? `<path d="M${p} -12v-8"/>` : `<path d="M-17 ${p}h-9M17 ${p}h9"/>`;
    }).join('');
    face = `<g class="surface-fader-scale">${ticks}</g>` + (cross
      ? `<rect class="surface-track" x="${-travel / 2}" y="-5" width="${travel}" height="10" rx="3"/>`
      : `<rect class="surface-track" x="-5" y="${-travel / 2}" width="10" height="${travel}" rx="3"/>`)
      + `<g class="surface-fader-cap">${cross ? '<g transform="rotate(90)">' + rasterSprite('cap', -29, -14, 58, 28) + '</g>' : rasterSprite('cap', -29, -14, 58, 28)}</g>`;
    highlight = cross ? `<rect class="surface-highlight" x="${-travel / 2 - 8}" y="-26" width="${travel + 16}" height="52" rx="5"/>`
      : `<rect class="surface-highlight" x="-32" y="${-travel / 2 - 8}" width="64" height="${travel + 16}" rx="5"/>`;
    if (c.topLabel) label = surfaceLabel(c.topLabel, 0, -travel / 2 - 17);
  }
  const hit = c.type === 'jog' ? '<circle class="surface-hit" r="132"/>' : c.type === 'jog-rim' ? ''
    : c.type === 'knob' ? '<circle class="surface-hit" r="29"/>'
    : c.type === 'fader' ? `<rect class="surface-hit" x="-32" y="${-c.travel / 2 - 8}" width="64" height="${c.travel + 16}"/>`
    : c.type === 'crossfader' ? `<rect class="surface-hit" x="${-c.travel / 2 - 8}" y="-26" width="${c.travel + 16}" height="52"/>`
    : c.round ? '<circle class="surface-hit" r="16"/>' : `<rect class="surface-hit" x="-32" y="${c.pad ? -27 : -22}" width="64" height="${c.pad ? 54 : 44}"/>`;
  return `<g class="surface-control surface-${c.type}${selected ? ' selected' : ''}${c.reserved ? ' reserved' : ''}" data-surface-id="${c.id}" tabindex="0" role="button" aria-label="Select ${esc(c.label)}${c.type === 'knob' ? ' knob' : c.type === 'button' ? ' button' : ''}"><title>${esc(c.label)}${c.pushable ? ' · turn / push' : ''}${c.reserved ? ' · controller Shift layer' : ''}</title><g transform="translate(${c.x} ${c.y})">${label}${highlight}<g data-control-face>${face}</g>${hit}</g></g>`;
}
function renderSurface() {
  const svg = $('#midi-surface');
  const parts = [
    `<defs><image id="traktor-atlas-image" href="${TRAKTOR_ATLAS}" width="1448" height="1086"/>` + Object.entries(TRAKTOR_SPRITES).map(([name, box]) => `<pattern id="traktor-sprite-${name}" width="1" height="1" viewBox="${box.join(' ')}" preserveAspectRatio="none"><use href="#traktor-atlas-image"/></pattern>`).join('') + '<clipPath id="traktor-face-clip"><rect x="18" y="18" width="1164" height="724" rx="5"/></clipPath><linearGradient id="traktor-gloss-black" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#131619"/><stop offset=".18" stop-color="#080a0c"/><stop offset=".32" stop-color="#1b1f23"/><stop offset=".38" stop-color="#111417"/><stop offset=".56" stop-color="#050708"/><stop offset="1" stop-color="#0f1215"/></linearGradient></defs>',
    '<rect class="surface-housing" x="8" y="8" width="1184" height="744" rx="12"/>',
    '<rect class="surface-faceplate" x="18" y="18" width="1164" height="724" rx="5"/>',
    '<image class="surface-metal-skin" href="/assets/traktor/brushed-black-v1.png" x="18" y="18" width="1164" height="724" preserveAspectRatio="none" clip-path="url(#traktor-face-clip)"/>',
    '<rect class="surface-mixer-gloss" x="414" y="53" width="372" height="684" fill="url(#traktor-gloss-black)"/>',
    '<path class="surface-mixer-reflection" d="M415 54h370M415 54v682M785 54v682"/>',
    '<path class="surface-panel-seam" d="M20 52h1160M413 53v687M787 53v687M20 737h1160"/>',
    '<text class="surface-hardware-brand" x="34" y="41">TRAKTOR <tspan>KONTROL S2</tspan></text>',
    '<text class="surface-ni-wordmark" x="1143" y="41">NI</text>',
    surfaceLabel('FX 1', 367, 185, 'orange'), surfaceLabel('FX 2', 1142, 185, 'orange'),
    surfaceLabel('LOOP', 248, 523), surfaceLabel('LOOP', 952, 523),
    surfaceLabel('CUE / REMIX SLOTS', 253, 595, 'orange'), surfaceLabel('CUE / REMIX SLOTS', 955, 595, 'orange'),
    surfaceLabel('TRANSPORT', 253, 672), surfaceLabel('TRANSPORT', 955, 672),
    surfaceLabel('MAIN', 600, 64, 'cyan'), '<g role="img" aria-label="Main level · hardware audio only"><title>Main level · hardware audio only</title>' + rasterSprite('knob', 577, 65, 46) + '</g>',
    surfaceLabel('LEVEL', 600, 126, 'cyan'), surfaceLabel('LOAD', 600, 484, 'cyan'),
    surfaceLabel('FX', 467, 174, 'orange'), surfaceLabel('FX', 733, 174, 'orange'),
    surfaceLabel('LEVEL', 600, 572),
    '<g class="surface-meter" aria-label="Hardware audio meters · output only">' + [574, 626].map(x => Array.from({ length: 11 }, (_, i) => `<rect x="${x}" y="${588 + i * 6}" width="5" height="3"/>`).join('')).join('') + '</g>',
    '<g class="surface-fixed-control" aria-label="Hardware status indicators"><circle cx="574" cy="512" r="3"/><circle cx="600" cy="512" r="3"/><circle cx="626" cy="512" r="3"/></g>',
    surfaceSkin === 'papinsvet' ? '<g class="surface-decals" aria-hidden="true">'
      + surfaceLabel('PAPINSVET', 600, 549, 'papinsvet-wordmark')
      + rasterSprite('wing', 313, 469, 88, 64) + rasterSprite('wild', 696, 678, 58) + '</g>' : '',
    ...SURFACE.map(renderSurfaceControl)
  ];
  svg.innerHTML = `<g transform="scale(.6)">${parts.join('')}</g>`;
  restoreSurfaceVisuals();
}
