/**
 * Alfie native web-push service worker.
 *
 * SvelteKit (adapter-static) auto-registers this file as the app service worker,
 * so it is the single place where push payloads from the backend are turned into
 * OS notifications. Keep it dependency-free: service workers run in their own
 * global scope with no access to `$lib`, the DOM, or any bundled app code.
 *
 * Payload contract (backend/open_webui/utils/webpush.py sends this exact JSON):
 *   {
 *     "title": string,
 *     "body":  string,
 *     "url":   string,   // path or absolute URL to open on click
 *     "icon":  string?,  // optional, falls back to the app icon
 *     "tag":   string?   // optional, collapses duplicate notifications
 *   }
 *
 * The handlers are defensive: a malformed or empty payload must never throw out
 * of the `push` event (an unhandled rejection there can disable push delivery on
 * some platforms), so every access is guarded and falls back to safe defaults.
 */

const DEFAULT_TITLE = 'Alfie';
const DEFAULT_ICON = '/static/web-app-manifest-192x192.png';
const DEFAULT_BADGE = '/static/favicon-96x96.png';

self.addEventListener('push', (event) => {
	let payload = {};

	try {
		if (event.data) {
			// Prefer JSON; gracefully degrade to plain text if the body is not JSON.
			try {
				payload = event.data.json() ?? {};
			} catch (e) {
				payload = { body: event.data.text() };
			}
		}
	} catch (e) {
		payload = {};
	}

	const title = typeof payload.title === 'string' && payload.title.trim() ? payload.title : DEFAULT_TITLE;

	const url = typeof payload.url === 'string' && payload.url ? payload.url : '/';

	const options = {
		body: typeof payload.body === 'string' ? payload.body : '',
		icon: typeof payload.icon === 'string' && payload.icon ? payload.icon : DEFAULT_ICON,
		badge: DEFAULT_BADGE,
		data: { url },
		// Collapse repeat notifications for the same chat/channel when the
		// backend supplies a tag; otherwise each push stacks separately.
		...(typeof payload.tag === 'string' && payload.tag ? { tag: payload.tag } : {})
	};

	event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
	event.notification.close();

	const targetUrl = event.notification?.data?.url || '/';

	event.waitUntil(
		(async () => {
			const absoluteUrl = new URL(targetUrl, self.location.origin).href;

			const clientList = await self.clients.matchAll({
				type: 'window',
				includeUncontrolled: true
			});

			// Focus an already-open Alfie tab if one exists; navigate it to the
			// target if it supports it, otherwise just bring it forward.
			for (const client of clientList) {
				try {
					if (new URL(client.url).origin === self.location.origin) {
						await client.focus();
						if ('navigate' in client && absoluteUrl !== client.url) {
							await client.navigate(absoluteUrl).catch(() => {});
						}
						return;
					}
				} catch (e) {
					// Ignore clients with opaque/cross-origin URLs we cannot inspect.
				}
			}

			// No existing window — open a fresh one.
			if (self.clients.openWindow) {
				await self.clients.openWindow(absoluteUrl);
			}
		})()
	);
});
