const fs = require('fs');

async function main() {
  const seasonsResp = await fetch('https://pjsk-page.vercel.app/api/seasons');
  const seasons = await seasonsResp.json();
  console.log('Seasons loaded:', seasons.length);

  const playersResp = await fetch('https://pjsk-page.vercel.app/api/players?season_id=8');
  const players = await playersResp.json();
  console.log('Players loaded for season 8:', players.length);

  // Let's create an object mocking the Vue component with all its methods
  // and run it exactly as web/index.html does!
  
  // Read index.html to extract the Vue methods
  const html = fs.readFileSync('web/index.html', 'utf8');

  // Let's test the filtering
  const selectedSeasonId = 8;
  const currentSeason = seasons.find(s => s.id === 8);
  console.log('Current season:', currentSeason);

  // Check how each player is processed
  let isSeasonPlacementCount = 0;
  let matchesPlayedCount = 0;
  let isZeroMmrUnplayedNonStandingCount = 0;

  for (const p of players) {
    const rank = p.rank || 'Placement';
    const is_season_placement = p.is_season_placement === true || p.isSeasonPlacement === true;
    const matches = p.matchesPlayed || p.matches_played || 0;
    const mmr = p.mmr || 0;
    
    // Test isZeroMmrUnplayedNonStanding
    // In index.html:
    // if (!p) return false;
    // if (this.isSeasonPlacement(p)) return false;
    // if (!this.isCurrentSeasonSelected()) return false;
    // if (this.matchesPlayedOf(p) !== 0) return false;
    // ...
    // return rank === '';
  }

  console.log('Sample player from API:', players[0]);
}

main().catch(console.error);

