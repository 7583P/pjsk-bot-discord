const fs = require('fs');

async function main() {
  const playersResp = await fetch('https://pjsk-page.vercel.app/api/players?season_id=8');
  const players = await playersResp.json();

  // Mocking the component state
  const comp = {
    selectedSeasonId: 8,
    selectedRank: null,
    filter: '',
    currentPage: 1,
    pageSize: 50,
    seasons: [{ id: 8, name: 'Season 5 (New Era)', active: true }],
    
    normalizeRank(r) {
      if (!r) return '';
      return String(r).trim();
    },
    rankFromMmr(m) {
      return 'Placement';
    },
    isCurrentSeasonSelected() {
      return true;
    },
    matchesPlayedOf(p) {
      return p?.matchesPlayed || p?.matches_played || 0;
    },
    startingMmrOf(p) {
      return p?.starting_mmr || 0;
    },
    rawMmrOf(p) {
      return p?.mmr || 0;
    },
    rawPeakMmrOf(p) {
      return p?.peak_mmr || 0;
    },
    backendSeasonPlacementFlag(p) {
      return false;
    },
    isSeasonPlacement(p) {
      return false;
    },
    isZeroMmrUnplayedNonStanding(p) {
      if (!p) return false;
      if (this.isSeasonPlacement(p)) return false;
      if (!this.isCurrentSeasonSelected()) return false;
      if (this.matchesPlayedOf(p) !== 0) return false;

      const mmr = this.rawMmrOf(p);
      const peak = this.rawPeakMmrOf(p);
      const starting = this.startingMmrOf(p);
      if (mmr > 0 || peak > 0 || starting > 0) return false;

      const rank = String(p?.rank || '').trim();
      return rank === '';
    }
  };

  const cleaned = players.map((p, idx) => {
    let rank = comp.normalizeRank(p.rank ?? p.role);
    if (!rank) rank = 'Placement';
    return { ...p, rank, _api_index: idx };
  });

  const filtered = cleaned
    .filter(p => p.in_guild !== false)
    .filter(p => !comp.isZeroMmrUnplayedNonStanding(p));

  console.log('Cleaned length:', cleaned.length);
  console.log('Filtered length:', filtered.length);
}

main().catch(console.error);

