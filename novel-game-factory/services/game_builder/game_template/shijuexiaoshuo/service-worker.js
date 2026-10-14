'use strict';

// The name of your game, no spaces or special characters.
const name = 'Douluodalu';

// The version of the cache, changing this will force everything to be cached
// again.
const version = '0.1.0';

// Keep the cache name in one place. A chapter is served below a nested URL
// (for example /<game>/<chapter>/), so the relative root must be './' rather
// than '/'. The latter escapes the chapter scope and can cache the wrong
// application's index page.
const cacheName = `${name}-v${version}`;

const files = [

	'./',

	// General Files
	'manifest.json',

	// Engine Files
	'engine/core/monogatari.css',
	'engine/core/monogatari.js',

	// HTML Files
	'index.html',

	// Style Sheets
	'style/main.css',

	// JavaScript Files
	'js/options.js',
	'js/storage.js',
	'js/script.js',
	'js/main.js',

	// App Images
	//
	// PWA icons are deliberately absent: they live on the asset CDN after the
	// build uploads assets/, so precaching them would only issue cross-origin
	// no-cors requests for files the game never reads at runtime.
	'favicon.ico'
];

self.addEventListener ('install', (event) => {
	self.skipWaiting ();
	event.waitUntil (
		caches.open (cacheName).then ((cache) => {
			// Assets may be published to OSS/CDN and removed from the local
			// chapter directory.  One missing optional icon must not make the
			// worker installation fail (which would leave an old worker serving
			// an old script forever).  Cache each file independently.
			return Promise.all (files.map ((file) => cache.add (file).catch ((error) => {
				console.warn ('[ServiceWorker] optional precache failed:', file, error);
				return null;
			})));
		})
	);
});

self.addEventListener ('activate', (event) => {
	event.waitUntil (
		caches.keys ().then ((keyList) => {
			return Promise.all (keyList.map ((key) => {
				// Only evict older versions belonging to this chapter.  Deleting
				// every cache also removes caches for sibling chapters opened in
				// the same origin.
				if (key.startsWith (`${name}-v`) && key !== cacheName) {
					return caches.delete (key);
				}
				return undefined;
			}));
		})
	);
	return self.clients.claim ();
});

self.addEventListener ('fetch', (event) => {

	if (event.request.method !== 'GET') {
		return;
	}

	// Online builds must pick up rebuilt HTML/JS immediately.  The previous
	// cache-first strategy could keep serving an old script (including old UI
	// click/back handlers) indefinitely because the active worker also cached
	// service-worker.js itself.  Use network-first for application code and the
	// worker script; static media remains cache-first for fast playback.
	const requestPath = new URL(event.request.url).pathname;
	const networkFirst = /(?:^|\/)(?:index\.html|service-worker\.js|js\/|style\/)/i.test(requestPath);

	event.respondWith (
		(networkFirst ? fetch (event.request).then ((response) => {
			const cacheCopy = response.clone ();
			caches.open (cacheName).then ((cache) => cache.put (event.request, cacheCopy));
			return response;
		}).catch (() => caches.match (event.request)) : caches.match (event.request)).then ((cached) => {
			if (networkFirst) return cached || unableToResolve ();
			function fetchedFromNetwork (response) {
				const cacheCopy = response.clone ();

				caches.open (cacheName).then (function add (cache) {
					cache.put (event.request, cacheCopy);
				});
				return response;
			}

			function unableToResolve () {
				return new Response (`
					<!DOCTYPE html><html lang=en><title>Bad Request</title><meta charset=UTF-8><meta content="width=device-width,initial-scale=1"name=viewport><style>body,html{width:100%;height:100%}body{text-align:center;color:#545454;margin:0;display:flex;justify-content:center;align-items:center;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Oxygen,Ubuntu,Cantarell,"Open Sans","Fira Sans","Droid Sans","Helvetica Neue",sans-serif}h1,h2{font-weight:lighter}h1{font-size:4em}h2{font-size:2em}</style><div><h1>Service Unavailable</h1><h2>Sorry, the server is currently unavailable or under maintenance, try again later.</h2></div>
				`, {
					status: 503,
					statusText: 'Service Unavailable',
					headers: new Headers ({
						'Content-Type': 'text/html'
					})
				});
			}

			const networked = fetch (event.request)
				.then (fetchedFromNetwork, unableToResolve)
				.catch (unableToResolve);

			return cached || networked;
		})
	);
});
