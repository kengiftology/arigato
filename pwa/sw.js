self.addEventListener("push", (e) => {
  const data = e.data ? e.data.json() : { title: "arigato", body: "ありがとうが届きました 🌱" };
  e.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: "/icon.png",
    })
  );
});
