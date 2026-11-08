<script>
  let CAN_EDIT_PROFILE = false;
  let AUTH_USER = null;
  let AUTH_TITLES = [];
  let AUTH_SELECTED_TITLE = null;
  let CURRENT_USER_PROFILE = null;

  const CLEAR_PIXEL = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==';

  const $  = (sel)=>document.querySelector(sel);
  const $$ = (sel)=>Array.from(document.querySelectorAll(sel));
  const show = (el,on=true)=> el && (el.style.display = on ? "" : "none");
  function showMsg(el, text, type){
    if(!el) return;
    el.className = "msg " + (type||"info");
    el.textContent = text;
    el.style.display = text ? "block" : "none";
  }

  function setActiveTab(name){
    $$(".tabbtn").forEach(b=>b.classList.toggle("active", name && b.dataset.tab===name));
    $$("main section").forEach(s=>s.classList.toggle("active", s.id===name));

    if(name === "profile"){
      const useBtn = $("#btnUseDiscordAvatar");
      const refreshBtn = $("#btnRefreshDiscordAvatar");
      const clearBtn = $("#btnClearPfp");

      if(useBtn) useBtn.disabled = !CAN_EDIT_PROFILE;
      if(refreshBtn) refreshBtn.disabled = !CAN_EDIT_PROFILE;
      if(clearBtn) clearBtn.disabled = !CAN_EDIT_PROFILE;

      show($("#profileGate"), !CAN_EDIT_PROFILE);

      if(CAN_EDIT_PROFILE){
        refreshDiscordAvatarPreview({ force: true, silent: false });
        fetchAuthTitles({ silent: true });
      } else {
        AUTH_TITLES = [];
        AUTH_SELECTED_TITLE = null;
        renderTitleSelector();
        renderSelectedPreview('');
      }
    }
  }

  function setActiveUser(){
    $$(".tabbtn").forEach(b=>b.classList.remove("active"));
    $$("main section").forEach(s=>s.classList.remove("active"));
    $("#userProfile").classList.add("active");
  }

  $("#tabs").addEventListener("click",(e)=>{
    const btn = e.target.closest(".tabbtn");
    if(!btn) return;
    if(btn.hasAttribute('disabled')) return;
    setActiveTab(btn.dataset.tab);
  });

  document.getElementById('btnConnectDiscord').addEventListener('click', ()=>{
    window.location.href = `${AUTH02_URL}`;
  });

  document.getElementById('btnResetAuth').addEventListener('click', ()=>{
    localStorage.removeItem('pjsk_verified');
    localStorage.removeItem('pjsk_user');
    CAN_EDIT_PROFILE = false;
    AUTH_USER = null;
    updateAuthUI();
    renderSelectedPreview('');
    showMsg($("#homeMsg"), 'Disconnected from Discord', 'info');
  });

  function checkVerifiedFromUrl(){
    const url = new URL(window.location.href);
    if(url.searchParams.get('verified') === '1'){
      localStorage.setItem('pjsk_verified','1');
      url.searchParams.delete('verified');
      history.replaceState({}, '', url.toString());
      return true;
    }
    return false;
  }

  function isLocallyVerified(){
    return localStorage.getItem('pjsk_verified') === '1';
  }

  function resolveDiscordAvatarUrl(u){
    if(!u) return '';

    // Some backends return the Discord user nested inside a wrapper.
    const source = u.user || u.profile || u.discordUser || u.discord_user || u;

    const id = source.id || source.user_id || source.discordId || source.discord_id || null;

    const direct =
      source.avatarUrl ||
      source.avatar_url ||
      source.discordAvatarUrl ||
      source.discord_avatar_url ||
      source.profile_image_url ||
      source.image_url ||
      source.picture ||
      source.pfp ||
      '';

    // Accept only real custom Discord avatar URLs.
    // Reject cdn.discordapp.com/embed/avatars/... because that is Discord's generic default icon.
    if(isDiscordAvatarUrl(direct)){
      return String(direct).trim();
    }

    const avatarHash =
      source.avatar_hash ||
      source.avatarHash ||
      source.discordAvatarHash ||
      source.discord_avatar_hash ||
      source.avatar ||
      '';

    // If Discord returns avatar:null, there is no custom avatar in the OAuth payload.
    // Do NOT build an embed avatar fallback here; use DEFAULT_PFP instead.
    if(!id || !avatarHash || /^https?:\/\//i.test(String(avatarHash))){
      return '';
    }

    const cleanHash = String(avatarHash).trim();
    const ext = cleanHash.startsWith('a_') ? 'gif' : 'png';
    return `https://cdn.discordapp.com/avatars/${id}/${cleanHash}.${ext}?size=256`;
  }

  async function fetchAuthProfile(options = {}){
    const { force = false } = options;

    if(!force){
      const cached = localStorage.getItem('pjsk_user');
      if(cached){
        try { AUTH_USER = JSON.parse(cached); } catch {}
      }

      if(AUTH_USER?.id && AUTH_USER?.nameLower && AUTH_USER?.avatarUrl){
        renderDiscordUserInfo();
        return true;
      }
    }

    try {
      const res = await fetch(`${AUTH02_URL}/me`, { credentials:'include', cache:'no-store' });
      if(res.ok){
        const u = await res.json();
        if(u){
          const id = u.id || u.user_id || u.discordId || u.discord_id || null;
          const username = u.username || u.global_name || u.displayName || u.name || u.discordName || '';
          const avatarUrl = resolveDiscordAvatarUrl(u);

          if(id || username){
            AUTH_USER = {
              id,
              username,
              nameLower: String(username || '').toLowerCase(),
              avatarUrl,
            };
            localStorage.setItem('pjsk_user', JSON.stringify(AUTH_USER));
            renderDiscordUserInfo();
            return true;
          }
        }
      } else if (res.status === 401) {
        localStorage.removeItem('pjsk_verified');
        localStorage.removeItem('pjsk_user');
        AUTH_USER = null;
      }
    } catch(e) {
      console.error('Error en fetchAuthProfile:', e);
    }

    renderDiscordUserInfo();
    return false;
  }

  function updateAuthUI(){
    CAN_EDIT_PROFILE = isLocallyVerified();
    const tab = document.getElementById('tabProfile');

    if(CAN_EDIT_PROFILE){
      tab.removeAttribute('disabled');
      showMsg($("#homeMsg"), 'Connected with Discord. You can now use your Discord avatar as your PFP.', 'ok');
      fetchAuthProfile({ force: true }).then(()=>{
        fetchAuthTitles({ silent: true });
        if(!AUTH_USER){
          showMsg($("#homeMsg"), 'Connected, but /auth02/me did not return the Discord profile yet.', 'info');
        }
      });
    }else{
      tab.setAttribute('disabled','disabled');
      show($("#profileGate"), true);
    }
  }

  (async function initAuth02Gate(){
    const viaUrl = checkVerifiedFromUrl();
    if(!viaUrl && isLocallyVerified()){
      showMsg($("#homeMsg"), 'Session Verified (persistida).', 'ok');
    }
    updateAuthUI();
    if(isLocallyVerified()){
      await fetchAuthProfile({ force: true });
      await fetchAuthTitles({ silent: true });
    }
  })();

  function renderSelectedPreview(url){
    const pv = $('#pfpPreview');
    if (pv){
      pv.src = url || DEFAULT_PFP;
      applyRankBorderToElement(pv, getAuthUserRank(), { glowAlpha: .35, glowSize: 12 });
    }
  }

  function renderDiscordUserInfo(){
    const title = $('#discordAvatarTitle');
    const subtitle = $('#discordAvatarSubtitle');
    const avatarUrl = AUTH_USER?.avatarUrl || '';

    renderSelectedPreview(avatarUrl);

    if(title){
      title.textContent = AUTH_USER?.username
        ? `Discord Avatar · ${AUTH_USER.username}`
        : 'Discord Avatar';
    }

    if(subtitle){
      if(!CAN_EDIT_PROFILE){
        subtitle.textContent = 'Connect with Discord to use your Discord profile picture.';
      } else if(avatarUrl){
        subtitle.textContent = 'This avatar comes directly from your Discord profile. Click Use Discord Avatar to save it as your PFP.';
      } else {
        subtitle.textContent = 'Could not read a Discord avatar from /auth02/me. Check the backend response.';
      }
    }

    renderProfileSelectedTitle();
  }

  function renderTitleSelector(){
    const select = $('#seasonTitleSelect');
    const help = $('#seasonTitleHelp');
    const saveBtn = $('#btnSaveSeasonTitle');
    const clearBtn = $('#btnClearSeasonTitle');

    renderProfileSelectedTitle();

    if(!select) return;

    const available = Array.isArray(AUTH_TITLES) ? AUTH_TITLES.map(normalizeSeasonTitle).filter(Boolean) : [];
    const selected = normalizeSeasonTitle(AUTH_SELECTED_TITLE);

    select.innerHTML = '<option value="">No title</option>';
    available.forEach(t => {
      const opt = document.createElement('option');
      opt.value = t.key;
      opt.textContent = t.label;
      if(selected && selected.key === t.key) opt.selected = true;
      select.appendChild(opt);
    });

    const canUse = CAN_EDIT_PROFILE && available.length > 0;
    select.disabled = !canUse;
    if(saveBtn) saveBtn.disabled = !canUse;
    if(clearBtn) clearBtn.disabled = !CAN_EDIT_PROFILE;

    if(help){
      if(!CAN_EDIT_PROFILE){
        help.textContent = 'Connect with Discord to manage your season titles.';
      } else if(available.length){
        help.textContent = 'Select one of the season titles you earned by finishing Top 1, Top 2 or Top 3.';
      } else {
        help.textContent = 'No season title available yet. Titles unlock when you finish Top 1, Top 2 or Top 3 in a completed season.';
      }
    }
  }

  async function fetchAuthTitles(options = {}){
    const { silent = true } = options;
    if(!CAN_EDIT_PROFILE){
      AUTH_TITLES = [];
      AUTH_SELECTED_TITLE = null;
      renderTitleSelector();
      return false;
    }

    try{
      const res = await fetch(`${AUTH02_URL}/titles`, { credentials:'include', cache:'no-store' });
      if(!res.ok){
        if(!silent) showMsg($('#pfpInfo'), 'Could not load season titles.', 'err');
        AUTH_TITLES = [];
        AUTH_SELECTED_TITLE = null;
        renderTitleSelector();
        return false;
      }

      const data = await res.json();
      AUTH_TITLES = Array.isArray(data.available) ? data.available : [];
      AUTH_SELECTED_TITLE = data.selected || null;
      renderTitleSelector();
      return true;
    }catch(e){
      console.warn('Error loading titles:', e);
      AUTH_TITLES = [];
      AUTH_SELECTED_TITLE = null;
      renderTitleSelector();
      return false;
    }
  }

  async function saveSelectedSeasonTitle(titleKey){
    if(!CAN_EDIT_PROFILE){
      alert('You must be connected with Discord');
      return false;
    }

    try{
      const r = await fetch(`${AUTH02_URL}/title`, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        credentials:'include',
        body: JSON.stringify({ titleKey: titleKey || '' })
      });

      if(!r.ok){
        const t = await r.text();
        throw new Error(t || ('HTTP ' + r.status));
      }

      const data = await r.json();
      AUTH_SELECTED_TITLE = data.selected || null;
      await fetchAuthTitles({ silent: true });

      if(window.__lbApp && typeof window.__lbApp.fetchPlayers === 'function'){
        await window.__lbApp.fetchPlayers();
      }

      showMsg($('#pfpInfo'), titleKey ? 'Season title saved.' : 'Season title cleared.', 'ok');
      return true;
    }catch(e){
      console.warn('Error saving season title:', e);
      showMsg($('#pfpInfo'), 'No se pudo guardar el title. Revisa /auth02/title en el backend.', 'err');
      return false;
    }
  }

  async function refreshDiscordAvatarPreview(options = {}){
    const { force = true, silent = false } = options;
    if(!CAN_EDIT_PROFILE){
      renderDiscordUserInfo();
      return false;
    }

    if(!silent) showMsg($('#pfpInfo'), 'Loading Discord avatar…', 'info');

    const ok = await fetchAuthProfile({ force });
    const avatarUrl = AUTH_USER?.avatarUrl || '';

    if(!ok || !avatarUrl){
      showMsg($('#pfpInfo'), 'No real Discord avatar found. Make sure /auth02/me returns the Discord avatar hash or avatarUrl.', 'err');
      return false;
    }

    if(!silent) showMsg($('#pfpInfo'), 'Discord avatar loaded.', 'ok');
    return true;
  }

  async function setPfpUrl(url, options = {}){
    const { silent = false } = options;

    if(!CAN_EDIT_PROFILE){
      alert('You must be connected with Discord');
      return false;
    }

    renderSelectedPreview(url);

    try{
      const payload = { url };
      if (AUTH_USER?.nameLower) payload.nameLower = AUTH_USER.nameLower;
      if (AUTH_USER?.id)       payload.userId    = AUTH_USER.id;

      const r = await fetch(`${AUTH02_URL}/set-pfp`, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        credentials:'include',
        body: JSON.stringify(payload)
      });

      if(!r.ok){
        const t = await r.text();
        throw new Error(t||('HTTP '+r.status));
      }

      if(window.__lbApp && typeof window.__lbApp.fetchPfpMap === 'function'){
        await window.__lbApp.fetchPfpMap();
      }

      if(!silent){
        showMsg($('#pfpInfo'), url ? 'Discord avatar saved as PFP.' : 'PFP removed. Default avatar will be shown.', 'ok');
      }

      return true;
    }catch(e){
      console.warn(e);
      if(!silent){
        showMsg($('#pfpInfo'), 'No se pudo actualizar el PFP. Revisa /auth02/set-pfp en el backend.', 'err');
      }
      return false;
    }
  }

  async function useDiscordAvatarAsPfp(){
    const ok = await refreshDiscordAvatarPreview({ force: true, silent: true });
    const avatarUrl = AUTH_USER?.avatarUrl || '';

    if(!ok || !avatarUrl){
      showMsg($('#pfpInfo'), 'No real Discord avatar found. Check /auth02/me backend response.', 'err');
      return;
    }

    await setPfpUrl(avatarUrl);
  }

  document.getElementById('btnUseDiscordAvatar').addEventListener('click', useDiscordAvatarAsPfp);

  document.getElementById('btnRefreshDiscordAvatar').addEventListener('click', async ()=>{
    await refreshDiscordAvatarPreview({ force: true, silent: false });
  });

  document.getElementById('btnClearPfp').addEventListener('click', async ()=>{
    await setPfpUrl('');
    renderSelectedPreview(DEFAULT_PFP);

    if(window.__lbApp && typeof window.__lbApp.fetchPfpMap === 'function'){
      await window.__lbApp.fetchPfpMap();
    }
  });


  const seasonTitleSelect = document.getElementById('seasonTitleSelect');
  const saveSeasonTitleBtn = document.getElementById('btnSaveSeasonTitle');
  const clearSeasonTitleBtn = document.getElementById('btnClearSeasonTitle');

  if(saveSeasonTitleBtn){
    saveSeasonTitleBtn.addEventListener('click', async ()=>{
      await saveSelectedSeasonTitle(seasonTitleSelect ? seasonTitleSelect.value : '');
    });
  }

  if(clearSeasonTitleBtn){
    clearSeasonTitleBtn.addEventListener('click', async ()=>{
      if(seasonTitleSelect) seasonTitleSelect.value = '';
      await saveSelectedSeasonTitle('');
    });
  }

  const backBtn = document.getElementById('backToLb');
  backBtn.addEventListener('click', (e)=>{ e.preventDefault(); setActiveTab('leaderboard'); });

  const btnUserMatches = document.getElementById('btnUserMatches');
  btnUserMatches.addEventListener('click',(e)=>{
    e.preventDefault();
    const el = document.getElementById('userMatches');
    if(el) el.scrollIntoView({behavior:'smooth', block:'start'});
  });

  function safeNumHist(val, fallback = 0){
    const n = Number(val);
    if (val === null || val === undefined || Number.isNaN(n)) return fallback;
    return n;
  }

  function last10TotalHist(r){
    return safeNumHist(r.last10_wins, 0) + safeNumHist(r.last10_losses, 0);
  }

  function fmtSignedHist(delta){
    const n = Number(delta || 0);
    if (!Number.isFinite(n)) return '+0';
    return (n >= 0 ? '+' : '') + n;
  }

  function formatMatchForUser(m){
    const low = m.gap_low ?? m.lv_low ?? m.gapLvLow ?? m.min_gap ?? null;
    const high = m.gap_high ?? m.lv_high ?? m.gapLvHigh ?? m.max_gap ?? null;
    const roomMmr = (m.room_mmr != null) ? m.room_mmr : '?';

    const headerLv = (low != null && high != null)
      ? `LV(${low}-${high})`
      : 'LV(?)';

    const header = `POS - PLAYER - ${headerLv}, ROOM MMR ${roomMmr}`;
    const lines = [header];

    if (Array.isArray(m.entries)){
      m.entries.forEach((e, idx)=>{
        const pos   = e.position ?? (idx + 1);
        const name  = e.name || `ID ${e.user_id || e.id}`;

        const ccField = resolveCountryFromLb(e);
        const flagEmoji = ccField ? countryToFlagEmoji(ccField) : '';
        const cc = flagEmoji ? ` ${flagEmoji}` : '';

        let stats = '';
        const hasNewStats =
          e.s_perfect != null || e.s_great != null || e.s_good != null ||
          e.s_bad != null || e.s_miss != null || e.pts != null || e.points != null;

        if (hasNewStats){
          const p   = Number(e.s_perfect ?? 0);
          const g1  = Number(e.s_great   ?? 0);
          const g2  = Number(e.s_good    ?? 0);
          const b   = Number(e.s_bad     ?? 0);
          const m_  = Number(e.s_miss    ?? 0);

          let pts = e.pts ?? e.points ?? e.score ?? e.total_score ?? null;
          if (pts == null){
            pts = p*4 + g1*3 + g2*2 + b*1 + m_*0;
          }
          stats = `${p},${g1},${g2},${b},${m_} | ${pts}`;
        } else if (Array.isArray(e.stats)){
          stats = e.stats.join(',');
        } else {
          stats = e.stats_text || '';
        }

        const before = Number(e.mmr_before ?? 0);
        const delta  = Number(e.mmr_delta ?? 0);
        const after  = Number(e.mmr_after ?? (before + delta));

        const prevLabel = e.was_placement
          ? `PLACEMENT(${before})`
          : `${before}`;

        const deltaStr = (delta >= 0 ? `+${delta}` : `${delta}`);

        lines.push(
          `${pos}. ${name}${cc} (${stats}) (${prevLabel}) ${deltaStr} = ${after}`
        );
      });
    }

    return lines.join('\n');
  }

  async function loadUserMatches(p){
    const body = document.getElementById('userMatchesBody');
    if(!body) return;

    body.innerHTML = '<div class="help">Loading matches…</div>';

    const targetId = String(p.id);

    const normalizeMatches = (data) => {
      const arr = Array.isArray(data) ? data : (Array.isArray(data?.matches) ? data.matches : []);
      return arr.map(m => ({
        ...m,
        room_mmr:   m.room_mmr   ?? m.avg_mmr ?? m.avgMmr ?? null,
        created_at: m.created_at ?? m.played_at ?? m.playedAt ?? null,
        entries: Array.isArray(m.entries)
          ? m.entries
          : (Array.isArray(m.players) ? m.players : []),
      }));
    };

    const entryId = (e) => {
      return e?.user_id ?? e?.id ?? e?.userId ?? e?.discord_id ?? e?.discordId ?? e?.discordID ?? null;
    };

    const safeFetchJson = async (url) => {
      const res = await fetch(url, { cache:'no-store' });
      if(!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    };

    let matches = [];

    try{
      const data = await safeFetchJson(`${API_BASE}/api/matches?user_id=${encodeURIComponent(targetId)}&limit=200`);
      matches = normalizeMatches(data);
    }catch(e){
      matches = [];
    }

    if(!matches.length){
      const fallbackUrls = [
        `${API_BASE}/api/matches?limit=800`,
        `${API_BASE}/api/matches?limit=500`,
        `${API_BASE}/api/matches`,
      ];

      let all = [];
      for (const url of fallbackUrls){
        try{
          const dataAll = await safeFetchJson(url);
          all = normalizeMatches(dataAll);
          if(all.length) break;
        }catch(e){}
      }

      matches = all.filter(m =>
        Array.isArray(m.entries) &&
        m.entries.some(e => String(entryId(e)) === targetId)
      );
    }

    if(!matches.length){
      body.innerHTML = `<div class="help">This player doesnt have matches yet.</div>`;
      return;
    }

    matches.sort((a,b)=>{
      const ad = a.created_at ? new Date(a.created_at).getTime() : 0;
      const bd = b.created_at ? new Date(b.created_at).getTime() : 0;
      if (bd !== ad) return bd - ad;
      return Number(b.result_id ?? 0) - Number(a.result_id ?? 0);
    });

    const last10 = matches.slice(0, 10);

    body.innerHTML = last10.map(m => {
      const headerParts = [];
      headerParts.push(`#${m.result_id}`);
      if(m.season_name) headerParts.push(m.season_name);

      const low = m.gap_low ?? m.lv_low ?? m.gapLvLow ?? m.min_gap ?? null;
      const high = m.gap_high ?? m.lv_high ?? m.gapLvHigh ?? m.max_gap ?? null;
      if(low != null && high != null) headerParts.push(`LV(${low}-${high})`);

      if(m.room_mmr != null) headerParts.push(`ROOM MMR ${m.room_mmr}`);

      if(m.created_at){
        try{ headerParts.push(new Date(m.created_at).toLocaleString()); }catch{}
      }

      const headerText = headerParts.join(' · ');
      return `
        <div class="step" style="flex-direction:column;align-items:stretch">
          <div style="font-weight:700;margin-bottom:4px">${headerText}</div>
          <pre class="mono-block">${formatMatchForUser(m)}</pre>
        </div>
      `;
    }).join('');
  }

  function isCurrentSeasonRow(row){
    if (!row) return false;
    if (row.is_current_season === true || row.current_season === true || row.season_is_current === true) return true;

    if (window.__lbApp && typeof window.__lbApp.isCurrentSeasonRecord === 'function') {
      const seasonId = row.seasonId ?? row.season_id ?? row.selectedSeasonId ?? row.selected_season_id ?? null;
      if (seasonId != null) {
        const season = (window.__lbApp.seasons || []).find(s => String(s.id) === String(seasonId));
        if (season) return window.__lbApp.isCurrentSeasonRecord(season);
      }
      if (typeof window.__lbApp.isCurrentSeasonSelected === 'function' && (row.seasonId == null && row.season_id == null)) {
        return window.__lbApp.isCurrentSeasonSelected();
      }
    }

    return false;
  }

  function isSeasonPlacementPlayer(p){
    if (!p) return false;
    if (p.is_season_placement === true) return true;

    const raw = String(p.season_status ?? p.seasonStatus ?? '').trim().toLowerCase();
    if (raw === 'season placement') return true;

    const matchesPlayed = Number(p.matchesPlayed ?? p.matches_played ?? 0);
    const rank = String(p.rank ?? '').trim();
    const explicitZero = Number(p.visible_mmr ?? p.display_mmr ?? NaN);
    const hasVisibleZero = Number.isFinite(explicitZero) && explicitZero === 0;

    if (hasVisibleZero && matchesPlayed === 0) return true;

    if (!isCurrentSeasonRow(p)) return false;

    return matchesPlayed === 0 && rank !== '' && rank !== 'Placement';
  }

  function clampMmrForDisplay(p){
    if (!p) return 0;

    if (p.visible_mmr != null) return Number(p.visible_mmr ?? 0);
    if (p.display_mmr != null) return Number(p.display_mmr ?? 0);

    if (isSeasonPlacementPlayer(p) && Number(p.matchesPlayed ?? p.matches_played ?? 0) === 0) return 0;
    return Number(p.mmr ?? 0);
  }

  function clampPeakMmrForDisplay(p){
    if (!p) return 0;

    if (p.visible_peak_mmr != null) return Number(p.visible_peak_mmr ?? 0);
    if (p.display_peak_mmr != null) return Number(p.display_peak_mmr ?? 0);

    if (isSeasonPlacementPlayer(p) && Number(p.matchesPlayed ?? p.matches_played ?? 0) === 0) return 0;
    return Number(p.peakMmr ?? p.peak_mmr ?? p.mmr ?? 0);
  }

  function profileRankFromMmr(mmr){
    const lb = window.__lbApp;
    if (lb && typeof lb.rankFromMmr === 'function') return lb.rankFromMmr(mmr);

    const n = Number(mmr ?? 0);
    if (n >= 1000) return 'Legend';
    if (n >= 950)  return 'DivineStar';
    if (n >= 900)  return 'Star';
    if (n >= 850)  return 'GrandChampion';
    if (n >= 800)  return 'Champion';
    if (n >= 750)  return 'MasterUp';
    if (n >= 700)  return 'Master';
    if (n >= 650)  return 'CrystalUp';
    if (n >= 600)  return 'Crystal';
    if (n >= 550)  return 'DiamondUp';
    if (n >= 500)  return 'Diamond';
    if (n >= 450)  return 'PlatinumUp';
    if (n >= 400)  return 'Platinum';
    if (n >= 350)  return 'GoldUp';
    if (n >= 300)  return 'Gold';
    if (n >= 250)  return 'SilverUp';
    if (n >= 200)  return 'Silver';
    if (n >= 150)  return 'BronzeUp';
    if (n >= 100)  return 'Bronze';
    if (n >= 50)   return 'IronUp';
    return 'Iron';
  }

  function isCurrentSeasonRecordForProfile(season, seasons=[]){
    if (!season) return false;

    if (season.is_current === true || season.current === true || season.active === true) return true;

    const name = String(season.name || '').trim().toLowerCase();
    if (name === 'current') return true;

    const ids = (Array.isArray(seasons) ? seasons : [])
      .map(s => Number(s?.id))
      .filter(n => Number.isFinite(n));

    if (!ids.length) return false;
    return Number(season.id) === Math.max(...ids);
  }

  function profileMatchesPlayedOf(row){
    return safeNumHist(row?.matchesPlayed ?? row?.matches_played, 0);
  }

  function isSeasonPlacementForProfileList(row, season, seasons=[]){
    if (!row) return false;
    if (row.is_season_placement === true) return true;

    const raw = String(row.season_status ?? row.seasonStatus ?? '').trim().toLowerCase();
    if (raw === 'season placement') return true;

    const matchesPlayed = profileMatchesPlayedOf(row);
    const rank = String(row.rank ?? '').trim();
    const explicitZero = Number(row.visible_mmr ?? row.display_mmr ?? NaN);
    const hasVisibleZero = Number.isFinite(explicitZero) && explicitZero === 0;

    if (hasVisibleZero && matchesPlayed === 0) return true;
    if (!isCurrentSeasonRecordForProfile(season, seasons)) return false;

    return matchesPlayed === 0 && rank !== '' && rank !== 'Placement';
  }

  function displayMmrForProfileList(row, season, seasons=[]){
    if (!row) return 0;

    if (row.visible_mmr != null) return safeNumHist(row.visible_mmr, 0);
    if (row.display_mmr != null) return safeNumHist(row.display_mmr, 0);

    if (isSeasonPlacementForProfileList(row, season, seasons) && profileMatchesPlayedOf(row) === 0) {
      return 0;
    }

    return safeNumHist(row.mmr, 0);
  }

  function isPlacementForProfileList(row, season, seasons=[]){
    if (isSeasonPlacementForProfileList(row, season, seasons)) return false;
    return String(row?.rank || '').trim() === 'Placement';
  }

  function sortPlayersForProfileHistory(rawPlayers, season, seasons=[]){
    const list = Array.isArray(rawPlayers) ? rawPlayers : [];

    const cleaned = list
      .map((row, idx) => {
        let rank = normalizeRankValue(row.rank);
        if (!rank && row.mmr != null) rank = profileRankFromMmr(row.mmr);
        if (!rank) rank = 'Placement';

        return {
          ...row,
          rank,
          _api_index: idx,
        };
      })
      .filter(row => row.in_guild !== false);

    const bucketOf = (row) => {
      const played = profileMatchesPlayedOf(row) > 0;
      if (played) return 0;
      if (isSeasonPlacementForProfileList(row, season, seasons) && profileMatchesPlayedOf(row) === 0) return 1;
      if (isPlacementForProfileList(row, season, seasons)) return 2;
      return 3;
    };

    return cleaned.sort((a, b) => {
      const ab = bucketOf(a);
      const bb = bucketOf(b);
      if (ab !== bb) return ab - bb;

      if (ab === 0) {
        const am = displayMmrForProfileList(a, season, seasons);
        const bm = displayMmrForProfileList(b, season, seasons);
        if (bm !== am) return bm - am;
      }

      if (ab === 1) {
        return safeNumHist(a._api_index, 0) - safeNumHist(b._api_index, 0);
      }

      const am = displayMmrForProfileList(a, season, seasons);
      const bm = displayMmrForProfileList(b, season, seasons);
      if (bm !== am) return bm - am;

      return String(a.name || '').localeCompare(String(b.name || ''), undefined, { sensitivity:'base' });
    });
  }

  function resolveProfileHistoryPosition({ season, seasons, sortedList, idx, playerId, extras }){
    const lb = window.__lbApp;
    const selectedSeasonId = lb?.selectedSeasonId ?? null;
    const isSelectedLeaderboardSeason = selectedSeasonId != null && String(season?.id) === String(selectedSeasonId);

    if (isSelectedLeaderboardSeason && lb?.playerRankMap && lb.playerRankMap[playerId]) {
      return lb.playerRankMap[playerId];
    }

    if (isSelectedLeaderboardSeason && extras && extras.position) {
      return extras.position;
    }

    return idx + 1;
  }

  async function showUserProfile(p, extras={}){
    setActiveUser();
    CURRENT_USER_PROFILE = p;

    const name = p.name || 'Unknown';
    const rank = normalizeRankValue(p.rank) || '-';
    const country = (p.country||'').toLowerCase();
    const pfp = (extras && extras.pfp) || (window.__lbApp ? window.__lbApp.pfpSrc(p) : '') || '';
    const isSP = isSeasonPlacementPlayer(p);

    const pfpEl = document.getElementById('userPfp');
    pfpEl.src = pfp || DEFAULT_PFP;
    applyRankBorderToElement(pfpEl, rank, { glowAlpha: .42, glowSize: 16 });

    const seasonTitleEl = document.getElementById('userProfileSeasonTitle');
    const seasonTitle = getPlayerSeasonTitle(p);
    if(seasonTitleEl){
      if(seasonTitle){
        seasonTitleEl.style.display = 'inline-flex';
        seasonTitleEl.textContent = seasonTitle.label;
        seasonTitleEl.className = `profile-selected-title ${seasonTitleClass(seasonTitle)}`;
      } else {
        seasonTitleEl.style.display = 'none';
        seasonTitleEl.textContent = '';
        seasonTitleEl.className = 'profile-selected-title';
      }
    }

    const titleEl = document.getElementById('userTitle');
    if (isSP) {
      titleEl.innerHTML = `${name} <span class="season-placement-chip">Season Placement</span>`;
    } else {
      titleEl.innerHTML = `${name} <span class="rankchip rank-${rank}">${rank}</span>`;
    }

    const metaEl = document.getElementById('userMeta');
    const pos = extras && extras.position ? `#${extras.position}` : '';
    const flag = country ? countryToFlagEmoji(country) : '';
    const mmrDisp = clampMmrForDisplay(p);

    const metaParts = [
      pos,
      p.mmr != null ? `MMR ${mmrDisp}` : null,
      isSP ? 'Season Placement' : null
    ].filter(Boolean);

    metaEl.innerHTML = metaParts.map(part => {
      if (part === 'Season Placement') {
        return `<span class="season-placement-text">Season Placement</span>`;
      }
      return part;
    }).join(' · ');

    if (flag) metaEl.innerHTML += ` ${flag}`;

    const body = document.getElementById('userHistoryBody');
    body.innerHTML = '<div class="help">Loading history…</div>';

    try {
      let seasons = (window.__lbApp && Array.isArray(window.__lbApp.seasons) && window.__lbApp.seasons.length)
        ? window.__lbApp.seasons
        : [];

      if (!seasons.length) {
        const sres = await fetch(`${API_BASE}/api/seasons`);
        seasons = (await sres.json()) || [];
      }

      const rows = [];
      for (const s of seasons) {
        try {
          const pres = await fetch(`${API_BASE}/api/players?season_id=${encodeURIComponent(s.id)}`);
          const rawList = (await pres.json()) || [];
          const list = sortPlayersForProfileHistory(rawList, s, seasons);

          // Importante:
          // aquí usamos el mismo orden visual que el Leaderboard,
          // no el orden crudo que devuelve el backend.
          const idx = list.findIndex(x => String(x.id) === String(p.id));
          if (idx >= 0) {
            const hit = list[idx];
            const rowIsCurrentSeason = isCurrentSeasonRecordForProfile(s, seasons);
            const resolvedPosition = resolveProfileHistoryPosition({
              season: s,
              seasons,
              sortedList: list,
              idx,
              playerId: p.id,
              extras,
            });

            rows.push({
              seasonId: s.id,
              seasonName: s.name === 'Current' ? 'PreSeason' : s.name,
              is_current_season: rowIsCurrentSeason,
              mmr: hit.mmr ?? null,
              visible_mmr: hit.visible_mmr ?? hit.display_mmr ?? null,
              peak_mmr: hit.peak_mmr ?? hit.peakMmr ?? null,
              visible_peak_mmr: hit.visible_peak_mmr ?? hit.display_peak_mmr ?? null,
              rank: normalizeRankValue(hit.rank) || null,
              is_season_placement: hit.is_season_placement === true,
              season_status: hit.season_status ?? null,
              position: resolvedPosition,
              matches: hit.matchesPlayed ?? hit.matches_played ?? null,
              matches_played: hit.matchesPlayed ?? hit.matches_played ?? null,
              last10_wins: hit.last10Wins ?? hit.last10_wins ?? null,
              last10_losses: hit.last10Losses ?? hit.last10_losses ?? null,
              last10_mmr_delta: hit.last10MmrDelta ?? hit.last10_mmr_delta ?? null,
            });
          }
        } catch (e) {
          console.warn('players by season error', s, e);
        }
      }

      if (!rows.length) {
        body.innerHTML = `<div class="help">This player doesnt have historial yet.</div>`;
      } else {
        rows.sort((a, b) => (Number(b.seasonId) || 0) - (Number(a.seasonId) || 0));

        const table = document.createElement('table');
        table.innerHTML = `
          <thead>
            <tr>
              <th>Season</th>
              <th>MMR</th>
              <th>Role</th>
              <th>Position</th>
              <th>Matches</th>
              <th>Last 10 (W/L)<br>(+-MMR)</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(r => {
              const hasLast10 = (r.last10_wins != null || r.last10_losses != null);
              const w = safeNumHist(r.last10_wins, 0);
              const l = safeNumHist(r.last10_losses, 0);
              const total = last10TotalHist(r);
              const delta = (r.last10_mmr_delta == null ? null : fmtSignedHist(r.last10_mmr_delta));
              const mmrShow = r.mmr == null ? '-' : clampMmrForDisplay(r);
              const isSPRow = r.is_season_placement === true || String(r.season_status ?? '').toLowerCase() === 'season placement';

              return `
                <tr>
                  <td>${r.seasonName}</td>
                  <td>${mmrShow}</td>
                  <td>
                    ${
                      isSPRow
                        ? `<span class="season-placement-chip">Season Placement</span>`
                        : `<span class="rankchip rank-${r.rank || '-'}">${r.rank || '-'}</span>`
                    }
                  </td>
                  <td>${r.position ?? '-'}</td>
                  <td>${r.matches ?? '-'}</td>
                  <td>
                    ${hasLast10 ? `${w}-${l}` : '-'}
                    ${hasLast10 && delta !== null
                      ? `<br><span class="help">(${total}/10) ${delta}</span>`
                      : ''
                    }
                  </td>
                </tr>
              `;
            }).join('')}
          </tbody>`;
        body.innerHTML = '';
        body.appendChild(table);
      }

      await loadUserMatches(p);

    } catch(err) {
      console.warn(err);
      body.innerHTML = `<div class="msg err">Cant load the history (fallback).</div>`;
    }
  }
  window.showUserProfile = showUserProfile;
</script>