const state = {
  ledgers: null,
  streetReels: null,
  chicagoStreets: [],      // 2,531 official Chicago streets for autocomplete
  crossStreets: null,      // precomputed street -> [[dir, lo, hi, fCross, tCross], ...]
  canvasesCache: new Map(),
  osd: null,
  activeLetter: 'ALL',
  savedCards: {},          // key "mid:slide" -> { mid, slide, reelNum, reelRange, status: 'yes'|'maybe'|'no', note, address, updatedAt }
  savedFilter: 'review',   // 'review' | 'all' | 'yes' | 'maybe' | 'no'
  activeCardKey: null,     // tracks which saved street card we are currently reviewing (even after jumping into a ledger!)
  viewer: {
    kind: null, // 'ledger' | 'street'
    mid: null,
    title: '',
    uicUrl: '',
    arkPermalink: '',
    slide: 1,
    total: 1,
    canvases: [],
    anchors: [],
    invert: false,
    grayscale: false,
    rotation: 0,
    sharpen: 0,
    gamma: 1,
    contrast: 105,
    brightness: 100,
    preset: 'normal',
  },
};

const qs = (sel, root = document) => root.querySelector(sel);
const qsa = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* -------------------- 1. Dark Mode Toggle -------------------- */
function initThemeToggle() {
  const meta = qs('meta[name="color-scheme"]');
  const btn = qs('#themeToggle');
  const label = qs('#themeLabel');

  const getSystem = () => (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const getStored = () => {
    try { return localStorage.getItem('color-scheme'); } catch { return null; }
  };
  const getEffective = () => getStored() || getSystem();

  function syncUi() {
    const stored = getStored();
    meta.content = stored ?? 'light dark';
    const eff = getEffective();
    label.textContent = eff === 'dark' ? 'Light' : 'Dark';
    btn.setAttribute('aria-label', eff === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
  }

  btn.addEventListener('click', () => {
    const target = getEffective() === 'dark' ? 'light' : 'dark';
    const sys = getSystem();
    try {
      if (target === sys) localStorage.removeItem('color-scheme');
      else localStorage.setItem('color-scheme', target);
    } catch {}
    meta.content = target === sys ? 'light dark' : target;
    syncUi();
  });

  window.addEventListener('storage', (e) => {
    if (e.key === 'color-scheme') syncUi();
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', syncUi);
  syncUi();
}

/* -------------------- 2. Header View Navigation (Home + 2nd-Tier Expert Views) -------------------- */
function initViewNav() {
  const views = ['jump', 'saved', 'streets', 'ledgers'];

  function showView(name) {
    if (!views.includes(name)) name = 'jump';
    views.forEach((v) => {
      const sec = qs(`#view-${v}`);
      if (sec) sec.hidden = v !== name;
      const nav = qs(`#nav-${v}`);
      if (nav) {
        if (v === name) nav.setAttribute('aria-current', 'page');
        else nav.removeAttribute('aria-current');
      }
    });
    if (name === 'ledgers') renderLedgersAndTimeline();
    if (name === 'saved') renderSavedCardsView();
  }

  document.addEventListener('click', (e) => {
    const trigger = e.target.closest('[data-view]');
    if (!trigger) return;
    if (trigger.tagName === 'A') e.preventDefault();
    showView(trigger.dataset.view);
  });

  showView('jump');
}

/* -------------------- 3. Helpers -------------------- */
function parseIsoMs(iso) {
  if (!iso || !/^\d{4}-\d{2}-\d{2}$/.test(iso)) return null;
  const t = Date.parse(`${iso}T00:00:00Z`);
  return Number.isFinite(t) ? t : null;
}

function formatMsIso(ms) {
  return new Date(ms).toISOString().slice(0, 10);
}

function shortBookTitle(title) {
  return (title || '').replace(/^Chicago Building Permits,\s*/i, '').trim();
}

function normalizeStreet(name) {
  let s = (name || '').toUpperCase().trim().replace(/[.,'"]/g, '');
  // Preserve Chicago's "AVENUE A..Z" (e.g. Avenue L, Avenue O)
  if (/^AVENUE\s+[A-Z]$/.test(s)) return s;
  // Strip trailing street type suffix only (keeps "NORTH AVE" -> "NORTH", "WEST END AVE" -> "WEST END")
  s = s.replace(/\s+(AVENUE|AVE|STREET|ST|BOULEVARD|BLVD|DRIVE|DR|PLACE|PL|ROAD|RD|COURT|CT|TERRACE|TER|PARKWAY|PKWY|LANE|LN)\b.*$/g, '');
  // Strip leading single-letter directional (N/S/E/W) only if followed by a street name
  s = s.replace(/^(N|S|E|W)\s+(?=\S)/, '');
  return s.replace(/\s+/g, ' ').trim();
}

/**
 * The filing order of the street index cards, as checked against every hand-read anchor (reels 97-99 run
 * Saginaw, St. Lawrence, St. Louis, Sangamon ... Seeley; reel 101 runs Southport, So. Shore):
 * named streets A-Z letter by letter (spaces ignored), "St." filed as "Saint"; then numbered streets.
 * Two streets file under another word: Martin L. King Drive under KING (reel 56, Kimbark ... King ... Kingston, with its old
 * name South Parkway) and 5th Avenue as FIFTH among the named streets (reel 34, Fifth ... Fillmore).
 */
const FILED_AS = [[/^MARTIN (L|LUTHER) KING( JR)?$/, 'KING'], [/^5TH$/, 'FIFTH']];
function streetSortKey(norm) {
  for (const [re, as] of FILED_AS) if (re.test(norm)) norm = as;
  const m = norm.match(/^(\d+)(?:ST|ND|RD|TH)?$/);
  if (m) return `1_${String(Number(m[1])).padStart(4, '0')}`;
  return `0_${norm.replace(/^ST\s+/, 'SAINT ').replace(/^SO\s+/, 'SOUTH ').replace(/\s+/g, '')}`;   // So. Shore = South Shore (after Southport)
}

/**
 * Convert a street sort key into a continuous numeric coordinate so we can
 * interpolate alphabetically across named streets or numerically across numbered streets.
 */
function streetCoord(sortKey) {
  if (sortKey.startsWith('1_')) {
    return 100 + Number(sortKey.slice(2)) / 200; // numbered streets 1..138 -> 100.005..100.69
  }
  const s = sortKey.slice(2).padEnd(5, ' ');
  let val = 0;
  let weight = 1;
  for (let i = 0; i < 5; i++) {
    const ch = s.charCodeAt(i);
    const code = ch >= 65 && ch <= 90 ? (ch - 64) : 0; // A=1..Z=26, space=0
    val += code * weight;
    weight /= 27;
  }
  return val; // roughly 0..27
}

/**
 * Compare (streetSortKey, houseNumber) pairs using UIC's verified filing order:
 * 1) Named streets A-Z, then numbered streets (Street & Place intermixed),
 * 2) Within a street, strictly by house number (N/S and E/W intermixed).
 */
function compareCardKey(stA, numA, stB, numB) {
  if (stA < stB) return -1;
  if (stA > stB) return 1;
  if (numA !== null && numB !== null && numA !== numB) return numA - numB;
  return 0;
}

function detectPossibleCorner(num) {
  if (num === null || !Number.isFinite(num)) return null;
  const mod = num % 100;
  const two = String(mod).padStart(2, '0');
  if (mod <= 3) return `ends in ${two} (start of block)`;
  if (mod >= 56 && mod <= 59) return `ends in ${two} (typical Chicago half-block corner)`;
  if (mod >= 96) return `ends in ${two} (end of block)`;
  return null;
}

function lookupCrossStreet(parsed) {
  if (!state.crossStreets || !parsed.street || parsed.num === null) return null;
  const blocks = state.crossStreets[parsed.street];
  if (!blocks || !blocks.length) return null;
  let match = blocks.find((b) => (!parsed.dir || b[0] === parsed.dir) && parsed.num >= b[1] && parsed.num <= b[2]);
  if (!match) match = blocks.find((b) => parsed.num >= b[1] && parsed.num <= b[2]);
  if (!match) return null;
  const [dir, lo, hi, fCross, tCross] = match;
  let cornerCross = null;
  let cornerSide = null;
  if (parsed.num <= lo + 3 && fCross) {
    cornerCross = fCross;
    cornerSide = 'low end of block';
  } else if (parsed.num >= hi - 3 && tCross) {
    cornerCross = tCross;
    cornerSide = 'high end of block';
  }
  return { dir, lo, hi, fCross, tCross, cornerCross, cornerSide };
}

function updateAddressAutocomplete() {
  const list = qs('#streetSuggestions');
  if (!list || !state.chicagoStreets.length) return;
  const raw = (qs('#jumpAddress').value || '').trim();
  const numMatch = raw.match(/^(\d+(?:-\d+)?\s+)(.*)$/);
  const numPrefix = numMatch ? numMatch[1] : '';
  const query = (numMatch ? numMatch[2] : raw).trim().toLowerCase();
  if (!query) {
    list.innerHTML = '';
    return;
  }
  const matches = [];
  for (const st of state.chicagoStreets) {
    const low = st.toLowerCase();
    if (low.startsWith(query) || low.includes(` ${query}`) || low.includes(query)) {
      matches.push(numPrefix + st);
      if (matches.length >= 12) break;
    }
  }
  list.innerHTML = matches.map((m) => `<option value="${esc(m)}"></option>`).join('');
}

function parseUserAddress(raw) {
  const cleaned = (raw || '').trim();
  let m = cleaned.match(/^(\d+)(?:-\d+)?\s+(?:(N|S|E|W|NORTH|SOUTH|EAST|WEST)\.?\s+)?(.+)$/i);
  if (m) {
    return { num: Number(m[1]), dir: (m[2] || '').toUpperCase().slice(0, 1), street: normalizeStreet(m[3]) };
  }
  m = cleaned.match(/^(.+?)\s+(\d+)(?:-\d+)?(?:\s+(N|S|E|W|NORTH|SOUTH|EAST|WEST)\.?)?$/i);
  if (m) {
    return { num: Number(m[2]), dir: (m[3] || '').toUpperCase().slice(0, 1), street: normalizeStreet(m[1]) };
  }
  return { num: null, dir: '', street: normalizeStreet(cleaned) };
}

function esc(str) {
  return String(str ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* -------------------- 4. Community Area & Historical Division Mapping -------------------- */
const COMMUNITY_AREAS = [
  // Far South / Beverly / Calumet
  { name: 'Beverly', divs: ['South', 'Southwest', 'Citywide'], annexed: 1890 },
  { name: 'Morgan Park', divs: ['South', 'Southwest', 'Citywide'], annexed: 1914 },
  { name: 'Washington Heights', divs: ['South', 'Southwest', 'Citywide'], annexed: 1890 },
  { name: 'Mount Greenwood', divs: ['South', 'Southwest', 'Citywide'], annexed: 1927 },
  { name: 'Auburn Gresham', divs: ['South', 'Southwest', 'Lake', 'Citywide'], annexed: 1889 },
  { name: 'Roseland', divs: ['South', 'Hyde Park', 'Citywide'], annexed: 1889 },
  { name: 'Pullman', divs: ['South', 'Hyde Park', 'Citywide'], annexed: 1889 },
  { name: 'West Pullman', divs: ['South', 'Citywide'], annexed: 1890 },
  { name: 'Riverdale', divs: ['South', 'Hyde Park', 'Citywide'], annexed: 1889 },
  { name: 'Calumet Heights', divs: ['South', 'Hyde Park', 'Citywide'], annexed: 1889 },
  { name: 'Avalon Park', divs: ['South', 'Hyde Park', 'Citywide'], annexed: 1889 },
  { name: 'Burnside', divs: ['South', 'Hyde Park', 'Citywide'], annexed: 1889 },
  { name: 'Chatham', divs: ['South', 'Hyde Park', 'Citywide'], annexed: 1889 },
  { name: 'Greater Grand Crossing', divs: ['South', 'Hyde Park', 'Lake', 'Citywide'], annexed: 1889 },
  { name: 'South Deering', divs: ['South', 'Hyde Park', 'Citywide'], annexed: 1889 },
  { name: 'East Side', divs: ['South', 'Hyde Park', 'Citywide'], annexed: 1889 },
  { name: 'Hegewisch', divs: ['South', 'Hyde Park', 'Citywide'], annexed: 1889 },
  // Hyde Park & South Lakefront
  { name: 'Hyde Park', divs: ['Hyde Park', 'South', 'Citywide'], annexed: 1889 },
  { name: 'Kenwood', divs: ['Hyde Park', 'South', 'Citywide'], annexed: 1889 },
  { name: 'Woodlawn', divs: ['Hyde Park', 'South', 'Citywide'], annexed: 1889 },
  { name: 'South Shore', divs: ['Hyde Park', 'South', 'Citywide'], annexed: 1889 },
  { name: 'South Chicago', divs: ['Hyde Park', 'South', 'Citywide'], annexed: 1889 },
  { name: 'Oakland', divs: ['South', 'Hyde Park', 'Citywide'] },
  { name: 'Grand Boulevard', divs: ['South', 'Hyde Park', 'Citywide'] },
  { name: 'Washington Park', divs: ['South', 'Hyde Park', 'Citywide'] },
  // Inner South & Town of Lake
  { name: 'Englewood', divs: ['South', 'Lake', 'Citywide'], annexed: 1889 },
  { name: 'West Englewood', divs: ['South', 'Southwest', 'Lake', 'Citywide'], annexed: 1889 },
  { name: 'New City', divs: ['South', 'Southwest', 'Lake', 'Citywide'], annexed: 1889 },
  { name: 'Fuller Park', divs: ['South', 'Lake', 'Citywide'], annexed: 1889 },
  { name: 'Bridgeport', divs: ['South', 'Southwest', 'Citywide'] },
  { name: 'Armour Square', divs: ['South', 'Citywide'] },
  { name: 'Douglas', divs: ['South', 'Citywide'] },
  { name: 'Near South Side', divs: ['South', 'Central', 'Citywide'] },
  // Southwest Side
  { name: 'Ashburn', divs: ['Southwest', 'South', 'Lake', 'Citywide'], annexed: 1889 },
  { name: 'Chicago Lawn', divs: ['Southwest', 'South', 'Lake', 'Citywide'], annexed: 1889 },
  { name: 'Gage Park', divs: ['Southwest', 'South', 'Lake', 'Citywide'], annexed: 1889 },
  { name: 'West Lawn', divs: ['Southwest', 'South', 'Citywide'], annexed: 1889 },
  { name: 'West Elsdon', divs: ['Southwest', 'South', 'Citywide'], annexed: 1889 },
  { name: 'Clearing', divs: ['Southwest', 'Citywide'], annexed: 1915 },
  { name: 'Garfield Ridge', divs: ['Southwest', 'Citywide'], annexed: 1915 },
  { name: 'Archer Heights', divs: ['Southwest', 'Lake', 'Citywide'], annexed: 1889 },
  { name: 'Brighton Park', divs: ['Southwest', 'South', 'Lake', 'Citywide'], annexed: 1889 },
  { name: 'McKinley Park', divs: ['Southwest', 'South', 'Citywide'] },
  { name: 'Lower West Side', divs: ['Southwest', 'West', 'Citywide'] },
  // Central / Loop
  { name: 'Loop', divs: ['Central', 'Citywide'] },
  // West Side
  { name: 'Near West Side', divs: ['West', 'Central', 'Citywide'] },
  { name: 'West Town', divs: ['West', 'Northwest', 'Citywide'] },
  { name: 'Humboldt Park', divs: ['West', 'Northwest', 'Citywide'] },
  { name: 'East Garfield Park', divs: ['West', 'Citywide'] },
  { name: 'West Garfield Park', divs: ['West', 'Citywide'] },
  { name: 'North Lawndale', divs: ['West', 'Southwest', 'Citywide'] },
  { name: 'South Lawndale', divs: ['West', 'Southwest', 'Citywide'] },
  { name: 'Austin', divs: ['West', 'Citywide'], annexed: 1899 },
  // Northwest Side
  { name: 'Logan Square', divs: ['Northwest', 'West', 'Citywide'], annexed: 1889 },
  { name: 'Avondale', divs: ['Northwest', 'Citywide'], annexed: 1889 },
  { name: 'Irving Park', divs: ['Northwest', 'Citywide'], annexed: 1889 },
  { name: 'Hermosa', divs: ['Northwest', 'West', 'Citywide'], annexed: 1889 },
  { name: 'Belmont Cragin', divs: ['Northwest', 'West', 'Citywide'], annexed: 1889 },
  { name: 'Montclare', divs: ['Northwest', 'West', 'Citywide'], annexed: 1889 },
  { name: 'Dunning', divs: ['Northwest', 'Citywide'], annexed: 1928 },
  { name: 'Portage Park', divs: ['Northwest', 'Citywide'], annexed: 1889 },
  { name: 'Jefferson Park', divs: ['Northwest', 'Citywide'], annexed: 1889 },
  { name: 'Forest Glen', divs: ['Northwest', 'Citywide'], annexed: 1889 },
  { name: 'North Park', divs: ['Northwest', 'North', 'Citywide'], annexed: 1889 },
  { name: 'Albany Park', divs: ['Northwest', 'North', 'Citywide'], annexed: 1889 },
  { name: 'Norwood Park', divs: ['Northwest', 'Citywide'], annexed: 1893 },
  { name: 'Edison Park', divs: ['Northwest', 'Citywide'], annexed: 1910 },
  { name: "O'Hare", divs: ['Northwest', 'Citywide'], annexed: 1956 },
  // North Side & Lake View
  { name: 'Near North Side', divs: ['North', 'Central', 'Citywide'] },
  { name: 'Lincoln Park', divs: ['North', 'Citywide'] },
  { name: 'Lake View', divs: ['Lake View', 'North', 'Citywide'], annexed: 1889 },
  { name: 'North Center', divs: ['Lake View', 'North', 'Citywide'], annexed: 1889 },
  { name: 'Lincoln Square', divs: ['North', 'Lake View', 'Citywide'], annexed: 1889 },
  { name: 'Uptown', divs: ['Lake View', 'North', 'Citywide'], annexed: 1889 },
  { name: 'Edgewater', divs: ['Lake View', 'North', 'Citywide'], annexed: 1889 },
  { name: 'Rogers Park', divs: ['North', 'Citywide'], annexed: 1893 },
  { name: 'West Ridge', divs: ['North', 'Citywide'], annexed: 1893 },
].sort((a, b) => a.name.localeCompare(b.name));

const COMMUNITY_BY_NAME = new Map(COMMUNITY_AREAS.map((c) => [c.name, c]));

const NEIGHBORHOOD_MATCHERS = {
  ALL: () => true,
  BEVERLY: (low) => low.includes('south') || low.includes('southwest') || low.includes('citywide') || low.includes('mixed'),
  HYDE_PARK: (low) => low.includes('hyde park') || low.includes('south') || low.includes('citywide') || low.includes('mixed'),
  SOUTH: (low) => low.includes('south') || low.includes('lake') || low.includes('hyde park') || low.includes('citywide') || low.includes('mixed'),
  SOUTHWEST: (low) => low.includes('southwest') || low.includes('west') || low.includes('south') || low.includes('citywide') || low.includes('mixed'),
  WEST: (low) => low.includes('west') || low.includes('citywide') || low.includes('mixed'),
  NORTHWEST: (low) => low.includes('northwest') || low.includes('west') || low.includes('north') || low.includes('citywide') || low.includes('mixed'),
  NORTH: (low) => low.includes('north') || low.includes('lake view') || low.includes('citywide') || low.includes('mixed'),
  CENTRAL: (low) => low.includes('central') || low.includes('citywide') || low.includes('mixed'),
};

function matchesDivisionToken(obsRegions, div) {
  const tokens = (obsRegions || '').toLowerCase().split(',').map((t) => t.trim()).filter(Boolean);
  const d = div.toLowerCase();
  if (d === 'citywide') {
    return tokens.some((t) => t === 'citywide' || t.includes('citywide')) || tokens[0] === 'mixed';
  }
  if (d === 'lake') return tokens.some((t) => t === 'lake' || t.includes('town of lake'));
  return tokens.some((t) => t === d);
}

function regionMatches(obsRegions, selected) {
  if (!selected || selected === 'ALL') return true;
  if (selected.startsWith('CA:')) {
    const ca = COMMUNITY_BY_NAME.get(selected.slice(3));
    if (!ca) return true;
    return ca.divs.some((d) => matchesDivisionToken(obsRegions, d));
  }
  if (selected.startsWith('DIV:')) {
    return matchesDivisionToken(obsRegions, selected.slice(4));
  }
  const low = (obsRegions || '').toLowerCase();
  if (NEIGHBORHOOD_MATCHERS[selected]) return NEIGHBORHOOD_MATCHERS[selected](low);
  if (low.includes('citywide') || low.includes('mixed')) return true;
  return low.includes(selected.toLowerCase());
}

function populateCommunityFilter() {
  const sel = qs('#ledgerCommunityFilter');
  if (!sel) return;
  const divOpts = [
    ['DIV:South', 'South Division only'],
    ['DIV:Southwest', 'Southwest Division only'],
    ['DIV:North', 'North Division only'],
    ['DIV:West', 'West Division only'],
    ['DIV:Northwest', 'Northwest Division only'],
    ['DIV:Central', 'Central / Loop only'],
    ['DIV:Hyde Park', 'Hyde Park (pre-1890s)'],
    ['DIV:Lake View', 'Lake View (pre-1890s)'],
    ['DIV:Lake', 'Town of Lake (pre-1890s)'],
    ['DIV:Citywide', 'Citywide / Mixed only'],
  ].map(([v, lbl]) => `<option value="${esc(v)}">${esc(lbl)}</option>`).join('');

  const caOpts = COMMUNITY_AREAS.map((c) => `<option value="CA:${esc(c.name)}">${esc(c.name)}</option>`).join('');
  sel.innerHTML = `
    <option value="ALL">All of Chicago (77 Community Areas)</option>
    <optgroup label="Chicago Community Areas (A–Z)">${caOpts}</optgroup>
    <optgroup label="Historical Permit Divisions">${divOpts}</optgroup>
  `;
}

/* -------------------- 5. Interpolation: Date -> Slide & Book+Page -> Slide -------------------- */
function interpolateLedgerSlide(seg, targetIso) {
  const targetMs = parseIsoMs(targetIso);
  const mid = String(seg.manifest_id);
  const allAnchors = (state.ledgers.anchors[mid] || []).filter(
    (a) => a.s >= seg.slide_start && a.s <= seg.slide_end && a.t === 'permits' && parseIsoMs(a.d0) !== null
  );
  if (!allAnchors.length || targetMs === null) {
    return { slide: seg.slide_start, leftAnchor: null, rightAnchor: null, estPage: seg.page_start };
  }

  const isDesc = seg.order_direction === 'desc';
  let lo = allAnchors[0];
  let hi = allAnchors[allAnchors.length - 1];
  for (let i = 0; i < allAnchors.length - 1; i++) {
    const a = allAnchors[i];
    const b = allAnchors[i + 1];
    const da = parseIsoMs(a.d0);
    const db = parseIsoMs(b.d0);
    const inBracket = isDesc ? (targetMs <= da && targetMs >= db) : (targetMs >= da && targetMs <= db);
    if (inBracket) { lo = a; hi = b; break; }
  }

  const dLo = parseIsoMs(lo.d0);
  const dHi = parseIsoMs(hi.d0);
  const frac = dHi !== dLo ? Math.max(0, Math.min(1, (targetMs - dLo) / (dHi - dLo))) : 0;
  const estSlide = Math.round(lo.s + frac * (hi.s - lo.s));
  const pLo = Number(lo.pl || lo.pr);
  const pHi = Number(hi.pr || hi.pl);
  const estPage = Number.isFinite(pLo) && Number.isFinite(pHi) && pLo > 0 && pHi > 0
    ? String(Math.round(pLo + frac * (pHi - pLo)))
    : (lo.pl || lo.pr || '');
  return { slide: estSlide, leftAnchor: lo, rightAnchor: hi, estPage };
}

function interpolatePageSlide(mid, targetPage) {
  const pageNum = Number(targetPage);
  const anchors = (state.ledgers.anchors[String(mid)] || [])
    .map((a) => {
      const pL = Number(a.pl);
      const pR = Number(a.pr);
      const pg = Number.isFinite(pL) && pL > 0 ? pL : (Number.isFinite(pR) && pR > 0 ? pR : null);
      return pg ? { ...a, pg, pgMax: Number.isFinite(pR) && pR > 0 ? pR : pg } : null;
    })
    .filter(Boolean)
    .sort((a, b) => a.s - b.s);

  if (!anchors.length || !Number.isFinite(pageNum)) {
    const roll = state.ledgers.rolls.find((r) => String(r.manifest_id) === String(mid));
    return { slide: roll?.first_spread_candidate || 2, note: 'Estimated from book start' };
  }

  // Exact hit on an anchor
  const exact = anchors.find((a) => pageNum >= a.pg && pageNum <= a.pgMax);
  if (exact) {
    return { slide: exact.s, note: `Exact match on slide p${exact.s} (dated ${exact.d0 || 'verified'})` };
  }

  // Find monotonic bracketing pair
  for (let i = 0; i < anchors.length - 1; i++) {
    const a = anchors[i];
    const b = anchors[i + 1];
    if (a.pg <= pageNum && pageNum <= b.pgMax && b.pg > a.pg) {
      const frac = (pageNum - a.pg) / (b.pg - a.pg);
      const est = Math.round(a.s + frac * (b.s - a.s));
      return { slide: est, note: `Between page ${a.pg} (slide p${a.s}) and page ${b.pg} (slide p${b.s})` };
    }
  }

  // Fallback nearest anchor + typical 2 pages/spread (or 1 folio/spread for wide pre-1912)
  let nearest = anchors[0];
  for (const a of anchors) {
    if (Math.abs(a.pg - pageNum) < Math.abs(nearest.pg - pageNum)) nearest = a;
  }
  const perSlide = nearest.pl && nearest.pr && nearest.pl === nearest.pr ? 1 : 2;
  const est = Math.max(1, Math.round(nearest.s + (pageNum - nearest.pg) / perSlide));
  return { slide: est, note: `Estimated from nearest verified page ${nearest.pg} (slide p${nearest.s})` };
}

/* -------------------- 6. Hero Search Modes -------------------- */
function populateBookSelects() {
  const sortedRolls = state.ledgers.rolls.slice().sort((a, b) => shortBookTitle(a.title).localeCompare(shortBookTitle(b.title), undefined, { numeric: true }));
  const opts = sortedRolls.map((r) => `<option value="${esc(r.manifest_id)}">${esc(shortBookTitle(r.title))}</option>`).join('');
  const sel = qs('#handoffBookSelect');
  if (sel) {
    sel.innerHTML = opts;
    if (sortedRolls.some((r) => String(r.manifest_id) === '825378')) sel.value = '825378';
  }
}

function matchReelsForAddress(parsed) {
  if (!parsed.street) return [];
  const targetKey = streetSortKey(parsed.street);
  const reels = state.streetReels.reels;
  const hits = [];

  for (const r of reels) {
    const sk = streetSortKey(r.s_st);
    const ek = streetSortKey(r.e_st);
    const cmpStart = compareCardKey(targetKey, parsed.num, sk, r.s_num);
    const cmpEnd = compareCardKey(targetKey, parsed.num, ek, r.e_num);
    if (cmpStart >= 0 && cmpEnd <= 0) hits.push(r);
  }
  if (!hits.length) {
    for (const r of reels) {
      if (r.s_raw.toUpperCase().includes(parsed.street) || r.e_raw.toUpperCase().includes(parsed.street)) hits.push(r);
    }
  }
  return hits.map((reel) => ({ reel, estSlide: estimateReelSlide(reel, parsed, targetKey) }));
}

function estimateReelSlide(reel, parsed, targetKey) {
  const sk = streetSortKey(reel.s_st);
  const ek = streetSortKey(reel.e_st);
  if (reel.anchors && reel.anchors.length) {
    const sorted = reel.anchors.slice().sort((a, b) => a.s - b.s);
    let lo = sorted[0], hi = sorted[sorted.length - 1];
    for (const a of sorted) {
      const ak = streetSortKey(a.st);
      const cmp = compareCardKey(ak, a.num ?? null, targetKey, parsed.num);
      if (cmp <= 0) lo = a;
      if (cmp >= 0) { hi = a; break; }
    }
    const loKey = streetSortKey(lo.st);
    const hiKey = streetSortKey(hi.st);
    let frac = 0.5;
    if (lo.s === hi.s) frac = 0;
    else if (loKey === hiKey && parsed.num !== null && lo.num != null && hi.num != null && hi.num !== lo.num) {
      frac = Math.max(0, Math.min(1, (parsed.num - lo.num) / (hi.num - lo.num)));
    } else {
      const cLo = streetCoord(loKey), cHi = streetCoord(hiKey), cT = streetCoord(targetKey);
      if (cHi !== cLo) frac = Math.max(0, Math.min(1, (cT - cLo) / (cHi - cLo)));
    }
    return Math.max(1, Math.min(reel.tot, Math.round(lo.s + frac * (hi.s - lo.s))));
  }
  if (sk === targetKey && ek === targetKey && parsed.num !== null && reel.s_num !== null && reel.e_num !== null && reel.e_num !== reel.s_num) {
    const f = Math.max(0, Math.min(1, (parsed.num - reel.s_num) / (reel.e_num - reel.s_num)));
    return Math.max(3, Math.round(3 + f * (reel.tot - 3)));
  }
  if (sk === targetKey) {
    const f = parsed.num !== null && reel.s_num !== null ? Math.min(0.18, Math.max(0.01, (parsed.num - reel.s_num) / 40000)) : 0.05;
    return Math.max(3, Math.round(f * reel.tot));
  }
  if (ek === targetKey) {
    const f = parsed.num !== null && reel.e_num !== null && reel.e_num > 0 ? Math.max(0.82, Math.min(0.99, 0.82 + 0.17 * (parsed.num / reel.e_num))) : 0.94;
    return Math.min(reel.tot, Math.round(f * reel.tot));
  }
  const cStart = streetCoord(sk), cEnd = streetCoord(ek), cTarget = streetCoord(targetKey);
  const f = cEnd !== cStart ? Math.max(0.02, Math.min(0.98, (cTarget - cStart) / (cEnd - cStart))) : 0.5;
  return Math.max(3, Math.min(reel.tot, Math.round(3 + f * (reel.tot - 3))));
}

function renderReelCardHtml(reel, estSlide, opts = {}) {
  const isCorner = Boolean(opts.cornerLabel);
  const badgeHtml = isCorner
    ? `<span class="badge corner-badge">Corner Cross Street</span> <span class="badge">Slide ~${estSlide.toLocaleString()} of ${reel.tot.toLocaleString()}</span>`
    : `<span class="badge">Slide ~${estSlide.toLocaleString()} of ${reel.tot.toLocaleString()}</span>`;
  const heading = isCorner
    ? `${esc(opts.cornerLabel)} — Street Index Reel ${reel.n}`
    : `Street Index Reel ${reel.n}`;
  const noteHtml = opts.note
    ? `<div class="corner-tip"><span>${opts.note}</span></div>`
    : '';
  return `
    <div class="result-item${isCorner ? ' corner-card' : ''}">
      <div class="result-main">
        <div class="result-left">
          <button type="button" class="result-thumb-btn open-viewer-btn"
            data-kind="street" data-mid="${esc(reel.mid)}" data-slide="${estSlide}"
            data-thumb-mid="${esc(reel.mid)}" data-thumb-slide="${estSlide}"
            aria-label="Preview slide ${estSlide} of Reel ${reel.n}">
            <span class="thumb-placeholder">Loading card…</span>
          </button>
          <div>
            <div class="result-title">${heading} ${badgeHtml}</div>
            <div class="result-sub">Covers ${esc(reel.s_raw)} → ${esc(reel.e_raw)}</div>
          </div>
        </div>
        <button type="button" class="btn btn-open open-viewer-btn"
          data-kind="street" data-mid="${esc(reel.mid)}" data-slide="${estSlide}">
          Open Index Cards ↗
        </button>
      </div>
      ${noteHtml}
    </div>
  `;
}

async function hydrateResultThumbnails(container) {
  const slots = qsa('[data-thumb-mid]', container);
  await Promise.all(slots.map(async (slot) => {
    const mid = slot.dataset.thumbMid;
    const slideNum = Number(slot.dataset.thumbSlide || 1);
    try {
      const canvases = await loadCanvases('street', mid);
      const canvas = canvases.find((c) => c.s === slideNum) || canvases[Math.max(0, Math.min(canvases.length - 1, slideNum - 1))];
      if (!canvas) return;
      const thumbW = Math.min(320, canvas.w || 320);
      const thumbUrl = `https://digital.library.uic.edu/iiif/3/${canvas.id}/pct:0,0,100,99/${thumbW},/0/default.jpg`;
      const img = document.createElement('img');
      img.className = 'result-thumb';
      img.alt = `Microfilm preview of slide ${slideNum}`;
      img.loading = 'lazy';
      img.src = thumbUrl;
      img.onerror = () => {
        slot.innerHTML = '<span class="thumb-placeholder">Card preview</span>';
      };
      slot.innerHTML = '';
      slot.appendChild(img);
    } catch {
      slot.innerHTML = '<span class="thumb-placeholder">Card preview</span>';
    }
  }));
}

function runAddressJump() {
  const raw = (qs('#jumpAddress').value || '').trim();
  const container = qs('#heroResults');
  if (!raw) {
    container.innerHTML = '';
    return;
  }
  const parsed = parseUserAddress(raw);
  if (!parsed.street) {
    container.innerHTML = '<p class="subtitle">Type a Chicago street name (and optional house number).</p>';
    return;
  }

  // Remember searched street direction so the viewer's "By Permit Date" ledger jump auto-selects the likely side!
  const dirToSide = { S: 'South', N: 'North', W: 'West', E: 'South' };
  const sideSel = qs('#handoffSide');
  if (sideSel && parsed.dir && dirToSide[parsed.dir]) {
    sideSel.value = dirToSide[parsed.dir];
  }

  const primaryMatches = matchReelsForAddress(parsed);
  if (!primaryMatches.length) {
    container.innerHTML = `<p class="subtitle">No street-index reel matched <strong>${esc(raw)}</strong>. Try just the street name (e.g. <code>Kilpatrick</code> or <code>Western</code>).</p>`;
    return;
  }

  const crossInfo = lookupCrossStreet(parsed);
  const cornerReason = detectPossibleCorner(parsed.num);
  const cards = [];

  for (const { reel, estSlide } of primaryMatches) {
    const fallbackNote = !crossInfo && cornerReason
      ? `<strong>Corner lot note:</strong> House number ${parsed.num} ${esc(cornerReason)}. Chicago corner lots were often filed under the cross street — check the intersecting street too.`
      : '';
    cards.push(renderReelCardHtml(reel, estSlide, { note: fallbackNote }));
  }

  // If this is a corner lot with a known cross street, automatically show the cross-street reel too!
  if (crossInfo?.cornerCross) {
    const crossParsed = parseUserAddress(crossInfo.cornerCross);
    const crossMatches = matchReelsForAddress(crossParsed);
    for (const { reel, estSlide } of crossMatches) {
      cards.push(renderReelCardHtml(reel, estSlide, {
        cornerLabel: crossInfo.cornerCross,
        note: `<strong>Corner lot:</strong> <strong>${esc(raw)}</strong> sits at the corner of <strong>${esc(crossInfo.cornerCross)}</strong> (${esc(crossInfo.cornerSide)}). Permits for corner houses were often filed under either street, so you may need to check both reels.`,
      }));
    }
  }

  container.innerHTML = cards.join('');
  hydrateResultThumbnails(container);
}

function runLedgerJump() {
  const targetDate = qs('#jumpDate').value;
  const targetRegion = qs('#jumpRegion').value;
  const container = qs('#heroResults');
  const targetMs = parseIsoMs(targetDate);
  if (!targetMs) {
    container.innerHTML = '<p class="subtitle">Pick a date between 1872-02-21 and 1954-06-30.</p>';
    return;
  }

  const pad = 14 * 86400 * 1000;
  const matches = state.ledgers.segments.filter((seg) => {
    const d0 = parseIsoMs(seg.date_min);
    const d1 = parseIsoMs(seg.date_max);
    return d0 && d1 && targetMs >= d0 - pad && targetMs <= d1 + pad && regionMatches(seg.observed_regions, targetRegion);
  });

  if (!matches.length) {
    container.innerHTML = `<p class="subtitle">No ledger books matched <strong>${esc(targetDate)}</strong> for that neighborhood. Try "Anywhere in Chicago".</p>`;
    return;
  }

  matches.sort((a, b) => {
    const aEx = targetMs >= parseIsoMs(a.date_min) && targetMs <= parseIsoMs(a.date_max) ? 0 : 1;
    const bEx = targetMs >= parseIsoMs(b.date_min) && targetMs <= parseIsoMs(b.date_max) ? 0 : 1;
    if (aEx !== bEx) return aEx - bEx;
    return (b.slide_end - b.slide_start) - (a.slide_end - a.slide_start);
  });

  container.innerHTML = matches.map((seg) => {
    const interp = interpolateLedgerSlide(seg, targetDate);
    const bracket = interp.leftAnchor && interp.rightAnchor
      ? `Bracketed by slide p${interp.leftAnchor.s} (${interp.leftAnchor.d0}) → p${interp.rightAnchor.s} (${interp.rightAnchor.d0})`
      : `Slides p${seg.slide_start}–p${seg.slide_end}`;
    return `
      <div class="result-item">
        <div class="result-main">
          <div>
            <div class="result-title">${esc(shortBookTitle(seg.title))} <span class="badge">Slide ~p${interp.slide}${interp.estPage ? ` · Page ~${esc(interp.estPage)}` : ''}</span></div>
            <div class="result-sub">Covers ${esc(seg.observed_regions || 'Chicago')} · ${esc(seg.date_min)} to ${esc(seg.date_max)}</div>
          </div>
          <button type="button" class="btn btn-primary open-viewer-btn"
            data-kind="ledger" data-mid="${esc(seg.manifest_id)}" data-slide="${interp.slide}">
            Open Ledger
          </button>
        </div>
        <details class="result-details">
          <summary>Details &amp; UIC link</summary>
          <div class="result-details-body">
            <span>${esc(bracket)}</span>
            <a href="${esc(seg.uic_viewer_url)}" target="_blank" rel="noopener">Open on UIC ↗</a>
          </div>
        </details>
      </div>
    `;
  }).join('');
}

/* -------------------- 7. Full-Height Visual Timeline -------------------- */
const LANES = [
  { key: 'Citywide', label: 'Citywide', sub: '1872–88 & 1940–54' },
  { key: 'North', label: 'North Side', sub: 'Lincoln Park – Rogers Pk' },
  { key: 'South', label: 'South Side', sub: 'Bridgeport – Far South' },
  { key: 'West', label: 'West Side', sub: 'West Loop – Austin' },
  { key: 'Northwest', label: 'Northwest', sub: 'Logan Sq – Jefferson Pk' },
  { key: 'Southwest', label: 'Southwest', sub: 'Pilsen – Marquette Pk' },
  { key: 'Central', label: 'Downtown', sub: 'Loop / Central' },
  { key: 'Annexed', label: 'Annexed Towns', sub: 'Hyde Pk · Lake · Lake View' },
];

function laneForSegment(seg, preferredDivs = null) {
  const tokens = (seg.observed_regions || '').toLowerCase().split(',').map((t) => t.trim()).filter(Boolean);
  if (preferredDivs && preferredDivs.length) {
    for (const d of preferredDivs) {
      if (matchesDivisionToken(seg.observed_regions, d)) {
        if (d === 'Hyde Park' || d === 'Lake View' || d === 'Lake') return 'Annexed';
        return d;
      }
    }
  }
  if (tokens.some((t) => t === 'hyde park' || t === 'lake view' || t === 'lake')) return 'Annexed';
  if (tokens.includes('central')) return 'Central';
  if (tokens.includes('northwest')) return 'Northwest';
  if (tokens.includes('southwest')) return 'Southwest';
  if (tokens.includes('north')) return 'North';
  if (tokens.includes('south')) return 'South';
  if (tokens.includes('west')) return 'West';
  return 'Citywide';
}

function getFilteredLedgerSegments() {
  const q = (qs('#ledgerSearch').value || '').trim().toLowerCase();
  const reg = qs('#ledgerCommunityFilter').value;
  const multiOnly = qs('#ledgerMultiOnly').checked;
  let yFrom = Math.max(1872, Math.min(1954, Number(qs('#ledgerYearFrom').value) || 1872));
  let yTo = Math.max(1872, Math.min(1954, Number(qs('#ledgerYearTo').value) || 1954));
  if (yFrom > yTo) [yFrom, yTo] = [yTo, yFrom];

  const rangeStartMs = Date.UTC(yFrom, 0, 1);
  const rangeEndMs = Date.UTC(yTo, 11, 31, 23, 59, 59);

  const filtered = state.ledgers.segments.filter((s) => {
    if (s.order_direction === 'single' && s.segment_count_in_roll > 1) return false;
    if (multiOnly && s.segment_count_in_roll <= 1) return false;
    if (!regionMatches(s.observed_regions, reg)) return false;

    const d0 = parseIsoMs(s.date_min);
    const d1 = parseIsoMs(s.date_max);
    if (d0 && d1 && (d1 < rangeStartMs || d0 > rangeEndMs)) return false;

    if (q) {
      const anchorNotes = (state.ledgers.anchors[s.manifest_id] || [])
        .filter((a) => a.s >= s.slide_start && a.s <= s.slide_end)
        .map((a) => a.rn).join(' ').toLowerCase();
      const hay = `${s.manifest_id} ${s.title} ${s.date_min} ${s.date_max} ${s.observed_regions} ${anchorNotes}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  return { filtered, yFrom, yTo, reg };
}

function renderTimeline(filtered, yFrom, yTo, reg) {
  const canvas = qs('#timelineCanvas');
  const readout = qs('#timelineReadout');
  const zoom = Number(qs('#timelineZoom').value || 1.5);

  const minYear = yFrom;
  const maxYear = Math.max(yFrom + 1, yTo + 1);
  const spanYears = maxYear - minYear;
  const minMs = Date.UTC(minYear, 0, 1);
  const maxMs = Date.UTC(maxYear, 0, 1);
  // Scale cleanly so narrow decade ranges fill the viewport nicely while full 83-year view stays scrollable
  const basePxPerYear = spanYears <= 15 ? 72 : (spanYears <= 35 ? 36 : 16);
  const pxPerYear = basePxPerYear * zoom;
  const trackWidth = Math.max(680, Math.round(spanYears * pxPerYear));
  const labelW = 148;
  const xForMs = (ms) => Math.max(0, Math.min(trackWidth, ((ms - minMs) / (maxMs - minMs)) * trackWidth));

  const preferredDivs = reg.startsWith('CA:')
    ? (COMMUNITY_BY_NAME.get(reg.slice(3))?.divs || null)
    : (reg.startsWith('DIV:') ? [reg.slice(4)] : null);

  const byLane = new Map(LANES.map((l) => [l.key, []]));
  for (const seg of filtered) {
    const d0 = parseIsoMs(seg.date_min);
    const d1 = parseIsoMs(seg.date_max);
    if (!d0 || !d1) continue;
    const x0 = xForMs(Math.max(minMs, d0));
    const x1 = Math.max(x0 + 22, xForMs(Math.min(maxMs, d1)));
    byLane.get(laneForSegment(seg, preferredDivs)).push({ seg, x0, x1 });
  }

  const milestones = [
    { y: 1889, label: '1889 Annexation' },
    { y: 1909, label: '1909 Renumbering' },
    { y: 1912, label: '1912 Forms' },
    { y: 1929, label: '1929 Crash' },
    { y: 1940, label: '1940 Citywide' },
  ].filter((m) => m.y >= minYear && m.y < maxYear);

  let html = '';
  if (milestones.length) {
    html += `<div class="tl-milestones-bar" style="width:${trackWidth + labelW}px">`;
    for (const m of milestones) {
      html += `<span class="tl-milestone-pill" style="left:${labelW + xForMs(Date.UTC(m.y, 0, 1))}px">${m.label}</span>`;
    }
    html += `</div>`;
  }

  // Year axis
  const tickStep = spanYears <= 12 ? 1 : (spanYears <= 30 || zoom >= 2 ? 2 : 5);
  html += `<div class="tl-axis" style="width:${trackWidth + labelW}px"><div style="margin-inline-start:${labelW}px;position:relative;height:100%">`;
  for (let y = minYear; y <= yTo; y += tickStep) {
    html += `<div class="tl-year-tick" style="left:${xForMs(Date.UTC(y, 0, 1))}px">${y}</div>`;
  }
  html += `</div></div>`;

  for (const m of milestones) {
    html += `<div class="tl-milestone-line" style="left:${labelW + xForMs(Date.UTC(m.y, 0, 1))}px"></div>`;
  }

  const activeLanes = reg === 'ALL'
    ? LANES
    : LANES.filter((l) => byLane.get(l.key).length > 0);

  if (!activeLanes.length) {
    canvas.innerHTML = '<div style="padding:1.25rem;color:var(--muted);font-size:0.88rem">No permit ledger books match this community area and date range. Try widening the years.</div>';
    return;
  }

  for (const lane of activeLanes) {
    const items = byLane.get(lane.key).sort((a, b) => a.x0 - b.x0);
    const rowEnds = [];
    const placed = items.map((it) => {
      let row = rowEnds.findIndex((endX) => endX + 4 <= it.x0);
      if (row === -1) { row = rowEnds.length; rowEnds.push(it.x1); }
      else { rowEnds[row] = it.x1; }
      return { ...it, row };
    });
    const laneH = Math.max(46, rowEnds.length * 28 + 14);
    html += `<div class="tl-lane" style="width:${trackWidth + labelW}px;height:${laneH}px">
      <div class="tl-lane-label">${esc(lane.label)}<small>${esc(lane.sub)}</small></div>
      <div class="tl-lane-track">`;
    for (const p of placed) {
      const s = p.seg;
      const eraCls = s.era === 'pre-1912' ? 'era-pre' : (s.era === 'post-1912' ? 'era-post' : 'era-trans');
      const multiCls = s.segment_count_in_roll > 1 ? 'multi-seg' : '';
      const shortLbl = shortBookTitle(s.title).split(':')[0];
      html += `<button type="button" class="tl-bar ${eraCls} ${multiCls}"
        style="left:${p.x0}px;width:${Math.max(22, p.x1 - p.x0)}px;top:${7 + p.row * 28}px"
        data-mid="${esc(s.manifest_id)}" data-seg="${s.segment_index}">${esc(shortLbl)}</button>`;
    }
    html += `</div></div>`;
  }

  canvas.innerHTML = html;

  const segLookup = new Map(state.ledgers.segments.map((s) => [`${s.manifest_id}:${s.segment_index}`, s]));
  const tooltip = qs('#timelineTooltip');
  qsa('.tl-bar', canvas).forEach((bar) => {
    const seg = segLookup.get(`${bar.dataset.mid}:${bar.dataset.seg}`);
    const visStartMs = Math.max(minMs, parseIsoMs(seg.date_min));
    const visEndMs = Math.min(maxMs, parseIsoMs(seg.date_max));
    bar.addEventListener('mousemove', (e) => {
      const rect = bar.getBoundingClientRect();
      const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / Math.max(1, rect.width)));
      const hoverIso = formatMsIso(visStartMs + frac * (visEndMs - visStartMs));
      const bookTitle = shortBookTitle(seg.title);
      readout.innerHTML = `<strong>${esc(bookTitle)}</strong> · Area: ${esc(seg.observed_regions || 'Chicago')} · Click to open near <strong>${hoverIso}</strong>`;
      if (tooltip) {
        tooltip.innerHTML = `<strong>${esc(bookTitle)}</strong><br>${esc(seg.observed_regions || 'Chicago')} · <strong>${hoverIso}</strong> (click to open)`;
        tooltip.hidden = false;
        const tw = tooltip.offsetWidth || 240;
        const th = tooltip.offsetHeight || 48;
        const tx = Math.max(8, Math.min(window.innerWidth - tw - 8, e.clientX + 12));
        const ty = Math.max(8, e.clientY - th - 12);
        tooltip.style.left = `${tx}px`;
        tooltip.style.top = `${ty}px`;
      }
    });
    bar.addEventListener('mouseleave', () => {
      if (tooltip) tooltip.hidden = true;
    });
    bar.addEventListener('click', (e) => {
      if (tooltip) tooltip.hidden = true;
      const rect = bar.getBoundingClientRect();
      const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / Math.max(1, rect.width)));
      const clickIso = formatMsIso(visStartMs + frac * (visEndMs - visStartMs));
      const interp = interpolateLedgerSlide(seg, clickIso);
      openViewer('ledger', seg.manifest_id, interp.slide);
    });
  });
}

function renderLedgersAndTimeline() {
  const { filtered, yFrom, yTo, reg } = getFilteredLedgerSegments();

  // Community area coverage note
  const noteEl = qs('#communityCoverageNote');
  if (reg.startsWith('CA:')) {
    const ca = COMMUNITY_BY_NAME.get(reg.slice(3));
    if (ca) {
      const annex = ca.annexed ? ` (annexed ${ca.annexed})` : '';
      noteEl.innerHTML = `Showing <strong>${esc(ca.name)}</strong>${annex} (${yFrom}–${yTo}) — possible ledger divisions: <strong>${esc(ca.divs.join(', '))}</strong>.`;
      noteEl.hidden = false;
    } else {
      noteEl.hidden = true;
    }
  } else {
    noteEl.hidden = true;
  }

  // Render timeline + books table together
  renderTimeline(filtered, yFrom, yTo, reg);

  qs('#ledgerCount').textContent = `${filtered.length} book sections`;
  qs('#ledgersTbody').innerHTML = filtered.map((s) => `
    <tr>
      <th scope="row">${esc(shortBookTitle(s.title))}</th>
      <td class="mono">${esc(s.date_min)} → ${esc(s.date_max)}</td>
      <td>${esc(s.observed_regions || 'Chicago')}</td>
      <td class="mono">${esc(s.page_start || '1')}–${esc(s.page_end || '?')}</td>
      <td>
        <button type="button" class="btn btn-sm btn-open open-viewer-btn"
          data-kind="ledger" data-mid="${esc(s.manifest_id)}" data-slide="${s.slide_start}">
          Open Book ↗
        </button>
      </td>
    </tr>
  `).join('');
}

/* -------------------- 9. Street Index A-Z Table -------------------- */
function initLetterBar() {
  const letters = ['ALL', ...'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split(''), '0–9'];
  const bar = qs('#letterBar');
  bar.innerHTML = letters.map((l) => `<button type="button" class="letter-btn" data-letter="${l}" aria-pressed="${l === 'ALL'}">${l}</button>`).join('');
  bar.addEventListener('click', (e) => {
    const btn = e.target.closest('.letter-btn');
    if (!btn) return;
    state.activeLetter = btn.dataset.letter;
    qsa('.letter-btn', bar).forEach((b) => b.setAttribute('aria-pressed', String(b === btn)));
    renderStreetReelsTable();
  });
}

function renderStreetReelsTable() {
  const q = (qs('#streetReelSearch').value || '').trim().toLowerCase();
  const L = state.activeLetter;

  const filtered = state.streetReels.reels.filter((r) => {
    if (L !== 'ALL') {
      const sIsNum = /^\d/.test(r.s_st);
      const eIsNum = /^\d/.test(r.e_st);
      if (L === '0–9') {
        if (!sIsNum && !eIsNum) return false;
      } else {
        const startC = sIsNum ? 'ZZ' : (r.s_st || '')[0];
        const endC = eIsNum ? 'ZZ' : (r.e_st || '')[0];
        if (!(startC <= L && L <= endC)) return false;
      }
    }
    if (q) {
      if (!`reel ${r.n} ${r.s_raw} ${r.e_raw}`.toLowerCase().includes(q)) return false;
    }
    return true;
  });

  qs('#streetReelCount').textContent = `${filtered.length} of ${state.streetReels.reels.length} reels`;
  qs('#streetsTbody').innerHTML = filtered.map((r) => `
    <tr>
      <th scope="row" class="mono">Reel ${r.n}</th>
      <td>${esc(r.s_raw)}</td>
      <td>${esc(r.e_raw)}</td>
      <td class="mono">${r.tot.toLocaleString()}</td>
      <td>
        <button type="button" class="btn btn-sm btn-open open-viewer-btn"
          data-kind="street" data-mid="${esc(r.mid)}" data-slide="3">
          Open Cards ↗
        </button>
      </td>
    </tr>
  `).join('');
}

/* -------------------- 9B. Saved Index Cards (Yes / Maybe / No + Notes + Review Queue) -------------------- */
const SAVED_STORAGE_KEY = 'uic_saved_cards_v1';

function loadSavedCards() {
  try {
    const raw = localStorage.getItem(SAVED_STORAGE_KEY);
    state.savedCards = raw ? JSON.parse(raw) : {};
  } catch {
    state.savedCards = {};
  }
  updateSavedBadge();
}

function persistSavedCards() {
  try {
    localStorage.setItem(SAVED_STORAGE_KEY, JSON.stringify(state.savedCards));
  } catch {}
  updateSavedBadge();
}

function cardKey(mid, slide) {
  return `${mid}:${slide}`;
}

function getReviewQueue() {
  const rank = { yes: 0, maybe: 1 };
  return Object.values(state.savedCards)
    .filter((c) => c.status === 'yes' || c.status === 'maybe')
    .sort((a, b) => (rank[a.status] - rank[b.status]) || ((a.updatedAt || 0) - (b.updatedAt || 0)));
}

function updateSavedBadge() {
  const navBtn = qs('#nav-saved');
  const badge = qs('#savedCountBadge');
  const reviewCount = getReviewQueue().length;
  const totalCount = Object.keys(state.savedCards).length;
  if (navBtn) navBtn.hidden = totalCount === 0;
  if (badge) {
    badge.textContent = String(reviewCount || totalCount);
    badge.hidden = totalCount === 0;
  }
  if (totalCount === 0 && qs('#view-saved') && !qs('#view-saved').hidden) {
    qs('#nav-jump')?.click();
  }
}

function upsertSavedCard(mid, slide, patch) {
  const key = cardKey(mid, slide);
  const existing = state.savedCards[key];
  const reel = state.streetReels?.reels?.find((r) => String(r.mid) === String(mid));
  const addr = (qs('#jumpAddress')?.value || '').trim();
  const next = {
    mid: String(mid),
    slide: Number(slide),
    reelNum: reel?.n ?? existing?.reelNum ?? '?',
    reelRange: reel ? `${reel.s_raw} → ${reel.e_raw}` : (existing?.reelRange || ''),
    status: existing?.status || 'maybe',
    note: existing?.note || '',
    address: existing?.address || addr,
    updatedAt: existing?.updatedAt || Date.now(),
    ...patch,
  };
  state.savedCards[key] = next;
  persistSavedCards();
  return next;
}

function toggleCardTag(mid, slide, status) {
  const key = cardKey(mid, slide);
  const existing = state.savedCards[key];
  if (existing && existing.status === status) {
    delete state.savedCards[key];
    if (state.activeCardKey === key) state.activeCardKey = null;
    persistSavedCards();
  } else {
    upsertSavedCard(mid, slide, { status });
    if (status === 'yes' || status === 'maybe') state.activeCardKey = key;
  }
  syncViewerSavedUi();
  if (!qs('#view-saved').hidden) renderSavedCardsView();
}

function updateCardNote(mid, slide, note) {
  const key = cardKey(mid, slide);
  const existing = state.savedCards[key];
  if (!existing && !note.trim()) return;
  upsertSavedCard(mid, slide, { note, status: existing?.status || 'maybe' });
  if (!existing) state.activeCardKey = key;
  syncViewerSavedUi();
  if (!qs('#view-saved').hidden) renderSavedCardsView();
}

function syncViewerSavedUi() {
  const v = state.viewer;
  const tagBar = qs('#cardTagBar');
  const queueBar = qs('#reviewQueueBar');
  if (!tagBar || !queueBar) return;

  // 1. Tag & Note bar (only when viewing a Street Index card)
  const isStreet = v.kind === 'street';
  tagBar.hidden = !isStreet;
  if (isStreet) {
    const key = cardKey(v.mid, v.slide);
    const saved = state.savedCards[key];
    if (saved && (saved.status === 'yes' || saved.status === 'maybe')) {
      state.activeCardKey = key;
    }
    qsa('.tag-btn', tagBar).forEach((b) => {
      b.setAttribute('aria-pressed', String(saved?.status === b.dataset.tag));
    });
    const noteEl = qs('#cardNoteInput');
    if (document.activeElement !== noteEl) {
      noteEl.value = saved?.note || '';
    }
  }

  // 2. Review Queue bar (visible whenever there is >= 1 Yes/Maybe saved card, in BOTH Street & Ledger views!)
  const queue = getReviewQueue();
  if (!queue.length) {
    queueBar.hidden = true;
    return;
  }
  queueBar.hidden = false;

  let idx = queue.findIndex((c) => cardKey(c.mid, c.slide) === state.activeCardKey);
  if (idx === -1 && isStreet) {
    idx = queue.findIndex((c) => String(c.mid) === String(v.mid) && c.slide === v.slide);
  }
  const activeCard = idx >= 0 ? queue[idx] : queue[0];
  const displayIdx = idx >= 0 ? idx + 1 : 0;
  const statusLabel = activeCard.status === 'yes' ? 'Yes' : 'Maybe';

  qs('#reviewQueueStatus').textContent = displayIdx
    ? `Review Queue (${displayIdx}/${queue.length} · ${statusLabel} · Reel ${activeCard.reelNum} p${activeCard.slide})`
    : `Review Queue (${queue.length} Yes/Maybe saved)`;

  const notePreview = qs('#reviewQueueNotePreview');
  notePreview.textContent = activeCard.note
    ? `Note: ${activeCard.note}`
    : (activeCard.address ? `Address: ${activeCard.address}` : 'Step through your Yes & Maybe cards to look each up in the ledgers.');
}

function stepReviewQueue(delta) {
  const queue = getReviewQueue();
  if (!queue.length) return;
  let idx = queue.findIndex((c) => cardKey(c.mid, c.slide) === state.activeCardKey);
  if (idx === -1) idx = delta > 0 ? -1 : 0;
  const nextIdx = (idx + delta + queue.length) % queue.length;
  const target = queue[nextIdx];
  state.activeCardKey = cardKey(target.mid, target.slide);
  openViewer('street', target.mid, target.slide);
}

function renderSavedCardsView() {
  const list = qs('#savedCardsList');
  if (!list) return;
  const all = Object.values(state.savedCards).sort((a, b) => (a.updatedAt || 0) - (b.updatedAt || 0));
  const reviewQueue = getReviewQueue();

  qs('#btnStartReview').hidden = reviewQueue.length === 0;
  qs('#btnStartReview').textContent = `Review Yes & Maybe Cards (${reviewQueue.length}) ↗`;
  qs('#btnClearSaved').hidden = all.length === 0;

  const f = state.savedFilter;
  const filtered = all.filter((c) => {
    if (f === 'review') return c.status === 'yes' || c.status === 'maybe';
    if (f === 'all') return true;
    return c.status === f;
  });

  if (!filtered.length) {
    list.innerHTML = all.length === 0
      ? '<p class="subtitle">No saved index cards yet. Open any Street Index reel and click <strong>✓ Yes</strong>, <strong>? Maybe</strong>, or <strong>✕ No</strong> to collect cards for ledger lookup.</p>'
      : '<p class="subtitle">No saved cards match this filter.</p>';
    return;
  }

  list.innerHTML = filtered.map((c) => {
    const key = cardKey(c.mid, c.slide);
    return `
      <div class="result-item" data-saved-key="${esc(key)}">
        <div class="result-main">
          <div class="result-left">
            <button type="button" class="result-thumb-btn open-viewer-btn"
              data-kind="street" data-mid="${esc(c.mid)}" data-slide="${c.slide}"
              data-thumb-mid="${esc(c.mid)}" data-thumb-slide="${c.slide}"
              aria-label="Open saved card Reel ${c.reelNum} slide ${c.slide}">
              <span class="thumb-placeholder">Loading card…</span>
            </button>
            <div>
              <div class="result-title">Reel ${esc(c.reelNum)}, Slide ${c.slide.toLocaleString()} ${c.address ? `<span class="badge">${esc(c.address)}</span>` : ''}</div>
              <div class="result-sub">${esc(c.reelRange)}</div>
            </div>
          </div>
          <button type="button" class="btn btn-open open-viewer-btn"
            data-kind="street" data-mid="${esc(c.mid)}" data-slide="${c.slide}">
            Open Card ↗
          </button>
        </div>
        <div class="saved-card-controls">
          <div class="tag-btn-group" role="group" aria-label="Change status">
            <button type="button" class="pill saved-tag-btn tag-yes" data-mid="${esc(c.mid)}" data-slide="${c.slide}" data-tag="yes" aria-pressed="${c.status === 'yes'}">✓ Yes</button>
            <button type="button" class="pill saved-tag-btn tag-maybe" data-mid="${esc(c.mid)}" data-slide="${c.slide}" data-tag="maybe" aria-pressed="${c.status === 'maybe'}">? Maybe</button>
            <button type="button" class="pill saved-tag-btn tag-no" data-mid="${esc(c.mid)}" data-slide="${c.slide}" data-tag="no" aria-pressed="${c.status === 'no'}">✕ No</button>
          </div>
          <input type="text" class="saved-note-edit" data-mid="${esc(c.mid)}" data-slide="${c.slide}" value="${esc(c.note || '')}" placeholder="Add note (e.g. permit date, book & page)…">
        </div>
      </div>
    `;
  }).join('');
  hydrateResultThumbnails(list);
}

/* -------------------- 10. Built-In IIIF Microfilm Viewer -------------------- */
async function loadCanvases(kind, mid) {
  const key = `${kind}:${mid}`;
  if (state.canvasesCache.has(key)) return state.canvasesCache.get(key);
  const path = kind === 'ledger' ? `../index/canvases_${mid}.json` : `../street_index/index/canvases_${mid}.json`;
  const resp = await fetch(path);
  if (!resp.ok) throw new Error(`Could not load reel index (${resp.status})`);
  const raw = await resp.json();
  const normalized = kind === 'ledger'
    ? raw.map((c) => ({ s: c.slide_index, id: c.iiif_image_id, w: c.width, h: c.height }))
    : raw.map((c) => ({ s: c.s, id: c.id, w: c.w, h: c.h }));
  state.canvasesCache.set(key, normalized);
  return normalized;
}

const DARKROOM_PRESETS = {
  normal: { invert: false, grayscale: false, sharpen: 0, gamma: 1.0, contrast: 105, brightness: 100 },
  faded: { invert: false, grayscale: true, sharpen: 1.5, gamma: 1.65, contrast: 190, brightness: 95 },
  dark: { invert: false, grayscale: true, sharpen: 1.25, gamma: 0.6, contrast: 155, brightness: 135 },
  negative: { invert: true, grayscale: true, sharpen: 1.0, gamma: 1.1, contrast: 145, brightness: 105 },
  bw: { invert: false, grayscale: true, sharpen: 2.0, gamma: 1.25, contrast: 240, brightness: 105 },
};

function applyDarkroomFilters() {
  const stage = qs('#osdStage');
  const v = state.viewer;

  // Update SVG gamma exponent (<feComponentTransfer>)
  const exp = String(v.gamma.toFixed(2));
  ['#gammaR', '#gammaG', '#gammaB'].forEach((id) => {
    const el = qs(id);
    if (el) el.setAttribute('exponent', exp);
  });

  // Update SVG 3x3 unsharp-mask sharpen kernel (<feConvolveMatrix>)
  const k = v.sharpen;
  const kernelEl = qs('#sharpenKernel');
  if (kernelEl) {
    const center = (1 + 4 * k).toFixed(2);
    const edge = (-k).toFixed(2);
    kernelEl.setAttribute('kernelMatrix', `0 ${edge} 0 ${edge} ${center} ${edge} 0 ${edge} 0`);
  }

  const useSvg = Math.abs(v.gamma - 1) > 0.01 || v.sharpen > 0.01;
  const parts = [];
  if (useSvg) parts.push('url(#microfilmEnhance)');
  if (v.invert) parts.push('invert(1)');
  if (v.grayscale) parts.push('grayscale(1)');
  parts.push(`contrast(${v.contrast}%)`, `brightness(${v.brightness}%)`);
  stage.style.filter = parts.join(' ');

  // Sync UI controls
  qs('#btnInvert').setAttribute('aria-pressed', String(v.invert));
  qs('#btnGrayscale').setAttribute('aria-pressed', String(v.grayscale));
  qs('#sharpenSlider').value = v.sharpen;
  qs('#gammaSlider').value = v.gamma;
  qs('#contrastSlider').value = v.contrast;
  qs('#brightnessSlider').value = v.brightness;
  qsa('.preset-btn').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.preset === v.preset)));
}

function applyDarkroomPreset(name) {
  const p = DARKROOM_PRESETS[name] || DARKROOM_PRESETS.normal;
  Object.assign(state.viewer, p, { preset: name });
  applyDarkroomFilters();
}

function ensureOsd() {
  if (state.osd) return state.osd;
  if (typeof window.OpenSeadragon === 'undefined') return null;
  state.osd = window.OpenSeadragon({
    id: 'osdStage',
    prefixUrl: 'https://cdn.jsdelivr.net/npm/openseadragon@4.1.1/build/openseadragon/images/',
    showNavigator: true,
    navigatorPosition: 'BOTTOM_RIGHT',
    crossOriginPolicy: 'Anonymous',
    ajaxWithCredentials: false,
    maxZoomPixelRatio: 2.5,
    visibilityRatio: 0.4,
    constrainDuringPan: false,
    gestureSettingsMouse: { clickToZoom: false, dblClickToZoom: true },
  });
  return state.osd;
}

function renderViewerSlide(slideNum) {
  const v = state.viewer;
  const clamped = Math.max(1, Math.min(v.total, Number(slideNum) || 1));
  v.slide = clamped;

  const canvas = v.canvases.find((c) => c.s === clamped) || v.canvases[clamped - 1];
  qs('#slideNumberInput').value = clamped;
  qs('#slideNumberInput').max = v.total;
  qs('#slideTotalLabel').textContent = `/ ${v.total.toLocaleString()}`;
  qs('#slideScrubber').max = v.total;
  qs('#slideScrubber').value = clamped;

  qsa('.anchor-chip', qs('#viewerAnchors')).forEach((ch) => {
    ch.classList.toggle('active', Number(ch.dataset.slide) === clamped);
  });

  syncViewerSavedUi();

  if (!canvas) return;
  const metaCode = qs('#viewerMetaCode');
  if (metaCode) metaCode.textContent = `manifest:${v.mid} · slide:${clamped}/${v.total}`;
  try {
    history.replaceState(null, '', `#view=${v.kind}:${v.mid}:${clamped}`);
  } catch {}
  const status = qs('#viewerStatus');
  status.hidden = false;
  status.textContent = `Loading slide ${clamped}…`;

  const infoUrl = `https://digital.library.uic.edu/iiif/3/${canvas.id}/info.json`;
  const osd = ensureOsd();
  if (osd) {
    osd.close();
    osd.open(infoUrl);
    osd.addOnceHandler('open', () => {
      status.hidden = true;
      if (v.rotation) osd.viewport.setRotation(v.rotation);
    });
    osd.addOnceHandler('open-failed', () => {
      osd.close();
      status.textContent = `Slide ${clamped} could not load from UIC — try stepping ±1 slide or open on UIC.`;
    });
  } else {
    status.textContent = 'Viewer script offline — use "UIC Page ↗" to view this slide.';
  }
}

async function openViewer(kind, mid, slideNum = 1) {
  const dialog = qs('#viewerDialog');
  const v = state.viewer;
  v.kind = kind;
  v.mid = String(mid);
  v.rotation = 0;

  if (kind === 'ledger') {
    const roll = state.ledgers.rolls.find((r) => String(r.manifest_id) === String(mid));
    const seg = state.ledgers.segments.find((s) => String(s.manifest_id) === String(mid));
    v.era = seg?.era || 'post-1912';
    v.title = roll ? shortBookTitle(roll.title) : `Ledger ${mid}`;
    v.uicUrl = roll?.uic_viewer_url || `https://digital.library.uic.edu/view/${mid}`;
    v.arkPermalink = roll?.ark_permalink || '#';
    v.anchors = (state.ledgers.anchors[String(mid)] || []).filter((a) => a.t === 'permits' && a.d0);
  } else {
    const reel = state.streetReels.reels.find((r) => String(r.mid) === String(mid));
    v.era = 'street-card';
    v.title = reel ? `Street Index Reel ${reel.n}: ${reel.s_raw} → ${reel.e_raw}` : `Reel ${mid}`;
    v.uicUrl = reel?.url || `https://digital.library.uic.edu/view/${mid}`;
    v.arkPermalink = reel?.ark ? `https://n2t.net/${reel.ark}` : '#';
    v.anchors = reel?.anchors || [];
  }

  qs('#viewerTitle').textContent = v.title;
  qs('#viewerSub').textContent = kind === 'street'
    ? 'Step through cards to find your address, then jump to the Ledger Book & Page below'
    : 'Scroll/pinch to zoom · Use arrow keys to turn pages';
  qs('#viewerUicLink').href = v.uicUrl;
  qs('#viewerArkLink').href = v.arkPermalink;

  // Show the Card -> Ledger handoff bar ONLY when viewing Street Index cards!
  qs('#cardHandoffBar').hidden = kind !== 'street';

  const anchorWrap = qs('#anchorStripWrap');
  const strip = qs('#viewerAnchors');
  if (v.anchors.length) {
    anchorWrap.hidden = false;
    strip.innerHTML = v.anchors.map((a) => {
      const lbl = kind === 'ledger' ? `p${a.s}: ${a.d0}` : `p${a.s}: ${a.raw_st} ${a.num}`;
      return `<button type="button" class="anchor-chip" data-slide="${a.s}" title="${esc(a.rn || '')}">${esc(lbl)}</button>`;
    }).join('');
  } else {
    anchorWrap.hidden = true;
    strip.innerHTML = '';
  }

  if (!dialog.open) dialog.showModal();
  applyDarkroomFilters();

  qs('#viewerStatus').hidden = false;
  qs('#viewerStatus').textContent = 'Opening reel…';
  try {
    v.canvases = await loadCanvases(kind, mid);
    v.total = v.canvases.length;
    renderViewerSlide(slideNum);
  } catch (err) {
    qs('#viewerStatus').textContent = String(err);
  }
}

/* -------------------- 10B. Save Slide & Smart Crop / Row Highlight Export -------------------- */
function currentCanvasObj() {
  const v = state.viewer;
  return v.canvases.find((c) => c.s === v.slide) || v.canvases[v.slide - 1] || null;
}

function loadCorsImage(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('Could not fetch image from UIC IIIF'));
    img.src = url;
  });
}

function activeCanvasFilterString() {
  const v = state.viewer;
  const parts = [];
  if (v.invert) parts.push('invert(1)');
  if (v.grayscale) parts.push('grayscale(1)');
  if (v.contrast !== 105) parts.push(`contrast(${v.contrast}%)`);
  if (v.brightness !== 100) parts.push(`brightness(${v.brightness}%)`);
  return parts.length ? parts.join(' ') : 'none';
}

async function exportIiifRegion({ region, maxWidth, highlightNormRect, filename }) {
  const canvasObj = currentCanvasObj();
  if (!canvasObj) return;
  const status = qs('#viewerStatus');
  status.hidden = false;
  status.textContent = 'Preparing high-res image…';

  try {
    const reqW = Math.min(maxWidth, canvasObj.w || maxWidth);
    const url = `https://digital.library.uic.edu/iiif/3/${canvasObj.id}/${region}/${reqW},/0/default.jpg`;
    const img = await loadCorsImage(url);

    const off = document.createElement('canvas');
    off.width = img.naturalWidth;
    off.height = img.naturalHeight;
    const ctx = off.getContext('2d');
    ctx.filter = activeCanvasFilterString();
    ctx.drawImage(img, 0, 0);
    ctx.filter = 'none';

    if (highlightNormRect) {
      const hx = highlightNormRect.x * off.width;
      const hy = highlightNormRect.y * off.height;
      const hw = highlightNormRect.w * off.width;
      const hh = highlightNormRect.h * off.height;
      ctx.fillStyle = 'rgba(250, 204, 21, 0.30)';
      ctx.fillRect(hx, hy, hw, hh);
      ctx.lineWidth = Math.max(3, Math.round(off.width / 550));
      ctx.strokeStyle = 'rgba(217, 119, 6, 0.95)';
      ctx.strokeRect(hx, hy, hw, hh);
    }

    const blob = await new Promise((res) => off.toBlob(res, 'image/png'));
    const dlUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = dlUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(dlUrl), 3000);
    status.hidden = true;
  } catch (err) {
    status.textContent = `Save failed: ${err.message}`;
  }
}

function initCropTool() {
  const overlay = qs('#cropOverlay');
  const box = qs('#cropBox');
  const toolbar = qs('#cropToolbar');
  const toggleBtn = qs('#btnToggleCrop');
  const highlightCheck = qs('#cropHighlightCheck');

  // Selection box in stage-relative pixels { x, y, w, h }
  const rect = { x: 80, y: 80, w: 300, h: 200 };
  let active = false;
  let dragMode = null; // 'move' | 'draw' | 'nw' | 'ne' | 'sw' | 'se'
  let startX = 0, startY = 0;
  let startRect = { ...rect };

  function syncBoxDom() {
    box.style.left = `${rect.x}px`;
    box.style.top = `${rect.y}px`;
    box.style.width = `${rect.w}px`;
    box.style.height = `${rect.h}px`;
    box.classList.toggle('is-highlighted', highlightCheck.checked);
  }

  function applyShapePreset(shape) {
    const stageW = overlay.clientWidth || 800;
    const stageH = overlay.clientHeight || 500;
    qsa('.crop-shape-btn').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.shape === shape)));
    if (shape === 'strip') {
      rect.w = Math.round(stageW * 0.84);
      rect.h = Math.max(36, Math.round(stageH * 0.09));
      rect.x = Math.round((stageW - rect.w) / 2);
      rect.y = Math.round(stageH * 0.44);
    } else {
      rect.w = Math.round(Math.min(stageW * 0.38, 360));
      rect.h = Math.round(Math.min(stageH * 0.34, 240));
      rect.x = Math.round((stageW - rect.w) / 2);
      rect.y = Math.round((stageH - rect.h) / 2);
    }
    syncBoxDom();
  }

  function setCropActive(on) {
    active = on;
    overlay.hidden = !on;
    toolbar.hidden = !on;
    toggleBtn.setAttribute('aria-pressed', String(on));
    if (on) {
      const isPre1912 = state.viewer.era === 'pre-1912';
      highlightCheck.checked = isPre1912;
      applyShapePreset(isPre1912 ? 'strip' : 'box');
    }
  }

  function getImageRectFromSelection() {
    const osd = state.osd;
    const canvasObj = currentCanvasObj();
    if (!osd || !canvasObj) return null;
    const imgW = canvasObj.w || 2500;
    const imgH = canvasObj.h || 1800;

    const tl = osd.viewport.viewportToImageCoordinates(
      osd.viewport.pointFromPixel(new window.OpenSeadragon.Point(rect.x, rect.y), true)
    );
    const br = osd.viewport.viewportToImageCoordinates(
      osd.viewport.pointFromPixel(new window.OpenSeadragon.Point(rect.x + rect.w, rect.y + rect.h), true)
    );

    const x0 = Math.max(0, Math.min(imgW - 10, Math.round(Math.min(tl.x, br.x))));
    const y0 = Math.max(0, Math.min(imgH - 10, Math.round(Math.min(tl.y, br.y))));
    const x1 = Math.max(x0 + 10, Math.min(imgW, Math.round(Math.max(tl.x, br.x))));
    const y1 = Math.max(y0 + 10, Math.min(imgH, Math.round(Math.max(tl.y, br.y))));
    return {
      x: x0,
      y: y0,
      w: x1 - x0,
      h: y1 - y0,
      norm: { x: x0 / imgW, y: y0 / imgH, w: (x1 - x0) / imgW, h: (y1 - y0) / imgH },
    };
  }

  overlay.addEventListener('pointerdown', (e) => {
    const oRect = overlay.getBoundingClientRect();
    startX = e.clientX - oRect.left;
    startY = e.clientY - oRect.top;
    startRect = { ...rect };
    const handle = e.target.closest('.crop-handle');
    if (handle) {
      dragMode = handle.dataset.handle;
    } else if (e.target === box) {
      dragMode = 'move';
    } else {
      dragMode = 'draw';
      rect.x = startX;
      rect.y = startY;
      rect.w = 1;
      rect.h = 1;
    }
    overlay.setPointerCapture(e.pointerId);
    syncBoxDom();
  });

  overlay.addEventListener('pointermove', (e) => {
    if (!dragMode) return;
    const oRect = overlay.getBoundingClientRect();
    const curX = Math.max(0, Math.min(oRect.width, e.clientX - oRect.left));
    const curY = Math.max(0, Math.min(oRect.height, e.clientY - oRect.top));
    const dx = curX - startX;
    const dy = curY - startY;

    if (dragMode === 'move') {
      rect.x = Math.max(0, Math.min(oRect.width - startRect.w, startRect.x + dx));
      rect.y = Math.max(0, Math.min(oRect.height - startRect.h, startRect.y + dy));
    } else if (dragMode === 'draw') {
      rect.x = Math.min(startX, curX);
      rect.y = Math.min(startY, curY);
      rect.w = Math.max(16, Math.abs(dx));
      rect.h = Math.max(16, Math.abs(dy));
    } else {
      let { x, y, w, h } = startRect;
      if (dragMode.includes('e')) w = Math.max(24, startRect.w + dx);
      if (dragMode.includes('s')) h = Math.max(20, startRect.h + dy);
      if (dragMode.includes('w')) {
        const nx = Math.min(startRect.x + startRect.w - 24, startRect.x + dx);
        w = startRect.x + startRect.w - nx;
        x = nx;
      }
      if (dragMode.includes('n')) {
        const ny = Math.min(startRect.y + startRect.h - 20, startRect.y + dy);
        h = startRect.y + startRect.h - ny;
        y = ny;
      }
      rect.x = Math.max(0, x);
      rect.y = Math.max(0, y);
      rect.w = Math.min(oRect.width - rect.x, w);
      rect.h = Math.min(oRect.height - rect.y, h);
    }
    syncBoxDom();
  });

  overlay.addEventListener('pointerup', () => {
    dragMode = null;
  });

  toggleBtn.addEventListener('click', () => setCropActive(!active));
  qs('#btnCancelCrop').addEventListener('click', () => setCropActive(false));
  highlightCheck.addEventListener('change', syncBoxDom);

  qsa('.crop-shape-btn').forEach((btn) => {
    btn.addEventListener('click', () => applyShapePreset(btn.dataset.shape));
  });

  qs('#btnSaveFullSlide').addEventListener('click', () => {
    const v = state.viewer;
    exportIiifRegion({
      region: 'pct:0,0,100,99',
      maxWidth: 2500,
      highlightNormRect: null,
      filename: `permit_${v.mid}_slide${v.slide}.png`,
    });
  });

  qs('#btnDownloadCrop').addEventListener('click', () => {
    const v = state.viewer;
    const ir = getImageRectFromSelection();
    if (!ir) return;
    exportIiifRegion({
      region: `${ir.x},${ir.y},${ir.w},${ir.h}`,
      maxWidth: 2400,
      highlightNormRect: highlightCheck.checked ? { x: 0, y: 0, w: 1, h: 1 } : null,
      filename: `permit_${v.mid}_slide${v.slide}_crop.png`,
    });
  });

  qs('#btnDownloadHighlighted').addEventListener('click', () => {
    const v = state.viewer;
    const ir = getImageRectFromSelection();
    if (!ir) return;
    exportIiifRegion({
      region: 'pct:0,0,100,99',
      maxWidth: 2500,
      highlightNormRect: ir.norm,
      filename: `permit_${v.mid}_slide${v.slide}_highlighted.png`,
    });
  });

  return { close: () => setCropActive(false) };
}

function initViewerControls() {
  const dialog = qs('#viewerDialog');
  const cropTool = initCropTool();
  qs('#viewerCloseBtn').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    cropTool.close();
    if (location.hash.startsWith('#view=')) {
      try { history.replaceState(null, '', location.pathname + location.search); } catch {}
    }
  });

  if (!('closedBy' in HTMLDialogElement.prototype)) {
    dialog.addEventListener('click', (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      const inside = rect.top <= event.clientY && event.clientY <= rect.top + rect.height
        && rect.left <= event.clientX && event.clientX <= rect.left + rect.width;
      if (!inside) dialog.close();
    });
  }

  qs('#btnPrev10').addEventListener('click', () => renderViewerSlide(state.viewer.slide - 10));
  qs('#btnPrev1').addEventListener('click', () => renderViewerSlide(state.viewer.slide - 1));
  qs('#btnNext1').addEventListener('click', () => renderViewerSlide(state.viewer.slide + 1));
  qs('#btnNext10').addEventListener('click', () => renderViewerSlide(state.viewer.slide + 10));

  qs('#slideJumpForm').addEventListener('submit', (e) => {
    e.preventDefault();
    renderViewerSlide(Number(qs('#slideNumberInput').value));
  });

  qs('#slideScrubber').addEventListener('change', (e) => {
    renderViewerSlide(Number(e.target.value));
  });

  // Card -> Ledger handoff mode switcher (Book & Page vs Permit Date)
  qsa('.handoff-switch-link').forEach((btn) => {
    btn.addEventListener('click', () => {
      const m = btn.dataset.hmode;
      qsa('[data-hpanel]').forEach((panel) => { panel.hidden = panel.dataset.hpanel !== m; });
      qs('#handoffToDateBtn')?.setAttribute('aria-expanded', String(m === 'date'));
      qs('#handoffToBookPageBtn')?.setAttribute('aria-expanded', String(m === 'bookpage'));
      qs('#handoffDateMatches').hidden = true;
      if (m === 'date') qs('#handoffDate')?.focus();
      else qs('#handoffBookSelect')?.focus();
    });
  });

  qs('#viewerLedgerHandoff').addEventListener('submit', (e) => {
    e.preventDefault();
    const mid = qs('#handoffBookSelect').value;
    const pg = Number(qs('#handoffPageNum').value || 1);
    const res = interpolatePageSlide(mid, pg);
    openViewer('ledger', mid, res.slide);
  });

  qs('#viewerDateHandoff').addEventListener('submit', (e) => {
    e.preventDefault();
    const iso = qs('#handoffDate').value;
    const side = qs('#handoffSide').value;
    const targetMs = parseIsoMs(iso);
    const box = qs('#handoffDateMatches');
    if (!targetMs) return;

    const pad = 14 * 86400 * 1000;
    const matches = state.ledgers.segments.filter((seg) => {
      if (seg.order_direction === 'single' && seg.segment_count_in_roll > 1) return false;
      const d0 = parseIsoMs(seg.date_min);
      const d1 = parseIsoMs(seg.date_max);
      if (!d0 || !d1 || targetMs < d0 - pad || targetMs > d1 + pad) return false;
      if (side === 'ALL') return true;
      return matchesDivisionToken(seg.observed_regions, side) || matchesDivisionToken(seg.observed_regions, 'Citywide');
    });

    if (!matches.length) {
      box.hidden = false;
      box.innerHTML = `<span>No ledger matched <strong>${esc(iso)}</strong> for ${esc(side)}. Try "Any side of Chicago".</span>`;
      return;
    }

    matches.sort((a, b) => (b.slide_end - b.slide_start) - (a.slide_end - a.slide_start));
    if (matches.length === 1) {
      const interp = interpolateLedgerSlide(matches[0], iso);
      box.hidden = true;
      openViewer('ledger', matches[0].manifest_id, interp.slide);
    } else {
      box.hidden = false;
      box.innerHTML = '<span>Matching ledgers:</span>' + matches.slice(0, 4).map((seg) => {
        const interp = interpolateLedgerSlide(seg, iso);
        return `<button type="button" class="btn btn-sm btn-open open-viewer-btn" data-kind="ledger" data-mid="${esc(seg.manifest_id)}" data-slide="${interp.slide}">${esc(shortBookTitle(seg.title).split(':')[0])} (p~${interp.slide}) ↗</button>`;
      }).join('');
    }
  });

  qs('#viewerAnchors').addEventListener('click', (e) => {
    const chip = e.target.closest('.anchor-chip');
    if (chip) renderViewerSlide(Number(chip.dataset.slide));
  });

  qs('#btnInvert').addEventListener('click', () => {
    state.viewer.invert = !state.viewer.invert;
    state.viewer.preset = 'custom';
    applyDarkroomFilters();
  });

  qs('#btnGrayscale').addEventListener('click', () => {
    state.viewer.grayscale = !state.viewer.grayscale;
    state.viewer.preset = 'custom';
    applyDarkroomFilters();
  });

  qs('#btnRotate').addEventListener('click', () => {
    state.viewer.rotation = (state.viewer.rotation + 90) % 360;
    if (state.osd?.viewport) state.osd.viewport.setRotation(state.viewer.rotation);
  });

  qs('#sharpenSlider').addEventListener('input', (e) => {
    state.viewer.sharpen = Number(e.target.value);
    state.viewer.preset = 'custom';
    applyDarkroomFilters();
  });

  qs('#gammaSlider').addEventListener('input', (e) => {
    state.viewer.gamma = Number(e.target.value);
    state.viewer.preset = 'custom';
    applyDarkroomFilters();
  });

  qs('#contrastSlider').addEventListener('input', (e) => {
    state.viewer.contrast = Number(e.target.value);
    state.viewer.preset = 'custom';
    applyDarkroomFilters();
  });

  qs('#brightnessSlider').addEventListener('input', (e) => {
    state.viewer.brightness = Number(e.target.value);
    state.viewer.preset = 'custom';
    applyDarkroomFilters();
  });

  qs('#btnResetImage').addEventListener('click', () => {
    state.viewer.rotation = 0;
    if (state.osd?.viewport) state.osd.viewport.setRotation(0);
    applyDarkroomPreset('normal');
  });

  qsa('.preset-btn').forEach((btn) => {
    btn.addEventListener('click', () => applyDarkroomPreset(btn.dataset.preset));
  });

  // Tag & Note controls inside viewer
  qsa('.tag-btn', qs('#cardTagBar')).forEach((btn) => {
    btn.addEventListener('click', () => {
      if (state.viewer.kind !== 'street') return;
      toggleCardTag(state.viewer.mid, state.viewer.slide, btn.dataset.tag);
    });
  });

  qs('#cardNoteInput').addEventListener('input', (e) => {
    if (state.viewer.kind !== 'street') return;
    updateCardNote(state.viewer.mid, state.viewer.slide, e.target.value);
  });

  qs('#btnQueuePrev').addEventListener('click', () => stepReviewQueue(-1));
  qs('#btnQueueNext').addEventListener('click', () => stepReviewQueue(1));

  // Saved Cards view controls
  qs('#savedFilterBar').addEventListener('click', (e) => {
    const btn = e.target.closest('.saved-filter-btn');
    if (!btn) return;
    state.savedFilter = btn.dataset.sfilter;
    qsa('.saved-filter-btn').forEach((b) => b.setAttribute('aria-pressed', String(b === btn)));
    renderSavedCardsView();
  });

  qs('#btnStartReview').addEventListener('click', () => {
    const queue = getReviewQueue();
    if (!queue.length) return;
    state.activeCardKey = cardKey(queue[0].mid, queue[0].slide);
    openViewer('street', queue[0].mid, queue[0].slide);
  });

  qs('#btnClearSaved').addEventListener('click', () => {
    if (!window.confirm('Clear all saved index cards and notes?')) return;
    state.savedCards = {};
    state.activeCardKey = null;
    persistSavedCards();
    renderSavedCardsView();
    syncViewerSavedUi();
  });

  qs('#savedCardsList').addEventListener('click', (e) => {
    const btn = e.target.closest('.saved-tag-btn');
    if (!btn) return;
    toggleCardTag(btn.dataset.mid, Number(btn.dataset.slide), btn.dataset.tag);
  });

  qs('#savedCardsList').addEventListener('input', (e) => {
    const inp = e.target.closest('.saved-note-edit');
    if (!inp) return;
    updateCardNote(inp.dataset.mid, Number(inp.dataset.slide), inp.value);
  });

  window.addEventListener('keydown', (e) => {
    if (!dialog.open) return;
    if (e.target.matches('input, select, textarea')) return;
    const step = e.shiftKey ? 10 : 1;
    if (e.key === 'ArrowLeft') { e.preventDefault(); renderViewerSlide(state.viewer.slide - step); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); renderViewerSlide(state.viewer.slide + step); }
    else if (e.key === 'i' || e.key === 'I') { state.viewer.invert = !state.viewer.invert; state.viewer.preset = 'custom'; applyDarkroomFilters(); }
    else if (e.key === 'g' || e.key === 'G') { state.viewer.grayscale = !state.viewer.grayscale; state.viewer.preset = 'custom'; applyDarkroomFilters(); }
    else if (e.key === 'r' || e.key === 'R') {
      state.viewer.rotation = (state.viewer.rotation + 90) % 360;
      if (state.osd?.viewport) state.osd.viewport.setRotation(state.viewer.rotation);
    } else if (state.viewer.kind === 'street' && (e.key === 'y' || e.key === 'Y')) {
      toggleCardTag(state.viewer.mid, state.viewer.slide, 'yes');
    } else if (state.viewer.kind === 'street' && (e.key === 'm' || e.key === 'M')) {
      toggleCardTag(state.viewer.mid, state.viewer.slide, 'maybe');
    } else if (state.viewer.kind === 'street' && (e.key === 'n' || e.key === 'N')) {
      toggleCardTag(state.viewer.mid, state.viewer.slide, 'no');
    }
  });

  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.open-viewer-btn');
    if (btn) {
      openViewer(btn.dataset.kind, btn.dataset.mid, Number(btn.dataset.slide || 1));
      return;
    }
    const crossBtn = e.target.closest('.cross-street-btn');
    if (crossBtn) {
      qs('#jumpAddress').value = crossBtn.dataset.addr;
      runAddressJump();
    }
  });
}

