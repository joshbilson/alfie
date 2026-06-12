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

/**
 * Persisted opt-in marker. The browser's PushSubscription is destroyed when the
 * service worker is unregistered on an app update (see `+layout.svelte`'s
 * `unregisterServiceWorkers()`), so the live subscription cannot be the source of
 * truth for "did the user want push?". This localStorage flag survives the
 * update/reload and lets `resubscribeWebPush()` re-create a fresh subscription
 * without a user gesture, and lets the Settings toggle reflect the real intent.
 */
const PUSH_OPT_IN_KEY = 'alfie:push-opt-in';

const setPushOptIn = (): void => {
	try {
		localStorage.setItem(PUSH_OPT_IN_KEY, '1');
	} catch (e) {
		// localStorage may be unavailable (private mode / disabled); push still
		// works for this session, it just won't auto-restore after an update.
	}
};

const clearPushOptIn = (): void => {
	try {
		localStorage.removeItem(PUSH_OPT_IN_KEY);
	} catch (e) {
		// ignore — see setPushOptIn.
	}
};

const hasPushOptIn = (): boolean => {
	try {
		return localStorage.getItem(PUSH_OPT_IN_KEY) === '1';
	} catch (e) {
		return false;
	}
};

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

	// Persist the opt-in so push can be auto-restored after an app update wipes
	// the live subscription (R13).
	setPushOptIn();

	return subscription;
};

/**
 * Tear down the local browser subscription. The backend prunes stale
 * subscriptions on 404/410 delivery failures, so this only needs to drop the
 * browser-side registration to stop delivery to this device.
 */
export const unsubscribeUser = async (): Promise<boolean> => {
	// Clear the opt-in first so a deliberate disable is never resurrected by
	// `resubscribeWebPush()` on the next update, even if the unsubscribe below
	// throws or there is no live subscription to drop.
	clearPushOptIn();

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
 * Whether push is "on" for this browser, for reflecting the toggle state on
 * mount and after an app update.
 *
 * The opt-in marker is the source of truth: after an app update the live
 * `PushSubscription` is destroyed and a fresh one is (re)created asynchronously
 * by `resubscribeWebPush()`. Reading only `getSubscription()` would briefly show
 * OFF during that window. So when permission is granted AND the user opted in,
 * report ON regardless of whether the fresh subscription has materialised yet.
 * Otherwise fall back to whether a live subscription exists.
 */
export const getPushSubscriptionState = async (): Promise<boolean> => {
	if (!isPushSupported() || Notification.permission !== 'granted') {
		return false;
	}

	if (hasPushOptIn()) {
		return true;
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
 * Re-subscribe push after an app update / service-worker re-registration (R13).
 *
 * The app-update path (`+layout.svelte`) unregisters the service worker, which
 * DESTROYS the live `PushSubscription`. After the post-update reload there is
 * therefore no subscription, so simply re-POSTing an existing one is not enough —
 * push would be silently lost. We instead use the persisted opt-in marker as the
 * source of truth:
 *
 *   - Marker absent  -> user never opted in (or deliberately disabled). Leave
 *     push off; this is the only early-return.
 *   - Marker present, live subscription exists -> re-POST it so the backend
 *     record cannot drift out of sync.
 *   - Marker present, no live subscription (the update case) -> RE-CREATE a fresh
 *     subscription and POST it. No user gesture is needed because permission is
 *     already 'granted'.
 *
 * Never prompts (it bails unless permission is already granted) and never
 * resurrects push the user turned off (it bails unless the marker is set). Safe
 * to call unconditionally from the update path.
 */
export const resubscribeWebPush = async (token: string): Promise<void> => {
	if (!isPushSupported() || Notification.permission !== 'granted') {
		return;
	}

	// The opt-in marker is the source of truth. If it is absent the user is
	// deliberately off — do nothing.
	if (!hasPushOptIn()) {
		return;
	}

	try {
		const registration = await navigator.serviceWorker.ready;
		const existing = await registration.pushManager.getSubscription();

		if (existing) {
			// Subscription survived (e.g. a soft reload): re-POST it so the
			// backend record cannot drift out of sync.
			await subscribeToWebPush(token, existing.toJSON());
			return;
		}

		// The update path unregistered the SW and destroyed the subscription.
		// Re-create it fresh — permission is already granted, so no gesture is
		// required.
		const publicKey = await getVapidPublicKey(token);
		if (!publicKey) {
			return;
		}

		const subscription = await registration.pushManager.subscribe({
			userVisibleOnly: true,
			applicationServerKey: urlBase64ToUint8Array(publicKey)
		});

		await subscribeToWebPush(token, subscription.toJSON());
	} catch (e) {
		console.error('Failed to re-subscribe web push after update:', e);
	}
};
