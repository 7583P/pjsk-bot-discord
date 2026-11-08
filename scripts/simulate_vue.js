const fs = require('fs');

async function test() {
  const resp = await fetch('https://pjsk-page.vercel.app/api/players?season_id=8');
  const data = await resp.json();
  console.log('Fetched players:', data.length);

  const html = fs.readFileSync('web/index.html', 'utf8');

  // Let's inspect positionBucket and filteredPlayers definitions in index.html
  const idx = html.indexOf('positionBucket(p)');
  console.log('--- positionBucket ---');
  console.log(html.slice(idx, idx + 600));

  const idx2 = html.indexOf('filteredPlayers()');
  console.log('--- filteredPlayers ---');
  console.log(html.slice(idx2, idx2 + 600));
}

test();

