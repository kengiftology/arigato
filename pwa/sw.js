self.addEventListener("push", (e) => {
  const data = e.data ? e.data.json() : { title: "ありがとう", body: "ありがとうが届きました 🌱" };
  e.waitUntil((async () => {
    // 開いているタブにも届ける（フォアグラウンドではアプリ内トーストで表示）
    const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    clients.forEach(c => c.postMessage({ type: "push", title: data.title, body: data.body }));
    await self.registration.showNotification(data.title, {
      body: data.body,
      icon: "/icon.png",
    });
  })());
});
