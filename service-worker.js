const CACHE='wafr-shell-v1';
const SHELL=['/app','/manifest.webmanifest','/app-icon-180.png','/app-icon-192.png','/app-icon-512.png'];

self.addEventListener('install',event=>{
  event.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)).catch(()=>{}));
  self.skipWaiting();
});

self.addEventListener('activate',event=>{
  event.waitUntil(
    caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener('fetch',event=>{
  const req=event.request;
  const url=new URL(req.url);

  if(req.method!=='GET' || url.origin!==self.location.origin || url.pathname.startsWith('/api/')){
    return;
  }

  if(url.pathname==='/app' || url.pathname==='/wafr'){
    event.respondWith(fetch(req).catch(()=>caches.match('/app')));
    return;
  }

  if(SHELL.includes(url.pathname)){
    event.respondWith(caches.match(req).then(cached=>cached||fetch(req)));
  }
});
