const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('public/index.html', 'utf8');
if (!html.includes('/app.js') || !html.includes('id="players"')) throw new Error('Missing web assets');
new vm.Script(fs.readFileSync('public/app.js', 'utf8'));
JSON.parse(fs.readFileSync('vercel.json', 'utf8'));
console.log('Web assets and Vercel JSON validated (static site, no bundling required).');
