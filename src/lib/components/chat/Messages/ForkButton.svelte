<script lang="ts">
	// Alfie B2: "Fork from here" — forks the conversation from this message into
	// a NEW chat owned by the same user, then navigates to it. Self-contained so
	// the message components only need a 2-line wiring addition (fork discipline).
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { goto } from '$app/navigation';
	import { toast } from 'svelte-sonner';

	import { forkChatById, getChatList, getPinnedChatList } from '$lib/apis/chats';
	import { chats, pinnedChats, currentChatPage } from '$lib/stores';

	import Tooltip from '$lib/components/common/Tooltip.svelte';

	const i18n = getContext<Writable<i18nType>>('i18n');

	export let chatId: string = '';
	export let messageId: string = '';
	// History mode carried into the fork: 'path' (default) copies the direct
	// root→message chain. 'siblings' / 'full' also supported by the backend.
	export let mode: 'path' | 'siblings' | 'full' = 'path';
	export let visible: boolean = true;

	let forking = false;

	const forkHandler = async () => {
		if (forking || !chatId || !messageId) return;
		forking = true;

		const res = await forkChatById(localStorage.token, chatId, messageId, mode).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		forking = false;

		if (res) {
			toast.success($i18n.t('Forked into a new chat'));
			await goto(`/c/${res.id}`);
			currentChatPage.set(1);
			await chats.set(await getChatList(localStorage.token, $currentChatPage));
			await pinnedChats.set(await getPinnedChatList(localStorage.token));
		}
	};
</script>

<Tooltip content={$i18n.t('Fork from here')} placement="bottom">
	<button
		aria-label={$i18n.t('Fork from here')}
		class="{visible
			? 'visible'
			: 'invisible group-hover:visible'} p-1.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition fork-message-button disabled:opacity-50"
		disabled={forking}
		on:click={forkHandler}
	>
		<!-- branch / fork glyph -->
		<svg
			xmlns="http://www.w3.org/2000/svg"
			fill="none"
			viewBox="0 0 24 24"
			stroke-width="2.3"
			stroke="currentColor"
			aria-hidden="true"
			class="w-4 h-4"
		>
			<path
				stroke-linecap="round"
				stroke-linejoin="round"
				d="M6 3v12m0 0a3 3 0 1 0 0 6 3 3 0 0 0 0-6Zm12-9a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm0 0v1.5a6 6 0 0 1-6 6H9"
			/>
		</svg>
	</button>
</Tooltip>
