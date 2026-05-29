const express = require('express');
const router = express.Router();
const fetch = require('node-fetch');
const TRANSPARENT_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=',
  'base64'
);

async function proxyTile(targetUrl, res) {
  try {
    const response = await fetch(targetUrl, {
      headers: { 'User-Agent': 'Mozilla/5.0 DigitalTwin/1.0' },
      timeout: 15000,
    });

    if (!response.ok) {
      res.set('Content-Type', 'image/png');
      res.set('Access-Control-Allow-Origin', '*');
      res.set('Cache-Control', 'no-cache, no-store');
      return res.send(TRANSPARENT_PNG);
    }

    const contentType = response.headers.get('content-type') || 'image/png';
    res.set('Content-Type', contentType);
    res.set('Access-Control-Allow-Origin', '*');
    res.set('Cache-Control', 'public, max-age=86400');
    const buffer = await response.buffer();
    res.send(buffer);
  } catch (err) {
    res.set('Content-Type', 'image/png');
    res.set('Access-Control-Allow-Origin', '*');
    res.set('Cache-Control', 'no-cache, no-store');
    res.send(TRANSPARENT_PNG);
  }
}

// ── 기존 arctic-hybrid.html 호환 경로 ──────────────────────────
router.get('/', (req, res, next) => {
  next();
});

router.get('/nsidc', async (req, res) => {
  const targetUrl = req.query.url;
  if (!targetUrl) return res.status(400).json({ error: 'url parameter required' });
  await proxyTile(targetUrl, res);
});

router.get('/copernicus', async (req, res) => {
  const targetUrl = req.query.url;
  if (!targetUrl) return res.status(400).json({ error: 'url parameter required' });
  await proxyTile(targetUrl, res);
});

module.exports = router;

// ── NSIDC 프록시: 단순 패스스루 ────────────
module.exports.legacyNsidcProxy = async (req, res) => {
  let qs = require('url').parse(req.url).query || '';
  qs = qs.replace(/&?_cb=[^&]*/gi, '').replace(/^&/, '');
  const targetUrl = 'https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi?' + qs;
  await proxyTile(targetUrl, res);
};

module.exports.legacyCopProxy = async (req, res) => {
  const qs = require('url').parse(req.url).query || '';
  const targetUrl = 'https://wmts.marine.copernicus.eu/teroWmts?' + qs;
  await proxyTile(targetUrl, res);
};

module.exports.legacySentinelProxy = async (req, res) => {
  const qs = require('url').parse(req.url).query || '';
  const targetUrl = 'https://sh.dataspace.copernicus.eu/ogc/wms/710b2915-4bc6-4fd8-b204-7ee69682da3f?' + qs;
  await proxyTile(targetUrl, res);
};