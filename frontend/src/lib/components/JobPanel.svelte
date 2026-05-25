<script lang="ts">
	import { onMount } from 'svelte';
	import { slide } from 'svelte/transition';
	import { browser } from '$app/environment';

	type JobStatus = 'queued' | 'running' | 'completed' | 'failed';

	interface Job {
		id: string;
		status: JobStatus;
		current_step?: string;
		current_tile?: number;
		total_tiles?: number;
		created_at: string;
		error?: string;
		params: Record<string, unknown>;
	}

	let {
		backendUrl,
		drawingMode,
		bboxStart,
		bboxEnd,
		onStartDrawing,
		onCancelDrawing
	}: {
		backendUrl: string;
		drawingMode: 'none' | 'first-click' | 'second-click' | 'done';
		bboxStart: [number, number] | null;
		bboxEnd: [number, number] | null;
		onStartDrawing: () => void;
		onCancelDrawing: () => void;
	} = $props();

	// Auth
	let username = $state(browser ? (localStorage.getItem('vf_username') ?? '') : '');
	let password = $state(browser ? (localStorage.getItem('vf_password') ?? '') : '');
	let authError = $state('');
	let authenticated = $state(false);

	// UI state
	let expanded = $state(false);
	let showNewJob = $state(false);
	let wayType = $state('railway');
	let submitting = $state(false);
	let submitError = $state('');

	// Jobs
	let jobs = $state<Job[]>([]);

	// Keep panel open when the new-job form is active or drawing is in progress
	let pinned = $derived(showNewJob || drawingMode !== 'none');

	// Indicator for trigger button
	let indicator = $derived.by((): 'idle' | 'running' | 'done' | 'failed' => {
		if (jobs.length === 0) return 'idle';
		if (jobs.some((j) => j.status === 'running' || j.status === 'queued')) return 'running';
		const last = [...jobs].sort(
			(a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
		)[0];
		return last.status === 'failed' ? 'failed' : 'done';
	});

	// Bounding box computed from drawn corners
	let bbox = $derived.by(() => {
		if (!bboxStart || !bboxEnd) return null;
		return {
			min_lat: Math.min(bboxStart[1], bboxEnd[1]),
			min_lon: Math.min(bboxStart[0], bboxEnd[0]),
			max_lat: Math.max(bboxStart[1], bboxEnd[1]),
			max_lon: Math.max(bboxStart[0], bboxEnd[0])
		};
	});

	// Sorted jobs: newest first
	let sortedJobs = $derived(
		[...jobs].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
	);

	// Auto-refresh while authenticated (keeps button indicator current even when panel is closed)
	$effect(() => {
		if (authenticated) {
			fetchJobs();
			const timer = setInterval(fetchJobs, 5000);
			return () => clearInterval(timer);
		}
	});

	function authHeader() {
		return 'Basic ' + btoa(`${username}:${password}`);
	}

	async function tryLogin() {
		authError = '';
		try {
			const res = await fetch(`${backendUrl}/api/jobs/`, {
				headers: { Authorization: authHeader() }
			});
			if (res.status === 401) {
				authError = 'Invalid credentials';
				return;
			}
			if (!res.ok) {
				authError = 'Server error';
				return;
			}
			if (browser) {
				localStorage.setItem('vf_username', username);
				localStorage.setItem('vf_password', password);
			}
			authenticated = true;
			jobs = await res.json();
		} catch {
			authError = 'Connection failed';
		}
	}

	async function fetchJobs() {
		try {
			const res = await fetch(`${backendUrl}/api/jobs/`, {
				headers: { Authorization: authHeader() }
			});
			if (res.ok) {
				jobs = await res.json();
			} else if (res.status === 401) {
				authenticated = false;
			}
		} catch {
			// ignore transient network errors
		}
	}

	async function submitJob() {
		if (!bbox) return;
		submitting = true;
		submitError = '';
		try {
			const res = await fetch(`${backendUrl}/api/jobs/`, {
				method: 'POST',
				headers: {
					'Content-Type': 'application/json',
					Authorization: authHeader()
				},
				body: JSON.stringify({ bounding_box: bbox, way_type: wayType })
			});
			if (!res.ok) {
				submitError = 'Failed to create job';
				return;
			}
			showNewJob = false;
			onCancelDrawing();
			await fetchJobs();
		} catch {
			submitError = 'Connection failed';
		} finally {
			submitting = false;
		}
	}

	function cancelNewJob() {
		showNewJob = false;
		onCancelDrawing();
	}

	// Auto-login with cached credentials on mount
	onMount(() => {
		if (username && password) tryLogin();
	});

	const stepLabels: Record<string, string> = {
		importing_ways: 'Importing ways',
		sampling_points: 'Sampling points',
		retrieving_dem: 'Retrieving elevation',
		performing_viewshed: 'Computing viewshed',
		saving_results: 'Saving results'
	};

	function stepLabel(job: Job): string {
		if (!job.current_step) return '';
		const label = stepLabels[job.current_step] ?? job.current_step;
		if (job.current_tile != null && job.total_tiles != null) {
			return `${label} (${job.current_tile}/${job.total_tiles})`;
		}
		return label;
	}

	function statusColor(status: JobStatus): string {
		switch (status) {
			case 'running':
				return 'text-blue-400';
			case 'queued':
				return 'text-yellow-400';
			case 'completed':
				return 'text-green-400';
			case 'failed':
				return 'text-red-400';
		}
	}

	function statusLabel(status: JobStatus): string {
		switch (status) {
			case 'running':
				return '▶ Running';
			case 'queued':
				return '⏳ Queued';
			case 'completed':
				return '✓ Done';
			case 'failed':
				return '✗ Failed';
		}
	}

	let drawInstruction = $derived(
		drawingMode === 'first-click'
			? 'Click the first corner on the map'
			: drawingMode === 'second-click'
				? 'Click the opposite corner on the map'
				: ''
	);
</script>

<!--
  Job management panel — hover the button to expand.
  Pinned open while new-job form is active or bbox is being drawn.
-->
<div
	class="absolute left-2.5 top-2.5 z-20 flex flex-col items-start"
	onmouseenter={() => (expanded = true)}
	onmouseleave={() => {
		if (!pinned) expanded = false;
	}}
	role="region"
	aria-label="Job management"
>
	<!-- Trigger button -->
	<button
		class="flex items-center gap-1.5 rounded bg-black/75 px-3 py-2 text-white shadow backdrop-blur-sm hover:bg-black/90"
		onclick={() => (expanded = !expanded)}
		aria-expanded={expanded}
		title="Job management"
	>
		<!-- Gear icon -->
		<svg
			xmlns="http://www.w3.org/2000/svg"
			class="h-4 w-4"
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="2"
			stroke-linecap="round"
			stroke-linejoin="round"
			aria-hidden="true"
		>
			<circle cx="12" cy="12" r="3"></circle>
			<path
				d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"
			></path>
		</svg>
		<span class="text-xs font-medium">Jobs</span>
		<!-- Status indicator -->
		{#if indicator === 'running'}
			<span
				class="inline-block h-3 w-3 animate-spin rounded-full border-2 border-gray-400 border-t-white"
				aria-label="Job running"
			></span>
		{:else if indicator === 'done'}
			<span class="text-xs font-bold text-green-400" aria-label="All jobs done">✓</span>
		{:else if indicator === 'failed'}
			<span class="text-xs font-bold text-red-400" aria-label="Last job failed">✗</span>
		{/if}
	</button>

	<!-- Expandable panel -->
	{#if expanded || pinned}
		<div
			class="mt-1 w-72 overflow-hidden rounded bg-black/75 text-white shadow backdrop-blur-sm"
			transition:slide={{ duration: 150 }}
		>
			{#if !authenticated}
				<!-- ── Login form ── -->
				<div class="flex flex-col gap-2 p-3">
					<p class="text-xs font-semibold text-gray-300">Sign in to manage jobs</p>
					<input
						class="rounded bg-gray-800 px-2 py-1.5 text-xs text-white placeholder-gray-500 outline-none focus:ring-1 focus:ring-blue-500"
						type="text"
						placeholder="Username"
						bind:value={username}
						autocomplete="username"
					/>
					<input
						class="rounded bg-gray-800 px-2 py-1.5 text-xs text-white placeholder-gray-500 outline-none focus:ring-1 focus:ring-blue-500"
						type="password"
						placeholder="Password"
						bind:value={password}
						autocomplete="current-password"
						onkeydown={(e) => e.key === 'Enter' && tryLogin()}
					/>
					{#if authError}
						<p class="text-xs text-red-400">{authError}</p>
					{/if}
					<button
						class="rounded bg-blue-600 px-3 py-1.5 text-xs text-white hover:bg-blue-500"
						onclick={tryLogin}
					>
						Sign in
					</button>
				</div>
			{:else if showNewJob}
				<!-- ── New job form ── -->
				<div class="flex flex-col gap-3 p-3">
					<div class="flex items-center justify-between">
						<p class="text-xs font-semibold">New Job</p>
						<button class="text-xs text-gray-400 hover:text-white" onclick={cancelNewJob}
							>Cancel</button
						>
					</div>

					<!-- Way type selector -->
					<div>
						<p class="mb-1 text-xs text-gray-400">Way type</p>
						<div class="flex rounded overflow-hidden border border-gray-600">
							{#each ['railway', 'road', 'path'] as wt (wt)}
								<button
									class="flex-1 px-2 py-1.5 text-xs transition-colors
										{wayType === wt
											? 'bg-blue-600 text-white'
											: 'bg-gray-800 text-gray-300 hover:bg-gray-700'}"
									onclick={() => (wayType = wt)}
								>
									{wt}
								</button>
							{/each}
						</div>
					</div>

					<!-- Bounding box -->
					<div>
						<p class="mb-1 text-xs text-gray-400">Bounding box</p>
						{#if drawingMode === 'none' && !bbox}
							<button
								class="w-full rounded bg-gray-700 px-2 py-1.5 text-xs hover:bg-gray-600"
								onclick={onStartDrawing}
							>
								Draw on map
							</button>
						{:else if drawingMode === 'first-click' || drawingMode === 'second-click'}
							<p class="text-xs italic text-yellow-300">{drawInstruction}</p>
						{:else if bbox}
							<p class="font-mono text-xs text-gray-300">
								({bbox.min_lat.toFixed(4)}, {bbox.min_lon.toFixed(4)}) →<br />
								({bbox.max_lat.toFixed(4)}, {bbox.max_lon.toFixed(4)})
							</p>
							<button
								class="mt-1 text-xs text-gray-400 hover:text-white"
								onclick={() => { onCancelDrawing(); onStartDrawing(); }}
							>
								Redraw
							</button>
						{/if}
					</div>

					{#if submitError}
						<p class="text-xs text-red-400">{submitError}</p>
					{/if}

					<button
						class="rounded bg-blue-600 px-3 py-1.5 text-xs text-white hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
						disabled={!bbox || submitting}
						onclick={submitJob}
					>
						{submitting ? 'Submitting…' : 'Submit Job'}
					</button>
				</div>
			{:else}
				<!-- ── Jobs list ── -->
				<div class="flex flex-col gap-2 p-3">
					<div class="flex items-center justify-between">
						<p class="text-xs font-semibold">Jobs</p>
						<button
							class="rounded bg-blue-600 px-2 py-1 text-xs text-white hover:bg-blue-500"
							onclick={() => (showNewJob = true)}
						>
							+ New
						</button>
					</div>

					{#if sortedJobs.length === 0}
						<p class="text-xs text-gray-400">No jobs yet.</p>
					{:else}
						<div class="flex max-h-64 flex-col gap-1.5 overflow-y-auto pr-0.5">
							{#each sortedJobs as job (job.id)}
								<div class="rounded bg-gray-800 px-2 py-1.5">
									<div class="flex items-center justify-between">
										<span class="font-mono text-xs text-gray-300">{job.id}</span>
										<span class="text-xs {statusColor(job.status)}"
											>{statusLabel(job.status)}</span
										>
									</div>
									{#if job.status === 'running' && job.current_step}
										<p class="mt-0.5 text-xs text-gray-400">{stepLabel(job)}</p>
									{/if}
									{#if job.status === 'failed' && job.error}
										<p
											class="mt-0.5 truncate text-xs text-red-400"
											title={job.error}
										>
											{job.error}
										</p>
									{/if}
									<p class="mt-0.5 text-xs text-gray-500">
										{new Date(job.created_at).toLocaleString()}
									</p>
								</div>
							{/each}
						</div>
					{/if}
				</div>
			{/if}
		</div>
	{/if}
</div>
