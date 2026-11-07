const CACHE_NAME = 'motogparr-v1.6';
const OFFLINE_URL = '/';

const ASSETS_TO_CACHE = [
    '/',
    '/static/css/style.css',
    '/static/js/app.js',
    '/assets/icon-192x192.png',
    '/assets/icon-512x512.png'
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS_TO_CACHE))
    );
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.map((cacheName) => {
                    if (cacheName !== CACHE_NAME) return caches.delete(cacheName);
                })
            );
        })
    );
    self.clients.claim();
});

self.addEventListener('fetch', (event) => {
    if (event.request.method !== 'GET') return;
    
    if (event.request.url.includes('/logs/') || 
        event.request.url.includes('/run') ||
        event.request.url.includes('/settings') ||
        event.request.url.includes('/status') ||
        event.request.url.includes('/library-status') ||
        event.request.url.includes('/app-config') ||
        event.request.url.includes('/release-mismatch') ||
        event.request.url.includes('/test-connections') ||
        // Health chips on the home tab: a cached queue/connection answer would
        // be a stale claim about whether the machine is alive right now.
        event.request.url.includes('/api/sabnzbd')) {
        return;
    }
    
    event.respondWith(
        fetch(event.request)
            .then((response) => {
                if (response.status === 200) {
                    const responseClone = response.clone();
                    caches.open(CACHE_NAME).then((cache) => cache.put(event.request, responseClone));
                }
                return response;
            })
            .catch(() => caches.match(event.request).then((response) => response || caches.match(OFFLINE_URL)))
    );
});
