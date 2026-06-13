<script lang="ts">
	import { getContext } from 'svelte';
	const i18n = getContext('i18n');

	import { config, settings } from '$lib/stores';
	import { getCodeBlockContents } from '$lib/utils';
	import { injectCsp } from '$lib/utils/csp';

	import SvgPanZoom from '$lib/components/common/SVGPanZoom.svelte';
	import ArrowsPointingOut from '$lib/components/icons/ArrowsPointingOut.svelte';
	import ChevronUp from '$lib/components/icons/ChevronUp.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';

	// The raw code from this code block (lang ∈ {html, svg, xml-with-svg}).
	export let lang = '';
	export let code = '';

	// Opens this artifact in the full side panel. Wired to the existing
	// onPreview() path (artifactCode → showArtifacts), so the Preview button's
	// behaviour is reused unchanged.
	export let onOpenPanel: (code: string) => void = () => {};

	let iframeElement: HTMLIFrameElement;
	let expanded = false;

	// Build the renderable artifact from this single code block using the SAME
	// extraction the side panel uses (getCodeBlockContents), so the inline set of
	// "renderable" artifacts is byte-identical to the panel's. We only ever pass
	// this block's own code in, so at most one artifact is produced.
	$: artifact = ((): { type: 'iframe' | 'svg'; content: string } | null => {
		if (!code) return null;
		const fenced = '```' + lang + '\n' + code + '\n```';
		const { codeBlocks, htmlGroups } = getCodeBlockContents(fenced) as {
			codeBlocks: Array<{ lang: string; code: string }>;
			htmlGroups: Array<{ html: string; css: string; js: string }>;
		};

		if (htmlGroups && htmlGroups.length > 0) {
			const group = htmlGroups[0];
			const renderedContent = `
				<!DOCTYPE html>
				<html lang="en">
				<head>
					<meta charset="UTF-8">
					<meta name="viewport" content="width=device-width, initial-scale=1.0">
					<${''}style>
						body { background-color: white; }
						${group.css}
					</${''}style>
				</head>
				<body>
					${group.html}
					<${''}script>
						${group.js}
					</${''}script>
				</body>
				</html>
			`;
			return { type: 'iframe', content: renderedContent };
		}

		for (const block of codeBlocks ?? []) {
			if (block.lang === 'svg' || (block.lang === 'xml' && block.code.includes('<svg'))) {
				return { type: 'svg', content: block.code };
			}
		}
		return null;
	})();

	const iframeLoadHandler = () => {
		try {
			iframeElement.contentWindow?.addEventListener(
				'click',
				function (e) {
					const target = (e.target as Element)?.closest('a');
					if (target && (target as HTMLAnchorElement).href) {
						e.preventDefault();
						const url = new URL((target as HTMLAnchorElement).href, iframeElement.baseURI);
						if (url.origin === window.location.origin) {
							iframeElement.contentWindow?.history.pushState(
								null,
								'',
								url.pathname + url.search + url.hash
							);
						} else {
							console.info('External navigation blocked:', url.href);
						}
					}
				},
				true
			);
		} catch {}
	};
</script>

{#if artifact}
	<div class="my-2 w-full rounded-2xl border border-gray-100 dark:border-gray-850 overflow-hidden">
		<div
			class="flex items-center justify-between px-2.5 py-1 bg-gray-50 dark:bg-gray-850 text-xs text-gray-500 dark:text-gray-400"
		>
			<span class="font-medium">{$i18n.t('Preview')}</span>
			<div class="flex items-center gap-1">
				<Tooltip content={expanded ? $i18n.t('Collapse') : $i18n.t('Expand')}>
					<button
						class="p-0.5 rounded-md hover:bg-black/5 dark:hover:bg-white/5 transition"
						aria-label={expanded ? $i18n.t('Collapse') : $i18n.t('Expand')}
						on:click={() => (expanded = !expanded)}
					>
						{#if expanded}
							<ChevronUp className="size-3.5" />
						{:else}
							<ChevronDown className="size-3.5" />
						{/if}
					</button>
				</Tooltip>
				<Tooltip content={$i18n.t('Open in full screen')}>
					<button
						class="p-0.5 rounded-md hover:bg-black/5 dark:hover:bg-white/5 transition"
						aria-label={$i18n.t('Open in full screen')}
						on:click={() => onOpenPanel(code)}
					>
						<ArrowsPointingOut className="size-3.5" />
					</button>
				</Tooltip>
			</div>
		</div>

		<div
			class="w-full bg-white dark:bg-gray-850 overflow-auto"
			style={expanded ? '' : 'max-height: 24rem;'}
		>
			{#if artifact.type === 'iframe'}
				<iframe
					bind:this={iframeElement}
					title={$i18n.t('Preview')}
					srcdoc={injectCsp(artifact.content, $config?.ui?.iframe_csp ?? '')}
					class="w-full border-0 bg-white"
					style={expanded ? 'height: 80vh;' : 'height: 24rem;'}
					sandbox="allow-scripts allow-downloads{($settings?.iframeSandboxAllowForms ?? false)
						? ' allow-forms'
						: ''}{($settings?.iframeSandboxAllowSameOrigin ?? false) ? ' allow-same-origin' : ''}"
					referrerpolicy="strict-origin-when-cross-origin"
					on:load={iframeLoadHandler}
				></iframe>
			{:else if artifact.type === 'svg'}
				<SvgPanZoom
					className="w-full {expanded ? 'h-[80vh]' : 'h-96'} max-h-full overflow-hidden"
					svg={artifact.content}
				/>
			{/if}
		</div>
	</div>
{/if}
