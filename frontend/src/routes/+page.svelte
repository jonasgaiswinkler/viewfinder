<script lang="ts">
	import {
		AttributionControl,
		FillLayer,
		GeoJSONSource,
		ImageLoader,
		LineLayer,
		MapLibre,
		Marker,
		SymbolLayer,
		VectorTileSource
	} from 'svelte-maplibre-gl';
	import { env } from '$env/dynamic/public';
	import type { LngLatLike } from 'maplibre-gl';

	import arrowImageUrl from '$lib/assets/arrow.png';
	import arrowBlueImageUrl from '$lib/assets/arrow_blue.png';
	import arrowReverseImageUrl from '$lib/assets/arrow_reverse.png';
	import JobPanel from '$lib/components/JobPanel.svelte';

	let mapStyle =
		'https://api.maptiler.com/maps/019c19fe-40db-76a8-a85c-b8a711d9f632/style.json?key=OdQtdfaWeZHG7a1SfGoY';
	let attribution = `<a href="https://www.jonasgaiswinkler.eu/imprint" target="_blank">Imprint</a>`;

	let vectorTiles = [`${env.PUBLIC_TEGOLA_URL}/maps/viewfinder/{z}/{x}/{y}`];
	let backendUrl = env.PUBLIC_BACKEND_URL ?? '';

	// --- Scenicness style config ---
	let scenicLow = $state(0);
	let scenicHigh = $state(1);
	let scenicColor = 'red';
	let routeColor = '#0066ff';

	// Fetch scenic range from backend
	async function fetchScenicRange() {
		try {
			const res = await fetch(`${backendUrl}/api/scenicness-range`);
			if (res.ok) {
				const data = await res.json();
				scenicLow = data.low;
				scenicHigh = data.high;
			}
		} catch (err) {
			console.error('Failed to fetch scenicness range:', err);
		}
	}

	fetchScenicRange();

	// Helper: interpolate on start_total_value between scenicLow and scenicHigh
	function scenicInterp(low: unknown, high: unknown): maplibregl.ExpressionSpecification {
		return ['interpolate', ['linear'], ['get', 'start_total_value'], scenicLow, low, scenicHigh, high] as unknown as maplibregl.ExpressionSpecification;
	}

	const arrowRotation: maplibregl.ExpressionSpecification =
		['case', ['>', ['get', 'start_relative_value'], 0], 90, -90] as unknown as maplibregl.ExpressionSpecification;

	const notTunnelFilter: maplibregl.ExpressionSpecification =
		['!=', ['get', 'is_tunnel'], true] as unknown as maplibregl.ExpressionSpecification;

	const scenicLinePaint = (lowColor: string, highColor: string) => ({
		'line-opacity': scenicInterp(0, 1),
		'line-color': scenicInterp(lowColor, highColor),
		'line-width': scenicInterp(2, 5)
	});

	// --- Routing state ---
	interface RouteResult {
		features: GeoJSON.Feature[];
		properties: {
			avg_relative_value: number;
			avg_total_value: number;
			total_length_m: number;
		};
	}

	let markers: LngLatLike[] = $state([]);
	let routeResult = $state<RouteResult | null>(null);
	let routeLoading = $state(false);
	let showScenicness = $derived(!routeResult);

	// Scenicness layer visibility driven by whether a route is active
	let scenicnessVisibility: 'visible' | 'none' = $derived(showScenicness ? 'visible' : 'none');

	// Empty GeoJSON used when no route result yet
	const emptyGeoJSON: GeoJSON.FeatureCollection = {
		type: 'FeatureCollection',
		features: []
	};

	let routeGeoJSON: GeoJSON.FeatureCollection = $derived(
		routeResult ? { type: 'FeatureCollection', features: routeResult.features } : emptyGeoJSON
	);

	function handleMapClick(e: maplibregl.MapMouseEvent) {
		if (drawingMode === 'first-click') {
			bboxStart = [e.lngLat.lng, e.lngLat.lat];
			drawingMode = 'second-click';
			return;
		}
		if (drawingMode === 'second-click') {
			bboxEnd = [e.lngLat.lng, e.lngLat.lat];
			mousePos = null;
			drawingMode = 'done';
			return;
		}
		const lngLat: LngLatLike = [e.lngLat.lng, e.lngLat.lat];
		markers = [...markers, lngLat];

		if (markers.length >= 2) {
			fetchRoute();
		}
	}

	async function fetchRoute() {
		routeLoading = true;
		try {
			const coordinates = markers.map((m) => {
				const arr = m as [number, number];
				return [arr[0], arr[1]];
			});
			const res = await fetch(`${backendUrl}/api/route`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ coordinates })
			});
			if (!res.ok) {
				const err = await res.json().catch(() => ({}));
				console.error('Route error:', err);
				return;
			}
			const data = await res.json();
			if (!data.properties || !data.features?.length) {
				console.warn('Route returned no results');
				routeResult = null;
			} else {
				routeResult = data;
			}
		} catch (err) {
			console.error('Route fetch failed:', err);
		} finally {
			routeLoading = false;
		}
	}

	function clearRoute() {
		markers = [];
		routeResult = null;
	}

	// --- Bbox drawing state (for job creation) ---
	let drawingMode = $state<'none' | 'first-click' | 'second-click' | 'done'>('none');
	let bboxStart = $state<[number, number] | null>(null);
	let bboxEnd = $state<[number, number] | null>(null);
	let mousePos = $state<[number, number] | null>(null);

	function onStartDrawing() {
		drawingMode = 'first-click';
		bboxStart = null;
		bboxEnd = null;
		mousePos = null;
	}

	function onCancelDrawing() {
		drawingMode = 'none';
		bboxStart = null;
		bboxEnd = null;
		mousePos = null;
	}

	function handleMapMouseMove(e: maplibregl.MapMouseEvent) {
		if (drawingMode === 'second-click') {
			mousePos = [e.lngLat.lng, e.lngLat.lat];
		}
	}

	// Live preview of the bounding box being drawn
	let bboxGeoJSON: GeoJSON.FeatureCollection = $derived.by(() => {
		const end = bboxEnd ?? (drawingMode === 'second-click' ? mousePos : null);
		if (!bboxStart || !end) return emptyGeoJSON;
		const minLon = Math.min(bboxStart[0], end[0]);
		const maxLon = Math.max(bboxStart[0], end[0]);
		const minLat = Math.min(bboxStart[1], end[1]);
		const maxLat = Math.max(bboxStart[1], end[1]);
		return {
			type: 'FeatureCollection',
			features: [
				{
					type: 'Feature',
					geometry: {
						type: 'Polygon',
						coordinates: [
							[
								[minLon, minLat],
								[maxLon, minLat],
								[maxLon, maxLat],
								[minLon, maxLat],
								[minLon, minLat]
							]
						]
					},
					properties: {}
				}
			]
		};
	});

	// Determine which side is recommended from the route result
	let recommendedSide: string = $derived.by(() => {
		if (!routeResult?.properties) return '';
		const avg = routeResult.properties.avg_relative_value;
		if (avg > 0) return 'right';
		if (avg < 0) return 'left';
		return 'equal';
	});
