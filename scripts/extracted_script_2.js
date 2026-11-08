<script>
  const API_BASE   = 'https://worker-production-1ad2.up.railway.app';
  const AUTH02_URL = `${API_BASE}/auth02`;
  const DEFAULT_PFP = '/assets/images/deleted_pfp.jpg';
  const DEFAULT_FLAG = '/assets/images/unknown_flag.jpg';

  function isDiscordAvatarUrl(url){
    const s = String(url || '').trim();
    if(!s) return false;

    // Only REAL custom Discord avatar CDN URLs are accepted.
    // IMPORTANT: /embed/avatars/ are Discord's generic default icons, so they are NOT accepted.
    // If there is no real Discord avatar, the page must show DEFAULT_PFP instead.
    return /^https:\/\/(cdn\.discordapp\.com|media\.discordapp\.net)\/avatars\/\d+\/[A-Za-z0-9_]+\.(png|jpg|jpeg|webp|gif)(\?.*)?$/i.test(s);
  }

  function pickDiscordAvatarUrl(...values){
    for(const value of values){
      const s = String(value || '').trim();
      if(isDiscordAvatarUrl(s)) return s;
    }
    return '';
  }

  const api = axios.create({
    baseURL: API_BASE,
    withCredentials: true,
    timeout: 10000,
  });

  const { createApp } = Vue;

  function countryToFlagEmoji(cc){
    if (!cc) return '';
    const code = String(cc).toUpperCase();
    if (code.length !== 2) return '';
    const base = 'A'.charCodeAt(0);
    const A = 0x1F1E6;
    let out = '';
    for (const ch of code){
      const diff = ch.charCodeAt(0) - base;
      if (diff < 0 || diff > 25) return '';
      out += String.fromCodePoint(A + diff);
    }
    return out;
  }

  function resolveCountryFromLb(e){
    const direct = e.country ?? e.country_code ?? e.countryCode ?? e.cc ?? null;
    if (direct) return direct;
    const lb = window.__lbApp;
    if (lb && Array.isArray(lb.players)){
      const id = e.user_id || e.id;
      const hit = lb.players.find(p => String(p.id) === String(id));
      if (hit && hit.country) return hit.country;
    }
    return null;
  }

  function normalizeRankValue(raw){
    if(!raw) return '';
    let r = String(raw).trim();
    const hasArrow = r.includes('↑') || r.includes('⬆');
    let base = r.replace(/[↑⬆]/g,'').trim();

    const key = base.toLowerCase().replace(/\s+/g,' ').trim();
    const map = { 'grand champion': 'GrandChampion', 'divine star': 'DivineStar' };
    base = map[key] || base;

    if(hasArrow){
      if(base === 'Iron')     return 'IronUp';
      if(base === 'Bronze')   return 'BronzeUp';
      if(base === 'Silver')   return 'SilverUp';
      if(base === 'Gold')     return 'GoldUp';
      if(base === 'Platinum') return 'PlatinumUp';
      if(base === 'Diamond')  return 'DiamondUp';
      if(base === 'Crystal')  return 'CrystalUp';
      if(base === 'Master')   return 'MasterUp';
    }
    return base;
  }


  function hexToRgba(hex, alpha){
    const clean = String(hex || '').replace('#','').trim();
    if(!/^[0-9a-f]{6}$/i.test(clean)) return `rgba(43,47,54,${alpha})`;

    const r = parseInt(clean.slice(0,2), 16);
    const g = parseInt(clean.slice(2,4), 16);
    const b = parseInt(clean.slice(4,6), 16);
    return `rgba(${r},${g},${b},${alpha})`;
  }

  function rankBorderColor(rawRank){
    const rank = normalizeRankValue(rawRank) || 'Placement';
    const map = {
      Placement: '#ff7000',
      Iron: '#8d7f79',
      IronUp: '#917f76',
      Bronze: '#9f4600',
      BronzeUp: '#c55500',
      Silver: '#c7c5c5',
      SilverUp: '#cfcfcf',
      Gold: '#ffcf87',
      GoldUp: '#ffbf60',
      Platinum: '#99a1f1',
      PlatinumUp: '#bf8bff',
      Diamond: '#5ec1ff',
      DiamondUp: '#00f6ff',
      Crystal: '#e69bff',
      CrystalUp: '#fab9fa',
      Master: '#c176ff',
      MasterUp: '#d6b9ff',
      Champion: '#ff5953',
      GrandChampion: '#ff7878',
      Star: '#de87ff',
      DivineStar: '#ff92f6',
      Legend: '#ff8be4'
    };
    return map[rank] || '#2b2f36';
  }

  function pfpBorderStyleFromRank(rawRank, options = {}){
    const color = rankBorderColor(rawRank);
    const glowAlpha = options.glowAlpha ?? 0.42;
    const glowSize = options.glowSize ?? 10;

    return {
      borderColor: color,
      boxShadow: `0 0 0 1px rgba(0,0,0,.75), 0 0 ${glowSize}px ${hexToRgba(color, glowAlpha)}`
    };
  }

  function applyRankBorderToElement(el, rawRank, options = {}){
    if(!el) return;
    const style = pfpBorderStyleFromRank(rawRank, options);
    el.style.borderColor = style.borderColor;
    el.style.boxShadow = style.boxShadow;
  }


  function ordinalPlace(n){
    const place = Number(n || 0);
    if(place === 1) return '1st';
    if(place === 2) return '2nd';
    if(place === 3) return '3rd';
    return String(place || '');
  }

  function normalizeSeasonTitle(raw){
    if(!raw) return null;

    if(typeof raw === 'string'){
      const label = raw.trim();
      if(!label) return null;
      const m = label.match(/(1st|2nd|3rd)/i);
      const place = m ? ({'1st':1,'2nd':2,'3rd':3}[m[1].toLowerCase()] || null) : null;
      return { key: label, label, place };
    }

    const key = raw.key || raw.titleKey || raw.title_key || raw.value || '';
    const place = Number(raw.place || raw.position || raw.rank || 0) || null;
    const label = raw.label || raw.title || raw.name || raw.text || '';
    const seasonId = raw.seasonId || raw.season_id || null;
    const seasonName = raw.seasonName || raw.season_name || null;

    if(label){
      return { key, label, place, seasonId, seasonName };
    }

    if(seasonId && place){
      return {
        key: key || `season:${seasonId}:${place}`,
        label: `Season ${seasonName || seasonId} ${ordinalPlace(place)} Place`,
        place,
        seasonId,
        seasonName
      };
    }

    return null;
  }

  function seasonTitleClass(title){
    const t = normalizeSeasonTitle(title);
    const place = Number(t?.place || 0);
    if(place === 1) return 'season-title-gold';
    if(place === 2) return 'season-title-silver';
    if(place === 3) return 'season-title-bronze';

    const label = String(t?.label || '').toLowerCase();
    if(label.includes('1st')) return 'season-title-gold';
    if(label.includes('2nd')) return 'season-title-silver';
    if(label.includes('3rd')) return 'season-title-bronze';
    return 'season-title-gold';
  }

  function getPlayerSeasonTitle(p){
    return normalizeSeasonTitle(
      p?.activeTitle ||
      p?.active_title ||
      p?.selectedTitle ||
      p?.selected_title ||
      p?.seasonTitle ||
      p?.season_title ||
      p?.title
    );
  }

  function renderProfileSelectedTitle(){
    const el = $('#profileSelectedTitle');
    if(!el) return;

    const title = normalizeSeasonTitle(AUTH_SELECTED_TITLE);
    if(!title){
      el.style.display = 'none';
      el.textContent = '';
      el.className = 'profile-selected-title';
      return;
    }

    el.style.display = 'inline-flex';
    el.textContent = title.label;
    el.className = `profile-selected-title ${seasonTitleClass(title)}`;
  }

  function getAuthUserRank(){
    const lb = window.__lbApp;
    if(!lb || !AUTH_USER) return '';

    const authId = String(AUTH_USER.id || '');
    const authName = String(AUTH_USER.nameLower || AUTH_USER.username || '').toLowerCase();

    const hit = (lb.players || []).find(p => {
      const ids = [p.id, p.discordId, p.discord_id, p.user_id].filter(v => v !== null && v !== undefined).map(v => String(v));
      if(authId && ids.includes(authId)) return true;
      return authName && lb.normalizeName && lb.normalizeName(p.name) === authName;
    });

    return hit?.rank || '';
  }

  /* ================= LEADERBOARD APP ================= */
  createApp({
    data(){
      return {
        players: [],
        seasons: [],
        filter: '',
        selectedSeasonId: null,
        playerRankMap: {},
        currentPage: 1,
        pageSize: 50,
        selectedRank: null,
        menuOpen: false,
        menuOpenSeason: false,
        seasonLabel: null,
        pfpById: {},
        pfpByName: {},
        ranks: [
          { key:'Legend',        label:'Legend',        range:'↑1000' },
          { key:'DivineStar',    label:'Divine Star',   range:'950-999' },
          { key:'Star',          label:'Star',          range:'900-949' }, 
          { key:'GrandChampion', label:'Grand Champion',range:'850-899' },
          { key:'Champion',      label:'Champion',      range:'800-849' },
          { key:'MasterUp',      label:'Master ↑',      range:'750-799' },
          { key:'Master',        label:'Master',        range:'700-749' },
          { key:'CrystalUp',     label:'Crystal ↑',     range:'650-699' },
          { key:'Crystal',       label:'Crystal',       range:'600-649' },
          { key:'DiamondUp',     label:'Diamond ↑',     range:'550-599' },
          { key:'Diamond',       label:'Diamond',       range:'500-549' },
          { key:'PlatinumUp',    label:'Platinum ↑',    range:'450-499' },
          { key:'Platinum',      label:'Platinum',      range:'400-449' },
          { key:'GoldUp',        label:'Gold ↑',        range:'350-399' },
          { key:'Gold',          label:'Gold',          range:'300-349' },
          { key:'SilverUp',      label:'Silver ↑',      range:'250-299' },
          { key:'Silver',        label:'Silver',        range:'200-249' },
          { key:'BronzeUp',      label:'Bronze ↑',      range:'150-199' },
          { key:'Bronze',        label:'Bronze',        range:'100-149' },
          { key:'IronUp',        label:'Iron ↑',        range:'50-99' },
          { key:'Iron',          label:'Iron',          range:'↓0-49' }
        ]
      }
    },
    computed:{
      filteredPlayers(){
        let list = this.players;
        if (this.selectedRank) {
          list = list.filter(p => p.rank === this.selectedRank);
        } else if (this.filter) {
          const q = this.filter.toLowerCase();
          list = list.filter(p => (p.name||'').toLowerCase().includes(q));
        }
        return list;
      },
      filteredCount(){
        if(!this.selectedRank) return 0;
        return this.players.filter(p => p.rank === this.selectedRank).length;
      },
      totalPages(){
        return Math.max(1, Math.ceil(this.filteredPlayers.length / this.pageSize));
      },
      paginatedPlayers(){
        const start = (this.currentPage - 1) * this.pageSize;
        return this.filteredPlayers.slice(start, start + this.pageSize);
      }
    },
    watch:{
      filter(){ this.currentPage = 1; },
      selectedRank(){
        this.currentPage = 1;
        this.filter = '';
      },
      currentPage(val){
        if(val < 1) this.currentPage = 1;
        else if (val > this.totalPages) this.currentPage = this.totalPages;
      }
    },
    methods:{
      safe(val, fallback = 0){
        const n = Number(val);
        if (val === null || val === undefined || Number.isNaN(n)) return fallback;
        return n;
      },

      selectedSeasonRecord(){
        return this.seasons.find(x => x.id == this.selectedSeasonId) || null;
      },

      isCurrentSeasonRecord(season){
        if (!season) return false;
        if (season.is_current === true || season.current === true || season.active === true) return true;

        const name = String(season.name || '').trim().toLowerCase();
        if (name === 'current') return true;

        const ids = this.seasons
          .map(s => Number(s?.id))
          .filter(n => Number.isFinite(n));
        if (!ids.length) return false;

        return Number(season.id) === Math.max(...ids);
      },

      isCurrentSeasonSelected(){
        return this.isCurrentSeasonRecord(this.selectedSeasonRecord());
      },

      matchesPlayedOf(p){
        return this.safe(p?.matchesPlayed ?? p?.matches_played, 0);
      },

      isSeasonPlacement(p){
        if (!p) return false;
        if (p.is_season_placement === true) return true;

        const raw = String(p.season_status ?? p.seasonStatus ?? '').trim().toLowerCase();
        if (raw === 'season placement') return true;

        const matchesPlayed = this.matchesPlayedOf(p);
        const rank = String(p?.rank || '').trim();
        const explicitZero = Number(p.visible_mmr ?? p.display_mmr ?? NaN);
        const hasVisibleZero = Number.isFinite(explicitZero) && explicitZero === 0;

        if (hasVisibleZero && matchesPlayed === 0) return true;

        if (!this.isCurrentSeasonSelected()) return false;

        return matchesPlayed === 0 && rank !== '' && rank !== 'Placement';
      },

      isUnplayedSeasonPlacement(p){
        return this.isSeasonPlacement(p) && this.matchesPlayedOf(p) === 0;
      },

      displayMmr(p){
        if (!p) return 0;

        if (p.visible_mmr != null) return this.safe(p.visible_mmr, 0);
        if (p.display_mmr != null) return this.safe(p.display_mmr, 0);

        if (this.isUnplayedSeasonPlacement(p)) return 0;
        return this.safe(p?.mmr, 0);
      },

      displayPeakMmr(p){
        if (!p) return 0;

        if (p.visible_peak_mmr != null) return this.safe(p.visible_peak_mmr, 0);
        if (p.display_peak_mmr != null) return this.safe(p.display_peak_mmr, 0);

        if (this.isUnplayedSeasonPlacement(p)) return 0;
        return this.safe(p?.peakMmr ?? p?.peak_mmr ?? p?.mmr, this.displayMmr(p));
      },

      isPlacement(p){
        if (this.isSeasonPlacement(p)) return false;
        return String(p?.rank || '').trim() === 'Placement';
      },

      sortMmrKey(p){
        return this.displayMmr(p);
      },

      last10Total(p){
        const w = this.safe(p.last10Wins ?? p.last10_wins, 0);
        const l = this.safe(p.last10Losses ?? p.last10_losses, 0);
        return w + l;
      },

      fmtSigned(delta){
        const n = Number(delta || 0);
        return (n >= 0 ? '+' : '') + n;
      },

      rankFromMmr(mmr){
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
      },

      peakRank(p){
        if (this.isPlacement(p)) return 'Placement';
        const peak = this.displayPeakMmr(p);
        return this.rankFromMmr(peak);
      },

      normalizeName(n){
        return String(n||'').toLowerCase();
      },

      normalizeRank(raw){
        return normalizeRankValue(raw);
      },

      pfpSrc(p){
        if(!p) return DEFAULT_PFP;

        const direct = pickDiscordAvatarUrl(
          p.pfp,
          p.avatarUrl,
          p.avatar_url,
          p.discordAvatarUrl,
          p.discord_avatar_url,
          p.discord_avatar,
          p.profile_image_url
        );
        if(direct) return direct;

        const saved = pickDiscordAvatarUrl(
          this.pfpById[String(p.discordId || p.discord_id || p.user_id || p.id)],
          this.pfpByName[this.normalizeName(p.name)]
        );
        return saved || DEFAULT_PFP;
      },


      pfpBorderStyle(p){
        return pfpBorderStyleFromRank(p?.rank, { glowAlpha: .45, glowSize: 9 });
      },

      playerSeasonTitle(p){
        return getPlayerSeasonTitle(p);
      },

      seasonTitleClass(title){
        return seasonTitleClass(title);
      },

      selectRank(rank){
        this.selectedRank = rank;
        this.menuOpen = false;
      },

      selectSeason(seasonId){
        this.selectedSeasonId = Number(seasonId);
        this.menuOpenSeason = false;
        this.currentPage = 1;
        this.selectedRank = null;
        this.filter = '';
        this.fetchPlayers();

        const name = this.displaySeason(this.selectedSeasonId);
        this.seasonLabel = (name === 'Current' ? 'PreSeason' : 'Season ' + name);
      },

      clearRank(){ this.selectedRank = null; },

      prevPage(){ if(this.currentPage > 1) this.currentPage--; },
      nextPage(){ if(this.currentPage < this.totalPages) this.currentPage++; },

      goToPage(){
        if (!this.currentPage) this.currentPage = 1;
        if (this.currentPage < 1) this.currentPage = 1;
        if (this.currentPage > this.totalPages) this.currentPage = this.totalPages;
      },

      async fetchPlayers(){
        try{
          const params = {};
          if (this.selectedSeasonId != null) params.season_id = this.selectedSeasonId;
          const resp = await api.get('/api/players', { params });

          const cleaned = (resp.data || []).map((p, idx) => {
            let rank = this.normalizeRank(p.rank);
            if (!rank && p.mmr != null) rank = this.rankFromMmr(p.mmr);
            if (!rank) rank = 'Placement';

            return {
              ...p,
              rank,
              _api_index: idx,
              is_season_placement: p.is_season_placement === true,
              season_status: p.season_status ?? null,
            };
          });

          const filtered = cleaned.filter(p => p.in_guild !== false);

          const bucketOf = (p) => {
            const played = this.matchesPlayedOf(p) > 0;
            if (played) return 0;
            if (this.isUnplayedSeasonPlacement(p)) return 1;
            if (this.isPlacement(p)) return 2;
            return 3;
          };

          const sorted = filtered.slice().sort((a, b) => {
            const ab = bucketOf(a);
            const bb = bucketOf(b);
            if (ab !== bb) return ab - bb;

            if (ab === 0) {
              const am = this.displayMmr(a);
              const bm = this.displayMmr(b);
              if (bm !== am) return bm - am;
            }

            if (ab === 1) {
              return this.safe(a._api_index, 0) - this.safe(b._api_index, 0);
            }

            const am = this.displayMmr(a);
            const bm = this.displayMmr(b);
            if (bm !== am) return bm - am;

            return String(a.name || '').localeCompare(String(b.name || ''), undefined, { sensitivity:'base' });
          });

          this.players = sorted;

          const map = {};
          sorted.forEach((p,i)=> map[p.id] = i+1);
          this.playerRankMap = map;
        }catch(err){
          console.error('API players error:', err?.message || err);
          this.players = [];
          this.playerRankMap = {};
        }
      },

      async fetchSeasons(){
        try{
          const resp = await api.get('/api/seasons');
          const raw = Array.isArray(resp.data) ? resp.data : [];
          this.seasons = raw;

          if (!this.selectedSeasonId && this.seasons.length) {
            const current = this.seasons.find(s => String(s.name || '').toLowerCase() === 'current');
            this.selectedSeasonId = current ? current.id : this.seasons[0].id;
          }

          const name = this.displaySeason(this.selectedSeasonId);
          this.seasonLabel = (name === 'Current' ? 'PreSeason' : 'Season ' + name);
        }catch(err){
          console.error('API seasons error:', err?.message || err);
        }
      },

      async fetchPfpMap(){
        try{
          const url = `${AUTH02_URL}/pfps`;
          let res = await fetch(url, { cache:"no-store", credentials:'include' });
          if(!res.ok) res = await fetch(url, { cache:"no-store" });
          if(!res.ok) throw new Error('pfps HTTP '+res.status);
          const raw = await res.json();

          const CLEAR_PIXEL = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==';
          const byId = {}, byName = {};
          if (raw && typeof raw === 'object') {
            const srcById   = raw.byId   || raw || {};
            const srcByName = raw.byName || {};
            Object.entries(srcById).forEach(([k,v])=>{
              // Keep only Discord avatars. Old Sekaipedia card URLs are ignored.
              if(v && v !== CLEAR_PIXEL && isDiscordAvatarUrl(v)) byId[String(k)] = v;
            });
            Object.entries(srcByName).forEach(([k,v])=>{
              // Keep only Discord avatars. Old Sekaipedia card URLs are ignored.
              if(v && v !== CLEAR_PIXEL && isDiscordAvatarUrl(v)) byName[String(k||'').toLowerCase()] = v;
            });
          }
          this.pfpById = byId;
          this.pfpByName = byName;
        }catch(e){
          console.warn('PFP map error', e);
        }
      },

      isValidIso2(cc){
        return /^[A-Za-z]{2}$/.test(String(cc || '').trim());
      },

      flagSrc(cc){
        const code = String(cc || '').trim().toLowerCase();

        if (!this.isValidIso2(code)) {
          return DEFAULT_FLAG;
        }

        return `https://flagcdn.com/24x18/${code}.png`;
      },

      onFlagError(event){
        if (!event || !event.target) return;

        if (event.target.src.includes(DEFAULT_FLAG)) return;

        event.target.src = DEFAULT_FLAG;
      },

      displayRank(rank){
        switch(rank){
          case 'DivineStar':     return 'Divine Star';
          case 'GrandChampion':  return 'Grand Champion';
          case 'IronUp':         return 'Iron ↑';
          case 'BronzeUp':       return 'Bronze ↑';
          case 'SilverUp':       return 'Silver ↑';
          case 'GoldUp':         return 'Gold ↑';
          case 'PlatinumUp':     return 'Platinum ↑';
          case 'DiamondUp':      return 'Diamond ↑';
          case 'CrystalUp':      return 'Crystal ↑';
          case 'MasterUp':       return 'Master ↑';
          default: return rank || '';
        }
      },

      displaySeason(seasonId){
        const hit = this.seasons.find(x => x.id == seasonId);
        return hit?.name || '';
      },

      splitName(name){
        const emojiPres = /\p{Emoji_Presentation}/u;
        return Array.from(String(name||'')).map(c=>({ char:c, isEmoji: emojiPres.test(c) }));
      },

      onDocClick(e){
        const dd1 = document.querySelector('#lbApp .dropdown');
        if(!dd1 || !dd1.contains(e.target)){
          this.menuOpen = false;
          this.menuOpenSeason = false;
        }
      },

      openUser(p){
        if (!p) return;
        window.showUserProfile(p, {
          pfp: this.pfpSrc(p),
          position: this.playerRankMap[p.id] || null,
        });
      }
    },

    async mounted(){
      window.__lbApp = this;
      await this.fetchSeasons();
      await this.fetchPlayers();
      await this.fetchPfpMap();
      this._timer = setInterval(this.fetchPlayers, 10000);
      document.addEventListener('click', this.onDocClick);
    },
    unmounted(){
      if(this._timer) clearInterval(this._timer);
      document.removeEventListener('click', this.onDocClick);
    }
  }).mount('#lbApp');


  /* ================= MATCHES GLOBAL APP ================= */
  createApp({
    data(){
      return {
        seasons: [],
        matches: [],
        selectedSeasonId: 'all',
        menuSeasonOpen: false,
        loading: false,
        error: null,
        searchId: '',
      }
    },
    computed:{
      sortedMatches(){
        return this.matches.slice().sort((a,b)=>{
          const ad = a.created_at ? new Date(a.created_at).getTime() : 0;
          const bd = b.created_at ? new Date(b.created_at).getTime() : 0;

          // Latest matches first. If dates are equal/missing, use the highest result_id first.
          if (bd !== ad) return bd - ad;

          const aId = Number(a.result_id ?? 0);
          const bId = Number(b.result_id ?? 0);
          return bId - aId;
        });
      },
      filteredMatches(){
        let list = this.sortedMatches;

        if (this.selectedSeasonId !== 'all') {
          list = list.filter(m => String(m.season_id) === String(this.selectedSeasonId));
        }

        if (this.searchId && this.searchId.trim() !== '') {
          const q = this.searchId.trim().toLowerCase();
          list = list.filter(m => String(m.result_id ?? '').toLowerCase().includes(q));
        }

        return list;
      }
    },
    methods:{
      normalizeSongs(raw){
        if (!raw) return [];
        if (Array.isArray(raw)) {
          return raw
            .map(s => ({
              level:      s.level        ?? s.lv              ?? s.difficulty_level ?? null,
              name:       s.name         ?? s.songName        ?? s.song_name        ?? s.title ?? '',
              difficulty: s.difficulty   ?? s.diff            ?? '',
              notes:      s.notes        ?? s.notesCount      ?? s.notes_count      ?? s.total_notes ?? null,
            }))
            .filter(s => s.name);
        }
        return [];
      },
      isSeasonOfficialName(name){
        const n = String(name || '').toLowerCase();
        if(!n) return false;
        if(n === 'current' || n.includes('preseason')) return true;
        const m = n.match(/(\d+)/);
        if(!m) return false;
        return parseInt(m[1],10) >= 1;
      },
      displaySeason(id){
        const hit = this.seasons.find(s => String(s.id) === String(id));
        return hit?.name || '';
      },
      selectSeason(val){
        this.selectedSeasonId = val;
        this.menuSeasonOpen = false;
        this.fetchMatches();
      },
      formatDate(iso){
        if (!iso) return '';
        try{
          const d = new Date(iso);
          return d.toLocaleString();
        }catch{
          return iso;
        }
      },
      gapLow(m){ return m.gap_low ?? m.lv_low ?? m.gapLvLow ?? m.min_gap ?? null; },
      gapHigh(m){ return m.gap_high ?? m.lv_high ?? m.gapLvHigh ?? m.max_gap ?? null; },
      gapText(m){
        const low = this.gapLow(m);
        const high = this.gapHigh(m);
        if (low == null || high == null) return '';
        return `LV(${low}-${high})`;
      },
      formatMatch(m){
        const low = this.gapLow(m);
        const high = this.gapHigh(m);
        const roomMmr = (m.room_mmr != null) ? m.room_mmr : '?';

        const headerLv = (low != null && high != null) ? `LV(${low}-${high})` : 'LV(?)';
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

            const prevLabel = e.was_placement ? `PLACEMENT(${before})` : `${before}`;
            const deltaStr = (delta >= 0 ? `+${delta}` : `${delta}`);

            lines.push(`${pos}. ${name}${cc} (${stats}) (${prevLabel}) ${deltaStr} = ${after}`);
          });
        }

        return lines.join('\n');
      },
      async fetchSeasons(){
        try{
          const resp = await api.get('/api/seasons');
          let raw = Array.isArray(resp.data) ? resp.data : [];
          this.seasons = raw.filter(s => this.isSeasonOfficialName(s.name));
        }catch(err){
          console.error('API seasons error (matches):', err?.message || err);
        }
      },
      async fetchMatches(){
        this.loading = true;
        this.error = null;
        try{
          const params = {};
          if (this.selectedSeasonId && this.selectedSeasonId !== 'all') params.season_id = this.selectedSeasonId;
          const resp = await api.get('/api/matches', { params });
          let arr = Array.isArray(resp.data) ? resp.data : [];

          this.matches = arr.map(m => ({
            ...m,
            room_mmr:  m.room_mmr  ?? m.avg_mmr   ?? m.avgMmr   ?? null,
            created_at: m.created_at ?? m.played_at ?? m.playedAt ?? null,
            entries: Array.isArray(m.entries) ? m.entries : (Array.isArray(m.players) ? m.players : []),
            songs: this.normalizeSongs(m.songs ?? m.levels ?? m.lv_songs?? m.charts ?? null),
          }));
        }catch(err){
          console.error('API matches error:', err?.message || err);
          this.error = 'Error loading matches (check /api/matches backend).';
        }finally{
          this.loading = false;
        }
      },

      onDocClick(e){
        const dd = document.querySelector('#matchesApp .dropdown');
        if (!dd || !dd.contains(e.target)) this.menuSeasonOpen = false;
      },
      pfpForUser(id, name, source = {}){
        const direct = pickDiscordAvatarUrl(
          source.pfp,
          source.avatarUrl,
          source.avatar_url,
          source.discordAvatarUrl,
          source.discord_avatar_url,
          source.discord_avatar,
          source.profile_image_url
        );
        if(direct) return direct;

        const lb = window.__lbApp;
        if(!lb) return DEFAULT_PFP;

        const normName = (lb.normalizeName ? lb.normalizeName(name) : String(name||'').toLowerCase());
        const saved = pickDiscordAvatarUrl(
          lb.pfpById?.[String(id)],
          lb.pfpByName?.[normName]
        );
        return saved || DEFAULT_PFP;
      },
      async openPlayer(entry, match){
        const userId = entry.user_id || entry.id;
        if(!userId) return;

        const name = entry.name || `ID ${userId}`;
        const country = resolveCountryFromLb(entry);
        let mmr = (entry.mmr_after != null ? entry.mmr_after : entry.mmr_before);
        let rank = normalizeRankValue(entry.rank || null);
        let position = null;
        let isSeasonPlacement = false;
        let seasonStatus = null;
        let seasonTitle = null;

        try{
          if (match && match.season_id != null){
            const resp = await api.get('/api/players', { params:{ season_id: match.season_id }});
            const rawList = Array.isArray(resp.data) ? resp.data : [];
            const seasonRecord =
              (this.seasons || []).find(s => String(s.id) === String(match.season_id)) ||
              (window.__lbApp?.seasons || []).find(s => String(s.id) === String(match.season_id)) ||
              { id: match.season_id, name: match.season_name || '' };
            const seasonsForSort =
              (this.seasons && this.seasons.length)
                ? this.seasons
                : ((window.__lbApp?.seasons && window.__lbApp.seasons.length) ? window.__lbApp.seasons : [seasonRecord]);

            const list = (typeof sortPlayersForProfileHistory === 'function')
              ? sortPlayersForProfileHistory(rawList, seasonRecord, seasonsForSort)
              : rawList.slice().sort((a,b)=>{
                  const am = Number(a.mmr ?? 0);
                  const bm = Number(b.mmr ?? 0);
                  if (bm !== am) return bm - am;
                  return String(a.name||'').localeCompare(String(b.name||''), undefined, {sensitivity:'base'});
                });

            const idx = list.findIndex(x => String(x.id) === String(userId));
            if(idx >= 0){
              position = idx+1;
              const hit = list[idx];
              if(hit.mmr != null) mmr = hit.mmr;
              if(hit.rank) rank = normalizeRankValue(hit.rank);
              isSeasonPlacement = hit.is_season_placement === true;
              seasonStatus = hit.season_status ?? null;
              seasonTitle = getPlayerSeasonTitle(hit);
            }
          }
        }catch(e){
          console.warn('error resolving player position', e);
        }

        const mmrDisp = Number(mmr ?? 0);

        const playerObj = {
          id:userId,
          name,
          country,
          mmr: mmrDisp,
          rank,
          is_season_placement: isSeasonPlacement,
          season_status: seasonStatus,
          seasonTitle,
          season_title: seasonTitle,
          selectedTitle: seasonTitle,
          activeTitle: seasonTitle,
        };

        const extras = { position, pfp: this.pfpForUser(userId, name, entry) };

        if (window.showUserProfile) window.showUserProfile(playerObj, extras);
      }
    },
    async mounted(){
      await this.fetchSeasons();
      await this.fetchMatches();
      document.addEventListener('click', this.onDocClick);
      this._timer = setInterval(this.fetchMatches, 30000);
    },
    unmounted(){
      document.removeEventListener('click', this.onDocClick);
      if (this._timer) clearInterval(this._timer);
    }
  }).mount('#matchesApp');
</script>