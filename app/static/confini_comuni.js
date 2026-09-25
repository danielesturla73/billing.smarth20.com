// Strato "Confini comuni (ISTAT)" per le mappe Leaflet (Daniele, 25/09/2026):
// si attiva dal controllo dei livelli e si scarica solo alla prima attivazione
// (il file dei comuni pesa ~1,6 MB). Linee tratteggiate e nome del comune al
// centro; non intercetta i clic, cosi' prese e distretti restano cliccabili.
function stratoConfiniComuni(mappa) {
  const strato = L.layerGroup();
  const etichette = L.layerGroup();
  let caricato = false;
  function aggiornaEtichette() {
    if (!mappa.hasLayer(strato)) return;
    if (mappa.getZoom() >= 11) etichette.addTo(mappa); else etichette.remove();
  }
  mappa.on('overlayadd', async function (e) {
    if (e.layer !== strato) return;
    if (!caricato) {
      caricato = true;
      const risposta = await fetch('/mappe/confini-comuni.geojson');
      if (!risposta.ok) { caricato = false; alert('Confini dei comuni non disponibili'); return; }
      const dati = await risposta.json();
      L.geoJSON(dati, {
        interactive: false,
        style: { color: '#7A3E9D', weight: 2, dashArray: '6 4', fill: false },
        onEachFeature: function (f, layer) {
          const p = f.properties;
          L.tooltip({ permanent: true, direction: 'center', className: 'etichetta-comune', interactive: false })
            .setLatLng(layer.getBounds().getCenter())
            .setContent(p.provincia ? `${p.comune} (${p.provincia})` : p.comune)
            .addTo(etichette);
        },
      }).addTo(strato);
    }
    aggiornaEtichette();
  });
  mappa.on('overlayremove', function (e) { if (e.layer === strato) etichette.remove(); });
  mappa.on('zoomend', aggiornaEtichette);
  return strato;
}
