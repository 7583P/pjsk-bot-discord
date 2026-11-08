const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync('web/index.html', 'utf8');

// Extract functions from index.html
// We can use vm to run the script block in a sandbox
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/g);
console.log('Script matches:', scriptMatch.length);

// Let's find seasonOrderValue, isTestSeasonValue, seasonMenuItemsFrom, displaySeasonForMenu, normalizeSeasonDisplayName
const sandbox = { console };
vm.createContext(sandbox);

// Extract the relevant helper functions
const fnNames = ['normalizeSeasonDisplayName', 'extractSeasonNumber', 'seasonTextForTest', 'seasonOrderValue', 'isTestSeasonValue', 'seasonMenuItemsFrom', 'displaySeasonForMenu'];
for (const fn of fnNames) {
  const re = new RegExp(`function ${fn}\\([\\s\\S]*?\\n  \\}`, 'g');
  const m = html.match(re);
  if (m) {
    vm.runInContext(m[0], sandbox);
  } else {
    console.log('Could not find', fn);
  }
}

const seasons = [
  { id: 8, name: "Season 5 (New Era)", active: true },
  { id: 7, name: "Season 4", active: false },
  { id: 6, name: "Season 3", active: false },
  { id: 5, name: "Season 2", active: false },
  { id: 4, name: "Season 1", active: false },
  { id: 3, name: "Pre Season", active: false },
  { id: 2, name: "Beta Test Season", active: false },
  { id: 1, name: "Alpha Test Season", active: false }
];

sandbox.seasons = seasons;
const res = vm.runInContext(`
  seasonMenuItemsFrom(seasons).map(item => {
    if (item.type === 'divider') return '--- DIVIDER ---';
    return {
      id: item.season.id,
      name: item.season.name,
      menuName: displaySeasonForMenu(item.season),
      order: seasonOrderValue(item.season)
    };
  })
`, sandbox);

console.log(JSON.stringify(res, null, 2));