/* -------------------- 11. Bootstrap -------------------- */
async function init() {
  initThemeToggle();
  loadSavedCards();
  initViewNav();
  initViewerControls();

  const [ledgersResp, streetsResp] = await Promise.all([
    fetch('./data/ledgers_bundle.json'),
    fetch('./data/street_reels_bundle.json'),
  ]);
  state.ledgers = await ledgersResp.json();
  state.streetReels = await streetsResp.json();

  populateBookSelects();
  populateCommunityFilter();

  // Address search form & street autocomplete
  qs('#jumpAddress').addEventListener('input', updateAddressAutocomplete);
  qs('#addressJumpForm').addEventListener('submit', (e) => { e.preventDefault(); runAddressJump(); });

  // Unified Permit Ledgers timeline + table filters
  const syncDecadePills = () => {
    const f = String(qs('#ledgerYearFrom').value);
    const t = String(qs('#ledgerYearTo').value);
    qsa('.decade-btn').forEach((b) => {
      b.setAttribute('aria-pressed', String(b.dataset.from === f && b.dataset.to === t));
    });
  };
  qs('#decadeBar').addEventListener('click', (e) => {
    const btn = e.target.closest('.decade-btn');
    if (!btn) return;
    qs('#ledgerYearFrom').value = btn.dataset.from;
    qs('#ledgerYearTo').value = btn.dataset.to;
    syncDecadePills();
    renderLedgersAndTimeline();
  });
  qs('#timelineZoom').addEventListener('input', renderLedgersAndTimeline);
  ['#ledgerSearch', '#ledgerCommunityFilter', '#ledgerYearFrom', '#ledgerYearTo', '#ledgerMultiOnly'].forEach((sel) => {
    qs(sel).addEventListener('input', () => { syncDecadePills(); renderLedgersAndTimeline(); });
    qs(sel).addEventListener('change', () => { syncDecadePills(); renderLedgersAndTimeline(); });
  });
  qs('#streetReelSearch').addEventListener('input', renderStreetReelsTable);
  initLetterBar();

  // Initial renders (no default address search on load)
  renderLedgersAndTimeline();
  renderStreetReelsTable();

  // Load precomputed Chicago autocomplete + cross-street tables asynchronously (non-blocking)
  fetch('./data/chicago_streets_autocomplete.json')
    .then((r) => (r.ok ? r.json() : []))
    .then((streets) => {
      state.chicagoStreets = streets;
      updateAddressAutocomplete();
    })
    .catch(() => {});

  fetch('./data/chicago_cross_streets.json')
    .then((r) => (r.ok ? r.json() : null))
    .then((cross) => {
      state.crossStreets = cross;
      if ((qs('#jumpAddress').value || '').trim()) runAddressJump();
    })
    .catch(() => {});

  // Deep-link support: #view=ledger:825378:3 or #view=street:1038601:2500
  const hashMatch = location.hash.match(/^#view=(ledger|street):([a-zA-Z0-9_-]+):(\d+)$/);
  if (hashMatch) {
    openViewer(hashMatch[1], hashMatch[2], Number(hashMatch[3]));
  }
}

init().catch((err) => {
  qs('#globalStats').textContent = `Error loading data: ${err.message}`;
});
