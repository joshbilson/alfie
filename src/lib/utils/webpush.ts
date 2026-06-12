import { WEBUI_API_BASE_URL } from '$lib/constants';

/**
 * Native web-push subscribe / unsubscribe helpers.
 *
 * Backend contract (Phase-3 backend agent owns these endpoints):
 *   GET  /api/v1/webpush/vapid-public-key -> { "publicKey": "<base64url applicationServerKey>" }
 *   POST /api/v1/webpush/subscribe         <- the browser PushSubscription JSON
 *                                             (stored into user.settings.ui.notifications.push_subscriptions)
 *
 * The VAPID public key is a base64url-encoded P-256 public point (the value
 * referenced by op://happypixels/alfie-stack/webpush_vapid_public). The browser's
 * PushManager requires it as a Uint8Array, hence `urlBase64ToUint8Array` below.
 *
 * Platform reality:
 *   - `Notification.requestPermission()` must be invoked from a user gesture.
 *   - On iOS/iPadOS, push only works when the app is installed to the home screen
 *     (standalone display mode). Browsers there report `'PushManager' in window`
 *     as false until installed.
 */

export const isPushSupported = (): boolean => {
	return (
		typeof navigator !== 'undefined' &&
		'serviceWorker' in navigator &&
		typeof window !== 'undefined' &&
		'PushManager' in window &&
		'Notification' in window
	);
};

/**
 * iOS/iPadOS only deliver web push to a home-screen-installed PWA. Returns true
 * when the current context is a Safari/WebKit browser tab that has NOT been
 * installed, so the UI can surface the "add to Home Screen" guidance.
 */
export const isIosUninstalledPwa = (): boolean => {
	if (typeof navigator === 'undefined' || typeof window === 'undefined') {
		return false;
	}

	const ua = navigator.userAgent || '';
	const isIos = /iP(hone|ad|od)/.test(ua) || (/Macintosh/.test(ua) && 'ontouchend' in document);

	if (!isIos) {
		return false;
	}

	const standalone =
		(window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) ||
		// @ts-ignore - non-standard iOS Safari flag
		window.navigator.standalone === true;

	return !standalone;
};

// Standard helper: base64url string -> Uint8Array for applicationServerKey.
export const urlBase64ToUint8Array = (base64String: string): Uint8Array => {
	const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
	const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');

	const rawData = atob(base64);
	const outputArray = new Uint8Array(rawData.length);
	for (let i = 0; i < rawData.length; ++i) {
		outputArray[i] = rawData.charCodeAt(i);
	}
	return outputArray;
};

export const getVapidPublicKey = async (token: string): Promise<string> => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/webpush/vapid-public-key`, {
		method: 'GET',
		headers: {
			'Content-Type': 'application/json',
			...(token && { Authorization: `Bearer ${token}` })
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			error = err.detail ?? err;
			return null;
		});

	if (error) {
		throw error;
	}

	return res?.publicKey ?? '';
};

export const subscribeToWebPush = async (
	token: string,
	subscription: PushSubscriptionJSON
) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/webpush/subscribe`, {
		method: 'POST',
		headers: {
			'Content-Type': 'application/json',
			...(token && { Authorization: `Bearer ${token}` })
		},
		body: JSON.stringify(subscription)
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			error = err.detail ?? err;
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

/**
 * Returns the active service-worker registration, waiting for it to become ready.
 * SvelteKit auto-registers `src/service-worker.js`, so we only ever read here.
 */
const getRegistration = async (): Promise<ServiceWorkerRegistration> => {
	return navigator.serviceWorker.ready;
};

/**
 * Subscribe the browser to web push and persist the subscription server-side.
 * MUST be called from a user gesture (it requests Notification permission).
 *
 * Returns the PushSubscription on success, or null when permission is denied /
 * push is unsupported.
 */
export const subscribeUser = async (token: string): Promise<PushSubscription | null> => {
	if (!isPushSupported()) {
		throw new Error('Push notifications are not supported in this browser.');
	}

	const permission = await Notification.requestPermission();
	if (permission !== 'granted') {
		return null;
	}

	const registration = await getRegistration();

	// Reuse an existing subscription if one is already present, otherwise create one.
	let subscription = await registration.pushManager.getSubscription();
	if (!subscription) {
		const publicKey = await getVapidPublicKey(token);
		if (!publicKey) {
			throw new Error('Server did not return a VAPID public key.');
		}

		subscription = await registration.pushManager.subscribe({
			userVisibleOnly: true,
			applicationServerKey: urlBase64ToUint8Array(publicKey)
		});
	}

	await subscribeToWebPush(token, subscription.toJSON());

	return subscription;
};

/**
 * Tear down the local browser subscription. The backend prunes stale
 * subscriptions on 404/410 delivery failures, so this only needs to drop the
 * browser-side registration to stop delivery to this device.
 */
export const unsubscribeUser = async (): Promise<boolean> => {
	if (!isPushSupported()) {
		return false;
	}

	const registration = await getRegistration();
	const subscription = await registration.pushManager.getSubscription();

	if (!subscription) {
		return true;
	}

	return subscription.unsubscribe();
};

/**
 * Whether this browser currently holds an active push subscription. Used to
 * reflect the toggle state on mount and after an app update.
 */
export const getPushSubscriptionState = async (): Promise<boolean> => {
	if (!isPushSupported() || Notification.permission !== 'granted') {
		return false;
	}

	try {
		const registration = await navigator.serviceWorker.ready;
		const subscription = await registration.pushManager.getSubscription();
		return subscription !== null;
	} catch (e) {
		return false;
	}
};

/**
 * Re-subscribe push after an app update / service-worker re-registration.
 * Only acts when the user previously granted permission AND already had a live
 * subscription, so it never prompts and never resurrects push the user disabled.
 * Safe to call unconditionally from the update path (R13).
 */
export const resubscribeWebPush = async (token: string): Promise<void> => {
	if (!isPushSupported() || Notification.permission !== 'granted') {
		return;
	}

	try {
		const registration = await navigator.serviceWorker.ready;
		const existing = await registration.pushManager.getSubscription();
		if (!existing) {
			// User had not opted in (or unsubscribed) — leave push off.
			return;
		}

		const publicKey = await getVapidPublicKey(token);
		if (!publicKey) {
			return;
		}

		// The push endpoint survives SW re-registration, but re-POST the
		// subscription so the backend record cannot drift out of sync.
		await subscribeToWebPush(token, existing.toJSON());
	} catch (e) {
		console.error('Failed to re-subscribe web push after update:', e);
	}
};