</script>

<div class="map-container" class:drawing-mode={drawingMode !== 'none'}>
	<MapLibre
		class="h-screen"
		style={mapStyle}
		attributionControl={false}
		center={[10.0539, 46.5645]}
		zoom={9}
		onclick={handleMapClick}
		onmousemove={handleMapMouseMove}
	>
		<AttributionControl compact={true} position="bottom-right" customAttribution={attribution}
		></AttributionControl>

		<!-- Waypoint markers -->
		{#each markers as lnglat, i (i)}
			<Marker {lnglat} />
		{/each}

		<!-- Existing vector tile layers (scenicness overview) -->
		<VectorTileSource tiles={vectorTiles}>
			<LineLayer
				layout={{ visibility: scenicnessVisibility }}
				paint={{
					'line-opacity': 1,
					'line-color': 'white',
					'line-width': 2
				}}
				sourceLayer="osm_ways"
			/>
			<LineLayer
				layout={{ visibility: scenicnessVisibility }}
				paint={scenicLinePaint('white', scenicColor)}
				sourceLayer="scenicness_segments"
			/>
			<ImageLoader images={{ arrow: arrowImageUrl }}>
				<SymbolLayer
					layout={{
						visibility: scenicnessVisibility,
						'symbol-placement': 'line-center',
						'icon-image': 'arrow',
						'icon-rotation-alignment': 'map',
						'icon-rotate': arrowRotation,
						'icon-size': 0.1
					}}
					filter={notTunnelFilter}
					minzoom={10}
					sourceLayer="scenicness_segments"
				/>
			</ImageLoader>
		</VectorTileSource>

		<!-- Bbox drawing preview -->
		<GeoJSONSource data={bboxGeoJSON}>
			<FillLayer paint={{ 'fill-color': '#4488ff', 'fill-opacity': 0.15 }} />
			<LineLayer
				paint={{ 'line-color': '#4488ff', 'line-width': 2, 'line-dasharray': [4, 2] }}
			/>
		</GeoJSONSource>

		<!-- Route result layers (blue style) -->
		<GeoJSONSource data={routeGeoJSON}>
			<LineLayer
				paint={{ ...scenicLinePaint('#b3d4fc', routeColor), 'line-opacity': scenicInterp(0.3, 1) }}
			/>
			<ImageLoader images={{ arrow_blue: arrowBlueImageUrl, arrow_reverse: arrowReverseImageUrl }}>
				<SymbolLayer
					layout={{
						'symbol-placement': 'line-center',
						'icon-image': 'arrow_blue',
						'icon-rotation-alignment': 'map',
						'icon-rotate': ['*', arrowRotation, ['case', ['get', 'flipped'], -1, 1]],
						'icon-size': 0.1
					}}
					filter={notTunnelFilter}
					minzoom={10}
				/>
				<SymbolLayer
					layout={{
						'icon-image': 'arrow_reverse',
						'icon-rotation-alignment': 'map',
						'icon-rotate': ['get', 'heading'],
						'icon-size': 0.15,
						'icon-allow-overlap': true
					}}
					filter={['==', ['get', 'type'], 'reversal']}
				/>
			</ImageLoader>
		</GeoJSONSource>
	</MapLibre>

	<!-- Job management panel -->
	<JobPanel
		{backendUrl}
		{drawingMode}
		{bboxStart}
		{bboxEnd}
		{onStartDrawing}
		{onCancelDrawing}
	/>

	<!-- UI overlay -->
	{#if markers.length > 0}
		<div class="overlay-panel">
			{#if routeLoading}
				<p class="text-sm text-gray-300">Computing route...</p>
			{:else if routeResult}
				<p class="text-sm text-white mb-1">
					Sit on the
					<strong class="text-blue-300">
						{recommendedSide === 'right'
							? 'right'
							: recommendedSide === 'left'
								? 'left'
								: 'either'}
					</strong>
					side
				</p>
				<p class="text-xs text-gray-400">
					{(routeResult.properties.total_length_m / 1000).toFixed(1)} km &middot; avg scenic: {routeResult.properties.avg_total_value.toFixed(3)}
				</p>
			{:else}
				<p class="text-sm text-gray-300">
					{markers.length} marker{markers.length !== 1 ? 's' : ''} placed &mdash; click to add
					more
				</p>
			{/if}
			<button
				class="mt-2 rounded bg-red-600 px-3 py-1 text-xs text-white hover:bg-red-500"
				onclick={clearRoute}
			>
				Clear
			</button>
		</div>
	{/if}
</div>

<style>
	.map-container {
		background: black;
		position: relative;
	}

	/* Crosshair cursor while drawing a bounding box */
	.drawing-mode :global(.maplibregl-canvas) {
		cursor: crosshair !important;
	}

	.overlay-panel {
		position: absolute;
		top: 10px;
		right: 10px;
		background: rgba(0, 0, 0, 0.75);
		padding: 12px 16px;
		border-radius: 8px;
		z-index: 10;
		backdrop-filter: blur(4px);
	}
</style>
