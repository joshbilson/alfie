<script lang="ts">
	// Alfie B6: "Save to Note" — captures this message's content into the user's
	// own Notes as a NEW private note via the notes-create API. Phone-first
	// "keep this output" capture. Self-contained so the message components only
	// need a 2-line wiring addition (fork discipline). A2: the note is created
	// under the caller's user.id server-side (routers/notes.py create_new_note);
	// access_grants is left empty so it is private by default.
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { marked } from 'marked';
	import { toast } from 'svelte-sonner';

	import { createNewNote } from '$lib/apis/notes';

	import Tooltip from '$lib/components/common/Tooltip.svelte';

	const i18n = getContext<Writable<i18nType>>('i18n');

	// The markdown body to save (the assistant message content, or a code block).
	export let content: string = '';
	export let visible: boolean = true;

	let saving = false;

	// Build a sensible note title: first non-empty line (stripped of leading
	// markdown heading/list markers, capped), falling back to a dated default.
	const buildTitle = (md: string): string => {
		const firstLine = (md ?? '')
			.split('\n')
			.map((l) => l.trim())
			.find((l) => l.length > 0);

		if (firstLine) {
			const cleaned = firstLine.replace(/^#{1,6}\s+/, '').replace(/^[-*+]\s+/, '');
			if (cleaned.length > 0) {
				return cleaned.length > 80 ? `${cleaned.slice(0, 80)}…` : cleaned;
			}
		}

		const date = new Date().toLocaleDateString();
		return $i18n.t('Saved from chat {{date}}', { date });
	};

	const saveHandler = async () => {
		const md = (content ?? '').trim();
		if (saving || !md) return;
		saving = true;

		const res = await createNewNote(localStorage.token, {
			title: buildTitle(md),
			data: {
				content: {
					json: null,
					html: marked.parse(md),
					md
				}
			},
			meta: null,
			// Private by default — no sharing grants. Users opt into internal
			// sharing later from the Notes UI (public sharing stays off).
			access_grants: []
		}).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		saving = false;

		if (res) {
			toast.success($i18n.t('Saved to Notes'));
		}
	};
</script>

<Tooltip content={$i18n.t('Save to Notes')} placement="bottom">
	<button
		aria-label={$i18n.t('Save to Notes')}
		class="{visible
			? 'visible'
			: 'invisible group-hover:visible'} p-1.5 hover:bg-black/5 dark:hover:bg-white/5 rounded-lg dark:hover:text-white hover:text-black transition save-to-note-button disabled:opacity-50"
		disabled={saving}
		on:click={saveHandler}
	>
		<!-- document-plus glyph -->
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
				d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m2.25 12 3 3m0 0 3-3m-3 3v-6m-1.5-9H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z"
			/>
		</svg>
	</button>
</Tooltip>
