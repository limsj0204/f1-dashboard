// 오프라인 캐싱 전략:
// - index.html / manifest.json은 "네트워크 우선" -> 항상 최신 데이터를 받아오고,
//   오프라인일 때만 캐시된 걸 대신 보여줌
// - 폰트, 아이콘처럼 거의 안 바뀌는 파일은 "캐시 우선" -> 빠르고 데이터 절약
const CACHE_NAME = "pitwall-v1";

const NETWORK_FIRST_FILES = ["index.html", "manifest.json"];

const CACHE_ASSETS = [
  "./",
  "./index.html",
  "./manifest.json",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
  "./icons/icon-apple-touch.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(CACHE_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

function isNetworkFirst(pathname) {
  return NETWORK_FIRST_FILES.some((f) => pathname.endsWith(f)) || pathname.endsWith("/");
}

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return; // let cross-origin (fonts CDN) pass through normally

  if (isNetworkFirst(url.pathname)) {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          return response;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => {
      return (
        cached ||
        fetch(event.request).then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          return response;
        })
      );
    })
  );
});
