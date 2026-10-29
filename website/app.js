document.addEventListener('DOMContentLoaded', () => {
    const url = '[https://corsproxy.io/](https://corsproxy.io/)?' + encodeURIComponent('[https://iptv-org.github.io/iptv/languages/spa.m3u](https://iptv-org.github.io/iptv/languages/spa.m3u)');
    const channelList = document.getElementById('channel-list');
    const videoPlayer = document.getElementById('video-player');

    async function loadChannels() {
        try {
            const response = await fetch(url);
            const text = await response.text();
            parseM3U(text);
        } catch (err) {
            console.error('Error fetching IPTV list:', err);
        }
    }

    function parseM3U(data) {
        const lines = data.split('\n');
        let channels = [];
        let currentChannel = {};

        for (let line of lines) {
            line = line.trim();
            if (line.startsWith('#EXTINF:')) {
                const tvgNameMatch = line.match(/tvg-name="([^"]*)"/);
                const logoMatch = line.match(/tvg-logo="([^"]*)"/);
                const commaIndex = line.lastIndexOf(',');
                const name = commaIndex !== -1 ? line.substring(commaIndex + 1).trim() : 'Channel';
                
                currentChannel = {
                    name: tvgNameMatch ? tvgNameMatch[1] : name,
                    logo: logoMatch ? logoMatch[1] : '',
                };
            } else if (line && !line.startsWith('#')) {
                currentChannel.url = line;
                channels.push(currentChannel);
                currentChannel = {};
            }
        }
        renderChannels(channels);
    }

    function renderChannels(channels) {
        if (!channelList) return;
        channelList.innerHTML = '';
        channels.forEach(ch => {
            const item = document.createElement('div');
            item.className = 'channel-item';
            item.textContent = ch.name;
            item.onclick = () => {
                if (videoPlayer) {
                    videoPlayer.src = ch.url;
                    videoPlayer.play();
                }
            };
            channelList.appendChild(item);
        });
    }

    loadChannels();
});