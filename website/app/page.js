'use client';

import { useState, useEffect, useRef } from 'react';

const CATEGories = [
  { id: 'spa', name: 'Canales TV', url: '[https://iptv-org.github.io/iptv/languages/spa.m3u](https://iptv-org.github.io/iptv/languages/spa.m3u)' },
  { id: 'movies', name: 'Películas', url: '[https://iptv-org.github.io/iptv/categories/movies.m3u](https://iptv-org.github.io/iptv/categories/movies.m3u)' },
  { id: 'series', name: 'Series', url: '[https://iptv-org.github.io/iptv/categories/series.m3u](https://iptv-org.github.io/iptv/categories/series.m3u)' },
  { id: 'animation', name: 'Anime & Animación', url: '[https://iptv-org.github.io/iptv/categories/animation.m3u](https://iptv-org.github.io/iptv/categories/animation.m3u)' }
];

export default function Home() {
  const [activeCategory, setActiveCategory] = useState('spa');
  const [items, setItems] = useState([]);
  const [filteredItems, setFilteredItems] = useState([]);
  const [currentItem, setCurrentItem] = useState(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [loading, setLoading] = useState(false);
  const videoRef = useRef(null);

  useEffect(() => {
    loadCategory(activeCategory);
  }, [activeCategory]);

  async function loadCategory(catId) {
    setLoading(true);
    const cat = CATEGories.find(c => c.id === catId);
    if (!cat) return;

    try {
      const proxyUrl = '[https://corsproxy.io/](https://corsproxy.io/)?' + encodeURIComponent(cat.url);
      const response = await fetch(proxyUrl);
      const text = await response.text();
      parseM3U(text);
    } catch (err) {
      console.error('Error fetching playlist:', err);
      setItems([]);
      setFilteredItems([]);
    } finally {
      setLoading(false);
    }
  }

  function parseM3U(data) {
    const lines = data.split('\n');
    let parsed = [];
    let current = {};

    for (let line of lines) {
      line = line.trim();
      if (line.startsWith('#EXTINF:')) {
        const tvgNameMatch = line.match(/tvg-name="([^"]*)"/);
        const logoMatch = line.match(/tvg-logo="([^"]*)"/);
        const commaIndex = line.lastIndexOf(',');
        const name = commaIndex !== -1 ? line.substring(commaIndex + 1).trim() : 'Contenido';
        
        current = {
          name: tvgNameMatch ? tvgNameMatch[1] : name,
          logo: logoMatch ? logoMatch[1] : '',
        };
      } else if (line && !line.startsWith('#')) {
        current.url = line;
        parsed.push(current);
        current = {};
      }
    }
    setItems(parsed);
    setFilteredItems(parsed);
  }

  useEffect(() => {
    const filtered = items.filter(item => 
      item.name && typeof item.name === 'string' && item.name.toLowerCase().includes(searchTerm.toLowerCase())
    );
    setFilteredItems(filtered);
  }, [searchTerm, items]);

  const selectItem = (item) => {
    setCurrentItem(item);
    if (videoRef.current) {
      videoRef.current.src = item.url;
      videoRef.current.play().catch(e => console.log('Playback error:', e));
    }
  };

  return (
    <div style={{ fontFamily: 'sans-serif', background: '#0f1015', color: '#fff', minHeight: '100vh', padding: '20px' }}>
      <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px', borderBottom: '1px solid #2a2b36', paddingBottom: '15px', flexWrap: 'wrap', gap: '15px' }}>
        <h1 style={{ margin: 0, fontSize: '24px', color: '#e50914', fontWeight: 'bold' }}>Zimflix IPTV & Anime Hub</h1>
        <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
          {CATEGories.map(cat => (
            <button
              key={cat.id}
              onClick={() => setActiveCategory(cat.id)}
              style={{
                padding: '8px 16px',
                borderRadius: '6px',
                border: 'none',
                background: activeCategory === cat.id ? '#e50914' : '#22232f',
                color: '#fff',
                cursor: 'pointer',
                fontWeight: '600',
                transition: 'background 0.2s'
              }}
            >
              {cat.name}
            </button>
          ))}
        </div>
        <input 
          type="text" 
          placeholder="Buscar películas, series, anime, canales..." 
          value={searchTerm}
          onChange={(e) => setSearchTerm(e.target.value)}
          style={{ padding: '10px 14px', borderRadius: '6px', border: '1px solid #3a3b4c', background: '#181922', color: '#fff', width: '280px', outline: 'none' }}
        />
      </header>

      <main style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: '20px' }}>
        <section>
          <div style={{ background: '#000', borderRadius: '12px', overflow: 'hidden', marginBottom: '15px', aspectRatio: '16/9', display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 8px 24px rgba(0,0,0,0.5)' }}>
            <video 
              ref={videoRef} 
              controls 
              style={{ width: '100%', height: '100%' }}
            />
          </div>
          <h2 style={{ margin: '0 0 5px 0', fontSize: '20px' }}>{currentItem ? currentItem.name : 'Selecciona un contenido para reproducir'}</h2>
          {currentItem?.logo && (
            <img src={currentItem.logo} alt="" style={{ height: '30px', marginTop: '5px', objectFit: 'contain' }} onError={(e) => e.target.style.display = 'none'} />
          )}
        </section>

        <section style={{ background: '#161722', borderRadius: '12px', padding: '15px', maxHeight: '75vh', overflowY: 'auto', boxShadow: '0 4px 12px rgba(0,0,0,0.3)' }}>
          <h3 style={{ margin: '0 0 15px 0', fontSize: '18px', borderBottom: '1px solid #2a2b36', paddingBottom: '10px' }}>
            {loading ? 'Cargando catálogos...' : `${CATEGories.find(c => c.id === activeCategory)?.name} (${filteredItems.length})`}
          </h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {loading ? (
              <p style={{ color: '#888', textAlign: 'center', padding: '20px' }}>Cargando lista de streaming...</p>
            ) : filteredItems.length === 0 ? (
              <p style={{ color: '#888', textAlign: 'center', padding: '20px' }}>No se encontraron resultados.</p>
            ) : (
              filteredItems.map((item, idx) => (
                <div 
                  key={idx} 
                  onClick={() => selectItem(item)}
                  style={{
                    padding: '12px', 
                    background: currentItem?.url === item.url ? '#e50914' : '#22232f', 
                    borderRadius: '8px', 
                    cursor: 'pointer', 
                    fontSize: '14px', 
                    transition: 'all 0.2s',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '10px',
                    boxShadow: currentItem?.url === item.url ? '0 4px 12px rgba(229,9,20,0.4)' : 'none'
                  }}
                >
                  {item.logo ? (
                    <img src={item.logo} alt="" style={{ width: '24px', height: '24px', objectFit: 'contain', borderRadius: '4px' }} onError={(e) => e.target.style.display = 'none'} />
                  ) : null}
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.name}</span>
                </div>
              ))
            )}
          </div>
        </section>
      </main>
    </div>
  );
}
