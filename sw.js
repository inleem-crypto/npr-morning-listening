const CACHE='npr-listening-v15';
const ASSETS=['./','./index.html','./lesson-status.js','./summary-review.js','./manifest.webmanifest','./icon.svg'];
const shellPaths=new Set(ASSETS.map(path=>new URL(path,self.registration.scope).href));
self.addEventListener('install',event=>{
  event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(ASSETS)).then(()=>self.skipWaiting()));
});
self.addEventListener('activate',event=>{
  event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key.startsWith('npr-listening-')&&key!==CACHE).map(key=>caches.delete(key)))).then(()=>self.clients.claim()));
});
self.addEventListener('fetch',event=>{
  const request=event.request;
  if(request.method!=='GET'||request.headers.has('Range')||!shellPaths.has(request.url))return;
  event.respondWith(fetch(request).then(async response=>{
    if(response.ok){const cache=await caches.open(CACHE);await cache.put(request,response.clone()).catch(()=>{})}
    return response;
  }).catch(()=>caches.match(request)));
});
