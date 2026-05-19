const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const www = path.join(root, 'www');

fs.mkdirSync(www, { recursive: true });
for (const entry of fs.readdirSync(www)) {
  try {
    fs.rmSync(path.join(www, entry), { recursive: true, force: true });
  } catch (error) {
    console.warn(`Skipped locked generated asset: ${entry}`);
  }
}

fs.copyFileSync(path.join(root, 'templates', 'index.html'), path.join(www, 'index.html'));
fs.cpSync(path.join(root, 'static'), path.join(www, 'static'), { recursive: true });

const manifest = {
  name: 'Touch Dashboard',
  short_name: 'TouchDash',
  start_url: 'index.html',
  display: 'standalone',
  background_color: '#090e17',
  theme_color: '#090e17',
  icons: [
    { src: 'static/icon-192.png', sizes: '192x192', type: 'image/png' },
    { src: 'static/icon-512.png', sizes: '512x512', type: 'image/png' }
  ]
};

fs.writeFileSync(path.join(www, 'manifest.json'), JSON.stringify(manifest, null, 2));
console.log('Prepared Capacitor web assets in www/');
