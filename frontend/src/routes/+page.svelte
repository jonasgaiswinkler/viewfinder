<script lang="ts">
	import {
		AttributionControl,
		ImageLoader,
		LineLayer,
		MapLibre,
		SymbolLayer,
		VectorTileSource
	} from 'svelte-maplibre-gl';
	import { env } from '$env/dynamic/public';

	import arrowImageUrl from '$lib/assets/arrow.png';

	let mapStyle =
		'https://api.maptiler.com/maps/019c19fe-40db-76a8-a85c-b8a711d9f632/style.json?key=OdQtdfaWeZHG7a1SfGoY';
	let attribution = `<a href="https://www.jonasgaiswinkler.eu/imprint" target="_blank">Imprint</a>`;

	let vectorTiles = [`${env.PUBLIC_TEGOLA_URL}/maps/viewfinder/{z}/{x}/{y}`];
</script>

<div class="map-container">
	<MapLibre
		class="h-screen"
		style={mapStyle}
		attributionControl={false}
		center={[10.0539, 46.5645]}
		zoom={9}
	>
		<AttributionControl compact={true} position="bottom-right" customAttribution={attribution}
		></AttributionControl>
		<VectorTileSource tiles={vectorTiles}>
			<LineLayer
				paint={{
					'line-opacity': 1,
					'line-color': 'white',
					'line-width': 2
				}}
				sourceLayer={'osm_ways'}
			/>
			<LineLayer
				paint={{
					'line-opacity': [
						'interpolate',
						['linear'],
						['get', 'start_total_value'],
						0.005,
						0,
						0.015,
						1
					],
					'line-color': [
						'interpolate',
						['linear'],
						['get', 'start_total_value'],
						0.005,
						'white',
						0.015,
						'red'
					],
					'line-width': [
						'interpolate',
						['linear'],
						['get', 'start_total_value'],
						0.005,
						2,
						0.015,
						5
					]
				}}
				sourceLayer={'scenicness_segments'}
			/>
			<ImageLoader images={{ arrow: arrowImageUrl }}
				><SymbolLayer
					layout={{
						'symbol-placement': 'line',
						'symbol-spacing': 25,
						'icon-image': 'arrow',
						'icon-rotation-alignment': 'map',
						'icon-rotate': [
							'case',
							['>', ['get', 'start_relative_value'], 0],
							90,
							-90
						],
						'icon-size': 0.1
					}}
					minzoom={17}
					sourceLayer={'scenicness_segments'}
				/><SymbolLayer
					layout={{
						'symbol-placement': 'line-center',
						'icon-image': 'arrow',
						'icon-rotation-alignment': 'map',
						'icon-rotate': [
							'case',
							['>', ['get', 'start_relative_value'], 0],
							90,
							-90
						],
						'icon-size': 0.1
					}}
					minzoom={10}
					maxzoom={17}
					sourceLayer={'scenicness_segments'}
				/></ImageLoader
			>
		</VectorTileSource>
	</MapLibre>
</div>

<style>
	.map-container {
		background: black;
	}
</style>
