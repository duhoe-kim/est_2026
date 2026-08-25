/* ================= 실제 데이터 로드 =================
   build_stations.py 가 만든 stations.data.js / stations.json 을 사용합니다.
   데이터 구조:  { meta, locations[], stations[{name,lat,lng,chargers,kw,usage[7][24]}] }
   usage[요일][시] = 점유율 0~1   (요일 0=월 … 6=일)
=================================================== */
let DATA = null;

/* 날짜 목록: 오늘부터 14일 (요일 인덱스는 데이터와 동일하게 0=월) */
/* ================= 날짜 =================
   달력에서 자유롭게 고릅니다. 요일은 날짜에서 자동으로 나옵니다.
   (혼잡 패턴은 요일×시간 단위이므로 어떤 날짜든 계산 가능합니다) */
const WK = '일월화수목금토';
const TODAY = (() => { const d = new Date(); d.setHours(0,0,0,0); return d; })();
const MAX_DAY = (() => { const d = new Date(TODAY); d.setDate(d.getDate() + 90); return d; })();
let curDate = new Date(TODAY);      /* 선택된 날짜 */
let calView = new Date(TODAY);      /* 달력에 보이는 달 */

const dLabel = d => `${d.getMonth() + 1}월 ${d.getDate()}일 (${WK[d.getDay()]})`;
const dShort = d => `${d.getMonth() + 1}/${d.getDate()} (${WK[d.getDay()]})`;
const dKey   = d => `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
const dWd    = d => (d.getDay() + 6) % 7;   /* 0=월 … 6=일 (데이터 기준) */

function setDate(d){
  curDate = new Date(d); curDate.setHours(0,0,0,0);
  if ($('dateName')) $('dateName').textContent = dLabel(curDate);
}
function setDateOffset(n){ const d = new Date(TODAY); d.setDate(d.getDate() + n); setDate(d); }

/* 두 좌표 사이 거리(km) */
function haversine(a, b) {
  const R = 6371, r = Math.PI / 180;
  const dLat = (b.lat - a.lat) * r, dLng = (b.lng - a.lng) * r;
  const x = Math.sin(dLat / 2) ** 2 +
            Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(x));
}

/* ================= 상태 ================= */
let radius = 3, kwFilter = 0, recs = [], sel = null, autoTimer = null, loadTimers = [];
const G = [
  { t:'여유', cls:'b-free' },
  { t:'주의', cls:'b-warn' },
  { t:'혼잡', cls:'b-busy' },
];
const freeOf = s => Math.max(0, Math.min(s.c, Math.round(s.c * (1 - s.rate))));
const gradeOf = v => v >= 8 ? 2 : v >= 5 ? 1 : 0;
const $ = id => document.getElementById(id);

/* ================= 셀렉트 채우기 ================= */
const HOURS = Array.from({length:24}, (_,i) => String(i).padStart(2,'0') + ':00');   /* 00~23시 */

function initSelects(){
  setDate(TODAY);
  $('inTime').innerHTML = HOURS.map(h => `<option${h === '14:00' ? ' selected' : ''}>${h}</option>`).join('');
  const def = DATA.locations.find(l => l.name === '제주국제공항') || DATA.locations[0];
  if (def) setLoc({ name:def.name, area:def.area || '', lat:def.lat, lng:def.lng });
}

document.querySelectorAll('#inRad .seg').forEach(s => s.onclick = () => { setSeg('#inRad', s); radius = +s.dataset.v; });
document.querySelectorAll('#inKw  .seg').forEach(s => s.onclick = () => { setSeg('#inKw',  s); kwFilter = +s.dataset.v; });
function setSeg(q, el){ document.querySelectorAll(q + ' .seg').forEach(x => x.classList.remove('on')); el.classList.add('on'); }

/* ================= 결과 화면에서 조건 바로 바꾸기 =================
   조건 칩을 누르면 해당 항목만 바텀시트로 열리고,
   값을 고르면 로딩 화면 없이 그 자리에서 다시 계산합니다. */
const SHEET_T = { date:'예정 날짜', time:'예정 시간' };
const RAD_OPT = [1, 3, 5];
const KW_OPT  = [[0,'전체'], [50,'50–100kW'], [100,'100–200kW'], [200,'200kW 이상']];
const CK = '<svg class="ck" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>';

const optRow = (label, on, val) =>
  `<button class="opt${on ? ' on' : ''}" type="button" data-v="${val}">${label}${CK}</button>`;

function openSheet(kind){
  if (!DATA) return;
  $('sheetTitle').textContent = SHEET_T[kind];
  const body = $('sheetBody');

  if (kind === 'date'){
    calView = new Date(curDate.getFullYear(), curDate.getMonth(), 1);
    body.innerHTML = '<div id="calBox"></div>' +
      '<button class="sh-done" type="button" onclick="closeSheet()">확인</button>';
    renderCal();
  } else if (kind === 'time'){
    body.innerHTML = `
      <div class="hgrid">
        ${HOURS.map(h => `<button type="button" data-t="${h}"${h === $('inTime').value ? ' class="on"' : ''}>${h}</button>`).join('')}
      </div>
      <div class="sh-hint">예측은 1시간 단위로 계산됩니다.</div>
      <button class="sh-done" type="button" onclick="closeSheet()">확인</button>`;
    body.querySelectorAll('.hgrid button').forEach(b => b.onclick = () => {
      $('inTime').value = b.dataset.t;
      body.querySelectorAll('.hgrid button').forEach(x => x.classList.toggle('on', x === b));
      liveUpdate();
    });
  } else {
    let rows = '';
    if (kind === 'rad')  rows = RAD_OPT.map(v => optRow(v + 'km', v === radius, v)).join('');
    if (kind === 'kw')   rows = KW_OPT.map(([v, l]) => optRow(l, v === kwFilter, v)).join('');
    body.innerHTML = rows;
    body.querySelectorAll('.opt').forEach(b => b.onclick = () => { applyCond(kind, b.dataset.v); closeSheet(); });
  }
  $('sheetWrap').classList.add('open');
}
/* 달력 그리기 */
function renderCal(){
  const y = calView.getFullYear(), m = calView.getMonth();
  const first = new Date(y, m, 1), last = new Date(y, m + 1, 0);
  const prevOk = new Date(y, m, 0) >= TODAY;
  const nextOk = new Date(y, m + 1, 1) <= MAX_DAY;

  let cells = '';
  for (let i = 0; i < first.getDay(); i++) cells += '<button class="off" disabled></button>';
  for (let day = 1; day <= last.getDate(); day++){
    const d = new Date(y, m, day);
    const dis = d < TODAY || d > MAX_DAY;
    const cls = [
      d.getDay() === 0 ? 'sun' : '', d.getDay() === 6 ? 'sat' : '',
      dKey(d) === dKey(TODAY) ? 'today' : '',
      dKey(d) === dKey(curDate) ? 'on' : '',
    ].filter(Boolean).join(' ');
    cells += `<button class="${cls}" data-d="${day}"${dis ? ' disabled' : ''}>${day}</button>`;
  }

  $('calBox').innerHTML = `
    <div class="cal-h">
      <button type="button" id="calPrev"${prevOk ? '' : ' disabled'}>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
      </button>
      <span class="ym">${y}년 ${m + 1}월</span>
      <button type="button" id="calNext"${nextOk ? '' : ' disabled'}>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>
      </button>
    </div>
    <div class="cal-w">${[...WK].map(w => `<span>${w}</span>`).join('')}</div>
    <div class="cal-g">${cells}</div>
    <div class="cal-sel">${dLabel(curDate)} 기준으로 예측</div>`;

  const pv = $('calPrev'), nx = $('calNext');
  if (pv) pv.onclick = () => { calView = new Date(y, m - 1, 1); renderCal(); };
  if (nx) nx.onclick = () => { calView = new Date(y, m + 1, 1); renderCal(); };
  $('calBox').querySelectorAll('.cal-g button:not([disabled])').forEach(b => b.onclick = () => {
    setDate(new Date(y, m, +b.dataset.d));
    renderCal();
    liveUpdateIfResult();
  });
}
/* 결과 화면에서 열렸을 때만 즉시 재계산 */
function liveUpdateIfResult(){ if ($('sc3').classList.contains('active')) liveUpdate(); }

function closeSheet(){ $('sheetWrap').classList.remove('open'); }
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeSheet(); });

function applyCond(kind, v){
  if (kind === 'loc')  $('inLoc').value  = v;
  if (kind === 'time') $('inTime').value = v;
  if (kind === 'rad' || kind === 'kw'){
    const inp = kind === 'rad' ? 'inRad' : 'inKw';
    if (kind === 'rad') radius = +v; else kwFilter = +v;
    const twin = document.querySelector('#' + inp + ' .seg[data-v="' + v + '"]');
    if (twin) setSeg('#' + inp, twin);
  }
  liveUpdate();
}

/* ================= 위치 검색 화면 =================
   1) 서버(/api/search-place)가 있으면 NAVER 지역 검색 결과를 씁니다.
   2) 서버 없이 index.html 만 열면 내장 지점 목록으로 자동 전환됩니다. (시연 안전장치)
   Client Secret 은 서버에만 있고 이 파일에는 들어오지 않습니다. */
let locReturn = 1;          /* 검색을 끝내고 돌아갈 화면 */
let locResults = [];        /* 현재 검색 결과 */
let locSel = -1;            /* 선택된 결과 index */
let locTimer = null;
let locMode = 'ready';      /* ready | api | offline | error */

let MAP_KEY = null, MAP_SDK = false, mapObj = null, mapMarkers = [], mapInfo = null;

/* 선택 상태 — 다른 기능(Directions 5 등)에서 꺼내 씁니다 */
let origin = null;                 /* 예정 위치 (출발 기준점) */
window.MIRIBOM_ORIGIN = null;
window.MIRIBOM_DESTINATION = null; /* 선택한 충전소 (목적지) */

/* Directions 5 는 '경도,위도' 순서의 문자열을 받습니다. */
function toDirectionsParam(p){ return p && p.lat != null ? `${p.lng},${p.lat}` : null; }
function getRouteParams(){
  return {
    start: toDirectionsParam(window.MIRIBOM_ORIGIN),
    goal:  toDirectionsParam(window.MIRIBOM_DESTINATION),
  };
}

function setLoc(place){
  origin = place;
  window.MIRIBOM_ORIGIN = place;
  $('inLoc').value = place.name;
  if ($('locName')) $('locName').textContent = place.name;
}
function setLocByName(name){
  const l = DATA.locations.find(x => x.name === name);
  if (l) setLoc({ name:l.name, area:l.area || '', lat:l.lat, lng:l.lng });
}

/* ── 지도 SDK ── */
async function initMapKey(){
  try{
    const r = await fetch('/api/config', { cache:'no-store' });
    const j = await r.json();
    if (j && j.mapClientId){
      MAP_KEY = j.mapClientId;
      const sc = document.createElement('script');
      sc.src = 'https://oapi.map.naver.com/openapi/v3/maps.js?ncpKeyId=' + encodeURIComponent(MAP_KEY) + '&submodules=geocoder';
      sc.onload = () => { MAP_SDK = true; };
      document.head.appendChild(sc);
    }
  }catch(e){ /* 서버 없이 열린 경우 — 대체 지도로 동작 */ }
}

function ensureMap(){
  if (!MAP_SDK || !window.naver || !naver.maps) return null;
  if (!mapObj){
    $('locMapBg').style.display = 'none';
    $('locMapTag').style.display = 'none';
    const host = document.createElement('div');
    $('locMap').appendChild(host);
    mapObj = new naver.maps.Map(host, {
      center: new naver.maps.LatLng(33.4996, 126.5312),
      zoom: 10, scaleControl:false, mapDataControl:false, logoControlOptions:{position:3},
    });
  }
  return mapObj;
}

/* ── 검색 ── */
function onLocInput(v){
  clearTimeout(locTimer);
  locTimer = setTimeout(() => doLocSearch(v), 300);
}
function clearLocQ(){ $('locQ').value = ''; doLocSearch(''); $('locQ').focus(); }

function localFallback(q){
  const query = (q || '').trim();
  const src = query
    ? DATA.locations.filter(l => l.name.includes(query) || (l.area || '').includes(query))
    : DATA.locations.slice(0, 5);
  return src.slice(0, 5).map(l => ({
    name:l.name, category:'', address:l.area || '제주특별자치도', roadAddress:'',
    lat:l.lat, lng:l.lng, coordType:'wgs84e7',
  }));
}

async function doLocSearch(q, force){
  const query = (q || '').trim();
  clearLocSel();

  if (!query){
    locMode = 'ready';
    locResults = localFallback('');
    renderLocResults('');
    return;
  }

  try{
    const r = await fetch('/api/search-place?query=' + encodeURIComponent(query) + '&display=5', { cache:'no-store' });
    const j = await r.json();
    if (!r.ok || !j.ok) throw new Error(j && j.message ? j.message : 'HTTP ' + r.status);
    locMode = 'api';
    locResults = j.items.map(fixCoord).filter(x => x.lat != null);
  }catch(e){
    locMode = (String(e.message).indexOf('Failed to fetch') >= 0 || e instanceof TypeError) ? 'offline' : 'error';
    locErr = e.message;
    locResults = localFallback(query);
  }
  renderLocResults(query);
}
let locErr = '';

/* 과거 형식(TM128)으로 오는 경우 지도 SDK 로 변환 */
function fixCoord(it){
  if (it.lat != null) return it;
  if (MAP_SDK && window.naver && naver.maps.TransCoord){
    const ll = naver.maps.TransCoord.fromTM128ToLatLng(new naver.maps.Point(it.mapx, it.mapy));
    return Object.assign({}, it, { lat: ll.lat(), lng: ll.lng() });
  }
  return it;
}

/* ── 결과 그리기 ── */
const esc = t => String(t == null ? '' : t).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

function renderLocResults(query){
  const st = $('locState');
  if (locMode === 'api')          st.textContent = `NAVER 지역 검색 결과 ${locResults.length}곳`;
  else if (locMode === 'offline') st.textContent = '서버에 연결되지 않아 내장 지점 목록에서 찾았습니다 · node server.js 로 실행하면 실제 검색이 됩니다';
  else if (locMode === 'error')   st.textContent = '검색을 실패해 내장 목록으로 대신 찾았습니다 · ' + locErr;
  else                            st.textContent = '자주 찾는 위치';
  st.className = 'lstate' + (locMode === 'error' ? ' warn' : '');

  if (!locResults.length){
    $('locList').innerHTML = `<div class="lempty">‘${esc(query)}’ 검색 결과가 없어요.<br>다른 이름이나 지역으로 찾아보세요.</div>`;
    drawMarkers();
    return;
  }

  $('locList').innerHTML = locResults.map((it, i) => `
    <button class="lrow" type="button" data-i="${i}">
      <span class="no">${i + 1}</span>
      <span><span class="nm">${esc(it.name)}</span>
        <span class="ar" style="display:block;">${esc(it.roadAddress || it.address || '')}${it.category ? ' · ' + esc(it.category.split('>').pop().trim()) : ''}</span></span>
    </button>`).join('');
  $('locList').querySelectorAll('.lrow').forEach(b => b.onclick = () => selectLoc(+b.dataset.i));

  drawMarkers();
}

/* ── 지도 마커 ── */
function drawMarkers(){
  const m = ensureMap();
  if (m){
    mapMarkers.forEach(mk => mk.setMap(null));
    mapMarkers = [];
    if (!locResults.length) return;
    const bounds = new naver.maps.LatLngBounds();
    locResults.forEach((it, i) => {
      const pos = new naver.maps.LatLng(it.lat, it.lng);
      const mk = new naver.maps.Marker({ position: pos, map: m, title: it.name });
      naver.maps.Event.addListener(mk, 'click', () => selectLoc(i));
      mapMarkers.push(mk);
      bounds.extend(pos);
    });
    if (locResults.length === 1) m.setCenter(bounds.getCenter()), m.setZoom(15);
    else m.fitBounds(bounds);
    return;
  }
  /* 지도 키가 없을 때 — 좌표를 상대 위치로 찍는 대체 표시 */
  $('locMap').querySelectorAll('.mk,.mkl').forEach(e => e.remove());
  if (!locResults.length) return;
  const la = locResults.map(x => x.lat), ln = locResults.map(x => x.lng);
  const pad = 0.02;
  const y0 = Math.min(...la) - pad, y1 = Math.max(...la) + pad;
  const x0 = Math.min(...ln) - pad, x1 = Math.max(...ln) + pad;
  locResults.forEach((it, i) => {
    const L = ((it.lng - x0) / (x1 - x0)) * 76 + 12;
    const T = (1 - (it.lat - y0) / (y1 - y0)) * 66 + 17;
    const d = document.createElement('div');
    d.className = 'mk' + (i === locSel ? ' on' : '');
    d.style.left = L + '%'; d.style.top = T + '%';
    d.onclick = () => selectLoc(i);
    $('locMap').appendChild(d);
    if (i === locSel){
      const lb = document.createElement('div');
      lb.className = 'mkl'; lb.textContent = it.name;
      lb.style.left = L + '%'; lb.style.top = T + '%';
      if (T < 32) lb.style.transform = 'translate(-50%,60%)';
      $('locMap').appendChild(lb);
    }
  });
}

/* ── 선택 ── */
function selectLoc(i){
  const it = locResults[i];
  if (!it) return;
  locSel = i;
  $('locList').querySelectorAll('.lrow').forEach((b, k) => b.classList.toggle('on', k === i));
  $('pickName').textContent = it.name;
  $('pickArea').textContent = (it.roadAddress || it.address || '') + `  ·  ${it.lat.toFixed(5)}, ${it.lng.toFixed(5)}`;
  $('locConfirm').style.display = '';

  if (mapObj){
    mapMarkers.forEach((mk, k) => mk.setZIndex(k === i ? 100 : 1));
    mapObj.panTo(new naver.maps.LatLng(it.lat, it.lng));
    if (mapInfo) mapInfo.close();
    mapInfo = new naver.maps.InfoWindow({
      content: '<div style="padding:6px 10px;font-size:12px;font-weight:700;">' + esc(it.name) + '</div>',
    });
    mapInfo.open(mapObj, mapMarkers[i]);
  } else {
    drawMarkers();
  }
}
function clearLocSel(){
  locSel = -1;
  $('locConfirm').style.display = 'none';
  if (mapInfo){ mapInfo.close(); mapInfo = null; }
}

function openLocSearch(ret){
  if (!DATA) return;
  locReturn = ret;
  closeSheet();
  document.querySelectorAll('.screen').forEach(x => x.classList.remove('active'));
  $('scLoc').classList.add('active');
  $('locQ').value = '';
  doLocSearch('');
  $('pbody').scrollTop = 0;
  setTimeout(() => { $('locQ').focus(); if (mapObj) naver.maps.Event.trigger(mapObj, 'resize'); }, 60);
}
function closeLocSearch(){ show(locReturn); }

function confirmLoc(){
  const it = locResults[locSel];
  if (!it) return;
  setLoc({ name:it.name, area:it.roadAddress || it.address || '', lat:it.lat, lng:it.lng });
  show(locReturn);
  if (locReturn === 3) liveUpdate();
  toast(it.name + ' 으로 설정했어요');
}

/* 조건 변경 → 로딩 없이 즉시 재계산 */
function liveUpdate(){
  if (!DATA) return;
  clearLoad();
  const timeStr = $('inTime').value || '14:00';
  const wd = dWd(curDate);
  const hour = parseInt(timeStr, 10);
  const minute = +timeStr.slice(3, 5) || 0;

  recs = poolAt(wd, hour, minute).slice(0, 3);
  renderResult($('inLoc').value, timeStr, wd, hour);

  ['resTitle', 'cards'].forEach(id => {
    const el = $(id);
    el.classList.remove('flash');
    void el.offsetWidth;
    el.classList.add('flash');
  });
}

/* ================= 화면 전환 ================= */
function show(n){
  [1,2,3,4].forEach(i => $('sc'+i).classList.toggle('active', i === n));
  $('scLoc').classList.remove('active');
  document.querySelectorAll('.vs').forEach(v => {
    const i = +v.dataset.s;
    v.classList.toggle('on', i === n);
    v.classList.toggle('done', i < n);
    v.querySelector('.dot').innerHTML = i < n
      ? '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#052E1F" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>'
      : i;
  });
  $('pbody').scrollTop = 0;
}
function goHome(){ clearLoad(); closeSheet(); show(1); }

/* ================= 예측 ================= */
function clearLoad(){ loadTimers.forEach(clearTimeout); loadTimers = []; }

/* 조건에 맞는 충전소를 특정 시각 기준으로 계산 */
function poolAt(wd, hour, minute){
  const org = origin || DATA.locations[0];
  const th = DATA.meta.thresholds;
  let pool = DATA.stations.filter(s => s.lat != null && s.lng != null);

  if (kwFilter === 50)  pool = pool.filter(s => s.kw >= 50  && s.kw < 100);
  if (kwFilter === 100) pool = pool.filter(s => s.kw >= 100 && s.kw < 200);
  if (kwFilter === 200) pool = pool.filter(s => s.kw >= 200);

  const out = [];
  for (const s0 of pool){
    const d = haversine(org, s0);
    if (d > radius) continue;
    const cur = s0.usage[wd][hour];
    const nxt = s0.usage[wd][(hour + 1) % 24];
    const rate = cur + (nxt - cur) * (minute / 60);   /* 분 단위 선형 보간 */
    const grade = rate < th.free_max ? 0 : rate < th.warn_max ? 1 : 2;
    out.push({ n: s0.name, d, c: s0.chargers, kw: s0.kw, rate, grade, lat: s0.lat, lng: s0.lng });
  }
  out.sort((a, b) => (a.grade - b.grade) || (a.rate - b.rate) || (a.d - b.d));
  return out;
}

function predict(){
  if (!DATA){ toast('데이터가 로드되지 않았습니다. index.html 을 압축 해제한 폴더에서 열어주세요.'); return; }
  const loc = $('inLoc').value, timeStr = $('inTime').value;
  const wd = dWd(curDate);
  const hour = parseInt(timeStr, 10);
  const minute = +timeStr.slice(3, 5) || 0;

  recs = poolAt(wd, hour, minute).slice(0, 3);
  runLoading(timeStr, () => { renderResult(loc, timeStr, wd, hour); show(3); });
}

/* ================= 결과 화면 지도 =================
   예정 위치(파랑)와 추천 충전소 Top 3(초록 번호)을 함께 표시합니다.
   지도 키가 있으면 NAVER Dynamic Map, 없으면 대체 이미지로 그립니다. */
let resMapObj = null, resMarkers = [];

function drawResMap(){
  const pts = recs.filter(r => r.lat != null).map((r, i) => ({ lat:r.lat, lng:r.lng, n:i + 1, name:r.n }));
  const org = origin;

  if (MAP_SDK && window.naver && naver.maps){
    if (!resMapObj){
      $('resMapBg').style.display = 'none';
      $('resMapTag').style.display = 'none';
      const host = document.createElement('div');
      host.className = 'nmap';
      $('resMap').appendChild(host);
      resMapObj = new naver.maps.Map(host, {
        center: new naver.maps.LatLng(org ? org.lat : 33.4996, org ? org.lng : 126.5312),
        zoom: 13, scaleControl:false, mapDataControl:false,
      });
    }
    resMarkers.forEach(m => m.setMap(null));
    resMarkers = [];
    const bounds = new naver.maps.LatLngBounds();

    if (org){
      const p = new naver.maps.LatLng(org.lat, org.lng);
      resMarkers.push(new naver.maps.Marker({ position:p, map:resMapObj, title:org.name,
        icon:{ content:'<div style="width:15px;height:15px;border-radius:50%;background:#38BDF8;border:2.5px solid #0F172A;"></div>', anchor:new naver.maps.Point(9,9) } }));
      bounds.extend(p);
    }
    pts.forEach(pt => {
      const p = new naver.maps.LatLng(pt.lat, pt.lng);
      resMarkers.push(new naver.maps.Marker({ position:p, map:resMapObj, title:pt.name,
        icon:{ content:`<div style="width:23px;height:23px;border-radius:50%;background:#34D399;border:2.5px solid #0F172A;color:#052E1F;font:800 11px/19px sans-serif;text-align:center;">${pt.n}</div>`,
               anchor:new naver.maps.Point(12,12) } }));
      bounds.extend(p);
    });
    if (pts.length || org) resMapObj.fitBounds(bounds);
    naver.maps.Event.trigger(resMapObj, 'resize');
    return;
  }

  /* 대체 표시 — 좌표를 상대 위치로 환산 */
  $('resMap').querySelectorAll('.pin').forEach(e => e.remove());
  const all = org ? pts.concat([{ lat:org.lat, lng:org.lng, org:true }]) : pts;
  if (!all.length) return;
  const pad = 0.008;
  const y0 = Math.min(...all.map(p => p.lat)) - pad, y1 = Math.max(...all.map(p => p.lat)) + pad;
  const x0 = Math.min(...all.map(p => p.lng)) - pad, x1 = Math.max(...all.map(p => p.lng)) + pad;
  const place = (p, cls, label) => {
    const L = ((p.lng - x0) / (x1 - x0 || 1)) * 74 + 13;
    const T = (1 - (p.lat - y0) / (y1 - y0 || 1)) * 64 + 18;
    const d = document.createElement('div');
    d.className = 'pin' + cls;
    d.style.left = L + '%'; d.style.top = T + '%';
    d.textContent = label || '';
    d.title = p.name || '';
    $('resMap').appendChild(d);
  };
  if (org) place(org, ' org', '');
  pts.forEach(pt => place(pt, '', String(pt.n)));
}

/* ================= 로딩 ================= */
function runLoading(timeStr, done){
  clearLoad(); show(2);
  $('loadTime').textContent = timeStr;
  $('chk1t').textContent = `반경 ${radius}km 내 급속충전소 탐색`;
  $('chk2t').textContent = kwFilter === 0 ? '출력 용량 전체 조건 적용' : `${kwLabel()} 출력 용량 조건 적용`;
  [1,2,3].forEach(i => { $('chk'+i).className = 'chk'; $('chk'+i).querySelector('.ic').innerHTML = '<span class="sp"></span>'; });
  $('pbar').style.width = '0%';

  const on  = (i, w) => { $('chk'+i).classList.add('on'); $('pbar').style.width = w + '%'; };
  const fin = i => {
    $('chk'+i).classList.add('done');
    $('chk'+i).querySelector('.ic').innerHTML =
      '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#34D399" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>';
  };
  loadTimers.push(setTimeout(() => on(1, 22), 60));
  loadTimers.push(setTimeout(() => { fin(1); on(2, 52); }, 620));
  loadTimers.push(setTimeout(() => { fin(2); on(3, 82); }, 1180));
  loadTimers.push(setTimeout(() => { fin(3); $('pbar').style.width = '100%'; }, 1780));
  loadTimers.push(setTimeout(done, 2080));
}

/* ================= 추천 이유 — 혼잡도 기준 ================= */
function reasonOf(s, i){
  if (i === 0){
    return ['예정 시간대 혼잡도가 가장 낮아요',
            '주변 후보 중 혼잡도가 가장 낮아요',
            '주변이 대체로 혼잡하지만 그중 나은 편이에요'][s.grade];
  }
  const top = recs[0];
  const closer = s.d < top.d;
  const worse  = s.grade > top.grade;
  const state  = ['여유로워요', '조금 붐벼요', '혼잡해요'][s.grade];
  if (worse) return (closer ? '더 가깝지만 ' : '거리도 멀고 ') + state;
  return closer ? '더 가깝고 혼잡도도 비슷해요' : '조금 멀지만 혼잡도는 비슷해요';
}

/* ================= 결과 ================= */
function renderResult(loc, timeStr, wd, hour){
  $('resTitle').innerHTML = recs.length
    ? (recs[0].grade === 0 ? '이 시간엔 <b>여유 있는 곳</b>이 있어요'
                           : '조건 내 충전소가 <b>다소 붐벼요</b>')
    : '조건에 맞는 충전소가 없어요';

  drawResMap();

  const box = $('cards');
  box.innerHTML = '';
  if (!recs.length){
    box.innerHTML = `<div class="empty">
      <h4>조건에 맞는 급속충전소가 없어요</h4>
      <p>검색 반경을 넓히거나<br>출력 용량 조건을 ‘전체’로 바꿔보세요.</p>
    </div>`;
    return;
  }

  recs.forEach((s, i) => {
    const g = G[s.grade];
    const el = document.createElement('div');
    el.className = 'pc rc' + (i === 0 ? ' best' : '');
    el.innerHTML = `
      <div class="rc-top">
        <div class="rk">${i + 1}</div>
        <span class="badge ${g.cls}"><i></i>${g.t}</span>
      </div>
      <div class="name-row">
        <span class="rc-name">${s.n}</span>
        <button class="btn-cp" title="이름 복사">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 00-2-2H6a2 2 0 00-2 2v8a2 2 0 002 2h2"/></svg>
        </button>
      </div>
      <div class="rc-stats">
        <div class="st"><div class="l">거리</div><div class="v mono">${s.d.toFixed(1)}<small>km</small></div></div>
        <div class="st"><div class="l">이용 가능 <span class="est">예측</span></div><div class="v mono">${freeOf(s)}<small>/${s.c}대</small></div></div>
      </div>
      <div class="why">${reasonOf(s, i)}</div>
      <button class="btn-route">이 충전소로 경로 보기
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
      </button>`;
    el.querySelector('.btn-route').onclick = () => openRoute(s, i);
    el.querySelector('.btn-cp').onclick = e => { e.stopPropagation(); copyName(s.n); };
    box.appendChild(el);
  });
}
function kwLabel(){
  if (kwFilter === 0) return '전체 kW';
  if (kwFilter === 200) return '200kW 이상';
  return `${kwFilter}–${kwFilter === 50 ? 100 : 200}kW`;
}

/* ================= 경로 ================= */
function openRoute(s, i){
  sel = s;
  const g = G[s.grade];
  $('detBadge').className = 'badge ' + g.cls;
  $('detBadge').innerHTML = '<i></i>' + g.t;
  $('detName').textContent = s.n;
  $('detDist').textContent = s.d.toFixed(1) + 'km';
  $('detTime').textContent = '약 ' + Math.max(2, Math.round(s.d * 2.6)) + '분';

  /* 예상 이용 가능 대수 = 전체 대수 × (1 − 예상 점유율)
     점유율은 과거 이용 패턴에서 계산한 값이므로 어디까지나 추정치입니다. */
  $('detN').innerHTML = `${freeOf(s)}<small>/${s.c}대</small>`;

  $('detBasic').innerHTML =
    `<span><b>24시간</b> 운영</span><span class="sep">·</span>` +
    `<span>전체 <b>${s.c}대</b></span><span class="sep">·</span>` +
    `<span><b>${s.kw}kW</b></span>`;
  $('mapFrom').textContent = $('inLoc').value;

  /* 목적지 좌표를 상태로 보관 — Directions 5 의 goal 로 그대로 사용합니다.
     getRouteParams() → { start:'경도,위도', goal:'경도,위도' } */
  window.MIRIBOM_DESTINATION = { name: s.n, lat: s.lat, lng: s.lng };

  show(4);
}

/* ================= 유틸 ================= */
function copyName(n){
  (navigator.clipboard ? navigator.clipboard.writeText(n) : Promise.reject())
    .then(() => toast('충전소 이름을 복사했어요 · ' + n))
    .catch(() => toast('이 환경에서는 복사를 지원하지 않아요'));
}
function copyDet(){ if (sel) copyName(sel.n); }
let tt = null;
function toast(msg){
  const t = $('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(tt); tt = setTimeout(() => t.classList.remove('show'), 2300);
}

/* ================= 자동 시연 ================= */
function toggleAuto(){
  const btn = $('autoBtn');
  if (autoTimer){ stopAuto(); return; }
  btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg> 시연 중지';
  btn.classList.add('stop');

  /* 시연은 실제 요일을 골라 보여줍니다 (다음 수요일 / 다음 토요일) */
  const nextDow = dow => { const d = new Date(TODAY); d.setDate(d.getDate() + ((dow - d.getDay() + 7) % 7 || 7)); return d; };
  const WED = nextDow(3), SAT = nextDow(6);

  const steps = [
    { d: 900,  f: () => { show(1); setLocByName('성산일출봉'); $('inTime').value = '14:00'; setDate(WED);
                          toast(`① ${WED.getMonth()+1}월 ${WED.getDate()}일 (수) 14시, 성산일출봉 5km 조건으로 검색합니다`); } },
    { d: 2600, f: () => predict() },
    { d: 3400, f: () => toast('② 가장 가까운 곳이 아니라, 그 시간에 여유로운 충전소를 추천해요') },
    { d: 3000, f: () => { if (recs[0]) openRoute(recs[0], 0); toast('③ 선택한 충전소까지 거리와 소요 시간을 확인합니다'); } },
    { d: 3000, f: () => { show(1); setDate(SAT); $('inTime').value = '18:00'; setLocByName('제주국제공항');
                          toast(`④ 이번엔 ${SAT.getMonth()+1}월 ${SAT.getDate()}일 (토) 저녁, 제주공항으로 바꿔볼게요`); } },
    { d: 2600, f: () => predict() },
    { d: 3400, f: () => toast('⑤ 같은 조건도 요일·시간에 따라 등급과 순위가 달라집니다') },
    { d: 3600, f: () => toast('⑥ 붐비는 시간이면 "몇 시로 옮기면 여유로운지"까지 제안합니다') },
    { d: 1200, f: () => { stopAuto(); toast('시연이 끝났어요. 직접 조건을 바꿔보세요'); } },
  ];
  let i = 0;
  const run = () => { if (i >= steps.length) return; const st = steps[i++]; st.f(); autoTimer = setTimeout(run, st.d); };
  run();
}
function stopAuto(){
  clearTimeout(autoTimer); autoTimer = null;
  const btn = $('autoBtn');
  btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg> 자동 시연';
  btn.classList.remove('stop');
}

/* ================= 시작 ================= */
function boot(data){
  DATA = data;
  initSelects();
  initMapKey();
  const m = DATA.meta;
  const note = document.querySelector('.left-note');
  if (note){
    note.innerHTML =
      `실제 <b style="color:var(--body)">${m.source}</b> 데이터를 전처리해 만든 결과입니다. ` +
      `분석 기간 ${m.period} · 제주 급속충전소 ${m.station_count}곳. ` +
      `혼잡도는 ${m.metric} 로 계산했으며, 여유·주의·혼잡 경계는 ` +
      `${m.thresholds.free_max} / ${m.thresholds.warn_max} 입니다. ` +
      `예상 혼잡도는 과거 이용 패턴 기반 추정치이며 실제 이용 가능 여부를 보장하지 않습니다.`;
  }
  show(1);
}

if (window.MIRIBOM_DATA){
  boot(window.MIRIBOM_DATA);
} else {
  fetch('stations.json').then(r => r.json()).then(boot).catch(() => {
    document.getElementById('scDynamic').innerHTML =
      '<section class="screen"><div class="empty"><h4>데이터 파일을 찾을 수 없어요</h4>' +
      '<p>build_stations.py 를 먼저 실행해<br>stations.data.js 를 만들어 주세요.</p></div></section>';
    document.getElementById('sc1').style.display = 'none';
  });
}
