export const metadata = {
  title: 'IPTV Next App',
  description: 'IPTV Streaming Hub built with Next.js',
}

export default function RootLayout({ children }) {
  return (
    <html lang="es">
      <body>{children}</body>
    </html>
  )
}
